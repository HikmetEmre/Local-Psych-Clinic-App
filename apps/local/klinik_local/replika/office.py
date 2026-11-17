"""Office side of the laptop copy: the sync API and applying what the laptop sends.

Authentication: each paired laptop holds a random token; the office keeps its SHA-256.
Everything here runs as the sync role (kasa_sync), outside any user's session.

Rules for a row the laptop changed:
- new on the laptop -> inserted; a double booking is refused (conflict "overlap"), a
  taken session number is replaced by the next free one ("renumbered");
- edited on the laptop and untouched at the office since the laptop last saw it ->
  the laptop's version is applied;
- edited on both sides -> the office version stays, the laptop's is kept in
  replica_conflicts for the admin to look at ("changed_on_both"). Nothing is lost.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from klinik_local.replika import store
from klinik_local.replika.store import BY_NAME, FULL, INCREMENTAL, Row
from klinik_shared.timeutil import now_utc

PAGE = 2000
MAX_ITEMS = 1000
OVERLAP = {"no_overlap_therapist", "no_overlap_room", "no_overlap_client"}
MESSAGES = {
    "overlap": "Dizüstünde verilen randevu ofisteki bir randevuyla çakışıyor; uygulanmadı.",
    "changed_on_both": "Bu kayıt hem ofiste hem dizüstünde değiştirildi; ofisteki hali korundu.",
    "rejected": "Dizüstündeki kayıt ofiste uygulanamadı.",
    "renumbered": "Seans numarası ofiste dolu olduğu için yeni numara verildi.",
}


def new_token() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _constraint(exc: IntegrityError) -> str | None:
    cause = getattr(exc.orig, "__cause__", None) or exc.orig
    return getattr(cause, "constraint_name", None)


def _sqlstate(exc: IntegrityError) -> str | None:
    cause = getattr(exc.orig, "__cause__", None) or exc.orig
    return getattr(cause, "sqlstate", None)


@dataclass
class Result:
    table: str
    id: UUID
    result: str  # applied | renumbered | conflict | rejected
    office: Row | None = None
    seq: int | None = None
    methods: list[Row] | None = None
    message: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "t": self.table,
            "id": str(self.id),
            "result": self.result,
            "office": store.encode_row(self.office) if self.office else None,
            "seq": self.seq,
            "methods": [store.encode_row(m) for m in self.methods] if self.methods else None,
            "message": self.message,
        }


# ---------------------------------------------------------------- pull


async def feed(
    conn: AsyncConnection, since: int, *, full: bool, limit: int = PAGE
) -> dict[str, Any]:
    out: dict[str, Any] = {"full": None, "rows": [], "next": since, "more": False}
    if full:
        out["full"] = {
            spec.name: [store.encode_row(r) for r in await store.select_rows(conn, spec.name)]
            for spec in FULL
        }
    candidates: list[tuple[int, str, Row]] = []
    for spec in INCREMENTAL:
        rows = await store.select_rows(
            conn,
            spec.name,
            "WHERE change_seq > :since ORDER BY change_seq LIMIT :limit",
            {"since": since, "limit": limit + 1},
        )
        candidates += [(int(r["change_seq"]), spec.name, r) for r in rows]
    candidates.sort(key=lambda item: item[0])
    taken = candidates[:limit]
    out["more"] = len(candidates) > limit
    for seq, table, row in taken:
        item: dict[str, Any] = {"t": table, "seq": seq, "row": store.encode_row(row)}
        if table == "therapy_sessions":
            item["methods"] = [
                store.encode_row(m) for m in await store.session_methods(conn, row["id"])
            ]
        out["rows"].append(item)
    if taken:
        out["next"] = taken[-1][0]
    return out


# ---------------------------------------------------------------- push


async def _current(conn: AsyncConnection, table: str, row_id: UUID, *, lock: bool) -> Row | None:
    rows = await store.select_rows(
        conn, table, "WHERE id = :id" + (" FOR UPDATE" if lock else ""), {"id": row_id}
    )
    return rows[0] if rows else None


async def _record_conflict(
    conn: AsyncConnection,
    replica_id: UUID,
    table: str,
    row_id: UUID,
    kind: str,
    laptop: Row | None,
    office: Row | None,
    *,
    methods: list[Row] | None = None,
    resolved: bool = False,
) -> None:
    laptop_json = store.json_safe(laptop)
    if laptop_json is not None and methods is not None:
        laptop_json["__methods"] = [store.encode_row(m) for m in methods]
    await conn.execute(
        text(
            "INSERT INTO replica_conflicts (replica_id, tbl, row_id, kind, message, laptop_row, "
            "office_row, resolved_at, resolution) VALUES (:r, :t, :id, :k, :m, "
            "CAST(:lap AS jsonb), CAST(:off AS jsonb), :done, :res)"
        ),
        {
            "r": replica_id, "t": table, "id": row_id, "k": kind, "m": MESSAGES[kind],
            "lap": json.dumps(laptop_json), "off": json.dumps(store.json_safe(office)),
            "done": now_utc() if resolved else None, "res": "noted" if resolved else None,
        },
    )  # fmt: skip


async def _seq(conn: AsyncConnection, table: str, row_id: UUID) -> int | None:
    value = (
        await conn.execute(
            text(f'SELECT change_seq FROM "{table}" WHERE id = :id'),
            {"id": row_id},
        )
    ).scalar_one_or_none()
    return int(value) if value is not None else None


async def _respond(
    conn: AsyncConnection, table: str, row_id: UUID, result: str, msg: str = ""
) -> Result:
    office = await _current(conn, table, row_id, lock=False)
    methods = (
        await store.session_methods(conn, row_id)
        if table == "therapy_sessions" and office
        else None
    )
    seq = await _seq(conn, table, row_id) if office else None
    return Result(table, row_id, result, office, seq, methods, msg)


async def apply_item(conn: AsyncConnection, replica_id: UUID, item: dict[str, Any]) -> Result:
    table = str(item.get("t"))
    spec = BY_NAME.get(table)
    if spec is None or not spec.pushable:
        raise ValueError(f"table {table!r} cannot be sent")
    row = store.decode_row(item["row"])
    row_id = UUID(str(item["id"]))
    if row.get("id") != row_id:
        raise ValueError("row id mismatch")
    methods = [store.decode_row(m) for m in item.get("methods") or []]
    base = item.get("base")
    current = await _current(conn, table, row_id, lock=True)
    if table == "clients" and current is not None:
        row.pop("session_counter", None)  # the office keeps counting its own

    async def write(kind: str) -> None:
        async with conn.begin_nested():
            if kind == "insert":
                await store.insert_row(conn, table, row)
            else:
                await store.update_row(conn, table, row)
            if table == "therapy_sessions":
                await store.replace_methods(conn, row_id, methods)

    if current is None:
        try:
            await write("insert")
            if table == "therapy_sessions":
                await store.raise_session_counters(conn)
            return await _respond(conn, table, row_id, "applied")
        except IntegrityError as exc:
            name = _constraint(exc)
            if name in OVERLAP:
                await _record_conflict(conn, replica_id, table, row_id, "overlap", row, None)
                return Result(table, row_id, "rejected", message=MESSAGES["overlap"])
            if name == "therapy_sessions_client_id_session_number_key":
                number = await store.next_number(conn, row["client_id"])
                old = row["session_number"]
                row["session_number"] = number
                await write("insert")
                await _record_conflict(
                    conn, replica_id, table, row_id, "renumbered", {**row, "session_number": old},
                    None, resolved=True,
                )  # fmt: skip
                return await _respond(conn, table, row_id, "renumbered", MESSAGES["renumbered"])
            if (
                _sqlstate(exc) == "23503"
                and table == "therapy_sessions"
                and row.get("appointment_id")
            ):
                # Its appointment was refused just before: keep the session, unlinked.
                row["appointment_id"] = None
                await write("insert")
                return await _respond(conn, table, row_id, "applied")
            await _record_conflict(
                conn, replica_id, table, row_id, "rejected", row, None, methods=methods
            )
            return Result(table, row_id, "rejected", message=MESSAGES["rejected"])

    office_seq = await _seq(conn, table, row_id)
    if base is not None and office_seq != int(base):
        await _record_conflict(
            conn, replica_id, table, row_id, "changed_on_both", row, current, methods=methods
        )
        return await _respond(conn, table, row_id, "conflict", MESSAGES["changed_on_both"])
    try:
        await write("update")
    except IntegrityError as exc:
        kind = "overlap" if _constraint(exc) in OVERLAP else "rejected"
        await _record_conflict(conn, replica_id, table, row_id, kind, row, current, methods=methods)
        return await _respond(conn, table, row_id, "conflict", MESSAGES[kind])
    return await _respond(conn, table, row_id, "applied")


async def apply_push(
    conn: AsyncConnection, replica_id: UUID, body: dict[str, Any]
) -> dict[str, Any]:
    items = body.get("items") or []
    if len(items) > MAX_ITEMS:
        raise ValueError("too many items")
    order = {name: i for i, name in enumerate(store.PUSH_ORDER)}
    items = sorted(items, key=lambda it: order.get(str(it.get("t")), 99))
    results = [(await apply_item(conn, replica_id, it)).as_json() for it in items]
    label = (
        await conn.execute(text("SELECT label FROM replicas WHERE id = :id"), {"id": replica_id})
    ).scalar_one()
    for entry in body.get("audit") or []:
        row = store.decode_row(entry)
        detail = json.loads(row.get("detail") or "{}")
        detail["via"] = f"dizüstü: {label}"
        await conn.execute(
            text(
                "INSERT INTO audit_log (ts, actor_id, actor_kind, action, entity_type, entity_id, "
                "detail, ip) VALUES (:ts, :actor, :kind, :action, :etype, :eid, "
                "CAST(:detail AS jsonb), :ip)"
            ),
            {
                "ts": row["ts"], "actor": row.get("actor_id"), "kind": row["actor_kind"],
                "action": row["action"], "etype": row.get("entity_type"),
                "eid": row.get("entity_id"), "detail": json.dumps(detail), "ip": row.get("ip"),
            },
        )  # fmt: skip
    return {"results": results}


# ---------------------------------------------------------------- HTTP


def build_api_router(sync_engine: AsyncEngine) -> APIRouter:
    router = APIRouter(prefix="/replika/api", include_in_schema=False)

    async def replica_for(request: Request, conn: AsyncConnection) -> UUID | None:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return None
        row = (
            await conn.execute(
                text(
                    "UPDATE replicas SET last_seen_at = now() "
                    "WHERE token_hash = :h AND revoked_at IS NULL RETURNING id"
                ),
                {"h": token_hash(token.strip())},
            )
        ).scalar_one_or_none()
        return row if isinstance(row, UUID) else None

    def denied() -> Response:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    @router.get("/ping")
    async def ping(request: Request) -> Response:
        async with sync_engine.begin() as conn:
            if await replica_for(request, conn) is None:
                return denied()
        return JSONResponse({"ok": True, "time": now_utc().isoformat()})

    @router.post("/pull")
    async def pull(request: Request) -> Response:
        body = await request.json()
        async with sync_engine.begin() as conn:
            # One consistent picture for the whole page.
            await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            if await replica_for(request, conn) is None:
                return denied()
            data = await feed(conn, int(body.get("since") or 0), full=bool(body.get("full")))
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    @router.post("/push")
    async def push(request: Request) -> Response:
        body = await request.json()
        async with sync_engine.begin() as conn:
            replica_id = await replica_for(request, conn)
            if replica_id is None:
                return denied()
            try:
                data = await apply_push(conn, replica_id, body)
            except (ValueError, KeyError, TypeError) as exc:
                return JSONResponse({"detail": f"bad request: {exc}"}, status_code=400)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    return router
