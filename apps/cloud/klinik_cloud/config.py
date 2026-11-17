from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read from the environment with the ``CLOUD_`` prefix."""

    model_config = SettingsConfigDict(env_prefix="CLOUD_", extra="ignore")

    # Connection of the app role (cloud_app). Use Supabase's session-mode connection.
    database_url: str | None = None
    # Fernet key protecting TOTP secrets at rest.
    auth_fernet_key: str | None = None

    # Web Push (VAPID). Generate with: python -m klinik_cloud.manage generate-keys
    vapid_private_key: str | None = None
    vapid_public_key: str | None = None
    # Contact the push services may use (mailto: or https:). Required for push.
    vapid_subject: str | None = None
    # Push delivery and reminders run inside the web process (Cloud Run: min 1 instance,
    # CPU always allocated). Tests switch them off.
    run_workers: bool = True

    cookie_secure: bool = True
    idle_timeout_minutes: int = 30
    session_max_hours: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
