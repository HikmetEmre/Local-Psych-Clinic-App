"""The worker's view of the office database, as role kasa_sync.

kasa_sync can touch the mirror, the outbox, sync_state and sync_conflicts, and call
reconcile_client_codes(). It cannot read clients or any clinical table; everything
it sends is already in the outbox payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from klinik_shared.enums import SyncReasonCode, SyncResult
from klinik_shared.sync_protocol import (
    MAX_PUSH_ITEMS,
    AppointmentChange,
    ChangesResponse,
    ItemResult,
    RoomChange,
    SyncItem,
)
from klinik_shared.timeutil import now_utc

MAX_SERVER_ERRORS = 10
_ITEM = TypeAdapter(SyncItem)

CONFLICT_KIND = {
    SyncReasonCode.UNKNOWN_APPOINTMENT: "sync_unknown_appointment",
    SyncReasonCode.APPOINTMENT_DELETED: "sync_unknown_appointment",
    SyncReasonCode.UNKNOWN_THERAPIST: "sync_error",
    SyncReasonCode.INVALID: "sync_error",
}


@dataclass(frozen=True)
class PendingItem:
    seq: int
    item: SyncItem

    @property
    def appointment_id(self) -> UUID | None:
        payload = self.item.payload
        return getattr(payload, "appointment_id", None)


class LocalStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    # ------------------------------------------------------------ state

    async def get_state(self) -> dict[str, str]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT key, value FROM sync_state"))).all()
        return {r.key: r.value for r in rows}

    @staticmethod
    async def _set(conn: AsyncConnection, **values: str) -> None:
        for key, value in values.items():
            await conn.execute(
                text(
                    "INSERT INTO sync_state (key, value, updated_at) VALUES (:k, :v, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
                ),
                {"k": key, "v": value},
            )

    async def set_state(self, **values: str) -> None:
        async with self.engine.begin() as conn:
            await self._set(conn, **values)

    async def cursor(self) -> int:
        return int((await self.get_state()).get("cursor", "0"))

    # ------------------------------------------------------------ pull

    async def apply_changes(self, response: ChangesResponse) -> int:
        """Upsert the mirror and move the cursor, in one transaction. Returns rows applied."""
        applied = 0
        async with self.engine.begin() as conn:
            for change in response.changes:
                if isinstance(change, AppointmentChange):
                    d = change.data
                    result = await conn.execute(
                        text(
                            "INSERT INTO appointments_mirror (id, client_code, therapist_id, "
                            "room_id, starts_at, duration_min, status, version, change_seq, "
                            "deleted_at, updated_at, synced_at) VALUES (:id, :code, :therapist, "
                            ":room, :starts, :duration, :status, :version, :seq, :deleted, "
                            ":updated, now()) ON CONFLICT (id) DO UPDATE SET "
                            "client_code = EXCLUDED.client_code, "
                            "therapist_id = EXCLUDED.therapist_id, room_id = EXCLUDED.room_id, "
                            "starts_at = EXCLUDED.starts_at, duration_min = EXCLUDED.duration_min, "
                            "status = EXCLUDED.status, version = EXCLUDED.version, "
                            "change_seq = EXCLUDED.change_seq, deleted_at = EXCLUDED.deleted_at, "
                            "updated_at = EXCLUDED.updated_at, synced_at = now() "
                            # Never let an older version overwrite a newer one.
                            "WHERE appointments_mirror.change_seq < EXCLUDED.change_seq"
                        ),
                        {
                            "id": d.id,
                            "code": d.client_code,
                            "therapist": d.therapist_id,
                            "room": d.room_id,
                            "starts": d.starts_at,
                            "duration": d.duration_min,
                            "status": d.status.value,
                            "version": d.version,
                            "seq": change.seq,
                            "deleted": d.deleted_at,
                            "updated": d.updated_at,
                        },
                    )
                elif isinstance(change, RoomChange):
                    r = change.data
                    result = await conn.execute(
                        text(
                            "INSERT INTO rooms_mirror (id, internal_label, is_active, sort_order, "
                            "change_seq, synced_at) VALUES (:id, :label, :active, :order, :seq, "
                            "now()) ON CONFLICT (id) DO UPDATE SET "
                            "internal_label = EXCLUDED.internal_label, "
                            "is_active = EXCLUDED.is_active, sort_order = EXCLUDED.sort_order, "
                            "change_seq = EXCLUDED.change_seq, synced_at = now() "
                            "WHERE rooms_mirror.change_seq < EXCLUDED.change_seq"
                        ),
                        {
                            "id": r.id,
                            "label": r.internal_label,
                            "active": r.is_active,
                            "order": r.sort_order,
                            "seq": change.seq,
                        },
                    )
                applied += result.rowcount or 0
            await self._set(
                conn, cursor=str(response.next_cursor), last_pull_ok=now_utc().isoformat()
            )
            if applied:
                await conn.execute(text("SELECT pg_notify('mirror_changed', '')"))
        return applied

    # ------------------------------------------------------------ push

    async def blocked_appointments(self, conn: AsyncConnection) -> set[UUID]:
        rows = (
            await conn.execute(
                text(
                    "SELECT appointment_id FROM sync_conflicts "
                    "WHERE status = 'open' AND appointment_id IS NOT NULL"
                )
            )
        ).all()
        return {r.appointment_id for r in rows}

    async def next_batch(self) -> list[PendingItem]:
        """Pending items in seq order: at most one per appointment, none for a blocked one.

        An item behind an unresolved conflict for the same appointment is put on hold,
        because it was written assuming the earlier change had gone through.
        """
        async with self.engine.begin() as conn:
            blocked = await self.blocked_appointments(conn)
            rows = (
                await conn.execute(
                    text(
                        "SELECT seq, item_id, kind, payload FROM sync_outbox "
                        "WHERE state = 'pending' ORDER BY seq LIMIT :n FOR UPDATE SKIP LOCKED"
                    ),
                    {"n": MAX_PUSH_ITEMS * 2},
                )
            ).all()
            batch: list[PendingItem] = []
            seen: set[UUID] = set()
            hold: list[int] = []
            for row in rows:
                payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
                item = _ITEM.validate_python(
                    {"item_id": row.item_id, "seq": row.seq, "kind": row.kind, "payload": payload}
                )
                pending = PendingItem(row.seq, item)
                appointment = pending.appointment_id
                if appointment is not None and appointment in blocked:
                    hold.append(row.seq)
                    continue
                if appointment is not None and appointment in seen:
                    break  # keep strict order: the rest waits for the next round
                if appointment is not None:
                    seen.add(appointment)
                batch.append(pending)
                if len(batch) == MAX_PUSH_ITEMS:
                    break
            if hold:
                await conn.execute(
                    text("UPDATE sync_outbox SET state = 'held' WHERE seq = ANY(:s)"),
                    {"s": hold},
                )
        return batch

    async def record_results(self, batch: list[PendingItem], results: list[ItemResult]) -> None:
        by_id = {r.item_id: r for r in results}
        async with self.engine.begin() as conn:
            for pending in batch:
                result = by_id.get(pending.item.item_id)
                if result is None:
                    continue
                if result.result in (SyncResult.APPLIED, SyncResult.DUPLICATE):
                    await conn.execute(
                        text(
                            "UPDATE sync_outbox SET state = 'sent', sent_at = now() "
                            "WHERE seq = :seq"
                        ),
                        {"seq": pending.seq},
                    )
                    continue
                await conn.execute(
                    text(
                        "UPDATE sync_outbox SET state = 'rejected', last_error_code = :code "
                        "WHERE seq = :seq"
                    ),
                    {
                        "seq": pending.seq,
                        "code": result.reason_code.value if result.reason_code else None,
                    },
                )
                await self._conflict_for(conn, pending, result)
            await self._set(conn, last_push_ok=now_utc().isoformat())

    async def _conflict_for(
        self, conn: AsyncConnection, pending: PendingItem, result: ItemResult
    ) -> None:
        payload: Any = pending.item.payload
        reason = result.reason_code or SyncReasonCode.INVALID
        await conn.execute(
            text(
                "INSERT INTO sync_conflicts (outbox_item_id, kind, appointment_id, client_code, "
                "reason_code, proposed_status, cloud_status, author_id) VALUES (:item, :kind, "
                ":appointment, :code, :reason, :proposed, :cloud, :author)"
            ),
            {
                "item": pending.item.item_id,
                "kind": CONFLICT_KIND.get(reason, "sync_status"),
                "appointment": getattr(payload, "appointment_id", None),
                "code": getattr(payload, "code", None)
                or (result.current.client_code if result.current else None),
                "reason": reason.value,
                "proposed": getattr(getattr(payload, "new_status", None), "value", None),
                "cloud": result.current.status.value if result.current else None,
                "author": getattr(payload, "actor_id", None),
            },
        )
        await conn.execute(
            text(
                "INSERT INTO audit_log (actor_kind, action, entity_type, entity_id, detail) "
                "VALUES ('sync', 'sync_item_rejected', 'sync_item', :id, CAST(:detail AS jsonb))"
            ),
            {
                "id": str(pending.item.item_id),
                "detail": json.dumps({"kind": pending.item.kind, "reason": reason.value}),
            },
        )

    async def record_server_error(self, batch: list[PendingItem], error: str) -> int:
        """Count a 5xx against each item; after 10 an item is parked for a person. Returns dead."""
        dead = 0
        async with self.engine.begin() as conn:
            for pending in batch:
                attempts = (
                    await conn.execute(
                        text(
                            "UPDATE sync_outbox SET attempts = attempts + 1, last_error_code = "
                            "'server_error' WHERE seq = :seq RETURNING attempts"
                        ),
                        {"seq": pending.seq},
                    )
                ).scalar_one()
                if attempts >= MAX_SERVER_ERRORS:
                    await conn.execute(
                        text("UPDATE sync_outbox SET state = 'dead' WHERE seq = :seq"),
                        {"seq": pending.seq},
                    )
                    failed = ItemResult(
                        item_id=pending.item.item_id,
                        result=SyncResult.REJECTED,
                        reason_code=SyncReasonCode.INVALID,
                    )
                    await self._conflict_for(conn, pending, failed)
                    dead += 1
            await self._set(conn, last_error=error[:200])
        return dead

    # ------------------------------------------------------------ reconcile

    async def reconcile_codes(self, cloud_codes: list[str]) -> list[str]:
        """Raise the code counter above every cloud code; report codes unknown here."""
        async with self.engine.begin() as conn:
            missing = list(
                (
                    await conn.execute(
                        text("SELECT reconcile_client_codes(CAST(:codes AS text[]))"),
                        {"codes": cloud_codes},
                    )
                ).scalar_one()
                or []
            )
            for code in missing:
                exists = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM sync_conflicts WHERE kind = 'unknown_client_code' "
                            "AND client_code = :c AND status = 'open'"
                        ),
                        {"c": code},
                    )
                ).first()
                if exists is None:
                    await conn.execute(
                        text(
                            "INSERT INTO sync_conflicts (kind, client_code, reason_code) "
                            "VALUES ('unknown_client_code', :c, 'missing_locally')"
                        ),
                        {"c": code},
                    )
            await self._set(conn, last_reconcile_ok=now_utc().isoformat())
        return missing


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
