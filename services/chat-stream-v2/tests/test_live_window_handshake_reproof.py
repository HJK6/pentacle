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
    # The error arrived after the absolute deadline: it is not acted on, and no retry follows.
    with pytest.raises(core.LiveWindowError, match="deadline"):
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

    def close_socket(self):  # real websockets abort: no close-handshake wait
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


# --- cycle 2: real websockets client against an owned raw loopback peer ---
import base64
import hashlib as _hashlib
import socket as _socket
import struct
import threading
import time as _time


def _frame(opcode, payload):
    head = bytes([0x80 | opcode])
    return head + (bytes([len(payload)]) if len(payload) < 126 else bytes([126]) + struct.pack("!H", len(payload))) + payload


class RawPeer:
    """Owned loopback WebSocket peer that never answers a close handshake."""
    def __init__(self, script):
        self.script, self.listener = script, _socket.socket()
        self.listener.bind(("127.0.0.1", 0)); self.listener.listen(1)
        self.url = "ws://127.0.0.1:%d" % self.listener.getsockname()[1]
        self.conn, self.done = None, threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True); self.thread.start()

    def serve(self):
        conn, _ = self.listener.accept(); self.conn = conn
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(4096)
        key = [l.split(b":", 1)[1].strip() for l in request.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(_hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        for step in self.script:
            if step[0] == "send":
                conn.sendall(_frame(0x1, json.dumps(step[1]).encode()))
            elif step[0] == "close":
                conn.sendall(_frame(0x8, struct.pack("!H", 1000)))
            elif step[0] == "sleep":
                _time.sleep(step[1])
            elif step[0] == "read":
                conn.recv(65536)
        self.done.wait(10)  # hold the socket open; never answer a close

    def stop(self):
        self.done.set()
        for s in (self.conn, self.listener):
            try:
                s and s.close()
            except OSError:
                pass


@pytest.mark.parametrize("peer_closes", [False, True])
def test_real_failed_socket_is_aborted_inside_the_budget(tmp_path, monkeypatch, peer_closes):
    monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {"type": "hello"})
    welcome = {"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": _time.time() + 600}}}
    script = [("send", welcome), ("read",), ("sleep", 1.2), ("send", {"type": "hello.error", "error": "DENIED"})]
    peer = RawPeer(script + ([("close",)] if peer_closes else []))
    before = set(threading.enumerate())
    started = _time.monotonic()
    try:
        connection = core._OperatorConnection(peer.url, tmp_path / "token.json", 2.0, None)
        with pytest.raises(core.LiveWindowError, match="DENIED"):
            connection.__enter__()
        elapsed = _time.monotonic() - started
        assert elapsed < 2.0, elapsed
        connection.close(); connection._abort()  # repeated cleanup is idempotent
        assert connection._socket is None and connection.handshake_diagnostics[0]["error"]["error"] == "DENIED"
        peer.conn.settimeout(0.5)
        while True:  # the owned transport is actually closed: the peer reads EOF
            data = peer.conn.recv(65536)
            if not data:
                break
        # The owned receive machinery terminates without waiting for the peer.
        deadline = _time.monotonic() + 2.0  # guaranteed by close_socket; scheduling only (0.5 s flaked 1/20 under load)
        while any(t.is_alive() for t in set(threading.enumerate()) - before - {peer.thread}) and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert not [t for t in set(threading.enumerate()) - before - {peer.thread} if t.is_alive()]
    finally:
        peer.stop()


@pytest.mark.parametrize("snapshot_at,accepted", [(4.999, True), (5.0, False), (5.03, False)])
def test_snapshot_at_or_after_the_deadline_is_rejected(tmp_path, monkeypatch, snapshot_at, accepted):
    clock = {"mono": 0.0}
    frames = [({"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": 1e12}}}, 4.99),
              ({"type": "snapshot", "sessions": []}, snapshot_at)]
    class Socket:
        closed = False
        def recv(self, timeout):
            assert timeout > 0 and clock["mono"] + timeout <= 5.0 + 1e-9  # never past the absolute deadline
            frame, at = frames.pop(0); clock["mono"] = at; return json.dumps(frame)
        def send(self, payload): pass
        def close(self): self.closed = True
    sock = Socket()
    monkeypatch.setattr(core, "connect", lambda url, **kw: sock)
    monkeypatch.setattr(core.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {"type": "hello"})
    connection = core._OperatorConnection("ws://127.0.0.1:0", tmp_path / "t.json", 5.0, None)
    if accepted:
        assert connection.__enter__().snapshot is not None
    else:
        with pytest.raises(core.LiveWindowError, match="deadline"):
            connection.__enter__()
        assert sock.closed


def test_late_welcome_gets_no_hello_and_no_retry(tmp_path, monkeypatch):
    clock = {"mono": 0.0}; sent = []; connects = []
    class Socket:
        def recv(self, timeout):
            clock["mono"] = 5.2
            return json.dumps({"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": 1e12}}})
        def send(self, payload): sent.append(payload)
        def close(self): pass
    monkeypatch.setattr(core, "connect", lambda url, **kw: (connects.append(1), Socket())[1])
    monkeypatch.setattr(core.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {"type": "hello"})
    with pytest.raises(core.LiveWindowError, match="deadline"):
        core._OperatorConnection("ws://127.0.0.1:0", tmp_path / "t.json", 5.0, None).__enter__()
    assert sent == [] and connects == [1]


# --- cycle 2 re-review (3585799e): proof latency and a peer close before hello ---
def test_no_hello_after_proof_consumes_the_deadline(tmp_path, monkeypatch):
    clock = {"mono": 0.0}; sent = []
    class Socket:
        def recv(self, timeout):
            return json.dumps({"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": 1e12}}})
        def send(self, payload): sent.append(payload)
        def close_socket(self): pass
    def slow_proof(path, welcome):
        clock["mono"] = 5.1
        return {"type": "hello"}
    monkeypatch.setattr(core, "connect", lambda url, **kw: Socket())
    monkeypatch.setattr(core.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(core, "operator_hello", slow_proof)
    with pytest.raises(core.LiveWindowError, match="deadline"):
        core._OperatorConnection("ws://127.0.0.1:0", tmp_path / "t.json", 5.0, None).__enter__()
    assert sent == []


def test_real_peer_close_before_hello_stays_inside_the_budget(tmp_path, monkeypatch):
    from websockets.protocol import CLOSING
    welcome = {"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": _time.time() + 600}}}
    peer = RawPeer([("send", welcome), ("sleep", 0.8), ("close",)])
    sockets = []
    real_connect = core.connect
    monkeypatch.setattr(core, "connect", lambda *a, **kw: (sockets.append(real_connect(*a, **kw)), sockets[-1])[1])
    def proof_until_peer_closes(path, welcome):
        end = _time.monotonic() + 1.2
        while sockets[0].protocol.state is not CLOSING and _time.monotonic() < end:
            _time.sleep(0.002)
        assert sockets[0].protocol.state is CLOSING
        return {"type": "hello"}
    monkeypatch.setattr(core, "operator_hello", proof_until_peer_closes)
    connection = core._OperatorConnection(peer.url, tmp_path / "token.json", 1.5, None)
    started = _time.monotonic()
    try:
        with pytest.raises(Exception):
            connection.__enter__()
        assert _time.monotonic() - started < 1.5
        assert connection._socket is None
    finally:
        connection._abort()
        peer.stop()


def test_real_successful_handshake_keeps_the_library_close_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "operator_hello", lambda path, welcome: {"type": "hello"})
    welcome = {"type": "welcome", "auth": {"operator": {"nonce": "n", "expires_at": _time.time() + 600}}}
    peer = RawPeer([("send", welcome), ("read",), ("send", {"type": "snapshot", "sessions": []})])
    connection = core._OperatorConnection(peer.url, tmp_path / "token.json", 2.0, None)
    try:
        assert connection.__enter__().snapshot == {"type": "snapshot", "sessions": []}
        # Ordinary RPC and shutdown after a good handshake keep websockets' own close deadline.
        assert connection._socket.close_deadline is None
    finally:
        connection._abort()
        peer.stop()
