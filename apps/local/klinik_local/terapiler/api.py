"""JSON API of the Terapiler tab (TERAPILER_TAB §7). Thin: all rules live in the service."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

from klinik_local.terapiler.catalog import Method, MethodChoices, load_catalog
from klinik_local.terapiler.filters import parse_filters
from klinik_local.terapiler.service import (
    SessionCreate,
    SessionError,
    SessionNotFound,
    SessionService,
    SessionUpdate,
    SessionVersionConflict,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.enums import MethodKind
from klinik_shared.permissions import Permission


def error_response(exc: SessionError) -> JSONResponse:
    return JSONResponse(
        {"detail": [{"loc": ["body", exc.field], "msg": exc.message, "type": exc.code}]},
        status_code=422,
    )


NOT_FOUND = {"detail": "not_found"}
CONFLICT = {"detail": "version_conflict"}


def _method(m: Method) -> dict[str, Any]:
    return {"code": m.code, "label_tr": m.label_tr, "description_tr": m.description_tr}


def _choices(c: MethodChoices) -> dict[str, Any]:
    return {"defaults": [_method(m) for m in c.defaults], "others": [_method(m) for m in c.others]}


def build_api_router(auth: AuthKit, sessions: SessionService) -> APIRouter:
    router = APIRouter(prefix="/api", include_in_schema=False)
    can_read = Depends(auth.require(Permission.SESSIONS_READ_OWN))
    can_write = Depends(auth.require(Permission.SESSIONS_WRITE_OWN))

    def no_store(payload: Any, status_code: int = 200) -> JSONResponse:
        return JSONResponse(payload, status_code=status_code, headers={"Cache-Control": "no-store"})

    @router.get("/therapy/age-groups")
    async def age_groups(user: CurrentUser = can_read) -> Response:
        async with auth.tx(user) as conn:
            catalog = await load_catalog(conn)
        return no_store(
            [
                {
                    "code": g.code,
                    "label_tr": g.label_tr,
                    "min_age": g.min_age,
                    "max_age": g.max_age,
                    "sort_order": g.sort_order,
                }
                for g in catalog.age_groups
            ]
        )

    @router.get("/therapy/methods")
    async def methods(age_group: str | None = None, user: CurrentUser = can_read) -> Response:
        async with auth.tx(user) as conn:
            catalog = await load_catalog(conn)
        group = catalog.group_by_code(age_group) if age_group else None
        if age_group and group is None:
            return no_store(NOT_FOUND, 404)
        return no_store(
            {
                "age_group": group.code if group else None,
                "formats": _choices(catalog.choices(MethodKind.FORMAT, group)),
                "modalities": _choices(catalog.choices(MethodKind.MODALITY, group)),
            }
        )

    @router.get("/therapy/sessions")
    async def list_sessions(request: Request, user: CurrentUser = can_read) -> Response:
        filters = parse_filters(request.query_params)
        async with auth.tx(user) as conn:
            page = await sessions.list_page(conn, user, filters)
        return no_store(
            {
                "items": [item.to_json() for item in page.items],
                "total": page.total,
                "page": page.page,
                "page_size": page.page_size,
            }
        )

    @router.get("/therapy/sessions/counts")
    async def counts(request: Request, user: CurrentUser = can_read) -> Response:
        async with auth.tx(user) as conn:
            return no_store(await sessions.counts(conn, user, parse_filters(request.query_params)))

    @router.get("/therapy/sessions/{session_id}")
    async def detail(session_id: UUID, user: CurrentUser = can_read) -> Response:
        """The only JSON route that returns decrypted notes; every call is audited."""
        async with auth.tx(user) as conn:
            try:
                return no_store((await sessions.get_detail(conn, user, session_id)).to_json())
            except SessionNotFound:
                return no_store(NOT_FOUND, 404)

    @router.post("/therapy/sessions")
    async def create(data: SessionCreate, user: CurrentUser = can_write) -> Response:
        try:
            async with auth.tx(user) as conn:
                item = await sessions.create(conn, user, data)
        except SessionError as exc:
            return error_response(exc)
        return no_store(item.to_json(), 201)

    @router.patch("/therapy/sessions/{session_id}")
    async def patch(
        session_id: UUID, data: SessionUpdate, user: CurrentUser = can_write
    ) -> Response:
        try:
            async with auth.tx(user) as conn:
                item = await sessions.update(conn, user, session_id, data)
        except SessionError as exc:
            return error_response(exc)
        except SessionNotFound:
            return no_store(NOT_FOUND, 404)
        except SessionVersionConflict:
            return no_store(CONFLICT, 409)
        return no_store(item.to_json())

    @router.delete("/therapy/sessions/{session_id}")
    async def remove(session_id: UUID, user: CurrentUser = can_write) -> Response:
        try:
            async with auth.tx(user) as conn:
                await sessions.soft_delete(conn, user, session_id)
        except SessionNotFound:
            return no_store(NOT_FOUND, 404)
        return Response(status_code=204)

    class LinkIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        appointment_id: UUID

    @router.post("/therapy/sessions/{session_id}/link-appointment")
    async def link(session_id: UUID, data: LinkIn, user: CurrentUser = can_write) -> Response:
        try:
            async with auth.tx(user) as conn:
                item = await sessions.link_appointment(conn, user, session_id, data.appointment_id)
        except SessionError as exc:
            return error_response(exc)
        except SessionNotFound:
            return no_store(NOT_FOUND, 404)
        return no_store(item.to_json())

    @router.get("/clients/{client_id}/sessions")
    async def timeline(client_id: UUID, limit: int = 10, user: CurrentUser = can_read) -> Response:
        async with auth.tx(user) as conn:
            items = await sessions.timeline(conn, client_id, limit)
        return no_store([item.to_json() for item in items])

    return router
