"""Authority is configured by the daemon, not granted by a router destination."""
import asyncio
import json

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from outbound_notices import OutboundNoticeQueue
from store import Store
from test_assistant_composite import (
    AUTHORITY_STREAM, COMPOSITE_STREAM, CONVERSATION_STREAM, LEAD_STREAM,
    ROUTER_ENDPOINT, _open_backend, _seed_dispatch,
)


async def setup_case(store):
    authority = await _open_backend(store, AUTHORITY_STREAM)
    conversation = await _open_backend(store, CONVERSATION_STREAM)
    lead = await store.open_session(*LEAD_STREAM.split(":"), provider="codex", role="lead",
                                    parent_stream_id=AUTHORITY_STREAM)
    composite = AssistantComposite(store, config=AssistantCompositeConfig(
        enabled=True, stream_id=COMPOSITE_STREAM, router_endpoint=ROUTER_ENDPOINT,
        astra_stream_id=AUTHORITY_STREAM, luna_stream_id=CONVERSATION_STREAM))
    await composite.ensure_projection()
    await _seed_dispatch(store, input_identity="operator-input", dispatch_id="luna-dispatch",
                         target=CONVERSATION_STREAM, generation=conversation["session_generation"])
    return composite, authority, conversation, lead


def admission(**kwargs):
    return {"operation": "lane.admit", "request_id": "authority-admission",
            "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "luna-dispatch",
            "payload": {"mode": "new", "subject": "Coordinate existing owners",
                        "request_message_id": "operator-input"}, **kwargs}


def publication(**kwargs):
    return {"request_id": "authority-publication", "composite_stream_id": COMPOSITE_STREAM,
            "dispatch_id": "luna-dispatch", "reply_to_message_id": "operator-input",
            "publish_kind": "prose", "message": "I am checking the existing owners.", **kwargs}


def test_authority_can_admit_publish_and_close_a_luna_origin_request():
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, authority, _, lead = await setup_case(store)
            admitted = await c.operation(admission(), actor_stream_id=AUTHORITY_STREAM)
            assert (await c.operation(admission(), actor_stream_id=AUTHORITY_STREAM))["duplicate"]
            pub = await c.publish(publication(), actor_stream_id=AUTHORITY_STREAM)
            assert (await c.publish(publication(), actor_stream_id=AUTHORITY_STREAM))["event_id"] == pub["event_id"]
            lane = admitted["lane_id"]
            await c.operation({"operation": "lane.bind", "request_id": "bind", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "luna-dispatch", "lane_id": lane, "expected_lane_version": 1,
                "payload": {"backend_kind": "lead", "backend_stream_id": LEAD_STREAM,
                            "backend_generation": lead["session_generation"]}}, actor_stream_id=AUTHORITY_STREAM)
            await c.operation({"operation": "lane.decision", "request_id": "start", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "luna-dispatch", "lane_id": lane, "expected_lane_version": 2,
                "payload": {"decision_id": "start", "transition": "start", "from_phase": "discussion", "to_phase": "execution",
                            "operator_basis_message_ids": ["operator-input"]}}, actor_stream_id=LEAD_STREAM)
            terminal = await c.terminal_report({"status": "done", "actor_stream_id": LEAD_STREAM,
                "report_id": "done", "lane_id": lane, "dispatch_id": "luna-dispatch"}, actor_generation=lead["session_generation"])
            messages = []
            class Comms:
                async def deliver_outbound_notice(self, msg, **kwargs):
                    messages.append(msg); return {"delivery_status": "delivered"}
            outbox = OutboundNoticeQueue(store, Comms())
            assert await outbox.drain_once(limit=10, force=True) == 2
            for msg in messages:
                ctx = json.loads(next(line.split("=", 1)[1] for line in msg["message"].splitlines()
                                      if line.startswith("authority_context=")))
                assert ctx["dispatch_id"] == "luna-dispatch" and ctx["original_message_id"] == "operator-input"
                assert ctx["lane_id"] == lane
            closed = await c.operation({"operation": "lane.close", "request_id": "close", "composite_stream_id": COMPOSITE_STREAM,
                "dispatch_id": "luna-dispatch", "lane_id": lane, "expected_lane_version": terminal["version"],
                "payload": {"completion_message_id": "done", "completion_disposition": "accepted"}}, actor_stream_id=AUTHORITY_STREAM)
            assert closed["next_phase"] == "closed"
            await c.publish(publication(request_id="result", publish_kind="result", evidence_refs=["close"]), actor_stream_id=AUTHORITY_STREAM)
        finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["stale", "foreign_actor", "wrong_input", "unresolved", "nonoperator", "question", "foreign_lane"])
def test_standing_authority_retains_provenance_and_scope(failure):
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, authority, conversation, _ = await setup_case(store)
            msg = admission(); actor = AUTHORITY_STREAM
            if failure == "stale": msg["_auth_context"] = {"session_generation": "stale-generation"}
            elif failure == "foreign_actor": actor = CONVERSATION_STREAM
            elif failure == "wrong_input": msg["payload"]["request_message_id"] = "other-input"
            elif failure in {"unresolved", "nonoperator"}:
                await store.submit(lambda conn: conn.execute(
                    "UPDATE v2_assistant_composite_routes SET " +
                    ("routing_state='fallback_dispatched'" if failure == "unresolved" else "actor_stream_id='peer:untrusted'") +
                    " WHERE dispatch_id='luna-dispatch'"))
            elif failure == "question":
                msg.update(operation="question.open", lane_id="unbound", expected_lane_version=1, payload={"envelope": {"body": "Not bound"}})
            elif failure == "foreign_lane":
                await c.operation(admission(), actor_stream_id=AUTHORITY_STREAM)
                msg.update(operation="lane.decision", request_id="foreign-lane", lane_id="other-lane", expected_lane_version=1,
                    payload={"transition": "cancel", "decision_id": "cancel", "from_phase": "discussion", "to_phase": "cancelled",
                             "operator_basis_message_ids": ["operator-input"]})
            with pytest.raises(ValueError): await c.operation(msg, actor_stream_id=actor)
        finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["stale", "foreign_actor", "wrong_input", "unknown_dispatch", "question", "result_without_receipt"])
def test_authority_publication_preserves_identity_and_evidence(failure):
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            msg = publication(); actor = AUTHORITY_STREAM
            if failure == "stale": msg["_auth_context"] = {"session_generation": "stale-generation"}
            elif failure == "foreign_actor": actor = LEAD_STREAM
            elif failure == "wrong_input": msg["reply_to_message_id"] = "other-input"
            elif failure == "unknown_dispatch": msg["dispatch_id"] = "unknown"
            elif failure == "question": msg.update(publish_kind="question", reply_to_question_id="invented")
            else: msg["publish_kind"] = "result"
            with pytest.raises(ValueError): await c.publish(msg, actor_stream_id=actor)
        finally: store.stop()
    asyncio.run(go())
