"""Operator commands for the local tier.

    python -m klinik_local.manage set-role-passwords
    python -m klinik_local.manage bootstrap-users users.toml     (or "-" for stdin)
    python -m klinik_local.manage generate-keys

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
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
