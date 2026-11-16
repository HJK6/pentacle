"""Composite tell routing (B): tell <name>:assistant reaches the bound pane,
is authorized to operator/agent only, generation-checked, and queued/flushed
across a rebind."""
from __future__ import annotations

import asyncio
import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions, VerbError
from store import Store


LOCAL = "fixture-host"
ASSISTANT_CHAT = "home:assistant"
DAFF_CHAT = "daff:assistant"
ASSISTANT_SEAT = "fixture-assistants:visible"
DAFF_SEAT = "fixture-daffs:visible"


class _Comms:
    def __init__(self):
        self.told = []

    async def tell(self, msg):
        self.told.append(dict(msg))
        return {"type": "tell.ok", "delivery_status": "delivered", "submission_confirmed": True}


class _UnconfirmedComms:
    """A Comms whose paste lands in the composer but is not yet submitted."""

    def __init__(self):
        self.told = []

    async def tell(self, msg):
        self.told.append(dict(msg))
        return {"type": "tell.ok", "delivery_status": "pasted_unsubmitted",
                "submission_confirmed": False}


async def _seat(store, stream_id):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high",
    )


def _config(name, env_prefix, stream_id, seat_stream, generation):
    return AssistantCompositeConfig.from_env({
        f"PENTACLE_ASSISTANT_{env_prefix}COMPOSITE_ENABLED": "1",
        f"PENTACLE_ASSISTANT_{env_prefix}COMPOSITE_STREAM_ID": stream_id,
        f"PENTACLE_ASSISTANT_{env_prefix}DIRECT_PRIMARY_STREAM_ID": seat_stream,
        f"PENTACLE_ASSISTANT_{env_prefix}DIRECT_PRIMARY_GENERATION": generation,
    }, name=name, env_prefix=env_prefix)


async def _server_with(store, composites):
    sessions = Sessions(store, tmux=None, local_host=LOCAL)
    comms = _Comms()
    for c in composites.values():
        await c.load_binding()
        await c.ensure_projection()
    await sessions.refresh()
    server = Server(store=store, sessions=sessions, comms=comms, local_host=LOCAL)
    server.assistant_composites = composites
    server.assistant_composite = composites.get("assistant")
    return server, comms, sessions


def test_composite_tell_delivers_both_directions_to_bound_pane():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            assistant_seat = await _seat(store, ASSISTANT_SEAT)
            daff_seat = await _seat(store, DAFF_SEAT)
            assistant = AssistantComposite(store, config=_config(
                "assistant", "", ASSISTANT_CHAT, ASSISTANT_SEAT, assistant_seat["session_generation"]))
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, daff_seat["session_generation"]))
            server, comms, sessions = await _server_with(store, {"assistant": assistant, "daff": daff})
            await sessions.refresh()

            # operator -> daff:assistant
            reply = await server._on_tell({
                "to_stream_id": DAFF_CHAT, "text": "hi daff",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            assert reply["delivery_status"] == "delivered"
            assert comms.told[-1]["to_stream_id"] == DAFF_SEAT
            assert comms.told[-1]["message"] == "hi daff"

            # daff -> home:assistant (agent stream token)
            await server._on_tell({
                "to_stream_id": ASSISTANT_CHAT, "text": "hi assistant",
                "from_stream_id": DAFF_CHAT,
                "_auth_context": {"token_verified": True, "stream_id": DAFF_SEAT},
            })
            assert comms.told[-1]["to_stream_id"] == ASSISTANT_SEAT
            assert comms.told[-1]["from_stream_id"] == DAFF_CHAT

            # assistant -> daff:assistant
            await server._on_tell({
                "to_stream_id": DAFF_CHAT, "text": "ack", "from_stream_id": ASSISTANT_CHAT,
                "_auth_context": {"token_verified": True, "stream_id": ASSISTANT_SEAT},
            })
            assert comms.told[-1]["to_stream_id"] == DAFF_SEAT
        finally:
            store.stop()

    asyncio.run(run())


def test_scoped_and_dot_clients_may_not_tell_composite():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, ASSISTANT_SEAT)
            assistant = AssistantComposite(store, config=_config(
                "assistant", "", ASSISTANT_CHAT, ASSISTANT_SEAT, seat["session_generation"]))
            server, comms, _ = await _server_with(store, {"assistant": assistant})
            for auth in ({"dot_principal": True}, {"scoped_principal": True, "token_verified": True}):
                try:
                    await server._on_tell({"to_stream_id": ASSISTANT_CHAT, "text": "x", "_auth_context": auth})
                except VerbError as exc:
                    assert exc.code == "dot_scope_denied"
                else:
                    raise AssertionError("scoped/dot tell to composite must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(run())


def test_unauthenticated_composite_tell_refused():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, ASSISTANT_SEAT)
            assistant = AssistantComposite(store, config=_config(
                "assistant", "", ASSISTANT_CHAT, ASSISTANT_SEAT, seat["session_generation"]))
            server, comms, _ = await _server_with(store, {"assistant": assistant})
            try:
                await server._on_tell({"to_stream_id": ASSISTANT_CHAT, "text": "x", "_auth_context": {}})
            except VerbError as exc:
                assert exc.code == "assistant_tell_unauthorized"
            else:
                raise AssertionError("unauthenticated composite tell must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(run())


def test_stale_generation_refused():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, ASSISTANT_SEAT)
            # Bind to a generation that does not match the live seat.
            assistant = AssistantComposite(store, config=_config(
                "assistant", "", ASSISTANT_CHAT, ASSISTANT_SEAT, "stalegen0000"))
            server, comms, _ = await _server_with(store, {"assistant": assistant})
            try:
                await server._on_tell({
                    "to_stream_id": ASSISTANT_CHAT, "text": "x",
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
                })
            except VerbError as exc:
                assert exc.code == "assistant_direct_generation_conflict"
            else:
                raise AssertionError("a stale binding generation must be refused")
            assert not comms.told
        finally:
            store.stop()

    asyncio.run(run())


def test_tell_queued_when_bound_pane_preserved_dead():
    """A protected seat the reconciler preserves (status='open' + presumed_dead_at)
    must be treated as UNBOUND for composite tells: the tell queues for replay
    after recovery rebinds, instead of being delivered to the dead generation."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            daff_seat = await _seat(store, DAFF_SEAT)
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, daff_seat["session_generation"]))
            server, comms, sessions = await _server_with(store, {"daff": daff})
            # Preserve the bound pane exactly as the reconciler's protected-seat
            # branch does (Sessions.mark_reconciled_dead): the row stays 'open'
            # but carries presumed_dead_at.
            await store.update_session(
                "fixture-daffs", "visible",
                expected_generation=daff_seat.get("created_at"),
                presumed_dead_at="2026-10-03T00:00:00Z")
            row = await store.fetch_session("fixture-daffs", "visible")
            assert row["status"] == "open" and row["presumed_dead_at"]

            reply = await server._on_tell({
                "to_stream_id": DAFF_CHAT, "text": "while-dead", "tell_id": "d1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            assert reply["queued"] is True
            assert reply["delivery_status"] == "queued_unbound"
            assert not comms.told  # never delivered to the dead pane
            queued = await store.claim_composite_tells(name="daff")
            assert [r["body"] for r in queued] == ["while-dead"]
        finally:
            store.stop()

    asyncio.run(run())


def test_flush_retains_tell_when_delivery_unconfirmed():
    """Flush must not dequeue a tell whose delivery is not committed (e.g.
    pasted_unsubmitted): a rebind could otherwise acknowledge and discard a tell
    before the target assistant receives it."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            assistant_seat = await _seat(store, ASSISTANT_SEAT)
            assistant = AssistantComposite(store, config=_config(
                "assistant", "", ASSISTANT_CHAT, ASSISTANT_SEAT, assistant_seat["session_generation"]))
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, "g-daff-pending"))
            server, comms, sessions = await _server_with(store, {"assistant": assistant, "daff": daff})
            server.comms = _UnconfirmedComms()

            reply = await server._on_tell({
                "to_stream_id": DAFF_CHAT, "text": "first", "tell_id": "t0",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            assert reply["queued"] is True

            live = await store.open_session(
                "fixture-daffs", "visible", provider="codex", role="assistant",
                visibility="default", pane_status="pane_alive",
                effective_model="gpt-6-sol", effective_effort="high")
            daff2 = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, live["session_generation"]))
            await daff2.load_binding()
            server.assistant_composites["daff"] = daff2

            delivered = await server._flush_composite_tells(daff2)
            assert delivered == 0  # unconfirmed -> not counted delivered
            assert server.comms.told  # it did attempt delivery
            # The row is retained for a later (confirmed) retry.
            still = await store.claim_composite_tells(name="daff")
            assert [r["body"] for r in still] == ["first"]
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("with_lineage_proof", [False, True])
def test_direct_send_during_dead_window_requires_proved_continuation(with_lineage_proof):
    """E queued-input replay for SENDS: a direct-primary input that the router
    resolves while the bound pane is preserved-dead must be re-queued (not failed,
    not pinned to the dead generation) and replayed to the live successor after
    an exactly proved successor. A rebind alone grants no old input."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            s1 = await _seat(store, DAFF_SEAT)
            dispatched = []

            async def dispatch(route):
                dispatched.append(route)
                return {"delivery": "landed"}

            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, s1["session_generation"]),
                dispatch=dispatch)
            await daff.load_binding()
            await daff.ensure_projection()
            sessions = Sessions(store, tmux=None, local_host=LOCAL)
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, local_host=LOCAL)
            server.assistant_composites = {"daff": daff}
            server.assistant_composite = daff

            # The bound pane goes preserved-dead before the input is resolved.
            await store.update_session(
                "fixture-daffs", "visible", expected_generation=s1.get("created_at"),
                presumed_dead_at="2026-10-03T00:00:00Z")

            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "during-dead",
                "msg_id": "m1", "request_id": "r1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            # The worker re-queues the route (no dispatch to the dead generation).
            for _ in range(50):
                route = await store.get_assistant_composite_route(
                    stream_id=DAFF_CHAT, input_identity="m1")
                if route and route["routing_state"] == "queued" and route.get("error_code") == "assistant_direct_target_unbound":
                    break
                await asyncio.sleep(0.01)
            assert route["routing_state"] == "queued"
            assert not dispatched

            # Model the exact persisted handoff proof, separately from a rebind.
            s2 = await store.open_session(
                "fixture-daffs", "visible2", provider="codex", role="assistant",
                visibility="default", pane_status="pane_alive",
                effective_model="gpt-6-sol", effective_effort="high")
            if with_lineage_proof:
                def record(conn):
                    conn.execute(
                        "INSERT INTO v2_assistant_direct_handoff_proofs VALUES(?,?,?,?,?)",
                        ("fixture-daffs:visible2", s2["session_generation"],
                         DAFF_SEAT, s1["session_generation"], "synthetic-proof"),
                    )
                    conn.commit()
                await store.submit(record)
            await store.recover_assistant_binding(
                name="daff", target_stream_id="fixture-daffs:visible2",
                target_generation=s2["session_generation"])
            await daff.load_binding()
            await daff.recover()  # wakes the worker
            for _ in range(100):
                route = await store.get_assistant_composite_route(
                    stream_id=DAFF_CHAT, input_identity="m1")
                if route["routing_state"] == "routing_failed" or route["routing_state"] == "resolved" and dispatched:
                    break
                await asyncio.sleep(0.01)
            if with_lineage_proof:
                assert route["routing_state"] == "resolved"
                assert route["route_target_generation"] == s2["session_generation"]
                assert len(dispatched) == 1
            else:
                assert route["routing_state"] == "routing_failed"
                assert route["error_code"] == "assistant_recovery_successor_unverified"
                assert not dispatched
        finally:
            store.stop()

    asyncio.run(run())


def test_tell_queued_while_unbound_then_flushed_in_order():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            # daff bound to a seat that is not open -> unbound for delivery.
            assistant_seat = await _seat(store, ASSISTANT_SEAT)
            assistant = AssistantComposite(store, config=_config(
                "assistant", "", ASSISTANT_CHAT, ASSISTANT_SEAT, assistant_seat["session_generation"]))
            daff = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, "g-daff-pending"))
            server, comms, sessions = await _server_with(store, {"assistant": assistant, "daff": daff})

            # Two tells arrive while daff's pane is absent -> both queue, in order.
            for i, text in enumerate(["first", "second"]):
                reply = await server._on_tell({
                    "to_stream_id": DAFF_CHAT, "text": text, "tell_id": f"t{i}",
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
                })
                assert reply["queued"] is True
                assert reply["delivery_status"] == "queued_unbound"
            assert not comms.told
            queued = await store.claim_composite_tells(name="daff")
            assert [r["body"] for r in queued] == ["first", "second"]

            # The daff pane comes up at the bound generation; flush delivers in order.
            await store.open_session(
                "fixture-daffs", "visible", provider="codex", role="assistant",
                visibility="default", pane_status="pane_alive",
                effective_model="gpt-6-sol", effective_effort="high",
                claude_session_id=None)
            # Rebind daff's binding generation to the live seat so delivery matches.
            live = await store.fetch_session("fixture-daffs", "visible")
            daff2 = AssistantComposite(store, config=_config(
                "daff", "DAFF_", DAFF_CHAT, DAFF_SEAT, live["session_generation"]))
            await daff2.load_binding()
            server.assistant_composites["daff"] = daff2

            delivered = await server._flush_composite_tells(daff2)
            assert delivered == 2
            assert [m["message"] for m in comms.told] == ["first", "second"]
            assert [m["to_stream_id"] for m in comms.told] == [DAFF_SEAT, DAFF_SEAT]
            assert await store.claim_composite_tells(name="daff") == []
        finally:
            store.stop()

    asyncio.run(run())
