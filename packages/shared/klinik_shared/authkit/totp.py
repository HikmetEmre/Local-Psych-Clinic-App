"""TOTP second factor and one-time recovery codes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime

import pyotp

STEP_SECONDS = 30
RECOVERY_CODE_COUNT = 10


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, username: str, issuer: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code if not ch.isspace() and ch != "-")


def matching_step(secret: str, code: str, at: datetime, window: int = 1) -> int | None:
    """Time step whose code equals ``code``, searching ``window`` steps either side.

    The caller must reject a step that is not greater than the last accepted one,
    which is what prevents a code from being replayed.
    """
    code = normalize_code(code)
    if len(code) != 6 or not code.isdecimal() or not code.isascii():
        return None
    totp = pyotp.TOTP(secret)
    current = int(at.timestamp()) // STEP_SECONDS
    for step in (current, *(current + d for i in range(1, window + 1) for d in (-i, i))):
        if hmac.compare_digest(totp.at(step * STEP_SECONDS), code):
            return step
    return None


def code_at(secret: str, at: datetime) -> str:
    """The valid code at a moment (used by tests and never by the web app)."""
    return pyotp.TOTP(secret).at(int(at.timestamp()))


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(5)
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> bytes:
    return hashlib.sha256(normalize_code(code).lower().encode("ascii", "ignore")).digest()
