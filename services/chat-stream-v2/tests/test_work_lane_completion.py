"""Correlated routing completion facts never come from report prose."""
import asyncio
import json

import pytest

from test_assistant_prose_mirror import ASSISTANT
from test_work_lanes import FD, run
from work_lanes_projection import project_lane


async def seed_route(env, lane, lead, *, resolved=True, correlated=True):
    dispatch = "dispatch:" + lane["lane_id"]
    def write(conn):
        conn.execute("INSERT INTO v2_assistant_composite_routes "
                     "(route_id,stream_id,input_identity,payload_digest,input_request_id,body,actor_stream_id,"
                     "routing_state,dispatch_id,route_target,route_target_generation,route_json,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (dispatch, ASSISTANT, dispatch, "synthetic", dispatch, "Synthetic work", "operator:fixture",
                      "resolved" if resolved else "deferred", dispatch, lead[0], lead[1],
                      json.dumps({"lane_id": lane["lane_id"] if correlated else "other-lane"}),
                      "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        conn.execute("UPDATE v2_assistant_composite_lanes SET phase='execution' WHERE lane_id=?", (lane["lane_id"],))
        conn.commit()
    await env.store.submit(write)
    return dispatch


async def complete(env, lane, lead, dispatch, report="synthetic-report"):
    return await env.store.complete_assistant_composite_lane(
        stream_id=ASSISTANT, lane_id=lane["lane_id"], dispatch_id=dispatch,
        actor_stream_id=lead[0], actor_generation=lead[1], report_id=report)


async def projected(store, lane_id):
    row = next(r for r in await store.work_lane_rows(include_done=True) if r["lane_id"] == lane_id)
    return project_lane(row, None, "2030-01-01T00:00:00Z")


async def retained_q3_case(env, *, correlated, stale, prior, expected):
    lead = await env.seat("report-lead", role="lead", parent_stream_id=FD)
    lane = (await env.adopt(state="paused", lead=lead, owner="fd"))["lane"]
    if correlated:
        dispatch = await seed_route(env, lane, lead)
        await complete(env, lane, lead, dispatch)
    def synthetic(conn):
        # Uncorrelated generic reports and prose are deliberately insufficient.
        conn.execute("UPDATE sessions SET status_card=? WHERE host=? AND session_name=?",
                     (json.dumps({"update": "All done; ready to close."}), *lead[0].split(":", 1)))
        conn.execute("UPDATE v2_assistant_composite_lanes SET lead_reported_done=? WHERE lane_id=?",
                     (prior, lane["lane_id"]))
        if stale:
            conn.execute("UPDATE v2_assistant_composite_terminal_reports SET actor_generation='old-generation' "
                         "WHERE lane_id=?", (lane["lane_id"],))
        conn.commit()
    await env.store.submit(synthetic)
    assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is expected


@pytest.mark.parametrize("resolved,correlated,expected", [(True, True, False), (False, True, None), (True, False, None)])
def test_resolved_dispatch_is_required_and_no_report_is_false(resolved, correlated, expected):
    async def body(env):
        lead = await env.seat("correlated", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        await seed_route(env, lane, lead, resolved=resolved, correlated=correlated)
        before = (await env.store.get_work_lane(lane["lane_id"]))["lane"]["version"]
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is expected
        await env.store.reconcile_work_lane_episodes(None)
        stored = (await env.store.get_work_lane(lane["lane_id"]))["lane"]
        assert stored["lead_reported_done"] == expected
        assert stored["version"] == before
    run(body)


def test_report_then_handoff_before_first_drain_and_restart(tmp_path):
    from store import Store
    path = str(tmp_path / "correlation.db")
    lane_id = None
    async def seed(env):
        nonlocal lane_id
        lead = await env.seat("predecessor", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        lane_id = lane["lane_id"]
        dispatch = await seed_route(env, lane, lead)
        await complete(env, lane, lead, dispatch)
        successor = await env.seat("successor", role="lead", parent_stream_id=FD,
                                   handoff_from_stream_id=lead[0])
        await env.store.work_lane_handoff(lead[0], successor[0])
        assert (await projected(env.store, lane_id))["lead_reported_done"] is True
    run(seed, path)
    async def restart():
        store = Store(path)
        store.start()
        try:
            await store.reconcile_work_lane_episodes(None)
            assert (await projected(store, lane_id))["lead_reported_done"] is True
        finally:
            store.stop()
    asyncio.run(restart())


def test_routing_reopen_clears_cache_and_old_replay_cannot_restore_it():
    async def body(env):
        lead = await env.seat("reopen-lead", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        dispatch = await seed_route(env, lane, lead)
        completed = await complete(env, lane, lead, dispatch)
        reopened = await env.store.apply_assistant_composite_operation(
            stream_id=ASSISTANT, operation_id="synthetic-reopen", operation="lane.decision",
            lane_id=lane["lane_id"], actor_stream_id=FD, actor_generation=env.gen,
            authority_stream_id=FD, dispatch_id=dispatch, expected_lane_version=completed["version"],
            payload={"transition": "reopen", "decision_id": "synthetic-decision", "from_phase": "completed",
                     "to_phase": "discussion", "operator_basis_message_ids": [dispatch]})
        assert reopened["next_phase"] == "discussion"
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is False
        await complete(env, lane, lead, dispatch)
        await env.store.reconcile_work_lane_episodes(None)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is False
        row = (await env.store.get_work_lane(lane["lane_id"]))["lane"]
        assert row["lead_reported_done"] == 0 and row["completion_report_id"] is None
    run(body)


def test_successor_report_before_binding_retains_known_false():
    async def body(env):
        lead = await env.seat("old-lead", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        dispatch = await seed_route(env, lane, lead)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is False
        successor = await env.seat("new-lead", role="lead", parent_stream_id=FD,
                                   handoff_from_stream_id=lead[0])
        await complete(env, lane, successor, dispatch)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is False
    run(body)


@pytest.mark.parametrize("field,value", [("actor_generation", "old-generation"),
                                          ("actor_stream_id", "fixture:unrelated"),
                                          ("lane_id", "unrelated-lane"),
                                          ("stream_id", "fixture:other-composite"),
                                          ("dispatch_id", "unrelated-dispatch")])
def test_uncorrelated_terminal_pointer_preserves_prior(field, value):
    async def body(env):
        lead = await env.seat("scope-lead", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        dispatch = await seed_route(env, lane, lead)
        await complete(env, lane, lead, dispatch)
        def change(conn):
            conn.execute(f"UPDATE v2_assistant_composite_terminal_reports SET {field}=?", (value,))
            conn.execute("UPDATE v2_assistant_composite_lanes SET lead_reported_done=0 WHERE lane_id=?", (lane["lane_id"],))
            conn.commit()
        await env.store.submit(change)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is False
    run(body)


def test_closed_lead_product_reopen_and_generation_replacement_retain_report():
    async def body(env):
        lead = await env.seat("closed-lead", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        dispatch = await seed_route(env, lane, lead)
        completed = await complete(env, lane, lead, dispatch)
        done = await env.op("set_state", {"to": "done", "outcome": "Synthetic completion"},
                            lane=lane["lane_id"], version=completed["version"])
        await env.op("set_state", {"to": "paused"}, lane=lane["lane_id"], version=done["lane"]["version"])
        await env.store.mark_closed(*lead[0].split(":", 1), closed_at="2026-01-02T00:00:00Z", pane_status="closed")
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is True
        await env.seat("closed-lead", role="lead", parent_stream_id=FD)
        await env.store.reconcile_work_lane_episodes(None)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is True
    run(body)


def test_split_admission_receipt_is_correlated_without_route_json_pointer():
    async def body(env):
        lead = await env.seat("split-lead", role="lead", parent_stream_id=FD)
        lane = (await env.adopt(state="paused", lead=lead))["lane"]
        dispatch = await seed_route(env, lane, lead, correlated=False)
        def admit(conn):
            conn.execute("INSERT INTO v2_assistant_composite_operations "
                         "(operation_id,stream_id,dispatch_id,lane_id,operation,payload_digest,created_at) "
                         "VALUES (?,?,?,?,?,?,?)", ("synthetic-admission", ASSISTANT, dispatch, lane["lane_id"],
                                                    "lane.admit", "synthetic", "2026-01-01T00:00:00Z"))
            conn.commit()
        await env.store.submit(admit)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is False
        await complete(env, lane, lead, dispatch)
        assert (await projected(env.store, lane["lane_id"]))["lead_reported_done"] is True
    run(body)
