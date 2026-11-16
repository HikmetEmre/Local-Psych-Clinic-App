"""Password hashing with bcrypt (brief §11)."""

from __future__ import annotations

import secrets

import bcrypt

MIN_LENGTH = 10
MAX_BYTES = 72  # bcrypt ignores (or refuses) anything beyond 72 bytes
DEFAULT_ROUNDS = 12

_rounds = DEFAULT_ROUNDS


class PasswordPolicyError(ValueError):
    """Raised with a Turkish, user-presentable message."""


def set_rounds(rounds: int) -> None:
    """Lower the cost factor in tests; never call this in production code."""
    global _rounds
    _rounds = rounds


def validate_new_password(password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"Şifre en az {MIN_LENGTH} karakter olmalı.")
    if len(password.encode("utf-8")) > MAX_BYTES:
        raise PasswordPolicyError("Şifre çok uzun (en fazla 72 bayt).")
    if password.strip() != password:
        raise PasswordPolicyError("Şifre boşlukla başlayamaz veya bitemez.")


def hash_password(password: str) -> str:
    validate_new_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode("ascii"))
    except ValueError:
        return False


def dummy_verify(password: str) -> None:
    """Burn the same time as a real check when the username does not exist."""
    verify_password(password[:MAX_BYTES] or "x", _dummy_hash())


_cached_dummy: tuple[int, str] | None = None


def _dummy_hash() -> str:
    global _cached_dummy
    if _cached_dummy is None or _cached_dummy[0] != _rounds:
        value = bcrypt.hashpw(secrets.token_bytes(24), bcrypt.gensalt(rounds=_rounds))
        _cached_dummy = (_rounds, value.decode("ascii"))
    return _cached_dummy[1]


def generate_temporary_password() -> str:
    """A one-time password for a new user; they must change it at first login."""
    return secrets.token_urlsafe(12)
