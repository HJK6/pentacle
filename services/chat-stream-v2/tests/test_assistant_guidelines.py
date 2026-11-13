import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import assistant_guidelines as ag
from front_desk_digest import WAKE_TOKEN
from test_peer_message_provenance import RecordingClaude, _comms, HOST, NAME, STREAM_ID, PEER, PEER_AUTH


class Store:
    def __init__(self):
        self.kv = {}
        self.queued = {}
    async def get(self, key):
        return self.kv.get(key)
    async def put(self, key, value):
        self.kv[key] = value
    async def enqueue_composite_tell(self, **msg):
        self.queued[msg['tell_id']] = msg


class Composite:
    config = SimpleNamespace(name='primary')
    def __init__(self):
        self.current = {'stream_id': 'host:desk', 'generation': 'g1'}
    async def binding(self):
        return self.current


class Rulings:
    def __init__(self, stream='host:advisor'):
        self.current = {'stream_id': stream, 'generation': 'a1', 'state': 'ready'}
    async def binding(self):
        return self.current


class RecordingComms:
    def __init__(self):
        self.sent = []
    async def tell(self, msg):
        self.sent.append(msg)
        return {'delivery_status': 'delivered'}


def files(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_ORCH_MEMORY_REPO', str(tmp_path))
    (tmp_path / 'agents').mkdir()
    (tmp_path / 'docs/reference').mkdir(parents=True)
    (tmp_path / 'agents/assistant_baseline.md').write_text('---\ntitle: one\n---\n\nfirst\n')
    (tmp_path / 'docs/reference/assistant_context.md').write_text('second\n')
    (tmp_path / 'agents/assistant_authority_baseline.md').write_text('advisor one\n')


def test_identity_ignores_frontmatter_and_refuses_escape(tmp_path, monkeypatch, caplog):
    files(tmp_path, monkeypatch)
    assert ag.DEFAULT_FRONT_DESK == ('agents/assistant_baseline.md',)
    configured = ('agents/assistant_baseline.md', 'docs/reference/assistant_context.md')
    first = ag.read_set(configured)
    (tmp_path / 'agents/assistant_baseline.md').write_text('---\ntitle: changed\n---\n\nfirst\n')
    assert ag.read_set(configured) == first
    (tmp_path / 'agents/assistant_baseline.md').write_text('first changed\n')
    assert ag.read_set(configured)[0] != first[0]
    (tmp_path / 'agents/assistant_baseline.md').write_text('first\n')
    (tmp_path / 'docs/reference/assistant_context.md').write_text('changed\n')
    assert ag.read_set(configured)[0] != first[0]
    assert ag.read_set(('../outside',)) is None
    assert '../outside' in caplog.text


def test_front_desk_triggers_rate_limit_missing_and_switch(tmp_path, monkeypatch, caplog):
    files(tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(ag.time, 'time', lambda: now[0])
    async def go():
        store, comp, comms = Store(), Composite(), RecordingComms()
        service = ag.AssistantGuidelines(store, comp, Rulings(), comms, env={
            'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on',
            'PENTACLE_ASSISTANT_GUIDELINES_FRONT_DESK_FILES': os.pathsep.join(
                ('agents/assistant_baseline.md', 'docs/reference/assistant_context.md'))})
        await service.front_desk()
        assert len(comms.sent) == 1
        assert comms.sent[0]['text'].startswith('[role baseline: assistant]\nguidelines ')
        assert comms.sent[0]['_front_desk_wake_token'] is WAKE_TOKEN
        await service.front_desk(stream='host:desk', generation='g1', previous=200000, current=150000)
        assert len(comms.sent) == 1
        await service.front_desk(stream='host:desk', generation='g1', previous=200000, current=60000)
        assert len(comms.sent) == 1
        now[0] += 600
        await service.front_desk(stream='host:desk', generation='g1', previous=60000, current=59000)
        assert len(comms.sent) == 2
        (tmp_path / 'docs/reference/assistant_context.md').write_text('new content')
        now[0] += 600
        await service.front_desk(stream='host:desk', generation='g1', previous=59000, current=58000)
        assert len(comms.sent) == 3 and 'new content' in comms.sent[-1]['text']
        assert not [key for key in store.kv if key.startswith('assistant.guidelines.delivery.')]
        (tmp_path / 'agents/assistant_baseline.md').unlink()
        comp.current = {'stream_id': 'host:desk2', 'generation': 'g2'}
        await service.front_desk()
        assert len(comms.sent) == 3 and 'agents/assistant_baseline.md' in caplog.text
        off = ag.AssistantGuidelines(store, comp, Rulings(), comms, env={})
        await off.front_desk()
        assert await off.advisor_for_target('host:advisor') is None
        assert len(comms.sent) == 3
    asyncio.run(go())


def test_unbound_queues_one_trigger(tmp_path, monkeypatch):
    files(tmp_path, monkeypatch)
    async def go():
        store, comp = Store(), Composite()
        comp.current = {'stream_id': '', 'generation': ''}
        service = ag.AssistantGuidelines(store, comp, Rulings(), RecordingComms(), env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
        await service.front_desk()
        await service.front_desk()
        assert len(store.queued) == 1
        assert next(iter(store.queued.values()))['body'] == 'guidelines:refresh'
    asyncio.run(go())


def test_advisor_tells_are_immediately_preceded_with_fresh_guidelines(tmp_path, monkeypatch):
    files(tmp_path, monkeypatch)
    async def go():
        tmux = RecordingClaude()
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider='claude')
            binding = Rulings(STREAM_ID)
            binding.current['generation'] = (await store.fetch_session(HOST, NAME))['session_generation']
            service = ag.AssistantGuidelines(store, Composite(), binding, comms, env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
            comms.guidelines = service
            await comms.tell({'to_stream_id': STREAM_ID, 'message': 'first', 'tell_id': 'p1', 'from_stream_id': PEER, '_auth_context': PEER_AUTH})
            (tmp_path / 'agents/assistant_authority_baseline.md').write_text('advisor two\n')
            await comms.tell({'to_stream_id': STREAM_ID, 'message': 'second', 'tell_id': 'p2', 'from_stream_id': PEER, '_auth_context': PEER_AUTH})
            assert len(tmux.pastes) == 4
            assert tmux.pastes[0].startswith('[role baseline: assistant_authority]\nguidelines ') and 'advisor one' in tmux.pastes[0]
            assert tmux.pastes[1].endswith('first')
            assert 'advisor two' in tmux.pastes[2] and tmux.pastes[3].endswith('second')
            assert (await service.inspect(STREAM_ID))['matches_current']
            (tmp_path / 'agents/assistant_authority_baseline.md').unlink()
            await comms.tell({'to_stream_id': STREAM_ID, 'message': 'third', 'tell_id': 'p3', 'from_stream_id': PEER, '_auth_context': PEER_AUTH})
            assert len(tmux.pastes) == 5 and tmux.pastes[-1].endswith('third')
        finally:
            store.stop()
    asyncio.run(go())


def test_context_observer_supplies_after_observed_compaction(tmp_path, monkeypatch):
    from context_adapters import ContextReading
    from routing_integrity import RoutingIntegrity
    from test_context_nudges import _context_harness
    from test_nudges import HOST as CONTEXT_HOST, _iso, _new_store
    import time
    files(tmp_path, monkeypatch)
    async def go():
        store = _new_store()
        try:
            h = await _context_harness(store)
            seat = await store.fetch_session(CONTEXT_HOST, 'child')
            comp, comms = Composite(), RecordingComms()
            comp.current = {'stream_id': f'{CONTEXT_HOST}:child', 'generation': seat['session_generation']}
            service = ag.AssistantGuidelines(store, comp, Rulings(), comms, env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
            observer = RoutingIntegrity(store, h.sessions, guidelines=service)
            now = time.time()
            await observer.observe_context(CONTEXT_HOST, 'child', provider='claude',
                reading=ContextReading(200000, model='claude-fable-5-1'), observed_at=_iso(now))
            assert len(comms.sent) == 1
            record = json.loads(await store.get('assistant.guidelines.front_desk'))
            record['delivered_at'] -= 601
            await store.put('assistant.guidelines.front_desk', json.dumps(record))
            await observer.observe_context(CONTEXT_HOST, 'child', provider='claude',
                reading=ContextReading(60000, model='claude-fable-5-1'), observed_at=_iso(now + 1))
            assert len(comms.sent) == 2
            inspect = await service.inspect(comp.current['stream_id'])
            assert inspect['identity'] and inspect['delivered_at'] and inspect['matches_current']
        finally:
            store.stop()
    asyncio.run(go())


def test_ruling_notice_and_other_seat_delivery(tmp_path, monkeypatch):
    files(tmp_path, monkeypatch)
    async def go():
        tmux = RecordingClaude()
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider='claude')
            binding = Rulings(STREAM_ID)
            binding.current['generation'] = (await store.fetch_session(HOST, NAME))['session_generation']
            service = ag.AssistantGuidelines(store, Composite(), binding, comms,
                env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
            comms.guidelines = service
            await comms.deliver_outbound_notice({'to_stream_id': STREAM_ID, 'stream_id': STREAM_ID,
                'message': '[Assistant lane ruling request]\n{}', 'tell_id': 'ruling-1',
                '_outbound_notice_kind': 'assistant_lane_ruling_request'})
            assert len(tmux.pastes) == 2 and tmux.pastes[0].startswith('[role baseline: assistant_authority]')
            assert '[Assistant lane ruling request]' in tmux.pastes[1]
            service.rulings.current['stream_id'] = 'elsewhere:advisor'
            await comms.tell({'to_stream_id': STREAM_ID, 'message': 'other', 'tell_id': 'p-other',
                              'from_stream_id': PEER, '_auth_context': PEER_AUTH})
            assert len(tmux.pastes) == 3 and tmux.pastes[-1].endswith('other')
        finally:
            store.stop()
    asyncio.run(go())


def test_guidelines_tell_bypasses_real_digest(tmp_path, monkeypatch):
    from test_front_desk_digest import _desk
    files(tmp_path, monkeypatch)
    async def go():
        tmux = RecordingClaude()
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider='claude')
            seat = await store.fetch_session(HOST, NAME)
            desk = _desk(store, STREAM_ID, seat['session_generation'])
            comms.assistant_ingress_policy = desk.suppress_routine_backend_ingress
            comp = Composite()
            comp.current = {'stream_id': STREAM_ID, 'generation': seat['session_generation']}
            service = ag.AssistantGuidelines(store, comp, Rulings(), comms,
                env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
            comms.guidelines = service
            held = await comms.tell({'to_stream_id': STREAM_ID, 'text': 'routine', 'tell_id': 'routine'})
            assert held['delivery_status'] == 'persisted'
            await service.front_desk()
            assert len(tmux.pastes) == 1 and tmux.pastes[0].startswith('[role baseline: assistant]')
            assert len(await desk.front_desk_digest._rows(STREAM_ID)) == 1
        finally:
            store.stop()
    asyncio.run(go())


def test_binding_transition_delivers_once_per_generation(tmp_path, monkeypatch):
    from test_assistant_composite_addressing import _run, SEAT_A, SEAT_B
    files(tmp_path, monkeypatch)
    async def body(h):
        service = ag.AssistantGuidelines(h.store, h.composite, Rulings(), h.comms,
            env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
        h.composite.guidelines = service
        h.comms.guidelines = service
        await h.composite.load_binding()
        await h.composite.load_binding()
        assert h.inputs_at(SEAT_A) == 1
        await h.move_to_b()
        assert h.inputs_at(SEAT_B) == 1
        await h.composite.load_binding()
        assert h.inputs_at(SEAT_B) == 1
        assert (await service.inspect(SEAT_B))['matches_current']
    _run(tmp_path, body, digest=True)


def test_missing_file_during_compaction_retries_when_file_returns(tmp_path, monkeypatch):
    files(tmp_path, monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(ag.time, 'time', lambda: now[0])
    async def go():
        store, comp, comms = Store(), Composite(), RecordingComms()
        service = ag.AssistantGuidelines(store, comp, Rulings(), comms,
            env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
        await service.front_desk()
        path = tmp_path / 'agents/assistant_baseline.md'
        path.unlink()
        now[0] += 601
        await service.front_desk(stream='host:desk', generation='g1', previous=200000, current=60000)
        assert len(comms.sent) == 1
        path.write_text('first\n')
        await service.front_desk(stream='host:desk', generation='g1', previous=60000, current=59000)
        assert len(comms.sent) == 2
    asyncio.run(go())


def test_unconfirmed_guidelines_are_not_claimed_or_repeated(tmp_path, monkeypatch):
    files(tmp_path, monkeypatch)
    async def go():
        class PendingStore(Store):
            confirmed = False
            async def get_tell_delivery(self, tell_id):
                return {'payload_digest': 'digest', 'reply': {'delivery_status': 'delivered' if self.confirmed else 'committed_pending_proof'}}
            async def promote_tell_delivery(self, tell_id, digest, reconcile):
                return await self.get_tell_delivery(tell_id)
        class PendingComms(RecordingComms):
            async def tell(self, msg):
                self.sent.append(msg)
                return {'delivery_status': 'committed_pending_proof'}
        store, comp, comms = PendingStore(), Composite(), PendingComms()
        service = ag.AssistantGuidelines(store, comp, Rulings(), comms,
            env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
        await service.front_desk()
        before = dict(store.kv)
        assert (await service.inspect('host:desk'))['identity'] is None
        assert store.kv == before
        await service.front_desk(stream='host:desk', generation='g1', previous=200000, current=60000)
        assert len(comms.sent) == 1
        store.confirmed = True
        info = await service.inspect('host:desk')
        assert info['identity'] and info['matches_current']
        await service.front_desk(stream='host:desk', generation='g1', previous=60000, current=59000)
        assert len(comms.sent) == 1
        assert json.loads(store.kv['assistant.guidelines.front_desk'])['pending']
    asyncio.run(go())


def test_failed_queued_refresh_does_not_strand_other_tells(tmp_path):
    from test_assistant_composite_addressing import _run, SEAT_A, SENDER
    async def body(h):
        class UnavailableGuidelines:
            enabled = True
            async def front_desk(self):
                return False
        h.server.guidelines = UnavailableGuidelines()
        await h.store.enqueue_composite_tell(name=h.composite.config.name, tell_id='guidelines:pending:test',
            from_stream_id='daemon:guidelines', body='guidelines:refresh', request_id='')
        await h.store.enqueue_composite_tell(name=h.composite.config.name, tell_id='ordinary-queued',
            from_stream_id=SENDER, body='ordinary queued tell', request_id='')
        await h.server._flush_composite_tells(h.composite)
        assert await h.store.claim_composite_tells(name=h.composite.config.name) == []
        assert h.inputs_at(SEAT_A) == 1
    _run(tmp_path, body)


def test_guidelines_delivery_refuses_stale_binding(tmp_path, monkeypatch):
    from sessions import VerbError
    files(tmp_path, monkeypatch)
    async def go():
        tmux = RecordingClaude()
        comms, store, sessions = _comms(tmux)
        try:
            await sessions.open(HOST, NAME, provider='claude')
            seat = await store.fetch_session(HOST, NAME)
            comp = Composite()
            comp.current = {'stream_id': STREAM_ID, 'generation': seat['session_generation']}
            service = ag.AssistantGuidelines(store, comp, Rulings(), comms,
                env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
            comms.guidelines = service
            comp.current = {'stream_id': 'elsewhere:desk', 'generation': 'new'}
            with pytest.raises(VerbError, match='guidelines target binding changed'):
                await comms.tell({'to_stream_id': STREAM_ID, 'text': 'stale', 'tell_id': 'stale',
                    '_guidelines_token': ag.DELIVERY_TOKEN, '_guidelines_role': 'front_desk',
                    '_guidelines_generation': seat['session_generation']})
            assert tmux.pastes == []
        finally:
            store.stop()
    asyncio.run(go())


def test_off_inspect_json_matches_base_82dc9599():
    from unittest.mock import AsyncMock
    from server import Server, SEND_FRAMES_NOT_IMPLEMENTED
    async def go():
        server = object.__new__(Server)
        session = {'status': 'closed', 'role': 'worker', 'session_generation': 'generation'}
        server.sessions = SimpleNamespace(resolve=AsyncMock(return_value=('host', 'seat')),
                                          get=lambda _stream: dict(session))
        server.store = SimpleNamespace(fetch_session_event_tail=AsyncMock(return_value=[]),
            find_report=AsyncMock(return_value=None), fetch_role_source=AsyncMock(return_value=None),
            latest_close_audit=AsyncMock(return_value=None), get_deferred_reap=AsyncMock(return_value=None),
            usage_provenance_summary=AsyncMock(return_value=None))
        server._composite_for = lambda _stream: None
        server.lane_rulings = None
        server.authority_restore = None
        server.assistant_composite = None
        baseline = {'type': 'inspect_stream.ok', 'stream_id': 'host:seat',
            'session': {**session, 'role_source': 'spawn'}, 'recent_events': [],
            'send_frames': dict(SEND_FRAMES_NOT_IMPLEMENTED), 'existing_report': None,
            'close_audit': None, 'lane_ruling': None, 'restore': None,
            'deferred_reap': None, 'usage_provenance': None}
        for enabled in (None, False):
            if enabled is None:
                if hasattr(server, 'guidelines'):
                    del server.guidelines
            else:
                server.guidelines = SimpleNamespace(enabled=False, inspect=AsyncMock())
            actual = await server._on_inspect_stream({'stream_id': 'host:seat', 'event_tail': 0})
            assert json.dumps(actual, sort_keys=True) == json.dumps(baseline, sort_keys=True)
        server.guidelines = SimpleNamespace(enabled=True, inspect=AsyncMock(return_value={'seat': 'advisor'}))
        enabled = await server._on_inspect_stream({'stream_id': 'host:seat', 'event_tail': 0})
        assert enabled['assistant_guidelines'] == {'seat': 'advisor'}
        assert {k: v for k, v in enabled.items() if k != 'assistant_guidelines'} == baseline
    asyncio.run(go())


def test_delivery_bookkeeping_stays_bounded_for_many_advisor_requests():
    async def go():
        store = Store()
        service = ag.AssistantGuidelines(store, Composite(), Rulings(), RecordingComms(),
            env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
        for index in range(1000):
            await service.advisor_record('host:advisor', 'a1', f'{index:064x}', f'tell-{index}', 'delivered')
        keys = {key for key in store.kv if key.startswith('assistant.guidelines.')}
        assert keys == {'assistant.guidelines.advisor'}
        record = json.loads(store.kv['assistant.guidelines.advisor'])
        assert record['tell_id'] == 'tell-999' and record['identity'] == f'{999:064x}'
        await service.advisor_record('host:advisor', 'a1', 'pending-hash', 'pending-tell', 'committed_pending_proof')
        record = json.loads(store.kv['assistant.guidelines.advisor'])
        assert record['tell_id'] == 'pending-tell' and record['previous_delivery']['identity'] == f'{999:064x}'
        assert {key for key in store.kv if key.startswith('assistant.guidelines.')} == keys
        from store import Store as SQLiteStore
        sqlite_store = SQLiteStore(':memory:')
        sqlite_store.start()
        try:
            real = ag.AssistantGuidelines(sqlite_store, Composite(), Rulings(), RecordingComms(),
                env={'PENTACLE_ASSISTANT_GUIDELINES_ENABLED': 'on'})
            for index in range(100):
                await real.advisor_record('host:advisor', 'a1', str(index), f'sqlite-{index}', 'delivered')
            stored_keys = await sqlite_store.submit(lambda conn: [r[0] for r in conn.execute(
                "SELECT k FROM kv WHERE k LIKE 'assistant.guidelines.%'")])
            assert stored_keys == ['assistant.guidelines.advisor']
            assert json.loads(await sqlite_store.get('assistant.guidelines.advisor'))['tell_id'] == 'sqlite-99'
        finally:
            sqlite_store.stop()
    asyncio.run(go())
