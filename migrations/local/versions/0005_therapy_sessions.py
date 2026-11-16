"""therapy_sessions, session_methods: the clinical log

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

LOCAL tier. TERAPILER_TAB §3.2-3.4, with the access rules of the brief (§5):
a session is visible and writable only to its therapist, unless the reader holds
sessions.read_others. Enforced here by row-level security as a second layer.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

MINE = "therapist_id = app_current_user_id()"
CAN_READ = (
    f"(({MINE} AND app_has_permission('sessions.read_own')) "
    "OR app_has_permission('sessions.read_others'))"
)
CAN_WRITE = f"({MINE} AND app_has_permission('sessions.write_own'))"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE therapy_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id uuid NOT NULL REFERENCES clients,
            therapist_id uuid NOT NULL REFERENCES users,
            -- Snapshot: a child who turns 13 keeps their past sessions under Çocuk.
            age_group_id_at_session smallint NOT NULL REFERENCES age_groups,
            format_method_id integer NOT NULL REFERENCES therapy_methods,
            session_number integer NOT NULL CHECK (session_number >= 1),
            session_date timestamptz NOT NULL,
            duration_min smallint NOT NULL DEFAULT 50 CHECK (duration_min BETWEEN 10 AND 240),
            status text NOT NULL
                CHECK (status IN ('planned', 'completed', 'cancelled', 'no_show')),
            notes_encrypted bytea,
            homework_encrypted bytea,
            next_session_date timestamptz,
            -- The cloud appointment this session records (NULL for walk-ins).
            appointment_id uuid,
            version integer NOT NULL DEFAULT 1,
            created_by uuid REFERENCES users,
            updated_by uuid REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            UNIQUE (client_id, session_number)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX therapy_sessions_appointment_idx ON therapy_sessions (appointment_id) "
        "WHERE appointment_id IS NOT NULL AND deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX therapy_sessions_group_date_idx "
        "ON therapy_sessions (age_group_id_at_session, session_date DESC)"
    )
    op.execute(
        "CREATE INDEX therapy_sessions_client_date_idx "
        "ON therapy_sessions (client_id, session_date DESC)"
    )
    op.execute(
        "CREATE INDEX therapy_sessions_therapist_date_idx "
        "ON therapy_sessions (therapist_id, session_date DESC)"
    )
    op.execute(
        "CREATE TRIGGER therapy_sessions_set_updated_at BEFORE UPDATE ON therapy_sessions "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )

    op.execute(
        """
        CREATE TABLE session_methods (
            session_id uuid NOT NULL REFERENCES therapy_sessions ON DELETE CASCADE,
            therapy_method_id integer NOT NULL REFERENCES therapy_methods,
            is_primary boolean NOT NULL DEFAULT false,
            PRIMARY KEY (session_id, therapy_method_id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX session_methods_one_primary_idx ON session_methods (session_id) "
        "WHERE is_primary"
    )
    op.execute("CREATE INDEX session_methods_method_idx ON session_methods (therapy_method_id)")

    # ---- integrity rules 1 and 2 (TERAPILER_TAB §3.3): CHECK cannot look at another table.
    op.execute(
        """
        CREATE FUNCTION require_method_kind() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            wanted text := TG_ARGV[0];
            method_id integer;
            actual text;
        BEGIN
            IF wanted = 'format' THEN
                method_id := NEW.format_method_id;
            ELSE
                method_id := NEW.therapy_method_id;
            END IF;
            SELECT kind INTO actual FROM therapy_methods WHERE id = method_id;
            IF actual IS DISTINCT FROM wanted THEN
                RAISE EXCEPTION 'therapy method % has kind %, expected %', method_id, actual, wanted
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER therapy_sessions_format_kind "
        "BEFORE INSERT OR UPDATE OF format_method_id ON therapy_sessions "
        "FOR EACH ROW EXECUTE FUNCTION require_method_kind('format')"
    )
    op.execute(
        "CREATE TRIGGER session_methods_modality_kind "
        "BEFORE INSERT OR UPDATE OF therapy_method_id ON session_methods "
        "FOR EACH ROW EXECUTE FUNCTION require_method_kind('modality')"
    )

    # ---- per-client session numbers (rule 5).
    # A counter on the client row, not max()+1: under row-level security a therapist cannot
    # see a colleague's sessions, so max() would hand out the same number twice.
    # The row lock taken by UPDATE serialises concurrent inserts for one client.
    op.execute(
        """
        CREATE FUNCTION next_session_number(p_client_id uuid) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            n integer;
        BEGIN
            IF NOT app_has_permission('sessions.write_own') THEN
                RAISE EXCEPTION 'not allowed to create sessions'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            UPDATE clients SET session_counter = session_counter + 1
            WHERE id = p_client_id AND deleted_at IS NULL
            RETURNING session_counter INTO n;
            IF n IS NULL THEN
                RAISE EXCEPTION 'client % not found', p_client_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN n;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION next_session_number(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION next_session_number(uuid) TO kasa_app")

    # ---- row-level security
    for table in ("therapy_sessions", "session_methods"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        f"CREATE POLICY therapy_sessions_read ON therapy_sessions FOR SELECT TO kasa_app "
        f"USING {CAN_READ}"
    )
    op.execute(
        f"CREATE POLICY therapy_sessions_insert ON therapy_sessions FOR INSERT TO kasa_app "
        f"WITH CHECK {CAN_WRITE}"
    )
    op.execute(
        f"CREATE POLICY therapy_sessions_update ON therapy_sessions FOR UPDATE TO kasa_app "
        f"USING {CAN_WRITE} WITH CHECK {CAN_WRITE}"
    )

    # session_methods inherits visibility from its session: the subquery itself runs
    # under the policies above.
    visible = "EXISTS (SELECT 1 FROM therapy_sessions s WHERE s.id = session_id)"
    own = (
        "(app_has_permission('sessions.write_own') AND EXISTS ("
        "SELECT 1 FROM therapy_sessions s "
        "WHERE s.id = session_id AND s.therapist_id = app_current_user_id()))"
    )
    op.execute(
        f"CREATE POLICY session_methods_read ON session_methods FOR SELECT TO kasa_app "
        f"USING ({visible})"
    )
    op.execute(
        f"CREATE POLICY session_methods_insert ON session_methods FOR INSERT TO kasa_app "
        f"WITH CHECK {own}"
    )
    op.execute(
        f"CREATE POLICY session_methods_update ON session_methods FOR UPDATE TO kasa_app "
        f"USING {own} WITH CHECK {own}"
    )
    op.execute(
        f"CREATE POLICY session_methods_delete ON session_methods FOR DELETE TO kasa_app "
        f"USING {own}"
    )

    # No DELETE on therapy_sessions: soft delete only.
    op.execute("GRANT SELECT, INSERT, UPDATE ON therapy_sessions TO kasa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON session_methods TO kasa_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS session_methods CASCADE")
    op.execute("DROP TABLE IF EXISTS therapy_sessions CASCADE")
    op.execute("DROP FUNCTION IF EXISTS next_session_number(uuid)")
    op.execute("DROP FUNCTION IF EXISTS require_method_kind()")
