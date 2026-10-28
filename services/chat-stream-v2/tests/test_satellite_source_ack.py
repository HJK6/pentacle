"""Delivery progress must not be mistaken for durable source-checkpoint progress."""
import asyncio
import json
from pathlib import Path
import runpy

import pytest

import satellite
from store import Store
from test_satellite_recovery import HOST, TARGET, NATIVE, _Wire, _sink, _record, _append


def producer(root):
    sat = satellite.Satellite(satellite.SatelliteConfig(
        host=HOST, checkout=str(root), no_autoupdate=True, push_secret='shared',
        host_secret='host-secret', held_span_path=str(root / 'held.json'),
        chat_checkpoint_path=str(root / 'chat.json')))
    sat.sha = TARGET
    return sat


def assert_7dd_load(path):
    frozen = runpy.run_path(str(Path(__file__).parent / 'fixtures/satellite_checkpoint_7dd.py'))
    loader = frozen['Loader']()
    loader.config = satellite.SatelliteConfig(host=HOST, chat_checkpoint_path=str(path))
    loader._load_chat_checkpoint()
    assert loader._chat_gap == ''
    assert loader._recovering
    for tail in loader._recovering.values():
        assert tail.offset <= tail.observed_size
        assert not tail.recovery_parent or tail.recovery_parent['end'] <= tail.offset
        assert tail.recovery_range is None or tail.recovery_range['start'] <= tail.offset <= tail.recovery_range['end']


@pytest.mark.parametrize('boundary', ['metadata_close', 'commit_then_close', 'control'])
@pytest.mark.parametrize('reconstruct', [False, True])
def test_source_ack_is_the_persisted_replay_origin(tmp_path, boundary, reconstruct):
    async def run():
        path = tmp_path / 'native.jsonl'
        _append(path, _record('initial', 'claude'))
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        try:
            await store.put('event_push.target_sha', TARGET)
            original = await store.open_session(HOST, 'seat', provider='claude', pane_pid='7001')
            casts = []
            sink = _sink(store, casts)
            wire = _Wire(sink)
            sat = producer(tmp_path)
            sat._usage_fences = {HOST + ':seat': (await sink._usage_fences(HOST))[0]}
            panes = {'seat': satellite._DiscoveredPane('seat', 'claude', str(path), pane_pid=7001)}
            events, hw, _ = sat._collect(panes)
            assert_7dd_load(tmp_path / 'chat.json')
            sat._apply_ack(await sat._push(wire, events, hw), hw)
            token = sat._tails['seat'].binding['checkpoint_id']
            initial_ack = sat._tails['seat'].offset
            if boundary == 'commit_then_close':
                _append(path, _record('committed-before-close', 'claude'))
            else:
                _append(path, {'type': 'cost-state', 'sessionId': NATIVE})
            events, hw, _ = sat._collect(panes)

            async def close():
                await store.mark_closed(HOST, 'seat', closed_at='2026-10-28T20:00:00Z', pane_status='pane_dead',
                                        expected_generation=original['session_generation'])

            bind = store.bind_chat_checkpoints
            if boundary == 'metadata_close':
                await close()
            elif boundary == 'commit_then_close':
                async def close_before_bind(host, sources):
                    history = await store.fetch_session_event_tail(HOST + ':seat', limit=100)
                    assert 'committed-before-close' in [e['text'] for e in history]
                    await close()
                    return await bind(host, sources)
                store.bind_chat_checkpoints = close_before_bind
            ack = await sat._push(wire, events, hw)
            store.bind_chat_checkpoints = bind
            sat._apply_ack(ack, hw)
            if boundary == 'control':
                assert ack['chat_bindings'][0]['status'] == 'bound'
                await close()
            else:
                assert ack['chat_bindings'][0]['status'] == 'source_identity_unproven'
            assert sat._tails['seat'].offset == hw[str(path)] > initial_ack  # delivery progresses
            durable = await store.submit(lambda c: c.execute(
                'SELECT ack_offset FROM v2_chat_checkpoints WHERE checkpoint_id=?', (token,)
            ).fetchone()[0])
            assert_7dd_load(tmp_path / 'chat.json')
            saved = json.loads((tmp_path / 'chat.json').read_text())['sources'][0]
            assert saved['offset'] == durable  # RED: generic pushOK is not a source ACK
            assert saved['offset'] <= saved['observed_size']
            _append(path, _record('late-visible-tail', 'claude'))
            active = producer(tmp_path) if reconstruct else sat
            events, hw, _ = active._collect({})
            ack = await active._push(wire, events, hw)
            assert ack['chat_recovery'][0]['status'] == 'drained', ack
            active._apply_ack(ack, hw)
            duplicate = await active._push(wire, events, hw)
            assert duplicate['inserted'] == 0
            history = await store.fetch_session_event_tail(HOST + ':seat', limit=100,
                                                          generation=original['session_generation'])
            expected = ['initial'] + (['committed-before-close'] if boundary == 'commit_then_close' else [])
            assert [e['text'] for e in history] == expected + ['late-visible-tail']
            assert (await store.fetch_session(HOST, 'seat'))['status'] == 'closed'
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['refused', 'missing_result', 'no_request', 'wrong_token', 'stale_request'])
def test_open_delivery_progress_and_later_binding_use_replay_start(tmp_path, monkeypatch, fault):
    async def run():
        path = tmp_path / 'native.jsonl'
        _append(path, _record('initial', 'claude'))
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        try:
            await store.put('event_push.target_sha', TARGET)
            original = await store.open_session(HOST, 'seat', provider='claude', pane_pid='7001')
            casts = []
            sink = _sink(store, casts)
            wire = _Wire(sink)
            sat = producer(tmp_path)
            sat.config.max_events_per_pass = 1
            sat._usage_fences = {HOST + ':seat': (await sink._usage_fences(HOST))[0]}
            panes = {'seat': satellite._DiscoveredPane('seat', 'claude', str(path), pane_pid=7001)}
            events, hw, _ = sat._collect(panes)
            assert_7dd_load(tmp_path / 'chat.json')
            sat._apply_ack(await sat._push(wire, events, hw), hw)
            initial = sat._tails['seat'].offset
            source_fn = sat._chat_source
            if fault == 'refused':
                def bad_generation(tail, end):
                    return {**source_fn(tail, end), 'generation': '0' * 32}
                monkeypatch.setattr(sat, '_chat_source', bad_generation)
            elif fault == 'no_request':
                monkeypatch.setattr(sat, '_chat_source', lambda *args: None)
            for i in range(4):
                _append(path, _record('open-' + str(i), 'claude'))
            old_delivery = initial
            for i in range(4):
                events, hw, _ = sat._collect(panes)
                assert [e['text'] for e in events] == ['open-' + str(i)]
                ack = await sat._push(wire, events, hw)
                if fault == 'refused':
                    assert ack['chat_bindings'][0]['status'] == 'source_identity_unproven'
                elif fault == 'missing_result':
                    ack['chat_bindings'] = []  # lost source receipt after real commit
                elif fault == 'wrong_token':
                    ack['chat_bindings'][0]['checkpoint_id'] = '0' * 32
                elif fault == 'stale_request':
                    ack['request_id'] -= 1
                sat._apply_ack(ack, hw)
                assert sat._tails['seat'].offset > old_delivery
                old_delivery = sat._tails['seat'].offset
                assert_7dd_load(tmp_path / 'chat.json')
                saved = json.loads((tmp_path / 'chat.json').read_text())['sources'][0]
                assert saved['offset'] == initial <= saved['observed_size']
            monkeypatch.setattr(sat, '_chat_source', source_fn)
            events, hw, _ = sat._collect(panes)
            assert events == []
            ack = await sat._push(wire, events, hw)
            assert wire.frames[-1]['chat_sources'][0]['start_offset'] == initial
            assert ack['chat_bindings'][0]['status'] == 'bound'
            sat._apply_ack(ack, hw)
            assert_7dd_load(tmp_path / 'chat.json')
            saved = json.loads((tmp_path / 'chat.json').read_text())['sources'][0]
            assert saved['offset'] == old_delivery
            assert saved['offset'] <= saved['observed_size']
            await store.mark_closed(HOST, 'seat', closed_at='2026-10-28T20:00:00Z',
                                    pane_status='pane_dead', expected_generation=original['session_generation'])
            _append(path, _record('late-visible-tail', 'claude'))
            active = producer(tmp_path)
            events, hw, _ = active._collect({})
            ack = await active._push(wire, events, hw)
            assert ack['chat_recovery'][0]['status'] == 'drained'
            active._apply_ack(ack, hw)
            history = await store.fetch_session_event_tail(HOST + ':seat', limit=100,
                                                          generation=original['session_generation'])
            assert [e['text'] for e in history] == ['initial', 'open-0', 'open-1', 'open-2', 'open-3',
                                                   'late-visible-tail']
        finally:
            store.stop()
    asyncio.run(run())


def test_readoption_and_same_path_rebind_keep_original_replay_cursor(tmp_path):
    async def run():
        path = tmp_path / 'native.jsonl'
        _append(path, _record('initial', 'claude'))
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        try:
            await store.put('event_push.target_sha', TARGET)
            await store.open_session(HOST, 'seat', provider='claude', pane_pid='7001')
            sink = _sink(store, [])
            fences = {HOST + ':seat': (await sink._usage_fences(HOST))[0]}
            panes = {'seat': satellite._DiscoveredPane('seat', 'claude', str(path), pane_pid=7001)}
            sat = producer(tmp_path)
            sat._usage_fences = fences
            events, hw, _ = sat._collect(panes)
            assert_7dd_load(tmp_path / 'chat.json')
            sat._apply_ack(await sat._push(_Wire(sink), events, hw), hw)
            token = sat._tails['seat'].binding['checkpoint_id']
            # Actual checkpoint reconstruction and recovery receipt precede re-adoption.
            sat = producer(tmp_path)
            sat._usage_fences = fences
            events, hw, _ = sat._collect(panes)
            assert_7dd_load(tmp_path / 'chat.json')
            sat._apply_ack(await sat._push(_Wire(sink), events, hw), hw)
            parent_end = sat._recovering[token].recovery_parent['end']
            sat._collect(panes)
            assert sat._replay_offset(sat._tails['seat']) == parent_end
            _append(path, {'type': 'cost-state', 'sessionId': NATIVE})
            real_source = sat._chat_source
            sat._chat_source = lambda *args: None
            events, hw, _ = sat._collect(panes)
            assert_7dd_load(tmp_path / 'chat.json')
            sat._apply_ack(await sat._push(_Wire(sink), events, hw), hw)
            delivery = sat._tails['seat'].offset
            assert delivery > parent_end
            assert_7dd_load(tmp_path / 'chat.json')
            sat._chat_source = real_source
            # A fresh observed generation retires the old object before making
            # a new tail; no daemon authority is claimed for this fake fence.
            sat._usage_fences = {HOST + ':seat': {**fences[HOST + ':seat'], 'session_generation': 'f' * 32}}
            sat._collect(panes)
            old = sat._recovering[token]
            assert old.offset == parent_end < delivery
            assert sat._tails['seat'] is not old
            assert not sat._tails['seat'].binding
            assert sat._tails['seat'].offset == delivery
            assert_7dd_load(tmp_path / 'chat.json')
        finally:
            store.stop()
    asyncio.run(run())


def test_fallback_resend_uses_immutable_actual_request_snapshot(tmp_path):
    async def run():
        path = tmp_path / 'native.jsonl'
        _append(path, _record('initial', 'claude'))
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        try:
            await store.put('event_push.target_sha', TARGET)
            await store.open_session(HOST, 'seat', provider='claude', pane_pid='7001')
            sat = producer(tmp_path)
            sink = _sink(store, [])
            sat._usage_fences = {HOST + ':seat': (await sink._usage_fences(HOST))[0]}
            panes = {'seat': satellite._DiscoveredPane('seat', 'claude', str(path), pane_pid=7001)}
            events, hw, _ = sat._collect(panes)
            class LegacyRefusal(_Wire):
                async def send(self, raw):
                    if not self.frames:
                        frame = json.loads(raw)
                        self.frames.append(frame)
                        sat._frame_had_unfenced = True
                        self.reply = {'type': 'event.push.error', 'request_id': frame['request_id'],
                                      'error': 'legacy_usage_unsupported'}
                    else:
                        await super().send(raw)
            wire = LegacyRefusal(sink)
            ack = await sat._push_pass(wire, events, hw)
            assert len(wire.frames) == 2
            assert wire.frames[0]['request_id'] != wire.frames[1]['request_id'] == ack['request_id']
            assert wire.frames[0]['chat_sources'] == wire.frames[1]['chat_sources']
            sent_end = wire.frames[1]['chat_sources'][0]['next_offset']
            sat._pass_sources[0]['next_offset'] += 1000  # newer mutable collection cannot alter in-flight proof
            sat._apply_ack(ack, hw)
            assert sat._replay_offset(sat._tails['seat']) == sent_end
            assert_7dd_load(tmp_path / 'chat.json')
        finally:
            store.stop()
    asyncio.run(run())
