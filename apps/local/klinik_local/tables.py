"""SQLAlchemy Core tables of the local tier (queries only; DDL lives in migrations/local)."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from klinik_shared.authkit.tables import build_audit_table, build_auth_tables

metadata = MetaData()

auth = build_auth_tables(metadata)
users = auth.users
user_permissions = auth.user_permissions
audit_log = build_audit_table(metadata)

_tz = DateTime(timezone=True)
_uuid = PGUUID(as_uuid=True)

clients = Table(
    "clients",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("client_code", Text, nullable=False, unique=True),
    Column("full_name_encrypted", LargeBinary, nullable=False),
    Column("phone_encrypted", LargeBinary),
    Column("email_encrypted", LargeBinary),
    Column("birth_date", Date),
    Column("assigned_therapist_id", _uuid, ForeignKey("users.id")),
    Column("is_active", Boolean, nullable=False),
    Column("session_counter", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_by", _uuid, ForeignKey("users.id")),
    Column("updated_by", _uuid, ForeignKey("users.id")),
    Column("created_at", _tz, nullable=False),
    Column("updated_at", _tz, nullable=False),
    Column("deleted_at", _tz),
)

client_code_counters = Table(
    "client_code_counters",
    metadata,
    Column("year", Integer, primary_key=True),
    Column("last_n", Integer, nullable=False),
)

sync_outbox = Table(
    "sync_outbox",
    metadata,
    Column("seq", BigInteger, primary_key=True),
    Column("item_id", _uuid, nullable=False, unique=True),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("state", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("last_error_code", Text),
    Column("created_at", _tz, nullable=False),
    Column("sent_at", _tz),
)

age_groups = Table(
    "age_groups",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("label_tr", Text, nullable=False),
    Column("min_age", Integer, nullable=False),
    Column("max_age", Integer),
    Column("sort_order", Integer, nullable=False),
    Column("is_active", Boolean, nullable=False),
)

therapy_methods = Table(
    "therapy_methods",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False, unique=True),
    Column("label_tr", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("description_tr", Text),
    Column("is_active", Boolean, nullable=False),
    Column("created_at", _tz, nullable=False),
)

age_group_therapy_defaults = Table(
    "age_group_therapy_defaults",
    metadata,
    Column("age_group_id", Integer, ForeignKey("age_groups.id"), primary_key=True),
    Column("therapy_method_id", Integer, ForeignKey("therapy_methods.id"), primary_key=True),
    Column("sort_order", Integer, nullable=False),
)

therapy_sessions = Table(
    "therapy_sessions",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("client_id", _uuid, ForeignKey("clients.id"), nullable=False),
    Column("therapist_id", _uuid, ForeignKey("users.id"), nullable=False),
    Column("age_group_id_at_session", Integer, ForeignKey("age_groups.id"), nullable=False),
    Column("format_method_id", Integer, ForeignKey("therapy_methods.id"), nullable=False),
    Column("session_number", Integer, nullable=False),
    Column("session_date", _tz, nullable=False),
    Column("duration_min", Integer, nullable=False),
    Column("status", Text, nullable=False),
    Column("notes_encrypted", LargeBinary),
    Column("homework_encrypted", LargeBinary),
    Column("next_session_date", _tz),
    Column("appointment_id", _uuid),
    Column("version", Integer, nullable=False),
    Column("created_by", _uuid, ForeignKey("users.id")),
    Column("updated_by", _uuid, ForeignKey("users.id")),
    Column("created_at", _tz, nullable=False),
    Column("updated_at", _tz, nullable=False),
    Column("deleted_at", _tz),
)

session_methods = Table(
    "session_methods",
    metadata,
    Column("session_id", _uuid, ForeignKey("therapy_sessions.id"), primary_key=True),
    Column("therapy_method_id", Integer, ForeignKey("therapy_methods.id"), primary_key=True),
    Column("is_primary", Boolean, nullable=False),
)
