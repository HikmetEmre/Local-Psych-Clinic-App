"""Append-only audit log (brief §11).

Rule: never put secrets, decrypted text or client identity in ``detail``. Record
*which* fields changed, not their values.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Table, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_shared.timeutil import now_utc

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | Sequence[JsonScalar] | Mapping[str, JsonScalar]

MAX_DETAIL_STRING = 64


class AuditDetailError(ValueError):
    pass


def _check(value: Any, key: str) -> None:
    if isinstance(value, str):
        if len(value) > MAX_DETAIL_STRING:
            raise AuditDetailError(
                f"audit detail {key!r} is too long to be an id or code; "
                "free text must not go into the audit log"
            )
    elif isinstance(value, Mapping):
        for k, v in value.items():
            _check(v, f"{key}.{k}")
    elif isinstance(value, Sequence):
        for item in value:
            _check(item, key)
    elif value is not None and not isinstance(value, int | float | bool):
        raise AuditDetailError(f"audit detail {key!r} has unsupported type {type(value).__name__}")


async def write_audit(
    conn: AsyncConnection,
    table: Table,
    *,
    action: str,
    actor_id: UUID | None,
    actor_kind: str = "user",
    entity_type: str | None = None,
    entity_id: UUID | str | None = None,
    detail: Mapping[str, JsonValue] | None = None,
    ip: str | None = None,
) -> None:
    detail = dict(detail or {})
    for key, value in detail.items():
        _check(value, key)
    await conn.execute(
        insert(table).values(
            ts=now_utc(),
            actor_id=actor_id,
            actor_kind=actor_kind,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            detail=detail,
            ip=ip,
        )
    )
