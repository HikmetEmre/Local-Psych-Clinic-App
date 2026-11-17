"""Klinik notlar pages: /danisanlar/{client}/notlar."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from klinik_local.danisanlar.service import ClientNotFound, ClientService
from klinik_local.notlar.service import (
    FILTERS,
    KINDS,
    NoteError,
    NoteNotFound,
    NoteService,
    NoteVersionConflict,
)
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import ISTANBUL, now_utc, to_istanbul


def parse_local(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=ISTANBUL)
    except ValueError:
        raise NoteError("noted_at", "Tarih geçersiz.") from None


def local_input(value: datetime) -> str:
    return to_istanbul(value).strftime("%Y-%m-%dT%H:%M")


def parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except ValueError:
        return None


def build_router(
    auth: AuthKit, notes: NoteService, clients: ClientService, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter(prefix="/danisanlar/{client_id}/notlar", include_in_schema=False)
    can_read = Depends(auth.require(Permission.SESSIONS_READ_OWN))
    can_write = Depends(auth.require(Permission.SESSIONS_WRITE_OWN))

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    def not_found(request: Request, what: str = "Danışan") -> Response:
        return render(
            request,
            "error.html",
            {"title": f"{what} bulunamadı", "message": "Bu kayıt mevcut değil veya silinmiş."},
            404,
        )

    async def page(
        request: Request,
        user: CurrentUser,
        client_id: UUID,
        *,
        values: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        params = request.query_params
        kind = params.get("tur", "")
        kind = kind if kind in FILTERS else ""
        query = params.get("ara", "")[:100]
        important = params.get("onemli") == "1"
        async with auth.tx(user) as conn:
            try:
                client = await clients.get(conn, client_id)
            except ClientNotFound:
                return not_found(request)
            entries = await notes.timeline(
                conn, client_id, kind=kind, query=query, important_only=important
            )
            sessions = (
                await notes.own_sessions(conn, user, client_id)
                if user.has(Permission.SESSIONS_WRITE_OWN)
                else []
            )
            therapists = {th.id: th.display_name for th in await clients.therapists(conn)}
        return render(
            request,
            "notlar/timeline.html",
            {
                "client": client,
                "therapist_name": therapists.get(client.assigned_therapist_id, "—")
                if client.assigned_therapist_id
                else "—",
                "entries": entries,
                "kinds": KINDS,
                "filters": FILTERS,
                "kind": kind,
                "query": query,
                "important": important,
                "filtered": bool(kind or query or important),
                "sessions": sessions,
                "values": values or {"kind": "general", "noted_at": local_input(now_utc())},
                "errors": errors or {},
                "can_write": user.has(Permission.SESSIONS_WRITE_OWN),
                "sees_others": user.has(Permission.SESSIONS_READ_OTHERS),
                "notice": {
                    "kaydedildi": "Not kaydedildi.",
                    "guncellendi": "Not güncellendi. Önceki hali geçmişte saklandı.",
                    "silindi": "Not silindi. Metni geçmişte saklanır.",
                    "degismedi": "Değişiklik yok.",
                }.get(params.get("durum", "")),
                "local": lambda value: to_istanbul(value).strftime("%d.%m.%Y %H:%M"),
            },
            status,
        )

    @router.get("", response_class=HTMLResponse)
    async def timeline(request: Request, client_id: UUID, user: CurrentUser = can_read) -> Response:
        return await page(request, user, client_id)

    @router.post("", response_class=HTMLResponse)
    async def create(request: Request, client_id: UUID, user: CurrentUser = can_write) -> Response:
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "csrf_token"}
        try:
            async with auth.tx(user) as conn:
                note_id = await notes.create(
                    conn,
                    user,
                    client_id=client_id,
                    kind=values.get("kind", ""),
                    title=values.get("title", ""),
                    body=values.get("body", ""),
                    noted_at=parse_local(values.get("noted_at")),
                    important=values.get("important") == "1",
                    session_id=parse_uuid(values.get("session_id")),
                )
        except NoteError as exc:
            return await page(
                request, user, client_id, values=values, errors={exc.field: exc.message}, status=422
            )
        return RedirectResponse(
            f"/danisanlar/{client_id}/notlar?durum=kaydedildi#not-{note_id}", status_code=303
        )

    async def edit_page(
        request: Request,
        user: CurrentUser,
        client_id: UUID,
        note_id: UUID,
        *,
        values: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        conflict: bool = False,
        status: int = 200,
    ) -> Response:
        async with auth.tx(user) as conn:
            try:
                client = await clients.get(conn, client_id)
                row = await notes.get(conn, note_id)
            except (ClientNotFound, NoteNotFound):
                return not_found(request, "Not")
        if row.author_id != user.id or row.client_id != client_id:
            return not_found(request, "Not")
        current = {
            "kind": row.kind,
            "title": notes.cipher.decrypt_optional(row.title_encrypted) or "",
            "body": notes.cipher.decrypt(row.body_encrypted),
            "noted_at": local_input(row.noted_at),
            "important": "1" if row.important else "",
            "version": str(row.version),
        }
        return render(
            request,
            "notlar/edit.html",
            {
                "client": client,
                "note_id": note_id,
                "values": current if values is None or conflict else values,
                "errors": errors or {},
                "kinds": KINDS,
                "conflict": conflict,
            },
            status,
        )

    @router.get("/{note_id}/duzenle", response_class=HTMLResponse)
    async def edit(
        request: Request, client_id: UUID, note_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        return await edit_page(request, user, client_id, note_id)

    @router.post("/{note_id}", response_class=HTMLResponse)
    async def save(
        request: Request, client_id: UUID, note_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        form = await request.form()
        values = {k: str(v) for k, v in form.items() if k != "csrf_token"}
        try:
            version = int(values.get("version", ""))
        except ValueError:
            version = -1
        try:
            async with auth.tx(user) as conn:
                changed = await notes.update(
                    conn,
                    user,
                    note_id,
                    kind=values.get("kind", ""),
                    title=values.get("title", ""),
                    body=values.get("body", ""),
                    noted_at=parse_local(values.get("noted_at")),
                    important=values.get("important") == "1",
                    expected_version=version,
                )
        except NoteNotFound:
            return not_found(request, "Not")
        except NoteVersionConflict:
            return await edit_page(request, user, client_id, note_id, conflict=True, status=409)
        except NoteError as exc:
            return await edit_page(
                request, user, client_id, note_id, values=values,
                errors={exc.field: exc.message}, status=422,
            )  # fmt: skip
        outcome = "guncellendi" if changed else "degismedi"
        return RedirectResponse(
            f"/danisanlar/{client_id}/notlar?durum={outcome}#not-{note_id}", status_code=303
        )

    @router.post("/{note_id}/sil")
    async def delete(
        request: Request, client_id: UUID, note_id: UUID, user: CurrentUser = can_write
    ) -> Response:
        try:
            async with auth.tx(user) as conn:
                await notes.delete(conn, user, note_id)
        except NoteNotFound:
            return not_found(request, "Not")
        return RedirectResponse(
            f"/danisanlar/{client_id}/notlar?{urlencode({'durum': 'silindi'})}", status_code=303
        )

    return router
