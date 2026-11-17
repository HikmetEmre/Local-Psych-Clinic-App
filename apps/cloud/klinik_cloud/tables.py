"""SQLAlchemy Core tables of the cloud tier (schema "app"). DDL lives in migrations/cloud."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from klinik_shared.authkit.tables import build_audit_table, build_auth_tables

SCHEMA = "app"
metadata = MetaData(schema=SCHEMA)

auth = build_auth_tables(metadata)
users = auth.users
user_permissions = auth.user_permissions
audit_log = build_audit_table(metadata)

_tz = DateTime(timezone=True)
_uuid = PGUUID(as_uuid=True)
_gen_uuid = text("gen_random_uuid()")

rooms = Table(
    "rooms",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=_gen_uuid),
    Column("internal_label", String(30), nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("sort_order", Integer, nullable=False, server_default=text("0")),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
)

client_codes = Table(
    "client_codes",
    metadata,
    Column("code", Text, primary_key=True),
    Column("active", Boolean, nullable=False),
    Column("assigned_therapist_id", _uuid, ForeignKey("app.users.id")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
)

user_settings = Table(
    "user_settings",
    metadata,
    Column("user_id", _uuid, ForeignKey("app.users.id"), primary_key=True),
    Column("default_room_id", _uuid, ForeignKey("app.rooms.id")),
    Column("quiet_start", Time, nullable=False),
    Column("quiet_end", Time, nullable=False),
    Column("push_enabled", Boolean, nullable=False),
    Column("reminder_24h", Boolean, nullable=False),
    Column("reminder_1h", Boolean, nullable=False),
)

appointments = Table(
    "appointments",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=_gen_uuid),
    Column("client_code", Text, ForeignKey("app.client_codes.code"), nullable=False),
    Column("therapist_id", _uuid, ForeignKey("app.users.id"), nullable=False),
    Column("room_id", _uuid, ForeignKey("app.rooms.id"), nullable=False),
    Column("starts_at", _tz, nullable=False),
    Column("duration_min", Integer, nullable=False),
    # Set by a trigger; the server_default only satisfies SQLAlchemy on insert.
    Column("ends_at", _tz, nullable=False, server_default=text("now()")),
    Column("status", Text, nullable=False, server_default=text("'planned'")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("change_seq", BigInteger, nullable=False, server_default=text("0")),
    Column("created_by", _uuid, ForeignKey("app.users.id")),
    Column("updated_by", _uuid, ForeignKey("app.users.id")),
    Column("updated_via", Text, nullable=False, server_default=text("'web'")),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("updated_at", _tz, nullable=False, server_default=text("now()")),
    Column("deleted_at", _tz),
)

conflicts = Table(
    "conflicts",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=_gen_uuid),
    Column("kind", Text, nullable=False),
    Column("appointment_id", _uuid, ForeignKey("app.appointments.id")),
    Column("author_id", _uuid, ForeignKey("app.users.id")),
    Column("proposed", JSONB, nullable=False),
    Column("reason_code", Text),
    Column("sync_item_id", _uuid),
    Column("status", Text, nullable=False, server_default=text("'open'")),
    Column("resolution", Text),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("resolved_by", _uuid, ForeignKey("app.users.id")),
    Column("resolved_at", _tz),
)

service_tokens = Table(
    "service_tokens",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=_gen_uuid),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    Column("scope", Text, nullable=False),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("last_used_at", _tz),
    Column("revoked_at", _tz),
)

sync_inbox = Table(
    "sync_inbox",
    metadata,
    Column("item_id", _uuid, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("received_at", _tz, nullable=False, server_default=text("now()")),
    Column("result", Text, nullable=False),
    Column("reason_code", Text),
)

notifications = Table(
    "notifications",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=_gen_uuid),
    Column("recipient_id", _uuid, ForeignKey("app.users.id"), nullable=False),
    Column("event_type", Text, nullable=False),
    Column("appointment_id", _uuid, ForeignKey("app.appointments.id")),
    Column("payload", JSONB, nullable=False),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("read_at", _tz),
    Column("push_state", Text, nullable=False, server_default=text("'pending'")),
    Column("push_after", _tz, nullable=False, server_default=text("now()")),
    Column("next_attempt_at", _tz),
    Column("pushed_at", _tz),
    Column("push_attempts", Integer, nullable=False, server_default=text("0")),
    Column("expires_at", _tz),
    Column("dedupe_key", Text, unique=True),
)

push_subscriptions = Table(
    "push_subscriptions",
    metadata,
    Column("id", _uuid, primary_key=True, server_default=_gen_uuid),
    Column("user_id", _uuid, ForeignKey("app.users.id"), nullable=False),
    Column("endpoint", Text, nullable=False, unique=True),
    Column("p256dh", Text, nullable=False),
    Column("auth", Text, nullable=False),
    Column("created_at", _tz, nullable=False, server_default=text("now()")),
    Column("last_success_at", _tz),
    Column("failure_count", Integer, nullable=False, server_default=text("0")),
)
