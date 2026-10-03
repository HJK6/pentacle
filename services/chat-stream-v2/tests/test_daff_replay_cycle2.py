"""Corrected-scope cycle 2 (ruling daff-foundation-replay-cycle2): B/E direct-input
replay across the SUPPORTED manual rebind path and durable admission of correlated
dead-window input (reply_to_message_id and reply_to_question_id), preserving the
question-answer lifecycle, stale/invalid correlation rejection, order and the
existing automatic-recovery path.  Disposable store/seats; no live daemon.
"""
from __future__ import annotations

import asyncio
import json

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store


LOCAL = "fixture-host"
DAFF_CHAT = "daff:assistant"
S1 = "fixture-daff:s1"
RECOVERY_SPEC = "spec_example__daff_recovery"


def _daff_config(seat_stream, generation):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_CHAT,
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": seat_stream,
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": generation,
        "PENTACLE_ASSISTANT_DAFF_REBIND_AUTHORIZED_SPEC_IDS": json.dumps([RECOVERY_SPEC]),
    }, name="daff", env_prefix="DAFF_")


async def _seat(store, stream_id):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high")


async def _spec_seat(store, stream_id):
    """A live successor carrying the recovery spec, authorized to rebind."""
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol", effective_effort="high",
        spec_id=RECOVERY_SPEC, qualified_spec_ids=[RECOVERY_SPEC],
        spec_binding_provenance=[{"spec_id": RECOVERY_SPEC, "provenance": "spawn_explicit",
                                  "granting_principal": "operator", "granted_at": "2026-10-03T00:00:00Z"}])


async def _preserve_dead(store, stream_id, row):
    """Mark the bound pane preserved-dead exactly as the reconciler's protected
    branch does: status stays 'open' but presumed_dead_at is set."""
    host, name = stream_id.split(":", 1)
    await store.update_session(
        host, name, expected_generation=row.get("created_at"),
        presumed_dead_at="2026-10-03T00:00:00Z")


async def _build(store, *, dispatch, question_answer=None):
    daff_seat = await _seat(store, S1)
    daff = AssistantComposite(
        store, config=_daff_config(S1, daff_seat["session_generation"]),
        dispatch=dispatch, question_answer=question_answer)
    await daff.load_binding()
    await daff.ensure_projection()
    sessions = Sessions(store, tmux=None, local_host=LOCAL)
    await sessions.refresh()
    server = Server(store=store, sessions=sessions, local_host=LOCAL)
    server.assistant_composites = {"daff": daff}
    server.assistant_composite = daff
    return daff, daff_seat, sessions, server


async def _rebind_to_successor(server, successor_stream, successor_row, *, revision=0, request_id="rb1"):
    return await server._on_assistant_rebind({
        "type": "assistant.rebind", "composite_stream_id": DAFF_CHAT,
        "request_id": request_id, "expected_revision": revision,
        "target_stream_id": successor_stream,
        "target_generation": successor_row["session_generation"],
        "_auth_context": {"token_verified": True, "stream_id": successor_stream,
                          "session_generation": successor_row["session_generation"]},
    })


async def _await_route(store, input_id, predicate, tries=100):
    route = None
    for _ in range(tries):
        route = await store.get_assistant_composite_route(stream_id=DAFF_CHAT, input_identity=input_id)
        if route and predicate(route):
            return route
        await asyncio.sleep(0.01)
    return route


def test_manual_rebind_replays_queued_direct_input_to_successor():
    """Defect (a): a direct input re-queued during a dead window must replay to the
    live successor through the SUPPORTED Server._on_assistant_rebind path."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            dispatched = []

            async def dispatch(route):
                dispatched.append(dict(route))
                return {"delivery": "landed"}

            daff, s1, _sessions, server = await _build(store, dispatch=dispatch)
            await _preserve_dead(store, S1, s1)

            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "during-dead", "msg_id": "m1", "request_id": "r1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            queued = await _await_route(store, "m1",
                                        lambda r: r["routing_state"] == "queued")
            assert queued["routing_state"] == "queued"
            assert not dispatched

            s2 = await _spec_seat(store, "fixture-daff:s2")
            await server.sessions.refresh()
            receipt = await _rebind_to_successor(server, "fixture-daff:s2", s2)
            assert receipt["new_binding"]["stream_id"] == "fixture-daff:s2"

            resolved = await _await_route(store, "m1",
                                          lambda r: r["routing_state"] == "resolved")
            assert resolved["routing_state"] == "resolved"
            assert resolved["route_target_generation"] == s2["session_generation"]
            assert dispatched and dispatched[0]["route_target"] == "fixture-daff:s2"
        finally:
            store.stop()

    asyncio.run(run())


def test_correlated_message_reply_admitted_and_replayed_during_dead_window():
    """Defect (b), message form: a valid reply_to_message_id during a dead window
    must be admitted durably (USER event + route persisted), not refused."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            dispatched = []

            async def dispatch(route):
                dispatched.append(dict(route))
                return {"delivery": "landed"}

            daff, s1, _sessions, server = await _build(store, dispatch=dispatch)
            # A prior direct input exists to correlate against.
            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "prior", "msg_id": "m0", "request_id": "r0",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            await _await_route(store, "m0", lambda r: r["routing_state"] == "resolved")

            await _preserve_dead(store, S1, s1)
            # The correlated reply must be admitted, not refused.
            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "my reply", "msg_id": "m1", "request_id": "r1",
                "reply_to_message_id": "m0",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            route = await store.get_assistant_composite_route(stream_id=DAFF_CHAT, input_identity="m1")
            assert route is not None  # durably admitted
            events = await store.fetch_session_event_tail(DAFF_CHAT, limit=20)
            assert any(e["kind"] == "USER" and e.get("text") == "my reply" for e in events)

            # And it replays to the successor after a manual rebind.
            s2 = await _spec_seat(store, "fixture-daff:s2")
            await server.sessions.refresh()
            await _rebind_to_successor(server, "fixture-daff:s2", s2)
            resolved = await _await_route(store, "m1", lambda r: r["routing_state"] == "resolved")
            assert resolved["route_target_generation"] == s2["session_generation"]
        finally:
            store.stop()

    asyncio.run(run())


def test_correlated_question_reply_admits_and_completes_answer_lifecycle():
    """Defect (b), question form: a valid reply_to_question_id during a dead window
    must be admitted durably AND complete the question-answer lifecycle (not be
    discarded), deferring only the pane dispatch to replay."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            dispatched = []
            answered = []

            async def dispatch(route):
                dispatched.append(dict(route))
                return {"delivery": "landed"}

            async def question_answer(question_id, body, msg):
                answered.append((question_id, body))
                return {"ok": True}

            daff, s1, _sessions, server = await _build(
                store, dispatch=dispatch, question_answer=question_answer)
            # Seed a lane with a pending question bound to the (soon dead) target.
            await store.submit(lambda conn: conn.execute(
                """INSERT INTO v2_assistant_composite_lanes
                   (lane_id, stream_id, phase, bound_stream_id, bound_generation,
                    pending_question_id, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("lane-1", DAFF_CHAT, "waiting", S1, s1["session_generation"],
                 "q-1", 1, "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z")))

            await _preserve_dead(store, S1, s1)
            await daff.accept_input({
                "text": "yes continue", "msg_id": "ans-1", "request_id": "r-ans",
                "reply_to_question_id": "q-1",
                "_auth_context": {"operator_authenticated": True},
            }, operator_principal="operator:fixture")

            route = await store.get_assistant_composite_route(stream_id=DAFF_CHAT, input_identity="ans-1")
            assert route is not None  # durably admitted during the dead window
            assert answered == [("q-1", "yes continue")]  # lifecycle completed, not discarded
        finally:
            store.stop()

    asyncio.run(run())


def test_dead_window_replay_preserves_submission_order_under_delay():
    """Multiple inputs queued during a dead window replay to the single target pane
    in admission order at the SUBMISSION boundary — even when the first dispatch is
    slow — and each is delivered exactly once (a same-identity retry is a no-op).

    The dispatch callback records the input identity it is given and holds the first
    dispatch behind a gate; if the worker did not serialize direct-primary dispatch,
    a later input could submit before the gated first one (observed order [b,a]).
    """
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            submitted = []
            gate = asyncio.Event()

            async def dispatch(route):
                envelope = json.loads(route["route_json"]).get("direct_envelope", {})
                submitted.append(envelope.get("reply_to_message_id"))
                if len(submitted) == 1:
                    await gate.wait()  # hold the first; a later one must not overtake it
                return {"delivery": "landed"}

            daff, s1, _sessions, server = await _build(store, dispatch=dispatch)
            await _preserve_dead(store, S1, s1)
            for mid in ("a", "b", "c"):
                await server._on_send({
                    "to_stream_id": DAFF_CHAT, "text": mid, "msg_id": mid, "request_id": f"r-{mid}",
                    "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
                })
            # A retry of the first input (new transport id) must not create a 2nd route.
            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "a", "msg_id": "a", "request_id": "r-a-retry",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            for mid in ("a", "b", "c"):
                assert (await _await_route(store, mid, lambda r: r["routing_state"] == "queued"))["routing_state"] == "queued"
            assert not submitted

            s2 = await _spec_seat(store, "fixture-daff:s2")
            await server.sessions.refresh()
            await _rebind_to_successor(server, "fixture-daff:s2", s2)

            # The worker submits 'a' (gated) and must NOT submit 'b'/'c' until 'a'
            # completes — serialized through the single pane.
            for _ in range(50):
                if submitted:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            assert submitted == ["a"]  # not overtaken while the first is in flight

            gate.set()
            for _ in range(200):
                if len(submitted) >= 3:
                    break
                await asyncio.sleep(0.01)
            assert submitted == ["a", "b", "c"]  # exactly once each, admission order

            for mid in ("a", "b", "c"):
                route = await _await_route(store, mid, lambda r: r["routing_state"] == "resolved")
                assert route["route_target_generation"] == s2["session_generation"]
        finally:
            store.stop()

    asyncio.run(run())


def test_automatic_recovery_path_replays_queued_direct_input():
    """Regression: the existing automatic-recovery entry point (recover_assistant_binding
    + load_binding + composite.recover) still replays a dead-window direct input to
    the live successor, unchanged by the manual-rebind repair."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            dispatched = []

            async def dispatch(route):
                dispatched.append(dict(route))
                return {"delivery": "landed"}

            daff, s1, _sessions, server = await _build(store, dispatch=dispatch)
            await _preserve_dead(store, S1, s1)
            await server._on_send({
                "to_stream_id": DAFF_CHAT, "text": "during-dead", "msg_id": "m1", "request_id": "r1",
                "_auth_context": {"operator_authenticated": True, "operator_principal": "operator:fixture"},
            })
            await _await_route(store, "m1", lambda r: r["routing_state"] == "queued")
            assert not dispatched

            # Daemon-owned recovery rebind (not the operator assistant.rebind path).
            s2 = await _seat(store, "fixture-daff:s2")
            await store.recover_assistant_binding(
                name="daff", target_stream_id="fixture-daff:s2",
                target_generation=s2["session_generation"])
            await daff.load_binding()
            await daff.recover()

            resolved = await _await_route(store, "m1", lambda r: r["routing_state"] == "resolved")
            assert resolved["route_target_generation"] == s2["session_generation"]
            assert dispatched and dispatched[0]["route_target"] == "fixture-daff:s2"
        finally:
            store.stop()

    asyncio.run(run())


def test_stale_question_correlation_still_rejected_during_dead_window():
    """The repair must NOT swallow invalid/stale correlation: a reply to a question
    whose lane is bound to a different generation is still refused."""
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            async def dispatch(route):
                return {"delivery": "landed"}

            async def question_answer(question_id, body, msg):
                return {"ok": True}

            daff, s1, _sessions, server = await _build(
                store, dispatch=dispatch, question_answer=question_answer)
            # Lane bound to a DIFFERENT (stale) generation than the current binding.
            await store.submit(lambda conn: conn.execute(
                """INSERT INTO v2_assistant_composite_lanes
                   (lane_id, stream_id, phase, bound_stream_id, bound_generation,
                    pending_question_id, version, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                ("lane-stale", DAFF_CHAT, "waiting", S1, "a-different-generation",
                 "q-stale", 1, "2026-10-03T00:00:00Z", "2026-10-03T00:00:00Z")))
            await _preserve_dead(store, S1, s1)
            try:
                await daff.accept_input({
                    "text": "answer", "msg_id": "ans-stale", "request_id": "r-stale",
                    "reply_to_question_id": "q-stale",
                    "_auth_context": {"operator_authenticated": True},
                }, operator_principal="operator:fixture")
            except ValueError as exc:
                assert str(exc) == "assistant_direct_question_stale"
            else:
                raise AssertionError("stale question correlation was not rejected")
            assert await store.get_assistant_composite_route(stream_id=DAFF_CHAT, input_identity="ans-stale") is None
        finally:
            store.stop()

    asyncio.run(run())
