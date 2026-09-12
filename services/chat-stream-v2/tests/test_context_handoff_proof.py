"""Fable handoff resolution, failure isolation and proof-oracle coverage."""
from __future__ import annotations
import asyncio
import pytest
import spawnctl as spawnctl_mod
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_spawn_rollback_hygiene import NeverReadyTmux

HOST = 'amaterasu'

@pytest.mark.parametrize('model', [None, 'claude-opus-5'])
def test_installed_fable_source_tuple_resolves_without_catalog_change(model):
    async def run():
        store = Store(':memory:'); store.start()
        try:
            await store.open_session(HOST, 'source', provider='claude',
                effective_model='claude-fable-5-1', effective_effort='high', role='nexus')
            ctl = SpawnCtl(store, object(), tmux=object())
            message = {'handoff_from_stream_id': HOST+':source'}
            if model: message['model'] = model
            resolved = await ctl._resolve_handoff(message, HOST, name='successor')
            assert resolved['model'] == (model or 'claude-fable-5-1')
            assert resolved['effort'] == 'high'
        finally: store.stop()
    asyncio.run(run())


def test_failed_successor_boot_preserves_source_and_child(monkeypatch):
    monkeypatch.setattr(spawnctl_mod, 'BOOT_READY_HARD_DEADLINE_S', .2)
    async def run():
        store=Store(':memory:'); store.start()
        try:
            tmux=NeverReadyTmux(kill_stalls=0)
            sessions=Sessions(store, tmux=tmux, local_host=HOST)
            await sessions.open(HOST, 'source', provider='claude', effective_model='claude-fable-5-1', effective_effort='high')
            await sessions.open(HOST, 'witness', parent_stream_id=HOST+':source')
            ctl=SpawnCtl(store, sessions, tmux=tmux)
            with pytest.raises(VerbError) as exc:
                await ctl.spawn({'session_name':'successor', 'command':'sleep 60',
                    'handoff':True, 'handoff_from_stream_id':HOST+':source',
                    'objective':'Disposable handoff rollback proof'}, HOST)
            assert exc.value.code == 'boot_not_ready'
            assert tmux.new_sessions == 1  # injection reached the readiness subject
            assert tmux.alive is False
            assert tmux.pastes == 0
            assert (await store.fetch_session(HOST,'source'))['status'] == 'open'
            assert (await store.fetch_session(HOST,'witness'))['parent_stream_id'] == HOST+':source'
            assert not await store.reservations(include_expired=True)
        finally: store.stop()
    asyncio.run(run())


def test_handoff_level_parent_can_still_spawn_child():
    from test_spawn_error_no_phantom_row import BootReadyTmux
    async def run():
        store=Store(':memory:');store.start()
        try:
            tmux=BootReadyTmux();sessions=Sessions(store,tmux=tmux,local_host=HOST)
            await sessions.open(HOST,'source',provider='claude',context_level='handoff',context_tokens=900_000)
            ctl=SpawnCtl(store,sessions,tmux=tmux)
            reply=await ctl.spawn({'session_name':'context-child','command':'sleep 60',
                'parent_stream_id':HOST+':source','objective':'Handoff level is notification-only'},HOST)
            assert reply['type']=='spawn.ok'
            assert reply['session']['parent_stream_id']==HOST+':source'
            assert tmux.alive
        finally:store.stop()
    asyncio.run(run())


def test_post_step_reparent_failure_is_rejected_by_installed_proof_oracle(monkeypatch):
    from tools.context_handoff_proof import verify_handoff
    from test_spawn_error_no_phantom_row import BootReadyTmux
    async def run():
        store=Store(':memory:');store.start()
        try:
            sessions=Sessions(store,tmux=BootReadyTmux(),local_host=HOST)
            for name in ('source','successor','witness'):
                await sessions.open(HOST,name,provider='claude',effective_model='claude-fable-5-1',effective_effort='high',
                    parent_stream_id=HOST+':source' if name=='witness' else None,
                    handoff_from_stream_id=HOST+':source' if name=='successor' else None)
            injected=[]
            async def fail_reparent(*args):
                injected.append(args);raise RuntimeError('injected post-step reparent failure')
            monkeypatch.setattr(sessions,'reparent_children',fail_reparent)
            ctl=SpawnCtl(store,sessions,tmux=sessions.tmux)
            await ctl._finish_handoff({'handoff_from_stream_id':HOST+':source'},HOST+':successor')
            assert injected==[(HOST+':source',HOST+':successor')]
            rows={}
            for name in ('source','successor','witness'):
                row=await store.fetch_session(HOST,name);row['session_generation']=row['created_at'];rows[HOST+':'+name]=row
            manifest={label:{key:rows[HOST+':'+label][key] for key in ('stream_id','session_generation','created_at')} for label in ('source','witness')}
            receipt={'stream_id':HOST+':successor','initial_prompt_delivery':{'delivery_status':'delivered'}}
            with pytest.raises(ValueError,match='witness parentage mismatch'):
                verify_handoff(manifest,rows,receipt)
        finally:store.stop()
    asyncio.run(run())
