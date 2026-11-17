"""Ana sayfa: the chosen day's appointments and sessions, and a month calendar.

The day defaults to today. Each day of the month shows how many appointments it has;
"+" on a day opens the booking form for that day and comes back here afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_local.gelir.service import IncomeService, format_try
from klinik_local.randevular.service import AppointmentService
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.calendar import DAYS_TR, MONTHS_TR, parse_day
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import istanbul_day_bounds, now_utc, to_istanbul


@dataclass(frozen=True)
class MonthCell:
    day: date
    in_month: bool
    is_today: bool
    is_selected: bool
    count: int


@dataclass(frozen=True)
class WalkIn:
    """A session on this day that is not tied to an appointment."""

    id: UUID
    session_date: datetime
    client_name: str
    client_code: str
    therapist_id: UUID
    therapist_name: str
    status: str


def month_weeks(selected: date, today: date, counts: dict[date, int]) -> list[list[MonthCell]]:
    first = selected.replace(day=1)
    start = first - timedelta(days=first.weekday())
    following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    weeks: list[list[MonthCell]] = []
    day = start
    while day < following or day.weekday() != 0:
        if day.weekday() == 0:
            weeks.append([])
        weeks[-1].append(
            MonthCell(
                day, day.month == first.month, day == today, day == selected, counts.get(day, 0)
            )
        )
        day += timedelta(days=1)
    return weeks


def shift_month(day: date, months: int) -> date:
    index = day.month - 1 + months
    return date(day.year + index // 12, index % 12 + 1, 1)


async def walk_ins(conn: AsyncConnection, cipher: FieldCipher, day: date) -> list[WalkIn]:
    s, c, u = t.therapy_sessions, t.clients, t.users
    start, end = istanbul_day_bounds(day)
    rows = (
        await conn.execute(
            select(
                s.c.id, s.c.session_date, s.c.status, c.c.client_code, c.c.full_name_encrypted,
                s.c.therapist_id, u.c.display_name,
            )
            .select_from(s.join(c, c.c.id == s.c.client_id).join(u, u.c.id == s.c.therapist_id))
            .where(
                s.c.deleted_at.is_(None),
                s.c.appointment_id.is_(None),
                s.c.session_date >= start,
                s.c.session_date < end,
            )
            .order_by(s.c.session_date)
        )
    ).all()  # fmt: skip
    return [
        WalkIn(r.id, r.session_date, cipher.decrypt(r.full_name_encrypted), r.client_code,
               r.therapist_id, r.display_name, r.status)
        for r in rows
    ]  # fmt: skip


async def paid_totals(conn: AsyncConnection, ids: list[UUID]) -> dict[UUID, Decimal]:
    if not ids:
        return {}
    p = t.payments
    rows = (
        await conn.execute(
            select(p.c.appointment_id, func.sum(p.c.amount))
            .where(p.c.appointment_id.in_(ids), p.c.voided_at.is_(None))
            .group_by(p.c.appointment_id)
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def build_router(
    auth: AuthKit,
    appointments: AppointmentService,
    income: IncomeService,
    cipher: FieldCipher,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    logged_in = Depends(auth.require())

    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request, user: CurrentUser = logged_in) -> Response:
        params = request.query_params
        today = to_istanbul(now_utc()).date()
        day = parse_day(params.get("gun"))
        sees_schedule = user.has(Permission.SCHEDULE_READ_ALL)
        is_therapist = user.has(Permission.SESSIONS_WRITE_OWN)
        # Therapists start with their own day, the admin with the whole clinic.
        who = params.get("kim")
        if who not in ("benim", "hepsi"):
            who = (
                "benim"
                if is_therapist and not user.has(Permission.SESSIONS_READ_OTHERS)
                else "hepsi"
            )
        only_mine = is_therapist and who == "benim"
        context: dict[str, Any] = {
            "day": day,
            "today": today,
            "day_title": (
                f"{day.day} {MONTHS_TR[day.month - 1]} {day.year}, {DAYS_TR[day.weekday()]}"
            ),
            "month_title": f"{MONTHS_TR[day.month - 1]} {day.year}",
            "prev_month": shift_month(day, -1).isoformat(),
            "next_month": shift_month(day, 1).isoformat(),
            "weekdays": [d[:3] for d in DAYS_TR],
            "is_therapist": is_therapist,
            "only_mine": only_mine,
            "who": who,
            "can_book": user.has(Permission.SCHEDULE_WRITE_ALL),
            "can_record": is_therapist,
            "money": format_try,
            "to_istanbul": to_istanbul,
            "notice": "Randevu kaydedildi." if params.get("durum") == "kaydedildi" else None,
            "appointments": [],
            "walk_ins": [],
            "weeks": [],
            "paid": {},
            "open_conflicts": 0,
        }
        if user.has(Permission.ADMIN_USERS) and not templates.env.globals.get("replica_mode"):
            async with auth.tx(user) as conn:
                context["open_conflicts"] = (
                    await conn.execute(
                        text("SELECT count(*) FROM replica_conflicts WHERE resolved_at IS NULL")
                    )
                ).scalar_one()
        if sees_schedule:
            first = day.replace(day=1) - timedelta(days=day.replace(day=1).weekday())
            async with auth.tx(user) as conn:
                listed = await appointments.in_range(
                    conn, day, 1, therapist_id=user.id if only_mine else None
                )
                counts = await appointments.counts_by_day(conn, first, 42)
                walk = await walk_ins(conn, cipher, day)
                visible = [a.id for a in listed if income.can_see(user, a.therapist_id)]
                paid = await paid_totals(conn, visible)
            context.update(
                appointments=listed,
                walk_ins=[w for w in walk if not only_mine or w.therapist_id == user.id],
                weeks=month_weeks(day, today, counts),
                paid=paid,
                paid_visible=set(visible),
                active_count=sum(1 for a in listed if a.status != "cancelled"),
            )
        response = templates.TemplateResponse(request, "home.html", context)
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
