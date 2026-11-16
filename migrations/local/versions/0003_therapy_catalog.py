"""therapy catalog: age_groups, therapy_methods, age_group_therapy_defaults

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17

LOCAL tier. TERAPILER_TAB §3.2. RLS is enabled in the same migration as each table.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CATALOG_TABLES = ("age_groups", "therapy_methods", "age_group_therapy_defaults")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE age_groups (
            id smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code text NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9_]{1,30}$'),
            label_tr text NOT NULL CHECK (btrim(label_tr) <> ''),
            min_age smallint NOT NULL CHECK (min_age >= 0),
            max_age smallint CHECK (max_age IS NULL OR max_age >= min_age),
            sort_order smallint NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            -- Age ranges must not overlap (both bounds inclusive; NULL = no upper bound).
            CONSTRAINT age_groups_no_overlap EXCLUDE USING gist (
                int4range(min_age, coalesce(max_age, 200), '[]') WITH &&
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE therapy_methods (
            id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            code text NOT NULL UNIQUE CHECK (code ~ '^[a-z][a-z0-9_]{1,40}$'),
            label_tr text NOT NULL CHECK (btrim(label_tr) <> ''),
            kind text NOT NULL CHECK (kind IN ('modality', 'format')),
            description_tr text,
            -- Soft-disable only: history references these rows.
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE age_group_therapy_defaults (
            age_group_id smallint NOT NULL REFERENCES age_groups ON DELETE CASCADE,
            therapy_method_id integer NOT NULL REFERENCES therapy_methods ON DELETE CASCADE,
            sort_order smallint NOT NULL DEFAULT 0,
            PRIMARY KEY (age_group_id, therapy_method_id)
        )
        """
    )

    # A method can never change kind: sessions rely on format/modality staying what they were.
    op.execute(
        """
        CREATE FUNCTION therapy_methods_kind_is_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.kind IS DISTINCT FROM OLD.kind THEN
                RAISE EXCEPTION 'therapy_methods.kind cannot be changed (method %)', OLD.code
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER therapy_methods_kind_immutable BEFORE UPDATE ON therapy_methods "
        "FOR EACH ROW EXECUTE FUNCTION therapy_methods_kind_is_immutable()"
    )

    for table in CATALOG_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        # Any logged-in user reads the catalog; only admin.catalog changes it.
        op.execute(
            f"CREATE POLICY {table}_read ON {table} FOR SELECT TO kasa_app "
            "USING (app_current_user_id() IS NOT NULL)"
        )
        op.execute(
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT TO kasa_app "
            "WITH CHECK (app_has_permission('admin.catalog'))"
        )
        op.execute(
            f"CREATE POLICY {table}_update ON {table} FOR UPDATE TO kasa_app "
            "USING (app_has_permission('admin.catalog')) "
            "WITH CHECK (app_has_permission('admin.catalog'))"
        )
    op.execute(
        "CREATE POLICY age_group_therapy_defaults_delete ON age_group_therapy_defaults "
        "FOR DELETE TO kasa_app USING (app_has_permission('admin.catalog'))"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON age_groups, therapy_methods TO kasa_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON age_group_therapy_defaults TO kasa_app")


def downgrade() -> None:
    for table in reversed(CATALOG_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS therapy_methods_kind_is_immutable()")
