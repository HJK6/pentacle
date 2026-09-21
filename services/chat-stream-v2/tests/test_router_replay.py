"""RED acceptance tests for the daemon-owned router replay contract."""

from __future__ import annotations

import asyncio
import json

from assistant_composite import AssistantComposite, AssistantCompositeConfig  # noqa: E402
from store import Store  # noqa: E402


COMPOSITE_STREAM = "replay-host-chat:assistant"
AUTHORITY_STREAM = "replay-host-authority:authority"
CONVERSATION_STREAM = "replay-host-conversation:conversation"
ROUTER_ENDPOINT = "ssh://replay-router/assistant-router-v1"


async def _open_backend(store: Store, stream_id: str) -> dict:
    host, name = stream_id.split(":", 1)
    return await store.open_session(host, name, provider="codex")


async def _admit_lane(store: Store, *, lane_id: str = "payments") -> None:
    await store.apply_assistant_composite_operation(
        stream_id=COMPOSITE_STREAM,
        operation_id="admit-" + lane_id,
        operation="lane.admit",
        lane_id=lane_id,
        dispatch_id="dispatch-admit-" + lane_id,
        actor_stream_id=AUTHORITY_STREAM,
        payload={
            "mode": "new",
            "subject": "payments migration",
            "request_message_id": "seed-" + lane_id,
        },
    )


def test_router_input_carries_authoritative_last_outbound_excerpt() -> None:
    """The router wire input includes the newest durable lane publication."""

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            authority = await _open_backend(store, AUTHORITY_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True,
                    stream_id=COMPOSITE_STREAM,
                    router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM,
                    luna_stream_id=CONVERSATION_STREAM,
                ),
            )
            await composite.ensure_projection()
            await _admit_lane(store)
            admitted = await store.admit_assistant_composite_input(
                stream_id=COMPOSITE_STREAM,
                input_identity="seed-input",
                input_request_id="seed-input",
                body="Start payments work.",
                attachments=[],
                reply_to_message_id=None,
                reply_to_question_id=None,
                actor_stream_id="operator:replay",
            )
            await store.update_assistant_composite_route(
                admitted["route_id"],
                routing_state="resolved",
                delivery_state="landed",
                dispatch_id="seed-dispatch",
                route_target=AUTHORITY_STREAM,
                route_target_generation=authority["session_generation"],
                route_payload={
                    "schema_version": "assistant-router/v1",
                    "disposition": "lane",
                    "lane_id": "payments",
                    "depends_on_message_id": None,
                    "reason": "seed",
                },
            )
            await store.record_assistant_composite_publication(
                stream_id=COMPOSITE_STREAM,
                publication_key="seed-publication",
                dispatch_id="seed-dispatch",
                reply_to_message_id="seed-input",
                reply_to_question_id=None,
                publish_kind="prose",
                attachment_ids=[],
                evidence_refs=[],
                canonical_payload={
                    "composite_stream_id": COMPOSITE_STREAM,
                    "dispatch_id": "seed-dispatch",
                    "reply_to_message_id": "seed-input",
                    "reply_to_question_id": None,
                    "publish_kind": "prose",
                    "message": "The signed receipt is ready.",
                    "attachment_ids": [],
                    "evidence_refs": [],
                },
                event={
                    "stream_id": COMPOSITE_STREAM,
                    "provider": "composite",
                    "kind": "ASSIST_TEXT",
                    "text": "The signed receipt is ready.",
                    "message_id": "publication:seed-publication",
                    "reply_to_message_id": "seed-input",
                    "reply_to_question_id": None,
                    "publish_kind": "prose",
                    "attachments": [],
                    "timestamp": "2026-09-21T00:00:00Z",
                    "raw": {
                        "assistant_composite": True,
                        "publish_kind": "prose",
                        "dispatch_id": "seed-dispatch",
                        "reply_to_message_id": "seed-input",
                    },
                },
                actor_stream_id=AUTHORITY_STREAM,
                actor_generation=None,
            )

            route = await store.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM,
                input_identity="seed-input",
            )
            assert route is not None
            router_input = await composite._router_input(route)
            lane = next(item for item in router_input["open_lanes"] if item["lane_id"] == "payments")
            assert lane["last_outbound_excerpt"] == "The signed receipt is ready."
            assert lane["last_outbound_truncated"] is False
        finally:
            await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_non_explicit_short_continuation_calls_router_without_local_lane_binding() -> None:
    """A bare continuation is classified, but a local regex cannot select its lane."""

    class RecordingRouter:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def classify(self, route: dict) -> dict:
            self.calls.append(route)
            return {
                "schema_version": "assistant-router/v1",
                "disposition": "lane",
                "lane_id": "payments",
                "depends_on_message_id": None,
                "reason": "router fixture",
            }

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []
        router = RecordingRouter()

        async def dispatch(route: dict) -> dict:
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        composite = None
        try:
            await _open_backend(store, AUTHORITY_STREAM)
            await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True,
                    stream_id=COMPOSITE_STREAM,
                    router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM,
                    luna_stream_id=CONVERSATION_STREAM,
                ),
                router=router,
                dispatch=dispatch,
            )
            await composite.ensure_projection()
            await _admit_lane(store)
            await composite.accept_input({"text": "Continue it.", "optimistic_id": "bare-continuation"})
            for _ in range(60):
                route = await store.get_assistant_composite_route(
                    stream_id=COMPOSITE_STREAM,
                    input_identity="bare-continuation",
                )
                if route is not None and route["routing_state"] in {"fallback_dispatched", "resolved", "routing_failed"}:
                    break
                await asyncio.sleep(0.01)
            assert len(router.calls) == 1
            assert router.calls[0]["body_excerpt"] == "Continue it."
            assert route is not None
            assert route["routing_state"] == "fallback_dispatched"
            assert route["route_target"] == CONVERSATION_STREAM
            assert dispatched and dispatched[0]["route_target"] == CONVERSATION_STREAM
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()

    asyncio.run(_go())


def test_router_decision_persists_receipt_id_for_live_proof() -> None:
    """A live proof can bind a normal route to a persisted router receipt."""

    class ConversationRouter:
        async def classify(self, _route: dict) -> dict:
            return {
                "schema_version": "assistant-router/v1",
                "disposition": "conversation",
                "lane_id": None,
                "depends_on_message_id": None,
                "reason": "ordinary chat",
            }

    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        dispatched: list[dict] = []

        async def dispatch(route: dict) -> dict:
            dispatched.append(dict(route))
            return {"delivery": "landed"}

        composite = None
        try:
            authority = await _open_backend(store, AUTHORITY_STREAM)
            await _open_backend(store, CONVERSATION_STREAM)
            composite = AssistantComposite(
                store,
                config=AssistantCompositeConfig(
                    enabled=True,
                    stream_id=COMPOSITE_STREAM,
                    router_endpoint=ROUTER_ENDPOINT,
                    astra_stream_id=AUTHORITY_STREAM,
                    luna_stream_id=CONVERSATION_STREAM,
                ),
                router=ConversationRouter(),
                dispatch=dispatch,
            )
            await composite.ensure_projection()
            await composite.accept_input({"text": "Hello there.", "optimistic_id": "router-receipt"})
            for _ in range(60):
                route = await store.get_assistant_composite_route(
                    stream_id=COMPOSITE_STREAM, input_identity="router-receipt",
                )
                if route is not None and route["routing_state"] == "resolved":
                    break
                await asyncio.sleep(0.01)
            assert route is not None
            decision = json.loads(route["route_json"])
            assert decision["router_decision_receipt_id"].startswith("assistant-router-decision-")
            assert route["dispatch_id"]
            assert dispatched and dispatched[0]["dispatch_id"] == route["dispatch_id"]
            assert authority["session_generation"]
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()

    asyncio.run(_go())
