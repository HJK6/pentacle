"""Retained native transcripts through pin rejection, close and reconstruction."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import satellite
from event_push import EventPush
from store import Store

HOST, OLD, TARGET = 'worker', 'a' * 40, 'b' * 40
NATIVE = '22222222-2222-4222-8222-222222222222'


def _record(text, provider):
    if provider == 'codex':
        return {'type': 'response_item', 'timestamp': '2026-10-27T01:00:00Z',
                'payload': {'id': text, 'type': 'message', 'role': 'user',
                            'content': [{'type': 'input_text', 'text': text}]}}
    return {'type': 'user', 'uuid': text, 'sessionId': NATIVE,
            'timestamp': '2026-10-27T01:00:00Z', 'message': {'role': 'user', 'content': text}}


def _append(path, record):
    with path.open('a') as f:
        f.write(json.dumps(record) + '\n')


def _producer(root, sha):
    sat = satellite.Satellite(satellite.SatelliteConfig(
        host=HOST, checkout=str(root), no_autoupdate=True, push_secret='shared',
        host_secret='host-secret', held_span_path=str(root / 'held.json')))
    sat.sha = sha
    return sat


class _Wire:
    def __init__(self, sink):
        self.sink = sink
        self.frames = []
        self.reply = None

    async def send(self, raw):
        frame = json.loads(raw)
        self.frames.append(frame)
        self.reply = await self.sink.handle_push(frame)

    async def recv(self):
        return json.dumps(self.reply)


def _sink(store, casts):
    async def broadcast(frame):
        casts.append(frame)
    async def secret():
        return 'shared'
    sink = EventPush(store, broadcast, None, recent_limit=500,
                     host_secrets={HOST: 'host-secret'})
    sink._secret = secret
    return sink


@pytest.mark.parametrize('reopen', [False, True])
@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_closed_source_reconstructs_and_drains_exact_history(tmp_path, monkeypatch, provider, reopen):
    monkeypatch.setattr(satellite, 'DEFAULT_CHAT_CHECKPOINT_PATH', str(tmp_path / 'chat.json'), raising=False)
    async def run():
        path = tmp_path / 'transcript.jsonl'
        if provider == 'codex':
            _append(path, {'type': 'session_meta', 'payload': {'id': NATIVE}})
        _append(path, _record('initial', provider))
        panes = {'seat': satellite._DiscoveredPane('seat', provider, str(path), pane_pid=7001)}
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        casts = []
        try:
            await store.put('event_push.target_sha', OLD)
            original = await store.open_session(HOST, 'seat', provider=provider, pane_pid='7001',
                                                created_at='2026-10-27T00:00:00Z')
            sat = _producer(tmp_path, OLD)
            sink = _sink(store, casts)
            sat._usage_fences = {f'{HOST}:seat': (await sink._usage_fences(HOST))[0]}
            events, hw, _ = sat._collect(panes)
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            assert ack['inserted'] == 1
            sat._apply_ack(ack, hw)
            _append(path, _record('early-buffered', provider))
            _append(path, {'type': 'padding', 'padding': 'x' * (satellite.DEFAULT_HISTORY_BYTES + 200)})
            _append(path, _record('final-buffered', provider))
            events, hw, _ = sat._collect(panes)
            await store.put('event_push.target_sha', TARGET)
            rejected = await sat._push(wire, events, hw)
            assert rejected['error'] == 'satellite_version_mismatch'
            sat._apply_ack(rejected, hw)
            await store.mark_closed(HOST, 'seat', closed_at='2026-10-27T01:01:00Z',
                                    pane_status='pane_dead', expected_generation=original['session_generation'])
            if reopen:
                await store.open_session(HOST, 'seat', provider=provider, pane_pid='7001',
                                         created_at='2026-10-27T00:00:00Z')
            before = await store.fetch_session(HOST, 'seat')
            if reopen:
                assert before['created_at'] != original['created_at']  # Store remints on reopen
        finally:
            store.stop()
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        try:
            casts.clear()
            sink = _sink(store, casts)
            sat = _producer(tmp_path, TARGET)
            events, hw, _ = sat._collect({})
            assert [e['text'] for e in events][-2:] == ['early-buffered', 'final-buffered']
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            assert ack['type'] == 'event.push.ok' and ack['inserted'] == 2, ack
            sat._apply_ack(ack, hw)
            replay = await sat._push(wire, events, hw)
            assert replay['inserted'] == 0, replay
            history = await store.fetch_session_event_tail(f'{HOST}:seat', limit=500,
                generation=original['session_generation'])
            if reopen:
                assert await store.fetch_session_event_tail(f'{HOST}:seat', limit=500) == []
            assert [e['text'] for e in history] == ['initial', 'early-buffered', 'final-buffered']
            from sessions import Sessions, VerbError
            from server import Server
            sessions = Sessions(store, tmux=None, local_host=HOST)
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, comms=None, local_host=HOST)
            request = {'stream_id': f'{HOST}:seat', 'generation': original['session_generation'],
                       '_auth_context': {'operator_authenticated': True}}
            response = await server._on_request_stream_events(request)
            consumer_events = []
            async for frame in response:
                consumer_events.extend(json.loads(frame.payload)['events'])
            assert [e['text'] for e in consumer_events] == ['initial', 'early-buffered', 'final-buffered']
            with pytest.raises(VerbError):
                await server._on_request_stream_events({**request, '_auth_context': {'token_verified': True}})
            if reopen:
                current = (await store.list_open_sessions_with_event_summary())[0]
                assert current['_bootstrap_event_seen'] == 0 and current['last_event_at'] == ''
            assert await store.fetch_session(HOST, 'seat') == before
            assert casts == []
            assert sat._collect({})[0] == []
        finally:
            store.stop()
    asyncio.run(run())


async def _interrupted(root, monkeypatch, *, max_events=500):
    monkeypatch.setattr(satellite, 'DEFAULT_CHAT_CHECKPOINT_PATH', str(root / 'chat.json'))
    path = root / 'transcript.jsonl'
    _append(path, {'type': 'session_meta', 'payload': {'id': NATIVE}})
    _append(path, _record('initial', 'codex'))
    store = Store(str(root / 'sessions.db'))
    store.start()
    casts = []
    sink = _sink(store, casts)
    await store.put('event_push.target_sha', TARGET)
    original = await store.open_session(HOST, 'seat', provider='codex', pane_pid='7001')
    sat = _producer(root, TARGET)
    sat._usage_fences = {f'{HOST}:seat': (await sink._usage_fences(HOST))[0]}
    panes = {'seat': satellite._DiscoveredPane('seat', 'codex', str(path), pane_pid=7001)}
    events, hw, _ = sat._collect(panes)
    ack = await sat._push(_Wire(sink), events, hw)
    sat._apply_ack(ack, hw)
    assert ack['chat_bindings'][0]['status'] == 'bound', ack
    _append(path, _record('buffered-one', 'codex'))
    _append(path, _record('buffered-two', 'codex'))
    sat._collect(panes)  # durable anchor before interruption, no delivery
    await store.mark_closed(HOST, 'seat', closed_at='2026-10-27T01:00:00Z',
                            pane_status='pane_dead', expected_generation=original['session_generation'])
    sat = _producer(root, TARGET)
    sat.config.max_events_per_pass = max_events
    casts.clear()
    return store, sat, sink, casts, original


@pytest.mark.parametrize('fault', ['token', 'native', 'file', 'generation', 'pid', 'offset', 'host', 'history'])
def test_recovery_refuses_only_bad_source_without_advancing_it(tmp_path, monkeypatch, fault):
    import copy
    async def run():
        store, sat, sink, casts, original = await _interrupted(tmp_path, monkeypatch)
        try:
            events, hw, _ = sat._collect({})
            wire = _Wire(sink)
            await sat._push(wire, events, hw)
            good = wire.frames[-1]
            # Build a bad first attempt against a fresh range state, retaining
            # the issued live checkpoint. The prior call established fixed EOF.
            bad = copy.deepcopy(good)
            bad['chat_recovery_pending'], bad['chat_recovery_gaps'] = 0, []
            batch = bad['chat_recovery'][0]
            if fault == 'token': batch['checkpoint_id'] = 'unissued'
            if fault == 'native': batch['native_session_id'] = 'different-native'
            if fault == 'file': batch['file_digest'] = 'f' * 64
            if fault == 'generation': batch['generation'] = 'later-generation'
            if fault == 'pid': batch['pane_pid'] = '7002'
            if fault == 'offset': bad['events'][0]['transcript_offset'] = -1
            if fault == 'host': batch['stream_id'] = 'foreign:seat'
            if fault == 'history':
                def erase_proof(conn):
                    with conn:
                        conn.execute("UPDATE v2_session_generation_history SET native_session_id=NULL WHERE generation=?",
                                     (original['session_generation'],))
                await store.submit(erase_proof)
            live = await store.open_session(HOST, 'live', provider='claude', pane_pid='8001')
            bad['events'].append({'stream_id': f'{HOST}:live', 'provider': 'claude', 'kind': 'USER',
                                  'text': 'unrelated', 'raw': {'jsonl_record_uuid': 'live-one'}})
            before = await store.fetch_session(HOST, 'seat')
            offsets = {key: tail.offset for key, tail in sat._recovering.items()}
            ack = await sink.handle_push(bad)
            assert ack['type'] == 'event.push.ok', ack
            assert ack['inserted'] == 1 and ack['dropped'], ack
            runtime = json.loads(await store.get('event_push.runtime.worker'))
            assert runtime['recovery_gaps'] or runtime['recovery_pending']
            sat._apply_ack(ack, hw)
            assert {key: tail.offset for key, tail in sat._recovering.items()} == offsets
            assert await store.fetch_session(HOST, 'seat') == before
            assert [x['event']['text'] for x in casts if x['type'] == 'chat.event'][-1:] == ['unrelated']
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['end', 'digest', 'start'])
def test_fixed_range_cannot_expand_or_change_on_retry(tmp_path, monkeypatch, change):
    import copy
    async def run():
        store, sat, sink, casts, _ = await _interrupted(tmp_path, monkeypatch, max_events=1)
        try:
            events, hw, _ = sat._collect({})
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            assert ack['chat_recovery'][0]['status'] == 'pending'
            bad = copy.deepcopy(wire.frames[-1])
            fixed = bad['chat_recovery'][0]['range']
            fixed[change] = 'f' * 64 if change == 'digest' else fixed[change] + 1
            refused = await sink.handle_push(bad)
            assert refused['inserted'] == 0 and refused['dropped'], refused
            sat._apply_ack(ack, hw)
            while True:
                events, hw, _ = sat._collect({})
                if not sat._pass_recovery:
                    break
                sat._apply_ack(await sat._push(wire, events, hw), hw)
            assert [e['text'] for e in await store.fetch_session_event_tail('worker:seat', limit=50)] == [
                'initial', 'buffered-one', 'buffered-two']
        finally:
            store.stop()
    asyncio.run(run())


def test_lost_recovery_ack_and_failed_checkpoint_replace_resend_exactly_once(tmp_path, monkeypatch):
    async def run():
        store, sat, sink, casts, _ = await _interrupted(tmp_path, monkeypatch)
        try:
            events, hw, _ = sat._collect({})
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            assert ack['inserted'] == 2
            # Drop the ACK, reconstruct, and lose persistence of the retry ACK.
            sat = _producer(tmp_path, TARGET)
            events, hw, _ = sat._collect({})
            ack = await sat._push(wire, events, hw)
            assert ack['inserted'] == 0
            original_replace = satellite.os.replace
            def fail_checkpoint(source, target):
                if Path(target) == tmp_path / 'chat.json':
                    raise OSError('injected checkpoint persistence interruption')
                return original_replace(source, target)
            with monkeypatch.context() as patch:
                patch.setattr(satellite.os, 'replace', fail_checkpoint)
                with pytest.raises(OSError):
                    sat._apply_ack(ack, hw)
            sat = _producer(tmp_path, TARGET)
            events, hw, _ = sat._collect({})
            ack = await sat._push(wire, events, hw)
            assert ack['inserted'] == 0
            sat._apply_ack(ack, hw)
            assert [e['text'] for e in await store.fetch_session_event_tail('worker:seat', limit=50)] == [
                'initial', 'buffered-one', 'buffered-two']
            assert casts == []
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['missing', 'replace', 'truncate', 'corrupt_checkpoint', 'missing_proof'])
def test_unrecoverable_sources_report_gap_and_keep_live_streams_moving(tmp_path, monkeypatch, damage):
    async def run():
        store, sat, sink, casts, _ = await _interrupted(tmp_path, monkeypatch)
        try:
            path = tmp_path / 'transcript.jsonl'
            if damage == 'missing': path.unlink()
            if damage == 'replace':
                path.rename(tmp_path / 'preimage.jsonl')
                _append(path, _record('foreign', 'codex'))
            if damage == 'truncate': path.write_text('{}\n')
            if damage == 'corrupt_checkpoint':
                (tmp_path / 'chat.json').write_text('{broken')
                sat = _producer(tmp_path, TARGET)
            if damage == 'missing_proof':
                next(iter(sat._recovering.values())).binding = {}
            live_path = tmp_path / 'live.jsonl'
            _append(live_path, _record('unrelated-live', 'claude'))
            await store.open_session(HOST, 'live', provider='claude', pane_pid='8001')
            events, hw, _ = sat._collect({'live': satellite._DiscoveredPane(
                'live', 'claude', str(live_path), pane_pid=8001)})
            assert [e['text'] for e in events] == ['unrelated-live']
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            assert ack['inserted'] == 1 and wire.frames[-1]['chat_recovery_gaps']
        finally:
            store.stop()
    asyncio.run(run())


def test_old_and_candidate_payloads_keep_exact_pin_admission_and_new_incarnation(tmp_path, monkeypatch):
    import copy
    async def run():
        store, sat, sink, casts, _ = await _interrupted(tmp_path, monkeypatch)
        try:
            events, hw, _ = sat._collect({})
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            new_frame = wire.frames[-1]
            first = json.loads(await store.get('event_push.runtime.worker'))
            assert first['incarnation'] == new_frame['satellite_incarnation']
            assert first['admitted'] is True and first['useful_events'] == 2
            # Existing installed-old event-only shape remains consumable by
            # the candidate daemon at its exact pin, without recovery authority.
            await store.open_session(HOST, 'legacy', provider='claude', pane_pid='8001')
            legacy = {'type': 'event.push', 'request_id': 1, 'push_secret': 'shared',
                      'host': HOST, 'satellite_sha': OLD, 'satellite_pid': new_frame['satellite_pid'],
                      'wire_version': 2, 'events': [{'stream_id': 'worker:legacy', 'provider': 'claude',
                      'kind': 'USER', 'text': 'installed-old', 'raw': {'jsonl_record_uuid': 'old'}}],
                      'high_water': {}}
            refused = await sink.handle_push(legacy)
            assert refused['error'] == 'satellite_version_mismatch'
            stale = json.loads(await store.get('event_push.runtime.worker'))
            assert stale['admitted'] is False and stale['useful_events'] == 0
            await store.put('event_push.target_sha', OLD)
            assert (await sink.handle_push(legacy))['inserted'] == 1
            await store.put('event_push.target_sha', TARGET)
            restarted = _producer(tmp_path, TARGET)
            assert restarted._incarnation != sat._incarnation
            assert (await restarted._push(wire, [], {}))['type'] == 'event.push.ok'
            second = json.loads(await store.get('event_push.runtime.worker'))
            assert second['pid'] == first['pid'] and second['incarnation'] != first['incarnation']
            assert second['useful_events'] == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_partial_final_record_is_never_acknowledged_or_hidden_as_green(tmp_path, monkeypatch):
    async def run():
        store, sat, sink, casts, _ = await _interrupted(tmp_path, monkeypatch)
        try:
            path = tmp_path / 'transcript.jsonl'
            with path.open('ab') as f:
                f.write(b'{"type":"response_item","payload":')
            events, hw, _ = sat._collect({})
            wire = _Wire(sink)
            ack = await sat._push(wire, events, hw)
            sat._apply_ack(ack, hw)
            tail = next(iter(sat._recovering.values()))
            assert tail.offset < path.stat().st_size and tail.partial_tail_bytes > 0
            assert wire.frames[-1]['chat_recovery_gaps'][0]['reason'] == 'incomplete_source_record'
            before = dict(tail.recovery_range)
            with path.open('ab') as f:
                f.write(b'{}}\n')
            sat._collect({})
            assert tail.recovery_range == before and tail.offset == before['end']
        finally:
            store.stop()
    asyncio.run(run())


def test_recovery_telemetry_requires_host_proof_before_runtime_write(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        try:
            sink = _sink(store, [])
            await store.put('event_push.target_sha', TARGET)
            frame = {'type': 'event.push', 'request_id': 1, 'push_secret': 'shared',
                     'host': HOST, 'satellite_sha': TARGET, 'satellite_pid': 9001,
                     'satellite_incarnation': 'claimed-new-boot', 'wire_version': 2,
                     'events': [], 'high_water': {}, 'chat_recovery_pending': 0, 'chat_recovery_gaps': []}
            ack = await sink.handle_push(frame)
            assert ack.get('error') == 'unauthorized_source_host', ack
            assert await store.get('event_push.runtime.worker') is None
        finally:
            store.stop()
    asyncio.run(run())


def test_bad_original_token_never_freezes_new_same_name_source(tmp_path, monkeypatch):
    import copy
    async def run():
        store, sat, sink, casts, original = await _interrupted(tmp_path, monkeypatch)
        try:
            new_native = '33333333-3333-4333-8333-333333333333'
            path = tmp_path / 'new-source.jsonl'
            record = _record('new-generation-live', 'claude')
            record['sessionId'] = new_native
            _append(path, record)
            await store.open_session(HOST, 'seat', provider='claude', pane_pid='7001')
            sat._usage_fences = {'worker:seat': (await sink._usage_fences(HOST))[0]}
            events, hw, _ = sat._collect({'seat': satellite._DiscoveredPane(
                'seat', 'claude', str(path), pane_pid=7001)})
            class BadTokenWire(_Wire):
                async def send(self, raw):
                    frame = json.loads(raw)
                    frame['chat_recovery'][0]['checkpoint_id'] = 'unissued'
                    await super().send(json.dumps(frame))
            wire = BadTokenWire(sink)
            ack = await sat._push(wire, events, hw)
            assert ack['inserted'] == 1 and ack['dropped']
            sat._apply_ack(ack, hw)
            assert sat._tails['seat'].source_pane_pid == '7001'
            assert sat._tails['seat'].binding['native_session_id'] == new_native
            assert [e['text'] for e in await store.fetch_session_event_tail('worker:seat', limit=50)] == [
                'new-generation-live']
        finally:
            store.stop()
    asyncio.run(run())
