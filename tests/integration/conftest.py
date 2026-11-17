"""Integration fixtures: a real PostgreSQL 16 in Docker.

Locally the tests skip when Docker is not running. In CI (``CI`` set) a missing
Docker is a failure, so these tests can never be skipped silently there.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

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
def database_factory(postgres_server: str) -> Iterator[Callable[[], str]]:
    """Creates empty databases on demand; all of them are dropped after the test."""
    import asyncio
    import uuid

    import asyncpg

    admin_dsn = postgres_server.replace("+asyncpg", "") + "/test"
    created: list[str] = []

    async def run(sql: str) -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    def create() -> str:
        name = f"t_{uuid.uuid4().hex[:12]}"
        asyncio.run(run(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return f"{postgres_server}/{name}"

    yield create
    for name in created:
        asyncio.run(run(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def fresh_database(database_factory: Callable[[], str]) -> str:
    """URL of a newly created, empty database (dropped afterwards)."""
    return database_factory()


@pytest.fixture(scope="session", autouse=True)
def _fast_bcrypt() -> None:
    """Cost factor 4 instead of 12: these tests hash dozens of passwords."""
    from klinik_shared.authkit import passwords

    passwords.set_rounds(4)


@pytest.fixture
def local_db(fresh_database: str) -> object:
    """A migrated local-tier database with the app and sync roles ready to log in."""
    from support.local import prepare_local_db

    return prepare_local_db(fresh_database)
