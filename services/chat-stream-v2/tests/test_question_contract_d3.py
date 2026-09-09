"""D3 question-contract coverage (epic_example_2026_01).

Structural admission format is replayed from the committed boundary fixture
(reproducing the banked labels); eligibility, the canonical answer payload, the
producer-generation guards and asker-lifetime expiry are asserted directly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from notify import Notify, QuestionFormatError

FIXTURE = (
    Path(__file__).parent / "fixtures" / "question_format_boundaries_20260908.json"
)

# My daemon rule name -> the banked structural label.
RULE_TO_BANKED = {
    "title_cap": "headline_length",
    "body_block_cap": "paragraph_length",
    "body_total_cap": "context_total",
    "body_block_lines": "block_raw_lines",
    "option_label_cap": "option_label_length",
    "option_desc_cap": "option_note_length",
    "option_max": "option_count",
    "context_cap_bypass": "context_in_body",
    "option_label_one_line": "option_label_one_line",
    "option_desc_one_line": "option_note_one_line",
    "ack_removed": "ack_removed",
}


def _run(coro):
    return asyncio.run(coro)


def _actions(question_id: str, options: list[dict]) -> list[dict]:
    return [
        {
            "kind": "yes_no", "action_id": f"a{i}", "label": opt.get("label"),
            "choice": i == 0,
            "value": {"schema_version": 1, "question_id": question_id, "answer": opt["value"]},
        }
        for i, opt in enumerate(options)
    ]


def _notify(tmp_path, **kw) -> Notify:
    return Notify(str(tmp_path / "notifications.db"), **kw)


def test_admission_boundaries_reproduce_banked_labels(tmp_path):
    cases = json.loads(FIXTURE.read_text())["cases"]
    notify = _notify(tmp_path)
    failures = []
    for case in cases:
        inp = case["input"]
        expected = list(case.get("expected_errors", []))
        options = inp.get("options", [])
        envelope = {
            "schema_version": 1, "question_id": "q-boundary", "dedup_key": "dk-boundary",
            "producer_stream_id": "hosta:v2-x", "response_mode": inp["response_mode"],
            "title": inp["title"], "body": inp["body"], "options": options,
        }
        if "context" in inp:
            envelope["context"] = inp["context"]
        try:
            notify._validate_prompt_envelope_and_actions(
                envelope, _actions("q-boundary", options)
            )
            got = []
        except QuestionFormatError as exc:
            got = [RULE_TO_BANKED.get(exc.rule, exc.rule)]
        if got != expected:
            failures.append((case["label"], expected, got))
    assert not failures, f"boundary label mismatches: {failures}"


def test_ack_mode_and_explicit_ack_are_removed(tmp_path):
    notify = _notify(tmp_path)
    with pytest.raises(QuestionFormatError) as raised:
        notify._validate_prompt_envelope_and_actions(
            {
                "schema_version": 1, "question_id": "q-ack", "dedup_key": "dk",
                "producer_stream_id": "hosta:v2-x", "response_mode": "ack",
                "title": "t", "body": "b",
                "options": [{"label": "Acknowledge", "value": "ack"}],
            },
            _actions("q-ack", [{"label": "Acknowledge", "value": "ack"}]),
        )
    assert raised.value.rule == "ack_removed"


def test_allow_custom_and_context_are_canonicalized(tmp_path):
    notify = _notify(tmp_path)
    for mode, options in (("single_choice", [{"label": "Yes", "value": "yes"}]),
                          ("free_text", [])):
        env, _actions_out = notify._validate_prompt_envelope_and_actions(
            {
                "schema_version": 1, "question_id": f"q-{mode}", "dedup_key": f"dk-{mode}",
                "producer_stream_id": "hosta:v2-x", "response_mode": mode,
                "title": "t", "body": "b", "options": options,
                "allow_custom": False,
            },
            _actions(f"q-{mode}", options),
        )
        assert env["allow_custom"] is True
        assert env["context"] is None


class _FakeSessions:
    """Minimal sessions registry double: rows keyed by stream id."""

    def __init__(self, rows: dict[str, dict]):
        self._rows = rows

    def get(self, stream_id: str):
        row = self._rows.get(stream_id)
        return dict(row) if row is not None else None

    def split(self, stream_id: str):
        host, _, name = stream_id.partition(":")
        return host, name


def _ask(question_id="q-ask", producer="hosta:v2-lead", body="Proceed?", options=None):
    options = [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}] if options is None else options
    return {
        "type": "prompt.ask", "request_id": f"r-{question_id}",
        "from_stream_id": producer,
        "_auth_context": {"stream_id": producer, "token_verified": True},
        "envelope": {
            "schema_version": 1, "question_id": question_id, "title": "Ask",
            "body": body, "dedup_key": f"dk-{question_id}",
            "producer_stream_id": producer, "response_mode": "single_choice",
            "options": options,
        },
        "actions": _actions(question_id, options),
    }


def test_eligibility_visible_ok_hidden_ask_parent_forged_rejected(tmp_path):
    async def go():
        sessions = _FakeSessions({
            "hosta:v2-lead": {"visibility": "visible", "status": "open", "session_generation": "g1"},
            "hosta:v2-default": {"visibility": "default", "status": "open", "session_generation": "gd"},
            "hosta:v2-worker": {"visibility": "hidden", "status": "open",
                               "session_generation": "g2", "parent_stream_id": "hosta:v2-lead"},
            "hosta:v2-nested": {"visibility": "nested", "status": "open",
                               "session_generation": "gn", "parent_stream_id": "hosta:v2-lead"},
            "hosta:v2-orphan": {"visibility": "subagent", "status": "open",
                               "session_generation": "g3"},
        })
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            # Visible producer succeeds and stores the generation.
            ok = await notify.prompt(_ask("q-vis", producer="hosta:v2-lead"))
            assert ok["type"] == "prompt.ask.ok"
            stored = await notify._db.call("get_agent_question", "q-vis")
            assert stored["producer_session_generation"] == "g1"

            # A `default`-visibility (operator-visible) seat is also eligible.
            assert (await notify.prompt(
                _ask("q-def", producer="hosta:v2-default")))["type"] == "prompt.ask.ok"

            # Hidden worker -> ask_parent naming its parent, nothing stored.
            hidden = await notify.prompt(_ask("q-hidden", producer="hosta:v2-worker"))
            assert hidden["error_code"] == "ask_parent"
            assert hidden["parent_stream_id"] == "hosta:v2-lead"
            assert await notify._db.call("get_agent_question", "q-hidden") is None

            # `nested` visibility is NOT operator-visible -> ask_parent (regression:
            # a blacklist of hidden/subagent let nested fall through to admission).
            nested = await notify.prompt(_ask("q-nested", producer="hosta:v2-nested"))
            assert nested["error_code"] == "ask_parent"
            assert nested["parent_stream_id"] == "hosta:v2-lead"
            assert await notify._db.call("get_agent_question", "q-nested") is None

            # Subagent with no parent -> ask_parent, parent null.
            orphan = await notify.prompt(_ask("q-orphan", producer="hosta:v2-orphan"))
            assert orphan["error_code"] == "ask_parent"
            assert orphan["parent_stream_id"] is None

            # Forged identity: token owner != claimed producer.
            forged = _ask("q-forged", producer="hosta:v2-lead")
            forged["_auth_context"] = {"stream_id": "hosta:v2-evil", "token_verified": True}
            forged["from_stream_id"] = "hosta:v2-evil"
            assert (await notify.prompt(forged))["error_code"] == "stream_ownership_unverified"

            # Closed producer cannot ask.
            closed = _ask("q-closed", producer="hosta:v2-gone")
            assert (await notify.prompt(closed))["error_code"] in (
                "producer_not_open", "stream_ownership_unverified")
        finally:
            await notify.stop()

    _run(go())


def test_generation_replacement_cannot_reuse_a_question_id(tmp_path):
    async def go():
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g1"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            assert (await notify.prompt(_ask("q-gen", producer="hosta:v2-lead")))["type"] == "prompt.ask.ok"
            # Same id from a replacement generation conflicts.
            rows["hosta:v2-lead"]["session_generation"] = "g2"
            conflict = await notify.prompt(_ask("q-gen", producer="hosta:v2-lead"))
            assert conflict["error_code"] == "prompt_question_generation_conflict"
        finally:
            await notify.stop()

    _run(go())


def test_canonical_answer_choice_and_free_text(tmp_path):
    async def go():
        sessions = _FakeSessions({
            "hosta:v2-lead": {"visibility": "visible", "status": "open", "session_generation": "g1"},
        })
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            await notify.prompt(_ask("q-choice", producer="hosta:v2-lead"))
            # selection + text on one payload; text maps to the stored custom_text.
            answered = await notify.prompt({
                "type": "prompt.answer", "request_id": "ans-1", "question_id": "q-choice",
                "selections": ["yes"], "text": "with a caveat",
                "_auth_context": {"operator_authenticated": True},
            })
            assert answered["type"] == "prompt.answer.ok"
            row = await notify._db.call("get_agent_question", "q-choice")
            assert row["answer"]["selections"] == ["yes"]
            assert row["answer"]["custom_text"] == "with a caveat"

            # Legacy aliases are rejected, not guessed.
            await notify.prompt(_ask("q-choice2", producer="hosta:v2-lead"))
            for alias in ("custom_text", "value", "note"):
                bad = {"type": "prompt.answer", "request_id": f"b-{alias}",
                       "question_id": "q-choice2", alias: "x",
                       "_auth_context": {"operator_authenticated": True}}
                assert (await notify.prompt(bad))["error_code"] == "prompt_invalid"

            # free_text: text only, selections rejected.
            ft = _ask("q-ft", producer="hosta:v2-lead", options=[])
            ft["envelope"]["response_mode"] = "free_text"
            ft["actions"] = []
            await notify.prompt(ft)
            assert (await notify.prompt({
                "type": "prompt.answer", "request_id": "ft-bad", "question_id": "q-ft",
                "selections": ["x"], "_auth_context": {"operator_authenticated": True},
            }))["error_code"] == "prompt_invalid"
            ok = await notify.prompt({
                "type": "prompt.answer", "request_id": "ft-ok", "question_id": "q-ft",
                "text": "a written answer", "_auth_context": {"operator_authenticated": True},
            })
            assert ok["type"] == "prompt.answer.ok"
        finally:
            await notify.stop()

    _run(go())


def test_choice_answered_with_text_only(tmp_path):
    # AC4: a choice question accepts nonblank text ALONE (no selection); the text
    # maps to the stored custom_text (opus advisory 1 coverage).
    async def go():
        sessions = _FakeSessions({
            "hosta:v2-lead": {"visibility": "visible", "status": "open", "session_generation": "g1"},
        })
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            await notify.prompt(_ask("q-textonly", producer="hosta:v2-lead"))
            resp = await notify.prompt({
                "type": "prompt.answer", "request_id": "to", "question_id": "q-textonly",
                "text": "none of the above, here is why",
                "_auth_context": {"operator_authenticated": True},
            })
            assert resp["type"] == "prompt.answer.ok", resp
            row = await notify._db.call("get_agent_question", "q-textonly")
            assert row["answer"]["custom_text"] == "none of the above, here is why"
            assert row["answer"]["selections"] == []
        finally:
            await notify.stop()

    _run(go())


def test_closing_the_asker_expires_both_records(tmp_path):
    async def go():
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g1"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            await notify.prompt(_ask("q-live", producer="hosta:v2-lead"))
            nid = (await notify._db.call("get_agent_question", "q-live"))["notification_id"]
            # Producer closes -> composed close hook calls this expiry.
            expired = await notify.expire_questions_for_closed_producer(
                "hosta:v2-lead", generation="g1")
            assert nid in expired
            assert (await notify._db.call("get_agent_question", "q-live"))["state"] == "expired"
            assert (await notify._db.call("get_notification", nid))["state"] == "expired"
        finally:
            await notify.stop()

    _run(go())


def test_answer_after_asker_replaced_expires_instead_of_delivering(tmp_path):
    async def go():
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g1"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            await notify.prompt(_ask("q-stale", producer="hosta:v2-lead"))
            # The asker is replaced by a new generation before the answer lands.
            rows["hosta:v2-lead"]["session_generation"] = "g2"
            resp = await notify.prompt({
                "type": "prompt.answer", "request_id": "stale", "question_id": "q-stale",
                "selections": ["yes"], "_auth_context": {"operator_authenticated": True},
            })
            assert resp["error_code"] == "question_producer_gone"
            assert (await notify._db.call("get_agent_question", "q-stale"))["state"] == "expired"
        finally:
            await notify.stop()

    _run(go())


def test_notification_resolve_rechecks_producer_generation(tmp_path):
    async def go():
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g1"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            ask = await notify.prompt(_ask("q-nr", producer="hosta:v2-lead"))
            nid = ask["notification"]["notification_id"]
            rows["hosta:v2-lead"]["session_generation"] = "g2"  # asker replaced
            resp = await notify.notification({
                "type": "notification.resolve", "request_id": "nr", "notification_id": nid,
                "action_kind": "yes_no", "selections": ["yes"],
                "_auth_context": {"operator_authenticated": True},
            })
            assert resp["error_code"] == "question_producer_gone"
            assert (await notify._db.call("get_agent_question", "q-nr"))["state"] == "expired"
        finally:
            await notify.stop()

    _run(go())


def test_closing_one_generation_keeps_a_successor_generations_questions(tmp_path):
    async def go():
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g2"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            # Successor (g2) has an open question; a stale (g1) question also exists.
            await notify.prompt(_ask("q-succ", producer="hosta:v2-lead"))
            # Seed a g1-generation open question directly (as if from the prior seat).
            g1_env = {
                "schema_version": 1, "question_id": "q-old", "title": "Ask", "body": "Old?",
                "dedup_key": "dk-q-old", "producer_stream_id": "hosta:v2-lead",
                "producer_session_generation": "g1", "response_mode": "single_choice",
                "options": [{"label": "Yes", "value": "yes"}], "allow_custom": True,
            }
            await notify._db.call("create_agent_question", envelope=g1_env,
                                  actions=_actions("q-old", g1_env["options"]))
            # Close of generation g1 must expire only q-old, not the successor q-succ.
            await notify.expire_questions_for_closed_producer("hosta:v2-lead", generation="g1")
            assert (await notify._db.call("get_agent_question", "q-old"))["state"] == "expired"
            assert (await notify._db.call("get_agent_question", "q-succ"))["state"] == "open"
        finally:
            await notify.stop()

    _run(go())


def test_list_and_status_do_not_serve_a_stale_open_question(tmp_path):
    async def go():
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g1"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            await notify.prompt(_ask("q-serve", producer="hosta:v2-lead"))
            rows.pop("hosta:v2-lead")  # asker gone (crash gap before expiry)
            status = await notify.prompt({"type": "prompt.status", "question_id": "q-serve"})
            assert status["question"]["state"] == "expired"
            # And a fresh list never returns it as open.
            listed = await notify.prompt({"type": "prompt.list", "open": True})
            assert "q-serve" not in [q["question_id"] for q in listed["questions"]]
        finally:
            await notify.stop()

    _run(go())


def test_startup_reconciles_open_questions_whose_asker_is_gone(tmp_path):
    async def go():
        db = str(tmp_path / "notifications.db")
        rows = {"hosta:v2-lead": {"visibility": "visible", "status": "open",
                                 "session_generation": "g1"}}
        sessions = _FakeSessions(rows)
        notify = _notify(tmp_path, sessions=sessions)
        await notify.start()
        try:
            await notify.prompt(_ask("q-orphaned", producer="hosta:v2-lead"))
        finally:
            await notify.stop()
        # Simulate a crash: the session is gone from the registry but the open
        # question survived in notifications.db.
        rows.pop("hosta:v2-lead")
        recovered = Notify(db, sessions=_FakeSessions(rows))
        await recovered.start()
        try:
            assert (await recovered._db.call(
                "get_agent_question", "q-orphaned"))["state"] == "expired"
        finally:
            await recovered.stop()

    _run(go())
