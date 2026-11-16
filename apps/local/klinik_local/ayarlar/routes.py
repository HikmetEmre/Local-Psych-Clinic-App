"""Ayarlar tab: user and permission administration (admin.users).

Users are created with the bootstrap command so their UUIDs match in both tiers.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from klinik_local import tables as t
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.labels_tr import PERMISSION_LABELS
from klinik_shared.permissions import ALL_PERMISSION_VALUES, Permission

USERS_PATH = "/ayarlar/kullanicilar"


def build_router(auth: AuthKit, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix="/ayarlar", include_in_schema=False)
    admin = Depends(auth.require(Permission.ADMIN_USERS))

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
            rows = (await conn.execute(select(t.users).order_by(t.users.c.display_name))).all()
            grants = (await conn.execute(select(t.user_permissions))).all()
        by_user: dict[UUID, set[str]] = {}
        for grant in grants:
            by_user.setdefault(grant.user_id, set()).add(grant.permission)
        response = templates.TemplateResponse(
            request,
            "ayarlar/users.html",
            {
                "users": rows,
                "grants": by_user,
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
                await conn.execute(select(t.users.c.display_name).where(t.users.c.id == user_id))
            ).one_or_none()
            if row is None:
                return await users_page(
                    request, user, error="Kullanıcı bulunamadı.", status_code=404
                )
            temporary = await auth.store.reset_password(conn, user_id, actor_id=user.id)
        return await users_page(request, user, temporary_password=(row.display_name, temporary))

    return router
