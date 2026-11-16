"""auth, permissions, audit log, clients, sync outbox, DB roles

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17

LOCAL tier.

DB roles (passwords are set outside migrations, see `klinik_local.manage`):
  kasa_app    the web app. Not the table owner, so RLS applies to it.
  kasa_sync   the sync worker. Outbox/mirror tables only; no client or clinical data.
  kasa_backup pg_dump. The only role that reads everything.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

PERMISSIONS = (
    "schedule.read_all",
    "schedule.write_all",
    "payments.write",
    "income.read_all",
    "income.read_own",
    "clients.read",
    "clients.write",
    "sessions.read_own",
    "sessions.write_own",
    "sessions.read_others",
    "admin.users",
    "admin.catalog",
    "admin.export",
    "admin.backup",
)


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kasa_app') THEN
                CREATE ROLE kasa_app NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kasa_sync') THEN
                CREATE ROLE kasa_sync NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kasa_backup') THEN
                CREATE ROLE kasa_backup NOLOGIN BYPASSRLS;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT pg_read_all_data TO kasa_backup")
    op.execute("GRANT USAGE ON SCHEMA public TO kasa_app, kasa_sync")

    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := now();
            RETURN NEW;
        END
        $$
        """
    )

    # ------------------------------------------------------------------ auth
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            username text NOT NULL UNIQUE
                CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
            display_name varchar(60) NOT NULL CHECK (btrim(display_name) <> ''),
            password_hash text NOT NULL,
            must_change_password boolean NOT NULL DEFAULT false,
            totp_secret_enc bytea,
            totp_confirmed_at timestamptz,
            totp_last_step bigint,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    permission_list = ", ".join(f"'{p}'" for p in PERMISSIONS)
    op.execute(
        f"""
        CREATE TABLE user_permissions (
            user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
            permission text NOT NULL CHECK (permission IN ({permission_list})),
            PRIMARY KEY (user_id, permission)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auth_sessions (
            id_hash bytea PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
            csrf_token text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            totp_passed_at timestamptz,
            revoked_at timestamptz,
            ip text
        )
        """
    )
    op.execute("CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id)")
    op.execute(
        """
        CREATE TABLE recovery_codes (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
            code_hash bytea NOT NULL,
            used_at timestamptz,
            UNIQUE (user_id, code_hash)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE login_attempts (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            username_key text NOT NULL,
            ip text,
            ts timestamptz NOT NULL DEFAULT now(),
            success boolean NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX login_attempts_key_ts_idx ON login_attempts (username_key, ts DESC)")

    # ------------------------------------------------------------------ audit
    op.execute(
        """
        CREATE TABLE audit_log (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            actor_id uuid,
            actor_kind text NOT NULL CHECK (actor_kind IN ('user', 'sync', 'system')),
            action text NOT NULL,
            entity_type text,
            entity_id text,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            ip text
        )
        """
    )
    op.execute("CREATE INDEX audit_log_ts_idx ON audit_log (ts DESC)")
    op.execute("CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id)")

    # ------------------------------------------------------------------ RLS helpers
    op.execute(
        """
        CREATE FUNCTION app_current_user_id() RETURNS uuid
        LANGUAGE sql STABLE AS $$
            SELECT nullif(current_setting('app.current_user_id', true), '')::uuid
        $$
        """
    )
    # SECURITY DEFINER so policies can consult user_permissions regardless of the caller's grants.
    op.execute(
        """
        CREATE FUNCTION app_has_permission(p text) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT EXISTS (
                SELECT 1
                FROM user_permissions up
                JOIN users u ON u.id = up.user_id
                WHERE up.user_id = app_current_user_id()
                  AND up.permission = p
                  AND u.is_active
            )
        $$
        """
    )

    # ------------------------------------------------------------------ clients
    op.execute(
        r"""
        CREATE TABLE clients (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_code text NOT NULL UNIQUE CHECK (client_code ~ '^DN-\d{4}-\d{4}$'),
            full_name_encrypted bytea NOT NULL,
            phone_encrypted bytea,
            email_encrypted bytea,
            birth_date date,
            assigned_therapist_id uuid REFERENCES users,
            is_active boolean NOT NULL DEFAULT true,
            session_counter integer NOT NULL DEFAULT 0 CHECK (session_counter >= 0),
            version integer NOT NULL DEFAULT 1,
            created_by uuid REFERENCES users,
            updated_by uuid REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX clients_assigned_idx ON clients (assigned_therapist_id)")
    op.execute(
        "CREATE TRIGGER clients_set_updated_at BEFORE UPDATE ON clients "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute(
        "CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute("ALTER TABLE clients ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE clients FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY clients_read ON clients FOR SELECT TO kasa_app
            USING (app_has_permission('clients.read'))
        """
    )
    op.execute(
        """
        CREATE POLICY clients_insert ON clients FOR INSERT TO kasa_app
            WITH CHECK (app_has_permission('clients.write'))
        """
    )
    op.execute(
        """
        CREATE POLICY clients_update ON clients FOR UPDATE TO kasa_app
            USING (app_has_permission('clients.write'))
            WITH CHECK (app_has_permission('clients.write'))
        """
    )

    # One row per year; the row lock taken by the upsert serialises code allocation.
    op.execute(
        """
        CREATE TABLE client_code_counters (
            year integer PRIMARY KEY CHECK (year BETWEEN 2000 AND 9999),
            last_n integer NOT NULL CHECK (last_n BETWEEN 1 AND 9999)
        )
        """
    )

    # ------------------------------------------------------------------ sync outbox
    op.execute(
        """
        CREATE TABLE sync_outbox (
            seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            item_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
            kind text NOT NULL
                CHECK (kind IN ('client_code.upsert', 'appointment.outcome', 'conflict.resolved')),
            payload jsonb NOT NULL,
            state text NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'sent', 'rejected', 'held', 'dead')),
            attempts integer NOT NULL DEFAULT 0,
            last_error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            sent_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX sync_outbox_pending_idx ON sync_outbox (seq) WHERE state = 'pending'")

    # ------------------------------------------------------------------ grants
    op.execute("GRANT SELECT, INSERT, UPDATE ON users TO kasa_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON user_permissions TO kasa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON auth_sessions TO kasa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON recovery_codes TO kasa_app")
    op.execute("GRANT SELECT, INSERT ON login_attempts TO kasa_app")
    # Append-only for the app: no UPDATE, no DELETE.
    op.execute("GRANT SELECT, INSERT ON audit_log TO kasa_app")
    # No DELETE: clients are only ever deactivated or soft-deleted.
    op.execute("GRANT SELECT, INSERT, UPDATE ON clients TO kasa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON client_code_counters TO kasa_app")
    op.execute("GRANT SELECT, INSERT ON sync_outbox TO kasa_app")
    # The sync worker sees the outbox and nothing about clients.
    op.execute("GRANT SELECT, UPDATE ON sync_outbox TO kasa_sync")
    op.execute("GRANT INSERT ON audit_log TO kasa_sync")


def downgrade() -> None:
    for table in (
        "sync_outbox",
        "client_code_counters",
        "clients",
        "audit_log",
        "login_attempts",
        "recovery_codes",
        "auth_sessions",
        "user_permissions",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS app_has_permission(text)")
    op.execute("DROP FUNCTION IF EXISTS app_current_user_id()")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.execute("REVOKE USAGE ON SCHEMA public FROM kasa_app, kasa_sync")
    # Roles are cluster-wide and may be used by other databases: they are left in place.
