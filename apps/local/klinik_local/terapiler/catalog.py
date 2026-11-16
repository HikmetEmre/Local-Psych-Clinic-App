"""Therapy catalog: age groups, methods and per-age-group defaults (TERAPILER_TAB §2, §4).

Nothing here is hard-coded: groups, ranges, methods and defaults all come from the
database, so the clinic can change them without a code change.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from klinik_local import tables as t
from klinik_shared.enums import MethodKind
from klinik_shared.timeutil import age_on


@dataclass(frozen=True)
class AgeGroup:
    id: int
    code: str
    label_tr: str
    min_age: int
    max_age: int | None  # inclusive; None = no upper bound
    sort_order: int

    def contains(self, age: int) -> bool:
        return age >= self.min_age and (self.max_age is None or age <= self.max_age)


@dataclass(frozen=True)
class Method:
    id: int
    code: str
    label_tr: str
    kind: str
    description_tr: str | None
    is_active: bool


class NoAgeGroupError(LookupError):
    """No active age group covers this age (a gap in the configured ranges)."""


def resolve_age_group(groups: Iterable[AgeGroup], birth_date: date, at_date: date) -> AgeGroup:
    """Age group of a client on the day of the session.

    The result is snapshotted on the session row and never recomputed later, so a
    child who turns 13 keeps their earlier sessions under Çocuk.
    """
    age = age_on(birth_date, at_date)
    for group in groups:
        if group.contains(age):
            return group
    raise NoAgeGroupError(f"no age group covers age {age}")


@dataclass(frozen=True)
class MethodChoices:
    """Methods offered for one age group: its defaults first, then everything else."""

    defaults: list[Method]
    others: list[Method]

    @property
    def all(self) -> list[Method]:
        return [*self.defaults, *self.others]


@dataclass(frozen=True)
class Catalog:
    age_groups: list[AgeGroup]  # active, in tab order
    methods: dict[str, Method]  # by code, including inactive ones (history refers to them)
    # age_group_id -> method ids in sort_order
    defaults: dict[int, list[int]] = field(default_factory=dict)

    def group_by_code(self, code: str) -> AgeGroup | None:
        return next((g for g in self.age_groups if g.code == code), None)

    def group_by_id(self, group_id: int) -> AgeGroup | None:
        return next((g for g in self.age_groups if g.id == group_id), None)

    def method_by_id(self, method_id: int) -> Method | None:
        return next((m for m in self.methods.values() if m.id == method_id), None)

    def resolve(self, birth_date: date, at_date: date) -> AgeGroup:
        return resolve_age_group(self.age_groups, birth_date, at_date)

    def choices(self, kind: MethodKind, group: AgeGroup | None) -> MethodChoices:
        """Active methods of one kind. With no group (the Tümü tab) nothing is a default."""
        active = [m for m in self.methods.values() if m.kind == kind.value and m.is_active]
        default_ids = self.defaults.get(group.id, []) if group is not None else []
        by_id = {m.id: m for m in active}
        defaults = [by_id[i] for i in default_ids if i in by_id]
        chosen = {m.id for m in defaults}
        others = sorted((m for m in active if m.id not in chosen), key=lambda m: m.label_tr)
        return MethodChoices(defaults, others)

    def is_default(self, group: AgeGroup, method: Method) -> bool:
        return method.id in self.defaults.get(group.id, [])


async def load_catalog(conn: AsyncConnection) -> Catalog:
    groups = [
        AgeGroup(r.id, r.code, r.label_tr, r.min_age, r.max_age, r.sort_order)
        for r in (
            await conn.execute(
                select(t.age_groups)
                .where(t.age_groups.c.is_active)
                .order_by(t.age_groups.c.sort_order, t.age_groups.c.id)
            )
        ).all()
    ]
    methods = {
        r.code: Method(r.id, r.code, r.label_tr, r.kind, r.description_tr, r.is_active)
        for r in (await conn.execute(select(t.therapy_methods))).all()
    }
    defaults: dict[int, list[int]] = {}
    d = t.age_group_therapy_defaults
    for r in (
        await conn.execute(
            select(d).order_by(d.c.age_group_id, d.c.sort_order, d.c.therapy_method_id)
        )
    ).all():
        defaults.setdefault(r.age_group_id, []).append(r.therapy_method_id)
    return Catalog(groups, methods, defaults)
