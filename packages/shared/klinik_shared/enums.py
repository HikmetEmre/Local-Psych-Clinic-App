"""Enum codes used in logic and storage. Display labels live in ``labels_tr``."""

from __future__ import annotations

from enum import StrEnum


class AppointmentStatus(StrEnum):
    PLANNED = "planned"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


# Therapy sessions (local) use the same four codes (TERAPILER_TAB §3.2).
SessionStatus = AppointmentStatus

# Statuses that occupy a slot for the no-double-booking constraint (brief §7).
SLOT_HOLDING_STATUSES: frozenset[AppointmentStatus] = frozenset(
    {AppointmentStatus.PLANNED, AppointmentStatus.COMPLETED}
)


class PaymentMethod(StrEnum):
    CASH = "cash"
    CARD = "card"
    TRANSFER = "transfer"


class MethodKind(StrEnum):
    MODALITY = "modality"
    FORMAT = "format"


class NotificationEvent(StrEnum):
    APPOINTMENT_CREATED = "appointment.created"
    APPOINTMENT_UPDATED = "appointment.updated"
    APPOINTMENT_CANCELLED = "appointment.cancelled"
    REMINDER_24H = "appointment.reminder_24h"
    REMINDER_1H = "appointment.reminder_1h"
    CONFLICT_CREATED = "conflict.created"


class PushState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class SyncItemKind(StrEnum):
    CLIENT_CODE_UPSERT = "client_code.upsert"
    APPOINTMENT_OUTCOME = "appointment.outcome"
    CONFLICT_RESOLVED = "conflict.resolved"


class SyncResult(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


class SyncReasonCode(StrEnum):
    STATUS_MISMATCH = "status_mismatch"
    UNKNOWN_APPOINTMENT = "unknown_appointment"
    APPOINTMENT_DELETED = "appointment_deleted"
    OVERLAP = "overlap"
    UNKNOWN_THERAPIST = "unknown_therapist"
    INVALID = "invalid"


class OutboxState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    REJECTED = "rejected"
    HELD = "held"
    DEAD = "dead"


class ConflictKind(StrEnum):
    BOOKING_VERSION = "booking_version"
    SYNC_STATUS = "sync_status"
    SYNC_UNKNOWN_APPOINTMENT = "sync_unknown_appointment"
    SYNC_ERROR = "sync_error"


class ConflictStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ConflictResolution(StrEnum):
    DISCARDED = "discarded"
    REAPPLIED = "reapplied"
