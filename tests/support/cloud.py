"""Helpers for integration tests of the cloud tier (Klinik Web)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from alembic import command
from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy.ext.asyncio import create_async_engine

from klinik_cloud import tables as t
from klinik_cloud.config import Settings
from klinik_cloud.main import create_app
from klinik_shared.authkit.admin import bootstrap_users, set_role_passwords
from klinik_shared.authkit.service import AuthStore
from klinik_shared.permissions import expand_sets
from support.local import (
    A_ID,
    B_ID,
    ROLE_PASSWORD,
    Kasa,
    alembic_config,
    env,
    execute,
    run,
)

CLOUD_AUTH_KEY = Fernet.generate_key().decode()


@dataclass(frozen=True)
class CloudDb:
    owner_url: str
    app_url: str

    def dsn(self, url: str | None = None) -> str:
        # asyncpg passes unknown DSN parameters to the server as settings.
        return (url or self.owner_url).replace("+asyncpg", "") + "?search_path=app"


def prepare_cloud_db(database_url: str) -> CloudDb:
    with env(CLOUD_MIGRATION_DATABASE_URL=database_url):
        command.upgrade(alembic_config("cloud"), "head")

    async def roles() -> None:
        engine = create_async_engine(database_url)
        try:
            await set_role_passwords(engine, {"cloud_app": ROLE_PASSWORD})
        finally:
            await engine.dispose()

    run(roles())
    app_url = re.sub(r"//[^@]+@", f"//cloud_app:{ROLE_PASSWORD}@", database_url)
    return CloudDb(database_url, app_url)


def cloud_store() -> AuthStore:
    return AuthStore(t.auth, t.audit_log, MultiFernet([Fernet(CLOUD_AUTH_KEY.encode())]))


def add_cloud_user(db: CloudDb, user_id: UUID, username: str, name: str, sets: list[str]) -> str:
    async def go() -> dict[str, str]:
        engine = create_async_engine(
            db.owner_url, connect_args={"server_settings": {"search_path": "app"}}
        )
        try:
            user = {
                "user_id": user_id,
                "username": username,
                "display_name": name,
                "permissions": expand_sets(*sets),
            }
            return await bootstrap_users(engine, cloud_store(), [user])
        finally:
            await engine.dispose()

    return run(go())[username]


def add_cloud_therapists(db: CloudDb) -> dict[str, str]:
    return {
        "a": add_cloud_user(db, A_ID, "terapist.a", "Terapist A", ["admin", "therapist"]),
        "b": add_cloud_user(db, B_ID, "terapist.b", "Terapist B", ["therapist", "scheduler"]),
    }


def add_room(db: CloudDb, label: str) -> UUID:
    rows = execute_returning(
        db, "INSERT INTO rooms (internal_label) VALUES ($1) RETURNING id", label
    )
    return UUID(str(rows))


def execute_returning(db: CloudDb, sql: str, *args: Any) -> Any:
    from support.local import scalar

    return scalar(db, sql, *args)  # type: ignore[arg-type]


def add_code(db: CloudDb, code: str, *, active: bool = True, therapist: UUID | None = None) -> None:
    execute(
        db,  # type: ignore[arg-type]
        "INSERT INTO client_codes (code, active, assigned_therapist_id) VALUES ($1, $2, $3)",
        code,
        active,
        therapist,
    )


class Web(Kasa):
    """One browser talking to Klinik Web."""

    cookie_name = "web_session"

    def __init__(self, db: CloudDb) -> None:
        app = create_app(
            Settings(database_url=db.app_url, auth_fernet_key=CLOUD_AUTH_KEY, run_workers=False)
        )
        super().__init__(db, app=app)
