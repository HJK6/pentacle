"""Report payload schema check and protocol detection from the call log."""
import json
import os
import re
import sys

_SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
if _SERVICES not in sys.path:
    sys.path.insert(0, _SERVICES)
from _shared import report_payload_v1  # noqa: E402  the authoritative payload schema

DIGEST = re.compile(r"[a-f0-9]{64}")


def validate_report_payload(payload, status="done", flags=None):
    """Return a list of schema errors for a report (empty when valid).

    Applies the authoritative ReportPayloadV1 validator after merging the
    structured flags (`qa_verdict`, `target_sha`, `completion_kind`) the way the
    real CLI does; a QA review flag set also needs scope and evidence digest.
    """
    flags = flags or {}
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    merged = dict(payload)
    for name in ("qa_verdict", "target_sha", "completion_kind"):
        if flags.get(name) is not None:
            if name in merged:
                return [f"--{name.replace('_', '-')} conflicts with {name} in --result"]
            merged[name] = flags[name]
    errors = []
    # Like the real CLI, QA review evidence is required only once a scope or digest flag is given.
    if flags.get("qa_reviewed_scope") is not None or flags.get("qa_gate_evidence_digest") is not None:
        for key, flag in (("target_sha", "--target-sha"), ("qa_reviewed_scope", "--qa-reviewed-scope"),
                          ("qa_gate_evidence_digest", "--qa-gate-evidence-digest")):
            if flags.get(key) is None:
                errors.append(f"QA review evidence requires {flag}")
        digest = flags.get("qa_gate_evidence_digest")
        if digest is not None and not DIGEST.fullmatch(digest):
            errors.append("--qa-gate-evidence-digest must be a lowercase 64-hex SHA-256")
        if flags.get("qa_reviewed_scope") is not None and not flags["qa_reviewed_scope"].strip():
            errors.append("--qa-reviewed-scope must be non-empty")
    try:
        report_payload_v1.validate(merged, status, enforce_inline_caps=True)
    except report_payload_v1.SchemaError as exc:
        errors.append(str(exc))
        errors.extend(f"{v.get('field')}: {v.get('detail')}" for v in exc.violations)
    return errors


def read_log(path):
    """Read the call log written by the fake orchestration CLI."""
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except FileNotFoundError:
        pass
    return entries


def _tell_marker(entry):
    # `tell <stream> <text...>`: the marker is the first word of the text.
    text = " ".join(entry.get("argv", [])[2:]).strip().upper()
    return text.split(None, 1)[0].rstrip(":") if text else ""


def valid_reports(entries):
    return [e for e in entries
            if e.get("cmd") == "report" and e.get("valid") and e.get("status") == "done"]


def reported_verdict(entries):
    """The last valid done report's QA verdict, or None."""
    for entry in reversed(valid_reports(entries)):
        if entry.get("qa_verdict") in ("accept", "reject"):
            return entry["qa_verdict"]
    return None


def check_protocol(entries, qa):
    """`yes` when START and END were sent and a schema-valid report was filed
    (QA tasks: with a verdict)."""
    markers = {_tell_marker(e) for e in entries if e.get("cmd") == "tell"}
    reports = valid_reports(entries)
    if qa:
        reports = [e for e in reports if e.get("qa_verdict") in ("accept", "reject")]
    return "yes" if {"START", "END"} <= markers and reports else "no"
