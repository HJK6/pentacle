"""Owner checkpoint validation of the existing display fields (lane tracking minimal, B1)."""
import pytest

from _shared.work_checkpoint import checkpoint_deficiencies, work_checkpoint

SPEC = """---
id: spec_demo__paper_bridge
title: Build a paper bridge
---
## Acceptance Criteria
- [x] Cut the supports.
- [ ] Attach the deck.

## Estimate
- elapsed_delivery_h: 4–8 (median 6)
{estimate}
"""
SUMMARY = """---
id: work_demo__paper_bridge
---
**Status** — Supports cut.

**Next action** — Attach the deck.
"""
PROSE_SUMMARY = """---
id: work_demo__paper_bridge
---
The supports are cut and the deck waits on glue, so attach it next.
"""
VALID = "- remaining_work_h: 1–3 (median 2) as_of 2026-10-09"


def codes(spec, summary=SUMMARY, status="in_progress"):
    return [d["code"] for d in checkpoint_deficiencies(spec, summary, status)[1]]


def test_reproduced_display_gap_names_each_exact_fix():
    # Invented record with the observed shape: comma before provisional, prose summary.
    spec = SPEC.format(estimate="- remaining_work_h: 2–5 (median 3) as_of 2026-10-15, provisional")
    facts, found = checkpoint_deficiencies(spec, PROSE_SUMMARY, "blocked")
    assert (facts["ac_checked"], facts["ac_total"]) == (1, 2)
    assert [d["code"] for d in found] == ["status_missing", "next_action_missing", "estimate_invalid"]
    by_code = {d["code"]: d["fix"] for d in found}
    assert "**Status** —" in by_code["status_missing"]
    assert "**Next action** —" in by_code["next_action_missing"]
    assert "as_of 2026-10-15, provisional" in by_code["estimate_invalid"]
    assert "remaining_work_h: unknown — <reason>" in by_code["estimate_invalid"]


@pytest.mark.parametrize("line", [VALID, VALID + " provisional", "- remaining_work_h: n/a — umbrella",
                                  "- remaining_work_h: n/a — coordination",
                                  "- remaining_work_h: unknown — waiting on the vendor quote"])
def test_valid_and_explicit_unknown_estimates_pass(line):
    assert codes(SPEC.format(estimate=line)) == []


def test_explicit_unknown_stays_null_on_the_wire():
    from _shared.specs_parser import parse_work_facts
    facts = parse_work_facts(SPEC.format(estimate="- remaining_work_h: unknown — no comparables"), SUMMARY,
                             "in_progress")
    assert facts["estimate"] is None and facts["estimate_exempt"] is False


@pytest.mark.parametrize("line", ["", "- remaining_work_h: unknown", "- remaining_work_h: TBD",
                                  VALID + " includes verification"])
def test_missing_or_malformed_estimate(line):
    assert codes(SPEC.format(estimate=line)) == ["estimate_missing" if not line else "estimate_invalid"]


def test_terminal_requires_only_status():
    spec = SPEC.format(estimate="")
    summary = "---\nid: w\n---\n**Status** — Shipped 2026-10-09.\n"
    assert codes(spec, summary, "completed") == []
    assert codes(spec, "---\nid: w\n---\nDone.\n", "deprecated") == ["status_missing"]
    assert codes("---\nid: spec_demo__x\n---\nNo sections.\n", summary, "completed") == []


def test_acceptance_section_required():
    spec = "---\nid: spec_demo__x\n---\n## Estimate\n" + VALID + "\n"
    assert codes(spec) == ["acceptance_missing"]


def test_reads_current_files_and_reports_unverifiable(tmp_path):
    folder = tmp_path / "work" / "in_progress" / "demo__paper_bridge"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text(SPEC.format(estimate=VALID))
    (folder / "summary.md").write_text(SUMMARY)
    result = work_checkpoint("spec_demo__paper_bridge", root=tmp_path)
    assert result["state"] == "valid" and result["deficiencies"] == []
    assert result["facts"]["status"] == "in_progress" and result["facts"]["source_changed_at"]
    (folder / "summary.md").write_text(PROSE_SUMMARY)  # a just-saved edit is read fresh
    assert work_checkpoint("spec_demo__paper_bridge", root=tmp_path)["state"] == "invalid"
    assert work_checkpoint("spec_demo__absent", root=tmp_path)["state"] == "unverifiable"
    assert work_checkpoint("spec_demo__paper_bridge", root=tmp_path / "nowhere")["state"] == "unverifiable"
