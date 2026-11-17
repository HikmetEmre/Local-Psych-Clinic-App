"""Calendar, booking, payments, homepage and roles in the single office app."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from support.local import (
    A_ID,
    B_ID,
    Kasa,
    LocalDb,
    add_admin,
    add_therapists,
    execute,
    fetch,
    scalar,
)

from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration


def day(offset: int) -> date:
    return datetime.now(ISTANBUL).date() + timedelta(days=offset)


@dataclass
class Clinic:
    db: LocalDb
    admin: Kasa
    a: Kasa
    b: Kasa
    room: str
    room2: str
    zeynep: str
    elif_: str


def make_client(kasa: Kasa, name: str) -> str:
    response = kasa.post("/danisanlar", {"full_name": name, "birth_date": "1990-05-01"})
    assert response.status_code == 303, response.text
    return response.headers["location"].split("/")[2].split("?")[0]


def add_room(kasa: Kasa, label: str) -> str:
    assert kasa.post("/ayarlar/odalar", {"label": label}).status_code == 303
    return str(scalar(kasa.db, "SELECT id FROM rooms WHERE label = $1", label))


@pytest.fixture
def clinic(local_db: LocalDb) -> Iterator[Clinic]:
    temporary = add_therapists(local_db)
    admin_password = add_admin(local_db)
    with Kasa(local_db) as admin, Kasa(local_db) as a, Kasa(local_db) as b:
        admin.onboard("yonetici", admin_password)
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        room = add_room(admin, "Oda 1")
        room2 = add_room(admin, "Oda 2")
        yield Clinic(
            local_db, admin, a, b, room, room2,
            make_client(admin, "Zeynep Arslan"), make_client(admin, "Elif Kaya"),
        )  # fmt: skip


def book(kasa: Kasa, client_id: str, therapist: Any, when: date, clock: str, **extra: str) -> Any:
    form = {
        "client_id": client_id,
        "therapist_id": str(therapist),
        "date": when.isoformat(),
        "time": clock,
        "duration_min": "50",
        **extra,
    }
    return kasa.post("/randevular", form)


def appointment_id(db: LocalDb, client_id: str) -> str:
    return str(
        scalar(
            db,
            "SELECT id FROM appointments WHERE client_id = $1 ORDER BY created_at DESC LIMIT 1",
            client_id,
        )
    )


# ---------------------------------------------------------------- booking


def test_booking_shows_names_in_the_calendar(clinic: Clinic) -> None:
    response = book(clinic.b, clinic.zeynep, B_ID, day(1), "10:00", room_id=clinic.room)
    assert response.status_code == 303, response.text
    page = clinic.a.get(f"/takvim?gun={day(1).isoformat()}")
    assert page.status_code == 200
    assert "Zeynep Arslan" in page.text and "10:00–10:50" in page.text


def test_room_defaults_to_the_first_room_when_none_is_chosen(clinic: Clinic) -> None:
    assert book(clinic.a, clinic.zeynep, A_ID, day(1), "09:00").status_code == 303
    assert str(scalar(clinic.db, "SELECT room_id FROM appointments")) == clinic.room


@pytest.mark.parametrize(
    ("who", "client", "room", "message"),
    [
        ("same_therapist", "elif", "room2", "Terapistin bu saatte başka bir randevusu var."),
        ("other_therapist", "elif", "room", "Oda bu saatte dolu."),
        ("other_therapist", "zeynep", "room2", "Danışanın bu saatte başka bir randevusu var."),
    ],
)
def test_double_booking_is_refused(
    clinic: Clinic, who: str, client: str, room: str, message: str
) -> None:
    assert (
        book(clinic.a, clinic.zeynep, A_ID, day(2), "14:00", room_id=clinic.room).status_code == 303
    )
    therapist = A_ID if who == "same_therapist" else B_ID
    client_id = clinic.elif_ if client == "elif" else clinic.zeynep
    response = book(clinic.a, client_id, therapist, day(2), "14:30", room_id=getattr(clinic, room))
    assert response.status_code == 422
    assert message in response.text
    assert scalar(clinic.db, "SELECT count(*) FROM appointments") == 1


def test_cancelled_slot_can_be_booked_again(clinic: Clinic) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(2), "11:00", room_id=clinic.room)
    appt = appointment_id(clinic.db, clinic.zeynep)
    assert clinic.a.post(f"/randevular/{appt}/iptal", {"version": "1"}).status_code == 303
    assert (
        book(clinic.a, clinic.elif_, A_ID, day(2), "11:00", room_id=clinic.room).status_code == 303
    )


def test_stale_edit_of_the_same_field_is_refused_and_shows_current_values(clinic: Clinic) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(3), "10:00", room_id=clinic.room)
    appt = appointment_id(clinic.db, clinic.zeynep)
    base = {
        "therapist_id": str(A_ID), "duration_min": "50", "room_id": clinic.room, "version": "1",
        "orig_date": day(3).isoformat(), "orig_time": "10:00", "orig_duration_min": "50",
        "orig_therapist_id": str(A_ID), "orig_room_id": clinic.room,
    }  # fmt: skip
    first = clinic.a.post(
        f"/randevular/{appt}", {**base, "date": day(3).isoformat(), "time": "12:00"}
    )
    assert first.status_code == 303
    second = clinic.b.post(
        f"/randevular/{appt}", {**base, "date": day(3).isoformat(), "time": "15:00"}
    )
    assert second.status_code == 409
    assert "az önce değiştirildi" in second.text
    assert 'value="12:00"' in second.text


def test_different_fields_are_merged(clinic: Clinic) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(3), "10:00", room_id=clinic.room)
    appt = appointment_id(clinic.db, clinic.zeynep)
    base = {
        "therapist_id": str(A_ID), "duration_min": "50", "room_id": clinic.room, "version": "1",
        "date": day(3).isoformat(), "time": "10:00",
        "orig_date": day(3).isoformat(), "orig_time": "10:00", "orig_duration_min": "50",
        "orig_therapist_id": str(A_ID), "orig_room_id": clinic.room,
    }  # fmt: skip
    assert clinic.a.post(f"/randevular/{appt}", {**base, "time": "11:00"}).status_code == 303
    merged = clinic.b.post(f"/randevular/{appt}", {**base, "room_id": clinic.room2})
    assert merged.status_code == 303 and "birlestirildi" in merged.headers["location"]
    row = fetch(clinic.db, "SELECT room_id, starts_at, version FROM appointments")[0]
    assert str(row["room_id"]) == clinic.room2
    assert row["starts_at"].astimezone(ISTANBUL).strftime("%H:%M") == "11:00"
    assert row["version"] == 3


# ---------------------------------------------------------------- sessions and outcomes


def test_recording_the_session_completes_the_appointment_and_deleting_reopens_it(
    clinic: Clinic,
) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(-1), "10:00", room_id=clinic.room)
    appt = appointment_id(clinic.db, clinic.zeynep)
    form = clinic.a.get(f"/terapiler/yeni?randevu={appt}")
    assert form.status_code == 200 and clinic.zeynep in form.text
    saved = clinic.a.post(
        "/terapiler",
        {
            "client_id": clinic.zeynep,
            "session_date": f"{day(-1).isoformat()}T10:00",
            "duration_min": "50",
            "format_code": "individual",
            "method": ["cbt"],
            "primary": "cbt",
            "status": "completed",
            "appointment_id": appt,
        },
    )
    assert saved.status_code == 303, saved.text
    assert scalar(clinic.db, "SELECT status FROM appointments") == "completed"
    session = saved.headers["location"].split("/")[2].split("?")[0]
    assert clinic.a.post(f"/terapiler/{session}/sil").status_code == 303
    assert scalar(clinic.db, "SELECT status FROM appointments") == "planned"


def test_a_therapist_cannot_record_a_session_on_a_colleagues_appointment(clinic: Clinic) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(-1), "10:00", room_id=clinic.room)
    appt = appointment_id(clinic.db, clinic.zeynep)
    response = clinic.b.post(
        "/terapiler",
        {
            "client_id": clinic.zeynep,
            "session_date": f"{day(-1).isoformat()}T10:00",
            "duration_min": "50",
            "format_code": "individual",
            "method": ["cbt"],
            "primary": "cbt",
            "status": "completed",
            "appointment_id": appt,
        },
    )
    assert response.status_code == 422
    assert "başka bir terapiste ait" in response.text


# ---------------------------------------------------------------- payments and income


def pay(kasa: Kasa, appt: str, amount: str) -> Any:
    return kasa.post("/odemeler", {"appointment_id": appt, "amount": amount, "method": "cash"})


def test_payments_by_role(clinic: Clinic) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(0), "09:00", room_id=clinic.room)
    book(clinic.b, clinic.elif_, B_ID, day(0), "09:00", room_id=clinic.room2)
    a_appt = appointment_id(clinic.db, clinic.zeynep)
    b_appt = appointment_id(clinic.db, clinic.elif_)

    assert "odeme=1" in pay(clinic.a, a_appt, "1.500").headers["location"]
    assert "odeme=1" in pay(clinic.b, b_appt, "750,50").headers["location"]
    # B may not pay into A's appointment, nor see its payments.
    refused = pay(clinic.b, a_appt, "100")
    assert "odeme_hatasi" in refused.headers["location"]
    assert "Ödemeler" not in clinic.b.get(f"/randevular/{a_appt}").text

    own = clinic.b.get("/gelir?donem=gun")
    assert own.status_code == 200
    assert "750,50 ₺" in own.text and "1.500,00 ₺" not in own.text

    everything = clinic.admin.get("/gelir?donem=gun")
    assert "2.250,50 ₺" in everything.text
    assert "Zeynep Arslan" in everything.text and "Elif Kaya" in everything.text


def test_payments_cannot_be_deleted_or_rewritten_by_the_app(clinic: Clinic) -> None:
    book(clinic.a, clinic.zeynep, A_ID, day(0), "09:00", room_id=clinic.room)
    pay(clinic.a, appointment_id(clinic.db, clinic.zeynep), "500")
    with pytest.raises(Exception, match="permission denied"):
        execute(clinic.db, "UPDATE payments SET amount = 1", url=clinic.db.app_url)


# ---------------------------------------------------------------- homepage


def test_homepage_lists_the_day_and_counts_the_month(clinic: Clinic) -> None:
    target = day(1)
    book(clinic.a, clinic.zeynep, A_ID, target, "10:00", room_id=clinic.room)
    book(clinic.b, clinic.elif_, B_ID, target, "10:00", room_id=clinic.room2)

    mine = clinic.a.get(f"/?gun={target.isoformat()}")
    assert mine.status_code == 200
    assert "Zeynep Arslan" in mine.text and "Elif Kaya" not in mine.text
    everyone = clinic.a.get(f"/?gun={target.isoformat()}&kim=hepsi")
    assert "Zeynep Arslan" in everyone.text and "Elif Kaya" in everyone.text

    admin = clinic.admin.get(f"/?gun={target.isoformat()}")
    assert "Zeynep Arslan" in admin.text and "Elif Kaya" in admin.text
    cell = re.search(
        rf'href="/\?gun={target.isoformat()}[^"]*"[^>]*>\s*'
        rf'<span class="month__num">{target.day}</span>\s*'
        r'<span class="month__count"[^>]*>(\d+)</span>',
        admin.text,
    )
    assert cell and cell.group(1) == "2"
    assert f"/randevular/yeni?baslangic={target.isoformat()}T10:00&geri=home" in admin.text


def test_booking_from_the_homepage_returns_there(clinic: Clinic) -> None:
    response = book(clinic.a, clinic.zeynep, A_ID, day(4), "16:00", back="home")
    assert response.headers["location"].startswith(f"/?gun={day(4).isoformat()}")


# ---------------------------------------------------------------- roles


def test_admin_reads_every_therapists_notes_but_a_therapist_does_not(clinic: Clinic) -> None:
    saved = clinic.b.post(
        "/terapiler",
        {
            "client_id": clinic.elif_,
            "session_date": f"{day(-2).isoformat()}T10:00",
            "duration_min": "50",
            "format_code": "individual",
            "method": ["cbt"],
            "primary": "cbt",
            "status": "completed",
            "notes": "Gizli not B",
        },
    )
    session = saved.headers["location"].split("/")[2].split("?")[0]
    assert "Gizli not B" in clinic.admin.get(f"/terapiler/{session}").text
    assert clinic.a.get(f"/terapiler/{session}").status_code == 404


def test_admin_creates_a_therapist_who_must_onboard(clinic: Clinic, local_db: LocalDb) -> None:
    page = clinic.admin.post(
        "/ayarlar/kullanicilar",
        {"display_name": "Ayşe Kaya", "username": "ayse.kaya", "role": "therapist"},
    )
    assert page.status_code == 200 and "Ayşe Kaya eklendi" in page.text
    temporary = re.search(r"geçici şifre: <code>([^<]+)</code>", page.text)
    assert temporary
    permissions = {
        r["permission"]
        for r in fetch(
            local_db,
            "SELECT permission FROM user_permissions p JOIN users u ON u.id = p.user_id "
            "WHERE u.username = 'ayse.kaya'",
        )
    }
    assert "sessions.write_own" in permissions and "sessions.read_others" not in permissions
    with Kasa(local_db) as ayse:
        ayse.onboard("ayse.kaya", temporary.group(1))
        assert ayse.get("/takvim").status_code == 200
        assert ayse.get("/ayarlar/kullanicilar").status_code == 403

    duplicate = clinic.admin.post(
        "/ayarlar/kullanicilar",
        {"display_name": "Başka", "username": "ayse.kaya", "role": "therapist"},
    )
    assert duplicate.status_code == 409


def test_role_change_and_self_protection(clinic: Clinic) -> None:
    to_admin = clinic.admin.post(f"/ayarlar/kullanicilar/{B_ID}/rol", {"role": "admin"})
    assert to_admin.status_code == 200 and "Yönetici" in to_admin.text
    assert scalar(clinic.db, "SELECT count(*) FROM user_permissions WHERE user_id = $1", B_ID) == 14
    admin_id = scalar(clinic.db, "SELECT id FROM users WHERE username = 'yonetici'")
    refused = clinic.admin.post(f"/ayarlar/kullanicilar/{admin_id}/rol", {"role": "therapist"})
    assert refused.status_code == 400


def test_a_therapist_has_no_admin_pages(clinic: Clinic) -> None:
    for path in ("/ayarlar/kullanicilar", "/ayarlar/odalar", "/ayarlar/yedek"):
        assert clinic.b.get(path).status_code == 403, path
