"""seed the default therapy catalog (idempotent)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17

LOCAL tier. TERAPILER_TAB §4. A starting point for the clinic, editable in the DB
without code changes. Re-running updates labels and adds missing rows; it never
removes anything the clinic has added.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

AGE_GROUPS = (
    ("child", "Çocuk", 0, 12, 1),
    ("adolescent", "Ergen", 13, 17, 2),
    ("adult", "Yetişkin", 18, 64, 3),
    ("geriatric", "Geriatri (65+)", 65, None, 4),
)

ALL = ("child", "adolescent", "adult", "geriatric")

# (code, label, age groups it is a default for)
FORMATS = (
    ("individual", "Bireysel Terapi", ALL),
    ("group", "Grup Terapisi", ALL),
    ("family", "Aile Terapisi", ALL),
    ("couple", "Çift Terapisi", ("adult", "geriatric")),
    ("parent_session", "Ebeveyn Görüşmesi", ("child", "adolescent")),
)

MODALITIES = (
    ("play_therapy", "Oyun Terapisi", ("child",)),
    ("sandtray", "Kum Tepsisi Terapisi", ("child", "adolescent")),
    ("art_therapy", "Sanat Terapisi", ("child", "adolescent", "adult")),
    ("emdr", "EMDR", ALL),
    ("cbt", "Bilişsel Davranışçı Terapi (BDT)", ALL),
    ("dbt", "Diyalektik Davranış Terapisi (DBT)", ("adolescent", "adult")),
    ("act", "Kabul ve Kararlılık Terapisi (ACT)", ("adolescent", "adult", "geriatric")),
    ("motivational_interviewing", "Motivasyonel Görüşme", ("adolescent", "adult")),
    ("schema_therapy", "Şema Terapi", ("adult",)),
    ("psychodynamic", "Psikodinamik Terapi", ("adult", "geriatric")),
    ("ipt", "Kişilerarası Terapi (IPT)", ("adolescent", "adult", "geriatric")),
    ("mbct", "Bilinçli Farkındalık Temelli BDT (MBCT)", ("adult", "geriatric")),
    ("gestalt", "Gestalt Terapi", ("adult",)),
    ("existential", "Varoluşçu Terapi", ("adult", "geriatric")),
    ("reminiscence", "Anı / Yaşam Gözden Geçirme Terapisi", ("geriatric",)),
    ("supportive", "Destekleyici Terapi", ALL),
)

# The spec fixes the order for Çocuk (TERAPILER_TAB §4.3). Other groups follow table order.
CHILD_ORDER = (
    "play_therapy",
    "individual",
    "family",
    "group",
    "emdr",
    "parent_session",
    "cbt",
    "sandtray",
    "art_therapy",
    "supportive",
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _defaults() -> list[tuple[str, str, int]]:
    rows: list[tuple[str, str, int]] = []
    for group in ALL:
        if group == "child":
            ordered = list(CHILD_ORDER)
        else:
            ordered = [code for code, _, groups in (*FORMATS, *MODALITIES) if group in groups]
        rows.extend((group, code, position) for position, code in enumerate(ordered, start=1))
    return rows


def upgrade() -> None:
    groups = ", ".join(
        f"({_quote(code)}, {_quote(label)}, {lo}, {'NULL' if hi is None else hi}, {order})"
        for code, label, lo, hi, order in AGE_GROUPS
    )
    op.execute(
        f"""
        INSERT INTO age_groups (code, label_tr, min_age, max_age, sort_order) VALUES {groups}
        ON CONFLICT (code) DO UPDATE SET label_tr = EXCLUDED.label_tr
        """
    )

    methods = ", ".join(
        f"({_quote(code)}, {_quote(label)}, {_quote(kind)})"
        for kind, items in (("format", FORMATS), ("modality", MODALITIES))
        for code, label, _ in items
    )
    op.execute(
        f"""
        INSERT INTO therapy_methods (code, label_tr, kind) VALUES {methods}
        ON CONFLICT (code) DO UPDATE SET label_tr = EXCLUDED.label_tr
        """
    )

    defaults = ", ".join(
        f"({_quote(group)}, {_quote(code)}, {order})" for group, code, order in _defaults()
    )
    op.execute(
        f"""
        INSERT INTO age_group_therapy_defaults (age_group_id, therapy_method_id, sort_order)
        SELECT ag.id, tm.id, x.ord
        FROM (VALUES {defaults}) AS x(group_code, method_code, ord)
        JOIN age_groups ag ON ag.code = x.group_code
        JOIN therapy_methods tm ON tm.code = x.method_code
        ON CONFLICT (age_group_id, therapy_method_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # Seed data only: rows the clinic added or edited are kept, so nothing is deleted here.
    pass
