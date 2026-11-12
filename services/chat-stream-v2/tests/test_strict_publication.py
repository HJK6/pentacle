"""Strict publication suppresses implicit replies, independently of the mirror flag."""
import pytest
import desk_stamp
from test_assistant_dispatch_turn_final import run
from test_assistant_prose_mirror import ASSISTANT, ROOT
import asyncio
import json
import shlex
from datetime import datetime
from types import SimpleNamespace
from assistant_lane_rulings import AssistantLaneRulings


def test_primary_turn_final_never_publishes_implicitly():
    async def body(h):
        route = await h.route("one-door")
        await h.dispatch_user(route, "one-door-user")
        await h.ingest("ASSIST_TEXT", "Unpublished turn prose", "one-door-final", final=True)
        assert await h.answers() == []
        assert await h.publications(route["dispatch_id"]) == []
    run(body)


def test_turn_end_notifies_once_and_copied_ack_is_accepted():
    async def body(h):
        route = await h.route("unanswered")
        await h.dispatch_user(route, "unanswered-user")
        await h.ingest("ASSIST_TEXT", "done", "unanswered-final", final=True)
        def notices(conn):
            return [dict(r) for r in conn.execute("SELECT * FROM v2_outbound_notices WHERE kind='dispatch_no_reply'")]
        rows = await h.store.submit(notices)
        assert len(rows) == 1
        assert route["dispatch_id"] in rows[0]["body"]
        command = rows[0]["body"].split("Acknowledgment: ", 1)[1].splitlines()[0]
        words = shlex.split(command)[3:]
        msg = {words[i][2:].replace("-", "_"): words[i + 1] for i in range(0, len(words), 2)}
        result = await h.composite.publish(msg, actor_stream_id=ROOT)
        assert result["duplicate"] is False
        await h.dispatch_user(route, "unanswered-user-again")
        await h.ingest("ASSIST_TEXT", "done", "unanswered-final-again", final=True)
        assert len(await h.store.submit(notices)) == 1
    run(body)


def test_unanswered_marker_at_300_seconds_once():
    async def body(h):
        route = await h.route("marker-input")
        stamp = json.loads(route["route_json"])["timings"]["landed_at"]
        landed = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        service = AssistantLaneRulings(SimpleNamespace(store=h.store, assistant_composite=h.composite,
                                                       broadcast=None))
        await service._mark_unanswered(landed + 299)
        assert await h.answers() == []
        await service._mark_unanswered(landed + 300)
        await service._mark_unanswered(landed + 301)
        answers = await h.answers()
        assert len(answers) == 1
        assert answers[0]["publish_kind"] == "unanswered"
        assert answers[0]["reply_to_message_id"] == "marker-input"
        assert answers[0]["text"] == "No reply from Bart yet to this message."
    run(body)


def test_desk_credential_is_persisted_and_dispatch_replies_are_refused(monkeypatch):
    credential = "6a54b9a1-406f-4d5f-8ffb-818b703e6a56"
    monkeypatch.setattr(desk_stamp, "load", lambda: {credential: "test-desk"})

    async def body(h):
        await h.composite.accept_input({"message": "desk message", "msg_id": "desk-1"},
                                       operator_principal="operator:" + credential)
        for _ in range(200):
            route = await h.store.get_assistant_composite_route(stream_id=ASSISTANT, input_identity="desk-1")
            if route and route["delivery_state"] == "landed":
                break
            await asyncio.sleep(.01)
        assert json.loads(route["route_json"])["desk_dispatch"] is True
        monkeypatch.setattr(desk_stamp, "load", lambda: {})
        assert (await h.activity("desk-1"))["response_state"] == "reply_not_required"
        for kind, state, suffix in (("prose", "final", ""), ("status", "acknowledged", ":ack"),
                                    ("status", None, ":status")):
            msg = {"request_id": "publish:" + route["dispatch_id"] + suffix,
                   "composite_stream_id": ASSISTANT, "dispatch_id": route["dispatch_id"],
                   "reply_to_message_id": "desk-1", "publish_kind": kind,
                   "response_state": state, "message": "reply"}
            with pytest.raises(ValueError, match="assistant_publish_desk_dispatch"):
                await h.composite.publish(msg, actor_stream_id=ROOT)
    run(body)
