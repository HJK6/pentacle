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
            await server.bind()
            plain_port = server.port
            tls_port = server.dot_tls_port
            assert tls_port and tls_port != plain_port

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE

            # (1) wss: Dot token is honoured.
            async with websockets.connect(f"wss://127.0.0.1:{tls_port}", ssl=client_ctx) as ws:
                assert json.loads(await ws.recv())["type"] == "welcome"
                await ws.send(json.dumps({
                    "type": "hello", "client": "agent-orch", "from_stream_id": DOT_ID,
                    "stream_token": TOKEN, "subscribe": {"mode": "rpc", "snapshot": False},
                }))
                await ws.send(json.dumps({
                    "type": "list_sessions", "request_id": "ls-tls",
                    "from_stream_id": DOT_ID, "stream_token": TOKEN,
                }))
                frames = []
                while not any(f.get("request_id") == "ls-tls" for f in frames):
                    frames.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
                assert frames[-1]["type"] == "list_sessions.ok"

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
