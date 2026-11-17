from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SYNC_", extra="ignore")

    # Connection as kasa_sync: outbox, mirror and sync tables only.
    local_database_url: str | None = None
    cloud_base_url: str | None = None
    cloud_token: str | None = None
    pull_wait_seconds: int = 25
    push_interval_seconds: float = 2.0

    @property
    def can_run(self) -> bool:
        return bool(self.local_database_url and self.cloud_base_url and self.cloud_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
