"""Parse list filters from the query string (shared by the JSON API and the HTML pages)."""

from __future__ import annotations

from contextlib import suppress
from datetime import date, timedelta
from uuid import UUID

from starlette.datastructures import QueryParams

from klinik_local.terapiler.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SORT_KEYS,
    SessionFilters,
)
from klinik_shared.enums import AppointmentStatus
from klinik_shared.timeutil import now_utc, to_istanbul

DEFAULT_RANGE_DAYS = 30
_STATUSES = {s.value for s in AppointmentStatus}


def _many(params: QueryParams, name: str) -> tuple[str, ...]:
    # The spec writes `method[]`; plain HTML forms send `method`. Accept both.
    values = [*params.getlist(name), *params.getlist(f"{name}[]")]
    return tuple(dict.fromkeys(v.strip() for v in values if v.strip()))


def _date(value: str | None) -> date | None:
    if value:
        with suppress(ValueError):
            return date.fromisoformat(value.strip())
    return None


def _int(value: str | None, default: int) -> int:
    with suppress(TypeError, ValueError):
        return int(value)  # type: ignore[arg-type]
    return default


def parse_filters(params: QueryParams, *, default_range: bool = False) -> SessionFilters:
    """Build filters from untrusted query parameters; anything invalid is ignored.

    With ``default_range`` (the HTML page) a missing start date means "last 30 days".
    """
    therapist_id: UUID | None = None
    with suppress(ValueError):
        therapist_id = UUID(params.get("therapist_id") or params.get("therapist") or "")

    sort = (params.get("sort") or "-session_date").strip()
    if sort.lstrip("-") not in SORT_KEYS:
        sort = "-session_date"

    date_from = _date(params.get("date_from"))
    if date_from is None and default_range and "date_from" not in params:
        date_from = to_istanbul(now_utc()).date() - timedelta(days=DEFAULT_RANGE_DAYS)

    return SessionFilters(
        age_group=(params.get("age_group") or params.get("group") or "").strip() or None,
        methods=_many(params, "method"),
        format=(params.get("format") or "").strip() or None,
        therapist_id=therapist_id,
        statuses=tuple(s for s in _many(params, "status") if s in _STATUSES),
        date_from=date_from,
        date_to=_date(params.get("date_to")),
        q=(params.get("q") or "").strip()[:100],
        sort=sort,
        page=max(1, _int(params.get("page"), 1)),
        page_size=max(1, min(_int(params.get("page_size"), DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)),
    )
