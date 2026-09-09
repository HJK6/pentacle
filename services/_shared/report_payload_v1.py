from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


STATUSES = frozenset({"done", "progress", "error", "aborted"})
FINDING_SEVERITIES = frozenset({"blocking", "major", "minor", "info"})
REQUIRED_FINDING_FIELDS = ("severity", "where", "issue", "suggested_fix")

INLINE_FIELD_CAPS = {
    "summary": 4 * 1024,
    "next_action": 4 * 1024,
    "reason": 512,
    "findings": 64 * 1024,
    "details": 256 * 1024,
    "extras": 256 * 1024,
}
INLINE_TOTAL_CAP = 1024 * 1024

# These are the only fields that belong inside a ReportPayloadV1 object. Keep
# this set next to the validator so every caller applies one fail-closed rule.
REPORT_PAYLOAD_FIELDS = frozenset({
    "summary", "findings", "next_action", "details", "extras", "reason",
    "completion_kind", "qa_verdict", "target_sha", "qa_attestation", "ac_claim",
})
GOVERNANCE_FIELDS = ("completion_kind", "qa_verdict", "target_sha", "qa_attestation", "ac_claim")
FULL_GIT_SHA = re.compile(r"[0-9a-fA-F]{40}")

# Report requests carry this protocol envelope around the payload. The
# daemon-authorized agent-orch release identity is an envelope field because it
# binds the persisted report to its originating orchestration release; custom
# report evidence still belongs under `extras`.
REPORT_ENVELOPE_FIELDS = frozenset({
    "type", "request_id", "report_id", "from_stream_id", "stream_id",
    "caller_stream_id", "actor_stream_id", "stream_token", "ownership_token",
    "msg_id", "status", "result_blob_sha", "terminate", "close_on_ingest",
    "discharges", "operator_confirm", "client_rpc_timeout_s",
    "agent_orch_attestation",
    # v2 server dispatch attaches this non-wire authorization context before
    # invoking the ledger; it is never persisted as report payload.
    "_auth_context",
})
REPORT_MESSAGE_FIELDS = REPORT_PAYLOAD_FIELDS | REPORT_ENVELOPE_FIELDS


class SchemaError(ValueError):
    def __init__(self, code: str, message: str, *, violations: list[dict[str, Any]] | None = None):
        self.code = code
        self.violations = violations or []
        super().__init__(message)


@dataclass(frozen=True)
class ReportPayloadV1:
    status: str
    summary: str | None = None
    findings: list[dict[str, Any]] | None = None
    next_action: str | None = None
    details: Any = None
    extras: Any = None
    reason: str | None = None
    completion_kind: str | None = None
    qa_verdict: str | None = None
    target_sha: str | None = None
    qa_attestation: dict[str, str] | None = None
    ac_claim: dict[str, Any] | None = None


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _string_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _violation(field: str, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"field": field, "code": code, "detail": detail, **extra}


def unknown_field_violations(
    value: dict[str, Any], *, allowed: frozenset[str]
) -> list[dict[str, Any]]:
    """Return deterministic violations for keys outside an explicit boundary."""
    return [
        _violation(str(field), "unknown_field", f"unknown field: {field}")
        for field in sorted((field for field in value if field not in allowed), key=str)
    ]


def unknown_report_message_violations(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the top-level report request envelope before projection."""
    return unknown_field_violations(message, allowed=REPORT_MESSAGE_FIELDS)


def _nested_key_paths(value: Any, key: str, *, path: str) -> list[str]:
    """Return every dict-key path below one opaque report payload field."""
    found: list[str] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            child_path = f"{path}.{child_key}"
            if child_key == key:
                found.append(child_path)
            found.extend(_nested_key_paths(child_value, key, path=child_path))
    elif isinstance(value, list):
        for index, child_value in enumerate(value):
            found.extend(_nested_key_paths(child_value, key, path=f"{path}[{index}]"))
    return found


def validate(
    payload: dict[str, Any],
    status: str,
    *,
    enforce_inline_caps: bool = True,
    qa_grade: bool = False,
) -> ReportPayloadV1:
    if status not in STATUSES:
        raise SchemaError("invalid_status", "status must be done, progress, error, or aborted")
    if not isinstance(payload, dict):
        raise SchemaError(
            "schema_error",
            "payload must be an object",
            violations=[_violation("payload", "wrong_type", "payload must be an object")],
        )

    violations: list[dict[str, Any]] = []
    unknown = unknown_field_violations(payload, allowed=REPORT_PAYLOAD_FIELDS)
    if unknown:
        violations.extend(unknown)
        if payload.get("qa_verdict") in {"accept", "reject"}:
            violations.append(
                _violation(
                    "qa_verdict",
                    "preserved_field",
                    "qa_verdict is valid; move unknown sidecars into extras and refile without dropping --qa-verdict",
                )
            )
    terminal = status in {"done", "error", "aborted"}
    if terminal:
        for field in ("summary", "findings", "next_action"):
            if field not in payload:
                violations.append(_violation(field, "missing_field", f"missing field: {field}", missing=True))
    missing_reason = False
    if status in {"error", "aborted"}:
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            missing_reason = True
            violations.append(
                _violation(
                    "reason",
                    "missing_field" if "reason" not in payload else "invalid_required_string",
                    "reason is required for error and aborted reports",
                    missing="reason" not in payload,
                )
            )

    if "summary" in payload:
        if not isinstance(payload["summary"], str):
            violations.append(_violation("summary", "wrong_type", "summary has wrong type"))
        elif terminal and not payload["summary"].strip():
            violations.append(_violation("summary", "invalid_required_string", "summary must be non-empty"))
    if "next_action" in payload:
        if not isinstance(payload["next_action"], str):
            if terminal or payload["next_action"] is not None:
                violations.append(_violation("next_action", "wrong_type", "next_action has wrong type"))
        elif terminal and not payload["next_action"].strip():
            violations.append(_violation("next_action", "invalid_required_string", "next_action must be non-empty"))
    if "reason" in payload and payload["reason"] is not None and not isinstance(payload["reason"], str):
        violations.append(_violation("reason", "wrong_type", "reason has wrong type"))
    if "findings" in payload:
        if not isinstance(payload["findings"], list):
            violations.append(_violation("findings", "wrong_type", "findings has wrong type"))
        else:
            for index, finding in enumerate(payload["findings"]):
                if not isinstance(finding, dict):
                    violations.append(_violation(f"findings[{index}]", "wrong_type", f"findings[{index}] has wrong type"))
                    continue
                for field in REQUIRED_FINDING_FIELDS:
                    if field not in finding:
                        violations.append(
                            _violation(
                                f"findings[{index}].{field}",
                                "missing_field",
                                f"missing field: findings[{index}].{field}",
                                missing=True,
                            )
                        )
                if "severity" in finding and finding["severity"] not in FINDING_SEVERITIES:
                    violations.append(
                        _violation(f"findings[{index}].severity", "wrong_type", f"findings[{index}].severity invalid")
                    )
                for field in ("where", "issue"):
                    if field in finding and not isinstance(finding[field], str):
                        violations.append(
                            _violation(f"findings[{index}].{field}", "wrong_type", f"findings[{index}].{field} has wrong type")
                        )
                if "suggested_fix" in finding and finding["suggested_fix"] is not None and not isinstance(finding["suggested_fix"], str):
                    violations.append(
                        _violation(
                            f"findings[{index}].suggested_fix",
                            "wrong_type",
                            f"findings[{index}].suggested_fix has wrong type",
                        )
                    )
    if "details" in payload and payload["details"] is not None and not isinstance(payload["details"], (str, dict, list)):
        violations.append(_violation("details", "wrong_type", "details has wrong type"))
    if "extras" in payload and payload["extras"] is not None and not isinstance(payload["extras"], dict):
        violations.append(_violation("extras", "wrong_type", "extras has wrong type"))
    if "ac_claim" in payload and payload["ac_claim"] is not None:
        claim = payload["ac_claim"]
        if not isinstance(claim, dict):
            violations.append(_violation("ac_claim", "wrong_type", "ac_claim must be an object"))
        else:
            expected = {"spec_id", "spec_source", "claims"}
            for field in sorted((expected - {"spec_source"}) - set(claim)):
                violations.append(_violation(f"ac_claim.{field}", "missing_field", f"missing field: ac_claim.{field}", missing=True))
            for field in sorted(set(claim) - expected):
                violations.append(_violation(f"ac_claim.{field}", "unexpected_field", f"unexpected field: ac_claim.{field}"))
            spec_id = claim.get("spec_id")
            if not isinstance(spec_id, str) or not spec_id.strip():
                violations.append(_violation("ac_claim.spec_id", "invalid_required_string", "ac_claim.spec_id must be non-empty"))
            source = claim.get("spec_source")
            if "spec_source" in claim and not isinstance(source, dict):
                violations.append(_violation("ac_claim.spec_source", "wrong_type", "ac_claim.spec_source must be an object"))
            else:
                source = source if isinstance(source, dict) else {}
                source_expected = {"path", "sha"}
                for field in sorted(set(source) - source_expected):
                    violations.append(_violation(f"ac_claim.spec_source.{field}", "unexpected_field", f"unexpected field: ac_claim.spec_source.{field}"))
                if "path" in source and (not isinstance(source.get("path"), str) or not str(source.get("path") or "").strip()):
                    violations.append(_violation("ac_claim.spec_source.path", "invalid_required_string", "ac_claim.spec_source.path must be non-empty"))
                if "sha" in source and (not isinstance(source.get("sha"), str) or FULL_GIT_SHA.fullmatch(source.get("sha") or "") is None):
                    violations.append(_violation("ac_claim.spec_source.sha", "invalid_value", "ac_claim.spec_source.sha must be a full 40-hex git SHA"))
            claims = claim.get("claims")
            if not isinstance(claims, list):
                violations.append(_violation("ac_claim.claims", "wrong_type", "ac_claim.claims must be a list"))
            else:
                for index, item in enumerate(claims):
                    field_path = f"ac_claim.claims[{index}]"
                    if not isinstance(item, dict):
                        violations.append(_violation(field_path, "wrong_type", f"{field_path} must be an object"))
                        continue
                    expected_item = {"index", "checked"}
                    for field in sorted(expected_item - set(item)):
                        violations.append(_violation(f"{field_path}.{field}", "missing_field", f"missing field: {field_path}.{field}", missing=True))
                    for field in sorted(set(item) - expected_item):
                        violations.append(_violation(f"{field_path}.{field}", "unexpected_field", f"unexpected field: {field_path}.{field}"))
                    item_index = item.get("index")
                    if not isinstance(item_index, int) or isinstance(item_index, bool) or item_index < 1:
                        violations.append(_violation(f"{field_path}.index", "invalid_value", f"{field_path}.index must be a positive integer"))
                    if not isinstance(item.get("checked"), bool):
                        violations.append(_violation(f"{field_path}.checked", "wrong_type", f"{field_path}.checked must be a boolean"))
    for container in ("details", "extras"):
        for field in GOVERNANCE_FIELDS:
            for path in _nested_key_paths(payload.get(container), field, path=container):
                violations.append(
                    _violation(
                        path,
                        "reserved_field",
                        f"{field} must be a literal top-level field",
                    )
                )
    if "completion_kind" in payload and payload["completion_kind"] is not None and payload["completion_kind"] not in {"implementation_ready", "tracked"}:
        violations.append(_violation("completion_kind", "invalid_value", "completion_kind must be implementation_ready or tracked"))
    if payload.get("completion_kind") in {"implementation_ready", "tracked"} and status != "done":
        violations.append(_violation("completion_kind", "invalid_status", "completion_kind requires status done"))
    if "qa_verdict" in payload and payload["qa_verdict"] is not None and payload["qa_verdict"] not in {"accept", "reject"}:
        violations.append(_violation("qa_verdict", "invalid_value", "qa_verdict must be accept or reject"))
    if "target_sha" in payload and (
        not isinstance(payload["target_sha"], str)
        or FULL_GIT_SHA.fullmatch(payload["target_sha"]) is None
    ):
        violations.append(_violation("target_sha", "invalid_value", "target_sha must be a full 40-hex git SHA"))
    if qa_grade and terminal:
        if "qa_verdict" not in payload:
            violations.append(_violation("qa_verdict", "missing_field", "missing field: qa_verdict", missing=True))
        elif payload.get("qa_verdict") is None:
            violations.append(_violation("qa_verdict", "invalid_value", "qa_verdict must be accept or reject"))
        if "target_sha" not in payload:
            violations.append(_violation("target_sha", "missing_field", "missing field: target_sha", missing=True))
    if "qa_attestation" in payload:
        attestation = payload["qa_attestation"]
        if payload.get("completion_kind") != "implementation_ready":
            violations.append(_violation(
                "qa_attestation",
                "invalid_dependency",
                "qa_attestation requires completion_kind=implementation_ready",
            ))
        if not isinstance(attestation, dict):
            violations.append(_violation("qa_attestation", "wrong_type", "qa_attestation must be an object"))
        else:
            expected = {"stream_id", "report_id"}
            for field in sorted(expected - set(attestation)):
                violations.append(_violation(f"qa_attestation.{field}", "missing_field", f"missing field: qa_attestation.{field}", missing=True))
            for field in sorted(set(attestation) - expected):
                violations.append(_violation(f"qa_attestation.{field}", "unexpected_field", f"unexpected field: qa_attestation.{field}"))
            for field in sorted(expected & set(attestation)):
                value = attestation[field]
                if not isinstance(value, str):
                    violations.append(_violation(f"qa_attestation.{field}", "wrong_type", f"qa_attestation.{field} has wrong type"))
                elif not value.strip():
                    violations.append(_violation(f"qa_attestation.{field}", "invalid_required_string", f"qa_attestation.{field} must be non-empty"))

    if enforce_inline_caps:
        cap_violations = inline_size_violations(payload)
        violations.extend(cap_violations)

    if violations:
        raise SchemaError(
            "schema_error" if unknown else ("missing_reason" if missing_reason else "schema_error"),
            "; ".join(str(v["detail"]) for v in violations),
            violations=violations,
        )

    return ReportPayloadV1(
        status=status,
        summary=payload.get("summary"),
        findings=payload.get("findings") if isinstance(payload.get("findings"), list) else None,
        next_action=payload.get("next_action"),
        details=payload.get("details"),
        extras=payload.get("extras"),
        reason=payload.get("reason") if isinstance(payload.get("reason"), str) and payload.get("reason") else None,
        completion_kind=payload.get("completion_kind") if payload.get("completion_kind") in {"implementation_ready", "tracked"} else None,
        qa_verdict=payload.get("qa_verdict") if payload.get("qa_verdict") in {"accept", "reject"} else None,
        target_sha=payload.get("target_sha") if isinstance(payload.get("target_sha"), str) else None,
        qa_attestation=payload.get("qa_attestation") if isinstance(payload.get("qa_attestation"), dict) else None,
        ac_claim=payload.get("ac_claim") if isinstance(payload.get("ac_claim"), dict) else None,
    )


def inline_size_violations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for field, limit in INLINE_FIELD_CAPS.items():
        if field not in payload:
            continue
        value = payload[field]
        actual = _string_size(value) if isinstance(value, str) else _json_size(value)
        if actual > limit:
            violations.append(_violation(field, "too_large", f"{field} exceeds {limit} bytes", limit=limit, actual=actual))
    total = _json_size(payload)
    if total > INLINE_TOTAL_CAP:
        violations.append(_violation("total", "too_large", f"payload exceeds {INLINE_TOTAL_CAP} bytes", limit=INLINE_TOTAL_CAP, actual=total))
    return violations
