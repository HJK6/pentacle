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

from . import summary_prose
from .summary_prose import placeholder, stage_lead, plain  # noqa: F401 - rule names in the contract

_RELEASE_BYTES = Path(__file__).read_bytes()


def nonterminal(status):
    return status not in {"completed", "deprecated"}


def always(status):
    return True


def blocked(status):
    return status == "blocked"


REQUIRED = {"always": always, "nonterminal": nonterminal, "blocked": blocked}


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    wire_key: str
    description: str
    required_when: Callable[[str], bool] = nonterminal
    limit: int = 240
    validators: tuple = (placeholder,)


def _contract_field(field_id, wire_key, description):
    # Limits, rules and requirement come from the shared contract fixture.
    spec = summary_prose.FIELDS[field_id]
    return Field(field_id, spec["label"], wire_key, description, REQUIRED[spec["required"]], spec["limit"],
                 tuple(summary_prose.RULES[rule] for rule in spec["rules"]))


FIELDS = (
    _contract_field("status", "status_text", "The present situation in plain words, without a stage name."),
    _contract_field("next_action", "next_action_text", "The concrete next action or resume condition in plain words."),
    _contract_field("blockers", "blockers_text", "The impediment and who or what it waits on, in plain words."),
)


def policy_revision(fields=None):
    return _policy_revision(tuple(FIELDS if fields is None else fields))


@lru_cache(maxsize=32)
def _policy_revision(fields):
    # The released module bytes and the loaded contract digest bind validator
    # logic and rule constants;
    # declarations additionally bind injected policies in substitution tests.
    def rule(fn):
        return [fn.__name__, inspect.getsource(fn)]
    declared = [[f.id, f.label, f.wire_key, f.description, f.limit, rule(f.required_when),
                 [rule(v) for v in f.validators]] for f in fields]
    return hashlib.sha256(_RELEASE_BYTES + summary_prose.CONTRACT_DIGEST.encode()
                          + json.dumps(declared, sort_keys=True).encode()).hexdigest()


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
                                 "fix": f"summary.md: write one **{field.label}** line (1-{field.limit} characters, no semicolons, ids, paths or joined letter-digit tokens). {field.description}"})
    revision = hashlib.sha256(json.dumps([status, canonical], sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":")).encode()).hexdigest()
    return {**values, "checkpoint_state": "invalid" if deficiencies else "valid",
            "checkpoint_deficiencies": deficiencies, "checkpoint_revision": revision,
            "checkpoint_policy_revision": policy_revision(fields)}


def bounded_prose(text, limit=2000):
    value = " ".join(str(text or "").split())
    return {"text": value[:limit] or None, "truncated": len(value) > limit}
