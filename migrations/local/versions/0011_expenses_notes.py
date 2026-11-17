"""expenses (gider) and the per-client clinical notes log

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18

LOCAL tier.

expenses: the clinic's costs (rent, bills, salaries...). Admin only (income.read_all),
like the clinic's total income. Never edited: a wrong entry is voided with a reason.

client_notes: a clinician's log per client (phone calls, observations, intake,
assessments, parent meetings, supervision, risk notes...), next to the session records.
Text is Fernet-encrypted in the app. Visible to its author and to the admin
(sessions.read_others); only the author writes it.

note_revisions: append-only history. Every edit or deletion of a note, and every change
to a session's notes / homework, first stores the previous text here. Nothing a
clinician wrote is ever lost.

All three sync with the owner's laptop copy (change_seq, replica tracking).
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

DEFER = "DEFERRABLE INITIALLY IMMEDIATE"
NOTE_KINDS = (
    "general", "phone", "observation", "intake", "assessment", "parent", "supervision",
    "risk", "other",
)  # fmt: skip
EXPENSE_CATEGORIES = (
    "rent", "utilities", "salary", "tax", "accounting", "supplies", "training",
    "maintenance", "cleaning", "marketing", "other",
)  # fmt: skip
TABLES = ("expenses", "client_notes", "note_revisions")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE expenses (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            spent_on date NOT NULL,
            category text NOT NULL CHECK (category IN ({_in(EXPENSE_CATEGORIES)})),
            description varchar(200),
            amount numeric(12, 2) NOT NULL CHECK (amount > 0 AND amount <= 10000000),
            method text NOT NULL CHECK (method IN ('cash', 'card', 'transfer')),
            recorded_by uuid NOT NULL REFERENCES users {DEFER},
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            voided_at timestamptz,
            voided_by uuid REFERENCES users {DEFER},
            void_reason text CHECK (void_reason IN ('entry_error', 'refund', 'duplicate')),
            change_seq bigint NOT NULL DEFAULT 0,
            CHECK ((voided_at IS NULL) = (voided_by IS NULL)),
            CHECK ((voided_at IS NULL) = (void_reason IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX expenses_spent_on_idx ON expenses (spent_on)")

    op.execute(
        f"""
        CREATE TABLE client_notes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id uuid NOT NULL REFERENCES clients {DEFER},
            author_id uuid NOT NULL REFERENCES users {DEFER},
            session_id uuid REFERENCES therapy_sessions {DEFER},
            kind text NOT NULL CHECK (kind IN ({_in(NOTE_KINDS)})),
            title_encrypted bytea,
            body_encrypted bytea NOT NULL,
            noted_at timestamptz NOT NULL,
            important boolean NOT NULL DEFAULT false,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            change_seq bigint NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("CREATE INDEX client_notes_client_idx ON client_notes (client_id, noted_at)")
    op.execute("CREATE INDEX client_notes_author_idx ON client_notes (author_id)")

    op.execute(
        f"""
        CREATE TABLE note_revisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            note_id uuid REFERENCES client_notes {DEFER},
            session_id uuid REFERENCES therapy_sessions {DEFER},
            version integer NOT NULL,
            kind text,
            title_encrypted bytea,
            body_encrypted bytea,
            homework_encrypted bytea,
            important boolean,
            noted_at timestamptz,
            reason text NOT NULL CHECK (reason IN ('edited', 'deleted')),
            edited_by uuid NOT NULL REFERENCES users {DEFER},
            edited_at timestamptz NOT NULL DEFAULT now(),
            change_seq bigint NOT NULL DEFAULT 0,
            CHECK ((note_id IS NULL) <> (session_id IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX note_revisions_note_idx ON note_revisions (note_id, edited_at)")
    op.execute("CREATE INDEX note_revisions_session_idx ON note_revisions (session_id, edited_at)")

    for table in TABLES:
        op.execute(f"UPDATE {table} SET change_seq = nextval('change_seq')")
        op.execute(f"CREATE UNIQUE INDEX {table}_change_seq_idx ON {table} (change_seq)")
        op.execute(
            f"CREATE TRIGGER {table}_change_seq BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION assign_change_seq()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_replica_track AFTER INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION replica_track()"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for table in ("expenses", "client_notes"):
        op.execute(
            f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )

    # ---- expenses: the admin's
    op.execute(
        "CREATE POLICY expenses_all ON expenses FOR ALL TO kasa_app "
        "USING (app_has_permission('income.read_all')) "
        "WITH CHECK (app_has_permission('income.read_all'))"
    )
    op.execute("GRANT SELECT, INSERT ON expenses TO kasa_app")
    op.execute("GRANT UPDATE (voided_at, voided_by, void_reason, version, updated_at) ON expenses TO kasa_app")

    # ---- notes: author writes; author and admin read
    visible = (
        "(author_id = app_current_user_id() OR app_has_permission('sessions.read_others'))"
    )
    own = "(author_id = app_current_user_id() AND app_has_permission('sessions.write_own'))"
    op.execute(f"CREATE POLICY client_notes_read ON client_notes FOR SELECT TO kasa_app USING {visible}")
    op.execute(f"CREATE POLICY client_notes_insert ON client_notes FOR INSERT TO kasa_app WITH CHECK {own}")
    op.execute(
        f"CREATE POLICY client_notes_update ON client_notes FOR UPDATE TO kasa_app "
        f"USING {own} WITH CHECK {own}"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON client_notes TO kasa_app")

    # ---- revisions: readable with what they belong to; append-only
    op.execute(
        "CREATE POLICY note_revisions_read ON note_revisions FOR SELECT TO kasa_app USING ("
        "(note_id IS NOT NULL AND EXISTS (SELECT 1 FROM client_notes n WHERE n.id = note_id)) "
        "OR (session_id IS NOT NULL AND EXISTS "
        "    (SELECT 1 FROM therapy_sessions s WHERE s.id = session_id)))"
    )
    op.execute(
        "CREATE POLICY note_revisions_insert ON note_revisions FOR INSERT TO kasa_app WITH CHECK ("
        "edited_by = app_current_user_id() AND ("
        " (note_id IS NOT NULL AND EXISTS (SELECT 1 FROM client_notes n "
        "   WHERE n.id = note_id AND n.author_id = app_current_user_id())) "
        " OR (session_id IS NOT NULL AND EXISTS (SELECT 1 FROM therapy_sessions s "
        "   WHERE s.id = session_id AND s.therapist_id = app_current_user_id()))))"
    )
    op.execute("GRANT SELECT, INSERT ON note_revisions TO kasa_app")

    # ---- the laptop copy
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON {', '.join(TABLES)} TO kasa_sync")


def downgrade() -> None:
    for table in ("note_revisions", "client_notes", "expenses"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
