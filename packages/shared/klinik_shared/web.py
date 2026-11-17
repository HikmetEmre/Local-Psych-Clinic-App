"""App factory pieces common to both web apps and the sync worker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import (
    BaseLoader,
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    select_autoescape,
)
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from klinik_shared.db import db_status, make_engine
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.timeutil import format_datetime_tr

UI_DIR = Path(__file__).parent / "ui"

CLINIC_NAME = "Nil Psikoloji"


# No inline scripts or styles anywhere: everything is served from our own origin.
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so that streaming responses are untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    permission: str | None = None  # shown only to users holding it


@dataclass(frozen=True)
class Tier:
    """How an app presents itself in the shared layout."""

    service: str  # machine name, shown in /healthz
    app_name: str  # "Klinik Web" / "Klinik Kasa"
    tagline: str
    badge: str  # short tier marker in the header


def build_templates(app_templates_dir: Path | None = None) -> Jinja2Templates:
    """Templates: the app's own directory first, then the shared layout."""
    loaders: list[BaseLoader] = []
    if app_templates_dir is not None:
        loaders.append(FileSystemLoader(app_templates_dir))
    loaders.append(PackageLoader("klinik_shared", "ui/templates"))
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["datetime_tr"] = format_datetime_tr
    env.globals["clinic_name"] = CLINIC_NAME
    env.globals["status_labels"] = STATUS_LABELS
    return Jinja2Templates(env=env, context_processors=[_request_context])


def _request_context(request: Request) -> dict[str, Any]:
    """Every template sees the logged-in user (or None) and the CSRF token for forms."""
    return {
        "user": getattr(request.state, "user", None),
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }


def create_base_app(
    *,
    tier: Tier,
    database_url: str | None,
    with_ui: bool = True,
    public_home: bool = True,
    templates_dir: Path | None = None,
    nav: tuple[NavItem, ...] = (),
    search_path: str | None = None,
) -> FastAPI:
    engine = make_engine(database_url, search_path=search_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Long-running jobs registered by the app (push delivery, reminders, ...).
        tasks = [asyncio.create_task(job()) for job in app.state.background]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            hub = getattr(app.state, "hub", None)
            if hub is not None:
                await hub.stop()
            if engine is not None:
                await engine.dispose()
            for extra in app.state.extra_engines:
                await extra.dispose()

    app = FastAPI(
        title=f"{CLINIC_NAME} · {tier.app_name}",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.state.background = []
    app.state.extra_engines = []  # further engines the app opened (disposed on shutdown)
    app.state.tier = tier
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        db = await db_status(engine)
        healthy = db != "down"
        return JSONResponse(
            {"status": "ok" if healthy else "degraded", "service": tier.service, "db": db},
            status_code=200 if healthy else 503,
        )

    if with_ui:
        templates = build_templates(templates_dir)
        templates.env.globals["tier"] = tier
        templates.env.globals["nav"] = nav
        app.state.templates = templates
        app.mount("/static/ui", StaticFiles(directory=UI_DIR / "static"), name="ui-static")

        if public_home:
            # Placeholder until the app has its own authenticated home page.
            @app.get("/", response_class=HTMLResponse, include_in_schema=False)
            async def home(request: Request) -> HTMLResponse:
                return templates.TemplateResponse(
                    request, "home.html", {"db": await db_status(engine)}
                )

    return app
