"""Gelir (income): payments against appointments, and totals (brief §5, §6).

- A payment always belongs to an appointment (and so to its client and therapist).
- Amounts are never edited: a wrong entry is voided with a reason and entered again.
- Income is attributed to the appointment's therapist. Who sees what:
  income.read_all -> clinic total and every therapist; income.read_own -> own only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.crypto.fernet import FieldCipher
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.enums import AppointmentStatus, PaymentMethod
from klinik_shared.permissions import Permission
from klinik_shared.timeutil import ISTANBUL, istanbul_day_bounds, now_utc, to_istanbul

MAX_AMOUNT = Decimal("1000000")
CENT = Decimal("0.01")
VOID_REASONS = {
    "entry_error": "Hatalı giriş",
    "refund": "İade",
    "duplicate": "Mükerrer kayıt",
}
PERIODS = ("gun", "hafta", "ay", "yil")
_AMOUNT_RE = re.compile(r"[0-9.,]+")


class PaymentError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class PaymentNotFound(Exception):
    pass


# ---------------------------------------------------------------- money


def parse_amount(raw: object) -> Decimal:
    """Accepts what people type: "1500", "1.500", "1.500,50", "1500,5", "1500.50", "₺ 750"."""
    text = str(raw or "").replace("₺", "").replace("TL", "").replace(" ", "").strip()
    if not text or not _AMOUNT_RE.fullmatch(text):
        raise PaymentError("amount", "Tutar geçersiz.")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1 or re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        text = text.replace(".", "")  # dots as thousands separators
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise PaymentError("amount", "Tutar geçersiz.") from None
    if value != value.quantize(CENT):
        raise PaymentError("amount", "Tutar en fazla iki ondalık basamak içerebilir.")
    if value <= 0:
        raise PaymentError("amount", "Tutar sıfırdan büyük olmalı.")
    if value > MAX_AMOUNT:
        raise PaymentError("amount", "Tutar çok büyük.")
    return value.quantize(CENT)


def format_try(value: Decimal | int | None) -> str:
    """Turkish style: 1.500,00 ₺"""
    amount = Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)
    whole, _, cents = f"{amount:.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    groups: list[str] = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    return f"{sign}{'.'.join(groups) or '0'},{cents} ₺"


# ---------------------------------------------------------------- periods


@dataclass(frozen=True)
class Period:
    kind: str
    first: date
    last: date  # inclusive

    @property
    def bounds(self) -> tuple[datetime, datetime]:
        return istanbul_day_bounds(self.first)[0], istanbul_day_bounds(self.last)[1]

    @property
    def days(self) -> list[date]:
        return [self.first + timedelta(days=i) for i in range((self.last - self.first).days + 1)]

    def step(self, direction: int) -> date:
        if self.kind == "gun":
            return self.first + timedelta(days=direction)
        if self.kind == "hafta":
            return self.first + timedelta(days=7 * direction)
        if self.kind == "yil":
            return date(self.first.year + direction, 1, 1)
        month = self.first.month - 1 + direction
        return date(self.first.year + month // 12, month % 12 + 1, 1)


def period_for(kind: str, day: date) -> Period:
    if kind == "gun":
        return Period(kind, day, day)
    if kind == "hafta":
        monday = day - timedelta(days=day.weekday())
        return Period(kind, monday, monday + timedelta(days=6))
    if kind == "yil":
        return Period("yil", date(day.year, 1, 1), date(day.year, 12, 31))
    first = day.replace(day=1)
    following = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return Period("ay", first, following - timedelta(days=1))


# ---------------------------------------------------------------- results


@dataclass(frozen=True)
class PaymentRow:
    id: UUID
    appointment_id: UUID
    client_code: str
    client_name: str
    amount: Decimal
    method: str
    paid_at: datetime
    therapist_id: UUID
    therapist_name: str
    recorded_by_name: str
    voided: bool
    void_reason: str | None


@dataclass(frozen=True)
class Report:
    period: Period
    total: Decimal
    count: int
    by_day: list[tuple[date, Decimal]]
    by_method: dict[str, Decimal]
    by_therapist: list[tuple[UUID, str, Decimal, int]]
    payments: list[PaymentRow]
    scope: str  # "all" or "own"


# ---------------------------------------------------------------- service


def _select_payments() -> Any:
    p, a, c = t.payments, t.appointments, t.clients
    therapist = t.users.alias("therapist")
    recorder = t.users.alias("recorder")
    return select(
        p.c.id,
        p.c.appointment_id,
        c.c.client_code,
        c.c.full_name_encrypted,
        p.c.amount,
        p.c.method,
        p.c.paid_at,
        a.c.therapist_id,
        therapist.c.display_name.label("therapist_name"),
        recorder.c.display_name.label("recorded_by_name"),
        p.c.voided_at,
        p.c.void_reason,
    ).select_from(
        p.join(a, a.c.id == p.c.appointment_id)
        .join(c, c.c.id == a.c.client_id)
        .join(therapist, therapist.c.id == a.c.therapist_id)
        .join(recorder, recorder.c.id == p.c.recorded_by)
    )


def _row(r: Any, cipher: FieldCipher) -> PaymentRow:
    return PaymentRow(
        r.id, r.appointment_id, r.client_code, cipher.decrypt(r.full_name_encrypted),
        r.amount, r.method, r.paid_at, r.therapist_id,
        r.therapist_name, r.recorded_by_name, r.voided_at is not None, r.void_reason,
    )  # fmt: skip


def income_scope(user: CurrentUser) -> str | None:
    if user.has(Permission.INCOME_READ_ALL):
        return "all"
    if user.has(Permission.INCOME_READ_OWN):
        return "own"
    return None


class IncomeService:
    def __init__(self, cipher: FieldCipher) -> None:
        self.cipher = cipher

    @staticmethod
    def can_see(user: CurrentUser, therapist_id: UUID) -> bool:
        """Payments of an appointment: admins see all, a therapist their own appointments'."""
        scope = income_scope(user)
        return scope == "all" or (scope == "own" and therapist_id == user.id)

    async def for_appointment(
        self, conn: AsyncConnection, appointment_id: UUID, *, recorded_by: UUID | None = None
    ) -> list[PaymentRow]:
        """The appointment's payments; with ``recorded_by``, only those that person took."""
        stmt = _select_payments().where(t.payments.c.appointment_id == appointment_id)
        if recorded_by is not None:
            stmt = stmt.where(t.payments.c.recorded_by == recorded_by)
        rows = (await conn.execute(stmt.order_by(t.payments.c.paid_at))).all()
        return [_row(r, self.cipher) for r in rows]

    async def collected_for_others(
        self, conn: AsyncConnection, user: CurrentUser, period: Period
    ) -> list[PaymentRow]:
        """Payments this person took for colleagues' appointments (cash to hand over).

        Not the colleague's income: only what this person received themselves.
        """
        p, a = t.payments, t.appointments
        start, end = period.bounds
        rows = (
            await conn.execute(
                _select_payments()
                .where(
                    p.c.recorded_by == user.id,
                    a.c.therapist_id != user.id,
                    p.c.paid_at >= start,
                    p.c.paid_at < end,
                )
                .order_by(p.c.paid_at.desc())
            )
        ).all()
        return [_row(r, self.cipher) for r in rows]

    async def record(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        *,
        appointment_id: UUID,
        amount: Decimal,
        method: str,
        paid_on: date | None,
    ) -> UUID:
        a = t.appointments
        appt = (
            await conn.execute(
                select(a.c.id, a.c.therapist_id, a.c.status, a.c.starts_at).where(
                    a.c.id == appointment_id, a.c.deleted_at.is_(None)
                )
            )
        ).one_or_none()
        if appt is None:
            raise PaymentError("appointment_id", "Randevu bulunamadı.")
        # Anyone at the desk may take a payment for any appointment (payments.write);
        # seeing a colleague's income is a different right (income.read_all).
        if appt.status == AppointmentStatus.CANCELLED.value:
            raise PaymentError("appointment_id", "İptal edilmiş randevuya ödeme girilemez.")
        if method not in {m.value for m in PaymentMethod}:
            raise PaymentError("method", "Ödeme yöntemi seçin.")
        today = to_istanbul(now_utc()).date()
        day = paid_on or today
        if day > today:
            raise PaymentError("paid_on", "Ödeme tarihi gelecekte olamaz.")
        if day < today - timedelta(days=365):
            raise PaymentError("paid_on", "Ödeme tarihi çok eski.")
        # Today: the actual moment. Another day: noon, so it cannot slip to a neighbouring day.
        paid_at = now_utc() if day == today else datetime.combine(day, time(12, 0), tzinfo=ISTANBUL)
        p = t.payments
        payment_id: UUID = (
            await conn.execute(
                insert(p)
                .values(
                    appointment_id=appt.id,
                    amount=amount,
                    method=method,
                    paid_at=paid_at,
                    recorded_by=actor.id,
                )
                .returning(p.c.id)
            )
        ).scalar_one()
        await write_audit(
            conn,
            t.audit_log,
            action="payment_recorded",
            actor_id=actor.id,
            entity_type="payment",
            entity_id=payment_id,
            detail={"appointment_id": str(appt.id), "amount": str(amount), "method": method},
        )
        return payment_id

    async def void(
        self, conn: AsyncConnection, actor: CurrentUser, payment_id: UUID, reason: str
    ) -> UUID:
        """Returns the appointment id. A voided payment stays visible, struck through."""
        if reason not in VOID_REASONS:
            raise PaymentError("void_reason", "İptal nedeni seçin.")
        p, a = t.payments, t.appointments
        where = [p.c.id == payment_id, p.c.voided_at.is_(None)]
        if not actor.has(Permission.INCOME_READ_ALL):
            # Own appointments' payments, or a payment this person took (a mistake to fix).
            where.append(
                or_(
                    p.c.appointment_id.in_(select(a.c.id).where(a.c.therapist_id == actor.id)),
                    p.c.recorded_by == actor.id,
                )
            )
        row = (
            await conn.execute(
                update(p)
                .where(*where)
                .values(voided_at=now_utc(), voided_by=actor.id, void_reason=reason)
                .returning(p.c.appointment_id, p.c.amount)
            )
        ).one_or_none()
        if row is None:
            raise PaymentNotFound
        await write_audit(
            conn,
            t.audit_log,
            action="payment_voided",
            actor_id=actor.id,
            entity_type="payment",
            entity_id=payment_id,
            detail={"amount": str(row.amount), "reason": reason},
        )
        return UUID(str(row.appointment_id))

    async def report(
        self,
        conn: AsyncConnection,
        user: CurrentUser,
        period: Period,
        *,
        therapist_id: UUID | None = None,
    ) -> Report:
        scope = income_scope(user)
        if scope is None:
            raise PermissionError
        if scope == "own":
            therapist_id = user.id  # whatever was asked for, own income only
        p, a = t.payments, t.appointments
        start, end = period.bounds
        where = and_(p.c.voided_at.is_(None), p.c.paid_at >= start, p.c.paid_at < end)
        if therapist_id is not None:
            where = and_(where, a.c.therapist_id == therapist_id)
        joined = p.join(a, a.c.id == p.c.appointment_id)

        local_day = func.date(func.timezone("Europe/Istanbul", p.c.paid_at))
        by_day_rows = (
            await conn.execute(
                select(local_day.label("day"), func.sum(p.c.amount))
                .select_from(joined)
                .where(where)
                .group_by(local_day)
            )
        ).all()
        per_day = {r[0]: r[1] for r in by_day_rows}
        by_method_rows = (
            await conn.execute(
                select(p.c.method, func.sum(p.c.amount))
                .select_from(joined)
                .where(where)
                .group_by(p.c.method)
            )
        ).all()
        by_therapist_rows = (
            await conn.execute(
                select(a.c.therapist_id, t.users.c.display_name, func.sum(p.c.amount), func.count())
                .select_from(joined.join(t.users, t.users.c.id == a.c.therapist_id))
                .where(where)
                .group_by(a.c.therapist_id, t.users.c.display_name)
                .order_by(t.users.c.display_name)
            )
        ).all()

        listing_where = and_(p.c.paid_at >= start, p.c.paid_at < end)
        if therapist_id is not None:
            listing_where = and_(listing_where, a.c.therapist_id == therapist_id)
        payments = [
            _row(r, self.cipher)
            for r in (
                await conn.execute(
                    _select_payments().where(listing_where).order_by(p.c.paid_at.desc())
                )
            ).all()
        ]
        total = sum((r[2] for r in by_therapist_rows), Decimal("0"))
        count = sum(int(r[3]) for r in by_therapist_rows)
        return Report(
            period=period,
            total=total,
            count=count,
            by_day=[(d, per_day.get(d, Decimal("0"))) for d in period.days],
            by_method={r[0]: r[1] for r in by_method_rows},
            by_therapist=[(r[0], r[1], r[2], int(r[3])) for r in by_therapist_rows],
            payments=payments,
            scope=scope,
        )
