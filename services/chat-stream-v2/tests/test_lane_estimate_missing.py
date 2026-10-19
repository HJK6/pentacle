"""Missing remaining estimates through real lane reconciliation and FD delivery."""
import asyncio
import json

import pytest

from _shared.specs_parser import parse_work_facts
from test_work_lane_alert_sink import subject, adopt, drain, facts, notices
from test_work_lane_episodes import member, seed_observations, episodes
from test_work_lanes import run
from work_lane_progress import lane_estimate_missing

VALID = "remaining_work_h: 2–4 (median 3) as_of 2026-10-09"


def estimated(*lines, identity="spec_demo__span", status="in_progress", quality="fresh"):
    return {**member(status, quality),
            **parse_work_facts("## Estimate\n" + "\n".join("- " + line for line in lines), "", status),
            "spec_id": identity}


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("declaration", ["elapsed_delivery_h: 10–24 (median 16)",
    "remaining_work_h: <p25>–<p75> (median <m>) as_of <date>"])
async def test_missing_estimate_real_journey(tmp_path, monkeypatch, active, partial, declaration):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        members = ["spec_demo__span", "spec_demo__other"] if partial else ["spec_demo__span"]
        lane = await adopt(s, kind="stale" if active else "completed", members=members)
        item = estimated(declaration)
        assert item["estimate"] is None and not item["estimate_exempt"]
        await seed_observations(s.store, [item] + ([estimated(VALID, identity="spec_demo__other")] if partial else []))
        await drain(s)
        await drain(s)
        rows = await episodes(s.store)
        assert [r["kind"] for r in rows] == ["estimate_missing"]
        (fact,) = await facts(s)
        assert fact["error_context"]["code"] == "lane_estimate_missing"
        assert rows[0]["emitted_ref"] == fact["notification_id"]
        await s.queue.drain_once()
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 1
        digest = next(r for r in await notices(s) if json.loads(r["metadata"]).get("typed_digest"))
        assert digest["body"].startswith("Work update digest.")
        assert "estimate" in digest["body"] and lane["lane_id"] in digest["body"]
        assert "agent-orch work-lane show " + lane["lane_id"] in digest["body"]
        assert (await s.store.get_work_lane(lane["lane_id"]))["lane"]["work_state"] == lane["work_state"]


@pytest.mark.parametrize("members,available,state,expected", [
    ([estimated()], True, "active", True),
    ([estimated()], True, "paused", True),
    ([estimated(), estimated(VALID, identity="spec_demo__other")], True, "paused", True),
    ([estimated(VALID)], True, "active", False),
    ([estimated("remaining_work_h: n/a — umbrella")], True, "paused", False),
    ([estimated("remaining_work_h: n/a — coordination")], True, "paused", False),
    ([estimated(status="completed"), estimated(status="deprecated")], True, "paused", False),
    ([], True, "paused", False),
    ([estimated()], False, "paused", None),
    ([], False, "paused", None),
    ([estimated(), estimated(quality="stale")], True, "paused", None),
    ([estimated(), estimated(quality="error")], True, "paused", None),
    ([estimated(), estimated(status="missing", quality="missing")], True, "paused", None),
    ([estimated(), estimated(status="ambiguous", quality="ambiguous")], True, "paused", None),
    ([estimated(status="missing")], True, "paused", None),
    ([estimated(status="ambiguous")], True, "paused", None),
    ([estimated(quality="stale")], False, "done", False),
])
def test_estimate_missing_total_predicate(members, available, state, expected):
    lane = {"_members": members, "_work_index_available": available,
            "work_state": state, "_qualifies": False}
    assert lane_estimate_missing(lane) is expected


def test_trailing_date_prose_is_missing_until_moved_to_its_own_line():
    invalid = estimated(VALID, VALID + " includes verification")
    assert invalid["estimate"] is None
    assert lane_estimate_missing({"_members": [invalid], "work_state": "paused"}, index_available=True)
    repaired = estimated(VALID, "basis: includes verification")
    assert repaired["estimate"]["median"] == 3
    assert lane_estimate_missing({"_members": [repaired], "work_state": "paused"}, index_available=True) is False


def test_continuity_membership_lead_presence_unknown_correct_and_recur(tmp_path, monkeypatch):
    # Completion has its independent readable/disposed-Custom guard. Supply
    # that real control input while keeping the indexed remaining estimate null.
    folder = tmp_path.resolve() / "work" / "in_progress" / "demo__other"
    folder.mkdir(parents=True)
    (folder / "spec.md").write_text("---\nid: spec_demo__other\n---\n"
                                  "## Acceptance Criteria\n### Custom\n- [x] **Finish** — disposed\n")
    monkeypatch.setenv("PENTACLE_MEMORY_ROOT", str(tmp_path.resolve()))
    async def body(env):
        lane = (await env.adopt(members=["spec_demo__span"]))["lane"]
        lid = lane["lane_id"]
        await seed_observations(env.store, [estimated(), estimated(identity="spec_demo__other")])
        await env.store.reconcile_work_lane_episodes(None)
        opening = (await episodes(env.store))[0]
        await env.op("set_members", {"members": ["spec_demo__other"]}, lane=lid, version=1)
        await env.op("set_state", {"to": "paused"}, lane=lid, version=2)
        replacement = await env.seat("replacement", provider="codex", role="lead")
        await env.op("set_lead", {"lead": {"stream_id": replacement[0], "generation": replacement[1]}}, lane=lid, version=3)
        await env.op("set_state", {"to": "active"}, lane=lid, version=4)
        await env.store.reconcile_work_lane_episodes(None)
        assert await episodes(env.store) == [opening]
        for quality in ("stale", "error", "missing", "ambiguous"):
            item = estimated(VALID, identity="spec_demo__other", quality=quality)
            await seed_observations(env.store, [item])
            await env.store.reconcile_work_lane_episodes(None)
            assert await episodes(env.store) == [opening]
        await seed_observations(env.store, [estimated(VALID, identity="spec_demo__other")], available=False)
        await env.store.reconcile_work_lane_episodes(None)
        assert await episodes(env.store) == [opening]
        await seed_observations(env.store, [estimated(VALID, identity="spec_demo__other")])
        await env.store.reconcile_work_lane_episodes(None)
        assert (await episodes(env.store))[0]["cleared_at"]
        await seed_observations(env.store, [estimated(identity="spec_demo__other")])
        await env.store.reconcile_work_lane_episodes(None)
        assert [r["sequence"] for r in await episodes(env.store)] == [1, 2]
        # Completing the lane clears even when observations are unavailable.
        await env.op("set_state", {"to": "done", "outcome": "Disposed"}, lane=lid, version=5)
        await seed_observations(env.store, [], available=False)
        await env.store.reconcile_work_lane_episodes(None)
        assert all(r["cleared_at"] for r in await episodes(env.store))
    run(body)


@pytest.mark.asyncio
async def test_pending_clear_before_sink_and_closed_lead(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path, configured=False) as s:
        lane = await adopt(s, kind="stale")
        await s.store.mark_closed("fixture", "v2-lead", closed_at="2026-10-09T00:00:00Z", pane_status="pane_dead")
        await seed_observations(s.store, [estimated()])
        await drain(s)
        # The closed lead is its own lead_lost fault; the estimate episode is independent of it.
        by_kind = {r["kind"]: r for r in await episodes(s.store)}
        assert set(by_kind) == {"estimate_missing", "lead_lost"}
        assert by_kind["estimate_missing"]["emitted_ref"] is None
        shown = (await s.store.get_work_lane(lane["lane_id"]))["lane"]
        assert shown["work_state"] == "active"
        await seed_observations(s.store, [estimated(VALID)])
        await drain(s)
        by_kind = {r["kind"]: r for r in await episodes(s.store)}
        assert by_kind["estimate_missing"]["cleared_at"] and not by_kind["lead_lost"]["cleared_at"]
        await s.server.configure_error_alerts(s.queue, s.alerts)
        await drain(s)
        await s.queue.drain_once()
        assert len(s.provider.pastes) == 1
        digest = next(r for r in await notices(s) if json.loads(r["metadata"]).get("typed_digest"))
        assert "historical opening; condition cleared" in digest["body"]
        estimate = [f for f in await facts(s) if f["error_context"]["code"] == "lane_estimate_missing"]
        assert [f["firing_count"] for f in estimate] == [1]


@pytest.mark.asyncio
async def test_crash_before_sink_retries_persisted_estimate_opening(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    async with subject(tmp_path) as s:
        await adopt(s, kind="estimate_missing")
        emit = s.inventory.episode_sink.emit
        async def crash_before_sink(fact):
            raise asyncio.CancelledError()
        s.inventory.episode_sink.emit = crash_before_sink
        with pytest.raises(asyncio.CancelledError):
            await drain(s)
        (pending,) = await episodes(s.store)
        assert pending["emitted_ref"] is None and await facts(s) == []
        s.inventory.episode_sink.emit = emit
        await drain(s)
        await s.queue.drain_once()
        (delivered,) = await episodes(s.store)
        assert delivered["episode_id"] == pending["episode_id"]
        assert delivered["fact_json"] == pending["fact_json"]
        assert delivered["emitted_ref"] and len(s.provider.pastes) == 1
