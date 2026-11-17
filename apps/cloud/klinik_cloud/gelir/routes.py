"""Gelir tab and the payment actions on an appointment."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from klinik_cloud import tables as t
from klinik_cloud.gelir.service import (
    PERIODS,
    VOID_REASONS,
    IncomeService,
    PaymentError,
    PaymentNotFound,
    format_try,
    income_scope,
    parse_amount,
    period_for,
)
from klinik_cloud.takvim.service import AppointmentService
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.calendar import MONTHS_TR, parse_day
from klinik_shared.labels_tr import PAYMENT_METHOD_LABELS
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import format_datetime_tr, now_utc, to_istanbul

PERIOD_LABELS = {"gun": "Gün", "hafta": "Hafta", "ay": "Ay"}


def period_title(kind: str, first: date, last: date) -> str:
    if kind == "gun":
        return f"{first.day} {MONTHS_TR[first.month - 1]} {first.year}"
    if kind == "ay":
        return f"{MONTHS_TR[first.month - 1]} {first.year}"
    if first.month == last.month:
        return f"{first.day}–{last.day} {MONTHS_TR[first.month - 1]} {first.year}"
    start = f"{first.day} {MONTHS_TR[first.month - 1]}"
    return f"{start} – {last.day} {MONTHS_TR[last.month - 1]} {last.year}"


def build_router(
    auth: AuthKit,
    income: IncomeService,
    appointments: AppointmentService,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    logged_in = Depends(auth.require())
    can_pay = Depends(auth.require(Permission.PAYMENTS_WRITE))
    can_export = Depends(auth.require(Permission.INCOME_READ_ALL, Permission.ADMIN_EXPORT))

    def forbidden(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Yetkiniz yok",
                "message": "Gelir bilgilerini görüntüleme yetkiniz bulunmuyor.",
            },
            status_code=403,
        )

    @router.get("/gelir", response_class=HTMLResponse)
    async def report(request: Request, user: CurrentUser = logged_in) -> Response:
        scope = income_scope(user)
        if scope is None:
            return forbidden(request)
        params = request.query_params
        kind = str(params.get("donem")) if params.get("donem") in PERIODS else "ay"
        period = period_for(kind, parse_day(params.get("tarih")))
        therapist_id = None
        if scope == "all" and params.get("terapist"):
            try:
                therapist_id = UUID(params["terapist"])
            except ValueError:
                therapist_id = None
        async with auth.tx(user) as conn:
            result = await income.report(conn, user, period, therapist_id=therapist_id)
            therapists = await appointments.therapists(conn) if scope == "all" else []
        largest = max((amount for _, amount in result.by_day), default=0) or 1

        def url(**changes: Any) -> str:
            query = {
                "donem": kind,
                "tarih": period.first.isoformat(),
                "terapist": str(therapist_id or ""),
                **{k: str(v) for k, v in changes.items()},
            }
            return "/gelir?" + urlencode({k: v for k, v in query.items() if v})

        response = templates.TemplateResponse(
            request,
            "gelir.html",
            {
                "r": result,
                "kind": kind,
                "title": period_title(kind, period.first, period.last),
                "periods": PERIOD_LABELS,
                "therapists": therapists,
                "therapist_id": therapist_id,
                "url": url,
                "prev": period.step(-1).isoformat(),
                "next": period.step(1).isoformat(),
                "today": to_istanbul(now_utc()).date().isoformat(),
                "largest": largest,
                "money": format_try,
                "method_labels": PAYMENT_METHOD_LABELS,
                "void_reasons": VOID_REASONS,
                "can_pay": user.has(Permission.PAYMENTS_WRITE),
                "can_export": user.has(Permission.ADMIN_EXPORT) and scope == "all",
            },
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("/gelir/odemeler.csv")
    async def export(request: Request, user: CurrentUser = can_export) -> Response:
        params = request.query_params
        kind = str(params.get("donem")) if params.get("donem") in PERIODS else "ay"
        period = period_for(kind, parse_day(params.get("tarih")))
        async with auth.tx(user) as conn:
            result = await income.report(conn, user, period)
            await write_audit(
                conn,
                t.audit_log,
                action="export",
                actor_id=user.id,
                entity_type="payments",
                detail={
                    "period": kind,
                    "from": period.first.isoformat(),
                    "rows": len(result.payments),
                },
            )
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(
            ["Tarih", "Danışan kodu", "Terapist", "Tutar", "Yöntem", "Durum", "Kaydeden"]
        )
        for p in result.payments:
            writer.writerow(
                [
                    format_datetime_tr(p.paid_at),
                    p.client_code,
                    p.therapist_name,
                    f"{p.amount:.2f}".replace(".", ","),
                    PAYMENT_METHOD_LABELS.get(p.method, p.method),
                    f"İptal ({VOID_REASONS.get(p.void_reason or '', '')})"
                    if p.voided
                    else "Geçerli",
                    p.recorded_by_name,
                ]
            )
        return Response(
            "﻿" + buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="odemeler-{period.first.isoformat()}.csv"'
                ),
                "Cache-Control": "no-store",
            },
        )

    @router.post("/odemeler", response_class=HTMLResponse)
    async def record(request: Request, user: CurrentUser = can_pay) -> Response:
        form = await request.form()
        try:
            appointment_id = UUID(str(form.get("appointment_id") or ""))
        except ValueError:
            return RedirectResponse("/takvim", status_code=303)
        error: str | None = None
        try:
            amount = parse_amount(form.get("amount"))
            raw_day = str(form.get("paid_on") or "")
            paid_on = date.fromisoformat(raw_day) if raw_day else None
            async with auth.tx(user) as conn:
                await income.record(
                    conn,
                    user,
                    appointment_id=appointment_id,
                    amount=amount,
                    method=str(form.get("method") or ""),
                    paid_on=paid_on,
                )
        except PaymentError as exc:
            error = exc.message
        except ValueError:
            error = "Ödeme tarihi geçersiz."
        target = f"/randevular/{appointment_id}"
        if error is not None:
            return RedirectResponse(
                f"{target}?{urlencode({'odeme_hatasi': error})}", status_code=303
            )
        return RedirectResponse(f"{target}?odeme=1#odemeler", status_code=303)

    @router.post("/odemeler/{payment_id}/iptal", response_class=HTMLResponse)
    async def void(request: Request, payment_id: UUID, user: CurrentUser = can_pay) -> Response:
        form = await request.form()
        back = str(form.get("geri") or "")
        try:
            async with auth.tx(user) as conn:
                appointment_id = await income.void(
                    conn, user, payment_id, str(form.get("reason") or "")
                )
        except PaymentNotFound:
            return RedirectResponse(back if back.startswith("/") else "/gelir", status_code=303)
        except PaymentError as exc:
            target = back if back.startswith("/") else "/gelir"
            joiner = "&" if "?" in target else "?"
            return RedirectResponse(
                f"{target}{joiner}{urlencode({'odeme_hatasi': exc.message})}", status_code=303
            )
        if back.startswith("/gelir"):
            return RedirectResponse(back, status_code=303)
        return RedirectResponse(
            f"/randevular/{appointment_id}?odeme=iptal#odemeler", status_code=303
        )

    return router
