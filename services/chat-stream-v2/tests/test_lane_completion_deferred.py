"""U1 is deferred; completion must not read specs, older guards remain active."""
import pytest
from _shared.specs_service import SpecsSubsystem
from test_lane_close_residual_guard import MEMBER, close_context, write_spec
from test_work_lanes import FD, run


@pytest.mark.parametrize('route', ['lane', 'local', 'remote', 'offline_operator', 'offline_manager'])
@pytest.mark.parametrize('source', ['unchecked', 'missing', 'unreadable'])
def test_deferred_completion_never_scans_specs(tmp_path, monkeypatch, route, source):
    path = write_spec(tmp_path.resolve())
    if source == 'missing':
        path.unlink()
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))

    async def body(env):
        lead = await env.seat('lead', role='lead', parent_stream_id=FD)
        lane = (await env.adopt(lead=lead, members=[MEMBER]))['lane']
        def forbidden_scan(*args, **kwargs):
            raise AssertionError('Deferred U1 completion scanned the spec namespace')
        monkeypatch.setattr(SpecsSubsystem, '_scan_folders_by_id', forbidden_scan)
        if source == 'unreadable':
            from pathlib import Path
            original = Path.read_text
            def denied(self, *args, **kwargs):
                if self == path:
                    raise PermissionError('Owned synthetic unreadable spec')
                return original(self, *args, **kwargs)
            monkeypatch.setattr(Path, 'read_text', denied)
        if route == 'lane':
            result = await env.op('set_state', {'to': 'done', 'outcome': 'U1 tracked separately'},
                                  lane=lane['lane_id'], version=1)
            assert result['lane']['work_state'] == 'done'
        else:
            sessions, _ = await close_context(env, lead, remote=route != 'local',
                                               online=not route.startswith('offline'))
            kwargs = ({'operator_confirm': True} if route == 'offline_operator' else
                      {'close_kind': 'manager_close'} if route == 'offline_manager' else {})
            result = await sessions.close(*lead[0].split(':', 1), **kwargs)
            assert result['session']['status'] == 'closed'
    run(body)


def test_deferred_completion_preserves_confirmation_and_replay(tmp_path, monkeypatch):
    write_spec(tmp_path.resolve())
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))
    async def body(env):
        lane = (await env.adopt(state='paused', lead=False, owner='operator', members=[MEMBER]))['lane']
        payload = {'to': 'done', 'outcome': 'U1 tracked separately'}
        with pytest.raises(ValueError, match='^work_lane_override_reason_required$'):
            await env.op('set_state', payload, lane=lane['lane_id'], version=1)
        payload['operator_confirmation'] = env.confirm('deferred-confirm', lane['lane_id'], 'set_state:done')
        done = await env.op('set_state', payload, lane=lane['lane_id'], version=1, request_id='deferred-done')
        assert done['lane']['work_state'] == 'done'
        assert (await env.op('set_state', payload, lane=lane['lane_id'], version=1,
                             request_id='deferred-done'))['duplicate']
        events = (await env.store.get_work_lane(lane['lane_id']))['events']
        assert sum(e.get('consumed_question_id') == 'deferred-confirm' for e in events) == 1
    run(body)
