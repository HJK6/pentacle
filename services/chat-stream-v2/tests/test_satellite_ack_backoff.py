"""Reconnect service failures over owned sockets, with deterministic retry time."""
from __future__ import annotations

import asyncio
import json
import random
import time
from types import SimpleNamespace

import pytest
import websockets

import satellite


def _satellite(tmp_path, monkeypatch, **config):
    monkeypatch.setattr(satellite, "sample_machine_stats", lambda host: {"host": host})
    sat = satellite.Satellite(satellite.SatelliteConfig(
        host="worker", checkout=str(tmp_path), push_secret="synthetic-secret",
        no_autoupdate=True, held_span_path=str(tmp_path / "held.json"),
        first_delay_s=0, interval_s=0.001, ack_timeout_s=0.02,
        backoff_base_s=1, backoff_max_s=8, **config,
    ))
    monkeypatch.setattr(sat, "_collect", lambda: ([], {}, False))
    return sat


def _retry_clock(monkeypatch, seed, stop_after):
    """Replace only the subject's sleep; socket deadlines keep real loop time."""
    delays, attempts = [], []
    rng = random.Random(seed)
    clock = [0.0]

    async def sleep(delay):
        if delay >= 0.5:
            delays.append(delay)
            clock[0] += delay
            if len(delays) >= stop_after:
                raise asyncio.CancelledError
        await asyncio.sleep(0)

    monkeypatch.setattr(satellite, "asyncio", SimpleNamespace(
        **{name: getattr(asyncio, name) for name in dir(asyncio) if name != "sleep"},
        sleep=sleep,
    ))
    monkeypatch.setattr(satellite, "random", SimpleNamespace(uniform=rng.uniform), raising=False)
    return delays, attempts, clock


@pytest.mark.parametrize("failure", ["close", "withhold", "stale"])
def test_failed_service_peers_grow_and_spread_retries(tmp_path, monkeypatch, failure):
    async def run():
        schedules, observed = [], []
        for peer in range(3):
            sat = _satellite(tmp_path / str(peer), monkeypatch)
            delays, attempts, clock = _retry_clock(monkeypatch, peer, 6)
            closed = 0

            async def handler(ws):
                nonlocal closed
                attempts.append(clock[0])
                try:
                    frame = json.loads(await ws.recv())
                    assert frame["type"] == "host.stats"
                    if failure == "stale":
                        await ws.send(json.dumps({"type": "host.stats.ok", "request_id": -1}))
                    if failure != "close":
                        await ws.wait_closed()
                finally:
                    closed += 1

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                sat.config.bart_ws = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(sat.run_forever(), 3)
            assert closed == len(attempts) == 6
            print(json.dumps({"peer": peer, "failure": failure, "attempts": attempts, "delays": delays}))
            observed.append(delays)
            schedules.append(attempts)
        assert len({tuple(s) for s in schedules}) == 3
        for peer, delays in enumerate(observed):
            rng = random.Random(peer)
            assert delays == pytest.approx([rng.uniform(b / 2, b) for b in [1, 2, 4, 8, 8, 8]])
    asyncio.run(run())


@pytest.mark.parametrize("mode", [
    "healthy", "push_error", "stats_error", "wrong_type", "partial_drop", "partial_usage",
    "stale_push", "malformed", "stats_wrong_type", "unfenced_rejected",
    "loss_conflict", "provenance_error", "provenance_rejected",
])
def test_only_complete_service_cycle_resets_retry(tmp_path, monkeypatch, mode):
    async def run():
        sat = _satellite(tmp_path, monkeypatch)
        delays, attempts, clock = _retry_clock(monkeypatch, 7, 4)
        closed = 0

        async def handler(ws):
            nonlocal closed
            number = len(attempts)
            attempts.append(clock[0])
            try:
                stats = json.loads(await ws.recv())
                if number != 2:
                    return
                await ws.send(json.dumps({
                    "type": {"stats_error": "host.stats.error", "stats_wrong_type": "pong"}.get(mode, "host.stats.ok"),
                    "request_id": stats["request_id"],
                }))
                push = json.loads(await ws.recv())
                reply = {"type": "event.push.ok", "request_id": push["request_id"]}
                if mode == "push_error":
                    reply["type"] = "event.push.error"
                elif mode == "wrong_type":
                    reply["type"] = "pong"
                elif mode == "partial_drop":
                    reply["dropped"] = [{"stream_id": "worker:s"}]
                elif mode == "partial_usage":
                    reply["usage_rejected"] = [{"stream_id": "worker:s", "reason": "stale"}]
                elif mode == "unfenced_rejected":
                    reply["wire_version"] = 2
                    reply["usage_unfenced"] = {"rejected": [{"key": "k", "seq": 1, "transient": True}]}
                elif mode == "loss_conflict":
                    reply["losses_conflict"] = [{"loss_id": "synthetic-loss"}]
                elif mode == "provenance_error":
                    reply["usage_provenance"] = {"error": "provenance_failed"}
                elif mode == "provenance_rejected":
                    reply["usage_provenance"] = {"rejected": [{"index": 0}]}
                if mode == "stale_push":
                    reply["request_id"] = stats["request_id"]
                await ws.send("not-json" if mode == "malformed" else json.dumps(reply))
            finally:
                closed += 1

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            sat.config.bart_ws = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(sat.run_forever(), 3)
        assert closed == len(attempts) == 4
        rng = random.Random(7)
        envelopes = [1, 2, 1, 2] if mode == "healthy" else [1, 2, 4, 8]
        assert delays == pytest.approx([rng.uniform(b / 2, b) for b in envelopes])
    asyncio.run(run())


def test_orderly_return_still_backs_off(tmp_path, monkeypatch):
    async def run():
        sat = _satellite(tmp_path, monkeypatch)
        delays, _, _ = _retry_clock(monkeypatch, 3, 4)
        calls = 0

        async def orderly(ws):
            nonlocal calls
            calls += 1
            if calls > 4:
                raise asyncio.CancelledError  # finite RED control for a tight loop

        monkeypatch.setattr(sat, "_session", orderly)
        async with websockets.serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0) as server:
            sat.config.bart_ws = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            with pytest.raises(asyncio.CancelledError):
                await sat.run_forever()
        assert calls == 4
        assert len(delays) == 4
    asyncio.run(run())


def test_long_outage_saturates_and_new_run_starts_at_base(tmp_path, monkeypatch):
    async def run():
        sat = _satellite(tmp_path, monkeypatch)
        sat.config.backoff_base_s = 1.0
        sat.config.backoff_max_s = 8.0

        def refused(*args, **kwargs):
            raise OSError("synthetic refusal")

        monkeypatch.setattr(satellite, "websockets", SimpleNamespace(connect=refused))
        for count in (1200, 2):
            delays, _, _ = _retry_clock(monkeypatch, 9, count)
            with pytest.raises(asyncio.CancelledError):
                await sat.run_forever()
            rng = random.Random(9)
            envelopes = ([1, 2, 4] + [8] * count)[:count]
            assert delays == pytest.approx([rng.uniform(b / 2, b) for b in envelopes])
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["receive", "backoff", "disabled"])
def test_shutdown_cancels_receive_backoff_and_disabled_mode(tmp_path, monkeypatch, phase):
    async def run():
        sat = _satellite(tmp_path, monkeypatch, disabled=phase == "disabled")
        sat.config.ack_timeout_s = 3600
        sat.config.backoff_base_s = sat.config.backoff_max_s = 3600
        ready = asyncio.Event()
        received = []
        closed = 0

        async def sleep(delay):
            if phase == "backoff" or phase == "disabled":
                ready.set()
            await asyncio.sleep(delay)

        monkeypatch.setattr(satellite, "asyncio", SimpleNamespace(
            **{name: getattr(asyncio, name) for name in dir(asyncio) if name != "sleep"},
            sleep=sleep,
        ))

        async def handler(ws):
            nonlocal closed
            try:
                if phase == "disabled":
                    async for frame in ws:
                        received.append(frame)
                else:
                    received.append(await ws.recv())
                    if phase == "receive":
                        ready.set()
                        await ws.wait_closed()
            finally:
                closed += 1

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            sat.config.bart_ws = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            task = asyncio.create_task(sat.run_forever())
            try:
                await asyncio.wait_for(ready.wait(), 2)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
        assert closed == 1
        assert len(received) == (0 if phase == "disabled" else 1)
    asyncio.run(run())


def test_three_peers_recover_lost_ack_without_loss_or_duplicate_delivery(tmp_path, monkeypatch):
    from event_push import EventPush
    from store import Store

    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        broadcasts, receipts, durations = [], [], []

        async def broadcast(frame):
            broadcasts.append(frame)

        async def secret():
            return "synthetic-secret"

        async def stats_handler(host, stats):
            pass

        alerts = SimpleNamespace(emit=lambda *args, **kwargs: None)
        sink = EventPush(store, broadcast, alerts, recent_limit=20, host_stats_handler=stats_handler)
        sink._secret = secret
        try:
            for peer in range(3):
                peer_root = tmp_path / str(peer)
                peer_root.mkdir()
                path = peer_root / "transcript.jsonl"
                path.write_text(json.dumps({
                    "type": "user", "uuid": f"input-{peer}",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"role": "user", "content": f"input {peer}"},
                }) + "\n")
                sat = _satellite(peer_root, monkeypatch)
                sat.config.ack_timeout_s = 2  # real SQLite/fsync work is not the timeout subject
                sat.config.host = f"worker-{peer}"
                sat.config.history_bytes = -1
                monkeypatch.setattr(sat, "_collect", satellite.Satellite._collect.__get__(sat))
                monkeypatch.setattr(sat, "_discover", lambda: {"s": str(path)})
                monkeypatch.setattr(satellite, "sample_machine_stats", lambda host: {
                    "host": host, "cpu_load_1m": 0, "memory_used_bytes": 1,
                    "memory_total_bytes": 2, "disk_used_bytes": 1,
                    "disk_total_bytes": 2, "uptime_seconds": 10,
                })
                await store.open_session(sat.config.host, "s", provider="claude")
                delays, attempts, clock = _retry_clock(monkeypatch, peer, 3)
                offsets, inserted = [], []
                closed = 0

                async def handler(ws):
                    nonlocal closed
                    number = len(attempts)
                    attempts.append(clock[0])
                    offsets.append(sat._tails["s"].offset if "s" in sat._tails else 0)
                    try:
                        if number == 2:
                            await ws.recv()
                            return
                        stats = json.loads(await ws.recv())
                        assert stats["push_secret"] == sat.config.push_secret
                        stats_ack = await sink.handle_host_stats(stats)
                        assert stats_ack["type"] == "host.stats.ok"
                        await ws.send(json.dumps(stats_ack))
                        push = json.loads(await ws.recv())
                        assert push["push_secret"] == sat.config.push_secret
                        started = time.perf_counter()
                        ack = await sink.handle_push(push)
                        durations.append(time.perf_counter() - started)
                        assert ack["type"] == "event.push.ok" and not ack["dropped"]
                        inserted.append(ack["inserted"])
                        if number == 0:
                            return  # durable accept followed by lost ACK
                        await ws.send(json.dumps(ack))
                    finally:
                        closed += 1

                async with websockets.serve(handler, "127.0.0.1", 0) as server:
                    sat.config.bart_ws = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(sat.run_forever(), 3)
                assert closed == len(attempts) == 3
                assert inserted == [1, 0]
                assert offsets == [0, 0, path.stat().st_size]
                assert sat._tails["s"].offset == path.stat().st_size
                rows = await store.fetch_session_event_tail(f"worker-{peer}:s", limit=10)
                assert len(rows) == 1 and rows[0]["text"] == f"input {peer}"
                rng = random.Random(peer)
                assert delays == pytest.approx([rng.uniform(b / 2, b) for b in [1, 1, 2]])
                receipts.append({"peer": peer, "offsets": offsets, "inserted": inserted,
                                 "closed": closed, "attempts": attempts, "delays": delays})
            assert len([f for f in broadcasts if f.get("type") == "chat.event"]) == 3
        finally:
            store.stop()
        ordered = sorted(durations)
        print(json.dumps({"recovery": receipts, "store_stopped": True,
                          "push_s": {"p50": ordered[len(ordered) // 2],
                                     "p95": ordered[-1], "max": ordered[-1]}}))
    asyncio.run(run())


@pytest.mark.parametrize("loss_only", [False, True])
def test_held_contention_retains_data_and_backoff_until_real_recovery(tmp_path, monkeypatch, loss_only):
    import sqlite3
    from test_usage_unfenced_ac10 import _Rig, _claude, _now, HOST, PANE, SID_A
    from usage_hold import ENABLED_TTL_S

    async def run():
        rig = _Rig(tmp_path)
        try:
            await rig.open("s", "generation-one")
            sat = rig.sat
            sat.config.no_autoupdate = True
            sat.config.first_delay_s = 0
            sat.config.interval_s = 0.001
            sat.config.ack_timeout_s = 2
            sat.config.backoff_base_s = 1
            sat.config.backoff_max_s = 8
            monkeypatch.setattr(sat, "_collect", lambda: ([], {}, False))
            monkeypatch.setattr(satellite, "sample_machine_stats", lambda host: {
                "host": host, "cpu_load_1m": 0, "memory_used_bytes": 1,
                "memory_total_bytes": 2, "disk_used_bytes": 1,
                "disk_total_bytes": 2, "uptime_seconds": 10,
            })
            held = sat._held_spans()
            record = _claude("held-input", _now())
            record["transcript_ts"] = record.pop("timestamp")
            held.hold({"stream_id": f"{HOST}:s", "provider": "claude", "source_pane_pid": PANE,
                       "native_session_id": SID_A, "source_file_identity_digest": "a" * 64}, [record], None)
            if loss_only:
                held.tick(enabled=True, elapsed_s=ENABLED_TTL_S + 1)
                assert held.frame_losses() and not held.pending_count()
            before = json.loads(held.path.read_text())
            delays, attempts, clock = _retry_clock(monkeypatch, 11, 5)
            original_record = rig.store.record_unfenced
            outcomes, closed = [], 0

            async def record_unfenced(*args, **kwargs):
                if len(attempts) == 3:
                    raise sqlite3.OperationalError("database is locked")
                return await original_record(*args, **kwargs)

            monkeypatch.setattr(rig.store, "record_unfenced", record_unfenced)

            async def handler(ws):
                nonlocal closed
                number = len(attempts)
                attempts.append(clock[0])
                try:
                    stats = json.loads(await ws.recv())
                    if number not in (2, 3):
                        return
                    await ws.send(json.dumps(await rig.ep.handle_host_stats(stats)))
                    # The first push negotiates v2; pending held state is not yet offered.
                    probe = json.loads(await ws.recv())
                    assert "usage_unfenced" not in probe and "usage_unfenced_losses" not in probe
                    await ws.send(json.dumps(await rig.ep.handle_push(probe)))
                    push = json.loads(await ws.recv())
                    assert bool(push.get("usage_unfenced_losses")) == loss_only
                    assert bool(push.get("usage_unfenced")) != loss_only
                    ack = await rig.ep.handle_push(push)
                    assert ack["type"] == "event.push.ok"
                    assert not ack["dropped"] and not ack["usage_rejected"]
                    outcomes.append(ack)
                    if number == 2:
                        assert bool(ack["usage_unfenced"]["rejected"]) != loss_only
                        assert not ack["losses_recorded"]
                    else:
                        assert not ack["usage_unfenced"]["rejected"]
                    await ws.send(json.dumps(ack))
                    # Next frame proves the subject has applied this ACK.
                    await ws.recv()
                    if number == 2:
                        after = json.loads(held.path.read_text())
                        assert after["entries"] == before["entries"]
                        assert after["losses"] == before["losses"]
                finally:
                    closed += 1

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                sat.config.bart_ws = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(sat.run_forever(), 3)
            assert closed == len(attempts) == 5
            assert len(outcomes) == 2
            assert not held.pending_count() and not held.frame_losses()
            rng = random.Random(11)
            assert delays == pytest.approx([rng.uniform(b / 2, b) for b in [1, 2, 4, 1, 2]])
            print(json.dumps({"loss_only": loss_only, "closed": closed,
                              "retained_then_drained": True, "delays": delays}))
        finally:
            rig.stop()
    asyncio.run(run())
