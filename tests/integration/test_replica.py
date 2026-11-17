"""The owner's laptop copy: two databases, two apps, the laptop talking to the office.

The laptop reaches the office through an in-process transport (no network), so these
tests cover the whole path: pairing, first copy, offline edits, sending, receiving,
double bookings, edits on both sides, and the admin's decisions on conflicts.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pytest
from support.local import (
    A_ID,
    ADMIN_ID,
    AUTH_KEY,
    FERNET_KEY,
    Kasa,
    LocalDb,
    add_admin,
    add_therapists,
    execute,
    fetch,
    prepare_local_db,
    scalar,
)

from klinik_local.replika.pairing import PairingError, open_sealed
from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration
PASSPHRASE = "uzun-bir-eslestirme-parolasi"


def day(offset: int) -> date:
    return datetime.now(ISTANBUL).date() + timedelta(days=offset)


@dataclass
class Pair:
    office_db: LocalDb
    laptop_db: LocalDb
    office: Kasa  # the admin at the office
    therapist: Kasa  # therapist A at the office
    laptop: Kasa  # the admin on the laptop
    room: str
    client: str
    admin_secret: str
    online: list[bool]

    def sync(self) -> Any:
        status = self.laptop.client.portal.call(self.laptop.app.state.replica.run_once)
        return status

    def book(
        self, kasa: Kasa, therapist: Any, when: date, clock: str, client: str | None = None
    ) -> Any:
        return kasa.post(
            "/randevular",
            {"client_id": client or self.client, "therapist_id": str(therapist),
             "date": when.isoformat(), "time": clock, "duration_min": "50",
             "room_id": self.room},
        )  # fmt: skip


@pytest.fixture
def pair(local_db: LocalDb, database_factory: Callable[[], str]) -> Iterator[Pair]:
    office_db = local_db
    laptop_db = prepare_local_db(database_factory())
    temporary = add_therapists(office_db)
    admin_password = add_admin(office_db)
    online = [True]
    with (
        Kasa(office_db, sync_database_url=office_db.sync_url) as office,
        Kasa(office_db) as therapist,
    ):
        admin_secret, _ = office.onboard("yonetici", admin_password)
        therapist.onboard("terapist.a", temporary["a"])
        assert office.post("/ayarlar/odalar", {"label": "Oda 1"}).status_code == 303
        room = str(scalar(office_db, "SELECT id FROM rooms"))
        made = office.post(
            "/danisanlar", {"full_name": "Zeynep Arslan", "birth_date": "1990-01-01"}
        )
        client = made.headers["location"].split("/")[2].split("?")[0]

        sealed = office.post(
            "/ayarlar/dizustu",
            {"label": "Sahibin dizüstü", "passphrase": PASSPHRASE, "passphrase2": PASSPHRASE},
        )
        assert sealed.status_code == 200
        assert sealed.headers["content-disposition"].startswith("attachment")
        payload = open_sealed(sealed.content, PASSPHRASE)
        # The copy must read the same encrypted text: it gets the office's keys.
        assert payload["fernet_keys"] == FERNET_KEY
        assert payload["auth_fernet_key"] == AUTH_KEY

        office_transport = httpx.ASGITransport(app=office.app)

        class Switchable(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                if not online[0]:
                    raise httpx.ConnectError("office not reachable")
                return await office_transport.handle_async_request(request)

        laptop = Kasa(
            laptop_db,
            mode="replica",
            sync_database_url=laptop_db.sync_url,
            office_url="https://ofis.test",
            replica_token=payload["token"],
            replica_interval_seconds=0,
        )
        laptop.app.state.replica.transport = Switchable()
        with laptop:
            laptop.client.portal.call(laptop.app.state.replica.mark_replica)
            pair = Pair(
                office_db, laptop_db, office, therapist, laptop, room, client, admin_secret, online
            )
            status = pair.sync()
            assert status.last_error is None, status.last_error
            assert laptop.login("yonetici", admin_secret).status_code == 303
            yield pair


# ---------------------------------------------------------------- pairing and first copy


def test_pairing_file_is_encrypted_and_needs_the_passphrase(pair: Pair) -> None:
    sealed = pair.office.post(
        "/ayarlar/dizustu",
        {"label": "İkinci", "passphrase": PASSPHRASE, "passphrase2": PASSPHRASE},
    )
    raw = sealed.content
    assert b"fernet" not in raw and b"token" not in raw
    with pytest.raises(PairingError, match="Parola yanlış"):
        open_sealed(raw, "yanlis-parola-12345")
    assert scalar(pair.office_db, "SELECT count(*) FROM replicas") == 2
    tokens = [bytes(r[0]) for r in fetch(pair.office_db, "SELECT token_hash FROM replicas")]
    assert all(len(t) == 32 for t in tokens)  # only hashes are stored


def test_first_copy_brings_users_rooms_clients_and_catalog(pair: Pair) -> None:
    for table in ("users", "user_permissions", "rooms", "clients", "therapy_methods", "age_groups"):
        office = scalar(pair.office_db, f"SELECT count(*) FROM {table}")
        laptop = scalar(pair.laptop_db, f"SELECT count(*) FROM {table}")
        assert office == laptop, table
    page = pair.laptop.get("/danisanlar")
    assert "Zeynep Arslan" in page.text
    assert "Dizüstü" in page.text  # the laptop says what it is


def test_laptop_hides_office_only_settings(pair: Pair) -> None:
    assert (
        pair.laptop.get("/ayarlar", follow_redirects=False).headers["location"]
        == "/ayarlar/esitleme"
    )
    for path in ("/ayarlar/kullanicilar", "/ayarlar/odalar", "/ayarlar/yedek", "/ayarlar/dizustu"):
        assert pair.laptop.get(path).status_code == 404, path
    assert pair.laptop.get("/hesap/sifre").status_code == 409


# ---------------------------------------------------------------- offline work, then sync


def test_offline_work_reaches_the_office(pair: Pair) -> None:
    pair.online[0] = False
    made = pair.laptop.post(
        "/danisanlar", {"full_name": "Ali Çevrimdışı", "birth_date": "1985-05-05"}
    )
    assert made.status_code == 303
    new_client = made.headers["location"].split("/")[2].split("?")[0]
    code = scalar(pair.laptop_db, "SELECT client_code FROM clients WHERE id = $1", new_client)
    assert code.endswith("-9001")  # the laptop's own number range
    assert pair.book(pair.laptop, ADMIN_ID, day(3), "09:00", client=new_client).status_code == 303
    status = pair.sync()
    assert status.last_error and "ulaşılamıyor" in status.last_error
    assert scalar(pair.laptop_db, "SELECT count(*) FROM replica_outbox") == 2

    pair.online[0] = True
    status = pair.sync()
    assert status.last_error is None
    assert scalar(pair.laptop_db, "SELECT count(*) FROM replica_outbox") == 0
    assert (
        scalar(pair.office_db, "SELECT client_code FROM clients WHERE id = $1", new_client) == code
    )
    assert scalar(pair.office_db, "SELECT count(*) FROM appointments") == 1
    office_page = pair.office.get(f"/takvim?gun={day(3).isoformat()}")
    assert "Ali Çevrimdışı" in office_page.text
    # The laptop's actions are in the office's audit log too.
    via = fetch(pair.office_db, "SELECT detail FROM audit_log WHERE action = 'client_created'")
    assert any("dizüstü" in json.loads(r["detail"]).get("via", "") for r in via)


def test_office_changes_reach_the_laptop(pair: Pair) -> None:
    assert pair.book(pair.therapist, A_ID, day(2), "14:00").status_code == 303
    pair.sync()
    page = pair.laptop.get(f"/?gun={day(2).isoformat()}")
    assert "Zeynep Arslan" in page.text and "14:00" in page.text


def test_a_session_with_notes_round_trips_encrypted(pair: Pair) -> None:
    pair.book(pair.laptop, ADMIN_ID, day(-1), "10:00")
    appt = str(scalar(pair.laptop_db, "SELECT id FROM appointments"))
    saved = pair.laptop.post(
        "/terapiler",
        {"client_id": pair.client, "session_date": f"{day(-1).isoformat()}T10:00",
         "duration_min": "50", "format_code": "individual", "method": ["cbt", "act"],
         "primary": "cbt", "status": "completed", "notes": "Dizüstünde yazılan not",
         "appointment_id": appt},
    )  # fmt: skip
    assert saved.status_code == 303, saved.text
    pair.sync()
    assert scalar(pair.office_db, "SELECT count(*) FROM session_methods") == 2
    assert scalar(pair.office_db, "SELECT status FROM appointments") == "completed"
    stored = bytes(scalar(pair.office_db, "SELECT notes_encrypted FROM therapy_sessions"))
    assert "Dizüstünde".encode() not in stored
    session = saved.headers["location"].split("/")[2].split("?")[0]
    assert "Dizüstünde yazılan not" in pair.office.get(f"/terapiler/{session}").text


# ---------------------------------------------------------------- conflicts


def test_double_booking_made_offline_is_refused_and_listed(pair: Pair) -> None:
    pair.online[0] = False
    assert pair.book(pair.laptop, ADMIN_ID, day(4), "11:00").status_code == 303
    # Meanwhile at the office the same room is booked at the same time.
    other = pair.office.post("/danisanlar", {"full_name": "Başka Danışan"})
    other_id = other.headers["location"].split("/")[2].split("?")[0]
    assert pair.book(pair.therapist, A_ID, day(4), "11:00", client=other_id).status_code == 303

    pair.online[0] = True
    status = pair.sync()
    assert status.last_error is None
    assert status.conflicts == 1
    # The office keeps its booking; the laptop's refused one is removed there and listed.
    assert scalar(pair.office_db, "SELECT count(*) FROM appointments") == 1
    assert scalar(pair.laptop_db, "SELECT count(*) FROM appointments") == 1
    page = pair.office.get("/ayarlar/cakismalar")
    assert "çakışıyor" in page.text and "Zeynep Arslan" in page.text
    conflict = str(scalar(pair.office_db, "SELECT id FROM replica_conflicts"))
    retry = pair.office.post(f"/ayarlar/cakismalar/{conflict}", {"karar": "applied_laptop"})
    assert "hata=" in retry.headers["location"]  # still clashes: nothing changed
    keep = pair.office.post(f"/ayarlar/cakismalar/{conflict}", {"karar": "kept_office"})
    assert keep.headers["location"].endswith("ok=1")
    assert "Bekleyen çakışma yok" in pair.office.get("/ayarlar/cakismalar").text


def test_same_client_edited_on_both_sides(pair: Pair) -> None:
    pair.online[0] = False
    base = {"birth_date": "1990-01-01", "phone": "", "email": "", "assigned_therapist_id": ""}
    laptop_edit = pair.laptop.post(
        f"/danisanlar/{pair.client}", {**base, "full_name": "Zeynep Dizüstü", "version": "1"}
    )
    assert laptop_edit.status_code == 303, laptop_edit.text
    office_edit = pair.office.post(
        f"/danisanlar/{pair.client}", {**base, "full_name": "Zeynep Ofis", "version": "1"}
    )
    assert office_edit.status_code == 303

    pair.online[0] = True
    status = pair.sync()
    assert status.conflicts == 1
    # The office version wins everywhere for now ...
    assert "Zeynep Ofis" in pair.laptop.get(f"/danisanlar/{pair.client}").text
    page = pair.office.get("/ayarlar/cakismalar")
    assert "Zeynep Ofis" in page.text and "Zeynep Dizüstü" in page.text
    # ... until the admin chooses the laptop's.
    conflict = str(scalar(pair.office_db, "SELECT id FROM replica_conflicts"))
    pair.office.post(f"/ayarlar/cakismalar/{conflict}", {"karar": "applied_laptop"})
    assert "Zeynep Dizüstü" in pair.office.get(f"/danisanlar/{pair.client}").text
    pair.sync()
    assert "Zeynep Dizüstü" in pair.laptop.get(f"/danisanlar/{pair.client}").text


def test_session_numbers_taken_meanwhile_are_renumbered(pair: Pair) -> None:
    pair.online[0] = False
    form = {
        "client_id": pair.client, "duration_min": "50", "format_code": "individual",
        "method": ["cbt"], "primary": "cbt", "status": "completed",
    }  # fmt: skip
    first = pair.laptop.post("/terapiler", {**form, "session_date": f"{day(-3).isoformat()}T09:00"})
    assert first.status_code == 303
    second = pair.therapist.post(
        "/terapiler", {**form, "session_date": f"{day(-2).isoformat()}T09:00"}
    )
    assert second.status_code == 303
    pair.online[0] = True
    pair.sync()
    numbers = [
        r[0]
        for r in fetch(pair.office_db, "SELECT session_number FROM therapy_sessions ORDER BY 1")
    ]
    assert numbers == [1, 2]
    laptop_number = scalar(
        pair.laptop_db,
        "SELECT session_number FROM therapy_sessions WHERE therapist_id = $1",
        ADMIN_ID,
    )
    assert laptop_number == 2


def test_revoked_laptop_can_no_longer_sync(pair: Pair) -> None:
    replica = str(scalar(pair.office_db, "SELECT id FROM replicas"))
    pair.office.post(f"/ayarlar/dizustu/{replica}/iptal")
    status = pair.sync()
    assert status.last_error and "tanımıyor" in status.last_error


def test_sync_api_needs_the_token(pair: Pair) -> None:
    assert pair.office.client.post("/replika/api/pull", json={"since": 0}).status_code == 401
    bad = {"Authorization": "Bearer yanlis"}
    assert pair.office.client.post("/replika/api/pull", json={}, headers=bad).status_code == 401


def test_status_page_on_the_laptop(pair: Pair) -> None:
    page = pair.laptop.get("/ayarlar/esitleme")
    assert "Ofisle eşit" in page.text and "https://ofis.test" in page.text
    execute(pair.office_db, "SELECT 1")


def test_new_sessions_do_not_make_a_client_look_edited(pair: Pair) -> None:
    """A session bumps the client's counter; that alone must never cause a conflict."""
    form = {
        "client_id": pair.client, "duration_min": "50", "format_code": "individual",
        "method": ["cbt"], "primary": "cbt", "status": "completed",
        "session_date": f"{day(-1).isoformat()}T09:00",
    }  # fmt: skip
    assert pair.therapist.post("/terapiler", form).status_code == 303
    base = {"birth_date": "1990-01-01", "phone": "0532 000 00 00", "email": ""}
    edit = pair.laptop.post(
        f"/danisanlar/{pair.client}",
        {**base, "full_name": "Zeynep Arslan", "assigned_therapist_id": "", "version": "1"},
    )
    assert edit.status_code == 303, edit.text
    status = pair.sync()
    assert status.last_error is None and status.conflicts == 0
    assert scalar(pair.office_db, "SELECT count(*) FROM replica_conflicts") == 0
    assert "0532 000 00 00" in pair.office.get(f"/danisanlar/{pair.client}").text
