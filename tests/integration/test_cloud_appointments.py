"""Klinik Web booking rules on a real PostgreSQL (brief §7, §16)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.cloud import (
    CloudDb,
    Web,
    add_cloud_therapists,
    add_cloud_user,
    add_code,
    add_room,
)
from support.local import A_ID, B_ID, fetch, run, scalar

from klinik_cloud.takvim.service import (
    AppointmentFields,
    AppointmentService,
    OverlapError,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.permissions import expand_sets
from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration

CODE = "DN-2026-0042"
OTHER = "DN-2026-0043"
VIEWER_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a07")


def next_monday() -> date:
    today = datetime.now(ISTANBUL).date()
    return today + timedelta(days=7 - today.weekday())


DAY = next_monday()


@dataclass
class Clinic:
    db: CloudDb
    a: Web
    b: Web
    room1: UUID
    room2: UUID


@pytest.fixture
def clinic(cloud_db: CloudDb) -> Iterator[Clinic]:
    temporary = add_cloud_therapists(cloud_db)
    room1, room2 = add_room(cloud_db, "Oda 1"), add_room(cloud_db, "Oda 2")
    add_code(cloud_db, CODE, therapist=A_ID)
    add_code(cloud_db, OTHER, therapist=B_ID)
    with Web(cloud_db) as a, Web(cloud_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        yield Clinic(cloud_db, a, b, room1, room2)


def book(who: Web, clinic: Clinic, **overrides: Any) -> Any:
    form = {
        "client_code": CODE,
        "therapist_id": str(A_ID),
        "date": DAY.isoformat(),
        "time": "14:00",
        "duration_min": "50",
        "room_id": str(clinic.room1),
        **overrides,
    }
    return who.post("/randevular", form)


def appointment_ids(db: CloudDb) -> list[UUID]:
    return [r["id"] for r in fetch(db, "SELECT id FROM appointments ORDER BY created_at")]  # type: ignore[arg-type]


# ---------------------------------------------------------------- booking


def test_b_books_for_a_and_it_shows_on_the_calendar(clinic: Clinic) -> None:
    response = book(clinic.b, clinic)
    assert response.status_code == 303
    assert response.headers["location"] == f"/takvim?gun={DAY.isoformat()}&durum=kaydedildi"

    row = fetch(clinic.db, "SELECT * FROM appointments")[0]  # type: ignore[arg-type]
    assert row["therapist_id"] == A_ID and row["created_by"] == B_ID
    # 14:00 in Istanbul is 11:00 UTC; ends_at is maintained by the database.
    assert row["starts_at"].astimezone(ISTANBUL).hour == 14
    assert row["ends_at"] - row["starts_at"] == timedelta(minutes=50)
    assert row["status"] == "planned" and row["version"] == 1

    week = clinic.a.get(f"/takvim?gun={DAY.isoformat()}").text
    assert CODE in week and "14:00–14:50" in week and "Oda 1" in week
    day = clinic.a.get(f"/takvim?gorunum=gun&gun={DAY.isoformat()}").text
    assert CODE in day and "Terapist A" in day
    audit = fetch(
        clinic.db, "SELECT action, actor_id FROM audit_log WHERE entity_type='appointment'"
    )  # type: ignore[arg-type]
    assert [(r["action"], r["actor_id"]) for r in audit] == [("appointment_created", B_ID)]


def test_lowercase_code_is_accepted(clinic: Clinic) -> None:
    assert book(clinic.a, clinic, client_code="dn-2026-0042").status_code == 303


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"client_code": "DN-0042"}, "Danışan kodu geçersiz"),
        ({"client_code": "Ayşe Yılmaz"}, "Danışan kodu geçersiz"),
        ({"client_code": "DN-2026-9999"}, "henüz bilinmiyor"),
        ({"time": ""}, "Tarih ve saat geçersiz"),
        ({"duration_min": "5"}, "10 ile 240"),
        ({"therapist_id": ""}, "Terapist seçin"),
        ({"therapist_id": str(VIEWER_ID)}, "Terapist seçimi geçersiz"),
        ({"room_id": ""}, "Oda seçin"),
    ],
)
def test_invalid_bookings_are_refused(
    clinic: Clinic, overrides: dict[str, str], message: str
) -> None:
    response = book(clinic.a, clinic, **overrides)
    assert response.status_code == 422
    assert message in response.text
    assert scalar(clinic.db, "SELECT count(*) FROM appointments") == 0  # type: ignore[arg-type]


def test_inactive_client_cannot_be_booked(clinic: Clinic) -> None:
    add_code(clinic.db, "DN-2025-0001", active=False)
    response = book(clinic.a, clinic, client_code="DN-2025-0001")
    assert response.status_code == 422 and "pasif" in response.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"client_code": OTHER}, "Terapistin bu saatte başka bir randevusu var."),
        (
            {"therapist_id": str(B_ID), "client_code": OTHER},
            "Oda bu saatte dolu.",
        ),
        (
            {"therapist_id": str(B_ID), "room_id": "ROOM2"},
            "Danışanın bu saatte başka bir randevusu var.",
        ),
    ],
)
def test_no_double_booking(clinic: Clinic, overrides: dict[str, str], message: str) -> None:
    assert book(clinic.a, clinic).status_code == 303
    if overrides.get("room_id") == "ROOM2":
        overrides = {**overrides, "room_id": str(clinic.room2)}
    clash = book(clinic.b, clinic, time="14:30", **overrides)
    assert clash.status_code == 422
    assert message in clash.text
    assert scalar(clinic.db, "SELECT count(*) FROM appointments") == 1  # type: ignore[arg-type]


def test_back_to_back_and_cancelled_slots_are_free(clinic: Clinic) -> None:
    assert book(clinic.a, clinic).status_code == 303
    # 14:50 starts exactly when 14:00 + 50 min ends.
    assert book(clinic.a, clinic, time="14:50", client_code=OTHER).status_code == 303
    first = appointment_ids(clinic.db)[0]
    clinic.a.post(f"/randevular/{first}/iptal", {"version": "1"})
    assert book(clinic.b, clinic, client_code=OTHER, time="13:30", therapist_id=str(B_ID),
                room_id=str(clinic.room2)).status_code == 303  # fmt: skip
    # The cancelled 14:00 slot can be booked again.
    assert book(clinic.a, clinic).status_code == 303
    assert scalar(clinic.db, "SELECT count(*) FROM appointments") == 4  # type: ignore[arg-type]


def test_concurrent_bookings_for_one_slot_exactly_one_wins(clinic: Clinic) -> None:
    """Definition of done: two (here twenty) simultaneous bookings, exactly one succeeds."""
    service = AppointmentService()
    actor = CurrentUser(
        id=B_ID,
        username="terapist.b",
        display_name="Terapist B",
        permissions=frozenset(str(p) for p in expand_sets("therapist", "scheduler")),
        must_change_password=False,
        session_id_hash=b"",
        csrf_token="",
    )
    for n in range(20):
        add_code(clinic.db, f"DN-2026-{n + 100:04d}")
    starts = datetime.combine(DAY, datetime.min.time(), tzinfo=ISTANBUL).replace(hour=9)

    async def attempt(engine: Any, n: int) -> str:
        try:
            async with engine.begin() as conn:
                await service.create(
                    conn,
                    actor,
                    client_code=f"DN-2026-{n + 100:04d}",
                    fields=AppointmentFields(
                        starts_at=starts + timedelta(minutes=n % 3 * 10),
                        therapist_id=A_ID,
                        room_id=clinic.room1 if n % 2 else clinic.room2,
                    ),
                )
            return "ok"
        except OverlapError as exc:
            return exc.message

    async def go() -> list[str]:
        engine = create_async_engine(
            clinic.db.app_url,
            pool_size=20,
            connect_args={"server_settings": {"search_path": "app"}},
        )
        try:
            return list(await asyncio.gather(*(attempt(engine, n) for n in range(20))))
        finally:
            await engine.dispose()

    results = run(go())
    assert results.count("ok") == 1
    assert set(results) - {"ok"} <= {
        "Terapistin bu saatte başka bir randevusu var.",
        "Oda bu saatte dolu.",
    }
    assert scalar(clinic.db, "SELECT count(*) FROM appointments") == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------- editing and versions


def edit_form(who: Web, appointment_id: UUID) -> dict[str, str]:
    page = who.get(f"/randevular/{appointment_id}").text
    fields: dict[str, str] = {}
    for name in ("version", "orig_date", "orig_time", "orig_duration_min",
                 "orig_therapist_id", "orig_room_id"):  # fmt: skip
        match = re.search(rf'name="{name}" value="([^"]*)"', page)
        assert match, name
        fields[name] = match.group(1)
    return {
        **fields,
        "therapist_id": fields["orig_therapist_id"],
        "date": fields["orig_date"],
        "time": fields["orig_time"],
        "duration_min": fields["orig_duration_min"],
        "room_id": fields["orig_room_id"],
    }


def test_edit_moves_the_appointment(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    form = edit_form(clinic.a, appointment)
    response = clinic.a.post(f"/randevular/{appointment}", {**form, "time": "16:00"})
    assert response.status_code == 303 and "durum=kaydedildi" in response.headers["location"]
    row = fetch(clinic.db, "SELECT * FROM appointments")[0]  # type: ignore[arg-type]
    assert row["starts_at"].astimezone(ISTANBUL).hour == 16 and row["version"] == 2


def test_same_field_edited_twice_becomes_a_conflict(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    a_form = edit_form(clinic.a, appointment)
    b_form = edit_form(clinic.b, appointment)

    assert (
        clinic.b.post(f"/randevular/{appointment}", {**b_form, "time": "15:00"}).status_code == 303
    )
    stale = clinic.a.post(f"/randevular/{appointment}", {**a_form, "time": "16:00"})
    assert stale.status_code == 409
    assert "Bu randevu az önce değiştirildi." in stale.text
    # B's change stands; A's is kept as a conflict for a human decision.
    row = fetch(clinic.db, "SELECT * FROM appointments")[0]  # type: ignore[arg-type]
    assert row["starts_at"].astimezone(ISTANBUL).hour == 15 and row["version"] == 2
    conflict = fetch(clinic.db, "SELECT * FROM conflicts")[0]  # type: ignore[arg-type]
    assert conflict["author_id"] == A_ID and conflict["status"] == "open"
    proposed = json.loads(conflict["proposed"])
    assert set(proposed) == {"starts_at", "duration_min", "therapist_id", "room_id"}
    page = clinic.a.get("/cakismalar").text
    assert "Sizin değişikliğiniz" in page
    proposed_start = datetime.fromisoformat(proposed["starts_at"]).astimezone(ISTANBUL)
    assert (proposed_start.date(), proposed_start.hour) == (DAY, 16)


def test_different_fields_are_merged(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    a_form = edit_form(clinic.a, appointment)
    b_form = edit_form(clinic.b, appointment)
    clinic.b.post(f"/randevular/{appointment}", {**b_form, "room_id": str(clinic.room2)})
    merged = clinic.a.post(f"/randevular/{appointment}", {**a_form, "time": "17:00"})
    assert merged.status_code == 303 and "durum=birlestirildi" in merged.headers["location"]
    row = fetch(clinic.db, "SELECT * FROM appointments")[0]  # type: ignore[arg-type]
    assert row["room_id"] == clinic.room2  # B's change kept
    assert row["starts_at"].astimezone(ISTANBUL).hour == 17  # A's change applied
    assert row["version"] == 3
    assert scalar(clinic.db, "SELECT count(*) FROM conflicts") == 0  # type: ignore[arg-type]


def test_resolving_a_conflict(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    a_form = edit_form(clinic.a, appointment)
    b_form = edit_form(clinic.b, appointment)
    clinic.b.post(f"/randevular/{appointment}", {**b_form, "time": "15:00"})
    clinic.a.post(f"/randevular/{appointment}", {**a_form, "time": "16:00"})
    conflict = scalar(clinic.db, "SELECT id FROM conflicts")  # type: ignore[arg-type]

    page = clinic.a.get("/cakismalar").text
    assert "Sizin değişikliğiniz" in page and "Benim değişikliğimi uygula" in page
    # B is neither the author nor an admin: does not see it and cannot resolve it.
    assert "Karar bekleyen çakışma yok." in clinic.b.get("/cakismalar").text
    assert clinic.b.post(f"/cakismalar/{conflict}/coz", {"karar": "discarded"}).status_code == 404

    applied = clinic.a.post(f"/cakismalar/{conflict}/coz", {"karar": "reapplied"})
    assert applied.status_code == 303
    row = fetch(clinic.db, "SELECT * FROM appointments")[0]  # type: ignore[arg-type]
    assert row["starts_at"].astimezone(ISTANBUL).hour == 16
    assert scalar(clinic.db, "SELECT resolution FROM conflicts") == "reapplied"  # type: ignore[arg-type]


def test_cancel(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    response = clinic.b.post(f"/randevular/{appointment}/iptal", {"version": "1"})
    assert response.status_code == 303 and "durum=iptal" in response.headers["location"]
    assert scalar(clinic.db, "SELECT status FROM appointments") == "cancelled"  # type: ignore[arg-type]
    actions = [
        r["action"]
        for r in fetch(
            clinic.db, "SELECT action FROM audit_log WHERE entity_type='appointment' ORDER BY id"
        )
    ]  # type: ignore[arg-type]
    assert actions == ["appointment_created", "appointment_cancelled"]


def test_status_outcomes_cannot_be_set_from_the_calendar(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    form = edit_form(clinic.a, appointment)
    response = clinic.a.post(f"/randevular/{appointment}", {**form, "status": "completed"})
    assert response.status_code == 422
    assert scalar(clinic.db, "SELECT status FROM appointments") == "planned"  # type: ignore[arg-type]


# ---------------------------------------------------------------- permissions


def test_viewer_without_write_permission(clinic: Clinic) -> None:
    temporary = add_cloud_user(clinic.db, VIEWER_ID, "izleyici", "İzleyici", ["therapist"])
    book(clinic.a, clinic)
    appointment = appointment_ids(clinic.db)[0]
    with Web(clinic.db) as viewer:
        viewer.onboard("izleyici", temporary)
        assert CODE in viewer.get(f"/takvim?gun={DAY.isoformat()}").text
        assert "+ Yeni Randevu" not in viewer.get("/takvim").text
        assert viewer.get("/randevular/yeni").status_code == 403
        assert book(viewer, clinic, time="09:00").status_code == 403
        assert viewer.post(f"/randevular/{appointment}/iptal", {"version": "1"}).status_code == 403
        assert viewer.get("/ayarlar/odalar").status_code == 403
    assert scalar(clinic.db, "SELECT count(*) FROM appointments") == 1  # type: ignore[arg-type]


def test_rooms_admin(clinic: Clinic) -> None:
    assert clinic.b.get("/ayarlar/odalar").status_code == 403
    assert clinic.a.post("/ayarlar/odalar", {"internal_label": "Oyun Odası"}).status_code == 303
    too_long = clinic.a.post("/ayarlar/odalar", {"internal_label": "x" * 31})
    assert too_long.status_code == 422
    assert (
        clinic.a.post(
            f"/ayarlar/odalar/varsayilan/{A_ID}", {"room_id": str(clinic.room2)}
        ).status_code
        == 303
    )
    form = clinic.a.get(f"/randevular/yeni?terapist={A_ID}").text
    assert f'value="{clinic.room2}" selected' in form


def test_anonymous_visitor_is_sent_to_login(clinic: Clinic) -> None:
    with Web(clinic.db) as stranger:
        for path in ("/", "/takvim", "/randevular/yeni", "/cakismalar", "/ayarlar/odalar"):
            response = stranger.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login"
