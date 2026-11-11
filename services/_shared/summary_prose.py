"""Read the shared readable-fields contract; limits, patterns and cases live in the fixture."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "pentacle-chat-core/tests/fixtures/summary_prose_cases.json"
_CONTRACT_BYTES = CONTRACT_PATH.read_bytes()
CONTRACT = json.loads(_CONTRACT_BYTES)
CONTRACT_DIGEST = hashlib.sha256(_CONTRACT_BYTES).hexdigest()
PATTERNS = {key: re.compile(value) for key, value in CONTRACT["patterns"].items()}
FIELDS = CONTRACT["fields"]


def normalized(value):
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in value).split())


def placeholder(value):
    return "placeholder" if normalized(value) in CONTRACT["placeholders"] else None


def stage_lead(value):
    return "stage_lead" if PATTERNS["stage_lead"].search(value) else None


def plain(value):
    return "not_plain" if any(PATTERNS[key].search(value) for key in CONTRACT["plain"]) else None


RULES = {"placeholder": placeholder, "stage_lead": stage_lead, "plain": plain}


def violation(field_id: str, value: str | None) -> str | None:
    """Return the first failing rule code for one field value, or None."""
    value = (value or "").strip()
    if not value:
        return "empty"
    if len(value) > FIELDS[field_id]["limit"]:
        return "too_long"
    return next((code for rule in FIELDS[field_id]["rules"] if (code := RULES[rule](value))), None)


def field_line(field_id: str, value: str | None) -> str | None:
    """The whole authored value when it passes the contract, otherwise None."""
    return None if violation(field_id, value) else value.strip()
