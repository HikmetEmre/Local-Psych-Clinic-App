"""Appointments (brief §7): the clinic's schedule, keyed by client code only.

- No double booking: three exclusion constraints in the database (therapist, room,
  client). A clash comes back as ``OverlapError`` with a Turkish message.
- Optimistic locking: every edit carries the version it was based on. If someone
  else saved in between, edits to *different* fields are merged; an edit to the
  *same* field is never resolved automatically: it becomes a conflict row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import and_, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_cloud import tables as t
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.client_code import is_valid_client_code
from klinik_shared.enums import AppointmentStatus, ConflictKind
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import istanbul_day_bounds, now_utc

EDITABLE = ("starts_at", "duration_min", "therapist_id", "room_id", "status")
WEB_STATUSES = (AppointmentStatus.PLANNED, AppointmentStatus.CANCELLED)
MAX_FUTURE = timedelta(days=730)
MAX_PAST = timedelta(days=31)

OVERLAP_MESSAGES = {
    "no_overlap_therapist": "Terapistin bu saatte başka bir randevusu var.",
    "no_overlap_room": "Oda bu saatte dolu.",
    "no_overlap_client": "Danışanın bu saatte başka bir randevusu var.",
}


class AppointmentError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class OverlapError(AppointmentError):
    pass


class AppointmentNotFound(Exception):
    pass


class BookingConflict(Exception):
    """Someone changed the same field(s) since the editor loaded the appointment."""

    def __init__(self, current: Appointment, conflict_id: UUID, fields: list[str]) -> None:
        super().__init__("version conflict")
        self.current = current
        self.conflict_id = conflict_id
        self.fields = fields


# ---------------------------------------------------------------- data shapes


class AppointmentFields(BaseModel):
    """What a person can set on an appointment. Also the shape stored in conflicts.proposed."""

    model_config = ConfigDict(extra="forbid")

    starts_at: AwareDatetime | None = None
    duration_min: int | None = Field(default=None, ge=10, le=240)
    therapist_id: UUID | None = None
    room_id: UUID | None = None
    status: AppointmentStatus | None = None

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if v is not None}


@dataclass(frozen=True)
class Appointment:
    id: UUID
    client_code: str
    therapist_id: UUID
    therapist_name: str
    room_id: UUID
    room_label: str
    starts_at: datetime
    ends_at: datetime
    duration_min: int
    status: str
    version: int
    updated_at: datetime

    def value(self, field: str) -> Any:
        return getattr(self, field)


@dataclass(frozen=True)
class Option:
    id: UUID
    label: str


# ---------------------------------------------------------------- helpers


def _constraint(exc: IntegrityError) -> str | None:
    orig = exc.orig
    cause = getattr(orig, "__cause__", None) or orig
    return getattr(cause, "constraint_name", None)


def _same(field: str, a: Any, b: Any) -> bool:
    if field == "status":
        return str(a) == str(b)
    return bool(a == b)


_SELECT = select(
    t.appointments.c.id,
    t.appointments.c.client_code,
    t.appointments.c.therapist_id,
    t.users.c.display_name.label("therapist_name"),
    t.appointments.c.room_id,
    t.rooms.c.internal_label.label("room_label"),
    t.appointments.c.starts_at,
    t.appointments.c.ends_at,
    t.appointments.c.duration_min,
    t.appointments.c.status,
    t.appointments.c.version,
    t.appointments.c.updated_at,
).select_from(
    t.appointments.join(t.users, t.users.c.id == t.appointments.c.therapist_id).join(
        t.rooms, t.rooms.c.id == t.appointments.c.room_id
    )
)


def _row(r: Any) -> Appointment:
    return Appointment(
        r.id,
        r.client_code,
        r.therapist_id,
        r.therapist_name,
        r.room_id,
        r.room_label,
        r.starts_at,
        r.ends_at,
        r.duration_min,
        r.status,
        r.version,
        r.updated_at,
    )


class AppointmentService:
    # ------------------------------------------------------------ reading

    async def in_range(
        self,
        conn: AsyncConnection,
        first_day: date,
        days: int,
        *,
        therapist_id: UUID | None = None,
        room_id: UUID | None = None,
    ) -> list[Appointment]:
        start = istanbul_day_bounds(first_day)[0]
        end = istanbul_day_bounds(first_day + timedelta(days=days - 1))[1]
        a = t.appointments
        stmt = _SELECT.where(a.c.deleted_at.is_(None), a.c.starts_at < end, a.c.ends_at > start)
        if therapist_id:
            stmt = stmt.where(a.c.therapist_id == therapist_id)
        if room_id:
            stmt = stmt.where(a.c.room_id == room_id)
        rows = (await conn.execute(stmt.order_by(a.c.starts_at, t.users.c.display_name))).all()
        return [_row(r) for r in rows]

    async def get(
        self, conn: AsyncConnection, appointment_id: UUID, *, lock: bool = False
    ) -> Appointment:
        stmt = _SELECT.where(
            t.appointments.c.id == appointment_id, t.appointments.c.deleted_at.is_(None)
        )
        if lock:
            stmt = stmt.with_for_update(of=t.appointments)
        row = (await conn.execute(stmt)).one_or_none()
        if row is None:
            raise AppointmentNotFound
        return _row(row)

    async def therapists(self, conn: AsyncConnection) -> list[Option]:
        u, p = t.users, t.user_permissions
        rows = (
            await conn.execute(
                select(u.c.id, u.c.display_name)
                .join(p, p.c.user_id == u.c.id)
                .where(u.c.is_active, p.c.permission == Permission.SESSIONS_WRITE_OWN.value)
                .order_by(u.c.display_name)
            )
        ).all()
        return [Option(r.id, r.display_name) for r in rows]

    async def rooms(self, conn: AsyncConnection, *, active_only: bool = True) -> list[Option]:
        r = t.rooms
        stmt = select(r.c.id, r.c.internal_label).order_by(r.c.sort_order, r.c.internal_label)
        if active_only:
            stmt = stmt.where(r.c.is_active)
        return [Option(x.id, x.internal_label) for x in (await conn.execute(stmt)).all()]

    async def client_codes(self, conn: AsyncConnection) -> list[tuple[str, UUID | None]]:
        c = t.client_codes
        rows = (
            await conn.execute(
                select(c.c.code, c.c.assigned_therapist_id)
                .where(c.c.active)
                .order_by(c.c.code.desc())
            )
        ).all()
        return [(r.code, r.assigned_therapist_id) for r in rows]

    async def default_room(self, conn: AsyncConnection, therapist_id: UUID) -> UUID | None:
        s = t.user_settings
        value = (
            await conn.execute(select(s.c.default_room_id).where(s.c.user_id == therapist_id))
        ).scalar_one_or_none()
        return value if isinstance(value, UUID) else None

    # ------------------------------------------------------------ validation

    async def _validate(
        self, conn: AsyncConnection, fields: dict[str, Any], *, is_new: bool
    ) -> None:
        if "starts_at" in fields:
            now = now_utc()
            starts = fields["starts_at"]
            if starts > now + MAX_FUTURE:
                raise AppointmentError(
                    "starts_at", "Randevu en fazla 2 yıl sonrası için verilebilir."
                )
            if is_new and starts < now - MAX_PAST:
                raise AppointmentError("starts_at", "Bu kadar eski bir tarihe randevu girilemez.")
        if "therapist_id" in fields:
            ids = {o.id for o in await self.therapists(conn)}
            if fields["therapist_id"] not in ids:
                raise AppointmentError("therapist_id", "Terapist seçimi geçersiz.")
        if "room_id" in fields:
            ids = {o.id for o in await self.rooms(conn)}
            if fields["room_id"] not in ids:
                raise AppointmentError("room_id", "Oda seçimi geçersiz.")
        if "status" in fields and fields["status"] not in WEB_STATUSES:
            # Completed / no-show come from the therapist's session record (via sync).
            raise AppointmentError("status", "Bu durum takvimden değiştirilemez.")

    async def _write(self, conn: AsyncConnection, stmt: Any) -> Any:
        """Run an insert/update; turn an exclusion-constraint clash into OverlapError."""
        try:
            async with conn.begin_nested():
                return await conn.execute(stmt)
        except IntegrityError as exc:
            name = _constraint(exc)
            if name in OVERLAP_MESSAGES:
                raise OverlapError("starts_at", OVERLAP_MESSAGES[name]) from None
            raise

    # ------------------------------------------------------------ writing

    async def create(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        *,
        client_code: str,
        fields: AppointmentFields,
        appointment_id: UUID | None = None,
    ) -> Appointment:
        data = fields.changes()
        for required, message in (
            ("starts_at", "Tarih ve saat zorunludur."),
            ("therapist_id", "Terapist seçin."),
            ("room_id", "Oda seçin."),
        ):
            if required not in data:
                raise AppointmentError(required, message)
        data.setdefault("duration_min", 50)
        data["status"] = AppointmentStatus.PLANNED.value
        if not is_valid_client_code(client_code):
            raise AppointmentError("client_code", "Danışan kodu geçersiz (DN-YYYY-NNNN).")
        c = t.client_codes
        known = (
            await conn.execute(select(c.c.active).where(c.c.code == client_code))
        ).scalar_one_or_none()
        if known is None:
            raise AppointmentError(
                "client_code",
                "Bu danışan kodu henüz bilinmiyor. Danışan Klinik Kasa'da kayıtlı mı?",
            )
        if not known:
            raise AppointmentError("client_code", "Bu danışan pasif. Yeni randevu verilemez.")
        await self._validate(conn, data, is_new=True)

        values = {
            **data,
            "client_code": client_code,
            "created_by": actor.id,
            "updated_by": actor.id,
        }
        if appointment_id is not None:
            values["id"] = appointment_id
        a = t.appointments
        new_id = (
            await self._write(conn, insert(a).values(**values).returning(a.c.id))
        ).scalar_one()
        await write_audit(
            conn,
            t.audit_log,
            action="appointment_created",
            actor_id=actor.id,
            entity_type="appointment",
            entity_id=new_id,
            detail={"client_code": client_code},
        )
        return await self.get(conn, new_id)

    async def update(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        appointment_id: UUID,
        *,
        changes: AppointmentFields,
        based_on: AppointmentFields,
        expected_version: int,
    ) -> tuple[Appointment, bool]:
        """Apply the editor's changes. Returns (appointment, merged).

        ``based_on`` holds the values the editor saw; ``merged`` is True when someone
        else had saved in between and their change touched different fields.
        """
        current = await self.get(conn, appointment_id, lock=True)
        wanted = (
            {k: v for k, v in changes.changes().items() if not _same(k, v, current.value(k))}
            if current.version == expected_version
            else changes.changes()
        )
        merged = False

        if current.version != expected_version:
            seen = based_on.changes()
            theirs = {f for f in EDITABLE if f in seen and not _same(f, seen[f], current.value(f))}
            mine = {f for f, v in wanted.items() if not _same(f, v, seen.get(f))}
            clash = sorted(mine & theirs)
            if clash:
                conflict_id = await self._record_conflict(conn, actor, current, wanted)
                raise BookingConflict(current, conflict_id, clash)
            wanted = {f: wanted[f] for f in mine}
            merged = True

        wanted = {k: v for k, v in wanted.items() if not _same(k, v, current.value(k))}
        if not wanted:
            return current, merged
        await self._validate(conn, wanted, is_new=False)
        a = t.appointments
        await self._write(
            conn,
            update(a)
            .where(and_(a.c.id == appointment_id, a.c.version == current.version))
            .values(**wanted, version=a.c.version + 1, updated_by=actor.id, updated_via="web"),
        )
        cancelled = wanted.get("status") == AppointmentStatus.CANCELLED
        await write_audit(
            conn,
            t.audit_log,
            action="appointment_cancelled" if cancelled else "appointment_updated",
            actor_id=actor.id,
            entity_type="appointment",
            entity_id=appointment_id,
            detail={"changed": sorted(wanted), "merged": merged},
        )
        return await self.get(conn, appointment_id), merged

    async def cancel(
        self, conn: AsyncConnection, actor: CurrentUser, appointment_id: UUID, expected_version: int
    ) -> Appointment:
        current = await self.get(conn, appointment_id, lock=True)
        changed, _ = await self.update(
            conn,
            actor,
            appointment_id,
            changes=AppointmentFields(status=AppointmentStatus.CANCELLED),
            based_on=AppointmentFields(status=AppointmentStatus(current.status)),
            expected_version=expected_version,
        )
        return changed

    async def _record_conflict(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        current: Appointment,
        wanted: dict[str, Any],
    ) -> UUID:
        proposed = AppointmentFields(**wanted).model_dump(mode="json", exclude_none=True)
        c = t.conflicts
        conflict_id: UUID = (
            await conn.execute(
                insert(c)
                .values(
                    kind=ConflictKind.BOOKING_VERSION.value,
                    appointment_id=current.id,
                    author_id=actor.id,
                    proposed=proposed,
                    reason_code="same_field_edited",
                )
                .returning(c.c.id)
            )
        ).scalar_one()
        await write_audit(
            conn,
            t.audit_log,
            action="conflict_created",
            actor_id=actor.id,
            entity_type="appointment",
            entity_id=current.id,
            detail={"fields": sorted(wanted)},
        )
        return conflict_id
