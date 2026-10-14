"""Injected setup has system provenance; commissioned briefs retain their sender."""
import argparse
import asyncio
import hashlib

import pytest
from agent_orch.cli import _initial_prompt_payload
from claude_jsonl_norm import normalize_claude_jsonl_records
from codex_rollout_norm import normalize_codex_rollout_records
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from submission_events import DurableUserEventProof, EventWatermark
import tmux_transport

PARENT = 'hosta:v2-parent'
SPAWN = 'spawn-public-probe'
OPENERS = ['[role baseline: lead]',
           '# AGENTS.md instructions for /tmp/workspace',
           'This session is being continued from a previous conversation']


def normalize(provider, text):
    if provider == 'claude':
        records = [{'type': 'user', 'message': {'role': 'user', 'content': text}}]
        fn = normalize_claude_jsonl_records
    else:
        records = [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': text}]}}]
        fn = normalize_codex_rollout_records
    return fn(records, host='hosta', session_name='v2-child')[0]


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('opener', OPENERS)
def test_injection_first_line_only(provider, opener):
    assert normalize(provider, '\n\n' + opener + '\ncontext')['kind'] == 'SYSTEM'
    assert normalize(provider, 'Operator text\n' + opener)['kind'] == 'USER'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('text', ['[role baseline: lead]x', '[role baseline: lead',
                                 '[role baseline: Lead]', 'Please proceed'])
def test_typed_near_matches(provider, text):
    assert normalize(provider, text)['kind'] == 'USER'


@pytest.mark.parametrize('provider,native,large', [('claude', False, False),
                                                 ('claude', False, True), ('codex', True, False)])
@pytest.mark.parametrize('parent', [PARENT, ''])
def test_cli_composed_prompt_through_actual_spawn_wire(provider, native, large, parent):
    baseline = '# QA Baseline\nRead the spec.\n'
    prompt = 'Complete the review.\n' + ('x' * 200 if large else '')
    args = argparse.Namespace(initial_prompt=prompt, initial_prompt_file=None)
    composed = _initial_prompt_payload(args, None, baseline_content=baseline)[0]['initial_prompt']
    class Stage:
        data = None
        async def stage_text(self, path, data):
            self.data = data
    stage = Stage()
    store = Store(':memory:')
    ctl = SpawnCtl(store, Sessions(store, tmux=stage, local_host='hosta'), tmux=stage)
    # Exercise the three delivery wires with the actual staging helper.
    old = tmux_transport.INITIAL_PROMPT_STAGE_THRESHOLD_BYTES
    tmux_transport.INITIAL_PROMPT_STAGE_THRESHOLD_BYTES = 100 if large else 10000
    try:
        wire, receipt = asyncio.run(ctl._prepare_brief(ctl._compose_spawn_brief(composed, parent_stream_id=parent, spawn_id=SPAWN), stage, stage_host='hosta',
            delivery_receipt={}, native_initial_prompt=native))
    finally:
        tmux_transport.INITIAL_PROMPT_STAGE_THRESHOLD_BYTES = old
    event = normalize(provider, wire)
    assert event['kind'] == ('TELL' if parent else 'USER')
    if parent:
        assert event['raw']['sender'] == parent
        assert event['raw']['tell_id'] == SPAWN
    if large:
        assert stage.data == composed.encode()
        assert receipt['prompt_sha256'] == hashlib.sha256(stage.data).hexdigest()
        assert 'Read ' in wire
    elif native:
        assert stage.data == wire.encode()
    else:
        assert wire.endswith(composed)
    event.update(stream_id='hosta:v2-child', daemon_seq=1)
    class Tail:
        async def fetch_session_event_tail(self, stream_id, *, limit):
            return [event]
    proof = DurableUserEventProof(Tail(), local_host='hosta')
    if native:
        result = asyncio.run(proof.wait_for_initial_user_event('hosta:v2-child', expected_text=wire, timeout_s=.1))
    else:
        result = asyncio.run(proof.lookup('hosta:v2-child', expected_text=wire,
                            watermark=EventWatermark('hosta:v2-child', 0, 'reachable')))
    assert result.proven

@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_setup_records_do_not_qualify_nudge_or_start_working(provider):
    from test_nudges import Harness
    from working_state import WorkingStateTracker
    from store import INBOUND_TURN_KINDS
    async def journey():
        store = Store(':memory:')
        store.start()
        try:
            h = Harness(store)
            await h.open('v2-child', provider=provider, user_event_count=0)
            tracker = WorkingStateTracker()
            for index, text in enumerate(OPENERS):
                event = normalize(provider, text)
                await store.append_session_event('hosta:v2-child', event,
                    identity=f'injected-{index}', limit=500)
                tracker.observe(event, 10000)
            assert sum([await store.count_session_events('hosta:v2-child', kind=k)
                        for k in INBOUND_TURN_KINDS]) == 0
            assert not await h.job._passes_user_turn_grace(h.sessions.get('hosta:v2-child'))
            assert tracker.get('hosta:v2-child', 10000)['tokens_phase'] == 'idle'
            wire = SpawnCtl._compose_spawn_brief('# QA Baseline\nReview.',
                      parent_stream_id=PARENT, spawn_id=SPAWN)
            peer = normalize(provider, wire)
            await store.append_session_event('hosta:v2-child', peer, identity='commission', limit=500)
            assert await store.count_session_events('hosta:v2-child', kind='TELL') == 1
            assert await store.count_session_events('hosta:v2-child', kind='USER') == 0
        finally:
            store.stop()
    asyncio.run(journey())


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_native_receipt_rejects_wrong_wire_sender_anchor_stream_and_duplicates(provider):
    wire = SpawnCtl._compose_spawn_brief('  Exact bytes\n\n', parent_stream_id=PARENT, spawn_id=SPAWN)
    event = normalize(provider, wire)
    event.update(stream_id='hosta:v2-child', daemon_seq=1)
    class Tail:
        events = [event]
        async def fetch_session_event_tail(self, stream_id, *, limit):
            return self.events
    tail = Tail()
    proof = DurableUserEventProof(tail, local_host='hosta')
    def check(expected=wire):
        return asyncio.run(proof.wait_for_initial_user_event('hosta:v2-child', expected_text=expected, timeout_s=.01))
    assert check().proven
    for wrong in [wire.rstrip(), wire.replace(PARENT, 'hosta:v2-other'), wire.replace(SPAWN, 'spawn-other')]:
        assert check(wrong).state == 'rejected'
    tail.events = [event, dict(event, daemon_seq=2)]
    assert check().reason == 'initial_user_event_not_exactly_once'
    tail.events = [dict(event, stream_id='hosta:v2-other')]
    assert check().state == 'pending'
    tail.events = []
    assert check().state == 'pending'

@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('body', OPENERS)
def test_system_injection_delivery_receipt_still_settles(provider, body):
    from comms import Comms
    event = normalize(provider, body + '\nContext')
    event.update(stream_id='hosta:v2-child', daemon_seq=10)
    class Tail:
        async def fetch_session_event_tail(self, stream_id, *, limit):
            return [event]
    proof = DurableUserEventProof(Tail(), local_host='hosta')
    observed = asyncio.run(proof.lookup('hosta:v2-child', expected_text=body + '\nContext',
                          watermark=EventWatermark('hosta:v2-child', 9, 'reachable')))
    assert observed.proven
    assert Comms._tail_event_matches_submission(event, 'hosta:v2-child', body + '\nContext')
    assert not Comms._tail_event_matches_submission(event, 'hosta:v2-child', body + '\nWrong')


def test_system_receipt_negative_controls():
    from comms import Comms
    from submission_events import system_submission_matches, provider_text_digest
    body = '[role baseline: lead]\nExact body'
    event = {'kind': 'SYSTEM', 'text': body, 'stream_id': 'hosta:v2-child', 'daemon_seq': 10}
    assert system_submission_matches(event, body)
    for bad in [dict(event, text='Ordinary context'), dict(event, text=body + ' '),
                dict(event, provider_text_digest='0' * 64), dict(event, kind='USER')]:
        assert not system_submission_matches(bad, body)
    assert dict(event, kind='USER')['kind'] == 'USER'  # proof never reclassifies stored input
    class Tail:
        events = []
        async def fetch_session_event_tail(self, stream_id, *, limit):
            return self.events
    tail = Tail()
    proof = DurableUserEventProof(tail, local_host='hosta')
    def lookup():
        return asyncio.run(proof.lookup('hosta:v2-child', expected_text=body,
                            watermark=EventWatermark('hosta:v2-child', 9, 'reachable')))
    tail.events = [dict(event, daemon_seq=9)]
    assert not lookup().proven
    tail.events = [event, dict(event, daemon_seq=11)]
    assert lookup().state == 'rejected'
    tail.events = [dict(event, provider_text_digest=provider_text_digest(body))]
    assert lookup().proven
    comms = object.__new__(Comms)
    comms.store = tail
    tail.events = [dict(event, daemon_seq=9)]
    assert not asyncio.run(comms._submission_event_proven('hosta:v2-child', body, 9))
    tail.events = [event, dict(event, daemon_seq=11)]
    assert not asyncio.run(comms._submission_event_proven('hosta:v2-child', body, 9))
    tail.events = [event]
    assert asyncio.run(comms._submission_event_proven('hosta:v2-child', body, 9))
    assert not Comms._tail_event_matches_submission(dict(event, kind='SYSTEM', text='Context'),
                                                   'hosta:v2-child', 'Context')

@pytest.mark.parametrize('provider,native,large', [('claude', False, False),
                                                 ('claude', False, True), ('codex', True, False)])
def test_spawn_admission_delivers_composed_cli_bytes(provider, native, large, monkeypatch):
    import launch
    async def run():
        store = Store(':memory:')
        store.start()
        class Transport:
            stages = {}
            alive = set()
            wire = ''
            async def stage_text(self, path, data):
                self.stages[path] = data
            async def has_session(self, name):
                return name in self.alive
            async def new_session(self, name, command, cwd=None, env=None):
                self.alive.add(name)
                if native:
                    # The native launcher consumes the exact path computed by
                    # admission, rather than whichever file happened to stage.
                    self.ingest_task = asyncio.create_task(self.native_ingest(name))
            async def native_ingest(self, name):
                for _ in range(100):
                    if sessions.get('hosta:' + name):
                        await self.deliver(name, self.stages[self.native_path].decode())
                        return
                    await asyncio.sleep(.001)
                raise AssertionError('spawn admission did not open the target')
            async def deliver(self, name, wire):
                self.wire = wire
                await store.append_session_event('hosta:' + name, normalize(provider, wire),
                                                  identity='delivered-wire', limit=500)
            async def paste(self, name, text):
                await self.deliver(name, text)
            async def capture(self, name):
                return 'READY\n⏵⏵ bypass permissions on (bypass)\n❯ '
            async def pane_pid(self, name):
                return ''
            async def session_state(self, name):
                return 'alive' if name in self.alive else 'gone'
            async def kill_session(self, name):
                self.alive.discard(name)
        tmux = Transport()
        sessions = Sessions(store, tmux=tmux, local_host='hosta')
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        async def resolve(msg, host, name):
            tmux.native_path = msg.get('_native_initial_prompt_path')
            command = launch.NATIVE_INITIAL_PROMPT_LAUNCHER if native else 'provider-command'
            return command, {'resolved_launch_tuple': {'provider': provider}}, {'provider': provider}
        monkeypatch.setattr(ctl, '_resolve_launch', resolve)  # counterpart launcher only
        monkeypatch.setattr(tmux_transport, 'INITIAL_PROMPT_STAGE_THRESHOLD_BYTES', 100 if large else 10000)
        try:
            await sessions.open('hosta', 'v2-parent', provider=provider)
            args = argparse.Namespace(initial_prompt='Review.\n' + ('x' * 200 if large else ''), initial_prompt_file=None)
            composed = _initial_prompt_payload(args, None, baseline_content='# QA Baseline\nRead the spec.')[0]['initial_prompt']
            reply = await ctl.spawn({'objective': 'Validate input provenance', 'session_name': 'v2-child',
                'request_id': SPAWN, 'parent_stream_id': PARENT, 'ready_marker': 'READY',
                'initial_prompt': composed}, 'hosta')
            assert reply['initial_prompt_delivery']['state'] == 'delivered'
            event = normalize(provider, tmux.wire)
            assert event['kind'] == 'TELL'
            assert event['raw']['sender'] == PARENT
            if large:
                assert list(tmux.stages.values()) == [composed.encode()]
            else:
                assert tmux.wire == f'[from {PARENT}] [send:{SPAWN}]\n{composed}'
        finally:
            for name in list(tmux.alive):
                await tmux.kill_session(name)
            store.stop()
    asyncio.run(run())

@pytest.mark.parametrize('text', OPENERS)
def test_queued_reserved_injection_is_system(text):
    record = {'type': 'attachment', 'attachment': {'type': 'queued_command',
              'origin': {'kind': 'human'}, 'prompt': text + '\ncontext',
              'timestamp': '2026-10-01T00:00:00Z'}}
    event = normalize_claude_jsonl_records([record], host='hosta', session_name='v2-child')[0]
    assert event['kind'] == 'SYSTEM'
    assert event['text'] == text + '\ncontext'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_p3_notice_keeps_pre_cutover_user_classification(provider):
    text = '[pentacle-notice:notice-1]\nExisting notice behavior.'
    event = normalize(provider, text)
    assert event['kind'] == 'USER'
    assert event['text'] == text
    if provider == 'claude':
        queued = {'type': 'attachment', 'attachment': {'type': 'queued_command',
                  'origin': {'kind': 'human'}, 'prompt': text}}
        event = normalize_claude_jsonl_records([queued], host='hosta', session_name='v2-child')[0]
        assert event['kind'] == 'USER' and event['text'] == text
        assert event['raw']['subtype'] == 'queued-command'


@pytest.mark.parametrize('provider', ['claude', 'codex'])
@pytest.mark.parametrize('body', OPENERS)
def test_new_system_kind_keeps_legacy_source_identity_without_history_mutation(provider, body):
    from ingest import _identity_key
    system = normalize(provider, body + '\nInjected context')
    system['raw']['jsonl_record_uuid'] = 'frozen-public-record'
    legacy = dict(system, kind='USER')
    assert system['kind'] == 'SYSTEM'
    assert _identity_key(system) == _identity_key(legacy)
    other = dict(system, text='Unrelated system context')
    assert _identity_key(other) != _identity_key(dict(other, kind='USER'))
    async def replay():
        store = Store(':memory:'); store.start()
        try:
            await store.open_session('hosta', 'v2-child', provider=provider)
            assert await store.append_session_event('hosta:v2-child', legacy,
                        identity=_identity_key(legacy), limit=500) is not None
            assert await store.append_session_event('hosta:v2-child', system,
                        identity=_identity_key(system), limit=500) is None
            rows = await store.fetch_session_event_tail('hosta:v2-child', limit=500)
            assert len(rows) == 1 and rows[0]['kind'] == 'USER'
        finally:
            store.stop()
    asyncio.run(replay())
