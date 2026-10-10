"""Portable progress/handoff contract. The sender still owns fleet protection."""
from __future__ import annotations
import asyncio
import copy
import json
import os
from pathlib import Path
import queue
import resource
import statistics
import threading
import time
import uuid

import pytest

from alerts import Alerts
from error_adapters import ErrorFact, adapt
from loop_watchdog import (LoopWatchdog, RequestTiming, StoreProgress, cause_at, episode_id,
                           read_json, request_timing, validate_handoff, validate_snapshot, write_json)
from store import Store
from test_error_alerts import subject  # noqa: F401


def progress(now=10.0):
    return {'version': 'daemon-progress.v1', 'instance_id': 'fixture-installation',
            'boot_id': '00000000-0000-4000-8000-000000000001', 'pid': 12,
            'sample_mono_s': now, 'loop_seq': 10, 'loop_mono_s': now,
            'store': {'enqueued_seq': 0, 'started_seq': 0, 'finished_seq': 0,
                      'pending_count': 0, 'oldest_pending_mono_s': None,
                      'current_started_mono_s': None}}


def handoff(instance='fixture-installation', *, ordinal=1, recovered=False, cause='loop'):
    boot = '00000000-0000-4000-8000-000000000001'
    row = {'boot_id': boot, 'ordinal': ordinal, 'episode_id': episode_id(instance, boot, ordinal),
           'condition': 'active', 'cause': cause}
    return {'version': 'daemon-episodes.v1', 'instance_id': instance, 'active': row,
            'recovery': {**row, 'condition': 'recovered'} if recovered else None}


def owned_path(tmp_path):
    root = tmp_path.resolve() / 'runtime'
    root.mkdir(mode=0o700)
    return root / 'progress.json'


def test_controlled_progress_threshold_and_idle():
    p = progress()
    assert cause_at(p, 14.999) is None
    assert cause_at(p, 15) == 'unavailable'
    p['sample_mono_s'] = 15
    assert cause_at(p, 15) == 'loop'
    p['loop_mono_s'] = 15
    assert cause_at(p, 15) is None  # empty Store never expires
    p['store'].update(enqueued_seq=2, started_seq=1, pending_count=1,
                      oldest_pending_mono_s=10, current_started_mono_s=10)
    assert cause_at(p, 15) == 'store'
    p['loop_mono_s'] = 10
    assert cause_at(p, 15) == 'loop_store'


@pytest.mark.parametrize('mutate', [
    lambda p: p.update(version='unknown'), lambda p: p.update(boot_id=123),
    lambda p: p.update(instance_id='/private/path'), lambda p: p.update(pid=False),
    lambda p: p.update(loop_seq=-1), lambda p: p.update(sample_mono_s=11),
    lambda p: p.update(loop_mono_s=float('nan')), lambda p: p.update(extra='secret'),
    lambda p: p['store'].update(started_seq=1),
    lambda p: p['store'].update(oldest_pending_mono_s=0),
    lambda p: p['store'].update(pending_count=-1),
    lambda p: p['store'].update(enqueued_seq=1, pending_count=1, oldest_pending_mono_s=11),
])
def test_invalid_progress_is_not_healthy(mutate):
    p = progress()
    mutate(p)
    with pytest.raises((ValueError, TypeError)):
        validate_snapshot(p, 10)


def test_queue_cap_and_constant_bookkeeping():
    p = StoreProgress()
    q = queue.Queue(maxsize=2)
    p.enqueue(q.put_nowait, (None, None, None, 1.0, None))
    p.enqueue(q.put_nowait, (None, None, None, 2.0, None))
    with pytest.raises(queue.Full):
        p.enqueue(q.put_nowait, (None, None, None, 3.0, None))
    assert len(p.pending) == 2
    assert p.snapshot()['enqueued_seq'] == 2
    q.get_nowait(); p.start(4.0)
    assert p.snapshot()['oldest_pending_mono_s'] == 2
    assert p.snapshot()['current_started_mono_s'] == 4
    p.finish(); q.get_nowait(); p.discard()
    assert p.snapshot() == progress()['store'] | {'enqueued_seq': 2, 'started_seq': 2, 'finished_seq': 2}


def test_owner_only_atomic_files(tmp_path):
    path = owned_path(tmp_path)
    write_json(path, progress())
    assert read_json(path) == progress()
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(path.parent.glob('.*.tmp'))
    path.chmod(0o644)
    with pytest.raises(ValueError): read_json(path)
    with pytest.raises(ValueError): write_json(path, progress())
    path.chmod(0o600)
    path.write_text('x' * 4097)
    with pytest.raises(ValueError): read_json(path)
    path.unlink(); path.symlink_to(tmp_path / 'outside')
    with pytest.raises((ValueError, OSError)): write_json(path, progress())
    with pytest.raises((ValueError, OSError)): read_json(path)
    path.unlink(); path.parent.chmod(0o755)
    with pytest.raises(ValueError): write_json(path, progress())
    path.parent.chmod(0o700)
    link = tmp_path / 'link'; link.symlink_to(path.parent, target_is_directory=True)
    with pytest.raises(OSError): write_json(link / path.name, progress())


def test_wrong_owner_and_hardlink_rejected(tmp_path, monkeypatch):
    path = owned_path(tmp_path)
    write_json(path, progress())
    os.link(path, path.with_name('hardlink'))
    with pytest.raises(ValueError): read_json(path)
    path.with_name('hardlink').unlink()
    uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises(ValueError): read_json(path)


def test_handoff_identity_shape_and_no_grants():
    h = handoff(recovered=True)
    assert validate_handoff(h, h['instance_id']) == h
    assert adapt('daemon_loop_stalled', h['active']) == ErrorFact(
        'daemon_runtime.v1', 'loop_stalled', h['active']['episode_id'], 'active', 'loop')
    for bad in ({**h, 'instance_id': 'another'}, {**h, 'active': None},
                {**h, 'recipient': 'forbidden'}):
        with pytest.raises(ValueError): validate_handoff(bad, h['instance_id'])
    for field, value in [('ordinal', 0), ('episode_id', 'forged'), ('cause', 'token_failure'), ('boot_id', 0)]:
        bad = copy.deepcopy(h); bad['active'][field] = value
        with pytest.raises((ValueError, TypeError)): validate_handoff(bad, h['instance_id'])
    assert adapt('daemon_loop_stalled', {'episode_id': 'x'*64, 'condition': 'active', 'cause': 'loop'}) is None


@pytest.mark.asyncio
async def test_real_store_current_queue_and_request_owned_timings(tmp_path):
    s = Store(str(tmp_path / 'hot.db'), max_pending=2); s.start()
    release, entered = threading.Event(), threading.Event()
    first, second = RequestTiming(), RequestTiming()
    def held(conn):
        entered.set(); release.wait(3)
    async def submit(timing, fn):
        token = request_timing.set(timing)
        try: return await s.submit(fn)
        finally: request_timing.reset(token)
    try:
        task = asyncio.create_task(submit(first, held))
        await asyncio.to_thread(entered.wait, 2)
        other = asyncio.create_task(submit(second, lambda c: 7))
        await asyncio.sleep(.03)
        snap = s.progress.snapshot()
        assert snap['started_seq'] == 1 and snap['pending_count'] == 1
        assert snap['current_started_mono_s'] <= snap['oldest_pending_mono_s']
        release.set()
        assert await other == 7
        await task
        assert first.calls == second.calls == 1
        assert first.execution_s >= .025 and second.execution_s < .02
        assert second.queue_s >= .025
        assert s.progress.snapshot()['current_started_mono_s'] is None
        assert s.progress.snapshot()['oldest_pending_mono_s'] is None
    finally:
        release.set(); s.stop()


@pytest.mark.asyncio
async def test_publisher_lifecycle_and_bounded_healthy_repetitions(tmp_path):
    path = owned_path(tmp_path)
    s = Store(str(tmp_path / 'hot.db')); s.start()
    w = LoopWatchdog(s, Alerts(), progress_path=path, instance_id='fixture-installation')
    w.start(asyncio.get_running_loop())
    samples = []
    try:
        await asyncio.sleep(.05)
        p = await asyncio.to_thread(read_json, path)
        assert validate_snapshot(p, time.monotonic())['store']['pending_count'] == 0
        for _ in range(20):
            started = time.monotonic()
            await asyncio.gather(*(s.get('fixture-missing') for _ in range(10)))
            samples.append(time.monotonic() - started)
        # A controlled offloaded wait does not occupy MAIN or Store.
        wait = asyncio.create_task(asyncio.to_thread(time.sleep, 1.05))
        await asyncio.sleep(.01)
        probe = time.monotonic(); await s.get('fixture-missing'); await asyncio.sleep(0)
        assert time.monotonic() - probe < .2
        await wait
        async with asyncio.timeout(2):
            while True:
                later = await asyncio.to_thread(read_json, path)
                if later['loop_seq'] > p['loop_seq']:
                    break
                await asyncio.sleep(.02)
        assert not w.binding_available  # absent checker handoff never protected
        assert max(samples) < .2
        print(json.dumps({'healthy_repetitions': 20, 'concurrency': 10,
                          'p50_s': statistics.median(samples), 'p95_s': sorted(samples)[18],
                          'max_s': max(samples), 'rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                          'host_load': os.getloadavg(), 'queue_cap': s._queue.maxsize}))
    finally:
        await w.stop(); s.stop()
    assert w.thread is None and s._thread is None
    newer = LoopWatchdog(s, Alerts(), progress_path=path, instance_id='fixture-installation')
    assert newer.boot_id != w.boot_id and newer.instance_id == w.instance_id


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['on', 'record-only', 'off', 'invalid', None])
async def test_real_typed_handoff_mode_and_recovery_suppression(subject, tmp_path, monkeypatch, mode):
    if mode is None: monkeypatch.delenv('PENTACLE_ERROR_ALERTS_MODE', raising=False)
    else: monkeypatch.setenv('PENTACLE_ERROR_ALERTS_MODE', mode)
    s = subject
    alerts = Alerts(s.store); alerts.sink = s.service
    path = owned_path(tmp_path)
    w = LoopWatchdog(s.store, alerts, progress_path=path, instance_id='fixture-installation')
    doc = handoff(recovered=True)
    write_json(path.with_name(path.name+'.episodes.json'), doc)
    await asyncio.to_thread(w._exchange, w.snapshot())
    await w.consume_once(); await w.consume_once()
    rows = await s.notify._db.call('error_rows')
    assert len(rows) == 1 and rows[0]['firing_count'] == 2
    assert rows[0]['error_context']['condition'] == 'recovered'
    await s.queue.drain_once()
    assert not s.provider.pastes
    assert (await s.notify._db.call('error_rows'))[0]['error_context']['disposition'] == 'suppressed_recovered'
    await asyncio.to_thread(w._exchange, w.snapshot())
    ack = read_json(path.with_name(path.name+'.ack.json'))
    assert ack['active'] == ack['terminal'] == doc['active']['episode_id']
    restarted = LoopWatchdog(s.store, alerts, progress_path=path, instance_id='fixture-installation')
    await asyncio.to_thread(restarted._exchange, restarted.snapshot())
    await restarted.consume_once()
    assert (await s.notify._db.call('error_rows'))[0]['firing_count'] == 2


@pytest.mark.asyncio
async def test_real_typed_initial_attempt_then_one_recovery(subject, tmp_path, monkeypatch):
    s = subject
    monkeypatch.setenv('PENTACLE_ERROR_ALERTS_MODE', 'on')
    alerts = Alerts(s.store); alerts.sink = s.service
    path = owned_path(tmp_path)
    w = LoopWatchdog(s.store, alerts, progress_path=path, instance_id='fixture-installation')
    ep_path = path.with_name(path.name+'.episodes.json')
    write_json(ep_path, handoff())
    await asyncio.to_thread(w._exchange, w.snapshot()); await w.consume_once()
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
    write_json(ep_path, handoff(recovered=True, cause='loop_store'))
    await asyncio.to_thread(w._exchange, w.snapshot()); await w.consume_once()
    monkeypatch.setenv('PENTACLE_FRONT_DESK_DIGEST_ENABLED', '0')
    for _ in range(3): await s.queue.drain_once()
    assert len(s.provider.pastes) == 2
    assert 'condition=recovered' in s.provider.pastes[-1]
    await w.consume_once(); await s.queue.drain_once()
    assert len(s.provider.pastes) == 2


@pytest.mark.asyncio
async def test_sink_failure_and_unacknowledged_state_not_replaced(tmp_path):
    class Sink:
        calls = 0
        async def emit(self, fact, principal=None):
            self.calls += 1
            if self.calls == 1: raise OSError('fixture failure')
            return 'fixture-notification'
    s = Store(); alerts = Alerts(); alerts.sink = Sink()
    path = owned_path(tmp_path)
    w = LoopWatchdog(s, alerts, progress_path=path, instance_id='fixture-installation')
    ep_path = path.with_name(path.name+'.episodes.json')
    write_json(ep_path, handoff())
    await asyncio.to_thread(w._exchange, w.snapshot())
    with pytest.raises(OSError): await w.consume_once()
    assert w.ack['active'] is None
    write_json(ep_path, handoff(ordinal=2))
    with pytest.raises(ValueError): await asyncio.to_thread(w._exchange, w.snapshot())
    await w.consume_once()
    assert w.ack['active'] == handoff()['active']['episode_id']
    assert alerts.sink.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,deliver', [('on', True), ('record-only', False), ('off', False), ('invalid', False), (None, False)])
async def test_active_typed_mode_policy(subject, monkeypatch, mode, deliver):
    if mode is None: monkeypatch.delenv('PENTACLE_ERROR_ALERTS_MODE', raising=False)
    else: monkeypatch.setenv('PENTACLE_ERROR_ALERTS_MODE', mode)
    alerts = Alerts(subject.store); alerts.sink = subject.service
    row = handoff()['active']
    await alerts.record('daemon_loop_stalled', **row)
    await subject.queue.drain_once()
    assert len(await subject.notify._db.call('error_rows')) == 1
    assert len(subject.provider.pastes) == int(deliver)


@pytest.mark.asyncio
async def test_finished_request_drops_background_timing():
    timing = RequestTiming(closed=True)
    timing.add(1, 2, 3)
    assert timing.fields() == {'store_calls': 0, 'store_queue_ms': 0.0, 'store_execution_ms': 0.0}


def test_default_capacity_snapshot_does_not_iterate_queue():
    from collections import deque
    class NoIteration(deque):
        def __iter__(self):
            raise AssertionError('snapshot must not walk pending work')
    p = StoreProgress(); p.pending = NoIteration()
    q = queue.Queue(maxsize=10000)
    for i in range(10000):
        p.enqueue(q.put_nowait, (None, None, None, float(i), None))
    with pytest.raises(queue.Full):
        p.enqueue(q.put_nowait, (None, None, None, 10001.0, None))
    snap = p.snapshot()
    assert snap['pending_count'] == snap['enqueued_seq'] == 10000
    assert snap['oldest_pending_mono_s'] == 0
    assert len(json.dumps(snap).encode()) < 512


@pytest.mark.asyncio
async def test_logging_failure_cannot_stop_publisher_or_retry(tmp_path, monkeypatch):
    import loop_watchdog
    def broken(*args, **kwargs): raise OSError('fixture diagnostic failure')
    monkeypatch.setattr(loop_watchdog.log, 'warning', broken)
    path = owned_path(tmp_path); s = Store()
    w = LoopWatchdog(s, Alerts(), progress_path=path, instance_id='fixture-installation')
    w.start(asyncio.get_running_loop())
    try:
        async with asyncio.timeout(3):
            while not path.exists(): await asyncio.sleep(.01)
        first = await asyncio.to_thread(read_json, path)
        async with asyncio.timeout(3):
            while True:
                current = await asyncio.to_thread(read_json, path)
                if current['sample_mono_s'] > first['sample_mono_s']: break
                await asyncio.sleep(.02)
        assert w.thread.is_alive()
        calls = []
        async def fail_then_ok():
            calls.append(1)
            if len(calls) == 1: raise OSError('fixture sink failure')
        monkeypatch.setattr(w, 'consume_once', fail_then_ok)
        consumer = asyncio.create_task(w.run_consumer())
        try:
            async with asyncio.timeout(3):
                while len(calls) < 2: await asyncio.sleep(.01)
        finally:
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError): await consumer
    finally: await w.stop()
