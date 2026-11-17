"""Ayarlar → Katalog: age groups, therapy methods and per-group defaults (admin.catalog).

Methods are never deleted (history refers to them), only deactivated. A method's
kind (format / modality) cannot change; the database refuses it too.
"""

from __future__ import annotations

import re
from typing import Any

from asyncpg.exceptions import ExclusionViolationError, UniqueViolationError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import DBAPIError

from klinik_local import tables as t
from klinik_local.terapiler.catalog import load_catalog
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.enums import MethodKind
from klinik_shared.permissions import Permission

PATH = "/ayarlar/katalog"
CODE_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")
MAX_LABEL = 80


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _cause(exc: DBAPIError) -> object:
    # asyncpg's exception sits two levels down: SQLAlchemy -> adapter -> asyncpg.
    inner = exc.orig
    return getattr(inner, "__cause__", None) or inner


def build_router(auth: AuthKit, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix=PATH, include_in_schema=False)
    admin = Depends(auth.require(Permission.ADMIN_CATALOG))

    async def page(
        request: Request,
        user: CurrentUser,
        *,
        notice: str | None = None,
        error: str | None = None,
        status: int = 200,
    ) -> Response:
        async with auth.tx(user) as conn:
            catalog = await load_catalog(conn)
            groups = (
                await conn.execute(select(t.age_groups).order_by(t.age_groups.c.sort_order))
            ).all()
        methods = sorted(catalog.methods.values(), key=lambda m: (m.kind, m.label_tr))
        defaults = {(g, m) for g, ids in catalog.defaults.items() for m in ids}
        response = templates.TemplateResponse(
            request,
            "ayarlar/katalog.html",
            {
                "groups": groups,
                "active_groups": catalog.age_groups,
                "methods": methods,
                "defaults": defaults,
                "notice": notice or ("Kaydedildi." if request.query_params.get("ok") else None),
                "error": error,
            },
            status_code=status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def done() -> Response:
        return RedirectResponse(f"{PATH}?ok=1", status_code=303)

    @router.get("", response_class=HTMLResponse)
    async def show(request: Request, user: CurrentUser = admin) -> Response:
        return await page(request, user)

    # ------------------------------------------------------------ age groups

    @router.post("/yas-gruplari/{group_id}", response_class=HTMLResponse)
    async def save_group(request: Request, group_id: int, user: CurrentUser = admin) -> Response:
        form = await request.form()
        label = _clean(form.get("label_tr"))
        min_age = _int(form.get("min_age"))
        max_raw = _clean(form.get("max_age"))
        max_age = _int(max_raw) if max_raw else None
        sort_order = _int(form.get("sort_order"))
        active = form.get("is_active") == "1"
        if (
            not label
            or len(label) > MAX_LABEL
            or min_age is None
            or min_age < 0
            or sort_order is None
            or (max_raw and (max_age is None or max_age < min_age))
        ):
            return await page(request, user, error="Yaş grubu bilgileri geçersiz.", status=422)
        try:
            async with auth.tx(user) as conn:
                await conn.execute(
                    update(t.age_groups)
                    .where(t.age_groups.c.id == group_id)
                    .values(
                        label_tr=label,
                        min_age=min_age,
                        max_age=max_age,
                        sort_order=sort_order,
                        is_active=active,
                    )
                )
                await write_audit(
                    conn,
                    t.audit_log,
                    action="catalog_age_group_updated",
                    actor_id=user.id,
                    entity_type="age_group",
                    entity_id=str(group_id),
                    detail={"min_age": min_age, "max_age": max_age, "is_active": active},
                )
        except DBAPIError as exc:
            if isinstance(_cause(exc), ExclusionViolationError):
                return await page(
                    request,
                    user,
                    error="Yaş aralıkları çakışıyor. Önce komşu grubun aralığını daraltın.",
                    status=409,
                )
            raise
        return done()

    @router.post("/yas-gruplari", response_class=HTMLResponse)
    async def add_group(request: Request, user: CurrentUser = admin) -> Response:
        form = await request.form()
        code = _clean(form.get("code")).lower()
        label = _clean(form.get("label_tr"))
        min_age = _int(form.get("min_age"))
        max_raw = _clean(form.get("max_age"))
        max_age = _int(max_raw) if max_raw else None
        sort_order = _int(form.get("sort_order")) or 99
        if (
            not CODE_RE.fullmatch(code)
            or not label
            or len(label) > MAX_LABEL
            or min_age is None
            or min_age < 0
            or (max_raw and (max_age is None or max_age < min_age))
        ):
            return await page(request, user, error="Yeni yaş grubu bilgileri geçersiz.", status=422)
        try:
            async with auth.tx(user) as conn:
                await conn.execute(
                    insert(t.age_groups).values(
                        code=code,
                        label_tr=label,
                        min_age=min_age,
                        max_age=max_age,
                        sort_order=sort_order,
                        is_active=True,
                    )
                )
                await write_audit(
                    conn,
                    t.audit_log,
                    action="catalog_age_group_created",
                    actor_id=user.id,
                    entity_type="age_group",
                    entity_id=code,
                )
        except DBAPIError as exc:
            cause = _cause(exc)
            if isinstance(cause, ExclusionViolationError):
                return await page(request, user, error="Yaş aralıkları çakışıyor.", status=409)
            if isinstance(cause, UniqueViolationError):
                return await page(request, user, error="Bu kod zaten kullanılıyor.", status=409)
            raise
        return done()

    # ------------------------------------------------------------ methods

    @router.post("/yontemler", response_class=HTMLResponse)
    async def add_method(request: Request, user: CurrentUser = admin) -> Response:
        form = await request.form()
        code = _clean(form.get("code")).lower()
        label = _clean(form.get("label_tr"))
        kind = str(form.get("kind") or "")
        if (
            not CODE_RE.fullmatch(code)
            or not label
            or len(label) > MAX_LABEL
            or kind not in {k.value for k in MethodKind}
        ):
            return await page(
                request,
                user,
                error="Yöntem kodu küçük harf, rakam ve _ içermeli; ad ve tür zorunludur.",
                status=422,
            )
        try:
            async with auth.tx(user) as conn:
                await conn.execute(
                    insert(t.therapy_methods).values(code=code, label_tr=label, kind=kind)
                )
                await write_audit(
                    conn,
                    t.audit_log,
                    action="catalog_method_created",
                    actor_id=user.id,
                    entity_type="therapy_method",
                    entity_id=code,
                    detail={"kind": kind},
                )
        except DBAPIError as exc:
            if isinstance(_cause(exc), UniqueViolationError):
                return await page(request, user, error="Bu kod zaten kullanılıyor.", status=409)
            raise
        return done()

    @router.post("/yontemler/{method_id}", response_class=HTMLResponse)
    async def save_method(request: Request, method_id: int, user: CurrentUser = admin) -> Response:
        form = await request.form()
        label = _clean(form.get("label_tr"))
        active = form.get("is_active") == "1"
        description = _clean(form.get("description_tr")) or None
        if not label or len(label) > MAX_LABEL or (description and len(description) > 300):
            return await page(request, user, error="Yöntem adı geçersiz.", status=422)
        async with auth.tx(user) as conn:
            await conn.execute(
                update(t.therapy_methods)
                .where(t.therapy_methods.c.id == method_id)
                .values(label_tr=label, is_active=active, description_tr=description)
            )
            await write_audit(
                conn,
                t.audit_log,
                action="catalog_method_updated",
                actor_id=user.id,
                entity_type="therapy_method",
                entity_id=str(method_id),
                detail={"is_active": active},
            )
        return done()

    # ------------------------------------------------------------ defaults matrix

    @router.post("/varsayilanlar", response_class=HTMLResponse)
    async def save_defaults(request: Request, user: CurrentUser = admin) -> Response:
        form = await request.form()
        wanted: set[tuple[int, int]] = set()
        for raw in form.getlist("default"):
            group_id, _, method_id = str(raw).partition(":")
            g, m = _int(group_id), _int(method_id)
            if g is not None and m is not None:
                wanted.add((g, m))
        d = t.age_group_therapy_defaults
        async with auth.tx(user) as conn:
            catalog = await load_catalog(conn)
            valid_groups = {g.id for g in catalog.age_groups}
            valid_methods = {m.id for m in catalog.methods.values()}
            wanted = {(g, m) for g, m in wanted if g in valid_groups and m in valid_methods}
            current = {(g, m) for g, ids in catalog.defaults.items() for m in ids}
            removed = current - wanted
            added = wanted - current
            for g, m in removed:
                await conn.execute(
                    delete(d).where(d.c.age_group_id == g, d.c.therapy_method_id == m)
                )
            # New defaults go to the end of their group's order.
            rows: list[dict[str, Any]] = []
            for g, m in sorted(added):
                position = (
                    len(catalog.defaults.get(g, []))
                    + 1
                    + sum(1 for row in rows if row["age_group_id"] == g)
                )
                rows.append({"age_group_id": g, "therapy_method_id": m, "sort_order": position})
            if rows:
                await conn.execute(insert(d), rows)
            if added or removed:
                await write_audit(
                    conn,
                    t.audit_log,
                    action="catalog_defaults_updated",
                    actor_id=user.id,
                    entity_type="age_group_therapy_defaults",
                    detail={"added": len(added), "removed": len(removed)},
                )
        return done()

    return router
