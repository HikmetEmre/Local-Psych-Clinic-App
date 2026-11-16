"""Alembic environment for the LOCAL tier (Klinik Kasa, office laptop)."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

URL_ENV_VARS = ("LOCAL_MIGRATION_DATABASE_URL", "LOCAL_DATABASE_URL")


def _database_url() -> str:
    for name in URL_ENV_VARS:
        if url := os.environ.get(name):
            return url
    raise RuntimeError(f"set one of {', '.join(URL_ENV_VARS)} to run local migrations")


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported; run against a database")

asyncio.run(_run_async())
