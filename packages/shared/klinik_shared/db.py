"""Async engine factory and a liveness ping, shared by all three services."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DbStatus = Literal["ok", "down", "not_configured"]


def make_engine(database_url: str | None) -> AsyncEngine | None:
    if not database_url:
        return None
    return create_async_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=5)


async def db_status(engine: AsyncEngine | None) -> DbStatus:
    if engine is None:
        return "not_configured"
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return "down"
    return "ok"
