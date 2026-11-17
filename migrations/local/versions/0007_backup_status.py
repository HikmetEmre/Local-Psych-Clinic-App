"""backup status, written by the backup job and shown in Ayarlar → Yedekleme

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17

LOCAL tier.
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE backup_status (
            key text PRIMARY KEY CHECK (key ~ '^[a-z_]{2,40}$'),
            value text NOT NULL CHECK (length(value) <= 300),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON backup_status TO kasa_backup")
    op.execute("GRANT SELECT ON backup_status TO kasa_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS backup_status")
