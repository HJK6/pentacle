"""Durable diagnostics and delivery receipts for assistant-router fallback."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store


COMPOSITE_STREAM = "fixture-host-chat:assistant"
AUTHORITY_STREAM = "fixture-host-authority:authority"
CONVERSATION_STREAM = "fixture-host-conversation:conversation"
ROUTER_ENDPOINT = "ssh://fixture-router/assistant-router-v1"
CAPTURED_ROUTE = Path(__file__).parent / "fixtures" / "assistant_router_captured_route.json"
CAPTURED_LANE = "assistant-lane-132b50c2c24496f6989e13ba"


async def _open_backend(store: Store, stream_id: str) -> dict:
    return await store.open_session(*stream_id.split(":", 1), provider="codex")


async def _seed_lane(store: Store, astra: dict) -> None:
    seed = await store.admit_assistant_composite_input(
        stream_id=COMPOSITE_STREAM,
        input_identity="router-fallback-seed",
        input_request_id="router-fallback-seed",
        body="seed",
        attachments=[],
        reply_to_message_id=None,
        reply_to_question_id=None,
        actor_stream_id="operator:fixture",
    )
    await store.update_assistant_composite_route(
        seed["route_id"],
        routing_state="resolved",
        delivery_state="landed",
        dispatch_id="router-fallback-seed-dispatch",
        route_target=AUTHORITY_STREAM,
        route_target_generation=astra["session_generation"],
    )
    await store.apply_assistant_composite_operation(
        stream_id=COMPOSITE_STREAM,
        operation_id="router-fallback-seed-lane",
        operation="lane.admit",
        lane_id=CAPTURED_LANE,
        dispatch_id="router-fallback-seed-dispatch",
        actor_stream_id=AUTHORITY_STREAM,
        payload={
            "mode": "new",
            "subject": "Pentacle desktop/web provider re-login buttons",
            "request_message_id": "router-fallback-seed",
        },
    )


def _config() -> AssistantCompositeConfig:
    return AssistantCompositeConfig(
        enabled=True,
        stream_id=COMPOSITE_STREAM,
        router_endpoint=ROUTER_ENDPOINT,
        astra_stream_id=AUTHORITY_STREAM,
        luna_stream_id=CONVERSATION_STREAM,
    )


def test_captured_route_replays_through_real_classifier_with_receipt() -> None:
    """The captured 4bc route decision remains a green local-router proof."""

    captured = json.loads(CAPTURED_ROUTE.read_text(encoding="utf-8"))

    class CapturedRouter:
        async def classify(self, _route: dict) -> dict:
            return dict(captured["decision"])

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []
        composite = None

        async def dispatch(route: dict) -> dict:
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        try:
            astra = await _open_backend(store, AUTHORITY_STREAM)
            await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store, config=_config(), router=CapturedRouter(), dispatch=dispatch,
            )
            await composite.ensure_projection()
            await _seed_lane(store, astra)
            admitted = await store.admit_assistant_composite_input(
                stream_id=COMPOSITE_STREAM,
                input_identity=captured["input_identity"],
                input_request_id=captured["input_identity"],
                body=captured["body"],
                attachments=[],
                reply_to_message_id=None,
                reply_to_question_id=None,
                actor_stream_id="operator:fixture",
            )
            await store.update_assistant_composite_route(
                admitted["route_id"], routing_state="queued",
                route_payload=captured["route_json"],
            )
            claimed = await store.claim_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, owner="router-replay-test",
            )
            assert claimed is not None
            await composite._classify_one(claimed)
            if composite._dispatch_tasks:
                await asyncio.gather(*tuple(composite._dispatch_tasks))

            route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=captured["input_identity"],
            )
            assert route is not None
            payload = json.loads(route["route_json"])
            assert route["routing_state"] == "resolved"
            assert route["delivery_state"] == "landed"
            assert payload["router_decision_receipt_id"].startswith("assistant-router-decision-")
            assert "kind" not in payload
            assert dispatched and dispatched[0]["route_target"] == AUTHORITY_STREAM
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_router_failure_persists_diagnostic_warns_once_and_survives_resolve(caplog) -> None:
    """A fallback carries bounded failure evidence through Luna resolution."""

    class RaisingRouter:
        async def classify(self, _route: dict) -> dict:
            raise RuntimeError("router boom " + ("x" * 1200))

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []
        composite = None

        async def dispatch(route: dict) -> dict:
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        try:
            astra = await _open_backend(store, AUTHORITY_STREAM)
            luna = await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store, config=_config(), router=RaisingRouter(), dispatch=dispatch,
            )
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            await composite.ensure_projection()
            await _seed_lane(store, astra)
            admitted = await composite.accept_input({
                "text": "Continue it.", "optimistic_id": "router-failure-diagnostic",
            })
            route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=admitted["message_id"],
            )
            assert route is not None

            with caplog.at_level(logging.WARNING, logger="chat_streamd_v2.assistant_composite"):
                await composite._classify_one(route)
                if composite._dispatch_tasks:
                    await asyncio.gather(*tuple(composite._dispatch_tasks))

            route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=admitted["message_id"],
            )
            assert route is not None
            payload = json.loads(route["route_json"])
            failure = payload["router_failure"]
            assert failure["exception_type"] == "RuntimeError"
            assert failure["message"].startswith("router boom ")
            assert len(failure["message"]) <= 512
            assert len(failure["traceback"]) <= 4096
            assert isinstance(failure["elapsed_ms"], int) and failure["elapsed_ms"] >= 0
            assert payload["fallback_receipt"]["dispatch_id"] == route["dispatch_id"]
            assert payload["fallback_receipt"]["receipt_id"].startswith("assistant-fallback-receipt-")
            warnings = [
                record for record in caplog.records
                if record.name == "chat_streamd_v2.assistant_composite"
                and record.levelno == logging.WARNING
            ]
            assert len(warnings) == 1
            assert route["dispatch_id"] in warnings[0].getMessage()
            assert f"elapsed_ms={failure['elapsed_ms']}" in warnings[0].getMessage()

            resolved = await composite.operation({
                "operation": "route.resolve", "request_id": "resolve-diagnostic",
                "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": route["dispatch_id"],
                "reply_to_message_id": admitted["message_id"],
                "lane_id": None, "expected_lane_version": None, "evidence_refs": [],
                "payload": {
                    "schema_version": "assistant-router/v1", "disposition": "conversation",
                    "lane_id": None, "depends_on_message_id": None, "reason": "clarify in Luna",
                },
            }, actor_stream_id=CONVERSATION_STREAM)
            assert resolved["duplicate"] is False
            if composite._dispatch_tasks:
                await asyncio.gather(*tuple(composite._dispatch_tasks))
            final = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=admitted["message_id"],
            )
            assert final is not None
            final_payload = json.loads(final["route_json"])
            assert final_payload["router_failure"] == failure
            assert final_payload["fallback_receipt"] == payload["fallback_receipt"]
            assert final_payload["backend_context"] == payload["routing_context"]
            assert final["delivery_state"] == "landed"
            assert dispatched[0]["routing_state"] == "fallback_dispatched"
            assert dispatched[-1]["routing_state"] == "resolved"
            assert luna["session_generation"]
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_plain_short_fallback_maps_real_committed_pending_receipt_to_definite_state() -> None:
    """A backend committed-pending receipt is not misreported as uncertain."""

    class RaisingRouter:
        async def classify(self, _route: dict) -> dict:
            raise ValueError("router unavailable")

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        composite = None

        async def dispatch(_route: dict) -> dict:
            return {"delivery": "committed_pending_proof"}

        try:
            await _open_backend(store, AUTHORITY_STREAM)
            await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store, config=_config(), router=RaisingRouter(), dispatch=dispatch,
            )
            await composite.ensure_projection()
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            accepted = await composite.accept_input({
                "text": "Continue it.", "optimistic_id": "plain-short-fallback",
            })
            route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=accepted["message_id"],
            )
            assert route is not None
            await composite._classify_one(route)
            if composite._dispatch_tasks:
                await asyncio.gather(*tuple(composite._dispatch_tasks))
            final = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=accepted["message_id"],
            )
            assert final is not None
            assert final["routing_state"] == "fallback_dispatched"
            assert final["delivery_state"] == "committed_pending"
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_route_resolve_cannot_replace_durable_fallback_receipts() -> None:
    """The store merge protects fallback diagnostics from a colliding payload."""

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await _open_backend(store, AUTHORITY_STREAM)
            luna = await _open_backend(store, CONVERSATION_STREAM)
            projection = await store.ensure_assistant_composite_projection(
                stream_id=COMPOSITE_STREAM, title="Fixture",
            )
            assert projection["stream_id"] == COMPOSITE_STREAM
            admitted = await store.admit_assistant_composite_input(
                stream_id=COMPOSITE_STREAM,
                input_identity="merge-protection-input",
                input_request_id="merge-protection-input",
                body="Continue it.",
                attachments=[],
                reply_to_message_id=None,
                reply_to_question_id=None,
                actor_stream_id="operator:fixture",
            )
            input_identity = "merge-protection-input"
            original_failure = {
                "exception_type": "RuntimeError",
                "message": "original router failure",
                "traceback": "Traceback (most recent call last): ...",
                "elapsed_ms": 42,
            }
            original_receipt = {
                "receipt_id": "assistant-fallback-receipt-original",
                "dispatch_id": "assistant-fallback-original",
                "kind": "luna_fallback_classifier",
            }
            await store.update_assistant_composite_route(
                admitted["route_id"], routing_state="fallback_dispatched", delivery_state="landed",
                dispatch_id="assistant-fallback-original", route_target=CONVERSATION_STREAM,
                route_target_generation=luna["session_generation"], route_payload={
                    "kind": "luna_fallback_classifier",
                    "reason": "router_failed:RuntimeError",
                    "routing_context": {"message_id": input_identity, "open_lanes": []},
                    "router_failure": original_failure,
                    "fallback_receipt": original_receipt,
                },
            )
            await store.apply_assistant_composite_operation(
                stream_id=COMPOSITE_STREAM,
                operation_id="merge-protection-resolve",
                operation="route.resolve",
                lane_id=None,
                actor_stream_id=CONVERSATION_STREAM,
                payload={
                    "schema_version": "assistant-router/v1",
                    "disposition": "conversation",
                    "lane_id": None,
                    "depends_on_message_id": None,
                    "reason": "resolved by Luna",
                    "router_failure": {"exception_type": "spoof"},
                    "fallback_receipt": {"receipt_id": "spoof"},
                },
                dispatch_id="assistant-fallback-original",
                reply_to_message_id=input_identity,
                route_id=admitted["route_id"],
                route_dispatch_id="assistant-resolve-merge-protection",
                route_target=CONVERSATION_STREAM,
                route_target_generation=luna["session_generation"],
            )
            final = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity=input_identity,
            )
            assert final is not None
            payload = json.loads(final["route_json"])
            assert payload["router_failure"] == original_failure
            assert payload["fallback_receipt"] == original_receipt
        finally:
            store.stop()

    asyncio.run(_go())
