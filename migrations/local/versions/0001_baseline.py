"""baseline: extensions

Revision ID: 0001
Revises:
Create Date: 2026-11-16

LOCAL tier.
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Needed for the non-overlapping age-range constraint on age_groups (M3).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
