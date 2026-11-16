"""Synthetic stalled-send replay of the recorded 204-frame queue shape."""
from __future__ import annotations

import asyncio
from collections import Counter
import json
import logging

from server import COALESCIBLE_BROADCAST_FRAME_TYPES, _conn_bucket, _encode_frame
from test_slow_consumer_logging import _connection, _events, _until

EARLY = {"session.inventory", "working.state", "hosts.stats"}


def _episode():
    frames = []
    # Invented payloads and ordering; aggregate counts/key cardinalities match.
    for n in range(92):
        if n < 78:
            frames.append({"type": "session.inventory", "sessions": [], "version": n})
        frames.append({"type": "working.state", "stream_id": f"fixture:s{n % 6}", "version": n, "working": bool(n % 2)})
        if n < 14:
            frames.append({"type": "hosts.stats", "hosts": [], "version": n})
        if n < 15:
            frames.append({"type": "chat.event", "event": {"stream_id": "fixture:s0", "seq": n, "text": "snow 雪"}})
    frames.extend([
        {"type": "work_lanes.inventory", "lanes": []},
        {"type": "limits.update", "version": 1},
        {"type": "fixture.one", "seq": 1},
        {"type": "fixture.two", "seq": 2},
        {"type": "session.inventory", "sessions": [], "version": 78},
    ])
    assert len(frames) == 204
    return frames


async def _run(caplog):
    caplog.set_level(logging.INFO)
    receipt = {}
    async with _connection() as (daemon, peer, queue, _handler):
        daemon._activate_client(peer)
        daemon._client_include_subagents[peer] = True
        daemon._client_events_mode[peer] = "summary"
        daemon._client_work_lanes_v1[peer] = True
        peer.send_gate = asyncio.Event()
        assert daemon._enqueue(peer, "snapshot", '{"type":"snapshot","fixture":"held"}')
        await _until(lambda: len(peer.attempts) == 2)
        assert queue.empty() and len(peer.sent) == 1
        depths, violations, checkpoint = [], [], None
        frames = _episode()
        for index, frame in enumerate(frames, 1):
            await daemon.broadcast(frame)
            items = list(queue._queue)
            keys = Counter(daemon._coalesce_frame_key(t, f) for t, f in items if t in EARLY)
            depths.append(queue.qsize())
            if any(n > 1 for n in keys.values()):
                violations.append(index)
            if index == 203:
                checkpoint = {"depth": queue.qsize(), "counts": dict(Counter(t for t, _ in items))}
        items = list(queue._queue)
        # Independent survivor oracle: retain last occurrence of an existing
        # replaceable key, leave all non-replaceable bytes in original order.
        encoded = [(f["type"], _encode_frame(f)) for f in frames]
        last = {daemon._coalesce_frame_key(t, f): i for i, (t, f) in enumerate(encoded) if t in COALESCIBLE_BROADCAST_FRAME_TYPES}
        expected = [(t, f) for i, (t, f) in enumerate(encoded)
                    if t not in COALESCIBLE_BROADCAST_FRAME_TYPES or last[daemon._coalesce_frame_key(t, f)] == i]
        assert items == expected
        assert len(items) == 27
        state = daemon._connection_diagnostics[peer]
        coalesced = {t: state.traffic[t]["coalesced"] for t in sorted(EARLY)}
        assert coalesced == {"session.inventory": 78, "working.state": 86, "hosts.stats": 13}
        assert sum(state.queued.values()) == queue.qsize()
        assert all(state.queued[t] == n for t, n in Counter(_conn_bucket(t) for t, _ in items).items())
        assert daemon._client_last_sent_digest.get(peer, {}) == {}
        peer.send_gate.set()
        await _until(lambda: len(peer.sent) == 29 and queue.empty())
        assert peer.sent[2:] == [f for _t, f in expected]
        chats = [f for f in peer.sent if json.loads(f).get("type") == "chat.event"]
        assert chats == [_encode_frame(f) for f in frames if f["type"] == "chat.event"]
        assert peer.close_calls == 0
        receipt = {"checkpoint_203": checkpoint, "first_duplicate_enqueue": violations[0] if violations else None,
                   "duplicate_steps": len(violations), "maximum_pending_after_enqueue": max(depths),
                   "final_pending_before_release": len(items), "coalesced": coalesced,
                   "queue_peak": state.queue_peak, "chat_bytes_and_order_preserved": True,
                   "pressure": [(p["phase"], p["queue_depth"]) for p in _events(caplog, "slow_consumer")]}
    receipt["cleanup"] = {"clients": len(daemon._clients), "writers": len(daemon._client_writer_tasks),
                          "queues": len(daemon._client_send_queues)}
    assert receipt["cleanup"] == {"clients": 0, "writers": 0, "queues": 0}
    print("EPISODE_RECEIPT=" + json.dumps(receipt, sort_keys=True))
    return receipt


def test_recorded_shape_final_compaction_control(caplog):
    """Both baseline and candidate retain newest states and exact event bytes."""
    asyncio.run(_run(caplog))


def test_stalled_reader_never_accumulates_duplicate_replaceable_keys(caplog):
    receipt = asyncio.run(_run(caplog))
    assert receipt["duplicate_steps"] == 0, receipt
    assert receipt["checkpoint_203"]["depth"] == 27
    assert receipt["maximum_pending_after_enqueue"] <= 27
    assert receipt["pressure"] == []


# Additional acceptance cells use the same production transport/diagnostic path.
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import inventory
import server
from inventory import InventoryEmitter
from server import Server
from test_slow_consumer_logging import DiagnosticPeer


def _state_frame(family, version, stream="fixture:s0"):
    frame = {"type": family, "version": version}
    if family == "working.state":
        frame.update(stream_id=stream, working=bool(version % 2))
    elif family == "session.inventory":
        frame["sessions"] = []
    elif family == "hosts.stats":
        frame["hosts"] = []
    return frame


def _configure(daemon, peer):
    daemon._activate_client(peer)
    daemon._client_include_subagents[peer] = True
    daemon._client_events_mode[peer] = "summary"
    daemon._client_work_lanes_v1[peer] = True


def _assert_queue_counts(daemon, peer, queue):
    state = daemon._connection_diagnostics[peer]
    actual = Counter(_conn_bucket(t) for t, _ in queue._queue)
    assert sum(state.queued.values()) == queue.qsize()
    assert all(count == actual[bucket] for bucket, count in state.queued.items())
    return state


@asynccontextmanager
async def _blocked(*, maxsize=256, daemon=None):
    async with _connection(maxsize=maxsize, daemon=daemon) as connection:
        daemon, peer, queue, _handler = connection
        _configure(daemon, peer)
        writer = daemon._client_writer_tasks[peer]
        peer.send_gate = asyncio.Event()
        assert daemon._enqueue(peer, "snapshot", '{"type":"snapshot","fixture":"held"}')
        await _until(lambda: len(peer.attempts) == 2)
        assert queue.empty() and len(peer.sent) == 1
        try:
            yield connection
        finally:
            peer.send_gate.set()
    assert writer.done()
    assert peer not in daemon._clients
    assert peer not in daemon._client_send_queues
    assert peer not in daemon._client_writer_tasks
    assert peer not in daemon._client_inflight_coalescible
    assert peer not in daemon._client_last_sent_digest


@pytest.mark.parametrize("family", sorted(EARLY))
def test_low_depth_identical_and_changed_target_replacement(family):
    async def run():
        async with _blocked() as (daemon, peer, queue, _handler):
            for version in (1, 1, 2, 2, 3):
                await daemon.broadcast(_state_frame(family, version))
                assert list(queue._queue) == [(family, _encode_frame(_state_frame(family, version)))]
                state = _assert_queue_counts(daemon, peer, queue)
                assert daemon._client_last_sent_digest.get(peer, {}) == {}
                assert state.traffic[family]["deduped"] == 0
                assert state.traffic[family]["broadcast_sent"] == 0
            assert state.traffic[family]["broadcast_enqueued"] == 5
            assert state.traffic[family]["coalesced"] == 4
            assert state.queue_peak == 1
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 3)
            assert peer.sent[-1] == _encode_frame(_state_frame(family, 3))
            assert state.traffic[family]["broadcast_sent"] == 1
    asyncio.run(run())


def test_keys_clients_and_late_families_remain_isolated():
    async def run():
        async with _blocked() as (daemon, first, first_queue, _handler):
            async with _blocked(daemon=daemon) as (_, second, second_queue, _handler2):
                for version in (1, 2, 2):
                    for family, stream in (("session.inventory", "fixture:s0"),
                                           ("working.state", "fixture:s0"),
                                           ("working.state", "fixture:s1"),
                                           ("hosts.stats", "fixture:s0")):
                        await daemon.broadcast(_state_frame(family, version, stream))
                assert first_queue.qsize() == second_queue.qsize() == 4
                second_before = list(second_queue._queue)
                replacement = _encode_frame(_state_frame("working.state", 9, "fixture:s0"))
                assert daemon._enqueue(first, "working.state", replacement)
                assert list(second_queue._queue) == second_before
                assert list(first_queue._queue) == [item for item in second_before
                    if not (item[0] == "working.state" and json.loads(item[1])["stream_id"] == "fixture:s0")] + [
                    ("working.state", replacement)]
                late = []
                for family in ("host.status", "schedule.inventory", "limits.update"):
                    for version in (1, 2):
                        item = (family, _encode_frame({"type": family, "host": "fixture-host", "version": version}))
                        late.append(item)
                        assert daemon._enqueue(first, *item)
                # A target-family enqueue must not advance any late family's policy.
                assert daemon._enqueue(first, "hosts.stats", _encode_frame(_state_frame("hosts.stats", 9)))
                assert [item for item in first_queue._queue if item[0] in {x[0] for x in late}] == late
                _assert_queue_counts(daemon, first, first_queue)
                _assert_queue_counts(daemon, second, second_queue)
    asyncio.run(run())


def test_enqueue_refuses_unregistered_or_absent_queue_without_mutation():
    daemon, peer = Server(), DiagnosticPeer()
    queue = asyncio.Queue(maxsize=2)
    daemon._client_send_queues[peer] = queue
    frame = _encode_frame(_state_frame("session.inventory", 1))
    assert daemon._enqueue(peer, "session.inventory", frame) is False
    assert queue.empty()
    daemon._client_send_queues.pop(peer)
    daemon._clients.add(peer)
    assert daemon._enqueue(peer, "session.inventory", frame) is False
    assert daemon._client_last_sent_digest == {}
    daemon._unregister_client(peer)


@pytest.mark.parametrize("family", sorted(EARLY))
def test_targeted_mode_removes_all_legacy_same_key_duplicates_only(family):
    async def run():
        async with _blocked() as (daemon, peer, queue, _handler):
            items = []
            for version in range(3):
                items.extend([
                    (family, _encode_frame(_state_frame(family, version))),
                    ("chat.event", '{"type":"chat.event","text":"same 雪"}'),
                    ("working.state", _encode_frame(_state_frame("working.state", version, "fixture:other"))),
                    ("host.status", _encode_frame({"type": "host.status", "host": "fixture-host", "version": version})),
                ])
            for item in items:
                queue.put_nowait(item)
                daemon._diag_enqueued(peer, queue, item[0])
            incoming = (family, _encode_frame(_state_frame(family, 99)))
            assert daemon._enqueue(peer, *incoming)
            key = (family, "fixture:s0") if family == "working.state" else (family,)
            expected = [item for item in items if daemon._coalesce_frame_key(*item) != key] + [incoming]
            assert list(queue._queue) == expected
            state = _assert_queue_counts(daemon, peer, queue)
            assert state.traffic[family]["coalesced"] == 3
            assert state.traffic["host.status"]["coalesced"] == 0
            if family != "working.state":
                assert state.traffic["working.state"]["coalesced"] == 0
    asyncio.run(run())


def test_working_key_identity_and_malformed_json_stay_unchanged():
    async def run():
        async with _blocked() as (daemon, peer, queue, _handler):
            malformed = [("working.state", "{invalid"), ("working.state", "{invalid")]
            for item in malformed:
                assert daemon._enqueue(peer, *item)
            # Existing valid-key normalization: numeric/string stream IDs share
            # a key; missing/falsey IDs share the empty-string key.
            payloads = [{"stream_id": 7}, {"stream_id": "7"}, {}, {"stream_id": None},
                        {"stream_id": ""}, {"stream_id": 0}, {"stream_id": "fixture:s0"}]
            for version, fields in enumerate(payloads):
                assert daemon._enqueue(peer, "working.state", _encode_frame({"type": "working.state", "version": version, **fields}))
            expected = malformed + [("working.state", _encode_frame({"type": "working.state", "version": n, **payloads[n]}))
                                    for n in (1, 5, 6)]
            assert list(queue._queue) == expected
            assert daemon._evict_superseded_queue_frames(queue) is False
            assert list(queue._queue) == expected
            _assert_queue_counts(daemon, peer, queue)
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
def test_append_only_bytes_and_latest_arrival_order_reach_writer(family):
    async def run():
        async with _blocked() as (daemon, peer, queue, _handler):
            first_chat = ("chat.event", '{ "type": "chat.event", "text": "same 雪 é" }')
            second_chat = ("chat.event", '{"type":"chat.event","seq":2}')
            old = (family, _encode_frame(_state_frame(family, 1)))
            new = (family, _encode_frame(_state_frame(family, 2)))
            prefix = [old, first_chat, new, second_chat]
            append_only = []
            for kind in ("chat.event", "notification", "completion.report", "work_lanes.inventory", "fixture.unknown"):
                # Identical repeated bytes must survive independently, even if
                # their families share the diagnostics 'other' bucket.
                item = (kind, '{ "type": ' + json.dumps(kind) + ', "text": "雪" }')
                append_only.extend([item, item])
            for item in prefix + append_only:
                assert daemon._enqueue(peer, *item)
            expected = [first_chat, new, second_chat] + append_only
            assert list(queue._queue) == expected
            state = _assert_queue_counts(daemon, peer, queue)
            assert all(state.traffic[_conn_bucket(t)]["coalesced"] == 0 for t, _ in append_only)
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 2 + len(expected))
            assert peer.sent[2:] == [frame for _, frame in expected]
            assert queue.empty()
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
def test_delivered_identical_dedup_and_reconnect_reseed(family):
    async def run():
        daemon = Server()
        for _ in range(2):
            async with _connection(daemon=daemon) as (_, peer, queue, _handler):
                _configure(daemon, peer)
                writer = daemon._client_writer_tasks[peer]
                frame = _state_frame(family, 1)
                await daemon.broadcast(frame)
                await _until(lambda: len(peer.sent) == 2)
                key = (family, "fixture:s0") if family == "working.state" else (family,)
                assert daemon._client_last_sent_digest[peer][key] == hash(peer.sent[-1])
                await daemon.broadcast(frame)
                assert queue.empty() and len(peer.sent) == 2
                state = daemon._connection_diagnostics[peer]
                assert state.traffic[family]["broadcast_enqueued"] == 1
                assert state.traffic[family]["broadcast_sent"] == 1
                assert state.traffic[family]["deduped"] == 1
                assert state.traffic[family]["coalesced"] == 0
            assert writer.done()
            assert peer not in daemon._client_last_sent_digest
            assert peer not in daemon._client_inflight_coalescible
        assert not daemon._clients and not daemon._client_send_queues and not daemon._client_writer_tasks
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
@pytest.mark.parametrize("b_queued", [True, False], ids=["queued-b", "inflight-b"])
def test_delivered_a_pending_b_arriving_a_converges(family, b_queued):
    async def run():
        async with _connection() as (daemon, peer, queue, _handler):
            _configure(daemon, peer)
            a, b = _state_frame(family, 1), _state_frame(family, 2)
            await daemon.broadcast(a)
            await _until(lambda: len(peer.sent) == 2)
            peer.send_gate = asyncio.Event()
            if b_queued:
                # A different coalescible key holds the writer, so B is pending.
                await daemon.broadcast(_state_frame("working.state", 99, "fixture:holder"))
                await _until(lambda: len(peer.attempts) == 3)
            await daemon.broadcast(b)
            if b_queued:
                assert list(queue._queue) == [(family, _encode_frame(b))]
            else:
                await _until(lambda: len(peer.attempts) == 3)
                assert queue.empty()
                assert daemon._client_inflight_coalescible[peer] == (family, _encode_frame(b))
            await daemon.broadcast(a)
            assert list(queue._queue) == [(family, _encode_frame(a))]
            state = _assert_queue_counts(daemon, peer, queue)
            assert state.traffic[family]["deduped"] == 0
            assert state.traffic[family]["coalesced"] == int(b_queued)
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 4)
            actual = [json.loads(frame)["version"] for frame in peer.sent[1:]
                      if json.loads(frame)["type"] == family
                      and json.loads(frame).get("stream_id") != "fixture:holder"]
            assert actual == ([1, 1] if b_queued else [1, 2, 1])
            assert not daemon._client_inflight_coalescible
            await daemon.broadcast(a)
            assert len(peer.sent) == 4 and queue.empty()
            assert state.traffic[family]["deduped"] == 1
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
def test_identical_inflight_target_is_not_replaced_or_counted_delivered(family):
    async def run():
        async with _connection() as (daemon, peer, queue, _handler):
            _configure(daemon, peer)
            peer.send_gate = asyncio.Event()
            frame = _state_frame(family, 1)
            await daemon.broadcast(frame)
            await _until(lambda: len(peer.attempts) == 2)
            for _ in range(3):
                await daemon.broadcast(frame)
            assert list(queue._queue) == [(family, _encode_frame(frame))]
            state = _assert_queue_counts(daemon, peer, queue)
            assert state.traffic[family]["coalesced"] == 2
            assert state.traffic[family]["broadcast_sent"] == state.traffic[family]["deduped"] == 0
            assert daemon._client_last_sent_digest.get(peer, {}) == {}
            assert daemon._client_inflight_coalescible[peer] == (family, _encode_frame(frame))
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 3)
            assert peer.sent[1:] == [_encode_frame(frame)] * 2
            assert state.traffic[family]["broadcast_sent"] == 2
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
@pytest.mark.parametrize("capacity", [1, 3, 5])
def test_full_queue_single_same_key_target_makes_room(family, capacity, caplog):
    async def run():
        caplog.set_level(logging.INFO)
        async with _blocked(maxsize=capacity) as (daemon, peer, queue, _handler):
            original = (family, _encode_frame(_state_frame(family, 1)))
            assert daemon._enqueue(peer, *original)
            events = [("chat.event", _encode_frame({"type": "chat.event", "seq": n})) for n in range(capacity - 1)]
            for item in events:
                assert daemon._enqueue(peer, *item)
            assert queue.full()
            pressure_before = list(_events(caplog, "slow_consumer"))
            for version in (2, 3, 4):
                newest = (family, _encode_frame(_state_frame(family, version)))
                assert daemon._enqueue(peer, *newest)
                assert list(queue._queue) == events + [newest]
                assert queue.full() and peer.close_calls == 0
                _assert_queue_counts(daemon, peer, queue)
            # Removing/re-putting inside one synchronous enqueue must not
            # create a recover/enter pair from the temporary capacity-one gap.
            assert _events(caplog, "slow_consumer") == pressure_before
            assert daemon._connection_diagnostics[peer].traffic[family]["coalesced"] == 3
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 2 + capacity)
            assert peer.sent[2:] == [frame for _, frame in events + [newest]]
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
def test_target_with_no_matching_key_preserves_overflow_fallback(family):
    async def run():
        async with _blocked(maxsize=3) as (daemon, peer, queue, _handler):
            late = [("host.status", _encode_frame({"type": "host.status", "host": "fixture-host", "version": n})) for n in (1, 2)]
            event = ("chat.event", '{"type":"chat.event","seq":1}')
            for item in [late[0], event, late[1]]:
                assert daemon._enqueue(peer, *item)
            assert queue.full()
            newest = (family, _encode_frame(_state_frame(family, 1)))
            assert daemon._enqueue(peer, *newest)
            assert list(queue._queue) == [event, late[1], newest]
            state = _assert_queue_counts(daemon, peer, queue)
            assert state.traffic["host.status"]["coalesced"] == 1
            assert peer.close_calls == 0
    asyncio.run(run())


def test_recorded_shape_diagnostics_agree_at_every_enqueue():
    async def run():
        async with _blocked() as (daemon, peer, queue, _handler):
            enqueued = Counter(snapshot=1)
            peak = 1
            for frame in _episode():
                await daemon.broadcast(frame)
                enqueued[_conn_bucket(frame["type"])] += 1
                state = _assert_queue_counts(daemon, peer, queue)
                peak = max(peak, queue.qsize())
                assert state.queue_peak == peak
                for bucket, metrics in state.traffic.items():
                    assert metrics["broadcast_enqueued"] == enqueued[bucket]
                    assert metrics["broadcast_sent"] == metrics["deduped"] == 0
                    if bucket != "snapshot":
                        assert metrics["coalesced"] == enqueued[bucket] - state.queued[bucket]
            assert peak == 27 and state.high_since is None
            assert {family: state.traffic[family]["coalesced"] for family in EARLY} == {
                "session.inventory": 78, "working.state": 86, "hosts.stats": 13}
            expected = list(queue._queue)
            peer.send_gate.set()
            await _until(lambda: len(peer.sent) == 29)
            sent = Counter(_conn_bucket(t) for t, _ in expected)
            sent["snapshot"] += 1
            for bucket, metrics in state.traffic.items():
                assert metrics["broadcast_sent"] == sent[bucket]
            _assert_queue_counts(daemon, peer, queue)
    asyncio.run(run())


def test_compactor_task_accounting_balances_replacements_and_default_mode():
    async def run():
        async with _connection() as (daemon, peer, queue, _handler):
            assert queue._unfinished_tasks == 0
            for n in range(50):
                for family in sorted(EARLY):
                    assert daemon._enqueue(peer, family, _encode_frame(_state_frame(family, n)))
                    assert queue._unfinished_tasks == queue.qsize()
            # Legacy default compaction re-puts survivors, removing only their
            # predecessors' unfinished work, with no writer involved.
            for n in range(4):
                item = ("host.status", _encode_frame({"type": "host.status", "host": "fixture-host", "version": n}))
                assert daemon._enqueue(peer, *item)
            assert daemon._evict_superseded_queue_frames(queue)
            assert queue._unfinished_tasks == queue.qsize() == 4
            while not queue.empty():
                kind, _ = queue.get_nowait()
                daemon._diag_removed(queue, kind)
                queue.task_done()
            assert queue._unfinished_tasks == 0
            await queue.join()  # queue-only control; never join writer-owned work
            _assert_queue_counts(daemon, peer, queue)
    asyncio.run(run())


@pytest.mark.parametrize("family", sorted(EARLY))
def test_failed_inflight_send_does_not_count_delivery_or_leave_owned_writer(family):
    async def run():
        async with _connection() as (daemon, peer, queue, _handler):
            _configure(daemon, peer)
            writer = daemon._client_writer_tasks[peer]
            state = daemon._connection_diagnostics[peer]
            peer.send_gate = asyncio.Event()
            await daemon.broadcast(_state_frame(family, 1))
            await _until(lambda: len(peer.attempts) == 2)
            await daemon.broadcast(_state_frame(family, 2))
            await daemon.broadcast(_state_frame(family, 3))
            assert state.traffic[family]["coalesced"] == 1
            peer.send_error = RuntimeError("synthetic send failure")
            peer.send_gate.set()
            await _until(writer.done)
            await writer
            assert state.writer_failed
            assert state.traffic[family]["broadcast_sent"] == 0
            assert state.traffic[family]["deduped"] == 0
            assert peer not in daemon._client_last_sent_digest
            assert peer not in daemon._client_inflight_coalescible
            assert peer not in daemon._client_send_queues
            assert peer not in daemon._client_writer_tasks
    asyncio.run(run())


@pytest.mark.parametrize("bypass", ["working-start", "working-idle", "membership-shrink", "immediate"])
def test_inventory_emitter_urgent_bypasses_replace_pending_state(monkeypatch, bypass):
    async def run():
        clock = [100.0]
        monkeypatch.setattr(inventory, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        async with _blocked() as (daemon, peer, queue, _handler):
            # Full projection makes the expected emitter payload byte-exact.
            daemon._client_events_mode[peer] = "full"
            rows = [{"stream_id": "fixture:s0", "working": bypass == "working-idle", "title": "first"}]
            if bypass == "membership-shrink":
                rows.append({"stream_id": "fixture:s1", "working": False})
            sessions = SimpleNamespace(list_open=lambda: [dict(row) for row in rows])
            emitter = InventoryEmitter(sessions, daemon.broadcast, min_interval_s=3600)
            try:
                assert await emitter.emit_if_changed()
                first = list(queue._queue)
                clock[0] += 1
                if bypass == "working-start":
                    rows[0]["working"] = True
                elif bypass == "working-idle":
                    rows[0]["working"] = False
                elif bypass == "membership-shrink":
                    rows.pop()
                else:
                    rows[0]["title"] = "second"
                assert await emitter.emit_if_changed(immediate=bypass == "immediate")
                expected = ("session.inventory", _encode_frame({"type": "session.inventory", "sessions": rows}))
                assert list(queue._queue) == [expected] and list(queue._queue) != first
                assert emitter._flush_task is None
                state = _assert_queue_counts(daemon, peer, queue)
                assert state.traffic["session.inventory"]["broadcast_enqueued"] == 2
                assert state.traffic["session.inventory"]["coalesced"] == 1
                assert not await emitter.emit_if_changed(immediate=True), "signature dedup must remain active"
                peer.send_gate.set()
                await _until(lambda: len(peer.sent) == 3)
                assert peer.sent[-1] == expected[1]
            finally:
                task = emitter._flush_task
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    assert task.done()
    asyncio.run(run())


def test_inventory_ordinary_churn_owns_one_deferred_flush(monkeypatch):
    async def run():
        clock = [100.0]
        monkeypatch.setattr(inventory, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        async with _blocked() as (daemon, peer, queue, _handler):
            daemon._client_events_mode[peer] = "full"
            row = {"stream_id": "fixture:s0", "working": False, "title": "first"}
            emitter = InventoryEmitter(SimpleNamespace(list_open=lambda: [dict(row)]), daemon.broadcast, min_interval_s=3600)
            owned_task = None
            try:
                assert inventory.DEFAULT_INVENTORY_MIN_INTERVAL_S == 2.0
                assert await emitter.emit_if_changed()
                baseline = list(queue._queue)
                clock[0] += 1
                row["title"] = "second"
                assert not await emitter.emit_if_changed()
                owned_task = emitter._flush_task
                assert owned_task is not None and not owned_task.done()
                row["title"] = "final"
                assert not await emitter.emit_if_changed()
                assert emitter._flush_task is owned_task
                assert list(queue._queue) == baseline
                # Cancellation is owned and awaited; immediate emission still
                # replaces the pending state without waiting for the long timer.
                assert await emitter.emit_if_changed(immediate=True)
                assert list(queue._queue) == [("session.inventory", _encode_frame({"type": "session.inventory", "sessions": [row]}))]
                assert emitter._flush_task is owned_task
            finally:
                if owned_task is not None:
                    owned_task.cancel()
                    await asyncio.gather(owned_task, return_exceptions=True)
                    assert owned_task.done()
    asyncio.run(run())
