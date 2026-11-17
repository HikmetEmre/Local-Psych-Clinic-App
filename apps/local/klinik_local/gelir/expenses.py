"""Gider (expenses): the clinic's costs, and the net result (income - expenses).

Clinic-level, so admin only (income.read_all), like the clinic's total income; the
database refuses anyone else. Amounts are never edited: a wrong entry is voided with a
reason and entered again, exactly like payments.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_local.gelir.service import VOID_REASONS, PaymentError, PaymentNotFound
from klinik_shared.audit import write_audit
from klinik_shared.authkit.service import CurrentUser
from klinik_shared.enums import PaymentMethod
from klinik_shared.timeutil import now_utc, to_istanbul

CATEGORIES = {
    "rent": "Kira",
    "utilities": "Faturalar (elektrik, su, doğalgaz, internet)",
    "salary": "Personel / maaş",
    "tax": "Vergi / SGK",
    "accounting": "Muhasebe",
    "supplies": "Malzeme ve test materyali",
    "training": "Eğitim / süpervizyon",
    "maintenance": "Bakım ve onarım",
    "cleaning": "Temizlik",
    "marketing": "Reklam / tanıtım",
    "other": "Diğer",
}
MAX_DESCRIPTION = 200


@dataclass(frozen=True)
class Expense:
    id: UUID
    spent_on: date
    category: str
    description: str
    amount: Decimal
    method: str
    recorded_by_name: str
    created_at: datetime
    voided: bool
    void_reason: str | None


@dataclass(frozen=True)
class ExpenseSummary:
    total: Decimal
    count: int
    by_category: list[tuple[str, Decimal, int]]  # (category code, amount, count), largest first
    items: list[Expense]  # including voided ones


class ExpenseService:
    async def in_period(self, conn: AsyncConnection, first: date, last: date) -> ExpenseSummary:
        e, u = t.expenses, t.users
        rows = (
            await conn.execute(
                select(e, u.c.display_name.label("recorder"))
                .join(u, u.c.id == e.c.recorded_by)
                .where(e.c.spent_on >= first, e.c.spent_on <= last)
                .order_by(e.c.spent_on.desc(), e.c.created_at.desc())
            )
        ).all()
        items = [
            Expense(
                r.id, r.spent_on, r.category, r.description or "", r.amount, r.method,
                r.recorder, r.created_at, r.voided_at is not None, r.void_reason,
            )
            for r in rows
        ]  # fmt: skip
        valid = [x for x in items if not x.voided]
        per: dict[str, list[Expense]] = defaultdict(list)
        for x in valid:
            per[x.category].append(x)
        by_category = sorted(
            (
                (code, sum((x.amount for x in xs), Decimal("0")), len(xs))
                for code, xs in per.items()
            ),
            key=lambda item: -item[1],
        )
        return ExpenseSummary(
            total=sum((x.amount for x in valid), Decimal("0")),
            count=len(valid),
            by_category=by_category,
            items=items,
        )

    async def record(
        self,
        conn: AsyncConnection,
        actor: CurrentUser,
        *,
        spent_on: date | None,
        category: str,
        description: str,
        amount: Decimal,
        method: str,
    ) -> UUID:
        if category not in CATEGORIES:
            raise PaymentError("category", "Gider türü seçin.")
        if method not in {m.value for m in PaymentMethod}:
            raise PaymentError("method", "Ödeme yöntemi seçin.")
        description = " ".join(description.split())
        if len(description) > MAX_DESCRIPTION:
            raise PaymentError("description", "Açıklama en fazla 200 karakter olabilir.")
        today = to_istanbul(now_utc()).date()
        day = spent_on or today
        if day > today + timedelta(days=31):
            raise PaymentError("spent_on", "Gider tarihi bu kadar ileride olamaz.")
        if day < today - timedelta(days=3 * 365):
            raise PaymentError("spent_on", "Gider tarihi çok eski.")
        expense_id: UUID = (
            await conn.execute(
                insert(t.expenses)
                .values(
                    spent_on=day,
                    category=category,
                    description=description or None,
                    amount=amount,
                    method=method,
                    recorded_by=actor.id,
                )
                .returning(t.expenses.c.id)
            )
        ).scalar_one()
        await write_audit(
            conn,
            t.audit_log,
            action="expense_recorded",
            actor_id=actor.id,
            entity_type="expense",
            entity_id=expense_id,
            detail={"category": category, "amount": str(amount)},
        )
        return expense_id

    async def void(
        self, conn: AsyncConnection, actor: CurrentUser, expense_id: UUID, reason: str
    ) -> None:
        if reason not in VOID_REASONS:
            raise PaymentError("void_reason", "İptal nedeni seçin.")
        e = t.expenses
        row = (
            await conn.execute(
                update(e)
                .where(e.c.id == expense_id, e.c.voided_at.is_(None))
                .values(
                    voided_at=now_utc(), voided_by=actor.id, void_reason=reason,
                    version=e.c.version + 1,
                )
                .returning(e.c.amount)
            )
        ).one_or_none()  # fmt: skip
        if row is None:
            raise PaymentNotFound
        await write_audit(
            conn,
            t.audit_log,
            action="expense_voided",
            actor_id=actor.id,
            entity_type="expense",
            entity_id=expense_id,
            detail={"amount": str(row.amount), "reason": reason},
        )


def monthly(
    first: date, last: date, income: list[tuple[date, Decimal]], expenses: list[Expense]
) -> list[dict[str, Any]]:
    """Income, expenses and net per calendar month in [first, last]."""
    months: dict[tuple[int, int], dict[str, Any]] = {}
    cursor = first.replace(day=1)
    while cursor <= last:
        months[(cursor.year, cursor.month)] = {
            "month": cursor, "income": Decimal("0"), "expense": Decimal("0"),
        }  # fmt: skip
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
    for day, amount in income:
        if (day.year, day.month) in months:
            months[(day.year, day.month)]["income"] += amount
    for x in expenses:
        if not x.voided and (x.spent_on.year, x.spent_on.month) in months:
            months[(x.spent_on.year, x.spent_on.month)]["expense"] += x.amount
    out = list(months.values())
    for m in out:
        m["net"] = m["income"] - m["expense"]
    return out
