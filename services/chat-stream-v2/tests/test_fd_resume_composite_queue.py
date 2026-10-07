"""Front-desk resume dead window (Addendum A of
spec_pentacle__daemon_restart_continuity_2026_10). In-process pins for the two
RED cells found on the disposable daemon (`tests/soak/test_fd_resume_recovery.py`):

  A7: a peer tell to bart is delivered into the front-desk digest hold
      (`persisted`, action committed, no paste). The post-rebind flush must count
      that durable hold as delivered, or every queued tell after the first is
      stranded behind it.
  A2: an operator input sent after the reconciler closes an unprotected bound
      seat must be admitted and queued (then replayed to the resumed generation
      after the rebind), not refused as a generation conflict.
"""
from __future__ import annotations

import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store

LOCAL = "fixture-host"
BART_CHAT = "bart:assistant"
FD = "fixture-fd:front"
OPERATOR = {"operator_authenticated": True, "operator_principal": "operator:fixture"}


class _HeldComms:
    """Comms whose tell to the front desk is held by the digest (live behaviour):
    a durable held notice row plus its id in the reply. `drop` models a digest
    drop: the same persisted reply with no durable row."""

    def __init__(self, store, drop=()):
        self.store = store
        self.drop = set(drop)
        self.told = []

    async def tell(self, msg):
        self.told.append(dict(msg))
        reply = {"type": "tell.ok", "delivery_status": "persisted", "submission_confirmed": False,
                 "action_committed": True, "assistant_backend_ingress": "persisted_suppressed"}
        if msg["message"] in self.drop:
            return reply
        nid = f"frontdesk-held:{msg['tell_id']}"
        try:
            await self.store.enqueue_outbound_notice(
                notice_id=nid, tell_id=nid, kind="front_desk_held", dedupe_key=nid,
                recipient_stream_id=msg["to_stream_id"], source_stream_id=msg.get("from_stream_id"),
                body=msg["message"], metadata={})
        except ValueError:
            pass  # the same tell_id recovers its retained hold
        return {**reply, "front_desk_hold_id": nid}


async def _seat(store, stream_id):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="claude", role="lead", visibility="default",
        pane_status="pane_alive", effective_model="claude-opus-5-5", effective_effort="high",
    )


def _config(generation):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": BART_CHAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": FD,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
    })


async def _close_dead(store, seat):
    """The reconciler's close of an unprotected seat whose pane is gone."""
    await store.mark_reconciled_dead(
        "fixture-fd", "front", expected_generation=seat["session_generation"],
        presumed_dead_at="2026-10-07T00:00:00Z", closed_at="2026-10-07T00:01:00Z")


def test_flush_dequeues_digest_held_tells_in_order():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, FD)
            bart = AssistantComposite(store, config=_config(seat["session_generation"]))
            await bart.load_binding()
            await bart.ensure_projection()
            sessions = Sessions(store, tmux=None, local_host=LOCAL)
            server = Server(store=store, sessions=sessions, comms=_HeldComms(store), local_host=LOCAL)
            server.assistant_composites = {"bart": bart}
            server.assistant_composite = bart
            await _close_dead(store, seat)
            for i, text in enumerate(["first", "second"]):
                reply = await server._on_tell({"to_stream_id": BART_CHAT, "text": text, "tell_id": f"t{i}",
                                               "_auth_context": OPERATOR})
                assert reply["delivery_status"] == "queued_unbound"

            resumed = await _seat(store, FD)  # resume: same stream, new generation
            bart2 = AssistantComposite(store, config=_config(resumed["session_generation"]))
            await bart2.load_binding()
            server.assistant_composites["bart"] = bart2

            delivered = await server._flush_composite_tells(bart2)
            assert delivered == 2
            assert [m["message"] for m in server.comms.told] == ["first", "second"]
            assert await store.claim_composite_tells(name="bart") == []
            # A replayed flush cannot double-deliver: nothing is left to deliver.
            assert await server._flush_composite_tells(bart2) == 0
        finally:
            store.stop()

    asyncio.run(run())


def test_flush_keeps_tell_without_durable_hold():
    """Regression control: a persisted reply with no surviving hold row (a digest
    drop) is not committed; the tell stays queued in order."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, FD)
            bart = AssistantComposite(store, config=_config(seat["session_generation"]))
            await bart.load_binding()
            await bart.ensure_projection()
            sessions = Sessions(store, tmux=None, local_host=LOCAL)
            server = Server(store=store, sessions=sessions, comms=_HeldComms(store, drop={"first"}),
                            local_host=LOCAL)
            server.assistant_composites = {"bart": bart}
            server.assistant_composite = bart
            await _close_dead(store, seat)
            for i, text in enumerate(["first", "second"]):
                await server._on_tell({"to_stream_id": BART_CHAT, "text": text, "tell_id": f"t{i}",
                                       "_auth_context": OPERATOR})
            resumed = await _seat(store, FD)
            bart2 = AssistantComposite(store, config=_config(resumed["session_generation"]))
            await bart2.load_binding()
            server.assistant_composites["bart"] = bart2
            assert await server._flush_composite_tells(bart2) == 0
            assert [r["body"] for r in await store.claim_composite_tells(name="bart")] == ["first", "second"]
        finally:
            store.stop()

    asyncio.run(run())


def test_input_after_unprotected_bound_seat_closes_is_queued_and_replayed():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, FD)
            dispatched = []

            async def dispatch(route):
                dispatched.append(route)
                return {"delivery": "landed"}

            bart = AssistantComposite(store, config=_config(seat["session_generation"]), dispatch=dispatch)
            await bart.load_binding()
            await bart.ensure_projection()
            await _close_dead(store, seat)

            receipt = await bart.accept_input(
                {"text": "while closed", "request_id": "r1", "optimistic_id": "in-closed"},
                operator_principal="operator:fixture")
            assert receipt["type"] == "assistant.send.accepted"
            for _ in range(100):
                route = await store.get_assistant_composite_route(stream_id=BART_CHAT, input_identity="in-closed")
                if route and route.get("error_code") == "assistant_direct_target_unbound":
                    break
                await asyncio.sleep(0.01)
            assert route["routing_state"] == "queued" and not dispatched

            # Resume (same stream, new generation), then the FD's CAS self-rebind.
            resumed = await _seat(store, FD)
            await store.recover_assistant_binding(name="bart", target_stream_id=FD,
                                                  target_generation=resumed["session_generation"])
            await bart.load_binding()
            await bart.recover()
            for _ in range(200):
                route = await store.get_assistant_composite_route(stream_id=BART_CHAT, input_identity="in-closed")
                if route["routing_state"] == "resolved" and dispatched:
                    break
                await asyncio.sleep(0.01)
            assert route["route_target_generation"] == resumed["session_generation"]
            assert len(dispatched) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_input_to_resumed_but_unbound_generation_is_still_refused():
    """Regression control: a LIVE seat at a generation the binding does not name
    (resumed, not yet rebound) stays a visible generation conflict."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            seat = await _seat(store, FD)
            bart = AssistantComposite(store, config=_config(seat["session_generation"]))
            await bart.load_binding()
            await bart.ensure_projection()
            await _close_dead(store, seat)
            await _seat(store, FD)
            try:
                await bart.accept_input({"text": "x", "request_id": "r2", "optimistic_id": "in-live"},
                                        operator_principal="operator:fixture")
            except ValueError as exc:
                assert str(exc) == "assistant_direct_generation_conflict"
            else:
                raise AssertionError("a live unbound generation must be refused")
        finally:
            store.stop()

    asyncio.run(run())
