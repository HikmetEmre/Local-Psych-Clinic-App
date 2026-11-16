"""Field encryption for clinical text and client contact details (brief §11).

This is the only module that encrypts or decrypts. Keys come from the environment
(an env file outside the repo), never from the database. The first key encrypts;
all keys decrypt, which is what makes rotation possible.

LOSING THE KEY LOSES THE DATA: backups contain ciphertext only. Keep an offline copy.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class DecryptionError(Exception):
    """The stored value cannot be decrypted with any configured key."""


def parse_keys(raw: str | None) -> list[Fernet]:
    keys = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not keys:
        raise ValueError("no Fernet key configured")
    try:
        return [Fernet(key.encode("ascii")) for key in keys]
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("invalid Fernet key (expected 32 url-safe base64 bytes)") from exc


class FieldCipher:
    def __init__(self, raw_keys: str | None) -> None:
        self._fernet = MultiFernet(parse_keys(raw_keys))

    @property
    def multi(self) -> MultiFernet:
        return self._fernet

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def encrypt_optional(self, plaintext: str | None) -> bytes | None:
        return self.encrypt(plaintext) if plaintext else None

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(token)).decode("utf-8")
        except InvalidToken as exc:
            raise DecryptionError("value cannot be decrypted with the configured keys") from exc

    def decrypt_optional(self, token: bytes | None) -> str | None:
        return self.decrypt(token) if token is not None else None

    def rotate(self, token: bytes) -> bytes:
        """Re-encrypt an old value under the current primary key."""
        return self._fernet.rotate(bytes(token))


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")
