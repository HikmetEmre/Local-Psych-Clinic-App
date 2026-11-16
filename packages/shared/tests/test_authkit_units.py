from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from klinik_shared.audit import AuditDetailError, write_audit
from klinik_shared.authkit import passwords, totp
from klinik_shared.textutil import fold_tr


@pytest.fixture(autouse=True)
def _fast_bcrypt() -> None:
    passwords.set_rounds(4)


def test_password_hash_roundtrip() -> None:
    hashed = passwords.hash_password("dogru-at-pil-zimba")
    assert hashed.startswith("$2")
    assert "dogru" not in hashed
    assert passwords.verify_password("dogru-at-pil-zimba", hashed)
    assert not passwords.verify_password("dogru-at-pil-zimbA", hashed)
    assert passwords.hash_password("dogru-at-pil-zimba") != hashed  # salted


@pytest.mark.parametrize(
    "bad", ["kisa", "x" * 9, "ş" * 40, " bosluklu-sifre-1", "bosluklu-sifre-1 "]
)
def test_password_policy(bad: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.hash_password(bad)


def test_verify_never_raises_on_bad_input() -> None:
    hashed = passwords.hash_password("dogru-at-pil-zimba")
    assert not passwords.verify_password("x" * 100, hashed)  # beyond the 72-byte bcrypt limit
    assert not passwords.verify_password("anything-at-all", "not-a-bcrypt-hash")
    assert not passwords.verify_password("", hashed)
    passwords.dummy_verify("")
    passwords.dummy_verify("x" * 500)


def test_temporary_password_satisfies_the_policy() -> None:
    passwords.validate_new_password(passwords.generate_temporary_password())


NOW = datetime(2026, 11, 17, 10, 0, 15, tzinfo=UTC)
STEP = timedelta(seconds=30)


def test_totp_accepts_current_previous_and_next_step() -> None:
    secret = totp.new_secret()
    current = int(NOW.timestamp()) // 30
    assert totp.matching_step(secret, totp.code_at(secret, NOW), NOW) == current
    assert totp.matching_step(secret, totp.code_at(secret, NOW - STEP), NOW) == current - 1
    assert totp.matching_step(secret, totp.code_at(secret, NOW + STEP), NOW) == current + 1


def test_totp_rejects_codes_outside_the_window() -> None:
    secret = totp.new_secret()
    old = totp.code_at(secret, NOW - 3 * STEP)
    assert totp.matching_step(secret, old, NOW) is None
    assert totp.matching_step(totp.new_secret(), totp.code_at(secret, NOW), NOW) is None


@pytest.mark.parametrize(
    "junk", ["", "12345", "1234567", "abcdef", "12 34", "１２３４５６", "-1e100"]
)
def test_totp_rejects_malformed_codes(junk: str) -> None:
    assert totp.matching_step(totp.new_secret(), junk, NOW) is None


def test_totp_tolerates_spaces_typed_by_the_user() -> None:
    secret = totp.new_secret()
    code = totp.code_at(secret, NOW)
    assert totp.matching_step(secret, f" {code[:3]} {code[3:]} ", NOW) is not None


def test_recovery_codes_are_unique_and_normalised() -> None:
    codes = totp.generate_recovery_codes()
    assert len(set(codes)) == 10
    code = codes[0]
    assert totp.hash_recovery_code(code) == totp.hash_recovery_code(code.replace("-", "").upper())
    assert totp.hash_recovery_code(code) == totp.hash_recovery_code(f"  {code} ")
    assert totp.hash_recovery_code(code) != totp.hash_recovery_code(codes[1])


def test_provisioning_uri_names_the_clinic() -> None:
    uri = totp.provisioning_uri(totp.new_secret(), "terapist.a", "Nil Psikoloji Kasa")
    assert uri.startswith("otpauth://totp/")
    assert "Nil%20Psikoloji%20Kasa" in uri and "terapist.a" in uri


@pytest.mark.parametrize(
    "detail",
    [
        {"note": "Danışan bugün çok kaygılıydı ve uzun uzun anlattı; " * 3},
        {"changed": ["full_name", "x" * 65]},
        {"nested": {"text": "y" * 65}},
        {"when": datetime(2026, 1, 1, tzinfo=UTC)},
    ],
)
async def test_audit_refuses_free_text_and_odd_types(detail: Any) -> None:
    # Validation happens before the database is touched, so no connection is needed.
    with pytest.raises(AuditDetailError):
        await write_audit(None, None, action="x", actor_id=None, detail=detail)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("needle", "haystack"),
    [
        ("isil", "Işıl Çınar"),
        ("IŞIL", "Işıl Çınar"),
        ("cinar", "Işıl Çınar"),
        ("ipek", "İpek Şahin"),
        ("SAHIN", "İpek Şahin"),
        ("gokce", "Gökçe Öğüt"),
        ("ogut", "Gökçe Öğüt"),
        ("dn-2026", "DN-2026-0042"),
    ],
)
def test_fold_tr_matches_regardless_of_case_and_diacritics(needle: str, haystack: str) -> None:
    assert fold_tr(needle) in fold_tr(haystack)
