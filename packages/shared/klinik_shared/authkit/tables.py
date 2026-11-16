"""SQLAlchemy Core definitions of the auth tables (same shape in both tiers).

The DDL itself lives in the Alembic migrations; these objects are for queries.
Pass a ``MetaData(schema="app")`` for the cloud tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID


@dataclass(frozen=True)
class AuthTables:
    users: Table
    user_permissions: Table
    auth_sessions: Table
    recovery_codes: Table
    login_attempts: Table


def _fk(metadata: MetaData, target: str) -> str:
    return f"{metadata.schema}.{target}" if metadata.schema else target


def build_auth_tables(metadata: MetaData) -> AuthTables:
    tz = DateTime(timezone=True)
    uuid = PGUUID(as_uuid=True)
    users_id = _fk(metadata, "users.id")

    users = Table(
        "users",
        metadata,
        Column("id", uuid, primary_key=True),
        Column("username", Text, nullable=False, unique=True),
        Column("display_name", String(60), nullable=False),
        Column("password_hash", Text, nullable=False),
        Column("must_change_password", Boolean, nullable=False),
        Column("totp_secret_enc", LargeBinary),
        Column("totp_confirmed_at", tz),
        Column("totp_last_step", BigInteger),
        Column("is_active", Boolean, nullable=False),
        Column("created_at", tz, nullable=False),
        Column("updated_at", tz, nullable=False),
    )
    user_permissions = Table(
        "user_permissions",
        metadata,
        Column("user_id", uuid, ForeignKey(users_id), primary_key=True),
        Column("permission", Text, primary_key=True),
    )
    auth_sessions = Table(
        "auth_sessions",
        metadata,
        Column("id_hash", LargeBinary, primary_key=True),
        Column("user_id", uuid, ForeignKey(users_id), nullable=False),
        Column("csrf_token", Text, nullable=False),
        Column("created_at", tz, nullable=False),
        Column("last_seen_at", tz, nullable=False),
        Column("totp_passed_at", tz),
        Column("revoked_at", tz),
        Column("ip", Text),
    )
    recovery_codes = Table(
        "recovery_codes",
        metadata,
        Column("id", BigInteger, primary_key=True),
        Column("user_id", uuid, ForeignKey(users_id), nullable=False),
        Column("code_hash", LargeBinary, nullable=False),
        Column("used_at", tz),
    )
    login_attempts = Table(
        "login_attempts",
        metadata,
        Column("id", BigInteger, primary_key=True),
        # sha256 of the typed username: people sometimes type a password into this box.
        Column("username_key", Text, nullable=False),
        Column("ip", Text),
        Column("ts", tz, nullable=False),
        Column("success", Boolean, nullable=False),
    )
    return AuthTables(users, user_permissions, auth_sessions, recovery_codes, login_attempts)


def build_audit_table(metadata: MetaData) -> Table:
    return Table(
        "audit_log",
        metadata,
        Column("id", BigInteger, primary_key=True),
        Column("ts", DateTime(timezone=True), nullable=False),
        Column("actor_id", PGUUID(as_uuid=True)),
        Column("actor_kind", Text, nullable=False),
        Column("action", Text, nullable=False),
        Column("entity_type", Text),
        Column("entity_id", Text),
        Column("detail", JSONB, nullable=False),
        Column("ip", Text),
    )
