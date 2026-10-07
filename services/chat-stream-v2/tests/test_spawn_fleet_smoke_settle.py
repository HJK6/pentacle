"""The fleet smoke's authoritative event snapshot must wait for the turn to settle.

Claude appends a SYSTEM turn-duration record ("Worked for 2s") a few
milliseconds after the final reply, in the same ingest batch. A snapshot taken
between the two made the reconnect replay report that record as unexpected and
the final as not-last, failing the event gate on a healthy daemon (production
smoke, 2026-10-07, seqs 455899162-455899164).
"""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import spawn_fleet_smoke


STREAM = "thoth:v2-fleet-smoke-claude-fixture"
USER = {"stream_id": STREAM, "daemon_seq": 101, "kind": "USER", "text": "Reply exactly MARKER"}
FINAL = {"stream_id": STREAM, "daemon_seq": 102, "kind": "ASSIST_TEXT", "text": "MARKER"}
SYSTEM = {"stream_id": STREAM, "daemon_seq": 103, "kind": "SYSTEM", "text": "Worked for 2s"}


class FakeSocket:
    """Answers the validate_session RPCs; SYSTEM commits after the first snapshot."""

    def __init__(self, churn=False):
        self.queue = [{"type": "chat.event", "event": USER}, {"type": "chat.event", "event": FINAL}]
        self.snapshots = 0
        self.churn = churn

    def send(self, text):
        request = json.loads(text)
        kind, rid = request["type"], request["request_id"]
        if kind == "request_stream_events":
            self.snapshots += 1
            events = [USER, FINAL] if self.snapshots == 1 else [USER, FINAL, SYSTEM]
            if self.churn:
                # A stream that never stops growing never settles.
                events = [USER, FINAL] + [dict(SYSTEM, daemon_seq=200 + i) for i in range(self.snapshots)]
            self.queue.append({"type": "request_stream_events.ok", "request_id": rid, "events": events})
            if self.snapshots == 1:
                # The trailing record commits right after the first read.
                self.queue.append({"type": "chat.event", "event": SYSTEM})
        elif kind == "list_sessions":
            self.queue.append({"type": "list_sessions.ok", "request_id": rid,
                               "active": [{"stream_id": STREAM, "working": False, "bootstrap_state": "ready"}]})
        elif kind == "asset.list":
            self.queue.append({"type": "asset.list.ok", "request_id": rid, "assets": []})
        elif kind == "ping":
            self.queue.append({"type": "pong", "request_id": rid})

    def recv(self, timeout=None):
        if not self.queue:
            raise TimeoutError("fake socket idle")
        return json.dumps(self.queue.pop(0))


@pytest.fixture
def validate(monkeypatch, request):
    socket = FakeSocket(churn=getattr(request, "param", False))

    @contextmanager
    def connection(_url, _token_path, _timeout):
        yield SimpleNamespace(snapshot=None, socket=socket)

    monkeypatch.setattr(spawn_fleet_smoke, "authenticated_operator_connection", connection)
    monkeypatch.setattr(spawn_fleet_smoke, "desktop_handshake_fetch_reply",
                        lambda *_a, **_k: {"events": [USER, FINAL, SYSTEM]})
    registry = SimpleNamespace(configure_runtime=lambda _snapshot: None)

    def run():
        with spawn_fleet_smoke._operator_connection("ws://fixture", None, 2.0, registry) as closures:
            _rpc, _ready, wait_event, _register, close_owned = closures
            wait_event(STREAM, "MARKER")
            return close_owned.validate(STREAM, "MARKER")
    return socket, run


def test_trailing_system_record_after_final_does_not_fail_event_gate(validate):
    socket, run = validate
    metrics = run()
    assert metrics["event"]["passed"] is True, metrics["event"]
    assert metrics["event"]["unexpected_seqs"] == []
    assert metrics["event"]["final_seq"] == SYSTEM["daemon_seq"]
    assert metrics["event"]["initial"]["passed"] is True
    assert socket.snapshots >= 2


@pytest.mark.parametrize("validate", [True], indirect=True)
def test_snapshot_that_never_settles_fails_inside_the_bound(validate, monkeypatch):
    monkeypatch.setattr(spawn_fleet_smoke, "EVENT_SETTLE_LIMIT_S", 0.6)
    socket, run = validate
    started = spawn_fleet_smoke.time.monotonic()
    with pytest.raises(RuntimeError, match="did not settle within 0.6s"):
        run()
    assert spawn_fleet_smoke.time.monotonic() - started < 0.6 + 0.3
    assert socket.snapshots >= 2


def test_row_after_the_bound_is_never_accepted(validate, monkeypatch):
    """QA da793a8d reproduction: with a bound shorter than one interval, the
    second (post-bound) snapshot must not be used."""
    monkeypatch.setattr(spawn_fleet_smoke, "EVENT_SETTLE_LIMIT_S", 0.01)
    socket, run = validate
    with pytest.raises(RuntimeError, match="did not settle"):
        run()
    assert socket.snapshots == 1
