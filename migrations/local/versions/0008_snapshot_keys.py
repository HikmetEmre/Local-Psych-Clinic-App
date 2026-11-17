"""snapshot keys: each therapist's key for the daily encrypted Drive snapshot

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17

LOCAL tier. The therapist picks a passphrase; we store only the PBKDF2-derived AES key,
itself Fernet-encrypted. The 17:00 job encrypts that therapist's snapshot with it; the
snapshot file asks for the passphrase in the browser and derives the same key there.
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE snapshot_keys (
            user_id uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
            salt bytea NOT NULL CHECK (length(salt) = 16),
            iterations integer NOT NULL CHECK (iterations >= 100000),
            key_enc bytea NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE snapshot_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE snapshot_keys FORCE ROW LEVEL SECURITY")
    # Each therapist manages only their own key.
    op.execute(
        "CREATE POLICY snapshot_keys_own ON snapshot_keys FOR ALL TO kasa_app "
        "USING (user_id = app_current_user_id()) WITH CHECK (user_id = app_current_user_id())"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON snapshot_keys TO kasa_app")
    # The jobs service (as kasa_backup) builds the snapshots and records that it did.
    op.execute("GRANT INSERT ON audit_log TO kasa_backup")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON audit_log FROM kasa_backup")
    op.execute("DROP TABLE IF EXISTS snapshot_keys")
