"""Time helpers. Store ``timestamptz`` (UTC), display in Europe/Istanbul (brief §7)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")

_DAYS_TR_SHORT = ("Pzt", "Sal", "Çar", "Perş", "Cum", "Cmt", "Paz")


def now_utc() -> datetime:
    return datetime.now(UTC)


def require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; attach a timezone")
    return dt


def to_istanbul(dt: datetime) -> datetime:
    return require_aware(dt).astimezone(ISTANBUL)


def format_datetime_tr(dt: datetime) -> str:
    """``dd.MM.yyyy HH:mm`` in Istanbul time."""
    return to_istanbul(dt).strftime("%d.%m.%Y %H:%M")


def format_short_tr(dt: datetime) -> str:
    """``12.11 Perş 14:00``: the compact form used in push notifications."""
    local = to_istanbul(dt)
    return f"{local:%d.%m} {_DAYS_TR_SHORT[local.weekday()]} {local:%H:%M}"


def istanbul_day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` of a calendar day in Istanbul."""
    start = datetime.combine(day, time.min, tzinfo=ISTANBUL)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def age_on(birth_date: date, at: date) -> int:
    """Completed years of age on ``at`` (the birthday itself counts)."""
    if at < birth_date:
        raise ValueError("date is before birth date")
    had_birthday = (at.month, at.day) >= (birth_date.month, birth_date.day)
    return at.year - birth_date.year - (0 if had_birthday else 1)
