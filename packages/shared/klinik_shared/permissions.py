"""Permission constants (brief §5).

Users hold *sets* of permissions, not a single role. The named sets below are
only convenient bundles for bootstrapping users.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    SCHEDULE_READ_ALL = "schedule.read_all"
    SCHEDULE_WRITE_ALL = "schedule.write_all"
    PAYMENTS_WRITE = "payments.write"
    INCOME_READ_ALL = "income.read_all"
    INCOME_READ_OWN = "income.read_own"
    CLIENTS_READ = "clients.read"
    CLIENTS_WRITE = "clients.write"
    SESSIONS_READ_OWN = "sessions.read_own"
    SESSIONS_WRITE_OWN = "sessions.write_own"
    SESSIONS_READ_OTHERS = "sessions.read_others"
    ADMIN_USERS = "admin.users"
    ADMIN_CATALOG = "admin.catalog"
    ADMIN_EXPORT = "admin.export"
    ADMIN_BACKUP = "admin.backup"


P = Permission

# Decision 2026-11-18: one office app with two roles.
# Therapist: the whole calendar (and booking), their own clients' sessions, own income.
THERAPIST_SET: frozenset[Permission] = frozenset(
    {
        P.SCHEDULE_READ_ALL,
        P.SCHEDULE_WRITE_ALL,
        P.PAYMENTS_WRITE,
        P.INCOME_READ_OWN,
        P.CLIENTS_READ,
        P.CLIENTS_WRITE,
        P.SESSIONS_READ_OWN,
        P.SESSIONS_WRITE_OWN,
    }
)

# Admin (the clinic owner) sees everything: every therapist's sessions and notes, all
# income, reports, users and settings. The owner can also run their own sessions.
ADMIN_SET: frozenset[Permission] = frozenset(Permission)

# Front desk without clinical access (kept for completeness; not offered as a role).
SCHEDULER_SET: frozenset[Permission] = frozenset(
    {
        P.SCHEDULE_READ_ALL,
        P.SCHEDULE_WRITE_ALL,
        P.PAYMENTS_WRITE,
        P.CLIENTS_READ,
        P.CLIENTS_WRITE,
    }
)

ROLES: dict[str, str] = {"admin": "Yönetici", "therapist": "Terapist"}


def role_of(permissions: set[str] | frozenset[str]) -> str | None:
    """The role whose set matches exactly, or None for a hand-made combination."""
    held = frozenset(permissions)
    for name in ROLES:
        if held == frozenset(p.value for p in PERMISSION_SETS[name]):
            return name
    return None


PERMISSION_SETS: dict[str, frozenset[Permission]] = {
    "admin": ADMIN_SET,
    "therapist": THERAPIST_SET,
    "scheduler": SCHEDULER_SET,
}

ALL_PERMISSION_VALUES: tuple[str, ...] = tuple(p.value for p in Permission)


def expand_sets(*names: str) -> frozenset[Permission]:
    """Union of named permission sets, e.g. ``expand_sets("therapist", "scheduler")``."""
    result: set[Permission] = set()
    for name in names:
        try:
            result |= PERMISSION_SETS[name]
        except KeyError:
            raise ValueError(f"unknown permission set: {name!r}") from None
    return frozenset(result)
