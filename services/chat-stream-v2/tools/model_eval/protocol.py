"""Report payload schema check and protocol detection from the call log."""
import json

REPORT_KEYS = {"summary", "findings", "next_action", "details", "extras"}
SEVERITIES = {"blocking", "major", "minor", "info"}


def validate_report_payload(payload):
    """Return a list of schema errors for a report payload (empty when valid)."""
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    errors = [f"unknown top-level key: {k}" for k in sorted(set(payload) - REPORT_KEYS)]
    for key in ("summary", "next_action"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} must be a non-empty string")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be an array")
        return errors
    for i, item in enumerate(findings):
        if not isinstance(item, dict):
            errors.append(f"findings[{i}] must be an object")
            continue
        if item.get("severity") not in SEVERITIES:
            errors.append(f"findings[{i}].severity must be one of {sorted(SEVERITIES)}")
        for key in ("where", "issue"):
            if not isinstance(item.get(key), str):
                errors.append(f"findings[{i}].{key} must be a string")
        fix = item.get("suggested_fix")
        if fix is not None and not isinstance(fix, str):
            errors.append(f"findings[{i}].suggested_fix must be a string or null")
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
