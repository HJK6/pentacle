"""A stalled fixture subscriber stays isolated from a healthy client."""

import socket
import time

import pytest
from websockets.exceptions import ConnectionClosed

from tests.contract.harness import event_stamp, expect_landed, observed, unique


def test_c12_slow_consumer_isolation(chat):
    """C12: README.md live writer queue; server.py:2214-2270 (1011 slow_consumer)."""
    chat.start()
    healthy = chat.client()
    healthy.subscribe(include_subagents=False)
    receiver = chat.spawn(healthy, unique("receiver"), visibility="visible")
    burst = chat.spawn(healthy, unique("burst"), visibility="hidden")

    transport = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    chat.stack.callback(transport.close)
    transport.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    transport.settimeout(5)
    transport.connect(("127.0.0.1", chat.daemon.port))
    slow = chat.client(sock=transport, max_queue=1, compression=None)
    slow.subscribe()

    # Fill the receive window with large frames while staying below the daemon's
    # frame-count bound. Leave the subscriber's application reader paused.
    prelude = [burst.record("assistant", f"{i:03d} " + "a" * 126976) for i in range(64)]
    burst.append(*prelude)

    def latest_stamp():
        tail = chat.inspect(healthy, burst.stream_id, event_tail=1)["recent_events"]
        return event_stamp(tail[-1]) if tail else None

    prelude_stamp = (prelude[-1]["uuid"], 0)
    observed(latest_stamp, lambda stamp: stamp == prelude_stamp,
             description="ingested below-overflow backlog", timeout=5)

    def held_pings(*, active_only=True):
        status = healthy.call("daemon.stats")
        assert status["type"] == "daemon.stats.ok", status
        control = status["stats"]["admission"]["pools"]["control"]
        return {row["connection_id"] for row in control["connections"]
                if "ping" in row["held_verbs"] and (row["active"] or not active_only)}

    # A held ping on the still-active slow socket witnesses the blocked writer
    # without reading from that socket or depending on a delayed close frame.
    slow.send({"type": "ping", "request_id": unique("stalled-ping")})
    stalled = observed(held_pings, lambda peers: len(peers) == 1,
                       description="ping held on active stalled subscriber", timeout=5)
    start = len(healthy.frames)
    request_id, message = unique("send"), unique("progress")
    reply = healthy.call("send", request_id=request_id, text=message, to_stream_id=receiver.stream_id)
    expect_landed(reply, receiver.stream_id, request_id)
    event = healthy.event(receiver.stream_id, start=start,
                          match=lambda event: event["kind"] == "USER" and event["text"] == message,
                          description="healthy client live event during pressure")
    assert event["request_id"] == request_id
    assert [entry["text"] for entry in chat.inputs(receiver)] == [message]
    assert not healthy.events_since(burst.stream_id, start)
    assert held_pings() == stalled, "C12 stalled subscriber was evicted before healthy completion"
    assert latest_stamp() == prelude_stamp

    flood = [burst.record("assistant", f"{i:03d} " + "b" * 8192) for i in range(320)]
    burst.append(*flood)
    observed(latest_stamp, lambda stamp: stamp == (flood[-1]["uuid"], 0),
             description="complete pressure burst with the subscriber still stalled", timeout=5)

    # Resume reads to observe the actual protocol close. A normal disconnect or
    # failure to fill the daemon's queue cannot satisfy this assertion.
    transport.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    deadline = time.monotonic() + 5
    try:
        with pytest.raises(ConnectionClosed) as closed:
            while (remaining := deadline - time.monotonic()) > 0:
                slow.receive(timeout=remaining)
    except TimeoutError as exc:
        pytest.fail(f"C12 pressure did not close the stalled socket after "
                    f"{len(slow.events_since(burst.stream_id, 0))} drained events: {exc}")
    received = closed.value.rcvd
    assert received is not None and (received.code, received.reason) == (1011, "slow_consumer"), (
        "C12 blocked subscriber did not receive the slow-consumer close", closed.value,
    )
    # Close can reach the peer before its held ping releases the shared credit.
    observed(lambda: held_pings(active_only=False), lambda peers: not peers,
             description="stalled ping credit released after close", timeout=5)
    assert healthy.call("ping")["type"] == "pong"
