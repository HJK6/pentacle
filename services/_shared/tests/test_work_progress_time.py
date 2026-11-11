"""Member progress time: only a stage change, a checked-count rise or a valid
Status change under one policy revision is progress."""
import hashlib

from _shared.specs_parser import parse_work_facts
from _shared.work_observations import advance_observation

ID = "spec_demo__progress"
T0, T1 = 1_800_000_000.0, 1_800_003_600.0
STAMP1 = "2027-01-15T09:00:00Z"


def texts(*, checked=0, status_line="Inspect the fixture.", estimate="2–4 (median 3) as_of 2026-11-10",
          criteria="Assemble", epic=None):
    boxes = "\n".join(f"- [{'x' if i < checked else ' '}] {criteria} {i}." for i in range(3))
    metadata = f"epic: {epic}\n" if epic else ""
    spec = (f"---\nid: {ID}\ntitle: Progress\n{metadata}---\n## Estimate\n\n- remaining_work_h: {estimate}\n\n"
            f"## Acceptance Criteria\n\n### Custom\n\n{boxes}\n")
    summary = f"---\n{metadata}---\n" + (f"**Status** — {status_line}\n" if status_line is not None else "") + \
        "**Next action** — Review the fixture.\n"
    return spec, summary


def candidate(status="in_progress", **kwargs):
    spec, summary = texts(**kwargs)
    facts = parse_work_facts(spec, summary, status)
    digest = hashlib.sha256((status + spec + summary).encode()).hexdigest()
    return {"spec_id": ID, "quality": "fresh", "facts": facts, "source_hash": digest, "path": "/fixture", "error": None}


def advance(previous, cand, now):
    return advance_observation(ID, previous, cand, now=now, settle_s=0, sweep=True)


def first(**kwargs):
    record, _ = advance(None, candidate(**kwargs), T0)
    return record


def test_new_record_starts_unknown():
    assert first()["progress_at"] is None


def test_stage_change_and_checked_rise_are_progress():
    record, change = advance(first(), candidate(status="needs_qa"), T1)
    assert record["progress_at"] == STAMP1
    record, change = advance(first(), candidate(checked=2), T1)
    assert record["progress_at"] == STAMP1
    assert change["prior"]["ac_checked"] == 0 and change["next"]["ac_checked"] == 2


def test_checked_drop_is_not_progress():
    record, _ = advance(first(checked=2), candidate(checked=1), T1)
    assert record["progress_at"] is None


def test_null_to_valid_status_sets_a_baseline_only():
    record, change = advance(first(status_line=None), candidate(), T1)
    assert record["progress_at"] is None
    assert change["prior"]["status_line"] is None and change["next"]["status_line"] == "Inspect the fixture."


def test_invalid_to_valid_status_is_a_baseline_and_valid_to_valid_is_progress():
    record, _ = advance(first(status_line="Inspect spec_demo__x."), candidate(), T1)
    assert record["progress_at"] is None
    record, change = advance(first(), candidate(status_line="Assemble the span."), T1)
    assert record["progress_at"] == STAMP1
    assert change["prior"]["status_line"] == "Inspect the fixture."
    assert change["next"]["status_line"] == "Assemble the span."
    assert change["prior"]["blocked_line"] is None and "blocked_line" in change["next"]


def test_policy_only_reparse_is_not_progress_and_has_no_comparable_prior_line():
    persisted = first()
    # A persisted record parsed under an older policy, whose line differed.
    persisted["last_good"].update(checkpoint_policy_revision="old-policy", status_line="Older parse of the line.")
    record, change = advance(persisted, candidate(), T1)
    assert record["progress_at"] is None
    assert change is None
    # A material edit in the same read still cannot produce a comparable line.
    record, change = advance(persisted, candidate(estimate="3–5 (median 4) as_of 2026-11-11"), T1)
    assert record["progress_at"] is None
    assert change["prior"]["status_line"] is None


def test_other_edits_are_not_progress():
    base = first(checked=1)
    for edited in (candidate(checked=1),
                   candidate(checked=1, estimate="3–5 (median 4) as_of 2026-11-11"),
                   candidate(checked=1, criteria="Reword"),
                   candidate(checked=1, epic="epic_other")):
        record, _ = advance(base, edited, T1)
        assert record["progress_at"] is None
    earlier, _ = advance(first(), candidate(status="needs_qa"), T0 + 60)
    record, _ = advance(earlier, candidate(status="needs_qa", estimate="3–5 (median 4) as_of 2026-11-11"), T1)
    assert record["progress_at"] == "2027-01-15T08:01:00Z"


def test_stale_reads_keep_progress_time():
    progressed, _ = advance(first(), candidate(status="needs_qa"), T1)
    record, _ = advance(progressed, {"quality": "stale", "error": "work_files_unsettled"}, T1 + 60)
    assert record["progress_at"] == STAMP1


def test_first_valid_estimate_and_spec_summary_line_are_facts():
    spec, summary = texts()
    spec = spec.replace("- remaining_work_h: ", "- remaining_work_h: draft\n- remaining_work_h: 1–2 (median 1.5) as_of 2026-11-01\n- remaining_work_h: ")
    spec = spec.replace("title: Progress\n", "title: Progress\nsummary: The daemon sends the fixture facts.\n")
    facts = parse_work_facts(spec, summary.replace("---\n---\n", "---\nblocked_by:\n- spec_demo__other\n---\n"), "blocked")
    assert facts["estimate_first"] == {"p25": 1.0, "p75": 2.0, "median": 1.5}
    assert facts["estimate"]["median"] == 3.0
    assert facts["summary_line"] == "The daemon sends the fixture facts."
    assert facts["_blocked_by"] == ["spec_demo__other"]
    bad = parse_work_facts(spec.replace("sends the fixture facts", "reads spec_demo__x"), summary, "in_progress")
    assert bad["summary_line"] is None and bad["_blocked_by"] is None
