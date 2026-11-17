"""admin laptop copy: bookkeeping for two-way sync with the office

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-18

LOCAL tier. The same schema runs on the office computer (authoritative) and on the
owner's laptop (an offline-capable copy).

Office:  replicas (paired laptops, token hashes), replica_conflicts (what could not be
         applied automatically, for the admin to decide).
Laptop:  replica_state (mode, cursor, status), replica_outbox (rows edited here and not
         yet sent), replica_base (the office version each row was last pulled at).

The sync role kasa_sync applies rows on both sides, so it bypasses row-level security.
Foreign keys and the double-booking / session-number constraints become DEFERRABLE
(still checked immediately unless a sync transaction defers them to its commit).
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TRACKED = ("clients", "appointments", "therapy_sessions", "payments")
# A client's session counter moves with every new session: that is not a change to the
# client (it must not look like an edit, nor bump the client's change number).
CLIENT_COLUMNS = (
    "client_code, full_name_encrypted, phone_encrypted, email_encrypted, birth_date, "
    "assigned_therapist_id, is_active, version, deleted_at, created_by, updated_by"
)
SLOT = "tstzrange(starts_at, ends_at)"
HOLDS_SLOT = "status IN ('planned', 'completed') AND deleted_at IS NULL"
OVERLAPS = (
    ("no_overlap_therapist", "therapist_id"),
    ("no_overlap_room", "room_id"),
    ("no_overlap_client", "client_id"),
)


def _overlap_constraints(deferrable: bool) -> None:
    suffix = " DEFERRABLE INITIALLY IMMEDIATE" if deferrable else ""
    for name, column in OVERLAPS:
        op.execute(f"ALTER TABLE appointments DROP CONSTRAINT {name}")
        op.execute(
            f"ALTER TABLE appointments ADD CONSTRAINT {name} EXCLUDE USING gist "
            f"({column} WITH =, {SLOT} WITH &&) WHERE ({HOLDS_SLOT}){suffix}"
        )
    op.execute(
        "ALTER TABLE therapy_sessions DROP CONSTRAINT therapy_sessions_client_id_session_number_key"
    )
    op.execute(
        "ALTER TABLE therapy_sessions ADD CONSTRAINT therapy_sessions_client_id_session_number_key "
        f"UNIQUE (client_id, session_number){suffix}"
    )


def _foreign_keys(deferrable: bool) -> None:
    mode = "DEFERRABLE INITIALLY IMMEDIATE" if deferrable else "NOT DEFERRABLE"
    op.execute(
        f"""
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conrelid::regclass AS tbl, conname FROM pg_constraint
                WHERE contype = 'f' AND connamespace = 'public'::regnamespace
            LOOP
                EXECUTE format('ALTER TABLE %s ALTER CONSTRAINT %I {mode}', c.tbl, c.conname);
            END LOOP;
        END
        $$
        """
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE replica_state (
            key text PRIMARY KEY,
            value text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE replicas (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            label varchar(60) NOT NULL CHECK (btrim(label) <> ''),
            token_hash bytea NOT NULL UNIQUE,
            created_by uuid REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz,
            revoked_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE replica_outbox (
            tbl text NOT NULL,
            row_id uuid NOT NULL,
            gen bigint NOT NULL DEFAULT 1,
            queued_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tbl, row_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE replica_base (
            tbl text NOT NULL,
            row_id uuid NOT NULL,
            office_seq bigint NOT NULL,
            PRIMARY KEY (tbl, row_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE replica_conflicts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            replica_id uuid REFERENCES replicas,
            tbl text NOT NULL,
            row_id uuid NOT NULL,
            kind text NOT NULL
                CHECK (kind IN ('changed_on_both', 'overlap', 'rejected', 'renumbered')),
            message text NOT NULL,
            laptop_row jsonb,
            office_row jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            resolved_by uuid REFERENCES users,
            resolution text CHECK (resolution IN ('kept_office', 'applied_laptop', 'noted'))
        )
        """
    )
    op.execute(
        "CREATE INDEX replica_conflicts_open_idx ON replica_conflicts (created_at) "
        "WHERE resolved_at IS NULL"
    )

    # Rows edited on the laptop are queued for sending; rows written by the sync itself
    # (which sets klinik.applying) are not. On the office nothing is queued.
    op.execute(
        """
        CREATE FUNCTION replica_track() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            IF coalesce(current_setting('klinik.applying', true), '') <> 'on'
               AND EXISTS (SELECT 1 FROM replica_state WHERE key = 'mode' AND value = 'replica')
            THEN
                INSERT INTO replica_outbox (tbl, row_id) VALUES (TG_TABLE_NAME, NEW.id)
                ON CONFLICT (tbl, row_id)
                DO UPDATE SET gen = replica_outbox.gen + 1, queued_at = now();
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION replica_track() FROM PUBLIC")
    for table in TRACKED:
        columns = f" OF {CLIENT_COLUMNS}" if table == "clients" else ""
        op.execute(
            f"CREATE TRIGGER {table}_replica_track AFTER INSERT OR UPDATE{columns} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION replica_track()"
        )
    op.execute("DROP TRIGGER clients_change_seq ON clients")
    op.execute(
        f"CREATE TRIGGER clients_change_seq BEFORE INSERT OR UPDATE OF {CLIENT_COLUMNS} "
        "ON clients FOR EACH ROW EXECUTE FUNCTION assign_change_seq()"
    )

    _overlap_constraints(deferrable=True)
    _foreign_keys(deferrable=True)

    # The app reads the mode and the status line; the sync role does the rest.
    op.execute("GRANT SELECT ON replica_state TO kasa_app")
    op.execute("GRANT SELECT ON replica_outbox TO kasa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON replicas TO kasa_app")
    op.execute("GRANT SELECT, UPDATE ON replica_conflicts TO kasa_app")
    op.execute("ALTER ROLE kasa_sync BYPASSRLS")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO kasa_sync")
    # Never needed by the sync: who is logged in where, and the snapshot keys.
    op.execute(
        "REVOKE SELECT ON auth_sessions, login_attempts, snapshot_keys, backup_status FROM kasa_sync"
    )
    op.execute(
        "GRANT INSERT, UPDATE ON clients, appointments, therapy_sessions, payments, users, "
        "rooms, age_groups, therapy_methods, recovery_codes TO kasa_sync"
    )
    op.execute(
        "GRANT INSERT, UPDATE, DELETE ON session_methods, user_permissions, "
        "age_group_therapy_defaults, recovery_codes TO kasa_sync"
    )
    # A laptop row the office refused (e.g. a double booking) is removed on the laptop.
    op.execute("GRANT DELETE ON appointments, payments TO kasa_sync")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON replica_state, replica_outbox, replica_base, "
        "replica_conflicts, replicas TO kasa_sync"
    )
    op.execute("GRANT SELECT, INSERT ON audit_log TO kasa_sync")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO kasa_sync")
    op.execute("GRANT EXECUTE ON FUNCTION next_session_number(uuid) TO kasa_sync")


def downgrade() -> None:
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM kasa_sync")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM kasa_sync")
    op.execute("REVOKE EXECUTE ON FUNCTION next_session_number(uuid) FROM kasa_sync")
    op.execute("ALTER ROLE kasa_sync NOBYPASSRLS")
    _foreign_keys(deferrable=False)
    _overlap_constraints(deferrable=False)
    for table in TRACKED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_replica_track ON {table}")
    op.execute("DROP TRIGGER clients_change_seq ON clients")
    op.execute(
        "CREATE TRIGGER clients_change_seq BEFORE INSERT OR UPDATE ON clients "
        "FOR EACH ROW EXECUTE FUNCTION assign_change_seq()"
    )
    op.execute("DROP FUNCTION IF EXISTS replica_track()")
    for table in ("replica_conflicts", "replica_base", "replica_outbox", "replicas", "replica_state"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
