"""Exercise detached screen RPCs through the real credential send boundary."""
import asyncio
import hashlib
import hmac
import json
import os

import pytest

from _shared import operator_auth as auth
from agent_orch import config, wsclient


@pytest.fixture
def protocol(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {})
    key = b"k" * 32
    credential_id = "00000000-0000-4000-8000-000000000001"
    envelope = auth.encode_envelope(credential_id, "pentacle", key)
    nonce = auth.encode_b64url(b"n" * auth.AUTH_NONCE_BYTES)
    challenge = {"protocol_version": 2, "scheme": auth.AUTH_SCHEME, "nonce": nonce}

    class Socket:
        def __init__(self):
            self.frames = []
            self.queue = [{"type": "welcome", "auth": {"operator": challenge}}]
            self.ready = False
            self.closed = False
            self.transport = self

        async def send(self, raw):
            frame = json.loads(raw)
            self.frames.append(frame)
            assert envelope not in raw and "proof_key" not in raw
            if frame["type"] == "hello":
                proof = frame.get("auth_v2", {}).get("proof")
                expected = auth.encode_b64url(hmac.new(key, auth.proof_transcript(
                    nonce, credential_id, "pentacle"), hashlib.sha256).digest())
                assert proof == expected, "hello lacks valid challenge proof"
                assert "token" not in frame
                self.queue.extend([{"type": "ping"}, {"type": "ready"}])
            elif frame["type"] != "pong":
                assert self.ready, "request sent before authenticated ready"
                self.queue.extend([
                    {"type": frame["type"] + ".ok", "request_id": "unrelated"},
                    {"type": "unrelated.ok", "request_id": frame["request_id"]},
                    {"type": frame["type"] + ".ok", "request_id": frame["request_id"]}])

        async def recv(self):
            if not self.queue:
                await asyncio.Future()
            frame = self.queue.pop(0)
            if frame["type"] == "ready":
                self.ready = True
            return json.dumps(frame)

        def abort(self):
            self.closed = True

        async def close(self):
            self.closed = True

    socket = Socket()
    async def connect(*args, **kwargs):
        return socket
    monkeypatch.setattr(wsclient.websockets, "connect", connect)
    return config.Config("ws://fixture.invalid", envelope, "fixture", tmp_path), socket


def test_detached_screen_status_proof(protocol):
    cfg, socket = protocol
    reply = asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease.status"}))
    assert socket.frames[0].get("auth_v2"), "hello lacks valid challenge proof"
    assert reply["type"] == "screen.lease.status.ok"
    assert socket.closed
    assert [f["type"] for f in socket.frames] == ["hello", "pong", "screen.lease.status"]


@pytest.mark.parametrize("verb", ["acquire", "renew", "release"])
@pytest.mark.parametrize("seat", [False, True])
def test_mutation_identity(protocol, monkeypatch, verb, seat):
    cfg, socket = protocol
    if seat:
        monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "fixture:seat")
        monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "synthetic-seat-token")
    asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease." + verb}))
    request = socket.frames[-1]
    assert request.get("from_stream_id") == ("fixture:seat" if seat else None)
    assert request.get("stream_token") == ("synthetic-seat-token" if seat else None)
    assert sum(f["type"] == request["type"] for f in socket.frames) == 1
    assert socket.closed


@pytest.mark.parametrize("challenge", [None, {}, {"protocol_version": 1},
    {"protocol_version": 2, "scheme": auth.AUTH_SCHEME, "nonce": "invalid"}])
def test_invalid_challenge_fails_closed(protocol, challenge):
    cfg, socket = protocol
    socket.queue[0]["auth"]["operator"] = challenge
    with pytest.raises(ValueError):
        asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease.acquire"}, timeout=.05))
    assert not socket.frames
    assert socket.closed


@pytest.mark.parametrize("failure", ["auth.error", "missing-ready", "lost-response", "cancel"])
def test_failure_cleanup_and_no_retry(protocol, monkeypatch, failure):
    cfg, socket = protocol
    original_send = socket.send
    async def send(raw):
        await original_send(raw)
        frame = json.loads(raw)
        if frame["type"] == "hello" and failure in {"auth.error", "missing-ready", "cancel"}:
            socket.queue = [{"type": "auth.error"}] if failure == "auth.error" else []
        if frame["type"] == "screen.lease.acquire" and failure == "lost-response":
            socket.queue = []
    monkeypatch.setattr(socket, "send", send)
    async def run():
        task = asyncio.create_task(wsclient.screen_lease_once(cfg, {"type": "screen.lease.acquire"}, timeout=.03))
        if failure == "cancel":
            await asyncio.sleep(.01)
            task.cancel()
        await task
    with pytest.raises((PermissionError, TimeoutError, asyncio.CancelledError)):
        asyncio.run(run())
    assert socket.closed
    requests = [f for f in socket.frames if f["type"] == "screen.lease.acquire"]
    assert len(requests) == (1 if failure == "lost-response" else 0)


def test_dashboard_still_uses_proof(protocol):
    cfg, socket = protocol
    reply = asyncio.run(wsclient.dashboard_once(cfg, {"type": "dashboard.owner.transfer"}))
    assert reply["type"] == "dashboard.owner.transfer.ok"
    assert socket.closed


@pytest.mark.parametrize("ambiguous", [False, True])
def test_service_never_loads_operator(protocol, monkeypatch, ambiguous):
    cfg, socket = protocol
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "codex-reset-monitor")
    if ambiguous:
        monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:seat")
    def forbidden(*args):
        pytest.fail("service touched operator loader")
    monkeypatch.setattr(wsclient, "resolve_operator_token", forbidden)
    monkeypatch.setattr("agent_orch.send_boundary.resolve_operator_token", forbidden)
    with pytest.raises(wsclient.ServiceCredentialUnavailable):
        asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease.status"}))
    assert not socket.frames


def test_malformed_envelope_fails_closed(protocol):
    from dataclasses import replace
    cfg, socket = protocol
    cfg = replace(cfg, token="pentacle-auth-v2:invalid")
    with pytest.raises(ValueError):
        asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease.acquire"}))
    assert socket.closed and not socket.frames


@pytest.mark.parametrize("stage", ["connect", "hello", "request", "close"])
def test_total_deadline(protocol, monkeypatch, stage):
    import time
    cfg, socket = protocol
    async def stall(*args, **kwargs):
        await asyncio.Future()
    if stage == "connect":
        monkeypatch.setattr(wsclient.websockets, "connect", stall)
    elif stage == "close":
        monkeypatch.setattr(socket, "close", stall)
    else:
        original = socket.send
        async def send(raw):
            frame = json.loads(raw)
            if frame["type"] == ("hello" if stage == "hello" else "screen.lease.acquire"):
                await stall()
            await original(raw)
        monkeypatch.setattr(socket, "send", send)
    started = time.monotonic()
    if stage == "close":
        asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease.acquire"}, timeout=.03))
    else:
        with pytest.raises(TimeoutError):
            asyncio.run(wsclient.screen_lease_once(cfg, {"type": "screen.lease.acquire"}, timeout=.03))
    assert time.monotonic() - started < .3
    if stage != "connect":
        assert socket.closed
