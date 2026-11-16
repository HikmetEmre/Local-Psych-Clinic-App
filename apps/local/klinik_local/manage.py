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
import tomllib
from typing import Any
from uuid import UUID

from cryptography.fernet import MultiFernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from klinik_local import tables as t
from klinik_local.crypto.fernet import generate_key, parse_keys
from klinik_shared.authkit.service import AuthStore
from klinik_shared.permissions import expand_sets

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


async def set_role_passwords(engine: AsyncEngine, passwords: dict[str, str]) -> list[str]:
    """Give the application roles a password and LOGIN. Returns the roles that were set."""
    done = []
    async with engine.begin() as conn:
        for role, password in passwords.items():
            # ALTER ROLE takes no bind parameters; format() quotes both parts safely.
            statement = (
                await conn.execute(
                    text(
                        "SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', "
                        "CAST(:role AS text), CAST(:password AS text))"
                    ),
                    {"role": role, "password": password},
                )
            ).scalar_one()
            await conn.execute(text(statement))
            done.append(role)
    return done


def parse_users_file(raw: bytes) -> list[dict[str, Any]]:
    data = tomllib.loads(raw.decode("utf-8"))
    users = data.get("users")
    if not isinstance(users, list) or not users:
        raise ValueError("the file needs at least one [[users]] entry")
    parsed = []
    for entry in users:
        parsed.append(
            {
                "user_id": UUID(str(entry["id"])),
                "username": str(entry["username"]),
                "display_name": str(entry["display_name"]),
                "permissions": expand_sets(*entry["sets"]),
            }
        )
    if len({u["user_id"] for u in parsed}) != len(parsed):
        raise ValueError("duplicate user id")
    return parsed


async def bootstrap_users(
    engine: AsyncEngine, store: AuthStore, users: list[dict[str, Any]]
) -> dict[str, str]:
    """Create missing users. Returns {username: temporary password} for the new ones."""
    created: dict[str, str] = {}
    async with engine.begin() as conn:
        for user in users:
            temporary = await store.ensure_user(conn, **user)
            if temporary is not None:
                created[user["username"]] = temporary
    return created


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
            raw = sys.stdin.buffer.read() if args.file == "-" else open(args.file, "rb").read()  # noqa: ASYNC230, SIM115
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
