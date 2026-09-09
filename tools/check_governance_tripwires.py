#!/usr/bin/env python3
"""Validate small, public change-record artifacts.

The checker intentionally validates only the fields declared by an artifact:
review mode checks one explicit governance scope and its ruling, retro mode
checks measured deletion arithmetic, and v2-gate-tag mode checks a compact
machine-readable build annotation. It does not inspect repositories or run
commands on a host.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from datetime import datetime
from pathlib import Path


_SCOPE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?Governance\s+scope:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RULING_RE = re.compile(
    r"^\s*(?:[-*]\s*)?Governance\s+ruling:\s*(?P<value>.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RATIO_RE = re.compile(
    r"^\s*(?:[-*]\s*)?Deletion\s+ratio:\s*"
    r"(?P<removed>\d+)\s*(?:removed|deleted)?\s*/\s*"
    r"(?P<added>\d+)\s*(?:added|inserted)?\s*=\s*"
    r"(?P<percent>\d+(?:\.\d+)?)\s*%\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NONE_RATIO_RE = re.compile(
    r"^\s*(?:[-*]\s*)?Deletion\s+ratio:\s*none\s*\(\s*"
    r"0\s+lines?\s+(?:removed|deleted)\s*;\s*"
    r"no\s+deletion\s+work\s*\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ANGLE_PLACEHOLDER_RE = re.compile(r"<[^>\r\n]+>")
_FIELD_RE = re.compile(
    r"^(?P<name>necessity|reused\s+substrate|authority)\s*[:=]\s*(?P<value>.+)$",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_GOVERNANCE_WORDS = re.compile(r"remove|replace|public_", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_FIELDS = ("workflow_id", "run_id", "run_url", "headSha", "old_main_sha", "candidate_sha", "timestamp")


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _is_placeholder(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        not normalized
        or normalized in {"...", "<...>", "tbd", "todo", "n/a", "none", "not applicable"}
        or _ANGLE_PLACEHOLDER_RE.search(value) is not None
        or "<...>" in normalized
        or "todo" in normalized
        or "tbd" in normalized
    )


def _scope_requires_ruling(scope: str) -> bool:
    normalized = " ".join(scope.casefold().split())
    if not normalized or normalized.startswith("none"):
        return False
    if "no new" in normalized or "no-new" in normalized:
        return False
    if "subtractive" in normalized or "compatibility-only" in normalized:
        return False
    return bool(_GOVERNANCE_WORDS.search(normalized))


def _review_errors(text: str) -> list[str]:
    # Examples in HTML comments are documentation, not submitted fields.
    text = _HTML_COMMENT_RE.sub("", text)
    scopes = list(_SCOPE_RE.finditer(text))
    if not scopes:
        return []
    if len(scopes) != 1:
        return ["review body must contain exactly one Governance scope line"]

    scope = scopes[0].group("value").strip()
    if not scope:
        return ["Governance scope must declare none, subtractive/compatibility-only, or a new public change"]
    if not _scope_requires_ruling(scope):
        normalized = scope.casefold()
        if any(word in normalized for word in ("none", "no new", "subtractive", "compatibility-only")):
            return []
        return ["Governance scope must explicitly declare none, subtractive/compatibility-only, or a new public change"]

    rulings = list(_RULING_RE.finditer(text))
    if len(rulings) != 1:
        return ["a governance change requires exactly one Governance ruling line"]
    ruling = rulings[0].group("value").strip()
    if _is_placeholder(ruling):
        return ["Governance ruling cannot be blank or a placeholder"]

    fields: dict[str, str] = {}
    for segment in ruling.split(";"):
        match = _FIELD_RE.match(segment.strip())
        if match:
            name = re.sub(r"\s+", " ", match.group("name").casefold())
            fields[name] = match.group("value").strip()
    errors = []
    for name in ("necessity", "reused substrate", "authority"):
        if name not in fields or _is_placeholder(fields[name]):
            errors.append(f"Governance ruling needs a non-placeholder {name} field")
    return errors


def _retro_errors(text: str) -> list[str]:
    ratio_lines = list(_RATIO_RE.finditer(text))
    no_deletion_lines = list(_NONE_RATIO_RE.finditer(text))
    if len(ratio_lines) + len(no_deletion_lines) != 1:
        return ["change record must contain exactly one measured Deletion ratio line or explicit no-deletion line"]
    if no_deletion_lines:
        return []

    match = ratio_lines[0]
    removed = int(match.group("removed"))
    added = int(match.group("added"))
    reported = float(match.group("percent"))
    if added == 0:
        if removed != 0 or reported != 0:
            return ["a zero-added deletion ratio must be 0 removed / 0 added = 0%"]
        return []
    expected = removed / added * 100
    if not math.isclose(reported, expected, abs_tol=0.02):
        return [f"Deletion ratio percentage is {reported:g}%, expected {expected:.2f}%"]
    return []


def _v2_gate_tag_errors(text: str) -> list[str]:
    lines = [line for line in text.strip().splitlines() if line]
    fields: dict[str, str] = {}
    for line in lines:
        if ": " not in line:
            return ["v2-gate tag annotation lines must use `field: value`"]
        key, value = line.split(": ", 1)
        if key not in _TAG_FIELDS or not value or key in fields:
            return ["v2-gate tag annotation must contain each required field exactly once"]
        fields[key] = value
    if tuple(fields) != _TAG_FIELDS:
        return ["v2-gate tag annotation fields must be ordered and complete"]
    if not fields["workflow_id"].isdigit() or not fields["run_id"].isdigit():
        return ["v2-gate workflow_id and run_id must be numeric"]
    if not fields["run_url"].endswith(f"/actions/runs/{fields['run_id']}"):
        return ["v2-gate run_url must bind run_id"]
    if any(not _SHA_RE.fullmatch(fields[key]) for key in ("headSha", "old_main_sha", "candidate_sha")):
        return ["v2-gate SHA fields must be exact lowercase 40-character SHAs"]
    if fields["headSha"] != fields["candidate_sha"]:
        return ["v2-gate headSha and candidate_sha must match"]
    try:
        datetime.fromisoformat(fields["timestamp"].replace("Z", "+00:00"))
    except ValueError:
        return ["v2-gate timestamp must be ISO-8601"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=("review", "retro", "v2-gate-tag"))
    parser.add_argument("path", nargs="?", default="-", help="artifact path, or - for stdin")
    args = parser.parse_args(argv)
    try:
        text = _read(args.path)
    except OSError as exc:
        print(f"FAIL: cannot read {args.path}: {exc}", file=sys.stderr)
        return 2

    errors = _review_errors(text) if args.kind == "review" else _retro_errors(text) if args.kind == "retro" else _v2_gate_tag_errors(text)
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"PASS: {args.kind} governance artifact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
