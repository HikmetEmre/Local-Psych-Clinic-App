"""Integration fixtures: a real PostgreSQL 16 in Docker.

Locally the tests skip when Docker is not running. In CI (``CI`` set) a missing
Docker is a failure, so these tests can never be skipped silently there.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

POSTGRES_IMAGE = "postgres:16-alpine"


def _start_postgres() -> object:
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(
        POSTGRES_IMAGE, username="test", password="test", dbname="test", driver=None
    )
    container.start()
    return container


@pytest.fixture(scope="session")
def postgres_server() -> Iterator[str]:
    """Base asyncpg URL (no database name) of a throwaway server."""
    try:
        container = _start_postgres()
    except Exception as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Docker is not available: {exc}")
    try:
        host = container.get_container_host_ip()  # type: ignore[attr-defined]
        port = container.get_exposed_port(5432)  # type: ignore[attr-defined]
        yield f"postgresql+asyncpg://test:test@{host}:{port}"
    finally:
        container.stop()  # type: ignore[attr-defined]


@pytest.fixture
def fresh_database(postgres_server: str) -> Iterator[str]:
    """URL of a newly created, empty database (dropped afterwards)."""
    import asyncio
    import uuid

    import asyncpg

    name = f"t_{uuid.uuid4().hex[:12]}"
    admin_dsn = postgres_server.replace("+asyncpg", "") + "/test"

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(run(f'CREATE DATABASE "{name}"'))
    try:
        yield f"{postgres_server}/{name}"
    finally:
        asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
