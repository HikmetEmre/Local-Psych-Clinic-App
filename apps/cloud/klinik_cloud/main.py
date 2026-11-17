"""Klinik Web app factory. Run with: uvicorn klinik_cloud.main:create_app --factory"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from klinik_cloud import sync_api
from klinik_cloud import tables as t
from klinik_cloud.ayarlar.odalar import build_router as build_rooms_router
from klinik_cloud.cakismalar.routes import build_router as build_conflicts_router
from klinik_cloud.cakismalar.routes import describe
from klinik_cloud.config import Settings, get_settings
from klinik_cloud.takvim.routes import build_router as build_calendar_router
from klinik_cloud.takvim.service import AppointmentService
from klinik_shared.authkit.admin import build_users_router
from klinik_shared.authkit.service import AuthPolicy, AuthStore
from klinik_shared.authkit.web import AuthKit, AuthWebConfig
from klinik_shared.permissions import Permission
from klinik_shared.web import NavItem, Tier, create_base_app

TIER = Tier(
    service="klinik-cloud",
    app_name="Klinik Web",
    tagline="Takvim, ödemeler ve bildirimler. Danışanlar yalnızca kod ile görünür.",
    badge="Bulut",
)

TEMPLATES_DIR = Path(__file__).parent / "templates"

NAV = (
    NavItem("Takvim", "/takvim", Permission.SCHEDULE_READ_ALL.value),
    NavItem("Çakışmalar", "/cakismalar", Permission.SCHEDULE_READ_ALL.value),
    NavItem("Ayarlar", "/ayarlar", Permission.ADMIN_USERS.value),
)


class ConfigError(RuntimeError):
    pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.database_url:
        # Without a database only /healthz and the static files are served.
        return create_base_app(tier=TIER, database_url=None)
    try:
        auth_cipher = MultiFernet([Fernet((settings.auth_fernet_key or "").encode())])
    except ValueError as exc:
        raise ConfigError("CLOUD_AUTH_FERNET_KEY is missing or invalid") from exc

    app = create_base_app(
        tier=TIER,
        database_url=settings.database_url,
        public_home=False,
        templates_dir=TEMPLATES_DIR,
        nav=NAV,
        search_path=t.SCHEMA,
    )
    templates: Jinja2Templates = app.state.templates
    templates.env.globals["describe"] = describe

    store = AuthStore(
        t.auth,
        t.audit_log,
        auth_cipher,
        AuthPolicy(
            idle_timeout=timedelta(minutes=settings.idle_timeout_minutes),
            max_lifetime=timedelta(hours=settings.session_max_hours),
        ),
    )
    auth = AuthKit(
        app.state.engine,
        store,
        AuthWebConfig(
            cookie_name="web_session",
            issuer="Nil Psikoloji Web",
            cookie_secure=settings.cookie_secure,
        ),
        templates,
    )
    auth.install(app)

    appointments = AppointmentService()
    app.state.appointments = appointments
    app.include_router(build_calendar_router(auth, appointments, templates))
    app.include_router(build_conflicts_router(auth, appointments, templates))
    app.include_router(build_users_router(auth, templates))
    app.include_router(build_rooms_router(auth, appointments, templates))
    sync_api.install(app, app.state.engine)
    return app
