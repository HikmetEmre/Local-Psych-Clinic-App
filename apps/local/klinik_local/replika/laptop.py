"""Laptop side of the copy: send local edits, then fetch everything new from the office.

Runs inside the laptop's app every minute (and on "Şimdi eşitle"). When the office is
not reachable nothing happens and work continues on the local copy; edits wait in
replica_outbox. The office's certificate is pinned: the laptop talks to that office only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from klinik_local.replika import store
from klinik_local.replika.store import BY_NAME, FULL, FULL_REPLACE, Row
from klinik_shared.timeutil import now_utc

log = logging.getLogger("klinik.replika")
BATCH = 500
TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class OfficeUnreachable(Exception):
    pass


class OfficeRefused(Exception):
    pass


@dataclass
class Status:
    """What the laptop's pages show. Kept in memory and in replica_state."""

    last_ok: datetime | None = None
    last_attempt: datetime | None = None
    last_error: str | None = None
    pending: int = 0
    sent: int = 0
    received: int = 0
    conflicts: int = 0
    running: bool = False
    history: list[str] = field(default_factory=list)

    @property
    def online(self) -> bool:
        return self.last_error is None and self.last_ok is not None


def pinned_context(cert_pem: str) -> ssl.SSLContext:
    """Trust exactly the office's own certificate, whatever name the laptop used."""
    context = ssl.create_default_context(cadata=cert_pem)
    context.check_hostname = False  # the certificate itself is pinned instead
    context.verify_mode = ssl.CERT_REQUIRED
    return context


async def get_state(conn: AsyncConnection) -> dict[str, str]:
    rows = (await conn.execute(text("SELECT key, value FROM replica_state"))).all()
    return {r[0]: r[1] for r in rows}


async def set_state(conn: AsyncConnection, **values: str) -> None:
    for key, value in values.items():
        await conn.execute(
            text(
                "INSERT INTO replica_state (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            ),
            {"k": key, "v": value},
        )


class ReplicaSync:
    def __init__(
        self,
        engine: AsyncEngine,
        office_url: str,
        token: str,
        cert_pem: str | None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.engine = engine
        self.office_url = office_url.rstrip("/")
        self.token = token
        self.cert_pem = cert_pem
        self.transport = transport
        self.status = Status()
        self.wake = asyncio.Event()
        self._lock = asyncio.Lock()

    def client(self) -> httpx.AsyncClient:
        verify: Any = pinned_context(self.cert_pem) if self.cert_pem else True
        return httpx.AsyncClient(
            base_url=f"{self.office_url}/replika/api",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=TIMEOUT,
            verify=verify,
            transport=self.transport,
        )

    async def _post(self, client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> Any:
        try:
            response = await client.post(path, json=body)
        except httpx.TransportError as exc:
            raise OfficeUnreachable(str(exc)) from exc
        if response.status_code == 401:
            raise OfficeRefused("Ofis bu dizüstünü tanımıyor (eşleştirme iptal edilmiş olabilir).")
        if response.status_code != 200:
            raise OfficeRefused(f"Ofis hata verdi ({response.status_code}).")
        return response.json()

    # ------------------------------------------------------------ push

    async def _collect(
        self, conn: AsyncConnection
    ) -> tuple[list[dict[str, Any]], list[tuple[str, UUID, int]], list[Row]]:
        queued = (
            await conn.execute(
                text("SELECT tbl, row_id, gen FROM replica_outbox ORDER BY queued_at LIMIT :n"),
                {"n": BATCH},
            )
        ).all()
        bases = {
            (r[0], r[1]): int(r[2])
            for r in (
                await conn.execute(text("SELECT tbl, row_id, office_seq FROM replica_base"))
            ).all()
        }
        items: list[dict[str, Any]] = []
        sent: list[tuple[str, UUID, int]] = []
        for tbl, row_id, gen in queued:
            if tbl not in BY_NAME:
                continue
            rows = await store.select_rows(conn, tbl, "WHERE id = :id", {"id": row_id})
            if not rows:
                continue
            item: dict[str, Any] = {
                "t": tbl, "id": str(row_id), "base": bases.get((tbl, row_id)),
                "row": store.encode_row(rows[0]),
            }  # fmt: skip
            if tbl == "therapy_sessions":
                item["methods"] = [
                    store.encode_row(m) for m in await store.session_methods(conn, row_id)
                ]
            items.append(item)
            sent.append((tbl, row_id, int(gen)))
        cursor = int((await get_state(conn)).get("audit_sent", "0"))
        audit = await store.select_rows(
            conn, "audit_log", "WHERE id > :c ORDER BY id LIMIT :n", {"c": cursor, "n": BATCH}
        )
        return items, sent, audit

    async def push(self, client: httpx.AsyncClient) -> int:
        total = 0
        while True:
            async with self.engine.begin() as conn:
                items, sent, audit = await self._collect(conn)
            if not items and not audit:
                return total
            body = {
                "items": items,
                "audit": [
                    store.encode_row({k: v for k, v in a.items() if k != "id"}) for a in audit
                ],
            }
            data = await self._post(client, "/push", body)
            gens = {(t, i): g for t, i, g in sent}
            async with self.engine.begin() as conn:
                await store.applying(conn)
                for result in data["results"]:
                    await self._settle(conn, result, gens)
                if audit:
                    await set_state(conn, audit_sent=str(audit[-1]["id"]))
            total += len(items)
            if len(items) < BATCH and len(audit) < BATCH:
                return total

    async def _settle(
        self, conn: AsyncConnection, result: dict[str, Any], gens: dict[tuple[str, UUID], int]
    ) -> None:
        table, row_id = result["t"], UUID(result["id"])
        if table not in BY_NAME:
            return
        spec = BY_NAME[table]
        gen = gens.get((table, row_id))
        still_same = (
            await conn.execute(
                text(
                    "DELETE FROM replica_outbox WHERE tbl = :t AND row_id = :id AND gen = :g "
                    "RETURNING 1"
                ),
                {"t": table, "id": row_id, "g": gen},
            )
        ).first() is not None
        if result["result"] == "rejected":
            # The office refused a row made here; its data is kept in the office's
            # conflict list. Remove it locally (and what hangs on it).
            if table == "appointments":
                await conn.execute(
                    text(
                        "UPDATE therapy_sessions SET appointment_id = NULL "
                        "WHERE appointment_id = :id"
                    ),
                    {"id": row_id},
                )
                await conn.execute(
                    text("DELETE FROM payments WHERE appointment_id = :id"), {"id": row_id}
                )
            if table in ("appointments", "payments"):
                await conn.execute(text(f'DELETE FROM "{table}" WHERE id = :id'), {"id": row_id})
            await conn.execute(
                text("DELETE FROM replica_base WHERE tbl = :t AND row_id = :id"),
                {"t": table, "id": row_id},
            )
            if result["result"] == "rejected":
                self.status.conflicts += 1
            return
        if result["result"] == "conflict":
            self.status.conflicts += 1
        office = result.get("office")
        if office is None or not still_same:
            return  # edited again meanwhile: the next round sends the newer version
        row = store.decode_row(office)
        await store.upsert(conn, table, spec.key, row)
        if table == "therapy_sessions":
            await store.replace_methods(
                conn, row_id, [store.decode_row(m) for m in result.get("methods") or []]
            )
        if result.get("seq") is not None:
            await self._set_base(conn, table, row_id, int(result["seq"]))

    @staticmethod
    async def _set_base(conn: AsyncConnection, table: str, row_id: UUID, seq: int) -> None:
        await conn.execute(
            text(
                "INSERT INTO replica_base (tbl, row_id, office_seq) VALUES (:t, :id, :s) "
                "ON CONFLICT (tbl, row_id) DO UPDATE SET office_seq = EXCLUDED.office_seq"
            ),
            {"t": table, "id": row_id, "s": seq},
        )

    # ------------------------------------------------------------ pull

    async def pull(self, client: httpx.AsyncClient) -> int:
        async with self.engine.begin() as conn:
            since = int((await get_state(conn)).get("cursor", "0"))
        full: dict[str, list[Any]] | None = None
        rows: list[dict[str, Any]] = []
        cursor = since
        first = True
        while True:
            page = await self._post(client, "/pull", {"since": cursor, "full": first})
            if first:
                full = page["full"]
                first = False
            rows += page["rows"]
            cursor = int(page["next"])
            if not page["more"]:
                break
        async with self.engine.begin() as conn:
            await store.applying(conn)
            if full is not None:
                await self._apply_full(conn, full)
            waiting = {
                (r[0], r[1])
                for r in (await conn.execute(text("SELECT tbl, row_id FROM replica_outbox"))).all()
            }
            order = {spec.name: i for i, spec in enumerate(store.SPECS)}
            for item in sorted(rows, key=lambda it: (order[it["t"]], it["seq"])):
                table = item["t"]
                row = store.decode_row(item["row"])
                if (table, row["id"]) in waiting:
                    continue  # edited here and not sent yet: the next push decides
                await store.upsert(conn, table, BY_NAME[table].key, row)
                if table == "therapy_sessions":
                    await store.replace_methods(
                        conn, row["id"], [store.decode_row(m) for m in item.get("methods") or []]
                    )
                await self._set_base(conn, table, row["id"], int(item["seq"]))
            await store.raise_session_counters(conn)
            await set_state(conn, cursor=str(cursor))
        return len(rows)

    async def _apply_full(self, conn: AsyncConnection, full: dict[str, list[Any]]) -> None:
        for spec in FULL:
            incoming = [store.decode_row(r) for r in full.get(spec.name, [])]
            for row in incoming:
                await store.upsert(conn, spec.name, spec.key, row)
            if spec.name in FULL_REPLACE:
                keep = {tuple(row[k] for k in spec.key) for row in incoming}
                existing = await store.select_rows(conn, spec.name)
                for row in existing:
                    key = tuple(row[k] for k in spec.key)
                    if key not in keep:
                        where = " AND ".join(f'"{k}" = :k{i}' for i, k in enumerate(spec.key))
                        await conn.execute(
                            text(f'DELETE FROM "{spec.name}" WHERE {where}'),
                            {f"k{i}": v for i, v in enumerate(key)},
                        )

    # ------------------------------------------------------------ one round / loop

    async def run_once(self) -> Status:
        async with self._lock:
            self.status.running = True
            self.status.last_attempt = now_utc()
            try:
                async with self.client() as client:
                    sent = await self.push(client)
                    received = await self.pull(client)
                self.status.sent += sent
                self.status.received += received
                self.status.last_ok = now_utc()
                self.status.last_error = None
            except OfficeUnreachable:
                self.status.last_error = (
                    "Ofis bilgisayarına ulaşılamıyor (ofis ağında değilsiniz ya da ofis kapalı)."
                )
            except OfficeRefused as exc:
                self.status.last_error = str(exc)
            except Exception as exc:  # keep the loop alive; show what happened
                log.exception("replica sync failed")
                self.status.last_error = f"Eşitleme hatası: {type(exc).__name__}"
            finally:
                self.status.running = False
                async with self.engine.begin() as conn:
                    self.status.pending = int(
                        (
                            await conn.execute(text("SELECT count(*) FROM replica_outbox"))
                        ).scalar_one()
                    )
                    await set_state(
                        conn,
                        last_attempt=self.status.last_attempt.isoformat(),
                        last_ok=self.status.last_ok.isoformat() if self.status.last_ok else "",
                        last_error=self.status.last_error or "",
                    )
            return self.status

    async def mark_replica(self) -> None:
        """Turn on change tracking in this database (it is the laptop copy)."""
        async with self.engine.begin() as conn:
            await set_state(conn, mode="replica", office_url=self.office_url)
            state = await get_state(conn)
            self.status.pending = int(
                (await conn.execute(text("SELECT count(*) FROM replica_outbox"))).scalar_one()
            )
        if state.get("last_ok"):
            self.status.last_ok = datetime.fromisoformat(state["last_ok"])

    async def start(self, interval: float) -> None:
        await self.mark_replica()
        if interval > 0:
            await self.loop(interval)

    async def loop(self, interval: float) -> None:
        while True:
            await self.run_once()
            self.wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=interval)
