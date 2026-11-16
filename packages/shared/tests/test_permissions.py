"""The permission bundles must reproduce the table in brief §5 exactly."""

from __future__ import annotations

import pytest

from klinik_shared.permissions import ALL_PERMISSION_VALUES, Permission, expand_sets

P = Permission

THERAPIST_A = expand_sets("admin", "therapist")
THERAPIST_B = expand_sets("therapist", "scheduler")

# permission -> (A has it, B has it), copied from the brief.
BRIEF_TABLE: dict[Permission, tuple[bool, bool]] = {
    P.SCHEDULE_READ_ALL: (True, True),
    P.SCHEDULE_WRITE_ALL: (True, True),
    P.PAYMENTS_WRITE: (True, True),
    P.INCOME_READ_ALL: (True, False),
    P.INCOME_READ_OWN: (True, True),
    P.CLIENTS_READ: (True, True),
    P.CLIENTS_WRITE: (True, True),
    P.SESSIONS_READ_OWN: (True, True),
    P.SESSIONS_WRITE_OWN: (True, True),
    P.SESSIONS_READ_OTHERS: (False, False),
    P.ADMIN_USERS: (True, False),
    P.ADMIN_CATALOG: (True, False),
    P.ADMIN_EXPORT: (True, False),
    P.ADMIN_BACKUP: (True, False),
}


def test_table_covers_every_permission() -> None:
    assert set(BRIEF_TABLE) == set(Permission)


@pytest.mark.parametrize("permission", list(Permission))
def test_therapist_a_and_b_match_the_brief(permission: Permission) -> None:
    a_expected, b_expected = BRIEF_TABLE[permission]
    assert (permission in THERAPIST_A) is a_expected
    assert (permission in THERAPIST_B) is b_expected


def test_a_future_secretary_gets_no_clinical_access() -> None:
    secretary = expand_sets("scheduler")
    assert P.SCHEDULE_WRITE_ALL in secretary
    assert not {P.SESSIONS_READ_OWN, P.SESSIONS_WRITE_OWN, P.SESSIONS_READ_OTHERS} & secretary
    assert not {p for p in secretary if p.value.startswith("admin.")}


def test_unknown_set_name_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown permission set"):
        expand_sets("therapist", "superuser")


def test_permission_values_are_unique_strings() -> None:
    assert len(set(ALL_PERMISSION_VALUES)) == len(ALL_PERMISSION_VALUES) == 14
