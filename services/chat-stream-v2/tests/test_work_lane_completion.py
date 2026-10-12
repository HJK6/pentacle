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


@pytest.mark.parametrize('route', ['lane', 'local', 'remote', 'offline_operator', 'offline_manager'])
def test_completion_preparation_barrier_releases_store_and_event_loop(tmp_path, monkeypatch, route):
    import threading
    from _shared import custom_ac
    from _shared.specs_service import SpecsSubsystem
    from test_lane_close_residual_guard import MEMBER, OTHER, CHECKED, write_spec, close_context
    root = tmp_path.resolve(); path = write_spec(root, custom=CHECKED)
    write_spec(root, OTHER, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('barrier-lead', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        parse_threads, scan_threads, worker_files = [], [], []
        parser, scan, fingerprint = custom_ac.parse_custom, SpecsSubsystem._scan_folders_by_id, SpecsSubsystem._identity_fingerprint
        def parse(text):
            parse_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), 'HARNESS_ERROR: preparation barrier not released'
            return parser(text)
        def scanning(*args, **kwargs):
            scan_threads.append(threading.get_ident())
            assert threading.get_ident() not in {threading.main_thread().ident, env.store._thread.ident}
            return scan(*args, **kwargs)
        def metadata(file):
            if threading.get_ident() == env.store._thread.ident:
                worker_files.append(str(file))
                assert file == path, 'Store worker statted an identity-only namespace file'
            return fingerprint(file)
        monkeypatch.setattr(custom_ac, 'parse_custom', parse)
        monkeypatch.setattr(SpecsSubsystem, '_scan_folders_by_id', scanning)
        monkeypatch.setattr(SpecsSubsystem, '_identity_fingerprint', staticmethod(metadata))
        if route == 'lane':
            request = env.op('set_state', {'to': 'done', 'outcome': 'Barrier completion'},
                             lane=lane['lane_id'], version=1)
        else:
            sessions, _ = await close_context(env, lead, remote=route != 'local', online=not route.startswith('offline'))
            kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                      {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
            request = sessions.close(*lead[0].split(':', 1), **kwargs)
        task = asyncio.create_task(request)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(env.store.put('unrelated-progress', 'committed'), 2)
            assert await asyncio.wait_for(env.store.get('unrelated-progress'), 2) == 'committed'
            progressed = loop.create_future()
            loop.call_soon(progressed.set_result, True)
            assert await asyncio.wait_for(progressed, 2)
            assert not task.done()
        finally:
            release.set()
        result = await asyncio.wait_for(task, 5)
        assert (result['lane']['work_state'] == 'done') if route == 'lane' else (result['session']['status'] == 'closed')
        assert len(parse_threads) == len(scan_threads) == 1
        assert set(parse_threads + scan_threads).isdisjoint({threading.main_thread().ident, env.store._thread.ident})
        assert worker_files  # Consumed metadata is rechecked; identity-only files are not.
        print('THREAD_RECEIPT', json.dumps({'route': route, 'main': threading.main_thread().ident,
              'store': env.store._thread.ident, 'parse': parse_threads, 'scan': scan_threads,
              'worker_metadata_files': worker_files, 'unrelated_store_committed_while_preparing': True}))
    run(body)


@pytest.mark.parametrize('route', ['lane', 'local', 'remote', 'offline_operator', 'offline_manager'])
def test_cancel_during_preparation_never_enqueues_completion(tmp_path, monkeypatch, route):
    import threading
    from _shared import custom_ac
    from test_lane_close_residual_guard import MEMBER, CHECKED, write_spec, close_context, LANE_CALLBACK, CLOSE_CALLBACK
    root = tmp_path.resolve(); write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('cancel-preparation', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        before = await env.store.get_work_lane(lane['lane_id'])
        loop = asyncio.get_running_loop()
        entered, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
        parser, submit = custom_ac.parse_custom, env.store.submit
        calls = []
        def parse(text):
            loop.call_soon_threadsafe(entered.set)
            try:
                assert release.wait(5), 'HARNESS_ERROR: cancellation preparation not released'
                return parser(text)
            finally:
                loop.call_soon_threadsafe(finished.set)
        async def submission(callback):
            if callback.__qualname__ in {LANE_CALLBACK, CLOSE_CALLBACK}:
                calls.append(callback.__qualname__)
            return await submit(callback)
        monkeypatch.setattr(custom_ac, 'parse_custom', parse)
        monkeypatch.setattr(env.store, 'submit', submission)
        if route == 'lane':
            request = env.op('set_state', {'to': 'done', 'outcome': 'Cancelled'}, lane=lane['lane_id'], version=1)
        else:
            sessions, _ = await close_context(env, lead, remote=route != 'local', online=not route.startswith('offline'))
            kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                      {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
            request = sessions.close(*lead[0].split(':', 1), **kwargs)
        task = asyncio.create_task(request)
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.wait_for(finished.wait(), 5)
        assert calls == []
        assert await env.store.get_work_lane(lane['lane_id']) == before
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        print('CANCELLATION_RECEIPT', json.dumps({'route': route, 'stage': 'preparation', 'enqueues': calls}))
    run(body)


def test_lane_cancel_after_enqueue_has_unknown_outcome_until_readback(tmp_path, monkeypatch):
    import threading
    import functools
    from test_lane_close_residual_guard import MEMBER, CHECKED, write_spec, LANE_CALLBACK
    root = tmp_path.resolve(); write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
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


@pytest.mark.parametrize('route', ['local', 'offline_operator', 'offline_manager'])
def test_close_cancel_waiting_for_store_lifecycle_lock_cannot_later_enqueue(tmp_path, monkeypatch, route):
    from test_lane_close_residual_guard import MEMBER, CHECKED, write_spec, close_context, CLOSE_ENVELOPE
    root = tmp_path.resolve(); write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('cancel-lock', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[MEMBER])
        sessions, _ = await close_context(env, lead, remote=route != 'local', online=False)
        kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                  {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
        get_lock = env.store.routing_integrity_lifecycle_lock
        held_lock = get_lock(lead[0])
        await held_lock.__aenter__()
        attempted = asyncio.Event()
        class Lock:
            def __init__(self):
                self.inner = get_lock(lead[0])
            async def __aenter__(self):
                attempted.set()
                return await self.inner.__aenter__()
            async def __aexit__(self, *args):
                return await self.inner.__aexit__(*args)
        monkeypatch.setattr(env.store, 'routing_integrity_lifecycle_lock', lambda stream_id: Lock())
        submit, calls = env.store.submit, []
        async def submission(callback):
            if callback.__qualname__ == CLOSE_ENVELOPE:
                calls.append(callback.__qualname__)
            return await submit(callback)
        monkeypatch.setattr(env.store, 'submit', submission)
        task = asyncio.create_task(sessions.close(*lead[0].split(':', 1), **kwargs))
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            task.cancel()
        finally:
            await held_lock.__aexit__(None, None, None)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert calls == []
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        assert await env.store.latest_close_audit(lead[0]) is None
    run(body)


@pytest.mark.parametrize('route', ['local', 'remote'])
def test_confirmed_dead_close_finishes_after_cancellation_before_enqueue(tmp_path, monkeypatch, route):
    from test_lane_close_residual_guard import (MEMBER, CHECKED, write_spec, PreparedRace,
                                               ordered_close_context)
    root = tmp_path.resolve(); path = write_spec(root, custom=CHECKED)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    async def body(env):
        lead = await env.seat('cancel-dead', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[MEMBER])
        def mutate(prepared):
            assert any(name == 'confirmed_dead' for _, name in seam.order)
            path.write_bytes(path.read_bytes().replace(b'[x]', b'[ ]'))
        seam = PreparedRace(env, monkeypatch, path, mutate, route=route)
        sessions, tmux, kwargs = await ordered_close_context(env, lead, seam, monkeypatch, route)
        get_lock = env.store.routing_integrity_lifecycle_lock
        held_lock = get_lock(lead[0]); await held_lock.__aenter__()
        attempted = asyncio.Event()
        class Lock:
            def __init__(self): self.inner = get_lock(lead[0])
            async def __aenter__(self):
                attempted.set()
                return await self.inner.__aenter__()
            async def __aexit__(self, *args): return await self.inner.__aexit__(*args)
        monkeypatch.setattr(env.store, 'routing_integrity_lifecycle_lock', lambda sid: Lock())
        task = asyncio.create_task(sessions.close(*lead[0].split(':', 1), **kwargs))
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            assert tmux.kills == 1 and tmux.recorded_death
            task.cancel()
        finally:
            await held_lock.__aexit__(None, None, None)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        seam.verify('confirmed-dead-cancel:' + route)
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'closed'
        audit = await env.store.latest_close_audit(lead[0])
        assert audit['disposition'] == 'closed' and 'u1_source_changed_after_termination:' in audit['reason']
    run(body)


def test_concurrent_close_source_flags_are_request_local_and_durable(tmp_path, monkeypatch):
    import functools
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_lane_close_residual_guard import (MEMBER, OTHER, CHECKED, write_spec,
        OrderedTmux, close_context, ADMISSION_CALLBACK, CLOSE_CALLBACK, CLOSE_ENVELOPE)
    from store import Store
    root = (tmp_path / 'memory').resolve()
    paths = {MEMBER: write_spec(root, custom=CHECKED), OTHER: write_spec(root, OTHER, custom=CHECKED)}
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    database = str(tmp_path / 'durable-audit.db')
    identities, observed_audits = {}, {}
    async def body(env):
        ready = asyncio.Event()
        preparation = env.store.prepare_lead_lane_completion
        submit = env.store.submit
        records, preparations, transports, calls = {}, {}, {}, []
        for spec_id in paths:
            lead = await env.seat('parallel-' + spec_id, role='lead', parent_stream_id=FD)
            identities[spec_id] = lead
            await env.adopt('stream:parallel-' + str(len(identities)), lead=lead, members=[spec_id])
            events = []
            records[lead[0]] = SimpleNamespace(order=events, mark=lambda name, rows=events: rows.append((len(rows) + 1, name)))
        async def prepare(sid, generation):
            result = await preparation(sid, generation)
            preparations[sid] = result
            records[sid].mark('prepared')
            if len(preparations) == 2: ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            return result
        async def submission(callback):
            name = callback.__qualname__
            if name not in {ADMISSION_CALLBACK, CLOSE_ENVELOPE}:
                return await submit(callback)
            closure = dict(zip(callback.__code__.co_freevars,
                               [cell.cell_contents for cell in callback.__closure__]))
            sid = closure['stream_id'] if name == ADMISSION_CALLBACK else closure['self'].stream_id
            if name == CLOSE_ENVELOPE:
                assert closure['transaction'].__qualname__ == CLOSE_CALLBACK
                assert sid in calls
            @functools.wraps(callback)
            def observed(conn):
                result = callback(conn)
                records[sid].mark('admitted' if name == ADMISSION_CALLBACK else 'committed')
                return result
            return await submit(observed)
        from store_work_lanes import _PreparedLaneCloseStore
        close_submit = _PreparedLaneCloseStore.submit
        async def close_submission(view, transaction):
            assert transaction.__qualname__ == CLOSE_CALLBACK
            sid = view.stream_id
            assert transports[sid].recorded_death
            assert view.prepared is preparations[sid]
            assert sid not in calls
            if sid == identities[MEMBER][0]:
                paths[MEMBER].write_bytes(paths[MEMBER].read_bytes().replace(b'[x]', b'[ ]'))
            records[sid].mark('mutation' if sid == identities[MEMBER][0] else 'unchanged')
            calls.append(sid)
            return await close_submit(view, transaction)
        monkeypatch.setattr(_PreparedLaneCloseStore, 'submit', close_submission)
        monkeypatch.setattr(env.store, 'prepare_lead_lane_completion', prepare)
        monkeypatch.setattr(env.store, 'submit', submission)
        monkeypatch.setattr(__import__('sessions').prockill, 'process_records', AsyncMock(return_value={}))
        requests = []
        for spec_id, lead in identities.items():
            sessions, _ = await close_context(env, lead)
            transports[lead[0]] = OrderedTmux(records[lead[0]])
            sessions.tmux = transports[lead[0]]
            requests.append(sessions.close(*lead[0].split(':', 1), reason='parallel:' + spec_id))
        results = await asyncio.gather(*requests)
        assert all(result['session']['status'] == 'closed' for result in results)
        assert len(calls) == 2 and set(calls) == {lead[0] for lead in identities.values()}
        for spec_id, lead in identities.items():
            audit = await env.store.latest_close_audit(lead[0])
            observed_audits[spec_id] = audit
            assert ('u1_source_changed_after_termination:' in audit['reason']) is (spec_id == MEMBER)
            assert ('completion_source_changed' in next(result['session'] for result in results
                    if result['session']['session_name'] == lead[0].split(':', 1)[1])) is (spec_id == MEMBER)
            sequence = {name: seq for seq, name in records[lead[0]].order}
            assert sequence['prepared'] < sequence['admitted'] < sequence['terminate'] < sequence['confirmed_dead'] < sequence['committed']
            print('CONCURRENT_CLOSE_RECEIPT', json.dumps({'spec_id': spec_id, 'order': records[lead[0]].order,
                                                        'audit_reason': audit['reason']}))
    run(body, database)
    async def restart():
        store = Store(database); store.start()
        try:
            for spec_id, lead in identities.items():
                assert await store.latest_close_audit(lead[0]) == observed_audits[spec_id]
                row = await store.fetch_session(*lead[0].split(':', 1))
                assert row['status'] == 'closed' and row['session_generation'] == lead[1]
        finally:
            store.stop()
    asyncio.run(restart())
