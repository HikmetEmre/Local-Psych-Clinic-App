"""Bildirimler: inbox, settings, Web Push subscriptions, live events and the PWA files."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from klinik_cloud import tables as t
from klinik_cloud.bildirimler import service
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.realtime import Hub
from klinik_shared.web import CLINIC_NAME, UI_DIR

STATIC_JS = UI_DIR / "static" / "js"
PUSH_HOSTS_HINT = "https://"


class _Keys(BaseModel):
    model_config = ConfigDict(extra="ignore")
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    endpoint: str = Field(min_length=12, max_length=1000, pattern=r"^https://")
    keys: _Keys


class EndpointIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=1000)


def _parse_time(value: object, default: time) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError:
        return default


def build_router(
    auth: AuthKit,
    templates: Jinja2Templates,
    hub: Hub,
    vapid_public_key: str | None,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    logged_in = Depends(auth.require())
    # The event stream must not keep an idle session alive (30-minute timeout).
    streaming = Depends(auth.require(touch=False))

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------ live events

    @router.get("/events")
    async def events(user: CurrentUser = streaming) -> Response:
        return hub.response(user.id)

    # ------------------------------------------------------------ inbox

    @router.get("/bildirimler", response_class=HTMLResponse)
    async def inbox(request: Request, user: CurrentUser = logged_in) -> Response:
        async with auth.tx(user) as conn:
            items = await service.inbox(conn, user.id)
            settings = await service.settings_for(conn, user.id)
            devices = len(
                (
                    await conn.execute(
                        select(t.push_subscriptions.c.id).where(
                            t.push_subscriptions.c.user_id == user.id
                        )
                    )
                ).all()
            )
        return render(
            request,
            "bildirimler/inbox.html",
            {
                "items": items,
                "settings": settings,
                "devices": devices,
                "vapid_public_key": vapid_public_key,
                "saved": request.query_params.get("kaydedildi") == "1",
            },
        )

    @router.get("/bildirimler/sayac", response_class=HTMLResponse)
    async def counter(request: Request, user: CurrentUser = streaming) -> Response:
        async with auth.tx(user) as conn:
            count = await service.unread_count(conn, user.id)
        return render(request, "bildirimler/_bell.html", {"count": count})

    @router.post("/bildirimler/{notification_id}/ac")
    async def open_one(notification_id: UUID, user: CurrentUser = logged_in) -> Response:
        async with auth.tx(user) as conn:
            row = (
                await conn.execute(
                    select(t.notifications).where(
                        t.notifications.c.id == notification_id,
                        t.notifications.c.recipient_id == user.id,
                    )
                )
            ).one_or_none()
            if row is None:
                return RedirectResponse("/bildirimler", status_code=303)
            await service.mark_read(conn, user.id, notification_id)
        return RedirectResponse(service.render(row.event_type, row.payload).url, status_code=303)

    @router.post("/bildirimler/tumu-okundu")
    async def read_all(user: CurrentUser = logged_in) -> Response:
        async with auth.tx(user) as conn:
            await service.mark_read(conn, user.id)
        return RedirectResponse("/bildirimler", status_code=303)

    @router.post("/bildirimler/ayarlar")
    async def save_settings(request: Request, user: CurrentUser = logged_in) -> Response:
        form = await request.form()
        settings = service.Settings(
            quiet_start=_parse_time(form.get("quiet_start"), service.DEFAULT_QUIET_START),
            quiet_end=_parse_time(form.get("quiet_end"), service.DEFAULT_QUIET_END),
            push_enabled=form.get("push_enabled") == "1",
            reminder_24h=form.get("reminder_24h") == "1",
            reminder_1h=form.get("reminder_1h") == "1",
        )
        async with auth.tx(user) as conn:
            await service.save_settings(conn, user.id, settings)
        return RedirectResponse("/bildirimler?kaydedildi=1", status_code=303)

    # ------------------------------------------------------------ push subscriptions (JSON)

    @router.post("/push/abonelik")
    async def subscribe(request: Request, user: CurrentUser = logged_in) -> Response:
        try:
            data = SubscriptionIn.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"detail": "invalid_subscription"}, status_code=422)
        async with auth.tx(user) as conn:
            await service.add_subscription(
                conn, user.id, data.endpoint, data.keys.p256dh, data.keys.auth
            )
        return JSONResponse({"ok": True}, status_code=201)

    @router.post("/push/abonelik/sil")
    async def unsubscribe(request: Request, user: CurrentUser = logged_in) -> Response:
        try:
            data = EndpointIn.model_validate(await request.json())
        except (ValidationError, ValueError):
            return JSONResponse({"detail": "invalid"}, status_code=422)
        async with auth.tx(user) as conn:
            await service.remove_subscription(conn, user.id, data.endpoint)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------ PWA

    @router.get("/manifest.webmanifest")
    async def manifest() -> Response:
        return JSONResponse(
            {
                "name": f"{CLINIC_NAME} · Klinik Web",
                "short_name": "Nil Klinik",
                "lang": "tr",
                "start_url": "/takvim",
                "scope": "/",
                "display": "standalone",
                "background_color": "#fbf8f2",
                "theme_color": "#356650",
                "icons": [
                    {"src": "/static/ui/img/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/ui/img/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ],
            },
            media_type="application/manifest+json",
        )

    @router.get("/sw.js")
    async def service_worker() -> Response:
        return FileResponse(
            Path(STATIC_JS / "sw.js"),
            media_type="text/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @router.get("/offline", response_class=HTMLResponse)
    async def offline(request: Request) -> Response:
        return templates.TemplateResponse(request, "offline.html", {})

    return router
