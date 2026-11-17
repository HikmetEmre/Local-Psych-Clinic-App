"""Klinik Kasa app factory. Run with: uvicorn klinik_local.main:create_app --factory"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cryptography.fernet import MultiFernet
from fastapi import Depends, FastAPI
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from klinik_local import tables as t
from klinik_local.anasayfa import build_router as build_home_router
from klinik_local.ayarlar.katalog import build_router as build_catalog_router
from klinik_local.ayarlar.odalar import build_router as build_rooms_router
from klinik_local.ayarlar.yedek import build_router as build_backup_router
from klinik_local.config import Settings, get_settings
from klinik_local.crypto.fernet import FieldCipher, parse_keys
from klinik_local.danisanlar.routes import build_router as build_clients_router
from klinik_local.danisanlar.service import ClientService
from klinik_local.gelir.routes import build_router as build_income_router
from klinik_local.gelir.service import IncomeService
from klinik_local.ozet_routes import build_router as build_snapshot_router
from klinik_local.randevular.routes import build_router as build_schedule_router
from klinik_local.randevular.service import AppointmentService
from klinik_local.terapiler.api import build_api_router as build_therapy_api
from klinik_local.terapiler.routes import build_router as build_therapy_pages
from klinik_local.terapiler.service import SessionService
from klinik_shared.authkit.admin import build_users_router
from klinik_shared.authkit.service import AuthPolicy, AuthStore, CurrentUser
from klinik_shared.authkit.web import AuthKit, AuthWebConfig
from klinik_shared.permissions import Permission
from klinik_shared.realtime import Hub, asyncpg_dsn
from klinik_shared.web import NavItem, Tier, create_base_app

TIER = Tier(
    service="klinik-local",
    app_name="Klinik",
    tagline="Randevular, seans kayıtları ve gelir. Bu veriler ofisten çıkmaz.",
    badge="Ofis",
)

TEMPLATES_DIR = Path(__file__).parent / "templates"

NAV = (
    NavItem("Ana sayfa", "/", None),
    NavItem("Takvim", "/takvim", Permission.SCHEDULE_READ_ALL.value),
    NavItem("Terapiler", "/terapiler", Permission.SESSIONS_READ_OWN.value),
    NavItem("Danışanlar", "/danisanlar", Permission.CLIENTS_READ.value),
    NavItem("Gelir", "/gelir", Permission.INCOME_READ_OWN.value),
    NavItem("Günlük özet", "/hesap/ozet", Permission.SESSIONS_READ_OWN.value),
    NavItem("Ayarlar", "/ayarlar", Permission.ADMIN_USERS.value),
)


class ConfigError(RuntimeError):
    pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.database_url:
        raise ConfigError("LOCAL_DATABASE_URL is required")
    try:
        cipher = FieldCipher(settings.fernet_keys)
    except ValueError as exc:
        raise ConfigError(f"LOCAL_FERNET_KEYS: {exc}") from exc
    try:
        auth_cipher = MultiFernet(parse_keys(settings.auth_fernet_key))
    except ValueError as exc:
        raise ConfigError(f"LOCAL_AUTH_FERNET_KEY: {exc}") from exc

    app = create_base_app(
        tier=TIER,
        database_url=settings.database_url,
        public_home=False,
        templates_dir=TEMPLATES_DIR,
        nav=NAV,
    )
    templates: Jinja2Templates = app.state.templates

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
            cookie_name="kasa_session",
            issuer="Nil Psikoloji Kasa",
            cookie_secure=settings.cookie_secure,
        ),
        templates,
    )
    auth.install(app)

    hub = Hub(asyncpg_dsn(settings.database_url), broadcast={"schedule_changed": "calendar"})
    app.state.hub = hub
    templates.env.globals["realtime"] = True
    streaming = Depends(auth.require(touch=False))

    @app.get("/events", include_in_schema=False)
    async def events(user: CurrentUser = streaming) -> Response:
        return hub.response(user.id)

    clients = ClientService(cipher)
    app.state.clients = clients
    appointments = AppointmentService(cipher)
    income = IncomeService(cipher)
    app.include_router(build_home_router(auth, appointments, income, cipher, templates))
    app.include_router(build_schedule_router(auth, appointments, clients, income, templates))
    app.include_router(build_income_router(auth, income, appointments, templates))
    app.include_router(build_rooms_router(auth, appointments, templates))

    sessions = SessionService(cipher)
    app.state.sessions = sessions
    app.include_router(build_therapy_api(auth, sessions))
    app.include_router(build_therapy_pages(auth, sessions, clients, templates))
    app.include_router(build_clients_router(auth, clients, templates))
    app.include_router(build_users_router(auth, templates))
    app.include_router(build_catalog_router(auth, templates))
    app.include_router(build_backup_router(auth, templates))
    app.include_router(build_snapshot_router(auth, cipher, templates))
    return app
