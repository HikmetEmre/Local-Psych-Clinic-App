"""Ajandam pages (owner only): /ajanda."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from klinik_local.ajanda.service import (
    CATEGORIES,
    HOUR_END,
    HOUR_START,
    REPEATS,
    Item,
    PlanError,
    PlanInput,
    PlanNotFound,
    PlanService,
    PlanVersionConflict,
    columns,
    now_quarter,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.calendar import DAYS_TR, MONTHS_TR, parse_day
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import ISTANBUL, now_utc, to_istanbul

VIEWS = {"ay": "Ay", "hafta": "Hafta", "gun": "Gün", "liste": "Liste"}
MONTH_SHOWN = 4  # chips per day in the month view before "+N daha"
LIST_DAYS = 30


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def view_range(view: str, day: date) -> tuple[date, date]:
    if view == "gun":
        return day, day
    if view == "hafta":
        monday = day - timedelta(days=day.weekday())
        return monday, monday + timedelta(days=6)
    if view == "liste":
        return day, day + timedelta(days=LIST_DAYS - 1)
    first = day.replace(day=1)
    start = first - timedelta(days=first.weekday())
    following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    end = following - timedelta(days=1)
    end += timedelta(days=6 - end.weekday())
    return start, end


def step(view: str, day: date, direction: int) -> date:
    if view == "gun":
        return day + timedelta(days=direction)
    if view == "hafta":
        return day + timedelta(days=7 * direction)
    if view == "liste":
        return day + timedelta(days=LIST_DAYS * direction)
    index = day.month - 1 + direction
    return date(day.year + index // 12, index % 12 + 1, 1)


def title_for(view: str, first: date, last: date, day: date) -> str:
    if view == "gun":
        return f"{day.day} {MONTHS_TR[day.month - 1]} {day.year}, {DAYS_TR[day.weekday()]}"
    if view == "ay":
        return f"{MONTHS_TR[day.month - 1]} {day.year}"
    if first.month == last.month:
        return f"{first.day}–{last.day} {MONTHS_TR[first.month - 1]} {first.year}"
    return (
        f"{first.day} {MONTHS_TR[first.month - 1]} – {last.day} {MONTHS_TR[last.month - 1]} "
        f"{last.year}"
    )


def form_values(
    item: dict[str, Any] | None = None, start: datetime | None = None
) -> dict[str, Any]:
    if item is not None:
        s, e = to_istanbul(item["starts_at"]), to_istanbul(item["ends_at"])
        if item["all_day"]:
            e = e - timedelta(days=1)
        return {
            "title": item["title"], "category": item["category"], "location": item["location"],
            "note": item["note"], "all_day": "1" if item["all_day"] else "",
            "start_date": s.date().isoformat(), "start_time": s.strftime("%H:%M"),
            "end_date": e.date().isoformat(), "end_time": e.strftime("%H:%M"),
            "repeat": item["repeat"],
            "repeat_until": item["repeat_until"].isoformat() if item["repeat_until"] else "",
            "version": str(item["version"]),
        }  # fmt: skip
    start = start or to_istanbul(now_utc()).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    return {
        "title": "", "category": "personal", "location": "", "note": "", "all_day": "",
        "start_date": start.date().isoformat(), "start_time": start.strftime("%H:%M"),
        "end_date": end.date().isoformat(), "end_time": end.strftime("%H:%M"),
        "repeat": "none", "repeat_until": "",
    }  # fmt: skip


def input_from(form: dict[str, str]) -> PlanInput:
    all_day = form.get("all_day") == "1"
    start_day = parse_date(form.get("start_date"))
    end_day = parse_date(form.get("end_date")) or start_day
    if start_day is None or end_day is None:
        raise PlanError("start_date", "Tarih geçersiz.")
    try:
        if all_day:
            starts = datetime.combine(start_day, time(0), tzinfo=ISTANBUL)
            ends = datetime.combine(end_day + timedelta(days=1), time(0), tzinfo=ISTANBUL)
        else:
            starts = datetime.combine(
                start_day, time.fromisoformat(form.get("start_time") or ""), tzinfo=ISTANBUL
            )
            ends = datetime.combine(
                end_day, time.fromisoformat(form.get("end_time") or ""), tzinfo=ISTANBUL
            )
    except ValueError:
        raise PlanError("start_time", "Saat geçersiz.") from None
    return PlanInput(
        title=form.get("title", ""),
        category=form.get("category", ""),
        starts_at=starts,
        ends_at=ends,
        all_day=all_day,
        location=form.get("location", ""),
        note=form.get("note", ""),
        repeat=form.get("repeat", "none"),
        repeat_until=parse_date(form.get("repeat_until")),
    )


def build_router(auth: AuthKit, plans: PlanService, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix="/ajanda", include_in_schema=False)
    # Ajandam is the clinic owner's: admin.users, and her own rows only (RLS too).
    owner = Depends(auth.require(Permission.ADMIN_USERS))

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("", response_class=HTMLResponse)
    async def diary(request: Request, user: CurrentUser = owner) -> Response:
        q = request.query_params
        view = q.get("gorunum", "hafta")
        view = view if view in VIEWS else "hafta"
        day = parse_day(q.get("gun"))
        show_plans = q.get("planlar", "1") != "0"
        show_appts = q.get("randevular", "1") != "0"
        first, last = view_range(view, day)
        today = to_istanbul(now_utc()).date()
        async with auth.tx(user) as conn:
            items = await plans.items(
                conn, user, first, last, plans=show_plans, appointments=show_appts
            )

        def url(**changes: Any) -> str:
            query = {
                "gorunum": view, "gun": day.isoformat(),
                "planlar": "" if show_plans else "0", "randevular": "" if show_appts else "0",
            }  # fmt: skip
            query.update({k: str(v) for k, v in changes.items()})
            return "/ajanda?" + urlencode({k: v for k, v in query.items() if v})

        context: dict[str, Any] = {
            "view": view, "views": VIEWS, "day": day, "today": today,
            "title": title_for(view, first, last, day), "url": url,
            "prev": step(view, day, -1).isoformat(), "next": step(view, day, 1).isoformat(),
            "show_plans": show_plans, "show_appts": show_appts, "categories": CATEGORIES,
            "hours": list(range(HOUR_START, HOUR_END)), "weekdays": DAYS_TR,
            "months": MONTHS_TR, "notice": {
                "kaydedildi": "Plan kaydedildi.", "silindi": "Plan silindi.",
                "tek": "Bu tekrar silindi; diğerleri duruyor.",
            }.get(q.get("durum", "")),
            "count_plans": sum(1 for i in items if i.kind == "plan"),
            "count_appts": sum(1 for i in items if i.kind == "appointment"),
        }  # fmt: skip
        if view in ("hafta", "gun"):
            days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
            context["columns"] = columns(days, items, today)
            context["now_q"] = now_quarter(now_utc())
        elif view == "ay":
            by_day: dict[date, list[Item]] = defaultdict(list)
            for item in items:
                d = item.local_start.date()
                end = (item.local_end - timedelta(microseconds=1)).date()
                while d <= end and d <= last:
                    if d >= first:
                        by_day[d].append(item)
                    d += timedelta(days=1)
            weeks, cursor = [], first
            while cursor <= last:
                weeks.append([cursor + timedelta(days=i) for i in range(7)])
                cursor += timedelta(days=7)
            context.update(weeks=weeks, by_day=by_day, shown=MONTH_SHOWN)
        else:
            grouped: dict[date, list[Item]] = defaultdict(list)
            for item in items:
                grouped[item.local_start.date()].append(item)
            context["agenda"] = sorted(grouped.items())
        return render(request, "ajanda/calendar.html", context)

    # ------------------------------------------------------------ plans

    def form_page(
        request: Request,
        *,
        values: dict[str, Any],
        plan_id: UUID | None = None,
        occurrence: str = "",
        errors: dict[str, str] | None = None,
        conflict: bool = False,
        status: int = 200,
    ) -> Response:
        return render(
            request,
            "ajanda/form.html",
            {
                "values": values,
                "plan_id": plan_id,
                "occurrence": occurrence,
                "errors": errors or {},
                "conflict": conflict,
                "categories": CATEGORIES,
                "repeats": REPEATS,
                "back": f"/ajanda?gun={values.get('start_date', '')}",
            },
            status,
        )

    @router.get("/yeni", response_class=HTMLResponse)
    async def new(request: Request, user: CurrentUser = owner) -> Response:
        raw = request.query_params.get("baslangic", "")
        start = None
        try:
            if "T" in raw:
                start = datetime.fromisoformat(raw).replace(tzinfo=ISTANBUL)
            elif raw:
                start = datetime.combine(date.fromisoformat(raw), time(9), tzinfo=ISTANBUL)
        except ValueError:
            start = None
        values = form_values(start=start)
        if request.query_params.get("tumgun") == "1":
            values["all_day"] = "1"
        return form_page(request, values=values)

    @router.post("", response_class=HTMLResponse)
    async def create(request: Request, user: CurrentUser = owner) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items() if k != "csrf_token"}
        try:
            data = input_from(form)
            async with auth.tx(user) as conn:
                await plans.create(conn, user, data)
        except PlanError as exc:
            return form_page(request, values=form, errors={exc.field: exc.message}, status=422)
        return RedirectResponse(
            f"/ajanda?gun={form.get('start_date', '')}&durum=kaydedildi", status_code=303
        )

    @router.get("/{plan_id}/duzenle", response_class=HTMLResponse)
    async def edit(request: Request, plan_id: UUID, user: CurrentUser = owner) -> Response:
        async with auth.tx(user) as conn:
            try:
                item = await plans.get(conn, user, plan_id)
            except PlanNotFound:
                return render(
                    request, "error.html",
                    {"title": "Plan bulunamadı", "message": "Bu plan mevcut değil veya silinmiş."},
                    404,
                )  # fmt: skip
        return form_page(
            request, values=form_values(item), plan_id=plan_id,
            occurrence=request.query_params.get("tarih", ""),
        )  # fmt: skip

    @router.post("/{plan_id}", response_class=HTMLResponse)
    async def save(request: Request, plan_id: UUID, user: CurrentUser = owner) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items() if k != "csrf_token"}
        try:
            version = int(form.get("version", ""))
        except ValueError:
            version = -1
        try:
            data = input_from(form)
            async with auth.tx(user) as conn:
                await plans.update(conn, user, plan_id, data, version)
        except PlanError as exc:
            return form_page(
                request, values=form, plan_id=plan_id, errors={exc.field: exc.message}, status=422
            )
        except PlanVersionConflict:
            async with auth.tx(user) as conn:
                item = await plans.get(conn, user, plan_id)
            return form_page(
                request, values=form_values(item), plan_id=plan_id, conflict=True, status=409
            )
        except PlanNotFound:
            return RedirectResponse("/ajanda", status_code=303)
        return RedirectResponse(
            f"/ajanda?gun={form.get('start_date', '')}&durum=kaydedildi", status_code=303
        )

    @router.post("/{plan_id}/sil")
    async def delete(request: Request, plan_id: UUID, user: CurrentUser = owner) -> Response:
        form = await request.form()
        only = parse_date(form.get("tarih")) if form.get("kapsam") == "bu" else None
        try:
            async with auth.tx(user) as conn:
                await plans.delete(conn, user, plan_id, only)
        except PlanNotFound:
            pass
        back = (only or to_istanbul(now_utc()).date()).isoformat()
        outcome = "tek" if only else "silindi"
        return RedirectResponse(f"/ajanda?gun={back}&durum={outcome}", status_code=303)

    return router
