"""The U4 protocol failure must retain safe evidence without becoming a pass."""
import asyncio
import json

import pytest

from helpers import u4_process_harness as harness


def test_matching_unexpected_reply_is_retained_and_still_fails():
    class Socket:
        async def send(self, frame):
            assert json.loads(frame) == {"type": "ping", "request_id": "probe"}

        async def recv(self):
            return json.dumps({"type": "error", "request_id": "probe",
                               "error_code": "busy", "message": "secret-canary" * 1000,
                               "token": "secret-canary", "retryable": True,
                               **{f"private-{n}": "secret-canary" for n in range(100)}})

    with pytest.raises(RuntimeError, match="^not_pong$") as caught:
        asyncio.run(harness.request(Socket(), "probe"))
    evidence = harness.error_evidence(caught.value)
    assert evidence["message"] == "not_pong"
    frame = evidence["unexpected_reply"]
    assert frame["type"] == "error"
    assert frame["body"]["error_code"] == "busy"
    assert frame["body"]["retryable"] is True
    encoded = json.dumps(frame)
    assert len(encoded) <= 1024
    assert "secret-canary" not in encoded and "private-" not in encoded


def test_unrecognized_strings_are_redacted_and_pong_still_passes():
    frame = harness.unexpected_reply_evidence({"type": "secret-canary", "error_code": "secret-canary"})
    assert "secret-canary" not in json.dumps(frame)

    class Socket:
        def __init__(self):
            self.replies = iter([{"type": "error", "request_id": "other"},
                                 {"type": "pong", "request_id": "probe"}])

        async def send(self, _frame):
            pass

        async def recv(self):
            return json.dumps(next(self.replies))

    assert asyncio.run(harness.request(Socket(), "probe"))["latency_s"] >= 0


def test_declared_ping_admission_failure_keeps_exact_safe_enums():
    class Socket:
        async def send(self, frame):
            assert json.loads(frame) == {"type": "ping", "request_id": "probe"}

        async def recv(self):
            return json.dumps({"type": "ping.error", "request_id": "probe",
                               "error_code": "server_overloaded", "retryable": True,
                               "message": "secret-canary", "token": "secret-canary"})

    with pytest.raises(RuntimeError, match="^not_pong$") as caught:
        asyncio.run(harness.request(Socket(), "probe"))
    frame = harness.error_evidence(caught.value)["unexpected_reply"]
    assert frame["type"] == "ping.error"
    assert frame["body"]["error_code"] == "server_overloaded"
    assert frame["body"]["retryable"] is True
    assert "secret-canary" not in json.dumps(frame)
    assert len(json.dumps(frame)) <= 1024


@pytest.mark.parametrize("held_responded,replies,reason,expected_sends", [
    (False, [{"type": "ping.error", "request_id": "held-main", "error_code": "server_overloaded", "retryable": True}], "held_probe_not_pong", 0),
    (False, [{"type": "pong", "request_id": "held-main"}, {"type": "ping.error", "request_id": "after-main", "error_code": "server_overloaded", "retryable": True}], "not_pong", 1),
    (True, [{"type": "ping.error", "request_id": "after-main", "error_code": "server_overloaded", "retryable": True}], "not_pong", 1),
])
def test_post_main_release_keeps_strict_correlated_error_capture(held_responded, replies, reason, expected_sends):
    class Socket:
        def __init__(self):
            self.replies = iter(replies)
            self.sent = []

        async def send(self, frame):
            self.sent.append(json.loads(frame))

        async def recv(self):
            return json.dumps(next(self.replies))

    socket = Socket()
    with pytest.raises(RuntimeError, match="^" + reason + "$") as caught:
        asyncio.run(harness.post_main_release_ping(socket, held_responded))
    captured = harness.error_evidence(caught.value)["unexpected_reply"]
    assert captured["type"] == "ping.error"
    assert captured["body"]["error_code"] == "server_overloaded"
    assert captured["body"]["retryable"] is True
    assert len(socket.sent) == expected_sends
    assert all(frame == {"type": "ping", "request_id": "after-main"} for frame in socket.sent)


@pytest.mark.parametrize("held_responded", [False, True])
def test_post_main_release_preserves_seen_and_unseen_prior_pong(held_responded):
    class Socket:
        def __init__(self):
            self.replies = iter(([{"type": "event", "request_id": "unrelated"},
                                  {"type": "pong", "request_id": "held-main"}]
                                 if not held_responded else [])
                                + [{"type": "pong", "request_id": "after-main"}])
            self.sent = []
            self.prior_seen = held_responded

        async def send(self, frame):
            assert self.prior_seen
            self.sent.append(json.loads(frame))

        async def recv(self):
            reply = next(self.replies)
            if reply.get("request_id") == "held-main":
                self.prior_seen = True
            return json.dumps(reply)

    socket = Socket()
    assert asyncio.run(harness.post_main_release_ping(socket, held_responded))["latency_s"] >= 0
    assert socket.sent == [{"type": "ping", "request_id": "after-main"}]


def test_post_main_release_pending_drain_is_bounded_without_sending_new_probe():
    class Socket:
        def __init__(self):
            self.sent = []
            self.cancelled = False

        async def send(self, frame):
            self.sent.append(json.loads(frame))

        async def recv(self):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    socket = Socket()
    with pytest.raises(TimeoutError):
        asyncio.run(harness.post_main_release_ping(socket, False))
    assert socket.cancelled
    assert socket.sent == []
