"""Regression coverage for client-attributed slow-consumer drops."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json
import logging

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import server
from server import Server
from test_logging_diagnostics import assert_conn_diag_schema, diagnostic_records


class Socket:
    remote_address = ("10.0.0.0", 9911)

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self, **_kwargs: object) -> None:
        self.close_calls += 1


def test_overflow_evicts_superseded_state_frames_instead_of_disconnecting() -> None:
    daemon = Server()
    client = Socket()
    queue = asyncio.Queue(maxsize=3)
    daemon._clients.add(client)
    daemon._client_send_queues[client] = queue
    daemon._drop_slow_consumer = lambda _client: (_ for _ in ()).throw(AssertionError("must not drop"))  # type: ignore[method-assign]
    for item in (
        ("session.inventory", '{"version":1}'),
        ("chat.event", '{"seq":1}'),
        ("session.inventory", '{"version":2}'),
    ):
        queue.put_nowait(item)

    assert daemon._enqueue(client, "chat.event", '{"seq":2}') is True
    assert list(queue._queue) == [
        ("chat.event", '{"seq":1}'),
        ("session.inventory", '{"version":2}'),
        ("chat.event", '{"seq":2}'),
    ]


def test_overflow_eviction_preserves_non_coalescible_frames_in_order() -> None:
    daemon = Server()
    client = Socket()
    queue = asyncio.Queue(maxsize=5)
    daemon._clients.add(client)
    daemon._client_send_queues[client] = queue
    for item in (
        ("chat.event", '{"seq":1}'),
        ("host.status", '{"host":"a","n":1}'),
        ("chat.event", '{"seq":2}'),
        ("host.status", '{"host":"a","n":2}'),
        ("chat.event", '{"seq":3}'),
    ):
        queue.put_nowait(item)

    assert daemon._enqueue(client, "chat.event", '{"seq":4}') is True
    assert list(queue._queue) == [
        ("chat.event", '{"seq":1}'),
        ("chat.event", '{"seq":2}'),
        ("host.status", '{"host":"a","n":2}'),
        ("chat.event", '{"seq":3}'),
        ("chat.event", '{"seq":4}'),
    ]


def test_behind_client_coalesces_superseded_state_before_queuefull() -> None:
    """A client past the 80% warn line collapses its superseded full-state
    backlog the moment more coalescible churn arrives — it does NOT wait for a
    hard QueueFull. Keeping a slow client's backlog small cuts the volume a slow
    mobile must carry and parse and prevents the 1011 overflow drop — the
    daemon-owned half of the mobile 4000 focused_heartbeat_timeout reconnect
    churn (the direct pong reply path itself is healthy).
    """
    daemon = Server()
    client = Socket()
    warn_at = max(1, int(server.CLIENT_SEND_QUEUE_MAX * server.CLIENT_SEND_QUEUE_WARN_RATIO))
    queue = asyncio.Queue(maxsize=server.CLIENT_SEND_QUEUE_MAX)
    daemon._clients.add(client)
    daemon._client_send_queues[client] = queue
    # Fill just below the warn line with superseded session.inventory churn (one
    # coalesce key) plus a real chat.event that must survive.
    for index in range(warn_at - 1):
        queue.put_nowait(("session.inventory", json.dumps({"version": index})))
    queue.put_nowait(("chat.event", '{"seq":1}'))

    # One more coalescible frame crosses the warn line -> proactive coalesce.
    assert daemon._enqueue(client, "session.inventory", '{"version":999}') is True

    items = list(queue._queue)
    inventory = [frame for frame_type, frame in items if frame_type == "session.inventory"]
    chat = [frame for frame_type, frame in items if frame_type == "chat.event"]
    assert inventory == ['{"version":999}']  # collapsed to the newest state
    assert chat == ['{"seq":1}']  # real event preserved, order intact
    assert queue.qsize() < warn_at  # backlog shed well before QueueFull


def test_behind_client_incompressible_flood_is_not_coalesce_scanned() -> None:
    """A pure chat.event flood is not coalescible: proactive shedding must NOT
    fire for it (no O(n) scan per frame) — the existing QueueFull path drops the
    genuinely-too-fast client instead."""
    daemon = Server()
    client = Socket()
    warn_at = max(1, int(server.CLIENT_SEND_QUEUE_MAX * server.CLIENT_SEND_QUEUE_WARN_RATIO))
    queue = asyncio.Queue(maxsize=server.CLIENT_SEND_QUEUE_MAX)
    daemon._clients.add(client)
    daemon._client_send_queues[client] = queue
    # Spy on the coalesce pass: an incompressible flood must never trigger it
    # (else every chat.event past the warn line pays an O(queue) scan).
    scan_calls = {"n": 0}
    original_evict = daemon._evict_superseded_queue_frames

    def counting_evict(target_queue):  # type: ignore[no-untyped-def]
        scan_calls["n"] += 1
        return original_evict(target_queue)

    daemon._evict_superseded_queue_frames = counting_evict  # type: ignore[method-assign]
    for index in range(warn_at):
        assert daemon._enqueue(client, "chat.event", json.dumps({"seq": index})) is True
    # Nothing coalesced away, and — the actual requirement — no scan ran at all.
    assert queue.qsize() == warn_at
    assert scan_calls["n"] == 0


def test_overflow_burst_schedules_single_slow_consumer_teardown() -> None:
    async def run() -> None:
        daemon = Server()
        client = Socket()
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(("chat.event", "{}"))
        daemon._clients.add(client)
        daemon._client_send_queues[client] = queue

        assert daemon._enqueue(client, "chat.event", "{}") is False
        assert daemon._enqueue(client, "chat.event", "{}") is False
        await asyncio.sleep(0)
        assert client.close_calls == 1

    asyncio.run(run())


class DiagnosticPeer(Socket):
    """Only the transport boundary is scripted; production hooks do all work."""

    remote_address = ("127.0.0.1", 9911)

    def __init__(self) -> None:
        super().__init__()
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.attempts: list[str] = []
        self.send_gate: asyncio.Event | None = None
        self.send_permits: asyncio.Semaphore | None = None
        self.send_error: Exception | None = None
        self.protocol = SimpleNamespace(close_sent=None, close_rcvd=None, close_rcvd_then_sent=None)
        self.close_code = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def send(self, frame: str) -> None:
        self.attempts.append(frame)
        if self.send_permits is not None:
            await self.send_permits.acquire()
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(frame)

    async def close(self, *, code=1000, reason="") -> None:
        self.close_calls += 1
        self.protocol.close_sent = Close(code, reason)
        self.protocol.close_rcvd = Close(code, reason)
        self.protocol.close_rcvd_then_sent = False
        self.close_code = code
        self.incoming.put_nowait(None)

    def finish(self, *, code=1000, reason="") -> None:
        self.protocol.close_rcvd = Close(code, reason)
        self.protocol.close_sent = Close(code, reason)
        self.protocol.close_rcvd_then_sent = True
        self.close_code = code
        self.incoming.put_nowait(None)


async def _until(predicate) -> None:
    # Bounded scheduling turns, not a timing-sensitive load test.
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate(), "scripted transport did not reach the expected boundary"


@asynccontextmanager
async def _connection(*, maxsize=server.CLIENT_SEND_QUEUE_MAX, daemon=None):
    daemon = daemon or Server()
    peer = DiagnosticPeer()
    queue = asyncio.Queue(maxsize=maxsize)
    daemon._client_send_queues[peer] = queue
    handler = asyncio.create_task(daemon._handle_client(peer))
    try:
        await _until(lambda: len(peer.sent) == 1 or handler.done())
        assert json.loads(peer.sent[0])["type"] == "welcome"
        yield daemon, peer, queue, handler
    finally:
        if not handler.done():
            peer.finish()
        await handler
        await asyncio.sleep(0)


def _events(caplog, event):
    return [payload for _record, payload in diagnostic_records(caplog) if payload["event"] == event]


def _only_event(caplog, event):
    found = _events(caplog, event)
    assert len(found) == 1, f"expected exactly one {event} lifecycle record, got {len(found)}"
    return found[0]


def _queue_identity(payload):
    assert_conn_diag_schema(payload)
    assert sum(payload["queued_by_type"].values()) == payload["queue_depth"]


def test_slow_consumer_overflow_has_safe_attributed_force_close(caplog) -> None:
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=3) as (daemon, peer, queue, handler):
            # These wire labels were logged verbatim by the old warnings.
            peer.remote_address = ("198.51.100.239", 9911)
            daemon._client_identities[peer] = "PRIVATE_CLIENT_OVERFLOW"
            for index in range(3):
                assert daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": index}))
            assert daemon._enqueue(peer, "chat.event", '{}') is False
            assert daemon._enqueue(peer, "chat.event", '{}') is False
            await handler
            assert peer.close_calls == 1
            assert peer.protocol.close_sent == Close(1011, "slow_consumer")
        force = _events(caplog, "force_close")
        assert len(force) == 1, "overflow must emit exactly one force_close before queue removal"
        assert force[0]["initiator"] == "server"
        assert force[0]["cause"] == "slow_consumer"
        assert force[0]["cause_source"] == "server_policy"
        assert force[0]["close_code"] == 1011
        assert force[0]["close_reason"] == "slow_consumer"
        assert force[0]["queue_max"] == force[0]["queue_depth"] == 3
        assert force[0]["traffic"]["chat.event"]["broadcast_enqueued"] == 3
        assert force[0]["traffic"]["chat.event"]["broadcast_sent"] == 0
        assert len(_events(caplog, "close")) == 1
        assert not [p for p in _events(caplog, "slow_consumer") if p["phase"] == "recover"]
        for payload in force + _events(caplog, "close"):
            _queue_identity(payload)
        assert "198.51.100.239" not in caplog.text
        assert "PRIVATE_CLIENT_OVERFLOW" not in caplog.text
        assert "slow_consumer overflow:" not in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize("maxsize,warn_at,recover_at", [(256, 204, 128), (5, 4, 2), (1, 1, 0)])
def test_pressure_uses_actual_capacity_and_recovers_on_writer_dequeue(caplog, maxsize, warn_at, recover_at):
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=maxsize) as (daemon, peer, queue, _handler):
            for index in range(warn_at - 1):
                assert daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": index}))
            assert _events(caplog, "slow_consumer") == []
            assert daemon._enqueue(peer, "chat.event", '{"type":"chat.event","seq":999}')
            entered = _events(caplog, "slow_consumer")
            assert len(entered) == 1, "crossing actual queue capacity's 80% threshold must emit enter"
            assert entered[0]["phase"] == "enter"
            assert entered[0]["queue_depth"] == entered[0]["queue_peak"] == warn_at
            assert entered[0]["queue_max"] == maxsize
            if warn_at < maxsize:
                assert daemon._enqueue(peer, "chat.event", '{"type":"chat.event","seq":1000}')
                assert len(_events(caplog, "slow_consumer")) == 1
            await _until(lambda: queue.empty() and len(peer.sent) == 1 + min(warn_at + 1, maxsize))
            pressure = _events(caplog, "slow_consumer")
            assert [p["phase"] for p in pressure] == ["enter", "recover"]
            assert pressure[1]["queue_depth"] == recover_at
            assert pressure[1]["episode"] == pressure[0]["episode"] == 1
            for payload in pressure:
                _queue_identity(payload)
        assert not _events(caplog, "force_close")
    asyncio.run(run())


def test_pressure_excludes_inflight_frame_waiting_for_send_lock(caplog):
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=5) as (daemon, peer, queue, _handler):
            lock = daemon._client_send_locks[peer]
            await lock.acquire()
            try:
                assert daemon._enqueue(peer, "chat.event", '{"type":"chat.event","seq":0}')
                await _until(queue.empty)
                for seq in range(1, 5):
                    assert daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": seq}))
                entered = _events(caplog, "slow_consumer")
                assert len(entered) == 1
                assert entered[0]["queue_depth"] == 4
                assert entered[0]["queued_by_type"]["chat.event"] == 4
                assert entered[0]["traffic"]["chat.event"]["broadcast_enqueued"] == 5
                assert entered[0]["traffic"]["chat.event"]["broadcast_sent"] == 0
                assert len(peer.sent) == 1
            finally:
                lock.release()
            await _until(lambda: len(peer.sent) == 6)
        close = _only_event(caplog, "close")
        assert close["traffic"]["chat.event"]["broadcast_sent"] == 5
        assert close["queue_peak"] == 4
    asyncio.run(run())


def test_pressure_episode_rate_limit_keeps_matching_recovery_and_boundary(caplog, monkeypatch):
    async def run():
        clock = [100.0]
        monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=5) as (daemon, peer, queue, _handler):
            for when in (110.0, 169.999, 170.0):
                clock[0] = when
                sent_before = len(peer.sent)
                for seq in range(4):
                    assert daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": seq}))
                clock[0] = when + 0.0001
                await _until(lambda: len(peer.sent) == sent_before + 4)
                assert queue.empty()
            pressure = _events(caplog, "slow_consumer")
            assert [(p["episode"], p["phase"]) for p in pressure] == [
                (1, "enter"), (1, "recover"), (3, "enter"), (3, "recover"),
            ], "a suppressed pressure episode must omit both halves, with the 60s boundary inclusive"
            assert pressure[2]["pressure_episodes_suppressed"] == 1
        close = _only_event(caplog, "close")
        assert close["pressure_episodes_suppressed_total"] == 1
        assert len(_events(caplog, "connect")) == len(_events(caplog, "close")) == 1
    asyncio.run(run())


def test_precompaction_peak_and_immediate_recovery_preserve_chat_order(caplog):
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, queue, _handler):
            for seq in range(202):
                assert daemon._enqueue(peer, "session.inventory", json.dumps({"type": "session.inventory", "version": seq}))
            assert daemon._enqueue(peer, "chat.event", '{"type":"chat.event","seq":1}')
            assert daemon._enqueue(peer, "session.inventory", '{"type":"session.inventory","version":999}')
            assert list(queue._queue) == [
                ("chat.event", '{"type":"chat.event","seq":1}'),
                ("session.inventory", '{"type":"session.inventory","version":999}'),
            ]
            pressure = _events(caplog, "slow_consumer")
            assert [(p["phase"], p["queue_depth"]) for p in pressure] == [("enter", 204), ("recover", 2)]
            assert [p["queue_peak"] for p in pressure] == [204, 204]
            assert pressure[0]["traffic"]["session.inventory"]["coalesced"] == 0
            assert pressure[1]["traffic"]["session.inventory"]["coalesced"] == 202
            await _until(lambda: len(peer.sent) == 3)
        close = _only_event(caplog, "close")
        assert close["traffic"]["session.inventory"]["broadcast_enqueued"] == 203
        assert close["traffic"]["session.inventory"]["broadcast_sent"] == 1
        assert close["traffic"]["chat.event"]["broadcast_sent"] == 1
        assert not _events(caplog, "force_close")
    asyncio.run(run())


def test_small_queue_diagnostics_do_not_move_coalescing_policy_threshold(caplog):
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=5) as (daemon, peer, queue, _handler):
            for version in range(4):
                assert daemon._enqueue(peer, "session.inventory", json.dumps({"version": version}))
            # Baseline proactive coalescing still uses 204, even though the new
            # diagnostic enter must use this queue's actual threshold of four.
            assert queue.qsize() == 4
            assert [json.loads(f)["version"] for _t, f in queue._queue] == list(range(4))
            pressure = _events(caplog, "slow_consumer")
            assert [(p["phase"], p["queue_depth"]) for p in pressure] == [("enter", 4)]
            assert daemon._enqueue(peer, "session.inventory", '{"version":4}')
            assert daemon._enqueue(peer, "chat.event", '{"seq":1}')
            assert list(queue._queue) == [("session.inventory", '{"version":4}'), ("chat.event", '{"seq":1}')]
            assert peer.close_calls == 0
            assert _events(caplog, "slow_consumer")[-1]["phase"] == "recover"
    asyncio.run(run())


def test_unregister_snapshot_survives_duplicate_removal_with_age(caplog, monkeypatch):
    async def run():
        clock = [10.0]
        monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=5) as (daemon, peer, queue, handler):
            daemon._enqueue(peer, "chat.event", '{"type":"chat.event","text":"unsent"}')
            daemon._enqueue(peer, "unrecognized.family", '{"type":"unrecognized.family"}')
            daemon._unregister_client(peer)
            assert not _events(caplog, "close"), "unregister alone is not socket termination"
            clock[0] = 10.125
            daemon._unregister_client(peer)
            peer.finish()
            await handler
        close = _events(caplog, "close")
        assert len(close) == 1
        assert close[0]["queue_depth"] == close[0]["queue_peak"] == 2
        assert close[0]["queue_snapshot_age_ms"] == 125
        assert close[0]["queued_by_type"]["chat.event"] == 1
        assert close[0]["queued_by_type"]["other"] == 1
        assert close[0]["tx_messages"] == 1
        _queue_identity(close[0])
    asyncio.run(run())


@pytest.mark.parametrize("interleave_history", [False, True])
def test_all_direct_stream_variants_count_exact_multibyte_bytes(caplog, interleave_history):
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, _queue, _handler):
            text = '{"type":"snapshot","work_lanes":[],"text":"雪☃"}'
            assert await daemon._send_direct(peer, text)
            assert await daemon._send_direct(peer, [{"type": "pong"}, {"type": "unlisted", "text": "é"}])
            async def chunks():
                yield {"type": "chat.event", "text": "é"}
                yield server._EncodedFrame("session.inventory", '{"type":"session.inventory","text":"雪"}')
            assert await daemon._send_direct(peer, chunks(), interleave_history=interleave_history)
            sent = list(peer.sent)
        close = _only_event(caplog, "close")
        assert close["tx_messages"] == 6
        assert close["tx_bytes"] == sum(len(frame.encode("utf-8")) for frame in sent)
        for bucket in close["traffic"]:
            family = [frame for frame in sent if (
                json.loads(frame).get("type") if json.loads(frame).get("type") in close["traffic"] else "other"
            ) == bucket]
            assert close["traffic"][bucket]["direct_sent"] == len(family)
            assert close["traffic"][bucket]["direct_sent_bytes"] == sum(len(frame.encode("utf-8")) for frame in family)
            assert close["traffic"][bucket]["broadcast_sent"] == 0
        assert close["traffic"]["snapshot"]["direct_sent"] == 1
        assert close["traffic"]["work_lanes.inventory"]["direct_sent"] == 0
        _queue_identity(close)
    asyncio.run(run())


def test_ping_receipt_pong_completion_and_send_wait_timing(caplog, monkeypatch):
    async def run():
        clock = [10.0]
        monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, _queue, _handler):
            lock = daemon._client_send_locks[peer]
            await lock.acquire()
            peer.send_gate = asyncio.Event()
            ping = json.dumps({"type": "ping", "request_id": "é-ping"}, ensure_ascii=False)
            clock[0] = 11.0
            peer.incoming.put_nowait(ping)
            # The actual request reaches _send_direct and parks on the lock.
            await _until(lambda: bool(lock._waiters))
            clock[0] = 11.125
            lock.release()
            await _until(lambda: len(peer.attempts) == 2)
            clock[0] = 11.375
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 2)
            clock[0] = 12.0
        close = _only_event(caplog, "close")
        assert close["rx_messages"] == 1
        assert close["rx_bytes"] == len(ping.encode("utf-8"))
        assert close["last_rx_age_ms"] == close["last_ping_age_ms"] == 1000
        assert close["last_tx_age_ms"] == close["last_pong_age_ms"] == 625
        assert close["send_lock_wait_max_ms"] == 125
        assert close["send_call_max_ms"] == 250
        assert close["traffic"]["pong"]["direct_sent"] == 1
        assert not _events(caplog, "auth_ok"), "an exempt anonymous ping isn't an authentication"
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["exception", "cancelled"])
def test_unsuccessful_broadcast_finishes_timing_without_counting_send(caplog, monkeypatch, failure):
    async def run():
        clock = [10.0]
        monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, queue, handler):
            peer.send_gate = asyncio.Event()
            daemon._enqueue(peer, "chat.event", '{"type":"chat.event","text":"failed"}')
            await _until(lambda: len(peer.attempts) == 2)
            # A second frame remains queued while the first is in websocket.send.
            daemon._enqueue(peer, "host.status", '{"type":"host.status","host":"fixture"}')
            clock[0] = 10.250
            writer = daemon._client_writer_tasks[peer]
            if failure == "exception":
                peer.send_error = OSError("synthetic transport failure")
                peer.send_gate.set()
            else:
                writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            assert not _events(caplog, "close")
            clock[0] = 10.500
            peer.finish()
            await handler
        close = _only_event(caplog, "close")
        assert close["send_call_max_ms"] == 250
        assert close["traffic"]["chat.event"]["broadcast_enqueued"] == 1
        assert close["traffic"]["chat.event"]["broadcast_sent"] == 0
        assert close["traffic"]["chat.event"]["broadcast_sent_bytes"] == 0
        assert close["tx_messages"] == 1
        assert close["queue_depth"] == 1
        assert close["queued_by_type"]["host.status"] == 1
    asyncio.run(run())


def test_broadcast_projection_dedup_and_bucket_accounting_remain_per_client(caplog):
    async def run():
        caplog.set_level(logging.INFO)
        daemon = Server()
        async with _connection(daemon=daemon) as (_d, capable, _q, _h):
            async with _connection(daemon=daemon) as (_d2, ordinary, _q2, _h2):
                for peer, mode, work_lanes in ((capable, "summary", True), (ordinary, "full", False)):
                    await daemon._serve(peer, json.dumps({
                        "type": "hello", "client": "pentacle", "capabilities": {"work_lanes_v1": work_lanes},
                        "subscribe": {"events_mode": mode, "include_subagents": True, "snapshot": False, "mode": "rpc"},
                    }))
                initial = {peer: len(peer.sent) for peer in (capable, ordinary)}
                inventory = {"type": "session.inventory", "sessions": [{
                    "stream_id": "fixture:seat", "host": "fixture", "session_name": "seat",
                    "transcript_path": "/synthetic/never-log", "last_text": "雪", "visibility": "default",
                }]}
                lane = {"type": "work_lanes.inventory", "work_lanes": [{"id": "fixture-lane"}]}
                chat = {"type": "chat.event", "event": {"stream_id": "fixture:seat", "text": "雪"}}
                for frame in (inventory, lane, chat):
                    await daemon.broadcast(frame)
                await _until(lambda: len(capable.sent) == initial[capable] + 3 and len(ordinary.sent) == initial[ordinary] + 2)
                await daemon.broadcast(inventory)
                await daemon.broadcast(chat)
                await _until(lambda: len(capable.sent) == initial[capable] + 4 and len(ordinary.sent) == initial[ordinary] + 3)
                projected = json.loads(capable.sent[initial[capable]])
                full = json.loads(ordinary.sent[initial[ordinary]])
                assert "transcript_path" not in projected["sessions"][0]
                assert full["sessions"][0]["transcript_path"] == "/synthetic/never-log"
                assert not any(json.loads(f)["type"] == "work_lanes.inventory" for f in ordinary.sent)
                sent_by_peer = {peer: list(peer.sent) for peer in (capable, ordinary)}
                ids = [p["conn_id"] for p in _events(caplog, "connect")]
                assert len(ids) == 2, "both real accepted clients need their own connect record"
        closes = {p["conn_id"]: p for p in _events(caplog, "close")}
        for peer, conn_id, lane_count in ((capable, ids[0], 1), (ordinary, ids[1], 0)):
            close = closes[conn_id]
            traffic = close["traffic"]
            assert traffic["session.inventory"]["broadcast_enqueued"] == 1
            assert traffic["session.inventory"]["broadcast_sent"] == 1
            assert traffic["session.inventory"]["deduped"] == 1
            assert traffic["work_lanes.inventory"]["broadcast_sent"] == lane_count
            assert traffic["work_lanes.inventory"]["broadcast_enqueued"] == lane_count
            assert traffic["chat.event"]["broadcast_sent"] == 2
            assert traffic["chat.event"]["deduped"] == 0
            assert close["tx_messages"] == len(sent_by_peer[peer])
            assert close["tx_bytes"] == sum(len(f.encode("utf-8")) for f in sent_by_peer[peer])
            assert close["tx_messages"] == sum(b["direct_sent"] + b["broadcast_sent"] for b in traffic.values())
            assert close["tx_bytes"] == sum(b["direct_sent_bytes"] + b["broadcast_sent_bytes"] for b in traffic.values())
    asyncio.run(run())


def test_pressure_hysteresis_does_not_restart_while_hovering_above_recovery(caplog):
    async def run():
        caplog.set_level(logging.INFO)
        async with _connection(maxsize=10) as (daemon, peer, queue, _handler):
            peer.send_permits = asyncio.Semaphore(0)
            for index in range(8):
                daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": index}))
            await _until(lambda: len(peer.attempts) == 2)
            assert queue.qsize() == 7  # one in-flight
            daemon._enqueue(peer, "chat.event", '{"type":"chat.event","seq":8}')
            peer.send_permits.release()
            peer.send_permits.release()
            await _until(lambda: len(peer.attempts) == 4)
            assert queue.qsize() == 6  # below warn, still above recovery
            for index in (9, 10):
                daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": index}))
            assert queue.qsize() == 8
            assert [(p["episode"], p["phase"]) for p in _events(caplog, "slow_consumer")] == [(1, "enter")]
            for _ in range(3):
                peer.send_permits.release()
            await _until(lambda: len(peer.attempts) == 7)
            assert queue.qsize() == 5
            assert [(p["episode"], p["phase"]) for p in _events(caplog, "slow_consumer")] == [(1, "enter"), (1, "recover")]
    asyncio.run(run())


@pytest.mark.parametrize("variant", ["list", "stream", "interleaved_stream"])
def test_failed_direct_send_times_and_counts_only_successful_completions(caplog, monkeypatch, variant):
    async def run():
        clock = [1.0]
        monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, _queue, handler):
            assert await daemon._send_direct(peer, [{"type": "snapshot", "text": "雪"}])
            successful = list(peer.sent)
            peer.send_gate = asyncio.Event()
            peer.send_error = ConnectionClosedError(None, None)
            async def chunks():
                yield server._EncodedFrame("chat.event", '{"type":"chat.event","text":"未送信"}')
            frames = [{"type": "chat.event", "text": "未送信"}] if variant == "list" else chunks()
            sending = asyncio.create_task(daemon._send_direct(peer, frames, interleave_history=variant == "interleaved_stream"))
            await _until(lambda: len(peer.attempts) == 3)
            clock[0] = 1.250
            peer.send_gate.set()
            assert await sending is False
            assert peer not in daemon._clients
            assert not _events(caplog, "close"), "a failed direct send unregisters but does not terminate the receive handler"
            clock[0] = 1.500
            peer.finish()
            await handler
        close = _only_event(caplog, "close")
        assert close["send_call_max_ms"] == 250
        assert close["tx_messages"] == 2
        assert close["tx_bytes"] == sum(len(f.encode("utf-8")) for f in successful)
        assert close["traffic"]["snapshot"]["direct_sent"] == 1
        assert close["traffic"]["chat.event"]["direct_sent"] == close["traffic"]["chat.event"]["direct_sent_bytes"] == 0
    asyncio.run(run())


@pytest.mark.parametrize("path", ["direct", "broadcast"])
def test_cancelled_send_lock_wait_is_measured_without_success(caplog, monkeypatch, path):
    async def run():
        clock = [5.0]
        monkeypatch.setattr(server, "_monotonic", lambda: clock[0])
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, queue, _handler):
            lock = daemon._client_send_locks[peer]
            await lock.acquire()
            try:
                if path == "direct":
                    task = asyncio.create_task(daemon._send_direct(peer, [{"type": "pong"}]))
                else:
                    daemon._enqueue(peer, "chat.event", '{"type":"chat.event"}')
                    task = daemon._client_writer_tasks[peer]
                await _until(lambda: bool(lock._waiters))
                clock[0] = 5.125
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError)
                assert len(peer.sent) == 1
                assert queue.empty()
            finally:
                lock.release()
        close = _only_event(caplog, "close")
        assert close["send_lock_wait_max_ms"] == 125
        assert close["send_call_max_ms"] == 0
        assert close["tx_messages"] == 1
        assert close["traffic"]["pong"]["direct_sent"] == 0
        assert close["traffic"]["chat.event"]["broadcast_sent"] == 0
    asyncio.run(run())
