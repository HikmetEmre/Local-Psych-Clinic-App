"""App factory pieces common to both web apps and the sync worker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

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

from klinik_shared.db import db_status, make_engine
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.timeutil import format_datetime_tr

UI_DIR = Path(__file__).parent / "ui"

CLINIC_NAME = "Nil Psikoloji"


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str


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
    return Jinja2Templates(env=env)


def create_base_app(
    *,
    tier: Tier,
    database_url: str | None,
    with_ui: bool = True,
    templates_dir: Path | None = None,
    nav: tuple[NavItem, ...] = (),
) -> FastAPI:
    engine = make_engine(database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(
        title=f"{CLINIC_NAME} · {tier.app_name}",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.state.tier = tier

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

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def home(request: Request) -> HTMLResponse:
            return templates.TemplateResponse(request, "home.html", {"db": await db_status(engine)})

    return app
