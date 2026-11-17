"""Terapiler: the session log (TERAPILER_TAB §3, §5, §7).

Rules enforced here (and again by the database where it can):
- notes and homework are Fernet-encrypted before they are stored and are decrypted
  only by ``get_detail``, which writes an audit row every time;
- list, count, timeline and export queries never touch the encrypted columns;
- age group, session number and therapist are decided by the server, never the caller;
- a therapist only ever sees their own sessions (row-level security is the backstop).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_local.randevular.service import set_outcome
from klinik_local.terapiler.catalog import AgeGroup, Catalog, Method, NoAgeGroupError, load_catalog
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.enums import AppointmentStatus, MethodKind
from klinik_shared.permissions import Permission
from klinik_shared.textutil import fold_tr
from klinik_shared.timeutil import istanbul_day_bounds, now_utc, to_istanbul

MAX_TEXT = 20_000
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
TIMELINE_LIMIT = 10


# ---------------------------------------------------------------- errors


def outcome_for(session_status: str) -> AppointmentStatus:
    """What the linked appointment should show for a session in this state."""
    if session_status in (AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW):
        return AppointmentStatus(session_status)
    return AppointmentStatus.PLANNED


class SessionError(Exception):
    """A rule violation the user can fix. ``message`` is Turkish and safe to display."""

    def __init__(self, field: str, message: str, code: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message
        self.code = code


class SessionNotFound(Exception):
    pass


class SessionVersionConflict(Exception):
    pass


# ---------------------------------------------------------------- input models


class _Strict(BaseModel):
    # extra="forbid" is what rejects server-owned fields (age_group_id_at_session,
    # session_number, therapist_id) when a client tries to send them.
    model_config = ConfigDict(extra="forbid")


class MethodRef(_Strict):
    code: str = Field(min_length=1, max_length=60)
    is_primary: bool = False


class SessionCreate(_Strict):
    id: UUID | None = None  # client-generated ids are accepted (offline-ready design)
    client_id: UUID
    session_date: AwareDatetime
    duration_min: int = Field(default=50, ge=10, le=240)
    format_code: str = Field(min_length=1, max_length=60)
    methods: list[MethodRef] = Field(default_factory=list, max_length=20)
    status: AppointmentStatus | None = None
    notes: str | None = Field(default=None, max_length=MAX_TEXT)
    homework: str | None = Field(default=None, max_length=MAX_TEXT)
    next_session_date: AwareDatetime | None = None
    appointment_id: UUID | None = None


class SessionUpdate(_Strict):
    """PATCH: only the fields present in the request are changed."""

    session_date: AwareDatetime | None = None
    duration_min: int | None = Field(default=None, ge=10, le=240)
    format_code: str | None = Field(default=None, min_length=1, max_length=60)
    methods: list[MethodRef] | None = Field(default=None, max_length=20)
    status: AppointmentStatus | None = None
    notes: str | None = Field(default=None, max_length=MAX_TEXT)
    homework: str | None = Field(default=None, max_length=MAX_TEXT)
    next_session_date: AwareDatetime | None = None
    version: int | None = None  # optimistic lock: the version the editor loaded


# ---------------------------------------------------------------- output


@dataclass(frozen=True)
class MethodItem:
    code: str
    label_tr: str
    is_primary: bool


@dataclass(frozen=True)
class SessionItem:
    id: UUID
    client_id: UUID
    client_code: str
    age_at_session: int | None
    age_group_code: str
    age_group_label: str
    session_number: int
    session_date: datetime
    duration_min: int
    format_code: str
    format_label: str
    status: str
    therapist_id: UUID
    therapist_name: str
    version: int
    appointment_id: UUID | None
    next_session_date: datetime | None
    methods: list[MethodItem] = field(default_factory=list)
    # Filled only for server-rendered pages inside the office; never serialised to JSON.
    client_name: str | None = None

    @property
    def primary_method(self) -> MethodItem | None:
        return next((m for m in self.methods if m.is_primary), None)

    def to_json(self) -> dict[str, Any]:
        """The list/detail shape of TERAPILER_TAB §7. No name, no notes."""
        return {
            "id": str(self.id),
            "client": {
                "id": str(self.client_id),
                "code": self.client_code,
                "age_at_session": self.age_at_session,
            },
            "age_group": {"code": self.age_group_code, "label_tr": self.age_group_label},
            "session_number": self.session_number,
            "session_date": to_istanbul(self.session_date).isoformat(),
            "duration_min": self.duration_min,
            "format": {"code": self.format_code, "label_tr": self.format_label},
            "methods": [
                {"code": m.code, "label_tr": m.label_tr, "is_primary": m.is_primary}
                for m in self.methods
            ],
            "status": self.status,
            "therapist": {"id": str(self.therapist_id), "display_name": self.therapist_name},
            "version": self.version,
            "next_session_date": (
                to_istanbul(self.next_session_date).isoformat() if self.next_session_date else None
            ),
        }


@dataclass(frozen=True)
class SessionDetail:
    item: SessionItem
    notes: str | None
    homework: str | None

    def to_json(self) -> dict[str, Any]:
        return {**self.item.to_json(), "notes": self.notes, "homework": self.homework}


@dataclass(frozen=True)
class SessionFilters:
    age_group: str | None = None
    methods: tuple[str, ...] = ()
    format: str | None = None
    therapist_id: UUID | None = None
    statuses: tuple[str, ...] = ()
    date_from: date | None = None
    date_to: date | None = None
    q: str = ""
    sort: str = "-session_date"
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    def as_audit_detail(self) -> dict[str, Any]:
        """Filter set for the export audit entry (codes and dates only, no search text)."""
        return {
            "age_group": self.age_group,
            "methods": list(self.methods),
            "format": self.format,
            "statuses": list(self.statuses),
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "has_search": bool(self.q.strip()),
        }


@dataclass(frozen=True)
class SessionPage:
    items: list[SessionItem]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size))

    @property
    def first(self) -> int:
        return 0 if self.total == 0 else (self.page - 1) * self.page_size + 1

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.page_size)


# ---------------------------------------------------------------- SQL fragments

# Never selects notes_encrypted / homework_encrypted.
_SESSION_DAY = "(s.session_date AT TIME ZONE 'Europe/Istanbul')::date"
_AGE = f"date_part('year', age({_SESSION_DAY}, c.birth_date))::int"
_COLUMNS = f"""
    s.id, s.client_id, c.client_code, {_AGE} AS age_at_session,
    ag.code AS age_group_code, ag.label_tr AS age_group_label,
    s.session_number, s.session_date, s.duration_min,
    f.code AS format_code, f.label_tr AS format_label,
    s.status, s.therapist_id, u.display_name AS therapist_name,
    s.version, s.appointment_id, s.next_session_date
"""
_FROM = """
    FROM therapy_sessions s
    JOIN clients c ON c.id = s.client_id
    JOIN age_groups ag ON ag.id = s.age_group_id_at_session
    JOIN therapy_methods f ON f.id = s.format_method_id
    JOIN users u ON u.id = s.therapist_id
"""
_SORTS = {
    "session_date": "s.session_date",
    "client": "c.client_code",
    "age": _AGE,
    "session_number": "s.session_number",
    "format": "f.label_tr",
    "duration_min": "s.duration_min",
    "therapist": "u.display_name",
    "status": "s.status",
}
SORT_KEYS = tuple(_SORTS)


def _order_by(sort: str) -> str:
    descending = sort.startswith("-")
    column = _SORTS.get(sort.lstrip("-"), _SORTS["session_date"])
    direction = "DESC" if descending else "ASC"
    return f"ORDER BY {column} {direction} NULLS LAST, s.id"


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SessionService:
    def __init__(self, cipher: FieldCipher) -> None:
        self.cipher = cipher

    # ------------------------------------------------------------ reading

    async def _name_matches(self, conn: AsyncConnection, query: str) -> list[UUID]:
        """Clients whose (decrypted) name contains the query. Runs server-side only."""
        needle = fold_tr(" ".join(query.split()))
        rows = (await conn.execute(select(t.clients.c.id, t.clients.c.full_name_encrypted))).all()
        return [r.id for r in rows if needle in fold_tr(self.cipher.decrypt(r.full_name_encrypted))]

    async def _where(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        filters: SessionFilters,
        *,
        with_age_group: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        clauses = ["s.deleted_at IS NULL"]
        params: dict[str, Any] = {}
        if with_age_group and filters.age_group:
            clauses.append("ag.code = :age_group")
            params["age_group"] = filters.age_group
        if filters.methods:
            clauses.append(
                "EXISTS (SELECT 1 FROM session_methods sm "
                "JOIN therapy_methods m ON m.id = sm.therapy_method_id "
                "WHERE sm.session_id = s.id AND m.code = ANY(CAST(:methods AS text[])))"
            )
            params["methods"] = list(filters.methods)
        if filters.format:
            clauses.append("f.code = :format")
            params["format"] = filters.format
        if filters.therapist_id and actor.has(Permission.SESSIONS_READ_OTHERS):
            clauses.append("s.therapist_id = :therapist_id")
            params["therapist_id"] = filters.therapist_id
        if filters.statuses:
            clauses.append("s.status = ANY(CAST(:statuses AS text[]))")
            params["statuses"] = list(filters.statuses)
        if filters.date_from:
            clauses.append("s.session_date >= :date_from")
            params["date_from"] = istanbul_day_bounds(filters.date_from)[0]
        if filters.date_to:
            clauses.append("s.session_date < :date_to")
            params["date_to"] = istanbul_day_bounds(filters.date_to)[1]
        query = filters.q.strip()
        if query:
            clauses.append("(c.client_code ILIKE :q_like OR c.id = ANY(CAST(:name_ids AS uuid[])))")
            params["q_like"] = _like(query)
            params["name_ids"] = await self._name_matches(conn, query)
        return "WHERE " + " AND ".join(clauses), params

    async def _attach_methods(
        self, conn: AsyncConnection, items: list[SessionItem]
    ) -> list[SessionItem]:
        if not items:
            return items
        rows = (
            await conn.execute(
                text(
                    "SELECT sm.session_id, m.code, m.label_tr, sm.is_primary "
                    "FROM session_methods sm JOIN therapy_methods m ON m.id = sm.therapy_method_id "
                    "WHERE sm.session_id = ANY(CAST(:ids AS uuid[])) "
                    "ORDER BY sm.is_primary DESC, m.label_tr"
                ),
                {"ids": [item.id for item in items]},
            )
        ).all()
        by_session: dict[UUID, list[MethodItem]] = {}
        for r in rows:
            by_session.setdefault(r.session_id, []).append(
                MethodItem(r.code, r.label_tr, r.is_primary)
            )
        return [replace(item, methods=by_session.get(item.id, [])) for item in items]

    @staticmethod
    def _item(row: Any) -> SessionItem:
        return SessionItem(
            id=row.id,
            client_id=row.client_id,
            client_code=row.client_code,
            age_at_session=row.age_at_session,
            age_group_code=row.age_group_code,
            age_group_label=row.age_group_label,
            session_number=row.session_number,
            session_date=row.session_date,
            duration_min=row.duration_min,
            format_code=row.format_code,
            format_label=row.format_label,
            status=row.status,
            therapist_id=row.therapist_id,
            therapist_name=row.therapist_name,
            version=row.version,
            appointment_id=row.appointment_id,
            next_session_date=row.next_session_date,
        )

    async def list_page(
        self, conn: AsyncConnection, actor: CurrentUser, filters: SessionFilters
    ) -> SessionPage:
        """One page of sessions. Filtering, sorting and paging all happen in SQL."""
        where, params = await self._where(conn, actor, filters)
        page_size = max(1, min(filters.page_size, MAX_PAGE_SIZE))
        total = (await conn.execute(text(f"SELECT count(*) {_FROM} {where}"), params)).scalar_one()
        pages = max(1, -(-total // page_size))
        page = max(1, min(filters.page, pages))
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_COLUMNS} {_FROM} {where} {_order_by(filters.sort)} "
                    "LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            )
        ).all()
        items = await self._attach_methods(conn, [self._item(r) for r in rows])
        return SessionPage(items, total, page, page_size)

    async def counts(
        self, conn: AsyncConnection, actor: CurrentUser, filters: SessionFilters
    ) -> dict[str, int]:
        """Badge counts per age group for the current filters, ignoring the tab itself."""
        where, params = await self._where(conn, actor, filters, with_age_group=False)
        rows = (
            await conn.execute(
                text(f"SELECT ag.code, count(*) AS n {_FROM} {where} GROUP BY ag.code"),
                params,
            )
        ).all()
        result = {r.code: r.n for r in rows}
        result["all"] = sum(result.values())
        return result

    async def export_rows(
        self, conn: AsyncConnection, actor: CurrentUser, filters: SessionFilters
    ) -> list[SessionItem]:
        """Every row of the current filtered view (no paging). Never notes or names."""
        where, params = await self._where(conn, actor, filters)
        rows = (
            await conn.execute(
                text(f"SELECT {_COLUMNS} {_FROM} {where} {_order_by(filters.sort)}"),
                params,
            )
        ).all()
        items = await self._attach_methods(conn, [self._item(r) for r in rows])
        await write_audit(
            conn,
            t.audit_log,
            action="export",
            actor_id=actor.id,
            entity_type="therapy_sessions",
            detail={"rows": len(items), **filters.as_audit_detail()},
        )
        return items

    async def timeline(
        self, conn: AsyncConnection, client_id: UUID, limit: int = TIMELINE_LIMIT
    ) -> list[SessionItem]:
        """A client's most recent sessions across all age groups."""
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_COLUMNS} {_FROM} "
                    "WHERE s.deleted_at IS NULL AND s.client_id = :client_id "
                    "ORDER BY s.session_date DESC, s.id LIMIT :limit"
                ),
                {"client_id": client_id, "limit": max(1, min(limit, 100))},
            )
        ).all()
        return await self._attach_methods(conn, [self._item(r) for r in rows])

    async def _load_item(self, conn: AsyncConnection, session_id: UUID) -> SessionItem:
        row = (
            await conn.execute(
                text(f"SELECT {_COLUMNS} {_FROM} WHERE s.deleted_at IS NULL AND s.id = :id"),
                {"id": session_id},
            )
        ).one_or_none()
        if row is None:
            raise SessionNotFound
        return (await self._attach_methods(conn, [self._item(row)]))[0]

    async def get_item(self, conn: AsyncConnection, session_id: UUID) -> SessionItem:
        """Session without clinical text (for edit forms that load notes separately)."""
        return await self._load_item(conn, session_id)

    async def get_detail(
        self, conn: AsyncConnection, actor: CurrentUser, session_id: UUID
    ) -> SessionDetail:
        """Session WITH decrypted notes. Every call is written to the audit log."""
        item = await self._load_item(conn, session_id)
        s = t.therapy_sessions
        row = (
            await conn.execute(
                select(s.c.notes_encrypted, s.c.homework_encrypted).where(s.c.id == session_id)
            )
        ).one()
        await write_audit(
            conn,
            t.audit_log,
            action="notes_read",
            actor_id=actor.id,
            entity_type="therapy_session",
            entity_id=session_id,
        )
        return SessionDetail(
            item,
            self.cipher.decrypt_optional(row.notes_encrypted),
            self.cipher.decrypt_optional(row.homework_encrypted),
        )

    # ------------------------------------------------------------ validation helpers

    @staticmethod
    def _format(catalog: Catalog, code: str, *, keep: str | None = None) -> Method:
        method = catalog.methods.get(code)
        if method is None or method.kind != MethodKind.FORMAT.value:
            raise SessionError("format_code", "Geçersiz seans formatı.", "invalid_format")
        if not method.is_active and code != keep:
            raise SessionError("format_code", "Bu format artık kullanılmıyor.", "inactive_format")
        return method

    @staticmethod
    def _modalities(
        catalog: Catalog, refs: Sequence[MethodRef], *, keep: frozenset[str] = frozenset()
    ) -> list[tuple[Method, bool]]:
        """Validated (method, is_primary) pairs with exactly one primary if any are given."""
        seen: set[str] = set()
        resolved: list[tuple[Method, bool]] = []
        for ref in refs:
            method = catalog.methods.get(ref.code)
            if method is None or method.kind != MethodKind.MODALITY.value:
                raise SessionError("methods", f"Geçersiz yöntem: {ref.code}", "invalid_method")
            if not method.is_active and ref.code not in keep:
                raise SessionError(
                    "methods", f"{method.label_tr} artık kullanılmıyor.", "inactive_method"
                )
            if ref.code in seen:
                raise SessionError("methods", "Aynı yöntem iki kez seçilemez.", "duplicate_method")
            seen.add(ref.code)
            resolved.append((method, ref.is_primary))
        primaries = sum(1 for _, primary in resolved if primary)
        if primaries > 1:
            raise SessionError(
                "methods", "Yalnızca bir yöntem birincil olabilir.", "multiple_primary"
            )
        if resolved and primaries == 0:
            first, _ = resolved[0]
            resolved[0] = (first, True)  # the first selected method is the primary one
        return resolved

    @staticmethod
    def _require_modality_when_completed(status: str, count: int) -> None:
        if status == AppointmentStatus.COMPLETED.value and count == 0:
            raise SessionError(
                "methods", "Tamamlanan seans için en az bir yöntem seçin.", "methods_required"
            )

    @staticmethod
    def _age_group(catalog: Catalog, birth_date: date | None, session_date: datetime) -> AgeGroup:
        if birth_date is None:
            raise SessionError(
                "client_id",
                "Danışanın doğum tarihi eksik. Önce danışan profilini güncelleyin.",
                "birth_date_missing",
            )
        at = to_istanbul(session_date).date()
        if at < birth_date:
            raise SessionError(
                "session_date", "Seans tarihi doğum tarihinden önce olamaz.", "before_birth"
            )
        try:
            return catalog.resolve(birth_date, at)
        except NoAgeGroupError:
            raise SessionError(
                "client_id", "Bu yaş için tanımlı bir yaş grubu yok.", "no_age_group"
            ) from None

    async def _replace_methods(
        self, conn: AsyncConnection, session_id: UUID, methods: list[tuple[Method, bool]]
    ) -> None:
        sm = t.session_methods
        await conn.execute(delete(sm).where(sm.c.session_id == session_id))
        if methods:
            await conn.execute(
                insert(sm),
                [
                    {"session_id": session_id, "therapy_method_id": m.id, "is_primary": primary}
                    for m, primary in methods
                ],
            )

    # ------------------------------------------------------------ writing

    async def create(
        self, conn: AsyncConnection, actor: CurrentUser, data: SessionCreate
    ) -> SessionItem:
        catalog = await load_catalog(conn)
        client = (
            await conn.execute(
                select(t.clients.c.id, t.clients.c.client_code, t.clients.c.birth_date).where(
                    t.clients.c.id == data.client_id, t.clients.c.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if client is None:
            raise SessionError("client_id", "Danışan bulunamadı.", "client_not_found")

        group = self._age_group(catalog, client.birth_date, data.session_date)
        format_method = self._format(catalog, data.format_code)
        methods = self._modalities(catalog, data.methods)
        status = data.status or (
            AppointmentStatus.PLANNED
            if data.session_date > now_utc()
            else AppointmentStatus.COMPLETED
        )
        self._require_modality_when_completed(status.value, len(methods))
        await self._check_appointment(conn, actor, data.appointment_id, client.id)

        number = (
            await conn.execute(
                text("SELECT next_session_number(:client_id)"), {"client_id": client.id}
            )
        ).scalar_one()

        values: dict[str, Any] = {
            "client_id": client.id,
            "therapist_id": actor.id,  # always the author; never taken from the request
            "age_group_id_at_session": group.id,
            "format_method_id": format_method.id,
            "session_number": number,
            "session_date": data.session_date,
            "duration_min": data.duration_min,
            "status": status.value,
            "notes_encrypted": self.cipher.encrypt_optional(data.notes),
            "homework_encrypted": self.cipher.encrypt_optional(data.homework),
            "next_session_date": data.next_session_date,
            "appointment_id": data.appointment_id,
            "created_by": actor.id,
            "updated_by": actor.id,
        }
        if data.id is not None:
            values["id"] = data.id
        s = t.therapy_sessions
        session_id = (await conn.execute(insert(s).values(**values).returning(s.c.id))).scalar_one()
        await self._replace_methods(conn, session_id, methods)
        await write_audit(
            conn,
            t.audit_log,
            action="session_created",
            actor_id=actor.id,
            entity_type="therapy_session",
            entity_id=session_id,
            detail={
                "client_code": client.client_code,
                "session_number": number,
                "status": status.value,
                "has_notes": bool(data.notes),
                "has_homework": bool(data.homework),
            },
        )
        if data.appointment_id is not None:
            await set_outcome(conn, actor, data.appointment_id, outcome_for(status.value))
        return await self._load_item(conn, session_id)

    async def _check_appointment(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        appointment_id: UUID | None,
        client_id: UUID,
    ) -> AppointmentStatus | None:
        """Validate a link to an appointment; returns the appointment's status."""
        if appointment_id is None:
            return None
        a = t.appointments
        row = (
            await conn.execute(
                select(a.c.client_id, a.c.therapist_id, a.c.status).where(
                    a.c.id == appointment_id, a.c.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if row is None:
            raise SessionError("appointment_id", "Randevu bulunamadı.", "unknown_appointment")
        if row.client_id != client_id:
            raise SessionError(
                "appointment_id", "Randevu bu danışana ait değil.", "appointment_client_mismatch"
            )
        if row.therapist_id != actor.id:
            raise SessionError(
                "appointment_id", "Bu randevu başka bir terapiste ait.", "appointment_not_yours"
            )
        if row.status == AppointmentStatus.CANCELLED.value:
            raise SessionError(
                "appointment_id", "Bu randevu iptal edilmiş.", "appointment_cancelled"
            )
        taken = (
            await conn.execute(
                text(
                    "SELECT 1 FROM therapy_sessions "
                    "WHERE appointment_id = :id AND deleted_at IS NULL"
                ),
                {"id": appointment_id},
            )
        ).first()
        if taken is not None:
            raise SessionError(
                "appointment_id", "Bu randevu için zaten bir seans kaydı var.", "appointment_taken"
            )
        return AppointmentStatus(row.status)

    async def update(
        self, conn: AsyncConnection, actor: CurrentUser, session_id: UUID, data: SessionUpdate
    ) -> SessionItem:
        s = t.therapy_sessions
        row = (
            await conn.execute(
                select(s).where(s.c.id == session_id, s.c.deleted_at.is_(None)).with_for_update()
            )
        ).one_or_none()
        # Someone else's session is "not found", not "forbidden": its existence is not revealed.
        if row is None or row.therapist_id != actor.id:
            raise SessionNotFound
        if data.version is not None and data.version != row.version:
            raise SessionVersionConflict

        sent = data.model_fields_set - {"version"}
        catalog = await load_catalog(conn)
        current = await self._load_item(conn, session_id)
        values: dict[str, Any] = {}
        changed: list[str] = []

        if (
            "session_date" in sent
            and data.session_date is not None
            and data.session_date != row.session_date
        ):
            birth_date = (
                await conn.execute(
                    select(t.clients.c.birth_date).where(t.clients.c.id == row.client_id)
                )
            ).scalar_one()
            # Moving the date can move the client into another age group: re-snapshot.
            group = self._age_group(catalog, birth_date, data.session_date)
            values["session_date"] = data.session_date
            values["age_group_id_at_session"] = group.id
            changed.append("session_date")
        if (
            "duration_min" in sent
            and data.duration_min is not None
            and data.duration_min != row.duration_min
        ):
            values["duration_min"] = data.duration_min
            changed.append("duration_min")
        if "format_code" in sent and data.format_code is not None:
            format_method = self._format(catalog, data.format_code, keep=current.format_code)
            if format_method.id != row.format_method_id:
                values["format_method_id"] = format_method.id
                changed.append("format")
        status = row.status
        if "status" in sent and data.status is not None and data.status.value != row.status:
            status = data.status.value
            values["status"] = status
            changed.append("status")
        for name in ("notes", "homework"):
            if name in sent:
                # The audit entry says THAT the text changed, never what it says.
                values[f"{name}_encrypted"] = self.cipher.encrypt_optional(getattr(data, name))
                changed.append(name)
        if "next_session_date" in sent and data.next_session_date != row.next_session_date:
            values["next_session_date"] = data.next_session_date
            changed.append("next_session_date")

        methods: list[tuple[Method, bool]] | None = None
        method_count = len(current.methods)
        if "methods" in sent and data.methods is not None:
            keep = frozenset(m.code for m in current.methods)
            methods = self._modalities(catalog, data.methods, keep=keep)
            method_count = len(methods)
            before = {(m.code, m.is_primary) for m in current.methods}
            if {(m.code, primary) for m, primary in methods} != before:
                changed.append("methods")
            else:
                methods = None
        self._require_modality_when_completed(status, method_count)

        if not changed:
            return current
        await conn.execute(
            update(s)
            .where(s.c.id == session_id)
            .values(**values, updated_by=actor.id, version=s.c.version + 1)
        )
        if methods is not None:
            await self._replace_methods(conn, session_id, methods)
        if row.appointment_id is not None and "status" in changed:
            await set_outcome(conn, actor, row.appointment_id, outcome_for(status))
        await write_audit(
            conn,
            t.audit_log,
            action="session_updated",
            actor_id=actor.id,
            entity_type="therapy_session",
            entity_id=session_id,
            detail={"changed": changed},
        )
        return await self._load_item(conn, session_id)

    async def soft_delete(
        self, conn: AsyncConnection, actor: CurrentUser, session_id: UUID
    ) -> None:
        s = t.therapy_sessions
        deleted = (
            await conn.execute(
                update(s)
                .where(s.c.id == session_id, s.c.deleted_at.is_(None), s.c.therapist_id == actor.id)
                .values(deleted_at=now_utc(), updated_by=actor.id, version=s.c.version + 1)
                .returning(s.c.id, s.c.appointment_id, s.c.status)
            )
        ).first()
        if deleted is None:
            raise SessionNotFound
        if deleted.appointment_id is not None:
            # The record of the outcome is gone, so the booking goes back to planned.
            await set_outcome(conn, actor, deleted.appointment_id, AppointmentStatus.PLANNED)
        await write_audit(
            conn,
            t.audit_log,
            action="session_deleted",
            actor_id=actor.id,
            entity_type="therapy_session",
            entity_id=session_id,
        )

    async def link_candidates(
        self, conn: AsyncConnection, actor: CurrentUser, session_id: UUID
    ) -> list[Any]:
        """The therapist's own bookings for this client, near the session date, still free."""
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id, m.starts_at, m.duration_min, m.status FROM therapy_sessions s "
                    "JOIN appointments m ON m.client_id = s.client_id "
                    "WHERE s.id = :sid AND s.appointment_id IS NULL AND s.deleted_at IS NULL "
                    "AND m.therapist_id = :me AND m.deleted_at IS NULL "
                    "AND m.status <> 'cancelled' "
                    "AND m.starts_at BETWEEN s.session_date - interval '14 days' "
                    "AND s.session_date + interval '14 days' "
                    "AND NOT EXISTS (SELECT 1 FROM therapy_sessions o "
                    "  WHERE o.appointment_id = m.id AND o.deleted_at IS NULL) "
                    "ORDER BY abs(extract(epoch FROM m.starts_at - s.session_date))"
                ),
                {"sid": session_id, "me": actor.id},
            )
        ).all()
        return list(rows)

    async def link_appointment(
        self, conn: AsyncConnection, actor: CurrentUser, session_id: UUID, appointment_id: UUID
    ) -> SessionItem:
        """Attach a walk-in session to a booking made afterwards (e.g. so it can be paid)."""
        s = t.therapy_sessions
        row = (
            await conn.execute(
                select(s).where(s.c.id == session_id, s.c.deleted_at.is_(None)).with_for_update()
            )
        ).one_or_none()
        if row is None or row.therapist_id != actor.id:
            raise SessionNotFound
        if row.appointment_id is not None:
            raise SessionError(
                "appointment_id", "Bu seans zaten bir randevuya bağlı.", "already_linked"
            )
        await self._check_appointment(conn, actor, appointment_id, row.client_id)
        await conn.execute(
            update(s)
            .where(s.c.id == session_id)
            .values(appointment_id=appointment_id, updated_by=actor.id, version=s.c.version + 1)
        )
        await set_outcome(conn, actor, appointment_id, outcome_for(row.status))
        await write_audit(
            conn,
            t.audit_log,
            action="session_linked",
            actor_id=actor.id,
            entity_type="therapy_session",
            entity_id=session_id,
            detail={"appointment_id": str(appointment_id)},
        )
        return await self._load_item(conn, session_id)

    # ------------------------------------------------------------ UI helpers

    @staticmethod
    def method_warnings(catalog: Catalog, group: AgeGroup, codes: Sequence[str]) -> list[str]:
        """Labels of selected methods that are not defaults of the age group (warn, never block)."""
        labels = []
        for code in codes:
            method = catalog.methods.get(code)
            if method is not None and not catalog.is_default(group, method):
                labels.append(method.label_tr)
        return labels
