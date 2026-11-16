"""Canonical checkpoint policy, including the observed malformed-next gap."""
from _shared.specs_parser import parse_work_facts
from dataclasses import replace
import pytest
from _shared import checkpoint_policy as policy

SPEC = "---\nid: spec_demo__visibility\n---\n## Goal\nExplain current work.\n"
SUMMARY = """---
id: work_demo__visibility
---
**Progress** — Finished the CI repair for v2.3; 12 fixtures pass.
**Status** — Waiting for the authorized release slot.
**Next action** — Run the final gate, then ask FD to relay the candidate.
"""


def test_authored_checkpoint_survives_punctuation_and_filter():
    facts = parse_work_facts(SPEC, SUMMARY, "in_progress")
    assert facts.get("checkpoint_state") == "valid"
    assert "progress_text" not in facts and [f.id for f in policy.FIELDS] == ["status", "next_action", "blockers"]
    assert facts.get("goal_text") == "Explain current work."


def test_malformed_next_label_is_diagnosed_not_silently_discarded():
    facts = parse_work_facts(SPEC, SUMMARY.replace("**Next action**", "**Next**"), "in_progress")
    assert facts.get("checkpoint_state") == "invalid"
    assert any(d["code"] == "next_action_missing" for d in facts["checkpoint_deficiencies"])


def test_duplicate_and_overlength_checkpoint_are_not_truncated_to_valid():
    text = SUMMARY + "**Status** — Another competing checkpoint.\n"
    facts = parse_work_facts(SPEC, text, "in_progress")
    assert facts.get("checkpoint_state") == "invalid"
    assert facts.get("checkpoint_deficiencies", [])[0]["code"] == "status_duplicate"
    facts = parse_work_facts(SPEC, SUMMARY.replace("Waiting for the authorized release slot.", "界" * 161), "in_progress")
    assert any(d["code"] == "status_too_long" for d in facts["checkpoint_deficiencies"])


def test_policy_substitution_uses_unchanged_source_pipeline(monkeypatch):
    original = policy.FIELDS
    def optional(status):
        return False
    added = policy.Field("review", "Review note", "review_text", "Review evidence.", optional)
    monkeypatch.setattr(policy, "FIELDS", (*original, added))
    facts = parse_work_facts(SPEC, SUMMARY + "**Review note** — F12 reproduced.\n", "in_progress")
    assert facts["checkpoint_state"] == "valid" and facts["review_text"] == "F12 reproduced."
    assert "review_text" in policy.detail_field_keys()
    revision = facts["checkpoint_policy_revision"]
    monkeypatch.setattr(policy, "FIELDS", (*original, replace(added, required_when=policy.always)))
    missing = parse_work_facts(SPEC, SUMMARY, "in_progress")
    assert missing["checkpoint_policy_revision"] != revision
    assert missing["checkpoint_deficiencies"][0]["code"] == "review_missing"
    monkeypatch.setattr(policy, "FIELDS", original)
    assert parse_work_facts(SPEC, SUMMARY, "in_progress")["checkpoint_state"] == "valid"


@pytest.mark.parametrize("status,code", [("In progress.", "status_stage_lead"), ("Blocked", "status_stage_lead"),
                                         ("ready_for_dev", "status_stage_lead"), ("TODO", "status_placeholder"),
                                         ("<placeholder>", "status_placeholder"), ("Waiting; then relay.", "status_not_plain")])
def test_structural_boilerplate_only(status, code):
    facts = parse_work_facts(SPEC, SUMMARY.replace("Waiting for the authorized release slot.", status), "in_progress")
    assert facts["checkpoint_state"] == "invalid"
    assert [d["code"] for d in facts["checkpoint_deficiencies"]] == [code]
    # A product name or broader prose is never semantically classified.
    assert parse_work_facts(SPEC, SUMMARY.replace("Waiting for the authorized release slot.", "TODOist v2.3 passes 12 fixtures."), "in_progress")["checkpoint_state"] == "valid"


def test_fences_do_not_create_competing_checkpoint():
    fenced = SUMMARY + "```md\n**Status** — Blocked\n```\n"
    assert parse_work_facts(SPEC, fenced, "in_progress")["checkpoint_state"] == "valid"


def test_contract_bytes_bind_policy_revision(monkeypatch):
    from _shared import summary_prose
    revision = policy.policy_revision()
    monkeypatch.setattr(summary_prose, "CONTRACT_DIGEST", "0" * 64)
    policy._policy_revision.cache_clear()
    try:
        assert policy.policy_revision() != revision
    finally:
        monkeypatch.undo()
        policy._policy_revision.cache_clear()
    assert policy.policy_revision() == revision


def test_contract_only_change_reevaluates_cached_facts_without_moving_checkpoint(tmp_path, monkeypatch):
    from _shared import summary_prose
    from _shared.work_observations import advance_observation, read_work_candidate
    folder = tmp_path / "work/in_progress/demo__visibility"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text(SPEC)
    (folder / "summary.md").write_text(SUMMARY)
    first = read_work_candidate(folder, "in_progress", now=1e10, quiet_s=0)
    record, _ = advance_observation("spec_demo__visibility", None, first, now=1e9, settle_s=0, sweep=True)
    assert read_work_candidate(folder, "in_progress", now=1e10, quiet_s=0, previous=first) == first
    monkeypatch.setattr(summary_prose, "CONTRACT_DIGEST", "0" * 64)
    policy._policy_revision.cache_clear()
    try:
        second = read_work_candidate(folder, "in_progress", now=1e10, quiet_s=0, previous=first)
        assert second is not first and second["facts"]["checkpoint_policy_revision"] != first["facts"]["checkpoint_policy_revision"]
        record, _ = advance_observation("spec_demo__visibility", record, second, now=1e9 + 60, settle_s=0, sweep=True)
        assert record["member"]["checkpoint_changed_at"] is None
        assert record["member"]["checkpoint_policy_revision"] == second["facts"]["checkpoint_policy_revision"]
    finally:
        monkeypatch.undo()
        policy._policy_revision.cache_clear()

