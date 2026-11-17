"""Fill an EMPTY development database with fictional demo data, to look at the app.

    LOCAL_MIGRATION_DATABASE_URL=... LOCAL_FERNET_KEYS=... LOCAL_AUTH_FERNET_KEY=... \\
        python scripts/demo_seed.py

Never run it against a real clinic database: it refuses if any client exists. Every name
here is made up. Creates an admin and two therapists (temporary passwords are printed),
three rooms, nine clients, a month of appointments with session records, notes and
payments, and prints a replica token for a demo laptop copy.
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from cryptography.fernet import MultiFernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher, parse_keys
from klinik_local.danisanlar.service import ClientInput, ClientService
from klinik_local.gelir.service import IncomeService
from klinik_local.randevular.service import AppointmentFields, AppointmentService
from klinik_local.replika.office import new_token
from klinik_local.terapiler.service import MethodRef, SessionCreate, SessionService
from klinik_shared.authkit.admin import bootstrap_users
from klinik_shared.authkit.service import AuthStore, CurrentUser
from klinik_shared.enums import AppointmentStatus
from klinik_shared.permissions import PERMISSION_SETS
from klinik_shared.timeutil import ISTANBUL, now_utc

random.seed(42)

USERS = [
    ("yonetici", "Klinik Sahibi", "admin"),
    ("ayse.demir", "Ayşe Demir", "therapist"),
    ("can.kaya", "Can Kaya", "therapist"),
]
# name, birth date, phone, therapist username, weekday, hour, modality
CLIENTS = [
    ("Elif Yıldız", date(2017, 4, 12), "0532 111 22 33", "ayse.demir", 0, 10, "play_therapy"),
    ("Mert Aydın", date(2015, 9, 3), "0533 222 33 44", "ayse.demir", 2, 15, "sandtray"),
    ("Zeynep Koç", date(2010, 1, 25), "0534 333 44 55", "ayse.demir", 3, 16, "cbt"),
    ("Ahmet Şahin", date(1985, 6, 30), "0535 444 55 66", "can.kaya", 0, 18, "cbt"),
    ("Selin Arslan", date(1992, 11, 8), "0536 555 66 77", "can.kaya", 1, 11, "emdr"),
    ("Burak Öztürk", date(1979, 2, 14), "0537 666 77 88", "can.kaya", 3, 14, "act"),
    ("Deniz Çelik", date(1996, 8, 21), "0538 777 88 99", "yonetici", 1, 17, "schema_therapy"),
    ("Gül Erdem", date(1968, 3, 2), "0539 888 99 00", "yonetici", 4, 10, "psychodynamic"),
    ("Hasan Polat", date(1955, 12, 19), "0530 999 00 11", "can.kaya", 4, 15, "reminiscence"),
]
NOTES = [
    "Hafta boyunca uykuda düzelme olduğunu anlattı. Okul/iş stresi azalmış.",
    "Kaygı tetikleyicilerini birlikte listeledik; nefes egzersizini seansta uyguladık.",
    "Aile ile yaşanan tartışmayı konuştuk. Sınır koyma üzerine çalıştık.",
    "Oyun sırasında kardeş temalı figürler kullandı; öfke ifadesi daha düzenli.",
    "Olumsuz otomatik düşünceler kaydı getirdi; iki düşünceyi birlikte yeniden çerçeveledik.",
    "Geçmiş anılar üzerine konuştuk; duygusal olarak yorgun ama iş birliği yüksek.",
]
HOMEWORK = [
    "Her akşam duygu günlüğü doldurmak.",
    "Günde iki kez 4-7-8 nefes egzersizi.",
    "Haftada üç gün 20 dakika yürüyüş.",
    "",
]


def actor(user_id: UUID, username: str, name: str, role: str) -> CurrentUser:
    return CurrentUser(
        id=user_id, username=username, display_name=name,
        permissions=frozenset(p.value for p in PERMISSION_SETS[role]),
        must_change_password=False, session_id_hash=b"", csrf_token="",
    )  # fmt: skip


async def as_user(conn: AsyncConnection, user: CurrentUser) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(user.id)}
    )


async def main() -> None:
    url = os.environ["LOCAL_MIGRATION_DATABASE_URL"]
    cipher = FieldCipher(os.environ["LOCAL_FERNET_KEYS"])
    store = AuthStore(
        t.auth, t.audit_log, MultiFernet(parse_keys(os.environ["LOCAL_AUTH_FERNET_KEY"]))
    )
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        if (await conn.execute(text("SELECT count(*) FROM clients"))).scalar_one():
            raise SystemExit("refusing: this database already has clients")

    ids = {u: uuid4() for u, _, _ in USERS}
    created = await bootstrap_users(
        engine,
        store,
        [
            {"user_id": ids[u], "username": u, "display_name": n, "permissions": PERMISSION_SETS[r]}
            for u, n, r in USERS
        ],
    )
    people = {u: actor(ids[u], u, n, r) for u, n, r in USERS}
    admin = people["yonetici"]

    clients_svc = ClientService(cipher)
    appts = AppointmentService(cipher)
    sessions = SessionService(cipher)
    income = IncomeService(cipher)
    today = datetime.now(ISTANBUL).date()
    first = today.replace(day=1)
    if (today - first).days > 28:
        first = today - timedelta(days=28)
    horizon = today + timedelta(days=21)

    async with engine.begin() as conn:
        await as_user(conn, admin)
        rooms = {}
        for label, order in (("Oda 1", 1), ("Oda 2", 2), ("Oyun Odası", 3)):
            rooms[label] = (
                await conn.execute(
                    t.rooms.insert().values(label=label, sort_order=order).returning(t.rooms.c.id)
                )
            ).scalar_one()
        for username, room in (
            ("ayse.demir", "Oyun Odası"),
            ("can.kaya", "Oda 2"),
            ("yonetici", "Oda 1"),
        ):
            await conn.execute(
                t.users.update()
                .where(t.users.c.id == ids[username])
                .values(default_room_id=rooms[room])
            )

    counts = {"appointments": 0, "sessions": 0, "payments": 0}
    for name, birth, phone, who, weekday, hour, modality in CLIENTS:
        therapist = people[who]
        async with engine.begin() as conn:
            await as_user(conn, admin)
            client = await clients_svc.create(
                conn, admin, ClientInput(name, phone, None, birth, therapist.id)
            )
        child = (today.year - birth.year) < 13
        day = first + timedelta(days=(weekday - first.weekday()) % 7)
        while day <= horizon:
            starts = datetime.combine(day, time(hour, 0), tzinfo=ISTANBUL)
            async with engine.begin() as conn:
                await as_user(conn, admin)
                appt = await appts.create(
                    conn, admin, client_id=client.id,
                    fields=AppointmentFields(starts_at=starts, therapist_id=therapist.id, duration_min=50),
                )  # fmt: skip
                counts["appointments"] += 1
                roll = random.random()
                if starts < now_utc() and roll < 0.08:
                    await appts.cancel(conn, admin, appt.id, appt.version)
                elif starts < now_utc():
                    await as_user(conn, therapist)
                    no_show = roll < 0.16
                    await sessions.create(
                        conn,
                        therapist,
                        SessionCreate(
                            client_id=client.id,
                            session_date=starts,
                            duration_min=50,
                            format_code="individual",
                            methods=[] if no_show else [MethodRef(code=modality, is_primary=True)],
                            status=AppointmentStatus.NO_SHOW
                            if no_show
                            else AppointmentStatus.COMPLETED,
                            notes=None if no_show else random.choice(NOTES),
                            homework=None if no_show else (random.choice(HOMEWORK) or None),
                            appointment_id=appt.id,
                        ),
                    )
                    counts["sessions"] += 1
                    if not no_show:
                        await as_user(conn, admin)
                        await income.record(
                            conn, admin, appointment_id=appt.id,
                            amount=Decimal("1200") if child else Decimal("1500"),
                            method=random.choice(["cash", "card", "transfer"]),
                            paid_on=day,
                        )  # fmt: skip
                        counts["payments"] += 1
            day += timedelta(days=7)

    token, digest = new_token()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO replicas (label, token_hash, created_by) VALUES (:l, :h, :u)"),
            {"l": "Demo dizüstü", "h": digest, "u": admin.id},
        )
    await engine.dispose()

    print(f"demo data: {counts}")
    for username, password in created.items():
        print(f"user {username}  temporary password: {password}")
    print(f"replica token: {token}")


if __name__ == "__main__":
    asyncio.run(main())
