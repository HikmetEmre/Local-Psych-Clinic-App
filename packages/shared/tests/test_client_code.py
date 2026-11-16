from __future__ import annotations

import re

import pytest

from klinik_shared.client_code import (
    CLIENT_CODE_PATTERN,
    ClientCode,
    ClientCodeError,
    format_client_code,
    is_valid_client_code,
    parse_client_code,
)


def test_format_pads_to_four_digits() -> None:
    assert format_client_code(2026, 42) == "DN-2026-0042"
    assert format_client_code(2026, 9999) == "DN-2026-9999"


def test_parse_roundtrip() -> None:
    code = parse_client_code("DN-2026-0042")
    assert code == ClientCode(2026, 42)
    assert str(code) == "DN-2026-0042"


@pytest.mark.parametrize(
    "bad",
    [
        "DN-0042",  # the short form from the brief's diagram is not valid
        "dn-2026-0042",
        "DN-2026-42",
        "DN-2026-00420",
        "DN-26-0042",
        " DN-2026-0042",
        "DN-2026-0042\n",
        "DN-2026-0000",
        "DN-1999-0001",
        "DN-２０２６-0042",  # full-width digits
        "",
        None,
        20260042,
    ],
)
def test_invalid_codes_are_rejected(bad: object) -> None:
    assert not is_valid_client_code(bad)
    with pytest.raises(ClientCodeError):
        parse_client_code(bad)


def test_number_overflow_is_an_error_not_a_longer_code() -> None:
    with pytest.raises(ClientCodeError):
        format_client_code(2026, 10000)
    with pytest.raises(ClientCodeError):
        format_client_code(2026, 0)


def test_codes_sort_by_year_then_number() -> None:
    codes = [ClientCode(2026, 2), ClientCode(2025, 900), ClientCode(2026, 1)]
    assert sorted(codes) == [ClientCode(2025, 900), ClientCode(2026, 1), ClientCode(2026, 2)]


def test_sql_pattern_agrees_with_parser() -> None:
    # The same pattern goes into the DB CHECK constraints.
    assert re.fullmatch(CLIENT_CODE_PATTERN, "DN-2026-0042")
    assert not re.fullmatch(CLIENT_CODE_PATTERN, "DN-0042")
