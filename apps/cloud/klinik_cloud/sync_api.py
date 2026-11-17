"""The cloud end of the sync protocol (plan §5). Called only by the office's sync worker.

Authentication is a bearer token with scope "sync" (no user session, no CSRF).
Every request must announce the protocol version it speaks.

Pull  GET  /sync/v1/changes?after=N&limit=500&wait=25   appointments and rooms, by change_seq
Push  POST /sync/v1/items                                client codes, outcomes, resolutions
      GET  /sync/v1/client-codes                         every code the cloud knows (reconcile)
      GET  /sync/v1/ping
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import insert, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from klinik_cloud import tables as t
from klinik_shared.audit import write_audit
from klinik_shared.enums import (
    AppointmentStatus,
    ConflictKind,
    ConflictStatus,
    SyncReasonCode,
    SyncResult,
)
from klinik_shared.sync_protocol import (
    MAX_PULL_LIMIT,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    AppointmentData,
    AppointmentOutcomeItem,
    ChangesResponse,
    ClientCodesResponse,
    ClientCodeUpsertItem,
    ClientCodeUpsertPayload,
    ConflictResolvedItem,
    ItemResult,
    PushRequest,
    PushResponse,
    RoomData,
    SyncItem,
)
from klinik_shared.timeutil import now_utc

MAX_WAIT_SECONDS = 25
POLL_INTERVAL = 1.0


class SyncAuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii", "ignore")).digest()


def new_token() -> str:
    return "sync_" + secrets.token_urlsafe(32)


async def register_sync_token(conn: AsyncConnection, token: str) -> None:
    s = t.service_tokens
    await conn.execute(
        pg_insert(s)
        .values(token_hash=hash_token(token), scope="sync")
        .on_conflict_do_nothing(index_elements=[s.c.token_hash])
    )


async def create_sync_token(conn: AsyncConnection) -> str:
    token = new_token()
    await conn.execute(insert(t.service_tokens).values(token_hash=hash_token(token), scope="sync"))
    return token


# ---------------------------------------------------------------- pull


async def changes_after(conn: AsyncConnection, after: int, limit: int) -> ChangesResponse:
    a, r = t.appointments, t.rooms
    appts = select(a.c.change_seq.label("seq"), a.c.id.label("id"), _literal("appointment")).where(
        a.c.change_seq > after
    )
    rooms = select(r.c.change_seq.label("seq"), r.c.id.label("id"), _literal("room")).where(
        r.c.change_seq > after
    )
    combined = union_all(appts, rooms).subquery()
    rows = (await conn.execute(select(combined).order_by(combined.c.seq).limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    appointment_ids = [row.id for row in rows if row.entity == "appointment"]
    room_ids = [row.id for row in rows if row.entity == "room"]
    by_id: dict[UUID, Any] = {}
    if appointment_ids:
        for row in (await conn.execute(select(a).where(a.c.id.in_(appointment_ids)))).all():
            by_id[row.id] = row
    if room_ids:
        for row in (await conn.execute(select(r).where(r.c.id.in_(room_ids)))).all():
            by_id[row.id] = row

    changes: list[dict[str, Any]] = []
    for item in rows:
        found = by_id.get(item.id)
        if found is None or found.change_seq != item.seq:
            # Changed again after the first query; the newer version comes in a later page.
            continue
        if item.entity == "appointment":
            data: Any = appointment_data(found)
        else:
            data = RoomData(
                id=found.id,
                internal_label=found.internal_label,
                is_active=found.is_active,
                sort_order=found.sort_order,
                updated_at=found.updated_at,
            )
        changes.append({"seq": item.seq, "entity": item.entity, "data": data})
    next_cursor = rows[-1].seq if rows else after
    return ChangesResponse.model_validate(
        {"changes": changes, "next_cursor": next_cursor, "has_more": has_more}
    )


def _literal(value: str) -> Any:
    from sqlalchemy import literal

    return literal(value).label("entity")


def appointment_data(row: Any) -> AppointmentData:
    return AppointmentData(
        id=row.id,
        client_code=row.client_code,
        therapist_id=row.therapist_id,
        room_id=row.room_id,
        starts_at=row.starts_at,
        duration_min=row.duration_min,
        status=AppointmentStatus(row.status),
        version=row.version,
        deleted_at=row.deleted_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------- push


async def _user_exists(conn: AsyncConnection, user_id: UUID | None) -> bool:
    if user_id is None:
        return False
    return (
        await conn.execute(select(t.users.c.id).where(t.users.c.id == user_id))
    ).first() is not None


async def _reject(
    conn: AsyncConnection,
    item: SyncItem,
    reason: SyncReasonCode,
    *,
    appointment_id: UUID | None = None,
    author_id: UUID | None = None,
    kind: ConflictKind = ConflictKind.SYNC_STATUS,
    proposed: dict[str, Any] | None = None,
    current: AppointmentData | None = None,
) -> ItemResult:
    """A refused item is never dropped: it becomes a conflict for a person to decide."""
    await conn.execute(
        insert(t.conflicts).values(
            kind=kind.value,
            appointment_id=appointment_id,
            author_id=author_id if await _user_exists(conn, author_id) else None,
            proposed=proposed or {},
            reason_code=reason.value,
            sync_item_id=item.item_id,
        )
    )
    await write_audit(
        conn,
        t.audit_log,
        action="sync_item_rejected",
        actor_id=None,
        actor_kind="sync",
        entity_type="sync_item",
        entity_id=item.item_id,
        detail={"kind": item.kind, "reason": reason.value},
    )
    return ItemResult(
        item_id=item.item_id, result=SyncResult.REJECTED, reason_code=reason, current=current
    )


async def apply_client_code(conn: AsyncConnection, item: ClientCodeUpsertItem) -> ItemResult:
    p = item.payload
    therapist = p.assigned_therapist_id
    if therapist is not None and not await _user_exists(conn, therapist):
        return await _reject(
            conn, item, SyncReasonCode.UNKNOWN_THERAPIST, kind=ConflictKind.SYNC_ERROR
        )
    c = t.client_codes
    await conn.execute(
        pg_insert(c)
        .values(code=p.code, active=p.active, assigned_therapist_id=therapist, updated_at=now_utc())
        .on_conflict_do_update(
            index_elements=[c.c.code],
            set_={"active": p.active, "assigned_therapist_id": therapist, "updated_at": now_utc()},
        )
    )
    return ItemResult(item_id=item.item_id, result=SyncResult.APPLIED)


async def apply_outcome(conn: AsyncConnection, item: AppointmentOutcomeItem) -> ItemResult:
    """Compare-and-set on status: applied only when the cloud still shows ``prev_status``."""
    p = item.payload
    a = t.appointments
    row = (
        await conn.execute(select(a).where(a.c.id == p.appointment_id).with_for_update())
    ).one_or_none()
    proposed = {"status": p.new_status.value}
    if row is None:
        return await _reject(
            conn,
            item,
            SyncReasonCode.UNKNOWN_APPOINTMENT,
            author_id=p.actor_id,
            kind=ConflictKind.SYNC_UNKNOWN_APPOINTMENT,
            proposed=proposed,
        )
    current = appointment_data(row)
    if row.deleted_at is not None:
        return await _reject(
            conn, item, SyncReasonCode.APPOINTMENT_DELETED, appointment_id=row.id,
            author_id=p.actor_id, proposed=proposed, current=current,
        )  # fmt: skip
    if row.status == p.new_status.value:
        return ItemResult(item_id=item.item_id, result=SyncResult.APPLIED, current=current)
    if row.status != p.prev_status.value:
        return await _reject(
            conn, item, SyncReasonCode.STATUS_MISMATCH, appointment_id=row.id,
            author_id=p.actor_id, proposed=proposed, current=current,
        )  # fmt: skip
    try:
        async with conn.begin_nested():
            await conn.execute(
                update(a)
                .where(a.c.id == row.id)
                .values(
                    status=p.new_status.value,
                    version=a.c.version + 1,
                    updated_via="sync",
                    updated_by=p.actor_id if await _user_exists(conn, p.actor_id) else None,
                )
            )
    except IntegrityError:
        # Reverting to "planned" can clash with a booking made in the meantime.
        return await _reject(
            conn, item, SyncReasonCode.OVERLAP, appointment_id=row.id,
            author_id=p.actor_id, proposed=proposed, current=current,
        )  # fmt: skip
    await write_audit(
        conn,
        t.audit_log,
        action="appointment_outcome",
        actor_id=p.actor_id if await _user_exists(conn, p.actor_id) else None,
        actor_kind="sync",
        entity_type="appointment",
        entity_id=row.id,
        detail={"status": p.new_status.value},
    )
    fresh = (await conn.execute(select(a).where(a.c.id == row.id))).one()
    return ItemResult(
        item_id=item.item_id, result=SyncResult.APPLIED, current=appointment_data(fresh)
    )


async def apply_resolution(conn: AsyncConnection, item: ConflictResolvedItem) -> ItemResult:
    c = t.conflicts
    await conn.execute(
        update(c)
        .where(
            c.c.sync_item_id == item.payload.conflict_item_id,
            c.c.status == ConflictStatus.OPEN.value,
        )
        .values(
            status=ConflictStatus.RESOLVED.value,
            resolution=item.payload.resolution.value,
            resolved_at=now_utc(),
        )
    )
    return ItemResult(item_id=item.item_id, result=SyncResult.APPLIED)


HANDLERS: dict[str, Callable[[AsyncConnection, Any], Awaitable[ItemResult]]] = {
    "client_code.upsert": apply_client_code,
    "appointment.outcome": apply_outcome,
    "conflict.resolved": apply_resolution,
}


async def process_item(engine: AsyncEngine, item: SyncItem) -> ItemResult:
    """Each item in its own transaction, recorded in sync_inbox in that same transaction."""
    inbox = t.sync_inbox
    async with engine.begin() as conn:
        seen = (
            await conn.execute(select(inbox).where(inbox.c.item_id == item.item_id))
        ).one_or_none()
        if seen is not None:
            return ItemResult(
                item_id=item.item_id,
                result=SyncResult.DUPLICATE,
                reason_code=SyncReasonCode(seen.reason_code) if seen.reason_code else None,
            )
        result = await HANDLERS[item.kind](conn, item)
        await conn.execute(
            insert(inbox).values(
                item_id=item.item_id,
                kind=item.kind,
                result=result.result.value,
                reason_code=result.reason_code.value if result.reason_code else None,
            )
        )
        return result


# ---------------------------------------------------------------- router


def build_router(engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/sync/v1", include_in_schema=False)

    async def require_sync(request: Request) -> None:
        version = request.headers.get(PROTOCOL_HEADER)
        if version != str(PROTOCOL_VERSION):
            raise SyncAuthError(426, "protocol_version")
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise SyncAuthError(401, "not_authenticated")
        s = t.service_tokens
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    update(s)
                    .where(
                        s.c.token_hash == hash_token(header[7:].strip()),
                        s.c.scope == "sync",
                        s.c.revoked_at.is_(None),
                    )
                    .values(last_used_at=now_utc())
                    .returning(s.c.id)
                )
            ).first()
        if row is None:
            raise SyncAuthError(401, "not_authenticated")

    authed = [Depends(require_sync)]

    @router.get("/ping", dependencies=authed)
    async def ping() -> Response:
        return JSONResponse({"ok": True, "protocol": PROTOCOL_VERSION})

    @router.get("/changes", dependencies=authed)
    async def changes(after: int = 0, limit: int = MAX_PULL_LIMIT, wait: int = 0) -> Response:
        limit = max(1, min(limit, MAX_PULL_LIMIT))
        wait = max(0, min(wait, MAX_WAIT_SECONDS))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            async with engine.connect() as conn:
                result = await changes_after(conn, max(0, after), limit)
            if result.changes or result.has_more or loop.time() >= deadline:
                return JSONResponse(result.model_dump(mode="json"))
            await asyncio.sleep(POLL_INTERVAL)

    @router.post("/items", dependencies=authed)
    async def items(request: PushRequest) -> Response:
        results = [await process_item(engine, item) for item in request.items]
        return JSONResponse(PushResponse(results=results).model_dump(mode="json"))

    @router.get("/client-codes", dependencies=authed)
    async def client_codes() -> Response:
        c = t.client_codes
        async with engine.connect() as conn:
            rows = (await conn.execute(select(c).order_by(c.c.code))).all()
        payload = ClientCodesResponse(
            codes=[
                ClientCodeUpsertPayload(
                    code=r.code, active=r.active, assigned_therapist_id=r.assigned_therapist_id
                )
                for r in rows
            ]
        )
        return JSONResponse(payload.model_dump(mode="json"))

    return router


def install(app: FastAPI, engine: AsyncEngine) -> None:
    async def _sync_auth(_: Request, exc: Exception) -> Response:
        if not isinstance(exc, SyncAuthError):  # pragma: no cover - registered for this type
            raise exc
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    app.add_exception_handler(SyncAuthError, _sync_auth)

    app.include_router(build_router(engine))
