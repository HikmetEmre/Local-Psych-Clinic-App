"""payments

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17

CLOUD tier, schema "app". Brief §3: amount, method, date, client code, appointment.
No free text: a correction is a void (with a reason code) plus a new entry.
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE payments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            appointment_id uuid NOT NULL,
            client_code text NOT NULL CHECK (client_code ~ '^DN-\d{4}-\d{4}$'),
            amount numeric(10, 2) NOT NULL CHECK (amount > 0 AND amount <= 1000000),
            currency text NOT NULL DEFAULT 'TRY' CHECK (currency IN ('TRY')),
            method text NOT NULL CHECK (method IN ('cash', 'card', 'transfer')),
            paid_at timestamptz NOT NULL,
            recorded_by uuid NOT NULL REFERENCES users,
            created_at timestamptz NOT NULL DEFAULT now(),
            voided_at timestamptz,
            voided_by uuid REFERENCES users,
            void_reason text CHECK (void_reason IN ('entry_error', 'refund', 'duplicate')),
            -- The payment's client code must be the appointment's client code.
            FOREIGN KEY (appointment_id, client_code) REFERENCES appointments (id, client_code),
            CHECK ((voided_at IS NULL) = (voided_by IS NULL)),
            CHECK ((voided_at IS NULL) = (void_reason IS NULL))
        )
        """
    )
    op.execute("CREATE INDEX payments_paid_at_idx ON payments (paid_at)")
    op.execute("CREATE INDEX payments_appointment_idx ON payments (appointment_id)")
    op.execute("ALTER TABLE payments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE payments FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY payments_app ON payments FOR ALL TO cloud_app USING (true) WITH CHECK (true)"
    )
    op.execute("GRANT SELECT, INSERT ON payments TO cloud_app")
    # Only the void columns can ever change; amounts and dates cannot be edited.
    op.execute("GRANT UPDATE (voided_at, voided_by, void_reason) ON payments TO cloud_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS payments")
