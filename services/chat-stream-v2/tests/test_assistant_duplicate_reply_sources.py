"""RED tests for spec_pentacle__assistant_duplicate_reply_sources_2026_10.

Two operator-visible duplicate sources on the canonical assistant chat:

* B: a bound seat publishes proactively, then ends the same turn with a
  structured final. The mirror correlates only dispatch publications, so the
  final is projected a second time (19 byte-identical pairs 10-08..10-13).
* A: several operator dispatches answered in one turn get the same body
  (3 pairs: 09-28, 09-30, 10-14). The daemon now batches inputs that arrive
  during an open dispatch and lets one publication close them all.
"""
from __future__ import annotations

import asyncio
import json

from assistant_composite import AssistantComposite, direct_dispatch_envelope
from store import Store
from test_assistant_prose_mirror import ASSISTANT, ROOT, _answer_rows, _config, _normalized_turn_event


def _proactive(generation: str, key: str, text: str) -> dict:
    return {"type": "assistant.publish", "request_id": key, "composite_stream_id": ASSISTANT,
            "publish_kind": "prose", "response_state": "final", "message": text,
            "_auth_context": {"token_verified": True, "stream_id": ROOT, "session_generation": generation}}


async def _tool_use_event(store, *, command: str, identity: str, session="turn-session"):
    """A Claude tool_use row (the seat's own publish call) normalized like live ingest."""
    from claude_jsonl_norm import normalize_claude_jsonl_record
    from datetime import datetime, timezone
    record = {"type": "assistant", "uuid": identity, "sessionId": session,
              "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "isSidechain": False,
              "message": {"role": "assistant", "id": identity, "stop_reason": "tool_use",
                          "content": [{"type": "tool_use", "id": identity, "name": "Bash", "input": {"command": command}}]}}
    event = normalize_claude_jsonl_record(record, host="fixture-root", session_name="visible")[0]
    return (await store.append_session_events_lifecycle_cas([
        {"stream_id": ROOT, "event": event, "identity": identity,
         "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}], limit=100))[0]


def test_proactive_publication_turn_final_is_not_mirrored():
    """A structured final in a turn that already published proactively is not re-projected.

    Turn 1 stores the publication BEFORE any row of the turn is ingested (delayed ingestion); the
    seat's own tool row carries the literal request id, so transcript correlation suppresses the
    final. Turn 2 uses a dynamic key (no literal id in the tool row) and is suppressed by the
    daemon-clock window after the previous final. Turn 3 has no publication and mirrors.
    """

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(generation))
            await composite.ensure_projection()
            # Turn 1: publication first, then the delayed transcript rows (trigger, tool row, final).
            first = await composite.publish(_proactive(generation, "publish:fd-card-reissue-1", "Card reissued."), actor_stream_id=ROOT)
            assert first["duplicate"] is False
            await _normalized_turn_event(store, provider="claude", kind="USER", text="typed directly", identity="u1")
            await _tool_use_event(store, command="agent-orch assistant publish --request-id publish:fd-card-reissue-1 --proactive --message 'Card reissued.'", identity="t1")
            await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Card reissued.", identity="f1", final=True)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["Card reissued."], [r["message_id"] for r in rows]
            # Turn 2: dynamic key (not literal in the tool row); the final differs only in Markdown.
            await _normalized_turn_event(store, provider="claude", kind="USER", text="typed directly again", identity="u2")
            await _tool_use_event(store, command="K=publish:fd-domain-$(date +%s); agent-orch assistant publish --request-id $K --proactive --message \"$(cat reply.md)\"", identity="t2")
            await composite.publish(_proactive(generation, "publish:fd-domain-1791990000", "Domain **bought**."), actor_stream_id=ROOT)
            await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Domain bought.", identity="f2", final=True)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["Card reissued.", "Domain **bought**."], [r["message_id"] for r in rows]
            # Turn 3 (control): no proactive publication; the final mirrors even with equal text.
            await _normalized_turn_event(store, provider="claude", kind="USER", text="typed directly once more", identity="u3")
            await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Card reissued.", identity="f3", final=True)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["Card reissued.", "Domain **bought**.", "Card reissued."]
            assert rows[-1]["publish_kind"] == "status"
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_direct_dispatch_envelope_carries_the_batched_dispatch_rule():
    """The daemon-authored packet, in context on every reply, owns the one-answer-per-batch rule."""
    config = _config("generation-1")
    route = {"input_identity": "optimistic-1", "reply_to_question_id": None,
             "body": "pause wmi runs", "attachments_json": "[]"}
    envelope = direct_dispatch_envelope(config, route, dispatch_id="assistant-direct-1",
                                        target=ROOT, generation="generation-1")
    wire = envelope["wire_body"]
    assert "Several inputs in one batch" in wire and "--reply-to-message-ids" in wire
    assert "never publish the same body twice" in wire
    # The rule is part of the trusted root instruction, above the untrusted input.
    assert wire.index("never publish the same body twice") < wire.index("<assistant-original-input-json>")


# ---- Daemon-enforced rules (operator 2026-10-14 ~14:31Z: "no clear enforcement or rules, just
# fuzzy logic"; "if i send a new message when an old message is still processing maybe we need a
# cleaner way to handle that"; ~15:05Z: no proactive guard, no near-copy filter). Scope: dispatch
# batching with one shared publication, plus the mirror correlation above. A novel proactive update
# must always be delivered (regression guard).

import pytest

from store import Store as _Store


async def _landed_composite(sends: list | None = None, broadcasts: list | None = None, pushes: list | None = None):
    store = _Store(":memory:")
    store.start()
    root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
    generation = root["session_generation"]

    async def _dispatch(route):
        if sends is not None:
            payload = route.get("route_json") or "{}"
            payload = json.loads(payload) if isinstance(payload, str) else payload
            sends.append({"dispatch_id": route["dispatch_id"], "input": route["input_identity"],
                          "wire_body": str(((payload.get("direct_envelope") or {}).get("wire_body")) or "")})
        return {"delivery": "landed"}

    async def _broadcast(payload):
        if broadcasts is not None:
            broadcasts.append(payload)

    async def _push(**kwargs):
        if pushes is not None:
            pushes.append(kwargs)

    composite = AssistantComposite(store, config=_config(generation), dispatch=_dispatch, broadcast=_broadcast)
    composite.reply_push = _push
    await composite.ensure_projection()
    return store, composite, generation


async def _accept(composite, store, input_id: str, text: str):
    await composite.accept_input({"message": text, "msg_id": input_id, "request_id": input_id},
                                 operator_principal="operator:fixture")
    for _ in range(200):
        route = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity=input_id)
        if route and route["routing_state"] == "resolved" and route["dispatch_id"] and route["delivery_state"] == "landed":
            return route
        await asyncio.sleep(0.01)
    raise AssertionError(f"route {input_id} never landed")


def _final(dispatch_id: str, reply_to: str, text: str, **extra) -> dict:
    return {"request_id": "publish:" + dispatch_id, "composite_stream_id": ASSISTANT,
            "dispatch_id": dispatch_id, "reply_to_message_id": reply_to, "reply_to_question_id": None,
            "publish_kind": "prose", "response_state": "final", "message": text,
            "attachment_ids": [], "evidence_refs": [], **extra}


async def _publication(store, dispatch_id: str):
    def _op(conn):
        row = conn.execute("SELECT * FROM v2_assistant_composite_publications WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        return dict(row) if row else None
    return await store.submit(_op)


def test_input_during_open_dispatch_is_batched_and_one_publication_answers_all():
    """Rule 1: an input admitted while a dispatch is open keeps its own dispatch and envelope but is
    marked as a batch member; one publication listing every input closes the whole batch."""

    async def _go():
        sends, broadcasts, pushes = [], [], []
        store, composite, _generation = await _landed_composite(sends, broadcasts, pushes)
        try:
            head = await _accept(composite, store, "in-a", "pause wmi runs")
            second = await _accept(composite, store, "in-b", "ill figure that out later, just pause for now")
            third = await _accept(composite, store, "in-c", "were you able to switch over?")
            assert second["dispatch_id"] and third["dispatch_id"]  # own identity, own envelope, one send each
            assert second.get("batched_into_dispatch_id") == head["dispatch_id"], second
            assert third.get("batched_into_dispatch_id") == head["dispatch_id"], third
            assert [s["input"] for s in sends] == ["in-a", "in-b", "in-c"]
            assert "batch" in sends[1]["wire_body"] and head["dispatch_id"] in sends[1]["wire_body"]
            assert "--reply-to-message-ids" in sends[1]["wire_body"]
            # One publication, declaring every input it answers, closes all three dispatches.
            result = await composite.publish(_final(head["dispatch_id"], "in-a", "WMI is paused.",
                                                    reply_to_message_ids=["in-a", "in-b", "in-c"]),
                                             actor_stream_id=ROOT)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["WMI is paused."]
            assert rows[0]["reply_to_message_ids"] == ["in-a", "in-b", "in-c"]
            assert result["duplicate"] is False and result["closed_dispatch_ids"] == [second["dispatch_id"], third["dispatch_id"]]
            for route in (second, third):
                closed = await _publication(store, route["dispatch_id"])
                assert closed and closed["merged_into_event_id"] == rows[0]["daemon_seq"], closed
            # Exactly one visible event and one push for the batch.
            assert sum(1 for b in broadcasts if b.get("type") == "chat.event") == 1 and len(pushes) == 1
            # Control: after the batch is answered, a new input is not a member.
            later = await _accept(composite, store, "in-d", "thanks")
            assert not later.get("batched_into_dispatch_id")
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_batch_member_not_declared_by_the_publication_stays_open_without_a_second_send():
    """Rule 1 race: a member the seat did not read is never re-sent; it stays an ordinary open
    dispatch that the seat answers under its own id."""

    async def _go():
        sends = []
        store, composite, _generation = await _landed_composite(sends)
        try:
            head = await _accept(composite, store, "in-a", "first")
            second = await _accept(composite, store, "in-b", "second, pasted after the seat composed its answer")
            assert second.get("batched_into_dispatch_id") == head["dispatch_id"]
            await composite.publish(_final(head["dispatch_id"], "in-a", "Answer to the first."), actor_stream_id=ROOT)
            assert [s["input"] for s in sends] == ["in-a", "in-b"]  # no re-send
            assert await _publication(store, second["dispatch_id"]) is None  # still open
            # A new input now batches into the still-open member, never into the published head.
            third = await _accept(composite, store, "in-c", "third")
            assert third.get("batched_into_dispatch_id") == second["dispatch_id"]
            # The seat answers the member under its own dispatch id, listing the third input.
            await composite.publish(_final(second["dispatch_id"], "in-b", "Answer to the second and third.",
                                           reply_to_message_ids=["in-b", "in-c"]), actor_stream_id=ROOT)
            assert [r["text"] for r in await _answer_rows(store)] == ["Answer to the first.", "Answer to the second and third."]
            assert (await _publication(store, third["dispatch_id"]))["merged_into_event_id"]
            # An id outside the caller's open dispatches is refused.
            with pytest.raises(ValueError, match="assistant_publish_reply_unverified"):
                await composite.publish(_final(head["dispatch_id"], "in-a", "x", reply_to_message_ids=["in-a", "in-z"]), actor_stream_id=ROOT)
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_novel_proactive_with_no_open_dispatch_is_always_delivered():
    """Regression guard (operator 2026-10-14, dispatch b0223cd9): unprompted updates must never be
    blocked. A brand-new proactive update with no open dispatch goes through immediately, including
    on a seat that has never received an operator dispatch (the earlier bug: the front desk could
    only speak after the operator messaged first; spec_pentacle__front_desk_mirror_trigger_scope_2026_10
    added the proactive path)."""

    async def _go():
        sends, broadcasts, pushes = [], [], []
        store, composite, generation = await _landed_composite(sends, broadcasts, pushes)
        try:
            assert sends == []  # no operator message ever
            first = await composite.publish(_proactive(generation, "publish:proactive-1", "Henry texted that his bot is ready."), actor_stream_id=ROOT)
            assert first["duplicate"] is False and not first.get("deferred")
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["Henry texted that his bot is ready."]
            assert [b["type"] for b in broadcasts] == ["chat.event"] and len(pushes) == 1
            # A second novel update, still with no dispatch, also goes through.
            second = await composite.publish(_proactive(generation, "publish:proactive-2", "The clinic confirmed 3 PM."), actor_stream_id=ROOT)
            assert second["duplicate"] is False and not second.get("deferred")
            assert len(await _answer_rows(store)) == 2
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


