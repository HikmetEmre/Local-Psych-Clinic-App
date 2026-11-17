"""Both migration chains go up, down and up again on a real PostgreSQL."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


def alembic_config(chain: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"), ini_section=chain)
    config.set_main_option("script_location", str(ROOT / "migrations" / chain))
    return config


def query(url: str, sql: str) -> list[Any]:
    async def run() -> list[Any]:
        conn = await asyncpg.connect(url.replace("+asyncpg", ""))
        try:
            return [row[0] for row in await conn.fetch(sql)]
        finally:
            await conn.close()

    return asyncio.run(run())


VERSION_TABLE_SCHEMA = (
    "SELECT table_schema FROM information_schema.tables WHERE table_name = 'alembic_version'"
)
PUBLIC_TABLES = "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"


@pytest.mark.parametrize(
    ("chain", "env_var", "version_schema"),
    [("local", "LOCAL_MIGRATION_DATABASE_URL", "public")],
)
def test_chain_up_down_up(
    chain: str,
    env_var: str,
    version_schema: str,
    fresh_database: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(env_var, fresh_database)
    config = alembic_config(chain)

    command.upgrade(config, "head")
    assert query(fresh_database, VERSION_TABLE_SCHEMA) == [version_schema]
    assert query(fresh_database, f"SELECT version_num FROM {version_schema}.alembic_version")
    assert "btree_gist" in query(fresh_database, "SELECT extname FROM pg_extension")

    command.downgrade(config, "base")
    assert query(fresh_database, f"SELECT version_num FROM {version_schema}.alembic_version") == []

    command.upgrade(config, "head")
    assert query(fresh_database, f"SELECT version_num FROM {version_schema}.alembic_version")


def test_migrations_refuse_to_run_without_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LOCAL_MIGRATION_DATABASE_URL", "LOCAL_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="LOCAL_MIGRATION_DATABASE_URL"):
        command.upgrade(alembic_config("local"), "head")
