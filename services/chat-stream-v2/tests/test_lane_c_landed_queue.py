"""Synthetic replay of a landed send retained in the queued-send registry."""
import json
import pytest

from queued_send_now import PREFIX
from test_spawn_wip_cap import setup
from test_work_lane_assignment import env, item, lane, seat, spec_update


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['assign', 'unassign'])
async def test_landed_queued_send_does_not_block_spec_change(env, action):
    old = item(env.root, 'demo__old')
    new = item(env.root, 'demo__new')
    ctl, sessions, _ = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, 'wl-replay', old, new)
    planner = await seat(env.store, 'planner', old, new, assigned=old, role='planner')
    request = 'synthetic-voice-send-landed'
    await env.store.append_send_receipt(
        to_stream_id=planner[0], request_id=request, receipt_id='landed-receipt',
        state='landed', wire_text='synthetic', display_text='synthetic',
        attachments=[], delivery='landed', submission_confirmed=True,
        from_stream_id=planner[0], actor_stream_id=planner[0])
    payload = {'stream_id': planner[0], 'state': 'queued', 'original_request_id': request}
    await env.store.submit(lambda c: (c.execute('INSERT INTO kv(k,v) VALUES (?,?)',
        (PREFIX + 'synthetic-landed', json.dumps(payload))), c.commit()))
    auth = {'token_verified': True, 'stream_id': planner[0], 'session_generation': planner[1]}
    result = await spec_update(env, ctl, sessions, action, planner[0], new if action == 'assign' else None, auth)
    assert result['session']['assigned_spec_id'] == (new if action == 'assign' else '')


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['assign', 'unassign'])
async def test_busy_other_lane_seat_does_not_block_spec_change(env, action):
    old = item(env.root, 'demo__busy_old')
    new = item(env.root, 'demo__busy_new')
    ctl, sessions, _ = await setup(env.root, env.store, env.monkeypatch)
    await lane(env.store, 'wl-busy-replay', old, new)
    planner = await seat(env.store, 'planner', old, new, assigned=old, role='planner')
    other = await seat(env.store, 'busy-worker', old, assigned=old, role='worker')
    await sessions.refresh()
    sessions.apply_live(other[0], working=True, online=True)
    auth = {'token_verified': True, 'stream_id': planner[0], 'session_generation': planner[1]}
    result = await spec_update(env, ctl, sessions, action, planner[0], new if action == 'assign' else None, auth)
    assert result['session']['assigned_spec_id'] == (new if action == 'assign' else '')
