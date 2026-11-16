"""Web integration: cookies, CSRF, ``require(permission)`` and the login routes."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import segno
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from klinik_shared.authkit import totp
from klinik_shared.authkit.passwords import PasswordPolicyError
from klinik_shared.authkit.service import AuthStore, CurrentUser, LoginStatus, PendingSession
from klinik_shared.permissions import Permission

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOGIN_PATH = "/login"
PASSWORD_PATH = "/hesap/sifre"  # noqa: S105 - a URL path, not a password

GENERIC_LOGIN_ERROR = "Kullanıcı adı veya şifre hatalı."
LOCKED_ERROR = "Çok fazla hatalı deneme. 15 dakika sonra tekrar deneyin."
CODE_ERROR = "Kod hatalı veya süresi dolmuş. Yeni kodu girin."


class NotAuthenticated(Exception):
    pass


class PasswordChangeRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class CsrfError(Exception):
    pass


@dataclass(frozen=True)
class AuthWebConfig:
    cookie_name: str
    issuer: str  # shown in the authenticator app
    cookie_secure: bool = True

    @property
    def csrf_cookie_name(self) -> str:
        return f"{self.cookie_name}_csrf"


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


class AuthKit:
    def __init__(
        self,
        engine: AsyncEngine,
        store: AuthStore,
        config: AuthWebConfig,
        templates: Jinja2Templates,
    ) -> None:
        self.engine = engine
        self.store = store
        self.config = config
        self.templates = templates

    # ------------------------------------------------------------ transactions

    @asynccontextmanager
    async def tx(self, user: CurrentUser | None = None) -> AsyncIterator[AsyncConnection]:
        """One transaction. With a user, ``app.current_user_id`` is set for RLS policies."""
        async with self.engine.begin() as conn:
            if user is not None:
                await conn.execute(
                    text("SELECT set_config('app.current_user_id', :uid, true)"),
                    {"uid": str(user.id)},
                )
            yield conn

    # ------------------------------------------------------------ current user

    async def current_user(self, request: Request, *, touch: bool = True) -> CurrentUser | None:
        if hasattr(request.state, "user"):
            cached: CurrentUser | None = request.state.user
            return cached
        token = request.cookies.get(self.config.cookie_name)
        user: CurrentUser | None = None
        if token:
            async with self.tx() as conn:
                user = await self.store.resolve(conn, token, touch=touch)
        request.state.user = user
        if user is not None:
            request.state.csrf_token = user.csrf_token
        return user

    def require(
        self, *permissions: Permission, touch: bool = True
    ) -> Callable[[Request], Awaitable[CurrentUser]]:
        """FastAPI dependency: a logged-in user holding all ``permissions``.

        Also verifies the CSRF token on every state-changing request.
        """

        async def dependency(request: Request) -> CurrentUser:
            user = await self.current_user(request, touch=touch)
            if user is None:
                raise NotAuthenticated
            if request.method not in SAFE_METHODS:
                await self._check_csrf(request, user.csrf_token)
            if user.must_change_password and request.url.path != PASSWORD_PATH:
                raise PasswordChangeRequired
            if any(not user.has(p) for p in permissions):
                raise Forbidden
            return user

        return dependency

    async def _check_csrf(self, request: Request, expected: str) -> None:
        supplied = request.headers.get("X-CSRF-Token")
        if supplied is None:
            content_type = request.headers.get("content-type", "")
            if content_type.startswith(
                ("application/x-www-form-urlencoded", "multipart/form-data")
            ):
                value = (await request.form()).get("csrf_token")
                supplied = value if isinstance(value, str) else None
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise CsrfError

    # ------------------------------------------------------------ cookies

    def _set_session_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            self.config.cookie_name,
            token,
            httponly=True,
            secure=self.config.cookie_secure,
            samesite="lax",
            path="/",
        )

    def _clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.config.cookie_name,
            httponly=True,
            secure=self.config.cookie_secure,
            samesite="lax",
            path="/",
        )

    def _anonymous_csrf(self, request: Request) -> tuple[str, bool]:
        """Double-submit token for forms shown before login. Returns (token, is_new)."""
        existing = request.cookies.get(self.config.csrf_cookie_name)
        if existing and len(existing) >= 32:
            return existing, False
        return secrets.token_urlsafe(32), True

    def _render(
        self,
        request: Request,
        name: str,
        context: dict[str, Any],
        *,
        status_code: int = 200,
        anonymous: bool = False,
    ) -> Response:
        fresh = False
        if anonymous:
            token, fresh = self._anonymous_csrf(request)
            request.state.csrf_token = token
        response = self.templates.TemplateResponse(request, name, context, status_code=status_code)
        response.headers["Cache-Control"] = "no-store"
        if anonymous and fresh:
            response.set_cookie(
                self.config.csrf_cookie_name,
                request.state.csrf_token,
                httponly=True,
                secure=self.config.cookie_secure,
                samesite="lax",
                path="/",
            )
        return response

    async def _check_anonymous_csrf(self, request: Request) -> None:
        cookie = request.cookies.get(self.config.csrf_cookie_name)
        value = (await request.form()).get("csrf_token")
        if not cookie or not isinstance(value, str) or not hmac.compare_digest(cookie, value):
            raise CsrfError

    async def _pending(self, request: Request) -> PendingSession | None:
        async with self.tx() as conn:
            return await self.store.get_pending(conn, request.cookies.get(self.config.cookie_name))

    # ------------------------------------------------------------ install

    def install(self, app: FastAPI) -> None:
        app.state.auth = self
        app.include_router(self._router())

        def to_login(request: Request) -> Response:
            if is_api(request):
                return JSONResponse({"detail": "not_authenticated"}, status_code=401)
            if wants_fragment(request):
                return Response(status_code=401, headers={"HX-Redirect": LOGIN_PATH})
            return RedirectResponse(LOGIN_PATH, status_code=303)

        @app.exception_handler(NotAuthenticated)
        async def _not_authenticated(request: Request, _: NotAuthenticated) -> Response:
            return to_login(request)

        @app.exception_handler(PasswordChangeRequired)
        async def _password_change(request: Request, _: PasswordChangeRequired) -> Response:
            if is_api(request):
                return JSONResponse({"detail": "password_change_required"}, status_code=403)
            if wants_fragment(request):
                return Response(status_code=403, headers={"HX-Redirect": PASSWORD_PATH})
            return RedirectResponse(PASSWORD_PATH, status_code=303)

        @app.exception_handler(Forbidden)
        async def _forbidden(request: Request, _: Forbidden) -> Response:
            if is_api(request):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            return self.templates.TemplateResponse(
                request,
                "error.html",
                {"title": "Yetkiniz yok", "message": "Bu sayfayı görüntüleme yetkiniz bulunmuyor."},
                status_code=403,
            )

        @app.exception_handler(CsrfError)
        async def _csrf(request: Request, _: CsrfError) -> Response:
            if is_api(request):
                return JSONResponse({"detail": "csrf"}, status_code=403)
            return self.templates.TemplateResponse(
                request,
                "error.html",
                {
                    "title": "İstek doğrulanamadı",
                    "message": "Sayfayı yenileyip tekrar deneyin.",
                },
                status_code=403,
            )

    # ------------------------------------------------------------ routes

    def _router(self) -> APIRouter:
        router = APIRouter(include_in_schema=False)
        cookie = self.config.cookie_name

        @router.get(LOGIN_PATH, response_class=HTMLResponse)
        async def login_form(request: Request) -> Response:
            if await self.current_user(request) is not None:
                return RedirectResponse("/", status_code=303)
            return self._render(request, "auth/login.html", {"error": None}, anonymous=True)

        @router.post(LOGIN_PATH, response_class=HTMLResponse)
        async def login_submit(request: Request) -> Response:
            await self._check_anonymous_csrf(request)
            form = await request.form()
            username = str(form.get("username") or "")
            password = str(form.get("password") or "")
            async with self.tx() as conn:
                result = await self.store.password_login(
                    conn, username, password, client_ip(request)
                )
            if result.status is not LoginStatus.OK or result.token is None:
                error = LOCKED_ERROR if result.status is LoginStatus.LOCKED else GENERIC_LOGIN_ERROR
                return self._render(
                    request, "auth/login.html", {"error": error}, status_code=401, anonymous=True
                )
            target = "/hesap/2fa/kur" if result.needs_enrollment else "/login/kod"
            response = RedirectResponse(target, status_code=303)
            self._set_session_cookie(response, result.token)
            return response

        @router.get("/login/kod", response_class=HTMLResponse)
        async def code_form(request: Request) -> Response:
            pending = await self._pending(request)
            if pending is None:
                return RedirectResponse(LOGIN_PATH, status_code=303)
            if not pending.totp_enrolled:
                return RedirectResponse("/hesap/2fa/kur", status_code=303)
            return self._render(request, "auth/code.html", {"error": None}, anonymous=True)

        @router.post("/login/kod", response_class=HTMLResponse)
        async def code_submit(request: Request) -> Response:
            await self._check_anonymous_csrf(request)
            pending = await self._pending(request)
            if pending is None:
                return RedirectResponse(LOGIN_PATH, status_code=303)
            code = str((await request.form()).get("code") or "")
            async with self.tx() as conn:
                token = await self.store.verify_second_factor(
                    conn, pending, code, client_ip(request)
                )
            if token is None:
                return self._render(
                    request,
                    "auth/code.html",
                    {"error": CODE_ERROR},
                    status_code=401,
                    anonymous=True,
                )
            response = RedirectResponse("/", status_code=303)
            self._set_session_cookie(response, token)
            return response

        def enroll_page(
            request: Request, username: str, secret: str, error: str | None
        ) -> Response:
            uri = totp.provisioning_uri(secret, username, self.config.issuer)
            qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#2a5140", border=2)
            return self._render(
                request,
                "auth/enroll.html",
                {"qr_svg": Markup(qr), "secret": secret, "error": error},  # noqa: S704
                status_code=401 if error else 200,
                anonymous=True,
            )

        @router.get("/hesap/2fa/kur", response_class=HTMLResponse)
        async def enroll_form(request: Request) -> Response:
            pending = await self._pending(request)
            if pending is None:
                return RedirectResponse(LOGIN_PATH, status_code=303)
            if pending.totp_enrolled:
                return RedirectResponse("/login/kod", status_code=303)
            async with self.tx() as conn:
                secret = await self.store.start_enrollment(conn, pending)
            return enroll_page(request, pending.username, secret, None)

        @router.post("/hesap/2fa/kur", response_class=HTMLResponse)
        async def enroll_submit(request: Request) -> Response:
            await self._check_anonymous_csrf(request)
            pending = await self._pending(request)
            if pending is None or pending.totp_enrolled:
                return RedirectResponse(LOGIN_PATH, status_code=303)
            code = str((await request.form()).get("code") or "")
            async with self.tx() as conn:
                confirmed = await self.store.confirm_enrollment(
                    conn, pending, code, client_ip(request)
                )
                secret = None if confirmed else await self.store.start_enrollment(conn, pending)
            if confirmed is None:
                return enroll_page(request, pending.username, secret or "", CODE_ERROR)
            token, recovery_codes = confirmed
            response = self._render(request, "auth/recovery.html", {"codes": recovery_codes})
            self._set_session_cookie(response, token)
            return response

        @router.post("/logout")
        async def logout(request: Request) -> Response:
            user = await self.current_user(request)
            if user is not None:
                await self._check_csrf(request, user.csrf_token)
                async with self.tx() as conn:
                    await self.store.logout(conn, request.cookies.get(cookie))
            response = RedirectResponse(LOGIN_PATH, status_code=303)
            self._clear_session_cookie(response)
            return response

        require_login = self.require()

        @router.get(PASSWORD_PATH, response_class=HTMLResponse)
        async def password_form(request: Request) -> Response:
            user = await require_login(request)
            return self._render(
                request,
                "auth/password.html",
                {"error": None, "done": False, "forced": user.must_change_password},
            )

        @router.post(PASSWORD_PATH, response_class=HTMLResponse)
        async def password_submit(request: Request) -> Response:
            user = await require_login(request)
            form = await request.form()
            current = str(form.get("current_password") or "")
            new = str(form.get("new_password") or "")
            repeat = str(form.get("new_password_repeat") or "")
            error: str | None = None
            if new != repeat:
                error = "Yeni şifreler birbiriyle aynı değil."
            else:
                try:
                    async with self.tx() as conn:
                        if not await self.store.change_password(conn, user, current, new):
                            error = "Mevcut şifre hatalı."
                except PasswordPolicyError as exc:
                    error = str(exc)
            if error is None and user.must_change_password:
                return RedirectResponse("/", status_code=303)
            return self._render(
                request,
                "auth/password.html",
                {"error": error, "done": error is None, "forced": user.must_change_password},
                status_code=400 if error else 200,
            )

        return router
