"""Regression coverage for client-attributed slow-consumer drops."""

from __future__ import annotations

import asyncio
import json
import logging

import server
from server import Server


class Socket:
    remote_address = ("10.0.0.0", 9911)

    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self, **_kwargs: object) -> None:
        self.close_calls += 1


def test_slow_consumer_drop_log_names_the_client(caplog) -> None:
    daemon = Server()
    client = Socket()
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait(("event", "{}"))
    daemon._clients.add(client)
    daemon._client_send_queues[client] = queue
    daemon._client_identities[client] = "public-client"
    daemon._drop_slow_consumer = lambda _client: None  # type: ignore[method-assign]

    caplog.set_level(logging.WARNING, logger=server.log.name)
    assert daemon._enqueue(client, "reply", "{}") is False

    assert "slow_consumer overflow" in caplog.text
    assert "client=public-client" in caplog.text
    assert "peer=('10.0.0.0', 9911)" in caplog.text


def test_depth_warning_at_80pct_names_client_and_peer(caplog) -> None:
    daemon = Server()
    client = Socket()
    queue = asyncio.Queue(maxsize=server.CLIENT_SEND_QUEUE_MAX)
    daemon._clients.add(client)
    daemon._client_send_queues[client] = queue
    daemon._client_identities[client] = "public-client"
    caplog.set_level(logging.WARNING, logger=server.log.name)

    for index in range(int(server.CLIENT_SEND_QUEUE_MAX * server.CLIENT_SEND_QUEUE_WARN_RATIO)):
        assert daemon._enqueue(client, "chat.event", json.dumps({"type": "chat.event", "seq": index}))

    assert "slow_consumer queue depth=204/256" in caplog.text
    assert "client=public-client" in caplog.text
    assert "peer=('10.0.0.0', 9911)" in caplog.text


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
