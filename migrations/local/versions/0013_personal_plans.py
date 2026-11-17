"""Ajandam: the clinic owner's personal plans

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-18

LOCAL tier. The owner's own diary next to her clinic appointments: meetings, training,
admin work, private appointments. Title, place and note are Fernet-encrypted in the app.
Only the plan's owner reads or writes it, and only with admin.users (the clinic owner);
row-level security enforces it. Plans stay on the main computer: they are not part of
the sync with connected computers.

Repeating plans are one row with a rule (daily / weekly / monthly until a date);
skip_dates holds the occurrences deleted one by one.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

CATEGORIES = ("personal", "meeting", "training", "admin", "travel", "other")


def upgrade() -> None:
    categories = ", ".join(f"'{c}'" for c in CATEGORIES)
    op.execute(
        f"""
        CREATE TABLE personal_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users DEFERRABLE INITIALLY IMMEDIATE,
            category text NOT NULL CHECK (category IN ({categories})),
            title_encrypted bytea NOT NULL,
            location_encrypted bytea,
            note_encrypted bytea,
            all_day boolean NOT NULL DEFAULT false,
            starts_at timestamptz NOT NULL,
            ends_at timestamptz NOT NULL,
            repeat text NOT NULL DEFAULT 'none'
                CHECK (repeat IN ('none', 'daily', 'weekly', 'monthly')),
            repeat_until date,
            skip_dates date[] NOT NULL DEFAULT '{{}}',
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            CHECK (ends_at > starts_at),
            CHECK (ends_at - starts_at <= interval '31 days'),
            CHECK ((repeat = 'none') = (repeat_until IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX personal_events_owner_idx ON personal_events (owner_id, starts_at)")
    op.execute(
        "CREATE TRIGGER personal_events_set_updated_at BEFORE UPDATE ON personal_events "
        "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
    )
    op.execute("ALTER TABLE personal_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE personal_events FORCE ROW LEVEL SECURITY")
    mine = "(owner_id = app_current_user_id() AND app_has_permission('admin.users'))"
    op.execute(
        f"CREATE POLICY personal_events_own ON personal_events FOR ALL TO kasa_app "
        f"USING {mine} WITH CHECK {mine}"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON personal_events TO kasa_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS personal_events")
