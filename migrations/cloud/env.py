"""Alembic environment for the CLOUD tier (Klinik Web).

Every cloud table lives in the private schema ``app`` (never ``public``), so that
Supabase's auto-generated Data API cannot expose it. The version table lives there too.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

SCHEMA = "app"
URL_ENV_VARS = ("CLOUD_MIGRATION_DATABASE_URL", "CLOUD_DATABASE_URL")


def _database_url() -> str:
    for name in URL_ENV_VARS:
        if url := os.environ.get(name):
            return url
    raise RuntimeError(f"set one of {', '.join(URL_ENV_VARS)} to run cloud migrations")


def _run(connection: Connection) -> None:
    # Unqualified names land in "app". Extensions live in "public" (plain Postgres) or
    # "extensions" (Supabase); a schema that does not exist is ignored.
    connection.execute(text(f'SET search_path TO "{SCHEMA}", public, extensions'))
    context.configure(
        connection=connection,
        target_metadata=None,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
            await connection.commit()
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; run against a database")

asyncio.run(_run_async())
