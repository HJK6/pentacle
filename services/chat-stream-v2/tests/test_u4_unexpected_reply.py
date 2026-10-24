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
