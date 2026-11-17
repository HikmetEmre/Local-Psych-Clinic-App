"""sync: read-only mirror of the cloud schedule, sync state, local conflicts

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17

LOCAL tier. Only the sync worker (kasa_sync) writes the mirror; the web app reads it.
The mirror holds what the cloud holds: client codes, never names.
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE rooms_mirror (
            id uuid PRIMARY KEY,
            internal_label varchar(30) NOT NULL,
            is_active boolean NOT NULL,
            sort_order smallint NOT NULL,
            change_seq bigint NOT NULL,
            synced_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # No foreign key to clients: after restoring an old backup a code may be unknown here.
    op.execute(
        r"""
        CREATE TABLE appointments_mirror (
            id uuid PRIMARY KEY,
            client_code text NOT NULL CHECK (client_code ~ '^DN-\d{4}-\d{4}$'),
            therapist_id uuid NOT NULL,
            room_id uuid NOT NULL,
            starts_at timestamptz NOT NULL,
            duration_min smallint NOT NULL,
            status text NOT NULL
                CHECK (status IN ('planned', 'completed', 'cancelled', 'no_show')),
            version integer NOT NULL,
            change_seq bigint NOT NULL,
            deleted_at timestamptz,
            updated_at timestamptz NOT NULL,
            synced_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX appointments_mirror_start_idx ON appointments_mirror (starts_at)")
    op.execute(
        "CREATE INDEX appointments_mirror_code_idx ON appointments_mirror (client_code, starts_at)"
    )

    op.execute(
        """
        CREATE TABLE sync_state (
            key text PRIMARY KEY CHECK (key ~ '^[a-z_]{2,40}$'),
            value text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("INSERT INTO sync_state (key, value) VALUES ('cursor', '0')")

    op.execute(
        """
        CREATE TABLE sync_conflicts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            outbox_item_id uuid,
            kind text NOT NULL CHECK (kind IN
                ('sync_status', 'sync_unknown_appointment', 'sync_error', 'unknown_client_code')),
            appointment_id uuid,
            client_code text,
            reason_code text NOT NULL CHECK (reason_code ~ '^[a-z_]{2,40}$'),
            proposed_status text,
            cloud_status text,
            author_id uuid,
            status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
            resolution text CHECK (resolution IN ('discarded', 'reapplied')),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_by uuid,
            resolved_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX sync_conflicts_open_idx ON sync_conflicts (created_at) WHERE status = 'open'")

    # After a restore from an old backup the local code counter may be behind the cloud.
    # Raise it above every code the cloud knows so no code is ever handed out twice, and
    # report the codes this office has no client for. Runs as the owner: kasa_sync itself
    # cannot read clients.
    op.execute(
        r"""
        CREATE FUNCTION reconcile_client_codes(cloud_codes text[]) RETURNS text[]
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            missing text[];
        BEGIN
            INSERT INTO client_code_counters (year, last_n)
            SELECT substr(code, 4, 4)::int, max(substr(code, 9, 4)::int)
            FROM unnest(cloud_codes) AS code
            WHERE code ~ '^DN-\d{4}-\d{4}$'
            GROUP BY 1
            ON CONFLICT (year) DO UPDATE
                SET last_n = greatest(client_code_counters.last_n, EXCLUDED.last_n);

            SELECT coalesce(array_agg(code ORDER BY code), '{}') INTO missing
            FROM unnest(cloud_codes) AS code
            WHERE NOT EXISTS (SELECT 1 FROM clients c WHERE c.client_code = code);
            RETURN missing;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION reconcile_client_codes(text[]) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION reconcile_client_codes(text[]) TO kasa_sync")

    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON rooms_mirror, appointments_mirror, sync_state TO kasa_sync"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON sync_conflicts TO kasa_sync")
    op.execute("GRANT SELECT ON rooms_mirror, appointments_mirror, sync_state TO kasa_app")
    op.execute("GRANT SELECT, UPDATE ON sync_conflicts TO kasa_app")
    # After a person decides a conflict, items that waited behind it may go again.
    # The app gets exactly that (held -> pending), not general write access to the outbox.
    op.execute(
        """
        CREATE FUNCTION release_held_outbox(p_appointment uuid) RETURNS integer
        LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
            WITH released AS (
                UPDATE sync_outbox SET state = 'pending'
                WHERE state = 'held' AND payload->>'appointment_id' = p_appointment::text
                RETURNING 1
            )
            SELECT count(*)::int FROM released
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION release_held_outbox(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION release_held_outbox(uuid) TO kasa_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS release_held_outbox(uuid)")
    op.execute("DROP FUNCTION IF EXISTS reconcile_client_codes(text[])")
    for table in ("sync_conflicts", "sync_state", "appointments_mirror", "rooms_mirror"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
