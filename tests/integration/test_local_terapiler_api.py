"""Terapiler backend through its JSON API (TERAPILER_TAB §9), on a real PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from support.local import (
    A_ID,
    B_ID,
    FERNET_KEY,
    Kasa,
    LocalDb,
    add_therapists,
    add_user,
    execute,
    fetch,
    run,
    scalar,
)

from klinik_local.crypto.fernet import FieldCipher
from klinik_local.danisanlar.service import today_istanbul
from klinik_local.terapiler.service import SessionCreate, SessionService
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.permissions import expand_sets
from klinik_shared.timeutil import ISTANBUL, now_utc

pytestmark = pytest.mark.integration

SECRET_NOTE = "Danışan kabuslarını anlattı; kum tepsisinde ev figürü kullandı."
SECRET_HOMEWORK = "Her akşam duygu günlüğü tutulacak."
SCHEDULER_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a05")


def years_ago(years: int, days: int = 0) -> str:
    today = today_istanbul()
    return (today.replace(year=today.year - years) - timedelta(days=days)).isoformat()


def at(days_from_now: int, hour: int = 14) -> str:
    moment = datetime.now(ISTANBUL).replace(hour=hour, minute=0, second=0, microsecond=0)
    return (moment + timedelta(days=days_from_now)).isoformat()


@dataclass
class Clinic:
    db: LocalDb
    a: Kasa
    b: Kasa
    child: str  # client ids
    teen: str
    adult: str
    elder: str

    def session(self, who: Kasa, client_id: str, **overrides: Any) -> dict[str, Any]:
        body = {
            "client_id": client_id,
            "session_date": at(-1),
            "format_code": "individual",
            "methods": [{"code": "cbt", "is_primary": True}],
            "status": "completed",
            **overrides,
        }
        response = who.api("POST", "/api/therapy/sessions", json=body)
        assert response.status_code == 201, response.text
        return dict(response.json())


def make_client(kasa: Kasa, db: LocalDb, name: str, birth_date: str) -> str:
    response = kasa.post("/danisanlar", {"full_name": name, "birth_date": birth_date})
    assert response.status_code == 303, response.text
    return response.headers["location"].split("/")[2].split("?")[0]


@pytest.fixture
def clinic(local_db: LocalDb) -> Iterator[Clinic]:
    temporary = add_therapists(local_db)
    with Kasa(local_db) as a, Kasa(local_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        yield Clinic(
            db=local_db,
            a=a,
            b=b,
            child=make_client(a, local_db, "Elif Kaya", years_ago(9, days=100)),
            teen=make_client(a, local_db, "Can Demir", years_ago(15, days=100)),
            adult=make_client(a, local_db, "Zeynep Arslan", years_ago(34, days=100)),
            elder=make_client(a, local_db, "Hasan Şahin", years_ago(71, days=100)),
        )


def codes(payload: dict[str, Any], key: str = "items") -> list[str]:
    return [item["client"]["code"] for item in payload[key]]


def listing(who: Kasa, **params: Any) -> dict[str, Any]:
    response = who.api("GET", "/api/therapy/sessions", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------- catalog


def test_age_groups_come_from_the_database(clinic: Clinic) -> None:
    groups = clinic.a.api("GET", "/api/therapy/age-groups").json()
    assert [g["code"] for g in groups] == ["child", "adolescent", "adult", "geriatric"]
    assert [g["label_tr"] for g in groups] == ["Çocuk", "Ergen", "Yetişkin", "Geriatri (65+)"]

    # A new age group needs no code change: it simply appears as a tab.
    execute(clinic.db, "UPDATE age_groups SET min_age = 80 WHERE code = 'geriatric'")
    execute(
        clinic.db,
        "INSERT INTO age_groups (code, label_tr, min_age, max_age, sort_order) "
        "VALUES ('senior', 'İleri Yaş', 65, 79, 4)",
    )
    execute(clinic.db, "UPDATE age_groups SET sort_order = 5 WHERE code = 'geriatric'")
    groups = clinic.a.api("GET", "/api/therapy/age-groups").json()
    assert [g["code"] for g in groups] == ["child", "adolescent", "adult", "senior", "geriatric"]
    created = clinic.session(clinic.a, clinic.elder)
    assert created["age_group"] == {"code": "senior", "label_tr": "İleri Yaş"}


def test_overlapping_age_ranges_are_impossible(clinic: Clinic) -> None:
    with pytest.raises(asyncpg.ExclusionViolationError):
        execute(
            clinic.db,
            "INSERT INTO age_groups (code, label_tr, min_age, max_age, sort_order) "
            "VALUES ('tween', 'Ön Ergen', 10, 14, 9)",
        )


def test_child_defaults_are_the_seeded_ones_in_order(clinic: Clinic) -> None:
    data = clinic.a.api("GET", "/api/therapy/methods", params={"age_group": "child"}).json()
    assert [m["code"] for m in data["modalities"]["defaults"]] == [
        "play_therapy",
        "emdr",
        "cbt",
        "sandtray",
        "art_therapy",
        "supportive",
    ]
    assert [m["code"] for m in data["formats"]["defaults"]] == [
        "individual",
        "family",
        "group",
        "parent_session",
    ]
    assert [m["code"] for m in data["formats"]["others"]] == ["couple"]
    others = {m["code"] for m in data["modalities"]["others"]}
    assert "schema_therapy" in others and "play_therapy" not in others
    assert len(data["modalities"]["defaults"]) + len(others) == 16


def test_seed_matches_the_spec_matrix(clinic: Clinic) -> None:
    def defaults(group: str) -> set[str]:
        data = clinic.a.api("GET", "/api/therapy/methods", params={"age_group": group}).json()
        return {m["code"] for m in data["modalities"]["defaults"]}

    assert defaults("adolescent") == {
        "sandtray", "art_therapy", "emdr", "cbt", "dbt", "act",
        "motivational_interviewing", "ipt", "supportive",
    }  # fmt: skip
    assert "reminiscence" in defaults("geriatric") and "play_therapy" not in defaults("adult")
    assert clinic.a.api("GET", "/api/therapy/methods", params={"age_group": "x"}).status_code == 404


def test_disabled_method_disappears_from_choices_but_not_from_history(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.child, methods=[{"code": "sandtray"}])
    execute(clinic.db, "UPDATE therapy_methods SET is_active = false WHERE code = 'sandtray'")
    data = clinic.a.api("GET", "/api/therapy/methods", params={"age_group": "child"}).json()
    assert "sandtray" not in {m["code"] for m in data["modalities"]["defaults"]}
    assert listing(clinic.a)["items"][0]["methods"][0]["code"] == "sandtray"
    # Not offered for new sessions, but an existing session keeps it when edited.
    refused = clinic.a.api(
        "POST",
        "/api/therapy/sessions",
        json={
            "client_id": clinic.child,
            "session_date": at(-1),
            "format_code": "individual",
            "methods": [{"code": "sandtray"}],
        },
    )
    assert refused.status_code == 422
    kept = clinic.a.api(
        "PATCH",
        f"/api/therapy/sessions/{created['id']}",
        json={"methods": [{"code": "sandtray"}, {"code": "cbt"}], "duration_min": 40},
    )
    assert kept.status_code == 200


# ---------------------------------------------------------------- creating


def test_create_returns_the_documented_shape(clinic: Clinic) -> None:
    created = clinic.session(
        clinic.a,
        clinic.child,
        format_code="family",
        methods=[{"code": "play_therapy", "is_primary": True}, {"code": "cbt"}],
        notes=SECRET_NOTE,
    )
    assert created["client"]["age_at_session"] == 9
    assert created["client"]["code"].startswith("DN-")
    assert created["age_group"] == {"code": "child", "label_tr": "Çocuk"}
    assert created["session_number"] == 1
    assert created["duration_min"] == 50
    assert created["format"] == {"code": "family", "label_tr": "Aile Terapisi"}
    assert created["methods"][0] == {
        "code": "play_therapy",
        "label_tr": "Oyun Terapisi",
        "is_primary": True,
    }
    assert created["status"] == "completed"
    assert created["therapist"] == {"id": str(A_ID), "display_name": "Terapist A"}
    assert created["session_date"].endswith("+03:00")
    assert "notes" not in created and SECRET_NOTE not in json.dumps(created)


@pytest.mark.parametrize(
    "forbidden",
    [
        {"age_group_id_at_session": 3},
        {"session_number": 99},
        {"therapist_id": str(B_ID)},
        {"created_by": str(B_ID)},
        {"deleted_at": None},
    ],
)
def test_server_owned_fields_are_rejected(clinic: Clinic, forbidden: dict[str, Any]) -> None:
    body = {
        "client_id": clinic.adult,
        "session_date": at(-1),
        "format_code": "individual",
        "methods": [{"code": "cbt"}],
        **forbidden,
    }
    assert clinic.a.api("POST", "/api/therapy/sessions", json=body).status_code == 422
    created = clinic.session(clinic.a, clinic.adult)
    patch = clinic.a.api("PATCH", f"/api/therapy/sessions/{created['id']}", json=forbidden)
    assert patch.status_code == 422
    assert scalar(clinic.db, "SELECT count(*) FROM therapy_sessions") == 1
    assert scalar(clinic.db, "SELECT therapist_id FROM therapy_sessions") == A_ID


def test_the_author_is_always_the_therapist(clinic: Clinic) -> None:
    clinic.session(clinic.b, clinic.adult)
    assert scalar(clinic.db, "SELECT therapist_id FROM therapy_sessions") == B_ID


@pytest.mark.parametrize(
    ("overrides", "error_type"),
    [
        ({"format_code": "emdr"}, "invalid_format"),  # a modality is not a format
        ({"format_code": "nope"}, "invalid_format"),
        ({"methods": [{"code": "individual"}]}, "invalid_method"),  # a format is not a modality
        ({"methods": [{"code": "nope"}]}, "invalid_method"),
        ({"methods": []}, "methods_required"),  # completed needs at least one
        (
            {
                "methods": [
                    {"code": "cbt", "is_primary": True},
                    {"code": "emdr", "is_primary": True},
                ]
            },
            "multiple_primary",
        ),
        ({"methods": [{"code": "cbt"}, {"code": "cbt"}]}, "duplicate_method"),
    ],
)
def test_invalid_sessions_are_rejected(
    clinic: Clinic, overrides: dict[str, Any], error_type: str
) -> None:
    body = {
        "client_id": clinic.adult,
        "session_date": at(-1),
        "format_code": "individual",
        "methods": [{"code": "cbt"}],
        "status": "completed",
        **overrides,
    }
    response = clinic.a.api("POST", "/api/therapy/sessions", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["type"] == error_type
    assert scalar(clinic.db, "SELECT count(*) FROM therapy_sessions") == 0
    # A rejected session must not consume a session number.
    assert scalar(clinic.db, "SELECT max(session_counter) FROM clients") == 0


@pytest.mark.parametrize("duration", [9, 241, 0, -50])
def test_duration_bounds(clinic: Clinic, duration: int) -> None:
    body = {
        "client_id": clinic.adult,
        "session_date": at(-1),
        "format_code": "individual",
        "duration_min": duration,
    }
    assert clinic.a.api("POST", "/api/therapy/sessions", json=body).status_code == 422


def test_first_method_becomes_primary_when_none_is_marked(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.adult, methods=[{"code": "emdr"}, {"code": "cbt"}])
    primary = [m["code"] for m in created["methods"] if m["is_primary"]]
    assert primary == ["emdr"]


def test_status_defaults_from_the_date(clinic: Clinic) -> None:
    body = {"client_id": clinic.adult, "format_code": "individual", "methods": [{"code": "cbt"}]}
    past = clinic.a.api("POST", "/api/therapy/sessions", json={**body, "session_date": at(-2)})
    future = clinic.a.api("POST", "/api/therapy/sessions", json={**body, "session_date": at(3)})
    assert past.json()["status"] == "completed"
    assert future.json()["status"] == "planned"
    # A planned session may be saved before its methods are known.
    no_methods = {**body, "methods": [], "session_date": at(4), "status": "planned"}
    assert clinic.a.api("POST", "/api/therapy/sessions", json=no_methods).status_code == 201


def test_missing_birth_date_blocks_saving(clinic: Clinic) -> None:
    nameless = make_client(clinic.a, clinic.db, "Doğum Tarihi Yok", "")
    response = clinic.a.api(
        "POST",
        "/api/therapy/sessions",
        json={"client_id": nameless, "session_date": at(-1), "format_code": "individual"},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == (
        "Danışanın doğum tarihi eksik. Önce danışan profilini güncelleyin."
    )


def test_naive_dates_and_unknown_clients_are_rejected(clinic: Clinic) -> None:
    body = {"client_id": clinic.adult, "format_code": "individual", "methods": [{"code": "cbt"}]}
    naive = clinic.a.api(
        "POST", "/api/therapy/sessions", json={**body, "session_date": "2026-11-10T15:00:00"}
    )
    assert naive.status_code == 422
    ghost = clinic.a.api(
        "POST",
        "/api/therapy/sessions",
        json={**body, "client_id": str(uuid4()), "session_date": at(-1)},
    )
    assert ghost.status_code == 422 and ghost.json()["detail"][0]["type"] == "client_not_found"


# ---------------------------------------------------------------- database-level integrity


def insert_session_sql(format_code: str) -> str:
    return (
        "INSERT INTO therapy_sessions (client_id, therapist_id, age_group_id_at_session, "
        "format_method_id, session_number, session_date, status) "
        "SELECT c.id, u.id, ag.id, m.id, 1, now(), 'completed' "
        "FROM clients c, users u, age_groups ag, therapy_methods m "
        f"WHERE u.username = 'terapist.a' AND ag.code = 'adult' AND m.code = '{format_code}' "
        "LIMIT 1"
    )


def test_database_rejects_a_modality_used_as_format(clinic: Clinic) -> None:
    with pytest.raises(asyncpg.CheckViolationError, match="expected format"):
        execute(clinic.db, insert_session_sql("emdr"))
    execute(clinic.db, insert_session_sql("individual"))  # the same row with a real format is fine


def test_database_rejects_a_format_used_as_modality_and_two_primaries(clinic: Clinic) -> None:
    execute(clinic.db, insert_session_sql("individual"))
    add_method = (
        "INSERT INTO session_methods (session_id, therapy_method_id, is_primary) "
        "SELECT s.id, m.id, true FROM therapy_sessions s, therapy_methods m WHERE m.code = $1"
    )
    with pytest.raises(asyncpg.CheckViolationError, match="expected modality"):
        execute(clinic.db, add_method, "family")
    execute(clinic.db, add_method, "cbt")
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(clinic.db, add_method, "emdr")  # a second primary
    with pytest.raises(asyncpg.CheckViolationError, match="kind cannot be changed"):
        execute(clinic.db, "UPDATE therapy_methods SET kind = 'format' WHERE code = 'cbt'")


# ---------------------------------------------------------------- age group snapshot


def test_past_sessions_keep_their_age_group_after_a_birthday(clinic: Clinic) -> None:
    """Turned 13 ten days ago: last month's session stays Çocuk, today's is Ergen."""
    client = make_client(clinic.a, clinic.db, "Yeni Ergen", years_ago(13, days=10))
    before = clinic.session(clinic.a, client, session_date=at(-30))
    after = clinic.session(clinic.a, client, session_date=at(-1))
    assert before["age_group"]["code"] == "child" and before["client"]["age_at_session"] == 12
    assert after["age_group"]["code"] == "adolescent" and after["client"]["age_at_session"] == 13

    assert [i["id"] for i in listing(clinic.a, age_group="child")["items"]] == [before["id"]]
    assert [i["id"] for i in listing(clinic.a, age_group="adolescent")["items"]] == [after["id"]]


def test_birthday_on_the_session_day_counts(clinic: Clinic) -> None:
    client = make_client(clinic.a, clinic.db, "Bugün On Sekiz", years_ago(18))
    assert (
        clinic.session(clinic.a, client, session_date=at(0, hour=9))["age_group"]["code"] == "adult"
    )
    assert (
        clinic.session(clinic.a, client, session_date=at(-1))["age_group"]["code"] == "adolescent"
    )


def test_moving_the_date_re_snapshots_the_age_group(clinic: Clinic) -> None:
    client = make_client(clinic.a, clinic.db, "Yeni Ergen", years_ago(13, days=10))
    created = clinic.session(clinic.a, client, session_date=at(-30))
    moved = clinic.a.api(
        "PATCH", f"/api/therapy/sessions/{created['id']}", json={"session_date": at(-2)}
    )
    assert moved.status_code == 200
    assert moved.json()["age_group"]["code"] == "adolescent"


# ---------------------------------------------------------------- session numbers


def test_session_numbers_count_per_client(clinic: Clinic) -> None:
    numbers = [clinic.session(clinic.a, clinic.adult)["session_number"] for _ in range(3)]
    assert numbers == [1, 2, 3]
    assert clinic.session(clinic.a, clinic.child)["session_number"] == 1


def test_numbers_stay_unique_when_both_therapists_see_one_client(clinic: Clinic) -> None:
    """max()+1 would give both therapists #1, because neither can see the other's rows."""
    first = clinic.session(clinic.a, clinic.adult)
    second = clinic.session(clinic.b, clinic.adult)
    third = clinic.session(clinic.a, clinic.adult)
    assert [first["session_number"], second["session_number"], third["session_number"]] == [1, 2, 3]


def test_deleted_numbers_are_not_reused(clinic: Clinic) -> None:
    first = clinic.session(clinic.a, clinic.adult)
    assert clinic.a.api("DELETE", f"/api/therapy/sessions/{first['id']}").status_code == 204
    assert clinic.session(clinic.a, clinic.adult)["session_number"] == 2


def test_concurrent_inserts_for_one_client_get_consecutive_numbers(clinic: Clinic) -> None:
    for _ in range(6):
        clinic.session(clinic.a, clinic.adult)
    service = SessionService(FieldCipher(FERNET_KEY))
    actor = CurrentUser(
        id=A_ID,
        username="terapist.a",
        display_name="Terapist A",
        permissions=frozenset(str(p) for p in expand_sets("admin", "therapist")),
        must_change_password=False,
        session_id_hash=b"",
        csrf_token="",
    )
    data = SessionCreate.model_validate(
        {
            "client_id": clinic.adult,
            "session_date": now_utc() - timedelta(hours=3),
            "format_code": "individual",
            "methods": [{"code": "cbt"}],
        }
    )

    async def create(engine: AsyncEngine) -> int:
        async with engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(A_ID)}
            )
            item = await service.create(conn, actor, data)
            await asyncio.sleep(0.05)  # keep both transactions open at once
            return item.session_number

    async def go() -> list[int]:
        engine = create_async_engine(clinic.db.app_url)
        try:
            return list(await asyncio.gather(create(engine), create(engine)))
        finally:
            await engine.dispose()

    assert sorted(run(go())) == [7, 8]


# ---------------------------------------------------------------- privacy between therapists


def test_a_therapist_cannot_reach_a_colleagues_sessions_through_any_route(clinic: Clinic) -> None:
    mine = clinic.session(clinic.a, clinic.adult, notes="A'nın notu")
    theirs = clinic.session(clinic.b, clinic.adult, notes=SECRET_NOTE)
    url = f"/api/therapy/sessions/{theirs['id']}"

    assert [i["id"] for i in listing(clinic.a)["items"]] == [mine["id"]]
    assert clinic.a.api("GET", "/api/therapy/sessions/counts").json()["all"] == 1
    assert clinic.a.api("GET", url).status_code == 404
    assert clinic.a.api("PATCH", url, json={"notes": "üzerine yazıldı"}).status_code == 404
    assert clinic.a.api("PATCH", url, json={"status": "cancelled"}).status_code == 404
    assert clinic.a.api("DELETE", url).status_code == 404
    timeline = clinic.a.api("GET", f"/api/clients/{clinic.adult}/sessions").json()
    assert [i["id"] for i in timeline] == [mine["id"]]
    # Asking for the colleague by id changes nothing without sessions.read_others.
    assert listing(clinic.a, therapist_id=str(B_ID))["total"] == 1
    assert listing(clinic.a, q="Zeynep")["total"] == 1

    # B's record is untouched and B still reads it.
    assert clinic.b.api("GET", url).json()["notes"] == SECRET_NOTE
    assert scalar(clinic.db, "SELECT count(*) FROM therapy_sessions WHERE deleted_at IS NULL") == 2
    # Admin (A) is admin of the system, not of a colleague's clinical records.
    assert "admin.users" in {
        r["permission"]
        for r in fetch(clinic.db, "SELECT permission FROM user_permissions WHERE user_id=$1", A_ID)
    }


def as_app(db: LocalDb, sql: str, user_id: UUID | None) -> list[asyncpg.Record]:
    async def go() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(db.dsn(db.app_url))
        try:
            async with conn.transaction():
                if user_id:
                    await conn.execute(
                        "SELECT set_config('app.current_user_id', $1, true)", str(user_id)
                    )
                return list(await conn.fetch(sql))
        finally:
            await conn.close()

    return run(go())


def test_row_level_security_hides_a_colleagues_sessions_in_sql(clinic: Clinic) -> None:
    clinic.session(clinic.a, clinic.adult)
    clinic.session(clinic.b, clinic.adult, methods=[{"code": "emdr"}, {"code": "act"}])
    count = "SELECT count(*) FROM therapy_sessions"

    assert as_app(clinic.db, count, A_ID)[0][0] == 1
    assert as_app(clinic.db, count, B_ID)[0][0] == 1
    assert as_app(clinic.db, count, None)[0][0] == 0
    assert as_app(clinic.db, "SELECT count(*) FROM session_methods", A_ID)[0][0] == 1
    assert as_app(clinic.db, "SELECT count(*) FROM session_methods", B_ID)[0][0] == 2
    assert as_app(clinic.db, "SELECT count(*) FROM session_methods", None)[0][0] == 0

    # Writing to a colleague's row: UPDATE matches nothing, INSERT is refused outright.
    as_app(clinic.db, "UPDATE therapy_sessions SET status = 'cancelled'", A_ID)
    assert scalar(clinic.db, "SELECT count(*) FROM therapy_sessions WHERE status='cancelled'") == 1
    forged = (
        insert_session_sql("individual")
        .replace("'terapist.a'", "'terapist.b'")
        .replace("1, now()", "50, now()")
    )
    with pytest.raises(asyncpg.InsufficientPrivilegeError, match="row-level security"):
        as_app(clinic.db, forged, A_ID)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        as_app(clinic.db, "DELETE FROM therapy_sessions", A_ID)
    # The catalog is readable only once someone is logged in.
    assert as_app(clinic.db, "SELECT count(*) FROM therapy_methods", None)[0][0] == 0
    assert as_app(clinic.db, "SELECT count(*) FROM therapy_methods", B_ID)[0][0] == 21
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        as_app(
            clinic.db,
            "INSERT INTO therapy_methods (code,label_tr,kind) VALUES ('x1','X','modality')",
            B_ID,
        )


def test_read_others_permission_opens_reading_but_not_writing(clinic: Clinic) -> None:
    theirs = clinic.session(clinic.b, clinic.adult, notes=SECRET_NOTE)
    execute(
        clinic.db,
        "INSERT INTO user_permissions (user_id, permission) VALUES ($1, 'sessions.read_others')",
        A_ID,
    )
    url = f"/api/therapy/sessions/{theirs['id']}"
    assert listing(clinic.a)["total"] == 1
    assert listing(clinic.a, therapist_id=str(B_ID))["total"] == 1
    assert listing(clinic.a, therapist_id=str(A_ID))["total"] == 0
    assert clinic.a.api("GET", url).json()["notes"] == SECRET_NOTE
    assert clinic.a.api("PATCH", url, json={"status": "cancelled"}).status_code == 404
    assert clinic.a.api("DELETE", url).status_code == 404
    reads = fetch(clinic.db, "SELECT actor_id FROM audit_log WHERE action = 'notes_read'")
    assert [r["actor_id"] for r in reads] == [A_ID]


def test_a_scheduler_only_user_has_no_access_to_the_clinical_log(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.adult)
    temporary = add_user(clinic.db, SCHEDULER_ID, "sekreter", "Sekreter", expand_sets("scheduler"))
    with Kasa(clinic.db) as secretary:
        secretary.onboard("sekreter", temporary)
        for method, path in (
            ("GET", "/api/therapy/sessions"),
            ("GET", "/api/therapy/sessions/counts"),
            ("GET", f"/api/therapy/sessions/{created['id']}"),
            ("GET", "/api/therapy/age-groups"),
            ("GET", f"/api/clients/{clinic.adult}/sessions"),
            ("DELETE", f"/api/therapy/sessions/{created['id']}"),
        ):
            assert secretary.api(method, path).status_code == 403, path
        post = secretary.api("POST", "/api/therapy/sessions", json={})
        assert post.status_code == 403
    assert as_app(clinic.db, "SELECT count(*) FROM therapy_sessions", SCHEDULER_ID)[0][0] == 0


def test_anonymous_api_calls_get_401_json(clinic: Clinic) -> None:
    with Kasa(clinic.db) as stranger:
        response = stranger.api("GET", "/api/therapy/sessions")
        assert response.status_code == 401
        assert response.json() == {"detail": "not_authenticated"}


def test_api_writes_need_the_csrf_header(clinic: Clinic) -> None:
    body = {
        "client_id": clinic.adult,
        "session_date": at(-1),
        "format_code": "individual",
        "methods": [{"code": "cbt"}],
    }
    response = clinic.a.client.post("/api/therapy/sessions", json=body)
    assert response.status_code == 403 and response.json() == {"detail": "csrf"}
    assert scalar(clinic.db, "SELECT count(*) FROM therapy_sessions") == 0


# ---------------------------------------------------------------- clinical text


def test_notes_are_encrypted_at_rest(clinic: Clinic) -> None:
    clinic.session(clinic.a, clinic.child, notes=SECRET_NOTE, homework=SECRET_HOMEWORK)
    row = fetch(clinic.db, "SELECT notes_encrypted, homework_encrypted FROM therapy_sessions")[0]
    for stored, plaintext in (
        (bytes(row["notes_encrypted"]), SECRET_NOTE),
        (bytes(row["homework_encrypted"]), SECRET_HOMEWORK),
    ):
        assert plaintext.encode() not in stored
        assert b"kabus" not in stored
        assert Fernet(FERNET_KEY.encode()).decrypt(stored).decode() == plaintext


def test_lists_never_contain_note_text(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.child, notes=SECRET_NOTE, homework=SECRET_HOMEWORK)
    for path in (
        "/api/therapy/sessions",
        "/api/therapy/sessions?q=Elif",
        "/api/therapy/sessions/counts",
        f"/api/clients/{clinic.child}/sessions",
    ):
        body = clinic.a.api("GET", path).text
        assert "kabus" not in body and "günlüğü" not in body, path
        assert "notes" not in body and "homework" not in body, path
    assert scalar(clinic.db, "SELECT count(*) FROM audit_log WHERE action = 'notes_read'") == 0
    assert "Elif" not in clinic.a.api("GET", "/api/therapy/sessions").text
    assert created["id"]


def test_opening_the_detail_decrypts_and_is_audited_every_time(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.child, notes=SECRET_NOTE, homework=SECRET_HOMEWORK)
    for _ in range(2):
        detail = clinic.a.api("GET", f"/api/therapy/sessions/{created['id']}")
        assert detail.status_code == 200
        assert detail.headers["cache-control"] == "no-store"
        assert detail.json()["notes"] == SECRET_NOTE
        assert detail.json()["homework"] == SECRET_HOMEWORK
    rows = fetch(
        clinic.db,
        "SELECT actor_id, entity_type, entity_id FROM audit_log WHERE action = 'notes_read'",
    )
    assert len(rows) == 2
    assert all(r["actor_id"] == A_ID and r["entity_id"] == created["id"] for r in rows)


def test_audit_log_never_contains_clinical_text(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.child, notes=SECRET_NOTE)
    url = f"/api/therapy/sessions/{created['id']}"
    clinic.a.api("PATCH", url, json={"notes": "Güncellenmiş gizli not", "status": "no_show"})
    clinic.a.api("GET", url)
    clinic.a.api("DELETE", url)

    rows = fetch(
        clinic.db,
        "SELECT action, detail FROM audit_log WHERE entity_type = 'therapy_session' ORDER BY id",
    )
    assert [r["action"] for r in rows] == [
        "session_created",
        "session_updated",
        "notes_read",
        "session_deleted",
    ]
    assert json.loads(rows[0]["detail"])["has_notes"] is True
    assert json.loads(rows[1]["detail"]) == {"changed": ["status", "notes"]}
    dump = scalar(clinic.db, "SELECT string_agg(audit_log::text, ' ') FROM audit_log")
    for secret in ("kabus", "gizli not", "Elif"):
        assert secret not in dump


# ---------------------------------------------------------------- updating


def test_patch_changes_only_what_is_sent(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.adult, notes=SECRET_NOTE, homework=SECRET_HOMEWORK)
    url = f"/api/therapy/sessions/{created['id']}"
    patched = clinic.a.api("PATCH", url, json={"duration_min": 80, "format_code": "couple"})
    assert patched.status_code == 200
    body = patched.json()
    assert body["duration_min"] == 80 and body["format"]["code"] == "couple"
    assert body["version"] == created["version"] + 1
    assert body["methods"] == created["methods"] and body["status"] == "completed"

    detail = clinic.a.api("GET", url).json()
    assert detail["notes"] == SECRET_NOTE and detail["homework"] == SECRET_HOMEWORK

    cleared = clinic.a.api("PATCH", url, json={"homework": None})
    assert cleared.status_code == 200
    detail = clinic.a.api("GET", url).json()
    assert detail["homework"] is None and detail["notes"] == SECRET_NOTE


def test_patch_with_a_stale_version_is_a_409(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.adult)
    url = f"/api/therapy/sessions/{created['id']}"
    assert clinic.a.api("PATCH", url, json={"duration_min": 60, "version": 1}).status_code == 200
    stale = clinic.a.api("PATCH", url, json={"duration_min": 90, "version": 1})
    assert stale.status_code == 409
    assert scalar(clinic.db, "SELECT duration_min FROM therapy_sessions") == 60


def test_a_session_cannot_be_completed_without_a_method(clinic: Clinic) -> None:
    planned = clinic.session(
        clinic.a, clinic.adult, session_date=at(2), status="planned", methods=[]
    )
    url = f"/api/therapy/sessions/{planned['id']}"
    refused = clinic.a.api("PATCH", url, json={"status": "completed"})
    assert refused.status_code == 422
    assert refused.json()["detail"][0]["type"] == "methods_required"
    done = clinic.a.api(
        "PATCH", url, json={"status": "completed", "methods": [{"code": "schema_therapy"}]}
    )
    assert done.status_code == 200 and done.json()["status"] == "completed"
    emptied = clinic.a.api("PATCH", url, json={"methods": []})
    assert emptied.status_code == 422


def test_delete_is_soft(clinic: Clinic) -> None:
    created = clinic.session(clinic.a, clinic.adult, notes=SECRET_NOTE)
    url = f"/api/therapy/sessions/{created['id']}"
    assert clinic.a.api("DELETE", url).status_code == 204
    assert clinic.a.api("GET", url).status_code == 404
    assert clinic.a.api("DELETE", url).status_code == 404
    assert listing(clinic.a)["total"] == 0
    row = fetch(clinic.db, "SELECT deleted_at, notes_encrypted FROM therapy_sessions")[0]
    assert row["deleted_at"] is not None and row["notes_encrypted"] is not None


# ---------------------------------------------------------------- filters, sorting, paging


@pytest.fixture
def log(clinic: Clinic) -> dict[str, dict[str, Any]]:
    c, a = clinic, clinic.a
    return {
        "child_play": c.session(
            a, c.child, session_date=at(-1), format_code="family",
            methods=[{"code": "play_therapy", "is_primary": True}, {"code": "cbt"}],
        ),
        "teen_emdr": c.session(
            a, c.teen, session_date=at(-3), methods=[{"code": "emdr"}], duration_min=90
        ),
        "adult_cbt": c.session(a, c.adult, session_date=at(-5), status="no_show"),
        "adult_schema": c.session(
            a, c.adult, session_date=at(-40), methods=[{"code": "schema_therapy"}],
            format_code="couple",
        ),
        "elder_planned": c.session(
            a, c.elder, session_date=at(4), status="planned", methods=[{"code": "reminiscence"}]
        ),
    }  # fmt: skip


def ids(payload: dict[str, Any]) -> list[str]:
    return [item["id"] for item in payload["items"]]


def test_default_sort_is_newest_first(clinic: Clinic, log: dict[str, dict[str, Any]]) -> None:
    order = ["elder_planned", "child_play", "teen_emdr", "adult_cbt", "adult_schema"]
    assert ids(listing(clinic.a)) == [log[k]["id"] for k in order]
    assert ids(listing(clinic.a, sort="session_date")) == [log[k]["id"] for k in reversed(order)]
    assert ids(listing(clinic.a, sort="; DROP TABLE x")) == [log[k]["id"] for k in order]


def test_filters(clinic: Clinic, log: dict[str, dict[str, Any]]) -> None:
    a = clinic.a

    def got(**params: Any) -> set[str]:
        found = set(ids(listing(a, **params)))
        return {name for name, item in log.items() if item["id"] in found}

    assert got(age_group="adult") == {"adult_cbt", "adult_schema"}
    assert got(method="emdr") == {"teen_emdr"}
    # Several methods are OR-ed; a non-primary method matches too.
    assert got(method=["emdr", "cbt"]) == {"teen_emdr", "adult_cbt", "child_play"}
    assert got(**{"method[]": ["schema_therapy"]}) == {"adult_schema"}
    assert got(format="family") == {"child_play"}
    assert got(status=["no_show", "planned"]) == {"adult_cbt", "elder_planned"}
    assert got(status="not-a-status") == set(log)
    today = today_istanbul()
    assert got(date_from=(today - timedelta(days=6)).isoformat(), date_to=today.isoformat()) == {
        "child_play",
        "teen_emdr",
        "adult_cbt",
    }
    assert got(date_to=(today - timedelta(days=30)).isoformat()) == {"adult_schema"}
    assert got(age_group="adult", method="cbt", status="no_show") == {"adult_cbt"}
    assert got(age_group="child", method="emdr") == set()


def test_search_by_code_and_by_name(clinic: Clinic, log: dict[str, dict[str, Any]]) -> None:
    teen_code = log["teen_emdr"]["client"]["code"]
    assert codes(listing(clinic.a, q=teen_code)) == [teen_code]
    assert codes(listing(clinic.a, q=teen_code[-4:].lower())) == [teen_code]
    # Name search runs on the server against decrypted names (Turkish-insensitive).
    assert codes(listing(clinic.a, q="sahin")) == [log["elder_planned"]["client"]["code"]]
    assert listing(clinic.a, q="ZEYNEP")["total"] == 2
    assert listing(clinic.a, q="yok böyle biri")["total"] == 0
    assert listing(clinic.a, q="%")["total"] == 0  # LIKE wildcards are literal


def test_sorting_by_other_columns(clinic: Clinic, log: dict[str, dict[str, Any]]) -> None:
    a = clinic.a
    ages = [i["client"]["age_at_session"] for i in listing(a, sort="age")["items"]]
    assert ages == sorted(ages) and ages[0] == 9 and ages[-1] == 71
    durations = [i["duration_min"] for i in listing(a, sort="-duration_min")["items"]]
    assert durations[0] == 90
    by_client = codes(listing(a, sort="client"))
    assert by_client == sorted(by_client)


def test_pagination(clinic: Clinic, log: dict[str, dict[str, Any]]) -> None:
    a = clinic.a
    first = listing(a, page_size=2)
    second = listing(a, page_size=2, page=2)
    third = listing(a, page_size=2, page=3)
    assert first["total"] == 5 and first["page_size"] == 2
    assert [len(p["items"]) for p in (first, second, third)] == [2, 2, 1]
    assert len({*ids(first), *ids(second), *ids(third)}) == 5
    assert listing(a, page_size=2, page=99)["page"] == 3  # clamped to the last page
    assert listing(a, page_size=5000)["page_size"] == 100
    assert listing(a, page="abc", page_size="x")["page"] == 1


def test_counts_ignore_the_age_group_filter_itself(
    clinic: Clinic, log: dict[str, dict[str, Any]]
) -> None:
    def counts(**params: Any) -> dict[str, int]:
        return dict(clinic.a.api("GET", "/api/therapy/sessions/counts", params=params).json())

    assert counts() == {"all": 5, "child": 1, "adolescent": 1, "adult": 2, "geriatric": 1}
    assert counts(age_group="child") == counts()
    assert counts(status="completed") == {"all": 3, "child": 1, "adolescent": 1, "adult": 1}
    assert counts(method="cbt", age_group="adult") == {"all": 2, "child": 1, "adult": 1}


def test_timeline_spans_age_groups_and_is_limited(clinic: Clinic) -> None:
    client = make_client(clinic.a, clinic.db, "Yeni Ergen", years_ago(13, days=10))
    for days in range(16, 4, -1):  # twelve sessions; the first six as a child
        clinic.session(clinic.a, client, session_date=at(-days))
    timeline = clinic.a.api("GET", f"/api/clients/{client}/sessions").json()
    assert len(timeline) == 10
    assert [i["session_number"] for i in timeline] == list(range(12, 2, -1))
    assert {i["age_group"]["code"] for i in timeline} == {"child", "adolescent"}
    assert len(clinic.a.api("GET", f"/api/clients/{client}/sessions?limit=3").json()) == 3
