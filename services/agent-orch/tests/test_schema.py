from __future__ import annotations

import pytest

from agent_orch.schema import InboxValidationError, validate_inbox


def valid_payload():
    return {
        "schema_version": "v1",
        "msg_id": 1,
        "from": None,
        "to": "hostb:codex-a",
        "phase": None,
        "role_hint": "qa",
        "task": "do work",
        "inputs": {},
        "extras": {},
    }


def test_valid_inbox():
    assert validate_inbox(valid_payload()).msg_id == 1


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p: p.pop("task"), "missing_field"),
        (lambda p: p.__setitem__("msg_id", "1"), "wrong_type"),
        (lambda p: p.__setitem__("schema_version", "v2"), "schema_version"),
    ],
)
def test_invalid_inbox(mutate, code):
    payload = valid_payload()
    mutate(payload)
    with pytest.raises(InboxValidationError) as excinfo:
        validate_inbox(payload)
    assert excinfo.value.code == code


def test_inbox_missing_two_top_level_fields_reports_both():
    payload = valid_payload()
    payload.pop("task")
    payload.pop("extras")

    with pytest.raises(InboxValidationError) as excinfo:
        validate_inbox(payload)

    assert excinfo.value.code == "missing_field"
    assert str(excinfo.value) == "missing field: task; missing field: extras"
    assert excinfo.value.violations == [
        {"code": "missing_field", "field": "task", "detail": "missing field: task"},
        {"code": "missing_field", "field": "extras", "detail": "missing field: extras"},
    ]

