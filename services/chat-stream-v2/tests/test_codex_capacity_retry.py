"""Capacity episodes through real Store/normalizer/Comms; no provider account."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest

from capacity_retry import CapacityRetry
from codex_rollout_norm import normalize_codex_rollout_record
from ingest import _identity_key
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from comms import Comms
from store import Store
import store_capacity_retry as policy

SID = 'fixture:v2-capacity'
NATIVE = 'native-capacity-fixture'


def normalized(payload, now, *, record_type='event_msg'):
    return normalize_codex_rollout_record(
        {'timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat(),
         'type': record_type, 'payload': payload},
        host='fixture', session_name='v2-capacity', session_id=NATIVE)[0]


class Tmux:
    def __init__(self, harness):
        self.harness = harness
        self.pastes = []
        self.fail = False
        self.mode = True
        self.screen = 'OpenAI Codex\n› \n  gpt-6-test high'
        self.keys = []

    async def capture(self, name):
        return self.screen

    async def input_mode_clear(self, name):
        return self.mode

    async def has_session(self, name):
        return True

    async def run(self, *args, **kwargs):
        if args[0] == 'capture-pane':
            return 0, self.screen
        self.keys.append(args)
        return 0, ''

    async def paste(self, name, text):
        self.pastes.append(text)
        if self.fail:
            raise RuntimeError('synthetic crash after possible input')
        await self.harness.message('user', text)


class Hosts:
    local_host = 'fixture'

    def __init__(self, tmux):
        self.tmux = tmux

    def tmux_for(self, host):
        return self.tmux

    def is_local(self, host):
        return True


class Harness:
    def __init__(self, path=':memory:'):
        self.store = Store(str(path)); self.store.start()
        self.now = time.time()
        self.tmux = Tmux(self)
        self.sessions = Sessions(self.store, tmux=self.tmux, local_host='fixture')
        self.hosts = Hosts(self.tmux)
        self.comms = Comms(self.store, self.sessions,
                           SpawnCtl(self.store, self.sessions, tmux=self.tmux), hosts=self.hosts)
        self.controller = CapacityRetry(self.store, self.sessions, self.comms, self.hosts,
                                        clock=lambda: self.now, probe=self.native_proof)
        self.native_valid = True
        self.index = 0

    async def native_proof(self, row, state):
        return self.native_valid

    async def open(self, reopen=False):
        if not reopen:
            await self.store.open_session('fixture', 'v2-capacity', provider='codex',
                pane_pid='8123', pane_status='pane_alive', bootstrap_state='ready',
                observer_binding={'generation': 'test-generation', 'pane_pid': '8123'},
                session_generation='test-generation')
        await self.sessions.refresh()
        row = self.sessions.get(SID)
        self.sessions.apply_live(SID, working=False, online=True, pane_status='pane_alive',
            capture_generation=row['session_generation'], capture_liveness='idle')

    async def append(self, event):
        lifecycle = await self.store.fetch_open_session_lifecycle(SID, pane_pid='8123')
        with patch('store.time.time', lambda: self.now):
            return await self.store.append_session_events_lifecycle_cas([
                {'stream_id': SID, 'event': event, 'identity': _identity_key(event), 'lifecycle': lifecycle}
            ], limit=500)

    async def error(self, turn, code='server_overloaded'):
        event = normalized({'type': 'task_complete', 'turn_id': turn,
                            'error': {'codex_error_info': code}}, self.now)
        await self.append(event)
        return event

    async def message(self, role, text):
        self.index += 1
        event = normalized({'type': 'message', 'id': f'message-{self.index}', 'role': role,
                            'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]},
                           self.now, record_type='response_item')
        await self.append(event)
        return event

    async def state(self):
        return await self.store.submit(lambda conn: policy.load(conn, SID))

    async def due(self):
        self.now = (await self.state())['next_retry_at']
        await self.controller.tick()

    async def control(self, action, *, all=False, auth=None, generation='test-generation'):
        self.index += 1
        return await self.controller.control({'action': action, 'all': all,
            **({} if all else {'target_stream_id': SID, 'expected_session_generation': generation}),
            'request_id': f'fixture-control-{self.index}', '_auth_context': auth if auth is not None else {'operator_authenticated': True}})

    def close(self):
        self.store.stop()


@pytest.mark.timeout(20)
def test_ordinal_free_three_attempts_require_new_errors_and_keep_budget():
    async def run():
        h = Harness()
        try:
            await h.open(); first = await h.error('turn-1')
            deadline = (await h.state())['next_retry_at']
            h.now = deadline - 1; await h.controller.tick()
            assert h.tmux.pastes == []
            h.now = deadline; await h.controller.tick()
            assert h.tmux.pastes == ['continue']
            state = await h.state()
            assert state['attempts_used'] == 1 and state['proof_event_id']
            assert state['state'] == 'submitted'
            await h.controller.tick(); await h.append(first); await h.controller.tick()
            assert h.tmux.pastes == ['continue']
            for number in (2, 3):
                h.now += 1
                await h.error(f'turn-{number}')
                assert (await h.state())['state'] == 'waiting'
                await h.due()
                assert len(h.tmux.pastes) == number
                assert (await h.state())['attempts_used'] == number
            h.now += 1; await h.error('turn-4'); await h.controller.tick()
            assert (await h.state())['state'] == 'exhausted'
            assert len(h.tmux.pastes) == 3
        finally:
            h.close()
    asyncio.run(run())


def test_progress_clears_status_and_duplicate_old_error_cannot_revive():
    async def run():
        h = Harness()
        try:
            await h.open(); old = await h.error('old')
            h.now += 1; await h.message('assistant', 'Actual provider work')
            assert (await h.state())['state'] == 'idle'
            await h.append(old)
            assert (await h.state())['state'] == 'idle'
            h.now += 1; await h.error('new')
            assert (await h.state())['state'] == 'waiting'
            assert (await h.state())['attempts_used'] == 0
        finally:
            h.close()
    asyncio.run(run())


@pytest.mark.parametrize('role', ['user', 'assistant'])
def test_quoted_capacity_text_never_arms(role):
    async def run():
        h = Harness()
        try:
            await h.open(); await h.message(role, 'Selected model is at capacity (server_overloaded)')
            assert (await h.state())['state'] == 'idle'
            assert h.tmux.pastes == []
        finally:
            h.close()
    asyncio.run(run())


@pytest.mark.parametrize('blocked', ['native', 'draft', 'mode', 'busy', 'offline', 'generation', 'pane'])
def test_final_guards_never_input_to_ineligible_seat(blocked):
    async def run():
        h = Harness()
        try:
            await h.open(); await h.error('one')
            if blocked == 'native': h.native_valid = False
            elif blocked == 'draft': h.tmux.screen = 'OpenAI Codex\n› operator draft\n gpt-6-test high'
            elif blocked == 'mode': h.tmux.mode = False
            elif blocked == 'busy': h.sessions.apply_live(SID, working=True)
            elif blocked == 'offline': h.sessions.apply_live(SID, online=False)
            elif blocked == 'generation':
                h.sessions.apply_durable(SID, session_generation='replacement')
            elif blocked == 'pane': await h.store.update_session('fixture', 'v2-capacity', pane_pid='9999')
            await h.due()
            assert h.tmux.pastes == []
            assert (await h.state())['attempts_used'] == 0
        finally:
            h.close()
    asyncio.run(run())


def test_holds_survive_inputs_and_explicit_resume_consumes_stale_frontier():
    async def run():
        h = Harness()
        try:
            await h.open(); old = await h.error('one')
            await h.control('hold')
            h.now += 100; await h.message('user', 'continue')
            await h.controller.tick(); assert h.tmux.pastes == []
            assert (await h.state())['held'] is True
            await h.control('resume'); await h.append(old); await h.controller.tick()
            assert h.tmux.pastes == []
            h.now += 1; await h.message('assistant', 'Provider resumed meaningful work')
            h.now += 1; await h.error('two'); await h.due()
            assert h.tmux.pastes == ['continue']
        finally:
            h.close()
    asyncio.run(run())


def test_global_hold_cancels_dispatch_already_waiting_on_native_proof():
    async def run():
        h = Harness(); entered = asyncio.Event(); release = asyncio.Event()
        async def probe(row, state):
            entered.set(); await release.wait(); return True
        try:
            await h.open(); await h.error('one'); h.controller.probe = probe
            h.now = (await h.state())['next_retry_at']
            dispatch = asyncio.create_task(h.controller.tick())
            await asyncio.wait_for(entered.wait(), 2)
            held = asyncio.create_task(h.control('hold', all=True))
            # Wait for the durable hold, which must precede acknowledgement.
            for _ in range(100):
                if await h.store.submit(policy.globally_held): break
                await asyncio.sleep(0)
            assert not held.done()
            release.set(); await asyncio.wait_for(asyncio.gather(dispatch, held), 3)
            assert h.tmux.pastes == []
        finally:
            release.set(); h.close()
    asyncio.run(run())


def test_uncertain_delivery_never_pastes_again_including_after_database_reopen(tmp_path):
    async def run():
        path = tmp_path/'capacity.sqlite3'; h = Harness(path)
        try:
            await h.open(); await h.error('one'); h.tmux.fail = True; await h.due()
            assert len(h.tmux.pastes) == 1
            assert (await h.state())['attempts_used'] == 1
        finally:
            h.close()
        h = Harness(path)
        try:
            await h.open(reopen=True)
            for _ in range(3): await h.controller.tick()
            assert h.tmux.pastes == []
            assert (await h.state())['attempts_used'] == 1
            assert (await h.state())['state'] == 'uncertain'
        finally:
            h.close()
    asyncio.run(run())


def test_waiting_deadline_and_budget_survive_database_reopen(tmp_path):
    async def run():
        path=tmp_path/'capacity.sqlite3'; h=Harness(path)
        try:
            await h.open(); await h.error('one'); await h.due()
            h.now += 1; await h.error('two'); before = await h.state()
        finally:
            h.close()
        h=Harness(path)
        try:
            await h.open(reopen=True)
            assert (await h.state()) == before
            h.now = before['next_retry_at'] - 1; await h.controller.tick()
            assert h.tmux.pastes == []
            await h.due()
            assert h.tmux.pastes == ['continue']
            assert (await h.state())['attempts_used'] == 2
        finally:
            h.close()
    asyncio.run(run())


@pytest.mark.parametrize('auth', [{}, {'token_verified': True, 'stream_id': SID, 'session_generation': 'test-generation'},
                                  {'operator': True}, {'operator_trusted': True}])
def test_control_requires_connection_bound_operator_or_current_manager(auth):
    async def run():
        h=Harness()
        try:
            await h.open()
            with pytest.raises(VerbError, match='operator or current lifecycle manager'):
                await h.control('hold', auth=auth)
            assert not await h.store.submit(policy.globally_held)
        finally:
            h.close()
    asyncio.run(run())


def test_unknown_error_and_old_generation_do_not_continue():
    async def run():
        h=Harness()
        try:
            await h.open(); await h.error('one'); h.now += 1
            await h.error('two', 'unknown'); await h.controller.tick()
            assert (await h.state())['state'] == 'idle'
            await h.error('three')
            await h.store.mark_closed('fixture', 'v2-capacity', closed_at='2026-10-17T18:00:00Z', pane_status='pane_dead')
            h.now += 100; await h.controller.tick()
            assert h.tmux.pastes == []
        finally:
            h.close()
    asyncio.run(run())


def test_manual_input_cancels_episode_until_provider_progress():
    async def run():
        h=Harness()
        try:
            await h.open(); await h.error('one')
            h.now += 1; await h.comms.send({'stream_id':SID,'message':'New instruction','request_id':'manual-input'})
            h.now += 1; await h.error('two')
            assert (await h.state())['state']=='idle'
            h.now += 1; await h.message('assistant','Provider work resumed')
            h.now += 1; await h.error('three'); await h.due()
            assert h.tmux.pastes[-1]=='continue'
        finally: h.close()
    asyncio.run(run())


@pytest.mark.parametrize('remote',[False,True])
def test_supported_interrupt_latches_before_escape_even_when_unconfirmed(remote):
    from uiverbs import UIVerbs
    async def run():
        h=Harness()
        try:
            await h.open(); await h.error('one')
            ui=UIVerbs(h.store,h.sessions,h.comms.spawnctl); ui.capacity_retry=h.controller
            if remote:
                h.sessions.local_host='coordinator'
                h.hosts.peers={'fixture':object()}
                async def reachable(host,what): pass
                async def session_state(name): return 'alive'
                async def identity(name): return {'pane_pid':'8123','pane_id':'%42','session_name':'v2-capacity'}
                h.hosts.ensure_reachable=reachable
                h.tmux.session_state=session_state; h.tmux.pane_identity=identity
                h.comms.spawnctl.hosts=h.hosts
            result=await ui.send_interrupt({'host':'fixture','session_name':'v2-capacity',
                                           'expected_session_generation':'test-generation'})
            assert result['type']=='send.interrupt.ok'
            assert (await h.state())['held'] is True
            assert h.tmux.keys and h.tmux.keys[-1][-1]=='Escape'
            h.now+=100; await h.controller.tick(); assert h.tmux.pastes==[]
        finally:h.close()
    asyncio.run(run())


def test_control_replay_cannot_undo_a_later_hold_and_stale_generation_refuses():
    async def run():
        h=Harness()
        try:
            await h.open()
            message={'action':'resume','all':True,'request_id':'original-resume',
                     '_auth_context':{'operator_authenticated':True}}
            await h.controller.control(message)
            await h.control('hold',all=True)
            replay=await h.controller.control(message)
            assert replay['duplicate'] is True
            assert await h.store.submit(policy.globally_held)
            with pytest.raises(VerbError,match='already used'):
                await h.controller.control({**message,'action':'hold'})
            with pytest.raises(VerbError,match='generation changed'):
                await h.control('resume',generation='stale')
        finally:h.close()
    asyncio.run(run())


def test_wire_claim_cannot_supply_capacity_control_authority():
    from server import Server
    async def run():
        h=Harness()
        try:
            await h.open()
            server=Server(store=h.store,sessions=h.sessions,local_host='fixture')
            server.handlers['capacity_retry.control']=h.controller.control
            replies=await server._dispatch(json.dumps({'type':'capacity_retry.control','action':'hold','all':True,
                'request_id':'forged','operator':True,'_auth_context':{'operator_authenticated':True}}))
            assert replies[0]['type']=='capacity_retry.control.error'
            assert not await h.store.submit(policy.globally_held)
        finally:h.close()
    asyncio.run(run())


def test_current_manager_generation_is_revalidated_in_control_transaction():
    async def run():
        h=Harness()
        try:
            await h.open()
            def grant(conn):
                conn.execute('INSERT OR REPLACE INTO v2_lifecycle_manager(id,stream_id,session_generation,revision,updated_at) VALUES (1,?,?,1,?)',
                             (SID,'test-generation',time.time()));conn.commit()
            await h.store.submit(grant)
            auth={'token_verified':True,'stream_id':SID,'session_generation':'test-generation'}
            await h.control('hold',auth=auth)
            with pytest.raises(VerbError):
                await h.control('resume',auth={**auth,'session_generation':'old'})
            assert (await h.state())['held'] is True
        finally:h.close()
    asyncio.run(run())


def test_authenticated_satellite_error_arms_and_replay_does_not_reset_deadline():
    from test_event_push import _sink, _push
    async def run():
        h=Harness()
        try:
            await h.open()
            sink,_,_= _sink(h.store,sessions=h.sessions)
            event=normalized({'type':'task_complete','turn_id':'satellite-turn',
                              'error':{'codex_error_info':'server_overloaded'}},h.now)
            event['source_pane_pid']='8123'
            first=await sink.handle_push(_push(sink,[event],host='fixture'))
            assert first['inserted']==1
            before=await h.state()
            replay=await sink.handle_push(_push(sink,[event],host='fixture'))
            assert replay['inserted']==0 and await h.state()==before
            await h.due(); assert h.tmux.pastes==['continue']
        finally:h.close()
    asyncio.run(run())


@pytest.mark.parametrize('matching',[True,False])
def test_satellite_first_anchor_after_error_must_match_original_native_identity(matching):
    async def run():
        h=Harness()
        try:
            await h.open(); await h.error('one')
            row=await h.store.fetch_session('fixture','v2-capacity')
            binding={**row['observer_binding'],'transcript':{'provider':'codex',
                'native_session_id':NATIVE if matching else 'foreign-native',
                'source_file_identity_digest':'f'*64}}
            await h.store.update_session('fixture','v2-capacity',observer_binding=binding)
            await h.due()
            assert h.tmux.pastes==(['continue'] if matching else [])
        finally:h.close()
    asyncio.run(run())


def test_hold_acknowledgement_drains_input_already_started():
    async def run():
        h=Harness(); entered=asyncio.Event(); release=asyncio.Event()
        original=h.tmux.paste
        async def paste(name,text):
            entered.set(); await release.wait(); await original(name,text)
        try:
            await h.open(); await h.error('one'); h.tmux.paste=paste
            h.now=(await h.state())['next_retry_at']
            dispatch=asyncio.create_task(h.controller.tick())
            await asyncio.wait_for(entered.wait(),2)
            hold=asyncio.create_task(h.control('hold',all=True))
            for _ in range(100):
                if await h.store.submit(policy.globally_held):break
                await asyncio.sleep(0)
            assert not hold.done()
            release.set(); await asyncio.wait_for(asyncio.gather(dispatch,hold),3)
            assert h.tmux.pastes==['continue']
            await h.controller.tick(); assert h.tmux.pastes==['continue']
        finally:release.set();h.close()
    asyncio.run(run())


def test_closed_reopened_generation_does_not_rearm_historical_error():
    async def run():
        h=Harness()
        try:
            await h.open(); old=await h.error('old-turn')
            await h.store.mark_closed('fixture','v2-capacity',closed_at='2026-10-17T18:00:00Z',pane_status='pane_dead')
            await h.store.open_session('fixture','v2-capacity',provider='codex',pane_pid='8123',
                pane_status='pane_alive',session_generation='replacement-generation')
            await h.append(old)
            row=await h.store.fetch_session('fixture','v2-capacity')
            assert row.get('capacity_retry') is None
            h.now+=1; await h.error('new-turn')
            assert (await h.state())['generation']=='replacement-generation'
            assert (await h.state())['attempts_used']==0
        finally:h.close()
    asyncio.run(run())


def test_existing_reconciler_cadence_never_dispatches_before_deadline():
    from reconciler import SessionReconciler, ReconcileConfig
    async def run():
        h=Harness(); task=None; pasted=[]
        try:
            await h.open(); await h.error('one')
            start=time.monotonic(); baseline=h.now
            # Scale sixty model seconds to 60ms, retaining the real asyncio
            # reconciler cadence/wait. Reconciliation work is independently
            # covered by its own suite; no second scheduler is introduced.
            h.controller.clock=lambda:baseline+(time.monotonic()-start)*1000
            original=h.tmux.paste
            async def paste(name,text):
                pasted.append(h.controller.clock()); await original(name,text)
            h.tmux.paste=paste
            loop=SessionReconciler(h.sessions,h.hosts,comms=h.comms,
                config=ReconcileConfig(interval_s=.02),on_reconcile_tick=h.controller.tick)
            async def reconcile():pass
            loop.reconcile_once=reconcile
            task=asyncio.create_task(loop.run_forever())
            deadline=(await h.state())['next_retry_at']
            while not pasted:
                await asyncio.sleep(.005)
                assert time.monotonic()-start<2
            assert pasted[0]>=deadline
            assert pasted[0]-deadline<150 # one cadence plus bounded input work
        finally:
            if task:task.cancel();await asyncio.gather(task,return_exceptions=True)
            h.close()
    asyncio.run(run())


def test_queued_exhaustion_notice_cannot_revive_work_after_feature_hold():
    from outbound_notices import OutboundNoticeQueue
    async def run():
        h=Harness()
        try:
            await h.open(); await h.error('one')
            queue=OutboundNoticeQueue(h.store,h.comms)
            controller=CapacityRetry(h.store,h.sessions,h.comms,h.hosts,outbound=queue,clock=lambda:h.now,probe=h.native_proof)
            def exhausted(conn):
                state=policy.load(conn,SID)
                state.update(state='exhausted',attempts_used=3,next_retry_at=None)
                policy.save(conn,SID,state);conn.commit()
            await h.store.submit(exhausted)
            h.sessions.apply_durable(SID,parent_stream_id='fixture:parent')
            await controller.tick()
            assert await h.store.list_outbound_notice_ids(limit=10,now=time.time(),force=True)
            await h.control('hold',all=True)
            assert await queue.drain_once(force=True)==0
            assert h.tmux.pastes==[]
            assert not await h.store.list_outbound_notice_ids(limit=10,now=time.time(),force=True)
        finally:h.close()
    asyncio.run(run())


@pytest.mark.parametrize('known',[True,False])
def test_refused_remote_interrupt_cannot_latch_a_hold(known):
    from uiverbs import UIVerbs
    async def run():
        h=Harness()
        try:
            await h.open(); await h.error('one')
            h.sessions.local_host='coordinator';h.hosts.peers={'fixture':object()} if known else {}
            h.comms.spawnctl.hosts=h.hosts
            ui=UIVerbs(h.store,h.sessions,h.comms.spawnctl);ui.capacity_retry=h.controller
            result=await ui.send_interrupt({'host':'fixture','session_name':'v2-capacity'})
            assert result['type']=='send.interrupt.error'
            assert (await h.state())['state']=='waiting' and not (await h.state())['held']
            assert h.tmux.keys==[]
        finally:h.close()
    asyncio.run(run())


@pytest.mark.parametrize('composer', [
    '› \x1b[38;2;255;255;255moperator draft\x1b[0m',
    '› \n  please investigate gpt-6 behavior',
    '› \n  !!!',
    '› \n  gpt-6-test high',
])
def test_occupied_composer_cannot_reach_capacity_paste(composer):
    async def run():
        h = Harness()
        try:
            await h.open(); await h.error('terminal-turn')
            h.tmux.screen = 'OpenAI Codex\n' + composer + '\n gpt-6-test high'
            await h.due()
            assert h.tmux.pastes == []
            assert (await h.state())['attempts_used'] == 0
        finally: h.close()
    asyncio.run(run())


def test_distinct_native_same_text_input_cancels_after_one_automatic_echo():
    async def run():
        h = Harness()
        try:
            await h.open(); await h.error('first'); await h.due()
            original = await h.state()
            assert original['proof_event_id'] is not None
            h.now += 1; await h.message('user', 'continue')
            state = await h.state()
            assert state['cancelled'] is True
            assert state['proof_event_id'] == original['proof_event_id']
            h.now += 1; await h.error('fresh-after-manual'); await h.controller.tick()
            assert h.tmux.pastes == ['continue']
            assert (await h.state())['state'] == 'idle'
        finally: h.close()
    asyncio.run(run())


@pytest.mark.parametrize('all_seats', [False, True])
@pytest.mark.parametrize('prior_attempt', [False, True])
def test_explicit_resume_allows_distinct_error_and_preserves_budget(all_seats, prior_attempt):
    async def run():
        h = Harness()
        try:
            await h.open(); old = await h.error('first')
            if prior_attempt: await h.due()
            used = (await h.state())['attempts_used']
            await h.control('hold', all=all_seats)
            await h.control('resume', all=all_seats)
            assert not (await h.state())['cancelled']
            await h.append(old)
            assert (await h.state())['state'] == 'idle'
            h.now += 1; await h.error('distinct-after-explicit-resume')
            assert (await h.state())['state'] == 'waiting'
            assert (await h.state())['attempts_used'] == used
            await h.due()
            assert len(h.tmux.pastes) == used + 1
        finally: h.close()
    asyncio.run(run())


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('target', ['host', 'session_name', 'generation'])
def test_canonical_interrupt_target_holds_before_escape(remote, target):
    from uiverbs import UIVerbs
    async def run():
        h = Harness()
        try:
            await h.open(); await h.error('first')
            if remote:
                h.sessions.local_host = 'coordinator'; h.hosts.peers = {'fixture': object()}
                async def reachable(host, what): pass
                async def session_state(name): return 'alive'
                async def identity(name): return {'pane_pid':'8123', 'pane_id':'%42', 'session_name':'v2-capacity'}
                h.hosts.ensure_reachable = reachable
                h.tmux.session_state = session_state; h.tmux.pane_identity = identity
                h.comms.spawnctl.hosts = h.hosts
            ui = UIVerbs(h.store, h.sessions, h.comms.spawnctl); ui.capacity_retry = h.controller
            msg = {'host':'fixture', 'session_name':'v2-capacity', 'expected_session_generation':'test-generation'}
            key = 'expected_session_generation' if target == 'generation' else target
            msg[key] = ' ' + msg[key] + ' '
            result = await ui.send_interrupt(msg)
            assert result['type'] == 'send.interrupt.ok'
            assert (await h.state())['held'] is True
            assert h.tmux.keys[-1][-1] == 'Escape'
            h.now += 100; await h.controller.tick()
            assert h.tmux.pastes == []
        finally: h.close()
    asyncio.run(run())
