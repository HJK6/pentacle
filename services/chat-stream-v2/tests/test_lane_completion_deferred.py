"""Restored U1 refuses bad inputs; the Store worker never scans specs."""
import threading
import pytest
from _shared.specs_service import SpecsSubsystem
from test_lane_close_residual_guard import MEMBER, CHECKED, close_context, write_spec
from sessions import VerbError
from test_work_lanes import FD, run


@pytest.mark.parametrize('route', ['lane', 'local', 'remote', 'offline_operator', 'offline_manager'])
@pytest.mark.parametrize('source', ['unchecked', 'missing', 'unreadable'])
def test_restored_completion_scans_off_store_only(tmp_path, monkeypatch, route, source):
    path = write_spec(tmp_path.resolve())
    if source == 'missing':
        path.unlink()
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))

    async def body(env):
        lead = await env.seat('lead', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        scans = []
        original_scan = SpecsSubsystem._scan_folders_by_id
        def checked_scan(*args, **kwargs):
            thread = threading.get_ident()
            assert thread not in {threading.main_thread().ident, env.store._thread.ident}
            scans.append(thread)
            return original_scan(*args, **kwargs)
        monkeypatch.setattr(SpecsSubsystem, '_scan_folders_by_id', checked_scan)
        if source == 'unreadable':
            from pathlib import Path
            original = Path.read_text
            def denied(self, *args, **kwargs):
                if self == path:
                    raise PermissionError('Owned synthetic unreadable spec')
                return original(self, *args, **kwargs)
            monkeypatch.setattr(Path, 'read_text', denied)
        before = await env.store.get_work_lane(lane['lane_id'])
        with pytest.raises((ValueError, VerbError)) as caught:
            if route == 'lane':
                await env.op('set_state', {'to': 'done', 'outcome': 'Synthetic completion'},
                             lane=lane['lane_id'], version=1)
            else:
                sessions, tmux = await close_context(env, lead, remote=route != 'local',
                                                     online=not route.startswith('offline'))
                kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                          {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
                await sessions.close(*lead[0].split(':', 1), **kwargs)
        if route != 'lane':
            assert tmux.kills == 0
        expected = 'lane_residuals_undisposed' if source == 'unchecked' else 'member_spec_unreadable'
        assert caught.value.code == expected
        assert await env.store.get_work_lane(lane['lane_id']) == before
        assert (await env.store.fetch_session(*lead[0].split(':', 1)))['status'] == 'open'
        assert len(scans) == 1
    run(body)


def test_restored_completion_preserves_confirmation_and_replay(tmp_path, monkeypatch):
    write_spec(tmp_path.resolve())
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))
    async def body(env):
        lane = (await env.adopt(state='paused', lead=False, owner='operator', members=[MEMBER]))['lane']
        payload = {'to': 'done', 'outcome': 'U1 tracked separately'}
        with pytest.raises(ValueError, match='^work_lane_override_reason_required$'):
            await env.op('set_state', payload, lane=lane['lane_id'], version=1)
        payload['operator_confirmation'] = env.confirm('deferred-confirm', lane['lane_id'], 'set_state:done')
        before = await env.store.get_work_lane(lane['lane_id'])
        with pytest.raises(ValueError, match='^lane_residuals_undisposed:'):
            await env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='deferred-done')
        assert await env.store.get_work_lane(lane['lane_id']) == before
        write_spec(tmp_path.resolve(), custom=CHECKED)
        done = await env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='deferred-done')
        assert done['lane']['work_state'] == 'done'
        write_spec(tmp_path.resolve())  # Committed replay precedes a new source refusal.
        assert (await env.op('set_state', payload, lane=lane['lane_id'], version=1,
                             request_id='deferred-done'))['duplicate']
        events = (await env.store.get_work_lane(lane['lane_id']))['events']
        assert sum(e.get('consumed_question_id') == 'deferred-confirm' for e in events) == 1
    run(body)
