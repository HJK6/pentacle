from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


REQUIRED_INBOX_FIELDS = (
    "schema_version",
    "msg_id",
    "from",
    "to",
    "phase",
    "role_hint",
    "task",
    "inputs",
    "extras",
)

INLINE_INBOX_MAX_BYTES = 65_536


class InboxValidationError(ValueError):
    def __init__(self, code: str, message: str, *, violations: list[dict[str, str]] | None = None):
        self.code = code
        self.violations = violations or []
        super().__init__(message)


@dataclass(frozen=True)
class InboxPayload:
    schema_version: str
    msg_id: int
    from_stream_id: str | None
    to_stream_id: str
    phase: str | None
    role_hint: str | None
    task: str
    inputs: dict[str, Any]
    extras: dict[str, Any]


def _require_type(field: str, value: Any, expected_type: type | tuple[type, ...]) -> None:
    if not isinstance(value, expected_type):
        raise InboxValidationError("wrong_type", f"{field} has wrong type")


def _require_optional_string(field: str, value: Any) -> None:
    if value is not None and not isinstance(value, str):
        raise InboxValidationError("wrong_type", f"{field} has wrong type")


def _violation(code: str, field: str, detail: str) -> dict[str, str]:
    return {"code": code, "field": field, "detail": detail}


def _aggregated_code(violations: list[dict[str, str]]) -> str:
    codes = {violation["code"] for violation in violations}
    if len(codes) == 1:
        return violations[0]["code"]
    return "validation_error"


def _raise_inbox_violations(violations: list[dict[str, str]]) -> None:
    if violations:
        raise InboxValidationError(
            _aggregated_code(violations),
            "; ".join(violation["detail"] for violation in violations),
            violations=violations,
        )


def validate_inbox(payload: dict[str, Any]) -> InboxPayload:
    if not isinstance(payload, dict):
        raise InboxValidationError("wrong_type", "payload must be an object")
    violations: list[dict[str, str]] = []
    for field in REQUIRED_INBOX_FIELDS:
        if field not in payload:
            violations.append(_violation("missing_field", field, f"missing field: {field}"))

    if "schema_version" in payload and payload["schema_version"] != "v1":
        violations.append(_violation("schema_version", "schema_version", "schema_version must be v1"))
    if "msg_id" in payload and (not isinstance(payload["msg_id"], int) or isinstance(payload["msg_id"], bool)):
        violations.append(_violation("wrong_type", "msg_id", "msg_id has wrong type"))
    if "from" in payload and payload["from"] is not None and not isinstance(payload["from"], str):
        violations.append(_violation("wrong_type", "from", "from has wrong type"))
    if "to" in payload and not isinstance(payload["to"], str):
        violations.append(_violation("wrong_type", "to", "to has wrong type"))
    if "phase" in payload and payload["phase"] is not None and not isinstance(payload["phase"], str):
        violations.append(_violation("wrong_type", "phase", "phase has wrong type"))
    if "role_hint" in payload and payload["role_hint"] is not None and not isinstance(payload["role_hint"], str):
        violations.append(_violation("wrong_type", "role_hint", "role_hint has wrong type"))
    if "task" in payload and not isinstance(payload["task"], str):
        violations.append(_violation("wrong_type", "task", "task has wrong type"))
    if "inputs" in payload and not isinstance(payload["inputs"], dict):
        violations.append(_violation("wrong_type", "inputs", "inputs has wrong type"))
    if "extras" in payload and not isinstance(payload["extras"], dict):
        violations.append(_violation("wrong_type", "extras", "extras has wrong type"))
    _raise_inbox_violations(violations)

    return InboxPayload(
        schema_version=payload["schema_version"],
        msg_id=payload["msg_id"],
        from_stream_id=payload["from"],
        to_stream_id=payload["to"],
        phase=payload["phase"],
        role_hint=payload["role_hint"],
        task=payload["task"],
        inputs=payload["inputs"],
        extras=payload["extras"],
    )


def build_inbox_payload(
    *,
    msg_id: int,
    from_stream_id: str | None,
    to_stream_id: str,
    task: str,
    phase: str | None = None,
    role_hint: str | None = None,
    inputs: dict[str, Any] | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "v1",
        "msg_id": msg_id,
        "from": from_stream_id,
        "to": to_stream_id,
        "phase": phase,
        "role_hint": role_hint,
        "task": task,
        "inputs": inputs or {},
        "extras": extras or {},
    }
    validate_inbox(payload)
    return payload


def inline_inbox_json(payload: dict[str, Any]) -> str:
    inbox = validate_inbox(payload)
    data = json_dumps_compact(payload)
    if len(data.encode("utf-8")) > INLINE_INBOX_MAX_BYTES:
        raise InboxValidationError(
            "inbox_too_large_for_inline",
            f"inbox JSON exceeds {INLINE_INBOX_MAX_BYTES}-byte inline cap",
        )
    return data


def inline_inbox_block(payload: dict[str, Any]) -> str:
    inbox = validate_inbox(payload)
    return f'<INBOX_V1 msg_id="{inbox.msg_id}">\n{inline_inbox_json(payload)}\n</INBOX_V1>\n\n'


def json_dumps_compact(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))
