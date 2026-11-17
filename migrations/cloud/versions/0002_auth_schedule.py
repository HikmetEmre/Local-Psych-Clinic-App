"""auth, rooms, client codes, appointments, conflicts, audit

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17

CLOUD tier, schema "app" (search_path is set by env.py).

Forbidden here (brief §3): client names, phones, emails, birth dates, addresses,
free text about a client or session, therapy methods or diagnoses. Clients appear
only as DN-YYYY-NNNN codes. tests/integration/test_cloud_schema.py enforces this.
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

TABLES = (
    "users",
    "user_permissions",
    "auth_sessions",
    "recovery_codes",
    "login_attempts",
    "audit_log",
    "rooms",
    "client_codes",
    "user_settings",
    "appointments",
    "conflicts",
)

# The same time-range expression for every no-double-booking constraint.
SLOT = "tstzrange(starts_at, ends_at)"
HOLDS_SLOT = "status IN ('planned', 'completed') AND deleted_at IS NULL"


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cloud_app') THEN
                CREATE ROLE cloud_app NOLOGIN;
            END IF;
        END
        $$
        """
    )
    # Nobody but the owner and cloud_app may even see this schema. On Supabase this is
    # what keeps the auto-generated Data API (roles anon / authenticated) out.
    op.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA app TO cloud_app")
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format('REVOKE ALL ON SCHEMA app FROM %I', r);
                END IF;
            END LOOP;
        END
        $$
        """
    )

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

    # ------------------------------------------------------------------ auth (as local)
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            username text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9_.-]{2,31}$'),
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
    op.execute(
        "CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
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
            username_key text NOT NULL CHECK (username_key ~ '^[0-9a-f]{64}$'),
            ip text,
            ts timestamptz NOT NULL DEFAULT now(),
            success boolean NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX login_attempts_key_ts_idx ON login_attempts (username_key, ts DESC)")
    op.execute(
        """
        CREATE TABLE audit_log (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts timestamptz NOT NULL DEFAULT now(),
            actor_id uuid,
            actor_kind text NOT NULL CHECK (actor_kind IN ('user', 'sync', 'system')),
            action text NOT NULL CHECK (action ~ '^[a-z_.]{2,60}$'),
            entity_type text CHECK (entity_type ~ '^[a-z_]{2,40}$'),
            entity_id text CHECK (length(entity_id) <= 64),
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            ip text
        )
        """
    )
    op.execute("CREATE INDEX audit_log_ts_idx ON audit_log (ts DESC)")
    op.execute("CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id)")

    # ------------------------------------------------------------------ change cursor
    # Every write to a mirrored table takes this lock and the next sequence number.
    # The lock is held until commit, so sequence order equals commit order and the sync
    # worker's cursor ("give me everything after N") can never skip a row.
    op.execute("CREATE SEQUENCE change_seq")
    op.execute(
        """
        CREATE FUNCTION assign_change_seq() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(4242001);
            NEW.change_seq := nextval('app.change_seq');
            RETURN NEW;
        END
        $$
        """
    )

    # ------------------------------------------------------------------ rooms
    op.execute(
        """
        CREATE TABLE rooms (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- The only free-text field allowed in the cloud (brief §3): a short room name.
            internal_label varchar(30) NOT NULL CHECK (btrim(internal_label) <> ''),
            is_active boolean NOT NULL DEFAULT true,
            sort_order smallint NOT NULL DEFAULT 0,
            change_seq bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE TRIGGER rooms_change_seq BEFORE INSERT OR UPDATE ON rooms "
        "FOR EACH ROW EXECUTE FUNCTION assign_change_seq()"
    )
    op.execute(
        "CREATE TRIGGER rooms_set_updated_at BEFORE UPDATE ON rooms "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute("CREATE UNIQUE INDEX rooms_change_seq_idx ON rooms (change_seq)")

    # ------------------------------------------------------------------ client codes
    # Pushed from the office by the sync worker: code, active flag, assigned therapist.
    op.execute(
        r"""
        CREATE TABLE client_codes (
            code text PRIMARY KEY CHECK (code ~ '^DN-\d{4}-\d{4}$'),
            active boolean NOT NULL DEFAULT true,
            assigned_therapist_id uuid REFERENCES users,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE user_settings (
            user_id uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
            default_room_id uuid REFERENCES rooms,
            quiet_start time NOT NULL DEFAULT '22:00',
            quiet_end time NOT NULL DEFAULT '08:00',
            push_enabled boolean NOT NULL DEFAULT true,
            reminder_24h boolean NOT NULL DEFAULT true,
            reminder_1h boolean NOT NULL DEFAULT true
        )
        """
    )

    # ------------------------------------------------------------------ appointments
    op.execute(
        """
        CREATE TABLE appointments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_code text NOT NULL REFERENCES client_codes,
            therapist_id uuid NOT NULL REFERENCES users,
            room_id uuid NOT NULL REFERENCES rooms,
            starts_at timestamptz NOT NULL,
            duration_min smallint NOT NULL CHECK (duration_min BETWEEN 10 AND 240),
            -- Maintained by a trigger. "starts_at + interval" is not IMMUTABLE for timestamptz,
            -- so Postgres refuses it inside an index or exclusion constraint.
            ends_at timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'completed', 'cancelled', 'no_show')),
            version integer NOT NULL DEFAULT 1,
            change_seq bigint NOT NULL,
            created_by uuid REFERENCES users,
            updated_by uuid REFERENCES users,
            updated_via text NOT NULL DEFAULT 'web' CHECK (updated_via IN ('web', 'sync')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            UNIQUE (id, client_code)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION appointments_set_ends_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.ends_at := NEW.starts_at + make_interval(mins => NEW.duration_min);
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER appointments_ends_at BEFORE INSERT OR UPDATE ON appointments "
        "FOR EACH ROW EXECUTE FUNCTION appointments_set_ends_at()"
    )
    op.execute(
        "CREATE TRIGGER appointments_change_seq BEFORE INSERT OR UPDATE ON appointments "
        "FOR EACH ROW EXECUTE FUNCTION assign_change_seq()"
    )
    op.execute(
        "CREATE TRIGGER appointments_set_updated_at BEFORE UPDATE ON appointments "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    # No double booking, enforced by the database (brief §7): per therapist, per room,
    # and per client (one person cannot be in two sessions at once).
    for name, column in (
        ("no_overlap_therapist", "therapist_id"),
        ("no_overlap_room", "room_id"),
        ("no_overlap_client", "client_code"),
    ):
        op.execute(
            f"ALTER TABLE appointments ADD CONSTRAINT {name} EXCLUDE USING gist "
            f"({column} WITH =, {SLOT} WITH &&) WHERE ({HOLDS_SLOT})"
        )
    op.execute("CREATE INDEX appointments_therapist_start_idx ON appointments (therapist_id, starts_at)")
    op.execute("CREATE INDEX appointments_start_idx ON appointments (starts_at)")
    op.execute("CREATE UNIQUE INDEX appointments_change_seq_idx ON appointments (change_seq)")

    # ------------------------------------------------------------------ conflicts
    op.execute(
        """
        CREATE TABLE conflicts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind text NOT NULL CHECK (kind IN
                ('booking_version', 'sync_status', 'sync_unknown_appointment', 'sync_error')),
            appointment_id uuid REFERENCES appointments,
            author_id uuid REFERENCES users,
            -- Typed values only (ids, codes, times, status); validated by the app.
            proposed jsonb NOT NULL DEFAULT '{}'::jsonb,
            reason_code text CHECK (reason_code ~ '^[a-z_]{2,40}$'),
            sync_item_id uuid,
            status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
            resolution text CHECK (resolution IN ('discarded', 'reapplied')),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_by uuid REFERENCES users,
            resolved_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX conflicts_open_idx ON conflicts (author_id) WHERE status = 'open'")

    # ------------------------------------------------------------------ RLS + grants
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # The cloud holds no clinical data; per-user rules live in the app. The policy
        # exists so that ANY other role (anon, authenticated, ...) sees nothing.
        op.execute(
            f"CREATE POLICY {table}_app ON {table} FOR ALL TO cloud_app USING (true) WITH CHECK (true)"
        )

    op.execute("GRANT SELECT, INSERT, UPDATE ON users, user_settings, rooms TO cloud_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON user_permissions TO cloud_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON auth_sessions, recovery_codes TO cloud_app")
    op.execute("GRANT SELECT, INSERT ON login_attempts, audit_log TO cloud_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON client_codes, appointments, conflicts TO cloud_app")
    op.execute("GRANT USAGE ON SEQUENCE change_seq TO cloud_app")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for fn in ("appointments_set_ends_at()", "assign_change_seq()", "set_updated_at()"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    op.execute("DROP SEQUENCE IF EXISTS change_seq")
    op.execute("REVOKE USAGE ON SCHEMA app FROM cloud_app")
