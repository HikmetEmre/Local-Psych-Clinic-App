"""Ayarlar → Odalar (admin.catalog): rooms and each therapist's default room."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert, select, update

from klinik_local import tables as t
from klinik_local.randevular.service import AppointmentService
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.permissions import Permission

PATH = "/ayarlar/odalar"
MAX_LABEL = 30


def _label(value: object) -> str:
    return " ".join(str(value or "").split())


def build_router(
    auth: AuthKit, service: AppointmentService, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter(prefix=PATH, include_in_schema=False)
    admin = Depends(auth.require(Permission.ADMIN_CATALOG))

    async def page(
        request: Request, user: CurrentUser, *, error: str | None = None, status: int = 200
    ) -> Response:
        async with auth.tx(user) as conn:
            rooms = (
                await conn.execute(select(t.rooms).order_by(t.rooms.c.sort_order, t.rooms.c.label))
            ).all()
            therapists = await service.therapists(conn)
            defaults = {
                r.id: r.default_room_id
                for r in (await conn.execute(select(t.users.c.id, t.users.c.default_room_id))).all()
            }
        response = templates.TemplateResponse(
            request,
            "ayarlar/odalar.html",
            {
                "rooms": rooms,
                "therapists": therapists,
                "defaults": defaults,
                "error": error,
                "notice": "Kaydedildi." if request.query_params.get("ok") else None,
            },
            status_code=status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("", response_class=HTMLResponse)
    async def show(request: Request, user: CurrentUser = admin) -> Response:
        return await page(request, user)

    @router.post("", response_class=HTMLResponse)
    async def add(request: Request, user: CurrentUser = admin) -> Response:
        form = await request.form()
        label = _label(form.get("label"))
        if not label or len(label) > MAX_LABEL:
            return await page(request, user, error="Oda adı 1–30 karakter olmalı.", status=422)
        async with auth.tx(user) as conn:
            room_id = (
                await conn.execute(insert(t.rooms).values(label=label).returning(t.rooms.c.id))
            ).scalar_one()
            await write_audit(
                conn, t.audit_log, action="room_created", actor_id=user.id,
                entity_type="room", entity_id=room_id,
            )  # fmt: skip
        return RedirectResponse(f"{PATH}?ok=1", status_code=303)

    @router.post("/{room_id}", response_class=HTMLResponse)
    async def save(request: Request, room_id: UUID, user: CurrentUser = admin) -> Response:
        form = await request.form()
        label = _label(form.get("label"))
        try:
            order = int(str(form.get("sort_order") or "0"))
        except ValueError:
            order = 0
        if not label or len(label) > MAX_LABEL:
            return await page(request, user, error="Oda adı 1–30 karakter olmalı.", status=422)
        active = form.get("is_active") == "1"
        async with auth.tx(user) as conn:
            await conn.execute(
                update(t.rooms)
                .where(t.rooms.c.id == room_id)
                .values(label=label, is_active=active, sort_order=order)
            )
            await write_audit(
                conn, t.audit_log, action="room_updated", actor_id=user.id,
                entity_type="room", entity_id=room_id, detail={"is_active": active},
            )  # fmt: skip
        return RedirectResponse(f"{PATH}?ok=1", status_code=303)

    @router.post("/varsayilan/{therapist_id}", response_class=HTMLResponse)
    async def default_room(
        request: Request, therapist_id: UUID, user: CurrentUser = admin
    ) -> Response:
        raw = str((await request.form()).get("room_id") or "")
        room_id: UUID | None
        try:
            room_id = UUID(raw) if raw else None
        except ValueError:
            return await page(request, user, error="Oda seçimi geçersiz.", status=422)
        async with auth.tx(user) as conn:
            if therapist_id not in {o.id for o in await service.therapists(conn)}:
                return await page(request, user, error="Terapist bulunamadı.", status=404)
            if room_id is not None and room_id not in {o.id for o in await service.rooms(conn)}:
                return await page(request, user, error="Oda seçimi geçersiz.", status=422)
            await conn.execute(
                update(t.users).where(t.users.c.id == therapist_id).values(default_room_id=room_id)
            )
        return RedirectResponse(f"{PATH}?ok=1", status_code=303)

    return router
