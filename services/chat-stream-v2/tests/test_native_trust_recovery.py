"""Native argv delivery recovers trust and preserves its failure history."""
import asyncio
import json
import logging
import sys
import time

import pytest
import spawnctl as spawnctl_mod
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from submission_events import EventProof
from test_adopt_reconcile import PeerEventTmux
from test_codex_ssh_free_readiness import CodexTui, _resolved_native_codex

HOST = 'fixture-node'
TEXT = 'native user prompt'


@pytest.mark.parametrize('dialog', ['Trust this folder?\n1. Trust and continue',
                                    'Do you trust the contents of this directory?\nPress enter to continue'])
def test_native_dialog_script_receives_one_enter_and_proof_continues(tmp_path, caplog, monkeypatch, dialog):
    monkeypatch.setattr(spawnctl_mod, 'SPAWN_SUBMISSION_PROOF_BOUND_S', .5)
    monkeypatch.setattr(spawnctl_mod, 'NATIVE_TRUST_POLL_S', .01, raising=False)
    script = tmp_path / 'fake_codex.py'
    script.write_text('import sys, time\nprint(sys.argv[1], flush=True)\ninput()\nprint("USER", flush=True)\ntime.sleep(2)\n')
    async def run():
        proc = await asyncio.create_subprocess_exec(sys.executable, str(script), dialog,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        pane = []
        async def read():
            while line := await proc.stdout.readline(): pane.append(line.decode())
        reader = asyncio.create_task(read())
        class Pane:
            enters = 0
            async def capture(self, name): return ''.join(pane)
            async def send_enter(self, name):
                self.enters += 1
                proc.stdin.write(b'\n'); await proc.stdin.drain()
            async def paste(self, *args): raise AssertionError('Never paste into a dialog')
        tmux = Pane()
        class Proof:
            async def wait_for_initial_user_event(self, sid, *, expected_text, timeout_s):
                end = time.monotonic() + timeout_s
                while time.monotonic() < end:
                    if 'USER\n' in pane: return EventProof('proven',sid,0,event_id=1)
                    await asyncio.sleep(.005)
                return EventProof('pending',sid,0,reason='timeout')
        ctl = SpawnCtl(Store(':memory:'), Sessions(Store(':memory:'),local_host=HOST), tmux=tmux, submission_proof=Proof())
        try:
            result = await ctl._confirm_native_initial_prompt(HOST+':child', TEXT)
            assert result.proven, 'baseline times out at the current trust dialog without Enter'
            assert tmux.enters == 1
        finally:
            proc.terminate(); await proc.wait(); await reader
    with caplog.at_level(logging.WARNING): asyncio.run(run())
    assert 'trust_dialog' in caplog.text


@pytest.mark.parametrize('original_instance,expected_reason', [('same-daemon','late_first_user_event'), ('old-daemon','adopted_after_restart')])
def test_late_native_settlement_retains_failed_outcome_receipt(monkeypatch, original_instance, expected_reason):
    async def run():
        store = Store(':memory:'); store.start()
        try:
            tmux = PeerEventTmux(alive=True, pane_pid='12345', screen='composer')
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux, owner_instance_id='same-daemon')
            await sessions.open(HOST,'late',provider='codex',objective='Probe late native delivery')
            await store.reserve_stream_id(HOST,'late',ttl_s=60,request_id='late-request',nonce='TEST',owner_instance_id='same-daemon')
            intent = {'open_fields':{'provider':'codex','objective':'Probe late native delivery'},'brief':TEXT,
                      'delivery_receipt':{'transport':'native_argv','state':'requested'}}
            await store.record_spawn_intent(HOST,'late',intent,request_id='late-request')
            failed = {**intent['delivery_receipt'],'state':'indeterminate',
                      'spawn_daemon_instance_id':original_instance,'failure_code':'native_initial_prompt_delivery_failed','delivery_failed_at':'2026-01-01T00:00:00Z'}
            await store.set_spawn_outcome(HOST,'late','indeterminate',request_id='late-request',delivery_receipt=failed)
            async def bound(*a, **kw): return True
            async def proof(*a, **kw): return EventProof('proven',HOST+':late',0,event_id=1)
            monkeypatch.setattr(ctl,'_commit_pane_bound',bound)
            monkeypatch.setattr(ctl,'_confirm_native_initial_prompt',proof)
            assert await ctl._adopt_interrupted_spawn(HOST,'late','late-request',intent,tmux,
                reservation={'owner_instance_id':'same-daemon','nonce':'TEST'}) == 'adopted'
            outcome=await store.get_spawn_outcome(HOST,'late')
            receipt=outcome['delivery_receipt']
            assert receipt.get('failure_code') == failed['failure_code']
            assert receipt.get('delivery_failed_at') == failed['delivery_failed_at']
            assert receipt.get('delivered_late_at')
            assert outcome['reason'].startswith(expected_reason)
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('pane,classification', [('Trust this folder?\nTrust and continue','trust_dialog'),
    ('View account usage or redeem an earned reset\n1. Show usage\n2. Redeem usage limit reset', 'reset_interstitial'),('', 'silent')])
def test_disconnected_native_failure_notifies_parent_durably(monkeypatch, caplog, pane, classification):
    async def run():
        store=Store(':memory:');store.start()
        try:
            class Tui(CodexTui):
                async def capture(self,name): return pane
            tmux=Tui()
            sessions=Sessions(store,tmux=tmux,local_host=HOST)
            class Proof:
                async def wait_for_initial_user_event(self,sid,**kw): return EventProof('pending',sid,0,reason='timeout')
            ctl=SpawnCtl(store,sessions,tmux=tmux,submission_proof=Proof())
            ctl._resolve_launch=_resolved_native_codex
            await sessions.open(HOST,'parent',objective='Receive child failure')
            task=asyncio.create_task(ctl.spawn({'schema':'SpawnRequestV2','provider':'codex','model':'gpt-6-sol',
                'effort':'high','initial_prompt':TEXT,'session_name':'failed','request_id':'failure-key',
                'parent_stream_id':HOST+':parent','objective':'Probe disconnected native failure'},HOST))
            await task
            # Admission reply has returned: there is no requester waiting for completion.
            await asyncio.gather(*tuple(ctl._background_spawns),return_exceptions=True)
            notices=await store.submit(lambda c:[dict(r) for r in c.execute("SELECT * FROM v2_outbound_notices WHERE kind='spawn_delivery_failed'")])
            assert len(notices)==1
            notice=notices[0]
            assert notice['recipient_stream_id']==HOST+':parent'
            assert ('reset_blocked' if classification=='reset_interstitial' else 'native_initial_prompt_delivery_failed') in notice['body']
            assert classification in notice['body']
        finally: store.stop()
    with caplog.at_level(logging.WARNING): asyncio.run(run())
    assert 'native_initial_prompt_delivery_failed' in caplog.text


def test_slow_capture_cannot_extend_native_proof_deadline(monkeypatch):
    monkeypatch.setattr(spawnctl_mod, 'SPAWN_SUBMISSION_PROOF_BOUND_S', .02)
    async def run():
        class Pane:
            async def capture(self,name): await asyncio.sleep(10)
        class Proof:
            async def wait_for_initial_user_event(self,sid,**kw):
                await asyncio.sleep(kw['timeout_s'])
                return EventProof('pending',sid,0,reason='timeout')
        store=Store(':memory:')
        ctl=SpawnCtl(store,Sessions(store,local_host=HOST),tmux=Pane(),submission_proof=Proof())
        result=await asyncio.wait_for(ctl._confirm_native_initial_prompt(HOST+':child',TEXT),.2)
        assert result.state=='pending'
    asyncio.run(run())
