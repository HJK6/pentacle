"""First-span usage undercount (spec_pentacle__usage_codex_rollup_and_calibration_2026_10 AC10).

A short seat's whole transcript is consumed by the satellite before its first usage fence arrives
(fences ride host.stats acks every 30 s). The journey runs a real Satellite against a real Store
through EventPush over a loopback socket: events land on the first push, the fence arrives on the
next host.stats ack, and the seat's tokens must then reach the ledger exactly once.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from event_push import EventPush
import satellite
from satellite import Satellite, SatelliteConfig, _DiscoveredPane
from store import Store
from store_usage import USAGE_CLOSE_GRACE_S, usage_row_admissible
from v2_runtime import iso_now
from test_usage_provenance import (HOST, HOST_SECRET, SATELLITE_SHA, _Alerts, _claude_assistant, _credential,
                                   _secret)

NAME = 'v2-short'
STREAM = f'{HOST}:{NAME}'
PANE = 11


class _Ws:
    """Loopback socket routing each frame to the daemon handler for its type."""

    def __init__(self, ep: EventPush) -> None:
        self.ep = ep
        self.frames: list[dict] = []
        self._replies: list[str] = []

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.frames.append(frame)
        handler = self.ep.handle_host_stats if frame['type'] == 'host.stats' else self.ep.handle_push
        self._replies.append(json.dumps(await handler(frame)))

    async def recv(self) -> str:
        return self._replies.pop(0)


async def _noop_stats(_host: str, _stats: dict) -> None:
    return None


def _daemon(tmp_path: Path) -> tuple[Store, EventPush, _Ws, Path]:
    db = tmp_path / 'sessions.db'
    store = Store(str(db))
    store.start()
    ep = EventPush(store, lambda _frame: asyncio.sleep(0), _Alerts(), recent_limit=20,
                   host_secrets={HOST: HOST_SECRET}, host_stats_handler=_noop_stats)
    ep._secret = _secret
    return store, ep, _Ws(ep), db


def _satellite(tmp_path: Path) -> Satellite:
    sat = Satellite(SatelliteConfig(host=HOST, checkout=str(tmp_path), host_secret=HOST_SECRET,
                                    push_secret='secret'))
    sat.sha = SATELLITE_SHA
    return sat


async def _pass(sat: Satellite, ws: _Ws, path: Path) -> dict:
    events, high_water, _capped = sat._collect({NAME: _DiscoveredPane(NAME, 'claude', str(path), pane_pid=PANE)})
    ack = await sat._push(ws, events, high_water)
    sat._apply_ack(ack, high_water)
    return ack


def _reply(mid: str, *, output: int) -> dict:
    """A real assistant reply: text content (a chat event) plus usage."""
    record = _claude_assistant(mid, output=output)
    record['message'].update(role='assistant', type='message',
                             content=[{'type': 'text', 'text': 'done in one reply'}])
    return record


def _ledger(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return sorted(conn.execute('SELECT record_key, tokens FROM v2_usage_records'))


def test_ac10_short_seat_first_span_before_fence_is_accounted_once(tmp_path: Path) -> None:
    async def run() -> None:
        store, _ep, ws, db = _daemon(tmp_path)
        try:
            await _open(store)
            path = tmp_path / 'native-claude.jsonl'
            path.write_text(''.join(json.dumps(r) + '\n' for r in [
                _credential('00000000-0000-4000-8000-00000000000a'),
                _reply('m1', output=7)]))
            sat = _satellite(tmp_path)
            # The seat answers before the satellite holds a fence for it: events land, usage cannot yet.
            first = await _pass(sat, ws, path)
            assert first['type'] == 'event.push.ok' and 'usage' not in ws.frames[-1]
            assert ws.frames[-1]['events'] and _ledger(db) == []
            # The next host.stats ack carries the fence; the next pass must offer the held span.
            stats = await sat._send_host_stats(ws)
            assert [f['stream_id'] for f in stats['usage_fences']] == [STREAM]
            second = await _pass(sat, ws, path)
            assert second.get('usage_recorded') == 1
            ledger = _ledger(db)
            assert [key for key, _ in ledger] == ['m1'] and json.loads(ledger[0][1])['output'] == 7
            usage = (await store.fetch_session(HOST, NAME))['usage']
            assert usage['tokens']['output'] == 7
            # Exactly once: no duplicate events, and later passes offer nothing more.
            assert ws.frames[-1]['events'] == []  # the held span is usage-only; events are never re-sent
            third = await _pass(sat, ws, path)
            assert 'usage' not in ws.frames[-1] and third.get('usage_recorded', 0) == 0
            assert _ledger(db) == ledger
        finally:
            store.stop()

    asyncio.run(run())


BINDING = {'executable': '/usr/bin/claude', 'pane_pid': str(PANE), 'pane_started_at': 'start-a'}


async def _open(store: Store) -> None:
    await store.open_session(HOST, NAME, provider='claude', pane_pid=str(PANE), session_generation='gen-a',
                             observer_binding=BINDING)


def test_ac10_held_span_merges_with_a_later_reply(tmp_path: Path) -> None:
    async def run() -> None:
        store, _ep, ws, db = _daemon(tmp_path)
        try:
            await _open(store)
            path = tmp_path / 'native-claude.jsonl'
            path.write_text(json.dumps(_reply('m1', output=7)) + '\n')
            sat = _satellite(tmp_path)
            await _pass(sat, ws, path)
            assert sat._awaiting_matching_fence()  # the session loop fetches fences early (EARLY_FENCE_MIN_S)
            await sat._send_host_stats(ws)
            assert not sat._awaiting_matching_fence()
            with path.open('a') as fh:  # a second reply lands before the next pass
                fh.write(json.dumps(_reply('m2', output=3)) + '\n')
            ack = await _pass(sat, ws, path)
            assert ack.get('usage_recorded') == 1
            assert [key for key, _ in _ledger(db)] == ['m1', 'm2']
            assert (await store.fetch_session(HOST, NAME))['usage']['tokens']['output'] == 10
        finally:
            store.stop()

    asyncio.run(run())


def test_ac10_seat_closed_before_any_fence_is_accounted_once(tmp_path: Path) -> None:
    """The seat answers and closes before the satellite ever holds its fence."""
    async def run() -> None:
        store, _ep, ws, db = _daemon(tmp_path)
        try:
            await _open(store)
            path = tmp_path / 'native-claude.jsonl'
            path.write_text(json.dumps(_reply('m1', output=7)) + '\n')
            sat = _satellite(tmp_path)
            await _pass(sat, ws, path)
            assert ws.frames[-1]['events'] and 'usage' not in ws.frames[-1]
            await store.mark_closed(HOST, NAME, closed_at=iso_now(), pane_status='pane_dead')
            events, high_water, _ = sat._collect({})  # the seat left tmux: its tail is forgotten
            assert events == [] and NAME not in sat._tails
            stats = await sat._send_host_stats(ws)  # the just-closed row keeps its fence for the grace window
            assert [f['stream_id'] for f in stats['usage_fences']] == [STREAM]
            events, high_water, _ = sat._collect({})
            ack = await sat._push(ws, events, high_water)
            sat._apply_ack(ack, high_water)
            assert ack.get('usage_recorded') == 1 and ws.frames[-1]['events'] == []
            assert [key for key, _ in _ledger(db)] == ['m1']
            assert sat._unfenced_usage == {} and not sat._awaiting_matching_fence()
            ack = await sat._push(ws, *sat._collect({})[:2])  # nothing further is offered
            assert 'usage' not in ws.frames[-1]
        finally:
            store.stop()

    asyncio.run(run())


def test_ac10_stale_pane_fence_holds_until_the_matching_fence(tmp_path: Path) -> None:
    async def run() -> None:
        store, _ep, ws, db = _daemon(tmp_path)
        try:
            await _open(store)
            path = tmp_path / 'native-claude.jsonl'
            path.write_text(json.dumps(_reply('m1', output=7)) + '\n')
            sat = _satellite(tmp_path)
            sat._usage_fences = {STREAM: {'stream_id': STREAM, 'session_generation': 'gen-old',
                                          'provider': 'claude', 'source_pane_pid': '99'}}
            await _pass(sat, ws, path)
            assert 'usage' not in ws.frames[-1] and sat._awaiting_matching_fence()
            await sat._send_host_stats(ws)
            ack = await _pass(sat, ws, path)
            assert ack.get('usage_recorded') == 1 and [key for key, _ in _ledger(db)] == ['m1']
        finally:
            store.stop()

    asyncio.run(run())


def test_ac10_held_span_after_close_expires_and_rebind_drops(tmp_path: Path, monkeypatch) -> None:
    sat = _satellite(tmp_path)
    path = tmp_path / 'native-claude.jsonl'
    path.write_text(json.dumps(_reply('x1', output=1)) + '\n')
    sat._collect({NAME: _DiscoveredPane(NAME, 'claude', str(path), pane_pid=PANE)})
    assert STREAM in sat._unfenced_usage
    other = tmp_path / 'other.jsonl'  # the tail rebinds to another transcript: the held span is dropped
    other.write_text(json.dumps({**_reply('y1', output=1), 'sessionId': 'native-other'}) + '\n')
    sat._collect({NAME: _DiscoveredPane(NAME, 'claude', str(other), pane_pid=PANE)})
    assert [r['message']['id'] for r in sat._unfenced_usage[STREAM]['records']] == ['y1']
    sat._collect({})  # closed: kept for the grace fence
    assert STREAM in sat._unfenced_usage
    clock = satellite.time.monotonic() + satellite.HELD_USAGE_TTL_S + 1
    monkeypatch.setattr(satellite.time, 'monotonic', lambda: clock)
    sat._collect({})
    assert sat._unfenced_usage == {}


def test_ac10_daemon_grace_window_bounds_closed_row_admission() -> None:
    row = {'status': 'closed', 'closed_at': '2026-10-07T14:00:00Z'}
    at = 1_791_381_600.0  # 2026-10-07T14:00:00Z
    assert usage_row_admissible(row, close_grace_s=USAGE_CLOSE_GRACE_S, now=at + USAGE_CLOSE_GRACE_S)
    assert not usage_row_admissible(row, close_grace_s=USAGE_CLOSE_GRACE_S, now=at + USAGE_CLOSE_GRACE_S + 1)
    assert not usage_row_admissible(row, now=at)  # local ingest: open rows only
    assert usage_row_admissible({'status': 'open'})
