"""User administration shared by both tiers: pages (admin.users) and operator commands.

The first users come from the installer's TOML file; afterwards the admin adds users
in Ayarlar -> Kullanicilar and gives each one a role (Yonetici or Terapist).
"""

from __future__ import annotations

import re
import tomllib
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from klinik_shared.authkit.service import AuthStore, CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.labels_tr import PERMISSION_LABELS
from klinik_shared.permissions import (
    ALL_PERMISSION_VALUES,
    PERMISSION_SETS,
    ROLES,
    Permission,
    expand_sets,
    role_of,
)

USERS_PATH = "/ayarlar/kullanicilar"
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,39}")

# ---------------------------------------------------------------- operator commands


async def set_role_passwords(engine: AsyncEngine, passwords: dict[str, str]) -> list[str]:
    """Give application roles a password and LOGIN. Returns the roles that were set."""
    done = []
    async with engine.begin() as conn:
        for role, password in passwords.items():
            # ALTER ROLE takes no bind parameters; format() quotes both parts safely.
            statement = (
                await conn.execute(
                    text(
                        "SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', "
                        "CAST(:role AS text), CAST(:password AS text))"
                    ),
                    {"role": role, "password": password},
                )
            ).scalar_one()
            await conn.execute(text(statement))
            done.append(role)
    return done


def parse_users_file(raw: bytes) -> list[dict[str, Any]]:
    data = tomllib.loads(raw.decode("utf-8"))
    users = data.get("users")
    if not isinstance(users, list) or not users:
        raise ValueError("the file needs at least one [[users]] entry")
    parsed = [
        {
            "user_id": UUID(str(entry["id"])),
            "username": str(entry["username"]),
            "display_name": str(entry["display_name"]),
            "permissions": expand_sets(*entry["sets"]),
        }
        for entry in users
    ]
    if len({u["user_id"] for u in parsed}) != len(parsed):
        raise ValueError("duplicate user id")
    return parsed


async def bootstrap_users(
    engine: AsyncEngine, store: AuthStore, users: list[dict[str, Any]]
) -> dict[str, str]:
    """Create missing users. Returns {username: temporary password} for the new ones."""
    created: dict[str, str] = {}
    async with engine.begin() as conn:
        for user in users:
            temporary = await store.ensure_user(conn, **user)
            if temporary is not None:
                created[user["username"]] = temporary
    return created


# ---------------------------------------------------------------- pages


def build_users_router(auth: AuthKit, templates: Jinja2Templates) -> APIRouter:
    """Ayarlar → Kullanıcılar. Each app supplies its own ``ayarlar/_subnav.html``."""
    router = APIRouter(prefix="/ayarlar", include_in_schema=False)
    admin = Depends(auth.require(Permission.ADMIN_USERS))
    users_table = auth.store.t.users
    grants_table = auth.store.t.user_permissions

    async def users_page(
        request: Request,
        user: CurrentUser,
        *,
        error: str | None = None,
        notice: str | None = None,
        temporary_password: tuple[str, str] | None = None,
        status_code: int = 200,
    ) -> Response:
        async with auth.tx(user) as conn:
            rows = (
                await conn.execute(select(users_table).order_by(users_table.c.display_name))
            ).all()
            grants = (await conn.execute(select(grants_table))).all()
        by_user: dict[UUID, set[str]] = {}
        for grant in grants:
            by_user.setdefault(grant.user_id, set()).add(grant.permission)
        response = templates.TemplateResponse(
            request,
            "ayarlar_users.html",
            {
                "users": rows,
                "grants": by_user,
                "roles": ROLES,
                "role_of": {uid: role_of(perms) for uid, perms in by_user.items()},
                "all_permissions": ALL_PERMISSION_VALUES,
                "permission_labels": PERMISSION_LABELS,
                "error": error,
                "notice": notice,
                "temporary_password": temporary_password,
            },
            status_code=status_code,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("")
    async def index(user: CurrentUser = admin) -> Response:
        return RedirectResponse(USERS_PATH, status_code=303)

    @router.get("/kullanicilar", response_class=HTMLResponse)
    async def list_users(request: Request, user: CurrentUser = admin) -> Response:
        return await users_page(request, user)

    @router.post("/kullanicilar", response_class=HTMLResponse)
    async def create_user(request: Request, user: CurrentUser = admin) -> Response:
        form = await request.form()
        username = str(form.get("username") or "").strip().lower()
        display_name = " ".join(str(form.get("display_name") or "").split())
        role = str(form.get("role") or "")
        error: str | None = None
        if not USERNAME_RE.fullmatch(username):
            error = (
                "Kullanıcı adı 3–40 karakter olmalı; küçük harf, rakam, nokta, tire "
                "veya alt çizgi içerebilir (ör. ayse.kaya)."
            )
        elif not display_name or len(display_name) > 80:
            error = "Ad soyad zorunludur."
        elif role not in ROLES:
            error = "Rol seçin."
        if error is not None:
            return await users_page(request, user, error=error, status_code=422)
        async with auth.tx(user) as conn:
            taken = (
                await conn.execute(select(func.count()).where(users_table.c.username == username))
            ).scalar_one()
            if taken:
                return await users_page(
                    request, user, error="Bu kullanıcı adı zaten var.", status_code=409
                )
            temporary = await auth.store.ensure_user(
                conn,
                user_id=uuid4(),
                username=username,
                display_name=display_name,
                permissions=PERMISSION_SETS[role],
            )
        if temporary is None:  # pragma: no cover - a fresh uuid never exists
            return await users_page(request, user, error="Kullanıcı eklenemedi.", status_code=500)
        return await users_page(
            request,
            user,
            notice=f"{display_name} eklendi ({ROLES[role]}).",
            temporary_password=(display_name, temporary),
        )

    @router.post("/kullanicilar/{user_id}/rol", response_class=HTMLResponse)
    async def set_role(request: Request, user_id: UUID, user: CurrentUser = admin) -> Response:
        role = str((await request.form()).get("role") or "")
        if role not in ROLES:
            return await users_page(request, user, error="Rol seçin.", status_code=422)
        if user_id == user.id and role != "admin":
            return await users_page(
                request, user, error="Kendi yönetici rolünüzü kaldıramazsınız.", status_code=400
            )
        async with auth.tx(user) as conn:
            await auth.store.set_permissions(conn, user_id, PERMISSION_SETS[role], actor_id=user.id)
        return await users_page(request, user, notice=f"Rol güncellendi: {ROLES[role]}.")

    @router.post("/kullanicilar/{user_id}/yetkiler", response_class=HTMLResponse)
    async def set_permissions(
        request: Request, user_id: UUID, user: CurrentUser = admin
    ) -> Response:
        form = await request.form()
        wanted = {str(v) for v in form.getlist("permission")} & set(ALL_PERMISSION_VALUES)
        if user_id == user.id and Permission.ADMIN_USERS.value not in wanted:
            return await users_page(
                request,
                user,
                error="Kendi kullanıcı yönetimi yetkinizi kaldıramazsınız.",
                status_code=400,
            )
        async with auth.tx(user) as conn:
            await auth.store.set_permissions(conn, user_id, wanted, actor_id=user.id)
        return await users_page(request, user, notice="Yetkiler güncellendi.")

    @router.post("/kullanicilar/{user_id}/durum", response_class=HTMLResponse)
    async def set_active(request: Request, user_id: UUID, user: CurrentUser = admin) -> Response:
        active = str((await request.form()).get("active") or "") == "1"
        if user_id == user.id and not active:
            return await users_page(
                request, user, error="Kendi hesabınızı pasif yapamazsınız.", status_code=400
            )
        async with auth.tx(user) as conn:
            await auth.store.set_active(conn, user_id, active, actor_id=user.id)
        return await users_page(request, user, notice="Kullanıcı durumu güncellendi.")

    @router.post("/kullanicilar/{user_id}/2fa-sifirla", response_class=HTMLResponse)
    async def reset_second_factor(
        request: Request, user_id: UUID, user: CurrentUser = admin
    ) -> Response:
        if user_id == user.id:
            return await users_page(
                request,
                user,
                error="Kendi iki adımlı doğrulamanızı buradan sıfırlayamazsınız.",
                status_code=400,
            )
        async with auth.tx(user) as conn:
            await auth.store.reset_second_factor(conn, user_id, actor_id=user.id)
        return await users_page(
            request,
            user,
            notice="İki adımlı doğrulama sıfırlandı. Kullanıcı sonraki girişte yeniden kuracak.",
        )

    @router.post("/kullanicilar/{user_id}/sifre-sifirla", response_class=HTMLResponse)
    async def reset_password(
        request: Request, user_id: UUID, user: CurrentUser = admin
    ) -> Response:
        if user_id == user.id:
            return await users_page(
                request,
                user,
                error="Kendi şifrenizi 'Şifre değiştir' sayfasından değiştirin.",
                status_code=400,
            )
        async with auth.tx(user) as conn:
            row = (
                await conn.execute(
                    select(users_table.c.display_name).where(users_table.c.id == user_id)
                )
            ).one_or_none()
            if row is None:
                return await users_page(
                    request, user, error="Kullanıcı bulunamadı.", status_code=404
                )
            temporary = await auth.store.reset_password(conn, user_id, actor_id=user.id)
        return await users_page(request, user, temporary_password=(row.display_name, temporary))

    return router
