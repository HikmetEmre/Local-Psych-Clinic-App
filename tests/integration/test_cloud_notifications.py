"""Notifications end to end on a real PostgreSQL (brief §8, §16)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from support.cloud import CloudDb, Web, add_cloud_therapists, add_code, add_room
from support.local import A_ID, B_ID, execute, fetch, run, scalar

from klinik_cloud.bildirimler.push import Subscription, deliver_due
from klinik_cloud.bildirimler.service import schedule_reminders
from klinik_shared.realtime import Hub, asyncpg_dsn
from klinik_shared.timeutil import ISTANBUL, now_utc

pytestmark = pytest.mark.integration

CODE = "DN-2026-0042"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def next_monday() -> datetime:
    today = datetime.now(ISTANBUL).replace(hour=0, minute=0, second=0, microsecond=0)
    return today + timedelta(days=7 - today.weekday())


DAY = next_monday().date()


@dataclass
class Clinic:
    db: CloudDb
    a: Web
    b: Web
    room: UUID


@pytest.fixture
def clinic(cloud_db: CloudDb) -> Iterator[Clinic]:
    temporary = add_cloud_therapists(cloud_db)
    room = add_room(cloud_db, "Oda 1")
    add_code(cloud_db, CODE, therapist=A_ID)
    # Daytime-only tests: no quiet hours unless a test sets them.
    for user in (A_ID, B_ID):
        execute(
            cloud_db,  # type: ignore[arg-type]
            "INSERT INTO user_settings (user_id, quiet_start, quiet_end) "
            "VALUES ($1, '00:00', '00:00')",
            user,
        )
    with Web(cloud_db) as a, Web(cloud_db) as b:
        a.onboard("terapist.a", temporary["a"])
        b.onboard("terapist.b", temporary["b"])
        yield Clinic(cloud_db, a, b, room)


def book(who: Web, clinic: Clinic, *, therapist: UUID = A_ID, time: str = "14:00") -> UUID:
    response = who.post(
        "/randevular",
        {
            "client_code": CODE,
            "therapist_id": str(therapist),
            "date": DAY.isoformat(),
            "time": time,
            "duration_min": "50",
            "room_id": str(clinic.room),
        },
    )
    assert response.status_code == 303, response.text
    return UUID(str(scalar(clinic.db, "SELECT id FROM appointments ORDER BY created_at DESC")))  # type: ignore[arg-type]


def notes(clinic: Clinic) -> list[tuple[UUID, str]]:
    rows = fetch(
        clinic.db,  # type: ignore[arg-type]
        "SELECT recipient_id, event_type FROM notifications ORDER BY created_at, recipient_id",
    )
    return [(r["recipient_id"], r["event_type"]) for r in rows]


# ---------------------------------------------------------------- who is told


def test_b_books_for_a_only_a_is_notified(clinic: Clinic) -> None:
    book(clinic.b, clinic)
    assert notes(clinic) == [(A_ID, "appointment.created")]


def test_a_books_for_themselves_b_is_notified(clinic: Clinic) -> None:
    book(clinic.a, clinic)
    assert notes(clinic) == [(B_ID, "appointment.created")]


def test_cancel_and_reassign(clinic: Clinic) -> None:
    appointment = book(clinic.b, clinic)
    clinic.b.post(f"/randevular/{appointment}/iptal", {"version": "1"})
    assert notes(clinic)[-1] == (A_ID, "appointment.cancelled")

    second = book(clinic.a, clinic, time="16:00")
    page = clinic.a.get(f"/randevular/{second}").text
    import re

    fields = {
        name: re.search(rf'name="{name}" value="([^"]*)"', page).group(1)  # type: ignore[union-attr]
        for name in (
            "version",
            "orig_date",
            "orig_time",
            "orig_duration_min",
            "orig_therapist_id",
            "orig_room_id",
        )
    }
    # A moves the appointment to B: B (new therapist) is told; A is the actor and is not.
    clinic.a.post(
        f"/randevular/{second}",
        {
            **fields,
            "therapist_id": str(B_ID),
            "date": fields["orig_date"],
            "time": fields["orig_time"],
            "duration_min": fields["orig_duration_min"],
            "room_id": fields["orig_room_id"],
        },
    )
    last = fetch(
        clinic.db,  # type: ignore[arg-type]
        "SELECT recipient_id, event_type, payload FROM notifications "
        "ORDER BY created_at DESC LIMIT 1",
    )[0]
    assert (last["recipient_id"], last["event_type"]) == (B_ID, "appointment.updated")
    assert json.loads(last["payload"])["changed_fields"] == ["therapist_id"]


def test_rolled_back_change_leaves_no_notification(clinic: Clinic) -> None:
    book(clinic.b, clinic)
    clash = clinic.b.post(
        "/randevular",
        {
            "client_code": CODE,
            "therapist_id": str(A_ID),
            "date": DAY.isoformat(),
            "time": "14:20",
            "duration_min": "50",
            "room_id": str(clinic.room),
        },
    )
    assert clash.status_code == 422
    assert len(notes(clinic)) == 1


def test_conflict_tells_the_admin(clinic: Clinic) -> None:
    appointment = book(clinic.a, clinic)
    execute(
        clinic.db, "UPDATE appointments SET starts_at = starts_at + interval '1 hour', version = 2"
    )  # type: ignore[arg-type]
    page = clinic.b.get(f"/randevular/{appointment}").text
    assert page  # B opened the stale version earlier in real life; simulate with version 1
    response = clinic.b.post(
        f"/randevular/{appointment}",
        {
            "version": "1",
            "orig_date": DAY.isoformat(),
            "orig_time": "14:00",
            "orig_duration_min": "50",
            "orig_therapist_id": str(A_ID),
            "orig_room_id": str(clinic.room),
            "therapist_id": str(A_ID),
            "date": DAY.isoformat(),
            "time": "10:00",
            "duration_min": "50",
            "room_id": str(clinic.room),
        },
    )
    assert response.status_code == 409
    assert (A_ID, "conflict.created") in notes(clinic)


# ---------------------------------------------------------------- inbox and bell


def test_inbox_bell_and_read_state(clinic: Clinic) -> None:
    book(clinic.b, clinic)
    bell = clinic.a.get("/bildirimler/sayac").text
    assert 'class="bell__count">1<' in bell
    inbox = clinic.a.get("/bildirimler").text
    assert "Yeni randevu" in inbox and CODE in inbox and "14:00" in inbox
    notification = scalar(clinic.db, "SELECT id FROM notifications")  # type: ignore[arg-type]
    opened = clinic.a.post(f"/bildirimler/{notification}/ac")
    assert (
        opened.status_code == 303 and opened.headers["location"] == f"/takvim?gun={DAY.isoformat()}"
    )
    assert "bell__count" not in clinic.a.get("/bildirimler/sayac").text
    # B cannot open A's notification.
    clinic.b.post(f"/bildirimler/{notification}/ac")
    assert scalar(clinic.db, "SELECT count(*) FROM notifications WHERE read_at IS NOT NULL") == 1  # type: ignore[arg-type]


def test_settings_and_quiet_hours(clinic: Clinic) -> None:
    saved = clinic.a.post(
        "/bildirimler/ayarlar",
        {"quiet_start": "00:00", "quiet_end": "23:59", "push_enabled": "1", "reminder_24h": "1"},
    )
    assert saved.status_code == 303
    book(clinic.b, clinic)
    row = fetch(clinic.db, "SELECT push_after, created_at FROM notifications")[0]  # type: ignore[arg-type]
    assert row["push_after"] > row["created_at"]  # waits for quiet hours to end
    assert (
        scalar(clinic.db, "SELECT reminder_1h FROM user_settings WHERE user_id = $1", A_ID) is False
    )  # type: ignore[arg-type]


def test_push_turned_off_still_fills_the_inbox(clinic: Clinic) -> None:
    clinic.a.post("/bildirimler/ayarlar", {"quiet_start": "00:00", "quiet_end": "00:00"})
    book(clinic.b, clinic)
    assert scalar(clinic.db, "SELECT push_state FROM notifications") == "skipped"  # type: ignore[arg-type]
    assert "Yeni randevu" in clinic.a.get("/bildirimler").text


# ---------------------------------------------------------------- devices


def test_subscribe_and_unsubscribe_a_device(clinic: Clinic) -> None:
    body = {"endpoint": ENDPOINT, "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}
    assert clinic.a.api("POST", "/push/abonelik", json=body).status_code == 201
    assert clinic.a.api("POST", "/push/abonelik", json=body).status_code == 201  # idempotent
    assert scalar(clinic.db, "SELECT count(*) FROM push_subscriptions") == 1  # type: ignore[arg-type]
    bad = {"endpoint": "http://insecure.example/x", "keys": body["keys"]}
    assert clinic.a.api("POST", "/push/abonelik", json=bad).status_code == 422
    assert clinic.a.client.post("/push/abonelik", json=body).status_code == 403  # no CSRF header
    assert (
        clinic.a.api("POST", "/push/abonelik/sil", json={"endpoint": ENDPOINT}).status_code == 200
    )
    assert scalar(clinic.db, "SELECT count(*) FROM push_subscriptions") == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------- delivery


@dataclass
class FakeSender:
    statuses: list[int]
    sent: list[dict[str, Any]] = field(default_factory=list)

    async def send(self, subscription: Subscription, data: dict[str, Any]) -> int:
        self.sent.append(data)
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def deliver(clinic: Clinic, sender: FakeSender, *, at: datetime | None = None) -> Any:
    async def go() -> Any:
        engine: AsyncEngine = create_async_engine(
            clinic.db.app_url, connect_args={"server_settings": {"search_path": "app"}}
        )
        try:
            return await deliver_due(engine, sender, now=at)
        finally:
            await engine.dispose()

    return run(go())


def subscribe(clinic: Clinic) -> None:
    clinic.a.api(
        "POST",
        "/push/abonelik",
        json={"endpoint": ENDPOINT, "keys": {"p256dh": "B" * 87, "auth": "a" * 22}},
    )


def test_push_success(clinic: Clinic) -> None:
    subscribe(clinic)
    book(clinic.b, clinic)
    sender = FakeSender([201])
    report = deliver(clinic, sender)
    assert report.sent == 1
    assert sender.sent[0]["title"] == "🗓 Yeni randevu"
    assert sender.sent[0]["body"].endswith(CODE)
    assert scalar(clinic.db, "SELECT push_state FROM notifications") == "sent"  # type: ignore[arg-type]
    assert deliver(clinic, sender).sent == 0  # never twice


def test_gone_subscription_is_removed(clinic: Clinic) -> None:
    subscribe(clinic)
    book(clinic.b, clinic)
    report = deliver(clinic, FakeSender([410]))
    assert report.removed_subscriptions == 1 and report.skipped == 1
    assert scalar(clinic.db, "SELECT count(*) FROM push_subscriptions") == 0  # type: ignore[arg-type]


def test_retry_with_backoff_then_give_up(clinic: Clinic) -> None:
    subscribe(clinic)
    book(clinic.b, clinic)
    sender = FakeSender([500])
    moment = now_utc()
    expected_gaps = [
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(hours=1),
    ]
    for gap in expected_gaps:
        assert deliver(clinic, sender, at=moment).retried == 1
        next_attempt = scalar(clinic.db, "SELECT next_attempt_at FROM notifications")  # type: ignore[arg-type]
        assert next_attempt - moment == gap
        assert deliver(clinic, sender, at=moment).retried == 0  # not due yet
        moment = next_attempt
    assert deliver(clinic, sender, at=moment).failed == 1
    row = fetch(clinic.db, "SELECT push_state, push_attempts FROM notifications")[0]  # type: ignore[arg-type]
    assert (row["push_state"], row["push_attempts"]) == ("failed", 5)
    assert "Yeni randevu" in clinic.a.get("/bildirimler").text  # the inbox keeps it


def test_read_or_expired_notifications_are_not_pushed(clinic: Clinic) -> None:
    subscribe(clinic)
    book(clinic.b, clinic)
    execute(clinic.db, "UPDATE notifications SET read_at = now()")  # type: ignore[arg-type]
    sender = FakeSender([201])
    assert deliver(clinic, sender).skipped == 1 and sender.sent == []


def test_no_device_means_skipped(clinic: Clinic) -> None:
    book(clinic.b, clinic)
    assert deliver(clinic, FakeSender([201])).skipped == 1


# ---------------------------------------------------------------- reminders


def remind(clinic: Clinic, at: datetime) -> int:
    async def go() -> int:
        engine = create_async_engine(
            clinic.db.app_url, connect_args={"server_settings": {"search_path": "app"}}
        )
        try:
            async with engine.begin() as conn:
                return await schedule_reminders(conn, at)
        finally:
            await engine.dispose()

    return run(go())


def test_reminders_are_sent_once_and_rearm_after_a_move(clinic: Clinic) -> None:
    appointment = book(clinic.b, clinic)
    starts = scalar(clinic.db, "SELECT starts_at FROM appointments")  # type: ignore[arg-type]
    assert remind(clinic, starts - timedelta(hours=23, minutes=30)) == 1
    assert remind(clinic, starts - timedelta(hours=23, minutes=10)) == 0  # deduplicated
    assert remind(clinic, starts - timedelta(minutes=50)) == 1
    assert remind(clinic, starts - timedelta(minutes=46)) == 0
    events = [e for _, e in notes(clinic) if "reminder" in e]
    assert events == ["appointment.reminder_24h", "appointment.reminder_1h"]

    execute(
        clinic.db,
        "UPDATE appointments SET starts_at = starts_at + interval '2 hours' WHERE id = $1",
        appointment,
    )  # type: ignore[arg-type]
    assert remind(clinic, starts + timedelta(hours=2) - timedelta(minutes=50)) == 1


def test_reminders_respect_the_setting_and_status(clinic: Clinic) -> None:
    clinic.a.post("/bildirimler/ayarlar", {"quiet_start": "00:00", "quiet_end": "00:00",
                                            "push_enabled": "1", "reminder_1h": "1"})  # fmt: skip
    appointment = book(clinic.b, clinic)
    starts = scalar(clinic.db, "SELECT starts_at FROM appointments")  # type: ignore[arg-type]
    assert remind(clinic, starts - timedelta(hours=23, minutes=30)) == 0  # 24 h turned off
    clinic.b.post(f"/randevular/{appointment}/iptal", {"version": "1"})
    assert remind(clinic, starts - timedelta(minutes=50)) == 0  # cancelled


# ---------------------------------------------------------------- live updates


def test_hub_delivers_schedule_and_personal_events(clinic: Clinic) -> None:
    async def go() -> tuple[list[str], list[str]]:
        hub = Hub(
            asyncpg_dsn(clinic.db.owner_url),
            broadcast={"schedule_changed": "calendar"},
            per_user={"new_notification": "notification"},
        )
        hub.start()
        await asyncio.wait_for(hub.wait_connected(), 5)
        got_a: list[str] = []
        got_b: list[str] = []
        with hub.subscribe(A_ID) as queue_a, hub.subscribe(B_ID) as queue_b:
            conn = await asyncpg.connect(clinic.db.dsn())
            try:
                # A real booking: the trigger announces the change, the service the inbox row.
                await conn.execute(
                    "INSERT INTO appointments "
                    "(client_code, therapist_id, room_id, starts_at, duration_min) "
                    "VALUES ($1, $2, $3, now() + interval '3 days', 50)",
                    CODE,
                    A_ID,
                    clinic.room,
                )
                await conn.execute("SELECT pg_notify('new_notification', $1)", str(A_ID))
            finally:
                await conn.close()
            for queue, sink in ((queue_a, got_a), (queue_b, got_b)):
                while True:
                    try:
                        sink.append(await asyncio.wait_for(queue.get(), 2))
                    except TimeoutError:
                        break
        await hub.stop()
        return got_a, got_b

    got_a, got_b = run(go())
    assert any("event: calendar" in f for f in got_a) and any("event: calendar" in f for f in got_b)
    assert any("event: notification" in f for f in got_a)
    assert not any("event: notification" in f for f in got_b)


def test_event_stream_and_pwa_files(clinic: Clinic) -> None:
    with Web(clinic.db) as stranger:
        assert stranger.get("/events").headers["location"] == "/login"
        manifest = stranger.get("/manifest.webmanifest")
        assert manifest.status_code == 200 and manifest.json()["start_url"] == "/takvim"
        sw = stranger.get("/sw.js")
        assert sw.status_code == 200 and sw.headers["service-worker-allowed"] == "/"
        assert stranger.get("/offline").status_code == 200
    page = clinic.a.get("/takvim").text
    assert 'sse-connect="/events"' in page and 'hx-trigger="sse:calendar"' in page
    assert 'rel="manifest"' in page and "/bildirimler/sayac" in page


def test_forced_password_page_has_no_background_requests(cloud_db: CloudDb) -> None:
    """Regression: the bell's request was redirected to this page and reloaded it forever."""
    temporary = add_cloud_therapists(cloud_db)
    with Web(cloud_db) as web:
        from klinik_shared.authkit import totp

        assert web.submit_password("terapist.a", temporary["a"]).status_code == 303
        page = web.get("/hesap/2fa/kur").text
        import re

        secret = re.search(r'class="secret">([A-Z2-7]+)<', page).group(1)  # type: ignore[union-attr]
        web.post(
            "/hesap/2fa/kur",
            {"code": totp.code_at(secret, now_utc())},
            csrf=re.search(r'name="csrf_token" value="([^"]+)"', page).group(1),  # type: ignore[union-attr]
        )
        forced = web.get("/hesap/sifre").text
        assert "sse-connect" not in forced and "/bildirimler/sayac" not in forced
