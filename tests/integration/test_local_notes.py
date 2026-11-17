"""Klinik notlar: the per-client clinical log, its privacy and its history."""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pypdf import PdfReader
from support.local import (
    B_ID,
    FERNET_KEY,
    Kasa,
    LocalDb,
    add_admin,
    add_therapists,
    fetch,
    scalar,
)

from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration
SECRET = "Danışan kendine zarar verme düşüncesinden bahsetti; güvenlik planı yapıldı."


class Office:
    def __init__(self, db: LocalDb, admin: Kasa, a: Kasa, b: Kasa, client: str) -> None:
        self.db, self.admin, self.a, self.b, self.client = db, admin, a, b, client

    @property
    def notes(self) -> str:
        return f"/danisanlar/{self.client}/notlar"

    def write(self, kasa: Kasa, body: str = SECRET, **form: str) -> str:
        data = {"kind": "risk", "title": "Risk değerlendirmesi", "body": body, "important": "1",
                "noted_at": datetime.now(ISTANBUL).strftime("%Y-%m-%dT%H:%M"), **form}  # fmt: skip
        response = kasa.post(self.notes, data)
        assert response.status_code == 303, response.text
        return response.headers["location"].split("#not-")[1]


@pytest.fixture
def office(local_db: LocalDb) -> Iterator[Office]:
    temporary = add_therapists(local_db)
    admin_password = add_admin(local_db)
    with Kasa(local_db) as admin, Kasa(local_db) as a, Kasa(local_db) as b:
        admin.onboard("yonetici", admin_password)
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        made = admin.post("/danisanlar", {"full_name": "Elif Kaya", "birth_date": "2015-02-02"})
        client = made.headers["location"].split("/")[2].split("?")[0]
        yield Office(local_db, admin, a, b, client)


def test_a_note_is_saved_encrypted_and_shown_in_the_log(office: Office) -> None:
    note = office.write(office.b)
    page = office.b.get(office.notes).text
    assert SECRET in page and "Risk değerlendirmesi" in page and "Önemli" in page
    assert f'id="not-{note}"' in page
    stored = fetch(office.db, "SELECT title_encrypted, body_encrypted FROM client_notes")[0]
    assert SECRET.encode() not in bytes(stored["body_encrypted"])
    assert Fernet(FERNET_KEY.encode()).decrypt(bytes(stored["body_encrypted"])).decode() == SECRET
    audit = fetch(office.db, "SELECT detail FROM audit_log WHERE action = 'note_created'")
    assert json.loads(audit[0]["detail"])["kind"] == "risk"
    assert "zarar" not in audit[0]["detail"]


def test_notes_are_private_to_the_author_and_the_admin(office: Office) -> None:
    office.write(office.b)
    assert SECRET in office.admin.get(office.notes).text
    assert SECRET not in office.a.get(office.notes).text  # a colleague never sees it
    assert scalar(office.db, "SELECT count(*) FROM client_notes") == 1


def test_editing_keeps_the_previous_text_in_the_history(office: Office) -> None:
    note = office.write(office.b, body="İlk hali")
    edit_page = office.b.get(f"{office.notes}/{note}/duzenle")
    assert edit_page.status_code == 200 and "İlk hali" in edit_page.text
    saved = office.b.post(
        f"{office.notes}/{note}",
        {"kind": "observation", "title": "", "body": "Düzeltilmiş hali", "version": "1",
         "noted_at": datetime.now(ISTANBUL).strftime("%Y-%m-%dT%H:%M")},
    )  # fmt: skip
    assert "durum=guncellendi" in saved.headers["location"]
    page = office.b.get(office.notes).text
    assert "Düzeltilmiş hali" in page and "Geçmiş (1 önceki sürüm)" in page and "İlk hali" in page
    # A stale second edit is refused, not silently applied.
    stale = office.b.post(
        f"{office.notes}/{note}",
        {"kind": "general", "title": "", "body": "Eski sekmeden", "version": "1", "noted_at": ""},
    )
    assert stale.status_code == 409


def test_only_the_author_edits_or_deletes(office: Office) -> None:
    note = office.write(office.b)
    assert office.admin.get(f"{office.notes}/{note}/duzenle").status_code == 404
    assert office.admin.post(f"{office.notes}/{note}/sil").status_code == 404
    assert office.a.post(f"{office.notes}/{note}/sil").status_code == 404


def test_deleting_hides_the_note_but_keeps_its_text(office: Office) -> None:
    note = office.write(office.b)
    deleted = office.b.post(f"{office.notes}/{note}/sil")
    assert "durum=silindi" in deleted.headers["location"]
    assert SECRET not in office.b.get(office.notes).text
    assert scalar(office.db, "SELECT deleted_at IS NOT NULL FROM client_notes") is True
    assert scalar(office.db, "SELECT reason FROM note_revisions") == "deleted"


def test_session_records_and_their_note_edits_are_in_the_same_log(office: Office) -> None:
    when = (datetime.now(ISTANBUL) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    made = office.b.post(
        "/terapiler",
        {"client_id": office.client, "session_date": when, "duration_min": "50",
         "format_code": "individual", "method": ["play_therapy"], "primary": "play_therapy",
         "status": "completed", "notes": "Oyunda kardeş teması", "homework": "Resim çizmek"},
    )  # fmt: skip
    session = made.headers["location"].split("/")[2].split("?")[0]
    office.write(office.b, body="Anne ile telefon görüşmesi", kind="phone", session_id=session)
    page = office.b.get(office.notes).text
    assert "Seans 1" in page and "Oyunda kardeş teması" in page and "Resim çizmek" in page
    assert "Telefon görüşmesi" in page and "Oyun Terapisi" in page
    # Changing the session's note keeps the earlier text too.
    patched = office.b.api(
        "PATCH", f"/api/therapy/sessions/{session}", json={"notes": "Güncel seans notu"}
    )
    assert patched.status_code == 200, patched.text
    page = office.b.get(office.notes).text
    assert "Güncel seans notu" in page and "Oyunda kardeş teması" in page
    assert (
        scalar(office.db, "SELECT count(*) FROM note_revisions WHERE session_id IS NOT NULL") == 1
    )


def test_filters_and_search(office: Office) -> None:
    office.write(office.b, body="Uyku düzeni bozulmuş", kind="observation", important="")
    office.write(office.b, body="Okul rehber öğretmeni aradı", kind="phone", important="")
    by_kind = office.b.get(office.notes, params={"tur": "phone"}).text
    assert "rehber öğretmeni" in by_kind and "Uyku düzeni" not in by_kind
    # Search works on the decrypted text, Turkish letters included.
    found = office.b.get(office.notes, params={"ara": "UYKU"}).text
    assert "Uyku düzeni" in found and "rehber" not in found
    none = office.b.get(office.notes, params={"onemli": "1"}).text
    assert "eşleşen kayıt yok" in none


def test_validation(office: Office) -> None:
    empty = office.b.post(office.notes, {"kind": "general", "body": "   ", "noted_at": ""})
    assert empty.status_code == 422 and "Not boş olamaz" in empty.text
    future = (datetime.now(ISTANBUL) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    later = office.b.post(office.notes, {"kind": "general", "body": "x", "noted_at": future})
    assert later.status_code == 422 and "gelecekte" in later.text
    bad = office.b.post(office.notes, {"kind": "diary", "body": "x", "noted_at": ""})
    assert bad.status_code == 422
    assert scalar(office.db, "SELECT count(*) FROM client_notes") == 0


def test_database_refuses_forged_authorship(office: Office) -> None:
    import asyncpg
    from support.local import run

    async def forge() -> None:
        conn = await asyncpg.connect(office.db.dsn(office.db.app_url))
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(B_ID))
                await conn.execute(
                    "INSERT INTO client_notes (client_id, author_id, kind, body_encrypted, "
                    "noted_at) SELECT $1, id, 'general', '\\x00', now() FROM users "
                    "WHERE username = 'terapist.a'",
                    office.client,
                )
        finally:
            await conn.close()

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(forge())
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(_as_app(office.db, "DELETE FROM note_revisions"))


async def _as_app(db: LocalDb, sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(db.dsn(db.app_url))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def test_client_pages_link_to_the_log_and_the_pdf_prints_it(office: Office) -> None:
    office.write(office.b)
    assert (
        f"/danisanlar/{office.client}/notlar" in office.b.get(f"/danisanlar/{office.client}").text
    )
    assert "Klinik notlar" in office.b.get("/danisanlar").text
    pdf = office.b.get(
        "/raporlar/pdf", params={"rapor": "danisan", "danisan": office.client, "notlar": "1"}
    )
    text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Klinik notlar" in text and "Risk / güvenlik notu" in text and "ÖNEMLİ" in text
    assert re.search(r"güvenlik\s+planı", text)
