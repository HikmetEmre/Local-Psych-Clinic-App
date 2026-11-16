"""Pseudonymous client codes: ``DN-YYYY-NNNN`` (brief §4).

Codes are generated only in the local tier. The cloud knows codes, never identities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CLIENT_CODE_PATTERN = r"^DN-\d{4}-\d{4}$"
"""Regex shared by Python validation and the DB ``CHECK`` constraints."""

_RE = re.compile(r"DN-(\d{4})-(\d{4})", re.ASCII)

MIN_YEAR = 2000
MAX_YEAR = 9999
MAX_NUMBER = 9999


class ClientCodeError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class ClientCode:
    year: int
    number: int

    def __post_init__(self) -> None:
        if not MIN_YEAR <= self.year <= MAX_YEAR:
            raise ClientCodeError(f"year out of range: {self.year}")
        if not 1 <= self.number <= MAX_NUMBER:
            raise ClientCodeError(
                f"number out of range: {self.number} (max {MAX_NUMBER} clients per year)"
            )

    def __str__(self) -> str:
        return f"DN-{self.year:04d}-{self.number:04d}"


def format_client_code(year: int, number: int) -> str:
    return str(ClientCode(year, number))


def parse_client_code(value: object) -> ClientCode:
    match = _RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ClientCodeError(f"not a client code: {value!r}")
    return ClientCode(int(match.group(1)), int(match.group(2)))


def is_valid_client_code(value: object) -> bool:
    try:
        parse_client_code(value)
    except ClientCodeError:
        return False
    return True
