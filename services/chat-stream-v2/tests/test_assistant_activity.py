"""Response obligations are durable per input, not per pane or quiet timer."""
import asyncio

import pytest

from store import Store
from assistant_composite import AssistantComposite
from test_assistant_standing_authority import setup_case, publication
from test_assistant_composite import COMPOSITE_STREAM, CONVERSATION_STREAM, _seed_dispatch


def test_acknowledgment_final_overlap_and_reconnect_preserve_response_obligations():
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, luna, _ = await setup_case(store)
            await _seed_dispatch(store, input_identity="second", dispatch_id="second-dispatch",
                                 target=CONVERSATION_STREAM, generation=luna["session_generation"])
            await c.refresh_activity()
            snapshot = c.project_session({"stream_id": COMPOSITE_STREAM})
            assert snapshot["working"] and snapshot["assistant_activity"]["pending_count"] == 2
            ack = publication(response_state="acknowledged")
            await c.publish(ack, actor_stream_id=CONVERSATION_STREAM)
            first = c.activity_snapshot()["inputs"]["operator-input"]
            assert first["response_state"] == "acknowledged"
            assert first["first_visible_at"] and first["final_visible_at"] is None
            assert first["work_state"] == "unknown"  # No composite lane does not prove no external work.
            assert first["reply_latency_ms"] >= 0
            await c.publish(publication(request_id="final", response_state="final"), actor_stream_id=CONVERSATION_STREAM)
            snapshot = c.activity_snapshot()
            assert snapshot["pending_count"] == 1
            assert snapshot["inputs"]["operator-input"]["response_state"] == "answered"
            assert snapshot["inputs"]["operator-input"]["final_visible_at"]
            assert snapshot["inputs"]["second"]["response_state"] == "awaiting_reply"
            assert c.project_session({})["working"]
            reopened = AssistantComposite(store, config=c.config)
            await reopened.refresh_activity()
            assert reopened.activity_snapshot()["inputs"] == snapshot["inputs"]
            events = await store.fetch_session_event_tail(COMPOSITE_STREAM, limit=20)
            enriched = await reopened.enrich_events(events)
            original = next(e for e in enriched if e.get("message_id") == "operator-input")
            assert original["raw"]["assistant_activity"]["response_state"] == "answered"
            assert original["text"] == next(e["text"] for e in events if e.get("message_id") == "operator-input")
            with pytest.raises(ValueError, match="idempotency_conflict"):
                await c.publish({**ack, "response_state": "final"}, actor_stream_id=CONVERSATION_STREAM)
        finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("delivery,state", [("uncertain", "uncertain"), ("failed", "failed")])
def test_delivery_failures_are_not_thinking_or_completed(delivery, state):
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            row = await store.find_assistant_composite_route_by_dispatch("luna-dispatch")
            await store.update_assistant_composite_route(row["route_id"], routing_state="resolved", delivery_state=delivery)
            await c.refresh_activity()
            activity = c.activity_snapshot()["inputs"]["operator-input"]
            assert activity["response_state"] == state and activity["final_visible_at"] is None
            assert not c.project_session({})["working"]
        finally: store.stop()
    asyncio.run(go())


def test_historical_prose_does_not_invent_completion():
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            await c.publish(publication(), actor_stream_id=CONVERSATION_STREAM)
            await c.refresh_activity()
            activity = c.activity_snapshot()["inputs"]["operator-input"]
            assert activity["response_state"] == "reply_received"
            assert activity["first_visible_at"] and activity["final_visible_at"] is None
        finally: store.stop()
    asyncio.run(go())
