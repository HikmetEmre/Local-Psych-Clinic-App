from __future__ import annotations

from fastapi import FastAPI

from klinik_cloud.config import Settings, get_settings
from klinik_shared.web import Tier, create_base_app

TIER = Tier(
    service="klinik-cloud",
    app_name="Klinik Web",
    tagline="Takvim, ödemeler ve bildirimler. Danışanlar yalnızca kod ile görünür.",
    badge="Bulut",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    return create_base_app(tier=TIER, database_url=settings.database_url)


app = create_app()
