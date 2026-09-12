"""Context crossing journeys through the real ledger/store/tell path."""
from __future__ import annotations

import asyncio
import time

import pytest

from context_adapters import ContextReading, context_fields
from test_nudges import HOST, Harness, _new_store, _iso
from routing_integrity import RoutingIntegrity


def test_hidden_handoff_crossing_notifies_seat_and_live_parent():
    async def run():
        store = _new_store()
        try:
            h = Harness(store)
            await h.open('parent', visibility='hidden', user_event_count=0)
            await h.open('child', visibility='hidden', parent_stream_id=f'{HOST}:parent', user_event_count=0)
            await h.observe()
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, 'child', provider='claude',
                reading=ContextReading(700_000, model='claude-fable-5-1'),
                observed_at=_iso(time.time()), expected_generation=None)
            result = await h.job.run_pass()
            assert result.sent == 2
            assert {name for name, text in h.tmux.pasted_by_name} == {'parent', 'child'}
            assert all('context_handoff' in text for name, text in h.tmux.pasted_by_name)
            assert len(await store.nudge_states()) >= 1
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('tokens,expected', [(250_000,'none'),(399_999,'none'),(400_000,'advisory'),(599_999,'advisory'),(600_000,'handoff')])
def test_claude_threshold_contract(tokens, expected, monkeypatch):
    for key in ('ADVISORY_ABS','HANDOFF_ABS','ADVISORY_PCT','HANDOFF_PCT'):
        monkeypatch.delenv('PENTACLE_CONTEXT_'+key, raising=False)
    assert context_fields('claude', ContextReading(tokens, model='claude-fable-5-1'))[2] == expected


async def _context_harness(store, *, parent=True, config=None):
    h = Harness(store, config)
    if parent:
        await h.open('parent', visibility='hidden', user_event_count=0)
    await h.open('child', visibility='hidden', user_event_count=0,
                 parent_stream_id=f'{HOST}:parent' if parent else None)
    await h.observe()
    return h


async def _read(h, tokens, at, *, provider='claude', name='child'):
    return await RoutingIntegrity(h.store, h.sessions).observe_context(
        HOST, name, provider=provider,
        reading=ContextReading(tokens, model='claude-fable-5-1', model_context_window=1_000_000),
        observed_at=_iso(at))


def test_ingestion_preserves_inter_sweep_compaction_and_restart_epochs(tmp_path):
    import json
    from ledger import NudgeJob
    async def run():
        store = _new_store(str(tmp_path/'episodes.db'))
        try:
            h = await _context_harness(store); now=time.time()
            await _read(h, 450_000, now-100)
            assert (await h.job.run_pass()).sent == 2
            first=json.loads((await store.nudge_state(f'{HOST}:child','context_advisory'))['basis'])
            await _read(h, 480_000, now-90)
            h.job=NudgeJob(h.sessions,h.comms,store)
            assert (await h.job.run_pass()).sent == 0
            assert json.loads((await store.nudge_state(f'{HOST}:child','context_advisory'))['basis'])['epoch']==first['epoch']
            await _read(h,700_000,now-80)
            assert (await h.job.run_pass()).sent==2
            # Both observations happen without a nudge sweep in between.
            await _read(h,450_000,now-70)
            await _read(h,710_000,now-60)
            assert (await h.job.run_pass()).sent==2
            await _read(h,100_000,now-50)
            await _read(h,450_000,now-40)
            assert (await h.job.run_pass()).sent==2
            assert len(h.tmux.pasted)==8
            assert all('480,000' not in text for text in h.tmux.pasted)
            # Reopen the SQLite worker as well as the job, not just an object.
            store.stop(); store.start(); await h.sessions.refresh()
            h.job=NudgeJob(h.sessions,h.comms,store)
            assert (await h.job.run_pass()).sent==0
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('fields', [
    {'context_updated_at':None}, {'context_updated_at':'not-a-date'},
    {'context_updated_at':'2026-08-01T00:00:00Z'},
    {'context_updated_at':'2999-01-01T00:00:00Z'},
    {'context_tokens':None}, {'context_tokens':-1}, {'context_tokens':float('nan')},
    {'model_context_window':0}, {'host_status':'offline'},
    {'pane_status':'pane_dead'}, {'routing_integrity':'mismatch'}, {'status':'closed'},
])
def test_context_suppression_never_claims_or_delivers(fields):
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);await _read(h,700_000,time.time()-10)
            if set(fields) <= {'host_status', 'pane_status'}:
                h.sessions.apply_live(f'{HOST}:child',**fields)
            else:
                await store.update_session(HOST,'child',**fields)
                await h.sessions.refresh()
            result=await h.job.run_pass()
            assert result.sent==0 and result.attempted==0
            assert not h.tmux.pasted
        finally: store.stop()
    asyncio.run(run())


def test_working_hidden_child_needs_no_operator_turn_or_card():
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);await _read(h,700_000,time.time()-10)
            h.sessions.apply_live(f'{HOST}:child',working=True)
            assert (await h.job.run_pass()).sent==2
        finally: store.stop()
    asyncio.run(run())


def test_cap_counts_each_recipient_and_preserves_deferred_parent():
    from ledger import NudgeConfig
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store,config=NudgeConfig(max_per_pass=1))
            await _read(h,700_000,time.time()-10)
            result=await h.job.run_pass()
            assert result.sent==1 and result.capped
            assert [n for n,_ in h.tmux.pasted_by_name]==['child']
            assert (await h.job.run_pass()).sent==1
            assert [n for n,_ in h.tmux.pasted_by_name]==['child','parent']
            assert (await h.job.run_pass()).sent==0
        finally: store.stop()
    asyncio.run(run())


def test_parentless_context_uses_real_notify_dedup_card(tmp_path):
    from notify import Notify
    async def run():
        store=_new_store();notify=Notify(db_path=str(tmp_path/'notifications.db'))
        await notify.start()
        try:
            h=await _context_harness(store,parent=False);h.job.notify=notify
            await _read(h,700_000,time.time()-10)
            assert (await h.job.run_pass()).sent==2
            assert (await h.job.run_pass()).sent==0
            cards=(await notify.notification({'type':'notification.list'}))['notifications']
            assert len(cards)==1 and cards[0]['producer']=='context_nudge'
            assert f'{HOST}:child' in cards[0]['body']
        finally: await notify.stop();store.stop()
    asyncio.run(run())


def test_parent_unavailable_is_not_operator_fallback_and_reparent_is_independent():
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store)
            await _read(h,700_000,time.time()-10)
            h.sessions.apply_live(f'{HOST}:parent',host_status='offline')
            assert (await h.job.run_pass()).sent==1
            await h.open('new-parent',visibility='hidden',user_event_count=0)
            await h.observe()
            await store.update_session(HOST,'child',parent_stream_id=f'{HOST}:new-parent')
            await h.sessions.refresh()
            assert (await h.job.run_pass()).sent==1
            assert [n for n,_ in h.tmux.pasted_by_name]==['child','new-parent']
        finally: store.stop()
    asyncio.run(run())


def test_out_of_order_ingestion_cannot_rearm_or_replace_telemetry():
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);now=time.time()
            await _read(h,700_000,now-10)
            assert (await h.job.run_pass()).sent==2
            assert await _read(h,100_000,now-20) is None
            assert h.sessions.get(f'{HOST}:child')['context_tokens']==700_000
            assert (await h.job.run_pass()).sent==0
        finally: store.stop()
    asyncio.run(run())


def test_pending_codex_receipt_is_reconciled_without_input(monkeypatch):
    import comms as comms_module
    from test_tell_immediate_delivery import ScriptedCodex
    monkeypatch.setattr(comms_module,'SUBMISSION_EVIDENCE_TIMEOUT_S',.01)
    monkeypatch.setattr(comms_module,'SUBMISSION_EVIDENCE_POLL_S',.001)
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store)
            await store.update_session(HOST,'child',provider='codex')
            await h.sessions.refresh()
            await _read(h,800_000,time.time()-10,provider='codex')
            # Real Comms runs; only its external pane is scripted.
            pane=ScriptedCodex(submit_on_retry=False)
            h.spawnctl.tmux=pane
            result=await h.job.run_pass()
            assert result.pending>=1
            count=len(pane.pastes);assert count==2
            await h.job.run_pass()
            assert len(pane.pastes)==count
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('window,tokens,expected', [(200_000,139_999,'none'),(200_000,140_000,'advisory'),(200_000,170_000,'handoff')])
def test_small_claude_window_keeps_percentage_caps(window,tokens,expected,monkeypatch):
    for key in ('ADVISORY_ABS','HANDOFF_ABS','ADVISORY_PCT','HANDOFF_PCT'):
        monkeypatch.delenv('PENTACLE_CONTEXT_'+key,raising=False)
    assert context_fields('claude',ContextReading(tokens,model='claude-haiku-4-5'))==(tokens,window,expected)


def test_threshold_environment_overrides_and_codex_contract(monkeypatch):
    monkeypatch.setenv('PENTACLE_CONTEXT_ADVISORY_ABS','200000')
    monkeypatch.setenv('PENTACLE_CONTEXT_HANDOFF_ABS','300000')
    assert context_fields('claude',ContextReading(250_000,model='claude-fable-5-1'))[2]=='advisory'
    for tokens,level in ((499_999,'none'),(500_000,'advisory'),(750_000,'handoff')):
        assert context_fields('codex',ContextReading(tokens,model_context_window=1_000_000))[2]==level


def test_unconfirmed_claude_paste_is_not_counted_as_delivered(monkeypatch):
    import comms as comms_module
    from test_tell_immediate_delivery import ScriptedClaude
    monkeypatch.setattr(comms_module,'SUBMISSION_EVIDENCE_TIMEOUT_S',.01)
    monkeypatch.setattr(comms_module,'SUBMISSION_EVIDENCE_POLL_S',.001)
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store)
            h.sessions.apply_live(f'{HOST}:parent',host_status='offline')
            await _read(h,700_000,time.time()-10)
            pane=ScriptedClaude(start='❯ ',submit_on_paste=False);h.spawnctl.tmux=pane
            result=await h.job.run_pass()
            assert result.sent==0 and result.pending==1
            assert (await h.job.run_pass()).attempted==0
            assert len(pane.pastes)==1
        finally:store.stop()
    asyncio.run(run())


def test_precommit_route_failure_retries_same_identity_after_cooldown(monkeypatch):
    import json
    from ledger import NudgeConfig
    from sessions import VerbError
    class Hosts:
        blocked=True
        def is_local(self, host):return False
        async def ensure_reachable(self, host, verb):
            if self.blocked:raise VerbError('host_offline','injected pre-input refusal')
        def tmux_for(self,host):return self.tmux
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store,config=NudgeConfig(cooldown_s=1))
            now=time.time();await _read(h,700_000,now-10)
            hosts=Hosts();hosts.tmux=h.tmux;h.comms.hosts=hosts
            result=await h.job.run_pass();assert result.errors==2 and not h.tmux.pasted
            before=json.loads((await store.nudge_state(f'{HOST}:child','context_handoff'))['basis'])
            assert (await h.job.run_pass()).attempted==0
            hosts.blocked=False;monkeypatch.setattr('ledger.time.time',lambda:now+2)
            await _read(h,710_000,now+1)
            assert (await h.job.run_pass()).sent==2
            after=json.loads((await store.nudge_state(f'{HOST}:child','context_handoff'))['basis'])
            assert {k:v['tell_id'] for k,v in before['deliveries'].items()}=={k:v['tell_id'] for k,v in after['deliveries'].items()}
            assert all('700,000' in text and '710,000' not in text for text in h.tmux.pasted)
        finally:store.stop()
    asyncio.run(run())


def test_cancel_after_paste_without_receipt_never_blindly_resends():
    from ledger import NudgeJob
    from test_nudges import RecordingTmux
    class InterruptedPane(RecordingTmux):
        async def paste(self,name,text):
            await super().paste(name,text)
            if name=='child':raise asyncio.CancelledError()
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);await _read(h,700_000,time.time()-10)
            pane=InterruptedPane();h.spawnctl.tmux=pane
            with pytest.raises(asyncio.CancelledError):await h.job.run_pass()
            h.job=NudgeJob(h.sessions,h.comms,store)
            assert (await h.job.run_pass()).sent==1
            assert [name for name,_ in pane.pasted_by_name]==['child','parent']
            assert (await h.job.run_pass()).attempted==0
        finally:store.stop()
    asyncio.run(run())


def test_crash_after_receipt_before_episode_stamp_reconciles_without_resend():
    from store import Store
    from ledger import NudgeJob
    class CrashStore(Store):
        armed=True
        async def record_nudge(self, sid,kind,at,basis=''):
            if self.armed and '"outcome": "delivered"' in basis:
                self.armed=False
                raise RuntimeError('injected crash after durable tell receipt')
            return await super().record_nudge(sid,kind,at,basis)
    async def run():
        store=CrashStore(':memory:');store.start()
        try:
            h=await _context_harness(store);await _read(h,700_000,time.time()-10)
            with pytest.raises(RuntimeError,match='injected crash'):await h.job.run_pass()
            assert [n for n,_ in h.tmux.pasted_by_name]==['child']
            h.job=NudgeJob(h.sessions,h.comms,store)
            assert (await h.job.run_pass()).sent==1
            assert [n for n,_ in h.tmux.pasted_by_name]==['child','parent']
        finally:store.stop()
    asyncio.run(run())


def test_handoff_supersedes_an_unsent_advisory_until_below_advisory_rearm():
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);now=time.time()
            await _read(h,700_000,now-20)
            assert (await h.job.run_pass()).sent==2
            await _read(h,450_000,now-10)
            assert (await h.job.run_pass()).sent==0
            await _read(h,100_000,now-5);await _read(h,450_000,now-1)
            assert (await h.job.run_pass()).sent==2
        finally:store.stop()
    asyncio.run(run())


def test_reopened_source_generation_starts_a_new_episode(monkeypatch):
    import json
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);now=time.time()
            await _read(h,700_000,now-10)
            assert (await h.job.run_pass()).sent==2
            old=json.loads((await store.nudge_state(f'{HOST}:child','context_handoff'))['basis'])
            await store.update_session(HOST,'child',status='closed')
            await h.sessions.refresh()
            await h.open('child',visibility='hidden',parent_stream_id=f'{HOST}:parent',user_event_count=0)
            await h.observe()
            monkeypatch.setattr('ledger.time.time',lambda:now+2)
            await _read(h,700_000,now+1)
            assert (await h.job.run_pass()).sent==2
            new=json.loads((await store.nudge_state(f'{HOST}:child','context_handoff'))['basis'])
            assert old['generation']!=new['generation'] and old['epoch']!=new['epoch']
            assert len(h.tmux.pasted)==4
        finally:store.stop()
    asyncio.run(run())


def test_observation_and_episode_transaction_roll_back_together():
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store);now=time.time()
            await _read(h,450_000,now-20)
            def install_failure(conn):
                conn.execute("CREATE TRIGGER fail_context BEFORE INSERT ON v2_nudge_state WHEN NEW.kind='context_handoff' BEGIN SELECT RAISE(ABORT,'injected episode write'); END")
            await store.submit(install_failure)
            import sqlite3
            with pytest.raises(sqlite3.IntegrityError,match='injected episode write'):
                await _read(h,700_000,now-10)
            assert (await store.fetch_session(HOST,'child'))['context_tokens']==450_000
            assert (await h.job.run_pass()).sent==2  # advisory was not superseded by failed write
            assert all('context_advisory' in text for text in h.tmux.pasted)
        finally:store.stop()
    asyncio.run(run())


def test_close_prunes_ingestion_episodes_even_when_nudge_job_never_runs():
    async def run():
        store=_new_store()
        try:
            h=await _context_harness(store)
            await _read(h,700_000,time.time()-10)
            assert len(await store.nudge_states())==2
            await store.mark_closed(HOST,'child',closed_at=_iso(time.time()),pane_status='pane_dead')
            assert await store.nudge_states()=={}
        finally:store.stop()
    asyncio.run(run())


def test_codex_file_batch_compaction_rearms_delivered_episode(tmp_path, monkeypatch):
    """A dip and rise in one FD read must survive until the next nudge sweep."""
    import json
    from ingest import Ingest, _close_stream

    async def run():
        store = _new_store()
        ingest = None
        try:
            h = Harness(store)
            rollout = tmp_path / 'rollout.jsonl'
            now = time.time()

            def reading(tokens, at):
                return json.dumps({
                    'type': 'event_msg', 'timestamp': _iso(at),
                    'payload': {'type': 'token_count', 'info': {
                        'last_token_usage': {'total_tokens': tokens},
                        'model_context_window': 1_000_000,
                    }},
                }) + '\n'

            rollout.write_text(json.dumps({
                'type': 'session_meta',
                'payload': {'id': '01a000b9-60a7-7a51-9e9a-bfed1711c4fb'},
            }) + '\n' + reading(800_000, now - 90))
            await h.open('parent', visibility='hidden', user_event_count=0)
            await h.open('child', provider='codex', visibility='hidden',
                         parent_stream_id=f'{HOST}:parent', user_event_count=0,
                         jsonl_path=str(rollout))
            await h.observe()
            paste = h.tmux.paste

            async def provider_paste(name, text):
                await paste(name, text)
                if name == 'child':
                    # Provider counterpart: consumed input emits a real durable
                    # USER event after Comms' pre-paste sequence watermark.
                    await store.append_session_event(f'{HOST}:child', {
                        'stream_id': f'{HOST}:child', 'provider': 'codex',
                        'kind': 'USER', 'text': text, 'timestamp': _iso(time.time()),
                    }, identity=f'provider-proof:{text}', limit=500)

            monkeypatch.setattr(h.tmux, 'paste', provider_paste)
            ingest = Ingest(store, h.sessions, h.tmux, lambda _: asyncio.sleep(0),
                            local_host=HOST, recent_limit=20,
                            routing_integrity=RoutingIntegrity(store, h.sessions))
            assert await ingest.run_pass() == 0  # bookkeeping is not a chat event
            assert (await h.job.run_pass()).sent == 2
            first = json.loads((await store.nudge_state(f'{HOST}:child', 'context_handoff'))['basis'])
            assert all(d['outcome'] == 'delivered' for d in first['deliveries'].values())
            with rollout.open('a') as stream:
                stream.write(reading(100_000, now - 80) + reading(800_000, now - 70))
            assert await ingest.run_pass() == 0
            second = json.loads((await store.nudge_state(f'{HOST}:child', 'context_handoff'))['basis'])
            assert second['epoch'] != first['epoch']
            assert second['crossing']['observed_at'] == _iso(now - 70)
            assert (await h.job.run_pass()).sent == 2
            second = json.loads((await store.nudge_state(f'{HOST}:child', 'context_handoff'))['basis'])
            assert {d['tell_id'] for d in first['deliveries'].values()}.isdisjoint(
                d['tell_id'] for d in second['deliveries'].values())
            assert len(h.tmux.pasted) == 4
            assert await ingest.run_pass() == 0
            assert (await h.job.run_pass()).attempted == 0
        finally:
            if ingest is not None:
                for state in ingest._streams.values():
                    _close_stream(state)
            store.stop()

    asyncio.run(run())
