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
    assert "Several dispatches in one turn" in wire
    assert "never publish the same body twice" in wire
    # The rule is part of the trusted root instruction, above the untrusted input.
    assert wire.index("never publish the same body twice") < wire.index("<assistant-original-input-json>")
