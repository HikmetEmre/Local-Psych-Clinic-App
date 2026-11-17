"""single app: scheduling and payments move into the office database

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-18

LOCAL tier. Decision 2026-11-18: one app on the office computer, two roles, no cloud.

- rooms, appointments and payments become local tables (they were cloud-owned);
  appointments reference clients by id (names live here now);
- no double booking: the same exclusion constraints the cloud used;
- the read-only mirror and the cloud sync tables are dropped; their appointments and
  rooms are carried over first;
- every table the admin laptop copy will sync gets updated_at + a commit-ordered
  change_seq; schedule changes also NOTIFY open calendars.
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

REPLICATED = (
    "users",
    "clients",
    "age_groups",
    "therapy_methods",
    "therapy_sessions",
    "rooms",
    "appointments",
    "payments",
)
SLOT = "tstzrange(starts_at, ends_at)"
HOLDS_SLOT = "status IN ('planned', 'completed') AND deleted_at IS NULL"


def upgrade() -> None:
    # ------------------------------------------------------------------ change feed
    op.execute("CREATE SEQUENCE change_seq")
    op.execute(
        """
        CREATE FUNCTION assign_change_seq() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            -- Held until commit: change numbers are handed out in commit order, so a
            -- reader asking for "everything after N" can never miss a row.
            PERFORM pg_advisory_xact_lock(4242001);
            NEW.change_seq := nextval('change_seq');
            IF TG_TABLE_NAME IN ('appointments', 'rooms') THEN
                PERFORM pg_notify('schedule_changed', TG_TABLE_NAME);
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    for table in ("age_groups", "therapy_methods"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()")
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    # ------------------------------------------------------------------ rooms
    op.execute(
        """
        CREATE TABLE rooms (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            label varchar(40) NOT NULL CHECK (btrim(label) <> ''),
            is_active boolean NOT NULL DEFAULT true,
            sort_order smallint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "INSERT INTO rooms (id, label, is_active, sort_order) "
        "SELECT id, internal_label, is_active, sort_order FROM rooms_mirror"
    )
    op.execute("ALTER TABLE users ADD COLUMN default_room_id uuid REFERENCES rooms")

    # ------------------------------------------------------------------ appointments
    op.execute(
        """
        CREATE TABLE appointments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id uuid NOT NULL REFERENCES clients,
            therapist_id uuid NOT NULL REFERENCES users,
            room_id uuid NOT NULL REFERENCES rooms,
            starts_at timestamptz NOT NULL,
            duration_min smallint NOT NULL CHECK (duration_min BETWEEN 10 AND 240),
            -- Kept by a trigger: "starts_at + interval" is not IMMUTABLE for timestamptz,
            -- so it cannot appear in an exclusion constraint directly.
            ends_at timestamptz NOT NULL,
            status text NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'completed', 'cancelled', 'no_show')),
            version integer NOT NULL DEFAULT 1,
            created_by uuid REFERENCES users,
            updated_by uuid REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
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
    # Carry over what the mirror held (in development: the appointments made so far).
    op.execute(
        """
        INSERT INTO appointments (id, client_id, therapist_id, room_id, starts_at, duration_min,
                                  ends_at, status, version, created_at, updated_at, deleted_at)
        SELECT m.id, c.id, m.therapist_id, m.room_id, m.starts_at, m.duration_min,
               m.starts_at, m.status, m.version, m.updated_at, m.updated_at, m.deleted_at
        FROM appointments_mirror m
        JOIN clients c ON c.client_code = m.client_code
        JOIN users u ON u.id = m.therapist_id
        JOIN rooms r ON r.id = m.room_id
        """
    )
    for name, column in (
        ("no_overlap_therapist", "therapist_id"),
        ("no_overlap_room", "room_id"),
        ("no_overlap_client", "client_id"),
    ):
        op.execute(
            f"ALTER TABLE appointments ADD CONSTRAINT {name} EXCLUDE USING gist "
            f"({column} WITH =, {SLOT} WITH &&) WHERE ({HOLDS_SLOT})"
        )
    op.execute("CREATE INDEX appointments_start_idx ON appointments (starts_at)")
    op.execute(
        "CREATE INDEX appointments_therapist_start_idx ON appointments (therapist_id, starts_at)"
    )
    op.execute("CREATE INDEX appointments_client_start_idx ON appointments (client_id, starts_at)")

    # Sessions now point at real appointments.
    op.execute(
        "UPDATE therapy_sessions s SET appointment_id = NULL WHERE appointment_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM appointments a WHERE a.id = s.appointment_id)"
    )
    op.execute(
        "ALTER TABLE therapy_sessions ADD CONSTRAINT therapy_sessions_appointment_fk "
        "FOREIGN KEY (appointment_id) REFERENCES appointments"
    )

    # ------------------------------------------------------------------ payments
    op.execute(
        """
        CREATE TABLE payments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            appointment_id uuid NOT NULL REFERENCES appointments,
            amount numeric(10, 2) NOT NULL CHECK (amount > 0 AND amount <= 1000000),
            currency text NOT NULL DEFAULT 'TRY' CHECK (currency IN ('TRY')),
            method text NOT NULL CHECK (method IN ('cash', 'card', 'transfer')),
            paid_at timestamptz NOT NULL,
            recorded_by uuid NOT NULL REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            voided_at timestamptz,
            voided_by uuid REFERENCES users,
            void_reason text CHECK (void_reason IN ('entry_error', 'refund', 'duplicate')),
            CHECK ((voided_at IS NULL) = (voided_by IS NULL)),
            CHECK ((voided_at IS NULL) = (void_reason IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX payments_paid_at_idx ON payments (paid_at)")
    op.execute("CREATE INDEX payments_appointment_idx ON payments (appointment_id)")

    # ------------------------------------------------------------------ change feed on everything replicated
    for table in REPLICATED:
        op.execute(f"ALTER TABLE {table} ADD COLUMN change_seq bigint")
        op.execute(f"UPDATE {table} SET change_seq = nextval('change_seq')")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN change_seq SET NOT NULL")
        op.execute(f"CREATE UNIQUE INDEX {table}_change_seq_idx ON {table} (change_seq)")
        op.execute(
            f"CREATE TRIGGER {table}_change_seq BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION assign_change_seq()"
        )
    for table in ("rooms", "appointments", "payments"):
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    # ------------------------------------------------------------------ row-level security
    for table in ("rooms", "appointments", "payments"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY rooms_read ON rooms FOR SELECT TO kasa_app "
        "USING (app_current_user_id() IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY rooms_write ON rooms FOR ALL TO kasa_app "
        "USING (app_has_permission('admin.catalog')) WITH CHECK (app_has_permission('admin.catalog'))"
    )
    op.execute(
        "CREATE POLICY appointments_read ON appointments FOR SELECT TO kasa_app "
        "USING (app_has_permission('schedule.read_all'))"
    )
    op.execute(
        "CREATE POLICY appointments_insert ON appointments FOR INSERT TO kasa_app "
        "WITH CHECK (app_has_permission('schedule.write_all'))"
    )
    # Recording a session outcome updates the appointment's status, so a therapist may
    # also update their own appointments.
    op.execute(
        "CREATE POLICY appointments_update ON appointments FOR UPDATE TO kasa_app "
        "USING (app_has_permission('schedule.write_all') OR therapist_id = app_current_user_id()) "
        "WITH CHECK (app_has_permission('schedule.write_all') OR therapist_id = app_current_user_id())"
    )
    # Income visibility (all vs own) is applied in the queries; the database refuses
    # payments to anyone without any income or payment right.
    op.execute(
        "CREATE POLICY payments_read ON payments FOR SELECT TO kasa_app USING ("
        "app_has_permission('payments.write') OR app_has_permission('income.read_all') "
        "OR app_has_permission('income.read_own'))"
    )
    op.execute(
        "CREATE POLICY payments_insert ON payments FOR INSERT TO kasa_app "
        "WITH CHECK (app_has_permission('payments.write'))"
    )
    op.execute(
        "CREATE POLICY payments_update ON payments FOR UPDATE TO kasa_app "
        "USING (app_has_permission('payments.write')) WITH CHECK (app_has_permission('payments.write'))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON rooms, appointments TO kasa_app")
    op.execute("GRANT SELECT, INSERT ON payments TO kasa_app")
    op.execute("GRANT UPDATE (voided_at, voided_by, void_reason, updated_at) ON payments TO kasa_app")
    op.execute("GRANT USAGE ON SEQUENCE change_seq TO kasa_app")

    # ------------------------------------------------------------------ the cloud sync is gone
    op.execute("DROP FUNCTION IF EXISTS release_held_outbox(uuid)")
    op.execute("DROP FUNCTION IF EXISTS reconcile_client_codes(text[])")
    for table in ("sync_conflicts", "sync_state", "appointments_mirror", "rooms_mirror", "sync_outbox"):
        op.execute(f"DROP TABLE IF EXISTS {table}")


def downgrade() -> None:
    # Data written after 0009 (local bookings, payments) does not survive a downgrade.
    for table in REPLICATED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_change_seq ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS change_seq")
    op.execute(
        "ALTER TABLE therapy_sessions DROP CONSTRAINT IF EXISTS therapy_sessions_appointment_fk"
    )
    op.execute("DROP TABLE IF EXISTS payments")
    op.execute("DROP TABLE IF EXISTS appointments")
    op.execute("DROP FUNCTION IF EXISTS appointments_set_ends_at()")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS default_room_id")
    op.execute("DROP TABLE IF EXISTS rooms")
    for table in ("age_groups", "therapy_methods"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON {table}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS updated_at")
    op.execute("DROP FUNCTION IF EXISTS assign_change_seq()")
    op.execute("DROP SEQUENCE IF EXISTS change_seq")
    # The outbox as revision 0002 made it, then the sync tables as revision 0006 made them.
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
    op.execute("GRANT SELECT, INSERT ON sync_outbox TO kasa_app")
    op.execute("GRANT SELECT, UPDATE ON sync_outbox TO kasa_sync")
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "rev0006", Path(__file__).with_name("0006_sync.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()
