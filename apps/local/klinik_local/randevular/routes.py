"""Takvim (calendar) and booking pages. Times are entered and shown in Europe/Istanbul."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from klinik_local.danisanlar.service import ClientService
from klinik_local.gelir.service import VOID_REASONS, IncomeService, format_try
from klinik_local.randevular.service import (
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
from klinik_shared.labels_tr import PAYMENT_METHOD_LABELS
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import ISTANBUL, now_utc, to_istanbul

DURATIONS = (30, 45, 50, 60, 75, 90, 120)
CONFLICT_FIELDS = {
    "starts_at": "Tarih / saat",
    "duration_min": "Süre",
    "therapist_id": "Terapist",
    "room_id": "Oda",
    "status": "Durum",
}
NOTICES = {
    "kaydedildi": "Randevu kaydedildi.",
    "birlestirildi": (
        "Randevu kaydedildi. Bu arada başka biri farklı bir alanı değiştirmişti; "
        "iki değişiklik birleştirildi."
    ),
    "iptal": "Randevu iptal edildi.",
}


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


def build_router(
    auth: AuthKit,
    service: AppointmentService,
    clients: ClientService,
    income: IncomeService,
    templates: Jinja2Templates,
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
            # A cancelled appointment frees its slot: it is not drawn on the calendar.
            appointments = await service.in_range(
                conn,
                first,
                days,
                therapist_id=therapist_id,
                room_id=room_id,
                include_cancelled=False,
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
            "randevular/calendar.html",
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
                "notice": NOTICES.get(params.get("durum", "")),
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
        show_payments = appointment is not None and income.can_see(user, appointment.therapist_id)
        async with auth.tx(user) as conn:
            therapists = await service.therapists(conn)
            rooms = await service.rooms(conn)
            people = await clients.search(conn, status="active") if appointment is None else []
            payments = (
                await income.for_appointment(conn, appointment.id)
                if appointment is not None and show_payments
                else []
            )
            session_id = (
                await service.session_for(conn, appointment.id) if appointment is not None else None
            )
        return render(
            request,
            "randevular/form.html",
            {
                "values": values,
                "appointment": appointment,
                "session_id": session_id,
                "errors": errors or {},
                "therapists": therapists,
                "rooms": rooms,
                "people": sorted(people, key=lambda c: c.full_name.casefold()),
                "durations": DURATIONS,
                "conflict": conflict,
                "conflict_fields": CONFLICT_FIELDS,
                "show_payments": show_payments,
                "payments": payments,
                "paid_total": sum((p.amount for p in payments if not p.voided), Decimal("0")),
                "can_pay": user.has(Permission.PAYMENTS_WRITE) and show_payments,
                "can_write": user.has(Permission.SCHEDULE_WRITE_ALL),
                "can_record": user.has(Permission.SESSIONS_WRITE_OWN),
                "money": format_try,
                "method_labels": PAYMENT_METHOD_LABELS,
                "void_reasons": VOID_REASONS,
                "payment_notice": {"1": "Ödeme kaydedildi.", "iptal": "Ödeme iptal edildi."}.get(
                    request.query_params.get("odeme", "")
                ),
                "payment_error": request.query_params.get("odeme_hatasi"),
                "today": to_istanbul(now_utc()).date().isoformat(),
            },
            status,
        )

    def values_of(appt: Appointment) -> dict[str, Any]:
        local = to_istanbul(appt.starts_at)
        return {
            "client_id": str(appt.client_id),
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
        day, _, clock = params.get("baslangic", "").partition("T")
        therapist = params.get("terapist", "")
        if not therapist and user.has(Permission.SESSIONS_WRITE_OWN):
            therapist = str(user.id)  # a therapist usually books for themselves
        room = params.get("oda", "")
        if therapist and not room and parse_uuid(therapist):
            async with auth.tx(user) as conn:
                room = str(await service.default_room(conn, UUID(therapist)) or "")
        values = {
            "client_id": params.get("danisan", ""),
            "date": day or to_istanbul(now_utc()).date().isoformat(),
            "time": clock[:5] or "10:00",
            "duration_min": params.get("sure", "50"),
            "therapist_id": therapist,
            "room_id": room,
            "back": params.get("geri", ""),
        }
        return await form_page(request, user, values=values)

    @router.post("/randevular", response_class=HTMLResponse)
    async def create(request: Request, user: CurrentUser = can_write) -> Response:
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "csrf_token"}
        fields, errors = fields_from(form)
        client_id = parse_uuid(values.get("client_id"))
        if client_id is None:
            errors["client_id"] = "Danışan seçin."
        created: Appointment | None = None
        if not errors and client_id is not None:
            try:
                async with auth.tx(user) as conn:
                    created = await service.create(conn, user, client_id=client_id, fields=fields)
            except AppointmentError as exc:
                errors = {exc.field: exc.message}
        if created is None:
            return await form_page(request, user, values=values, errors=errors, status=422)
        local = to_istanbul(created.starts_at).date().isoformat()
        if values.get("back") == "home":
            return RedirectResponse(f"/?gun={local}&durum=kaydedildi", status_code=303)
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
        if saved is None and conflict is None:
            async with auth.tx(user) as conn:
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
