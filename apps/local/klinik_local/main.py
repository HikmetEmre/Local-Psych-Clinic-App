"""Klinik Kasa app factory. Run with: uvicorn klinik_local.main:create_app --factory"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from cryptography.fernet import MultiFernet
from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from klinik_local import tables as t
from klinik_local.anasayfa import build_router as build_home_router
from klinik_local.ayarlar.katalog import build_router as build_catalog_router
from klinik_local.ayarlar.odalar import build_router as build_rooms_router
from klinik_local.ayarlar.yedek import build_router as build_backup_router
from klinik_local.config import Settings, get_settings
from klinik_local.crypto.fernet import FieldCipher, parse_keys
from klinik_local.danisanlar.routes import build_router as build_clients_router
from klinik_local.danisanlar.service import REPLICA_CODE_FLOOR, ClientService
from klinik_local.gelir.routes import build_router as build_income_router
from klinik_local.gelir.service import IncomeService
from klinik_local.ozet_routes import build_router as build_snapshot_router
from klinik_local.randevular.routes import build_router as build_schedule_router
from klinik_local.randevular.service import AppointmentService
from klinik_local.raporlar.routes import build_router as build_reports_router
from klinik_local.replika.laptop import ReplicaSync
from klinik_local.replika.office import build_api_router as build_replica_api
from klinik_local.replika.routes import build_laptop_router
from klinik_local.replika.routes import build_office_router as build_replica_pages
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
    NavItem("Raporlar", "/raporlar", Permission.SESSIONS_READ_OWN.value),
    NavItem("Günlük özet", "/hesap/ozet", Permission.SESSIONS_READ_OWN.value),
    NavItem("Ayarlar", "/ayarlar", Permission.ADMIN_USERS.value),
)
# The laptop copy: no daily snapshot (made at the office), settings = sync status.
REPLICA_NAV = tuple(item for item in NAV if item.href != "/hesap/ozet")
REPLICA_TIER = Tier(
    service="klinik-local",
    app_name="Klinik",
    tagline="Dizüstü kopya: ofis ağına bağlanınca ofisle eşitlenir.",
    badge="Dizüstü",
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

    replica = settings.is_replica
    if replica and not (settings.office_url and settings.replica_token):
        raise ConfigError("LOCAL_OFFICE_URL and LOCAL_REPLICA_TOKEN are required on the laptop")
    app = create_base_app(
        tier=REPLICA_TIER if replica else TIER,
        database_url=settings.database_url,
        public_home=False,
        templates_dir=TEMPLATES_DIR,
        nav=REPLICA_NAV if replica else NAV,
    )
    templates: Jinja2Templates = app.state.templates
    templates.env.globals["replica_mode"] = replica
    sync_engine = (
        # A connection per sync round is plenty (one laptop, once a minute).
        create_async_engine(settings.sync_database_url, poolclass=NullPool)
        if settings.sync_database_url
        else None
    )
    if sync_engine is not None:
        app.state.extra_engines.append(sync_engine)
    if replica:
        if sync_engine is None:
            raise ConfigError("LOCAL_SYNC_DATABASE_URL is required on the laptop")
        install_password_notice(app, templates)

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

    clients = ClientService(cipher, code_floor=REPLICA_CODE_FLOOR if replica else 0)
    app.state.clients = clients
    appointments = AppointmentService(cipher)
    income = IncomeService(cipher)
    app.include_router(build_home_router(auth, appointments, income, cipher, templates))
    app.include_router(build_schedule_router(auth, appointments, clients, income, templates))
    app.include_router(build_income_router(auth, income, appointments, templates))
    app.include_router(build_reports_router(auth, cipher, clients, appointments, templates))

    sessions = SessionService(cipher)
    app.state.sessions = sessions
    app.include_router(build_therapy_api(auth, sessions))
    app.include_router(build_therapy_pages(auth, sessions, clients, templates))
    app.include_router(build_clients_router(auth, clients, templates))
    if replica:
        if sync_engine is None or not settings.office_url or not settings.replica_token:
            raise ConfigError("laptop sync settings are incomplete")
        cert = (
            Path(settings.office_cert).read_text(encoding="ascii") if settings.office_cert else None
        )
        sync = ReplicaSync(sync_engine, settings.office_url, settings.replica_token, cert)
        app.state.replica = sync
        templates.env.globals["replica_status"] = lambda: sync.status
        app.state.background.append(lambda: sync.start(settings.replica_interval_seconds))
        app.include_router(build_laptop_router(auth, sync, templates))
        return app

    # Office only: administration, backups, snapshot, and the laptop's sync API.
    app.include_router(build_users_router(auth, templates))
    app.include_router(build_rooms_router(auth, appointments, templates))
    app.include_router(build_catalog_router(auth, templates))
    app.include_router(build_backup_router(auth, templates))
    app.include_router(build_snapshot_router(auth, cipher, templates))
    if sync_engine is not None:
        app.include_router(build_replica_api(sync_engine))
        app.include_router(build_replica_pages(auth, settings, sync_engine, cipher, templates))
    return app


def install_password_notice(app: FastAPI, templates: Jinja2Templates) -> None:
    """On the laptop, passwords are managed at the office (they arrive by sync)."""

    @app.api_route("/hesap/sifre", methods=["GET", "POST"], include_in_schema=False)
    async def password_elsewhere(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Şifre ofiste değiştirilir",
                "message": (
                    "Bu dizüstü kopyada şifre ve iki adımlı doğrulama değiştirilemez. "
                    "Ofis bilgisayarında değiştirin; bir sonraki eşitlemede buraya da gelir."
                ),
            },
            status_code=409,
        )
