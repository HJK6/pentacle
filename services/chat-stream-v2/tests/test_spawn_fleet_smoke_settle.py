from _shared.spawn_profiles import resolve_test_profile
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


def test_real_smoke_payload_uses_test_profile_before_any_provider_call():
    """The launch builder must not inherit an operator's desktop work tuple."""
    captured = []
    def rpc(payload, expected):
        if payload['type'] == 'spawn_catalog_get':
            return {'test_prompt_profile': resolve_test_profile(), 'type': 'spawn_catalog_get.ok', 'catalog_version': 'fixture',
                    'profiles': {'desktop_manual': {'codex': ['gpt-6.1-sol', 'high']},
                                 'test_prompt': {'codex': ['gpt-6-luna', 'low']}}}
        captured.append(payload)
        raise RuntimeError('intercepted before provider execution')
    with pytest.raises(RuntimeError, match='intercepted before provider execution'):
        spawn_fleet_smoke.run_cell('fixture', 'codex', 'prompted', rpc=rpc,
            wait_ready=lambda *_: None, wait_event=lambda *_: None,
            verify_teardown=lambda *_: None)
    assert (captured[0]['model'], captured[0]['effort']) == ('gpt-6-luna', 'low')
    assert captured[0]['resolution_source'] == 'explicit_override'


def test_rpc_timeout_reports_budget_and_elapsed_after_pushes(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(spawn_fleet_smoke, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    class PushSocket:
        def send(self, _text):
            pass

        def recv(self, timeout=None):
            if timeout > 0.03:
                clock[0] += 0.04
                return json.dumps({"type": "chat.event", "event": USER})
            raise TimeoutError("timed out in 0.0s")

    @contextmanager
    def connection(*_args):
        yield SimpleNamespace(snapshot=None, socket=PushSocket())

    monkeypatch.setattr(spawn_fleet_smoke, "authenticated_operator_connection", connection)
    registry = SimpleNamespace(configure_runtime=lambda _snapshot: None)
    with spawn_fleet_smoke._operator_connection("ws://fixture", None, 0.1, registry) as closures:
        rpc = closures[0]
        with pytest.raises(RuntimeError, match=r"list_sessions.*budget.*elapsed") as error:
            rpc({"type": "list_sessions"}, "list_sessions", deadline=spawn_fleet_smoke.time.monotonic() + 0.05)
        assert isinstance(error.value.__cause__, TimeoutError)
        assert "budget=0.050s" in str(error.value)
        assert float(str(error.value).split("elapsed=")[1][:-1]) > 0


def test_settle_timeout_is_its_own_gate(validate, monkeypatch):
    socket, run = validate
    original_send = socket.send
    def send(text):
        req = json.loads(text)
        if req["type"] == "request_stream_events" and socket.snapshots:
            socket.queue.append({"type": "chat.event", "event": SYSTEM})
            return
        original_send(text)
    monkeypatch.setattr(socket, "send", send)
    with pytest.raises(RuntimeError, match="authoritative event set did not settle"):
        run()


def test_convergence_rpc_timeout_returns_failed_gate(validate, monkeypatch):
    socket, run = validate
    original_send = socket.send
    first = [True]
    def send(text):
        req = json.loads(text)
        if req['type'] == 'list_sessions' and first[0]:
            first[0] = False
            socket.queue.append({'type': 'chat.event', 'event': SYSTEM})
            return
        original_send(text)
    monkeypatch.setattr(socket, 'send', send)
    metrics = run()
    gate = metrics['convergence']
    assert gate['passed'] is False
    assert gate['wait_ms'] >= 0 and gate['pressure_cleared'] is True
    assert 'timed out' in gate['error']
