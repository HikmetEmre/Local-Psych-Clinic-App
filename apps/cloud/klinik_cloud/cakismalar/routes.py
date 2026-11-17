"""Çakışmalar: changes that could not be applied automatically and need a person (brief §9).

A booking conflict keeps the rejected change in ``proposed``. Its author (or an
admin) either keeps the current appointment or re-applies the change on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_cloud import tables as t
from klinik_cloud.takvim.service import (
    Appointment,
    AppointmentError,
    AppointmentFields,
    AppointmentNotFound,
    AppointmentService,
    BookingConflict,
)
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.enums import ConflictResolution, ConflictStatus
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import now_utc

FIELD_LABELS = {
    "starts_at": "Tarih / saat",
    "duration_min": "Süre",
    "therapist_id": "Terapist",
    "room_id": "Oda",
    "status": "Durum",
}


@dataclass(frozen=True)
class ConflictView:
    id: UUID
    kind: str
    created_at: datetime
    author_name: str | None
    author_id: UUID | None
    proposed: AppointmentFields
    appointment: Appointment | None


def build_router(
    auth: AuthKit, service: AppointmentService, templates: Jinja2Templates
) -> APIRouter:
    router = APIRouter(prefix="/cakismalar", include_in_schema=False)
    can_read = Depends(auth.require(Permission.SCHEDULE_READ_ALL))

    def sees_all(user: CurrentUser) -> bool:
        return user.has(Permission.ADMIN_USERS)

    async def open_conflicts(conn: AsyncConnection, user: CurrentUser) -> list[ConflictView]:
        c, u = t.conflicts, t.users
        stmt = (
            select(c, u.c.display_name)
            .select_from(c.outerjoin(u, u.c.id == c.c.author_id))
            .where(c.c.status == ConflictStatus.OPEN.value)
            .order_by(c.c.created_at.desc())
        )
        if not sees_all(user):
            stmt = stmt.where(c.c.author_id == user.id)
        views = []
        for row in (await conn.execute(stmt)).all():
            appointment = None
            if row.appointment_id is not None:
                try:
                    appointment = await service.get(conn, row.appointment_id)
                except AppointmentNotFound:
                    appointment = None
            views.append(
                ConflictView(
                    row.id,
                    row.kind,
                    row.created_at,
                    row.display_name,
                    row.author_id,
                    AppointmentFields.model_validate(row.proposed),
                    appointment,
                )
            )
        return views

    async def page(
        request: Request, user: CurrentUser, *, notice: str | None = None,
        error: str | None = None, status: int = 200,
    ) -> Response:  # fmt: skip
        async with auth.tx(user) as conn:
            items = await open_conflicts(conn, user)
            names = {o.id: o.label for o in await service.therapists(conn)}
            names.update({o.id: o.label for o in await service.rooms(conn, active_only=False)})
        response = templates.TemplateResponse(
            request,
            "cakismalar.html",
            {
                "items": items,
                "names": names,
                "labels": FIELD_LABELS,
                "notice": notice,
                "error": error,
            },
            status_code=status,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.get("", response_class=HTMLResponse)
    async def index(request: Request, user: CurrentUser = can_read) -> Response:
        return await page(request, user)

    @router.post("/{conflict_id}/coz", response_class=HTMLResponse)
    async def resolve(
        request: Request, conflict_id: UUID, user: CurrentUser = can_read
    ) -> Response:
        choice = str((await request.form()).get("karar") or "")
        if choice not in {ConflictResolution.DISCARDED.value, ConflictResolution.REAPPLIED.value}:
            return await page(request, user, error="Geçersiz seçim.", status=400)
        c = t.conflicts
        error: str | None = None
        async with auth.tx(user) as conn:
            row = (
                await conn.execute(
                    select(c)
                    .where(c.c.id == conflict_id, c.c.status == ConflictStatus.OPEN.value)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None or not (row.author_id == user.id or sees_all(user)):
                return await page(request, user, error="Çakışma bulunamadı.", status=404)
            if choice == ConflictResolution.REAPPLIED.value:
                if not user.has(Permission.SCHEDULE_WRITE_ALL):
                    return await page(
                        request, user, error="Randevu değiştirme yetkiniz yok.", status=403
                    )
                try:
                    current = await service.get(conn, row.appointment_id)
                    proposed = AppointmentFields.model_validate(row.proposed)
                    await service.update(
                        conn,
                        user,
                        current.id,
                        changes=proposed,
                        based_on=proposed,
                        expected_version=current.version,
                    )
                except AppointmentNotFound:
                    error = "Randevu artık mevcut değil."
                except (AppointmentError, BookingConflict) as exc:
                    error = getattr(exc, "message", "Değişiklik uygulanamadı.")
            if error is None:
                await conn.execute(
                    update(c)
                    .where(c.c.id == conflict_id)
                    .values(
                        status=ConflictStatus.RESOLVED.value,
                        resolution=choice,
                        resolved_by=user.id,
                        resolved_at=now_utc(),
                    )
                )
                await write_audit(
                    conn,
                    t.audit_log,
                    action="conflict_resolved",
                    actor_id=user.id,
                    entity_type="conflict",
                    entity_id=conflict_id,
                    detail={"resolution": choice},
                )
        if error is not None:
            return await page(request, user, error=error, status=409)
        return RedirectResponse("/cakismalar?ok=1", status_code=303)

    return router


def describe(fields: AppointmentFields, names: dict[UUID, str]) -> list[tuple[str, Any]]:
    """Human-readable (label, value) pairs for a proposed change."""
    from klinik_shared.labels_tr import STATUS_LABELS
    from klinik_shared.timeutil import format_datetime_tr

    pairs: list[tuple[str, Any]] = []
    for key, value in fields.changes().items():
        if key == "starts_at":
            shown: Any = format_datetime_tr(value)
        elif key in ("therapist_id", "room_id"):
            shown = names.get(value, "—")
        elif key == "status":
            shown = STATUS_LABELS.get(str(value), str(value))
        elif key == "duration_min":
            shown = f"{value} dk"
        else:
            shown = value
        pairs.append((FIELD_LABELS.get(key, key), shown))
    return pairs
