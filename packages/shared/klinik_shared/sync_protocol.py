"""Wire format between the sync worker (office) and the cloud sync API (plan §5).

Every model forbids unknown fields: nothing that is not listed here can travel
between the tiers, which is what keeps names and notes inside the office.
"""

from __future__ import annotations

from datetime import datetime
from itertools import pairwise
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from klinik_shared.client_code import CLIENT_CODE_PATTERN
from klinik_shared.enums import (
    AppointmentStatus,
    ConflictResolution,
    SyncReasonCode,
    SyncResult,
)

PROTOCOL_VERSION = 1
PROTOCOL_HEADER = "X-Sync-Protocol"
MAX_PUSH_ITEMS = 50
MAX_PULL_LIMIT = 500

ClientCodeStr = Annotated[str, StringConstraints(pattern=CLIENT_CODE_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- pull (cloud -> mirror)


class AppointmentData(_Strict):
    id: UUID
    client_code: ClientCodeStr
    therapist_id: UUID
    room_id: UUID
    starts_at: datetime
    duration_min: int = Field(ge=10, le=240)
    status: AppointmentStatus
    version: int = Field(ge=1)
    deleted_at: datetime | None = None
    updated_at: datetime


class RoomData(_Strict):
    id: UUID
    internal_label: str = Field(max_length=30)
    is_active: bool
    sort_order: int
    updated_at: datetime


class AppointmentChange(_Strict):
    seq: int = Field(ge=1)
    entity: Literal["appointment"]
    data: AppointmentData


class RoomChange(_Strict):
    seq: int = Field(ge=1)
    entity: Literal["room"]
    data: RoomData


Change = Annotated[AppointmentChange | RoomChange, Field(discriminator="entity")]


class ChangesResponse(_Strict):
    changes: list[Change]
    next_cursor: int = Field(ge=0)
    has_more: bool


# ---------------------------------------------------------------- push (outbox -> cloud)


class ClientCodeUpsertPayload(_Strict):
    code: ClientCodeStr
    active: bool
    assigned_therapist_id: UUID | None


# The local tier may push an outcome, or revert one back to planned.
_OUTCOME_TARGETS = (
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.PLANNED,
)


class AppointmentOutcomePayload(_Strict):
    """Compare-and-set on status: applied only if the cloud status is ``prev_status``."""

    appointment_id: UUID
    new_status: AppointmentStatus
    prev_status: AppointmentStatus
    actor_id: UUID
    recorded_at: datetime

    @model_validator(mode="after")
    def _check_transition(self) -> AppointmentOutcomePayload:
        if self.new_status not in _OUTCOME_TARGETS:
            raise ValueError("only completed, no_show or a revert to planned may be pushed")
        if self.new_status == self.prev_status:
            raise ValueError("new_status equals prev_status")
        return self


class ConflictResolvedPayload(_Strict):
    conflict_item_id: UUID
    resolution: ConflictResolution


class _ItemBase(_Strict):
    item_id: UUID
    seq: int = Field(ge=1)


class ClientCodeUpsertItem(_ItemBase):
    kind: Literal["client_code.upsert"]
    payload: ClientCodeUpsertPayload


class AppointmentOutcomeItem(_ItemBase):
    kind: Literal["appointment.outcome"]
    payload: AppointmentOutcomePayload


class ConflictResolvedItem(_ItemBase):
    kind: Literal["conflict.resolved"]
    payload: ConflictResolvedPayload


SyncItem = Annotated[
    ClientCodeUpsertItem | AppointmentOutcomeItem | ConflictResolvedItem,
    Field(discriminator="kind"),
]


class PushRequest(_Strict):
    items: list[SyncItem] = Field(min_length=1, max_length=MAX_PUSH_ITEMS)

    @model_validator(mode="after")
    def _check_order(self) -> PushRequest:
        seqs = [item.seq for item in self.items]
        if any(b <= a for a, b in pairwise(seqs)):
            raise ValueError("items must be sent in strictly increasing seq order")
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate item_id in one request")
        return self


class ItemResult(_Strict):
    item_id: UUID
    result: SyncResult
    reason_code: SyncReasonCode | None = None
    current: AppointmentData | None = None

    @model_validator(mode="after")
    def _check_reason(self) -> ItemResult:
        if self.result is SyncResult.REJECTED and self.reason_code is None:
            raise ValueError("a rejected item needs a reason_code")
        return self


class PushResponse(_Strict):
    results: list[ItemResult]


class ClientCodesResponse(_Strict):
    """Reconcile after a local restore: every code the cloud knows."""

    codes: list[ClientCodeUpsertPayload]
