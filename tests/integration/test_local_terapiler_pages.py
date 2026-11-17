"""Terapiler tab as a browser sees it (TERAPILER_TAB §6), on a real PostgreSQL."""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
from support.local import A_ID, Kasa, LocalDb, add_therapists, fetch, scalar

from klinik_local.danisanlar.service import today_istanbul
from klinik_shared.timeutil import ISTANBUL

pytestmark = pytest.mark.integration

NOTE = "Danışan kabuslarını anlattı; kum tepsisinde ev figürü kullandı."
HOMEWORK = "Her akşam duygu günlüğü."


def years_ago(years: int) -> str:
    today = today_istanbul()
    return (today.replace(year=today.year - years) - timedelta(days=100)).isoformat()


def local(days_from_now: int, hour: int = 14) -> str:
    moment = datetime.now(ISTANBUL).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (moment + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M")


@dataclass
class Office:
    db: LocalDb
    a: Kasa
    b: Kasa
    child: str
    adult: str
    no_birth: str


def make_client(kasa: Kasa, name: str, birth_date: str) -> str:
    response = kasa.post("/danisanlar", {"full_name": name, "birth_date": birth_date})
    assert response.status_code == 303, response.text
    return response.headers["location"].split("/")[2].split("?")[0]


@pytest.fixture
def office(local_db: LocalDb) -> Iterator[Office]:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as a, Kasa(local_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        yield Office(
            db=local_db,
            a=a,
            b=b,
            child=make_client(a, "Elif Kaya", years_ago(9)),
            adult=make_client(a, "Zeynep Arslan", years_ago(34)),
            no_birth=make_client(a, "Doğum Tarihsiz", ""),
        )


def save(kasa: Kasa, client_id: str, **overrides: Any) -> Any:
    form: dict[str, Any] = {
        "client_id": client_id,
        "session_date": local(-1),
        "duration_min": "50",
        "format_code": "individual",
        "method": ["cbt"],
        "primary": "cbt",
        "status": "completed",
        "notes": "",
        "homework": "",
        "next_session_date": "",
        **overrides,
    }
    return kasa.post("/terapiler", form)


def created_id(response: Any) -> str:
    assert response.status_code == 303, response.text
    return str(response.headers["location"].split("/")[2].split("?")[0])


# ---------------------------------------------------------------- list


def test_list_shows_tabs_from_the_database_with_counts(office: Office) -> None:
    created_id(save(office.a, office.child, method=["play_therapy"], primary="play_therapy"))
    created_id(save(office.a, office.adult))
    page = office.a.get("/terapiler")
    assert page.status_code == 200
    for label in ("Tümü", "Çocuk", "Ergen", "Yetişkin", "Geriatri (65+)"):
        assert label in page.text
    counts = re.findall(r'class="segment__count">(\d+)<', page.text)
    assert counts == ["2", "1", "0", "1", "0"]


def test_tab_shows_its_default_method_chips(office: Office) -> None:
    child = office.a.get("/terapiler?group=child").text
    assert 'value="play_therapy"' in child
    defaults_part = child.split('class="more"')[0]
    assert "Oyun Terapisi" in defaults_part and "Şema Terapi" not in defaults_part
    adult = office.a.get("/terapiler?group=adult").text.split('class="more"')[0]
    assert "Şema Terapi" in adult and "Oyun Terapisi" not in adult


def test_filters_and_sort_live_in_the_url(office: Office) -> None:
    created_id(save(office.a, office.child, method=["play_therapy"], primary="play_therapy"))
    created_id(save(office.a, office.adult, status="no_show"))
    url = "/terapiler?group=adult&status=no_show&sort=-duration_min&method=cbt"
    page = office.a.get(url).text
    assert page.count("<code") == 1 and "Gelmedi" in page
    # Switching tab keeps status and sort but drops the method filter.
    child_tab = re.search(r'href="([^"]*group=child[^"]*)"', page)
    assert child_tab
    href = child_tab.group(1).replace("&amp;", "&")
    assert "status=no_show" in href and "sort=-duration_min" in href and "method=" not in href
    # The active sort is marked and its link flips direction.
    assert "Süre ↓" in page


def test_empty_states(office: Office) -> None:
    assert "Henüz seans kaydı yok." in office.a.get("/terapiler").text
    assert "Bu yaş grubunda henüz seans kaydı yok." in office.a.get("/terapiler?group=adult").text
    created_id(save(office.a, office.adult))
    filtered = office.a.get("/terapiler?group=adult&status=cancelled").text
    assert "Filtrelere uyan kayıt bulunamadı." in filtered


def test_default_range_is_the_last_30_days(office: Office) -> None:
    created_id(save(office.a, office.adult, session_date=local(-45)))
    assert "Filtrelere uyan kayıt bulunamadı." in office.a.get("/terapiler").text
    assert office.a.get("/terapiler?date_from=").text.count("<code") == 1


def test_list_never_contains_note_text(office: Office) -> None:
    session = created_id(save(office.a, office.child, notes=NOTE, homework=HOMEWORK))
    pages = [
        office.a.get("/terapiler").text,
        office.a.get(f"/terapiler/zaman/{office.child}?satir={session}").text,
    ]
    for page in pages:
        assert "kabus" not in page and "günlüğü" not in page
    assert scalar(office.db, "SELECT count(*) FROM audit_log WHERE action = 'notes_read'") == 0


def test_client_name_appears_only_as_a_hover_title(office: Office) -> None:
    created_id(save(office.a, office.child))
    page = office.a.get("/terapiler").text
    assert 'title="Elif Kaya"' in page
    assert ">Elif Kaya<" not in page


def test_pagination_links(office: Office) -> None:
    for days in range(1, 13):
        created_id(save(office.a, office.adult, session_date=local(-days)))
    page = office.a.get("/terapiler?page_size=10").text
    assert "1–10 / 12" in page
    second = office.a.get("/terapiler?page_size=10&page=2").text
    assert "11–12 / 12" in second and second.count("<code") == 2


def test_timeline_row_spans_age_groups_and_can_close(office: Office) -> None:
    session = created_id(save(office.a, office.adult))
    row = office.a.get(f"/terapiler/zaman/{office.adult}?satir={session}")
    assert row.status_code == 200
    assert f'id="tl-{session}"' in row.text and "Son seanslar" in row.text
    assert f'href="/danisanlar/{office.adult}"' in row.text
    closed = office.a.get(f"/terapiler/zaman-kapat?satir={session}").text
    assert f'id="tl-{session}" hidden' in closed


# ---------------------------------------------------------------- new session form


def test_new_session_form_and_live_age_group(office: Office) -> None:
    form = office.a.get("/terapiler/yeni")
    assert form.status_code == 200
    assert "Elif Kaya" in form.text and "Danışan seçildiğinde yaş grubu" in form.text

    fragment = office.a.get(
        "/terapiler/form/yontemler",
        params={"client_id": office.child, "session_date": local(-1)},
    )
    assert fragment.status_code == 200
    assert "Yaş Grubu: Çocuk (9)" in fragment.text
    defaults = fragment.text.split("Diğer yöntemler")[0]
    assert "Oyun Terapisi" in defaults and "Şema Terapi" not in defaults
    assert "Ebeveyn Görüşmesi" in fragment.text and "Çift Terapisi" not in fragment.text

    missing = office.a.get("/terapiler/form/yontemler", params={"client_id": office.no_birth})
    assert "Danışanın doğum tarihi eksik" in missing.text


def test_choosing_a_non_default_method_warns_but_saves(office: Office) -> None:
    fragment = office.a.get(
        "/terapiler/form/yontemler",
        params={"client_id": office.child, "session_date": local(-1), "method": "schema_therapy"},
    )
    assert "varsayılanları arasında değil: Şema Terapi" in fragment.text
    response = save(office.a, office.child, method=["schema_therapy"], primary="schema_therapy")
    assert response.status_code == 303


def test_create_from_the_form(office: Office) -> None:
    response = save(
        office.a,
        office.child,
        format_code="family",
        method=["cbt", "play_therapy"],
        primary="play_therapy",
        notes=NOTE,
        homework=HOMEWORK,
        duration_min="45",
    )
    session = created_id(response)
    row = fetch(office.db, "SELECT * FROM therapy_sessions")[0]
    assert str(row["id"]) == session
    assert row["therapist_id"] == A_ID and row["duration_min"] == 45
    primary = scalar(
        office.db,
        "SELECT m.code FROM session_methods sm JOIN therapy_methods m "
        "ON m.id = sm.therapy_method_id WHERE sm.is_primary",
    )
    assert primary == "play_therapy"
    assert NOTE.encode() not in bytes(row["notes_encrypted"])
    # 14:00 typed in the clinic is 11:00 UTC.
    assert row["session_date"].astimezone(ISTANBUL).hour == 14


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"client_id": ""}, "Danışan seçin."),
        ({"session_date": ""}, "Tarih ve saat zorunludur."),
        ({"session_date": "dün öğleden sonra"}, "Tarih ve saat geçersiz."),
        ({"duration_min": "5"}, "Süre 10 ile 240 dakika arasında olmalı."),
        ({"method": [], "primary": ""}, "Tamamlanan seans için en az bir yöntem seçin."),
        ({"format_code": "emdr"}, "Geçersiz seans formatı."),
    ],
)
def test_form_errors_are_shown_and_nothing_is_saved(
    office: Office, overrides: dict[str, Any], message: str
) -> None:
    response = save(office.a, overrides.pop("client_id", office.adult), notes=NOTE, **overrides)
    assert response.status_code == 422
    assert message in response.text
    assert NOTE in response.text  # the typed note is not lost
    assert scalar(office.db, "SELECT count(*) FROM therapy_sessions") == 0


def test_missing_birth_date_blocks_saving(office: Office) -> None:
    response = save(office.a, office.no_birth)
    assert response.status_code == 422
    assert "Danışanın doğum tarihi eksik. Önce danışan profilini güncelleyin." in response.text


def test_the_live_refresh_never_carries_note_text(office: Office) -> None:
    form = office.a.get("/terapiler/yeni").text
    include = re.findall(r'hx-include="([^"]+)"', form)
    assert include and all("notes" not in i and "closest form" not in i for i in include)


# ---------------------------------------------------------------- detail, edit, delete


def test_detail_shows_notes_and_is_audited(office: Office) -> None:
    session = created_id(save(office.a, office.child, notes=NOTE, homework=HOMEWORK))
    page = office.a.get(f"/terapiler/{session}")
    assert page.status_code == 200
    assert NOTE in page.text and HOMEWORK in page.text and "Elif Kaya" in page.text
    assert page.headers["cache-control"] == "no-store"
    reads = fetch(office.db, "SELECT actor_id, entity_id FROM audit_log WHERE action='notes_read'")
    assert [(r["actor_id"], r["entity_id"]) for r in reads] == [(A_ID, session)]


def test_edit_keeps_unchanged_notes_untouched(office: Office) -> None:
    session = created_id(save(office.a, office.adult, notes=NOTE))
    before = bytes(scalar(office.db, "SELECT notes_encrypted FROM therapy_sessions"))
    form = office.a.get(f"/terapiler/{session}/duzenle").text
    assert NOTE in form

    def hidden(name: str) -> str:
        match = re.search(rf'name="{name}" value="([^"]*)"', form)
        assert match
        return match.group(1)

    response = office.a.post(
        f"/terapiler/{session}",
        {
            "client_id": office.adult,
            "session_date": local(-1),
            "duration_min": "70",
            "format_code": "individual",
            "method": ["cbt"],
            "primary": "cbt",
            "status": "completed",
            "notes": NOTE.replace("\n", "\r\n"),
            "homework": "",
            "next_session_date": "",
            "version": hidden("version"),
            "notes_sha": hidden("notes_sha"),
            "homework_sha": hidden("homework_sha"),
        },
    )
    assert response.status_code == 303, response.text
    assert scalar(office.db, "SELECT duration_min FROM therapy_sessions") == 70
    # The ciphertext was not rewritten, and the audit says only the duration changed.
    assert bytes(scalar(office.db, "SELECT notes_encrypted FROM therapy_sessions")) == before
    detail = scalar(
        office.db, "SELECT detail FROM audit_log WHERE action='session_updated' ORDER BY id DESC"
    )
    assert json.loads(detail) == {"changed": ["duration_min"]}


def test_edit_with_a_stale_version_is_refused(office: Office) -> None:
    session = created_id(save(office.a, office.adult))
    base = {
        "client_id": office.adult,
        "session_date": local(-1),
        "format_code": "individual",
        "method": ["cbt"],
        "primary": "cbt",
        "status": "completed",
        "notes_sha": "",
        "homework_sha": "",
    }
    assert (
        office.a.post(
            f"/terapiler/{session}", {**base, "duration_min": "60", "version": "1"}
        ).status_code
        == 303
    )
    stale = office.a.post(f"/terapiler/{session}", {**base, "duration_min": "90", "version": "1"})
    assert stale.status_code == 409 and "siz düzenlerken değiştirildi" in stale.text
    assert scalar(office.db, "SELECT duration_min FROM therapy_sessions") == 60


def test_delete_from_the_detail_page(office: Office) -> None:
    session = created_id(save(office.a, office.adult))
    response = office.a.post(f"/terapiler/{session}/sil")
    assert response.status_code == 303 and response.headers["location"] == "/terapiler?silindi=1"
    assert office.a.get(f"/terapiler/{session}").status_code == 404
    assert scalar(office.db, "SELECT deleted_at IS NOT NULL FROM therapy_sessions") is True


def test_a_colleagues_session_pages_are_404(office: Office) -> None:
    theirs = created_id(save(office.b, office.adult, notes=NOTE))
    for path in (f"/terapiler/{theirs}", f"/terapiler/{theirs}/duzenle"):
        page = office.a.get(path)
        assert page.status_code == 404 and "kabus" not in page.text
    assert office.a.post(f"/terapiler/{theirs}/sil").status_code == 404
    assert office.a.post(f"/terapiler/{theirs}", {"duration_min": "90"}).status_code in (404, 422)
    assert "<code" not in office.a.get("/terapiler").text.split("<tbody>")[-1]
    assert scalar(office.db, "SELECT deleted_at IS NULL FROM therapy_sessions") is True
    assert scalar(office.db, "SELECT count(*) FROM audit_log WHERE action='notes_read'") == 0


# ---------------------------------------------------------------- export


def test_export_is_admin_only_has_no_notes_and_is_audited(office: Office) -> None:
    created_id(save(office.a, office.child, notes=NOTE, method=["cbt", "emdr"], primary="emdr"))
    created_id(save(office.a, office.adult))
    assert "CSV indir" in office.a.get("/terapiler").text
    assert "CSV indir" not in office.b.get("/terapiler").text
    assert office.b.get("/terapiler/disa-aktar.csv").status_code == 403

    response = office.a.get("/terapiler/disa-aktar.csv?group=child")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    body = response.content.decode("utf-8-sig")
    assert "kabus" not in body and "Elif" not in body
    rows = list(csv.reader(io.StringIO(body), delimiter=";"))
    assert rows[0][0] == "Danışan kodu" and len(rows) == 2
    assert rows[1][7] == "EMDR" and rows[1][8].startswith("Bilişsel")

    audit = fetch(office.db, "SELECT actor_id, detail FROM audit_log WHERE action = 'export'")
    assert len(audit) == 1 and audit[0]["actor_id"] == A_ID
    assert json.loads(audit[0]["detail"])["age_group"] == "child"
    assert json.loads(audit[0]["detail"])["rows"] == 1
