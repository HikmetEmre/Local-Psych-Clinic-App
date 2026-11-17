"""Ayarlar → Katalog: the clinic edits its therapy catalog without code changes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from support.local import Kasa, LocalDb, add_therapists, fetch, scalar

pytestmark = pytest.mark.integration


@pytest.fixture
def kasas(local_db: LocalDb) -> Iterator[tuple[Kasa, Kasa]]:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as a, Kasa(local_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        yield a, b


def ids(db: LocalDb, table: str, code: str) -> int:
    return int(scalar(db, f"SELECT id FROM {table} WHERE code = $1", code))


def defaults(db: LocalDb, group: str) -> list[str]:
    rows = fetch(
        db,
        "SELECT m.code FROM age_group_therapy_defaults d "
        "JOIN age_groups g ON g.id = d.age_group_id "
        "JOIN therapy_methods m ON m.id = d.therapy_method_id "
        "WHERE g.code = $1 ORDER BY d.sort_order",
        group,
    )
    return [r["code"] for r in rows]


def test_only_catalog_admins_reach_the_page(local_db: LocalDb, kasas: tuple[Kasa, Kasa]) -> None:
    a, b = kasas
    page = a.get("/ayarlar/katalog")
    assert page.status_code == 200
    assert "Yaş grupları" in page.text and "Oyun Terapisi" in page.text
    assert b.get("/ayarlar/katalog").status_code == 403
    assert (
        b.post(
            "/ayarlar/katalog/yontemler", {"code": "x_y", "label_tr": "X", "kind": "modality"}
        ).status_code
        == 403
    )
    assert scalar(local_db, "SELECT count(*) FROM therapy_methods WHERE code = 'x_y'") == 0


def test_add_and_deactivate_a_method(local_db: LocalDb, kasas: tuple[Kasa, Kasa]) -> None:
    a, _ = kasas
    added = a.post(
        "/ayarlar/katalog/yontemler",
        {"code": "narrative_therapy", "label_tr": "Anlatı Terapisi", "kind": "modality"},
    )
    assert added.status_code == 303
    methods = a.api("GET", "/api/therapy/methods").json()["modalities"]["others"]
    assert "narrative_therapy" in {m["code"] for m in methods}

    method_id = ids(local_db, "therapy_methods", "narrative_therapy")
    off = a.post(f"/ayarlar/katalog/yontemler/{method_id}", {"label_tr": "Anlatı Terapisi"})
    assert off.status_code == 303
    methods = a.api("GET", "/api/therapy/methods").json()["modalities"]["others"]
    assert "narrative_therapy" not in {m["code"] for m in methods}
    actions = [
        r["action"]
        for r in fetch(local_db, "SELECT action FROM audit_log WHERE action LIKE 'catalog_%'")
    ]
    assert actions == ["catalog_method_created", "catalog_method_updated"]


@pytest.mark.parametrize(
    "form",
    [
        {"code": "Has Spaces", "label_tr": "X", "kind": "modality"},
        {"code": "ok_code", "label_tr": "", "kind": "modality"},
        {"code": "ok_code", "label_tr": "X", "kind": "diagnosis"},
        {"code": "emdr", "label_tr": "EMDR 2", "kind": "modality"},  # duplicate code
    ],
)
def test_bad_new_methods_are_refused(
    local_db: LocalDb, kasas: tuple[Kasa, Kasa], form: dict[str, Any]
) -> None:
    a, _ = kasas
    before = scalar(local_db, "SELECT count(*) FROM therapy_methods")
    response = a.post("/ayarlar/katalog/yontemler", form)
    assert response.status_code in (409, 422)
    assert scalar(local_db, "SELECT count(*) FROM therapy_methods") == before


def test_edit_the_defaults_matrix(local_db: LocalDb, kasas: tuple[Kasa, Kasa]) -> None:
    a, _ = kasas
    child = ids(local_db, "age_groups", "child")
    current = [
        f"{r['age_group_id']}:{r['therapy_method_id']}"
        for r in fetch(local_db, "SELECT * FROM age_group_therapy_defaults")
    ]
    play = ids(local_db, "therapy_methods", "play_therapy")
    schema = ids(local_db, "therapy_methods", "schema_therapy")
    wanted = [d for d in current if d != f"{child}:{play}"] + [f"{child}:{schema}", "999:1", "junk"]
    assert a.post("/ayarlar/katalog/varsayilanlar", {"default": wanted}).status_code == 303
    after = defaults(local_db, "child")
    assert "play_therapy" not in after and after[-1] == "schema_therapy"
    assert defaults(local_db, "adult")[:2] == ["individual", "group"]  # untouched order
    chips = a.get("/terapiler?group=child").text.split('class="more"')[0]
    assert "Şema Terapi" in chips and "Oyun Terapisi" not in chips


def test_age_ranges_cannot_overlap(local_db: LocalDb, kasas: tuple[Kasa, Kasa]) -> None:
    a, _ = kasas
    child = ids(local_db, "age_groups", "child")
    clash = a.post(
        f"/ayarlar/katalog/yas-gruplari/{child}",
        {"label_tr": "Çocuk", "min_age": "0", "max_age": "14", "sort_order": "1", "is_active": "1"},
    )
    assert clash.status_code == 409 and "çakışıyor" in clash.text
    assert scalar(local_db, "SELECT max_age FROM age_groups WHERE code = 'child'") == 12

    # Narrowing first, then adding a new group in the gap, works.
    ok = a.post(
        f"/ayarlar/katalog/yas-gruplari/{child}",
        {"label_tr": "Çocuk", "min_age": "0", "max_age": "9", "sort_order": "0", "is_active": "1"},
    )
    assert ok.status_code == 303
    added = a.post(
        "/ayarlar/katalog/yas-gruplari",
        {
            "code": "tween",
            "label_tr": "Ön Ergen",
            "min_age": "10",
            "max_age": "12",
            "sort_order": "1",
        },
    )
    assert added.status_code == 303
    tabs = [g["code"] for g in a.api("GET", "/api/therapy/age-groups").json()]
    assert tabs == ["child", "tween", "adolescent", "adult", "geriatric"]
