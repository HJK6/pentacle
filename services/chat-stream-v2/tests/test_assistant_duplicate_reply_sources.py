"""RED tests for spec_pentacle__assistant_duplicate_reply_sources_2026_10.

Two operator-visible duplicate sources on the canonical assistant chat:

* B: a bound seat publishes proactively, then ends the same turn with a
  structured final. The mirror correlates only dispatch publications, so the
  final is projected a second time (19 byte-identical pairs 10-08..10-13).
* A: several operator dispatches answered in one turn get the same body
  (3 pairs: 09-28, 09-30, 10-14). The batched-dispatch rule lived only in
  host-local FD checkpoint prose; the daemon-authored dispatch envelope is
  the durable home because it is in context on every reply.
"""
from __future__ import annotations

import asyncio
import json

from assistant_composite import AssistantComposite, direct_dispatch_envelope
from store import Store
from test_assistant_prose_mirror import ASSISTANT, ROOT, _answer_rows, _config, _normalized_turn_event


def _proactive(generation: str, key: str, text: str) -> dict:
    return {"type": "assistant.publish", "request_id": key, "composite_stream_id": ASSISTANT,
            "publish_kind": "prose", "message": text,
            "_auth_context": {"token_verified": True, "stream_id": ROOT, "session_generation": generation}}


def test_proactive_publication_turn_final_is_not_mirrored():
    """A structured final in a turn that already published proactively is not re-projected."""

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(generation))
            await composite.ensure_projection()
            # Turn 1: an unknown (mirror-eligible) trigger, a proactive publish, then the
            # seat's own turn-final repeating the published text byte-for-byte.
            await _normalized_turn_event(store, provider="claude", kind="USER", text="typed directly", identity="u1")
            first = await composite.publish(_proactive(generation, "publish:proactive-1", "Card reissued."), actor_stream_id=ROOT)
            assert first["duplicate"] is False
            await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Card reissued.", identity="f1", final=True)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["Card reissued."], [r["message_id"] for r in rows]
            # Turn 2: same shape, but the final differs from the publication only in
            # Markdown. The turn is correlated, not the bytes.
            await _normalized_turn_event(store, provider="claude", kind="USER", text="typed directly again", identity="u2")
            await composite.publish(_proactive(generation, "publish:proactive-2", "Domain **bought**."), actor_stream_id=ROOT)
            await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Domain bought.", identity="f2", final=True)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["Card reissued.", "Domain **bought**."], [r["message_id"] for r in rows]
            # Control: the next ordinary turn without a proactive publication still mirrors,
            # even when its text equals an earlier publication.
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


# ---- Daemon-enforced rules (operator scope change 2026-10-14 ~14:31Z: "no clear enforcement
# or rules, just fuzzy logic"; "if i send a new message when an old message is still processing
# maybe we need a cleaner way to handle that"). Every rule below is enforced by the daemon.

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
    dispatch that the seat answers under its own id (or that merges under Rule 4)."""

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


def test_identical_final_to_another_dispatch_is_merged_not_duplicated():
    """Rule 4 backstop: the 2026-10-14 13:41Z journey. Same body to two dispatches yields one row,
    one chat.event.update, no chat.event, no reply push."""

    async def _go():
        sends, broadcasts, pushes = [], [], []
        store, composite, _generation = await _landed_composite(sends, broadcasts, pushes)
        try:
            first = await _accept(composite, store, "in-a", "pause wmi runs")
            await composite.publish(_final(first["dispatch_id"], "in-a", "WMI is **paused**."), actor_stream_id=ROOT)
            second = await _accept(composite, store, "in-b", "just pause for now")
            assert second["dispatch_id"] != first["dispatch_id"] and not second.get("batched_into_dispatch_id")
            before = len(broadcasts)
            merged = await composite.publish(_final(second["dispatch_id"], "in-b", "WMI is paused."), actor_stream_id=ROOT)
            rows = await _answer_rows(store)
            assert [r["text"] for r in rows] == ["WMI is **paused**."]
            assert merged["merged_into_event_id"] == rows[0]["daemon_seq"] and merged["duplicate"] is False
            assert rows[0]["reply_to_message_ids"] == ["in-a", "in-b"]
            assert [b["type"] for b in broadcasts[before:]] == ["chat.event.update"] and len(pushes) == 1
            # Replay is idempotent and returns the same merge.
            again = await composite.publish(_final(second["dispatch_id"], "in-b", "WMI is paused."), actor_stream_id=ROOT)
            assert again["duplicate"] is True and again["merged_into_event_id"] == rows[0]["daemon_seq"]
            # The merged dispatch is closed: a later ordinary no-dispatch final still mirrors.
            await _normalized_turn_event(store, provider="claude", kind="USER", text="typed directly", identity="u1")
            await _normalized_turn_event(store, provider="claude", kind="ASSIST_TEXT", text="Anything else?", identity="f1", final=True)
            assert [r["text"] for r in await _answer_rows(store)] == ["WMI is **paused**.", "Anything else?"]
            # Control: a different answer to a third dispatch is a new row.
            third = await _accept(composite, store, "in-c", "did you switch hosts?")
            await composite.publish(_final(third["dispatch_id"], "in-c", "Yes, the switch finished at 03:31Z."), actor_stream_id=ROOT)
            assert len(await _answer_rows(store)) == 3
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_proactive_publish_is_refused_while_a_dispatch_is_open():
    """Rule 2a: proactive content is folded into the open reply, never published beside it."""

    async def _go():
        store, composite, generation = await _landed_composite()
        try:
            head = await _accept(composite, store, "in-a", "pause wmi runs")
            with pytest.raises(ValueError, match="assistant_publish_proactive_blocked_open_dispatch"):
                await composite.publish(_proactive(generation, "publish:proactive-1", "By the way, PR 87 passed QA."), actor_stream_id=ROOT)
            assert [r["text"] for r in await _answer_rows(store)] == []
            await composite.publish(_final(head["dispatch_id"], "in-a", "WMI is paused. PR 87 passed QA; holding the merge."), actor_stream_id=ROOT)
            # With no open dispatch the proactive path works as before.
            ok = await composite.publish(_proactive(generation, "publish:proactive-2", "Reminder: the clinic calls at 3 PM."), actor_stream_id=ROOT)
            assert ok["duplicate"] is False
            # Idempotent replay of an accepted proactive publication wins over a later open dispatch.
            await _accept(composite, store, "in-b", "another question")
            replay = await composite.publish(_proactive(generation, "publish:proactive-2", "Reminder: the clinic calls at 3 PM."), actor_stream_id=ROOT)
            assert replay["duplicate"] is True and replay["event_id"] == ok["event_id"]
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_proactive_near_duplicate_of_recent_visible_message_is_refused_unless_overridden():
    """Rule 2b: deterministic similarity against the last 6 h of operator-visible rows."""

    async def _go():
        store, composite, generation = await _landed_composite()
        try:
            first = await _accept(composite, store, "in-a", "status?")
            await composite.publish(_final(first["dispatch_id"], "in-a", "Domain **bought**: expressdispo.com for $11.08."), actor_stream_id=ROOT)
            with pytest.raises(ValueError, match="assistant_publish_proactive_duplicate"):
                await composite.publish(_proactive(generation, "publish:proactive-dup", "Domain bought: expressdispo.com for $11.08."), actor_stream_id=ROOT)
            assert len(await _answer_rows(store)) == 1
            # Different content passes.
            await composite.publish(_proactive(generation, "publish:proactive-new", "Henry texted that his bot is ready."), actor_stream_id=ROOT)
            # An explicit override with a reason is recorded and allowed.
            forced = await composite.publish(_proactive(generation, "publish:proactive-forced", "Domain bought: expressdispo.com for $11.08.",
                                                        override_duplicate_reason="operator asked for the receipt again"), actor_stream_id=ROOT)
            assert forced["duplicate"] is False
            rows = await _answer_rows(store)
            assert len(rows) == 3 and rows[-1]["raw"]["override_duplicate_reason"] == "operator asked for the receipt again"
            # Idempotent replay of the refused key with the same payload is still refused; the
            # accepted override replays as duplicate.
            again = await composite.publish(_proactive(generation, "publish:proactive-forced", "Domain bought: expressdispo.com for $11.08.",
                                                       override_duplicate_reason="operator asked for the receipt again"), actor_stream_id=ROOT)
            assert again["duplicate"] is True
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_similarity_is_deterministic_and_markdown_insensitive():
    """One shared similarity function for rules 2b and 4."""
    from assistant_similarity import normalized, similarity

    assert normalized("WMI is **paused**.\n\n- **Daily brief:** stopped.") == normalized("wmi is paused. daily brief: stopped.")
    assert similarity("WMI is **paused**.", "WMI is paused.") == 1.0
    assert similarity("Nothing new.", "Nothing changed.") < 0.9
    assert similarity("", "anything") == 0.0
