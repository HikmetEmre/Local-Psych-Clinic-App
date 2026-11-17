"""Hesap → Günlük özet: each therapist chooses the passphrase for their own snapshot file."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from klinik_local.crypto.fernet import FieldCipher
from klinik_local.snapshot import MIN_PASSPHRASE, PassphraseError, new_key_record
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.permissions import Permission

PATH = "/hesap/ozet"


def build_router(auth: AuthKit, cipher: FieldCipher, templates: Jinja2Templates) -> APIRouter:
    from klinik_local import tables as t

    router = APIRouter(include_in_schema=False)
    therapist = Depends(auth.require(Permission.SESSIONS_READ_OWN))

    async def page(
        request: Request, user: CurrentUser, *, error: str | None = None, status: int = 200
    ) -> Response:
        async with auth.tx(user) as conn:
            has_key = (
                await conn.execute(
                    text("SELECT updated_at FROM snapshot_keys WHERE user_id = :u"), {"u": user.id}
                )
            ).scalar_one_or_none()
            last = (
                await conn.execute(
                    text("SELECT value FROM backup_status WHERE key = 'last_snapshot_ok'")
                )
            ).scalar_one_or_none()
        response = templates.TemplateResponse(
            request,
            "ozet.html",
            {
                "has_key": has_key,
                "last": last,
                "error": error,
                "min_length": MIN_PASSPHRASE,
                "saved": request.query_params.get("kaydedildi") == "1",
            },
            status_code=status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get(PATH, response_class=HTMLResponse)
    async def show(request: Request, user: CurrentUser = therapist) -> Response:
        return await page(request, user)

    @router.post(PATH, response_class=HTMLResponse)
    async def save(request: Request, user: CurrentUser = therapist) -> Response:
        form = await request.form()
        first = str(form.get("passphrase") or "")
        second = str(form.get("passphrase_repeat") or "")
        if first != second:
            return await page(request, user, error="Şifreler birbiriyle aynı değil.", status=400)
        try:
            record = new_key_record(first, cipher)
        except PassphraseError as exc:
            return await page(request, user, error=str(exc), status=400)
        async with auth.tx(user) as conn:
            await conn.execute(
                text(
                    "INSERT INTO snapshot_keys (user_id, salt, iterations, key_enc, updated_at) "
                    "VALUES (:u, :s, :i, :k, now()) ON CONFLICT (user_id) DO UPDATE SET "
                    "salt = EXCLUDED.salt, iterations = EXCLUDED.iterations, "
                    "key_enc = EXCLUDED.key_enc, updated_at = now()"
                ),
                {
                    "u": user.id,
                    "s": record["salt"],
                    "i": record["iterations"],
                    "k": record["key_enc"],
                },
            )
            await write_audit(
                conn, t.audit_log, action="snapshot_key_set", actor_id=user.id,
                entity_type="user", entity_id=user.id,
            )  # fmt: skip
        return RedirectResponse(f"{PATH}?kaydedildi=1", status_code=303)

    @router.post(f"{PATH}/kapat", response_class=HTMLResponse)
    async def disable(request: Request, user: CurrentUser = therapist) -> Response:
        async with auth.tx(user) as conn:
            await conn.execute(text("DELETE FROM snapshot_keys WHERE user_id = :u"), {"u": user.id})
            await write_audit(
                conn, t.audit_log, action="snapshot_key_removed", actor_id=user.id,
                entity_type="user", entity_id=user.id,
            )  # fmt: skip
        return RedirectResponse(PATH, status_code=303)

    return router
