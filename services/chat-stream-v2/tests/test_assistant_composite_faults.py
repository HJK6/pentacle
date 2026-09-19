"""Crash-boundary checks for assistant composite routing/delivery state."""

from __future__ import annotations

import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store


COMPOSITE_STREAM = "fixture-host-chat:assistant"
AUTHORITY_STREAM = "fixture-host-authority:authority"
ROUTER_ENDPOINT = "ssh://fixture-router/assistant-router-v1"


class _AstraRouter:
    async def classify(self, _route):
        return {
            "schema_version": "assistant-router/v1", "disposition": "new_topic",
            "lane_id": None, "depends_on_message_id": None, "reason": "test work",
        }


def _config() -> AssistantCompositeConfig:
    return AssistantCompositeConfig(
        enabled=True,
        stream_id=COMPOSITE_STREAM,
        router_endpoint=ROUTER_ENDPOINT,
        astra_stream_id=AUTHORITY_STREAM,
    )


def test_post_intent_dispatch_exception_stays_uncertain_without_reinjection() -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        attempts = 0

        async def broken_dispatch(_route):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("connection dropped after downstream action may have begun")

        try:
            await store.open_session("fixture-host-authority", "authority", provider="codex")
            composite = AssistantComposite(store, config=_config(), router=_AstraRouter(), dispatch=broken_dispatch)
            await composite.ensure_projection()
            receipt = await composite.accept_input({
                "text": "do the thing", "request_id": "rpc", "optimistic_id": "one",
            })
            for _ in range(30):
                route = await store.get_assistant_composite_route(
                    stream_id=COMPOSITE_STREAM, input_identity="one",
                )
                if route and route.get("delivery_state") == "uncertain":
                    break
                await asyncio.sleep(0.01)
            # Query the route through its stable dispatch discovered from the task.
            await asyncio.gather(*tuple(composite._dispatch_tasks))
            route = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="one")
            assert route is not None and route["delivery_state"] == "uncertain"
            claimed = await store.claim_assistant_composite_route(stream_id=COMPOSITE_STREAM, owner="test")
            assert claimed is None, "uncertain intent must not be blindly re-queued"
            assert attempts == 1
            assert receipt["routing_state"] == "queued"
        finally:
            store.stop()

    asyncio.run(_go())


def test_projection_identity_survives_store_restart(tmp_path) -> None:
    async def _open(path):
        store = Store(str(path))
        store.start()
        try:
            composite = AssistantComposite(store, config=_config())
            return await composite.ensure_projection()
        finally:
            store.stop()

    first = asyncio.run(_open(tmp_path / "sessions.db"))
    second = asyncio.run(_open(tmp_path / "sessions.db"))
    assert first["created_at"] == second["created_at"]
    assert first["session_generation"] == second["session_generation"]


def test_restart_requeues_only_pre_action_classification_and_retains_intent_uncertain(tmp_path) -> None:
    async def _go() -> None:
        path = tmp_path / "sessions.db"
        first = Store(str(path))
        first.start()
        try:
            composite = AssistantComposite(first, config=_config())
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            await composite.ensure_projection()
            await composite.accept_input({"text": "classify again", "optimistic_id": "classifying"})
            await composite.accept_input({"text": "physical unknown", "optimistic_id": "intent"})
            classifying = await first.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="classifying",
            )
            intent = await first.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="intent",
            )
            assert classifying is not None and intent is not None
            claimed = await first.claim_assistant_composite_route(stream_id=COMPOSITE_STREAM, owner="old-daemon")
            assert claimed is not None and claimed["input_identity"] == "classifying"
            await first.update_assistant_composite_route(
                intent["route_id"], routing_state="resolved", delivery_state="intent",
                dispatch_id="assistant-dispatch-intent", route_target=AUTHORITY_STREAM,
            )
        finally:
            first.stop()

        second = Store(str(path))
        second.start()
        try:
            composite = AssistantComposite(second, config=_config())
            await composite.ensure_projection()
            recovered = await composite.recover()
            assert recovered == {"requeued_classifying": 1, "retained_uncertain": 1}
            classifying = await second.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="classifying",
            )
            intent = await second.get_assistant_composite_route(
                stream_id=COMPOSITE_STREAM, input_identity="intent",
            )
            assert classifying is not None and classifying["routing_state"] == "queued"
            assert intent is not None and intent["delivery_state"] == "uncertain"
            assert intent["error_code"] == "recovery_delivery_evidence_required"
        finally:
            await composite.stop()
            second.stop()

    asyncio.run(_go())


def test_publication_requires_dispatch_target_current_generation() -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            astra = await store.open_session("fixture-host-authority", "authority", provider="codex")
            composite = AssistantComposite(store, config=_config())
            await composite.ensure_projection()
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            await composite.accept_input({"text": "original", "optimistic_id": "original"})
            route = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity="original")
            assert route is not None
            await store.update_assistant_composite_route(
                route["route_id"], routing_state="resolved", delivery_state="landed",
                dispatch_id="dispatch-1", route_target=AUTHORITY_STREAM,
                route_target_generation=astra["session_generation"],
            )
            await store.mark_closed(
                "fixture-host-authority", "authority", closed_at="2026-09-19T00:00:00Z", pane_status="closed",
                expected_generation=astra["session_generation"],
            )
            await store.open_session("fixture-host-authority", "authority", provider="codex")
            try:
                await composite.publish({
                    "request_id": "pub", "composite_stream_id": COMPOSITE_STREAM,
                    "dispatch_id": "dispatch-1", "reply_to_message_id": "original",
                    "reply_to_question_id": None, "publish_kind": "prose", "message": "stale reply",
                    "attachment_ids": [], "evidence_refs": [],
                }, actor_stream_id=AUTHORITY_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_publish_provenance_unverified"
            else:  # pragma: no cover
                raise AssertionError("reused session name published a stale dispatch")
        finally:
            store.stop()

    asyncio.run(_go())


def test_publication_replay_freezes_wire_payload_and_rejects_cross_route_reply() -> None:
    async def _go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            authority = await store.open_session("fixture-host-authority", "authority", provider="codex")
            composite = AssistantComposite(store, config=_config())
            await composite.ensure_projection()
            composite._wake_worker = lambda: None  # type: ignore[method-assign]
            for identity in ("input-a", "input-b"):
                await composite.accept_input({"text": identity, "optimistic_id": identity})
                route = await store.get_assistant_composite_route(stream_id=COMPOSITE_STREAM, input_identity=identity)
                assert route is not None
                await store.update_assistant_composite_route(
                    route["route_id"], routing_state="resolved", delivery_state="landed",
                    dispatch_id="dispatch-" + identity, route_target=AUTHORITY_STREAM,
                    route_target_generation=authority["session_generation"],
                )
            request = {
                "request_id": "publish-a", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "dispatch-input-a", "reply_to_message_id": "input-a",
                "reply_to_question_id": None, "publish_kind": "prose", "message": "complete",
                "attachment_ids": [], "evidence_refs": [],
            }
            first = await composite.publish(request, actor_stream_id=AUTHORITY_STREAM)
            replay = await composite.publish(dict(request), actor_stream_id=AUTHORITY_STREAM)
            assert first["duplicate"] is False and replay["duplicate"] is True
            try:
                await composite.publish({**request, "message": "changed"}, actor_stream_id=AUTHORITY_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_publication_idempotency_conflict"
            else:  # pragma: no cover
                raise AssertionError("changed publication reused its request id")
            try:
                await composite.publish({**request, "request_id": "cross-route", "reply_to_message_id": "input-b"}, actor_stream_id=AUTHORITY_STREAM)
            except ValueError as exc:
                assert str(exc) == "assistant_publish_reply_unverified"
            else:  # pragma: no cover
                raise AssertionError("dispatch accepted another route's reply id")
        finally:
            store.stop()

    asyncio.run(_go())
