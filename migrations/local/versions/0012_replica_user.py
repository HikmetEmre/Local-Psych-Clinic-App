"""each synced copy belongs to one user

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

LOCAL tier. Decision 2026-11-18: the owner's laptop is the main computer; the employee
therapist's laptop is a synced copy. A copy now belongs to one user and receives only
what that user may see (the calendar, all clients, her own sessions, notes and income;
no colleague's notes, no expenses, no other user's password). A copy without a user
(made before this) receives everything, as before.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE replicas ADD COLUMN user_id uuid "
        "REFERENCES users DEFERRABLE INITIALLY IMMEDIATE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE replicas DROP COLUMN user_id")
