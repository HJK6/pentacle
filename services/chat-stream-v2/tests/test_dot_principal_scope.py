"""The external scoped ("Dot") principal: TLS-only, read-metadata-only,
Bart-only-messaging, default-deny, revocable.

Dot is the operator's cloud OpenAI agent on Amaterasu. The daemon — not the
client — enforces that its token works only over daemon-terminated TLS, that it
can read only an allowlisted set of metadata fields (never transcript content),
that it can message only the current assistant binding as an attributed
`[from <dot>]` handoff (never operator provenance), and that every other verb
and target is denied.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from comms import Comms
from sessions import VerbError
from server import (
    DOT_ALLOWED_VERBS,
    DOT_LIST_FIELDS,
    DOT_REQUIRES_TLS_CODE,
    DOT_SCOPE_DENIED_CODE,
    Server,
)
from store import STREAM_TOKEN_HASH_VERSION

DOT_ID = "amaterasu:dot"
TOKEN = "dot-seat-token"
BACKEND = "thoth:v2-bartbackend"


class Peer:
    """A fake remote websocket (Amaterasu-like, non-loopback)."""

    def __init__(self, address="100.104.128.92"):
        self.remote_address = (address, 54321) if address is not None else None


def _open_token_store(status_holder=None):
    """A store whose single seat token resolves to the Dot id. `status_holder`
    (a one-key dict {"status": ...}) lets a test flip the token to revoked."""
    holder = status_holder if status_holder is not None else {"status": "open"}

    async def stream_token_state(digest):
        assert digest == hashlib.sha256(TOKEN.encode()).hexdigest()
        if not holder.get("status"):
            return None
        return {
            "stream_id": DOT_ID,
            "status": holder["status"],
            "token_hash_version": STREAM_TOKEN_HASH_VERSION,
            "session_generation": "g1",
        }

    return SimpleNamespace(stream_token_state=stream_token_state), holder


def _dot_frame(verb, **extra):
    return json.dumps({
        "type": verb, "request_id": f"{verb}-req",
        "from_stream_id": DOT_ID, "stream_token": TOKEN, **extra,
    })


# --------------------------------------------------------------------------- #
# Transport: the token is honoured ONLY over daemon-terminated TLS.
# --------------------------------------------------------------------------- #

def test_dot_token_denied_over_plain_ws():
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        calls = []
        daemon.handlers["list_sessions"] = lambda m: _record(calls, "list_sessions.ok")

        peer = Peer()  # NOT in _tls_connections -> plain ws
        reply = await daemon._dispatch(_dot_frame("list_sessions"), websocket=peer)
        assert reply[0]["error_code"] == DOT_REQUIRES_TLS_CODE
        assert reply[0]["request_id"] == "list_sessions-req"
        # Even the handshake verb (hello) is refused over plain ws for a Dot token.
        hello = await daemon._dispatch(
            _dot_frame("hello", subscribe={"mode": "rpc", "snapshot": False}), websocket=peer,
        )
        assert hello[0]["error_code"] == DOT_REQUIRES_TLS_CODE
        assert calls == []

    asyncio.run(run())


def test_dot_token_allowed_over_tls():
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        calls = []
        daemon.handlers["list_sessions"] = lambda m: _record(calls, "list_sessions.ok")

        peer = Peer()
        daemon._tls_connections.add(peer)  # daemon-terminated TLS connection
        reply = await daemon._dispatch(_dot_frame("list_sessions"), websocket=peer)
        assert reply[0]["type"] == "list_sessions.ok"
        assert calls == ["list_sessions.ok"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Default-deny verb surface, proven over the whole registry.
# --------------------------------------------------------------------------- #

def test_dot_default_deny_over_registry():
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        peer = Peer()
        daemon._tls_connections.add(peer)
        reached = []
        # Replace every registered handler with a reach-recording passthrough so a
        # verb that slips past the gate is observable.
        for verb in list(daemon.handlers):
            daemon.handlers[verb] = _passthrough(reached, verb)

        for verb in sorted(daemon.handlers):
            reply = await daemon._dispatch(_dot_frame(verb), websocket=peer)
            if verb in DOT_ALLOWED_VERBS:
                assert reply[0]["type"] == f"{verb}.ok", verb
            else:
                # Denied before the handler, by some denial code; handler never ran.
                assert reply[0].get("error_code"), verb
                assert verb not in reached, verb
        # Only the allowlist was ever reached.
        assert set(reached) <= DOT_ALLOWED_VERBS

    asyncio.run(run())


@pytest.mark.parametrize("verb", [
    "inspect_stream", "request_stream_events", "tell", "spawn", "close",
    "rename", "status_card",
])
def test_dot_representative_verbs_hit_dot_scope_denied(verb):
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        peer = Peer()
        daemon._tls_connections.add(peer)
        daemon.handlers[verb] = _passthrough([], verb)
        reply = await daemon._dispatch(_dot_frame(verb), websocket=peer)
        assert reply[0]["error_code"] == DOT_SCOPE_DENIED_CODE

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Read: metadata-only projection; content/transcript redacted. Non-Dot unchanged.
# --------------------------------------------------------------------------- #

def test_dot_list_projection_is_metadata_only():
    async def run():
        row = {
            "stream_id": "thoth:v2-worker", "host": "thoth", "session_name": "v2-worker",
            "visibility": "default", "role": "lead", "phase": "qa", "working": True,
            "last_event_at": "2026-09-30T00:00:00Z",
            "last_kind": "ASSIST_TEXT", "provider": "claude",
            "objective": "OBJECTIVE free text", "status_card": {"goal": "ship"},
            "display_name": "Nice Title",
            "working_label": "Waiting for SECRET-TITLE",  # title-bearing free text
            # content / transcript / internals that must never egress:
            "last_text": "SECRET TRANSCRIPT BODY", "draft": "SECRET DRAFT",
            "question": "SECRET QUESTION?", "preview": "SECRET PREVIEW",
            "pending_peer_messages": [{"from": "x", "text": "secret"}],
            "usage": {"tokens": {"output": 9}}, "agents": ["sub1"],
            "assistant_activity": {"x": 1},
        }
        sessions = SimpleNamespace(list_open=lambda: [dict(row)])
        store = SimpleNamespace(all_role_sources=_async_return({}))
        daemon = Server(store=store, sessions=sessions, dot_principal_stream_ids=[DOT_ID])
        daemon.inventory_ready.set()

        listed = await daemon._on_list_sessions({"_auth_context": {"dot_principal": True}})
        s = listed["active"][0]
        for leaked in ("last_text", "draft", "question", "preview",
                       "pending_peer_messages", "usage", "agents", "assistant_activity"):
            assert leaked not in s, leaked
        # Allowlisted free-text (classified egress) and metadata survive.
        assert s["objective"] == "OBJECTIVE free text"
        assert s["status_card"] == {"goal": "ship"}
        assert s["display_name"] == "Nice Title"
        assert s["role"] == "lead" and s["working"] is True
        assert set(s).issubset(DOT_LIST_FIELDS)
        # working_label can embed a title, so it is classified as free-text egress
        # (not pure metadata) and still projected.
        from server import DOT_FREETEXT_FIELDS, DOT_METADATA_FIELDS
        assert "working_label" in DOT_FREETEXT_FIELDS
        assert "working_label" not in DOT_METADATA_FIELDS
        assert s["working_label"] == "Waiting for SECRET-TITLE"

        # A non-Dot caller still sees the full row (no regression).
        full = await daemon._on_list_sessions({})
        assert full["active"][0]["last_text"] == "SECRET TRANSCRIPT BODY"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Write: only to the current assistant binding, attributed, not operator.
# --------------------------------------------------------------------------- #

class _FakeComposite:
    def __init__(self, backend): self._backend = backend
    def is_stream(self, sid): return str(sid or "") == "bart:assistant"
    async def binding(self):
        return {"type": "assistant.binding.ok", "stream_id": self._backend, "generation": "g"}


class _FakeComms:
    def __init__(self): self.sent = []
    async def send(self, msg):
        self.sent.append(msg)
        return {"type": "send.result", "delivery": "landed"}


class _FakeSessions:
    async def resolve(self, msg):
        sid = str(msg.get("to_stream_id") or msg.get("stream_id") or "")
        host, _, name = sid.partition(":")
        return host, name


def _dot_server_for_send(backend=BACKEND):
    store, _ = _open_token_store()
    comms = _FakeComms()
    daemon = Server(store=store, sessions=_FakeSessions(), comms=comms,
                    dot_principal_stream_ids=[DOT_ID])
    daemon.assistant_composite = _FakeComposite(backend)
    return daemon, comms


def test_dot_send_reaches_current_binding_attributed_not_operator():
    async def run():
        daemon, comms = _dot_server_for_send()
        auth = {"dot_principal": True, "token_verified": True, "stream_id": DOT_ID}
        res = await daemon._on_send({
            "to_stream_id": "bart:assistant", "text": "please orchestrate X",
            "_auth_context": auth,
        })
        assert res["delivery"] == "landed"
        assert res["delivered_to_binding"] == BACKEND
        sent = comms.sent[0]
        assert sent["to_stream_id"] == BACKEND          # resolved at send time
        assert sent["from_stream_id"] == DOT_ID          # drives [from dot]
        # Not operator provenance.
        assert not sent["_auth_context"].get("operator_authenticated")

    asyncio.run(run())


def test_dot_send_to_non_bart_target_denied():
    async def run():
        daemon, comms = _dot_server_for_send()
        auth = {"dot_principal": True, "token_verified": True, "stream_id": DOT_ID}
        with pytest.raises(VerbError) as excinfo:
            await daemon._on_send({
                "to_stream_id": "thoth:v2-someoneelse", "text": "hi",
                "_auth_context": auth,
            })
        assert excinfo.value.code == DOT_SCOPE_DENIED_CODE
        assert comms.sent == []

    asyncio.run(run())


def test_non_dot_non_operator_still_refused_at_composite():
    async def run():
        daemon, _ = _dot_server_for_send()
        auth = {"token_verified": True, "stream_id": "thoth:v2-random"}  # not dot, not operator
        with pytest.raises(VerbError) as excinfo:
            await daemon._on_send({
                "to_stream_id": "bart:assistant", "text": "x", "_auth_context": auth,
            })
        assert excinfo.value.code == "assistant_send_unauthorized"

    asyncio.run(run())


def test_dot_attribution_envelope_and_operator_cannot_spoof():
    # A token-verified Dot peer gets the [from <dot>] stamp.
    dot_msg = {"from_stream_id": DOT_ID,
               "_auth_context": {"token_verified": True, "stream_id": DOT_ID}}
    wrapped = Comms._peer_delivery_wire(dot_msg, "send", "anchor-1", "do the thing")
    assert wrapped.startswith(f"[from {DOT_ID}]")
    # An operator (operator_authenticated, no seat token) cannot forge peer attribution.
    op_msg = {"from_stream_id": DOT_ID,
              "_auth_context": {"operator_authenticated": True, "stream_id": ""}}
    assert Comms._peer_delivery_wire(op_msg, "send", "anchor-2", "x") == "x"


# --------------------------------------------------------------------------- #
# Revocation drops a LIVE connection on its next RPC (per-RPC revalidation).
# --------------------------------------------------------------------------- #

def test_dot_revocation_drops_live_connection():
    async def run():
        store, holder = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        peer = Peer()
        daemon._tls_connections.add(peer)
        daemon.handlers["list_sessions"] = lambda m: _record([], "list_sessions.ok")

        first = await daemon._dispatch(_dot_frame("list_sessions"), websocket=peer)
        assert first[0]["type"] == "list_sessions.ok"
        # Revoke the seat token (e.g. the seat was closed): next RPC is refused,
        # even though the connection stayed open.
        holder["status"] = ""  # token no longer resolvable / open
        after = await daemon._dispatch('{"type":"list_sessions","request_id":"after"}', websocket=peer)
        assert after[0]["error_code"] == "authentication_required"

    asyncio.run(run())


def test_dot_revocation_on_loopback_does_not_fall_through_to_local_access():
    """A revoked Dot on a LOOPBACK TLS socket must stay denied — it must not slip
    past the loopback exemption into unauthenticated local access. The Dot scope
    is sticky to the connection, so dot_principal flipping false on revocation
    still refuses the request."""
    async def run():
        store, holder = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        reached = []
        for verb in ("list_sessions", "spawn", "close"):
            daemon.handlers[verb] = _passthrough(reached, verb)
        peer = Peer("127.0.0.1")   # loopback
        daemon._tls_connections.add(peer)

        # First RPC authenticates the Dot and marks the connection sticky.
        first = await daemon._dispatch(_dot_frame("list_sessions"), websocket=peer)
        assert first[0]["type"] == "list_sessions.ok"
        assert peer in daemon._client_dot_connections
        reached.clear()  # ignore the legitimate pre-revocation call

        # Revoke; a loopback RPC with no token must NOT reach the handler.
        holder["status"] = ""
        for verb in ("list_sessions", "spawn", "close"):
            reply = await daemon._dispatch(
                json.dumps({"type": verb, "request_id": f"rev-{verb}"}), websocket=peer,
            )
            assert reply[0]["error_code"] == "authentication_required", verb
        assert reached == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Broadcasts: a Dot connection only ever gets the projected inventory, over TLS.
# --------------------------------------------------------------------------- #

def test_dot_broadcasts_restricted_to_projected_inventory():
    daemon = Server(dot_principal_stream_ids=[DOT_ID])
    tls_peer = Peer()
    daemon._tls_connections.add(tls_peer)
    daemon._client_dot_connections.add(tls_peer)
    daemon._client_authenticated_streams[tls_peer] = DOT_ID

    inv = daemon._frame_for_client(tls_peer, "session.inventory", {
        "sessions": [{"stream_id": "x", "visibility": "default",
                      "objective": "o", "last_text": "SECRET"}],
    })
    assert inv is not None
    assert "last_text" not in inv["sessions"][0]
    assert inv["sessions"][0].get("objective") == "o"
    # Every other broadcast type is withheld.
    assert daemon._frame_for_client(tls_peer, "chat.event", {"event": {"stream_id": "x"}}) is None
    assert daemon._frame_for_client(tls_peer, "working.state", {"stream_id": "x"}) is None
    assert daemon._frame_for_client(tls_peer, "notification", {"notification": {}}) is None

    # A Dot connection on PLAIN ws receives nothing at all, not even inventory.
    plain_peer = Peer()
    daemon._client_dot_connections.add(plain_peer)
    daemon._client_authenticated_streams[plain_peer] = DOT_ID
    assert daemon._frame_for_client(plain_peer, "session.inventory", {"sessions": []}) is None

    # A REVOKED Dot (sticky connection, auth cleared) gets nothing — not even the
    # projected inventory — and does not fall back to the normal fan-out, even on
    # a loopback socket (regression for the loopback-revocation finding).
    loop_peer = Peer("127.0.0.1")
    daemon._tls_connections.add(loop_peer)
    daemon._client_dot_connections.add(loop_peer)
    # no entry in _client_authenticated_streams == revoked / revalidation cleared
    assert daemon._frame_for_client(loop_peer, "session.inventory", {
        "sessions": [{"stream_id": "x", "visibility": "default", "last_text": "SECRET"}],
    }) is None
    assert daemon._frame_for_client(loop_peer, "chat.event", {"event": {"stream_id": "x"}}) is None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _record(calls, label):
    async def _inner():
        calls.append(label)
        return {"type": label}
    return _inner()


def _passthrough(reached, verb):
    async def _handler(_msg):
        reached.append(verb)
        return {"type": f"{verb}.ok"}
    return _handler


def _async_return(value):
    async def _inner(*_a, **_k):
        return value
    return _inner
