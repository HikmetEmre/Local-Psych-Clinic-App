"""Operator commands for the cloud tier.

    python -m klinik_cloud.manage set-role-passwords
    python -m klinik_cloud.manage bootstrap-users users.toml     (or "-" for stdin)
    python -m klinik_cloud.manage generate-keys

The first two connect with CLOUD_MIGRATION_DATABASE_URL (the owner role).
Use the same users.toml as for the office so that user ids match.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from klinik_cloud import tables as t
from klinik_shared.authkit.admin import bootstrap_users, parse_users_file, set_role_passwords
from klinik_shared.authkit.service import AuthStore

ROLE_PASSWORD_ENV = {"cloud_app": "CLOUD_APP_DB_PASSWORD"}


def owner_engine() -> AsyncEngine:
    url = os.environ.get("CLOUD_MIGRATION_DATABASE_URL")
    if not url:
        raise SystemExit("CLOUD_MIGRATION_DATABASE_URL is not set")
    return create_async_engine(url, connect_args={"server_settings": {"search_path": t.SCHEMA}})


def auth_store() -> AuthStore:
    key = os.environ.get("CLOUD_AUTH_FERNET_KEY", "")
    return AuthStore(t.auth, t.audit_log, MultiFernet([Fernet(key.encode())]))


async def _main(args: argparse.Namespace) -> int:
    if args.command == "generate-keys":
        print(f"CLOUD_AUTH_FERNET_KEY={Fernet.generate_key().decode()}")
        return 0
    engine = owner_engine()
    try:
        if args.command == "set-role-passwords":
            passwords = {
                role: os.environ[env]
                for role, env in ROLE_PASSWORD_ENV.items()
                if os.environ.get(env)
            }
            if not passwords:
                raise SystemExit("CLOUD_APP_DB_PASSWORD is not set")
            for role in await set_role_passwords(engine, passwords):
                print(f"password set: {role}")
            return 0
        if args.command == "bootstrap-users":
            raw = (
                sys.stdin.buffer.read()
                if args.file == "-"
                else await asyncio.to_thread(Path(args.file).read_bytes)
            )
            created = await bootstrap_users(engine, auth_store(), parse_users_file(raw))
            if not created:
                print("all users already exist; nothing changed")
            for username, temporary in created.items():
                print(f"created {username}  temporary password: {temporary}")
            return 0
    finally:
        await engine.dispose()
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(prog="klinik_cloud.manage")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("set-role-passwords", help="set the DB password of cloud_app")
    bootstrap = sub.add_parser("bootstrap-users", help="create users from a TOML file")
    bootstrap.add_argument("file", help='path to the TOML file, or "-" for stdin')
    sub.add_parser("generate-keys", help="print a new auth Fernet key")
    sys.exit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()
