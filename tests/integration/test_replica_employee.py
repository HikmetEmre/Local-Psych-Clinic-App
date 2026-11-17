"""The employee therapist's laptop: a synced copy that holds only what she may see.

Main computer = the owner's laptop (admin). The employee's laptop is paired to her user:
it gets the whole calendar and the client list, her own sessions, notes and income, and
nothing of the owner's clinical notes, the expenses or anybody else's password.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from support.local import (
    ADMIN_ID,
    B_ID,
    Kasa,
    LocalDb,
    add_admin,
    add_therapists,
    fetch,
    prepare_local_db,
    scalar,
)

from klinik_local.replika.office import LOCKED_HASH
from klinik_local.replika.pairing import open_sealed
from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration
PASSPHRASE = "uzun-bir-eslestirme-parolasi"
OWNER_NOTE = "Sahibin gizli seans notu"
OWNER_LOG = "Sahibin gizli klinik notu"


def day(offset: int) -> date:
    return datetime.now(ISTANBUL).date() + timedelta(days=offset)


@dataclass
class Clinic:
    main_db: LocalDb
    copy_db: LocalDb
    owner: Kasa  # admin on the main computer
    employee_main: Kasa  # the employee on the main computer
    employee: Kasa  # the employee on her own laptop
    client: str
    owner_appt: str
    token: str
    online: list[bool]

    def sync(self) -> Any:
        return self.employee.client.portal.call(self.employee.app.state.replica.run_once)


def session_form(client: str, when: date, notes: str, appt: str = "") -> dict[str, Any]:
    return {
        "client_id": client, "session_date": f"{when.isoformat()}T10:00", "duration_min": "50",
        "format_code": "individual", "method": ["cbt"], "primary": "cbt",
        "status": "completed", "notes": notes, "appointment_id": appt,
    }  # fmt: skip


@pytest.fixture
def clinic(local_db: LocalDb, database_factory: Callable[[], str]) -> Iterator[Clinic]:
    main_db = local_db
    copy_db = prepare_local_db(database_factory())
    temporary = add_therapists(main_db)
    owner_password = add_admin(main_db)
    online = [True]
    with Kasa(main_db, sync_database_url=main_db.sync_url) as owner, Kasa(main_db) as emp_main:
        owner_secret, _ = owner.onboard("yonetici", owner_password)
        emp_secret, _ = emp_main.onboard("terapist.b", temporary["b"])
        owner.post("/ayarlar/odalar", {"label": "Oda 1"})
        owner.post("/ayarlar/odalar", {"label": "Oda 2"})
        made = owner.post("/danisanlar", {"full_name": "Zeynep Arslan", "birth_date": "1990-01-01"})
        client = made.headers["location"].split("/")[2].split("?")[0]
        # The owner's own clinical work and the clinic's money.
        owner.post(
            "/randevular",
            {"client_id": client, "therapist_id": str(ADMIN_ID), "date": day(-1).isoformat(),
             "time": "10:00", "duration_min": "50"},
        )  # fmt: skip
        owner_appt = str(scalar(main_db, "SELECT id FROM appointments"))
        assert (
            owner.post(
                "/terapiler", session_form(client, day(-1), OWNER_NOTE, owner_appt)
            ).status_code
            == 303
        )
        owner.post(
            f"/danisanlar/{client}/notlar",
            {"kind": "risk", "body": OWNER_LOG, "noted_at": ""},
        )
        owner.post("/odemeler", {"appointment_id": owner_appt, "amount": "2.000", "method": "card"})
        owner.post(
            "/giderler",
            {"spent_on": day(0).isoformat(), "category": "rent", "amount": "25.000",
             "method": "transfer", "description": "Kira"},
        )  # fmt: skip
        # The employee's own work at the main computer.
        assert (
            emp_main.post("/terapiler", session_form(client, day(-2), "Benim notum")).status_code
            == 303
        )

        sealed = owner.post(
            "/ayarlar/dizustu",
            {
                "user_id": str(B_ID),
                "label": "",
                "passphrase": PASSPHRASE,
                "passphrase2": PASSPHRASE,
            },
        )
        assert sealed.status_code == 200
        payload = open_sealed(sealed.content, PASSPHRASE)
        main_transport = httpx.ASGITransport(app=owner.app)

        class Switchable(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                if not online[0]:
                    raise httpx.ConnectError("main computer not reachable")
                return await main_transport.handle_async_request(request)

        employee = Kasa(
            copy_db,
            mode="replica",
            sync_database_url=copy_db.sync_url,
            office_url="https://ana.test",
            replica_token=payload["token"],
            replica_interval_seconds=0,
        )
        employee.app.state.replica.transport = Switchable()
        with employee:
            employee.client.portal.call(employee.app.state.replica.mark_replica)
            c = Clinic(main_db, copy_db, owner, emp_main, employee, client, owner_appt,
                       payload["token"], online)  # fmt: skip
            assert c.sync().last_error is None
            assert employee.login("terapist.b", emp_secret).status_code == 303
            c.owner_secret = owner_secret  # type: ignore[attr-defined]
            yield c


def test_the_copy_holds_only_what_she_may_see(clinic: Clinic) -> None:
    db = clinic.copy_db
    # The whole calendar and the client list.
    assert scalar(db, "SELECT count(*) FROM appointments") == 1
    assert scalar(db, "SELECT count(*) FROM clients") == 1
    # Only her own clinical records.
    assert scalar(db, "SELECT count(*) FROM therapy_sessions") == 1
    assert scalar(db, "SELECT therapist_id FROM therapy_sessions") == B_ID
    assert scalar(db, "SELECT count(*) FROM client_notes") == 0
    # No clinic money, no colleague's payments.
    assert scalar(db, "SELECT count(*) FROM expenses") == 0
    assert scalar(db, "SELECT count(*) FROM payments") == 0
    # Other users exist (names on the calendar) but cannot log in here.
    owner = fetch(db, "SELECT password_hash, totp_secret_enc FROM users WHERE id = $1", ADMIN_ID)[0]
    assert owner["password_hash"] == LOCKED_HASH and owner["totp_secret_enc"] is None
    assert scalar(db, "SELECT count(*) FROM recovery_codes WHERE user_id <> $1", B_ID) == 0
    # The owner's text is nowhere in the copy's database, not even encrypted.
    assert scalar(clinic.main_db, "SELECT count(*) FROM therapy_sessions") == 2
    assert scalar(clinic.main_db, "SELECT count(*) FROM client_notes") == 1


def test_she_sees_the_owners_appointment_but_not_its_notes(clinic: Clinic) -> None:
    page = clinic.employee.get(f"/takvim?gun={day(-1).isoformat()}").text
    assert "Zeynep Arslan" in page
    log = clinic.employee.get(f"/danisanlar/{clinic.client}/notlar").text
    assert "Benim notum" in log and OWNER_NOTE not in log and OWNER_LOG not in log


def test_the_owner_cannot_log_in_on_the_employees_laptop(clinic: Clinic) -> None:
    from support.local import PASSWORD

    clinic.employee.post("/logout")
    refused = clinic.employee.submit_password("yonetici", PASSWORD)
    assert refused.status_code != 303  # the owner's password does not work on this copy
    own = clinic.employee.submit_password("terapist.b", PASSWORD)
    assert own.status_code == 303 and own.headers["location"] == "/login/kod"


def test_offline_work_reaches_the_main_computer(clinic: Clinic) -> None:
    clinic.online[0] = False
    # She books for the owner, takes a payment at the desk for the owner's session ...
    booked = clinic.employee.post(
        "/randevular",
        {"client_id": clinic.client, "therapist_id": str(ADMIN_ID), "date": day(3).isoformat(),
         "time": "15:00", "duration_min": "50"},
    )  # fmt: skip
    assert booked.status_code == 303
    paid = clinic.employee.post(
        "/odemeler", {"appointment_id": clinic.owner_appt, "amount": "500", "method": "cash"}
    )
    assert "odeme=1" in paid.headers["location"]
    # ... and writes her own note.
    clinic.employee.post(
        f"/danisanlar/{clinic.client}/notlar",
        {"kind": "phone", "body": "Danışanın annesi aradı", "noted_at": ""},
    )
    clinic.online[0] = True
    status = clinic.sync()
    assert status.last_error is None and status.conflicts == 0
    assert scalar(clinic.main_db, "SELECT count(*) FROM appointments") == 2
    assert scalar(clinic.main_db, "SELECT count(*) FROM payments WHERE recorded_by = $1", B_ID) == 1
    gelir = clinic.owner.get("/gelir?donem=gun").text
    assert "2.500,00 ₺" in gelir  # the owner's income includes what the employee took
    assert "Danışanın annesi aradı" in clinic.owner.get(f"/danisanlar/{clinic.client}/notlar").text
    # On her laptop the 500 shows as collected for the owner, not as her income.
    own = clinic.employee.get("/gelir?donem=gun").text
    assert "Diğer terapistler adına aldığınız ödemeler" in own and "500,00 ₺" in own


def test_the_main_computer_refuses_what_she_may_not_send(clinic: Clinic) -> None:
    from klinik_local.replika import store

    owner_session = fetch(
        clinic.main_db, "SELECT * FROM therapy_sessions WHERE therapist_id = $1", ADMIN_ID
    )[0]
    forged = {
        "items": [
            {"t": "therapy_sessions", "id": str(owner_session["id"]),
             "base": None, "row": store.encode_row({**dict(owner_session), "duration_min": 10})}
        ]
    }  # fmt: skip
    headers = {"Authorization": f"Bearer {clinic.token}"}
    response = clinic.owner.client.post("/replika/api/push", json=forged, headers=headers)
    assert response.status_code == 400
    expense_id = uuid4()
    row = {"id": expense_id, "spent_on": day(0), "category": "rent", "amount": 1,
           "method": "cash", "recorded_by": B_ID}  # fmt: skip
    expense = {"items": [{"t": "expenses", "id": str(expense_id), "base": None,
                          "row": store.encode_row(row)}]}  # fmt: skip
    assert (
        clinic.owner.client.post("/replika/api/push", json=expense, headers=headers).status_code
        == 400
    )
    assert (
        scalar(
            clinic.main_db,
            "SELECT duration_min FROM therapy_sessions WHERE id = $1",
            owner_session["id"],
        )
        == 50
    )
