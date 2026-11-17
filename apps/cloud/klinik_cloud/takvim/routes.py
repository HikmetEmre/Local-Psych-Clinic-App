"""Takvim (calendar) and booking pages of Klinik Web.

Clients appear only as codes here. Times are entered and shown in Europe/Istanbul.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from klinik_cloud.takvim.service import (
    Appointment,
    AppointmentError,
    AppointmentFields,
    AppointmentNotFound,
    AppointmentService,
    BookingConflict,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.calendar import Person, build_grid, first_day, parse_day, span, title_for
from klinik_shared.enums import AppointmentStatus
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import ISTANBUL, now_utc, to_istanbul

DURATIONS = (30, 45, 50, 60, 75, 90, 120)


def parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def local_start(day: object, clock: object) -> datetime | None:
    try:
        return datetime.combine(
            date.fromisoformat(str(day)), time.fromisoformat(str(clock)), tzinfo=ISTANBUL
        )
    except ValueError:
        return None


# ---------------------------------------------------------------- router


def build_router(
    auth: AuthKit, service: AppointmentService, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    can_read = Depends(auth.require(Permission.SCHEDULE_READ_ALL))
    can_write = Depends(auth.require(Permission.SCHEDULE_READ_ALL, Permission.SCHEDULE_WRITE_ALL))

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    def not_found(request: Request) -> Response:
        return render(
            request,
            "error.html",
            {"title": "Randevu bulunamadı", "message": "Bu randevu mevcut değil veya silinmiş."},
            404,
        )

    @router.get("/", include_in_schema=False)
    async def home(user: CurrentUser = Depends(auth.require())) -> Response:  # noqa: B008
        target = "/takvim" if user.has(Permission.SCHEDULE_READ_ALL) else "/hesap/sifre"
        return RedirectResponse(target, status_code=303)

    # ------------------------------------------------------------ calendar

    @router.get("/takvim", response_class=HTMLResponse)
    async def calendar(request: Request, user: CurrentUser = can_read) -> Response:
        params = request.query_params
        view = "gun" if params.get("gorunum") == "gun" else "hafta"
        day = parse_day(params.get("gun"))
        therapist_id = parse_uuid(params.get("terapist"))
        room_id = parse_uuid(params.get("oda"))
        first = first_day(view, day)
        days = span(view)
        async with auth.tx(user) as conn:
            therapists = await service.therapists(conn)
            rooms = await service.rooms(conn, active_only=False)
            appointments = await service.in_range(
                conn, first, days, therapist_id=therapist_id, room_id=room_id
            )
        shown = [t for t in therapists if therapist_id is None or t.id == therapist_id]
        grid = build_grid(view, day, appointments, [Person(t.id, t.label) for t in shown])
        color = {th.id: index % 4 for index, th in enumerate(therapists)}
        step = timedelta(days=days)

        def cal_url(**changes: Any) -> str:
            query = {
                "gorunum": view,
                "gun": day.isoformat(),
                "terapist": str(therapist_id or ""),
                "oda": str(room_id or ""),
            }
            query.update({k: str(v) for k, v in changes.items()})
            return "/takvim?" + urlencode({k: v for k, v in query.items() if v})

        return render(
            request,
            "takvim/calendar.html",
            {
                "view": view,
                "day": day,
                "title": title_for(view, day),
                "grid": grid,
                "therapists": therapists,
                "rooms": rooms,
                "therapist_id": therapist_id,
                "room_id": room_id,
                "color": color,
                "cal_url": cal_url,
                "prev_day": (day - step).isoformat(),
                "next_day": (day + step).isoformat(),
                "today": to_istanbul(now_utc()).date().isoformat(),
                "can_write": user.has(Permission.SCHEDULE_WRITE_ALL),
                "to_istanbul": to_istanbul,
                "notice": {
                    "kaydedildi": "Randevu kaydedildi.",
                    "birlestirildi": (
                        "Randevu kaydedildi. Bu arada başka biri farklı bir alanı değiştirmişti; "
                        "iki değişiklik birleştirildi."
                    ),
                    "iptal": "Randevu iptal edildi.",
                }.get(params.get("durum", "")),
            },
        )

    # ------------------------------------------------------------ form helpers

    async def form_page(
        request: Request,
        user: CurrentUser,
        *,
        values: dict[str, Any],
        appointment: Appointment | None = None,
        errors: dict[str, str] | None = None,
        status: int = 200,
        conflict: BookingConflict | None = None,
    ) -> Response:
        async with auth.tx(user) as conn:
            therapists = await service.therapists(conn)
            rooms = await service.rooms(conn)
            codes = await service.client_codes(conn)
        return render(
            request,
            "takvim/form.html",
            {
                "values": values,
                "appointment": appointment,
                "errors": errors or {},
                "therapists": therapists,
                "rooms": rooms,
                "codes": codes,
                "durations": DURATIONS,
                "statuses": [(s.value, STATUS_LABELS[s.value]) for s in AppointmentStatus],
                "conflict": conflict,
                "conflict_fields": {
                    "starts_at": "Tarih / saat",
                    "duration_min": "Süre",
                    "therapist_id": "Terapist",
                    "room_id": "Oda",
                    "status": "Durum",
                },
            },
            status,
        )

    def values_of(appt: Appointment) -> dict[str, Any]:
        local = to_istanbul(appt.starts_at)
        return {
            "client_code": appt.client_code,
            "date": local.date().isoformat(),
            "time": local.strftime("%H:%M"),
            "duration_min": str(appt.duration_min),
            "therapist_id": str(appt.therapist_id),
            "room_id": str(appt.room_id),
            "status": appt.status,
            "version": str(appt.version),
        }

    def fields_from(form: Any, prefix: str = "") -> tuple[AppointmentFields, dict[str, str]]:
        errors: dict[str, str] = {}
        starts_at = local_start(form.get(f"{prefix}date"), form.get(f"{prefix}time"))
        if starts_at is None:
            errors["starts_at"] = "Tarih ve saat geçersiz."
        raw: dict[str, Any] = {
            "starts_at": starts_at,
            "duration_min": form.get(f"{prefix}duration_min") or None,
            "therapist_id": parse_uuid(form.get(f"{prefix}therapist_id")),
            "room_id": parse_uuid(form.get(f"{prefix}room_id")),
        }
        if form.get(f"{prefix}status"):
            raw["status"] = form.get(f"{prefix}status")
        try:
            return AppointmentFields(**{k: v for k, v in raw.items() if v is not None}), errors
        except ValidationError as exc:
            for err in exc.errors():
                name = str(err["loc"][0]) if err["loc"] else "form"
                errors.setdefault(
                    name,
                    "Süre 10 ile 240 dakika arasında olmalı."
                    if name == "duration_min"
                    else "Bu alan geçersiz.",
                )
            return AppointmentFields(), errors

    # ------------------------------------------------------------ create

    @router.get("/randevular/yeni", response_class=HTMLResponse)
    async def new(request: Request, user: CurrentUser = can_write) -> Response:
        params = request.query_params
        start = params.get("baslangic", "")
        day, _, clock = start.partition("T")
        therapist = params.get("terapist", "")
        room = params.get("oda", "")
        if therapist and not room:
            async with auth.tx(user) as conn:
                default = (
                    await service.default_room(conn, UUID(therapist))
                    if parse_uuid(therapist)
                    else None
                )
            room = str(default or "")
        values = {
            "client_code": params.get("kod", ""),
            "date": day or to_istanbul(now_utc()).date().isoformat(),
            "time": clock[:5] or "10:00",
            "duration_min": params.get("sure", "50"),
            "therapist_id": therapist,
            "room_id": room,
        }
        return await form_page(request, user, values=values)

    @router.post("/randevular", response_class=HTMLResponse)
    async def create(request: Request, user: CurrentUser = can_write) -> Response:
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "csrf_token"}
        values["client_code"] = values.get("client_code", "").strip().upper()
        fields, errors = fields_from(form)
        created: Appointment | None = None
        if not errors:
            try:
                async with auth.tx(user) as conn:
                    created = await service.create(
                        conn, user, client_code=values["client_code"], fields=fields
                    )
            except AppointmentError as exc:
                errors = {exc.field: exc.message}
        if created is None:
            return await form_page(request, user, values=values, errors=errors, status=422)
        local = to_istanbul(created.starts_at).date().isoformat()
        return RedirectResponse(f"/takvim?gun={local}&durum=kaydedildi", status_code=303)

    # ------------------------------------------------------------ edit

    @router.get("/randevular/{appointment_id}", response_class=HTMLResponse)
    async def edit(
        request: Request, appointment_id: UUID, user: CurrentUser = can_read
    ) -> Response:
        async with auth.tx(user) as conn:
            try:
                appt = await service.get(conn, appointment_id)
            except AppointmentNotFound:
                return not_found(request)
        return await form_page(request, user, values=values_of(appt), appointment=appt)

    @router.post("/randevular/{appointment_id}", response_class=HTMLResponse)
    async def save(
        request: Request, appointment_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "csrf_token"}
        changes, errors = fields_from(form)
        based_on, _ = fields_from(form, prefix="orig_")
        try:
            version = int(values.get("version", ""))
        except ValueError:
            version = -1
        conflict: BookingConflict | None = None
        merged = False
        saved: Appointment | None = None
        current: Appointment | None = None
        if not errors:
            # One transaction; a conflict row is committed even though the edit is not applied.
            async with auth.tx(user) as conn:
                try:
                    saved, merged = await service.update(
                        conn,
                        user,
                        appointment_id,
                        changes=changes,
                        based_on=based_on,
                        expected_version=version,
                    )
                except AppointmentNotFound:
                    return not_found(request)
                except BookingConflict as exc:
                    conflict = exc
                except AppointmentError as exc:
                    errors = {exc.field: exc.message}
                if saved is None:
                    try:
                        current = await service.get(conn, appointment_id)
                    except AppointmentNotFound:
                        return not_found(request)
        if conflict is not None:
            return await form_page(
                request,
                user,
                values=values_of(conflict.current),
                appointment=conflict.current,
                conflict=conflict,
                status=409,
            )
        if saved is None:
            return await form_page(
                request, user, values=values, appointment=current, errors=errors, status=422
            )
        local = to_istanbul(saved.starts_at).date().isoformat()
        outcome = "birlestirildi" if merged else "kaydedildi"
        return RedirectResponse(f"/takvim?gun={local}&durum={outcome}", status_code=303)

    @router.post("/randevular/{appointment_id}/iptal", response_class=HTMLResponse)
    async def cancel(
        request: Request, appointment_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        form = await request.form()
        try:
            version = int(str(form.get("version") or ""))
        except ValueError:
            version = -1
        conflict: BookingConflict | None = None
        async with auth.tx(user) as conn:
            try:
                appt = await service.cancel(conn, user, appointment_id, version)
            except AppointmentNotFound:
                return not_found(request)
            except BookingConflict as exc:
                conflict = exc
        if conflict is not None:
            return await form_page(
                request,
                user,
                values=values_of(conflict.current),
                appointment=conflict.current,
                conflict=conflict,
                status=409,
            )
        local = to_istanbul(appt.starts_at).date().isoformat()
        return RedirectResponse(f"/takvim?gun={local}&durum=iptal", status_code=303)

    return router
