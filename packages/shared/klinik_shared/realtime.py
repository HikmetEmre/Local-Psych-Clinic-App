"""Live updates: Postgres LISTEN/NOTIFY fanned out to browsers as Server-Sent Events.

One LISTEN connection per app process. A broadcast channel (e.g. "schedule_changed")
becomes an event for every open tab; a per-user channel (e.g. "new_notification",
payload = user id) only for that user's tabs. NOTIFY is only a hint to refresh:
nothing depends on it for correctness, and a lost event costs one stale view until
the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
from fastapi.responses import StreamingResponse

log = logging.getLogger("klinik_shared.realtime")

HEARTBEAT_SECONDS = 20.0
RECONNECT_SECONDS = 5.0
QUEUE_SIZE = 50


def asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


@dataclass
class _Subscriber:
    user_id: UUID
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))


class Hub:
    def __init__(
        self,
        dsn: str,
        *,
        broadcast: Mapping[str, str],
        per_user: Mapping[str, str] | None = None,
    ) -> None:
        """``broadcast`` / ``per_user``: Postgres channel -> SSE event name."""
        self.dsn = dsn
        self.broadcast = dict(broadcast)
        self.per_user = dict(per_user or {})
        self._subscribers: dict[int, _Subscriber] = {}
        self._ids = itertools.count()
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    # ------------------------------------------------------------ fan-out

    def _on_notify(self, _conn: Any, _pid: int, channel: str, payload: str) -> None:
        if channel in self.broadcast:
            self.publish(self.broadcast[channel])
        elif channel in self.per_user:
            try:
                user = UUID(payload)
            except ValueError:
                return
            self.publish(self.per_user[channel], user_id=user)

    def publish(self, event: str, *, user_id: UUID | None = None) -> None:
        frame = f"event: {event}\ndata: {event}\n\n"
        for sub in list(self._subscribers.values()):
            if user_id is not None and sub.user_id != user_id:
                continue
            with contextlib.suppress(asyncio.QueueFull):
                # A tab that is not reading only misses refresh hints.
                sub.queue.put_nowait(frame)

    # ------------------------------------------------------------ connection

    async def _listen_forever(self) -> None:
        channels = [*self.broadcast, *self.per_user]
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self.dsn)
                for channel in channels:
                    await conn.add_listener(channel, self._on_notify)
                self._connected.set()
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _c, ev=lost: ev.set())
                await lost.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("realtime listener down (%s); retrying", type(exc).__name__)
            finally:
                self._connected.clear()
                if conn is not None and not conn.is_closed():
                    with contextlib.suppress(Exception):
                        await conn.close()
            await asyncio.sleep(RECONNECT_SECONDS)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen_forever(), name="realtime-hub")

    async def wait_connected(self) -> None:
        await self._connected.wait()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # ------------------------------------------------------------ streams

    @contextlib.contextmanager
    def subscribe(self, user_id: UUID) -> Any:
        key = next(self._ids)
        sub = _Subscriber(user_id)
        self._subscribers[key] = sub
        try:
            yield sub.queue
        finally:
            self._subscribers.pop(key, None)

    async def stream(self, user_id: UUID) -> AsyncIterator[str]:
        self.start()
        with self.subscribe(user_id) as queue:
            yield "retry: 5000\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"

    def response(self, user_id: UUID) -> StreamingResponse:
        return StreamingResponse(
            self.stream(user_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
