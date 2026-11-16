from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from klinik_shared.timeutil import (
    age_on,
    format_datetime_tr,
    format_short_tr,
    istanbul_day_bounds,
    to_istanbul,
)


def test_display_is_istanbul_time() -> None:
    stored = datetime(2026, 11, 12, 11, 0, tzinfo=UTC)
    assert format_datetime_tr(stored) == "12.11.2026 14:00"
    assert format_short_tr(stored) == "12.11 Perş 14:00"


def test_late_utc_evening_is_already_tomorrow_in_istanbul() -> None:
    stored = datetime(2026, 11, 12, 22, 30, tzinfo=UTC)
    assert to_istanbul(stored).date() == date(2026, 11, 13)


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="naive"):
        format_datetime_tr(datetime(2026, 11, 12, 14, 0))


def test_day_bounds_are_half_open_and_in_utc() -> None:
    start, end = istanbul_day_bounds(date(2026, 11, 12))
    assert start == datetime(2026, 11, 11, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 12, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("birth", "at", "expected"),
    [
        (date(2014, 6, 15), date(2026, 6, 14), 11),  # day before the birthday
        (date(2014, 6, 15), date(2026, 6, 15), 12),  # the birthday itself counts
        (date(2013, 11, 16), date(2026, 11, 15), 12),
        (date(2013, 11, 16), date(2026, 11, 16), 13),
        (date(2008, 2, 29), date(2026, 2, 28), 17),  # leap-day birthday
        (date(2008, 2, 29), date(2026, 3, 1), 18),
        (date(2026, 1, 1), date(2026, 1, 1), 0),
    ],
)
def test_age_on(birth: date, at: date, expected: int) -> None:
    assert age_on(birth, at) == expected


def test_age_before_birth_is_an_error() -> None:
    with pytest.raises(ValueError, match="before birth"):
        age_on(date(2026, 1, 2), date(2026, 1, 1))
