"""A landed, unpublished dispatch's own turn-final prose reaches the composite.

spec_pentacle__dispatch_turn_final_projection_2026_10: the bound seat answered
an operator dispatch by ending its turn with prose and never publishing, so the
operator saw no reply.  The daemon projects that prose as an acknowledgment.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from assistant_composite import AssistantComposite
from claude_jsonl_norm import normalize_claude_jsonl_record
from ingest import append_ingested_event, broadcast_assistant_mirror
from store import Store
from test_assistant_prose_mirror import ASSISTANT, ROOT, _config


SESSION = "fixture-session"


class Harness:
    def __init__(self, store, composite, generation):
        self.store, self.composite, self.generation = store, composite, generation
        self.frames = []

    async def broadcast(self, frame):
        self.frames.append(frame)

    def composite_frames(self):
        return [f for f in self.frames if f["event"].get("stream_id") == ASSISTANT]

    async def ingest(self, kind, text, identity, *, final=False, optimistic_id=None,
                     record=None, session=SESSION):
        record = record or identity
        role = "assistant" if kind == "ASSIST_TEXT" else "user"
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = normalize_claude_jsonl_record({
            "type": role, "uuid": record, "sessionId": session, "timestamp": stamp,
            "isSidechain": False,
            "message": {"role": role, "id": record,
                        "stop_reason": "end_turn" if final else "tool_use",
                        "content": [{"type": "text", "text": text}]},
        }, host="fixture-root", session_name="visible")[0]
        if optimistic_id is not None:
            event["optimistic_id"] = optimistic_id
        event["raw"]["message_id"] = identity
        return await append_ingested_event(
            self.store, self.broadcast, event, recent_limit=100,
            lifecycle=await self.store.fetch_open_session_lifecycle(ROOT, pane_pid="4242"),
        )

    async def route(self, input_id, *, delivery="landed", reply_to=None):
        await self.composite.accept_input({"message": "Operator question " + input_id,
                                           "msg_id": input_id, "request_id": input_id},
                                          operator_principal="operator:fixture")
        for _ in range(200):
            route = await self.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity=input_id)
            if route and route["routing_state"] == "resolved" and route["delivery_state"] == "landed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("direct route never landed")
        def mutate(conn):
            conn.execute("UPDATE v2_assistant_composite_routes SET delivery_state=?, reply_to_message_id=? "
                         "WHERE route_id=?", (delivery, reply_to, route["route_id"]))
            conn.commit()
        await self.store.submit(mutate)
        return await self.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity=input_id)

    async def dispatch_user(self, route, identity):
        wire = json.loads(route["route_json"])["direct_envelope"]["wire_body"]
        return await self.ingest("USER", wire, identity, optimistic_id=route["dispatch_id"])

    async def answers(self):
        return [e for e in await self.store.fetch_session_event_tail(ASSISTANT, limit=100)
                if e["kind"] == "ASSIST_TEXT"]

    async def publications(self, dispatch_id):
        def read(conn):
            return [dict(r) for r in conn.execute(
                "SELECT * FROM v2_assistant_composite_publications WHERE dispatch_id=? ORDER BY event_id",
                (dispatch_id,))]
        return await self.store.submit(read)

    async def activity(self, input_id):
        return (await self.store.assistant_composite_activity(stream_id=ASSISTANT))[input_id]


def run(body):
    async def go():
        store = Store(":memory:")
        store.start()
        composite = None
        try:
            root = await store.open_session("fixture-root", "visible", provider="claude", pane_pid="4242")
            async def dispatch(_route):
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(root["session_generation"]), dispatch=dispatch)
            await composite.ensure_projection()
            await body(Harness(store, composite, root["session_generation"]))
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(go())


PROSE = "I don't have the filament total for all five levels yet. I'll answer as soon as that comes back."


def test_fixture_final_is_structured_and_mirrors_without_open_route():
    """Control: the same fixture final is mirror-eligible when no dispatch is open."""
    async def body(h):
        await h.ingest("USER", "typed directly", "plain-user")
        seq = await h.ingest("ASSIST_TEXT", PROSE, "plain-final", final=True)
        assert await h.store.assistant_mirror_event_for_source(seq) is not None
        assert [e["text"] for e in await h.answers()] == [PROSE]
    run(body)


def test_landed_unpublished_dispatch_turn_final_is_projected_once(caplog):
    async def body(h):
        route = await h.route("operator-input-1")
        assert route["reply_to_message_id"] is None
        user_seq = await h.dispatch_user(route, "dispatch-user")
        h.frames.clear()
        seq = await h.ingest("ASSIST_TEXT", PROSE, "turn-final", final=True)
        rows = await h.answers()
        assert len(rows) == 1
        row = rows[0]
        assert row["text"] == PROSE
        assert row["provider"] == "composite" and row["publish_kind"] == "status"
        assert row["message_id"] == "publication:turnfinal:" + route["dispatch_id"]
        assert row["reply_to_message_id"] == "operator-input-1"
        raw = row["raw"]
        assert raw["assistant_composite"] is True and raw["publish_kind"] == "status"
        assert raw["response_state"] == "acknowledged"
        assert raw["dispatch_id"] == route["dispatch_id"]
        assert raw["reply_to_message_id"] == "operator-input-1"
        assert raw["reply_to_question_id"] is None
        assert raw["mirrored_from"] == {"stream_id": ROOT, "generation": h.generation,
                                        "event_id": seq, "event_ts": raw["mirrored_from"]["event_ts"]}
        pubs = await h.publications(route["dispatch_id"])
        assert [p["publication_key"] for p in pubs] == ["turnfinal:" + route["dispatch_id"]]
        payload = json.loads(pubs[0]["canonical_payload_json"])
        assert payload["response_state"] == "acknowledged" and payload["message"] == PROSE
        assert pubs[0]["reply_to_message_id"] == "operator-input-1"
        frames = h.composite_frames()
        assert len(frames) == 1 and frames[0]["type"] == "chat.event"
        assert frames[0]["event"]["daemon_seq"] == row["daemon_seq"]
        assert frames[0]["event"]["raw"]["dispatch_id"] == route["dispatch_id"]
        assert (await h.activity("operator-input-1"))["response_state"] == "acknowledged"
        line = [r.getMessage() for r in caplog.records if "assistant_mirror_turnfinal_projected" in r.getMessage()]
        assert len(line) == 1
        assert f"source_event_id={seq}" in line[0] and f"trigger_event_id={user_seq}" in line[0]
        assert f"dispatch_id={route['dispatch_id']}" in line[0]
        assert f"composite_event_id={row['daemon_seq']}" in line[0]
    with caplog.at_level("INFO"):
        run(body)


@pytest.mark.parametrize("kind", ["status-ack", "prose-final"])
def test_existing_publication_blocks_projection(kind):
    """Plan 3 (i): an explicit ack or final means no projection and no duplicate."""
    async def body(h):
        route = await h.route("operator-input-1")
        await h.dispatch_user(route, "dispatch-user")
        msg = {"request_id": ("ack:" if kind == "status-ack" else "publish:") + route["dispatch_id"],
               "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
               "reply_to_message_id": "operator-input-1", "reply_to_question_id": None,
               "publish_kind": "status" if kind == "status-ack" else "prose",
               "response_state": "acknowledged" if kind == "status-ack" else "final",
               "message": "Explicit reply", "attachment_ids": [], "evidence_refs": []}
        if kind == "status-ack":
            def receipt(conn):
                conn.execute("INSERT INTO v2_assistant_composite_publications(publication_key,stream_id,"
                             "payload_digest,canonical_payload_json,dispatch_id,reply_to_message_id,"
                             "reply_to_question_id,publish_kind,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (msg["request_id"], ASSISTANT, "d", json.dumps({"response_state": "acknowledged"}),
                              route["dispatch_id"], "operator-input-1", None, "status", 0, "now"))
                conn.commit()
            await h.store.submit(receipt)
        else:
            await h.composite.publish(msg, actor_stream_id=ROOT)
        before = len(await h.answers())
        h.frames.clear()
        seq = await h.ingest("ASSIST_TEXT", PROSE, "turn-final", final=True)
        assert await h.store.assistant_mirror_event_for_source(seq) is None
        assert len(await h.answers()) == before and h.composite_frames() == []
        assert not [p for p in await h.publications(route["dispatch_id"])
                    if p["publication_key"].startswith("turnfinal:")]
    run(body)


def test_final_before_its_user_row_is_suppressed():
    """Plan 3 (ii)."""
    async def body(h):
        route = await h.route("operator-input-1")
        await h.ingest("ASSIST_TEXT", PROSE, "early-final", final=True)
        await h.dispatch_user(route, "late-user")
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)


def test_trigger_older_than_previous_final_is_suppressed():
    """Plan 3 (iii): a second final with no new USER does not reach back past the first."""
    async def body(h):
        route = await h.route("operator-input-1", delivery="committed_pending")
        await h.dispatch_user(route, "dispatch-user")
        await h.ingest("ASSIST_TEXT", "first final while pending", "final-1", final=True)
        def land(conn):
            conn.execute("UPDATE v2_assistant_composite_routes SET delivery_state='landed' WHERE route_id=?",
                         (route["route_id"],))
            conn.commit()
        await h.store.submit(land)
        await h.ingest("ASSIST_TEXT", PROSE, "final-2", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)


def test_two_landed_routes_attribute_to_newest_dispatch_user():
    """Plan 3 (iv)."""
    async def body(h):
        a = await h.route("operator-input-a")
        b = await h.route("operator-input-b")
        await h.dispatch_user(b, "user-b")
        await h.ingest("ASSIST_TEXT", PROSE, "final-b", final=True)
        rows = await h.answers()
        assert len(rows) == 1 and rows[0]["raw"]["dispatch_id"] == b["dispatch_id"]
        assert rows[0]["reply_to_message_id"] == "operator-input-b"
        assert await h.publications(a["dispatch_id"]) == []
    run(body)


@pytest.mark.parametrize("state,activity", [
    ("intent", "awaiting_reply"), ("committed_pending", "awaiting_reply"), ("uncertain", "uncertain"),
])
def test_unlanded_delivery_states_stay_suppressed(state, activity):
    """Plan 3 (v): only landed projects; delivery uncertainty precedence is unchanged."""
    async def body(h):
        route = await h.route("operator-input-1", delivery=state)
        await h.dispatch_user(route, "dispatch-user")
        before = (await h.activity("operator-input-1"))["response_state"]
        await h.ingest("ASSIST_TEXT", PROSE, "turn-final", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
        after = (await h.activity("operator-input-1"))["response_state"]
        assert after == before
        if state == "uncertain":
            assert after == activity
    run(body)


def test_reply_input_correlates_to_input_identity_not_reply_target():
    """Plan 3 (vi)."""
    async def body(h):
        route = await h.route("operator-input-2", reply_to="earlier-msg")
        assert route["reply_to_message_id"] == "earlier-msg"
        await h.dispatch_user(route, "dispatch-user")
        await h.ingest("ASSIST_TEXT", PROSE, "turn-final", final=True)
        rows = await h.answers()
        assert len(rows) == 1
        assert rows[0]["reply_to_message_id"] == "operator-input-2"
        assert rows[0]["raw"]["reply_to_message_id"] == "operator-input-2"
        pub = (await h.publications(route["dispatch_id"]))[0]
        assert pub["reply_to_message_id"] == "operator-input-2"
    run(body)


@pytest.mark.parametrize("same_record", [True, False])
def test_restart_reingest_adds_no_second_projection(same_record):
    """Plan 3 (vii): a re-read of the same final under a fresh source_event_id."""
    async def body(h):
        route = await h.route("operator-input-1")
        await h.dispatch_user(route, "dispatch-user")
        first = await h.ingest("ASSIST_TEXT", PROSE, "turn-final", final=True)
        h.frames.clear()
        if same_record:
            # The append layer drops a byte-identical re-read while the original row
            # is retained; model a re-read after retention by perturbing the row key
            # for the same provider record, then broadcast exactly as ingest does.
            event = dict((await h.store.fetch_session_event_tail(ROOT, limit=100))[-1])
            event.pop("daemon_seq", None)
            event["raw"] = {**event["raw"], "reingested": True}
            second = (await h.store.append_session_events_lifecycle_cas([
                {"stream_id": ROOT, "event": event, "identity": "restart-reread",
                 "lifecycle": await h.store.fetch_open_session_lifecycle(ROOT, pane_pid="4242")}], limit=100))[0]
            await broadcast_assistant_mirror(h.store, h.broadcast, second)
        else:
            second = await h.ingest("ASSIST_TEXT", PROSE, "turn-final-reread", final=True, record="turn-final-2")
        assert isinstance(first, int) and isinstance(second, int) and second != first
        assert len(await h.answers()) == 1
        assert len(await h.publications(route["dispatch_id"])) == 1
        assert h.composite_frames() == []
    run(body)


@pytest.mark.parametrize("normalized", [True, False])
def test_tell_triggered_final_is_suppressed(normalized):
    """Plan 3 (viii): a trusted tell after the dispatch owns the final."""
    async def body(h):
        route = await h.route("operator-input-1")
        await h.dispatch_user(route, "dispatch-user")
        await h.store.put_tell_delivery("trusted", {"reply": {"to_stream_id": ROOT},
                                                    "delivery": {"to_stream_id": ROOT}})
        text = "[from peer:seat] [tell:trusted] status"
        if normalized:
            await h.ingest("USER", text, "tell-input")
        else:
            event = {"stream_id": ROOT, "provider": "claude", "kind": "USER", "text": text,
                     "raw": {"source_session_identity": SESSION, "transport": "claude-jsonl"}}
            await append_ingested_event(h.store, h.broadcast, event, recent_limit=100,
                                        lifecycle=await h.store.fetch_open_session_lifecycle(ROOT, pane_pid="4242"))
        await h.ingest("ASSIST_TEXT", PROSE, "tell-final", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)


def test_ordinary_user_after_dispatch_suppresses_older_dispatch():
    """Plan 3 (ix): USER A(dispatch), USER X(ordinary), final X never attributes to A."""
    async def body(h):
        route = await h.route("operator-input-a")
        await h.dispatch_user(route, "user-a")
        await h.ingest("USER", "typed into the pane directly", "user-x")
        await h.ingest("ASSIST_TEXT", PROSE, "final-x", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)


def test_sequential_dispatches_each_project_to_their_own_turn():
    """Plan 3 (x)."""
    async def body(h):
        a = await h.route("operator-input-a")
        await h.dispatch_user(a, "user-a")
        await h.ingest("ASSIST_TEXT", "Holding note for A", "final-a", final=True)
        b = await h.route("operator-input-b")
        await h.dispatch_user(b, "user-b")
        await h.ingest("ASSIST_TEXT", "Holding note for B", "final-b", final=True)
        rows = await h.answers()
        assert [(r["text"], r["raw"]["dispatch_id"], r["reply_to_message_id"]) for r in rows] == [
            ("Holding note for A", a["dispatch_id"], "operator-input-a"),
            ("Holding note for B", b["dispatch_id"], "operator-input-b"),
        ]
    run(body)


def test_later_explicit_final_publish_still_lands_after_projection():
    """The holding note never blocks the seat's real final answer."""
    async def body(h):
        route = await h.route("operator-input-1")
        await h.dispatch_user(route, "dispatch-user")
        await h.ingest("ASSIST_TEXT", PROSE, "turn-final", final=True)
        await h.composite.publish({
            "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": ASSISTANT,
            "dispatch_id": route["dispatch_id"], "reply_to_message_id": "operator-input-1",
            "reply_to_question_id": None, "publish_kind": "prose", "response_state": "final",
            "message": "The full answer", "attachment_ids": [], "evidence_refs": []}, actor_stream_id=ROOT)
        assert [r["text"] for r in await h.answers()] == [PROSE, "The full answer"]
        assert (await h.activity("operator-input-1"))["response_state"] == "answered"
    run(body)
