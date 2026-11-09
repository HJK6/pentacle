"""Full source -> observation -> bounded wire checkpoint and census journeys."""
from dataclasses import replace
import json

from _shared.specs_parser import parse_work_facts
from _shared.work_observations import advance_observation
from _shared import checkpoint_policy as policy
from _shared.work_goals import goal_id
from work_lanes_projection import project_lane, build_frame, page_lane_detail

NOW = "2026-11-09T10:00:00.000Z"
SPEC = "---\nid: spec_demo__visible\n---\n## Goal\nUnderstand current work.\n"
SUMMARY = "**Progress** — Reproduced F12.\n**Status** — Awaiting source review.\n**Next action** — Run the focused gate.\n"


def candidate(summary=SUMMARY, source_hash="a", spec=SPEC):
    return {"quality": "fresh", "path": "work/in_progress/demo__visible", "source_hash": source_hash,
            "facts": parse_work_facts(spec, summary, "in_progress"), "document_revisions": {"spec.md": "a" * 64}}


def advance(previous, value, now):
    return advance_observation("spec_demo__visible", previous, value, now=now, settle_s=0, sweep=True)[0]


def test_checkpoint_age_is_source_change_not_scan_history_or_policy(monkeypatch):
    record = advance(None, candidate(), 1000)
    assert record["member"]["checkpoint_changed_at"] is None
    first = record["member"]["checkpoint_first_observed_at"]
    record = json.loads(json.dumps(record))  # persisted/restarted observation
    record = advance(record, candidate(source_hash="history-only"), 2000)
    assert record["member"]["checkpoint_changed_at"] is None
    assert record["member"]["checkpoint_first_observed_at"] == first
    record = advance(record, candidate(SUMMARY.replace("Reproduced F12.", "Fixed F12; 12 tests pass."), "changed"), 3000)
    changed = record["member"]["checkpoint_changed_at"]
    assert changed == "1970-01-01T00:50:00.000Z"
    monkeypatch.setattr(policy, "FIELDS", tuple(replace(f, description=f.description + " Updated guidance.") for f in policy.FIELDS))
    record = advance(record, candidate(SUMMARY.replace("Reproduced F12.", "Fixed F12; 12 tests pass."), "changed"), 4000)
    assert record["member"]["checkpoint_changed_at"] == changed
    record = advance(record, {"quality": "stale", "error": "root unavailable"}, 5000)
    assert record["member"]["checkpoint_state"] == "unverifiable"
    assert record["member"]["checkpoint_changed_at"] == changed
    for now in (6000, 7000):
        record = advance(record, {"quality": "missing", "error": "summary unavailable"}, now)
    assert record["member"]["checkpoint_first_observed_at"] == first
    record = json.loads(json.dumps(record))
    record = advance(record, candidate(SUMMARY.replace("Reproduced F12.", "Fixed F12; 12 tests pass."), "changed"), 8000)
    assert record["member"]["checkpoint_changed_at"] == changed
    assert record["member"]["checkpoint_first_observed_at"] == first


def visibility_fixture(count=65):
    members = []
    for i in range(count):
        member = advance(None, candidate(), 1000)["member"]
        member.update(spec_id=f"spec_demo__s{i:02}", goal_id=goal_id("epic_" + str(i % 9)))
        members.append(member)
    members[-1].update(status="blocked", checkpoint_state="invalid")
    lane = {"lane_id": "wl-visible", "version": 1, "work_state": "active", "_qualifies": True,
            "bound_stream_id": "fixture:lead", "bound_generation": "g", "_members": members,
            "_work_index_available": True, "_goals": {"epic_" + str(i): {
                "facts": {"epic_id": "epic_" + str(i), "title": str(i), "rank": i,
                          "_detail": {"goal_text": "Achieve the result.", "summary_text": "A useful goal."}},
                "quality": "fresh", "source_hash": "c" * 64} for i in range(9)}}
    seats = {f"fixture:{i}": {"status": "open", "role": "qa", "online": True,
                             "working": i == 9, "assigned_spec_id": members[-1]["spec_id"]} for i in range(10)}
    questions = {members[-1]["spec_id"]: {"count": 2, "first_question_id": "q"}}
    return lane, seats, questions


def test_counts_and_questions_beyond_member_seat_page_goal_caps():
    lane, seats, questions = visibility_fixture()
    inventory = build_frame([lane], seats, now_iso=NOW, questions_by_spec=questions)["lanes"][0]
    assert len(inventory["members"]) == 8 and len(inventory["goals"]) == 8
    assert inventory["members"][-1]["open_questions"]["count"] == 2
    assert inventory["lead"]["presence"]["working"] is True
    assert not {"visibility_counts", "selected_goal"} & inventory.keys()
    assert all("progress_text" not in m and "checkpoint_state" not in m for m in inventory["members"])
    detail = project_lane(lane, None, NOW, all_members=True, sessions_by_stream=seats, questions_by_spec=questions)
    counts = detail["visibility_counts"]
    assert [counts[k] for k in ("unfinished", "working", "blocked", "needs_answer", "checkpoint_deficient")] == [65, 1, 1, 1, 1]
    shown = {"lane": lane, "events": [], "updates": [], "_projection_row": lane, "_work_index": {"available": True}}
    page = page_lane_detail(shown, detail, sessions_by_stream=seats, questions_by_spec=questions)
    assert len(page["members"]) == 32 and page["projection"]["visibility_counts"] == counts
    selected = page_lane_detail(shown, detail, goal_filter=lane["_members"][-1]["goal_id"],
                                sessions_by_stream=seats, questions_by_spec=questions)["projection"]["selected_goal"]
    assert selected["visibility_counts"]["working"] == 1 and selected["visibility_counts"]["needs_answer"] == 1
    assert selected["goal_text"] == "Achieve the result."
    assert all("goal_text" not in g for g in detail["goals"])
    uncertain = project_lane({**lane, "_work_index_available": False}, None, NOW, all_members=True)
    assert uncertain["visibility_counts"]["blocked"] is None and uncertain["visibility_counts"]["needs_answer"] is None


def test_actual_populated_detail_serializer_stays_bounded():
    lane, seats, questions = visibility_fixture(32)
    text = "\U0001f9ed" * 240
    spec = SPEC.replace("Understand current work.", "\U0001f9ed" * 2001)
    summary = "\n".join(f"**{f.label}** — {text}" for f in policy.FIELDS)
    facts = candidate(summary, spec=spec)["facts"]
    for member in lane["_members"]:
        member.update(facts)
    detail = project_lane(lane, None, NOW, all_members=True, sessions_by_stream=seats, questions_by_spec=questions)
    for member in detail["members"]:
        new = {key: value for key, value in member.items() if key in policy.detail_field_keys()
               or key.startswith("checkpoint_") or key.startswith("goal_")}
        assert len(json.dumps(new).encode()) <= 49152
        assert member["goal_text_truncated"] is True
    shown = {"lane": lane, "events": [], "updates": [], "_projection_row": lane, "_work_index": {"available": True}}
    response = page_lane_detail(shown, detail)
    assert len(json.dumps(response).encode()) <= 4194304
    assert all("checkpoint" not in member for member in response["members"])
