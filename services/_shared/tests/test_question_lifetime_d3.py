"""Store-level coverage for question lifecycle and terminal-state behavior."""

from __future__ import annotations

from _shared.notifications_store import (
    NotificationResolutionConflict,
    NotificationStore,
    NotificationTerminalState,
    QUESTION_STATE_ANSWERED,
    QUESTION_STATE_EXPIRED,
    QUESTION_STATE_OPEN,
)


def _store(tmp_path) -> NotificationStore:
    return NotificationStore(str(tmp_path / "notifications.db"))


def _seed(store, question_id, *, producer="hosta:v2-x", generation="g1",
          mode="single_choice", dedup=None):
    options = [] if mode == "free_text" else [
        {"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]
    envelope = {
        "schema_version": 1, "question_id": question_id, "title": "t", "body": "b",
        "dedup_key": dedup or f"dk-{question_id}", "producer_stream_id": producer,
        "producer_session_generation": generation, "response_mode": mode,
        "options": options, "allow_custom": True,
    }
    actions = [
        {"kind": "yes_no", "action_id": f"a{i}", "choice": i == 0,
         "value": {"schema_version": 1, "question_id": question_id, "answer": o["value"]}}
        for i, o in enumerate(options)
    ]
    return store.create_agent_question(envelope=envelope, actions=actions)


def test_producer_generation_is_persisted_and_readback(tmp_path):
    store = _store(tmp_path)
    q = _seed(store, "q-gen", generation="gen-42")
    assert q["producer_session_generation"] == "gen-42"
    assert store.get_agent_question("q-gen")["producer_session_generation"] == "gen-42"


def test_asker_close_expires_notification_and_question_together(tmp_path):
    store = _store(tmp_path)
    q = _seed(store, "q1")
    nid = q["notification_id"]
    expired = store.expire_open_questions_for_producer("hosta:v2-x")
    assert expired == [nid]
    assert store.get_agent_question("q1")["state"] == QUESTION_STATE_EXPIRED
    assert store.get_notification(nid)["state"] == "expired"


def test_replacement_generation_only_expires_the_old_generation(tmp_path):
    store = _store(tmp_path)
    _seed(store, "q-old", generation="g1", dedup="dk-old")
    _seed(store, "q-new", generation="g2", dedup="dk-new")
    expired = store.expire_open_questions_for_producer(
        "hosta:v2-x", superseding_generation="g2")
    assert store.get_agent_question("q-old")["state"] == QUESTION_STATE_EXPIRED
    assert store.get_agent_question("q-new")["state"] == QUESTION_STATE_OPEN
    assert len(expired) == 1


def test_only_generation_close_spares_successor_and_expires_generationless(tmp_path):
    store = _store(tmp_path)
    _seed(store, "q-g1", generation="g1", dedup="dk-g1")
    _seed(store, "q-g2", generation="g2", dedup="dk-g2")
    _seed(store, "q-null", generation=None, dedup="dk-null")
    expired = store.expire_open_questions_for_producer("hosta:v2-x", only_generation="g1")
    # Closed generation g1 and the generationless row expire; the g2 successor survives.
    assert store.get_agent_question("q-g1")["state"] == QUESTION_STATE_EXPIRED
    assert store.get_agent_question("q-null")["state"] == QUESTION_STATE_EXPIRED
    assert store.get_agent_question("q-g2")["state"] == QUESTION_STATE_OPEN
    assert len(expired) == 2


def test_answer_then_close_keeps_answered_first_terminal_wins(tmp_path):
    store = _store(tmp_path)
    q = _seed(store, "q-ans")
    store.answer_agent_question_for_notification(
        q["notification_id"], {"selections": ["yes"], "by": "operator"})
    assert store.get_agent_question("q-ans")["state"] == QUESTION_STATE_ANSWERED
    # A later producer close must not clobber the accepted answer.
    store.expire_open_questions_for_producer("hosta:v2-x")
    assert store.get_agent_question("q-ans")["state"] == QUESTION_STATE_ANSWERED


def test_close_before_answer_cannot_reopen(tmp_path):
    store = _store(tmp_path)
    q = _seed(store, "q-exp")
    store.expire_open_questions_for_producer("hosta:v2-x")
    assert store.get_agent_question("q-exp")["state"] == QUESTION_STATE_EXPIRED
    # Resolving an already-expired notification is a terminal-state conflict.
    try:
        store.resolve_notification(
            q["notification_id"], action_kind="yes_no", by="operator",
            choice=True, selections=["yes"])
        reopened = True
    except (NotificationTerminalState, NotificationResolutionConflict):
        reopened = False
    assert reopened is False
    assert store.get_agent_question("q-exp")["state"] == QUESTION_STATE_EXPIRED


def test_one_shot_clear_is_paired_idempotent_and_preserves_answers(tmp_path):
    store = _store(tmp_path)
    # An answered row (must be preserved), a NULL-producer open row, a normal open row.
    ans = _seed(store, "q-answered", dedup="dk-a")
    store.answer_agent_question_for_notification(
        ans["notification_id"], {"selections": ["yes"], "by": "operator"})
    _seed(store, "q-null", producer=None, generation=None, dedup="dk-null")
    open_row = _seed(store, "q-open", dedup="dk-open")

    dry = store.clear_open_agent_questions_once(dry_run=True)
    assert dry["cleared"] == 2 and dry["already"] is False and dry["open_after"] == dry["open_before"]
    assert store.get_agent_question("q-open")["state"] == QUESTION_STATE_OPEN

    result = store.clear_open_agent_questions_once()
    assert result["cleared"] == 2
    assert result["open_after"] == 0
    assert store.get_agent_question("q-open")["state"] == QUESTION_STATE_EXPIRED
    assert store.get_agent_question("q-null")["state"] == QUESTION_STATE_EXPIRED
    # Terminal answer preserved.
    assert store.get_agent_question("q-answered")["state"] == QUESTION_STATE_ANSWERED
    assert store.get_notification(open_row["notification_id"])["state"] == "expired"

    # Idempotent: a second run is a no-op and clears nothing new.
    again = store.clear_open_agent_questions_once()
    assert again["already"] is True and again["open_after"] == 0


def test_one_shot_clear_marker_blocks_post_clear_questions(tmp_path):
    store = _store(tmp_path)
    _seed(store, "q-pre", dedup="dk-pre")
    store.clear_open_agent_questions_once()
    # A question created AFTER the marker survives a re-run of the clear.
    _seed(store, "q-post", dedup="dk-post")
    result = store.clear_open_agent_questions_once()
    assert result["already"] is True
    assert store.get_agent_question("q-post")["state"] == QUESTION_STATE_OPEN


def test_ttl_expiry_uses_the_shared_terminalization_path(tmp_path):
    store = _store(tmp_path)
    envelope = {
        "schema_version": 1, "question_id": "q-ttl", "title": "t", "body": "b",
        "dedup_key": "dk-ttl", "producer_stream_id": "hosta:v2-x",
        "producer_session_generation": "g1", "response_mode": "free_text",
        "options": [], "allow_custom": True, "ttl_seconds": 1,
    }
    q = store.create_agent_question(envelope=envelope, actions=[])
    expired = store.expire_due(now="2999-01-01T00:00:00Z")
    assert q["notification_id"] in expired
    assert store.get_agent_question("q-ttl")["state"] == QUESTION_STATE_EXPIRED

