from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobSettings(BaseSettings):
    """Read from the environment / the installation's .env file."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    # kasa_backup: reads everything (bypasses row-level security), writes backup_status.
    backup_database_url: str = Field(alias="LOCAL_BACKUP_DATABASE_URL")
    fernet_keys: str | None = Field(default=None, alias="LOCAL_FERNET_KEYS")

    pg_bin: Path | None = Field(default=None, alias="LOCAL_PG_BIN")
    backup_dir: Path = Field(default=Path("backups"), alias="LOCAL_BACKUP_DIR")
    snapshot_dir: Path = Field(default=Path("ozet"), alias="LOCAL_SNAPSHOT_DIR")

    # Public key only. The private key that opens backups is kept offline.
    age_recipient: str = Field(alias="BACKUP_AGE_RECIPIENT")
    rclone_exe: str = Field(default="rclone", alias="RCLONE_EXE")
    rclone_config: Path | None = Field(default=None, alias="RCLONE_CONFIG")
    # e.g. "klinik-drive:Nil Psikoloji"; backups go to <remote>/yedek, snapshots to <remote>/ozet
    rclone_remote: str | None = Field(default=None, alias="BACKUP_RCLONE_REMOTE")
    cloud_dump_url: str | None = Field(default=None, alias="BACKUP_CLOUD_DUMP_URL")

    snapshot_hour: int = Field(default=17, alias="SNAPSHOT_HOUR")

    def tool(self, name: str) -> str:
        """Full path of a PostgreSQL client tool (pg_dump, pg_restore, initdb, ...)."""
        if self.pg_bin is not None:
            for candidate in (self.pg_bin / f"{name}.exe", self.pg_bin / name):
                if candidate.exists():
                    return str(candidate)
        found = shutil.which(name)
        if found is None:
            raise FileNotFoundError(f"{name} not found (set LOCAL_PG_BIN)")
        return found
