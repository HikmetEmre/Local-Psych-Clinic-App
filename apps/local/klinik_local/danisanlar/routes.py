"""Danışanlar tab (local tier only)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from klinik_local.danisanlar.service import (
    ClientNotFound,
    ClientService,
    ClientView,
    VersionConflict,
    parse_client_form,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.permissions import Permission

STATUS_FILTERS = {"aktif": "active", "pasif": "inactive", "tum": "all"}


def build_router(auth: AuthKit, clients: ClientService, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix="/danisanlar", include_in_schema=False)
    can_read = Depends(auth.require(Permission.CLIENTS_READ))
    can_write = Depends(auth.require(Permission.CLIENTS_WRITE))

    def not_found(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": "Danışan bulunamadı", "message": "Bu kayıt mevcut değil."},
            status_code=404,
        )

    def form_page(
        request: Request,
        *,
        client: ClientView | None,
        values: dict[str, Any],
        errors: dict[str, str],
        therapists: list[Any],
        status_code: int = 200,
        saved: bool = False,
        conflict: bool = False,
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "danisanlar/form.html",
            {
                "client": client,
                "values": values,
                "errors": errors,
                "therapists": therapists,
                "saved": saved,
                "conflict": conflict,
            },
            status_code=status_code,
        )

    def values_of(client: ClientView) -> dict[str, Any]:
        return {
            "full_name": client.full_name,
            "phone": client.phone or "",
            "email": client.email or "",
            "birth_date": client.birth_date.isoformat() if client.birth_date else "",
            "assigned_therapist_id": str(client.assigned_therapist_id or ""),
            "version": client.version,
        }

    @router.get("", response_class=HTMLResponse)
    async def list_clients(
        request: Request, q: str = "", durum: str = "aktif", user: CurrentUser = can_read
    ) -> Response:
        status = STATUS_FILTERS.get(durum, "active")
        async with auth.tx(user) as conn:
            rows = await clients.search(conn, query=q, status=status)
            therapists = {th.id: th.display_name for th in await clients.therapists(conn)}
        return templates.TemplateResponse(
            request,
            "danisanlar/list.html",
            {
                "clients": rows,
                "therapists": therapists,
                "q": q,
                "durum": durum if durum in STATUS_FILTERS else "aktif",
                "can_write": user.has(Permission.CLIENTS_WRITE),
            },
        )

    @router.get("/yeni", response_class=HTMLResponse)
    async def new_client(request: Request, user: CurrentUser = can_write) -> Response:
        async with auth.tx(user) as conn:
            therapists = await clients.therapists(conn)
        default_therapist = str(user.id) if any(th.id == user.id for th in therapists) else ""
        return form_page(
            request,
            client=None,
            values={"assigned_therapist_id": default_therapist},
            errors={},
            therapists=therapists,
        )

    @router.post("", response_class=HTMLResponse)
    async def create_client(request: Request, user: CurrentUser = can_write) -> Response:
        form = dict(await request.form())
        async with auth.tx(user) as conn:
            therapists = await clients.therapists(conn)
            data, errors = parse_client_form(form, {th.id for th in therapists})
            created = await clients.create(conn, user, data) if data is not None else None
        if created is None:
            return form_page(
                request,
                client=None,
                values=form,
                errors=errors,
                therapists=therapists,
                status_code=422,
            )
        return RedirectResponse(f"/danisanlar/{created.id}?kaydedildi=1", status_code=303)

    @router.get("/{client_id}", response_class=HTMLResponse)
    async def edit_client(
        request: Request, client_id: UUID, kaydedildi: int = 0, user: CurrentUser = can_read
    ) -> Response:
        async with auth.tx(user) as conn:
            try:
                client = await clients.get(conn, client_id)
            except ClientNotFound:
                return not_found(request)
            therapists = await clients.therapists(conn)
        return form_page(
            request,
            client=client,
            values=values_of(client),
            errors={},
            therapists=therapists,
            saved=bool(kaydedildi),
        )

    @router.post("/{client_id}", response_class=HTMLResponse)
    async def update_client(
        request: Request, client_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        form = dict(await request.form())
        try:
            expected_version = int(str(form.get("version") or ""))
        except ValueError:
            expected_version = -1
        conflict = False
        async with auth.tx(user) as conn:
            try:
                current = await clients.get(conn, client_id)
            except ClientNotFound:
                return not_found(request)
            therapists = await clients.therapists(conn)
            data, errors = parse_client_form(form, {th.id for th in therapists})
            if data is not None:
                try:
                    await clients.update(conn, user, client_id, data, expected_version)
                except VersionConflict:
                    conflict = True
        if data is None or conflict:
            return form_page(
                request,
                client=current,
                values={**form, "version": current.version},
                errors=errors,
                therapists=therapists,
                status_code=409 if conflict else 422,
                conflict=conflict,
            )
        return RedirectResponse(f"/danisanlar/{client_id}?kaydedildi=1", status_code=303)

    @router.post("/{client_id}/durum")
    async def set_status(
        request: Request, client_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        active = str((await request.form()).get("active") or "") == "1"
        async with auth.tx(user) as conn:
            try:
                await clients.set_active(conn, user, client_id, active)
            except ClientNotFound:
                return not_found(request)
        return RedirectResponse(f"/danisanlar/{client_id}?kaydedildi=1", status_code=303)

    return router
