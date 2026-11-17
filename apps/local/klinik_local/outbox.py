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
from klinik_shared.enums import AppointmentStatus, ConflictResolution, SyncItemKind
from klinik_shared.sync_protocol import (
    AppointmentOutcomePayload,
    ClientCodeUpsertPayload,
    ConflictResolvedPayload,
)
from klinik_shared.timeutil import now_utc


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


def cloud_status_for(session_status: str) -> AppointmentStatus:
    """What the cloud appointment should show for a linked session in this state.

    Only an outcome (completed / no-show) is the office's to report; anything else
    leaves the booking as planned. Cancelling a booking is done in the calendar.
    """
    if session_status in (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW):
        return AppointmentStatus(session_status)
    return AppointmentStatus.PLANNED


async def enqueue_outcome(
    conn: AsyncConnection,
    *,
    appointment_id: UUID,
    new_status: AppointmentStatus,
    prev_status: AppointmentStatus,
    actor_id: UUID,
) -> bool:
    """Queue a status change for the cloud. Returns False when there is nothing to send."""
    if new_status == prev_status:
        return False
    payload = AppointmentOutcomePayload(
        appointment_id=appointment_id,
        new_status=new_status,
        prev_status=prev_status,
        actor_id=actor_id,
        recorded_at=now_utc(),
    )
    await conn.execute(
        insert(t.sync_outbox).values(
            kind=SyncItemKind.APPOINTMENT_OUTCOME.value, payload=payload.model_dump(mode="json")
        )
    )
    return True


async def enqueue_resolution(
    conn: AsyncConnection, *, conflict_item_id: UUID, resolution: ConflictResolution
) -> None:
    payload = ConflictResolvedPayload(conflict_item_id=conflict_item_id, resolution=resolution)
    await conn.execute(
        insert(t.sync_outbox).values(
            kind=SyncItemKind.CONFLICT_RESOLVED.value, payload=payload.model_dump(mode="json")
        )
    )
