from __future__ import annotations

import base64
from datetime import UTC, datetime, time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from klinik_cloud.bildirimler.push import generate_vapid_keys
from klinik_cloud.bildirimler.service import (
    NotificationPayload,
    in_quiet_hours,
    push_after_for,
    render,
)
from klinik_shared.timeutil import ISTANBUL

NIGHT = (time(22, 0), time(8, 0))


def ist(hour: int, minute: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=ISTANBUL)


@pytest.mark.parametrize(
    ("moment", "quiet"),
    [
        (ist(21, 59), False),
        (ist(22, 0), True),
        (ist(23, 30), True),
        (ist(0, 0), True),
        (ist(7, 59), True),
        (ist(8, 0), False),
        (ist(14, 0), False),
    ],
)
def test_quiet_window_across_midnight(moment: datetime, quiet: bool) -> None:
    assert in_quiet_hours(moment, *NIGHT) is quiet


def test_push_waits_until_quiet_hours_end() -> None:
    assert push_after_for(ist(21, 59), *NIGHT) == ist(21, 59)
    assert push_after_for(ist(22, 0), *NIGHT) == ist(8, 0, day=13)  # next morning
    assert push_after_for(ist(23, 45), *NIGHT) == ist(8, 0, day=13)
    assert push_after_for(ist(3, 10), *NIGHT) == ist(8, 0)  # same morning
    assert push_after_for(ist(8, 0), *NIGHT) == ist(8, 0)


def test_daytime_window_and_disabled_window() -> None:
    lunch = (time(12, 0), time(13, 30))
    assert push_after_for(ist(12, 15), *lunch) == ist(13, 30)
    assert push_after_for(ist(13, 30), *lunch) == ist(13, 30)
    assert push_after_for(ist(23, 0), time(0, 0), time(0, 0)) == ist(23, 0)  # no quiet hours


def test_utc_input_is_judged_in_istanbul_time() -> None:
    # 19:30 UTC is 22:30 in Istanbul: quiet.
    assert in_quiet_hours(datetime(2026, 11, 12, 19, 30, tzinfo=UTC), *NIGHT)


def test_text_has_date_time_and_code_only() -> None:
    appointment = uuid4()
    rendered = render(
        "appointment.created",
        {
            "appointment_id": str(appointment),
            "client_code": "DN-2026-0042",
            "starts_at": ist(14, 0).isoformat(),
        },
    )
    assert rendered.title == "🗓 Yeni randevu"
    assert rendered.body == "12.11 Perş 14:00 · DN-2026-0042"
    assert rendered.url == "/takvim?gun=2026-11-12"
    assert rendered.tag == f"appt-{appointment}"


def test_update_text_names_the_changed_fields() -> None:
    rendered = render(
        "appointment.updated",
        {
            "client_code": "DN-2026-0042",
            "starts_at": ist(15, 0).isoformat(),
            "changed_fields": ["room_id", "starts_at"],
        },
    )
    assert rendered.body == "12.11 Perş 15:00 · DN-2026-0042 (oda, saat değişti)"


def test_conflict_links_to_the_conflicts_page() -> None:
    assert render("conflict.created", {"conflict_id": str(uuid4())}).url == "/cakismalar"


@pytest.mark.parametrize(
    "extra",
    [{"client_name": "Ayşe"}, {"note": "x"}, {"client_code": "Ayşe Yılmaz"}],
)
def test_payload_refuses_anything_identifying(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate(extra)


def test_vapid_keys_have_the_expected_shape() -> None:
    private, public = generate_vapid_keys()
    raw_public = base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))
    assert len(raw_public) == 65 and raw_public[0] == 4  # uncompressed P-256 point
    assert "=" not in private and "=" not in public
