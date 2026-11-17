"""Rows as JSON between the office and the laptop, and generic read / upsert.

Values are tagged by type so they survive JSON exactly: uuid, bytes (encrypted text
stays encrypted, both sides hold the same keys), timestamps, dates, decimals, jsonb.
Column names always come from the database's own catalogue, never from the request.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

Row = dict[str, Any]

# Kept by each side itself: never sent, never overwritten.
LOCAL_COLUMNS = frozenset({"change_seq"})
# Columns that only ever go up when a copy arrives (each side counts on its own too).
MERGE = {("clients", "session_counter"): "{c} = GREATEST({t}.{c}, EXCLUDED.{c})"}


@dataclass(frozen=True)
class Spec:
    name: str
    key: tuple[str, ...]  # primary key columns
    incremental: bool  # has change_seq and is sent row by row; else sent whole
    pushable: bool = False  # the laptop may change it


# In foreign-key order: a row's references come before it.
SPECS: tuple[Spec, ...] = (
    Spec("rooms", ("id",), incremental=False),
    Spec("users", ("id",), incremental=False),
    Spec("user_permissions", ("user_id", "permission"), incremental=False),
    Spec("recovery_codes", ("id",), incremental=False),
    Spec("age_groups", ("id",), incremental=False),
    Spec("therapy_methods", ("id",), incremental=False),
    Spec("age_group_therapy_defaults", ("age_group_id", "therapy_method_id"), incremental=False),
    Spec("clients", ("id",), incremental=True, pushable=True),
    Spec("appointments", ("id",), incremental=True, pushable=True),
    Spec("therapy_sessions", ("id",), incremental=True, pushable=True),
    Spec("payments", ("id",), incremental=True, pushable=True),
)
BY_NAME = {s.name: s for s in SPECS}
INCREMENTAL = tuple(s for s in SPECS if s.incremental)
FULL = tuple(s for s in SPECS if not s.incremental)
PUSH_ORDER = ("clients", "appointments", "therapy_sessions", "payments")
# Removed when absent from the office's full copy (plain link tables).
FULL_REPLACE = frozenset({"user_permissions", "age_group_therapy_defaults", "recovery_codes"})


# ---------------------------------------------------------------- values


def encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, UUID):
        return {"$u": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, datetime):
        return {"$t": value.isoformat()}
    if isinstance(value, date):
        return {"$d": value.isoformat()}
    if isinstance(value, time):
        return {"$h": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$n": str(value)}
    if isinstance(value, (dict, list)):
        return {"$j": value}
    raise TypeError(f"cannot send {type(value).__name__}")


def decode_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if len(value) != 1:
        raise ValueError("bad value")
    tag, raw = next(iter(value.items()))
    if tag == "$u":
        return UUID(raw)
    if tag == "$b":
        return base64.b64decode(raw)
    if tag == "$t":
        return datetime.fromisoformat(raw)
    if tag == "$d":
        return date.fromisoformat(raw)
    if tag == "$h":
        return time.fromisoformat(raw)
    if tag == "$n":
        return Decimal(raw)
    if tag == "$j":
        return json.dumps(raw)  # jsonb columns take JSON text
    raise ValueError(f"unknown value tag {tag!r}")


def encode_row(row: Row) -> Row:
    return {k: encode_value(v) for k, v in row.items() if k not in LOCAL_COLUMNS}


def decode_row(row: Row) -> Row:
    return {k: decode_value(v) for k, v in row.items() if k not in LOCAL_COLUMNS}


def json_safe(row: Row | None) -> Any:
    """For storing a row in a jsonb column (conflicts): same tags, already JSON."""
    return None if row is None else encode_row(row)


# ---------------------------------------------------------------- database


class Columns:
    """Column names per table, read once from the catalogue."""

    def __init__(self) -> None:
        self._cache: dict[str, list[str]] = {}

    async def of(self, conn: AsyncConnection, table: str) -> list[str]:
        if table not in self._cache:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t "
                        "ORDER BY ordinal_position"
                    ),
                    {"t": table},
                )
            ).all()
            if not rows:
                raise ValueError(f"unknown table {table!r}")
            self._cache[table] = [r[0] for r in rows]
        return self._cache[table]


COLUMNS = Columns()


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def select_rows(
    conn: AsyncConnection, table: str, where: str = "", params: dict[str, Any] | None = None
) -> list[Row]:
    if table not in BY_NAME and table not in ("session_methods", "audit_log"):
        raise ValueError(table)
    cols = await COLUMNS.of(conn, table)
    sql = f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(table)} {where}"
    result = await conn.execute(text(sql), params or {})
    return [dict(r._mapping) for r in result.all()]


async def upsert(conn: AsyncConnection, table: str, key: tuple[str, ...], row: Row) -> None:
    """Insert or overwrite one row (only columns the table really has)."""
    cols = [c for c in await COLUMNS.of(conn, table) if c in row and c not in LOCAL_COLUMNS]
    names = ", ".join(_q(c) for c in cols)
    values = ", ".join(f":p{i}" for i in range(len(cols)))
    updates = ", ".join(
        MERGE.get((table, c), "{c} = EXCLUDED.{c}").format(t=_q(table), c=_q(c))
        for c in cols
        if c not in key
    )
    conflict = ", ".join(_q(c) for c in key)
    action = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
    sql = (
        f"INSERT INTO {_q(table)} ({names}) OVERRIDING SYSTEM VALUE VALUES ({values}) "
        f"ON CONFLICT ({conflict}) {action}"
    )
    await conn.execute(text(sql), {f"p{i}": row[c] for i, c in enumerate(cols)})


async def update_row(conn: AsyncConnection, table: str, row: Row) -> int:
    cols = [
        c for c in await COLUMNS.of(conn, table) if c in row and c not in (*LOCAL_COLUMNS, "id")
    ]
    sets = ", ".join(f"{_q(c)} = :p{i}" for i, c in enumerate(cols))
    params = {f"p{i}": row[c] for i, c in enumerate(cols)}
    params["id"] = row["id"]
    result = await conn.execute(
        text(f"UPDATE {_q(table)} SET {sets} WHERE id = :id"),
        params,
    )
    return int(result.rowcount or 0)


async def insert_row(conn: AsyncConnection, table: str, row: Row) -> None:
    cols = [c for c in await COLUMNS.of(conn, table) if c in row and c not in LOCAL_COLUMNS]
    sql = (
        f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in cols)}) "
        f"OVERRIDING SYSTEM VALUE VALUES ({', '.join(f':p{i}' for i in range(len(cols)))})"
    )
    await conn.execute(text(sql), {f"p{i}": row[c] for i, c in enumerate(cols)})


async def session_methods(conn: AsyncConnection, session_id: UUID) -> list[Row]:
    return await select_rows(conn, "session_methods", "WHERE session_id = :s", {"s": session_id})


async def replace_methods(conn: AsyncConnection, session_id: UUID, methods: list[Row]) -> None:
    await conn.execute(text("DELETE FROM session_methods WHERE session_id = :s"), {"s": session_id})
    for method in methods:
        await insert_row(conn, "session_methods", {**method, "session_id": session_id})


async def raise_session_counters(conn: AsyncConnection) -> None:
    """Every client's counter at least its highest known session number."""
    await conn.execute(
        text(
            "UPDATE clients c SET session_counter = m.top FROM "
            "(SELECT client_id, max(session_number) AS top FROM therapy_sessions "
            " GROUP BY client_id) m WHERE m.client_id = c.id AND c.session_counter < m.top"
        )
    )


async def next_number(conn: AsyncConnection, client_id: UUID) -> int:
    """The next free session number for a client, without a user context (sync only)."""
    value = (
        await conn.execute(
            text(
                "UPDATE clients SET session_counter = GREATEST(session_counter, "
                "(SELECT coalesce(max(session_number), 0) FROM therapy_sessions "
                " WHERE client_id = :c)) + 1 WHERE id = :c RETURNING session_counter"
            ),
            {"c": client_id},
        )
    ).scalar_one()
    return int(value)


async def applying(conn: AsyncConnection) -> None:
    """Mark this transaction as the sync: not queued again, constraints checked at commit."""
    await conn.execute(text("SELECT set_config('klinik.applying', 'on', true)"))
    await conn.execute(text("SET CONSTRAINTS ALL DEFERRED"))
