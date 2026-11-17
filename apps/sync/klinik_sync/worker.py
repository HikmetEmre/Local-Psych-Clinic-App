"""The sync worker: pull the cloud schedule into the mirror, push the outbox to the cloud.

Failure behaviour (brief §9):
- cloud or internet down: both loops back off (up to 5 min) and resume by themselves;
  the outbox keeps every item, so nothing is lost while the cloud is away;
- token refused or protocol mismatch: the worker stops and says so in sync_state,
  because retrying cannot fix either;
- an item the cloud refuses becomes a local conflict, never a silent drop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from klinik_shared.sync_protocol import PushRequest
from klinik_sync.cloud_client import (
    CloudAuthFailed,
    CloudClient,
    CloudError,
    CloudServerError,
    CloudUnavailable,
    ProtocolMismatch,
)
from klinik_sync.store import LocalStore

log = logging.getLogger("klinik_sync")

MAX_BACKOFF = 300.0


@dataclass
class Backoff:
    base: float = 2.0
    current: float = field(default=0.0)

    def fail(self) -> float:
        self.current = min(MAX_BACKOFF, self.current * 2 if self.current else self.base)
        return self.current

    def reset(self) -> None:
        self.current = 0.0


class SyncWorker:
    def __init__(
        self,
        store: LocalStore,
        cloud: CloudClient,
        *,
        pull_wait: int = 25,
        push_interval: float = 2.0,
    ) -> None:
        self.store = store
        self.cloud = cloud
        self.pull_wait = pull_wait
        self.push_interval = push_interval
        self.stopped = asyncio.Event()

    # ------------------------------------------------------------ single steps (used by tests)

    async def pull_once(self, *, wait: int = 0) -> int:
        """Fetch and apply changes until the cloud has nothing newer. Returns rows applied."""
        applied = 0
        while True:
            response = await self.cloud.changes(await self.store.cursor(), wait=wait)
            applied += await self.store.apply_changes(response)
            if not response.has_more:
                return applied
            wait = 0

    async def push_once(self) -> int:
        """Send queued items until the outbox is empty. Returns items the cloud answered."""
        answered = 0
        while True:
            batch = await self.store.next_batch()
            if not batch:
                return answered
            try:
                response = await self.cloud.push(PushRequest(items=[p.item for p in batch]))
            except CloudServerError as exc:
                await self.store.record_server_error(batch, str(exc))
                raise
            await self.store.record_results(batch, list(response.results))
            answered += len(response.results)

    async def reconcile(self) -> list[str]:
        codes = await self.cloud.client_codes()
        return await self.store.reconcile_codes([c.code for c in codes.codes])

    # ------------------------------------------------------------ loops

    async def _fatal(self, exc: CloudError) -> None:
        state = "auth_failed" if isinstance(exc, CloudAuthFailed) else "protocol_mismatch"
        log.error("sync stopped: %s", state)
        await self.store.set_state(worker_state=state, last_error=state)
        self.stopped.set()

    async def _loop(self, name: str, step: object, idle: float) -> None:
        backoff = Backoff()
        while not self.stopped.is_set():
            try:
                await step()  # type: ignore[operator]
                backoff.reset()
                delay = idle
            except (CloudAuthFailed, ProtocolMismatch) as exc:
                await self._fatal(exc)
                return
            except (CloudUnavailable, CloudServerError) as exc:
                delay = backoff.fail()
                log.warning("%s failed (%s); retrying in %.0f s", name, exc, delay)
                await self.store.set_state(last_error=f"{name}: {type(exc).__name__}")
            except Exception:
                delay = backoff.fail()
                log.exception("%s crashed; retrying in %.0f s", name, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopped.wait(), timeout=delay)

    async def run(self) -> None:
        await self.store.set_state(worker_state="running")
        try:
            await self.reconcile()
        except (CloudAuthFailed, ProtocolMismatch) as exc:
            await self._fatal(exc)
            return
        except CloudError as exc:
            log.warning("reconcile skipped: %s", exc)
        await asyncio.gather(
            self._loop("pull", lambda: self.pull_once(wait=self.pull_wait), 0.0),
            self._loop("push", self.push_once, self.push_interval),
        )

    def stop(self) -> None:
        self.stopped.set()
