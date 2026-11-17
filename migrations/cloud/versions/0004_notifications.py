"""notifications (outbox + inbox) and Web Push subscriptions

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17

CLOUD tier, schema "app". Brief §8.

A notification row is written in the same transaction as the change it reports, so
it cannot get lost. The same table is the user's inbox. ``payload`` holds typed values
only (ids, client code, times, changed field names); the text is rendered on delivery
and never contains a name or anything clinical.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

EVENTS = (
    "appointment.created",
    "appointment.updated",
    "appointment.cancelled",
    "appointment.reminder_24h",
    "appointment.reminder_1h",
    "conflict.created",
)


def upgrade() -> None:
    events = ", ".join(f"'{e}'" for e in EVENTS)
    op.execute(
        f"""
        CREATE TABLE notifications (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            recipient_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
            event_type text NOT NULL CHECK (event_type IN ({events})),
            appointment_id uuid REFERENCES appointments ON DELETE SET NULL,
            payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            read_at timestamptz,
            push_state text NOT NULL DEFAULT 'pending'
                CHECK (push_state IN ('pending', 'sent', 'failed', 'skipped')),
            -- Not before this moment (end of the recipient's quiet hours).
            push_after timestamptz NOT NULL DEFAULT now(),
            next_attempt_at timestamptz,
            pushed_at timestamptz,
            push_attempts integer NOT NULL DEFAULT 0,
            expires_at timestamptz,
            dedupe_key text UNIQUE CHECK (length(dedupe_key) <= 120)
        )
        """
    )
    op.execute(
        "CREATE INDEX notifications_inbox_idx ON notifications (recipient_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX notifications_unread_idx ON notifications (recipient_id) WHERE read_at IS NULL"
    )
    op.execute(
        "CREATE INDEX notifications_push_due_idx "
        "ON notifications (coalesce(next_attempt_at, push_after)) WHERE push_state = 'pending'"
    )

    op.execute(
        """
        CREATE TABLE push_subscriptions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
            -- The browser's push-service URL and keys (not personal data about clients).
            endpoint text NOT NULL UNIQUE CHECK (endpoint ~ '^https://' AND length(endpoint) <= 1000),
            p256dh text NOT NULL CHECK (length(p256dh) <= 200),
            auth text NOT NULL CHECK (length(auth) <= 100),
            created_at timestamptz NOT NULL DEFAULT now(),
            last_success_at timestamptz,
            failure_count integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("CREATE INDEX push_subscriptions_user_idx ON push_subscriptions (user_id)")

    for table in ("notifications", "push_subscriptions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_app ON {table} FOR ALL TO cloud_app "
            "USING (true) WITH CHECK (true)"
        )
    op.execute("GRANT SELECT, INSERT, UPDATE ON notifications TO cloud_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON push_subscriptions TO cloud_app")

    # Every schedule change also announces itself, delivered on commit: open calendars
    # (Server-Sent Events) and the office mirror refresh without polling.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assign_change_seq() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(4242001);
            NEW.change_seq := nextval('app.change_seq');
            PERFORM pg_notify('schedule_changed', TG_TABLE_NAME);
            RETURN NEW;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assign_change_seq() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(4242001);
            NEW.change_seq := nextval('app.change_seq');
            RETURN NEW;
        END
        $$
        """
    )
    op.execute("DROP TABLE IF EXISTS push_subscriptions")
    op.execute("DROP TABLE IF EXISTS notifications")
