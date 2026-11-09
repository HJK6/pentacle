"""Released checkpoint field/rule policy. Orchestration consumes its result.

Add/remove fields here; parsers, detail projection and readiness use the same
declarations. No clocks, I/O, or semantic truth claims belong in validators.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import inspect
from functools import lru_cache
from typing import Callable

_RELEASE_BYTES = Path(__file__).read_bytes()


def nonterminal(status):
    return status not in {"completed", "deprecated"}


def always(status):
    return True


def blocked(status):
    return status == "blocked"


def normalized(value):
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in value).split())


def placeholder(value):
    return "placeholder" if normalized(value) in {"todo", "tbd", "placeholder"} else None


def lifecycle_only(value):
    return "boilerplate" if normalized(value) in {
        "backlog", "analysis", "ready for dev", "ready for development", "in progress", "needs qa",
        "blocked", "completed", "deprecated", "paused", "active", "done"} else None


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    wire_key: str
    description: str
    required_when: Callable[[str], bool] = nonterminal
    limit: int = 240
    validators: tuple = (placeholder,)


FIELDS = (
    Field("progress", "Progress", "progress_text", "Latest achieved result, or honestly no result yet."),
    Field("status", "Status", "status_text", "Present situation beyond a lifecycle label.", always,
          validators=(placeholder, lifecycle_only)),
    Field("next_action", "Next action", "next_action_text", "Concrete next action or resume condition."),
    Field("blockers", "Blockers", "blockers_text", "Impediment and responsible party or dependency.", blocked),
)


def policy_revision(fields=None):
    return _policy_revision(tuple(FIELDS if fields is None else fields))


@lru_cache(maxsize=32)
def _policy_revision(fields):
    # The released module bytes bind validator logic and global rule constants;
    # declarations additionally bind injected policies in substitution tests.
    def rule(fn):
        return [fn.__name__, inspect.getsource(fn)]
    declared = [[f.id, f.label, f.wire_key, f.description, f.limit, rule(f.required_when),
                 [rule(v) for v in f.validators]] for f in fields]
    return hashlib.sha256(_RELEASE_BYTES + json.dumps(declared, sort_keys=True).encode()).hexdigest()


def detail_field_keys(fields=None):
    return {f.wire_key for f in (FIELDS if fields is None else fields)} - {"status_text", "next_action_text"}


def parse_checkpoint(summary, status, *, fields=None):
    """Summary is already fence-stripped by the shared source parser."""
    fields = FIELDS if fields is None else fields
    if len(fields) > 8:
        raise ValueError("checkpoint_policy_field_limit")
    values, canonical, deficiencies = {}, {}, []
    for field in fields:
        found = re.findall(rf"^\*\*{re.escape(field.label)}\*\*[ \t]*[—–-][ \t]*(.*)$", summary, re.M)
        found = [value.strip() for value in found]
        if found:
            canonical[field.id] = found
        code = None
        if len(found) > 1:
            code = "duplicate"
        elif not found or not found[0]:
            if found or field.required_when(status):
                code = "missing" if not found else "empty"
        elif len(found[0]) > field.limit:
            code = "too_long"
        else:
            code = next((error for validator in field.validators if (error := validator(found[0]))), None)
        values[field.wire_key] = found[0] if len(found) == 1 and found[0] and not code else None
        if code:
            deficiencies.append({"field": field.wire_key, "code": field.id + "_" + code,
                                 "fix": f"summary.md: write one **{field.label}** line (1-{field.limit} characters). {field.description}"})
    revision = hashlib.sha256(json.dumps([status, canonical], sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()
    return {**values, "checkpoint_state": "invalid" if deficiencies else "valid",
            "checkpoint_deficiencies": deficiencies, "checkpoint_revision": revision,
            "checkpoint_policy_revision": policy_revision(fields)}


def bounded_prose(text, limit=2000):
    value = " ".join(str(text or "").split())
    return {"text": value[:limit] or None, "truncated": len(value) > limit}
