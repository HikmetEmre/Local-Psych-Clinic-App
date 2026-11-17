"""The cloud database holds no client identity and no free text (brief §3, §11).

Two independent checks over the migrated cloud schema:
1. denylist: no column name that suggests identity, contact data, notes or diagnoses;
2. allowlist: every text-like column is on a reviewed list. A new string column fails
   here until someone looks at it and adds it deliberately.

Plus: nothing lives in ``public``, and Supabase's API roles cannot reach the schema.
"""

from __future__ import annotations

import re

import asyncpg
import pytest
from support.cloud import CloudDb, prepare_cloud_db
from support.local import execute, fetch, run

pytestmark = pytest.mark.integration

FORBIDDEN = re.compile(
    r"name|phone|tel|email|mail|birth|dob|address|adres|note|comment|reason|descr|"
    r"diagnos|method|therap(y|i)_|text|memo|remark|ad_soyad|isim",
    re.IGNORECASE,
)
# Columns whose names match the pattern but are reviewed and harmless.
DENYLIST_EXCEPTIONS = {
    ("users", "username"),  # staff login name
    ("users", "display_name"),  # staff display name ("Uzm. Psk. A. Y.")
    ("login_attempts", "username_key"),  # sha256 of the typed username
    ("conflicts", "reason_code"),  # machine code, CHECK ^[a-z_]{2,40}$
    ("sync_inbox", "reason_code"),
    ("client_codes", "assigned_therapist_id"),  # a user id
    ("appointments", "therapist_id"),
    ("user_settings", "default_room_id"),
}

# Every text / varchar / json / bytea column in the cloud, and why it is acceptable.
TEXTUAL_ALLOWLIST = {
    ("users", "username"): "staff login, CHECK-limited",
    ("users", "display_name"): "staff name, varchar(60)",
    ("users", "password_hash"): "bcrypt hash",
    ("users", "totp_secret_enc"): "Fernet token",
    ("auth_sessions", "id_hash"): "sha256",
    ("auth_sessions", "csrf_token"): "random token",
    ("auth_sessions", "ip"): "request ip",
    ("recovery_codes", "code_hash"): "sha256",
    ("login_attempts", "username_key"): "sha256 hex, CHECK",
    ("login_attempts", "ip"): "request ip",
    ("audit_log", "actor_kind"): "enum",
    ("audit_log", "action"): "code, CHECK",
    ("audit_log", "entity_type"): "code, CHECK",
    ("audit_log", "entity_id"): "id, length-limited",
    ("audit_log", "detail"): "jsonb written by write_audit (refuses free text)",
    ("audit_log", "ip"): "request ip",
    ("user_permissions", "permission"): "enum",
    ("rooms", "internal_label"): "the one allowed label, varchar(30)",
    ("client_codes", "code"): "DN-YYYY-NNNN, CHECK",
    ("appointments", "client_code"): "DN-YYYY-NNNN, FK",
    ("appointments", "status"): "enum",
    ("appointments", "updated_via"): "enum",
    ("conflicts", "kind"): "enum",
    ("conflicts", "proposed"): "jsonb of AppointmentFields (extra=forbid): times, ids, status",
    ("conflicts", "reason_code"): "code, CHECK",
    ("conflicts", "status"): "enum",
    ("conflicts", "resolution"): "enum",
    ("service_tokens", "token_hash"): "sha256",
    ("service_tokens", "scope"): "enum",
    ("sync_inbox", "kind"): "enum",
    ("sync_inbox", "result"): "enum",
    ("sync_inbox", "reason_code"): "code, CHECK",
    ("notifications", "event_type"): "enum",
    ("notifications", "payload"): "jsonb of NotificationPayload (extra=forbid): ids, code, times",
    ("notifications", "push_state"): "enum",
    ("notifications", "dedupe_key"): "reminder key, length-limited",
    ("push_subscriptions", "endpoint"): "browser push-service URL, CHECK https + length",
    ("push_subscriptions", "p256dh"): "browser public key",
    ("push_subscriptions", "auth"): "browser auth secret",
}

TEXTUAL_TYPES = ("text", "character varying", "character", "json", "jsonb", "bytea", "USER-DEFINED")


def columns(db: CloudDb) -> list[tuple[str, str, str]]:
    rows = fetch(
        db,  # type: ignore[arg-type]
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'app' AND table_name <> 'alembic_version' ORDER BY 1, 2",
    )
    return [(r["table_name"], r["column_name"], r["data_type"]) for r in rows]


def test_no_forbidden_column_names(cloud_db: CloudDb) -> None:
    offenders = [
        f"{table}.{column}"
        for table, column, _ in columns(cloud_db)
        if FORBIDDEN.search(column) and (table, column) not in DENYLIST_EXCEPTIONS
    ]
    assert offenders == [], f"columns that look like personal or clinical data: {offenders}"


def test_every_textual_column_is_reviewed(cloud_db: CloudDb) -> None:
    textual = {
        (table, column)
        for table, column, data_type in columns(cloud_db)
        if data_type in TEXTUAL_TYPES
    }
    unreviewed = sorted(textual - TEXTUAL_ALLOWLIST.keys())
    assert unreviewed == [], (
        "new text/json/bytea columns in the cloud schema must be reviewed and added to "
        f"TEXTUAL_ALLOWLIST: {unreviewed}"
    )
    stale = sorted(TEXTUAL_ALLOWLIST.keys() - textual)
    assert stale == [], f"allowlist entries for columns that no longer exist: {stale}"


def test_free_text_columns_are_length_limited(cloud_db: CloudDb) -> None:
    rows = fetch(
        cloud_db,  # type: ignore[arg-type]
        "SELECT table_name, column_name, character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = 'app' AND data_type = 'character varying' "
        "AND table_name <> 'alembic_version'",
    )
    limits = {(r["table_name"], r["column_name"]): r["character_maximum_length"] for r in rows}
    assert limits == {("rooms", "internal_label"): 30, ("users", "display_name"): 60}


def test_client_codes_are_the_only_client_reference(cloud_db: CloudDb) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        execute(cloud_db, "INSERT INTO client_codes (code) VALUES ('Ayşe Yılmaz')")  # type: ignore[arg-type]
    with pytest.raises(asyncpg.CheckViolationError):
        execute(cloud_db, "INSERT INTO client_codes (code) VALUES ('DN-0042')")  # type: ignore[arg-type]
    with pytest.raises(asyncpg.StringDataRightTruncationError):
        execute(
            cloud_db,  # type: ignore[arg-type]
            "INSERT INTO rooms (internal_label) VALUES ($1)",
            "Ayşe Yılmaz'ın kaygı seansı için ayrılan oda",
        )


def test_nothing_lives_in_the_public_schema(cloud_db: CloudDb) -> None:
    rows = fetch(
        cloud_db,  # type: ignore[arg-type]
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'",
    )
    assert rows == []


def test_every_table_has_row_level_security(cloud_db: CloudDb) -> None:
    rows = fetch(
        cloud_db,  # type: ignore[arg-type]
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'app' AND c.relkind = 'r' AND c.relname <> 'alembic_version'",
    )
    assert len(rows) == 15
    assert all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows)


def test_supabase_api_roles_cannot_reach_the_schema(fresh_database: str) -> None:
    """Simulate Supabase: anon and authenticated exist before the migrations run."""
    base = fresh_database.replace("+asyncpg", "")

    async def setup() -> None:
        conn = await asyncpg.connect(base)
        try:
            for role in ("anon", "authenticated"):
                await conn.execute(
                    f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{role}') "
                    f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $$"
                )
                await conn.execute(f"GRANT {role} TO CURRENT_USER")
        finally:
            await conn.close()

    run(setup())
    db = prepare_cloud_db(fresh_database)
    execute(db, "INSERT INTO rooms (internal_label) VALUES ('Oda 1')")  # type: ignore[arg-type]

    async def probe(role: str) -> None:
        conn = await asyncpg.connect(base)
        try:
            await conn.execute(f"SET ROLE {role}")
            await conn.fetch("SELECT * FROM app.rooms")
        finally:
            await conn.close()

    for role in ("anon", "authenticated", "public"):
        if role == "public":
            continue
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(probe(role))


def test_app_role_cannot_rewrite_history(cloud_db: CloudDb) -> None:
    async def as_app(sql: str) -> None:
        conn = await asyncpg.connect(cloud_db.dsn(cloud_db.app_url))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    row = fetch(cloud_db, "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='cloud_app'")[
        0
    ]  # type: ignore[arg-type]
    assert row["rolsuper"] is False and row["rolbypassrls"] is False
    for statement in (
        "UPDATE audit_log SET action = 'x'",
        "DELETE FROM audit_log",
        "DELETE FROM appointments",
        "DROP TABLE appointments",
        "CREATE TABLE app.notes (body text)",
    ):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            run(as_app(statement))
