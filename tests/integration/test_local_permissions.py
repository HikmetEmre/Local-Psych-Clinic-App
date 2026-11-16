"""Who may open what in Klinik Kasa (two-user permission matrix, brief §5)."""

from __future__ import annotations

from uuid import UUID

import pytest
from support.local import (
    A_ID,
    B_ID,
    PASSWORD,
    Kasa,
    LocalDb,
    add_therapists,
    add_user,
    execute,
    fetch,
    scalar,
)

from klinik_shared.permissions import Permission

pytestmark = pytest.mark.integration

READER_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a03")
NOBODY_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a04")

ADMIN_POSTS = (
    f"/ayarlar/kullanicilar/{A_ID}/yetkiler",
    f"/ayarlar/kullanicilar/{A_ID}/durum",
    f"/ayarlar/kullanicilar/{A_ID}/2fa-sifirla",
    f"/ayarlar/kullanicilar/{A_ID}/sifre-sifirla",
)


@pytest.fixture
def secrets(local_db: LocalDb) -> dict[str, str]:
    temporary = add_therapists(local_db)
    temporary["reader"] = add_user(
        local_db, READER_ID, "okuyucu", "Sadece Okur", [Permission.CLIENTS_READ]
    )
    temporary["nobody"] = add_user(local_db, NOBODY_ID, "yetkisiz", "Yetkisiz", [])
    usernames = {"a": "terapist.a", "b": "terapist.b", "reader": "okuyucu", "nobody": "yetkisiz"}
    result = {}
    for key, username in usernames.items():
        with Kasa(local_db) as kasa:
            result[key], _ = kasa.onboard(username, temporary[key])
    return result


def test_anonymous_visitors_are_sent_to_login(local_db: LocalDb) -> None:
    with Kasa(local_db) as kasa:
        for path in ("/", "/danisanlar", "/danisanlar/yeni", "/ayarlar", "/ayarlar/kullanicilar"):
            response = kasa.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login", path
        assert kasa.get("/healthz").status_code == 200
        assert kasa.get("/static/ui/css/klinik.css").status_code == 200


def test_admin_therapist_sees_clients_and_settings(
    local_db: LocalDb, secrets: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", secrets["a"])
        assert kasa.get("/danisanlar").status_code == 200
        assert kasa.get("/danisanlar/yeni").status_code == 200
        assert kasa.get("/ayarlar/kullanicilar").status_code == 200
        home = kasa.get("/").text
        assert 'href="/ayarlar"' in home and 'href="/danisanlar"' in home


def test_therapist_scheduler_has_no_admin_access(
    local_db: LocalDb, secrets: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.b", secrets["b"])
        assert kasa.get("/danisanlar").status_code == 200
        assert kasa.get("/danisanlar/yeni").status_code == 200
        assert kasa.get("/ayarlar").status_code == 403
        assert kasa.get("/ayarlar/kullanicilar").status_code == 403
        for path in ADMIN_POSTS:
            assert kasa.post(path, {"permission": "admin.users", "active": "0"}).status_code == 403
        assert 'href="/ayarlar"' not in kasa.get("/").text
    # Nothing changed for A.
    assert scalar(local_db, "SELECT is_active FROM users WHERE id = $1", A_ID) is True
    assert scalar(local_db, "SELECT totp_confirmed_at FROM users WHERE id = $1", A_ID) is not None


def test_read_only_user_cannot_write_clients(local_db: LocalDb, secrets: dict[str, str]) -> None:
    with Kasa(local_db) as writer:
        writer.login("terapist.a", secrets["a"])
        writer.post("/danisanlar", {"full_name": "Ayşe Yılmaz"})
    client_id = scalar(local_db, "SELECT id FROM clients")

    with Kasa(local_db) as kasa:
        kasa.login("okuyucu", secrets["reader"])
        assert kasa.get("/danisanlar").status_code == 200
        page = kasa.get(f"/danisanlar/{client_id}")
        assert page.status_code == 200 and "Ayşe Yılmaz" in page.text
        assert "Kaydet" not in page.text
        assert kasa.get("/danisanlar/yeni").status_code == 403
        assert kasa.post("/danisanlar", {"full_name": "Yeni Kişi"}).status_code == 403
        assert (
            kasa.post(f"/danisanlar/{client_id}", {"full_name": "X", "version": "1"}).status_code
            == 403
        )
        assert kasa.post(f"/danisanlar/{client_id}/durum", {"active": "0"}).status_code == 403
    assert scalar(local_db, "SELECT count(*) FROM clients") == 1
    assert scalar(local_db, "SELECT is_active FROM clients") is True


def test_user_without_permissions_sees_no_client_data(
    local_db: LocalDb, secrets: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("yetkisiz", secrets["nobody"])
        assert kasa.get("/").status_code == 200
        assert kasa.get("/danisanlar").status_code == 403
        assert kasa.post("/danisanlar", {"full_name": "Yeni Kişi"}).status_code == 403
        assert 'href="/danisanlar"' not in kasa.get("/").text


def test_admin_edits_permissions_and_they_apply_immediately(
    local_db: LocalDb, secrets: dict[str, str]
) -> None:
    with Kasa(local_db) as admin, Kasa(local_db) as other:
        admin.login("terapist.a", secrets["a"])
        other.login("terapist.b", secrets["b"])
        assert other.get("/danisanlar").status_code == 200

        response = admin.post(
            f"/ayarlar/kullanicilar/{B_ID}/yetkiler", {"permission": ["schedule.read_all"]}
        )
        assert response.status_code == 200 and "Yetkiler güncellendi" in response.text
        assert other.get("/danisanlar").status_code == 403

    grants = [
        r["permission"]
        for r in fetch(local_db, "SELECT permission FROM user_permissions WHERE user_id = $1", B_ID)
    ]
    assert grants == ["schedule.read_all"]
    audit = fetch(
        local_db,
        "SELECT actor_id, detail FROM audit_log "
        "WHERE action = 'permissions_set' ORDER BY id DESC LIMIT 1",
    )[0]
    assert audit["actor_id"] == A_ID


def test_admin_cannot_lock_themselves_out(local_db: LocalDb, secrets: dict[str, str]) -> None:
    with Kasa(local_db) as admin:
        admin.login("terapist.a", secrets["a"])
        no_admin = admin.post(
            f"/ayarlar/kullanicilar/{A_ID}/yetkiler", {"permission": ["clients.read"]}
        )
        assert no_admin.status_code == 400
        assert admin.post(f"/ayarlar/kullanicilar/{A_ID}/durum", {"active": "0"}).status_code == 400
        assert admin.post(f"/ayarlar/kullanicilar/{A_ID}/2fa-sifirla").status_code == 400
        assert admin.get("/ayarlar/kullanicilar").status_code == 200


def test_unknown_permission_names_are_ignored(local_db: LocalDb, secrets: dict[str, str]) -> None:
    with Kasa(local_db) as admin:
        admin.login("terapist.a", secrets["a"])
        admin.post(
            f"/ayarlar/kullanicilar/{B_ID}/yetkiler",
            {"permission": ["clients.read", "superuser.everything"]},
        )
    grants = fetch(local_db, "SELECT permission FROM user_permissions WHERE user_id = $1", B_ID)
    assert [r["permission"] for r in grants] == ["clients.read"]


def test_deactivating_a_user_ends_their_session(local_db: LocalDb, secrets: dict[str, str]) -> None:
    with Kasa(local_db) as admin, Kasa(local_db) as other:
        admin.login("terapist.a", secrets["a"])
        other.login("terapist.b", secrets["b"])
        assert admin.post(f"/ayarlar/kullanicilar/{B_ID}/durum", {"active": "0"}).status_code == 200
        assert other.get("/danisanlar").headers["location"] == "/login"
        assert other.submit_password("terapist.b", PASSWORD).status_code == 401

        assert admin.post(f"/ayarlar/kullanicilar/{B_ID}/durum", {"active": "1"}).status_code == 200
        assert other.submit_password("terapist.b", PASSWORD).status_code == 303


def test_second_factor_reset_makes_the_user_enroll_again(
    local_db: LocalDb, secrets: dict[str, str]
) -> None:
    with Kasa(local_db) as admin, Kasa(local_db) as other:
        admin.login("terapist.a", secrets["a"])
        other.login("terapist.b", secrets["b"])
        assert admin.post(f"/ayarlar/kullanicilar/{B_ID}/2fa-sifirla").status_code == 200
        assert other.get("/danisanlar").headers["location"] == "/login"

        again = other.submit_password("terapist.b", PASSWORD)
        assert again.headers["location"] == "/hesap/2fa/kur"
    assert scalar(local_db, "SELECT count(*) FROM recovery_codes WHERE user_id = $1", B_ID) == 0


def test_password_reset_shows_a_one_time_password(
    local_db: LocalDb, secrets: dict[str, str]
) -> None:
    import re

    with Kasa(local_db) as admin:
        admin.login("terapist.a", secrets["a"])
        page = admin.post(f"/ayarlar/kullanicilar/{B_ID}/sifre-sifirla")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        match = re.search(r"geçici şifre: <code>([^<]+)</code>", page.text)
        assert match
        temporary = match.group(1)

    with Kasa(local_db) as other:
        assert other.submit_password("terapist.b", PASSWORD).status_code == 401
        execute(local_db, "DELETE FROM login_attempts")
        result = other.login("terapist.b", secrets["b"], password=temporary)
        assert result.status_code == 303
        forced = other.get("/danisanlar")
        assert forced.status_code == 303 and forced.headers["location"] == "/hesap/sifre"
