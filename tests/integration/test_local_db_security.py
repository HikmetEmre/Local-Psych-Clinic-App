"""Second layer: what the database itself refuses, whatever the application does."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg
import pytest
from support.local import A_ID, LocalDb, add_therapists, add_user, execute, fetch, run, scalar

pytestmark = pytest.mark.integration

NOBODY_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a04")

INSERT_CLIENT = (
    "INSERT INTO clients (client_code, full_name_encrypted) VALUES ('DN-2026-0001', '\\x00')"
)


@pytest.fixture
def db(local_db: LocalDb) -> LocalDb:
    add_therapists(local_db)
    add_user(local_db, NOBODY_ID, "yetkisiz", "Yetkisiz", [])
    execute(local_db, INSERT_CLIENT)  # as owner
    return local_db


def as_app(db: LocalDb, sql: str, user_id: UUID | None = None, *, url: str | None = None) -> Any:
    """Run one statement as kasa_app, inside a transaction, optionally as a logged-in user."""

    async def go() -> Any:
        conn = await asyncpg.connect(db.dsn(url or db.app_url))
        try:
            async with conn.transaction():
                if user_id is not None:
                    await conn.execute(
                        "SELECT set_config('app.current_user_id', $1, true)", str(user_id)
                    )
                return await conn.fetch(sql)
        finally:
            await conn.close()

    return run(go())


def test_app_role_is_neither_superuser_nor_table_owner(db: LocalDb) -> None:
    row = fetch(db, "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'kasa_app'")[0]
    assert row["rolsuper"] is False and row["rolbypassrls"] is False
    owner = scalar(db, "SELECT tableowner FROM pg_tables WHERE tablename = 'clients'")
    assert owner != "kasa_app"
    forced = fetch(
        db, "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='clients'"
    )[0]
    assert forced["relrowsecurity"] and forced["relforcerowsecurity"]


def test_without_a_user_the_clients_table_looks_empty(db: LocalDb) -> None:
    assert scalar(db, "SELECT count(*) FROM clients") == 1  # owner sees it
    assert as_app(db, "SELECT count(*) FROM clients")[0][0] == 0
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        as_app(db, INSERT_CLIENT.replace("0001", "0002"))


def test_a_user_without_the_permission_sees_no_clients(db: LocalDb) -> None:
    assert as_app(db, "SELECT count(*) FROM clients", NOBODY_ID)[0][0] == 0
    assert as_app(db, "SELECT count(*) FROM clients", A_ID)[0][0] == 1
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        as_app(db, INSERT_CLIENT.replace("0001", "0002"), NOBODY_ID)
    # An UPDATE by someone without write permission silently matches nothing.
    as_app(db, "UPDATE clients SET is_active = false", NOBODY_ID)
    assert scalar(db, "SELECT is_active FROM clients") is True


def test_a_deactivated_user_loses_access_at_the_database_too(db: LocalDb) -> None:
    execute(db, "UPDATE users SET is_active = false WHERE id = $1", A_ID)
    assert as_app(db, "SELECT count(*) FROM clients", A_ID)[0][0] == 0


def test_the_user_setting_does_not_leak_between_transactions(db: LocalDb) -> None:
    async def go() -> tuple[int, int]:
        conn = await asyncpg.connect(db.dsn(db.app_url))
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", str(A_ID))
                inside = await conn.fetchval("SELECT count(*) FROM clients")
            after = await conn.fetchval("SELECT count(*) FROM clients")
            return inside, after
        finally:
            await conn.close()

    assert run(go()) == (1, 0)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET action = 'tampered'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
        "DELETE FROM clients",
        "DELETE FROM users",
        "DELETE FROM appointments",
        "DELETE FROM payments",
        "UPDATE payments SET amount = 1",
        "DROP TABLE clients",
        "ALTER TABLE clients DISABLE ROW LEVEL SECURITY",
        "CREATE TABLE public.sneaky (x int)",
    ],
)
def test_app_role_cannot(db: LocalDb, statement: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        as_app(db, statement, A_ID)


@pytest.mark.parametrize(
    "table", ["clients", "users", "auth_sessions", "recovery_codes", "user_permissions"]
)
def test_sync_role_cannot_read_identity_or_credentials(db: LocalDb, table: str) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        as_app(db, f"SELECT * FROM {table}", url=db.sync_url)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO clients (client_code, full_name_encrypted) VALUES ('DN-0042', '\\x00')",
        "INSERT INTO clients (client_code, full_name_encrypted) VALUES ('DN-2026-0001', '\\x00')",
        "INSERT INTO user_permissions (user_id, permission) "
        "SELECT id, 'superuser.everything' FROM users LIMIT 1",
        "INSERT INTO users (id, username, display_name, password_hash) "
        "VALUES (gen_random_uuid(), 'Has Spaces', 'x', 'x')",
        "INSERT INTO client_code_counters (year, last_n) VALUES (2026, 10000)",
    ],
)
def test_constraints_reject_bad_rows_even_from_the_owner(db: LocalDb, statement: str) -> None:
    with pytest.raises((asyncpg.CheckViolationError, asyncpg.UniqueViolationError)):
        execute(db, statement)
