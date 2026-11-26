"""An RPC-only operator connection can opt out of the hello snapshot.

Journey: `authenticated_operator_connection` against an owned raw loopback
WebSocket peer that scripts the daemon's welcome, `ready` and `snapshot`
frames, then one RPC. Subject: the `snapshot` option of the connection.
Control: the default still sends no opt-out and waits past `ready` for the
snapshot. Runner: Python 3.13 and pytest; barriers only, the handshake
timeout is a stuck detector.
"""
import threading
import time

from tools.live_window import core
from tests.test_live_window_handshake_reproof import RawPeer

WELCOME = {"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": time.time() + 600}}}
READY = {"type": "ready", "snapshot": False, "events_mode": "summary"}
SNAPSHOT = {"type": "snapshot", "sessions": []}
PONG = {"type": "pong", "request_id": "r1"}


def recording_hello(monkeypatch) -> list[dict]:
    """The real hello's subscribe shape, without a credential file."""
    monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {
        "type": "hello", "subscribe": {"include_subagents": True, "events_mode": "summary"}})
    sent: list[dict] = []
    real_send = core._OperatorConnection._send

    def send(self, frame):
        sent.append(frame)
        return real_send(self, frame)

    monkeypatch.setattr(core._OperatorConnection, "_send", send)
    return sent


def test_default_still_waits_for_and_keeps_the_snapshot(tmp_path, monkeypatch):
    sent = recording_hello(monkeypatch)
    peer = RawPeer([("send", WELCOME), ("read",), ("send", READY), ("send", SNAPSHOT),
                    ("read",), ("send", PONG)])
    try:
        with core.authenticated_operator_connection(peer.url, tmp_path / "token.json", 5.0) as connection:
            assert connection.snapshot == SNAPSHOT
            assert connection.rpc({"type": "ping", "request_id": "r1"}) == PONG
    finally:
        peer.stop()
    assert sent[0]["subscribe"] == {"include_subagents": True, "events_mode": "summary"}


def test_opt_out_sends_the_flag_never_waits_for_a_snapshot_and_rpc_works(tmp_path, monkeypatch):
    sent = recording_hello(monkeypatch)
    entered = threading.Event()
    # After `ready` the peer sends nothing until the client has entered: a
    # client still waiting for a snapshot would stall here until its deadline.
    peer = RawPeer([("send", WELCOME), ("read",), ("send", READY), ("wait", entered),
                    ("read",), ("send", PONG)])
    try:
        with core.authenticated_operator_connection(peer.url, tmp_path / "token.json", 5.0,
                                                    snapshot=False) as connection:
            entered.set()
            assert connection.snapshot is None
            assert connection.rpc({"type": "ping", "request_id": "r1"}) == PONG
    finally:
        entered.set()
        peer.stop()
    assert sent[0]["subscribe"] == {"include_subagents": True, "events_mode": "summary", "snapshot": False}
