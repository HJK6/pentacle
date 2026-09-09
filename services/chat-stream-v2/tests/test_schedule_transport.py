"""Authenticated schedule snapshot and lifecycle transport contract."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from _shared import operator_auth
from server import Server


CREDENTIAL_ID = "credential-a"


def _trust(client_kind: str = "pentacle") -> operator_auth.ConnectionTrust:
    return operator_auth.ConnectionTrust(
        transport="v2",
        credential_id=CREDENTIAL_ID,
        client_kind=client_kind,
        operator_trusted=True,
    )


class Inventory:
    def __init__(self) -> None:
        self.schema_health = "ok"
        self.calls = 0

    async def schedule_inventory(self) -> list[dict[str, Any]]:
        self.calls += 1
        return [{
            "schedule_id": "sched-visible",
            "state": "pending",
            "prompt_preview": "trusted preview",
            "created_by_stream_id": "hosta:owner",
        }]


class AcceptingRegistry:
    def verify(
        self, credential_id: str, client_kind: str, _nonce: str, _proof: str,
    ) -> operator_auth.ConnectionTrust:
        assert credential_id == CREDENTIAL_ID
        assert client_kind == "pentacle"
        return _trust()


def _authenticated_hello(server: Server, websocket: object) -> dict[str, Any]:
    nonce, expires_at = operator_auth.new_nonce()
    server._operator_challenges[websocket] = (nonce, expires_at)
    return {
        "type": "hello",
        "client": "pentacle",
        "_client_websocket": websocket,
        "auth_v2": {
            "scheme": operator_auth.AUTH_SCHEME,
            "credential_id": CREDENTIAL_ID,
            "proof": operator_auth.encode_b64url(b"p" * operator_auth.AUTH_PROOF_BYTES),
        },
    }


def test_authenticated_hello_reseeds_schedule_snapshot_on_reconnect() -> None:
    async def go() -> None:
        server = Server()
        inventory = Inventory()
        server.window_schedule = inventory
        server.operator_credential_registry = AcceptingRegistry()

        first = object()
        first_snapshot = (await server._on_hello(_authenticated_hello(server, first)))[1]
        second = object()
        second_snapshot = (await server._on_hello(_authenticated_hello(server, second)))[1]

        assert first_snapshot["schedules"] == second_snapshot["schedules"]
        assert first_snapshot["schedules"][0]["prompt_preview"] == "trusted preview"
        assert inventory.calls == 2

    asyncio.run(go())


@pytest.mark.parametrize("connection_kind", ["authless", "token_client", "peer"])
def test_hello_schedule_snapshot_is_default_deny(connection_kind: str) -> None:
    async def go() -> None:
        server = Server()
        inventory = Inventory()
        server.window_schedule = inventory
        websocket = object()
        if connection_kind == "token_client":
            server._client_authenticated_streams[websocket] = "hosta:seat"
            server._client_identities[websocket] = "public-client"
        elif connection_kind == "peer":
            server._connection_trust[websocket] = _trust("public-client")

        snapshot = (await server._on_hello({
            "type": "hello",
            "client": "public-client" if connection_kind != "authless" else "",
            "_client_websocket": websocket,
        }))[1]

        assert "schedules" not in snapshot
        assert inventory.calls == 0

    asyncio.run(go())


@pytest.mark.parametrize("frame_type", ["schedule.inventory", "schedule.lifecycle"])
def test_registered_broadcast_recipient_gets_schedule_frames_only_after_operator_auth(
    frame_type: str,
) -> None:
    async def go() -> None:
        server = Server()
        pre_hello = object()
        token_cli = object()
        peer = object()
        trusted = object()
        # Broadcast registration precedes hello. Keep all four in the same
        # subscription shape so the trust bit in grouping is exercised.
        server._clients = [pre_hello, token_cli, peer, trusted]  # type: ignore[assignment]
        server._client_authenticated_streams[token_cli] = "hosta:seat"
        server._connection_trust[peer] = _trust("public-client")
        server._connection_trust[trusted] = _trust()
        sent: list[tuple[object, str, dict[str, Any]]] = []

        def enqueue(websocket: object, emitted_type: str, encoded: str) -> bool:
            sent.append((websocket, emitted_type, json.loads(encoded)))
            return True

        server._enqueue = enqueue  # type: ignore[method-assign]
        payload = {
            "type": frame_type,
            "schedules": [{"schedule_id": "example-schedule"}],
            "schedule": {"schedule_id": "example-schedule", "prompt_preview": "placeholder"},
        }
        await server.broadcast(payload)

        assert [(websocket, emitted_type) for websocket, emitted_type, _ in sent] == [
            (trusted, frame_type),
        ]

    asyncio.run(go())
