"""Notifications (brief §8): outbox rows, recipients, quiet hours, text, inbox, reminders.

Rule: notify everyone affected, never the person who made the change.
Privacy: push text carries a date, a time and a client code. Never a name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_cloud import tables as t
from klinik_shared.enums import AppointmentStatus, NotificationEvent
from klinik_shared.labels_tr import NOTIFICATION_TITLES
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import format_short_tr, now_utc, to_istanbul

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(8, 0)
REMINDER_WINDOWS = {
    NotificationEvent.REMINDER_24H: (timedelta(hours=23), timedelta(hours=24)),
    NotificationEvent.REMINDER_1H: (timedelta(minutes=45), timedelta(minutes=60)),
}
ICONS = {
    NotificationEvent.APPOINTMENT_CREATED: "🗓",
    NotificationEvent.APPOINTMENT_UPDATED: "🗓",
    NotificationEvent.APPOINTMENT_CANCELLED: "🗓",
    NotificationEvent.REMINDER_24H: "⏰",
    NotificationEvent.REMINDER_1H: "⏰",
    NotificationEvent.CONFLICT_CREATED: "⚠️",
}
FIELD_LABELS = {
    "starts_at": "saat",
    "duration_min": "süre",
    "therapist_id": "terapist",
    "room_id": "oda",
    "status": "durum",
}


class NotificationPayload(BaseModel):
    """Everything a notification may carry. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")

    appointment_id: UUID | None = None
    client_code: str | None = Field(default=None, pattern=r"^DN-\d{4}-\d{4}$")
    starts_at: AwareDatetime | None = None
    therapist_id: UUID | None = None
    changed_fields: list[str] = Field(default_factory=list, max_length=10)
    actor_id: UUID | None = None
    conflict_id: UUID | None = None


@dataclass(frozen=True)
class Settings:
    quiet_start: time = DEFAULT_QUIET_START
    quiet_end: time = DEFAULT_QUIET_END
    push_enabled: bool = True
    reminder_24h: bool = True
    reminder_1h: bool = True


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    url: str
    tag: str


# ---------------------------------------------------------------- quiet hours


def in_quiet_hours(moment: datetime, start: time, end: time) -> bool:
    local = to_istanbul(moment).time()
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end  # the window crosses midnight


def push_after_for(moment: datetime, start: time, end: time) -> datetime:
    """When a push may go out: now, or the end of the quiet window (Istanbul time)."""
    if not in_quiet_hours(moment, start, end):
        return moment
    local = to_istanbul(moment)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


# ---------------------------------------------------------------- text


def render(event: str, payload: dict[str, Any]) -> Rendered:
    data = NotificationPayload.model_validate(payload)
    kind = NotificationEvent(event)
    title = NOTIFICATION_TITLES.get(kind.value, "Bildirim")
    parts = []
    if data.starts_at is not None:
        parts.append(format_short_tr(data.starts_at))
    if data.client_code:
        parts.append(data.client_code)
    body = " · ".join(parts)
    if kind is NotificationEvent.APPOINTMENT_UPDATED and data.changed_fields:
        changed = ", ".join(FIELD_LABELS.get(f, f) for f in data.changed_fields)
        body = f"{body} ({changed} değişti)" if body else f"{changed} değişti"
    if kind is NotificationEvent.CONFLICT_CREATED:
        url = "/cakismalar"
    elif data.starts_at is not None:
        url = f"/takvim?gun={to_istanbul(data.starts_at).date().isoformat()}"
    else:
        url = "/bildirimler"
    tag = f"appt-{data.appointment_id}" if data.appointment_id else f"n-{kind.value}"
    return Rendered(f"{ICONS.get(kind, '🔔')} {title}", body, url, tag)


# ---------------------------------------------------------------- settings


async def settings_for(conn: AsyncConnection, user_id: UUID) -> Settings:
    s = t.user_settings
    row = (await conn.execute(select(s).where(s.c.user_id == user_id))).one_or_none()
    if row is None:
        return Settings()
    return Settings(
        row.quiet_start, row.quiet_end, row.push_enabled, row.reminder_24h, row.reminder_1h
    )


async def save_settings(conn: AsyncConnection, user_id: UUID, settings: Settings) -> None:
    s = t.user_settings
    values = {
        "quiet_start": settings.quiet_start,
        "quiet_end": settings.quiet_end,
        "push_enabled": settings.push_enabled,
        "reminder_24h": settings.reminder_24h,
        "reminder_1h": settings.reminder_1h,
    }
    await conn.execute(
        pg_insert(s)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(index_elements=[s.c.user_id], set_=values)
    )


# ---------------------------------------------------------------- writing (outbox)


async def recipients(
    conn: AsyncConnection, therapist_id: UUID | None, actor_id: UUID | None
) -> list[UUID]:
    """The assigned therapist and every scheduler, minus whoever made the change."""
    u, p = t.users, t.user_permissions
    schedulers = (
        await conn.execute(
            select(u.c.id)
            .join(p, p.c.user_id == u.c.id)
            .where(u.c.is_active, p.c.permission == Permission.SCHEDULE_WRITE_ALL.value)
        )
    ).scalars()
    wanted = set(schedulers)
    if therapist_id is not None:
        active = (
            await conn.execute(select(u.c.id).where(u.c.id == therapist_id, u.c.is_active))
        ).first()
        if active is not None:
            wanted.add(therapist_id)
    if actor_id is not None:
        wanted.discard(actor_id)
    return sorted(wanted, key=str)


async def _insert(
    conn: AsyncConnection,
    recipient: UUID,
    event: NotificationEvent,
    payload: NotificationPayload,
    *,
    expires_at: datetime | None = None,
    dedupe_key: str | None = None,
) -> bool:
    settings = await settings_for(conn, recipient)
    now = now_utc()
    stmt = (
        pg_insert(t.notifications)
        .values(
            recipient_id=recipient,
            event_type=event.value,
            appointment_id=payload.appointment_id,
            payload=payload.model_dump(mode="json", exclude_none=True),
            push_after=push_after_for(now, settings.quiet_start, settings.quiet_end),
            push_state="pending" if settings.push_enabled else "skipped",
            expires_at=expires_at,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=[t.notifications.c.dedupe_key])
        .returning(t.notifications.c.id)
    )
    inserted = (await conn.execute(stmt)).first()
    if inserted is not None:
        # Delivered on commit; open browser tabs of this user update their bell.
        await conn.execute(text("SELECT pg_notify('new_notification', :r)"), {"r": str(recipient)})
    return inserted is not None


async def notify_appointment(
    conn: AsyncConnection,
    event: NotificationEvent,
    *,
    appointment_id: UUID,
    client_code: str,
    starts_at: datetime,
    therapist_id: UUID,
    actor_id: UUID | None,
    changed_fields: Iterable[str] = (),
    extra_recipients: Iterable[UUID] = (),
) -> int:
    payload = NotificationPayload(
        appointment_id=appointment_id,
        client_code=client_code,
        starts_at=starts_at,
        therapist_id=therapist_id,
        changed_fields=sorted(changed_fields),
        actor_id=actor_id,
    )
    targets = set(await recipients(conn, therapist_id, actor_id)) | (
        set(extra_recipients) - {actor_id}
    )
    count = 0
    for recipient in sorted(targets, key=str):
        count += await _insert(conn, recipient, event, payload, expires_at=starts_at)
    return count


async def notify_conflict(
    conn: AsyncConnection,
    *,
    conflict_id: UUID,
    author_id: UUID | None,
    actor_id: UUID | None,
    appointment_id: UUID | None = None,
    client_code: str | None = None,
    starts_at: datetime | None = None,
) -> int:
    """The author of the refused change and the admins."""
    u, p = t.users, t.user_permissions
    admins = set(
        (
            await conn.execute(
                select(u.c.id)
                .join(p, p.c.user_id == u.c.id)
                .where(u.c.is_active, p.c.permission == Permission.ADMIN_USERS.value)
            )
        ).scalars()
    )
    targets = admins | ({author_id} if author_id else set())
    if actor_id is not None:
        targets.discard(actor_id)
    payload = NotificationPayload(
        appointment_id=appointment_id,
        client_code=client_code,
        starts_at=starts_at,
        actor_id=actor_id,
        conflict_id=conflict_id,
    )
    count = 0
    for recipient in sorted(targets, key=str):
        count += await _insert(conn, recipient, NotificationEvent.CONFLICT_CREATED, payload)
    return count


async def schedule_reminders(conn: AsyncConnection, now: datetime | None = None) -> int:
    """Insert due 24 h / 1 h reminders. Idempotent: the dedupe key includes the start time,
    so a rescheduled appointment gets fresh reminders and an unchanged one never repeats."""
    now = now or now_utc()
    a = t.appointments
    created = 0
    for event, (low, high) in REMINDER_WINDOWS.items():
        rows = (
            await conn.execute(
                select(a.c.id, a.c.client_code, a.c.starts_at, a.c.therapist_id).where(
                    a.c.status == AppointmentStatus.PLANNED.value,
                    a.c.deleted_at.is_(None),
                    a.c.starts_at > now + low,
                    a.c.starts_at <= now + high,
                )
            )
        ).all()
        for row in rows:
            settings = await settings_for(conn, row.therapist_id)
            wanted = (
                settings.reminder_24h
                if event is NotificationEvent.REMINDER_24H
                else settings.reminder_1h
            )
            if not wanted:
                continue
            payload = NotificationPayload(
                appointment_id=row.id,
                client_code=row.client_code,
                starts_at=row.starts_at,
                therapist_id=row.therapist_id,
            )
            key = f"reminder:{row.id}:{event.value}:{row.starts_at.astimezone(UTC).isoformat()}"
            created += await _insert(
                conn, row.therapist_id, event, payload, expires_at=row.starts_at, dedupe_key=key
            )
    return created


# ---------------------------------------------------------------- inbox


@dataclass(frozen=True)
class InboxItem:
    id: UUID
    event_type: str
    created_at: datetime
    read: bool
    rendered: Rendered


async def inbox(conn: AsyncConnection, user_id: UUID, limit: int = 100) -> list[InboxItem]:
    n = t.notifications
    rows = (
        await conn.execute(
            select(n)
            .where(n.c.recipient_id == user_id)
            .order_by(n.c.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        InboxItem(
            r.id, r.event_type, r.created_at, r.read_at is not None, render(r.event_type, r.payload)
        )
        for r in rows
    ]


async def unread_count(conn: AsyncConnection, user_id: UUID) -> int:
    n = t.notifications
    return int(
        (
            await conn.execute(
                select(func.count())
                .select_from(n)
                .where(n.c.recipient_id == user_id, n.c.read_at.is_(None))
            )
        ).scalar_one()
    )


async def mark_read(
    conn: AsyncConnection, user_id: UUID, notification_id: UUID | None = None
) -> None:
    n = t.notifications
    stmt = update(n).where(n.c.recipient_id == user_id, n.c.read_at.is_(None))
    if notification_id is not None:
        stmt = stmt.where(n.c.id == notification_id)
    await conn.execute(stmt.values(read_at=now_utc()))


async def add_subscription(
    conn: AsyncConnection, user_id: UUID, endpoint: str, p256dh: str, auth: str
) -> None:
    s = t.push_subscriptions
    await conn.execute(
        pg_insert(s)
        .values(user_id=user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
        .on_conflict_do_update(
            index_elements=[s.c.endpoint],
            set_={"user_id": user_id, "p256dh": p256dh, "auth": auth, "failure_count": 0},
        )
    )


async def remove_subscription(conn: AsyncConnection, user_id: UUID, endpoint: str) -> None:
    s = t.push_subscriptions
    await conn.execute(delete(s).where(s.c.user_id == user_id, s.c.endpoint == endpoint))
