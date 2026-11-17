from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from klinik_cloud.gelir.service import PaymentError, format_try, parse_amount, period_for


@pytest.mark.parametrize(
    ("typed", "value"),
    [
        ("1500", "1500.00"),
        ("1.500", "1500.00"),
        ("1.500,50", "1500.50"),
        ("1500,5", "1500.50"),
        ("1500.50", "1500.50"),
        ("₺ 750", "750.00"),
        ("750 TL", "750.00"),
        ("123.456", "123456.00"),
        ("0,01", "0.01"),
    ],
)
def test_amounts_as_people_type_them(typed: str, value: str) -> None:
    assert parse_amount(typed) == Decimal(value)


@pytest.mark.parametrize(
    "typed", ["", "abc", "-50", "0", "0,00", "1,005", "1.500,505", "1.000.001", "1e3", "12,34,56"]
)
def test_bad_amounts(typed: str) -> None:
    with pytest.raises(PaymentError):
        parse_amount(typed)


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (Decimal("0"), "0,00 ₺"),
        (Decimal("5"), "5,00 ₺"),
        (Decimal("1500"), "1.500,00 ₺"),
        (Decimal("1234567.891"), "1.234.567,89 ₺"),
        (Decimal("999.995"), "1.000,00 ₺"),
    ],
)
def test_turkish_money_format(value: Decimal, shown: str) -> None:
    assert format_try(value) == shown


def test_periods() -> None:
    day = date(2026, 11, 18)  # a Wednesday
    assert (period_for("gun", day).first, period_for("gun", day).last) == (day, day)
    week = period_for("hafta", day)
    assert (week.first, week.last) == (date(2026, 11, 16), date(2026, 11, 22))
    month = period_for("ay", day)
    assert (month.first, month.last) == (date(2026, 11, 1), date(2026, 11, 30))
    december = period_for("ay", date(2026, 12, 31))
    assert december.last == date(2026, 12, 31) and december.step(1) == date(2027, 1, 1)
    assert period_for("ay", date(2027, 1, 5)).step(-1) == date(2026, 12, 1)
    assert len(period_for("ay", date(2028, 2, 10)).days) == 29  # leap year


def test_month_bounds_are_istanbul_midnights() -> None:
    from datetime import UTC, datetime

    start, end = period_for("ay", date(2026, 11, 5)).bounds
    assert start == datetime(2026, 10, 31, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 30, 21, 0, tzinfo=UTC)
