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
        await env.op("set_state", {"to": "paused", "reason": "Fixture checkpoint hold", "next_action": "Resume after the fixture dependency clears"}, lane=lane["lane_id"], version=done["lane"]["version"])
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


def test_lane_cancel_after_enqueue_has_unknown_outcome_until_readback(tmp_path, monkeypatch):
    import threading
    import functools
    MEMBER = 'spec_fixture__cancel'
    LANE_CALLBACK = '_WorkLanesStoreMixin.apply_work_lane_operation.<locals>._op'
    async def body(env):
        lane = (await env.adopt(state='paused', owner='operator', members=[MEMBER]))['lane']
        payload = {'to': 'done', 'outcome': 'Already queued',
                   'operator_confirmation': env.confirm('cancel-confirm', lane['lane_id'], 'set_state:done')}
        loop = asyncio.get_running_loop()
        entered, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
        submit, calls = env.store.submit, []
        async def submission(callback):
            if callback.__qualname__ != LANE_CALLBACK:
                return await submit(callback)
            calls.append(callback.__qualname__)
            @functools.wraps(callback)
            def held(conn):
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert release.wait(5), 'HARNESS_ERROR: queued callback not released'
                    return callback(conn)
                finally:
                    loop.call_soon_threadsafe(finished.set)
            return await submit(held)
        monkeypatch.setattr(env.store, 'submit', submission)
        task = asyncio.create_task(env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='cancel-done'))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not finished.is_set()  # Cancellation is not a refusal or proof of rollback.
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 5)
        after = await env.store.get_work_lane(lane['lane_id'])
        assert after['lane']['work_state'] == 'done'
        assert sum(e.get('consumed_question_id') == 'cancel-confirm' for e in after['events']) == 1
        assert calls == [LANE_CALLBACK]  # No automatic retry.
        print('CANCELLATION_RECEIPT', json.dumps({'route': 'lane', 'stage': 'after_enqueue',
              'reported': 'CancelledError; outcome unknown until durable readback', 'readback': 'done', 'enqueues': len(calls)}))
    run(body)


# ---------------------------------------------------------------------------
# V3-V7: automatic completion, certainty, stored history, option-A reopening.
# ---------------------------------------------------------------------------
from _shared.work_goals import derive_lane_id
from test_work_lane_goals import write_goal, write_item, scanner, reconcile
from test_work_lane_episodes import RecordingSink, member, seed_observations
from test_work_lanes import lane_updates
from work_lanes_projection import build_frame


async def build_lane(env, root, *, key, goal='g', items=(), extra_goals=(), state='paused',
                     owner='operator', **kw):
    """Real flow: observe the charter(s), adopt with them, then resolve items."""
    (root / 'work').mkdir(parents=True, exist_ok=True)
    lane_id = derive_lane_id(ASSISTANT, key)
    write_goal(root, lane_id, goal)
    for extra in extra_goals:
        write_goal(root, lane_id, extra)
    for name, status in items:
        write_item(root, name, status=status, epic='epic_' + goal)
    await reconcile(env.store, scanner(root))
    payload = {'adoption_key': key, 'title': 'L', 'summary': 'g', 'owner_kind': owner,
               'work_state': state, 'visible_chat': {'stream_id': ASSISTANT},
               'goals': ['epic_' + goal, *('epic_' + name for name in extra_goals)],
               'no_spec_reason': 'Fixture', **kw}
    lane = (await env.op('adopt', payload, request_id='adopt:' + key, inject_goals=False))['lane']
    await reconcile(env.store, scanner(root))
    return lane


async def lifecycle(env):
    return await env.store.reconcile_lane_lifecycle()


async def lane_now(env, lane_id):
    return (await env.store.get_work_lane(lane_id))['lane']


async def lane_lifecycle_events(env, lane_id):
    shown = await env.store.get_work_lane(lane_id)
    return [e for e in shown['events'] if e['actor_stream_id'] == 'daemon:reconciler'
            and e['payload'].get('auto')]


async def frame_has(env, lane_id):
    rows, index = await env.store.work_lane_snapshot(include_done=False) if False else (
        await env.store.work_lane_rows(), await env.store.work_index_status())
    frame = build_frame(rows, {}, work_index=index)
    return any(l['lane_id'] == lane_id for l in frame['lanes']), frame['counts']['open']


def test_v4_auto_completion_no_confirmation_event_episode_and_frame(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v4', goal='g', items=[('i1', 'completed')])
        lid = lane['lane_id']
        present, open_before = await frame_has(env, lid)
        assert present
        result = await lifecycle(env)
        assert lid in result['completed']
        st = await lane_now(env, lid)
        assert st['work_state'] == 'done' and st['work_state_reason'] == 'goals_done' and st['done_at']
        events = await lane_lifecycle_events(env, lid)
        assert len(events) == 1
        assert events[0]['payload'] == {'to': 'done', 'auto': 'goals_done', 'goals': ['epic_g']}
        # The completed episode drives exactly one alert; no lane_update is published.
        sink = RecordingSink()
        await env.store.reconcile_work_lane_episodes(sink)
        assert sum(c['kind'] == 'completed' for c in sink.calls) == 1
        updates = [u for u in await lane_updates(env.store) if u['raw']['lane_update']['lane_id'] == lid]
        assert updates == []
        present, open_after = await frame_has(env, lid)
        assert not present and open_after == open_before - 1
        # A second refresh opens no new completed episode.
        await lifecycle(env)
        again = RecordingSink()
        await env.store.reconcile_work_lane_episodes(again)
        assert sum(c['kind'] == 'completed' for c in again.calls) == 0
    run(body, str(tmp_path / 'v4.db'))


def test_v4_stays_open_when_goal_not_done(tmp_path):
    root = tmp_path / 'memory'
    async def case(env, key, items, *, db):
        lane = await build_lane(env, root, key=key, goal=key.split(':')[1], items=items)
        await lifecycle(env)
        return (await lane_now(env, lane['lane_id']))['work_state']
    async def body(env):
        # Empty goal, all-deprecated goal, and all-done-plus-open-unplaced all stay open.
        assert await case(env, 'stream:empty', [], db='a') == 'paused'
        assert await case(env, 'stream:dep', [('d1', 'deprecated')], db='b') == 'paused'
    run(body, str(tmp_path / 'v4b.db'))


def test_v4_open_unplaced_item_keeps_lane_open(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:unplaced'
        lane_id = derive_lane_id(ASSISTANT, key)
        write_goal(root, lane_id, 'g')
        write_item(root, 'done1', status='completed', epic='epic_g')
        write_item(root, 'loose', status='in_progress', epic=None)
        await reconcile(env.store, scanner(root))
        await env.op('adopt', {'adoption_key': key, 'title': 'L', 'summary': 'g', 'owner_kind': 'operator',
            'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT}, 'goals': ['epic_g'],
            'members': ['spec_demo__loose']}, request_id='adopt:' + key, inject_goals=False)
        await reconcile(env.store, scanner(root))
        await lifecycle(env)
        assert (await lane_now(env, lane_id))['work_state'] == 'paused'
    run(body, str(tmp_path / 'v4c.db'))


def test_v4_parked_and_blocked_lanes_complete(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        parked = await build_lane(env, root, key='stream:parked', goal='p', items=[('p1', 'completed')])
        await env.op('set_state', {'to': 'paused', 'parked': True, 'reason': 'Parked fixture'},
                     lane=parked['lane_id'], version=parked['version'])
        blocked = await build_lane(env, root, key='stream:blk', goal='b', items=[('b1', 'completed')],
                                   state='blocked', blocker='dep')
        await lifecycle(env)
        assert (await lane_now(env, parked['lane_id']))['work_state'] == 'done'
        assert (await lane_now(env, blocked['lane_id']))['work_state'] == 'done'
    run(body, str(tmp_path / 'v4d.db'))


def test_v5_uncertainty_never_completes(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v5', goal='g', items=[('i1', 'completed')])
        lid = lane['lane_id']
        # Index unavailable leaves the otherwise-complete lane open.
        await env.store.mark_work_index_pending()
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'paused'
        # A stale member (charter made invalid after acceptance) also blocks completion.
        path = root / 'epics' / 'g.md'
        path.write_text('---\nnot: valid\n---\n')
        await reconcile(env.store, scanner(root))
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'paused'
    run(body, str(tmp_path / 'v5.db'))


def test_v3_manual_reopen_requires_certain_unfinished_work(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v3', goal='g', items=[('i1', 'completed')])
        lid = lane['lane_id']
        await lifecycle(env)  # auto-completes
        assert (await lane_now(env, lid))['work_state'] == 'done'
        version = (await lane_now(env, lid))['version']
        # Reopen refused with goal_required while it has no unfinished work.
        with pytest.raises(ValueError, match='work_lane_goal_required'):
            await env.op('set_state', {'to': 'paused', 'reason': 'Reopen', 'next_action': 'Resume now'},
                         lane=lid, version=version)
        # Uncertain data refuses with goal_unresolved.
        await env.store.mark_work_index_pending()
        with pytest.raises(ValueError, match='work_lane_goal_unresolved'):
            await env.op('set_state', {'to': 'paused', 'reason': 'Reopen', 'next_action': 'Resume now'},
                         lane=lid, version=version)
        # A new open item under the goal makes reopen certain and accepted.
        write_item(root, 'i2', status='in_progress', epic='epic_g')
        await reconcile(env.store, scanner(root))
        reopened = await env.op('set_state', {'to': 'paused', 'reason': 'Reopen', 'next_action': 'Resume now'},
                                lane=lid, version=version)
        assert reopened['lane']['work_state'] == 'paused'
    run(body, str(tmp_path / 'v3.db'))


def test_v6_completed_lane_history_reads_back(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v6', goal='g', items=[('i1', 'completed')])
        lid, title = lane['lane_id'], lane['title']
        await lifecycle(env)
        shown = await env.store.get_work_lane(lid)
        assert shown['lane']['work_state'] == 'done' and shown['lane']['title'] == title
        assert shown['events'] and any(m['spec_id'] == 'spec_demo__i1' for m in shown['members'])
        listed = await env.store.work_lane_rows(include_done=True)
        assert any(r['lane_id'] == lid for r in listed)
        assert not any(r['lane_id'] == lid for r in await env.store.work_lane_rows())
    run(body, str(tmp_path / 'v6.db'))


def test_v7a_completed_lane_auto_reopens_on_new_work(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v7a', goal='g', items=[('i1', 'completed')])
        lid = lane['lane_id']
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'done'
        before_updates = len([u for u in await lane_updates(env.store)
                              if u['raw']['lane_update']['lane_id'] == lid])
        # A new open item under the done goal reopens the lane to paused/reopened.
        write_item(root, 'i2', status='in_progress', epic='epic_g')
        await reconcile(env.store, scanner(root))
        result = await lifecycle(env)
        assert lid in result['reopened']
        st = await lane_now(env, lid)
        assert st['work_state'] == 'paused' and st['work_state_reason'] == 'reopened'
        events = await lane_lifecycle_events(env, lid)
        assert events[-1]['payload'] == {'to': 'paused', 'auto': 'work_added'}
        # Reopen publishes nothing.
        after_updates = len([u for u in await lane_updates(env.store)
                             if u['raw']['lane_update']['lane_id'] == lid])
        assert after_updates == before_updates
    run(body, str(tmp_path / 'v7a.db'))


def test_v7a_new_empty_goal_reopens(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v7g', goal='g', items=[('i1', 'completed')])
        lid = lane['lane_id']
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'done'
        # A brand-new goal with no items yet reopens the settled lane.
        write_goal(root, lid, 'later')
        await reconcile(env.store, scanner(root))
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'paused'
    run(body, str(tmp_path / 'v7g.db'))


def test_v7_open_unplaced_item_reopens(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        (root / 'work').mkdir(parents=True, exist_ok=True)
        key = 'stream:v7unplaced'
        lane_id = derive_lane_id(ASSISTANT, key)
        write_goal(root, lane_id, 'g')
        write_item(root, 'done1', status='completed', epic='epic_g')
        write_item(root, 'loose', status='completed', epic=None)
        await reconcile(env.store, scanner(root))
        await env.op('adopt', {'adoption_key': key, 'title': 'L', 'summary': 'g', 'owner_kind': 'operator',
            'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT}, 'goals': ['epic_g'],
            'members': ['spec_demo__loose']}, request_id='adopt:' + key, inject_goals=False)
        await reconcile(env.store, scanner(root))
        await lifecycle(env)
        # Goal done and the unplaced member completed: the lane completes.
        assert (await lane_now(env, lane_id))['work_state'] == 'done'
        # Reopening the unplaced member (open, no goal) reopens the settled lane.
        import shutil
        shutil.rmtree(root / 'work' / 'completed' / 'demo__loose')
        write_item(root, 'loose', status='in_progress', epic=None)
        await reconcile(env.store, scanner(root))
        result = await lifecycle(env)
        assert lane_id in result['reopened']
        st = await lane_now(env, lane_id)
        assert st['work_state'] == 'paused' and st['work_state_reason'] == 'reopened'
    run(body, str(tmp_path / 'v7unplaced.db'))


def test_v7c_hand_closed_lane_settles_then_reopens(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        # A lane closed by hand while a goal is still unfinished is not settled.
        lane = await build_lane(env, root, key='stream:v7c', goal='g', items=[('i1', 'in_progress')])
        lid = lane['lane_id']
        closed = await env.op('set_state', {'to': 'done', 'outcome': 'Closed early', 'reason': 'FD closes early'},
                              lane=lid, version=lane['version'])
        assert closed['lane']['work_state_reason'] == 'fd'
        await lifecycle(env)  # still has unfinished goal → stays hand-closed, not settled
        assert (await lane_now(env, lid))['work_state_reason'] != 'goals_done'
        # Finish the goal: the next refresh marks it settled, silently, no alert.
        (root / 'work' / 'in_progress' / 'demo__i1').rename(root / 'work' / 'completed' / 'demo__i1'
            if (root / 'work' / 'completed').exists() else root / 'work' / 'completed_tmp')
        import shutil
        done_dir = root / 'work' / 'completed'
        done_dir.mkdir(parents=True, exist_ok=True)
        src = root / 'work' / 'completed_tmp'
        if src.exists():
            shutil.move(str(src), str(done_dir / 'demo__i1'))
        await reconcile(env.store, scanner(root))
        sink = RecordingSink()
        await env.store.reconcile_work_lane_episodes(sink)
        result = await lifecycle(env)
        assert lid in result['settled']
        st = await lane_now(env, lid)
        assert st['work_state'] == 'done' and st['work_state_reason'] == 'goals_done'
        settle_events = [e for e in (await env.store.get_work_lane(lid))['events']
                         if e['payload'].get('auto') == 'settled']
        assert len(settle_events) == 1
        # Now a new open item reopens it.
        write_item(root, 'i2', status='in_progress', epic='epic_g')
        await reconcile(env.store, scanner(root))
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'paused'
    run(body, str(tmp_path / 'v7c.db'))


def test_v7b_hand_closed_with_unfinished_goal_stays_done(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        lane = await build_lane(env, root, key='stream:v7b', goal='g', items=[('i1', 'in_progress')])
        lid = lane['lane_id']
        closed = await env.op('set_state', {'to': 'done', 'outcome': 'Closed', 'reason': 'FD closes'},
                              lane=lid, version=lane['version'])
        assert closed['lane']['work_state_reason'] == 'fd'
        # Adding another goal and another item leaves it hand-closed, not reopened.
        write_goal(root, lid, 'extra')
        write_item(root, 'i2', status='in_progress', epic='epic_g')
        await reconcile(env.store, scanner(root))
        result = await lifecycle(env)
        assert lid not in result['reopened'] and lid not in result['settled']
        st = await lane_now(env, lid)
        assert st['work_state'] == 'done' and st['work_state_reason'] == 'fd'
    run(body, str(tmp_path / 'v7b.db'))


def test_v7d_pre_release_done_lane_settles_and_reopens(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        # A done lane with no unfinished work (closed by hand before the rule shipped)
        # is settled at the first certain refresh, then reopens on a new goal.
        lane = await build_lane(env, root, key='stream:v7d', goal='g', items=[('i1', 'completed')])
        lid = lane['lane_id']
        closed = await env.op('set_state', {'to': 'done', 'outcome': 'Closed', 'reason': 'FD closes'},
                              lane=lid, version=lane['version'])
        assert closed['lane']['work_state_reason'] == 'fd'
        result = await lifecycle(env)
        assert lid in result['settled']
        assert (await lane_now(env, lid))['work_state_reason'] == 'goals_done'
        write_goal(root, lid, 'again')
        await reconcile(env.store, scanner(root))
        await lifecycle(env)
        assert (await lane_now(env, lid))['work_state'] == 'paused'
    run(body, str(tmp_path / 'v7d.db'))


def test_v7e_member_conflict_leaves_settled_lane_done(tmp_path):
    root = tmp_path / 'memory'
    async def body(env):
        # A settled lane whose explicit member is held by another open lane stays done.
        (root / 'work').mkdir(parents=True, exist_ok=True)
        lid = derive_lane_id(ASSISTANT, 'stream:v7e1')
        write_goal(root, lid, 'g')
        write_item(root, 'i1', status='completed', epic='epic_g')
        write_item(root, 'loose', status='completed', epic=None)
        await reconcile(env.store, scanner(root))
        await env.op('adopt', {'adoption_key': 'stream:v7e1', 'title': 'L', 'summary': 'g',
            'owner_kind': 'operator', 'work_state': 'paused', 'visible_chat': {'stream_id': ASSISTANT},
            'goals': ['epic_g'], 'members': ['spec_demo__loose']}, request_id='adopt:v7e1', inject_goals=False)
        await reconcile(env.store, scanner(root))
        await lifecycle(env)  # all work done → settled (goals_done)
        assert (await lane_now(env, lid))['work_state'] == 'done'
        # Another open lane holds the same explicit member; the done lane is excluded
        # from the conflict check, so this adoption is allowed.
        await env.adopt('stream:v7e2', state='paused', owner='operator', members=['spec_demo__loose'])
        # New unfinished work on the settled lane, but its member now conflicts.
        write_goal(root, lid, 'reopen')
        await reconcile(env.store, scanner(root))
        result = await lifecycle(env)
        assert lid not in result['reopened']
        assert (await lane_now(env, lid))['work_state'] == 'done'
    run(body, str(tmp_path / 'v7e.db'))
