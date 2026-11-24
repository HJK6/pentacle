from __future__ import annotations

import asyncio
import json

from daemon_identity import (
    MAX_PROTOCOL_VERSION, MIN_PROTOCOL_VERSION, PROTOCOL_VERSION, SCHEMA_VERSION,
)
from server import Server, _welcome_frame


def test_welcome_advertises_candidate_schema_and_protocol_identity() -> None:
    frame = _welcome_frame(nonce="n", expires_at=1, runtime_sha="a" * 40)
    assert frame["release_sha"] == frame["runtime_sha"] == "a" * 40
    assert frame["schema_version"] == SCHEMA_VERSION
    assert frame["protocol_version"] == PROTOCOL_VERSION
    assert frame["min_protocol_version"] == MIN_PROTOCOL_VERSION
    assert frame["max_protocol_version"] == MAX_PROTOCOL_VERSION


def test_legacy_hello_without_protocol_version_remains_accepted() -> None:
    server = Server(port=0)
    frames = asyncio.run(server._dispatch_inner(json.dumps({"type": "hello"})))
    assert frames
    assert frames[0]["type"] == "hello"
    assert not any(frame.get("error_code") == "unsupported_protocol" for frame in frames)


def test_unsupported_protocol_is_refused_before_hello_state_changes() -> None:
    server = Server(port=0)
    peer = object()
    frames = asyncio.run(server._dispatch_inner(json.dumps({
        "type": "hello", "client": "fast-contact-proof-v1", "protocol_version": 1,
    }), websocket=peer))
    assert frames == [{
        "type": "hello.error", "request_id": None, "error_code": "unsupported_protocol",
        "supported_protocol_min": MIN_PROTOCOL_VERSION,
        "supported_protocol_max": MAX_PROTOCOL_VERSION,
    }]
    assert peer not in server._client_identities
    assert peer not in server._contact_proof_clients
    assert peer not in server._operator_challenges
