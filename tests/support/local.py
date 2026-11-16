"""Helpers for integration tests of the local tier (Klinik Kasa)."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable, Coroutine, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import asyncpg
import httpx
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet, MultiFernet
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from klinik_local import manage
from klinik_local import tables as t
from klinik_local.config import Settings
from klinik_local.main import create_app
from klinik_shared.authkit import totp
from klinik_shared.authkit.service import AuthStore
from klinik_shared.permissions import Permission, expand_sets
from klinik_shared.timeutil import now_utc

ROOT = Path(__file__).resolve().parents[2]

FERNET_KEY = Fernet.generate_key().decode()
AUTH_KEY = Fernet.generate_key().decode()
ROLE_PASSWORD = "test_role_pw"

A_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a01")
B_ID = UUID("0b9c6f0e-5d1a-4f6b-9a53-1f2f1c7a1a02")
PASSWORD = "dogru-at-pil-zimba-42"


@dataclass(frozen=True)
class LocalDb:
    owner_url: str  # superuser: migrations and test assertions
    app_url: str  # kasa_app: what the web app uses (RLS applies)
    sync_url: str  # kasa_sync: what the sync worker uses

    def dsn(self, url: str | None = None) -> str:
        return (url or self.owner_url).replace("+asyncpg", "")


def alembic_config(chain: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"), ini_section=chain)
    config.set_main_option("script_location", str(ROOT / "migrations" / chain))
    return config


@contextmanager
def env(**values: str) -> Any:
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def with_engine[T](url: str, fn: Callable[[AsyncEngine], Coroutine[Any, Any, T]]) -> T:
    async def go() -> T:
        engine = create_async_engine(url)
        try:
            return await fn(engine)
        finally:
            await engine.dispose()

    return run(go())


def prepare_local_db(database_url: str) -> LocalDb:
    """Migrate a fresh database and give the application roles a password."""
    with env(LOCAL_MIGRATION_DATABASE_URL=database_url):
        command.upgrade(alembic_config("local"), "head")
    with_engine(
        database_url,
        lambda e: manage.set_role_passwords(
            e, {"kasa_app": ROLE_PASSWORD, "kasa_sync": ROLE_PASSWORD}
        ),
    )

    def as_role(role: str) -> str:
        return re.sub(r"//[^@]+@", f"//{role}:{ROLE_PASSWORD}@", database_url)

    return LocalDb(database_url, as_role("kasa_app"), as_role("kasa_sync"))


# ------------------------------------------------------------------ direct SQL


def fetch(db: LocalDb, sql: str, *args: Any, url: str | None = None) -> list[asyncpg.Record]:
    async def go() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(db.dsn(url))
        try:
            return list(await conn.fetch(sql, *args))
        finally:
            await conn.close()

    return run(go())


def execute(db: LocalDb, sql: str, *args: Any, url: str | None = None) -> str:
    async def go() -> str:
        conn = await asyncpg.connect(db.dsn(url))
        try:
            return str(await conn.execute(sql, *args))
        finally:
            await conn.close()

    return run(go())


def scalar(db: LocalDb, sql: str, *args: Any, url: str | None = None) -> Any:
    rows = fetch(db, sql, *args, url=url)
    return rows[0][0] if rows else None


# ------------------------------------------------------------------ users


def auth_store() -> AuthStore:
    return AuthStore(t.auth, t.audit_log, MultiFernet([Fernet(AUTH_KEY.encode())]))


def add_user(
    db: LocalDb,
    user_id: UUID,
    username: str,
    display_name: str,
    permissions: Iterable[Permission | str],
) -> str:
    """Create a user as the bootstrap command would. Returns the temporary password."""
    user = {
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "permissions": frozenset(permissions),
    }
    created = with_engine(db.owner_url, lambda e: manage.bootstrap_users(e, auth_store(), [user]))
    return created[username]


def add_therapists(db: LocalDb) -> dict[str, str]:
    """Therapist A (admin + therapist) and B (therapist + scheduler), as in the brief."""
    return {
        "a": add_user(db, A_ID, "terapist.a", "Terapist A", expand_sets("admin", "therapist")),
        "b": add_user(db, B_ID, "terapist.b", "Terapist B", expand_sets("therapist", "scheduler")),
    }


# ------------------------------------------------------------------ browser-like client

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_SECRET_RE = re.compile(r'class="secret">([A-Z2-7]+)<')
_RECOVERY_RE = re.compile(r"<code>([0-9a-f]{5}-[0-9a-f]{5})</code>")


def csrf_in(html: str) -> str:
    match = _CSRF_RE.search(html)
    assert match, "no CSRF token in page"
    return match.group(1)


class Kasa:
    """One browser talking to one instance of the local app."""

    def __init__(self, db: LocalDb, **settings: Any) -> None:
        self.db = db
        self.app = create_app(
            Settings(
                database_url=db.app_url,
                fernet_keys=FERNET_KEY,
                auth_fernet_key=AUTH_KEY,
                **settings,
            )
        )
        self.client = TestClient(self.app, base_url="https://testserver", follow_redirects=False)
        self.csrf = ""

    def __enter__(self) -> Self:
        self.client.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.client.__exit__(exc_type, exc, tb)

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.get(path, **kwargs)

    def post(
        self, path: str, data: dict[str, Any] | None = None, *, csrf: str | None = None
    ) -> httpx.Response:
        token = self.csrf if csrf is None else csrf
        return self.client.post(path, data={**(data or {}), "csrf_token": token})

    def api(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """JSON API call with the CSRF header a browser script would send."""
        headers = {"X-CSRF-Token": self.csrf, **kwargs.pop("headers", {})}
        return self.client.request(method, path, headers=headers, **kwargs)

    # --- login steps, individually, so tests can break the flow where they need to

    def submit_password(self, username: str, password: str) -> httpx.Response:
        token = csrf_in(self.get("/login").text)
        return self.post("/login", {"username": username, "password": password}, csrf=token)

    def submit_code(self, code: str) -> httpx.Response:
        token = csrf_in(self.get("/login/kod").text)
        return self.post("/login/kod", {"code": code}, csrf=token)

    def _load_session_csrf(self, path: str = "/") -> None:
        page = self.get(path)
        if page.status_code == 303:  # e.g. a forced password change
            page = self.get(page.headers["location"])
        self.csrf = csrf_in(page.text)

    def onboard(
        self, username: str, temporary_password: str, password: str = PASSWORD
    ) -> tuple[str, list[str]]:
        """First login: enroll TOTP, receive recovery codes, change the temporary password.

        Returns (totp secret, recovery codes).
        """
        response = self.submit_password(username, temporary_password)
        assert response.status_code == 303, response.text
        assert response.headers["location"] == "/hesap/2fa/kur"

        page = self.get("/hesap/2fa/kur")
        assert page.status_code == 200
        secret = _SECRET_RE.search(page.text)
        assert secret, "no TOTP secret on the enrollment page"
        done = self.post(
            "/hesap/2fa/kur",
            {"code": totp.code_at(secret.group(1), now_utc())},
            csrf=csrf_in(page.text),
        )
        assert done.status_code == 200, done.text
        recovery_codes = _RECOVERY_RE.findall(done.text)
        assert len(recovery_codes) == 10

        forced = self.get("/")
        assert forced.status_code == 303 and forced.headers["location"] == "/hesap/sifre"
        self._load_session_csrf("/hesap/sifre")
        changed = self.post(
            "/hesap/sifre",
            {
                "current_password": temporary_password,
                "new_password": password,
                "new_password_repeat": password,
            },
        )
        assert changed.status_code == 303, changed.text
        return secret.group(1), recovery_codes

    def login(
        self,
        username: str,
        secret: str,
        password: str = PASSWORD,
        *,
        code: str | None = None,
        allow_reuse_of_time_step: bool = True,
    ) -> httpx.Response:
        """Password + TOTP. Returns the response to the code submission."""
        if allow_reuse_of_time_step:
            # Tests log in several times within one 30 s step; production never resets this.
            execute(self.db, "UPDATE users SET totp_last_step = NULL WHERE username = $1", username)
        response = self.submit_password(username, password)
        assert response.status_code == 303, response.text
        assert response.headers["location"] == "/login/kod"
        result = self.submit_code(code or totp.code_at(secret, now_utc()))
        if result.status_code == 303:
            self._load_session_csrf()
        return result

    def session_cookie(self) -> str | None:
        return self.client.cookies.get("kasa_session")


def ago(**kwargs: float) -> datetime:
    return now_utc() - timedelta(**kwargs)
