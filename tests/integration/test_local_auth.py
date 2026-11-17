"""Login, second factor, sessions and CSRF on the local tier (real PostgreSQL)."""

from __future__ import annotations

import pytest
from support.local import (
    PASSWORD,
    Kasa,
    LocalDb,
    add_therapists,
    ago,
    csrf_in,
    execute,
    fetch,
    scalar,
)

from klinik_shared.authkit import totp
from klinik_shared.timeutil import now_utc

pytestmark = pytest.mark.integration


@pytest.fixture
def onboarded(local_db: LocalDb) -> dict[str, str]:
    """Both therapists exist and have finished first login. Returns their TOTP secrets."""
    temporary = add_therapists(local_db)
    secrets = {}
    for key, username in (("a", "terapist.a"), ("b", "terapist.b")):
        with Kasa(local_db) as kasa:
            secrets[key], _ = kasa.onboard(username, temporary[key])
    return secrets


def audit_actions(db: LocalDb) -> list[str]:
    return [r["action"] for r in fetch(db, "SELECT action FROM audit_log ORDER BY id")]


def test_first_login_enrolls_second_factor_and_forces_a_new_password(local_db: LocalDb) -> None:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as kasa:
        kasa.onboard("terapist.a", temporary["a"])
        home = kasa.get("/")
        assert home.status_code == 200
        assert "Terapist A" in home.text

    row = fetch(
        local_db,
        "SELECT must_change_password, totp_confirmed_at, totp_secret_enc FROM users "
        "WHERE username = 'terapist.a'",
    )[0]
    assert row["must_change_password"] is False
    assert row["totp_confirmed_at"] is not None
    actions = audit_actions(local_db)
    assert {"user_created", "totp_enrolled", "login_ok", "password_changed"} <= set(actions)


def test_totp_secret_is_encrypted_at_rest(local_db: LocalDb) -> None:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as kasa:
        secret, _ = kasa.onboard("terapist.a", temporary["a"])
    stored = scalar(local_db, "SELECT totp_secret_enc FROM users WHERE username = 'terapist.a'")
    assert secret.encode() not in bytes(stored)


def test_login_needs_password_and_code(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        assert kasa.get("/").headers["location"] == "/login"
        result = kasa.login("terapist.a", onboarded["a"])
        assert result.status_code == 303 and result.headers["location"] == "/"
        assert kasa.get("/danisanlar").status_code == 200


def test_username_is_case_insensitive(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        assert kasa.submit_password("  Terapist.A ", PASSWORD).status_code == 303


def test_wrong_password_and_unknown_user_are_indistinguishable(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        wrong = kasa.submit_password("terapist.a", "definitely-wrong-pw")
        unknown = kasa.submit_password("nobody.here", "definitely-wrong-pw")
    assert wrong.status_code == unknown.status_code == 401
    assert "Kullanıcı adı veya şifre hatalı." in wrong.text
    assert "Kullanıcı adı veya şifre hatalı." in unknown.text
    assert kasa.session_cookie() is None


def test_typed_usernames_are_not_stored_in_clear(local_db: LocalDb) -> None:
    """People type passwords into the username box; the attempt log must not keep them."""
    with Kasa(local_db) as kasa:
        kasa.submit_password("MySecretPassword123", "x" * 12)
    dump = scalar(local_db, "SELECT string_agg(login_attempts::text, ' ') FROM login_attempts")
    assert "mysecretpassword123" not in dump.lower()
    assert (
        "mysecretpassword123"
        not in str(
            scalar(local_db, "SELECT string_agg(audit_log::text, ' ') FROM audit_log")
        ).lower()
    )


def test_account_locks_after_five_failures(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        for _ in range(5):
            assert kasa.submit_password("terapist.a", "wrong-password-1").status_code == 401
        locked = kasa.submit_password("terapist.a", PASSWORD)
        assert locked.status_code == 401
        assert "Çok fazla hatalı deneme" in locked.text
        # The other therapist is unaffected.
        assert kasa.submit_password("terapist.b", PASSWORD).status_code == 303
    assert "login_locked" in audit_actions(local_db)

    # After the window has passed the account opens again.
    execute(local_db, "UPDATE login_attempts SET ts = $1", ago(minutes=16))
    with Kasa(local_db) as kasa:
        assert kasa.submit_password("terapist.a", PASSWORD).status_code == 303


def test_wrong_code_is_rejected(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        result = kasa.login("terapist.a", onboarded["a"], code="000000")
        assert result.status_code == 401
        assert kasa.get("/danisanlar").headers["location"] == "/login"


def test_a_code_cannot_be_replayed(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    code = totp.code_at(onboarded["a"], now_utc())
    with Kasa(local_db) as first:
        assert first.login("terapist.a", onboarded["a"], code=code).status_code == 303
    with Kasa(local_db) as second:
        replay = second.login(
            "terapist.a", onboarded["a"], code=code, allow_reuse_of_time_step=False
        )
        assert replay.status_code == 401


def test_password_alone_does_not_open_any_page(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        assert kasa.submit_password("terapist.a", PASSWORD).status_code == 303
        for path in ("/", "/danisanlar", "/ayarlar/kullanicilar", "/hesap/sifre"):
            response = kasa.get(path)
            assert response.status_code == 303 and response.headers["location"] == "/login", path


def test_recovery_code_works_exactly_once(local_db: LocalDb) -> None:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as kasa:
        secret, codes = kasa.onboard("terapist.a", temporary["a"])
    with Kasa(local_db) as kasa:
        assert kasa.login("terapist.a", secret, code=codes[0]).status_code == 303
    with Kasa(local_db) as kasa:
        assert kasa.login("terapist.a", secret, code=codes[0]).status_code == 401
    assert "recovery_code_used" in audit_actions(local_db)
    stored = scalar(
        local_db, "SELECT string_agg(encode(code_hash, 'hex'), ' ') FROM recovery_codes"
    )
    assert codes[1].replace("-", "") not in stored


def test_session_expires_after_30_idle_minutes(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", onboarded["a"])
        execute(local_db, "UPDATE auth_sessions SET last_seen_at = $1", ago(minutes=29))
        assert kasa.get("/danisanlar").status_code == 200

        execute(local_db, "UPDATE auth_sessions SET last_seen_at = $1", ago(minutes=31))
        expired = kasa.get("/danisanlar")
        assert expired.status_code == 303 and expired.headers["location"] == "/login"
        # The expired session is revoked in the database, not just ignored.
        assert (
            scalar(local_db, "SELECT count(*) FROM auth_sessions WHERE revoked_at IS NOT NULL") == 1
        )


def test_session_has_an_absolute_lifetime(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", onboarded["a"])
        execute(local_db, "UPDATE auth_sessions SET created_at = $1", ago(hours=13))
        assert kasa.get("/danisanlar").headers["location"] == "/login"


def test_logout_revokes_the_session_server_side(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", onboarded["a"])
        stolen = kasa.session_cookie()
        assert stolen
        assert kasa.post("/logout").status_code == 303
        assert kasa.session_cookie() is None
        # Replaying the old cookie value gets nowhere.
        replayed = kasa.get("/danisanlar", headers={"cookie": f"kasa_session={stolen}"})
        assert replayed.status_code == 303 and replayed.headers["location"] == "/login"
    assert "logout" in audit_actions(local_db)


def test_state_changing_requests_need_the_csrf_token(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", onboarded["a"])
        form = {"full_name": "Deneme Danışan"}
        assert kasa.post("/danisanlar", form, csrf="").status_code == 403
        assert kasa.post("/danisanlar", form, csrf="x" * 43).status_code == 403
        assert kasa.post("/logout", csrf="wrong").status_code == 403
        assert scalar(local_db, "SELECT count(*) FROM clients") == 0
        assert kasa.post("/danisanlar", form).status_code == 303


def test_login_form_needs_its_own_csrf_token(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        kasa.get("/login")
        response = kasa.post(
            "/login", {"username": "terapist.a", "password": PASSWORD}, csrf="forged"
        )
        assert response.status_code == 403
        assert kasa.session_cookie() is None


def test_cookie_flags_and_security_headers(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        response = kasa.submit_password("terapist.a", PASSWORD)
        cookie = response.headers["set-cookie"].lower()
        assert "kasa_session=" in cookie
        for flag in ("httponly", "secure", "samesite=lax", "path=/"):
            assert flag in cookie
        assert "max-age" not in cookie and "expires" not in cookie

        page = kasa.get("/login")
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "'unsafe-inline'" not in page.headers["content-security-policy"]
        assert page.headers["x-content-type-options"] == "nosniff"
        assert page.headers["x-frame-options"] == "DENY"
        assert page.headers["cache-control"] == "no-store"


def test_session_token_is_stored_hashed(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", onboarded["a"])
        token = kasa.session_cookie()
    assert token
    stored = [bytes(r["id_hash"]) for r in fetch(local_db, "SELECT id_hash FROM auth_sessions")]
    assert all(token.encode() not in value for value in stored)
    assert all(len(value) == 32 for value in stored)


def test_login_rotates_the_session_token(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    """The cookie issued after the password step is not the one valid after the code."""
    with Kasa(local_db) as kasa:
        kasa.submit_password("terapist.a", PASSWORD)
        pending = kasa.session_cookie()
        execute(local_db, "UPDATE users SET totp_last_step = NULL")
        assert kasa.submit_code(totp.code_at(onboarded["a"], now_utc())).status_code == 303
        assert kasa.session_cookie() != pending


def test_changing_password_signs_out_other_devices(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    new_password = "yepyeni-sifre-2026"
    with Kasa(local_db) as laptop, Kasa(local_db) as phone:
        laptop.login("terapist.a", onboarded["a"])
        phone.login("terapist.a", onboarded["a"])

        wrong = laptop.post(
            "/hesap/sifre",
            {
                "current_password": "not-my-password",
                "new_password": new_password,
                "new_password_repeat": new_password,
            },
        )
        assert wrong.status_code == 400 and "Mevcut şifre hatalı" in wrong.text

        short = laptop.post(
            "/hesap/sifre",
            {"current_password": PASSWORD, "new_password": "kisa", "new_password_repeat": "kisa"},
        )
        assert short.status_code == 400 and "en az 10 karakter" in short.text

        changed = laptop.post(
            "/hesap/sifre",
            {
                "current_password": PASSWORD,
                "new_password": new_password,
                "new_password_repeat": new_password,
            },
        )
        assert changed.status_code == 200 and "Şifreniz değiştirildi" in changed.text
        assert laptop.get("/danisanlar").status_code == 200
        assert phone.get("/danisanlar").headers["location"] == "/login"

    with Kasa(local_db) as kasa:
        assert kasa.submit_password("terapist.a", PASSWORD).status_code == 401
        assert kasa.submit_password("terapist.a", new_password).status_code == 303


def test_deactivated_user_cannot_log_in(local_db: LocalDb, onboarded: dict[str, str]) -> None:
    execute(local_db, "UPDATE users SET is_active = false WHERE username = 'terapist.b'")
    with Kasa(local_db) as kasa:
        assert kasa.submit_password("terapist.b", PASSWORD).status_code == 401


def test_logged_in_user_is_sent_away_from_the_login_page(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        kasa.login("terapist.a", onboarded["a"])
        response = kasa.get("/login")
        assert response.status_code == 303 and response.headers["location"] == "/"
        assert csrf_in(kasa.get("/").text) == kasa.csrf


def test_one_address_trying_many_accounts_is_blocked(
    local_db: LocalDb, onboarded: dict[str, str]
) -> None:
    with Kasa(local_db) as kasa:
        for n in range(20):
            assert kasa.submit_password(f"guess.{n}", "wrong-password-1").status_code == 401
        # Correct credentials from the same address are refused for now...
        blocked = kasa.submit_password("terapist.a", PASSWORD)
        assert blocked.status_code == 401 and "Çok fazla hatalı deneme" in blocked.text
    # ...and work again once the window has passed.
    execute(local_db, "UPDATE login_attempts SET ts = $1", ago(minutes=16))
    with Kasa(local_db) as kasa:
        assert kasa.submit_password("terapist.a", PASSWORD).status_code == 303
