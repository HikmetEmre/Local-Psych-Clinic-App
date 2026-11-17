"""Gider (expenses), net income on the Gelir page and in the PDF report."""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from pypdf import PdfReader
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


class Office:
    def __init__(self, db: LocalDb, admin: Kasa, a: Kasa, b: Kasa) -> None:
        self.db, self.admin, self.a, self.b = db, admin, a, b


@pytest.fixture
def office(local_db: LocalDb) -> Iterator[Office]:
    temporary = add_therapists(local_db)
    admin_password = add_admin(local_db)
    with Kasa(local_db) as admin, Kasa(local_db) as a, Kasa(local_db) as b:
        admin.onboard("yonetici", admin_password)
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])  # plain therapist role
        admin.post("/ayarlar/odalar", {"label": "Oda 1"})
        yield Office(local_db, admin, a, b)


def today() -> str:
    return datetime.now(ISTANBUL).date().isoformat()


def add_expense(kasa: Kasa, **form: str) -> Any:
    data = {"spent_on": today(), "category": "rent", "amount": "10.000", "method": "transfer",
            "description": "Kasım kirası", "donem": "ay", **form}  # fmt: skip
    return kasa.post("/giderler", data)


def paid_session(office: Office, amount: str) -> None:
    made = office.admin.post("/danisanlar", {"full_name": "Zeynep Arslan"})
    client = made.headers["location"].split("/")[2].split("?")[0]
    office.a.post(
        "/randevular",
        {"client_id": client, "therapist_id": str(A_ID), "date": today(), "time": "08:00",
         "duration_min": "50"},
    )  # fmt: skip
    appt = str(scalar(office.db, "SELECT id FROM appointments"))
    office.a.post("/odemeler", {"appointment_id": appt, "amount": amount, "method": "cash"})


def test_admin_records_expenses_and_sees_the_net(office: Office) -> None:
    paid_session(office, "25.000")
    response = add_expense(office.admin)
    assert response.status_code == 303 and "gider=1" in response.headers["location"]
    add_expense(office.admin, category="utilities", amount="1.250,50", description="Elektrik")
    page = office.admin.get("/gelir?donem=ay").text
    assert "Gelir · Gider" in page
    assert "11.250,50 ₺" in page  # expenses
    assert "13.749,50 ₺" in page  # net = 25.000 - 11.250,50
    assert "Kasım kirası" in page and "Faturalar" in page
    row = fetch(office.db, "SELECT amount, category FROM expenses ORDER BY amount DESC")[0]
    assert str(row["amount"]) == "10000.00" and row["category"] == "rent"


def test_net_can_be_negative_and_voided_expenses_do_not_count(office: Office) -> None:
    add_expense(office.admin, amount="3.000")
    add_expense(office.admin, amount="500", category="cleaning", description="Temizlik")
    expense = str(scalar(office.db, "SELECT id FROM expenses WHERE amount = 3000"))
    voided = office.admin.post(
        f"/giderler/{expense}/iptal", {"reason": "entry_error", "donem": "ay"}
    )
    assert "gider=iptal" in voided.headers["location"]
    page = office.admin.get("/gelir?donem=ay").text
    assert "-500,00 ₺" in page and "kpi--loss" in page
    assert (
        scalar(office.db, "SELECT void_reason FROM expenses WHERE id = $1", expense)
        == "entry_error"
    )


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"category": "yacht"}, "Gider türü"),
        ({"amount": "abc"}, "Tutar geçersiz"),
        ({"amount": "0"}, "sıfırdan büyük"),
        ({"method": "crypto"}, "Ödeme yöntemi"),
        ({"description": "x" * 201}, "200 karakter"),
    ],
)
def test_bad_expenses_are_refused(office: Office, form: dict[str, str], message: str) -> None:
    response = add_expense(office.admin, **form)
    assert "gider_hatasi" in response.headers["location"]
    assert message in office.admin.get(response.headers["location"]).text
    assert scalar(office.db, "SELECT count(*) FROM expenses") == 0


def test_therapists_never_see_or_write_expenses(office: Office) -> None:
    add_expense(office.admin)
    assert add_expense(office.b).status_code == 403
    page = office.b.get("/gelir?donem=ay").text
    assert "Giderler" not in page and "Kasım kirası" not in page
    rows = execute(office.db, "SELECT 1", url=office.db.app_url)
    assert rows
    # The database itself hides them from a therapist.
    from support.local import run

    async def as_therapist() -> int:
        import asyncpg

        conn = await asyncpg.connect(office.db.dsn(office.db.app_url))
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(B_ID))
                return int(await conn.fetchval("SELECT count(*) FROM expenses"))
        finally:
            await conn.close()

    assert run(as_therapist()) == 0


def test_yearly_view_has_months(office: Office) -> None:
    add_expense(office.admin)
    page = office.admin.get("/gelir?donem=yil").text
    assert "Aylara göre" in page and "Ocak" in page and "Aralık" in page


def test_report_shows_expenses_and_net_income(office: Office) -> None:
    paid_session(office, "25.000")
    add_expense(office.admin)
    add_expense(office.admin, category="salary", amount="5.000", description="Sekreter maaşı")
    first = datetime.now(ISTANBUL).date().replace(day=1)
    params = {
        "rapor": "gelir",
        "baslangic": (first - timedelta(days=40)).isoformat(),
        "bitis": today(),
    }
    response = office.admin.get("/raporlar/pdf", params=params)
    assert response.status_code == 200
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
    assert "Gelir-gider raporu" in text
    assert "NET GELİR" in text and "10.000,00 ₺" in text  # 25.000 - 15.000
    assert "Kira" in text and "Personel / maaş" in text and "Sekreter maaşı" in text
    assert "Aylara göre gelir, gider ve net" in text

    therapist = office.b.get("/raporlar/pdf", params=params)
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(therapist.content)).pages)
    assert "Gelir raporu" in text and "NET GELİR" not in text and "Kira" not in text
