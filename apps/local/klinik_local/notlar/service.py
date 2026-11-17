"""Klinik notlar: each client's clinical log.

One timeline per client, newest first, made of
- the session records (with their notes and homework), and
- free-standing notes: phone calls, observations, intake, assessments, parent meetings,
  supervision, risk notes...

Rules:
- the text (title and body) is encrypted; the audit log records that a note was written,
  edited or deleted, never what it says;
- a note is visible to its author and to the admin (sessions.read_others); only the
  author writes it (row-level security enforces both again in the database);
- nothing is lost: before a note is edited or deleted, the previous version is stored in
  note_revisions (append-only); session note edits are kept the same way;
- edits are checked against the version the editor loaded (no silent overwrite).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.labels_tr import STATUS_LABELS
from klinik_shared.timeutil import format_datetime_tr, now_utc

KINDS = {
    "general": "Genel not",
    "phone": "Telefon görüşmesi",
    "observation": "Gözlem",
    "intake": "Ön görüşme",
    "assessment": "Değerlendirme / test",
    "parent": "Veli / aile görüşmesi",
    "supervision": "Süpervizyon",
    "risk": "Risk / güvenlik notu",
    "other": "Diğer",
}
SESSION_KIND = "session"
FILTERS = {SESSION_KIND: "Seans kayıtları", **KINDS}
MAX_TITLE = 120
MAX_BODY = 20_000
# A note dated a few minutes ahead (clock differences) is fine; later is a mistake.
_GRACE = timedelta(minutes=5)


class NoteError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class NoteNotFound(Exception):
    pass


class NoteVersionConflict(Exception):
    pass


@dataclass(frozen=True)
class Revision:
    version: int
    edited_at: datetime
    edited_by: str
    reason: str
    title: str
    body: str
    homework: str = ""


@dataclass
class Entry:
    """One line of a client's timeline: a note or a session record."""

    kind: str  # a KINDS code, or "session"
    id: UUID
    at: datetime
    author_id: UUID
    author: str
    title: str
    body: str
    homework: str = ""
    important: bool = False
    version: int = 1
    session_id: UUID | None = None
    session_number: int | None = None
    status: str | None = None
    methods: str = ""
    revisions: list[Revision] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def kind_label(self) -> str:
        if self.kind == SESSION_KIND:
            return f"Seans {self.session_number}" if self.session_number else "Seans"
        return KINDS.get(self.kind, self.kind)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status or "", "")

    def matches(self, query: str) -> bool:
        needle = query.casefold()
        return any(needle in text.casefold() for text in (self.title, self.body, self.homework))


class NoteService:
    def __init__(self, cipher: FieldCipher) -> None:
        self.cipher = cipher

    def _text(self, token: bytes | None) -> str:
        return self.cipher.decrypt_optional(token) or ""

    # ------------------------------------------------------------ reading

    async def timeline(
        self,
        conn: AsyncConnection,
        client_id: UUID,
        *,
        kind: str = "",
        query: str = "",
        important_only: bool = False,
    ) -> list[Entry]:
        """Everything the viewer may see about this client, newest first."""
        users = {
            r.id: r.display_name
            for r in (await conn.execute(select(t.users.c.id, t.users.c.display_name))).all()
        }
        entries: list[Entry] = []
        n = t.client_notes
        if kind != SESSION_KIND:
            stmt = select(n).where(n.c.client_id == client_id, n.c.deleted_at.is_(None))
            if kind:
                stmt = stmt.where(n.c.kind == kind)
            if important_only:
                stmt = stmt.where(n.c.important)
            for r in (await conn.execute(stmt)).all():
                entries.append(
                    Entry(
                        kind=r.kind, id=r.id, at=r.noted_at, author_id=r.author_id,
                        author=users.get(r.author_id, "—"), title=self._text(r.title_encrypted),
                        body=self._text(r.body_encrypted), important=r.important,
                        version=r.version, session_id=r.session_id,
                        created_at=r.created_at, updated_at=r.updated_at,
                    )
                )  # fmt: skip
        if kind in ("", SESSION_KIND) and not important_only:
            s, sm, m = t.therapy_sessions, t.session_methods, t.therapy_methods
            rows = (
                await conn.execute(
                    select(s).where(s.c.client_id == client_id, s.c.deleted_at.is_(None))
                )
            ).all()
            methods: dict[UUID, list[str]] = {}
            if rows:
                for mr in (
                    await conn.execute(
                        select(sm.c.session_id, m.c.label_tr)
                        .join(m, m.c.id == sm.c.therapy_method_id)
                        .where(sm.c.session_id.in_([r.id for r in rows]))
                        .order_by(sm.c.is_primary.desc(), m.c.label_tr)
                    )
                ).all():
                    methods.setdefault(mr.session_id, []).append(mr.label_tr)
            for r in rows:
                entries.append(
                    Entry(
                        kind=SESSION_KIND, id=r.id, at=r.session_date, author_id=r.therapist_id,
                        author=users.get(r.therapist_id, "—"), title="",
                        body=self._text(r.notes_encrypted),
                        homework=self._text(r.homework_encrypted),
                        version=r.version, session_id=r.id, session_number=r.session_number,
                        status=r.status, methods=", ".join(methods.get(r.id, [])),
                        created_at=r.created_at, updated_at=r.updated_at,
                    )
                )  # fmt: skip
        if query.strip():
            entries = [e for e in entries if e.matches(query.strip())]
        await self._attach_revisions(conn, entries, users)
        entries.sort(key=lambda e: e.at, reverse=True)
        return entries

    async def _attach_revisions(
        self, conn: AsyncConnection, entries: list[Entry], users: dict[UUID, str]
    ) -> None:
        if not entries:
            return
        r = t.note_revisions
        notes = [e.id for e in entries if e.kind != SESSION_KIND]
        sessions = [e.id for e in entries if e.kind == SESSION_KIND]
        conditions = []
        if notes:
            conditions.append(r.c.note_id.in_(notes))
        if sessions:
            conditions.append(r.c.session_id.in_(sessions))
        rows = (
            await conn.execute(select(r).where(or_(*conditions)).order_by(r.c.edited_at.desc()))
        ).all()
        by_owner: dict[UUID, list[Revision]] = {}
        for row in rows:
            owner = row.note_id or row.session_id
            by_owner.setdefault(owner, []).append(
                Revision(
                    version=row.version, edited_at=row.edited_at,
                    edited_by=users.get(row.edited_by, "—"), reason=row.reason,
                    title=self._text(row.title_encrypted), body=self._text(row.body_encrypted),
                    homework=self._text(row.homework_encrypted),
                )
            )  # fmt: skip
        for entry in entries:
            entry.revisions = by_owner.get(entry.id, [])

    async def counts(self, conn: AsyncConnection, client_id: UUID) -> dict[str, int]:
        """How many notes / session records the viewer can see (for the client page)."""
        entries = await self.timeline(conn, client_id)
        return {
            "notes": sum(1 for e in entries if e.kind != SESSION_KIND),
            "sessions": sum(1 for e in entries if e.kind == SESSION_KIND),
            "important": sum(1 for e in entries if e.important),
        }

    async def get(self, conn: AsyncConnection, note_id: UUID) -> Any:
        n = t.client_notes
        row = (
            await conn.execute(select(n).where(n.c.id == note_id, n.c.deleted_at.is_(None)))
        ).one_or_none()
        if row is None:
            raise NoteNotFound
        return row

    # ------------------------------------------------------------ writing

    def _validate(self, kind: str, title: str, body: str) -> tuple[str, str]:
        if kind not in KINDS:
            raise NoteError("kind", "Not türü seçin.")
        title = " ".join(title.split())
        body = body.strip()
        if not body:
            raise NoteError("body", "Not boş olamaz.")
        if len(title) > MAX_TITLE:
            raise NoteError("title", f"Başlık en fazla {MAX_TITLE} karakter olabilir.")
        if len(body) > MAX_BODY:
            raise NoteError("body", "Not çok uzun.")
        return title, body

    async def _check_session(
        self, conn: AsyncConnection, actor: CurrentUser, client_id: UUID, session_id: UUID | None
    ) -> None:
        if session_id is None:
            return
        s = t.therapy_sessions
        row = (
            await conn.execute(
                select(s.c.client_id, s.c.therapist_id).where(
                    s.c.id == session_id, s.c.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if row is None or row.client_id != client_id or row.therapist_id != actor.id:
            raise NoteError("session_id", "Seans seçimi geçersiz.")

    async def create(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        *,
        client_id: UUID,
        kind: str,
        title: str,
        body: str,
        noted_at: datetime | None,
        important: bool,
        session_id: UUID | None = None,
    ) -> UUID:
        title, body = self._validate(kind, title, body)
        if noted_at is not None and noted_at > now_utc() + _GRACE:
            raise NoteError("noted_at", "Not tarihi gelecekte olamaz.")
        c = t.clients
        exists = (
            await conn.execute(select(c.c.id).where(c.c.id == client_id, c.c.deleted_at.is_(None)))
        ).first()
        if exists is None:
            raise NoteError("client_id", "Danışan bulunamadı.")
        await self._check_session(conn, actor, client_id, session_id)
        n = t.client_notes
        note_id: UUID = (
            await conn.execute(
                insert(n)
                .values(
                    client_id=client_id,
                    author_id=actor.id,
                    session_id=session_id,
                    kind=kind,
                    title_encrypted=self.cipher.encrypt_optional(title or None),
                    body_encrypted=self.cipher.encrypt(body),
                    noted_at=noted_at or now_utc(),
                    important=important,
                )
                .returning(n.c.id)
            )
        ).scalar_one()
        await write_audit(
            conn, t.audit_log, action="note_created", actor_id=actor.id,
            entity_type="client_note", entity_id=note_id,
            detail={"client_id": str(client_id), "kind": kind, "important": important},
        )  # fmt: skip
        return note_id

    async def _keep(self, conn: AsyncConnection, actor: CurrentUser, row: Any, reason: str) -> None:
        await conn.execute(
            insert(t.note_revisions).values(
                note_id=row.id, version=row.version, kind=row.kind,
                title_encrypted=row.title_encrypted, body_encrypted=row.body_encrypted,
                important=row.important, noted_at=row.noted_at, reason=reason,
                edited_by=actor.id,
            )
        )  # fmt: skip

    async def _own(self, conn: AsyncConnection, actor: CurrentUser, note_id: UUID) -> Any:
        n = t.client_notes
        row = (
            await conn.execute(
                select(n).where(n.c.id == note_id, n.c.deleted_at.is_(None)).with_for_update()
            )
        ).one_or_none()
        # Someone else's note is "not found" for writing (the admin reads but does not edit).
        if row is None or row.author_id != actor.id:
            raise NoteNotFound
        return row

    async def update(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        note_id: UUID,
        *,
        kind: str,
        title: str,
        body: str,
        noted_at: datetime | None,
        important: bool,
        expected_version: int,
    ) -> bool:
        """Returns False when nothing changed."""
        row = await self._own(conn, actor, note_id)
        if row.version != expected_version:
            raise NoteVersionConflict
        title, body = self._validate(kind, title, body)
        noted_at = noted_at or row.noted_at
        same = (
            kind == row.kind
            and title == self._text(row.title_encrypted)
            and body == self._text(row.body_encrypted)
            and important == row.important
            and noted_at == row.noted_at
        )
        if same:
            return False
        await self._keep(conn, actor, row, "edited")
        n = t.client_notes
        await conn.execute(
            update(n)
            .where(n.c.id == note_id)
            .values(
                kind=kind,
                title_encrypted=self.cipher.encrypt_optional(title or None),
                body_encrypted=self.cipher.encrypt(body),
                noted_at=noted_at,
                important=important,
                version=n.c.version + 1,
            )
        )
        await write_audit(
            conn, t.audit_log, action="note_updated", actor_id=actor.id,
            entity_type="client_note", entity_id=note_id, detail={"kind": kind},
        )  # fmt: skip
        return True

    async def delete(self, conn: AsyncConnection, actor: CurrentUser, note_id: UUID) -> UUID:
        """Soft delete; the text stays in the history. Returns the client id."""
        row = await self._own(conn, actor, note_id)
        await self._keep(conn, actor, row, "deleted")
        n = t.client_notes
        await conn.execute(
            update(n).where(n.c.id == note_id).values(deleted_at=now_utc(), version=n.c.version + 1)
        )
        await write_audit(
            conn, t.audit_log, action="note_deleted", actor_id=actor.id,
            entity_type="client_note", entity_id=note_id,
        )  # fmt: skip
        return UUID(str(row.client_id))

    async def own_sessions(
        self, conn: AsyncConnection, actor: CurrentUser, client_id: UUID
    ) -> list[tuple[UUID, str]]:
        """The actor's sessions with this client, for "this note belongs to session ..."."""
        s = t.therapy_sessions
        rows = (
            await conn.execute(
                select(s.c.id, s.c.session_number, s.c.session_date)
                .where(
                    s.c.client_id == client_id,
                    s.c.therapist_id == actor.id,
                    s.c.deleted_at.is_(None),
                )
                .order_by(s.c.session_date.desc())
            )
        ).all()
        return [
            (r.id, f"Seans {r.session_number} · {format_datetime_tr(r.session_date)}") for r in rows
        ]
