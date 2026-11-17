"""Appointments in the office app (one app, no cloud).

- No double booking: three exclusion constraints in the database (therapist, room,
  client). A clash comes back as ``OverlapError`` with a Turkish message.
- Optimistic locking: every edit carries the version it was based on. If someone else
  saved in between, edits to *different* fields are merged; an edit to the *same* field
  is refused with the current values, never overwritten silently.
- Outcomes (completed / no-show) come from the therapist's session record.
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

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.enums import AppointmentStatus
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import istanbul_day_bounds, now_utc

EDITABLE = ("starts_at", "duration_min", "therapist_id", "room_id", "status")
CALENDAR_STATUSES = (AppointmentStatus.PLANNED, AppointmentStatus.CANCELLED)
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

    def __init__(self, current: Appointment, fields: list[str]) -> None:
        super().__init__("version conflict")
        self.current = current
        self.fields = fields


class AppointmentFields(BaseModel):
    """What a person can set on an appointment."""

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
    client_id: UUID
    client_code: str
    client_name: str
    therapist_id: UUID
    therapist_name: str
    room_id: UUID
    room_label: str
    starts_at: datetime
    ends_at: datetime
    duration_min: int
    status: str
    version: int
    session_id: UUID | None = None

    def value(self, field: str) -> Any:
        return getattr(self, field)


@dataclass(frozen=True)
class Option:
    id: UUID
    label: str


def _constraint(exc: IntegrityError) -> str | None:
    orig = exc.orig
    cause = getattr(orig, "__cause__", None) or orig
    return getattr(cause, "constraint_name", None)


def _same(field: str, a: Any, b: Any) -> bool:
    if field == "status":
        return str(a) == str(b)
    return bool(a == b)


a_ = t.appointments
_SELECT = select(
    a_.c.id,
    a_.c.client_id,
    t.clients.c.client_code,
    t.clients.c.full_name_encrypted,
    a_.c.therapist_id,
    t.users.c.display_name.label("therapist_name"),
    a_.c.room_id,
    t.rooms.c.label.label("room_label"),
    a_.c.starts_at,
    a_.c.ends_at,
    a_.c.duration_min,
    a_.c.status,
    a_.c.version,
).select_from(
    a_.join(t.users, t.users.c.id == a_.c.therapist_id)
    .join(t.rooms, t.rooms.c.id == a_.c.room_id)
    .join(t.clients, t.clients.c.id == a_.c.client_id)
)


class AppointmentService:
    def __init__(self, cipher: FieldCipher) -> None:
        self.cipher = cipher

    def _row(self, r: Any, session_id: UUID | None = None) -> Appointment:
        return Appointment(
            r.id, r.client_id, r.client_code, self.cipher.decrypt(r.full_name_encrypted),
            r.therapist_id, r.therapist_name, r.room_id, r.room_label, r.starts_at, r.ends_at,
            r.duration_min, r.status, r.version, session_id,
        )  # fmt: skip

    # ------------------------------------------------------------ reading

    async def in_range(
        self,
        conn: AsyncConnection,
        first_day: date,
        days: int,
        *,
        therapist_id: UUID | None = None,
        room_id: UUID | None = None,
        include_cancelled: bool = True,
    ) -> list[Appointment]:
        start = istanbul_day_bounds(first_day)[0]
        end = istanbul_day_bounds(first_day + timedelta(days=days - 1))[1]
        stmt = _SELECT.where(a_.c.deleted_at.is_(None), a_.c.starts_at < end, a_.c.ends_at > start)
        if therapist_id:
            stmt = stmt.where(a_.c.therapist_id == therapist_id)
        if room_id:
            stmt = stmt.where(a_.c.room_id == room_id)
        if not include_cancelled:
            stmt = stmt.where(a_.c.status != AppointmentStatus.CANCELLED.value)
        rows = (await conn.execute(stmt.order_by(a_.c.starts_at, t.users.c.display_name))).all()
        # Linked session records the viewer may see (row-level security filters the rest).
        s = t.therapy_sessions
        linked = {
            r.appointment_id: r.id
            for r in (
                await conn.execute(
                    select(s.c.id, s.c.appointment_id).where(
                        s.c.appointment_id.in_([r.id for r in rows]), s.c.deleted_at.is_(None)
                    )
                )
            ).all()
        } if rows else {}  # fmt: skip
        return [self._row(r, linked.get(r.id)) for r in rows]

    async def counts_by_day(
        self, conn: AsyncConnection, first_day: date, days: int
    ) -> dict[date, int]:
        """Non-cancelled appointments per Istanbul day (for the month calendar)."""
        from sqlalchemy import func

        start = istanbul_day_bounds(first_day)[0]
        end = istanbul_day_bounds(first_day + timedelta(days=days - 1))[1]
        local_day = func.date(func.timezone("Europe/Istanbul", a_.c.starts_at))
        rows = (
            await conn.execute(
                select(local_day, func.count())
                .where(
                    a_.c.deleted_at.is_(None),
                    a_.c.status != AppointmentStatus.CANCELLED.value,
                    a_.c.starts_at >= start,
                    a_.c.starts_at < end,
                )
                .group_by(local_day)
            )
        ).all()
        return {r[0]: int(r[1]) for r in rows}

    async def get(
        self, conn: AsyncConnection, appointment_id: UUID, *, lock: bool = False
    ) -> Appointment:
        stmt = _SELECT.where(a_.c.id == appointment_id, a_.c.deleted_at.is_(None))
        if lock:
            stmt = stmt.with_for_update(of=a_)
        row = (await conn.execute(stmt)).one_or_none()
        if row is None:
            raise AppointmentNotFound
        return self._row(row)

    async def session_for(self, conn: AsyncConnection, appointment_id: UUID) -> UUID | None:
        """The linked session record, if the viewer may see it (row-level security)."""
        s = t.therapy_sessions
        value = (
            await conn.execute(
                select(s.c.id).where(s.c.appointment_id == appointment_id, s.c.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        return value if isinstance(value, UUID) else None

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
        stmt = select(r.c.id, r.c.label).order_by(r.c.sort_order, r.c.label)
        if active_only:
            stmt = stmt.where(r.c.is_active)
        return [Option(x.id, x.label) for x in (await conn.execute(stmt)).all()]

    async def default_room(self, conn: AsyncConnection, therapist_id: UUID) -> UUID | None:
        value = (
            await conn.execute(
                select(t.users.c.default_room_id).where(t.users.c.id == therapist_id)
            )
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
        if "status" in fields and fields["status"] not in CALENDAR_STATUSES:
            # Completed / no-show come from the therapist's session record.
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
        client_id: UUID,
        fields: AppointmentFields,
        appointment_id: UUID | None = None,
    ) -> Appointment:
        data = fields.changes()
        for required, message in (
            ("starts_at", "Tarih ve saat zorunludur."),
            ("therapist_id", "Terapist seçin."),
        ):
            if required not in data:
                raise AppointmentError(required, message)
        if "room_id" not in data:
            room = await self.default_room(conn, data["therapist_id"])
            rooms = await self.rooms(conn)
            if room is None and rooms:
                room = rooms[0].id
            if room is None:
                raise AppointmentError("room_id", "Önce Ayarlar → Odalar'dan bir oda ekleyin.")
            data["room_id"] = room
        data.setdefault("duration_min", 50)
        data["status"] = AppointmentStatus.PLANNED.value
        c = t.clients
        client = (
            await conn.execute(
                select(c.c.is_active).where(c.c.id == client_id, c.c.deleted_at.is_(None))
            )
        ).one_or_none()
        if client is None:
            raise AppointmentError("client_id", "Danışan seçin.")
        if not client.is_active:
            raise AppointmentError("client_id", "Bu danışan pasif. Yeni randevu verilemez.")
        await self._validate(conn, data, is_new=True)

        values = {**data, "client_id": client_id, "created_by": actor.id, "updated_by": actor.id}
        if appointment_id is not None:
            values["id"] = appointment_id
        new_id = (
            await self._write(conn, insert(a_).values(**values).returning(a_.c.id))
        ).scalar_one()
        await write_audit(
            conn, t.audit_log, action="appointment_created", actor_id=actor.id,
            entity_type="appointment", entity_id=new_id,
        )  # fmt: skip
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
        """Apply the editor's changes. Returns (appointment, merged)."""
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
                raise BookingConflict(current, clash)
            wanted = {f: wanted[f] for f in mine}
            merged = True

        wanted = {k: v for k, v in wanted.items() if not _same(k, v, current.value(k))}
        if not wanted:
            return current, merged
        await self._validate(conn, wanted, is_new=False)
        await self._write(
            conn,
            update(a_)
            .where(and_(a_.c.id == appointment_id, a_.c.version == current.version))
            .values(**wanted, version=a_.c.version + 1, updated_by=actor.id),
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


async def set_outcome(
    conn: AsyncConnection, actor: CurrentUser, appointment_id: UUID, status: AppointmentStatus
) -> None:
    """Record a session outcome on its appointment (completed / no-show, or back to planned).

    Called in the same transaction as the session change. A cancelled appointment keeps
    its status: cancelling is a calendar decision, not the session's.
    """
    await conn.execute(
        update(a_)
        .where(
            a_.c.id == appointment_id,
            a_.c.deleted_at.is_(None),
            a_.c.status != AppointmentStatus.CANCELLED.value,
            a_.c.status != status.value,
        )
        .values(status=status.value, version=a_.c.version + 1, updated_by=actor.id)
    )
