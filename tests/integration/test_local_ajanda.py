"""Ajandam: the clinic owner's diary (her clinic appointments + her private plans)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from support.local import (
    A_ID,
    ADMIN_ID,
    B_ID,
    Kasa,
    LocalDb,
    add_admin,
    add_therapists,
    fetch,
    run,
    scalar,
)

from klinik_local.ajanda.service import Item, columns, occurrence_starts
from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration


def day(offset: int) -> date:
    return datetime.now(ISTANBUL).date() + timedelta(days=offset)


class Office:
    def __init__(self, db: LocalDb, owner: Kasa, b: Kasa, client: str) -> None:
        self.db, self.owner, self.b, self.client = db, owner, b, client

    def plan(self, **form: str) -> Any:
        data = {
            "title": "Süpervizyon", "category": "training", "start_date": day(1).isoformat(),
            "start_time": "13:00", "end_date": day(1).isoformat(), "end_time": "14:30",
            "repeat": "none", "location": "Online", "note": "Vaka sunumu hazırla", **form,
        }  # fmt: skip
        return self.owner.post("/ajanda", data)


@pytest.fixture
def office(local_db: LocalDb) -> Iterator[Office]:
    temporary = add_therapists(local_db)
    owner_password = add_admin(local_db)
    with Kasa(local_db) as owner, Kasa(local_db) as b:
        owner.onboard("yonetici", owner_password)
        b.onboard("terapist.b", temporary["b"])
        owner.post("/ayarlar/odalar", {"label": "Oda 1"})
        owner.post("/ayarlar/odalar", {"label": "Oda 2"})
        made = owner.post("/danisanlar", {"full_name": "Zeynep Arslan"})
        client = made.headers["location"].split("/")[2].split("?")[0]
        yield Office(local_db, owner, b, client)


def book(kasa: Kasa, client: str, therapist: Any, when: date, clock: str) -> None:
    response = kasa.post(
        "/randevular",
        {"client_id": client, "therapist_id": str(therapist), "date": when.isoformat(),
         "time": clock, "duration_min": "50"},
    )  # fmt: skip
    assert response.status_code == 303, response.text


def test_only_the_owner_has_the_tab(office: Office) -> None:
    assert "/ajanda" in office.owner.get("/").text
    assert "/ajanda" not in office.b.get("/").text
    assert office.b.get("/ajanda").status_code == 403
    assert office.b.post("/ajanda", {"title": "x"}).status_code == 403


def test_her_appointments_and_plans_on_one_calendar(office: Office) -> None:
    book(office.owner, office.client, ADMIN_ID, day(1), "10:00")
    book(office.b, office.client, B_ID, day(1), "16:00")  # someone else's: not in her diary
    assert "durum=kaydedildi" in office.plan().headers["location"]
    week = office.owner.get(f"/ajanda?gorunum=hafta&gun={day(1).isoformat()}").text
    assert "Zeynep Arslan" in week and "10:00–10:50" in week
    assert "Süpervizyon" in week and "13:00–14:30" in week and "Online" in week
    assert "16:00–16:50" not in week
    # Positioned on the time grid: 13:00 is 24 quarter-hours after 07:00; 90 min = 6.
    assert "ag-t-24 ag-h-6" in week
    for view in ("ay", "gun", "liste"):
        page = office.owner.get(f"/ajanda?gorunum={view}&gun={day(1).isoformat()}")
        assert page.status_code == 200 and "Süpervizyon" in page.text, view
    only_plans = office.owner.get(f"/ajanda?gun={day(1).isoformat()}&randevular=0").text
    assert "Süpervizyon" in only_plans and "Zeynep Arslan" not in only_plans


def test_plans_are_encrypted_and_private(office: Office) -> None:
    office.plan(title="Doktor randevusu", category="personal", note="Kan tahlili")
    row = fetch(office.db, "SELECT * FROM personal_events")[0]
    assert b"Doktor" not in bytes(row["title_encrypted"])
    assert b"Kan" not in bytes(row["note_encrypted"])

    async def as_user(user_id: Any) -> int:
        import asyncpg

        conn = await asyncpg.connect(office.db.dsn(office.db.app_url))
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', $1, true)", str(user_id)
                )
                return int(await conn.fetchval("SELECT count(*) FROM personal_events"))
        finally:
            await conn.close()

    assert run(as_user(ADMIN_ID)) == 1
    assert run(as_user(B_ID)) == 0  # the database hides them from everyone else
    assert run(as_user(A_ID)) == 0  # ... even from a user with other admin tools
    # Never part of the sync with connected computers.
    from klinik_local.replika.store import BY_NAME

    assert "personal_events" not in BY_NAME


def test_repeating_plan_and_deleting_one_occurrence(office: Office) -> None:
    office.plan(
        title="Yoga", category="personal", start_date=day(0).isoformat(), start_time="08:00",
        end_date=day(0).isoformat(), end_time="09:00", repeat="weekly",
        repeat_until=day(21).isoformat(),
    )  # fmt: skip
    listing = office.owner.get(f"/ajanda?gorunum=liste&gun={day(0).isoformat()}").text
    assert listing.count(">Yoga</a>") == 4  # today, +7, +14, +21
    plan_id = str(scalar(office.db, "SELECT id FROM personal_events"))
    edit = office.owner.get(f"/ajanda/{plan_id}/duzenle?tarih={day(7).isoformat()}")
    assert "Yalnızca bu tekrarı sil" in edit.text
    office.owner.post(f"/ajanda/{plan_id}/sil", {"kapsam": "bu", "tarih": day(7).isoformat()})
    listing = office.owner.get(f"/ajanda?gorunum=liste&gun={day(0).isoformat()}").text
    assert listing.count(">Yoga</a>") == 3
    office.owner.post(f"/ajanda/{plan_id}/sil", {"kapsam": "hepsi"})
    listing = office.owner.get(f"/ajanda?gorunum=liste&gun={day(0).isoformat()}").text
    assert ">Yoga</a>" not in listing


def test_editing_and_all_day_plans(office: Office) -> None:
    office.plan(title="Kongre", category="travel", all_day="1", start_date=day(2).isoformat(),
                end_date=day(4).isoformat())  # fmt: skip
    plan_id = str(scalar(office.db, "SELECT id FROM personal_events"))
    month = office.owner.get(f"/ajanda?gorunum=ay&gun={day(2).isoformat()}").text
    assert month.count("Kongre") >= 3  # shown on each of its three days
    form = office.owner.get(f"/ajanda/{plan_id}/duzenle").text
    assert f'value="{day(4).isoformat()}"' in form  # inclusive last day
    saved = office.owner.post(
        f"/ajanda/{plan_id}",
        {"title": "Psikoloji Kongresi", "category": "travel", "all_day": "1",
         "start_date": day(2).isoformat(), "end_date": day(3).isoformat(), "repeat": "none",
         "version": "1"},
    )  # fmt: skip
    assert saved.status_code == 303
    stale = office.owner.post(
        f"/ajanda/{plan_id}",
        {"title": "Eski", "category": "travel", "all_day": "1", "start_date": day(2).isoformat(),
         "end_date": day(3).isoformat(), "repeat": "none", "version": "1"},
    )  # fmt: skip
    assert stale.status_code == 409
    week = office.owner.get(f"/ajanda?gorunum=hafta&gun={day(2).isoformat()}").text
    assert "Psikoloji Kongresi" in week


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"title": "  "}, "Başlık yazın"),
        ({"end_time": "12:00"}, "Bitiş, başlangıçtan sonra"),
        ({"repeat": "weekly"}, "Tekrarın biteceği tarihi"),
        ({"repeat": "daily", "repeat_until": "2099-01-01"}, "en fazla 2 yıl"),
        ({"category": "party"}, "Tür seçin"),
    ],
)
def test_bad_plans_are_refused(office: Office, form: dict[str, str], message: str) -> None:
    response = office.plan(**form)
    assert response.status_code == 422 and message in response.text
    assert scalar(office.db, "SELECT count(*) FROM personal_events") == 0


def test_layout_puts_overlapping_items_side_by_side() -> None:
    from uuid import uuid4

    d = day(0)

    def item(h: int, m: int, minutes: int) -> Item:
        start = datetime(d.year, d.month, d.day, h, m, tzinfo=ISTANBUL)
        return Item("plan", uuid4(), "x", start, start + timedelta(minutes=minutes))

    col = columns([d], [item(10, 0, 60), item(10, 30, 60), item(12, 0, 30)], d)[0]
    placed = sorted(col.timed, key=lambda p: p.top)
    assert [p.css for p in placed] == [
        "ag-t-12 ag-h-4 ag-l-0-2", "ag-t-14 ag-h-4 ag-l-1-2", "ag-t-20 ag-h-2 ag-l-0-1",
    ]  # fmt: skip


def test_monthly_repeat_keeps_the_day_or_the_month_end() -> None:
    class Row:
        starts_at = datetime(2027, 1, 31, 9, tzinfo=ISTANBUL)
        ends_at = datetime(2027, 1, 31, 10, tzinfo=ISTANBUL)
        repeat = "monthly"
        repeat_until = date(2027, 4, 30)
        skip_dates: tuple[date, ...] = ()

    days = occurrence_starts(Row(), date(2027, 1, 1), date(2027, 5, 1))
    assert days == [date(2027, 1, 31), date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)]
