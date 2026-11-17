"""Terapiler tab: server-rendered pages (TERAPILER_TAB §6).

All filtering, sorting and paging happen on the server (``SessionService``).
Notes are decrypted only on the detail and edit pages, and the service audits
each of those reads.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from starlette.datastructures import QueryParams

from klinik_local import tables as t
from klinik_local.danisanlar.service import ClientNotFound, ClientService, ClientView
from klinik_local.terapiler.catalog import AgeGroup, Catalog, Method, NoAgeGroupError, load_catalog
from klinik_local.terapiler.filters import parse_filters
from klinik_local.terapiler.service import (
    SessionCreate,
    SessionError,
    SessionFilters,
    SessionNotFound,
    SessionService,
    SessionUpdate,
    SessionVersionConflict,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.enums import AppointmentStatus, MethodKind
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import ISTANBUL, age_on, format_datetime_tr, now_utc, to_istanbul

PAGE_SIZES = (10, 25, 50, 100)

# (sort key, label); a None key is not sortable.
COLUMNS = (
    ("client", "Danışan"),
    ("age", "Yaş"),
    ("session_number", "Seans"),
    ("session_date", "Tarih"),
    (None, "Yöntem(ler)"),
    ("format", "Format"),
    ("duration_min", "Süre"),
    ("therapist", "Terapist"),
    ("status", "Durum"),
)

FIELD_ERRORS = {
    "client_id": "Danışan seçin.",
    "session_date": "Tarih ve saat geçersiz.",
    "duration_min": "Süre 10 ile 240 dakika arasında olmalı.",
    "format_code": "Format seçin.",
    "methods": "Yöntem seçimi geçersiz.",
    "status": "Durum geçersiz.",
    "notes": "Seans notu çok uzun.",
    "homework": "Ödev metni çok uzun.",
    "next_session_date": "Sonraki seans tarihi geçersiz.",
}


# ---------------------------------------------------------------- helpers


def link(params: QueryParams, path: str = "/terapiler", **changes: Any) -> str:
    """``path`` with the current query string; given keys are replaced, or dropped when None."""
    items = [(k, v) for k, v in params.multi_items() if k not in changes]
    for key, value in changes.items():
        if value is None:
            continue
        if isinstance(value, list | tuple):
            items.extend((key, str(v)) for v in value)
        else:
            items.append((key, str(value)))
    query = urlencode(items)
    return f"{path}?{query}" if query else path


def local_input(dt: datetime | None) -> str:
    """Value for ``<input type="datetime-local">``, in Istanbul time."""
    return to_istanbul(dt).strftime("%Y-%m-%dT%H:%M") if dt else ""


def parse_local(value: object) -> datetime | None:
    """A time typed in the clinic (``2026-11-17T14:00``) is Istanbul time."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw)  # ValueError for junk
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=ISTANBUL)


def digest(value: str | None) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def form_text(form: Mapping[str, Any], name: str) -> str | None:
    value = str(form.get(name) or "").replace("\r\n", "\n")
    return value if value.strip() else None


def selected_methods(form: Any) -> list[dict[str, Any]]:
    """Ticked modalities, the starred (primary) one first."""
    codes = list(dict.fromkeys(str(v) for v in form.getlist("method") if str(v).strip()))
    primary = str(form.get("primary") or "")
    if primary in codes:
        codes.remove(primary)
        codes.insert(0, primary)
    return [{"code": code, "is_primary": code == primary} for code in codes]


def parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def default_session_time() -> str:
    now = to_istanbul(now_utc()).replace(minute=0, second=0, microsecond=0)
    return local_input(now)


@dataclass(frozen=True)
class MethodSection:
    """Everything the form needs that depends on (client, date): the age group and choices."""

    group: AgeGroup | None
    age: int | None
    problem: str | None  # why no age group could be determined
    formats: list[Method]
    default_methods: list[Method]
    other_methods: list[Method]
    warnings: list[str]


def method_section(
    catalog: Catalog, client: ClientView | None, values: Mapping[str, Any]
) -> MethodSection:
    group: AgeGroup | None = None
    age: int | None = None
    problem: str | None = None
    try:
        when = parse_local(values.get("session_date")) or now_utc()
    except ValueError:
        when = now_utc()
    if client is not None:
        if client.birth_date is None:
            problem = "Danışanın doğum tarihi eksik. Önce danışan profilini güncelleyin."
        else:
            day = to_istanbul(when).date()
            if day < client.birth_date:
                problem = "Seans tarihi doğum tarihinden önce olamaz."
            else:
                age = age_on(client.birth_date, day)
                try:
                    group = catalog.resolve(client.birth_date, day)
                except NoAgeGroupError:
                    problem = "Bu yaş için tanımlı bir yaş grubu yok."

    formats = (
        catalog.choices(MethodKind.FORMAT, group).defaults
        if group is not None
        else catalog.choices(MethodKind.FORMAT, None).all
    )
    current_format = catalog.methods.get(str(values.get("format_code") or ""))
    if current_format is not None and current_format not in formats:
        formats = [*formats, current_format]  # an existing session keeps its format

    modalities = catalog.choices(MethodKind.MODALITY, group)
    offered = {m.code for m in modalities.all}
    selected = [str(c) for c in values.get("methods") or []]
    kept = [
        catalog.methods[c]
        for c in selected
        if c not in offered and c in catalog.methods and catalog.methods[c].kind == "modality"
    ]
    warnings: list[str] = []
    if group is not None:
        warnings = SessionService.method_warnings(catalog, group, selected)
    return MethodSection(
        group, age, problem, formats, modalities.defaults, [*modalities.others, *kept], warnings
    )


def validation_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for err in exc.errors():
        name = str(err["loc"][0]) if err["loc"] else "form"
        errors.setdefault(name, FIELD_ERRORS.get(name, "Bu alan geçersiz."))
    return errors


def build_router(
    auth: AuthKit,
    sessions: SessionService,
    clients: ClientService,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(prefix="/terapiler", include_in_schema=False)
    can_read = Depends(auth.require(Permission.SESSIONS_READ_OWN))
    can_write = Depends(auth.require(Permission.SESSIONS_WRITE_OWN, Permission.CLIENTS_READ))
    can_export = Depends(auth.require(Permission.SESSIONS_READ_OWN, Permission.ADMIN_EXPORT))

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    def not_found(request: Request) -> Response:
        return render(
            request,
            "error.html",
            {"title": "Seans bulunamadı", "message": "Bu kayıt mevcut değil veya size ait değil."},
            404,
        )

    # ------------------------------------------------------------ list

    @router.get("", response_class=HTMLResponse)
    async def index(request: Request, user: CurrentUser = can_read) -> Response:
        params = request.query_params
        filters = parse_filters(params, default_range=True)
        sees_others = user.has(Permission.SESSIONS_READ_OTHERS)
        async with auth.tx(user) as conn:
            catalog = await load_catalog(conn)
            group = catalog.group_by_code(filters.age_group) if filters.age_group else None
            if filters.age_group and group is None:
                filters = replace(filters, age_group=None)
            page = await sessions.list_page(conn, user, filters)
            counts = await sessions.counts(conn, user, filters)
            any_in_group = (await sessions.counts(conn, user, SessionFilters())).get(
                group.code if group else "all", 0
            )
            therapists = await clients.therapists(conn) if sees_others else []
            names: dict[UUID, str] = {}
            if user.has(Permission.CLIENTS_READ) and page.items:
                wanted = {item.client_id for item in page.items}
                everyone = await clients.search(conn, status="all")
                names = {c.id: c.full_name for c in everyone if c.id in wanted}
        chips = catalog.choices(MethodKind.MODALITY, group)
        return render(
            request,
            "terapiler/list.html",
            {
                "page": page,
                "filters": filters,
                "group": group,
                "catalog": catalog,
                "counts": counts,
                "any_in_group": any_in_group,
                "chip_defaults": chips.defaults,
                "chip_others": chips.others,
                "formats": catalog.choices(MethodKind.FORMAT, None).all,
                "therapists": therapists,
                "sees_others": sees_others,
                "names": names,
                "columns": [c for c in COLUMNS if sees_others or c[0] != "therapist"],
                "statuses": [(s.value, STATUS_LABELS[s.value]) for s in AppointmentStatus],
                "page_sizes": PAGE_SIZES,
                "params": params,
                "link": link,
                "can_write": user.has(Permission.SESSIONS_WRITE_OWN),
                "can_export": user.has(Permission.ADMIN_EXPORT),
                "deleted": params.get("silindi") == "1",
            },
        )

    # ------------------------------------------------------------ export (admin)

    @router.get("/disa-aktar.csv")
    async def export(request: Request, user: CurrentUser = can_export) -> Response:
        filters = parse_filters(request.query_params, default_range=True)
        async with auth.tx(user) as conn:
            rows = await sessions.export_rows(conn, user, filters)
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")  # Turkish Excel expects semicolons
        writer.writerow(
            ["Danışan kodu", "Yaş", "Yaş grubu", "Seans", "Tarih", "Süre (dk)", "Format",
             "Birincil yöntem", "Diğer yöntemler", "Durum", "Terapist"]
        )  # fmt: skip
        for item in rows:
            primary = item.primary_method
            writer.writerow(
                [
                    item.client_code,
                    item.age_at_session if item.age_at_session is not None else "",
                    item.age_group_label,
                    item.session_number,
                    format_datetime_tr(item.session_date),
                    item.duration_min,
                    item.format_label,
                    primary.label_tr if primary else "",
                    ", ".join(m.label_tr for m in item.methods if not m.is_primary),
                    STATUS_LABELS.get(item.status, item.status),
                    item.therapist_name,
                ]
            )
        stamp = to_istanbul(now_utc()).strftime("%Y%m%d-%H%M")
        return Response(
            "﻿" + buffer.getvalue(),  # BOM so Excel reads UTF-8
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="seanslar-{stamp}.csv"',
                "Cache-Control": "no-store",
            },
        )

    # ------------------------------------------------------------ timeline row (htmx)

    @router.get("/zaman/{client_id}", response_class=HTMLResponse)
    async def timeline(
        request: Request, client_id: UUID, satir: str = "", user: CurrentUser = can_read
    ) -> Response:
        async with auth.tx(user) as conn:
            items = await sessions.timeline(conn, client_id)
        return render(
            request,
            "terapiler/_timeline_row.html",
            {"items": items, "row_id": satir, "client_id": client_id},
        )

    @router.get("/zaman-kapat", response_class=HTMLResponse)
    async def timeline_close(
        request: Request, satir: str = "", user: CurrentUser = can_read
    ) -> Response:
        return render(request, "terapiler/_timeline_closed.html", {"row_id": satir})

    # ------------------------------------------------------------ form

    async def form_page(
        request: Request,
        user: CurrentUser,
        *,
        values: dict[str, Any],
        session_id: UUID | None = None,
        errors: dict[str, str] | None = None,
        status: int = 200,
        conflict: bool = False,
        fragment: bool = False,
    ) -> Response:
        async with auth.tx(user) as conn:
            catalog = await load_catalog(conn)
            client_list = await clients.search(conn, status="all" if session_id else "active")
        by_id = {str(c.id): c for c in client_list}
        section = method_section(catalog, by_id.get(str(values.get("client_id") or "")), values)
        context = {
            "values": values,
            "errors": errors or {},
            "section": section,
            "clients": client_list,
            "client": by_id.get(str(values.get("client_id") or "")),
            "session_id": session_id,
            "statuses": [(s.value, STATUS_LABELS[s.value]) for s in AppointmentStatus],
            "conflict": conflict,
        }
        name = "terapiler/_methods.html" if fragment else "terapiler/form.html"
        return render(request, name, context, status)

    @router.get("/yeni", response_class=HTMLResponse)
    async def new(request: Request, user: CurrentUser = can_write) -> Response:
        values: dict[str, Any] = {
            "client_id": request.query_params.get("client_id", ""),
            "session_date": default_session_time(),
            "duration_min": 50,
            "format_code": "individual",
            "methods": [],
            "primary": "",
            "status": "",
            "appointment_id": "",
        }
        errors: dict[str, str] = {}
        appointment_id = parse_uuid(request.query_params.get("randevu"))
        if appointment_id is not None:
            # Seansı kaydet from the calendar: start from the booked appointment.
            async with auth.tx(user) as conn:
                m = t.appointments_mirror
                appt = (
                    await conn.execute(
                        select(m).where(m.c.id == appointment_id, m.c.deleted_at.is_(None))
                    )
                ).one_or_none()
                everyone = await clients.search(conn, status="all") if appt else []
            if appt is None:
                errors["appointment_id"] = "Randevu bulunamadı."
            else:
                client = next((c for c in everyone if c.client_code == appt.client_code), None)
                values.update(
                    client_id=str(client.id) if client else "",
                    session_date=local_input(appt.starts_at),
                    duration_min=appt.duration_min,
                    appointment_id=str(appt.id),
                    status="planned" if appt.starts_at > now_utc() else "completed",
                )
                if client is None:
                    errors["client_id"] = (
                        f"{appt.client_code} kodlu danışan bu bilgisayarda kayıtlı değil."
                    )
        return await form_page(request, user, values=values, errors=errors)

    @router.get("/form/yontemler", response_class=HTMLResponse)
    async def methods_fragment(request: Request, user: CurrentUser = can_write) -> Response:
        """Re-render the age-group badge and method choices after client or date changes."""
        params = request.query_params
        values: dict[str, Any] = {
            "client_id": params.get("client_id", ""),
            "session_date": params.get("session_date", ""),
            "format_code": params.get("format_code", ""),
            "methods": params.getlist("method"),
            "primary": params.get("primary", ""),
        }
        session_id = None
        raw_id = params.get("session_id")
        if raw_id:
            try:
                session_id = UUID(raw_id)
            except ValueError:
                session_id = None
        return await form_page(request, user, values=values, session_id=session_id, fragment=True)

    def values_from_form(form: Any) -> dict[str, Any]:
        methods = selected_methods(form)
        return {
            "client_id": str(form.get("client_id") or ""),
            "session_date": str(form.get("session_date") or ""),
            "duration_min": str(form.get("duration_min") or ""),
            "format_code": str(form.get("format_code") or ""),
            "methods": [m["code"] for m in methods],
            "primary": next((m["code"] for m in methods if m["is_primary"]), ""),
            "status": str(form.get("status") or ""),
            "notes": form_text(form, "notes") or "",
            "homework": form_text(form, "homework") or "",
            "next_session_date": str(form.get("next_session_date") or ""),
            "version": str(form.get("version") or ""),
            "notes_sha": str(form.get("notes_sha") or ""),
            "homework_sha": str(form.get("homework_sha") or ""),
            "appointment_id": str(form.get("appointment_id") or ""),
        }

    def dates_or_errors(values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        parsed: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name in ("session_date", "next_session_date"):
            try:
                parsed[name] = parse_local(values.get(name))
            except ValueError:
                errors[name] = FIELD_ERRORS[name]
        if parsed.get("session_date") is None and "session_date" not in errors:
            errors["session_date"] = "Tarih ve saat zorunludur."
        return parsed, errors

    @router.post("", response_class=HTMLResponse)
    async def create(request: Request, user: CurrentUser = can_write) -> Response:
        form = await request.form()
        values = values_from_form(form)
        dates, errors = dates_or_errors(values)
        if errors:
            return await form_page(request, user, values=values, errors=errors, status=422)
        payload = {
            "client_id": values["client_id"] or None,
            "session_date": dates["session_date"],
            "duration_min": values["duration_min"] or 50,
            "format_code": values["format_code"],
            "methods": selected_methods(form),
            "status": values["status"] or None,
            "notes": form_text(form, "notes"),
            "homework": form_text(form, "homework"),
            "next_session_date": dates["next_session_date"],
            "appointment_id": parse_uuid(values["appointment_id"]),
        }
        try:
            data = SessionCreate.model_validate(payload)
            async with auth.tx(user) as conn:
                item = await sessions.create(conn, user, data)
        except ValidationError as exc:
            errors = validation_errors(exc)
        except SessionError as exc:
            errors = {exc.field: exc.message}
        if errors:
            return await form_page(request, user, values=values, errors=errors, status=422)
        return RedirectResponse(f"/terapiler/{item.id}?kaydedildi=1", status_code=303)

    # ------------------------------------------------------------ one session

    @router.get("/{session_id}", response_class=HTMLResponse)
    async def detail(request: Request, session_id: UUID, user: CurrentUser = can_read) -> Response:
        async with auth.tx(user) as conn:
            try:
                found = await sessions.get_detail(conn, user, session_id)  # audited
            except SessionNotFound:
                return not_found(request)
            timeline = await sessions.timeline(conn, found.item.client_id)
            client_name = None
            if user.has(Permission.CLIENTS_READ):
                try:
                    client_name = (await clients.get(conn, found.item.client_id)).full_name
                except ClientNotFound:
                    client_name = None
        return render(
            request,
            "terapiler/detail.html",
            {
                "s": found.item,
                "notes": found.notes,
                "homework": found.homework,
                "timeline": timeline,
                "client_name": client_name,
                "own": found.item.therapist_id == user.id
                and user.has(Permission.SESSIONS_WRITE_OWN),
                "saved": request.query_params.get("kaydedildi") == "1",
            },
        )

    @router.get("/{session_id}/duzenle", response_class=HTMLResponse)
    async def edit(request: Request, session_id: UUID, user: CurrentUser = can_write) -> Response:
        async with auth.tx(user) as conn:
            try:
                found = await sessions.get_detail(conn, user, session_id)  # audited
            except SessionNotFound:
                return not_found(request)
        s = found.item
        if s.therapist_id != user.id:
            return not_found(request)
        primary = s.primary_method
        values = {
            "client_id": str(s.client_id),
            "session_date": local_input(s.session_date),
            "duration_min": s.duration_min,
            "format_code": s.format_code,
            "methods": [m.code for m in s.methods],
            "primary": primary.code if primary else "",
            "status": s.status,
            "notes": found.notes or "",
            "homework": found.homework or "",
            "next_session_date": local_input(s.next_session_date),
            "version": s.version,
            # The save sends a note only if it changed, so an unchanged note is not rewritten.
            "notes_sha": digest(found.notes),
            "homework_sha": digest(found.homework),
        }
        return await form_page(request, user, values=values, session_id=session_id)

    @router.post("/{session_id}", response_class=HTMLResponse)
    async def update(request: Request, session_id: UUID, user: CurrentUser = can_write) -> Response:
        form = await request.form()
        values = values_from_form(form)
        dates, errors = dates_or_errors(values)
        if errors:
            return await form_page(
                request, user, values=values, session_id=session_id, errors=errors, status=422
            )
        payload: dict[str, Any] = {
            "session_date": dates["session_date"],
            "duration_min": values["duration_min"] or None,
            "format_code": values["format_code"] or None,
            "methods": selected_methods(form),
            "status": values["status"] or None,
            "next_session_date": dates["next_session_date"],
            "version": values["version"] or None,
        }
        for name in ("notes", "homework"):
            text_value = form_text(form, name)
            if digest(text_value) != values[f"{name}_sha"]:
                payload[name] = text_value
        conflict = False
        try:
            data = SessionUpdate.model_validate(payload)
            async with auth.tx(user) as conn:
                await sessions.update(conn, user, session_id, data)
        except ValidationError as exc:
            errors = validation_errors(exc)
        except SessionError as exc:
            errors = {exc.field: exc.message}
        except SessionNotFound:
            return not_found(request)
        except SessionVersionConflict:
            conflict = True
        if errors or conflict:
            return await form_page(
                request,
                user,
                values=values,
                session_id=session_id,
                errors=errors,
                status=409 if conflict else 422,
                conflict=conflict,
            )
        return RedirectResponse(f"/terapiler/{session_id}?kaydedildi=1", status_code=303)

    @router.post("/{session_id}/sil")
    async def remove(request: Request, session_id: UUID, user: CurrentUser = can_write) -> Response:
        try:
            async with auth.tx(user) as conn:
                await sessions.soft_delete(conn, user, session_id)
        except SessionNotFound:
            return not_found(request)
        return RedirectResponse("/terapiler?silindi=1", status_code=303)

    return router
