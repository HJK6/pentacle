"""Coverage for per-client delivered-time byte-identical deduplication.

The server suppresses a coalescible broadcast
frame that is byte-identical to the one this client was last *delivered*. The
digest is recorded in the writer loop AFTER a successful `websocket.send` — never
at enqueue — so a frame coalesced-away or dropped before delivery is never
suppressed and the client always converges to the latest visible state.

The tests cover two invariants:
  * append-only `chat.event` is never deduped;
  * a frame evicted/coalesced-away before delivery does NOT poison
    the digest, so an identical frame broadcast later is still delivered
    (convergence / no lost update).
"""

from __future__ import annotations

import asyncio
import json

import server
from server import Server


class SendSpy:
    """A websocket that records what actually reached the wire.

    `gate`, when set to an unset Event, blocks each send so a frame can be held
    in-flight (accepted by the writer loop, not yet delivered) for the
    convergence tests.
    """

    remote_address = ("10.0.0.0", 5555)

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.gate: asyncio.Event | None = None

    async def send(self, frame: str) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(frame)

    async def close(self, **_kwargs: object) -> None:
        pass


def _sub(daemon: Server, ws: SendSpy, *, events_mode: str = "full") -> None:
    daemon._client_include_subagents[ws] = True
    daemon._client_events_mode[ws] = events_mode


def _attach_no_writer(daemon: Server, ws: SendSpy) -> asyncio.Queue:
    """Wire a client's queue/lock WITHOUT starting the writer loop, so nothing
    is ever 'delivered' and the delivered-digest stays empty."""
    daemon._clients.add(ws)
    queue: asyncio.Queue = asyncio.Queue(maxsize=server.CLIENT_SEND_QUEUE_MAX)
    daemon._client_send_queues[ws] = queue
    daemon._client_send_locks[ws] = asyncio.Lock()
    _sub(daemon, ws)
    return queue


async def _settle(daemon: Server, ws: SendSpy) -> None:
    """Let the writer loop drain the queue and record delivered digests."""
    queue = daemon._client_send_queues.get(ws)
    for _ in range(50):
        await asyncio.sleep(0.005)
        if queue is None or queue.qsize() == 0:
            await asyncio.sleep(0.005)
            return


def _host_status(load: int, host: str = "hosta") -> dict:
    return {"type": "host.status", "host": host, "load": load}


def _loads(ws: SendSpy) -> list[int]:
    return [json.loads(s)["load"] for s in ws.sent]


def test_byte_identical_suppressed_only_after_delivery_and_changed_always_sent() -> None:
    async def run() -> None:
        daemon = Server()
        ws = SendSpy()
        daemon._register_client(ws)  # starts the writer loop
        _sub(daemon, ws)

        await daemon.broadcast(_host_status(1))
        await _settle(daemon, ws)
        assert _loads(ws) == [1]  # first frame delivered
        key = ("host.status", "hosta")
        assert daemon._client_last_sent_digest[ws][key] == hash(ws.sent[-1])

        await daemon.broadcast(_host_status(1))  # byte-identical
        await _settle(daemon, ws)
        assert _loads(ws) == [1]  # suppressed: no new visible state

        await daemon.broadcast(_host_status(2))  # changed
        await _settle(daemon, ws)
        assert _loads(ws) == [1, 2]  # a changed frame is always delivered

        await daemon.broadcast(_host_status(2))  # byte-identical again
        await _settle(daemon, ws)
        assert _loads(ws) == [1, 2]

    asyncio.run(run())


def test_enqueue_never_suppresses_only_delivery_does() -> None:
    """Two byte-identical frames broadcast before EITHER is delivered must BOTH
    enqueue — suppression is gated on delivery, not enqueue. This is the core
    invariant the enqueue-time digest violated (delivery invariant)."""
    async def run() -> None:
        daemon = Server()
        ws = SendSpy()
        queue = _attach_no_writer(daemon, ws)

        await daemon.broadcast(_host_status(1))
        await daemon.broadcast(_host_status(1))  # identical, nothing delivered yet

        assert queue.qsize() == 2  # neither suppressed
        assert daemon._client_last_sent_digest.get(ws, {}) == {}  # no digest recorded

    asyncio.run(run())


def test_evicted_before_delivery_does_not_poison_digest_client_converges() -> None:
    """`_evict_superseded_queue_frames` collapses a superseded coalescible
    backlog. An evicted frame is not delivered, so it must not record a digest;
    an identical frame broadcast afterwards is still enqueued (delivered later),
    so the client converges instead of silently losing the update."""
    async def run() -> None:
        daemon = Server()
        ws = SendSpy()
        queue = _attach_no_writer(daemon, ws)

        # Two same-key host.status frames queued; the newest supersedes.
        queue.put_nowait(("host.status", json.dumps(_host_status(1))))
        queue.put_nowait(("host.status", json.dumps(_host_status(2))))
        assert daemon._evict_superseded_queue_frames(queue) is True
        assert [json.loads(f)["load"] for _t, f in list(queue._queue)] == [2]  # v1 evicted
        assert daemon._client_last_sent_digest.get(ws, {}) == {}  # eviction records nothing

        # The evicted state (load=1) is broadcast again -> must NOT be suppressed
        # (it was never delivered) -> it converges into the queue.
        await daemon.broadcast(_host_status(1))
        assert [json.loads(f)["load"] for _t, f in list(queue._queue)] == [2, 1]

    asyncio.run(run())


def test_in_flight_frame_not_yet_delivered_is_not_suppressed() -> None:
    """A frame accepted by the writer loop but held in-flight (send blocked) has
    not been delivered, so an identical broadcast is still enqueued; once the
    send unblocks the client converges (both the held frame and any later state
    reach the wire)."""
    async def run() -> None:
        daemon = Server()
        ws = SendSpy()
        ws.gate = asyncio.Event()  # hold sends
        daemon._register_client(ws)
        _sub(daemon, ws)

        await daemon.broadcast(_host_status(1))
        await asyncio.sleep(0.01)  # writer picks up frame, blocks in send()
        assert ws.sent == []  # nothing delivered yet
        assert daemon._client_last_sent_digest.get(ws, {}) == {}

        await daemon.broadcast(_host_status(1))  # identical, still nothing delivered
        ws.gate.set()  # release
        await _settle(daemon, ws)
        assert _loads(ws) == [1, 1]  # both delivered -> no lost update / converged

    asyncio.run(run())


async def _assert_latest_state_after_pending_change(*, b_queued: bool) -> None:
    """Assert A-delivered/B-pending/A-arriving finishes at newest A."""
    daemon = Server()
    ws = SendSpy()
    daemon._register_client(ws)
    _sub(daemon, ws)

    # First A genuinely reaches the wire and therefore owns the delivered
    # digest.  Everything below is the post-delivery interleaving.
    await daemon.broadcast(_host_status(1))
    await _settle(daemon, ws)
    assert _loads(ws) == [1]

    ws.gate = asyncio.Event()
    if b_queued:
        # Hold a DIFFERENT key in send() so B remains in the queue.
        await daemon.broadcast(_host_status(99, host="hostc"))
        await asyncio.sleep(0.01)
    await daemon.broadcast(_host_status(2))  # B: same hosta key, differs from A
    if b_queued:
        queued = list(daemon._client_send_queues[ws]._queue)
        assert ("host.status", json.dumps(_host_status(2))) in queued
    else:
        # Let the writer take B and block in websocket.send: B is in-flight.
        await asyncio.sleep(0.01)
        assert daemon._client_send_queues[ws].qsize() == 0

    await daemon.broadcast(_host_status(1))  # newest state is A again
    ws.gate.set()
    await _settle(daemon, ws)

    host_a_loads = [json.loads(frame)["load"] for frame in ws.sent
                  if json.loads(frame).get("host") == "hosta"]
    assert host_a_loads == [1, 2, 1]


def test_delivered_a_queued_b_arriving_a_delivers_final_a() -> None:
    """Queued B must not let the old delivered-A digest suppress final A."""
    asyncio.run(_assert_latest_state_after_pending_change(b_queued=True))


def test_delivered_a_inflight_b_arriving_a_delivers_final_a() -> None:
    """In-flight B must not let the old delivered-A digest suppress final A."""
    asyncio.run(_assert_latest_state_after_pending_change(b_queued=False))


def test_reconnect_reseeds_digest() -> None:
    """`_unregister_client` clears the digest, so a reconnecting client is sent a
    full frame (the hello snapshot also reseeds)."""
    async def run() -> None:
        daemon = Server()
        ws = SendSpy()
        daemon._register_client(ws)
        _sub(daemon, ws)
        await daemon.broadcast(_host_status(1))
        await _settle(daemon, ws)
        assert _loads(ws) == [1]

        daemon._unregister_client(ws)
        assert ws not in daemon._client_last_sent_digest

        ws2 = SendSpy()
        daemon._register_client(ws2)
        _sub(daemon, ws2)
        await daemon.broadcast(_host_status(1))  # identical state, fresh client
        await _settle(daemon, ws2)
        assert _loads(ws2) == [1]  # full frame on reconnect, not suppressed

    asyncio.run(run())


def test_chat_event_is_never_deduped() -> None:
    """Append-only `chat.event` is not coalescible: two identical events are both
    delivered (delivery invariant — inventory can never restore a lost event)."""
    async def run() -> None:
        daemon = Server()
        ws = SendSpy()
        daemon._register_client(ws)
        _sub(daemon, ws)

        event = {"type": "chat.event", "event": {"stream_id": "hosta:v2-x", "seq": 1}}
        await daemon.broadcast(event)
        await daemon.broadcast(dict(event))  # byte-identical
        await _settle(daemon, ws)

        assert len(ws.sent) == 2  # both delivered, never suppressed
        assert ws not in daemon._client_last_sent_digest  # chat.event records no digest

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - manual smoke
    test_byte_identical_suppressed_only_after_delivery_and_changed_always_sent()
    test_enqueue_never_suppresses_only_delivery_does()
    test_evicted_before_delivery_does_not_poison_digest_client_converges()
    test_in_flight_frame_not_yet_delivered_is_not_suppressed()
    test_reconnect_reseeds_digest()
    test_chat_event_is_never_deduped()
    print("ok")
