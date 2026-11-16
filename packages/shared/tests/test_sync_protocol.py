from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from klinik_shared.sync_protocol import (
    MAX_PUSH_ITEMS,
    AppointmentOutcomeItem,
    ChangesResponse,
    ClientCodeUpsertItem,
    ItemResult,
    PushRequest,
)


def code_item(seq: int = 1, **payload_extra: Any) -> dict[str, Any]:
    return {
        "item_id": str(uuid4()),
        "seq": seq,
        "kind": "client_code.upsert",
        "payload": {
            "code": "DN-2026-0042",
            "active": True,
            "assigned_therapist_id": str(uuid4()),
            **payload_extra,
        },
    }


def outcome_item(seq: int = 1, new: str = "completed", prev: str = "planned") -> dict[str, Any]:
    return {
        "item_id": str(uuid4()),
        "seq": seq,
        "kind": "appointment.outcome",
        "payload": {
            "appointment_id": str(uuid4()),
            "new_status": new,
            "prev_status": prev,
            "actor_id": str(uuid4()),
            "recorded_at": "2026-11-16T14:50:00+03:00",
        },
    }


def test_push_request_parses_each_kind() -> None:
    request = PushRequest.model_validate({"items": [code_item(1), outcome_item(2)]})
    assert isinstance(request.items[0], ClientCodeUpsertItem)
    assert isinstance(request.items[1], AppointmentOutcomeItem)


@pytest.mark.parametrize("leak", ["full_name", "phone", "notes", "birth_date"])
def test_identity_or_clinical_fields_cannot_ride_along(leak: str) -> None:
    """The whole point of extra=forbid: nothing unlisted leaves the office."""
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": [code_item(1, **{leak: "Ayşe Yılmaz"})]})


def test_unknown_top_level_and_item_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": [code_item(1)], "comment": "x"})
    item = code_item(1)
    item["note"] = "x"
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": [item]})


def test_unknown_kind_is_rejected() -> None:
    item = code_item(1)
    item["kind"] = "client.upsert_full"
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": [item]})


def test_malformed_client_code_is_rejected() -> None:
    item = code_item(1)
    item["payload"]["code"] = "DN-0042"
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": [item]})


def test_items_must_be_in_increasing_seq_order() -> None:
    with pytest.raises(ValidationError, match="increasing seq"):
        PushRequest.model_validate({"items": [code_item(2), code_item(1)]})
    with pytest.raises(ValidationError, match="increasing seq"):
        PushRequest.model_validate({"items": [code_item(1), code_item(1)]})


def test_duplicate_item_id_in_one_request_is_rejected() -> None:
    first = code_item(1)
    second = code_item(2)
    second["item_id"] = first["item_id"]
    with pytest.raises(ValidationError, match="duplicate item_id"):
        PushRequest.model_validate({"items": [first, second]})


def test_batch_size_limits() -> None:
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": []})
    too_many = [code_item(i) for i in range(1, MAX_PUSH_ITEMS + 2)]
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"items": too_many})


@pytest.mark.parametrize(
    ("new", "prev", "ok"),
    [
        ("completed", "planned", True),
        ("no_show", "planned", True),
        ("no_show", "completed", True),  # therapist corrects the outcome
        ("planned", "completed", True),  # revert after deleting a session
        ("cancelled", "planned", False),  # cancelling belongs to the cloud
        ("completed", "completed", False),
    ],
)
def test_outcome_transitions(new: str, prev: str, ok: bool) -> None:
    payload = {"items": [outcome_item(1, new, prev)]}
    if ok:
        PushRequest.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            PushRequest.model_validate(payload)


def test_rejected_result_needs_a_reason() -> None:
    with pytest.raises(ValidationError, match="reason_code"):
        ItemResult.model_validate({"item_id": str(uuid4()), "result": "rejected"})
    ItemResult.model_validate(
        {"item_id": str(uuid4()), "result": "rejected", "reason_code": "status_mismatch"}
    )


def test_changes_response_parses_appointments_and_rooms() -> None:
    response = ChangesResponse.model_validate(
        {
            "changes": [
                {
                    "seq": 1043,
                    "entity": "appointment",
                    "data": {
                        "id": str(uuid4()),
                        "client_code": "DN-2026-0042",
                        "therapist_id": str(uuid4()),
                        "room_id": str(uuid4()),
                        "starts_at": "2026-11-12T14:00:00+03:00",
                        "duration_min": 50,
                        "status": "planned",
                        "version": 3,
                        "deleted_at": None,
                        "updated_at": "2026-11-10T09:00:00+03:00",
                    },
                },
                {
                    "seq": 1044,
                    "entity": "room",
                    "data": {
                        "id": str(uuid4()),
                        "internal_label": "Oda 1",
                        "is_active": True,
                        "sort_order": 1,
                        "updated_at": "2026-11-10T09:00:00+03:00",
                    },
                },
            ],
            "next_cursor": 1044,
            "has_more": False,
        }
    )
    assert [c.entity for c in response.changes] == ["appointment", "room"]


def test_appointment_change_rejects_extra_columns() -> None:
    with pytest.raises(ValidationError):
        ChangesResponse.model_validate(
            {
                "changes": [
                    {
                        "seq": 1,
                        "entity": "room",
                        "data": {
                            "id": str(uuid4()),
                            "internal_label": "Oda 1",
                            "is_active": True,
                            "sort_order": 1,
                            "updated_at": "2026-11-10T09:00:00+03:00",
                            "description": "x",
                        },
                    }
                ],
                "next_cursor": 1,
                "has_more": False,
            }
        )
