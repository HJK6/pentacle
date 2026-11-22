"""Shared identifiers for prospective advisor-rule evidence."""

import re
import unicodedata


OPERATOR_ORIGIN = "registered_operator_ws_v1"
PATH = "docs/reference/assistant_bart_context.md"
ACCEPT_KEY = "advisor_rule_accept"
STAMP_FIELDS = frozenset({
    "advisor_stream_id", "advisor_generation", "fd_stream_id", "fd_generation",
    "fd_binding_revision", "operator_text_sha256",
})
REVIEW_FIELDS = frozenset({
    "version", "verdict", "operator_message_id", "fd_event_id", "path",
    "base_revision", "new_bytes_sha256", "scope",
})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_STREAM = re.compile(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+")
_GENERATION = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 200 and bool(value.strip())
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def _sha256(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def valid_principals(stamp):
    for role in ("fd", "advisor"):
        stream, generation = stamp.get(role + "_stream_id"), stamp.get(role + "_generation")
        if (not isinstance(stream, str) or len(stream) > 200 or not _STREAM.fullmatch(stream)
                or not isinstance(generation, str) or not _GENERATION.fullmatch(generation)):
            return False
    revision = stamp.get("fd_binding_revision")
    return (stamp["fd_stream_id"] != stamp["advisor_stream_id"]
            and type(revision) is int and revision >= 0 and _sha256(stamp.get("operator_text_sha256")))


def valid_review(value):
    if (not isinstance(value, dict) or value.keys() != REVIEW_FIELDS
            or type(value["version"]) is not int or value["version"] != 1
            or value["verdict"] != "accept" or value["path"] != PATH
            or not valid_id(value["operator_message_id"]) or not valid_id(value["fd_event_id"])
            or not _sha256(value["base_revision"]) or not _sha256(value["new_bytes_sha256"])):
        return False
    scope = value["scope"]
    if (not isinstance(scope, dict) or scope.keys() != {"mode", "removed", "added"}
            or scope["mode"] not in ("single", "batch", "reorganize")):
        return False
    for lines in (scope["removed"], scope["added"]):
        if not isinstance(lines, list) or not all(map(_sha256, lines)) or lines != sorted(lines):
            return False
    if scope["mode"] == "single":
        removed, added = len(scope["removed"]), len(scope["added"])
        return max(removed, added) == 1
    return True


def valid_accept(stamp, report):
    extras = report.get("extras")
    review = extras.get("advisor_rule") if isinstance(extras, dict) else None
    return (isinstance(stamp, dict) and stamp.keys() == REVIEW_FIELDS | STAMP_FIELDS
            and valid_review(review) and review == {key: stamp[key] for key in REVIEW_FIELDS}
            and valid_principals(stamp) and valid_id(report.get("report_id"))
            and report.get("status") == "progress" and type(report.get("msg_id")) is int
            and report["msg_id"] == 0
            and all(report.get(key) is None for key in (
                "completion_kind", "qa_verdict", "target_sha", "qa_attestation", "ac_claim",
            ))
            and report.get("from_stream_id") == stamp["advisor_stream_id"]
            and report.get("session_generation") == stamp["advisor_generation"]
            and report.get("provenance_version") == "v1"
            and report.get("provenance_generation") == stamp["advisor_generation"])
