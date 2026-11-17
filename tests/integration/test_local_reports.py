"""PDF reports: content, Turkish text, and who may see what."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
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
    fetch,
    scalar,
)

from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration


def day(offset: int) -> date:
    return datetime.now(ISTANBUL).date() + timedelta(days=offset)


def text_of(response: Any) -> str:
    assert response.status_code == 200, response.text[:500]
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    if dump := os.environ.get("KLINIK_DUMP_PDF_DIR"):  # to look at the layout by eye
        name = response.headers["content-disposition"].split('"')[1]
        (Path(dump) / f"{len(list(Path(dump).iterdir())):02d}-{name}").write_bytes(response.content)
    reader = PdfReader(io.BytesIO(response.content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


class Office:
    def __init__(self, db: LocalDb, admin: Kasa, a: Kasa, b: Kasa) -> None:
        self.db, self.admin, self.a, self.b = db, admin, a, b

    def client(self, name: str) -> str:
        r = self.admin.post("/danisanlar", {"full_name": name, "birth_date": "1988-02-03"})
        assert r.status_code == 303, r.text
        return r.headers["location"].split("/")[2].split("?")[0]

    def book(self, kasa: Kasa, client: str, therapist: Any, when: date, clock: str) -> str:
        r = kasa.post(
            "/randevular",
            {"client_id": client, "therapist_id": str(therapist), "date": when.isoformat(),
             "time": clock, "duration_min": "50"},
        )  # fmt: skip
        assert r.status_code == 303, r.text
        return str(
            scalar(
                self.db,
                "SELECT id FROM appointments WHERE client_id = $1 ORDER BY created_at DESC LIMIT 1",
                client,
            )
        )

    def session(self, kasa: Kasa, client: str, appt: str, when: date, note: str) -> None:
        r = kasa.post(
            "/terapiler",
            {
                "client_id": client, "session_date": f"{when.isoformat()}T10:00",
                "duration_min": "50", "format_code": "individual", "method": ["cbt", "act"],
                "primary": "cbt", "status": "completed", "notes": note,
                "homework": "Nefes egzersizi", "appointment_id": appt,
            },
        )  # fmt: skip
        assert r.status_code == 303, r.text


@pytest.fixture
def office(local_db: LocalDb) -> Iterator[Office]:
    temporary = add_therapists(local_db)
    admin_password = add_admin(local_db)
    with Kasa(local_db) as admin, Kasa(local_db) as a, Kasa(local_db) as b:
        admin.onboard("yonetici", admin_password)
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        assert admin.post("/ayarlar/odalar", {"label": "Oda Çiçek"}).status_code == 303
        yield Office(local_db, admin, a, b)


@pytest.fixture
def filled(office: Office) -> Office:
    zeynep = office.client("Zeynep Şahin Güler")
    ismail = office.client("İsmail Öztürk")
    yesterday = day(-1)
    za = office.book(office.a, zeynep, A_ID, yesterday, "10:00")
    ib = office.book(office.b, ismail, B_ID, yesterday, "12:00")
    office.session(office.a, zeynep, za, yesterday, "A'nın gizli notu: ilerleme var.")
    office.session(office.b, ismail, ib, yesterday, "B'nin gizli notu: kaygı azaldı.")
    office.a.post("/odemeler", {"appointment_id": za, "amount": "1.200", "method": "card"})
    office.b.post("/odemeler", {"appointment_id": ib, "amount": "900", "method": "cash"})
    office.book(office.a, ismail, A_ID, day(2), "15:00")
    office.zeynep, office.ismail = zeynep, ismail  # type: ignore[attr-defined]
    return office


def period(**extra: str) -> dict[str, str]:
    return {"baslangic": day(-10).isoformat(), "bitis": day(10).isoformat(), **extra}


def test_reports_page_lists_reports_by_role(office: Office) -> None:
    admin = office.admin.get("/raporlar").text
    for title in ("Seans raporu", "Danışan dosyası", "Randevu listesi", "Gelir raporu",
                  "Randevu istatistikleri"):  # fmt: skip
        assert title in admin
    therapist = office.b.get("/raporlar").text
    assert "Yalnızca sizin seanslarınız" in therapist
    assert "Yalnızca sizin gelirleriniz" in therapist


def test_admin_session_report_has_everyone_and_notes(filled: Office) -> None:
    text = text_of(
        filled.admin.get("/raporlar/pdf", params={"rapor": "seanslar", "notlar": "1", **period()})
    )
    assert "Seans raporu" in text and "Nil Psikoloji" in text
    assert "Zeynep Şahin Güler" in text and "İsmail Öztürk" in text
    assert "Terapist A" in text and "Terapist B" in text
    assert "ilerleme var" in text and "kaygı azaldı" in text
    assert "Bilişsel" in text or "CBT" in text or "cbt" in text.lower()
    assert "Sayfa 1 /" in text


def test_therapist_session_report_is_own_only_whatever_is_asked(filled: Office) -> None:
    text = text_of(
        filled.b.get(
            "/raporlar/pdf",
            params={"rapor": "seanslar", "notlar": "1", "terapist": str(A_ID), **period()},
        )
    )
    assert "kaygı azaldı" in text
    assert "ilerleme var" not in text and "Zeynep" not in text


def test_client_file(filled: Office) -> None:
    zeynep = filled.zeynep  # type: ignore[attr-defined]
    text = text_of(
        filled.a.get("/raporlar/pdf", params={"rapor": "danisan", "danisan": zeynep, "notlar": "1"})
    )
    assert "Danışan dosyası: Zeynep Şahin Güler" in text
    assert "03.02.1988" in text
    assert "ilerleme var" in text and "Nefes egzersizi" in text
    assert "1.200,00 ₺" in text
    # B has no sessions with Zeynep and she is not assigned to B.
    refused = filled.b.get("/raporlar/pdf", params={"rapor": "danisan", "danisan": zeynep})
    assert refused.status_code == 404


def test_appointment_list_and_statistics(filled: Office) -> None:
    listing = text_of(filled.b.get("/raporlar/pdf", params={"rapor": "randevular", **period()}))
    assert "Randevu listesi" in listing
    assert "Zeynep Şahin Güler" in listing and "İsmail Öztürk" in listing
    assert "Oda Çiçek" in listing
    stats = text_of(filled.admin.get("/raporlar/pdf", params={"rapor": "istatistik", **period()}))
    assert "Katılım oranı" in stats and "%100" in stats
    assert "Toplam randevu" in stats


def test_income_report_by_role(filled: Office) -> None:
    clinic = text_of(filled.admin.get("/raporlar/pdf", params={"rapor": "gelir", **period()}))
    assert "2.100,00 ₺" in clinic and "Kart" in clinic and "Nakit" in clinic
    own = text_of(
        filled.b.get("/raporlar/pdf", params={"rapor": "gelir", "terapist": str(A_ID), **period()})
    )
    assert "900,00 ₺" in own and "1.200,00 ₺" not in own and "Zeynep" not in own


def test_presets_and_empty_period(office: Office) -> None:
    text = text_of(
        office.admin.get("/raporlar/pdf", params={"rapor": "gelir", "donem": "gecen-ay"})
    )
    assert "Bu dönemde kayıt yok." in text


def test_report_generation_is_audited_without_content(filled: Office) -> None:
    filled.admin.get("/raporlar/pdf", params={"rapor": "seanslar", "notlar": "1", **period()})
    rows = fetch(
        filled.db, "SELECT actor_id, detail FROM audit_log WHERE action = 'report_generated'"
    )
    assert len(rows) == 1
    detail = json.loads(rows[0]["detail"])
    assert detail["report"] == "seanslar" and detail["notes"] is True
    assert "ilerleme" not in rows[0]["detail"]


def test_bad_requests(office: Office) -> None:
    assert office.b.get("/raporlar/pdf", params={"rapor": "yok"}).status_code == 403
    assert office.admin.get("/raporlar/pdf", params={"rapor": "danisan"}).status_code == 422
    too_long = {"rapor": "gelir", "baslangic": "2024-01-01", "bitis": "2026-01-01"}
    assert office.admin.get("/raporlar/pdf", params=too_long).status_code == 422
