"""sync: service tokens and the idempotency ledger

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17

CLOUD tier, schema "app".
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The sync worker's credential. Only a hash is stored; the token is shown once.
    op.execute(
        """
        CREATE TABLE service_tokens (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            token_hash bytea NOT NULL UNIQUE,
            scope text NOT NULL CHECK (scope IN ('sync')),
            created_at timestamptz NOT NULL DEFAULT now(),
            last_used_at timestamptz,
            revoked_at timestamptz
        )
        """
    )
    # Every item the office has pushed, with the answer it got. A repeated item_id
    # returns the stored answer instead of being applied twice.
    op.execute(
        """
        CREATE TABLE sync_inbox (
            item_id uuid PRIMARY KEY,
            kind text NOT NULL
                CHECK (kind IN ('client_code.upsert', 'appointment.outcome', 'conflict.resolved')),
            received_at timestamptz NOT NULL DEFAULT now(),
            result text NOT NULL CHECK (result IN ('applied', 'rejected')),
            reason_code text CHECK (reason_code ~ '^[a-z_]{2,40}$')
        )
        """
    )
    for table in ("service_tokens", "sync_inbox"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_app ON {table} FOR ALL TO cloud_app "
            "USING (true) WITH CHECK (true)"
        )
    op.execute("GRANT SELECT, INSERT, UPDATE ON service_tokens TO cloud_app")
    op.execute("GRANT SELECT, INSERT ON sync_inbox TO cloud_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sync_inbox")
    op.execute("DROP TABLE IF EXISTS service_tokens")
