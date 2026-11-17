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

    # "office" (the main computer) or "replica" (the owner's laptop copy).
    mode: str = "office"
    # Connection of the sync role (kasa_sync): applies rows from the other side.
    sync_database_url: str | None = None
    # Office: how the laptop reaches this computer, e.g. https://OFIS-PC:8443 (pairing).
    public_url: str | None = None
    # Office: this computer's HTTPS certificate (goes into the pairing file).
    tls_cert: str | None = None
    # Laptop: where the office is, its certificate (pinned) and the replica token.
    office_url: str | None = None
    office_cert: str | None = None
    replica_token: str | None = None
    replica_interval_seconds: int = 60

    @property
    def is_replica(self) -> bool:
        return self.mode == "replica"

    cookie_secure: bool = True
    idle_timeout_minutes: int = 30
    session_max_hours: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
