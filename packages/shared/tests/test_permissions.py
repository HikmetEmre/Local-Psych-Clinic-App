"""The two roles of the office app (decision 2026-11-18): admin sees all, therapist own."""

from __future__ import annotations

import pytest

from klinik_shared.permissions import (
    ALL_PERMISSION_VALUES,
    ROLES,
    Permission,
    expand_sets,
    role_of,
)

P = Permission

ADMIN = expand_sets("admin")
THERAPIST = expand_sets("therapist")

# permission -> (admin has it, therapist has it)
ROLE_TABLE: dict[Permission, tuple[bool, bool]] = {
    P.SCHEDULE_READ_ALL: (True, True),
    P.SCHEDULE_WRITE_ALL: (True, True),
    P.PAYMENTS_WRITE: (True, True),
    P.INCOME_READ_ALL: (True, False),
    P.INCOME_READ_OWN: (True, True),
    P.CLIENTS_READ: (True, True),
    P.CLIENTS_WRITE: (True, True),
    P.SESSIONS_READ_OWN: (True, True),
    P.SESSIONS_WRITE_OWN: (True, True),
    P.SESSIONS_READ_OTHERS: (True, False),
    P.ADMIN_USERS: (True, False),
    P.ADMIN_CATALOG: (True, False),
    P.ADMIN_EXPORT: (True, False),
    P.ADMIN_BACKUP: (True, False),
}


def test_table_covers_every_permission() -> None:
    assert set(ROLE_TABLE) == set(Permission)


@pytest.mark.parametrize("permission", list(Permission))
def test_admin_and_therapist_roles(permission: Permission) -> None:
    admin_expected, therapist_expected = ROLE_TABLE[permission]
    assert (permission in ADMIN) is admin_expected
    assert (permission in THERAPIST) is therapist_expected


def test_role_of_recognises_exact_sets_only() -> None:
    assert role_of({p.value for p in ADMIN}) == "admin"
    assert role_of({p.value for p in THERAPIST}) == "therapist"
    assert role_of({p.value for p in THERAPIST} | {"admin.users"}) is None
    assert set(ROLES) == {"admin", "therapist"}


def test_a_secretary_set_gets_no_clinical_access() -> None:
    secretary = expand_sets("scheduler")
    assert P.SCHEDULE_WRITE_ALL in secretary
    assert not {P.SESSIONS_READ_OWN, P.SESSIONS_WRITE_OWN, P.SESSIONS_READ_OTHERS} & secretary
    assert not {p for p in secretary if p.value.startswith("admin.")}


def test_unknown_set_name_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown permission set"):
        expand_sets("therapist", "superuser")


def test_permission_values_are_unique_strings() -> None:
    assert len(set(ALL_PERMISSION_VALUES)) == len(ALL_PERMISSION_VALUES) == 14
