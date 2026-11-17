"""Raporlar tab: choose a report and a period, download a PDF.

Each download is written to the audit log (which report, which period; never content).
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_local.danisanlar.service import ClientService
from klinik_local.randevular.service import AppointmentService
from klinik_local.raporlar import pdf
from klinik_local.raporlar.reports import BUILDERS, KINDS, Params, ReportNotFound
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import now_utc, to_istanbul

MAX_DAYS = 400
PRESETS = {
    "bu-ay": "Bu ay",
    "gecen-ay": "Geçen ay",
    "bu-hafta": "Bu hafta",
    "bugun": "Bugün",
    "bu-yil": "Bu yıl",
}


def parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def preset_range(name: str, today: date) -> tuple[date, date]:
    if name == "bugun":
        return today, today
    if name == "bu-hafta":
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if name == "gecen-ay":
        last = today.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if name == "bu-yil":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    first = today.replace(day=1)
    following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first, following - timedelta(days=1)


def allowed_kinds(user: CurrentUser) -> dict[str, str]:
    kinds = {}
    if user.has(Permission.SESSIONS_READ_OWN):
        kinds["seanslar"] = KINDS["seanslar"]
    if user.has(Permission.CLIENTS_READ) and user.has(Permission.SESSIONS_READ_OWN):
        kinds["danisan"] = KINDS["danisan"]
    if user.has(Permission.SCHEDULE_READ_ALL):
        kinds["randevular"] = KINDS["randevular"]
        kinds["istatistik"] = KINDS["istatistik"]
    if user.has(Permission.INCOME_READ_ALL) or user.has(Permission.INCOME_READ_OWN):
        kinds["gelir"] = KINDS["gelir"]
    return kinds


def build_router(
    auth: AuthKit,
    cipher: FieldCipher,
    clients: ClientService,
    appointments: AppointmentService,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(prefix="/raporlar", include_in_schema=False)
    logged_in = Depends(auth.require())

    async def page(
        request: Request, user: CurrentUser, *, error: str | None = None, status: int = 200
    ) -> Response:
        today = to_istanbul(now_utc()).date()
        first, last = preset_range("bu-ay", today)
        async with auth.tx(user) as conn:
            therapists = await appointments.therapists(conn)
            rooms = await appointments.rooms(conn, active_only=False)
            people = await clients.search(conn, status="all")
        response = templates.TemplateResponse(
            request,
            "raporlar.html",
            {
                "kinds": allowed_kinds(user),
                "therapists": therapists,
                "rooms": rooms,
                "people": sorted(people, key=lambda c: c.full_name.casefold()),
                "first": first.isoformat(),
                "last": last.isoformat(),
                "presets": PRESETS,
                "sees_all": user.has(Permission.SESSIONS_READ_OTHERS),
                "income_all": user.has(Permission.INCOME_READ_ALL),
                "error": error,
            },
            status_code=status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("", response_class=HTMLResponse)
    async def index(request: Request, user: CurrentUser = logged_in) -> Response:
        return await page(request, user)

    @router.get("/pdf")
    async def download(request: Request, user: CurrentUser = logged_in) -> Response:
        q = request.query_params
        kind = q.get("rapor", "")
        if kind not in allowed_kinds(user):
            return await page(request, user, error="Bu raporu oluşturma yetkiniz yok.", status=403)
        today = to_istanbul(now_utc()).date()
        if q.get("donem") in PRESETS:
            first, last = preset_range(str(q.get("donem")), today)
        else:
            first = parse_date(q.get("baslangic")) or preset_range("bu-ay", today)[0]
            last = parse_date(q.get("bitis")) or first
        if last < first:
            first, last = last, first
        if (last - first).days > MAX_DAYS:
            return await page(request, user, error="Dönem en fazla 400 gün olabilir.", status=422)
        params = Params(
            kind=kind,
            first=first,
            last=last,
            therapist_id=parse_uuid(q.get("terapist")),
            room_id=parse_uuid(q.get("oda")),
            client_id=parse_uuid(q.get("danisan")),
            include_notes=q.get("notlar") == "1",
            include_cancelled=(q.getlist("iptaller") or ["1"])[-1] == "1",
        )
        if kind == "danisan" and params.client_id is None:
            return await page(request, user, error="Danışan seçin.", status=422)
        meta = pdf.Meta(
            title=KINDS[kind],
            subtitle=f"{first.strftime('%d.%m.%Y')} – {last.strftime('%d.%m.%Y')}"
            if kind != "danisan"
            else "Danışan dosyası",
            author=user.display_name,
            generated=now_utc(),
        )
        try:
            async with auth.tx(user) as conn:
                story = await BUILDERS[kind](conn, cipher, user, params, meta)
                await write_audit(
                    conn,
                    t.audit_log,
                    action="report_generated",
                    actor_id=user.id,
                    entity_type="report",
                    entity_id=params.client_id,
                    detail={
                        "report": kind,
                        "from": first.isoformat(),
                        "to": last.isoformat(),
                        "therapist": str(params.therapist_id or ""),
                        "notes": params.include_notes,
                    },
                )
        except ReportNotFound:
            return await page(request, user, error="Bu rapor için kayıt bulunamadı.", status=404)
        content = await asyncio.to_thread(pdf.build, meta, story)
        name = (
            f"nil-{kind}-{first.isoformat()}.pdf"
            if kind != "danisan"
            else "nil-danisan-dosyasi.pdf"
        )
        headers: dict[str, Any] = {
            "Content-Disposition": f"inline; filename=\"{name}\"; filename*=UTF-8''{quote(name)}",
            "Cache-Control": "no-store",
        }
        return Response(content, media_type="application/pdf", headers=headers)

    return router
