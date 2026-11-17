"""Office <-> cloud sync, end to end on two real databases (brief §9, §16; plan §5).

Each test has its own office database (Klinik Kasa) and cloud database (Klinik Web).
The worker talks to the real cloud sync API in-process, through an httpx transport.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from support.cloud import (
    CLOUD_AUTH_KEY,
    CloudDb,
    Web,
    add_cloud_therapists,
    add_room,
)
from support.local import (
    A_ID,
    Kasa,
    LocalDb,
    add_therapists,
    execute,
    fetch,
    run,
    scalar,
)

from klinik_cloud.config import Settings as CloudSettings
from klinik_cloud.main import create_app as create_cloud_app
from klinik_cloud.sync_api import create_sync_token
from klinik_local.danisanlar.service import today_istanbul
from klinik_shared.sync_protocol import PROTOCOL_HEADER
from klinik_shared.timeutil import ISTANBUL
from klinik_sync.cloud_client import (
    CloudAuthFailed,
    CloudClient,
    CloudUnavailable,
    ProtocolMismatch,
)
from klinik_sync.store import LocalStore
from klinik_sync.worker import SyncWorker

pytestmark = pytest.mark.integration

NAME = "Elif Kaya"
NOTE = "Kabuslarını anlattı."


def next_monday() -> datetime:
    today = datetime.now(ISTANBUL).replace(hour=0, minute=0, second=0, microsecond=0)
    return today + timedelta(days=7 - today.weekday())


DAY = next_monday().date()


@dataclass
class Recorder:
    """Keeps every request and response body that crossed the office/cloud boundary."""

    bodies: list[str] = field(default_factory=list)


@dataclass
class Setup:
    local: LocalDb
    cloud: CloudDb
    token: str
    kasa: Kasa
    web: Web
    room: UUID
    recorder: Recorder

    def worker(
        self,
        *,
        token: str | None = None,
        down: bool = False,
        protocol: str | None = None,
    ) -> Callable[[Callable[[SyncWorker], Coroutine[Any, Any, Any]]], Any]:
        """Run ``fn(worker)`` in a fresh event loop with a fresh cloud app and engines."""

        def runner(fn: Callable[[SyncWorker], Coroutine[Any, Any, Any]]) -> Any:
            async def go() -> Any:
                cloud_app = create_cloud_app(
                    CloudSettings(database_url=self.cloud.app_url, auth_fernet_key=CLOUD_AUTH_KEY)
                )
                inner = httpx.ASGITransport(app=cloud_app)
                recorder = self.recorder

                class Transport(httpx.AsyncBaseTransport):
                    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                        if down:
                            raise httpx.ConnectError("cloud unreachable", request=request)
                        if protocol is not None:
                            request.headers[PROTOCOL_HEADER] = protocol
                        recorder.bodies.append(
                            request.url.query.decode() + request.content.decode()
                        )
                        response = await inner.handle_async_request(request)
                        body = await response.aread()
                        recorder.bodies.append(body.decode())
                        return httpx.Response(
                            response.status_code, headers=response.headers, content=body
                        )

                engine = create_async_engine(self.local.sync_url)
                client = CloudClient("http://cloud", token or self.token, transport=Transport())
                try:
                    return await fn(SyncWorker(LocalStore(engine), client, pull_wait=0))
                finally:
                    await client.aclose()
                    await engine.dispose()
                    await cloud_app.state.engine.dispose()

            return run(go())

        return runner

    def sync(self) -> None:
        """One full round: reconcile, push, pull."""

        async def step(w: SyncWorker) -> None:
            await w.reconcile()
            await w.push_once()
            await w.pull_once()

        self.worker()(step)

    def push(self, **kwargs: Any) -> int:
        return int(self.worker(**kwargs)(lambda w: w.push_once()))

    def pull(self, **kwargs: Any) -> int:
        return int(self.worker(**kwargs)(lambda w: w.pull_once()))


@pytest.fixture
def setup(local_db: LocalDb, cloud_db: CloudDb) -> Iterator[Setup]:
    local_temp = add_therapists(local_db)
    cloud_temp = add_cloud_therapists(cloud_db)
    room = add_room(cloud_db, "Oda 1")

    async def token() -> str:
        engine = create_async_engine(
            cloud_db.owner_url, connect_args={"server_settings": {"search_path": "app"}}
        )
        try:
            async with engine.begin() as conn:
                return await create_sync_token(conn)
        finally:
            await engine.dispose()

    with Kasa(local_db) as kasa, Web(cloud_db) as web:
        kasa.onboard("terapist.a", local_temp["a"])
        web.onboard("terapist.b", cloud_temp["b"])  # B does the booking, as in the brief
        yield Setup(local_db, cloud_db, run(token()), kasa, web, room, Recorder())


def new_client(s: Setup, name: str = NAME, years: int = 9) -> str:
    today = today_istanbul()
    birth = (today.replace(year=today.year - years) - timedelta(days=100)).isoformat()
    response = s.kasa.post(
        "/danisanlar",
        {"full_name": name, "birth_date": birth, "assigned_therapist_id": str(A_ID)},
    )
    assert response.status_code == 303
    client_id = response.headers["location"].split("/")[2].split("?")[0]
    return str(scalar(s.local, "SELECT client_code FROM clients WHERE id = $1", UUID(client_id)))


def book(s: Setup, code: str, *, time: str = "14:00", therapist: UUID = A_ID) -> UUID:
    response = s.web.post(
        "/randevular",
        {
            "client_code": code,
            "therapist_id": str(therapist),
            "date": DAY.isoformat(),
            "time": time,
            "duration_min": "50",
            "room_id": str(s.room),
        },
    )
    assert response.status_code == 303, response.text
    return UUID(
        str(
            scalar(
                s.cloud,  # type: ignore[arg-type]
                "SELECT id FROM appointments WHERE client_code = $1 ORDER BY created_at DESC",
                code,
            )
        )
    )


def record_session(s: Setup, appointment: UUID, status: str = "completed") -> str:
    form = s.kasa.get(f"/terapiler/yeni?randevu={appointment}").text
    assert NAME in form and "takvimdeki randevuya bağlanacak" in form
    client_id = scalar(s.local, "SELECT id FROM clients ORDER BY created_at LIMIT 1")
    response = s.kasa.post(
        "/terapiler",
        {
            "client_id": str(client_id),
            "session_date": f"{DAY.isoformat()}T14:00",
            "duration_min": "50",
            "format_code": "individual",
            "method": ["play_therapy"],
            "primary": "play_therapy",
            "status": status,
            "notes": NOTE,
            "appointment_id": str(appointment),
        },
    )
    assert response.status_code == 303, response.text
    return str(response.headers["location"].split("/")[2].split("?")[0])


def cloud_status(s: Setup, appointment: UUID) -> str:
    return str(scalar(s.cloud, "SELECT status FROM appointments WHERE id = $1", appointment))  # type: ignore[arg-type]


def outbox_states(s: Setup) -> list[str]:
    return [r["state"] for r in fetch(s.local, "SELECT state FROM sync_outbox ORDER BY seq")]


# ---------------------------------------------------------------- the happy path


def test_full_round_trip(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    assert outbox_states(s) == ["pending"]

    # 1. the office pushes the new client code; the cloud learns code + therapist, no name
    assert s.push() == 1
    row = fetch(s.cloud, "SELECT * FROM client_codes")[0]  # type: ignore[arg-type]
    assert (row["code"], row["active"], row["assigned_therapist_id"]) == (code, True, A_ID)
    assert outbox_states(s) == ["sent"]

    # 2. B books for A in the cloud; the office mirror picks it up
    appointment = book(s, code)
    assert s.pull() == 2  # the room and the appointment
    mirror = fetch(s.local, "SELECT * FROM appointments_mirror")[0]
    assert mirror["id"] == appointment and mirror["client_code"] == code
    page = s.kasa.get(f"/takvim?gun={DAY.isoformat()}").text
    assert NAME in page and "Seansı kaydet" in page and "Oda 1" in page

    # 3. A records the session from the calendar as completed
    session = record_session(s, appointment)
    assert outbox_states(s) == ["sent", "pending"]
    assert s.push() == 1
    assert cloud_status(s, appointment) == "completed"
    assert scalar(s.cloud, "SELECT updated_via FROM appointments") == "sync"  # type: ignore[arg-type]

    # 4. the new status comes back down; the calendar now links to the session
    assert s.pull() == 1
    assert scalar(s.local, "SELECT status FROM appointments_mirror") == "completed"
    page = s.kasa.get(f"/takvim?gun={DAY.isoformat()}").text
    assert f'href="/terapiler/{session}">Seansı aç' in page

    # 5. pulling again finds nothing new
    assert s.pull() == 0


def test_no_name_or_note_ever_crosses_to_the_cloud(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    record_session(s, appointment)
    s.sync()
    traffic = " ".join(s.recorder.bodies)
    assert code in traffic  # the pseudonymous code does travel
    for secret in ("Elif", "Kaya", "Kabus", "play_therapy", "Oyun"):
        assert secret not in traffic
    dump = scalar(s.cloud, "SELECT string_agg(t::text, ' ') FROM (SELECT * FROM appointments) t")  # type: ignore[arg-type]
    assert "Elif" not in dump


def test_deleting_the_session_reverts_the_booking_to_planned(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    session = record_session(s, appointment, status="no_show")
    s.push()
    assert cloud_status(s, appointment) == "no_show"
    assert s.kasa.post(f"/terapiler/{session}/sil").status_code == 303
    s.push()
    assert cloud_status(s, appointment) == "planned"


def test_outcome_on_a_rescheduled_appointment_is_accepted(setup: Setup) -> None:
    """The compare-and-set is on status, not version: moving the time does not block it."""
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    record_session(s, appointment)
    execute(
        s.cloud,  # type: ignore[arg-type]
        "UPDATE appointments SET starts_at = starts_at + interval '1 hour', version = version + 1",
    )
    s.push()
    assert cloud_status(s, appointment) == "completed"
    assert scalar(s.local, "SELECT count(*) FROM sync_conflicts") == 0


# ---------------------------------------------------------------- idempotency


def test_replaying_the_same_items_changes_nothing(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    record_session(s, appointment)
    s.push()
    version = scalar(s.cloud, "SELECT version FROM appointments")  # type: ignore[arg-type]
    for _ in range(3):
        # As if the worker died after sending but before recording the answer.
        execute(s.local, "UPDATE sync_outbox SET state = 'pending', sent_at = NULL")
        assert s.push() == 2
    assert outbox_states(s) == ["sent", "sent"]
    assert scalar(s.cloud, "SELECT version FROM appointments") == version  # type: ignore[arg-type]
    assert scalar(s.cloud, "SELECT count(*) FROM sync_inbox") == 2  # type: ignore[arg-type]
    assert scalar(s.cloud, "SELECT count(*) FROM conflicts") == 0  # type: ignore[arg-type]
    outcomes = fetch(s.cloud, "SELECT action FROM audit_log WHERE action = 'appointment_outcome'")  # type: ignore[arg-type]
    assert len(outcomes) == 1


# ---------------------------------------------------------------- one side down


def test_cloud_down_work_continues_and_queue_drains_in_order(setup: Setup) -> None:
    s = setup
    first = new_client(s)
    s.push()
    appointment = book(s, first)
    s.pull()

    # The cloud disappears. Clients, sessions and notes still work in the office.
    second = new_client(s, "Can Demir", 15)
    record_session(s, appointment)
    with pytest.raises(CloudUnavailable):
        s.push(down=True)
    with pytest.raises(CloudUnavailable):
        s.pull(down=True)
    assert outbox_states(s) == ["sent", "pending", "pending"]
    assert scalar(s.local, "SELECT attempts FROM sync_outbox WHERE seq = 2") == 0  # not dead
    page = s.kasa.get(f"/takvim?gun={DAY.isoformat()}").text
    assert NAME in page  # the read-only schedule is still there

    # The cloud comes back: the queue drains by itself, in order.
    assert s.push() == 2
    assert outbox_states(s) == ["sent", "sent", "sent"]
    assert cloud_status(s, appointment) == "completed"
    codes = [r["code"] for r in fetch(s.cloud, "SELECT code FROM client_codes ORDER BY code")]  # type: ignore[arg-type]
    assert codes == sorted([first, second])


def test_office_down_the_mirror_catches_up(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    second = new_client(s, "Can Demir", 15)
    s.push()
    # The office is offline: bookings, a move and a cancellation happen in the cloud.
    one = book(s, code, time="09:00")
    two = book(s, second, time="11:00")
    three = book(s, code, time="16:00")
    execute(s.cloud, "UPDATE appointments SET status = 'cancelled' WHERE id = $1", two)  # type: ignore[arg-type]
    execute(
        s.cloud,  # type: ignore[arg-type]
        "UPDATE appointments SET starts_at = starts_at + interval '30 minutes' WHERE id = $1",
        three,
    )
    # Back online: one pull, no manual action.
    s.pull()
    mirror = {r["id"]: r for r in fetch(s.local, "SELECT * FROM appointments_mirror")}
    assert set(mirror) == {one, two, three}
    assert mirror[two]["status"] == "cancelled"
    assert mirror[three]["starts_at"].astimezone(ISTANBUL).strftime("%H:%M") == "16:30"
    cloud_seq = scalar(s.cloud, "SELECT max(change_seq) FROM appointments")  # type: ignore[arg-type]
    assert int(scalar(s.local, "SELECT value FROM sync_state WHERE key = 'cursor'")) == cloud_seq


def test_pull_pages_through_a_large_backlog(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    for _ in range(30):
        execute(s.cloud, "UPDATE appointments SET version = version + 1 WHERE id = $1", appointment)  # type: ignore[arg-type]

    async def small_pages(w: SyncWorker) -> int:
        total = 0
        while True:
            response = await w.cloud.changes(await w.store.cursor(), limit=1)
            total += await w.store.apply_changes(response)
            if not response.has_more:
                return total

    s.worker()(small_pages)
    assert scalar(s.local, "SELECT version FROM appointments_mirror") == 31


def test_cursor_order_equals_commit_order(setup: Setup) -> None:
    """A second writer waits for the first to commit, so no change number is ever skipped."""
    s = setup

    async def go() -> None:
        first = await asyncpg.connect(s.cloud.dsn())
        second = await asyncpg.connect(s.cloud.dsn())
        try:
            tx = first.transaction()
            await tx.start()
            await first.execute("INSERT INTO rooms (internal_label) VALUES ('Oda A')")
            await second.execute("SET lock_timeout = '300ms'")
            with pytest.raises(asyncpg.LockNotAvailableError):
                await second.execute("INSERT INTO rooms (internal_label) VALUES ('Oda B')")
            await tx.commit()
            await second.execute("INSERT INTO rooms (internal_label) VALUES ('Oda B')")
        finally:
            await first.close()
            await second.close()

    run(go())
    seqs = [
        r["change_seq"] for r in fetch(s.cloud, "SELECT change_seq FROM rooms ORDER BY created_at")
    ]  # type: ignore[arg-type]
    assert seqs == sorted(seqs)


# ---------------------------------------------------------------- conflicts


def test_outcome_for_a_cancelled_booking_becomes_a_conflict(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    session = record_session(s, appointment)
    # Meanwhile the booking is cancelled in the cloud, and the therapist corrects the outcome.
    execute(s.cloud, "UPDATE appointments SET status = 'cancelled' WHERE id = $1", appointment)  # type: ignore[arg-type]
    s.kasa.api("PATCH", f"/api/therapy/sessions/{session}", json={"status": "no_show"})
    s.push()

    assert cloud_status(s, appointment) == "cancelled"  # never overwritten automatically
    assert outbox_states(s) == ["sent", "rejected", "held"]
    cloud_conflict = fetch(s.cloud, "SELECT * FROM conflicts")[0]  # type: ignore[arg-type]
    assert (cloud_conflict["kind"], cloud_conflict["reason_code"]) == (
        "sync_status",
        "status_mismatch",
    )
    assert cloud_conflict["author_id"] == A_ID
    local = fetch(s.local, "SELECT * FROM sync_conflicts")[0]
    assert (local["proposed_status"], local["cloud_status"]) == ("completed", "cancelled")

    page = s.kasa.get("/cakismalar").text
    assert NAME in page and "başka biri tarafından değiştirilmiş" in page

    # A keeps the cloud's version. The decision travels to the cloud and closes both sides.
    assert s.kasa.post(f"/cakismalar/{local['id']}/coz", {"karar": "discarded"}).status_code == 303
    assert outbox_states(s) == ["sent", "rejected", "pending", "pending"]
    s.push()
    first, second = fetch(
        s.cloud,  # type: ignore[arg-type]
        "SELECT status, resolution FROM conflicts ORDER BY created_at",
    )
    assert (first["status"], first["resolution"]) == ("resolved", "discarded")
    # The held correction was then evaluated on its own and refused too (still cancelled):
    # it is a separate decision, so it gets its own conflict instead of vanishing.
    assert second["status"] == "open"
    assert cloud_status(s, appointment) == "cancelled"


def test_reapplying_a_rejected_outcome(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    record_session(s, appointment)
    execute(s.cloud, "UPDATE appointments SET status = 'no_show' WHERE id = $1", appointment)  # type: ignore[arg-type]
    s.push()
    s.pull()
    conflict = scalar(s.local, "SELECT id FROM sync_conflicts")
    assert s.kasa.post(f"/cakismalar/{conflict}/coz", {"karar": "reapplied"}).status_code == 303
    s.push()
    assert cloud_status(s, appointment) == "completed"
    assert scalar(s.local, "SELECT status FROM sync_conflicts") == "resolved"


def test_outcome_for_an_unknown_appointment(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    appointment = book(s, code)
    s.pull()
    record_session(s, appointment)
    execute(s.cloud, "DELETE FROM appointments WHERE id = $1", appointment)  # type: ignore[arg-type]
    s.push()
    local = fetch(s.local, "SELECT * FROM sync_conflicts")[0]
    assert (local["kind"], local["reason_code"]) == (
        "sync_unknown_appointment",
        "unknown_appointment",
    )
    assert scalar(s.cloud, "SELECT kind FROM conflicts") == "sync_unknown_appointment"  # type: ignore[arg-type]


def test_a_repeatedly_failing_item_is_parked_after_ten_server_errors(setup: Setup) -> None:
    s = setup
    new_client(s)

    async def failing(w: SyncWorker) -> None:
        from klinik_sync.cloud_client import CloudServerError

        async def boom(_: Any) -> Any:
            raise CloudServerError("HTTP 500")

        w.cloud.push = boom  # type: ignore[method-assign]
        for _ in range(10):
            with pytest.raises(CloudServerError):
                await w.push_once()

    s.worker()(failing)
    assert outbox_states(s) == ["dead"]
    assert scalar(s.local, "SELECT kind FROM sync_conflicts") == "sync_error"


# ---------------------------------------------------------------- restore from an old backup


def test_reconcile_after_restoring_an_old_backup(setup: Setup) -> None:
    """The cloud knows codes this office no longer has: never hand them out again."""
    s = setup
    year = today_istanbul().year
    for n in (1, 2, 7):
        execute(
            s.cloud,  # type: ignore[arg-type]
            "INSERT INTO client_codes (code, active) VALUES ($1, true)",
            f"DN-{year}-{n:04d}",
        )

    async def reconcile(w: SyncWorker) -> list[str]:
        return await w.reconcile()

    missing = s.worker()(reconcile)
    assert missing == [f"DN-{year}-0001", f"DN-{year}-0002", f"DN-{year}-0007"]
    assert new_client(s) == f"DN-{year}-0008"
    s.worker()(reconcile)  # running again does not duplicate the warnings
    rows = fetch(
        s.local, "SELECT client_code FROM sync_conflicts WHERE kind = 'unknown_client_code'"
    )
    assert len(rows) == 3
    assert "bu bilgisayarda kaydı yok" in s.kasa.get("/cakismalar").text


# ---------------------------------------------------------------- credentials and health


def test_bad_token_and_protocol_mismatch_are_not_retried(setup: Setup) -> None:
    s = setup
    with pytest.raises(CloudAuthFailed):
        s.pull(token="sync_wrong")
    with pytest.raises(ProtocolMismatch):
        s.pull(protocol="99")
    execute(s.cloud, "UPDATE service_tokens SET revoked_at = now()")  # type: ignore[arg-type]

    async def run_worker(w: SyncWorker) -> None:
        await asyncio.wait_for(w.run(), timeout=10)

    s.worker()(run_worker)  # stops by itself instead of looping
    assert (
        scalar(s.local, "SELECT value FROM sync_state WHERE key = 'worker_state'") == "auth_failed"
    )
    assert "erişim anahtarı reddedildi" in s.kasa.get("/takvim").text


def test_sync_api_requires_its_token(setup: Setup) -> None:
    s = setup
    with Web(s.cloud) as stranger:
        headers = {PROTOCOL_HEADER: "1"}
        assert stranger.client.get("/sync/v1/changes", headers=headers).status_code == 401
        assert (
            stranger.client.get(
                "/sync/v1/client-codes",
                headers={**headers, "Authorization": "Bearer nope"},
            ).status_code
            == 401
        )
        assert (
            stranger.client.get(
                "/sync/v1/ping", headers={"Authorization": f"Bearer {s.token}"}
            ).status_code
            == 426
        )
    # A logged-in web session is not a sync credential either.
    assert s.web.client.get("/sync/v1/ping", headers={PROTOCOL_HEADER: "1"}).status_code == 401


def test_stale_sync_banner(setup: Setup) -> None:
    s = setup
    assert "henüz bulutla eşitlenmedi" in s.kasa.get("/takvim").text
    s.pull()
    assert "alert--warn" not in s.kasa.get("/takvim").text
    old = (datetime.now(ISTANBUL) - timedelta(minutes=6)).isoformat()
    execute(s.local, "UPDATE sync_state SET value = $1 WHERE key = 'last_pull_ok'", old)
    assert "Bulutla son eşitleme" in s.kasa.get("/takvim").text
    assert "Bulutla son eşitleme" in s.kasa.get("/").text


def test_session_link_rules(setup: Setup) -> None:
    s = setup
    code = new_client(s)
    s.push()
    mine = book(s, code)
    s.pull()
    record_session(s, mine)
    # The same appointment cannot get a second session.
    again = s.kasa.api(
        "POST",
        "/api/therapy/sessions",
        json={
            "client_id": str(scalar(s.local, "SELECT id FROM clients")),
            "session_date": f"{DAY.isoformat()}T14:00:00+03:00",
            "format_code": "individual",
            "methods": [{"code": "cbt"}],
            "appointment_id": str(mine),
        },
    )
    assert again.status_code == 422
    assert again.json()["detail"][0]["type"] == "appointment_taken"
    payload = json.dumps(
        fetch(s.local, "SELECT payload FROM sync_outbox ORDER BY seq")[-1]["payload"]
    )
    assert "Elif" not in payload and NOTE not in payload
