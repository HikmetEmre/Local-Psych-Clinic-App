"""Danışanlar: encryption at rest, code allocation, outbox and audit (real PostgreSQL)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from support.local import (
    A_ID,
    B_ID,
    FERNET_KEY,
    Kasa,
    LocalDb,
    add_therapists,
    fetch,
    run,
    scalar,
)

from klinik_local.crypto.fernet import FieldCipher
from klinik_local.danisanlar.service import ClientInput, ClientService, today_istanbul
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.permissions import expand_sets

pytestmark = pytest.mark.integration

YEAR = today_istanbul().year
NAME = "Zeynep Öztürk-Çağlar"
PHONE = "+90 532 111 22 33"
EMAIL = "zeynep@example.org"


@pytest.fixture
def kasa(local_db: LocalDb) -> Any:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as session:
        session.onboard("terapist.a", temporary["a"])
        yield session


def new_client(kasa: Kasa, **overrides: str) -> str:
    form = {
        "full_name": NAME,
        "phone": PHONE,
        "email": EMAIL,
        "birth_date": "2015-03-20",
        "assigned_therapist_id": str(A_ID),
        **overrides,
    }
    response = kasa.post("/danisanlar", form)
    assert response.status_code == 303, response.text
    return response.headers["location"].split("?")[0]


def outbox(db: LocalDb) -> list[dict[str, Any]]:
    rows = fetch(db, "SELECT kind, payload, state FROM sync_outbox ORDER BY seq")
    return [{"kind": r["kind"], "state": r["state"], **json.loads(r["payload"])} for r in rows]


def test_create_assigns_a_code_and_shows_the_client(kasa: Kasa, local_db: LocalDb) -> None:
    path = new_client(kasa)
    page = kasa.get(path)
    assert page.status_code == 200
    assert NAME in page.text and f"DN-{YEAR}-0001" in page.text

    listing = kasa.get("/danisanlar")
    assert NAME in listing.text and f"DN-{YEAR}-0001" in listing.text
    assert new_client(kasa, full_name="İkinci Danışan").startswith("/danisanlar/")
    codes = [
        r["client_code"] for r in fetch(local_db, "SELECT client_code FROM clients ORDER BY 1")
    ]
    assert codes == [f"DN-{YEAR}-0001", f"DN-{YEAR}-0002"]


def test_identity_is_encrypted_at_rest(kasa: Kasa, local_db: LocalDb) -> None:
    new_client(kasa)
    row = fetch(local_db, "SELECT * FROM clients")[0]
    for column, plaintext in (
        ("full_name_encrypted", NAME),
        ("phone_encrypted", PHONE),
        ("email_encrypted", EMAIL),
    ):
        stored = bytes(row[column])
        assert plaintext.encode() not in stored
        assert Fernet(FERNET_KEY.encode()).decrypt(stored).decode() == plaintext
    # Nothing readable anywhere in the row.
    dump = scalar(local_db, "SELECT clients::text FROM clients")
    # (bytea prints as hex, so only letters outside 0-9a-f are meaningful here)
    assert "Zeynep" not in dump and "example.org" not in dump


def test_the_database_cannot_be_read_with_a_different_key(kasa: Kasa, local_db: LocalDb) -> None:
    from cryptography.fernet import InvalidToken

    new_client(kasa)
    stored = bytes(scalar(local_db, "SELECT full_name_encrypted FROM clients"))
    with pytest.raises(InvalidToken):
        Fernet(Fernet.generate_key()).decrypt(stored)


def test_outbox_carries_only_code_active_and_therapist(kasa: Kasa, local_db: LocalDb) -> None:
    new_client(kasa)
    assert outbox(local_db) == [
        {
            "kind": "client_code.upsert",
            "state": "pending",
            "code": f"DN-{YEAR}-0001",
            "active": True,
            "assigned_therapist_id": str(A_ID),
        }
    ]
    dump = scalar(local_db, "SELECT string_agg(sync_outbox::text, ' ') FROM sync_outbox")
    assert "Zeynep" not in dump and "2015" not in dump


def test_audit_log_names_fields_but_never_values(kasa: Kasa, local_db: LocalDb) -> None:
    path = new_client(kasa)
    kasa.post(
        path,
        {
            "full_name": "Zeynep Öztürk",
            "phone": PHONE,
            "email": "",
            "birth_date": "2015-03-20",
            "assigned_therapist_id": str(A_ID),
            "version": "1",
        },
    )
    rows = fetch(
        local_db,
        "SELECT action, actor_id, entity_type, detail FROM audit_log "
        "WHERE entity_type = 'client' ORDER BY id",
    )
    assert [r["action"] for r in rows] == ["client_created", "client_updated"]
    assert all(r["actor_id"] == A_ID for r in rows)
    assert json.loads(rows[1]["detail"]) == {"changed": ["full_name", "email"]}

    dump = scalar(local_db, "SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    for secret in ("Zeynep", "Öztürk", "example.org", "532"):
        assert secret not in dump


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"full_name": "   "}, "Ad soyad zorunludur."),
        ({"full_name": "x" * 121}, "en fazla 120"),
        ({"birth_date": "2999-01-01"}, "gelecekte olamaz"),
        ({"birth_date": "1850-01-01"}, "Doğum tarihi geçersiz."),
        ({"birth_date": "not-a-date"}, "Doğum tarihi geçersiz."),
        ({"email": "not-an-email"}, "E-posta adresi geçersiz."),
        ({"phone": "call me maybe"}, "Telefon numarası geçersiz."),
        ({"assigned_therapist_id": "0b9c6f0e-5d1a-4f6b-9a53-000000000000"}, "Terapist seçimi"),
    ],
)
def test_invalid_input_is_rejected_and_nothing_is_stored(
    kasa: Kasa, local_db: LocalDb, overrides: dict[str, str], message: str
) -> None:
    form = {"full_name": NAME, "birth_date": "2015-03-20", **overrides}
    response = kasa.post("/danisanlar", form)
    assert response.status_code == 422
    assert message in response.text
    assert scalar(local_db, "SELECT count(*) FROM clients") == 0
    assert scalar(local_db, "SELECT count(*) FROM sync_outbox") == 0
    # A failed create must not burn a client number.
    assert scalar(local_db, "SELECT count(*) FROM client_code_counters") == 0


def test_optional_fields_may_be_left_empty(kasa: Kasa, local_db: LocalDb) -> None:
    response = kasa.post("/danisanlar", {"full_name": "Sadece İsim"})
    assert response.status_code == 303
    row = fetch(local_db, "SELECT * FROM clients")[0]
    assert row["phone_encrypted"] is None and row["email_encrypted"] is None
    assert row["birth_date"] is None and row["assigned_therapist_id"] is None
    assert outbox(local_db)[0]["assigned_therapist_id"] is None


def test_update_tells_the_cloud_only_when_the_therapist_changes(
    kasa: Kasa, local_db: LocalDb
) -> None:
    path = new_client(kasa)
    base = {"full_name": NAME, "phone": "", "email": "", "birth_date": "2015-03-20"}

    kasa.post(path, {**base, "assigned_therapist_id": str(A_ID), "version": "1"})
    assert len(outbox(local_db)) == 1  # a phone change is none of the cloud's business

    response = kasa.post(path, {**base, "assigned_therapist_id": str(B_ID), "version": "2"})
    assert response.status_code == 303
    items = outbox(local_db)
    assert len(items) == 2
    assert items[1]["assigned_therapist_id"] == str(B_ID) and items[1]["active"] is True


def test_stale_edit_is_refused(kasa: Kasa, local_db: LocalDb) -> None:
    path = new_client(kasa)
    form = {"full_name": "Birinci Değişiklik", "version": "1"}
    assert kasa.post(path, form).status_code == 303

    stale = kasa.post(path, {"full_name": "Eski Sekmeden Gelen", "version": "1"})
    assert stale.status_code == 409
    assert "başka biri tarafından değiştirildi" in stale.text
    assert "Birinci Değişiklik" in kasa.get(path).text
    assert scalar(local_db, "SELECT version FROM clients") == 2


def test_deactivate_and_reactivate(kasa: Kasa, local_db: LocalDb) -> None:
    path = new_client(kasa)
    assert kasa.post(f"{path}/durum", {"active": "0"}).status_code == 303
    assert scalar(local_db, "SELECT is_active FROM clients") is False
    assert NAME not in kasa.get("/danisanlar").text
    assert NAME in kasa.get("/danisanlar?durum=pasif").text
    assert NAME in kasa.get("/danisanlar?durum=tum").text

    assert kasa.post(f"{path}/durum", {"active": "1"}).status_code == 303
    assert [item["active"] for item in outbox(local_db)] == [True, False, True]
    actions = [
        r["action"]
        for r in fetch(
            local_db, "SELECT action FROM audit_log WHERE entity_type='client' ORDER BY id"
        )
    ]
    assert actions == ["client_created", "client_deactivated", "client_activated"]


def test_search_by_code_and_by_name_with_turkish_letters(kasa: Kasa) -> None:
    new_client(kasa, full_name="Işıl Çınar")
    new_client(kasa, full_name="İpek Şahin")
    new_client(kasa, full_name="Mehmet Demir")

    def names(query: str) -> set[str]:
        page = kasa.get("/danisanlar", params={"q": query}).text
        return {n for n in ("Işıl Çınar", "İpek Şahin", "Mehmet Demir") if n in page}

    assert names("ışıl") == {"Işıl Çınar"}
    assert names("ISIL") == {"Işıl Çınar"}
    assert names("ipek") == {"İpek Şahin"}
    assert names("İPEK") == {"İpek Şahin"}
    assert names("demir") == {"Mehmet Demir"}
    assert names(f"DN-{YEAR}-0002") == {"İpek Şahin"}
    assert names("0003") == {"Mehmet Demir"}
    assert names("yok böyle biri") == set()
    assert "Aramaya uyan danışan bulunamadı" in kasa.get("/danisanlar?q=zzz").text


def test_age_is_shown_from_the_birth_date(kasa: Kasa) -> None:
    today = today_istanbul()
    birthday_today = date(today.year - 9, today.month, today.day)
    path = new_client(kasa, birth_date=birthday_today.isoformat())
    assert "Yaş: 9" in kasa.get(path).text


def test_unknown_client_is_a_404(kasa: Kasa) -> None:
    missing = "/danisanlar/0b9c6f0e-5d1a-4f6b-9a53-ffffffffffff"
    assert kasa.get(missing).status_code == 404
    assert kasa.post(missing, {"full_name": "X", "version": "1"}).status_code == 404
    assert kasa.get("/danisanlar/not-a-uuid").status_code == 422


def test_html_in_a_name_is_escaped(kasa: Kasa) -> None:
    path = new_client(kasa, full_name="<script>alert(1)</script>")
    for page in (kasa.get(path).text, kasa.get("/danisanlar").text):
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


# ------------------------------------------------------------------ service level


def actor() -> CurrentUser:
    return CurrentUser(
        id=A_ID,
        username="terapist.a",
        display_name="Terapist A",
        permissions=frozenset(str(p) for p in expand_sets("admin", "therapist")),
        must_change_password=False,
        session_id_hash=b"",
        csrf_token="",
    )


SET_USER = text("SELECT set_config('app.current_user_id', :uid, true)")


def test_concurrent_creates_get_distinct_consecutive_codes(local_db: LocalDb) -> None:
    add_therapists(local_db)
    service = ClientService(FieldCipher(FERNET_KEY))
    count = 12

    async def create_one(engine: AsyncEngine, index: int) -> str:
        async with engine.begin() as conn:
            await conn.execute(SET_USER, {"uid": str(A_ID)})
            view = await service.create(
                conn, actor(), ClientInput(f"Danışan {index}", None, None, None, None)
            )
            await asyncio.sleep(0.01)  # hold the transaction open so they really overlap
            return view.client_code

    async def go() -> list[str]:
        engine = create_async_engine(local_db.app_url, pool_size=count)
        try:
            return list(await asyncio.gather(*(create_one(engine, i) for i in range(count))))
        finally:
            await engine.dispose()

    codes = run(go())
    assert sorted(codes) == [f"DN-{YEAR}-{n:04d}" for n in range(1, count + 1)]
    assert scalar(local_db, "SELECT last_n FROM client_code_counters") == count


def test_client_and_outbox_item_roll_back_together(local_db: LocalDb) -> None:
    """The outbox row is written in the same transaction as the client (brief §9)."""
    add_therapists(local_db)
    service = ClientService(FieldCipher(FERNET_KEY))

    async def go() -> None:
        engine = create_async_engine(local_db.app_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(SET_USER, {"uid": str(A_ID)})
                await service.create(
                    conn, actor(), ClientInput("Geri Alınacak", None, None, None, None)
                )
                counts = (
                    await conn.execute(
                        text(
                            "SELECT (SELECT count(*) FROM clients), "
                            "(SELECT count(*) FROM sync_outbox)"
                        )
                    )
                ).one()
                assert tuple(counts) == (1, 1)
                raise RuntimeError("boom")
        finally:
            await engine.dispose()

    with pytest.raises(RuntimeError, match="boom"):
        run(go())

    for table in (
        "clients",
        "sync_outbox",
        "client_code_counters",
        "audit_log WHERE entity_type='client'",
    ):
        assert scalar(local_db, f"SELECT count(*) FROM {table}") == 0, table
