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

    # One subscribed socket pauses reading once its client queue fills. The
    # prelude exceeds the small receive window and precedes the overflow burst.
    prelude = [burst.record("assistant", f"{i:03d} " + "a" * 65536) for i in range(24)]
    burst.append(*prelude)

    def latest_stamp():
        tail = chat.inspect(healthy, burst.stream_id, event_tail=1)["recent_events"]
        return event_stamp(tail[-1]) if tail else None

    observed(latest_stamp, lambda stamp: stamp == (prelude[-1]["uuid"], 0),
             description="ingested pressure prelude", timeout=5)
    flood = [burst.record("assistant", f"{i:03d} " + "b" * 8192) for i in range(320)]
    start = len(healthy.frames)
    request_id, message = unique("send"), unique("progress")
    burst.append(*flood)
    reply = healthy.call("send", request_id=request_id, text=message, to_stream_id=receiver.stream_id)
    expect_landed(reply, receiver.stream_id, request_id)
    event = healthy.event(receiver.stream_id, start=start,
                          match=lambda event: event["kind"] == "USER" and event["text"] == message,
                          description="healthy client live event during pressure")
    assert event["request_id"] == request_id
    assert [entry["text"] for entry in chat.inputs(receiver)] == [message]
    assert not healthy.events_since(burst.stream_id, start)

    # Resume reads to observe the actual protocol close. A normal disconnect or
    # failure to fill the daemon's queue cannot satisfy this assertion.
    transport.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
    deadline = time.monotonic() + 5
    with pytest.raises(ConnectionClosed) as closed:
        while (remaining := deadline - time.monotonic()) > 0:
            slow.receive(timeout=remaining)
    received = closed.value.rcvd
    assert received is not None and (received.code, received.reason) == (1011, "slow_consumer"), (
        "C12 blocked subscriber did not receive the slow-consumer close", closed.value,
    )
    assert healthy.call("ping")["type"] == "pong"
