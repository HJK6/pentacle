"""Literal assistant-alias peer addressing for tell/send (contract 3, stable addressing only).

Each attempt resolves the then-current binding once; existing transport retry,
retention, digest and receipt semantics govern. These tests run the real Server
handlers over real Comms/Store/Sessions with a fake tmux, and count physical
inputs P, stored tell records T, distinct send receipt IDs R and send receipt
history rows H. No alias logical ledger, receipt or custody state exists.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import ASSISTANT_ALIAS, Server
from sessions import Sessions, VerbError
from test_send_semantics import FakeTmux, HOST, _new_comms

ALIAS = ASSISTANT_ALIAS
COMPOSITE_CHAT = f"{ALIAS}:assistant"
SEAT_A = f"{HOST}:seat-a"
SEAT_B = f"{HOST}:seat-b"
SENDER = f"{HOST}:seat-s"
BODY = "synthetic commissioned task"


class _NamedTmux(FakeTmux):
    """FakeTmux that records which pane each physical paste reached."""

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[tuple[str, str]] = []

    async def paste(self, name: str, text: str) -> None:
        await super().paste(name, text)
        self.inputs.append((name, text))


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmux = _NamedTmux()
        self.comms, self.store, self.sessions = _new_comms(self.tmux, tmp_path)
        self.server: Server | None = None
        self.composite: AssistantComposite | None = None
        self.gen: dict[str, str] = {}

    async def start(self, *, bound_generation: str | None = None, digest: bool = False) -> None:
        for stream in (SEAT_A, SEAT_B, SENDER):
            row = await self.sessions.open(HOST, stream.split(":", 1)[1], provider="claude",
                                          pane_status="pane_alive")
            self.gen[stream] = row["session_generation"]
        self.composite = AssistantComposite(self.store, config=AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": COMPOSITE_CHAT,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": SEAT_A,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": bound_generation or self.gen[SEAT_A],
        }, name=ALIAS, env_prefix=""))
        await self.composite.load_binding()
        await self.composite.ensure_projection()
        await self.sessions.refresh()
        self.server = Server(store=self.store, sessions=self.sessions, comms=self.comms, local_host=HOST)
        self.server.assistant_composites = {ALIAS: self.composite}
        self.server.assistant_composite = self.composite
        if digest:
            # Production wiring (main.py): tells reach the digest through the
            # ingress policy, sends through Comms.front_desk_digest.
            self.comms.assistant_ingress_policy = self.composite.suppress_routine_backend_ingress
            self.comms.front_desk_digest = self.composite.front_desk_digest

    async def move_to_b(self) -> None:
        """Synthetic plain rebind A -> B (no transfer carry, A stays open)."""
        await self.store.recover_assistant_binding(
            name=ALIAS, target_stream_id=SEAT_B, target_generation=self.gen[SEAT_B])
        await self.composite.load_binding()

    def peer(self, **extra) -> dict:
        return {"token_verified": True, "stream_id": SENDER,
                "session_generation": self.gen[SENDER], **extra}

    def inputs_at(self, stream: str) -> int:
        return sum(1 for name, _ in self.tmux.inputs if name == stream.split(":", 1)[1])

    async def receipts(self, target: str | None = None) -> tuple[int, int]:
        """(R distinct receipt ids, H receipt history rows), optionally per target."""
        def op(conn):
            where, args = ("WHERE to_stream_id=?", (target,)) if target else ("", ())
            rows = conn.execute(f"SELECT receipt_id FROM v2_send_receipts {where}", args).fetchall()
            return len({r[0] for r in rows}), len(rows)
        return await self.store.submit(op)

    async def tell_records(self) -> int:
        return await self.store.submit(
            lambda conn: conn.execute("SELECT COUNT(*) FROM v2_tell_deliveries").fetchone()[0])

    async def holds(self) -> list[tuple[str, str]]:
        return await self.store.submit(lambda conn: [
            (r[0], r[1]) for r in conn.execute(
                "SELECT notice_id, recipient_stream_id FROM v2_outbound_notices "
                "WHERE kind='front_desk_held' ORDER BY created_at, notice_id").fetchall()])

    def tell(self, **msg):
        return self.server._on_tell({"to_stream_id": ALIAS, "text": "synthetic status",
                                     "from_stream_id": SENDER, "_auth_context": self.peer(), **msg})

    def send(self, **msg):
        return self.server._on_send({
            "to_stream_id": ALIAS, "text": BODY, "msg_id": 101, "from_stream_id": SENDER,
            "inbox": {"schema_version": "v1", "msg_id": 101, "from": SENDER, "to": ALIAS,
                      "phase": None, "role_hint": None, "task": BODY, "inputs": {}, "extras": {}},
            "_auth_context": self.peer(), **msg})


def _run(tmp_path: Path, body, **start):
    async def go():
        h = _Harness(tmp_path)
        try:
            await h.start(**start)
            await body(h)
        finally:
            h.store.stop()
    asyncio.run(go())


async def _refused(coro, code: str) -> None:
    with pytest.raises(VerbError) as exc:
        await coro
    assert exc.value.code == code


# -- tell -------------------------------------------------------------------

def test_alias_tell_reaches_a_then_fresh_work_reaches_b(tmp_path):
    async def body(h):
        reply = await h.tell(tell_id="tell-example-1")
        assert reply["type"] == "tell.ok" and reply["to_stream_id"] == COMPOSITE_CHAT
        assert reply["delivery_status"] == "delivered"
        assert (h.inputs_at(SEAT_A), await h.tell_records()) == (1, 1)

        # Same retained key/body at A replays the underlying record: no new input.
        again = await h.tell(tell_id="tell-example-1")
        assert again["type"] == "tell.ok"
        assert (h.inputs_at(SEAT_A), await h.tell_records()) == (1, 1)

        await h.move_to_b()
        # Same retained key/body now resolves to B: transport conflict, P1/T1.
        await _refused(h.tell(tell_id="tell-example-1"), "tell_id_conflict")
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B), await h.tell_records()) == (1, 0, 1)
        # A fresh tell_id is new work and reaches B (it may duplicate meaning).
        fresh = await h.tell(tell_id="tell-example-2")
        assert fresh["to_stream_id"] == COMPOSITE_CHAT
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B), await h.tell_records()) == (1, 1, 2)
    _run(tmp_path, body)


def test_alias_tell_changed_body_same_key_conflicts(tmp_path):
    async def body(h):
        await h.tell(tell_id="tell-example-1")
        await _refused(h.tell(tell_id="tell-example-1", text="changed body"), "tell_id_conflict")
        assert (h.inputs_at(SEAT_A), await h.tell_records()) == (1, 1)
    _run(tmp_path, body)


def test_alias_tell_unbound_queues_with_existing_composite_semantics(tmp_path):
    async def body(h):
        await h.store.update_session(HOST, "seat-a", status="closed")
        reply = await h.tell(tell_id="tell-example-1")
        assert reply["delivery_status"] == "queued_unbound" and reply["queued"] is True
        await h.tell(tell_id="tell-example-1")  # retry while unbound
        assert (h.inputs_at(SEAT_A), await h.tell_records()) == (0, 0)
        assert len(await h.store.claim_composite_tells(name=ALIAS)) == 1
    _run(tmp_path, body)


def test_alias_tell_stale_generation_refused(tmp_path):
    async def body(h):
        await _refused(h.tell(tell_id="tell-example-1"), "assistant_direct_generation_conflict")
        assert (h.inputs_at(SEAT_A), await h.tell_records()) == (0, 0)
    _run(tmp_path, body, bound_generation="stale-generation")


def test_alias_tell_routine_text_held_for_dual_operator_peer_and_gate_passes(tmp_path):
    async def body(h):
        dual = h.peer(operator_authenticated=True, operator_principal="agent:" + SENDER,
                      operator_authority_source="stream_token")
        held = await h.tell(tell_id="tell-example-1", _auth_context=dual)
        assert held["delivery_status"] == "persisted" and held["front_desk_hold_id"]
        assert h.inputs_at(SEAT_A) == 0
        # Same-target retained replay adds no hold.
        await h.tell(tell_id="tell-example-1", _auth_context=dual)
        assert len(await h.holds()) == 1
        gate = await h.tell(tell_id="tell-example-2", text="GATE: synthetic decision", _auth_context=dual)
        assert gate["delivery_status"] == "delivered"
        assert h.inputs_at(SEAT_A) == 1
    _run(tmp_path, body, digest=True)


# -- send -------------------------------------------------------------------

def test_alias_send_lands_at_a_with_ordinary_physical_receipt(tmp_path):
    async def body(h):
        result = await h.send(request_id="request-example-1")
        assert result["type"] == "send.result" and result["delivery"] == "landed"
        assert result["to_stream_id"] == SEAT_A
        assert result["receipt_id"]
        assert h.inputs_at(SEAT_A) == 1
        assert await h.receipts() == (1, 2)  # P1/R1/H2
        # Physical lookup only; no alias receipt projection.
        assert await h.store.get_send_receipt(SEAT_A, "request-example-1") is not None
        assert await h.store.get_send_receipt(ALIAS, "request-example-1") is None
    _run(tmp_path, body)


def test_alias_send_same_request_at_a_coalesces(tmp_path):
    async def body(h):
        await h.send(request_id="request-example-1")
        replay = await h.send(request_id="request-example-1")
        assert replay["coalesced"] is True and replay["do_not_resubmit"] is True
        assert replay["reconcile_command"] == f"agent-orch send-receipt {SEAT_A} request-example-1"
        assert h.inputs_at(SEAT_A) == 1
        assert await h.receipts() == (2, 3)  # P1/R2/H3
        # A sequential independent invocation: fresh request, same msg_id/body.
        await h.send(request_id="request-example-2")
        assert h.inputs_at(SEAT_A) == 2
        assert await h.receipts() == (3, 5)  # P2/R3/H5
    _run(tmp_path, body)


def test_alias_send_separate_invocation_same_msg_id_delivers_again(tmp_path):
    async def body(h):
        await h.send(request_id="request-example-1")
        await h.send(request_id="request-example-2")
        assert h.inputs_at(SEAT_A) == 2
        assert await h.receipts() == (2, 4)  # P2/R2/H4: msg_id alone is not dedupe
    _run(tmp_path, body)


def test_alias_send_rotated_request_same_optimistic_id(tmp_path):
    async def body(h):
        await h.send(request_id="request-example-1", optimistic_id="optimistic-example-1")
        at_a = await h.send(request_id="request-example-2", optimistic_id="optimistic-example-1")
        assert at_a["coalesced"] is True
        assert (h.inputs_at(SEAT_A), *await h.receipts()) == (1, 2, 3)
        await h.move_to_b()
        at_b = await h.send(request_id="request-example-3", optimistic_id="optimistic-example-1")
        assert at_b["to_stream_id"] == SEAT_B and not at_b.get("coalesced")
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B)) == (1, 1)
        assert await h.receipts() == (3, 5)  # P2/R3/H5: independent physical receipt at B
        assert await h.receipts(SEAT_B) == (1, 2)
    _run(tmp_path, body)


def test_alias_send_same_identifiers_after_move_can_deliver_again(tmp_path):
    async def body(h):
        await h.send(request_id="request-example-1", optimistic_id="optimistic-example-1")
        await h.move_to_b()
        result = await h.send(request_id="request-example-1", optimistic_id="optimistic-example-1")
        # Explicitly allowed: physical idempotency is target scoped.
        assert result["to_stream_id"] == SEAT_B and result["delivery"] == "landed"
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B)) == (1, 1)
        assert await h.receipts() == (2, 4)  # P2/R2/H4, separate target receipts
        assert await h.receipts(SEAT_A) == (1, 2)
        assert await h.receipts(SEAT_B) == (1, 2)
    _run(tmp_path, body)


def test_alias_send_unbound_fails_before_acceptance(tmp_path):
    async def body(h):
        await h.store.update_session(HOST, "seat-a", status="closed")
        await _refused(h.send(request_id="request-example-1"), "assistant_alias_unbound")
        await _refused(h.send(request_id="request-example-1"), "assistant_alias_unbound")
        assert (len(h.tmux.inputs), *await h.receipts()) == (0, 0, 0)
        assert await h.holds() == []
        assert await h.store.claim_composite_tells(name=ALIAS) == []
        # A later new attempt at B may run.
        await h.move_to_b()
        result = await h.send(request_id="request-example-2")
        assert result["to_stream_id"] == SEAT_B and result["delivery"] == "landed"
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B), *await h.receipts()) == (0, 1, 1, 2)
    _run(tmp_path, body)


def test_alias_send_stale_generation_refused_before_acceptance(tmp_path):
    async def body(h):
        await _refused(h.send(request_id="request-example-1"), "assistant_direct_generation_conflict")
        assert (len(h.tmux.inputs), *await h.receipts()) == (0, 0, 0)
    _run(tmp_path, body, bound_generation="stale-generation")


def test_alias_send_digest_holds_routine_peer_and_plain_rebind_leaves_hold(tmp_path):
    async def body(h):
        dual = h.peer(operator_authenticated=True, operator_principal="agent:" + SENDER,
                      operator_authority_source="stream_token")
        held = await h.send(request_id="request-example-1", _auth_context=dual,
                            _assistant_composite_backend_dispatch=True,
                            _assistant_operator_authenticated=True)
        assert held["delivery"] == "persisted" and held["to_stream_id"] == SEAT_A
        assert h.tmux.inputs == []
        assert await h.receipts() == (1, 2)  # P0/R1/H2, E0
        replay = await h.send(request_id="request-example-1", _auth_context=dual)
        assert replay["coalesced"] is True
        assert await h.receipts() == (2, 3)  # same-target coalescing adds no hold
        holds = await h.holds()
        assert [recipient for _, recipient in holds] == [SEAT_A]
        await h.move_to_b()
        assert await h.holds() == holds  # plain alias resolution never moves A's hold
        again = await h.send(request_id="request-example-1", _auth_context=dual)
        assert again["to_stream_id"] == SEAT_B and again["delivery"] == "persisted"
        assert [recipient for _, recipient in await h.holds()] == [SEAT_A, SEAT_B]
        assert await h.receipts(SEAT_B) == (1, 2)  # a separate operation at B, honestly counted
        gate = await h.send(request_id="request-example-2", text="GATE: synthetic decision",
                            _auth_context=dual)
        assert gate["delivery"] == "landed" and h.inputs_at(SEAT_B) == 1
        assert (h.inputs_at(SEAT_A), *await h.receipts(SEAT_A), *await h.receipts(SEAT_B)) == (0, 2, 3, 2, 4)
    _run(tmp_path, body, digest=True)


def test_alias_send_does_not_mutate_caller_message(tmp_path):
    async def body(h):
        msg = {"to_stream_id": ALIAS, "text": BODY, "msg_id": 101, "from_stream_id": SENDER,
               "request_id": "request-example-1", "_auth_context": h.peer(),
               "inbox": {"schema_version": "v1", "msg_id": 101, "from": SENDER, "to": ALIAS,
                         "phase": None, "role_hint": None, "task": BODY, "inputs": {}, "extras": {}}}
        before = copy.deepcopy(msg)
        await h.server._on_send(msg)
        assert msg == before
    _run(tmp_path, body)


# -- authorization, target shape and compatibility ----------------------------

@pytest.mark.parametrize("auth,send_code,tell_code", [
    ({}, "assistant_send_unauthorized", "assistant_tell_unauthorized"),
    ({"operator_authenticated": True, "operator_principal": "operator:fixture"},
     "assistant_send_unauthorized", "assistant_tell_unauthorized"),
    ({"token_verified": True, "stream_id": SENDER, "dot_principal": True}, "dot_scope_denied", "dot_scope_denied"),
    ({"scoped_principal": True, "scope_stream": COMPOSITE_CHAT, "credential_id": "c"}, "scope_denied", "dot_scope_denied"),
    ({"token_verified": False, "stream_id": "", "reason_code": "wrong_seat"},
     "assistant_send_unauthorized", "assistant_tell_unauthorized"),
])
def test_alias_refuses_non_peer_authority(tmp_path, auth, send_code, tell_code):
    async def body(h):
        await _refused(h.send(request_id="request-example-1", _auth_context=auth), send_code)
        await _refused(h.tell(tell_id="tell-example-1", _auth_context=auth), tell_code)
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
    _run(tmp_path, body)


def test_alias_refuses_spoofed_sender_claim(tmp_path):
    async def body(h):
        await _refused(h.send(request_id="request-example-1", from_stream_id=SEAT_B), "assistant_send_unauthorized")
        await _refused(h.tell(tell_id="tell-example-1", from_stream_id=SEAT_B), "assistant_tell_unauthorized")
        assert (len(h.tmux.inputs), *await h.receipts()) == (0, 0, 0)
    _run(tmp_path, body)


def test_alias_requires_explicit_matching_sender_claim(tmp_path):
    """A verified token is not enough: the claim must be present and equal."""
    async def body(h):
        for claim in ({"from_stream_id": ""}, {"from_stream_id": "  "}):
            await _refused(h.send(request_id="request-example-1", **claim), "assistant_send_unauthorized")
            await _refused(h.tell(tell_id="tell-example-1", **claim), "assistant_tell_unauthorized")
        missing_send = {"to_stream_id": ALIAS, "text": BODY, "msg_id": 101,
                        "request_id": "request-example-2", "_auth_context": h.peer()}
        await _refused(h.server._on_send(missing_send), "assistant_send_unauthorized")
        missing_tell = {"to_stream_id": ALIAS, "text": "synthetic status",
                        "tell_id": "tell-example-2", "_auth_context": h.peer()}
        await _refused(h.server._on_tell(missing_tell), "assistant_tell_unauthorized")
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
    _run(tmp_path, body)


def test_explicit_composite_peer_send_still_refused(tmp_path):
    async def body(h):
        await _refused(h.server._on_send({
            "to_stream_id": COMPOSITE_CHAT, "text": BODY, "msg_id": 101, "from_stream_id": SENDER,
            "request_id": "request-example-1", "_auth_context": h.peer(),
        }), "assistant_send_unauthorized")
        assert (len(h.tmux.inputs), *await h.receipts()) == (0, 0, 0)
    _run(tmp_path, body)


@pytest.mark.parametrize("extra", [
    {"host": HOST}, {"session_name": "seat-a"}, {"stream_id": SEAT_A},
    {"inbox": {"to": SEAT_A}}, {"inbox": ALIAS},
])
def test_alias_rejects_conflicting_target_members(tmp_path, extra):
    async def body(h):
        await _refused(h.send(request_id="request-example-1", **extra), "bad_request")
        await _refused(h.tell(tell_id="tell-example-1", **extra), "bad_request")
        assert await h.holds() == []
        assert await h.store.claim_composite_tells(name=ALIAS) == []
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
    _run(tmp_path, body)


@pytest.mark.parametrize("target", [ALIAS.capitalize(), ALIAS.upper(), "daff", ALIAS + " "])
def test_unsupported_bare_names_remain_invalid(tmp_path, target):
    async def body(h):
        await _refused(h.send(request_id="request-example-1", to_stream_id=target), "bad_request")
        await _refused(h.tell(tell_id="tell-example-1", to_stream_id=target), "bad_request")
        assert (len(h.tmux.inputs), *await h.receipts()) == (0, 0, 0)
    _run(tmp_path, body)


def test_old_daemon_transport_rejects_raw_alias_without_acceptance(tmp_path):
    """The pre-alias path (Sessions.resolve -> Comms) refuses the raw alias
    as bad_request with zero input/receipt/hold: the safe new-CLI/old-daemon
    compatibility result."""
    async def body(h):
        h.comms.front_desk_digest = h.composite.front_desk_digest
        await _refused(h.comms.send({"to_stream_id": ALIAS, "text": BODY, "msg_id": 101,
                                     "request_id": "request-example-1", "_auth_context": h.peer()}),
                       "bad_request")
        await _refused(h.comms.tell({"to_stream_id": ALIAS, "text": "synthetic status",
                                     "tell_id": "tell-example-1", "_auth_context": h.peer()}),
                       "bad_request")
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
        assert await h.holds() == []
    _run(tmp_path, body)


def test_legacy_server_handlers_reject_new_cli_alias_wire(tmp_path, monkeypatch):
    """Mixed versions: the new CLI wire against the pre-alias Server handlers.

    With the alias branch disabled, `_on_tell`/`_on_send` are exactly the base
    handlers (the branch is their only change), so this drives the real legacy
    Server boundary, not just Comms."""
    import server as server_module

    async def body(h):
        h.comms.assistant_ingress_policy = h.composite.suppress_routine_backend_ingress
        h.comms.front_desk_digest = h.composite.front_desk_digest
        monkeypatch.setattr(server_module, "ASSISTANT_ALIAS", "\0legacy-daemon")
        # Exact new-CLI wire shapes (see the agent-orch CLI wire tests).
        await _refused(h.server._on_send({
            "type": "send", "to_stream_id": ALIAS, "text": BODY, "msg_id": 101,
            "from_stream_id": SENDER, "retry": False, "request_id": "send-example-1",
            "inbox": {"schema_version": "v1", "msg_id": 101, "from": SENDER, "to": ALIAS,
                      "phase": None, "role_hint": None, "task": BODY, "inputs": {}, "extras": {}},
            "_auth_context": h.peer(),
        }), "bad_request")
        await _refused(h.server._on_tell({
            "type": "tell", "to_stream_id": ALIAS, "text": "synthetic status", "tell_id": "tell-example-1",
            "from_stream_id": SENDER, "ttl_seconds": 300, "request_id": "tell-request-1",
            "_auth_context": h.peer(),
        }), "bad_request")
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
        assert await h.holds() == []
        assert await h.store.claim_composite_tells(name=ALIAS) == []
    _run(tmp_path, body)


def test_alias_unavailable_without_alias_composite(tmp_path):
    async def body(h):
        h.server.assistant_composites = {}
        h.server.assistant_composite = None
        await _refused(h.send(request_id="request-example-1"), "assistant_composite_unavailable")
        await _refused(h.tell(tell_id="tell-example-1"), "assistant_composite_unavailable")
        assert (len(h.tmux.inputs), *await h.receipts()) == (0, 0, 0)
    _run(tmp_path, body)


def test_alias_send_classifier_drop_is_persisted_without_hold_or_input(tmp_path):
    async def body(h):
        dropped = await h.send(request_id="request-example-1",
                               text="context_advisory: example-other:seat crossed threshold")
        assert dropped["delivery"] == "persisted" and dropped["to_stream_id"] == SEAT_A
        assert (h.tmux.inputs, await h.holds()) == ([], [])
    _run(tmp_path, body, digest=True)


def test_alias_service_authority_keeps_existing_tell_only_shape(tmp_path):
    """A service operation may reach the alias only through its unchanged
    allowlist (no current service may send); tell keeps composite parity."""
    async def body(h):
        service = {"service_authenticated": True, "service_actor": "example-service"}
        await _refused(h.send(request_id="request-example-1", from_stream_id="example-service",
                              _auth_context=service), "assistant_send_unauthorized")
        reply = await h.tell(tell_id="tell-example-1", from_stream_id="example-service", _auth_context=service)
        assert reply["to_stream_id"] == COMPOSITE_CHAT and h.inputs_at(SEAT_A) == 1
        assert await h.receipts() == (0, 0)
    _run(tmp_path, body)


def test_alias_through_outer_dispatch_auth_and_private_field_stripping(tmp_path):
    """Wire path: the verified seat token is the only authority; forged private
    fields (auth context, backend/operator markers) are stripped before the
    alias adapter, and a tokenless loopback caller is refused."""
    import hashlib
    import json
    from server import STREAM_TOKEN_HASH_VERSION

    class Socket:
        remote_address = ("127.0.0.1", 12345)

        async def send(self, _payload):
            return None

    async def body(h):
        token = "synthetic-seat-token-s"
        await h.store.grant_stream_token(HOST, "seat-s", hashlib.sha256(token.encode()).hexdigest(),
                                         STREAM_TOKEN_HASH_VERSION)
        socket = Socket()
        h.server._register_client(socket)
        forged = {"_auth_context": {"operator_authenticated": True}, "_assistant_operator_authenticated": True,
                  "_assistant_composite_backend_dispatch": True}

        async def call(via=socket, **msg):
            frames = await h.server._dispatch(json.dumps(msg), websocket=via)
            return frames[0]

        held = await call(type="send", request_id="request-example-1", to_stream_id=ALIAS, text=BODY,
                          msg_id=101, from_stream_id=SENDER, stream_token=token, **forged)
        assert held["type"] == "send.result" and held["delivery"] == "persisted"
        assert held["to_stream_id"] == SEAT_A and h.tmux.inputs == []
        told = await call(type="tell", request_id="tell-request-1", to_stream_id=ALIAS, text="GATE: synthetic",
                          tell_id="tell-example-1", from_stream_id=SENDER, stream_token=token, **forged)
        assert told["type"] == "tell.ok" and told["to_stream_id"] == COMPOSITE_CHAT
        assert h.inputs_at(SEAT_A) == 1

        # A fresh loopback connection: no seat binding cached from the calls above.
        fresh = Socket()
        h.server._register_client(fresh)
        anonymous = await call(via=fresh, type="send", request_id="request-example-2", to_stream_id=ALIAS, text=BODY,
                               msg_id=102, **forged)
        assert anonymous["error_code"] == "assistant_send_unauthorized"
        spoofed = await call(type="send", request_id="request-example-3", to_stream_id=ALIAS, text=BODY,
                             msg_id=103, from_stream_id=SEAT_B, stream_token=token)
        assert spoofed["error_code"] == "assistant_send_unauthorized"
        # Supplied token without a sender claim, then the connection's cached
        # token binding without a claim: both refused for tell and send.
        for extra in ({"stream_token": token}, {}):
            unclaimed_send = await call(type="send", request_id="request-example-4", to_stream_id=ALIAS,
                                        text=BODY, msg_id=104, **extra)
            assert unclaimed_send["error_code"] == "assistant_send_unauthorized"
            unclaimed_tell = await call(type="tell", request_id="tell-request-2", to_stream_id=ALIAS,
                                        text="GATE: synthetic", tell_id="tell-example-2", **extra)
            assert unclaimed_tell["error_code"] == "assistant_tell_unauthorized"
        conflicting_tell = await call(type="tell", request_id="tell-request-3", to_stream_id=ALIAS,
                                      text="GATE: synthetic", tell_id="tell-example-3", from_stream_id=SENDER,
                                      stream_token=token, inbox={"to": SEAT_B})
        assert conflicting_tell["error_code"] == "bad_request"
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B), await h.tell_records()) == (1, 0, 1)
        assert (h.inputs_at(SEAT_A), *await h.receipts()) == (1, 1, 2)  # held send: R1/H2, refusals add none
    _run(tmp_path, body, digest=True)


_TARGET_SHAPES = {
    "plain": {},
    "inbox_alias": {"inbox": {"to": ALIAS}},
    "host": {"host": HOST},
    "session_name": {"session_name": "seat-a"},
    "stream_id": {"stream_id": SEAT_A},
    "inbox_other": {"inbox": {"to": SEAT_B}},
    "inbox_missing_to": {"inbox": {}},
    "inbox_not_object": {"inbox": ALIAS},
}
_CLAIMS = {"match": SENDER, "missing": None, "blank": "", "other": SEAT_B}


@pytest.mark.parametrize("verb", ["tell", "send"])
@pytest.mark.parametrize("shape", sorted(_TARGET_SHAPES))
@pytest.mark.parametrize("claim", sorted(_CLAIMS))
def test_alias_admission_is_identical_for_both_verbs(tmp_path, verb, shape, claim):
    """Cross-product of verb x target shape x sender claim, with a verified
    seat token: target shape is checked first (bad_request), then the claim
    must equal the verified seat; only a clean shape plus a matching claim is
    accepted. Nothing is accepted on any refusal."""
    async def body(h):
        msg = {"to_stream_id": ALIAS, "text": BODY if verb == "send" else "synthetic status",
               "_auth_context": h.peer(), **copy.deepcopy(_TARGET_SHAPES[shape])}
        if verb == "send":
            msg.update(msg_id=101, request_id="request-example-1")
        else:
            msg.update(tell_id="tell-example-1")
        if _CLAIMS[claim] is not None:
            msg["from_stream_id"] = _CLAIMS[claim]
        handler = h.server._on_send if verb == "send" else h.server._on_tell
        if shape not in {"plain", "inbox_alias"}:
            await _refused(handler(msg), "bad_request")
        elif claim != "match":
            await _refused(handler(msg), f"assistant_{verb}_unauthorized")
        else:
            reply = await handler(msg)
            assert reply["type"] == ("send.result" if verb == "send" else "tell.ok")
            assert h.inputs_at(SEAT_A) == 1
            return
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
        assert await h.holds() == []
        assert await h.store.claim_composite_tells(name=ALIAS) == []
    _run(tmp_path, body)


def test_allowlisted_service_keeps_its_unchanged_payload_allowlist(tmp_path, monkeypatch):
    """Services stay subject to their unchanged verb/payload allowlists: the
    brief producer's tell allowlist names only its configured composite/ops
    targets, so a raw alias is refused before any handler, while its explicit
    composite tell is unchanged. Its send stays forbidden."""
    import json

    class Socket:
        remote_address = ("127.0.0.1", 12345)

        async def send(self, _payload):
            return None

    async def body(h):
        actor = "example-host:wmi-daily-brief"
        token = "synthetic-service-token"
        token_path = tmp_path / "brief-token"
        token_path.write_text(token)
        token_path.chmod(0o600)
        config_path = tmp_path / "brief-config.json"
        config_path.write_text(json.dumps({"stream_id": actor, "token_file": str(token_path),
                                           "composite_stream_id": COMPOSITE_CHAT, "ops_stream_id": SEAT_B}))
        config_path.chmod(0o600)
        monkeypatch.setenv("PENTACLE_WMI_BRIEF_PRODUCER_CONFIG", str(config_path))
        socket = Socket()
        h.server._register_client(socket)

        async def call(**msg):
            return (await h.server._dispatch(json.dumps(msg), websocket=socket))[0]

        hello = await call(type="hello", request_id="service-hello", from_stream_id=actor, stream_token=token,
                           subscribe={"snapshot": False, "mode": "rpc"})
        assert hello["type"] == "ready", hello
        tell = {"type": "tell", "from_stream_id": actor, "stream_token": token, "text": "GATE synthetic brief",
                "ttl_seconds": 3600, "sanitize": True}
        refused = await call(**tell, request_id="service-alias", tell_id="service-alias", to_stream_id=ALIAS)
        assert refused["error_code"] == "system_producer_payload_invalid"
        refused_send = await call(type="send", request_id="service-send", to_stream_id=ALIAS, text=BODY,
                                  msg_id=101, from_stream_id=actor, stream_token=token)
        assert refused_send["error_code"] == "system_producer_forbidden"
        assert (len(h.tmux.inputs), *await h.receipts(), await h.tell_records()) == (0, 0, 0, 0)
        explicit = await call(**tell, request_id="service-explicit", tell_id="service-explicit",
                              to_stream_id=COMPOSITE_CHAT)
        assert explicit["type"] == "tell.ok" and explicit["to_stream_id"] == COMPOSITE_CHAT
        assert h.inputs_at(SEAT_A) == 1
    _run(tmp_path, body)


def test_alias_send_sampled_at_a_completes_at_a_across_concurrent_rebind(tmp_path):
    """One binding sample per attempt, no at-effect fence: a rebind to B after
    the sample but before ordinary send completes still lands at A. The next
    attempt samples B."""
    async def body(h):
        ordinary_send = h.comms.send

        async def rebind_then_send(msg):
            await h.move_to_b()
            return await ordinary_send(msg)

        h.comms.send = rebind_then_send
        raced = await h.send(request_id="request-example-1")
        assert raced["to_stream_id"] == SEAT_A and raced["delivery"] == "landed"
        assert (await h.composite.binding())["stream_id"] == SEAT_B
        h.comms.send = ordinary_send
        later = await h.send(request_id="request-example-2")
        assert later["to_stream_id"] == SEAT_B
        assert (h.inputs_at(SEAT_A), h.inputs_at(SEAT_B)) == (1, 1)
        assert await h.receipts(SEAT_A) == (1, 2)
        assert await h.receipts(SEAT_B) == (1, 2)
    _run(tmp_path, body)


def test_alias_send_preserves_payload_fields_into_ordinary_send(tmp_path):
    """The transient physical message keeps sender, msg_id, text, attachments,
    request/optimistic ids and flags; only target fields change."""
    async def body(h):
        seen = []

        async def capture(msg):
            seen.append(copy.deepcopy(msg))
            return {"type": "send.result", "delivery": "landed", "to_stream_id": msg["to_stream_id"]}

        h.comms.send = capture
        attachments = [{"blob_sha": "0" * 64, "filename": "example.png", "mime": "image/png"}]
        await h.send(request_id="request-example-1", optimistic_id="optimistic-example-1",
                     attachments=attachments, retry=True, sanitize=True, meta={"example": True})
        (physical,) = seen
        assert (physical["to_stream_id"], physical["host"], physical["session_name"]) == (SEAT_A, HOST, "seat-a")
        assert physical["inbox"]["to"] == SEAT_A and physical["inbox"]["task"] == BODY
        for key, value in (("attachments", attachments), ("request_id", "request-example-1"),
                           ("optimistic_id", "optimistic-example-1"), ("retry", True), ("sanitize", True),
                           ("meta", {"example": True}), ("msg_id", 101), ("text", BODY),
                           ("from_stream_id", SENDER)):
            assert physical[key] == value, key
        assert [key for key in physical if key.startswith("_")] == ["_auth_context"]
        assert physical["_auth_context"]["operator_authenticated"] is False
    _run(tmp_path, body)
