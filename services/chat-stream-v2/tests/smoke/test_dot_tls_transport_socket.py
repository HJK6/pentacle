"""Real-socket proof of the Dot transport boundary.

The daemon terminates TLS itself on a dedicated listener. A Dot token works over
that wss endpoint and is refused over plain ws (both the plain fleet port and a
plain-ws attempt against the TLS port, which cannot even complete a handshake).
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import ssl

import pytest
import websockets

from server import Server
from sessions import Sessions
from store import STREAM_TOKEN_HASH_VERSION, Store

DOT_ID = "amaterasu:dot"
TOKEN = "dot-tls-smoke-token"


def _self_signed(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "dot.crt"
    key_path = tmp_path / "dot.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


def test_dot_token_works_over_wss_and_is_refused_over_plain_ws(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        server = None
        try:
            await store.open_session("amaterasu", "dot", provider="codex", pane_status="pane_alive")
            await store.grant_stream_token(
                "amaterasu", "dot",
                hashlib.sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION,
            )
            sessions = Sessions(store, local_host="thoth")
            await sessions.refresh()
            cert, key = _self_signed(tmp_path)
            server = Server(
                host="127.0.0.1", port=0, store=store, sessions=sessions,
                dot_principal_stream_ids=[DOT_ID],
                dot_tls_port=0, dot_tls_cert=cert, dot_tls_key=key, dot_tls_binds=["127.0.0.1"],
            )
            # Seed host telemetry so a leaked hosts.stats hello frame would show.
            server._host_stats["thoth"] = {"cpu": 0.9, "secret_host_metric": "LEAKME"}
            await server.bind()
            plain_port = server.port
            tls_port = server.dot_tls_port
            assert tls_port and tls_port != plain_port

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE

            # (1) wss: Dot token is honoured. The hello (subscribe:{all:true},
            # like the stock agent-orch client) returns a well-formed but EMPTY
            # snapshot and NO hosts.stats frame — the client does not hang and no
            # fleet data / host telemetry egresses. The read verb is denied.
            async with websockets.connect(f"wss://127.0.0.1:{tls_port}", ssl=client_ctx) as ws:
                assert json.loads(await ws.recv())["type"] == "welcome"
                await ws.send(json.dumps({
                    "type": "hello", "client": "agent-orch", "from_stream_id": DOT_ID,
                    "stream_token": TOKEN, "subscribe": {"all": True, "include_subagents": True},
                }))
                hello_seq = []
                while not any(f.get("type") == "snapshot" for f in hello_seq):
                    hello_seq.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
                assert [f["type"] for f in hello_seq] == ["hello", "snapshot"]
                snap = hello_seq[-1]
                assert snap["sessions"] == [] and snap["hosts"] == {}
                assert "LEAKME" not in json.dumps(hello_seq)
                assert "hosts.stats" not in json.dumps(hello_seq)
                # Read is DISABLED by default: list_sessions is refused. This
                # list_sessions also acts as a BARRIER: on the old path hosts.stats
                # is appended AFTER the snapshot, so any trailing telemetry frame
                # would arrive on the wire before this response. Assert none did.
                await ws.send(json.dumps({
                    "type": "list_sessions", "request_id": "ls-tls",
                    "from_stream_id": DOT_ID, "stream_token": TOKEN,
                }))
                frames = []
                while not any(f.get("request_id") == "ls-tls" for f in frames):
                    frames.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
                assert all(f.get("type") != "hosts.stats" for f in frames), frames
                assert frames[-1]["error_code"] == "dot_scope_denied"

            # (2) plain ws against the plain fleet port: Dot token refused.
            async with websockets.connect(f"ws://127.0.0.1:{plain_port}") as ws:
                assert json.loads(await ws.recv())["type"] == "welcome"
                await ws.send(json.dumps({
                    "type": "list_sessions", "request_id": "ls-plain",
                    "from_stream_id": DOT_ID, "stream_token": TOKEN,
                }))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
                assert reply["error_code"] == "external_requires_tls"

            # (3) plain ws against the TLS port: the handshake cannot complete.
            with pytest.raises((OSError, websockets.InvalidMessage, asyncio.TimeoutError,
                                websockets.ConnectionClosed, EOFError)):
                async with await asyncio.wait_for(
                    websockets.connect(f"ws://127.0.0.1:{tls_port}"), 3,
                ) as ws:
                    await asyncio.wait_for(ws.recv(), 3)
        finally:
            if server is not None:
                await server.close()
            store.stop()

    asyncio.run(run())


BACKEND = "thoth:v2-bartbackend-disposable"


class _StandinComposite:
    """A disposable stand-in Bart binding for the round-trip proof."""
    enabled = True
    def __init__(self, backend):
        self._backend = backend
    def is_stream(self, sid):
        return str(sid or "") == "bart:assistant"
    async def binding(self):
        return {"type": "assistant.binding.ok", "stream_id": self._backend, "generation": "g"}


class _RecordingComms:
    """Records the attributed peer send and returns a backend-identifying result;
    the daemon must sanitize that result before it reaches Dot."""
    def __init__(self):
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)
        return {"type": "send.result", "delivery": "landed", "to_stream_id": msg.get("to_stream_id"),
                "host": "thoth", "session_name": "v2-bartbackend-disposable"}


def test_dot_wss_send_ack_sanitized_and_live_revocation(tmp_path):
    """Real-TLS WSS round trip (disposable binding): Dot connects over wss, sends
    to the current Bart composite, gets a sanitized ack (no backend seat/host),
    the delivery preserves [from dot] attribution, and revoking the token drops
    the live connection on the next RPC. Never touches live Bart."""
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        server = None
        try:
            await store.open_session("amaterasu", "dot", provider="codex", pane_status="pane_alive")
            await store.grant_stream_token(
                "amaterasu", "dot",
                hashlib.sha256(TOKEN.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION,
            )
            sessions = Sessions(store, local_host="thoth")
            await sessions.refresh()
            cert, key = _self_signed(tmp_path)
            server = Server(
                host="127.0.0.1", port=0, store=store, sessions=sessions,
                dot_principal_stream_ids=[DOT_ID],
                dot_tls_port=0, dot_tls_cert=cert, dot_tls_key=key, dot_tls_binds=["127.0.0.1"],
            )
            recording = _RecordingComms()
            server.assistant_composite = _StandinComposite(BACKEND)
            server.comms = recording
            await server.bind()
            tls_port = server.dot_tls_port

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE

            async with websockets.connect(f"wss://127.0.0.1:{tls_port}", ssl=client_ctx) as ws:
                assert json.loads(await ws.recv())["type"] == "welcome"
                await ws.send(json.dumps({
                    "type": "hello", "client": "agent-orch", "from_stream_id": DOT_ID,
                    "stream_token": TOKEN, "subscribe": {"all": True},
                }))
                # Drain the hello sequence up to the snapshot.
                seq = []
                while not any(f.get("type") == "snapshot" for f in seq):
                    seq.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))

                # Send to the current Bart binding over wss.
                await ws.send(json.dumps({
                    "type": "send", "request_id": "snd-tls",
                    "from_stream_id": DOT_ID, "stream_token": TOKEN,
                    "to_stream_id": "bart:assistant", "text": "handoff from dot",
                }))
                acks = []
                while not any(f.get("request_id") == "snd-tls" for f in acks):
                    acks.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
                ack = acks[-1]
                assert ack["type"] == "send.result"
                assert ack["to_stream_id"] == "bart:assistant"
                # The whitelisted delivery status DOES survive the real TLS RPC
                # path (not merely that backend detail was dropped).
                assert ack["delivery"] == "landed"
                # The ack discloses NO backend seat/host/session_name.
                assert BACKEND not in json.dumps(ack)
                assert "v2-bartbackend-disposable" not in json.dumps(ack)
                for leaked in ("delivered_to_binding", "host", "session_name"):
                    assert leaked not in ack, leaked
                # ...but the message WAS delivered to the resolved backend as an
                # attributed [from dot] handoff (provenance preserved).
                assert recording.sent, "no delivery recorded"
                delivered = recording.sent[-1]
                assert delivered["to_stream_id"] == BACKEND
                assert delivered["from_stream_id"] == DOT_ID

                # Revoke the Dot seat; the next RPC on the LIVE wss connection is
                # refused (per-RPC revalidation drops the live connection).
                await store.mark_closed(
                    "amaterasu", "dot",
                    closed_at="2026-10-01T00:00:00Z", pane_status="pane_dead",
                )
                await ws.send(json.dumps({
                    "type": "send", "request_id": "snd-revoked",
                    "from_stream_id": DOT_ID, "stream_token": TOKEN,
                    "to_stream_id": "bart:assistant", "text": "after revoke",
                }))
                post = []
                while not any(f.get("request_id") == "snd-revoked" for f in post):
                    post.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
                assert post[-1]["error_code"] == "authentication_required"
                # No second delivery happened after revocation.
                assert len(recording.sent) == 1
        finally:
            if server is not None:
                await server.close()
            store.stop()

    asyncio.run(run())
