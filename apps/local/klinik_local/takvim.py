"""Klinik Kasa: Takvim (read-only mirror with real names) and Çakışmalar (sync conflicts).

The office never writes appointments: booking happens in Klinik Web. Here the
schedule is shown with names (joined locally by client code) and each appointment
leads to its session record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.danisanlar.service import ClientService
from klinik_local.outbox import enqueue_outcome, enqueue_resolution
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.calendar import Person, build_grid, first_day, parse_day, span, title_for
from klinik_shared.enums import AppointmentStatus, ConflictResolution
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import istanbul_day_bounds, now_utc, to_istanbul

STALE_AFTER = timedelta(minutes=5)

REASONS = {
    "status_mismatch": "Randevunun durumu bulutta başka biri tarafından değiştirilmiş.",
    "unknown_appointment": "Randevu bulutta bulunamadı.",
    "appointment_deleted": "Randevu bulutta silinmiş.",
    "overlap": "Randevu yeniden planlanamadı: saat artık dolu.",
    "unknown_therapist": "Terapist bulutta tanımlı değil.",
    "invalid": "Bulut bu kaydı kabul etmedi.",
    "missing_locally": (
        "Bu danışan kodu bulutta var ama bu bilgisayarda kaydı yok "
        "(ör. eski bir yedekten geri dönüldü). Danışanı yeniden kaydetmeniz gerekebilir."
    ),
}


@dataclass(frozen=True)
class MirrorAppointment:
    id: UUID
    client_code: str
    client_name: str | None
    therapist_id: UUID
    therapist_name: str
    room_label: str
    starts_at: datetime
    ends_at: datetime
    status: str
    session_id: UUID | None


async def sync_banner(conn: AsyncConnection) -> str | None:
    """A warning when the office has not heard from the cloud for a while (brief §9)."""
    rows = (await conn.execute(select(t.sync_state.c.key, t.sync_state.c.value))).all()
    state = {r.key: r.value for r in rows}
    if state.get("worker_state") == "auth_failed":
        return "Eşitleme durdu: bulut erişim anahtarı reddedildi. Yöneticiye haber verin."
    if state.get("worker_state") == "protocol_mismatch":
        return "Eşitleme durdu: yazılım sürümleri uyumsuz. Güncelleme gerekiyor."
    last = state.get("last_pull_ok")
    try:
        seen = datetime.fromisoformat(last) if last else None
    except ValueError:
        seen = None
    if seen is None:
        return "Takvim henüz bulutla eşitlenmedi. Gösterilen randevular eksik olabilir."
    if now_utc() - seen > STALE_AFTER:
        return (
            f"Bulutla son eşitleme: {to_istanbul(seen):%d.%m.%Y %H:%M}. "
            "Takvim güncel olmayabilir; yeni randevular bulut erişilince görünecek."
        )
    return None


def build_router(
    auth: AuthKit,
    clients: ClientService,
    templates: Jinja2Templates,
    web_base_url: str | None,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    can_read = Depends(auth.require(Permission.SCHEDULE_READ_ALL))
    web = (web_base_url or "").rstrip("/") or None

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    # ------------------------------------------------------------ takvim

    @router.get("/takvim", response_class=HTMLResponse)
    async def calendar(request: Request, user: CurrentUser = can_read) -> Response:
        params = request.query_params
        view = "gun" if params.get("gorunum") == "gun" else "hafta"
        day = parse_day(params.get("gun"))
        first = first_day(view, day)
        start = istanbul_day_bounds(first)[0]
        end = istanbul_day_bounds(first + timedelta(days=span(view) - 1))[1]
        async with auth.tx(user) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT m.id, m.client_code, m.therapist_id, u.display_name, "
                        "coalesce(r.internal_label, '—') AS room_label, m.starts_at, "
                        "m.starts_at + make_interval(mins => m.duration_min) AS ends_at, "
                        "m.status, s.id AS session_id "
                        "FROM appointments_mirror m "
                        "LEFT JOIN users u ON u.id = m.therapist_id "
                        "LEFT JOIN rooms_mirror r ON r.id = m.room_id "
                        # RLS: only the viewer's own sessions are visible here.
                        "LEFT JOIN therapy_sessions s "
                        "  ON s.appointment_id = m.id AND s.deleted_at IS NULL "
                        "WHERE m.deleted_at IS NULL AND m.starts_at < :end "
                        "AND m.starts_at + make_interval(mins => m.duration_min) > :start "
                        "ORDER BY m.starts_at"
                    ),
                    {"start": start, "end": end},
                )
            ).all()
            names: dict[str, str] = {}
            if user.has(Permission.CLIENTS_READ) and rows:
                codes = {r.client_code for r in rows}
                names = {
                    c.client_code: c.full_name
                    for c in await clients.search(conn, status="all")
                    if c.client_code in codes
                }
            therapists = [Person(th.id, th.display_name) for th in await clients.therapists(conn)]
            banner = await sync_banner(conn)
        items = [
            MirrorAppointment(
                r.id,
                r.client_code,
                names.get(r.client_code),
                r.therapist_id,
                r.display_name or "—",
                r.room_label,
                r.starts_at,
                r.ends_at,
                r.status,
                r.session_id,
            )
            for r in rows
        ]
        grid = build_grid(view, day, items, therapists)
        color = {th.id: index % 4 for index, th in enumerate(therapists)}
        step = timedelta(days=span(view))

        def cal_url(**changes: Any) -> str:
            query = {"gorunum": view, "gun": day.isoformat(), **changes}
            return "/takvim?" + "&".join(f"{k}={v}" for k, v in query.items())

        return render(
            request,
            "takvim.html",
            {
                "view": view,
                "day": day,
                "title": title_for(view, day),
                "grid": grid,
                "therapists": therapists,
                "color": color,
                "cal_url": cal_url,
                "prev_day": (day - step).isoformat(),
                "next_day": (day + step).isoformat(),
                "today": to_istanbul(now_utc()).date().isoformat(),
                "to_istanbul": to_istanbul,
                "web": web,
                "sync_banner": banner,
                "can_record": user.has(Permission.SESSIONS_WRITE_OWN),
            },
        )

    # ------------------------------------------------------------ çakışmalar

    def sees_all(user: CurrentUser) -> bool:
        return user.has(Permission.ADMIN_USERS)

    @router.get("/cakismalar", response_class=HTMLResponse)
    async def conflicts(request: Request, user: CurrentUser = can_read) -> Response:
        c = t.sync_conflicts
        stmt = select(c).where(c.c.status == "open").order_by(c.c.created_at.desc())
        if not sees_all(user):
            stmt = stmt.where(c.c.author_id == user.id)
        async with auth.tx(user) as conn:
            items = (await conn.execute(stmt)).all()
            names: dict[str, str] = {}
            if user.has(Permission.CLIENTS_READ):
                names = {
                    x.client_code: x.full_name for x in await clients.search(conn, status="all")
                }
        return render(
            request,
            "cakismalar.html",
            {
                "items": items,
                "reasons": REASONS,
                "names": names,
                "ok": request.query_params.get("ok") == "1",
            },
        )

    @router.post("/cakismalar/{conflict_id}/coz")
    async def resolve(
        request: Request, conflict_id: UUID, user: CurrentUser = can_read
    ) -> Response:
        choice = str((await request.form()).get("karar") or "")
        if choice not in (ConflictResolution.DISCARDED.value, ConflictResolution.REAPPLIED.value):
            return RedirectResponse("/cakismalar", status_code=303)
        c = t.sync_conflicts
        async with auth.tx(user) as conn:
            row = (
                await conn.execute(
                    select(c).where(c.c.id == conflict_id, c.c.status == "open").with_for_update()
                )
            ).one_or_none()
            if row is None or not (row.author_id == user.id or sees_all(user)):
                return render(
                    request,
                    "error.html",
                    {"title": "Çakışma bulunamadı", "message": "Bu kayıt mevcut değil."},
                    404,
                )
            if choice == ConflictResolution.REAPPLIED.value and not (
                row.appointment_id and row.proposed_status
            ):
                choice = ConflictResolution.DISCARDED.value
            await conn.execute(
                update(c)
                .where(c.c.id == conflict_id)
                .values(
                    status="resolved", resolution=choice, resolved_by=user.id, resolved_at=now_utc()
                )
            )
            if row.outbox_item_id is not None:
                await enqueue_resolution(
                    conn,
                    conflict_item_id=row.outbox_item_id,
                    resolution=ConflictResolution(choice),
                )
            if choice == ConflictResolution.REAPPLIED.value:
                # Re-send the outcome on top of what the cloud shows now.
                current = (
                    await conn.execute(
                        select(t.appointments_mirror.c.status).where(
                            t.appointments_mirror.c.id == row.appointment_id
                        )
                    )
                ).scalar_one_or_none()
                await enqueue_outcome(
                    conn,
                    appointment_id=row.appointment_id,
                    new_status=AppointmentStatus(row.proposed_status),
                    prev_status=AppointmentStatus(current or row.cloud_status or "planned"),
                    actor_id=user.id,
                )
            if row.appointment_id is not None:
                # Items that waited behind this conflict may go now.
                await conn.execute(
                    text("SELECT release_held_outbox(:a)"), {"a": row.appointment_id}
                )
            await write_audit(
                conn,
                t.audit_log,
                action="conflict_resolved",
                actor_id=user.id,
                entity_type="sync_conflict",
                entity_id=conflict_id,
                detail={"resolution": choice},
            )
        return RedirectResponse("/cakismalar?ok=1", status_code=303)

    return router
