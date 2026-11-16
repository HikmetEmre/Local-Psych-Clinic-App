from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the environment with the ``LOCAL_`` prefix (see ``.env.example``)."""

    model_config = SettingsConfigDict(env_prefix="LOCAL_", extra="ignore")

    # Connection of the app role (kasa_app), which is subject to row-level security.
    database_url: str | None = None
    # Fernet keys for client contact details and clinical text: "primary[,older,...]".
    fernet_keys: str | None = None
    # Fernet key protecting TOTP secrets at rest.
    auth_fernet_key: str | None = None

    cookie_secure: bool = True
    idle_timeout_minutes: int = 30
    session_max_hours: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
