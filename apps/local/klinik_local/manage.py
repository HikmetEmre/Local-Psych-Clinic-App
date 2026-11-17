"""Operator commands for the local tier.

    python -m klinik_local.manage set-role-passwords
    python -m klinik_local.manage bootstrap-users users.toml     (or "-" for stdin)
    python -m klinik_local.manage generate-keys
    python -m klinik_local.manage verify-restore      (RESTORE_DSN = a restored copy)

The first two connect with LOCAL_MIGRATION_DATABASE_URL (the owner role).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from cryptography.fernet import MultiFernet
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from klinik_local import tables as t
from klinik_local.crypto.fernet import generate_key, parse_keys
from klinik_local.restore_check import verify_restore
from klinik_shared.authkit.admin import bootstrap_users, parse_users_file, set_role_passwords
from klinik_shared.authkit.service import AuthStore

__all__ = ["bootstrap_users", "main", "parse_users_file", "set_role_passwords"]

ROLE_PASSWORD_ENV = {
    "kasa_app": "LOCAL_APP_DB_PASSWORD",
    "kasa_sync": "LOCAL_SYNC_DB_PASSWORD",
    "kasa_backup": "LOCAL_BACKUP_DB_PASSWORD",
}


def _owner_engine() -> AsyncEngine:
    url = os.environ.get("LOCAL_MIGRATION_DATABASE_URL")
    if not url:
        raise SystemExit("LOCAL_MIGRATION_DATABASE_URL is not set")
    return create_async_engine(url)


async def _main(args: argparse.Namespace) -> int:
    if args.command == "generate-keys":
        print(f"LOCAL_FERNET_KEYS={generate_key()}")
        print(f"LOCAL_AUTH_FERNET_KEY={generate_key()}")
        print("# Keep an OFFLINE copy of LOCAL_FERNET_KEYS. Without it, notes cannot be recovered.")
        return 0

    if args.command == "generate-cert":
        from klinik_local import tls

        names = args.name or tls.default_names()
        cert, _ = tls.write(Path(args.out), names)
        print(f"certificate: {cert}")
        print(f"names: {', '.join(names)}")
        print(f"SHA-256: {tls.fingerprint(cert.read_bytes())}")
        return 0

    if args.command == "import-pairing":
        from klinik_local.replika.pairing import PairingError, env_lines, open_sealed

        passphrase = os.environ.get("KLINIK_PAIRING_PASSPHRASE", "")
        try:
            payload = open_sealed(await asyncio.to_thread(Path(args.file).read_bytes), passphrase)
        except PairingError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 3
        values = env_lines(payload)
        if payload.get("office_cert"):
            cert = Path(args.cert_out)
            await asyncio.to_thread(cert.write_text, payload["office_cert"], encoding="ascii")
            values["LOCAL_OFFICE_CERT"] = str(cert)
        for key, value in values.items():
            print(f"{key}={value}")
        return 0

    if args.command == "replica-sync":
        from klinik_local.config import get_settings
        from klinik_local.replika.laptop import ReplicaSync

        settings = get_settings()
        if not (settings.sync_database_url and settings.office_url and settings.replica_token):
            raise SystemExit("laptop settings are incomplete (LOCAL_MODE=replica?)")
        engine = create_async_engine(settings.sync_database_url)
        try:
            pinned = (
                await asyncio.to_thread(Path(settings.office_cert).read_text, "ascii")
                if settings.office_cert
                else None
            )
            sync = ReplicaSync(engine, settings.office_url, settings.replica_token, pinned)
            await sync.mark_replica()
            status = await sync.run_once()
        finally:
            await engine.dispose()
        if status.last_error:
            print(f"ERROR {status.last_error}")
            return 4
        print(f"OK sent={status.sent} received={status.received}")
        return 0

    if args.command == "verify-restore":
        dsn = os.environ.get("RESTORE_DSN")
        if not dsn:
            raise SystemExit("RESTORE_DSN is not set")
        report = await verify_restore(dsn, os.environ.get("LOCAL_FERNET_KEYS"))
        for line in report.lines():
            print(line)
        return 0 if report.ok else 1

    engine = _owner_engine()
    try:
        if args.command == "set-role-passwords":
            passwords = {
                role: os.environ[env]
                for role, env in ROLE_PASSWORD_ENV.items()
                if os.environ.get(env)
            }
            if "kasa_app" not in passwords:
                raise SystemExit("LOCAL_APP_DB_PASSWORD is not set")
            for role in await set_role_passwords(engine, passwords):
                print(f"password set: {role}")
            return 0

        if args.command == "create-user":
            from uuid import uuid4

            from klinik_shared.authkit.admin import USERNAME_RE
            from klinik_shared.permissions import PERMISSION_SETS

            username = args.username.strip().lower()
            if not USERNAME_RE.fullmatch(username):
                raise SystemExit(f"invalid username: {username!r}")
            store = AuthStore(
                t.auth,
                t.audit_log,
                MultiFernet(parse_keys(os.environ.get("LOCAL_AUTH_FERNET_KEY"))),
            )
            user = {
                "user_id": uuid4(),
                "username": username,
                "display_name": " ".join(args.name.split()),
                "permissions": PERMISSION_SETS[args.role],
            }
            async with engine.begin() as conn:
                taken = (
                    await conn.execute(t.users.select().where(t.users.c.username == username))
                ).first()
            if taken is not None:
                print(f"exists {username}")
                return 0
            created = await bootstrap_users(engine, store, [user])
            print(f"created {username}  temporary password: {created[username]}")
            return 0

        if args.command == "bootstrap-users":
            raw = (
                sys.stdin.buffer.read()
                if args.file == "-"
                else await asyncio.to_thread(Path(args.file).read_bytes)
            )
            store = AuthStore(
                t.auth,
                t.audit_log,
                MultiFernet(parse_keys(os.environ.get("LOCAL_AUTH_FERNET_KEY"))),
            )
            created = await bootstrap_users(engine, store, parse_users_file(raw))
            if not created:
                print("all users already exist; nothing changed")
            for username, temporary in created.items():
                print(f"created {username}  temporary password: {temporary}")
            if created:
                print("Each user must change the password and set up 2FA at first login.")
            return 0
    finally:
        await engine.dispose()
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(prog="klinik_local.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "set-role-passwords", help="set DB passwords of kasa_app / kasa_sync / kasa_backup"
    )
    bootstrap = sub.add_parser("bootstrap-users", help="create users from a TOML file")
    bootstrap.add_argument("file", help='path to the TOML file, or "-" for stdin')
    sub.add_parser("generate-keys", help="print new Fernet keys for the env file")
    create = sub.add_parser("create-user", help="create one user with a role")
    create.add_argument("--username", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--role", choices=["admin", "therapist"], default="admin")
    sub.add_parser("verify-restore", help="check a restored backup (RESTORE_DSN)")
    pairing = sub.add_parser(
        "import-pairing", help="read a pairing file (passphrase in KLINIK_PAIRING_PASSPHRASE)"
    )
    pairing.add_argument("file")
    pairing.add_argument("--cert-out", required=True, help="where to save the office certificate")
    sub.add_parser("replica-sync", help="laptop: mark as copy and sync once with the office")
    cert = sub.add_parser("generate-cert", help="create the office HTTPS certificate")
    cert.add_argument("--out", required=True, help="directory for klinik.crt / klinik.key")
    cert.add_argument("--name", action="append", help="DNS name or IP (repeatable)")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
