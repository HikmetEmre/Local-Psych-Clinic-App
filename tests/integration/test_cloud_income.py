"""Gelir: payments and income visibility on a real PostgreSQL (brief §5, §7 plan M7)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg
import pytest
from support.cloud import CloudDb, Web, add_cloud_therapists, add_cloud_user, add_code, add_room
from support.local import A_ID, B_ID, execute, fetch, run, scalar

from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration

A_CODE = "DN-2026-0042"
B_CODE = "DN-2026-0043"
SECRETARY_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a09")


@dataclass
class Clinic:
    db: CloudDb
    a: Web
    b: Web
    a_appt: UUID  # A's session
    b_appt: UUID  # B's session


def make_appointment(db: CloudDb, code: str, therapist: UUID, room: UUID, days_ago: int) -> UUID:
    return UUID(
        str(
            scalar(
                db,  # type: ignore[arg-type]
                "INSERT INTO appointments (client_code, therapist_id, room_id, starts_at, "
                "duration_min) VALUES ($1, $2, $3, now() - make_interval(days => $4), 50) "
                "RETURNING id",
                code,
                therapist,
                room,
                days_ago,
            )
        )
    )


@pytest.fixture
def clinic(cloud_db: CloudDb) -> Iterator[Clinic]:
    temporary = add_cloud_therapists(cloud_db)
    room = add_room(cloud_db, "Oda 1")
    add_code(cloud_db, A_CODE, therapist=A_ID)
    add_code(cloud_db, B_CODE, therapist=B_ID)
    a_appt = make_appointment(cloud_db, A_CODE, A_ID, room, 1)
    b_appt = make_appointment(cloud_db, B_CODE, B_ID, room, 2)
    with Web(cloud_db) as a, Web(cloud_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        yield Clinic(cloud_db, a, b, a_appt, b_appt)


def pay(who: Web, appointment: UUID, amount: str, method: str = "cash", day: str = "") -> str:
    response = who.post(
        "/odemeler",
        {"appointment_id": str(appointment), "amount": amount, "method": method, "paid_on": day},
    )
    assert response.status_code == 303
    return str(response.headers["location"])


def totals(page: str) -> list[str]:
    return re.findall(r'class="kpi__value">([^<]+)<', page)


# ---------------------------------------------------------------- recording


def test_b_records_a_payment_for_a(clinic: Clinic) -> None:
    location = pay(clinic.b, clinic.a_appt, "1.500")
    assert location.endswith("odeme=1#odemeler")
    row = fetch(clinic.db, "SELECT * FROM payments")[0]  # type: ignore[arg-type]
    assert row["amount"] == Decimal("1500.00") and row["client_code"] == A_CODE
    assert row["recorded_by"] == B_ID and row["currency"] == "TRY"
    page = clinic.b.get(f"/randevular/{clinic.a_appt}").text
    assert "1.500,00 ₺" in page and "Nakit" in page
    audit = fetch(clinic.db, "SELECT detail FROM audit_log WHERE action = 'payment_recorded'")  # type: ignore[arg-type]
    assert len(audit) == 1


@pytest.mark.parametrize(
    ("amount", "day", "message"),
    [
        ("abc", "", "Tutar geçersiz."),
        ("0", "", "sıfırdan büyük"),
        ("100", "2999-01-01", "gelecekte olamaz"),
        ("100", "2000-01-01", "çok eski"),
    ],
)
def test_invalid_payments(clinic: Clinic, amount: str, day: str, message: str) -> None:
    location = pay(clinic.a, clinic.a_appt, amount, day=day)
    assert "odeme_hatasi" in location
    assert message in clinic.a.get(location).text
    assert scalar(clinic.db, "SELECT count(*) FROM payments") == 0  # type: ignore[arg-type]


def test_cancelled_appointment_takes_no_payment(clinic: Clinic) -> None:
    execute(clinic.db, "UPDATE appointments SET status = 'cancelled' WHERE id = $1", clinic.a_appt)  # type: ignore[arg-type]
    location = pay(clinic.a, clinic.a_appt, "500")
    assert "odeme_hatasi" in location
    assert scalar(clinic.db, "SELECT count(*) FROM payments") == 0  # type: ignore[arg-type]


def test_payment_without_an_appointment_or_with_a_wrong_code_is_impossible(clinic: Clinic) -> None:
    with pytest.raises(asyncpg.NotNullViolationError):
        execute(
            clinic.db,  # type: ignore[arg-type]
            "INSERT INTO payments (client_code, amount, method, paid_at, recorded_by) "
            "VALUES ($1, 10, 'cash', now(), $2)",
            A_CODE,
            A_ID,
        )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        execute(
            clinic.db,  # type: ignore[arg-type]
            "INSERT INTO payments (appointment_id, client_code, amount, method, paid_at, "
            "recorded_by) VALUES ($1, $2, 10, 'cash', now(), $3)",
            clinic.a_appt,
            B_CODE,  # the code of another client
            A_ID,
        )


def test_voiding_keeps_the_record_and_drops_it_from_totals(clinic: Clinic) -> None:
    pay(clinic.a, clinic.a_appt, "1000")
    pay(clinic.a, clinic.a_appt, "250", "card")
    wrong = scalar(clinic.db, "SELECT id FROM payments WHERE amount = 1000")  # type: ignore[arg-type]
    response = clinic.b.post(
        f"/odemeler/{wrong}/iptal",
        {"reason": "entry_error", "geri": f"/randevular/{clinic.a_appt}"},
    )
    assert response.status_code == 303
    page = clinic.a.get(f"/randevular/{clinic.a_appt}").text
    assert "İptal: Hatalı giriş" in page and "1.000,00 ₺" in page  # still shown
    assert "250,00 ₺</th>" in page  # total excludes it
    assert totals(clinic.a.get("/gelir").text)[0] == "250,00 ₺"
    # Voiding twice changes nothing; a bad reason is refused.
    clinic.b.post(f"/odemeler/{wrong}/iptal", {"reason": "refund"})
    assert (
        scalar(clinic.db, "SELECT void_reason FROM payments WHERE id = $1", wrong) == "entry_error"
    )  # type: ignore[arg-type]
    other = scalar(clinic.db, "SELECT id FROM payments WHERE amount = 250")  # type: ignore[arg-type]
    clinic.b.post(f"/odemeler/{other}/iptal", {"reason": "because"})
    assert scalar(clinic.db, "SELECT voided_at FROM payments WHERE id = $1", other) is None  # type: ignore[arg-type]


def test_amounts_cannot_be_edited_even_by_the_app_role(clinic: Clinic) -> None:
    pay(clinic.a, clinic.a_appt, "100")

    async def as_app(sql: str) -> None:
        conn = await asyncpg.connect(clinic.db.dsn(clinic.db.app_url))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    for statement in (
        "UPDATE payments SET amount = 1",
        "UPDATE payments SET paid_at = now() - interval '1 year'",
        "DELETE FROM payments",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(as_app(statement))


# ---------------------------------------------------------------- who sees what


def test_admin_sees_clinic_total_and_each_therapist(clinic: Clinic) -> None:
    pay(clinic.b, clinic.a_appt, "1000")
    pay(clinic.b, clinic.b_appt, "600", "transfer")
    page = clinic.a.get("/gelir").text
    assert totals(page)[0] == "1.600,00 ₺"
    assert "Terapist bazında" in page
    assert "1.000,00 ₺" in page and "600,00 ₺" in page
    only_b = clinic.a.get(f"/gelir?terapist={B_ID}").text
    assert totals(only_b)[0] == "600,00 ₺"


def test_therapist_without_read_all_sees_only_their_own(clinic: Clinic) -> None:
    pay(clinic.b, clinic.a_appt, "1000")
    pay(clinic.b, clinic.b_appt, "600")
    page = clinic.b.get("/gelir").text
    assert totals(page)[0] == "600,00 ₺"
    assert "Terapist bazında" not in page and A_CODE not in page
    assert "Yalnızca kendi seanslarınızın geliri" in page
    # Asking for A's income in the URL changes nothing.
    sneaky = clinic.b.get(f"/gelir?terapist={A_ID}").text
    assert totals(sneaky)[0] == "600,00 ₺" and A_CODE not in sneaky
    assert clinic.b.get("/gelir/odemeler.csv").status_code == 403


def test_a_pure_secretary_records_payments_but_sees_no_income(clinic: Clinic) -> None:
    temporary = add_cloud_user(clinic.db, SECRETARY_ID, "sekreter", "Sekreter", ["scheduler"])
    with Web(clinic.db) as secretary:
        secretary.onboard("sekreter", temporary)
        assert secretary.get("/gelir").status_code == 403
        assert 'href="/gelir"' not in secretary.get("/takvim").text
        pay(secretary, clinic.a_appt, "300")
    assert scalar(clinic.db, "SELECT recorded_by FROM payments") == SECRETARY_ID  # type: ignore[arg-type]


def test_export_for_the_admin(clinic: Clinic) -> None:
    pay(clinic.b, clinic.a_appt, "1500,50", "card")
    response = clinic.a.get("/gelir/odemeler.csv")
    assert response.status_code == 200
    body = response.content.decode("utf-8-sig")
    assert A_CODE in body and "1500,50" in body and "Kart" in body
    assert scalar(clinic.db, "SELECT count(*) FROM audit_log WHERE action = 'export'") == 1  # type: ignore[arg-type]


# ---------------------------------------------------------------- arithmetic and calendars


def test_sums_are_exact(clinic: Clinic) -> None:
    for amount in ("0,10", "0,20", "33,33", "33,33", "33,33"):
        pay(clinic.a, clinic.a_appt, amount)
    assert totals(clinic.a.get("/gelir").text)[0] == "100,29 ₺"
    assert scalar(clinic.db, "SELECT sum(amount) FROM payments") == Decimal("100.29")  # type: ignore[arg-type]


def test_month_edges_follow_istanbul_time(clinic: Clinic) -> None:
    def insert(local: datetime, amount: int) -> None:
        execute(
            clinic.db,  # type: ignore[arg-type]
            "INSERT INTO payments (appointment_id, client_code, amount, method, paid_at, "
            "recorded_by) VALUES ($1, $2, $3, 'cash', $4, $5)",
            clinic.a_appt,
            A_CODE,
            amount,
            local,
            A_ID,
        )

    insert(datetime(2026, 10, 31, 23, 30, tzinfo=ISTANBUL), 10)  # 20:30 UTC: still October
    insert(datetime(2026, 11, 1, 0, 30, tzinfo=ISTANBUL), 20)  # 21:30 UTC Oct 31: November
    insert(datetime(2026, 11, 30, 23, 59, tzinfo=ISTANBUL), 40)
    insert(datetime(2026, 12, 1, 0, 0, tzinfo=ISTANBUL), 80)
    assert totals(clinic.a.get("/gelir?donem=ay&tarih=2026-10-15").text)[0] == "10,00 ₺"
    assert totals(clinic.a.get("/gelir?donem=ay&tarih=2026-11-15").text)[0] == "60,00 ₺"
    assert totals(clinic.a.get("/gelir?donem=gun&tarih=2026-12-01").text)[0] == "80,00 ₺"
    week = clinic.a.get("/gelir?donem=hafta&tarih=2026-11-30").text  # Mon 30 Nov - Sun 6 Dec
    assert totals(week)[0] == "120,00 ₺"


def test_payment_dated_another_day_lands_on_that_day(clinic: Clinic) -> None:
    yesterday = (datetime.now(ISTANBUL) - timedelta(days=1)).date()
    pay(clinic.a, clinic.a_appt, "75", day=yesterday.isoformat())
    paid = scalar(clinic.db, "SELECT paid_at FROM payments")  # type: ignore[arg-type]
    assert paid.astimezone(ISTANBUL).date() == yesterday
