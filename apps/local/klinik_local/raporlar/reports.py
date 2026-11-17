"""The five PDF reports. Every query runs inside the viewer's transaction, so row-level
security applies on top of the checks here (a therapist never receives a colleague's
session rows, whatever the parameters say).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from reportlab.lib.units import mm
from reportlab.platypus import CondPageBreak, Flowable, PageBreak, Spacer
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_local.gelir.service import VOID_REASONS, format_try
from klinik_local.raporlar import pdf
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.calendar import DAYS_TR, MONTHS_TR
from klinik_shared.labels_tr import PAYMENT_METHOD_LABELS, STATUS_LABELS
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import istanbul_day_bounds, now_utc, to_istanbul

KINDS = {
    "seanslar": "Seans raporu",
    "danisan": "Danışan dosyası",
    "randevular": "Randevu listesi",
    "gelir": "Gelir raporu",
    "istatistik": "Randevu istatistikleri",
}


class ReportNotFound(Exception):
    pass


@dataclass(frozen=True)
class Params:
    kind: str
    first: date
    last: date  # inclusive
    therapist_id: UUID | None = None
    room_id: UUID | None = None
    client_id: UUID | None = None
    include_notes: bool = False
    include_cancelled: bool = True

    @property
    def bounds(self) -> tuple[datetime, datetime]:
        return istanbul_day_bounds(self.first)[0], istanbul_day_bounds(self.last)[1]


# ---------------------------------------------------------------- formatting


def d(value: date | datetime | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = to_istanbul(value)
        return value.strftime("%d.%m.%Y %H:%M")
    return value.strftime("%d.%m.%Y")


def hm(value: datetime) -> str:
    return to_istanbul(value).strftime("%H:%M")


def period_text(first: date, last: date) -> str:
    if first == last:
        return f"{first.day} {MONTHS_TR[first.month - 1]} {first.year}"
    if (first.year, first.month) == (last.year, last.month) and first.day == 1:
        following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        if last == following - timedelta(days=1):
            return f"{MONTHS_TR[first.month - 1]} {first.year}"
    return f"{d(first)} – {d(last)}"


def hours(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} sa {m:02d} dk" if h else f"{m} dk"


def percent(part: int, whole: int) -> str:
    return f"%{round(100 * part / whole)}" if whole else "—"


def age_on(birth: date | None, when: date) -> str:
    if birth is None:
        return "—"
    years = when.year - birth.year - ((when.month, when.day) < (birth.month, birth.day))
    return f"{years}"


# ---------------------------------------------------------------- shared lookups


async def user_names(conn: AsyncConnection) -> dict[UUID, str]:
    rows = (await conn.execute(select(t.users.c.id, t.users.c.display_name))).all()
    return {r.id: r.display_name for r in rows}


async def visible_sessions(
    conn: AsyncConnection,
    cipher: FieldCipher,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    therapist_id: UUID | None = None,
    client_id: UUID | None = None,
    with_notes: bool = False,
) -> list[dict[str, Any]]:
    s, c, f = t.therapy_sessions, t.clients, t.therapy_methods.alias("format")
    ag = t.age_groups
    stmt = (
        select(
            s.c.id, s.c.client_id, s.c.therapist_id, s.c.session_number, s.c.session_date,
            s.c.duration_min, s.c.status, s.c.notes_encrypted, s.c.homework_encrypted,
            s.c.next_session_date, c.c.client_code, c.c.full_name_encrypted,
            f.c.label_tr.label("format_label"), ag.c.label_tr.label("age_group"),
        )
        .select_from(
            s.join(c, c.c.id == s.c.client_id)
            .join(f, f.c.id == s.c.format_method_id)
            .join(ag, ag.c.id == s.c.age_group_id_at_session)
        )
        .where(s.c.deleted_at.is_(None))
        .order_by(s.c.session_date)
    )  # fmt: skip
    if start is not None and end is not None:
        stmt = stmt.where(s.c.session_date >= start, s.c.session_date < end)
    if therapist_id is not None:
        stmt = stmt.where(s.c.therapist_id == therapist_id)
    if client_id is not None:
        stmt = stmt.where(s.c.client_id == client_id)
    rows = (await conn.execute(stmt)).all()
    methods: dict[UUID, list[tuple[str, bool]]] = defaultdict(list)
    if rows:
        sm, m = t.session_methods, t.therapy_methods
        for mr in (
            await conn.execute(
                select(sm.c.session_id, m.c.label_tr, sm.c.is_primary)
                .join(m, m.c.id == sm.c.therapy_method_id)
                .where(sm.c.session_id.in_([r.id for r in rows]))
                .order_by(sm.c.is_primary.desc(), m.c.label_tr)
            )
        ).all():
            methods[mr.session_id].append((mr.label_tr, mr.is_primary))
    out = []
    for r in rows:
        item = {
            "id": r.id,
            "client_id": r.client_id,
            "therapist_id": r.therapist_id,
            "number": r.session_number,
            "date": r.session_date,
            "minutes": r.duration_min,
            "status": r.status,
            "code": r.client_code,
            "client": cipher.decrypt(r.full_name_encrypted),
            "format": r.format_label,
            "age_group": r.age_group,
            "methods": methods.get(r.id, []),
            "next": r.next_session_date,
        }
        if with_notes:
            item["notes"] = cipher.decrypt_optional(r.notes_encrypted) or ""
            item["homework"] = cipher.decrypt_optional(r.homework_encrypted) or ""
        out.append(item)
    return out


async def visible_appointments(
    conn: AsyncConnection,
    cipher: FieldCipher,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    therapist_id: UUID | None = None,
    room_id: UUID | None = None,
    client_id: UUID | None = None,
    include_cancelled: bool = True,
) -> list[dict[str, Any]]:
    a, c, r = t.appointments, t.clients, t.rooms
    stmt = (
        select(
            a.c.id, a.c.client_id, a.c.therapist_id, a.c.room_id, a.c.starts_at, a.c.ends_at,
            a.c.duration_min, a.c.status, c.c.client_code, c.c.full_name_encrypted,
            r.c.label.label("room"),
        )
        .select_from(a.join(c, c.c.id == a.c.client_id).join(r, r.c.id == a.c.room_id))
        .where(a.c.deleted_at.is_(None))
        .order_by(a.c.starts_at)
    )  # fmt: skip
    if start is not None and end is not None:
        stmt = stmt.where(a.c.starts_at >= start, a.c.starts_at < end)
    if therapist_id is not None:
        stmt = stmt.where(a.c.therapist_id == therapist_id)
    if room_id is not None:
        stmt = stmt.where(a.c.room_id == room_id)
    if client_id is not None:
        stmt = stmt.where(a.c.client_id == client_id)
    if not include_cancelled:
        stmt = stmt.where(a.c.status != "cancelled")
    return [
        {
            "id": x.id, "client_id": x.client_id, "therapist_id": x.therapist_id,
            "room_id": x.room_id, "start": x.starts_at, "end": x.ends_at,
            "minutes": x.duration_min, "status": x.status, "code": x.client_code,
            "client": cipher.decrypt(x.full_name_encrypted), "room": x.room,
        }
        for x in (await conn.execute(stmt)).all()
    ]  # fmt: skip


async def payments_for(
    conn: AsyncConnection,
    cipher: FieldCipher,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    therapist_id: UUID | None = None,
    client_id: UUID | None = None,
) -> list[dict[str, Any]]:
    p, a, c = t.payments, t.appointments, t.clients
    recorder = t.users.alias("recorder")
    stmt = (
        select(
            p.c.id, p.c.amount, p.c.method, p.c.paid_at, p.c.voided_at, p.c.void_reason,
            a.c.therapist_id, a.c.starts_at, c.c.client_code, c.c.full_name_encrypted,
            recorder.c.display_name.label("recorder"),
        )
        .select_from(
            p.join(a, a.c.id == p.c.appointment_id)
            .join(c, c.c.id == a.c.client_id)
            .join(recorder, recorder.c.id == p.c.recorded_by)
        )
        .order_by(p.c.paid_at)
    )  # fmt: skip
    if start is not None and end is not None:
        stmt = stmt.where(p.c.paid_at >= start, p.c.paid_at < end)
    if therapist_id is not None:
        stmt = stmt.where(a.c.therapist_id == therapist_id)
    if client_id is not None:
        stmt = stmt.where(a.c.client_id == client_id)
    return [
        {
            "id": x.id, "amount": x.amount, "method": x.method, "paid_at": x.paid_at,
            "voided": x.voided_at is not None, "void_reason": x.void_reason,
            "therapist_id": x.therapist_id, "appointment_at": x.starts_at,
            "code": x.client_code, "client": cipher.decrypt(x.full_name_encrypted),
            "recorder": x.recorder,
        }
        for x in (await conn.execute(stmt)).all()
    ]  # fmt: skip


def method_text(methods: list[tuple[str, bool]]) -> str:
    return ", ".join(f"{label}{' (birincil)' if primary else ''}" for label, primary in methods)


def status_counts(items: list[dict[str, Any]]) -> Counter[str]:
    return Counter(item["status"] for item in items)


def notes_blocks(sessions: list[dict[str, Any]], names: dict[UUID, str]) -> list[Flowable]:
    out: list[Flowable] = []
    for s in sessions:
        if not (s.get("notes") or s.get("homework")):
            continue
        title = (
            f"{d(s['date'])} · {s['client']} ({s['code']}) · Seans {s['number']} · "
            f"{names.get(s['therapist_id'], '—')}"
        )
        block: list[Flowable] = [pdf.p(title, pdf.H3)]
        if s.get("notes"):
            block.append(pdf.markup("<b>Not</b>", pdf.SMALL))
            block.append(pdf.p(s["notes"], pdf.NOTE))
        if s.get("homework"):
            block.append(pdf.markup("<b>Ödev</b>", pdf.SMALL))
            block.append(pdf.p(s["homework"], pdf.NOTE))
        out.append(pdf.keep(*block))
    return out or [pdf.empty("Bu seanslarda not veya ödev yazılmamış.")]


# ---------------------------------------------------------------- 1. sessions


async def session_report(
    conn: AsyncConnection, cipher: FieldCipher, user: CurrentUser, params: Params, meta: pdf.Meta
) -> list[Flowable]:
    sees_all = user.has(Permission.SESSIONS_READ_OTHERS)
    therapist_id = params.therapist_id if sees_all else user.id
    start, end = params.bounds
    names = await user_names(conn)
    sessions = await visible_sessions(
        conn, cipher, start=start, end=end, therapist_id=therapist_id,
        with_notes=params.include_notes,
    )  # fmt: skip
    width = pdf.content_width(meta)
    scope = names.get(therapist_id, "—") if therapist_id else "Tüm terapistler"
    story = pdf.heading(
        "Seans raporu",
        [f"Dönem: {period_text(params.first, params.last)}", f"Terapist: {scope}"],
    )
    counts = status_counts(sessions)
    done = [s for s in sessions if s["status"] == "completed"]
    minutes = sum(s["minutes"] for s in done)
    story.append(
        pdf.facts(
            [
                ("Toplam seans", len(sessions)),
                ("Tamamlanan", counts["completed"]),
                ("Gelmedi", counts["no_show"]),
                ("İptal", counts["cancelled"]),
                ("Planlanan", counts["planned"]),
                ("Farklı danışan", len({s["client_id"] for s in sessions})),
                ("Toplam seans süresi", hours(minutes)),
                ("Ortalama süre", f"{round(minutes / len(done))} dk" if done else "—"),
                ("Katılım oranı", percent(counts["completed"], counts["completed"] + counts["no_show"])),
                ("Notlu seans", sum(1 for s in sessions if s.get("notes")) if params.include_notes else "—"),
            ],
            width,
        )
    )  # fmt: skip
    if not sessions:
        return [*story, Spacer(1, 8), pdf.empty()]

    if therapist_id is None:
        story += pdf.section("Terapistlere göre")
        per: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        for s in sessions:
            per[s["therapist_id"]].append(s)
        rows = []
        for tid, items in sorted(per.items(), key=lambda kv: names.get(kv[0], "")):
            c = status_counts(items)
            rows.append(
                [
                    names.get(tid, "—"), len(items), c["completed"], c["no_show"], c["cancelled"],
                    hours(sum(i["minutes"] for i in items if i["status"] == "completed")),
                    len({i["client_id"] for i in items}),
                ]
            )  # fmt: skip
        story.append(
            pdf.table(
                ["Terapist", "Seans", "Tamamlanan", "Gelmedi", "İptal", "Süre", "Danışan"],
                rows, [width * x for x in (0.28, 0.1, 0.14, 0.11, 0.09, 0.16, 0.12)],
                right=(1, 2, 3, 4, 5, 6),
            )
        )  # fmt: skip

    method_use: Counter[str] = Counter()
    for s in done:
        for label, _ in s["methods"]:
            method_use[label] += 1
    story += pdf.section("Kullanılan yöntemler (tamamlanan seanslar)")
    if method_use:
        story.append(pdf.Bars([(k, v, str(v)) for k, v in method_use.most_common()], width))
    else:
        story.append(pdf.empty("Tamamlanan seans yok."))

    story += pdf.section("Biçim ve yaş grubu")
    formats = Counter(s["format"] for s in sessions)
    groups = Counter(s["age_group"] for s in sessions)
    story.append(
        pdf.table(
            ["Seans biçimi", "Seans"], [[k, v] for k, v in formats.most_common()],
            [width * 0.7, width * 0.3], right=(1,),
        )
    )  # fmt: skip
    story.append(Spacer(1, 6))
    story.append(
        pdf.table(
            ["Yaş grubu (seans tarihinde)", "Seans"], [[k, v] for k, v in groups.most_common()],
            [width * 0.7, width * 0.3], right=(1,),
        )
    )  # fmt: skip

    story += pdf.section("Seans listesi")
    multi = therapist_id is None
    header = [
        "Tarih",
        "Danışan",
        "No",
        *(["Terapist"] if multi else []),
        "Biçim",
        "Yöntemler",
        "Durum",
        "Süre",
    ]
    widths = (
        (0.13, 0.2, 0.05, 0.13, 0.11, 0.2, 0.1, 0.08) if multi
        else (0.14, 0.24, 0.05, 0.13, 0.26, 0.1, 0.08)
    )  # fmt: skip
    rows = [
        [
            d(s["date"]), f"{s['client']}\n{s['code']}", s["number"],
            *([names.get(s["therapist_id"], "—")] if multi else []),
            s["format"], method_text(s["methods"]) or "—",
            STATUS_LABELS.get(s["status"], s["status"]), f"{s['minutes']} dk",
        ]
        for s in sessions
    ]  # fmt: skip
    struck = [i for i, s in enumerate(sessions) if s["status"] == "cancelled"]
    story.append(
        pdf.table(
            header, rows, [width * w for w in widths], right=(2, len(header) - 1), struck=struck
        )
    )
    if params.include_notes:
        story.append(PageBreak())
        story += [pdf.p("Seans notları ve ödevler", pdf.H2)]
        story += notes_blocks(sessions, names)
    return story


# ---------------------------------------------------------------- 2. client file


async def client_file(
    conn: AsyncConnection, cipher: FieldCipher, user: CurrentUser, params: Params, meta: pdf.Meta
) -> list[Flowable]:
    if params.client_id is None:
        raise ReportNotFound
    c = t.clients
    row = (
        await conn.execute(select(c).where(c.c.id == params.client_id, c.c.deleted_at.is_(None)))
    ).one_or_none()
    if row is None:
        raise ReportNotFound
    names = await user_names(conn)
    sessions = await visible_sessions(
        conn, cipher, client_id=row.id, with_notes=params.include_notes
    )
    sees_all = user.has(Permission.SESSIONS_READ_OTHERS)
    if not sees_all and row.assigned_therapist_id != user.id and not sessions:
        # Not this therapist's client: the file is not theirs to print.
        raise ReportNotFound
    appointments = await visible_appointments(conn, cipher, client_id=row.id)
    width = pdf.content_width(meta)
    full_name = cipher.decrypt(row.full_name_encrypted)
    today = to_istanbul(now_utc()).date()
    story = pdf.heading(
        f"Danışan dosyası: {full_name}",
        [f"Danışan kodu: {row.client_code}", "Bu belge gizlidir; yalnızca tedavi amacıyla kullanılır."],
    )  # fmt: skip
    story.append(
        pdf.facts(
            [
                ("Ad soyad", full_name),
                ("Danışan kodu", row.client_code),
                ("Doğum tarihi", d(row.birth_date)),
                ("Yaş", age_on(row.birth_date, today)),
                ("Telefon", cipher.decrypt_optional(row.phone_encrypted) or "—"),
                ("E-posta", cipher.decrypt_optional(row.email_encrypted) or "—"),
                ("Sorumlu terapist", names.get(row.assigned_therapist_id, "—")),
                ("Durum", "Aktif" if row.is_active else "Pasif"),
                ("Kayıt tarihi", d(row.created_at)),
                ("Son güncelleme", d(row.updated_at)),
            ],
            width,
        )
    )  # fmt: skip
    counts = status_counts(sessions)
    upcoming = [a for a in appointments if a["start"] > now_utc() and a["status"] == "planned"]
    story += pdf.section("Özet")
    story.append(
        pdf.facts(
            [
                ("Kayıtlı seans", len(sessions)),
                ("Tamamlanan", counts["completed"]),
                ("Gelmedi", counts["no_show"]),
                ("İptal", counts["cancelled"]),
                ("İlk seans", d(sessions[0]["date"]) if sessions else "—"),
                ("Son seans", d(sessions[-1]["date"]) if sessions else "—"),
                ("Sıradaki randevu", d(upcoming[0]["start"]) if upcoming else "—"),
                ("Toplam randevu", len(appointments)),
            ],
            width,
        )
    )  # fmt: skip
    if not sees_all:
        story.append(Spacer(1, 4))
        story.append(pdf.p("Yalnızca sizin seans kayıtlarınız gösterilir.", pdf.SMALL))

    method_use: Counter[str] = Counter()
    for s in sessions:
        if s["status"] == "completed":
            for label, _ in s["methods"]:
                method_use[label] += 1
    if method_use:
        story += pdf.section("Kullanılan yöntemler")
        story.append(pdf.Bars([(k, v, str(v)) for k, v in method_use.most_common()], width))

    story += pdf.section("Seans geçmişi")
    if sessions:
        story.append(
            pdf.table(
                ["Tarih", "No", "Terapist", "Biçim", "Yöntemler", "Durum", "Süre"],
                [
                    [
                        d(s["date"]), s["number"], names.get(s["therapist_id"], "—"), s["format"],
                        method_text(s["methods"]) or "—",
                        STATUS_LABELS.get(s["status"], s["status"]), f"{s['minutes']} dk",
                    ]
                    for s in sessions
                ],
                [width * w for w in (0.15, 0.05, 0.15, 0.13, 0.3, 0.12, 0.1)],
                right=(1, 6),
                struck=[i for i, s in enumerate(sessions) if s["status"] == "cancelled"],
            )
        )  # fmt: skip
    else:
        story.append(pdf.empty("Kayıtlı seans yok."))

    story += pdf.section("Randevular")
    if appointments:
        story.append(
            pdf.table(
                ["Tarih", "Saat", "Terapist", "Oda", "Durum"],
                [
                    [
                        d(to_istanbul(a["start"]).date()), f"{hm(a['start'])}–{hm(a['end'])}",
                        names.get(a["therapist_id"], "—"), a["room"],
                        STATUS_LABELS.get(a["status"], a["status"]),
                    ]
                    for a in appointments
                ],
                [width * w for w in (0.18, 0.18, 0.26, 0.18, 0.2)],
                struck=[i for i, a in enumerate(appointments) if a["status"] == "cancelled"],
            )
        )  # fmt: skip
    else:
        story.append(pdf.empty("Randevu yok."))

    scope = "all" if user.has(Permission.INCOME_READ_ALL) else "own"
    payments = await payments_for(
        conn, cipher, client_id=row.id, therapist_id=None if scope == "all" else user.id
    )
    if payments or user.has(Permission.INCOME_READ_OWN):
        story += pdf.section("Ödemeler")
        if payments:
            valid = [x for x in payments if not x["voided"]]
            story.append(
                pdf.table(
                    ["Tarih", "Randevu", "Yöntem", "Tutar", "Durum"],
                    [
                        [
                            d(x["paid_at"]), d(x["appointment_at"]),
                            PAYMENT_METHOD_LABELS.get(x["method"], x["method"]),
                            format_try(x["amount"]),
                            f"İptal: {VOID_REASONS.get(x['void_reason'] or '', '')}" if x["voided"] else "Geçerli",
                        ]
                        for x in payments
                    ],
                    [width * w for w in (0.2, 0.22, 0.16, 0.18, 0.24)],
                    right=(3,),
                    total=["Toplam (geçerli)", "", "", format_try(sum((x["amount"] for x in valid), Decimal("0"))), ""],
                    struck=[i for i, x in enumerate(payments) if x["voided"]],
                )
            )  # fmt: skip
        else:
            story.append(pdf.empty("Ödeme kaydı yok."))

    if params.include_notes and sessions:
        story.append(PageBreak())
        story += [pdf.p("Seans notları ve ödevler", pdf.H2)]
        story += notes_blocks(sessions, names)
    return story


# ---------------------------------------------------------------- 3. appointment list


async def appointment_list(
    conn: AsyncConnection, cipher: FieldCipher, user: CurrentUser, params: Params, meta: pdf.Meta
) -> list[Flowable]:
    start, end = params.bounds
    names = await user_names(conn)
    items = await visible_appointments(
        conn, cipher, start=start, end=end, therapist_id=params.therapist_id,
        room_id=params.room_id, include_cancelled=params.include_cancelled,
    )  # fmt: skip
    rooms = {
        r.id: r.label for r in (await conn.execute(select(t.rooms.c.id, t.rooms.c.label))).all()
    }
    width = pdf.content_width(meta)
    lines = [f"Dönem: {period_text(params.first, params.last)}"]
    lines.append(
        f"Terapist: {names.get(params.therapist_id, '—') if params.therapist_id else 'Tümü'}"
    )
    lines.append(f"Oda: {rooms.get(params.room_id, '—') if params.room_id else 'Tümü'}")
    if not params.include_cancelled:
        lines.append("İptal edilen randevular hariç")
    story = pdf.heading("Randevu listesi", lines)
    counts = status_counts(items)
    story.append(
        pdf.facts(
            [
                ("Randevu", len(items)),
                ("Planlanan", counts["planned"]),
                ("Tamamlanan", counts["completed"]),
                ("Gelmedi", counts["no_show"]),
                ("İptal", counts["cancelled"]),
                ("Toplam süre", hours(sum(a["minutes"] for a in items if a["status"] != "cancelled"))),
            ],
            width,
            columns=3,
        )
    )  # fmt: skip
    if not items:
        return [*story, Spacer(1, 8), pdf.empty()]
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for a in items:
        by_day[to_istanbul(a["start"]).date()].append(a)
    for day, entries in sorted(by_day.items()):
        title = f"{day.day} {MONTHS_TR[day.month - 1]} {day.year}, {DAYS_TR[day.weekday()]} · {len(entries)} randevu"
        story += [CondPageBreak(25 * mm), pdf.p(title, pdf.H3)]
        story.append(
            pdf.table(
                ["Saat", "Danışan", "Kod", "Terapist", "Oda", "Durum"],
                [
                    [
                        f"{hm(a['start'])}–{hm(a['end'])}", a["client"], a["code"],
                        names.get(a["therapist_id"], "—"), a["room"],
                        STATUS_LABELS.get(a["status"], a["status"]),
                    ]
                    for a in entries
                ],
                [width * w for w in (0.14, 0.26, 0.15, 0.19, 0.12, 0.14)],
                struck=[i for i, a in enumerate(entries) if a["status"] == "cancelled"],
            )
        )  # fmt: skip
    return story


# ---------------------------------------------------------------- 4. income


async def income_report(
    conn: AsyncConnection, cipher: FieldCipher, user: CurrentUser, params: Params, meta: pdf.Meta
) -> list[Flowable]:
    sees_all = user.has(Permission.INCOME_READ_ALL)
    if not sees_all and not user.has(Permission.INCOME_READ_OWN):
        raise ReportNotFound
    therapist_id = params.therapist_id if sees_all else user.id
    start, end = params.bounds
    names = await user_names(conn)
    items = await payments_for(conn, cipher, start=start, end=end, therapist_id=therapist_id)
    valid = [x for x in items if not x["voided"]]
    voided = [x for x in items if x["voided"]]
    total = sum((x["amount"] for x in valid), Decimal("0"))
    width = pdf.content_width(meta)
    scope = names.get(therapist_id, "—") if therapist_id else "Tüm klinik"
    story = pdf.heading(
        "Gelir raporu",
        [f"Dönem: {period_text(params.first, params.last)}", f"Kapsam: {scope}",
         "Gelir, randevunun terapistine yazılır. İptal edilen ödemeler toplama katılmaz."],
    )  # fmt: skip
    days = (params.last - params.first).days + 1
    story.append(
        pdf.facts(
            [
                ("Toplam gelir", format_try(total)),
                ("Ödeme sayısı", len(valid)),
                ("Ortalama ödeme", format_try(total / len(valid)) if valid else "—"),
                ("Günlük ortalama", format_try(total / days)),
                ("İptal edilen ödeme", len(voided)),
                ("İptal tutarı", format_try(sum((x["amount"] for x in voided), Decimal("0")))),
            ],
            width,
        )
    )  # fmt: skip
    if not items:
        return [*story, Spacer(1, 8), pdf.empty()]

    by_method: dict[str, Decimal] = defaultdict(Decimal)
    for x in valid:
        by_method[x["method"]] += x["amount"]
    story += pdf.section("Ödeme yöntemine göre")
    story.append(
        pdf.table(
            ["Yöntem", "Ödeme", "Tutar", "Pay"],
            [
                [PAYMENT_METHOD_LABELS.get(m, m), sum(1 for x in valid if x["method"] == m),
                 format_try(v), percent(int(v * 100), int(total * 100))]
                for m, v in sorted(by_method.items(), key=lambda kv: -kv[1])
            ],
            [width * w for w in (0.4, 0.15, 0.25, 0.2)],
            right=(1, 2, 3),
            total=["Toplam", len(valid), format_try(total), "%100" if total else "—"],
        )
    )  # fmt: skip

    if therapist_id is None:
        per: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
        for x in valid:
            per[x["therapist_id"]].append(x)
        story += pdf.section("Terapistlere göre")
        story.append(
            pdf.table(
                ["Terapist", "Ödeme", "Tutar", "Pay"],
                [
                    [names.get(tid, "—"), len(xs), format_try(sum((x["amount"] for x in xs), Decimal("0"))),
                     percent(int(sum(x["amount"] for x in xs) * 100), int(total * 100))]
                    for tid, xs in sorted(per.items(), key=lambda kv: names.get(kv[0], ""))
                ],
                [width * w for w in (0.4, 0.15, 0.25, 0.2)],
                right=(1, 2, 3),
                total=["Toplam", len(valid), format_try(total), "%100" if total else "—"],
            )
        )  # fmt: skip

    by_day: dict[date, Decimal] = defaultdict(Decimal)
    for x in valid:
        by_day[to_istanbul(x["paid_at"]).date()] += x["amount"]
    if by_day:
        story += pdf.section("Günlere göre")
        story.append(
            pdf.Bars(
                [(f"{day.strftime('%d.%m')} {DAYS_TR[day.weekday()][:3]}", float(v), format_try(v))
                 for day, v in sorted(by_day.items())],
                width,
            )
        )  # fmt: skip

    story += pdf.section("Ödeme listesi")
    story.append(
        pdf.table(
            ["Tarih", "Danışan", "Terapist", "Yöntem", "Tutar", "Kaydeden", "Durum"],
            [
                [
                    d(x["paid_at"]), f"{x['client']}\n{x['code']}", names.get(x["therapist_id"], "—"),
                    PAYMENT_METHOD_LABELS.get(x["method"], x["method"]), format_try(x["amount"]),
                    x["recorder"],
                    f"İptal: {VOID_REASONS.get(x['void_reason'] or '', '')}" if x["voided"] else "Geçerli",
                ]
                for x in items
            ],
            [width * w for w in (0.15, 0.2, 0.15, 0.1, 0.13, 0.13, 0.14)],
            right=(4,),
            total=["Toplam (geçerli)", "", "", "", format_try(total), "", ""],
            struck=[i for i, x in enumerate(items) if x["voided"]],
        )
    )  # fmt: skip
    return story


# ---------------------------------------------------------------- 5. statistics


async def appointment_stats(
    conn: AsyncConnection, cipher: FieldCipher, user: CurrentUser, params: Params, meta: pdf.Meta
) -> list[Flowable]:
    start, end = params.bounds
    names = await user_names(conn)
    items = await visible_appointments(
        conn, cipher, start=start, end=end, therapist_id=params.therapist_id
    )
    width = pdf.content_width(meta)
    scope = names.get(params.therapist_id, "—") if params.therapist_id else "Tüm klinik"
    story = pdf.heading(
        "Randevu istatistikleri",
        [f"Dönem: {period_text(params.first, params.last)}", f"Kapsam: {scope}"],
    )
    counts = status_counts(items)
    held = counts["completed"] + counts["no_show"]
    c = t.clients
    new_clients = len(
        (
            await conn.execute(
                select(c.c.id).where(and_(c.c.created_at >= start, c.c.created_at < end))
            )
        ).all()
    )
    story.append(
        pdf.facts(
            [
                ("Toplam randevu", len(items)),
                ("Tamamlanan", counts["completed"]),
                ("Gelmedi", counts["no_show"]),
                ("İptal", counts["cancelled"]),
                ("Planlanan (henüz)", counts["planned"]),
                ("Katılım oranı", percent(counts["completed"], held)),
                ("Gelmeme oranı", percent(counts["no_show"], held)),
                ("İptal oranı", percent(counts["cancelled"], len(items))),
                ("Farklı danışan", len({a["client_id"] for a in items})),
                ("Yeni kayıtlı danışan", new_clients),
            ],
            width,
        )
    )  # fmt: skip
    if not items:
        return [*story, Spacer(1, 8), pdf.empty()]

    active = [a for a in items if a["status"] != "cancelled"]
    per: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for a in items:
        per[a["therapist_id"]].append(a)
    story += pdf.section("Terapistlere göre")
    story.append(
        pdf.table(
            ["Terapist", "Randevu", "Tamamlanan", "Gelmedi", "İptal", "Katılım", "Saat"],
            [
                [
                    names.get(tid, "—"), len(xs), status_counts(xs)["completed"],
                    status_counts(xs)["no_show"], status_counts(xs)["cancelled"],
                    percent(status_counts(xs)["completed"],
                            status_counts(xs)["completed"] + status_counts(xs)["no_show"]),
                    hours(sum(x["minutes"] for x in xs if x["status"] != "cancelled")),
                ]
                for tid, xs in sorted(per.items(), key=lambda kv: names.get(kv[0], ""))
            ],
            [width * w for w in (0.26, 0.11, 0.14, 0.11, 0.09, 0.12, 0.17)],
            right=(1, 2, 3, 4, 5, 6),
        )
    )  # fmt: skip

    weekdays = Counter(to_istanbul(a["start"]).weekday() for a in active)
    story += pdf.section("Haftanın günlerine göre (iptaller hariç)")
    story.append(
        pdf.Bars(
            [(DAYS_TR[i], weekdays.get(i, 0), str(weekdays.get(i, 0))) for i in range(7)], width
        )
    )
    hours_of_day = Counter(to_istanbul(a["start"]).hour for a in active)
    if hours_of_day:
        story += pdf.section("Saatlere göre (iptaller hariç)")
        story.append(
            pdf.Bars(
                [(f"{h:02d}:00", hours_of_day[h], str(hours_of_day[h])) for h in sorted(hours_of_day)],
                width,
            )
        )  # fmt: skip
    rooms = Counter(a["room"] for a in active)
    story += pdf.section("Odalara göre (iptaller hariç)")
    story.append(
        pdf.table(
            ["Oda", "Randevu", "Saat"],
            [
                [room, n, hours(sum(a["minutes"] for a in active if a["room"] == room))]
                for room, n in rooms.most_common()
            ],
            [width * 0.5, width * 0.2, width * 0.3],
            right=(1, 2),
        )
    )  # fmt: skip
    busiest = Counter(to_istanbul(a["start"]).date() for a in active).most_common(5)
    if busiest:
        story += pdf.section("En yoğun günler")
        story.append(
            pdf.table(
                ["Gün", "Randevu"],
                [[f"{d(day)} {DAYS_TR[day.weekday()]}", n] for day, n in busiest],
                [width * 0.7, width * 0.3],
                right=(1,),
            )
        )
    return story


BUILDERS = {
    "seanslar": session_report,
    "danisan": client_file,
    "randevular": appointment_list,
    "gelir": income_report,
    "istatistik": appointment_stats,
}
