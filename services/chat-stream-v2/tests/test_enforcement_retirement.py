"""Retired enforcement must not gate the real spawn, close or lane boundaries."""
import asyncio
import json

import pytest

from _shared.specs_service import SpecsSubsystem
from sessions import Sessions
from store import Store
from test_work_lanes import FD, run

SPEC = 'spec_fixture__retirement'


class QuietTmux:
    async def has_session(self, name): return False
    async def session_state(self, name): return 'gone'


class Peers:
    def __init__(self, online): self.online = online
    def is_local(self, host): return False
    def known(self, host): return host == 'host-a'
    def tmux_for(self, host): return QuietTmux()
    async def probe_once(self, host): return self.online


@pytest.mark.parametrize('route', ['lane', 'local', 'remote', 'offline_operator', 'offline_manager'])
@pytest.mark.parametrize('source', ['unchecked', 'missing', 'malformed'])
def test_completion_never_reads_spec_source(tmp_path, monkeypatch, route, source):
    folder = tmp_path / 'work/in_progress/fixture__retirement'
    folder.mkdir(parents=True)
    path = folder / 'spec.md'
    if source != 'missing':
        path.write_text('---\nid: ' + SPEC + '\n---\n## Acceptance Criteria\n### Custom\n' +
                        ('- [ ] Remaining work\n' if source == 'unchecked' else '- [ ] tracked: ???\n'))
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))

    async def body(env):
        lead = await env.seat('retirement-lead', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[SPEC]))['lane']
        def forbidden(*args, **kwargs):
            raise AssertionError('Completion must not resolve or read spec files')
        monkeypatch.setattr(SpecsSubsystem, '_scan_folders_by_id', forbidden)
        if route == 'lane':
            done = await env.op('set_state', {'to': 'done', 'outcome': 'Recorded by owner'},
                                lane=lane['lane_id'], version=lane['version'])
            assert done['lane']['work_state'] == 'done'
        else:
            sessions = Sessions(env.store, tmux=QuietTmux(),
                                local_host='host-a' if route == 'local' else 'local-fixture',
                                hosts=Peers(not route.startswith('offline')) if route != 'local' else None)
            await sessions.refresh()
            kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                      {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
            await sessions.close(*lead[0].split(':'), expected_generation=lead[1], **kwargs)
            assert (await env.store.fetch_session(*lead[0].split(':')))['status'] == 'closed'
            # Closing a lead still does not claim its product lane is complete.
            assert (await env.store.get_work_lane(lane['lane_id']))['lane']['work_state'] != 'done'
    run(body)


def test_lane_completion_keeps_authority_generation_and_cas():
    async def body(env):
        lane = (await env.adopt(members=[SPEC]))['lane']
        outsider = await env.seat('outsider', role='lead')
        payload = {'to': 'done', 'outcome': 'Recorded by owner'}
        for kwargs, code in [({'actor': outsider[0], 'gen': outsider[1]}, 'work_lane_actor_unverified'),
                             ({'gen': 'old-generation'}, 'assistant_actor_generation_unverified'),
                             ({'version': 999}, 'assistant_lane_version_conflict')]:
            call = dict(lane=lane['lane_id'], version=lane['version'])
            call.update(kwargs)
            with pytest.raises(ValueError, match=code):
                await env.op('set_state', payload, **call)
        assert (await env.store.get_work_lane(lane['lane_id']))['lane'] == lane
    run(body)


def test_spec_bound_spawn_needs_no_lane_and_replay_stays_idempotent(tmp_path, monkeypatch):
    from test_spawn_wip_cap import setup, message, cleanup
    folder = tmp_path / 'work/backlog/fixture__retirement'
    folder.mkdir(parents=True)
    (folder / 'spec.md').write_text('---\nid: ' + SPEC + '\nowner: fixture\n---\n')
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path))
    async def go():
        store = Store(':memory:'); store.start()
        ctl, sessions, tmux = await setup(tmp_path, store, monkeypatch)
        try:
            from server import Server
            class RemotePeer:
                remote_address = ('192.0.2.10', 54321)
            server = Server(store=store, sessions=sessions, spawnctl=ctl, local_host='fixture')
            denied = await server._dispatch(json.dumps({**message(SPEC), 'type': 'spawn',
                'stream_token': 'invalid', 'from_stream_id': 'fixture:untrusted',
                '_auth_context': {'operator_authenticated': True, 'token_verified': True}}),
                websocket=RemotePeer())
            assert denied[0]['error_code'] == 'authentication_required'
            assert tmux.created == 0
            reply = await ctl.spawn(message(SPEC), 'fixture')
            assert reply['type'] == 'spawn.ok'
            assert reply['session']['spec_id'] == SPEC
            assert await store.work_lane_rows(include_done=True) == []
            assert (await ctl.spawn(message(SPEC), 'fixture'))['type'] == 'spawn.ok'
            assert tmux.created == 1
        finally:
            await cleanup(store, sessions, tmux)
            store.stop()
    asyncio.run(go())


def test_legacy_budget_notice_is_terminal_without_delivery_and_history_survives(tmp_path):
    from outbound_notices import OutboundNoticeQueue
    from test_outbound_notices import _RecordingComms, _enqueue, _stored_notice
    db = tmp_path / 'upgrade.db'
    async def go():
        store = Store(str(db)); store.start()
        try:
            await store.open_session('fixture-node', 'parent')
            await store.put('fd_turn_budget.cursor:historical', 'retained history')
            await store.append_session_event('fixture-node:parent', {'kind': 'fd_turn_budget', 'historical': True},
                                             identity='historical-crossing', limit=100)
            queue = OutboundNoticeQueue(store)
            await _enqueue(queue, notice_id='legacy', kind='fd_turn_budget')
        finally:
            store.stop()
        # Reopen an upgrade preimage, then use the ordinary bounded queue pass.
        store = Store(str(db)); store.start()
        try:
            comms = _RecordingComms()
            queue = OutboundNoticeQueue(store, comms)
            await queue.drain_once(force=True)
            old = await _stored_notice(store, 'legacy')
            assert old['terminal_reason'] == 'fd_turn_budget_stale_or_disabled'
            assert old['terminal_at'] and not old['delivered_at'] and not comms.calls
            await queue.drain_once(force=True)
            assert await _stored_notice(store, 'legacy') == old
            assert await store.get('fd_turn_budget.cursor:historical') == 'retained history'
            await store.append_session_event('fixture-node:parent', {'kind': 'ordinary'}, identity='ordinary', limit=100)
            assert await store.submit(lambda c: c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0]) == 2
            await _enqueue(queue, notice_id='ordinary', kind='report')
            assert await queue.drain_once(force=True) == 1
            assert len(comms.calls) == 1
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize('route', ['local', 'offline_operator', 'offline_manager',
                                   'confirmed_local', 'confirmed_remote'])
def test_close_cancellation_at_store_lock_preserves_enqueue_boundary(monkeypatch, route):
    async def body(env):
        lead = await env.seat('cancel-lock', role='lead', parent_stream_id=FD)
        await env.adopt(lead=lead, members=[SPEC])
        local = route in {'local', 'confirmed_local'}
        sessions = Sessions(env.store, tmux=QuietTmux(),
                            local_host='host-a' if local else 'local-fixture',
                            hosts=None if local else Peers(route == 'confirmed_remote'))
        await sessions.refresh()
        if route == 'confirmed_local':
            from unittest.mock import AsyncMock
            monkeypatch.setattr(sessions.tmux, 'has_session', AsyncMock(return_value=True))
            monkeypatch.setattr(sessions, '_terminate_pane', AsyncMock(
                return_value=('ok', '', {'reap_status': 'reaped', 'survivors': []})))
        kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                  {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
        get_lock = env.store.routing_integrity_lifecycle_lock
        held = get_lock(lead[0])
        await held.__aenter__()
        attempted = asyncio.Event()
        class Lock:
            def __init__(self): self.inner = get_lock(lead[0])
            async def __aenter__(self):
                attempted.set()
                return await self.inner.__aenter__()
            async def __aexit__(self, *args): return await self.inner.__aexit__(*args)
        monkeypatch.setattr(env.store, 'routing_integrity_lifecycle_lock', lambda stream_id: Lock())
        submit, calls = env.store.submit, []
        async def submission(callback):
            if callback.__qualname__ == 'Store.mark_closed.<locals>.transaction':
                calls.append(callback.__qualname__)
            return await submit(callback)
        monkeypatch.setattr(env.store, 'submit', submission)
        task = asyncio.create_task(sessions.close(*lead[0].split(':', 1), **kwargs))
        try:
            await asyncio.wait_for(attempted.wait(), 5)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            await held.__aexit__(None, None, None)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        row = await env.store.fetch_session(*lead[0].split(':', 1))
        audit = await env.store.latest_close_audit(lead[0])
        confirmed = route.startswith('confirmed_')
        assert len(calls) == int(confirmed)
        assert row['status'] == ('closed' if confirmed else 'open')
        assert (audit is not None) == confirmed
        if confirmed:
            assert sessions.get(lead[0]) is None
        else:
            assert sessions.get(lead[0])['status'] == 'open'
    run(body)
