"""baseline: extensions

Revision ID: 0001
Revises:
Create Date: 2026-11-16

CLOUD tier. The private schema "app" is created by env.py before migrations run.
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Needed for the no-double-booking exclusion constraints (M4).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
