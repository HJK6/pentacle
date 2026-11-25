"""Wire handshake and recovery on a real daemon connection."""

from tests.contract.harness import unique


def test_c1_handshake_and_errors(chat):
    """C1: docs/chat_protocol.md, server.py:3291-3300 (unknown verb)."""
    chat.start()
    full = chat.client()
    assert full.receive().body["type"] == "welcome"
    request_id = unique("hello")
    full.send({"type": "hello", "request_id": request_id,
               "subscribe": {"include_subagents": True, "events_mode": "full"}})
    reply = full.receive().body
    assert (reply.get("type"), reply.get("request_id")) == ("hello", request_id), (
        "C1 expected the correlated hello immediately after welcome", reply,
    )
    snapshot = full.receive().body
    assert snapshot["type"] == "snapshot", ("C1 expected snapshot immediately after hello", snapshot)
    assert snapshot["sessions"] == [], snapshot

    rpc = chat.client()
    assert rpc.receive().body["type"] == "welcome"
    ready = rpc.call("hello", subscribe={"mode": "rpc"})
    assert ready["type"] == "ready", ready
    assert rpc.call("list_sessions")["type"] == "list_sessions.ok"
    assert all(frame.body["type"] != "snapshot" for frame in rpc.frames), "C1 RPC hello sent a snapshot"

    for raw, code in (("{", "bad_json"), ("[]", "bad_envelope")):
        start = len(full.frames)
        full.socket.send(raw)
        error = full.until(lambda body: body.get("type") == "protocol.error",
                           start=start, description=code).body
        assert error["error_code"] == code, error
        assert full.call("list_sessions")["type"] == "list_sessions.ok"

    verb = unique("unknown")
    error = full.call(verb)
    assert (error["type"], error["error_code"]) == (f"{verb}.error", "unsupported_in_v2"), (
        "C1 unknown verb error", error,
    )
    assert full.call("list_sessions")["type"] == "list_sessions.ok"
