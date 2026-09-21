"""Focused acceptance tests for the daemon-owned assistant composite stream."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from assistant_composite import AssistantComposite, AssistantCompositeConfig  # noqa: E402
from outbound_notices import OutboundNoticeQueue  # noqa: E402
from store import Store  # noqa: E402


COMPOSITE_STREAM = "fixture-host-chat:assistant"
AUTHORITY_STREAM = "fixture-host-authority:authority"
CONVERSATION_STREAM = "fixture-host-conversation:conversation"
LEAD_STREAM = "fixture-host-lead:lead"
ROUTER_ENDPOINT = "ssh://fixture-router/assistant-router-v1"


async def _open_backend(store, stream_id: str):
    host, name = stream_id.split(":", 1)
    return await store.open_session(host, name, provider="codex")


def test_router_endpoint_is_portable_required_config_only_when_enabled() -> None:
    """A disabled daemon has no fleet-name default; enabling requires an endpoint."""
    disabled = AssistantCompositeConfig.from_env({})
    assert disabled.enabled is False
    assert disabled.router_endpoint == ""

    try:
        AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "true",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "host:assistant",
            "PENTACLE_ASSISTANT_ROUTER_ACTION_PATH": "/opt/private/local_actions.py",
            "PENTACLE_ASSISTANT_ASTRA_STREAM_ID": "host:authority",
            "PENTACLE_ASSISTANT_LUNA_STREAM_ID": "host:conversation",
        })
    except ValueError as exc:
        assert str(exc) == "assistant_router_endpoint_required"
    else:  # pragma: no cover - assertion failure is the evidence
        raise AssertionError("enabled composite must require a configured router endpoint")


def test_composite_input_replays_across_rotating_rpc_request_ids() -> None:
    """The UI's stable optimistic id, not its retry request id, is immutable input identity."""

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            config = AssistantCompositeConfig(
                enabled=True,
                stream_id=COMPOSITE_STREAM,
                router_endpoint=ROUTER_ENDPOINT,
            )
            composite = AssistantComposite(store, config=config)
            projection = await composite.ensure_projection()
            assert projection["stream_id"] == COMPOSITE_STREAM
            assert projection["session_kind"] == "assistant_composite"

            first = await composite.accept_input({
                "text": "Please summarize this.", "request_id": "rpc-1",
                "optimistic_id": "stable-input-1", "attachments": [],
            })
            replay = await composite.accept_input({
                "text": "Please summarize this.", "request_id": "rpc-2",
                "optimistic_id": "stable-input-1", "attachments": [],
            })
            assert replay["route_id"] == first["route_id"]
            assert replay["duplicate"] is True

            try:
                await composite.accept_input({
                    "text": "Different text", "request_id": "rpc-3",
                    "optimistic_id": "stable-input-1", "attachments": [],
                })
            except ValueError as exc:
                assert str(exc) == "assistant_input_idempotency_conflict"
            else:  # pragma: no cover - assertion failure is the evidence
                raise AssertionError("changed payload must not reuse optimistic_id")

            events = await store.fetch_session_event_tail(COMPOSITE_STREAM, limit=10)
            assert [event["text"] for event in events] == ["Please summarize this."]
        finally:
            store.stop()

    asyncio.run(_go())


def test_routing_advances_without_waiting_for_backend_completion() -> None:
    """The one classifier is ordered; detached backend conversations are not."""

    class Router:
        async def classify(self, _route):
            return {
                "schema_version": "assistant-router/v1", "disposition": "new_topic",
                "lane_id": None, "depends_on_message_id": None, "reason": "test work",
            }

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        released = asyncio.Event()
        dispatched: list[dict] = []
        try:
            await _open_backend(store, AUTHORITY_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM,
                    router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM,
                ),
                router=Router(),
                dispatch=lambda route: _blocking_dispatch(route, dispatched, released),
            )
            await composite.ensure_projection()
            await composite.accept_input({"text": "first", "request_id": "1", "optimistic_id": "first"}, operator_principal="operator:fixture")
            await composite.accept_input({"text": "second", "request_id": "2", "optimistic_id": "second"}, operator_principal="operator:fixture")
            for _ in range(30):
                if len(dispatched) == 2:
                    break
                await asyncio.sleep(0.01)
            assert [item["body"] for item in dispatched] == ["first", "second"]
            assert not released.is_set(), "queue must have advanced before either backend completes"

            dispatch_id = dispatched[0]["dispatch_id"]
            published = await composite.publish({
                "request_id": "first-response", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": dispatch_id, "reply_to_message_id": "first",
                "reply_to_question_id": None, "publish_kind": "prose", "message": "Here is the answer.",
                "attachment_ids": [], "evidence_refs": [],
            }, actor_stream_id=AUTHORITY_STREAM)
            assert published["duplicate"] is False
            try:
                await composite.publish({
                    "request_id": "forged", "composite_stream_id": COMPOSITE_STREAM,
                    "dispatch_id": dispatch_id, "reply_to_message_id": "first",
                    "reply_to_question_id": None, "publish_kind": "prose", "message": "forged",
                    "attachment_ids": [], "evidence_refs": [],
                }, actor_stream_id="fixture-host-other:other")
            except ValueError as exc:
                assert str(exc) == "assistant_publish_provenance_unverified"
            else:  # pragma: no cover
                raise AssertionError("a concurrent backend must not publish another route's response")
            released.set()
            await asyncio.gather(*tuple(composite._dispatch_tasks))
        finally:
            store.stop()

    asyncio.run(_go())


def test_route_resolve_creates_one_correlated_intent_and_detaches_dispatch() -> None:
    """A deferred causal input resumes through the typed operation, not prose."""

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []
        released = asyncio.Event()
        try:
            astra = await _open_backend(store, AUTHORITY_STREAM)
            luna = await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM,
                    router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM,
                    luna_stream_id=CONVERSATION_STREAM,
                ),
                dispatch=lambda route: _blocking_dispatch(route, dispatched, released),
            )
            # Build the deferred row deterministically before any classifier
            # owns it; this test exercises the typed resolve boundary itself.
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            await composite.ensure_projection()
            await composite.accept_input({"text": "wait for it", "optimistic_id": "deferred-1"})
            route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="deferred-1",
            )
            assert route is not None
            await store.update_assistant_composite_route(
                route["route_id"], routing_state="fallback_dispatched", delivery_state="committed_pending",
                dispatch_id="assistant-fallback-1", route_target=CONVERSATION_STREAM,
                route_target_generation=luna["session_generation"], route_payload={
                    "kind": "luna_fallback_classifier",
                    "reason": "router_failed:ValueError",
                    "routing_context": {"message_id": "deferred-1", "open_lanes": []},
                    "router_decision_receipt_id": "assistant-router-decision-captured",
                },
            )
            result = await composite.operation({
                "operation": "route.resolve", "request_id": "resolve-1",
                "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "assistant-fallback-1",
                "reply_to_message_id": "deferred-1", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [],
                "payload": {
                    "schema_version": "assistant-router/v1", "disposition": "conversation",
                    "lane_id": None, "depends_on_message_id": None, "reason": "ordinary chat",
                },
            }, actor_stream_id=CONVERSATION_STREAM)
            assert result["duplicate"] is False
            for _ in range(30):
                if dispatched:
                    break
                await asyncio.sleep(0.01)
            assert len(dispatched) == 1
            assert dispatched[0]["route_target"] == CONVERSATION_STREAM
            assert dispatched[0]["delivery_state"] == "intent"
            resolved_route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="deferred-1",
            )
            assert resolved_route is not None
            resolved_payload = json.loads(resolved_route["route_json"])
            assert resolved_payload["router_decision_receipt_id"] == "assistant-router-decision-captured"
            assert resolved_payload["kind"] == "luna_fallback_classifier"
            assert resolved_payload["backend_context"] == {"message_id": "deferred-1", "open_lanes": []}

            replay = await composite.operation({
                "operation": "route.resolve", "request_id": "resolve-1",
                "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "assistant-fallback-1",
                "reply_to_message_id": "deferred-1", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [],
                "payload": {
                    "schema_version": "assistant-router/v1", "disposition": "conversation",
                    "lane_id": None, "depends_on_message_id": None, "reason": "ordinary chat",
                },
            }, actor_stream_id=CONVERSATION_STREAM)
            assert replay["duplicate"] is True
            assert len(dispatched) == 1
            try:
                await composite.operation({
                    "operation": "route.resolve", "request_id": "resolve-1",
                    "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "assistant-fallback-1",
                    "reply_to_message_id": "different-input", "lane_id": None, "expected_lane_version": None,
                    "evidence_refs": [],
                    "payload": {
                        "schema_version": "assistant-router/v1", "disposition": "conversation",
                        "lane_id": None, "depends_on_message_id": None, "reason": "ordinary chat",
                    },
                }, actor_stream_id=CONVERSATION_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_operation_idempotency_conflict"
            else:  # pragma: no cover
                raise AssertionError("route resolve replay changed its reply correlation")
            released.set()
            await asyncio.gather(*tuple(composite._dispatch_tasks))
        finally:
            store.stop()

    asyncio.run(_go())


def test_explicit_current_lane_reply_skips_router_and_uses_current_binding() -> None:
    """Reply metadata is a correlation, not an inference prompt."""

    class MustNotRoute:
        async def classify(self, _route):  # pragma: no cover - assertion is direct
            raise AssertionError("explicit reply must not call the router")

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []
        released = asyncio.Event()
        try:
            astra = await _open_backend(store, AUTHORITY_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
                router=MustNotRoute(),
                dispatch=lambda route: _blocking_dispatch(route, dispatched, released),
            )
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            await composite.ensure_projection()
            await store.apply_assistant_composite_operation(
                stream_id=COMPOSITE_STREAM, operation_id="admit-lane", operation="lane.admit", lane_id="lane-1",
                dispatch_id="seed-admit", actor_stream_id=AUTHORITY_STREAM, payload={
                    "mode": "new", "subject": "payments", "request_message_id": "first",
                },
            )
            await store.apply_assistant_composite_operation(
                stream_id=COMPOSITE_STREAM, operation_id="bind-lane", operation="lane.bind", lane_id="lane-1",
                dispatch_id="seed-bind", expected_lane_version=1, actor_stream_id=AUTHORITY_STREAM, payload={
                    "backend_kind": "assistant_authority",
                    "backend_stream_id": AUTHORITY_STREAM, "backend_generation": astra["session_generation"],
                },
            )
            await composite.accept_input({"text": "first", "optimistic_id": "first"})
            first = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="first")
            assert first is not None
            await store.update_assistant_composite_route(
                first["route_id"], routing_state="resolved", delivery_state="landed",
                dispatch_id="first-dispatch", route_target=AUTHORITY_STREAM,
                route_target_generation=astra["session_generation"], route_payload={
                    "schema_version": "assistant-router/v1", "disposition": "lane", "lane_id": "lane-1",
                    "depends_on_message_id": None, "reason": "active lane",
                },
            )
            await composite.accept_input({
                "text": "yes, do that", "optimistic_id": "reply", "reply_to_message_id": "first",
            })
            for _ in range(30):
                if dispatched:
                    break
                await asyncio.sleep(0.01)
            assert len(dispatched) == 1
            assert dispatched[0]["body"] == "yes, do that"
            assert dispatched[0]["route_target"] == AUTHORITY_STREAM
            reply_route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="reply",
            )
            assert reply_route is not None
            assert "router_decision_receipt_id" not in json.loads(reply_route["route_json"])
            released.set()
            await asyncio.gather(*tuple(composite._dispatch_tasks))
        finally:
            store.stop()

    asyncio.run(_go())


async def _blocking_dispatch(route, dispatched, released):
    dispatched.append(dict(route))
    await released.wait()
    return {"delivery": "landed"}


async def _admit_open_lane(store, *, lane_id: str, subject: str, operation_id: str) -> None:
    await store.apply_assistant_composite_operation(
        stream_id=COMPOSITE_STREAM, operation_id=operation_id, operation="lane.admit", lane_id=lane_id,
        dispatch_id="seed-" + operation_id, actor_stream_id=AUTHORITY_STREAM, payload={
            "mode": "new", "subject": subject, "request_message_id": operation_id,
        },
    )


async def _wait_for_dispatches(dispatched: list[dict], count: int) -> None:
    for _ in range(60):
        if len(dispatched) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} dispatches, got {len(dispatched)}")


def test_local_multilane_unbound_consent_falls_back_once_with_context_then_luna_clarifies() -> None:
    """A local guessed lane cannot turn bare consent into a task-lane dispatch."""

    class GuessedPaymentsRouter:
        async def classify(self, _route):
            return {
                "schema_version": "assistant-router/v1", "disposition": "lane",
                "lane_id": "payments", "depends_on_message_id": None, "reason": "guessed",
            }

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []

        async def dispatch(route):
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        try:
            await _open_backend(store, AUTHORITY_STREAM)
            luna = await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
                router=GuessedPaymentsRouter(), dispatch=dispatch,
            )
            await composite.ensure_projection()
            await _admit_open_lane(store, lane_id="payments", subject="payments migration", operation_id="admit-payments")
            await _admit_open_lane(store, lane_id="mobile", subject="mobile release", operation_id="admit-mobile")

            await composite.accept_input({"text": "Yes, do it.", "optimistic_id": "bare-consent"})
            await _wait_for_dispatches(dispatched, 1)
            route = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="bare-consent")
            assert route is not None
            assert route["routing_state"] == "fallback_dispatched"
            assert route["route_target"] == CONVERSATION_STREAM
            fallback_payload = json.loads(route["route_json"])
            assert fallback_payload["kind"] == "luna_fallback_classifier"
            context = fallback_payload["routing_context"]
            assert "body_excerpt" not in context
            assert {entry["lane_id"] for entry in context["open_lanes"]} == {"payments", "mobile"}
            assert dispatched[0]["route_target"] == CONVERSATION_STREAM

            resolved = await composite.operation({
                "operation": "route.resolve", "request_id": "resolve-bare-consent",
                "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": route["dispatch_id"],
                "reply_to_message_id": "bare-consent", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [],
                "payload": {
                    "schema_version": "assistant-router/v1", "disposition": "clarify",
                    "lane_id": None, "depends_on_message_id": None, "reason": "two open lanes",
                },
            }, actor_stream_id=CONVERSATION_STREAM)
            assert resolved["duplicate"] is False
            await _wait_for_dispatches(dispatched, 2)
            final = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="bare-consent")
            assert final is not None and final["routing_state"] == "resolved"
            assert final["route_target"] == CONVERSATION_STREAM
            assert len(dispatched) == 2, "a valid Luna resolve must not recurse into fallback"
            assert luna["session_generation"]
        finally:
            await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_local_lane_provenance_requires_explicit_subject_not_one_lane_continuation() -> None:
    """A distinctive subject may prove a lane; sole-lane regex matching may not."""

    class Router:
        async def classify(self, route):
            body = route["body_excerpt"]
            if body == "For payments, use the signed receipt.":
                lane_id = "payments"
            elif body == "What is the next step there?":
                lane_id = "payments"
            else:  # pragma: no cover - fixture assertion is direct
                raise AssertionError(body)
            return {
                "schema_version": "assistant-router/v1", "disposition": "lane",
                "lane_id": lane_id, "depends_on_message_id": None, "reason": "grounded",
            }

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []

        async def dispatch(route):
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        try:
            await _open_backend(store, AUTHORITY_STREAM)
            await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
                router=Router(), dispatch=dispatch,
            )
            await composite.ensure_projection()
            await _admit_open_lane(store, lane_id="payments", subject="payments migration", operation_id="admit-payments")
            await _admit_open_lane(store, lane_id="mobile", subject="mobile release", operation_id="admit-mobile")
            await composite.accept_input({
                "text": "For payments, use the signed receipt.", "optimistic_id": "named-payments",
            })
            await _wait_for_dispatches(dispatched, 1)
            named = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="named-payments")
            assert named is not None and named["routing_state"] == "resolved"
            assert named["route_target"] == AUTHORITY_STREAM

            await store.apply_assistant_composite_operation(
                stream_id=COMPOSITE_STREAM, operation_id="cancel-mobile", operation="lane.decision", lane_id="mobile",
                dispatch_id="seed-cancel", expected_lane_version=1, actor_stream_id=AUTHORITY_STREAM, payload={
                    "decision_id": "cancel-mobile", "transition": "cancel",
                    "from_phase": "discussion", "to_phase": "cancelled", "operator_basis_message_ids": ["named-payments"],
                },
            )
            await composite.accept_input({
                "text": "What is the next step there?", "optimistic_id": "one-lane-continuation",
            })
            await _wait_for_dispatches(dispatched, 2)
            continuation = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="one-lane-continuation",
            )
            assert continuation is not None and continuation["routing_state"] == "fallback_dispatched"
            assert continuation["route_target"] == CONVERSATION_STREAM
            assert dispatched[1]["route_target"] == CONVERSATION_STREAM
        finally:
            await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_luna_fallback_can_resolve_a_current_lane_without_local_provenance_recheck() -> None:
    """Fallback is correlated once; its valid typed resolution is not recursively filtered."""

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []

        async def dispatch(route):
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        try:
            astra = await _open_backend(store, AUTHORITY_STREAM)
            luna = await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
                dispatch=dispatch,
            )
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            await composite.ensure_projection()
            await _admit_open_lane(store, lane_id="payments", subject="payments migration", operation_id="admit-payments")
            await _admit_open_lane(store, lane_id="mobile", subject="mobile release", operation_id="admit-mobile")
            await composite.accept_input({"text": "Continue it.", "optimistic_id": "fallback-lane"})
            route = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="fallback-lane")
            assert route is not None
            await store.update_assistant_composite_route(
                route["route_id"], routing_state="fallback_dispatched", delivery_state="landed",
                dispatch_id="assistant-fallback-lane", route_target=CONVERSATION_STREAM,
                route_target_generation=luna["session_generation"], route_payload={"kind": "luna_fallback_classifier"},
            )
            resolved = await composite.operation({
                "operation": "route.resolve", "request_id": "resolve-fallback-lane",
                "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "assistant-fallback-lane",
                "reply_to_message_id": "fallback-lane", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [],
                "payload": {
                    "schema_version": "assistant-router/v1", "disposition": "lane",
                    "lane_id": "payments", "depends_on_message_id": None, "reason": "Luna has correlated context",
                },
            }, actor_stream_id=CONVERSATION_STREAM)
            assert resolved["duplicate"] is False
            await _wait_for_dispatches(dispatched, 1)
            current = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="fallback-lane")
            assert current is not None and current["routing_state"] == "resolved"
            assert current["route_target"] == AUTHORITY_STREAM
            assert astra["session_generation"]
        finally:
            await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_router_fixture_has_30_or_more_deterministic_cases() -> None:
    fixture = Path(__file__).parent / "fixtures" / "assistant_router_v1_cases.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and len(cases) >= 30
    assert all(
        case["expected"]["disposition"] in {"lane", "new_topic", "clarify", "conversation", "defer"}
        for case in cases
    )


def test_authority_operation_requires_frozen_scope_and_closed_payload() -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            authority = await _open_backend(store, AUTHORITY_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
            )
            await composite.ensure_projection()
            await _seed_dispatch(
                store, input_identity="operation-input", dispatch_id="operation-dispatch", target=AUTHORITY_STREAM,
                generation=authority["session_generation"],
            )
            base = {
                "operation": "lane.admit", "request_id": "operation-admit",
                "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "operation-dispatch",
                "lane_id": None, "expected_lane_version": None, "evidence_refs": [],
                "payload": {"mode": "new", "subject": "fixture work", "request_message_id": "operation-input"},
            }
            try:
                await composite.operation({**base, "payload": {**base["payload"], "unknown": True}}, actor_stream_id=AUTHORITY_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_operation_payload_invalid"
            else:  # pragma: no cover
                raise AssertionError("unknown operation payload key was accepted")
            try:
                await composite.operation({**base, "composite_stream_id": "fixture-host-other:assistant"}, actor_stream_id=AUTHORITY_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_operation_required_fields"
            else:  # pragma: no cover
                raise AssertionError("foreign composite stream was accepted")
            admitted = await composite.operation(base, actor_stream_id=AUTHORITY_STREAM)
            assert admitted["lane_id"].startswith("assistant-lane-")
            try:
                await composite.operation({**base, "payload": {**base["payload"], "subject": "changed"}}, actor_stream_id=AUTHORITY_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_operation_idempotency_conflict"
            else:  # pragma: no cover
                raise AssertionError("changed authority payload replay was accepted")
        finally:
            store.stop()

    asyncio.run(_go())


def test_stale_question_operation_cannot_reach_existing_question_store() -> None:
    """Lane/version validation is durable before the cross-store question effect."""
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        calls = 0

        async def question_operation(_operation, _msg):
            nonlocal calls
            calls += 1
            return {"type": "prompt.ask.ok", "question": {"question_id": "fixture-question"}}

        try:
            authority = await _open_backend(store, AUTHORITY_STREAM)
            lead = await _open_backend(store, LEAD_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
                question_operation=question_operation,
            )
            await composite.ensure_projection()
            await _seed_dispatch(store, input_identity="question-admit", dispatch_id="question-admit-dispatch",
                                 target=AUTHORITY_STREAM, generation=authority["session_generation"])
            admitted = await composite.operation({
                "operation": "lane.admit", "request_id": "question-admit", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "question-admit-dispatch", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [], "payload": {"mode": "new", "subject": "fixture", "request_message_id": "question-admit"},
            }, actor_stream_id=AUTHORITY_STREAM)
            lane_id = admitted["lane_id"]
            await _seed_dispatch(store, input_identity="question-bind", dispatch_id="question-bind-dispatch",
                                 target=AUTHORITY_STREAM, generation=authority["session_generation"], lane_id=lane_id)
            await composite.operation({
                "operation": "lane.bind", "request_id": "question-bind", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "question-bind-dispatch", "lane_id": lane_id, "expected_lane_version": 1,
                "evidence_refs": [], "payload": {
                    "backend_kind": "assistant_conversation", "backend_stream_id": LEAD_STREAM,
                    "backend_generation": lead["session_generation"],
                },
            }, actor_stream_id=AUTHORITY_STREAM)
            await _seed_dispatch(store, input_identity="question-open", dispatch_id="question-open-dispatch",
                                 target=LEAD_STREAM, generation=lead["session_generation"], lane_id=lane_id)
            try:
                await composite.operation({
                    "operation": "question.open", "request_id": "question-stale", "composite_stream_id": COMPOSITE_STREAM,
                    "dispatch_id": "question-open-dispatch", "lane_id": lane_id, "expected_lane_version": 1,
                    "evidence_refs": [], "payload": {"envelope": {}},
                }, actor_stream_id=LEAD_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_lane_version_conflict"
            else:  # pragma: no cover
                raise AssertionError("stale question operation was accepted")
            assert calls == 0
        finally:
            store.stop()

    asyncio.run(_go())


async def _seed_dispatch(
    store, *, input_identity: str, dispatch_id: str, target: str, generation: str, lane_id: str | None = None,
) -> None:
    """Create only durable route receipts; no model/router is involved in authority tests."""
    route = await store.admit_assistant_composite_input(
        stream_id=COMPOSITE_STREAM, input_identity=input_identity, input_request_id=input_identity,
        body="fixture authority input", attachments=[], reply_to_message_id=None,
        reply_to_question_id=None, actor_stream_id="operator:fixture",
    )
    await store.update_assistant_composite_route(
        route["route_id"], routing_state="resolved", delivery_state="landed", dispatch_id=dispatch_id,
        route_target=target, route_target_generation=generation,
        route_payload={"schema_version": "assistant-router/v1", "disposition": "lane",
                       "lane_id": lane_id, "depends_on_message_id": None, "reason": "fixture"},
    )


def test_decision_and_terminal_report_enqueue_one_authority_wake_each() -> None:
    """Gate transitions use the established durable outbox, never routine progress."""

    class _OutboxComms:
        def __init__(self) -> None:
            self.deliveries: list[dict] = []

        async def deliver_outbound_notice(self, msg, **_kwargs):
            self.deliveries.append(dict(msg))
            return {"delivery_status": "delivered"}

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            astra = await _open_backend(store, AUTHORITY_STREAM)
            lead = await _open_backend(store, LEAD_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM,
                ),
            )
            await composite.ensure_projection()
            await _seed_dispatch(
                store, input_identity="msg-admit", dispatch_id="dispatch-admit", target=AUTHORITY_STREAM,
                generation=astra["session_generation"],
            )
            admitted = await composite.operation({
                "operation": "lane.admit", "request_id": "admit", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-admit", "lane_id": None, "expected_lane_version": None,
                "evidence_refs": [],
                "payload": {"mode": "new", "subject": "test", "request_message_id": "msg-admit"},
            }, actor_stream_id=AUTHORITY_STREAM)
            lane_id = admitted["lane_id"]
            await _seed_dispatch(
                store, input_identity="msg-bind", dispatch_id="dispatch-bind", target=AUTHORITY_STREAM,
                generation=astra["session_generation"], lane_id=lane_id,
            )
            await composite.operation({
                "operation": "lane.bind", "request_id": "bind", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-bind", "lane_id": lane_id, "expected_lane_version": 1,
                "evidence_refs": [],
                "payload": {
                    "backend_kind": "assistant_conversation",
                    "backend_stream_id": LEAD_STREAM, "backend_generation": lead["session_generation"],
                },
            }, actor_stream_id=AUTHORITY_STREAM)
            await _seed_dispatch(
                store, input_identity="msg-decision", dispatch_id="dispatch-decision", target=LEAD_STREAM,
                generation=lead["session_generation"], lane_id=lane_id,
            )
            decision = {
                "operation": "lane.decision", "request_id": "decision-1", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-decision", "lane_id": lane_id, "expected_lane_version": 2,
                "evidence_refs": [],
                "payload": {
                    "decision_id": "d1", "transition": "start",
                    "from_phase": "discussion", "to_phase": "execution",
                    "operator_basis_message_ids": ["msg-admit"],
                },
            }
            assert not (await composite.operation(decision, actor_stream_id=LEAD_STREAM))["duplicate"]
            assert (await composite.operation(decision, actor_stream_id=LEAD_STREAM))["duplicate"]
            terminal = await composite.terminal_report({
                "status": "done", "actor_stream_id": LEAD_STREAM, "report_id": "report-1",
                "lane_id": lane_id, "dispatch_id": "dispatch-decision",
            }, actor_generation=lead["session_generation"])
            assert terminal is not None and terminal["phase"] == "completed"
            replayed_terminal = await composite.terminal_report({
                "status": "done", "actor_stream_id": LEAD_STREAM, "report_id": "report-1",
                "lane_id": lane_id, "dispatch_id": "dispatch-decision",
            }, actor_generation=lead["session_generation"])
            assert replayed_terminal is not None and replayed_terminal["version"] == terminal["version"]
            await _seed_dispatch(
                store, input_identity="msg-close", dispatch_id="dispatch-close", target=AUTHORITY_STREAM,
                generation=astra["session_generation"], lane_id=lane_id,
            )
            closed = await composite.operation({
                "operation": "lane.close", "request_id": "close-1", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-close", "lane_id": lane_id,
                "expected_lane_version": terminal["version"], "evidence_refs": [],
                "payload": {
                    "completion_message_id": "report-1", "completion_disposition": "accepted",
                },
            }, actor_stream_id=AUTHORITY_STREAM)
            assert closed["next_phase"] == "closed"

            ids = await store.list_outbound_notice_ids(limit=10, force=True)
            assert set(ids) == {"assistant-decision:decision-1", f"assistant-terminal:{lane_id}:report-1"}
            transport = _OutboxComms()
            outbox = OutboundNoticeQueue(store, transport)
            assert await outbox.drain_once(limit=10, force=True) == 2
            assert len(transport.deliveries) == 2
            for delivered in transport.deliveries:
                context_line = next(line for line in delivered["message"].splitlines()
                                    if line.startswith("authority_context="))
                context = json.loads(context_line.split("=", 1)[1])
                assert context["dispatch_id"] == "dispatch-admit"
                assert context["original_message_id"] == "msg-admit"
                assert context["lane_id"] == lane_id
                assert context["expected_lane_version"] >= 3
            assert await outbox.drain_once(limit=10, force=True) == 0
            assert astra["session_generation"]
        finally:
            store.stop()

    asyncio.run(_go())
