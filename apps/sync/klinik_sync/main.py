from __future__ import annotations

from fastapi import FastAPI

from klinik_shared.web import Tier, create_base_app
from klinik_sync.config import Settings, get_settings

TIER = Tier(
    service="klinik-sync",
    app_name="Eşitleme",
    tagline="Ofis ile bulut arasında eşitleme.",
    badge="Sync",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # No UI: this process only exposes /healthz. Pull/push loops arrive in M5.
    return create_base_app(tier=TIER, database_url=settings.local_database_url, with_ui=False)


app = create_app()
