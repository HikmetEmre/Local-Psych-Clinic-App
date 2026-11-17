from __future__ import annotations

from uuid import UUID

import pytest
from cryptography.fernet import Fernet

from klinik_local.config import Settings
from klinik_local.crypto.fernet import DecryptionError, FieldCipher, generate_key
from klinik_local.danisanlar.service import parse_client_form
from klinik_local.main import ConfigError, create_app
from klinik_local.manage import parse_users_file
from klinik_shared.permissions import Permission

THERAPIST = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a01")


def test_cipher_roundtrip_and_ciphertext_is_not_plaintext() -> None:
    cipher = FieldCipher(generate_key())
    token = cipher.encrypt("Seans notu: çocuk bugün çok konuşkandı.")
    assert b"Seans" not in token and "çocuk".encode() not in token
    assert cipher.decrypt(token) == "Seans notu: çocuk bugün çok konuşkandı."
    assert cipher.encrypt("aynı metin") != cipher.encrypt("aynı metin")  # random IV


def test_optional_values() -> None:
    cipher = FieldCipher(generate_key())
    assert cipher.encrypt_optional(None) is None
    assert cipher.encrypt_optional("") is None
    assert cipher.decrypt_optional(None) is None


def test_wrong_key_cannot_decrypt() -> None:
    token = FieldCipher(generate_key()).encrypt("gizli")
    with pytest.raises(DecryptionError):
        FieldCipher(generate_key()).decrypt(token)
    with pytest.raises(DecryptionError):
        FieldCipher(generate_key()).decrypt(b"not a fernet token")


def test_key_rotation_keeps_old_data_readable() -> None:
    old, new = generate_key(), generate_key()
    token = FieldCipher(old).encrypt("eski kayıt")

    rotated = FieldCipher(f"{new},{old}")
    assert rotated.decrypt(token) == "eski kayıt"
    fresh = rotated.rotate(token)
    # After re-encryption the old key is no longer needed.
    assert FieldCipher(new).decrypt(fresh) == "eski kayıt"
    assert Fernet(new.encode()).decrypt(rotated.encrypt("yeni")) == b"yeni"


@pytest.mark.parametrize("raw", [None, "", " , ", "too-short", "x" * 44])
def test_missing_or_malformed_keys_are_refused(raw: str | None) -> None:
    with pytest.raises(ValueError, match="Fernet key"):
        FieldCipher(raw)


def test_app_refuses_to_start_without_its_configuration() -> None:
    key = generate_key()
    url = "postgresql+asyncpg://kasa_app:x@127.0.0.1:1/kasa"
    with pytest.raises(ConfigError, match="LOCAL_DATABASE_URL"):
        create_app(Settings(database_url=None, fernet_keys=key, auth_fernet_key=key))
    with pytest.raises(ConfigError, match="LOCAL_FERNET_KEYS"):
        create_app(Settings(database_url=url, fernet_keys=None, auth_fernet_key=key))
    with pytest.raises(ConfigError, match="LOCAL_AUTH_FERNET_KEY"):
        create_app(Settings(database_url=url, fernet_keys=key, auth_fernet_key="bad"))


def test_form_parsing_normalises_input() -> None:
    data, errors = parse_client_form(
        {
            "full_name": "  Ayşe   Yılmaz ",
            "phone": " 0532 111 22 33 ",
            "email": " Ayse@Example.ORG ",
            "birth_date": "2015-03-20",
            "assigned_therapist_id": str(THERAPIST),
        },
        {THERAPIST},
    )
    assert errors == {}
    assert data is not None
    assert data.full_name == "Ayşe Yılmaz"
    assert data.phone == "0532 111 22 33"
    assert data.email == "ayse@example.org"
    assert data.birth_date is not None and data.birth_date.year == 2015
    assert data.assigned_therapist_id == THERAPIST


def test_form_parsing_collects_every_error() -> None:
    data, errors = parse_client_form(
        {"full_name": "", "email": "x", "birth_date": "31.12.2015", "assigned_therapist_id": "zzz"},
        {THERAPIST},
    )
    assert data is None
    assert set(errors) == {"full_name", "email", "birth_date", "assigned_therapist_id"}


USERS_TOML = b"""
[[users]]
id = "0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a01"
username = "terapist.a"
display_name = "Terapist A"
sets = ["admin", "therapist"]
"""


def test_users_file_parsing() -> None:
    users = parse_users_file(USERS_TOML)
    assert users[0]["user_id"] == THERAPIST
    assert Permission.ADMIN_USERS in users[0]["permissions"]
    assert Permission.SESSIONS_READ_OTHERS in users[0]["permissions"]  # admin sees all

    with pytest.raises(ValueError, match="at least one"):
        parse_users_file(b"")
    with pytest.raises(ValueError, match="unknown permission set"):
        parse_users_file(USERS_TOML.replace(b'"admin", "therapist"', b'"root"'))
    with pytest.raises(ValueError, match="duplicate user id"):
        parse_users_file(USERS_TOML + USERS_TOML)
