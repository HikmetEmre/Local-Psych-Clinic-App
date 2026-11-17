"""Ajandam: the clinic owner's diary.

Her clinic appointments (read from the clinic calendar, booked in her name) and her
personal plans, in one place. Plans are private: encrypted text, visible and editable
only by their owner with admin.users (row-level security too), never synced.

Times are Europe/Istanbul wall-clock times. An all-day plan runs from local midnight of
its first day to local midnight after its last day.
"""

from __future__ import annotations

import calendar as pycal
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_local.randevular.service import AppointmentService
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.timeutil import ISTANBUL, to_istanbul

CATEGORIES = {
    "personal": "Kişisel",
    "meeting": "Toplantı",
    "training": "Eğitim / süpervizyon",
    "admin": "İdari iş",
    "travel": "Seyahat",
    "other": "Diğer",
}
REPEATS = {"none": "Tekrar yok", "daily": "Her gün", "weekly": "Her hafta", "monthly": "Her ay"}
MAX_TITLE = 120
MAX_LOCATION = 120
MAX_NOTE = 5000
MAX_REPEAT = timedelta(days=2 * 365)
REPEAT_SPAN = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=28),
}


class PlanError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class PlanNotFound(Exception):
    pass


class PlanVersionConflict(Exception):
    pass


@dataclass(frozen=True)
class PlanInput:
    title: str
    category: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    location: str = ""
    note: str = ""
    repeat: str = "none"
    repeat_until: date | None = None


@dataclass
class Item:
    """One thing on the diary: an occurrence of a plan, or a clinic appointment."""

    kind: str  # "plan" | "appointment"
    id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    category: str = "personal"
    location: str = ""
    note: str = ""
    repeat: str = "none"
    occurrence: date | None = None  # the plan occurrence's first day (for edit / delete one)
    status: str = ""
    room: str = ""
    client_code: str = ""

    @property
    def local_start(self) -> datetime:
        return to_istanbul(self.starts_at)

    @property
    def local_end(self) -> datetime:
        return to_istanbul(self.ends_at)

    @property
    def url(self) -> str:
        if self.kind == "appointment":
            return f"/randevular/{self.id}"
        when = self.occurrence.isoformat() if self.occurrence else ""
        return f"/ajanda/{self.id}/duzenle?tarih={when}"

    @property
    def css(self) -> str:
        return "ag-ev--appt" if self.kind == "appointment" else f"ag-ev--{self.category}"

    @property
    def time_text(self) -> str:
        if self.all_day:
            return "Tüm gün"
        return f"{self.local_start:%H:%M}–{self.local_end:%H:%M}"


def _add_months(day: date, months: int) -> date:
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, pycal.monthrange(year, month)[1]))


def occurrence_starts(row: Any, first: date, last: date) -> list[date]:
    """Local first days of a plan's occurrences that may touch [first, last]."""
    start = to_istanbul(row.starts_at).date()
    span_days = (row.ends_at - row.starts_at).days + 1
    if row.repeat == "none":
        return [start] if start <= last and start + timedelta(days=span_days) >= first else []
    until = min(row.repeat_until or last, last)
    skipped = set(row.skip_dates or [])
    out: list[date] = []
    n = 0
    while True:
        if row.repeat == "daily":
            day = start + timedelta(days=n)
        elif row.repeat == "weekly":
            day = start + timedelta(days=7 * n)
        else:
            day = _add_months(start, n)
        if day > until:
            break
        if day + timedelta(days=span_days) >= first and day not in skipped:
            out.append(day)
        n += 1
        if n > 800:  # two years of days at most (guarded by the form too)
            break
    return out


class PlanService:
    def __init__(self, cipher: FieldCipher) -> None:
        self.cipher = cipher
        self.appointments = AppointmentService(cipher)

    # ------------------------------------------------------------ reading

    async def items(
        self,
        conn: AsyncConnection,
        user: CurrentUser,
        first: date,
        last: date,
        *,
        plans: bool = True,
        appointments: bool = True,
    ) -> list[Item]:
        out: list[Item] = []
        if appointments:
            days = (last - first).days + 1
            for a in await self.appointments.in_range(
                conn, first, days, therapist_id=user.id, include_cancelled=False
            ):
                out.append(
                    Item(
                        kind="appointment", id=a.id, title=a.client_name, starts_at=a.starts_at,
                        ends_at=a.ends_at, status=a.status, room=a.room_label,
                        client_code=a.client_code,
                    )
                )  # fmt: skip
        if plans:
            p = t.personal_events
            window_end = datetime.combine(last + timedelta(days=1), time(0), tzinfo=ISTANBUL)
            window_start = datetime.combine(first, time(0), tzinfo=ISTANBUL)
            rows = (
                await conn.execute(
                    select(p).where(
                        p.c.owner_id == user.id,
                        p.c.deleted_at.is_(None),
                        p.c.starts_at < window_end,
                        # a repeating plan may have started long before the window
                        (p.c.repeat != "none") | (p.c.ends_at > window_start),
                    )
                )
            ).all()
            for row in rows:
                local_start = to_istanbul(row.starts_at)
                duration = row.ends_at - row.starts_at
                text = self._texts(row)
                for day in occurrence_starts(row, first, last):
                    starts = datetime.combine(
                        day, local_start.timetz().replace(tzinfo=None), tzinfo=ISTANBUL
                    )
                    out.append(
                        Item(
                            kind="plan", id=row.id, title=text["title"], starts_at=starts,
                            ends_at=starts + duration, all_day=row.all_day,
                            category=row.category, location=text["location"], note=text["note"],
                            repeat=row.repeat, occurrence=day,
                        )
                    )  # fmt: skip
        out.sort(key=lambda i: (i.starts_at, not i.all_day, i.title))
        return out

    def _texts(self, row: Any) -> dict[str, str]:
        return {
            "title": self.cipher.decrypt(row.title_encrypted),
            "location": self.cipher.decrypt_optional(row.location_encrypted) or "",
            "note": self.cipher.decrypt_optional(row.note_encrypted) or "",
        }

    async def get(self, conn: AsyncConnection, user: CurrentUser, plan_id: UUID) -> dict[str, Any]:
        p = t.personal_events
        row = (
            await conn.execute(
                select(p).where(
                    p.c.id == plan_id, p.c.owner_id == user.id, p.c.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if row is None:
            raise PlanNotFound
        return {**row._mapping, **self._texts(row)}

    async def count(self, conn: AsyncConnection, user: CurrentUser) -> int:
        p = t.personal_events
        value = (
            await conn.execute(
                select(func.count()).where(p.c.owner_id == user.id, p.c.deleted_at.is_(None))
            )
        ).scalar_one()
        return int(value)

    # ------------------------------------------------------------ writing

    def _validate(self, data: PlanInput) -> PlanInput:
        title = " ".join(data.title.split())
        location = " ".join(data.location.split())
        note = data.note.strip()
        if not title:
            raise PlanError("title", "Başlık yazın.")
        if len(title) > MAX_TITLE:
            raise PlanError("title", f"Başlık en fazla {MAX_TITLE} karakter olabilir.")
        if len(location) > MAX_LOCATION:
            raise PlanError("location", f"Yer en fazla {MAX_LOCATION} karakter olabilir.")
        if len(note) > MAX_NOTE:
            raise PlanError("note", "Not çok uzun.")
        if data.category not in CATEGORIES:
            raise PlanError("category", "Tür seçin.")
        if data.repeat not in REPEATS:
            raise PlanError("repeat", "Tekrar seçimi geçersiz.")
        if data.ends_at <= data.starts_at:
            raise PlanError("ends_at", "Bitiş, başlangıçtan sonra olmalı.")
        duration = data.ends_at - data.starts_at
        if duration > timedelta(days=31):
            raise PlanError("ends_at", "Bir plan en fazla 31 gün sürebilir.")
        until = data.repeat_until
        if data.repeat == "none":
            until = None
        else:
            first_day = to_istanbul(data.starts_at).date()
            if until is None:
                raise PlanError("repeat_until", "Tekrarın biteceği tarihi seçin.")
            if until < first_day:
                raise PlanError("repeat_until", "Tekrar bitişi, ilk günden önce olamaz.")
            if until - first_day > MAX_REPEAT:
                raise PlanError("repeat_until", "Tekrar en fazla 2 yıl sürebilir.")
            if duration > REPEAT_SPAN[data.repeat]:
                raise PlanError("ends_at", "Plan, tekrar aralığından uzun olamaz.")
        return PlanInput(
            title, data.category, data.starts_at, data.ends_at, data.all_day, location, note,
            data.repeat, until,
        )  # fmt: skip

    def _values(self, data: PlanInput) -> dict[str, Any]:
        return {
            "category": data.category,
            "title_encrypted": self.cipher.encrypt(data.title),
            "location_encrypted": self.cipher.encrypt_optional(data.location or None),
            "note_encrypted": self.cipher.encrypt_optional(data.note or None),
            "all_day": data.all_day,
            "starts_at": data.starts_at,
            "ends_at": data.ends_at,
            "repeat": data.repeat,
            "repeat_until": data.repeat_until,
        }

    async def create(self, conn: AsyncConnection, user: CurrentUser, data: PlanInput) -> UUID:
        data = self._validate(data)
        p = t.personal_events
        plan_id: UUID = (
            await conn.execute(
                insert(p).values(owner_id=user.id, **self._values(data)).returning(p.c.id)
            )
        ).scalar_one()
        await write_audit(
            conn, t.audit_log, action="plan_created", actor_id=user.id,
            entity_type="personal_event", entity_id=plan_id,
        )  # fmt: skip
        return plan_id

    async def update(
        self,
        conn: AsyncConnection,
        user: CurrentUser,
        plan_id: UUID,
        data: PlanInput,
        expected_version: int,
    ) -> None:
        data = self._validate(data)
        p = t.personal_events
        current = (
            await conn.execute(
                select(p.c.version)
                .where(p.c.id == plan_id, p.c.owner_id == user.id, p.c.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            raise PlanNotFound
        if current != expected_version:
            raise PlanVersionConflict
        await conn.execute(
            update(p).where(p.c.id == plan_id).values(**self._values(data), version=p.c.version + 1)
        )
        await write_audit(
            conn, t.audit_log, action="plan_updated", actor_id=user.id,
            entity_type="personal_event", entity_id=plan_id,
        )  # fmt: skip

    async def delete(
        self, conn: AsyncConnection, user: CurrentUser, plan_id: UUID, only: date | None = None
    ) -> None:
        """Delete the whole plan, or (``only``) one occurrence of a repeating plan."""
        p = t.personal_events
        row = (
            await conn.execute(
                select(p.c.repeat, p.c.skip_dates)
                .where(p.c.id == plan_id, p.c.owner_id == user.id, p.c.deleted_at.is_(None))
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise PlanNotFound
        if only is not None and row.repeat != "none":
            values: dict[str, Any] = {"skip_dates": sorted({*row.skip_dates, only})}
        else:
            values = {"deleted_at": func.now()}
        await conn.execute(
            update(p).where(p.c.id == plan_id).values(**values, version=p.c.version + 1)
        )
        await write_audit(
            conn, t.audit_log, action="plan_deleted", actor_id=user.id,
            entity_type="personal_event", entity_id=plan_id,
            detail={"occurrence": only.isoformat() if only and row.repeat != "none" else None},
        )  # fmt: skip


# ---------------------------------------------------------------- layout

HOUR_START = 7
HOUR_END = 22
QUARTERS = (HOUR_END - HOUR_START) * 4
MAX_LANES = 4


@dataclass
class Placed:
    item: Item
    top: int  # quarter-hours from HOUR_START
    height: int
    lane: int = 0
    lanes: int = 1
    clipped_before: bool = False
    clipped_after: bool = False

    @property
    def css(self) -> str:
        lanes = min(self.lanes, MAX_LANES)
        lane = min(self.lane, lanes - 1)
        return f"ag-t-{self.top} ag-h-{self.height} ag-l-{lane}-{lanes}"


@dataclass
class DayColumn:
    day: date
    is_today: bool
    all_day: list[Item] = field(default_factory=list)
    timed: list[Placed] = field(default_factory=list)


def _quarter(moment: datetime, day: date) -> int:
    base = datetime.combine(day, time(HOUR_START), tzinfo=ISTANBUL)
    minutes = (to_istanbul(moment) - base).total_seconds() / 60
    return int(max(0, min(QUARTERS, minutes // 15)))


def columns(days: list[date], items: list[Item], today: date) -> list[DayColumn]:
    out = []
    for day in days:
        col = DayColumn(day, day == today)
        day_start = datetime.combine(day, time(0), tzinfo=ISTANBUL)
        day_end = day_start + timedelta(days=1)
        grid_start = datetime.combine(day, time(HOUR_START), tzinfo=ISTANBUL)
        grid_end = datetime.combine(day, time(HOUR_END), tzinfo=ISTANBUL)
        for item in items:
            if item.starts_at >= day_end or item.ends_at <= day_start:
                continue
            if item.all_day or (item.ends_at - item.starts_at) >= timedelta(hours=20):
                col.all_day.append(item)
                continue
            start = max(item.starts_at, day_start)
            end = min(item.ends_at, day_end)
            top = _quarter(start, day)
            bottom = _quarter(end, day)
            if end <= grid_start:
                top, bottom = 0, 1
            elif start >= grid_end:
                top, bottom = QUARTERS - 1, QUARTERS
            if end > grid_start and end - start < timedelta(minutes=15):
                bottom = top + 1
            col.timed.append(
                Placed(
                    item, top, max(1, bottom - top), clipped_before=start < grid_start,
                    clipped_after=end > grid_end,
                )
            )  # fmt: skip
        _assign_lanes(col.timed)
        out.append(col)
    return out


def _assign_lanes(placed: list[Placed]) -> None:
    """Side by side when they overlap: lanes per cluster of overlapping blocks."""
    placed.sort(key=lambda p: (p.top, -p.height))
    cluster: list[Placed] = []
    cluster_end = -1
    lane_ends: list[int] = []

    def close() -> None:
        for p in cluster:
            p.lanes = max(1, len(lane_ends))

    for p in placed:
        if p.top >= cluster_end and cluster:
            close()
            cluster, lane_ends = [], []
        for index, end in enumerate(lane_ends):
            if end <= p.top:
                p.lane = index
                lane_ends[index] = p.top + p.height
                break
        else:
            p.lane = len(lane_ends)
            lane_ends.append(p.top + p.height)
        cluster.append(p)
        cluster_end = max(cluster_end, p.top + p.height)
    if cluster:
        close()


def now_quarter(now: datetime) -> int | None:
    local = to_istanbul(now)
    if not HOUR_START <= local.hour < HOUR_END:
        return None
    return _quarter(local, local.date())
