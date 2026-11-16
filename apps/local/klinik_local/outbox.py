"""Local sync outbox (brief §9).

Items are written in the SAME transaction as the business change, so a change and
its outbound message commit or roll back together. The sync worker sends them in
``seq`` order. Payloads are validated by the shared protocol models, so nothing
outside those models (names, notes) can be queued.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_shared.enums import SyncItemKind
from klinik_shared.sync_protocol import ClientCodeUpsertPayload


async def enqueue_client_code(
    conn: AsyncConnection, *, code: str, active: bool, assigned_therapist_id: UUID | None
) -> None:
    payload = ClientCodeUpsertPayload(
        code=code, active=active, assigned_therapist_id=assigned_therapist_id
    )
    await conn.execute(
        insert(t.sync_outbox).values(
            kind=SyncItemKind.CLIENT_CODE_UPSERT.value, payload=payload.model_dump(mode="json")
        )
    )
