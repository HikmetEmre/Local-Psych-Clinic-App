"""Danışanlar (clients): identity lives here and never leaves the office.

Names and contact details are Fernet-encrypted in this layer. The audit log
records which fields changed, never their values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.client_code import format_client_code
from klinik_shared.permissions import Permission
from klinik_shared.textutil import fold_tr
from klinik_shared.timeutil import age_on, now_utc, to_istanbul

MAX_NAME = 120
MAX_PHONE = 30
MAX_EMAIL = 120
EARLIEST_BIRTH = date(1900, 1, 1)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"[0-9+()\-\s]{7,30}")


class ClientNotFound(Exception):
    pass


class VersionConflict(Exception):
    """Someone else changed the record after it was loaded."""


@dataclass(frozen=True)
class ClientInput:
    full_name: str
    phone: str | None
    email: str | None
    birth_date: date | None
    assigned_therapist_id: UUID | None


@dataclass(frozen=True)
class ClientView:
    id: UUID
    client_code: str
    full_name: str
    phone: str | None
    email: str | None
    birth_date: date | None
    assigned_therapist_id: UUID | None
    is_active: bool
    version: int

    @property
    def age(self) -> int | None:
        if self.birth_date is None:
            return None
        return age_on(self.birth_date, today_istanbul())


@dataclass(frozen=True)
class Therapist:
    id: UUID
    display_name: str


def today_istanbul() -> date:
    return to_istanbul(now_utc()).date()


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def parse_client_form(
    form: Mapping[str, object], therapist_ids: set[UUID]
) -> tuple[ClientInput | None, dict[str, str]]:
    """Validate a submitted form. Returns (input, errors); input is None if there are errors."""
    errors: dict[str, str] = {}

    full_name = _clean(form.get("full_name"))
    if not full_name:
        errors["full_name"] = "Ad soyad zorunludur."
    elif len(full_name) > MAX_NAME:
        errors["full_name"] = f"Ad soyad en fazla {MAX_NAME} karakter olabilir."

    phone = _clean(form.get("phone")) or None
    if phone and not _PHONE_RE.fullmatch(phone):
        errors["phone"] = "Telefon numarası geçersiz."

    email = _clean(form.get("email")).lower() or None
    if email and (len(email) > MAX_EMAIL or not _EMAIL_RE.fullmatch(email)):
        errors["email"] = "E-posta adresi geçersiz."

    birth_date: date | None = None
    raw_birth = _clean(form.get("birth_date"))
    if raw_birth:
        try:
            birth_date = date.fromisoformat(raw_birth)
        except ValueError:
            errors["birth_date"] = "Doğum tarihi geçersiz."
        else:
            if birth_date > today_istanbul():
                errors["birth_date"] = "Doğum tarihi gelecekte olamaz."
            elif birth_date < EARLIEST_BIRTH:
                errors["birth_date"] = "Doğum tarihi geçersiz."

    assigned: UUID | None = None
    raw_assigned = _clean(form.get("assigned_therapist_id"))
    if raw_assigned:
        try:
            assigned = UUID(raw_assigned)
        except ValueError:
            assigned = None
        if assigned not in therapist_ids:
            errors["assigned_therapist_id"] = "Terapist seçimi geçersiz."

    if errors:
        return None, errors
    return ClientInput(full_name, phone, email, birth_date, assigned), {}


class ClientService:
    def __init__(self, cipher: FieldCipher) -> None:
        self.cipher = cipher

    # ------------------------------------------------------------ reads

    def _view(self, row: Any) -> ClientView:
        return ClientView(
            id=row.id,
            client_code=row.client_code,
            full_name=self.cipher.decrypt(row.full_name_encrypted),
            phone=self.cipher.decrypt_optional(row.phone_encrypted),
            email=self.cipher.decrypt_optional(row.email_encrypted),
            birth_date=row.birth_date,
            assigned_therapist_id=row.assigned_therapist_id,
            is_active=row.is_active,
            version=row.version,
        )

    async def get(self, conn: AsyncConnection, client_id: UUID) -> ClientView:
        row = (
            await conn.execute(
                select(t.clients).where(
                    t.clients.c.id == client_id, t.clients.c.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if row is None:
            raise ClientNotFound
        return self._view(row)

    async def search(
        self, conn: AsyncConnection, *, query: str = "", status: str = "active"
    ) -> list[ClientView]:
        """Match on client code or (decrypted) name.

        Fernet ciphertext cannot be indexed, so names are decrypted in memory and
        filtered here. That is fine for a clinic-sized client list.
        """
        stmt = select(t.clients).where(t.clients.c.deleted_at.is_(None))
        if status == "active":
            stmt = stmt.where(t.clients.c.is_active)
        elif status == "inactive":
            stmt = stmt.where(t.clients.c.is_active.is_(False))
        rows = (await conn.execute(stmt.order_by(t.clients.c.client_code.desc()))).all()
        views = [self._view(row) for row in rows]
        needle = fold_tr(_clean(query))
        if needle:
            views = [
                v
                for v in views
                if needle in fold_tr(v.client_code) or needle in fold_tr(v.full_name)
            ]
        return views

    async def therapists(self, conn: AsyncConnection) -> list[Therapist]:
        """Active users who can run sessions (candidates for 'assigned therapist')."""
        rows = (
            await conn.execute(
                select(t.users.c.id, t.users.c.display_name)
                .join(t.user_permissions, t.user_permissions.c.user_id == t.users.c.id)
                .where(
                    t.users.c.is_active,
                    t.user_permissions.c.permission == Permission.SESSIONS_WRITE_OWN.value,
                )
                .order_by(t.users.c.display_name)
            )
        ).all()
        return [Therapist(r.id, r.display_name) for r in rows]

    # ------------------------------------------------------------ writes

    async def _next_code(self, conn: AsyncConnection) -> str:
        """Allocate the next ``DN-YYYY-NNNN``.

        The upsert locks the year's counter row until commit, so concurrent creates
        get consecutive numbers, and a rolled-back create returns its number.
        """
        year = today_istanbul().year
        counters = t.client_code_counters
        stmt = (
            pg_insert(counters)
            .values(year=year, last_n=1)
            .on_conflict_do_update(
                index_elements=[counters.c.year], set_={"last_n": counters.c.last_n + 1}
            )
            .returning(counters.c.last_n)
        )
        number = (await conn.execute(stmt)).scalar_one()
        return format_client_code(year, number)

    async def create(
        self, conn: AsyncConnection, actor: CurrentUser, data: ClientInput
    ) -> ClientView:
        code = await self._next_code(conn)
        row = (
            await conn.execute(
                pg_insert(t.clients)
                .values(
                    client_code=code,
                    full_name_encrypted=self.cipher.encrypt(data.full_name),
                    phone_encrypted=self.cipher.encrypt_optional(data.phone),
                    email_encrypted=self.cipher.encrypt_optional(data.email),
                    birth_date=data.birth_date,
                    assigned_therapist_id=data.assigned_therapist_id,
                    created_by=actor.id,
                    updated_by=actor.id,
                )
                .returning(t.clients)
            )
        ).one()
        await write_audit(
            conn,
            t.audit_log,
            action="client_created",
            actor_id=actor.id,
            entity_type="client",
            entity_id=row.id,
            detail={"client_code": code},
        )
        return self._view(row)

    async def update(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        client_id: UUID,
        data: ClientInput,
        expected_version: int,
    ) -> ClientView:
        before = await self.get(conn, client_id)
        changed = [
            field
            for field in ("full_name", "phone", "email", "birth_date", "assigned_therapist_id")
            if getattr(before, field) != getattr(data, field)
        ]
        row = (
            await conn.execute(
                update(t.clients)
                .where(t.clients.c.id == client_id, t.clients.c.version == expected_version)
                .values(
                    full_name_encrypted=self.cipher.encrypt(data.full_name),
                    phone_encrypted=self.cipher.encrypt_optional(data.phone),
                    email_encrypted=self.cipher.encrypt_optional(data.email),
                    birth_date=data.birth_date,
                    assigned_therapist_id=data.assigned_therapist_id,
                    updated_by=actor.id,
                    version=t.clients.c.version + 1,
                )
                .returning(t.clients)
            )
        ).one_or_none()
        if row is None:
            raise VersionConflict
        if changed:
            await write_audit(
                conn,
                t.audit_log,
                action="client_updated",
                actor_id=actor.id,
                entity_type="client",
                entity_id=client_id,
                detail={"changed": changed},
            )
        return self._view(row)

    async def set_active(
        self, conn: AsyncConnection, actor: CurrentUser, client_id: UUID, active: bool
    ) -> ClientView:
        before = await self.get(conn, client_id)
        if before.is_active == active:
            return before
        row = (
            await conn.execute(
                update(t.clients)
                .where(t.clients.c.id == client_id)
                .values(is_active=active, updated_by=actor.id, version=t.clients.c.version + 1)
                .returning(t.clients)
            )
        ).one()
        await write_audit(
            conn,
            t.audit_log,
            action="client_activated" if active else "client_deactivated",
            actor_id=actor.id,
            entity_type="client",
            entity_id=client_id,
        )
        return self._view(row)
