"""Local-bootstrap trust belongs to native tools, not arbitrary browser pages."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import websockets
from websockets.datastructures import Headers

from _shared import operator_auth
from server import Server


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("values", [[], ["https://untrusted.example"], ["null"], [""], ["http://127.0.0.1:7791"], ["broken"], ["null", "null"]])
def test_local_bootstrap_requires_no_origin(peer, legacy, values):
    headers = Headers([("oRiGiN", value) for value in values])
    transport = {"request_headers": headers} if legacy else {"request": SimpleNamespace(headers=headers)}
    socket = SimpleNamespace(remote_address=(peer, 1234), **transport)
    assert Server._is_loopback_client(socket) is (not values)


def test_remote_peer_never_gains_local_bootstrap_from_originless_headers():
    socket = SimpleNamespace(remote_address=("192.0.2.1", 1234), request=SimpleNamespace(headers=Headers()))
    assert not Server._is_loopback_client(socket)


class FixtureSessions:
    def list_open(self):
        return [{"host": "fixture", "session_name": "test", "stream_id": "fixture:test", "visibility": "default"}]


@pytest.mark.parametrize("origin", [None, "https://untrusted.example", "null", "", "http://127.0.0.1:7791"])
def test_actual_socket_read_hello_and_fanout_boundary(origin):
    async def run():
        server = Server(host="127.0.0.1", port=0, sessions=FixtureSessions())
        try:
            port = await server.bind()
            async with websockets.connect(f"ws://127.0.0.1:{port}", origin=origin) as socket:
                assert json.loads(await socket.recv())["type"] == "welcome"
                await socket.send(json.dumps({"type": "list_sessions", "request_id": "read"}))
                response = json.loads(await asyncio.wait_for(socket.recv(), 2))
                if origin is None:
                    assert response["type"] == "list_sessions.ok" and len(response["active"]) == 1
                else:
                    assert response == {"type": "list_sessions.error", "error_code": "authentication_required", "request_id": "read"}
                await socket.send(json.dumps({"type": "hello", "client": "origin-test", "subscribe": {"exclude_event_types": ["hosts.stats"]}}))
                response = json.loads(await asyncio.wait_for(socket.recv(), 2))
                if origin is None:
                    assert response["type"] == "hello"
                    snapshot = json.loads(await asyncio.wait_for(socket.recv(), 2))
                    assert snapshot["type"] == "snapshot" and len(snapshot["sessions"]) == 1
                else:
                    assert response == {"type": "hello.error", "error_code": "authentication_required"}
                frame = {"type": "origin.fixture", "marker": "not-for-anonymous-browsers"}
                peer = next(iter(server._clients))
                assert (server._frame_for_client(peer, frame["type"], frame) is not None) is (origin is None)
                await server.broadcast(frame)
                if origin is None:
                    assert json.loads(await asyncio.wait_for(socket.recv(), 2)) == frame
                else:
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(socket.recv(), .05)
        finally:
            await server.close()
    asyncio.run(run())


@pytest.mark.parametrize("client_kind", ["pentacle", "pentacle-mobile"])
@pytest.mark.parametrize("valid", [True, False])
def test_authenticated_browser_retains_access_but_invalid_proof_is_rejected(tmp_path, client_kind, valid):
    async def run():
        directory = tmp_path / "auth"
        directory.mkdir(mode=0o700)
        registry = operator_auth.OperatorCredentialRegistry(directory / "operator-credentials.json")
        _id, envelope = registry.issue(client_kind, label="isolated test only")
        credential = operator_auth.decode_envelope(envelope)
        server = Server(host="127.0.0.1", port=0, sessions=FixtureSessions())
        server.operator_credential_registry = registry
        try:
            port = await server.bind()
            async with websockets.connect(f"ws://127.0.0.1:{port}", origin="https://pentacle.example") as socket:
                welcome = json.loads(await socket.recv())
                proof = operator_auth.make_proof(credential["proof_key"] if valid else b"x" * 32, welcome["auth"]["operator"]["nonce"], credential["credential_id"], client_kind)
                await socket.send(json.dumps({"type": "hello", "client": client_kind, "subscribe": {"exclude_event_types": ["hosts.stats"]}, "auth_v2": {"scheme": operator_auth.AUTH_SCHEME, "credential_id": credential["credential_id"], "proof": proof}}))
                response = json.loads(await asyncio.wait_for(socket.recv(), 2))
                if not valid:
                    assert response == {"type": "hello.error", "error_code": "operator_auth_invalid"}
                    await socket.send(json.dumps({"type": "list_sessions"}))
                    assert json.loads(await asyncio.wait_for(socket.recv(), 2))["error_code"] == "authentication_required"
                    return
                assert response["type"] == "hello"
                snapshot = json.loads(await asyncio.wait_for(socket.recv(), 2))
                assert snapshot["type"] == "snapshot" and len(snapshot["sessions"]) == 1
                await socket.send(json.dumps({"type": "list_sessions"}))
                response = json.loads(await asyncio.wait_for(socket.recv(), 2))
                assert response["type"] == "list_sessions.ok" and len(response["active"]) == 1
                frame = {"type": "origin.fixture", "marker": "authenticated-browser"}
                await server.broadcast(frame)
                assert json.loads(await asyncio.wait_for(socket.recv(), 2)) == frame
        finally:
            await server.close()
    asyncio.run(run())
