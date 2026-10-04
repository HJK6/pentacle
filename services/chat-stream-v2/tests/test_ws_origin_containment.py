"""H1 containment (advisor 435d2d15): the daemon refuses, at the WS opening
handshake and before `_handle_client` runs, any upgrade carrying a browser
cross-origin signature — any `Origin` header (including null/empty/malformed/
duplicate) or any `Sec-Fetch-*` header — on EVERY applicable listener (the plain
fleet listener and the daemon-TLS/Dot listener). The allowlist is empty and
there is no `X-Forwarded-*` identity exemption. Legitimate daemon clients
(agent-orch, the web-service bridge, native mobile) send neither header.
"""

from __future__ import annotations

import asyncio
import http
import socket
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers

import server
from server import Server, _browser_origin_signal


def _headers(pairs):
    h = Headers()
    for name, value in pairs:
        h[name] = value
    return h


# --------------------------------------------------------------------------- #
# Pure signal detector: default-deny on header NAME, value never consulted.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pairs, expected", [
    # Any Origin, whatever the value — including null / empty / malformed.
    ([("Host", "x"), ("Origin", "https://evil.example")], "origin"),
    ([("Origin", "")], "origin"),
    ([("Origin", "null")], "origin"),
    ([("Origin", "not a url")], "origin"),
    # Case-insensitive on the name.
    ([("OrIgIn", "https://e")], "origin"),
    # Duplicate Origin headers cannot slip past (raw_items preserves both).
    ([("Origin", "https://a"), ("Origin", "https://b")], "origin"),
    # Any Sec-Fetch-* header, even with no Origin at all.
    ([("Sec-Fetch-Site", "cross-site")], "sec-fetch-site"),
    ([("sec-fetch-mode", "websocket")], "sec-fetch-mode"),
    ([("SEC-FETCH-DEST", "empty")], "sec-fetch-dest"),
    # A forwarded header is a wire claim, never an identity exemption.
    ([("X-Forwarded-For", "127.0.0.1"), ("Origin", "https://e")], "origin"),
    # Legitimate daemon clients send neither header -> allowed.
    ([("Host", "x"), ("User-Agent", "python-websockets")], None),
    ([("Host", "x"), ("X-Forwarded-For", "10.0.0.1")], None),
    ([], None),
])
def test_browser_origin_signal(pairs, expected):
    assert _browser_origin_signal(_headers(pairs)) == expected


# --------------------------------------------------------------------------- #
# process_request wrapper: Response (403) on a signal, None otherwise.
# --------------------------------------------------------------------------- #

class _FakeConnection:
    def __init__(self, address="127.0.0.1"):
        self.remote_address = (address, 54321)
        self.responded = None

    def respond(self, status, text):
        self.responded = (status, text)
        return SimpleNamespace(status=status, text=text)


def _daemon():
    return Server(store=SimpleNamespace())


def test_reject_browser_origin_denies_origin():
    daemon = _daemon()
    conn = _FakeConnection()
    out = daemon._reject_browser_origin(conn, SimpleNamespace(
        headers=_headers([("Origin", "https://evil.example")])))
    assert out is not None
    assert conn.responded[0] == http.HTTPStatus.FORBIDDEN
    assert out.status == http.HTTPStatus.FORBIDDEN


def test_reject_browser_origin_denies_sec_fetch_without_origin():
    daemon = _daemon()
    conn = _FakeConnection()
    out = daemon._reject_browser_origin(conn, SimpleNamespace(
        headers=_headers([("Sec-Fetch-Site", "cross-site")])))
    assert out is not None
    assert conn.responded[0] == http.HTTPStatus.FORBIDDEN


def test_reject_browser_origin_allows_clean_client():
    daemon = _daemon()
    conn = _FakeConnection()
    out = daemon._reject_browser_origin(conn, SimpleNamespace(
        headers=_headers([("Host", "x"), ("User-Agent", "python-websockets")])))
    assert out is None
    assert conn.responded is None


# --------------------------------------------------------------------------- #
# Wiring: the guard is installed on EVERY listener (plain + daemon-TLS/Dot).
# --------------------------------------------------------------------------- #

def test_process_request_wired_on_both_listeners(monkeypatch):
    async def run():
        calls = []

        class _FakeSock:
            family = socket.AF_INET

            def getsockname(self):
                return ("127.0.0.1", 12345)

        class _FakeServer:
            sockets = [_FakeSock()]

            def close(self):
                pass

            async def wait_closed(self):
                pass

        async def fake_serve(handler, host, port, **kwargs):
            calls.append({"handler": handler, "host": host, "kwargs": kwargs})
            return _FakeServer()

        monkeypatch.setattr(server, "serve", fake_serve)
        # Keep the TLS path hermetic: a real SSLContext, no real cert files.
        monkeypatch.setattr(server.ssl.SSLContext, "load_cert_chain",
                            lambda self, certfile, keyfile: None)

        daemon = Server(
            store=SimpleNamespace(), binds=["127.0.0.1"], port=0,
            dot_tls_cert="/dev/null", dot_tls_key="/dev/null",
            dot_tls_port=0, dot_tls_binds=["127.0.0.1"],
        )
        await daemon.bind()  # binds the plain listener, then the TLS listener

        assert len(calls) == 2, "expected the plain AND the daemon-TLS listener"
        for call in calls:
            assert call["kwargs"].get("process_request") == daemon._reject_browser_origin
        # The TLS listener is the one that received an ssl context.
        assert any("ssl" in c["kwargs"] for c in calls)

    asyncio.run(run())
