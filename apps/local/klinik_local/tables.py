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
    Numeric,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from klinik_shared.authkit.tables import build_audit_table, build_auth_tables

metadata = MetaData()

auth = build_auth_tables(metadata)
users = auth.users
users.append_column(Column("default_room_id", PGUUID(as_uuid=True), ForeignKey("rooms.id")))
users.append_column(Column("change_seq", BigInteger))
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

rooms = Table(
    "rooms",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("label", Text, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("sort_order", Integer, nullable=False, server_default=text("0")),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
)

appointments = Table(
    "appointments",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("client_id", _uuid, ForeignKey("clients.id"), nullable=False),
    Column("therapist_id", _uuid, ForeignKey("users.id"), nullable=False),
    Column("room_id", _uuid, ForeignKey("rooms.id"), nullable=False),
    Column("starts_at", _tz, nullable=False),
    Column("duration_min", Integer, nullable=False),
    # Kept by a trigger; the default only satisfies SQLAlchemy on insert.
    Column("ends_at", _tz, nullable=False, server_default=text("now()")),
    Column("status", Text, nullable=False, server_default=text("'planned'")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_by", _uuid, ForeignKey("users.id")),
    Column("updated_by", _uuid, ForeignKey("users.id")),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
    Column("deleted_at", _tz),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
)

payments = Table(
    "payments",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("appointment_id", _uuid, ForeignKey("appointments.id"), nullable=False),
    Column("amount", Numeric(10, 2), nullable=False),
    Column("currency", Text, nullable=False, server_default=text("'TRY'")),
    Column("method", Text, nullable=False),
    Column("paid_at", _tz, nullable=False),
    Column("recorded_by", _uuid, ForeignKey("users.id"), nullable=False),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
    Column("voided_at", _tz),
    Column("voided_by", _uuid, ForeignKey("users.id")),
    Column("void_reason", Text),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
)

expenses = Table(
    "expenses",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("spent_on", Date, nullable=False),
    Column("category", Text, nullable=False),
    Column("description", Text),
    Column("amount", Numeric(12, 2), nullable=False),
    Column("method", Text, nullable=False),
    Column("recorded_by", _uuid, ForeignKey("users.id"), nullable=False),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
    Column("voided_at", _tz),
    Column("voided_by", _uuid, ForeignKey("users.id")),
    Column("void_reason", Text),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
)

client_notes = Table(
    "client_notes",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("client_id", _uuid, ForeignKey("clients.id"), nullable=False),
    Column("author_id", _uuid, ForeignKey("users.id"), nullable=False),
    Column("session_id", _uuid, ForeignKey("therapy_sessions.id")),
    Column("kind", Text, nullable=False),
    Column("title_encrypted", LargeBinary),
    Column("body_encrypted", LargeBinary, nullable=False),
    Column("noted_at", _tz, nullable=False),
    Column("important", Boolean, nullable=False, server_default=text("false")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
    Column("deleted_at", _tz),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
)

note_revisions = Table(
    "note_revisions",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("note_id", _uuid, ForeignKey("client_notes.id")),
    Column("session_id", _uuid, ForeignKey("therapy_sessions.id")),
    Column("version", Integer, nullable=False),
    Column("kind", Text),
    Column("title_encrypted", LargeBinary),
    Column("body_encrypted", LargeBinary),
    Column("homework_encrypted", LargeBinary),
    Column("important", Boolean),
    Column("noted_at", _tz),
    Column("reason", Text, nullable=False),
    Column("edited_by", _uuid, ForeignKey("users.id"), nullable=False),
    Column("edited_at", _tz, nullable=False, server_default=text("now()")),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
)
