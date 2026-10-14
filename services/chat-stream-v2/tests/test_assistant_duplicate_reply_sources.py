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
            # Exactly one visible assistant event and one push for the batch (the three
            # operator inputs are echoed as their own USER events).
            assistant_events = [b for b in broadcasts if b.get("type") == "chat.event" and b["event"]["kind"] == "ASSIST_TEXT"]
            assert len(assistant_events) == 1 and len(pushes) == 1
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


# ---- Planned GREEN controls (spec Plan steps 3 and 4) ---------------------------------------------

import logging

from store_routing import _routing_iso_now


async def _landed_member_pair(sends=None):
    store, composite, generation = await _landed_composite(sends)
    head = await _accept(composite, store, "in-a", "first")
    return store, composite, generation, head


def test_question_answer_gets_no_batch_membership():
    """Rule 1 control: an answer to an operator question keeps its own explicit route, unbatched."""

    async def _go():
        sends = []
        store, composite, generation, head = await _landed_member_pair(sends)
        try:
            def _lane(conn):
                conn.execute(
                    "INSERT INTO v2_assistant_composite_lanes(lane_id,stream_id,phase,bound_stream_id,"
                    "bound_generation,pending_question_id,created_at,updated_at) "
                    "VALUES ('lane-q',?,'discussion',?,?,'q-1',?,?)",
                    (ASSISTANT, ROOT, generation, _routing_iso_now(), _routing_iso_now()))
                conn.commit()
            await store.submit(_lane)

            async def _answered(question_id, body, msg):
                return {"ok": True}
            composite.question_answer = _answered
            await composite.accept_input({"message": "yes", "msg_id": "in-q", "request_id": "in-q",
                                          "reply_to_question_id": "q-1"}, operator_principal="operator:fixture")
            for _ in range(200):
                answer = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="in-q")
                if answer and answer["delivery_state"] == "landed":
                    break
                await asyncio.sleep(0.01)
            assert answer["dispatch_id"] and not answer.get("batched_into_dispatch_id"), answer
            assert "<assistant-dispatch-batch-json>" not in sends[-1]["wire_body"]
            assert (await _publication(store, head["dispatch_id"])) is None  # the head is still open
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_voice_answer_take_gets_no_batch_membership():
    """Rule 1 control: a voice take bound to question answers is never a batch member."""

    async def _go():
        sends = []
        store, composite, _generation, head = await _landed_member_pair(sends)
        try:
            meta = {"voice": {"duration_s": 2.0},
                    "voice_answers": {"version": 1, "recording_id": "rec-1", "blob_sha": "b" * 64,
                                      "duration_s": 2.0, "items": []}}
            await composite.accept_input({"text": "yes to the lead", "optimistic_id": "in-voice",
                                          "request_id": "in-voice", "meta": meta},
                                         operator_principal="operator:fixture")
            voice = await _accept(composite, store, "in-voice", "yes to the lead")
            assert await composite._voice_answers_for(voice) is not None
            assert voice["dispatch_id"] and not voice.get("batched_into_dispatch_id"), voice
            assert "<assistant-dispatch-batch-json>" not in sends[-1]["wire_body"]
            assert head["dispatch_id"] != voice["dispatch_id"]
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_rebind_keeps_members_with_their_seat_and_stale_generation_gets_no_membership(tmp_path):
    """Rule 1 controls: after a rebind the new seat's input never joins the old seat's open
    dispatches (stale target generation); the old seat's members keep dispatch authority like any
    route, and one id of the new seat cannot be closed by the old seat."""
    from test_assistant_hot_rebind import CHAT, A, B, _seat, _config as _rebind_config, _request

    async def _go():
        store = _Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            sends = []

            async def _dispatch(route):
                sends.append(route["input_identity"])
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_rebind_config(a["session_generation"]), dispatch=_dispatch)
            await composite.load_binding()
            await composite.ensure_projection()

            async def _landed(input_id, text):
                await composite.accept_input({"text": text, "request_id": input_id, "optimistic_id": input_id},
                                             operator_principal="operator:fixture")
                for _ in range(200):
                    route = await store.get_assistant_composite_route(stream_id=CHAT, input_identity=input_id)
                    if route and route["delivery_state"] == "landed":
                        return route
                    await asyncio.sleep(0.01)
                raise AssertionError(input_id)
            head = await _landed("in-a", "first")
            member = await _landed("in-b", "second")
            assert member["batched_into_dispatch_id"] == head["dispatch_id"]
            await composite.rebind(_request("move-a-b", 0, B), actor_stream_id=A)
            fresh = await _landed("in-c", "third, after the rebind")
            assert fresh["route_target"] == B and not fresh.get("batched_into_dispatch_id")
            assert "<assistant-dispatch-batch-json>" not in json.loads(fresh["route_json"])["direct_envelope"]["wire_body"]
            assert sends == ["in-a", "in-b", "in-c"]
            final = {"request_id": "publish:" + head["dispatch_id"], "composite_stream_id": CHAT,
                     "dispatch_id": head["dispatch_id"], "reply_to_message_id": "in-a",
                     "publish_kind": "prose", "response_state": "final", "message": "Both answered.",
                     "attachment_ids": [], "evidence_refs": []}
            with pytest.raises(ValueError, match="assistant_publish_reply_unverified"):
                await composite.publish({**final, "reply_to_message_ids": ["in-a", "in-c"]}, actor_stream_id=A)
            result = await composite.publish({**final, "reply_to_message_ids": ["in-a", "in-b"]}, actor_stream_id=A)
            assert result["closed_dispatch_ids"] == [member["dispatch_id"]]
            replay = await composite.publish({**final, "reply_to_message_ids": ["in-a", "in-b"]}, actor_stream_id=A)
            assert replay["duplicate"] is True and replay["closed_dispatch_ids"] == [member["dispatch_id"]]
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_batch_publication_with_attachment_validates_and_closes_listed_dispatch(tmp_path):
    """Rule 1 control: attachment validation is unchanged for a publication that answers a batch."""
    from test_managed_attachment_publish import publication_fixture
    from test_managed_attachment_upload import upload
    from test_assistant_direct_primary import ROOT as DIRECT_ROOT

    async def _go():
        async with publication_fixture(tmp_path) as (blobs, store, server, msg):
            route = await store.admit_assistant_composite_input(
                stream_id=msg["composite_stream_id"], input_identity="input-2", input_request_id="input-2",
                body="Synthetic second request", attachments=[], reply_to_message_id=None,
                reply_to_question_id=None, actor_stream_id="operator:fixture")
            await store.update_assistant_composite_route(
                route["route_id"], routing_state="resolved", delivery_state="landed", dispatch_id="dispatch-2",
                route_target=DIRECT_ROOT, route_target_generation=msg["_auth_context"]["session_generation"],
                route_payload={"admission_mode": "direct_primary"})
            uploaded = await upload(blobs)
            msg.update(message="Both files attached.", attachment_ids=[uploaded["upload_id"]],
                       reply_to_message_ids=["input-1", "input-2"])
            result = await server._on_assistant_publish(msg)
            assert result["closed_dispatch_ids"] == ["dispatch-2"]
            closed = await _publication(store, "dispatch-2")
            assert closed["merged_into_event_id"] == result["event_id"]

    asyncio.run(_go())


def test_batch_block_reaches_the_real_main_dispatch_and_envelope_checks_pass(tmp_path):
    """Rule 1 control: the second input's wire body carries the batch block and the real
    main.py._dispatch_assistant_route envelope checks still accept it (one paste per input)."""
    from assistant_composite import AssistantCompositeConfig
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME
    from test_voice_context import main_dispatch_subject

    async def _go():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        composite = None
        try:
            await _open(comms, sessions)
            root = await store.fetch_session(HOST, NAME)
            target = f"{HOST}:{NAME}"
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": ASSISTANT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": target,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": root["session_generation"],
            })
            main_dispatch = main_dispatch_subject(comms, store)
            results = []

            async def _recipient(route):
                result = await main_dispatch(route)
                results.append(result)
                return result
            composite = AssistantComposite(store, config=config, dispatch=_recipient)
            await composite.ensure_projection()
            for input_id, text in (("in-a", "pause wmi runs"), ("in-b", "just pause for now")):
                await composite.accept_input({"message": text, "msg_id": input_id, "request_id": input_id},
                                             operator_principal="operator:fixture")
                for _ in range(300):
                    if len(results) >= (1 if input_id == "in-a" else 2):
                        break
                    await asyncio.sleep(0.01)
            assert [r.get("submission_confirmed") for r in results] == [True, True], results
            assert len(tmux.pastes) == 2
            assert "<assistant-dispatch-batch-json>" not in tmux.pastes[0]
            assert "<assistant-dispatch-batch-json>" in tmux.pastes[1] and "in-a" in tmux.pastes[1]
            member = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="in-b")
            head = await store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="in-a")
            assert member["batched_into_dispatch_id"] == head["dispatch_id"]
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()

    asyncio.run(_go())


async def _turn_row(store, stream_id, pane_pid, *, provider, kind, identity, text="", command=None,
                    final=False, timestamp=None, session="turn-session"):
    """One structured transcript row (USER, TOOL_USE or ASSIST_TEXT) from any seat, normalized."""
    from claude_jsonl_norm import normalize_claude_jsonl_record
    from codex_rollout_norm import normalize_codex_rollout_record
    from datetime import datetime, timezone
    host, name = stream_id.split(":", 1)
    stamp = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if provider == "claude":
        if kind == "TOOL_USE":
            content = [{"type": "tool_use", "id": identity, "name": "Bash", "input": {"command": command}}]
        else:
            content = [{"type": "text", "text": text}]
        record = {"type": "user" if kind == "USER" else "assistant", "uuid": identity, "sessionId": session,
                  "timestamp": stamp, "isSidechain": False,
                  "message": {"role": "user" if kind == "USER" else "assistant", "id": identity,
                              "stop_reason": "end_turn" if final else "tool_use", "content": content}}
        event = normalize_claude_jsonl_record(record, host=host, session_name=name)[0]
    else:
        if kind == "TOOL_USE":
            payload = {"type": "function_call", "name": "exec_command", "call_id": identity,
                       "arguments": json.dumps({"cmd": command})}
        else:
            payload = {"type": "message", "id": identity, "role": "user" if kind == "USER" else "assistant",
                       "phase": "final_answer" if final else "commentary",
                       "content": [{"type": "input_text" if kind == "USER" else "output_text", "text": text}]}
        record = {"type": "response_item", "timestamp": stamp, "payload": payload}
        event = normalize_codex_rollout_record(record, host=host, session_name=name, session_id=session)[0]
    return (await store.append_session_events_lifecycle_cas([
        {"stream_id": stream_id, "event": event, "identity": identity,
         "lifecycle": await store.fetch_open_session_lifecycle(stream_id, pane_pid=pane_pid)}], limit=100))[0]


async def _restamp_publication(store, key):
    """Move a stored publication's daemon time into the current turn window (overlap fixture)."""
    def _op(conn):
        conn.execute("UPDATE v2_assistant_composite_publications SET created_at=? WHERE publication_key=?",
                     (_routing_iso_now(), key))
        conn.commit()
    await store.submit(_op)


def test_proactive_correlation_controls_previous_turn_missing_generation_and_codex():
    """Rule 3 controls: a previous turn's dynamic-key publication never suppresses the next turn; a
    publication without raw.actor_generation never matches; a Codex final_answer turn with its own
    function_call publication is suppressed and the next Codex turn mirrors."""

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(generation))
            await composite.ensure_projection()
            turn = lambda **kw: _turn_row(store, ROOT, "4242", **kw)
            # Previous turn: dynamic key published inside turn 1 (suppressed by the clock window).
            await turn(provider="claude", kind="USER", identity="p-u1", text="typed")
            await turn(provider="claude", kind="TOOL_USE", identity="p-t1", command="agent-orch assistant publish --request-id $K")
            published = await composite.publish(_proactive(generation, "publish:dyn-1", "Lane moved."), actor_stream_id=ROOT)
            assert published["event_id"]
            await turn(provider="claude", kind="ASSIST_TEXT", identity="p-f1", text="Lane moved.", final=True)
            # Turn 2 has no publication: it mirrors even though turn 1's publication is recent.
            await turn(provider="claude", kind="USER", identity="p-u2", text="typed again")
            await turn(provider="claude", kind="ASSIST_TEXT", identity="p-f2", text="Nothing new.", final=True)
            assert [r["text"] for r in await _answer_rows(store)] == ["Lane moved.", "Nothing new."]
            # Missing raw.actor_generation (a pre-upgrade row): never matches, even by literal key.
            await composite.publish(_proactive(generation, "publish:legacy-1", "Legacy update."), actor_stream_id=ROOT)

            def _strip(conn):
                conn.execute("UPDATE session_event_tail SET event_json=json_remove(event_json,'$.raw.actor_generation') "
                             "WHERE event_id=(SELECT event_id FROM v2_assistant_composite_publications WHERE publication_key='publish:legacy-1')")
                conn.commit()
            await store.submit(_strip)
            await turn(provider="claude", kind="USER", identity="p-u3", text="typed once more")
            await turn(provider="claude", kind="TOOL_USE", identity="p-t3", command="agent-orch assistant publish --request-id publish:legacy-1")
            await _restamp_publication(store, "publish:legacy-1")
            await turn(provider="claude", kind="ASSIST_TEXT", identity="p-f3", text="Legacy update.", final=True)
            assert [r["text"] for r in await _answer_rows(store)][-2:] == ["Legacy update.", "Legacy update."]
            # Codex: function_call carries the literal key; publication stored before the turn rows.
            await composite.publish(_proactive(generation, "publish:codex-1", "Codex update."), actor_stream_id=ROOT)
            await turn(provider="codex", kind="USER", identity="c-u1", text="typed", session="codex-session")
            await turn(provider="codex", kind="TOOL_USE", identity="c-t1", session="codex-session",
                       command="agent-orch assistant publish --request-id publish:codex-1 --proactive")
            await turn(provider="codex", kind="ASSIST_TEXT", identity="c-f1", text="Codex update.", final=True, session="codex-session")
            rows = [r["text"] for r in await _answer_rows(store)]
            assert rows[-1] == "Codex update." and rows.count("Codex update.") == 1
            await turn(provider="codex", kind="USER", identity="c-u2", text="typed again", session="codex-session")
            await turn(provider="codex", kind="ASSIST_TEXT", identity="c-f2", text="Codex update.", final=True, session="codex-session")
            assert [r["text"] for r in await _answer_rows(store)].count("Codex update.") == 2
            # Proactive events persist the publishing generation.
            event = [r for r in await _answer_rows(store) if r["message_id"] == "publication:publish:codex-1"][0]
            assert event["raw"]["actor_generation"] == generation
            assert event["raw"]["published_by"] == {"stream_id": ROOT, "generation": generation}
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_previous_generation_and_other_seat_publications_never_suppress(tmp_path):
    """Rule 3 controls: inside the overlapping window and even with the literal key in the turn,
    a publication by the previous generation of the seat, or by another seat, never suppresses."""
    from test_assistant_prose_mirror import _set_durable_binding

    async def _go():
        store = Store(str(tmp_path / "mirror.sqlite"))
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            old_generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(old_generation))
            await composite.ensure_projection()
            await composite.publish(_proactive(old_generation, "publish:old-gen", "Old generation update."), actor_stream_id=ROOT)
            await composite.stop()
            await store.update_session("fixture-root", "visible", status="closed")
            replacement = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            rebound = AssistantComposite(store, config=_config(replacement["session_generation"]))
            await rebound.ensure_projection()
            await _turn_row(store, ROOT, "4242", provider="claude", kind="USER", identity="g-u1", text="typed")
            await _turn_row(store, ROOT, "4242", provider="claude", kind="TOOL_USE", identity="g-t1",
                            command="agent-orch assistant publish --request-id publish:old-gen")
            await _restamp_publication(store, "publish:old-gen")
            await _turn_row(store, ROOT, "4242", provider="claude", kind="ASSIST_TEXT", identity="g-f1",
                            text="Old generation update.", final=True)
            assert [r["text"] for r in await _answer_rows(store)] == ["Old generation update.", "Old generation update."]
            # Another seat: A's proactive publication, then a hot rebind to B; B's turn names A's key.
            await rebound.publish(_proactive(replacement["session_generation"], "publish:seat-a", "Seat A update."), actor_stream_id=ROOT)
            other = await store.open_session("fixture-newdesk", "visible", provider="codex", pane_pid="5252")
            await _set_durable_binding(store, "fixture-newdesk:visible", other["session_generation"])
            seat_b = "fixture-newdesk:visible"
            await _turn_row(store, seat_b, "5252", provider="claude", kind="USER", identity="b-u1", text="typed")
            await _turn_row(store, seat_b, "5252", provider="claude", kind="TOOL_USE", identity="b-t1",
                            command="agent-orch assistant publish --request-id publish:seat-a")
            await _restamp_publication(store, "publish:seat-a")

            def _same_generation(conn):
                # Even a (forged) equal generation string from another seat never matches.
                conn.execute("UPDATE session_event_tail SET event_json=json_set(event_json,'$.raw.actor_generation',?) "
                             "WHERE event_id=(SELECT event_id FROM v2_assistant_composite_publications WHERE publication_key='publish:seat-a')",
                             (other["session_generation"],))
                conn.commit()
            await store.submit(_same_generation)
            await _turn_row(store, seat_b, "5252", provider="claude", kind="ASSIST_TEXT", identity="b-f1",
                            text="Seat A update.", final=True)
            assert [r["text"] for r in await _answer_rows(store)][-2:] == ["Seat A update.", "Seat A update."]
            await rebound.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_dynamic_key_outside_the_window_mirrors_and_logs_the_miss(caplog):
    """Rule 3 known limit: a dynamic-key publication stored while the previous final was still
    being ingested cannot be placed; the final mirrors as before and the miss class is logged."""
    from datetime import datetime, timezone

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(generation))
            await composite.ensure_projection()
            turn = lambda **kw: _turn_row(store, ROOT, "4242", **kw)
            await turn(provider="claude", kind="USER", identity="m-u1", text="typed")
            boundary_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            await asyncio.sleep(0.01)
            # Turn 2's dynamic-key publication lands before turn 1's (legacy, unstructured) final is ingested.
            await composite.publish(_proactive(generation, "publish:dyn-late", "Late update."), actor_stream_id=ROOT)
            await asyncio.sleep(0.01)
            legacy = {"stream_id": ROOT, "provider": "codex", "kind": "ASSIST_TEXT", "text": "Turn one answer.",
                      "timestamp": boundary_ts, "raw": {"source_session_identity": "turn-session", "message_id": "m-f1"}}
            await store.append_session_events_lifecycle_cas([
                {"stream_id": ROOT, "event": legacy, "identity": "m-f1",
                 "lifecycle": await store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}], limit=100)
            await turn(provider="claude", kind="USER", identity="m-u2", text="typed again")
            await turn(provider="claude", kind="TOOL_USE", identity="m-t2", command="agent-orch assistant publish --request-id $K")
            with caplog.at_level(logging.INFO, logger="chat_streamd_v2.store"):
                await turn(provider="claude", kind="ASSIST_TEXT", identity="m-f2", text="Late update.", final=True)
            assert [r["text"] for r in await _answer_rows(store)] == ["Late update.", "Turn one answer.", "Late update."]
            assert "assistant mirror proactive correlation miss" in caplog.text
            assert "miss_class=dynamic_key_ingest_lag" in caplog.text
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())


def test_ordinary_final_ingested_after_a_later_proactive_publication_stays_visible():
    """RED (Astra advisory 3f5bd4bd, FD ruling 2b79b111): turn N finishes at the provider, turn N+1
    publishes proactively before N's transcript rows arrive. N's ordinary final has no publication of
    its own and must stay visible; only N+1's final, whose tool row names the key, is suppressed."""
    from datetime import datetime, timezone

    async def _go():
        store = Store(":memory:")
        store.start()
        try:
            root = await store.open_session("fixture-root", "visible", provider="codex", pane_pid="4242")
            generation = root["session_generation"]
            composite = AssistantComposite(store, config=_config(generation))
            await composite.ensure_projection()
            turn = lambda **kw: _turn_row(store, ROOT, "4242", provider="claude", **kw)
            await turn(kind="USER", identity="d-u0", text="Initial input")
            await turn(kind="ASSIST_TEXT", identity="d-f0", text="Initial answer", final=True)
            prior_final_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            await asyncio.sleep(0.01)
            await composite.publish(_proactive(generation, "publish:future-turn", "Novel proactive update"), actor_stream_id=ROOT)
            await turn(kind="USER", identity="d-u1", text="Independent question", timestamp=prior_final_ts)
            await turn(kind="ASSIST_TEXT", identity="d-f1", text="Independent answer must be visible", final=True, timestamp=prior_final_ts)
            await turn(kind="USER", identity="d-u2", text="Proactive trigger")
            await turn(kind="TOOL_USE", identity="d-t2", command="agent-orch assistant publish --request-id publish:future-turn --proactive")
            await turn(kind="ASSIST_TEXT", identity="d-f2", text="Novel proactive update", final=True)
            assert [r["text"] for r in await _answer_rows(store)] == [
                "Initial answer", "Novel proactive update", "Independent answer must be visible"]
            await composite.stop()
        finally:
            store.stop()

    asyncio.run(_go())
