"""Validate a spec's existing lane display fields at an owner checkpoint.

The lane UI already renders acceptance counts, status, next action and the
remaining estimate parsed by ``parse_work_facts``. This module only reports
which of those existing fields would be missing or malformed, with the exact
line to fix. It reads the current files, never a cached observation, and never
certifies that a checkbox is true.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import hashlib
from typing import Any

from .specs_parser import _section, _without_fences, parse_work_facts
from .specs_service import SOURCE_READ_ERRORS, _decode_identity_bytes, _input_token

ESTIMATE_FORMS = ("`- remaining_work_h: <p25>–<p75> (median <m>) as_of YYYY-MM-DD [provisional]`, "
                  "`- remaining_work_h: n/a — umbrella|coordination`, or "
                  "`- remaining_work_h: unknown — <reason>`")
# Recognized here only: the parser and wire keep an explicit unknown as a null estimate.
_UNKNOWN = re.compile(r"unknown[ \t]+—[ \t]+\S.*")


def _last_remaining_declaration(spec_text: str) -> str | None:
    estimate = _section(_without_fences(spec_text), "Estimate") or ""
    found = re.findall(r"^[ \t]*(?:- )?remaining_work_h:[ \t]*(.*)$", estimate, re.M)
    return found[-1].strip() if found else None


def checkpoint_deficiencies(spec_text: str, summary_text: str,
                            status: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    facts = parse_work_facts(spec_text, summary_text, status)
    found: list[dict[str, str]] = []
    if facts["terminal"] is None and not facts["ac_total"]:
        found.append({"field": "acceptance", "code": "acceptance_missing",
                      "fix": "spec.md: add `## Acceptance Criteria` with `- [ ] <criterion>` lines"})
    found.extend(facts["checkpoint_deficiencies"])
    if facts["terminal"] is None:
        declared = _last_remaining_declaration(spec_text)
        if facts["estimate"] is None and not facts["estimate_exempt"] and not (
                declared and _UNKNOWN.fullmatch(declared)):
            if declared is None:
                found.append({"field": "estimate", "code": "estimate_missing",
                              "fix": "spec.md `## Estimate`: append " + ESTIMATE_FORMS})
            else:
                found.append({"field": "estimate", "code": "estimate_invalid",
                              "fix": "spec.md: repair the last remaining_work_h declaration. Use a range with median/as_of, n/a for umbrella/coordination, or unknown with a reason."})
    return facts, found


def work_checkpoint(spec_id: str, *, root: str | Path | None = None) -> dict[str, Any]:
    """Return ``{state: valid|invalid|unverifiable, facts, deficiencies, error}`` from the current files."""
    from .spec_reader import SpecReader
    try:
        reader = SpecReader(root, identity_only=True)
        status, folder, _identity, spec_text = reader.read(spec_id)
        summary_path = Path(folder) / "summary.md"
        summary = reader.service._read_identity_input(summary_path)
        if summary[1] is None:
            raise FileNotFoundError(summary_path)
        summary_text = _decode_identity_bytes(summary[1])
        # Seal the consumed pair, including summary body changes and a spec
        # publication while the summary was read. Never mix their revisions.
        reader.read(spec_id)
        if _input_token(summary) != _input_token(reader.service._read_identity_input(summary_path)):
            raise ValueError("spec_changed")
        changed = max(reader.consumed[Path(folder) / "spec.md"].fingerprint[4], summary[0][4]) / 1e9
    except (ValueError, *SOURCE_READ_ERRORS) as exc:
        return {"state": "unverifiable", "spec_id": spec_id, "facts": None, "deficiencies": [],
                "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
    facts, found = checkpoint_deficiencies(spec_text, summary_text, status)
    facts["source_changed_at"] = datetime.fromtimestamp(changed, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"state": "invalid" if found else "valid", "spec_id": spec_id, "facts": facts,
            "deficiencies": found, "error": None, "policy_revision": facts["checkpoint_policy_revision"],
            "source_revisions": {"spec.md": hashlib.sha256(reader.consumed[Path(folder) / "spec.md"].content).hexdigest(),
                                 "summary.md": hashlib.sha256(summary[1]).hexdigest()}}
