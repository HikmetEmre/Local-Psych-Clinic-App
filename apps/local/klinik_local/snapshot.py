"""Daily encrypted snapshot for reading from home (replaces remote access to the office).

Every day at 17:00 the jobs service writes one file per therapist and copies it to the
clinic's Google Drive folder. The file is a single HTML page holding AES-256-GCM
ciphertext; it decrypts in the browser after the therapist types their snapshot
passphrase. Google only ever stores ciphertext.

Contents, per therapist: the clinic schedule for the next 14 days (with names), their
own clients' contact details, and their own sessions of the last 90 days with notes and
homework. Never another therapist's notes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from klinik_local.crypto.fernet import FieldCipher
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.timeutil import format_datetime_tr, now_utc, to_istanbul

ITERATIONS = 310_000
MIN_PASSPHRASE = 10
AAD = b"nil-psikoloji-ozet-v1"
SCHEDULE_DAYS = 14
HISTORY_DAYS = 90
ASSETS = Path(__file__).parent / "snapshot_assets"


class PassphraseError(ValueError):
    pass


# ---------------------------------------------------------------- keys


def derive_key(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, 32)


def new_key_record(passphrase: str, cipher: FieldCipher) -> dict[str, Any]:
    """What to store for a newly chosen passphrase (never the passphrase itself)."""
    if len(passphrase) < MIN_PASSPHRASE:
        raise PassphraseError(f"Özet şifresi en az {MIN_PASSPHRASE} karakter olmalı.")
    salt = secrets.token_bytes(16)
    key = derive_key(passphrase, salt)
    return {
        "salt": salt,
        "iterations": ITERATIONS,
        "key_enc": cipher.encrypt(base64.b64encode(key).decode("ascii")),
    }


def encrypt_payload(key: bytes, payload: dict[str, Any]) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return nonce, AESGCM(key).encrypt(nonce, data, AAD)


def decrypt_payload(key: bytes, nonce: bytes, ciphertext: bytes) -> dict[str, Any]:
    """Python mirror of what the page does in the browser (used by tests)."""
    result: dict[str, Any] = json.loads(AESGCM(key).decrypt(nonce, ciphertext, AAD))
    return result


# ---------------------------------------------------------------- content


@dataclass(frozen=True)
class Recipient:
    user_id: UUID
    username: str
    display_name: str
    key: bytes
    salt: bytes
    iterations: int


async def recipients(conn: asyncpg.Connection, cipher: FieldCipher) -> list[Recipient]:
    rows = await conn.fetch(
        "SELECT u.id, u.username, u.display_name, k.salt, k.iterations, k.key_enc "
        "FROM snapshot_keys k JOIN users u ON u.id = k.user_id WHERE u.is_active "
        "ORDER BY u.username"
    )
    return [
        Recipient(
            r["id"],
            r["username"],
            r["display_name"],
            base64.b64decode(cipher.decrypt(r["key_enc"])),
            r["salt"],
            r["iterations"],
        )
        for r in rows
    ]


async def build_payload(
    conn: asyncpg.Connection, cipher: FieldCipher, user: Recipient, now: datetime
) -> dict[str, Any]:
    names = {
        r["client_code"]: (r["id"], cipher.decrypt(r["full_name_encrypted"]))
        for r in await conn.fetch(
            "SELECT id, client_code, full_name_encrypted FROM clients WHERE deleted_at IS NULL"
        )
    }
    schedule = [
        {
            "when": format_datetime_tr(r["starts_at"]),
            "day": to_istanbul(r["starts_at"]).date().isoformat(),
            "minutes": r["duration_min"],
            "client": names.get(r["client_code"], (None, r["client_code"]))[1],
            "code": r["client_code"],
            "therapist": r["display_name"] or "—",
            "room": r["room"] or "—",
            "status": STATUS_LABELS.get(r["status"], r["status"]),
            "mine": r["therapist_id"] == user.user_id,
        }
        for r in await conn.fetch(
            "SELECT m.*, u.display_name, rm.internal_label AS room FROM appointments_mirror m "
            "LEFT JOIN users u ON u.id = m.therapist_id "
            "LEFT JOIN rooms_mirror rm ON rm.id = m.room_id "
            "WHERE m.deleted_at IS NULL AND m.status <> 'cancelled' "
            "AND m.starts_at >= $1 AND m.starts_at < $2 ORDER BY m.starts_at",
            now - timedelta(hours=2),
            now + timedelta(days=SCHEDULE_DAYS),
        )
    ]
    clients = [
        {
            "code": r["client_code"],
            "name": cipher.decrypt(r["full_name_encrypted"]),
            "phone": cipher.decrypt_optional(r["phone_encrypted"]) or "",
            "email": cipher.decrypt_optional(r["email_encrypted"]) or "",
            "birth": r["birth_date"].strftime("%d.%m.%Y") if r["birth_date"] else "",
        }
        for r in await conn.fetch(
            "SELECT DISTINCT c.* FROM clients c WHERE c.deleted_at IS NULL AND c.is_active AND "
            "(c.assigned_therapist_id = $1 OR EXISTS (SELECT 1 FROM therapy_sessions s "
            " WHERE s.client_id = c.id AND s.therapist_id = $1 AND s.deleted_at IS NULL)) "
            "ORDER BY c.client_code",
            user.user_id,
        )
    ]
    sessions = [
        {
            "when": format_datetime_tr(r["session_date"]),
            "code": r["client_code"],
            "client": cipher.decrypt(r["full_name_encrypted"]),
            "number": r["session_number"],
            "format": r["format_label"],
            "methods": r["methods"] or "",
            "status": STATUS_LABELS.get(r["status"], r["status"]),
            "notes": cipher.decrypt_optional(r["notes_encrypted"]) or "",
            "homework": cipher.decrypt_optional(r["homework_encrypted"]) or "",
        }
        for r in await conn.fetch(
            "SELECT s.*, c.client_code, c.full_name_encrypted, f.label_tr AS format_label, "
            "(SELECT string_agg(m.label_tr, ', ' ORDER BY sm.is_primary DESC, m.label_tr) "
            " FROM session_methods sm JOIN therapy_methods m ON m.id = sm.therapy_method_id "
            " WHERE sm.session_id = s.id) AS methods "
            "FROM therapy_sessions s JOIN clients c ON c.id = s.client_id "
            "JOIN therapy_methods f ON f.id = s.format_method_id "
            "WHERE s.therapist_id = $1 AND s.deleted_at IS NULL AND s.session_date >= $2 "
            "ORDER BY s.session_date DESC",
            user.user_id,
            now - timedelta(days=HISTORY_DAYS),
        )
    ]
    return {
        "generated": format_datetime_tr(now),
        "therapist": user.display_name,
        "schedule": schedule,
        "clients": clients,
        "sessions": sessions,
    }


# ---------------------------------------------------------------- the page


def render_page(user: Recipient, nonce: bytes, ciphertext: bytes, generated: datetime) -> str:
    template = (ASSETS / "ozet.html").read_text(encoding="utf-8")
    noble = (ASSETS / "noble.min.js").read_text(encoding="utf-8")
    params = {
        "salt": base64.b64encode(user.salt).decode(),
        "iterations": user.iterations,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "aad": AAD.decode(),
    }
    # Everything sensitive is inside the ciphertext; these fields are safe in the clear.
    return (
        template.replace("/*__PARAMS__*/null", json.dumps(params))
        .replace("/*__NOBLE__*/", noble)
        .replace("__LOGO__", _logo())
        .replace("__THERAPIST__", _html(user.display_name))
        .replace("__GENERATED__", format_datetime_tr(generated))
    )


def _logo() -> str:
    from klinik_shared.web import UI_DIR

    raw = (UI_DIR / "static" / "img" / "mark-96.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _html(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


@dataclass(frozen=True)
class SnapshotResult:
    files: list[Path]
    skipped_users: list[str]


async def write_snapshots(
    dsn: str, fernet_keys: str | None, out_dir: Path, *, now: datetime | None = None, keep: int = 7
) -> SnapshotResult:
    """One file per therapist who has set a snapshot passphrase."""
    now = now or now_utc()
    cipher = FieldCipher(fernet_keys)
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = await asyncpg.connect(dsn)
    files: list[Path] = []
    try:
        everyone = {
            r["username"]
            for r in await conn.fetch(
                "SELECT u.username FROM users u JOIN user_permissions p ON p.user_id = u.id "
                "WHERE u.is_active AND p.permission = 'sessions.read_own'"
            )
        }
        users = await recipients(conn, cipher)
        for user in users:
            payload = await build_payload(conn, cipher, user, now)
            nonce, ciphertext = encrypt_payload(user.key, payload)
            stamp = to_istanbul(now).strftime("%Y-%m-%d")
            target = out_dir / f"nil-ozet-{user.username}-{stamp}.html"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(render_page(user, nonce, ciphertext, now), encoding="utf-8")
            os.replace(tmp, target)
            files.append(target)
            await conn.execute(
                "INSERT INTO audit_log (actor_kind, action, entity_type, entity_id, detail) "
                "VALUES ('system', 'snapshot_created', 'user', $1, $2::jsonb)",
                str(user.user_id),
                json.dumps(
                    {"sessions": len(payload["sessions"]), "clients": len(payload["clients"])}
                ),
            )
            old = sorted(out_dir.glob(f"nil-ozet-{user.username}-*.html"), reverse=True)
            for stale in old[keep:]:
                stale.unlink(missing_ok=True)
    finally:
        await conn.close()
    return SnapshotResult(files, sorted(everyone - {u.username for u in users}))
