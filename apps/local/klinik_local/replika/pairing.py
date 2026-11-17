"""The pairing file that turns a fresh installation into the owner's laptop copy.

Made at the office (Ayarlar → Dizüstü kopya), carried on a USB stick, read once by the
laptop's installer. It holds the office's address and certificate, the laptop's token,
and the encryption keys (the copy must read the same encrypted notes), so it is
encrypted with a passphrase the admin chooses (AES-256-GCM, key from PBKDF2-SHA256).
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from klinik_local.snapshot import derive_key

ITERATIONS = 600_000
AAD = b"nil-klinik-eslestirme-v1"
MIN_PASSPHRASE = 10
KIND = "nil-klinik-eslestirme"


class PairingError(ValueError):
    pass


def seal(payload: dict[str, Any], passphrase: str) -> bytes:
    if len(passphrase) < MIN_PASSPHRASE:
        raise PairingError(f"Parola en az {MIN_PASSPHRASE} karakter olmalı.")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = derive_key(passphrase, salt, ITERATIONS)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sealed = AESGCM(key).encrypt(nonce, data, AAD)
    envelope = {
        "kind": KIND,
        "v": 1,
        "iterations": ITERATIONS,
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "data": base64.b64encode(sealed).decode(),
    }
    return json.dumps(envelope, indent=1).encode("utf-8")


def open_sealed(raw: bytes, passphrase: str) -> dict[str, Any]:
    try:
        envelope = json.loads(raw.decode("utf-8-sig"))
        if envelope.get("kind") != KIND:
            raise PairingError("Bu dosya bir eşleştirme dosyası değil.")
        key = derive_key(
            passphrase, base64.b64decode(envelope["salt"]), int(envelope["iterations"])
        )
        data = AESGCM(key).decrypt(
            base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["data"]), AAD
        )
    except InvalidTag:
        raise PairingError("Parola yanlış ya da dosya bozuk.") from None
    except (ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, PairingError):
            raise
        raise PairingError("Eşleştirme dosyası okunamadı.") from None
    result: dict[str, Any] = json.loads(data)
    return result


def env_lines(payload: dict[str, Any]) -> dict[str, str]:
    """The laptop's klinik.env values that come from the pairing file."""
    return {
        "LOCAL_MODE": "replica",
        "LOCAL_OFFICE_URL": str(payload["office_url"]),
        "LOCAL_REPLICA_TOKEN": str(payload["token"]),
        "LOCAL_FERNET_KEYS": str(payload["fernet_keys"]),
        "LOCAL_AUTH_FERNET_KEY": str(payload["auth_fernet_key"]),
    }
