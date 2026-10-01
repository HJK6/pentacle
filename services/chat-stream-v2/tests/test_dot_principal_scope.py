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
    """A fake remote websocket (non-loopback; RFC 5737 TEST-NET address)."""

    def __init__(self, address="198.51.100.92"):
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
        # `ping` is a base-allowed verb (handshake/keepalive), reachable
        # independent of the read toggle; it proves the token is honoured over
        # daemon-terminated TLS. (The read verb `list_sessions` is covered by the
        # read-toggle test below and is denied by default.)
        daemon.handlers["ping"] = lambda m: _record(calls, "ping.ok")

        peer = Peer()
        daemon._tls_connections.add(peer)  # daemon-terminated TLS connection
        reply = await daemon._dispatch(_dot_frame("ping"), websocket=peer)
        assert reply[0]["type"] == "ping.ok"
        assert calls == ["ping.ok"]

    asyncio.run(run())


def test_dot_read_verb_denied_by_default_enabled_by_toggle():
    """v1: `list_sessions` is denied by default (read OFF); the
    `PENTACLE_DOT_READ_ENABLED`/`dot_read_enabled` toggle re-enables it."""
    async def run():
        # Default: read disabled -> list_sessions denied before the handler.
        store, _ = _open_token_store()
        off = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        assert off.dot_read_enabled is False
        reached = []
        off.handlers["list_sessions"] = _passthrough(reached, "list_sessions")
        peer = Peer()
        off._tls_connections.add(peer)
        reply = await off._dispatch(_dot_frame("list_sessions"), websocket=peer)
        assert reply[0]["error_code"] == DOT_SCOPE_DENIED_CODE
        assert reached == []

        # Toggle ON: the projected read verb is reachable again.
        store2, _ = _open_token_store()
        on = Server(store=store2, dot_principal_stream_ids=[DOT_ID], dot_read_enabled=True)
        assert on.dot_read_enabled is True
        on_reached = []
        on.handlers["list_sessions"] = _passthrough(on_reached, "list_sessions")
        peer2 = Peer()
        on._tls_connections.add(peer2)
        reply2 = await on._dispatch(_dot_frame("list_sessions"), websocket=peer2)
        assert reply2[0]["type"] == "list_sessions.ok"
        assert on_reached == ["list_sessions"]

    asyncio.run(run())


def test_dot_read_toggle_reads_env(monkeypatch):
    monkeypatch.setenv("PENTACLE_DOT_READ_ENABLED", "true")
    assert Server(dot_principal_stream_ids=[DOT_ID]).dot_read_enabled is True
    monkeypatch.setenv("PENTACLE_DOT_READ_ENABLED", "0")
    assert Server(dot_principal_stream_ids=[DOT_ID]).dot_read_enabled is False
    monkeypatch.delenv("PENTACLE_DOT_READ_ENABLED", raising=False)
    assert Server(dot_principal_stream_ids=[DOT_ID]).dot_read_enabled is False


# --------------------------------------------------------------------------- #
# Default-deny verb surface, proven over the whole registry.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("read_enabled", [False, True])
def test_dot_default_deny_over_registry(read_enabled):
    """Only the EFFECTIVE allowlist is reachable for a Dot principal: the base
    verbs (ping/hello/send) always, plus the read verb (list_sessions) ONLY when
    the read toggle is on. Every other registered handler denies by default."""
    from server import DOT_BASE_ALLOWED_VERBS, DOT_READ_VERBS

    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID],
                        dot_read_enabled=read_enabled)
        effective = DOT_BASE_ALLOWED_VERBS | (DOT_READ_VERBS if read_enabled else frozenset())
        peer = Peer()
        daemon._tls_connections.add(peer)
        reached = []
        # Replace every registered handler with a reach-recording passthrough so a
        # verb that slips past the gate is observable.
        for verb in list(daemon.handlers):
            daemon.handlers[verb] = _passthrough(reached, verb)

        for verb in sorted(daemon.handlers):
            reply = await daemon._dispatch(_dot_frame(verb), websocket=peer)
            if verb in effective:
                assert reply[0]["type"] == f"{verb}.ok", verb
            else:
                # Denied before the handler, by some denial code; handler never ran.
                assert reply[0].get("error_code"), verb
                assert verb not in reached, verb
        # Only the effective allowlist was ever reached. With read OFF (default),
        # list_sessions is NOT reachable.
        assert set(reached) <= effective
        if not read_enabled:
            assert "list_sessions" not in reached

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
        # The message is delivered to the resolved live backend...
        sent = comms.sent[0]
        assert sent["to_stream_id"] == BACKEND          # resolved at send time
        assert sent["from_stream_id"] == DOT_ID          # drives [from dot]
        assert not sent["_auth_context"].get("operator_authenticated")  # not operator
        # ...but the ACK to Dot discloses NO fleet data: only the stable composite
        # id + safe delivery status; never the resolved backend seat/host/name.
        assert res["delivery"] == "landed"
        assert res["to_stream_id"] == "bart:assistant"
        assert BACKEND not in json.dumps(res)
        for leaked in ("delivered_to_binding", "host", "session_name"):
            assert leaked not in res, leaked

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
        # Probe with a base-allowed verb so the pre-revocation call succeeds
        # regardless of the (default-off) read toggle.
        daemon.handlers["ping"] = lambda m: _record([], "ping.ok")

        first = await daemon._dispatch(_dot_frame("ping"), websocket=peer)
        assert first[0]["type"] == "ping.ok"
        # Revoke the seat token (e.g. the seat was closed): next RPC is refused,
        # even though the connection stayed open.
        holder["status"] = ""  # token no longer resolvable / open
        after = await daemon._dispatch('{"type":"ping","request_id":"after"}', websocket=peer)
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
        for verb in ("ping", "spawn", "close"):
            daemon.handlers[verb] = _passthrough(reached, verb)
        peer = Peer("127.0.0.1")   # loopback
        daemon._tls_connections.add(peer)

        # First RPC authenticates the Dot and marks the connection sticky
        # (base-allowed `ping`, independent of the read toggle).
        first = await daemon._dispatch(_dot_frame("ping"), websocket=peer)
        assert first[0]["type"] == "ping.ok"
        assert peer in daemon._client_dot_connections
        reached.clear()  # ignore the legitimate pre-revocation call

        # Revoke; a loopback RPC with no token must NOT reach the handler.
        holder["status"] = ""
        for verb in ("ping", "spawn", "close"):
            reply = await daemon._dispatch(
                json.dumps({"type": verb, "request_id": f"rev-{verb}"}), websocket=peer,
            )
            assert reply[0]["error_code"] == "authentication_required", verb
        assert reached == []

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Broadcasts: a Dot connection only ever gets the projected inventory, over TLS.
# --------------------------------------------------------------------------- #

def test_dot_broadcasts_none_when_read_disabled():
    """v1 default (read OFF): a Dot connection receives NO broadcast frame at
    all — not even the projected inventory. This closes the subscription/push
    read path and means Bart's reply frames never reach Dot over wss."""
    daemon = Server(dot_principal_stream_ids=[DOT_ID])  # read OFF by default
    assert daemon.dot_read_enabled is False
    tls_peer = Peer()
    daemon._tls_connections.add(tls_peer)
    daemon._client_dot_connections.add(tls_peer)
    daemon._client_authenticated_streams[tls_peer] = DOT_ID
    for ftype, payload in (
        ("session.inventory", {"sessions": [{"stream_id": "x", "visibility": "default",
                                             "objective": "o", "last_text": "SECRET"}]}),
        ("chat.event", {"event": {"stream_id": "x"}}),
        ("working.state", {"stream_id": "x"}),
        ("notification", {"notification": {}}),
        ("hosts.stats", {"hosts": {"thoth": {"cpu": 1}}}),
    ):
        assert daemon._frame_for_client(tls_peer, ftype, payload) is None, ftype


def test_dot_broadcasts_projected_inventory_when_read_enabled():
    daemon = Server(dot_principal_stream_ids=[DOT_ID], dot_read_enabled=True)
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
    # Every other broadcast type is withheld even with read ON.
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


# --------------------------------------------------------------------------- #
# v1 delta: the hello RESPONSE discloses no fleet data (full frame sequence),
# and Dot-bound error frames are code-only. (spec-QA cycle-1 F1 + F2.)
# --------------------------------------------------------------------------- #

def test_dot_hello_response_empty_and_no_hosts_stats():
    """A Dot connection's hello returns exactly [hello, empty-snapshot]: no fleet
    sessions/notifications/working_states AND no directly-appended hosts.stats
    frame (which carries live host telemetry and bypasses _frame_for_client).
    The assertion scans EVERY returned frame, not only the snapshot event."""
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID],
                        local_host="thoth-secret-hostid")
        # Seed host telemetry so a leaked hosts.stats frame would be detectable.
        daemon._host_stats["thoth"] = {"cpu": 0.9, "secret_host_metric": "LEAKME"}
        peer = Peer()
        daemon._tls_connections.add(peer)
        frames = await daemon._dispatch(_dot_frame(
            "hello", client="agent-orch",
            subscribe={"all": True, "include_subagents": True},
        ), websocket=peer)
        types = [f.get("type") for f in frames]
        assert types == ["hello", "snapshot"], types   # NO hosts.stats third frame
        snap = frames[1]
        assert snap["sessions"] == []
        assert snap["notifications"] == []
        assert snap["working_states"] == {}
        assert snap["hosts"] == {}
        # No host telemetry OR host id anywhere in the whole sequence.
        blob = json.dumps(frames)
        assert "hosts.stats" not in blob
        assert "LEAKME" not in blob
        assert "secret_host_metric" not in blob
        assert "thoth-secret-hostid" not in blob   # consent_host_id must not leak the host
        assert "consent_host_id" not in blob

    asyncio.run(run())


def test_dot_hello_still_denied_over_plain_ws():
    """Hardening the hello response must not weaken the TLS-only gate."""
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, dot_principal_stream_ids=[DOT_ID])
        peer = Peer()  # NOT in _tls_connections -> plain ws
        frames = await daemon._dispatch(_dot_frame(
            "hello", subscribe={"all": True}), websocket=peer)
        assert frames[0]["error_code"] == DOT_REQUIRES_TLS_CODE

    asyncio.run(run())


class _RaisingComms:
    """comms.send that fails with an exception carrying fleet identifiers."""
    def __init__(self):
        self.sent = []
    async def send(self, msg):
        self.sent.append(msg)
        raise RuntimeError("backend thoth:v2-SECRETBACKEND unreachable at 10.9.9.9")


def test_dot_send_error_frame_is_code_only():
    """A Dot send failure returns a code-only error frame: no free-form error
    text or extras, so backend/internal identifiers never egress to Dot."""
    async def run():
        store, _ = _open_token_store()
        daemon = Server(store=store, sessions=_FakeSessions(), comms=_RaisingComms(),
                        dot_principal_stream_ids=[DOT_ID])
        daemon.assistant_composite = _FakeComposite(BACKEND)
        peer = Peer()
        daemon._tls_connections.add(peer)
        frames = await daemon._dispatch(_dot_frame(
            "send", to_stream_id="bart:assistant", text="handoff"), websocket=peer)
        err = frames[0]
        assert err["type"] == "send.error"
        assert err["error_code"] == "internal_error"
        assert err.get("request_id") == "send-req"
        # Code-only: no free-form text / extras, synthetic identifiers absent.
        assert set(err) <= {"type", "error_code", "request_id"}
        blob = json.dumps(err)
        assert "SECRETBACKEND" not in blob and "10.9.9.9" not in blob


    asyncio.run(run())


def test_dot_scrub_outbound_unit():
    """The scrub reduces any *.error to code-only and leaves acks untouched."""
    scrub = Server._dot_scrub_outbound
    err = scrub({"type": "send.error", "error_code": "boom",
                 "error": "SECRET thoth:v2-x", "extra_field": "SECRET", "request_id": "r1"})
    assert err == {"type": "send.error", "error_code": "boom", "request_id": "r1"}
    ok = {"type": "send.result", "to_stream_id": "bart:assistant", "delivery": "landed"}
    assert scrub(ok) == ok   # non-error passes through unchanged


def test_dot_error_scrub_does_not_apply_to_non_dot_connection():
    """A non-Dot connection still receives full error detail (no regression)."""
    async def run():
        daemon = Server(dot_principal_stream_ids=[DOT_ID])
        def _boom(_m):
            raise VerbError("boom", "SECRET thoth:v2-detail leaked")
        daemon.handlers["ping"] = _boom
        peer = Peer("127.0.0.1")  # loopback, NOT a Dot connection
        frames = await daemon._dispatch(
            json.dumps({"type": "ping", "request_id": "p1"}), websocket=peer)
        assert frames[0]["error_code"] == "boom"
        assert "SECRET thoth:v2-detail leaked" in json.dumps(frames[0])  # not scrubbed

    asyncio.run(run())
