"""Pages for the laptop copy.

Office (admin): Ayarlar → Dizüstü kopya (pair a laptop, see / revoke pairings) and
Ayarlar → Çakışmalar (what could not be merged automatically).
Laptop: Ayarlar → Eşitleme (status, "Şimdi eşitle").
"""

from __future__ import annotations

import json
import socket
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from klinik_local import tables as t
from klinik_local.config import Settings
from klinik_local.crypto.fernet import DecryptionError, FieldCipher
from klinik_local.replika import store
from klinik_local.replika.laptop import ReplicaSync
from klinik_local.replika.office import new_token
from klinik_local.replika.pairing import MIN_PASSPHRASE, PairingError, seal
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.authkit.web import AuthKit
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import format_datetime_tr, now_utc

TABLE_LABELS = {
    "clients": "Danışan",
    "appointments": "Randevu",
    "therapy_sessions": "Seans kaydı",
    "payments": "Ödeme",
    "expenses": "Gider",
    "client_notes": "Klinik not",
    "note_revisions": "Not geçmişi",
}
FIELD_LABELS = {
    "full_name_encrypted": "Ad soyad",
    "phone_encrypted": "Telefon",
    "email_encrypted": "E-posta",
    "birth_date": "Doğum tarihi",
    "assigned_therapist_id": "Sorumlu terapist",
    "is_active": "Aktif",
    "starts_at": "Başlangıç",
    "duration_min": "Süre (dk)",
    "therapist_id": "Terapist",
    "room_id": "Oda",
    "client_id": "Danışan",
    "status": "Durum",
    "session_date": "Seans tarihi",
    "session_number": "Seans no",
    "notes_encrypted": "Not",
    "homework_encrypted": "Ödev",
    "next_session_date": "Sonraki seans",
    "appointment_id": "Randevu",
    "amount": "Tutar",
    "method": "Yöntem",
    "paid_at": "Ödeme tarihi",
    "voided_at": "İptal tarihi",
    "void_reason": "İptal nedeni",
    "deleted_at": "Silinme",
    "format_method_id": "Seans biçimi",
    "kind": "Tür",
    "title_encrypted": "Başlık",
    "body_encrypted": "Not",
    "noted_at": "Not tarihi",
    "important": "Önemli",
    "spent_on": "Gider tarihi",
    "category": "Gider türü",
    "description": "Açıklama",
}
HIDDEN = {
    "id", "created_at", "updated_at", "change_seq", "version", "created_by", "updated_by",
    "ends_at", "session_counter", "age_group_id_at_session", "recorded_by", "voided_by",
    "author_id",
    "__methods",
}  # fmt: skip


def build_office_router(
    auth: AuthKit,
    settings: Settings,
    sync_engine: AsyncEngine,
    cipher: FieldCipher,
    templates: Jinja2Templates,
) -> APIRouter:
    router = APIRouter(prefix="/ayarlar", include_in_schema=False)
    admin = Depends(auth.require(Permission.ADMIN_USERS))

    def render(request: Request, name: str, context: dict[str, Any], status: int = 200) -> Response:
        response = templates.TemplateResponse(request, name, context, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        return response

    def office_url() -> str:
        if settings.public_url:
            return settings.public_url
        return f"https://{socket.gethostname()}:8443"

    # ------------------------------------------------------------ pairing

    async def pairing_page(
        request: Request, user: CurrentUser, *, error: str | None = None, status: int = 200
    ) -> Response:
        async with auth.tx(user) as conn:
            replicas = (
                await conn.execute(
                    text(
                        "SELECT r.id, r.label, r.created_at, r.last_seen_at, r.revoked_at, "
                        "u.display_name, o.display_name AS owner FROM replicas r "
                        "LEFT JOIN users u ON u.id = r.created_by "
                        "LEFT JOIN users o ON o.id = r.user_id ORDER BY r.created_at DESC"
                    )
                )
            ).all()
            people = (
                await conn.execute(
                    text("SELECT id, display_name FROM users WHERE is_active ORDER BY display_name")
                )
            ).all()
        return render(
            request,
            "ayarlar/dizustu.html",
            {
                "replicas": replicas,
                "people": people,
                "office_url": office_url(),
                "has_cert": bool(settings.tls_cert and Path(settings.tls_cert).exists()),
                "min_passphrase": MIN_PASSPHRASE,
                "error": error,
                "notice": "Eşleştirme iptal edildi." if request.query_params.get("iptal") else None,
            },
            status,
        )

    @router.get("/dizustu", response_class=HTMLResponse)
    async def pairing(request: Request, user: CurrentUser = admin) -> Response:
        return await pairing_page(request, user)

    @router.post("/dizustu", response_class=HTMLResponse)
    async def create_pairing(request: Request, user: CurrentUser = admin) -> Response:
        form = await request.form()
        label = " ".join(str(form.get("label") or "").split())[:60]
        passphrase = str(form.get("passphrase") or "")
        try:
            owner_id = UUID(str(form.get("user_id") or ""))
        except ValueError:
            return await pairing_page(
                request, user, error="Bilgisayarın kime ait olduğunu seçin.", status=422
            )
        async with auth.tx(user) as conn:
            owner = (
                await conn.execute(
                    text("SELECT display_name FROM users WHERE id = :id AND is_active"),
                    {"id": owner_id},
                )
            ).scalar_one_or_none()
        if owner is None:
            return await pairing_page(request, user, error="Kullanıcı bulunamadı.", status=422)
        label = label or f"{owner} bilgisayarı"
        if passphrase != str(form.get("passphrase2") or ""):
            return await pairing_page(request, user, error="Parolalar aynı değil.", status=422)
        cert_pem = None
        if settings.tls_cert and Path(settings.tls_cert).exists():
            cert_pem = Path(settings.tls_cert).read_text(encoding="ascii")
        token, digest = new_token()
        payload = {
            "office_url": office_url(),
            "office_cert": cert_pem,
            "token": token,
            "fernet_keys": settings.fernet_keys,
            "auth_fernet_key": settings.auth_fernet_key,
            "label": label,
            "user": owner,
            "created": now_utc().isoformat(),
        }
        try:
            sealed = seal(payload, passphrase)
        except PairingError as exc:
            return await pairing_page(request, user, error=str(exc), status=422)
        async with auth.tx(user) as conn:
            replica_id = (
                await conn.execute(
                    text(
                        "INSERT INTO replicas (label, token_hash, created_by, user_id) "
                        "VALUES (:l, :h, :u, :o) RETURNING id"
                    ),
                    {"l": label, "h": digest, "u": user.id, "o": owner_id},
                )
            ).scalar_one()
            await write_audit(
                conn, t.audit_log, action="replica_paired", actor_id=user.id,
                entity_type="replica", entity_id=replica_id, detail={"label": label},
            )  # fmt: skip
        return Response(
            sealed,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="nil-klinik-eslestirme.json"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/dizustu/{replica_id}/iptal")
    async def revoke(replica_id: UUID, user: CurrentUser = admin) -> Response:
        async with auth.tx(user) as conn:
            await conn.execute(
                text(
                    "UPDATE replicas SET revoked_at = now() WHERE id = :id AND revoked_at IS NULL"
                ),
                {"id": replica_id},
            )
            await write_audit(
                conn, t.audit_log, action="replica_revoked", actor_id=user.id,
                entity_type="replica", entity_id=replica_id,
            )  # fmt: skip
        return RedirectResponse("/ayarlar/dizustu?iptal=1", status_code=303)

    # ------------------------------------------------------------ conflicts

    async def names(conn: Any) -> dict[UUID, str]:
        out: dict[UUID, str] = {}
        for r in (await conn.execute(text("SELECT id, display_name FROM users"))).all():
            out[r[0]] = r[1]
        for r in (await conn.execute(text("SELECT id, label FROM rooms"))).all():
            out[r[0]] = r[1]
        for r in (
            await conn.execute(text("SELECT id, client_code, full_name_encrypted FROM clients"))
        ).all():
            out[r[0]] = f"{cipher.decrypt(r[2])} ({r[1]})"
        return out

    def shown(value: Any, lookup: dict[UUID, str]) -> str:
        if value is None:
            return "—"
        if isinstance(value, bytes):
            try:
                return cipher.decrypt(value)
            except DecryptionError:
                return "(okunamadı)"
        if isinstance(value, UUID):
            return lookup.get(value, str(value)[:8])
        if isinstance(value, datetime):
            return format_datetime_tr(value)
        if isinstance(value, date):
            return value.strftime("%d.%m.%Y")
        if isinstance(value, bool):
            return "Evet" if value else "Hayır"
        if isinstance(value, str) and value in STATUS_LABELS:
            return STATUS_LABELS[value]
        return str(value)

    def decode(raw: Any) -> dict[str, Any]:
        if raw is None:
            return {}
        data = raw if isinstance(raw, dict) else json.loads(raw)
        return {k: store.decode_value(v) for k, v in data.items() if k != "__methods"}

    @router.get("/cakismalar", response_class=HTMLResponse)
    async def conflicts(request: Request, user: CurrentUser = admin) -> Response:
        async with sync_engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.*, r.label AS replica FROM replica_conflicts c "
                        "LEFT JOIN replicas r ON r.id = c.replica_id "
                        "WHERE c.resolved_at IS NULL ORDER BY c.created_at"
                    )
                )
            ).all()
            lookup = await names(conn)
        items = []
        for c in rows:
            laptop, office = decode(c.laptop_row), decode(c.office_row)
            fields = []
            for key in [*office.keys(), *[k for k in laptop if k not in office]]:
                if key in HIDDEN:
                    continue
                a, b = office.get(key), laptop.get(key)
                if office and a == b:
                    continue
                fields.append(
                    (
                        FIELD_LABELS.get(key, key),
                        shown(a, lookup) if office else "—",
                        shown(b, lookup),
                    )
                )
            subject = laptop.get("client_id") or laptop.get("id")
            items.append(
                {
                    "id": c.id,
                    "what": TABLE_LABELS.get(c.tbl, c.tbl),
                    "subject": lookup.get(subject, "") if isinstance(subject, UUID) else "",
                    "message": c.message,
                    "kind": c.kind,
                    "replica": c.replica or "—",
                    "when": format_datetime_tr(c.created_at),
                    "fields": fields,
                    "office_exists": bool(office),
                }
            )
        return render(
            request,
            "ayarlar/cakismalar.html",
            {
                "items": items,
                "notice": {"1": "Karar kaydedildi."}.get(request.query_params.get("ok", "")),
                "error": request.query_params.get("hata"),
            },
        )

    @router.post("/cakismalar/{conflict_id}")
    async def resolve(request: Request, conflict_id: UUID, user: CurrentUser = admin) -> Response:
        choice = str((await request.form()).get("karar") or "")
        if choice not in ("kept_office", "applied_laptop"):
            return RedirectResponse("/ayarlar/cakismalar", status_code=303)
        error = None
        async with sync_engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT * FROM replica_conflicts WHERE id = :id AND resolved_at IS NULL "
                        "FOR UPDATE"
                    ),
                    {"id": conflict_id},
                )
            ).one_or_none()
            if row is None:
                return RedirectResponse("/ayarlar/cakismalar", status_code=303)
            if choice == "applied_laptop":
                error = await _apply_laptop(conn, row)
            if error is None:
                await conn.execute(
                    text(
                        "UPDATE replica_conflicts SET resolved_at = now(), resolved_by = :u, "
                        "resolution = :r WHERE id = :id"
                    ),
                    {"u": user.id, "r": choice, "id": conflict_id},
                )
                await write_audit(
                    conn, t.audit_log, action="replica_conflict_resolved", actor_id=user.id,
                    entity_type=row.tbl, entity_id=row.row_id, detail={"resolution": choice},
                )  # fmt: skip
        if error:
            from urllib.parse import urlencode

            return RedirectResponse(
                f"/ayarlar/cakismalar?{urlencode({'hata': error})}", status_code=303
            )
        return RedirectResponse("/ayarlar/cakismalar?ok=1", status_code=303)

    async def _apply_laptop(conn: Any, conflict: Any) -> str | None:
        raw = (
            conflict.laptop_row
            if isinstance(conflict.laptop_row, dict)
            else json.loads(conflict.laptop_row)
        )
        methods = [store.decode_row(m) for m in raw.get("__methods") or []]
        row = store.decode_row({k: v for k, v in raw.items() if k != "__methods"})
        table = conflict.tbl
        try:
            async with conn.begin_nested():
                current = await store.select_rows(
                    conn, table, "WHERE id = :id FOR UPDATE", {"id": row["id"]}
                )
                if current:
                    if "version" in row and "version" in current[0]:
                        row["version"] = int(current[0]["version"]) + 1
                    await store.update_row(conn, table, row)
                else:
                    await store.insert_row(conn, table, row)
                if table == "therapy_sessions":
                    await store.replace_methods(conn, row["id"], methods)
        except IntegrityError:
            if table == "appointments":
                return "Randevu hâlâ başka bir randevuyla çakışıyor. Takvimden yeni bir saat verin."
            return "Bağlı bilgisayardaki hali uygulanamadı (kayıt kurallarına uymuyor)."
        return None

    return router


def build_laptop_router(auth: AuthKit, sync: ReplicaSync, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter(prefix="/ayarlar", include_in_schema=False)
    logged_in = Depends(auth.require())

    @router.get("", include_in_schema=False)
    async def index(user: CurrentUser = logged_in) -> Response:
        return RedirectResponse("/ayarlar/esitleme", status_code=303)

    @router.get("/esitleme", response_class=HTMLResponse)
    async def status(request: Request, user: CurrentUser = logged_in) -> Response:
        response = templates.TemplateResponse(
            request,
            "ayarlar/esitleme.html",
            {"s": sync.status, "office_url": sync.office_url, "fmt": format_datetime_tr},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/esitleme")
    async def sync_now(user: CurrentUser = logged_in) -> Response:
        await sync.run_once()
        return RedirectResponse("/ayarlar/esitleme", status_code=303)

    return router
