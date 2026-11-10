"""Shared operator handshake: one re-proof only for a timely hello judged after expiry."""
import json

import pytest

from tools.live_window import core


class FakeSocket:
    def __init__(self, server, expires_at, reply):
        self.server, self.expires_at, self.reply = server, expires_at, reply
        self.frames = [{"type": "welcome", "auth": {"operator": {"nonce": "n%d" % len(server.sockets),
                                                                 "expires_at": expires_at}}}]
        self.sent, self.closed = [], False

    def recv(self, timeout):
        if not self.frames:
            raise TimeoutError("no frame")
        frame = self.frames.pop(0)
        if frame["type"] == "hello.error":
            self.server.now = self.server.error_at.pop(0)
        return json.dumps(frame)

    def send(self, payload):
        self.server.now = self.server.send_at.pop(0)
        self.sent.append(json.loads(payload))
        self.frames.append(self.reply)

    def close(self):
        self.closed = True


class FakeServer:
    """Scripted welcome/hello sequence; wall-clock samples come from the script."""
    def __init__(self, monkeypatch, attempts):
        self.attempts, self.sockets, self.now = list(attempts), [], 0.0
        self.send_at = [a["send_at"] for a in attempts]
        self.error_at = [a["error_at"] for a in attempts if "error_at" in a]
        monkeypatch.setattr(core, "connect", self.connect)
        monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {
            "type": "hello", "nonce": welcome["auth"]["operator"]["nonce"]})
        monkeypatch.setattr(core, "_wall_clock", lambda: self.now)

    def connect(self, url, **kwargs):
        if not self.attempts:
            raise AssertionError("unexpected reconnect")
        attempt = self.attempts.pop(0)
        reply = ({"type": "snapshot", "sessions": []} if attempt.get("accept")
                 else {"type": "hello.error", "error": attempt.get("error", "operator_auth_invalid"),
                       **({"code": attempt["code"]} if "code" in attempt else {})})
        socket = FakeSocket(self, attempt["expires_at"], reply)
        self.sockets.append(socket)
        return socket


def enter(tmp_path, timeout=5.0):
    return core._OperatorConnection("ws://127.0.0.1:0", tmp_path / "token.json", timeout, None).__enter__()


def test_timely_hello_judged_after_expiry_reproves_once_on_a_fresh_socket(tmp_path, monkeypatch):
    server = FakeServer(monkeypatch, [{"expires_at": 100.0, "send_at": 99.9, "error_at": 113.4},
                                      {"expires_at": 124.0, "send_at": 114.0, "accept": True}])
    connection = enter(tmp_path)
    assert connection.snapshot["type"] == "snapshot"
    assert [s.sent[0]["nonce"] for s in server.sockets] == ["n0", "n1"]
    assert server.sockets[0].closed and not server.sockets[1].closed
    assert connection.handshake_diagnostics[0]["error"]["type"] == "hello.error"
    assert connection.handshake_diagnostics[0]["sent_at"] == 99.9
    connection.close()


def test_explicit_expiry_code_on_a_timely_send_reproves(tmp_path, monkeypatch):
    server = FakeServer(monkeypatch, [{"expires_at": 100.0, "send_at": 95.0, "error_at": 99.0, "code": "operator_nonce_expired"},
                                      {"expires_at": 110.0, "send_at": 100.5, "accept": True}])
    assert enter(tmp_path).snapshot is not None and len(server.sockets) == 2


@pytest.mark.parametrize("attempt", [
    {"expires_at": 100.0, "send_at": 100.0, "error_at": 101.0},                               # late send
    {"expires_at": 100.0, "send_at": 100.5, "error_at": 101.0, "code": "operator_nonce_expired"},  # late send, coded
    {"expires_at": 100.0, "send_at": 95.0, "error_at": 96.0},                                  # timely denial
    {"expires_at": 100.0, "send_at": 95.0, "error_at": 96.0, "error": "wrong credential"},
])
def test_no_retry_for_late_send_or_timely_denial(tmp_path, monkeypatch, attempt):
    server = FakeServer(monkeypatch, [attempt])
    with pytest.raises(core.LiveWindowError, match="operator nonce proof rejected"):
        enter(tmp_path)
    assert len(server.sockets) == 1 and server.sockets[0].closed


def test_second_failure_terminates_and_keeps_the_first(tmp_path, monkeypatch):
    server = FakeServer(monkeypatch, [{"expires_at": 100.0, "send_at": 99.0, "error_at": 112.0},
                                      {"expires_at": 122.0, "send_at": 112.5, "error_at": 125.0}])
    with pytest.raises(core.LiveWindowError) as caught:
        enter(tmp_path)
    assert "first failure" in str(caught.value) and len(server.sockets) == 2
    assert all(s.closed for s in server.sockets)


def test_retry_never_extends_the_original_deadline(tmp_path, monkeypatch):
    server = FakeServer(monkeypatch, [{"expires_at": 100.0, "send_at": 99.0, "error_at": 112.0},
                                      {"expires_at": 122.0, "send_at": 112.5, "accept": True}])
    monotonic = {"now": 0.0}
    monkeypatch.setattr(core.time, "monotonic", lambda: monotonic["now"])
    original = FakeSocket.recv
    def slow(self, timeout):
        frame = original(self, timeout)
        if json.loads(frame)["type"] == "hello.error":
            monotonic["now"] = 6.0  # the first error consumed the whole budget
        return frame
    monkeypatch.setattr(FakeSocket, "recv", slow)
    with pytest.raises(core.LiveWindowError, match="operator nonce proof rejected"):
        enter(tmp_path, timeout=5.0)
    assert len(server.sockets) == 1 and server.sockets[0].closed


def test_failed_socket_is_closed_when_welcome_is_malformed(tmp_path, monkeypatch):
    server = FakeServer(monkeypatch, [{"expires_at": 100.0, "send_at": 99.0, "accept": True}])
    original = server.connect
    def bad(url, **kwargs):
        socket = original(url, **kwargs); socket.frames = [{"type": "snapshot"}]; return socket
    monkeypatch.setattr(core, "connect", bad)
    with pytest.raises(core.LiveWindowError, match="expected welcome"):
        enter(tmp_path)
    assert server.sockets[0].closed


class SlowClose:
    """Models websockets.sync close: waits up to its close_timeout for the peer."""
    def __init__(self, clock, frames):
        self.clock, self.frames, self.close_timeout, self.closed = clock, frames, None, False

    def recv(self, timeout):
        frame = self.frames.pop(0)
        if frame["type"] == "hello.error":
            self.clock.update(mono=4.9, wall=101.0)
        return json.dumps(frame)

    def send(self, payload):
        pass

    def close(self):
        self.clock["mono"] += self.close_timeout
        self.closed = True


def _slow_close_server(monkeypatch, frames_per_socket):
    clock = {"mono": 0.0, "wall": 99.0}
    sockets = []
    def connect(url, **kwargs):
        socket = SlowClose(clock, frames_per_socket.pop(0)); socket.close_timeout = kwargs["close_timeout"]
        sockets.append(socket); return socket
    monkeypatch.setattr(core, "connect", connect)
    monkeypatch.setattr(core.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(core, "_wall_clock", lambda: clock["wall"])
    monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {"type": "hello"})
    return clock, sockets


def test_failed_socket_cleanup_stays_inside_the_original_budget(tmp_path, monkeypatch):
    welcome = {"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": 100.0}}}
    clock, sockets = _slow_close_server(monkeypatch, [[welcome, {"type": "hello.error", "error": "FIRST_AUTH_FAILURE"}]])
    with pytest.raises(core.LiveWindowError, match="FIRST_AUTH_FAILURE"):
        enter(tmp_path, timeout=5.0)
    assert len(sockets) == 1 and sockets[0].closed
    assert clock["mono"] <= 5.0


def test_reproof_transport_failure_keeps_the_first_failure(tmp_path, monkeypatch):
    server = FakeServer(monkeypatch, [{"expires_at": 100.0, "send_at": 99.9, "error_at": 113.4}])
    original = server.connect
    def connect(url, **kwargs):
        if server.sockets:
            raise OSError("connection refused")
        return original(url, **kwargs)
    monkeypatch.setattr(core, "connect", connect)
    with pytest.raises(core.LiveWindowError) as caught:
        enter(tmp_path)
    assert "connection refused" in str(caught.value) and "first failure" in str(caught.value)
    assert "operator_auth_invalid" in str(caught.value)
