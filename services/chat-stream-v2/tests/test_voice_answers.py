"""voice_answers.v1: recording-to-question binding ingest and the binding-scoped
`voice_answer.answer` entry point (front desk answers on the operator's behalf)."""

from __future__ import annotations

import asyncio
import json

import pytest

from assistant_composite import (  # noqa: E402
    AssistantComposite, AssistantCompositeConfig, direct_dispatch_envelope,
)
from notify import Notify  # noqa: E402
from store import Store  # noqa: E402
from voice_answers import voice_answer_by  # noqa: E402


COMPOSITE = "fixture-host-chat:assistant"
ROUTER_ENDPOINT = "ssh://fixture-router/assistant-router-v1"
FRONT = "fixture-host:v2-front"      # hidden seat bound to the assistant thread
LEAD = "fixture-host:v2-lead"        # operator-visible seat
WORKER = "fixture-host:v2-worker"    # hidden worker under LEAD
OTHER = "fixture-host:v2-other"      # operator-visible seat
OPERATOR = {"operator_authenticated": True, "operator_principal": "operator:fixture-cred"}
FRONT_AUTH = {"stream_id": FRONT, "token_verified": True, "session_generation": "g-front"}


class _Sessions:
    def __init__(self, rows: dict[str, dict]):
        self.rows = rows

    def get(self, stream_id: str):
        row = self.rows.get(stream_id)
        return dict(row) if row is not None else None

    def split(self, stream_id: str):
        host, _, name = stream_id.partition(":")
        return host, name

    async def visible_question_scope_stream_ids(self, viewer: str) -> list[str]:
        return [viewer] + [sid for sid, row in self.rows.items()
                           if row.get("parent_stream_id") == viewer and row.get("visibility") == "hidden"]


class _TellStore:
    async def list_delivered_tell_ids(self, *, prefix: str, limit: int) -> list[str]:
        return []


class _Outbound:
    """Captures the durable answer-to-asker queue (prompt.answer's delivery path)."""

    def __init__(self):
        self.rows: list[dict] = []

    def register_kind(self, *_args, **_kwargs) -> None:
        pass

    async def enqueue(self, **row) -> dict:
        self.rows.append(row)
        return {"created": True}


class _Comms:
    def __init__(self, sessions: _Sessions):
        self.sessions = sessions
        self.store = _TellStore()
        self.tells: list[dict] = []

    async def tell(self, message: dict) -> dict:
        self.tells.append(message)
        return {"duplicate": False, "delivery_status": "delivered"}


def _options():
    return [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]


def _actions(question_id: str):
    return [{"kind": "yes_no", "action_id": f"a{i}", "label": o["label"], "choice": i == 0,
             "value": {"schema_version": 1, "question_id": question_id, "answer": o["value"]}}
            for i, o in enumerate(_options())]


class _Harness:
    def __init__(self, tmp_path):
        self.sessions = _Sessions({
            FRONT: {"visibility": "hidden", "status": "open", "session_generation": "g-front"},
            LEAD: {"visibility": "default", "status": "open", "session_generation": "g-lead"},
            WORKER: {"visibility": "hidden", "status": "open", "session_generation": "g-worker",
                     "parent_stream_id": LEAD},
            OTHER: {"visibility": "default", "status": "open", "session_generation": "g-other"},
        })
        self.comms = _Comms(self.sessions)
        self.outbound = _Outbound()
        self.store = Store(":memory:")
        self.notify = Notify(str(tmp_path / "notifications.db"), comms=self.comms, outbound=self.outbound,
                             sessions=self.sessions, notice_store=self.store,
                             assistant_binding=self._binding, assistant_stream_id=COMPOSITE)
        self.composite = AssistantComposite(
            self.store,
            config=AssistantCompositeConfig(enabled=True, stream_id=COMPOSITE, router_endpoint=ROUTER_ENDPOINT),
            voice_answers_validate=self.notify.validate_voice_answers,
        )
        self.questions: dict[str, dict] = {}

    async def _binding(self):
        return {"stream_id": FRONT, "generation": "g-front"}

    async def __aenter__(self):
        self.store.start()
        await self.notify.start()
        await self.composite.ensure_projection()
        for qid, producer in (("q-lead", LEAD), ("q-other", OTHER), ("q-front", FRONT)):
            asked = await self.notify.prompt({
                "type": "prompt.ask", "request_id": "ask-" + qid, "from_stream_id": producer,
                "_auth_context": {"stream_id": producer, "token_verified": True,
                                  "session_generation": self.sessions.rows[producer]["session_generation"]},
                "envelope": {"schema_version": 1, "question_id": qid, "title": "Ask " + qid,
                             "body": "Proceed?", "dedup_key": "dk-" + qid, "producer_stream_id": producer,
                             "response_mode": "single_choice", "options": _options()},
                "actions": _actions(qid),
            })
            assert asked["type"] == "prompt.ask.ok", asked
            self.questions[qid] = asked["question"]
        # A hidden worker's question surfaces in its visible lead (seeded as the
        # shared store holds it; hidden seats cannot ask over prompt.ask).
        self.questions["q-worker"] = await self.notify._db.call(
            "create_agent_question",
            envelope={"schema_version": 1, "question_id": "q-worker", "title": "Worker ask",
                      "body": "Proceed?", "dedup_key": "dk-q-worker", "producer_stream_id": WORKER,
                      "response_mode": "single_choice", "options": _options()},
            actions=_actions("q-worker"))
        return self

    async def __aexit__(self, *exc):
        await self.notify.stop()
        self.store.stop()

    def item(self, qid: str, *, key: str | None = None, **overrides) -> dict:
        question = self.questions[qid]
        producer = question["producer_stream_id"]
        surface = {"q-worker": LEAD, "q-front": COMPOSITE}.get(qid, producer)
        item = {"key": key or f"{question['notification_id']}:0", "question_id": qid,
                "notification_id": question["notification_id"], "producer_stream_id": producer,
                "surface_stream_id": surface, "prompt": "Proceed?",
                "segment": {"start_s": 0.0, "end_s": 2.0}}
        item.update(overrides)
        return item

    async def send(self, items, *, recording_id="rec-1", optimistic_id="voice-turn-1",
                   text="yes to the lead, no idea about the other", auth=OPERATOR,
                   operator_principal="operator:fixture-cred", **extra_meta):
        meta = {"voice": {"duration_s": 9.5},
                "voice_answers": {"version": 1, "recording_id": recording_id, "blob_sha": "b" * 64,
                                  "duration_s": 9.5, "items": items, **extra_meta}}
        return await self.composite.accept_input({
            "text": text, "request_id": "rpc-" + optimistic_id, "optimistic_id": optimistic_id,
            "attachments": [], "meta": meta, "_auth_context": dict(auth),
        }, operator_principal=operator_principal)

    async def user_events(self):
        events = await self.store.fetch_session_event_tail(COMPOSITE, limit=50)
        return [e for e in events if e.get("kind") == "USER"]

    async def answer(self, qid, *, recording_id="rec-1", auth=FRONT_AUTH, request_id="va-1", **body):
        msg = {"type": "voice_answer.answer", "request_id": request_id, "recording_id": recording_id,
               "question_id": qid, "_auth_context": dict(auth)}
        if not body:
            body = {"selections": ["yes"]}
        msg.update(body)
        handler = self.notify.wire_handlers()["voice_answer.answer"]
        return await handler(msg)

    async def state(self, qid):
        return (await self.notify._db.call("get_agent_question", qid))["state"]


def _run(tmp_path, body):
    async def go():
        async with _Harness(tmp_path) as h:
            await body(h)
    asyncio.run(go())


# -- binding ingest ---------------------------------------------------------


def test_bound_binding_writes_status_on_user_event_and_never_closes(tmp_path):
    async def body(h):
        accepted = await h.send([h.item("q-lead"), h.item("q-other")])
        assert accepted["duplicate"] is False
        [event] = await h.user_events()
        assert event["meta"]["voice"] == {"duration_s": 9.5}
        assert event["meta"]["voice_answers_status"] == {"state": "bound", "stale_keys": []}
        echoed = event["meta"]["voice_answers"]
        assert echoed["recording_id"] == "rec-1"
        assert [i["question_id"] for i in echoed["items"]] == ["q-lead", "q-other"]
        binding = await h.store.get_voice_answer_binding("rec-1")
        assert binding["state"] == "bound" and binding["input_identity"] == "voice-turn-1"
        assert binding["stream_id"] == COMPOSITE and binding["actor_stream_id"] == "operator:fixture-cred"
        # Ingest never answers or closes a question.
        assert await h.state("q-lead") == "open" and await h.state("q-other") == "open"
        assert h.outbound.rows == []
    _run(tmp_path, body)


def test_stale_item_is_kept_and_marked_turn_not_rejected(tmp_path):
    async def body(h):
        answered = await h.notify.prompt({"type": "prompt.answer", "request_id": "op", "question_id": "q-other",
                                          "selections": ["no"], "_auth_context": OPERATOR})
        assert answered["type"] == "prompt.answer.ok"
        lead, other = h.item("q-lead", key="k-lead"), h.item("q-other", key="k-other")
        await h.send([lead, other])
        [event] = await h.user_events()
        assert event["meta"]["voice_answers_status"] == {"state": "bound", "stale_keys": ["k-other"]}
        binding = await h.store.get_voice_answer_binding("rec-1")
        assert {i["key"]: i["stale"] for i in binding["items"]} == {"k-lead": False, "k-other": True}
    _run(tmp_path, body)


@pytest.mark.parametrize("mutation,reason", [
    ({"question_id": "q-missing"}, "unknown_question"),
    ({"notification_id": "n-other"}, "notification_mismatch"),
    ({"producer_stream_id": LEAD}, "producer_mismatch"),
    ({"surface_stream_id": LEAD}, "surface_mismatch"),
])
def test_unknown_or_mismatched_item_drops_binding_but_turn_posts(tmp_path, mutation, reason):
    async def body(h):
        await h.send([h.item("q-lead"), h.item("q-other", **mutation)])
        [event] = await h.user_events()
        assert event["text"] == "yes to the lead, no idea about the other"
        assert event["meta"]["voice_answers_status"] == {"state": "dropped", "reason": reason, "stale_keys": []}
        binding = await h.store.get_voice_answer_binding("rec-1")
        assert binding["state"] == "dropped" and binding["reason"] == reason
        assert await h.state("q-lead") == "open"
    _run(tmp_path, body)


@pytest.mark.parametrize("items_fn,extra,reason", [
    (lambda h: [], {}, "invalid_payload"),
    (lambda h: [h.item("q-lead", key=f"k{i}") for i in range(21)], {}, "invalid_payload"),
    (lambda h: [h.item("q-lead", key="dup"), h.item("q-other", key="dup")], {}, "invalid_payload"),
    (lambda h: [h.item("q-lead", segment={"start_s": 3, "end_s": 1})], {}, "invalid_payload"),
    (lambda h: [h.item("q-lead")], {"version": 2}, "invalid_payload"),
])
def test_malformed_binding_is_dropped_and_turn_posts(tmp_path, items_fn, extra, reason):
    async def body(h):
        await h.send(items_fn(h), **extra)
        [event] = await h.user_events()
        status = event["meta"]["voice_answers_status"]
        assert status["state"] == "dropped" and status["reason"] == reason
    _run(tmp_path, body)


def test_hidden_seat_surfaced_question_validates_on_producer_identity(tmp_path):
    async def body(h):
        # Worker question surfaced in its lead; front-desk seat question surfaced in the thread.
        await h.send([h.item("q-worker"), h.item("q-front")])
        [event] = await h.user_events()
        assert event["meta"]["voice_answers_status"]["state"] == "bound"
        # Claiming the surfacing lead as the asker is a producer mismatch.
        await h.send([h.item("q-worker", producer_stream_id=LEAD)], recording_id="rec-2",
                     optimistic_id="voice-turn-2")
        second = (await h.user_events())[-1]
        assert second["meta"]["voice_answers_status"] == {
            "state": "dropped", "reason": "producer_mismatch", "stale_keys": []}
    _run(tmp_path, body)


def test_replayed_recording_id_creates_no_second_binding_or_turn(tmp_path):
    async def body(h):
        first = await h.send([h.item("q-lead")])
        same = await h.send([h.item("q-lead")])
        rotated = await h.send([h.item("q-lead")], optimistic_id="voice-turn-retry")
        assert same["duplicate"] is True and rotated["duplicate"] is True
        assert same["route_id"] == rotated["route_id"] == first["route_id"]
        assert len(await h.user_events()) == 1
        binding = await h.store.get_voice_answer_binding("rec-1")
        assert binding["input_identity"] == "voice-turn-1"
    _run(tmp_path, body)


def test_binding_requires_authenticated_operator_turn(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")], auth={}, operator_principal="scoped:fixture-scoped-cred")
        [event] = await h.user_events()
        assert event["meta"]["voice_answers_status"] == {
            "state": "dropped", "reason": "operator_unauthenticated", "stale_keys": []}
        refused = await h.answer("q-lead")
        assert refused["error_code"] == "voice_answer_binding_dropped"
        assert await h.state("q-lead") == "open"
    _run(tmp_path, body)


def test_plain_voice_turn_without_binding_is_unchanged(tmp_path):
    async def body(h):
        await h.composite.accept_input({"text": "hello", "request_id": "r", "optimistic_id": "plain",
                                        "attachments": [], "meta": {"voice": {"duration_s": 1.0}},
                                        "_auth_context": dict(OPERATOR)}, operator_principal="operator:fixture-cred")
        [event] = await h.user_events()
        assert event["meta"] == {"voice": {"duration_s": 1.0}}
    _run(tmp_path, body)


def test_binding_reaches_front_desk_dispatch_envelope(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead", key="k-lead")])
        route = await h.store.get_assistant_composite_route(stream_id=COMPOSITE, input_identity="voice-turn-1")
        binding = await h.store.get_voice_answer_binding("rec-1")
        envelope = direct_dispatch_envelope(h.composite.config, route, dispatch_id="d1", target=FRONT,
                                            generation="g-front", voice_answers=binding)
        assert envelope["original_input"] == {"text": route["body"], "attachments": []}
        assert "<voice-answers-binding-json>" in envelope["wire_body"]
        block = envelope["wire_body"].split("<voice-answers-binding-json>\n", 1)[1].split("\n</voice-answers", 1)[0]
        parsed = json.loads(block)
        assert parsed["recording_id"] == "rec-1" and parsed["status"]["state"] == "bound"
        assert parsed["items"][0]["question_id"] == "q-lead"
        assert "agent-orch voice-answer answer" in envelope["wire_body"] or "voice_answer.answer" in envelope["wire_body"]
    _run(tmp_path, body)


# -- voice_answer.answer ----------------------------------------------------


def test_bound_seat_answers_bound_question_with_front_desk_provenance(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead"), h.item("q-other")])
        reply = await h.answer("q-lead")
        assert reply["type"] == "voice_answer.answer.ok", reply
        assert reply["outcome"] == "answered" and reply["replayed"] is False
        assert reply["ack"]["question_id"] == "q-lead" and reply["ack"]["recording_id"] == "rec-1"
        assert reply["ack"]["provenance"] == {"actor": "front_desk", "on_behalf_of": "operator",
                                              "recording_id": "rec-1", "actor_stream_id": FRONT}
        question = await h.notify._db.call("get_agent_question", "q-lead")
        assert question["state"] == "answered"
        assert question["answer"]["selections"] == ["yes"]
        assert question["answer"]["by"] == voice_answer_by("rec-1")
        assert question["answer"]["actor_stream_id"] == FRONT
        # Same answer effects as prompt.answer: the asker is told.
        assert [row["recipient_stream_id"] for row in h.outbound.rows] == [LEAD]
        assert "by=" + voice_answer_by("rec-1") in h.outbound.rows[0]["body"]
        assert await h.state("q-other") == "open"
        binding = await h.store.get_voice_answer_binding("rec-1")
        assert set(binding["acks"]) == {"q-lead"}
    _run(tmp_path, body)


def test_free_text_answer_through_voice_entry_point(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        reply = await h.answer("q-lead", selections=None, text="go ahead with yes")
        assert reply["type"] == "voice_answer.answer.ok", reply
        question = await h.notify._db.call("get_agent_question", "q-lead")
        assert question["answer"]["custom_text"] == "go ahead with yes"
    _run(tmp_path, body)


@pytest.mark.parametrize("auth", [
    {"stream_id": LEAD, "token_verified": True, "session_generation": "g-lead"},
    {"stream_id": FRONT, "token_verified": True, "session_generation": "g-stale"},
    {"stream_id": FRONT, "token_verified": False},
    OPERATOR,
    {},
])
def test_refuses_any_caller_but_the_bound_front_desk_seat(tmp_path, auth):
    async def body(h):
        await h.send([h.item("q-lead")])
        refused = await h.answer("q-lead", auth=auth)
        assert refused["type"] == "voice_answer.answer.error"
        assert refused["error_code"] == "voice_answer_seat_unauthorized"
        assert await h.state("q-lead") == "open"
    _run(tmp_path, body)


def test_refuses_unknown_recording_and_question_outside_binding(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        unknown = await h.answer("q-lead", recording_id="rec-unknown")
        assert unknown["error_code"] == "voice_answer_binding_not_found"
        outside = await h.answer("q-other")
        assert outside["error_code"] == "voice_answer_question_not_bound"
        assert await h.state("q-other") == "open"
    _run(tmp_path, body)


def test_refuses_dropped_binding(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead"), h.item("q-other", producer_stream_id=LEAD)])
        refused = await h.answer("q-lead")
        assert refused["error_code"] == "voice_answer_binding_dropped"
        assert await h.state("q-lead") == "open"
    _run(tmp_path, body)


def test_question_closed_by_another_path_returns_stale(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        closed = await h.notify.prompt({"type": "prompt.answer", "request_id": "op", "question_id": "q-lead",
                                        "selections": ["no"], "_auth_context": OPERATOR})
        assert closed["type"] == "prompt.answer.ok"
        stale = await h.answer("q-lead")
        assert stale["type"] == "voice_answer.answer.error"
        assert stale["error_code"] == "voice_answer_stale" and stale["outcome"] == "stale"
        question = await h.notify._db.call("get_agent_question", "q-lead")
        assert question["answer"]["selections"] == ["no"]
        assert "q-lead" not in (await h.store.get_voice_answer_binding("rec-1"))["acks"]
    _run(tmp_path, body)


def test_refuses_origin_mismatch_against_stored_binding(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        # Defence in depth: a stored item whose asker no longer matches the question row.
        def _tamper(conn):
            row = conn.execute("SELECT binding_json FROM v2_voice_answer_bindings WHERE recording_id='rec-1'").fetchone()
            binding = json.loads(row[0])
            binding["items"][0]["producer_stream_id"] = OTHER
            conn.execute("UPDATE v2_voice_answer_bindings SET binding_json=? WHERE recording_id='rec-1'",
                         (json.dumps(binding),))
            conn.commit()
        await h.store.submit(_tamper)
        refused = await h.answer("q-lead")
        assert refused["error_code"] == "voice_answer_origin_mismatch"
        assert await h.state("q-lead") == "open"
    _run(tmp_path, body)


def test_lost_ack_replay_returns_stored_answered_ack(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        first = await h.answer("q-lead", request_id="va-1")
        replay = await h.answer("q-lead", request_id="va-2")
        assert replay["type"] == "voice_answer.answer.ok", replay
        assert replay["outcome"] == "answered" and replay["replayed"] is True
        assert replay["ack"] == first["ack"]
        # A different selection on replay cannot re-answer either.
        changed = await h.answer("q-lead", request_id="va-3", selections=["no"])
        assert changed["outcome"] == "answered" and changed["ack"] == first["ack"]
        question = await h.notify._db.call("get_agent_question", "q-lead")
        assert question["answer"]["selections"] == ["yes"]
        assert len(h.outbound.rows) == 1
    _run(tmp_path, body)


def test_apply_then_transport_error_then_retry_yields_exactly_one_answer(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        original = h.store.record_voice_answer_ack
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("ack write lost after the answer applied")
            return await original(*args, **kwargs)

        h.store.record_voice_answer_ack = flaky
        failed = await h.answer("q-lead", request_id="va-1")
        assert failed["type"] == "voice_answer.answer.error"
        assert await h.state("q-lead") == "answered"
        retried = await h.answer("q-lead", request_id="va-2")
        assert retried["type"] == "voice_answer.answer.ok" and retried["outcome"] == "answered"
        question = await h.notify._db.call("get_agent_question", "q-lead")
        assert question["answer"]["by"] == voice_answer_by("rec-1")
        assert len(h.outbound.rows) == 1
        binding = await h.store.get_voice_answer_binding("rec-1")
        assert set(binding["acks"]) == {"q-lead"}
    _run(tmp_path, body)


def test_generic_prompt_answer_authority_is_unchanged(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        # Binding fields on the generic verb grant nothing; an unverified caller stays refused.
        refused = await h.notify.prompt({"type": "prompt.answer", "request_id": "p", "question_id": "q-lead",
                                         "recording_id": "rec-1", "selections": ["yes"], "_auth_context": {}})
        assert refused["type"] == "prompt.error" and refused["error_code"] == "question_unauthorized"
        # The verified-relay rule on prompt.answer is untouched (and records no voice ack).
        relayed = await h.notify.prompt({"type": "prompt.answer", "request_id": "p2", "question_id": "q-lead",
                                         "selections": ["yes"], "_auth_context": FRONT_AUTH})
        assert relayed["type"] == "prompt.answer.ok"
        assert relayed["question"]["answer"]["by"] == f"agent_relay:{FRONT}"
        assert (await h.store.get_voice_answer_binding("rec-1"))["acks"] == {}
        stale = await h.answer("q-lead")
        assert stale["outcome"] == "stale"
    _run(tmp_path, body)


def test_failure_after_resolution_before_projection_retries_to_one_answer(tmp_path):
    async def body(h):
        await h.send([h.item("q-lead")])
        original = h.notify._after_resolution
        calls = {"n": 0}

        async def crash_once(record, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("lost after the resolution committed")
            return await original(record, **kwargs)

        h.notify._after_resolution = crash_once
        failed = await h.answer("q-lead", request_id="va-1")
        assert failed["error_code"] == "voice_answer_failed" and failed["outcome"] == "failed"
        # A retry with a different selection cannot replace the committed answer.
        retried = await h.answer("q-lead", request_id="va-2", selections=["no"])
        assert retried["type"] == "voice_answer.answer.ok" and retried["outcome"] == "answered", retried
        record = await h.notify._db.call("get_notification", h.questions["q-lead"]["notification_id"])
        assert record["resolution"]["selections"] == ["yes"]
        assert record["resolution"]["by"] == voice_answer_by("rec-1")
        assert record["resolution"]["actor_class"] == "front_desk_voice_answer"
    _run(tmp_path, body)


def test_voice_answer_outlives_its_submitting_connection():
    from server import _is_send_frame
    assert _is_send_frame(json.dumps({"type": "voice_answer.answer", "recording_id": "r", "question_id": "q"}))
