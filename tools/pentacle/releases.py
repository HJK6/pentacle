"""Stable-release records, including byte-preserving append-only validation."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import subprocess
from pathlib import Path

OFFICIAL_REMOTE = "https://github.com/HJK6/pentacle.git"
MANIFEST = "releases/manifest.json"
SHA = re.compile(r"[0-9a-f]{40}\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
FIELDS = {"version", "commit", "date", "changelog_sections", "tested_update_from",
          "required_actions", "evidence_sha256"}


class ReleaseError(ValueError):
    pass


def git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError("Git could not complete the release check.") from None
    if result.returncode:
        raise ReleaseError("Git could not complete the release check.")
    return result.stdout


def ancestor(root: Path, older: str, newer: str) -> bool:
    try:
        result = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer],
                                capture_output=True, timeout=30, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def version_key(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ReleaseError("Release versions must be MAJOR.MINOR.PATCH without leading zeroes.")
    return tuple(map(int, value.split(".")))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("Duplicate manifest keys are not allowed.")
        result[key] = value
    return result


def parse(text: str) -> dict:
    try:
        data = json.loads(text, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ReleaseError("Invalid JSON number.")))
    except (ValueError, TypeError):
        raise ReleaseError("The release manifest is not valid JSON.") from None
    if not isinstance(data, dict) or set(data) != {"schema_version", "releases"}:
        raise ReleaseError("The manifest must contain schema_version and releases.")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1 or not isinstance(data["releases"], list):
        raise ReleaseError("Unsupported release manifest schema.")
    previous = None
    commits = set()
    known = set()
    for entry in data["releases"]:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise ReleaseError("A release entry has missing or unknown fields.")
        key = version_key(entry["version"])
        if previous is not None and key <= previous:
            raise ReleaseError("Release versions must be unique and strictly increasing.")
        previous = key
        commit = entry["commit"]
        if not isinstance(commit, str) or not SHA.fullmatch(commit) or commit in commits:
            raise ReleaseError("Release commits must be unique full lowercase Git SHAs.")
        commits.add(commit)
        try:
            date = entry["date"]
            if not isinstance(date, str) or dt.date.fromisoformat(date).isoformat() != date:
                raise ValueError
        except (ValueError, TypeError):
            raise ReleaseError("Release dates must be UTC YYYY-MM-DD dates.") from None
        for name in ("changelog_sections", "tested_update_from", "required_actions"):
            values = entry[name]
            if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or "\n" in v or "\r" in v for v in values):
                raise ReleaseError("Release lists must contain nonempty single-line strings.")
            if len(values) != len(set(values)):
                raise ReleaseError("Release lists must not repeat values.")
        starts = entry["tested_update_from"]
        if not starts or any(not SHA.fullmatch(v) and v not in known for v in starts):
            raise ReleaseError("Tested starts must name earlier stable versions or full commits.")
        if not isinstance(entry["evidence_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["evidence_sha256"]):
            raise ReleaseError("Release evidence must have a SHA256 digest.")
        if any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \((?:[2-9]|[1-9][0-9]+)\))?", heading) for heading in entry["changelog_sections"]):
            raise ReleaseError("Changelog sections must name dated headings.")
        known.add(entry["version"])
    return data


def entry_spans(text: str) -> tuple[list[str], int]:
    """Return exact JSON object tokens and the releases array closing offset.

    Parsing has already rejected duplicate keys. Walk top-level tokens rather
    than matching a string that could also occur inside a release action.
    """
    decoder = json.JSONDecoder()
    cursor = text.index("{") + 1
    while True:
        while text[cursor].isspace() or text[cursor] == ",":
            cursor += 1
        key, cursor = decoder.raw_decode(text, cursor)
        while text[cursor].isspace() or text[cursor] == ":":
            cursor += 1
        if key == "releases":
            break
        _, cursor = decoder.raw_decode(text, cursor)
    cursor += 1  # '['
    spans = []
    while True:
        while text[cursor].isspace() or text[cursor] == ",":
            cursor += 1
        if text[cursor] == "]":
            return spans, cursor
        start = cursor
        _, cursor = decoder.raw_decode(text, cursor)
        spans.append(text[start:cursor])


def verify(root: Path, ref: str, base: str | None = None) -> dict:
    tip = git(root, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
    text = git(root, "show", f"{tip}:{MANIFEST}")
    data = parse(text)
    for entry in data["releases"]:
        if not ancestor(root, entry["commit"], tip):
            raise ReleaseError("A release commit is not an ancestor of the checked ref.")
    if base:
        base_sha = git(root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").strip()
        # Introducing the manifest is legal. All other failures remain errors.
        paths = git(root, "ls-tree", "--name-only", base_sha, "--", MANIFEST).splitlines()
        if MANIFEST in paths:
            old_text = git(root, "show", f"{base_sha}:{MANIFEST}")
            parse(old_text)
            old, _ = entry_spans(old_text)
            new, _ = entry_spans(text)
            if new[:len(old)] != old:
                raise ReleaseError("An earlier release entry changed or was removed.")
    return data


def add(root: Path, *, version: str, commit: str, date: str, sections: list[str],
        starts: list[str], actions: list[str], evidence: Path) -> dict:
    path = root / MANIFEST
    text = path.read_text(encoding="utf-8")
    data = parse(text)
    if not SHA.fullmatch(commit) or not ancestor(root, commit, "HEAD"):
        raise ReleaseError("The release commit must already be an ancestor of HEAD.")
    try:
        digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    except OSError:
        raise ReleaseError("A readable upgrade-journey evidence file is required.") from None
    headings = {line[3:] for line in git(root, "show", f"{commit}:CHANGELOG.md").splitlines() if line.startswith("## ")}
    if any(section not in headings for section in sections):
        raise ReleaseError("A requested changelog section does not exist at the release commit.")
    if data["releases"]:
        previous = data["releases"][-1]
        if not ({previous["version"], previous["commit"]} & set(starts)):
            raise ReleaseError("The previous stable release must be a directly tested start.")
    entry = dict(version=version, commit=commit, date=date, changelog_sections=sections,
                 tested_update_from=starts, required_actions=actions, evidence_sha256=digest)
    parse(json.dumps({"schema_version": 1, "releases": [*data["releases"], entry]}))
    spans, close = entry_spans(text)
    # Append without reserializing old entries: their original bytes are policy.
    prefix = text[:close].rstrip()
    new_text = prefix + ("," if spans else "") + "\n    " + json.dumps(entry, ensure_ascii=False) + "\n  " + text[close:]
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(new_text, encoding="utf-8")
    temporary.replace(path)
    return entry
