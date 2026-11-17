"""Week / day calendar grid shared by Klinik Web and Klinik Kasa (Europe/Istanbul)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol
from uuid import UUID

from klinik_shared.timeutil import now_utc, to_istanbul

DAY_START = 8
DAY_END = 21  # exclusive
DAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
MONTHS_TR = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)  # fmt: skip


class Placeable(Protocol):
    @property
    def starts_at(self) -> datetime: ...

    @property
    def therapist_id(self) -> UUID: ...


@dataclass(frozen=True)
class Person:
    id: UUID
    label: str


@dataclass
class Column:
    key: str
    title: str
    subtitle: str
    day: date
    therapist_id: UUID | None = None
    is_today: bool = False


@dataclass
class Grid[T: Placeable]:
    columns: list[Column]
    hours: list[int]
    cells: dict[tuple[int, int], list[T]] = field(default_factory=dict)

    def at(self, column: int, hour: int) -> list[T]:
        return self.cells.get((column, hour), [])


def today_istanbul() -> date:
    return to_istanbul(now_utc()).date()


def first_day(view: str, day: date) -> date:
    return day if view == "gun" else day - timedelta(days=day.weekday())


def span(view: str) -> int:
    return 1 if view == "gun" else 7


def build_grid[T: Placeable](
    view: str, day: date, items: Sequence[T], therapists: Sequence[Person]
) -> Grid[T]:
    """Day view: one column per therapist. Week view: one column per day."""
    today = today_istanbul()
    if view == "gun":
        columns = [
            Column(str(th.id), th.label, "", day, th.id, day == today) for th in therapists
        ] or [Column("all", "Randevular", "", day, None, day == today)]
    else:
        monday = first_day("hafta", day)
        columns = []
        for i in range(7):
            d = monday + timedelta(days=i)
            columns.append(Column(d.isoformat(), DAYS_TR[i], f"{d:%d.%m}", d, None, d == today))

    hours = set(range(DAY_START, DAY_END))
    cells: dict[tuple[int, int], list[T]] = {}
    for item in items:
        local = to_istanbul(item.starts_at)
        for index, column in enumerate(columns):
            if local.date() != column.day:
                continue
            if column.therapist_id is not None and column.therapist_id != item.therapist_id:
                continue
            hours.add(local.hour)
            cells.setdefault((index, local.hour), []).append(item)
    return Grid(columns, sorted(hours), cells)


def parse_day(value: str | None) -> date:
    try:
        return date.fromisoformat(value) if value else today_istanbul()
    except ValueError:
        return today_istanbul()


def title_for(view: str, day: date) -> str:
    if view == "gun":
        return f"{day.day} {MONTHS_TR[day.month - 1]} {day.year}, {DAYS_TR[day.weekday()]}"
    monday = first_day("hafta", day)
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {MONTHS_TR[monday.month - 1]} {monday.year}"
    return (
        f"{monday.day} {MONTHS_TR[monday.month - 1]} – "
        f"{sunday.day} {MONTHS_TR[sunday.month - 1]} {sunday.year}"
    )
