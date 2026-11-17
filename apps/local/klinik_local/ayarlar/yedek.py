"""Ayarlar → Yedekleme (admin.backup): is the office data actually safe?"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import now_utc

DAILY_LATE = timedelta(hours=26)
RESTORE_TEST_LATE = timedelta(days=35)


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.split(" ")[0])
    except ValueError:
        return None


def assess(status: dict[str, tuple[str, datetime]]) -> list[str]:
    """Plain-language warnings, most important first."""
    warnings: list[str] = []
    now = now_utc()
    daily = _when(status.get("last_daily_ok", ("", now))[0])
    if daily is None:
        warnings.append("Henüz başarılı bir gece yedeği yok.")
    elif now - daily > DAILY_LATE:
        warnings.append("Son gece yedeği 26 saatten eski. Yedekleme çalışmıyor olabilir.")
    error = status.get("last_error")
    if error is not None and (daily is None or error[1] > daily):
        warnings.append(f"Son hata: {error[0]}")
    restore = status.get("last_restore_test")
    restore_time = _when(restore[0]) if restore else None
    if restore_time is None:
        warnings.append("Geri yükleme testi hiç yapılmadı. Aylık test için runbook'a bakın.")
    elif now - restore_time > RESTORE_TEST_LATE:
        warnings.append("Son geri yükleme testi bir aydan eski.")
    elif restore and "FAILED" in restore[0]:
        warnings.append("Son geri yükleme testi BAŞARISIZ oldu.")
    return warnings


def build_router(auth: AuthKit, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix="/ayarlar", include_in_schema=False)
    admin = Depends(auth.require(Permission.ADMIN_BACKUP))

    @router.get("/yedek", response_class=HTMLResponse)
    async def page(request: Request, user: CurrentUser = admin) -> Response:
        async with auth.tx(user) as conn:
            rows = (
                await conn.execute(text("SELECT key, value, updated_at FROM backup_status"))
            ).all()
        status = {r.key: (r.value, r.updated_at) for r in rows}
        context: dict[str, Any] = {"status": status, "warnings": assess(status)}
        response = templates.TemplateResponse(request, "ayarlar/yedek.html", context)
        response.headers["Cache-Control"] = "no-store"
        return response

    return router
