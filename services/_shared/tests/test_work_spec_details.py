"""File observations for structured spec details, with synthetic source text."""
import json
import re
from pathlib import Path

import pytest

from _shared.specs_parser import parse_work_facts
from _shared.summary_prose import CONTRACT_PATH, summary_line
from _shared.work_observations import MATERIAL_FIELDS, advance_observation


def spec(body="", status="in_progress", created="2026-01-01"):
    return f"---\nid: spec_demo__detail\nstatus: {status}\ncreated_at: '{created}'\n---\n{body}"


CASES = json.loads(CONTRACT_PATH.read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_prose_contract(case):
    assert summary_line(case["label"]) == case["daemon_line"]


def test_parser_keeps_legacy_text_and_derives_clean_lines():
    result = parse_work_facts(spec(), "**Status** — Tests pass. Review pending\n**Next action** — Review scripts/check.py now.", "in_progress")
    assert result["status_text"] == "Tests pass. Review pending"
    assert result["next_action_text"] == "Review scripts/check.py now."
    assert result["status_line"] == "Tests pass."
    assert result["next_line"] is None


def test_loader_does_not_define_inline_regexes():
    import _shared.summary_prose as loader
    text = Path(loader.__file__).read_text()
    assert not re.search(r"re\.(?:compile|search|match|fullmatch|finditer|sub)\(\s*[rf]*[\"']", text)


def test_custom_criteria_order_dispositions_and_bounds():
    body = """## Tracking
- parked: 2026-02-01 operator:decision — Waiting for a prerequisite.
## Acceptance Criteria
### Common
- [x] **Common check** — not included.
### Custom
- [ ] **Title** — details.
- [x] Done `code`.
- [ ] Waived check (waived: Superseded).
- [ ] Tracked check tracked: spec_demo__followup (new_scope)
- [ ] `code` stays.
"""
    facts = parse_work_facts(spec(body, "blocked"), "", "blocked")
    assert facts["criteria"] == [
        {"title": "Title", "disposition": "parked"},
        {"title": "Done `code`.", "disposition": "done"},
        {"title": "Waived check", "disposition": "waived"},
        {"title": "Tracked check", "disposition": "tracked"},
        {"title": "`code` stays.", "disposition": "parked"}]
    assert parse_work_facts(spec(body), "", "in_progress")["criteria"][0]["disposition"] == "open"
    many = "## Acceptance Criteria\n### Custom\n" + "\n".join("- [ ] " + "z" * 121 for _ in range(41))
    criteria = parse_work_facts(spec(many), "", "in_progress")["criteria"]
    assert len(criteria) == 40 and all(item["title"] == "z" * 119 + "…" for item in criteria)


def test_transitions_map_leaving_dates_to_entered_status():
    body = """## Tracking
- summary transition: 2026-01-03; source lifecycle: backlog; fields refreshed.
- summary transition: 2026-01-06; source lifecycle: analysis; fields refreshed.
"""
    assert parse_work_facts(spec(body), "", "in_progress")["transitions"] == [
        {"status": "backlog", "at": "2026-01-01"}, {"status": "analysis", "at": "2026-01-03"},
        {"status": "in_progress", "at": "2026-01-06"}]
    assert parse_work_facts(spec(), "", "in_progress")["transitions"] == [{"status": "in_progress", "at": "2026-01-01"}]
    many = "## Tracking\n" + "\n".join(f"- summary transition: 2026-01-{i:02}; source lifecycle: analysis; moved." for i in range(2, 12))
    transitions = parse_work_facts(spec(many), "", "in_progress")["transitions"]
    assert len(transitions) == 8 and transitions[0]["at"] == "2026-01-04" and transitions[-1]["at"] == "2026-01-11"


def test_example_dispositions_and_invalid_parks_do_not_dispose_criteria():
    body = """## Tracking
- parked: 2026-02-30 operator:decision — Invalid calendar day.
## Acceptance Criteria
### Custom
- [ ] Example `(waived: explanatory text)`
- [ ] Example "(waived: explanatory text)"
- [ ] Example `tracked: spec_demo__followup (new_scope)`
- [ ] Empty waiver (waived: )
- [ ] Quoted reason (waived: "Superseded")
- [ ] Code reason (waived: `old`)
```
- [x] Fenced example
```
"""
    criteria = parse_work_facts(spec(body, "blocked"), "", "blocked")["criteria"]
    assert len(criteria) == 6 and all(item["disposition"] == "open" for item in criteria[:4])
    assert all(item["disposition"] == "waived" for item in criteria[4:])
    assert criteria[0]["title"] == "Example `(waived: explanatory text)`"


def test_only_file_details_extend_material_fields():
    assert MATERIAL_FIELDS == ("status", "ac_checked", "ac_total", "estimate", "estimate_exempt", "status_text", "next_action_text", "criteria", "transitions")
    def candidate(facts, digest):
        return {"quality": "fresh", "facts": facts, "source_hash": digest, "path": "/synthetic"}
    facts = parse_work_facts(spec("## Acceptance Criteria\n### Custom\n- [ ] First"), "", "in_progress")
    record, _ = advance_observation("spec_demo__detail", None, candidate(facts, "a"), now=1, settle_s=0, sweep=True)
    for field, changed in (("criteria", [{"title": "Second", "disposition": "open"}]),
                           ("transitions", [{"status": "in_progress", "at": "2026-01-02"}])):
        updated, event = advance_observation("spec_demo__detail", record, candidate({**facts, field: changed}, "b"), now=2, settle_s=0, sweep=True)
        assert updated["member"]["obs_rev"] == 2 and event is not None
    for field in ("icon", "worked", "lead_seat"):
        updated, event = advance_observation("spec_demo__detail", record, candidate({**facts, field: "changed"}, "c"), now=2, settle_s=0, sweep=True)
        assert updated["member"]["obs_rev"] == 1 and event is None
