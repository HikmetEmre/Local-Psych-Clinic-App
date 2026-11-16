from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SYNC_", extra="ignore")

    local_database_url: str | None = None
    cloud_base_url: str | None = None
    cloud_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
