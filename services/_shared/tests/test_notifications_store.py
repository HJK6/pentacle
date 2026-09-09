"""Tests for the durable notification store.

Behavior coverage:

* insert + get + list (states filter, limit, newest-first ordering).
* create-time validation: bad severity, bad action kind, spawn_worker missing
  provider/prompt all raise InvalidNotification.
* dedup: same (producer, dedup_key) while open collapses to one refreshed row
  with created_at preserved + updated_at bumped; a different dedup_key makes a
  second row; resolving the open row lets a fresh dedup row be created.
* resolve each action kind (ack/yes_no/spawn_worker/resolved): end state,
  resolution payload, resolved_at/updated_at set.
* resolve error paths: missing -> NotificationNotFound, already-resolved ->
  NotificationTerminalState, yes_no without bool choice -> InvalidNotification.
* expire_due marks only due open rows.
* retention: open rows never pruned, keep-N bound, age-based prune via injected
  now, prune idempotent.
* restart durability: persist a mix of open + resolved to a tmp-file DB, close,
  reopen, assert records + states survive.
"""

from __future__ import annotations

import json
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from _shared import notifications_store
from _shared.notifications_store import (
    InvalidNotification,
    NotificationNotFound,
    NotificationResolutionConflict,
    NotificationStore,
    NotificationTerminalState,
    STATE_ACKED,
    STATE_ANSWERED,
    STATE_EXPIRED,
    STATE_DONE,
    STATE_FAILED,
    STATE_OPEN,
    STATE_RESOLVED,
    STATE_RUNNING,
    STATE_SPAWNED,
    QUESTION_STATE_DISMISSED,
)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture
def store() -> NotificationStore:
    s = NotificationStore(":memory:")
    yield s
    s.close()


# ----------------------------------------------------------------------
# Insert + get + list
# ----------------------------------------------------------------------


def test_create_returns_record_and_round_trips(store):
    rec = store.create_notification(
        producer="schedule_timer",
        title="Schedule failed",
        body="host offline",
        severity="warning",
        actions=[{"kind": "ack"}],
    )
    assert isinstance(rec["notification_id"], str) and len(rec["notification_id"]) >= 32
    assert rec["producer"] == "schedule_timer"
    assert rec["title"] == "Schedule failed"
    assert rec["body"] == "host offline"
    assert rec["severity"] == "warning"
    assert rec["state"] == STATE_OPEN
    assert rec["actions"] == [{"kind": "ack", "action_id": "a0"}]
    assert rec["resolution"] is None
    assert rec["resolved_at"] is None
    assert rec["created_at"] is not None
    assert rec["updated_at"] is not None

    fetched = store.get_notification(rec["notification_id"])
    assert fetched == rec


def test_init_migrates_old_schema_answer_to_stream_id(tmp_path):
    db_path = tmp_path / "old-notifications.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE notifications (
            notification_id TEXT NOT NULL PRIMARY KEY,
            created_at TEXT,
            updated_at TEXT,
            producer TEXT NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            dedup_key TEXT,
            state TEXT NOT NULL,
            actions TEXT,
            resolution TEXT,
            ttl_seconds INTEGER,
            expires_at TEXT,
            resolved_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO notifications (
            notification_id, created_at, updated_at, producer, severity, title,
            body, dedup_key, state, actions, resolution, ttl_seconds, expires_at,
            resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "old-1",
            "2026-06-19T00:00:00+00:00",
            "2026-06-19T00:00:00+00:00",
            "old-producer",
            "info",
            "old title",
            None,
            None,
            STATE_OPEN,
            "[]",
            None,
            None,
            None,
            None,
        ),
    )
    conn.commit()
    conn.close()

    migrated = NotificationStore(db_path)
    try:
        columns = {
            row["name"]
            for row in migrated._conn.execute("PRAGMA table_info(notifications)")
        }
        assert "answer_to_stream_id" in columns
        assert "firing_history" in columns
        assert "last_resurfaced_at" in columns
        old = migrated.get_notification("old-1")
        assert old["title"] == "old title"
        assert old["answer_to_stream_id"] is None
        assert old["firing_history"] == []

        new = migrated.create_notification(
            producer="agent",
            title="question",
            answer_to_stream_id="hostb:codex-publisher",
        )
        assert new["answer_to_stream_id"] == "hostb:codex-publisher"
        assert migrated.get_notification(new["notification_id"])["answer_to_stream_id"] == (
            "hostb:codex-publisher"
        )
    finally:
        migrated.close()


def test_create_defaults(store):
    rec = store.create_notification(producer="p", title="t", now="2026-07-06T00:00:00Z")
    assert rec["severity"] == "info"
    assert rec["body"] is None
    assert rec["actions"] == []
    assert rec["dedup_key"] is None
    assert rec["ttl_seconds"] == 7 * 24 * 60 * 60
    assert rec["expires_at"] == "2026-07-13T00:00:00Z"


@pytest.mark.parametrize(
    ("severity", "expected_days"),
    [
        ("info", 7),
        ("warning", 7),
        ("critical", 30),
    ],
)
def test_create_without_ttl_stamps_default_expires_at_by_severity(store, severity, expected_days):
    now = "2026-07-06T12:00:00Z"
    rec = store.create_notification(
        producer="p",
        title="t",
        severity=severity,
        now=now,
    )

    assert rec["ttl_seconds"] == expected_days * 24 * 60 * 60
    assert _parse_iso(rec["expires_at"]) == _parse_iso(now) + timedelta(days=expected_days)


def test_create_keeps_explicit_ttl_and_zero_opt_out(store):
    with_ttl = store.create_notification(
        producer="p",
        title="ttl",
        ttl_seconds=60,
        now="2026-07-06T00:00:00Z",
    )
    assert with_ttl["ttl_seconds"] == 60
    assert with_ttl["expires_at"] == "2026-07-06T00:01:00Z"

    opt_out = store.create_notification(
        producer="p",
        title="permanent",
        ttl_seconds=0,
        now="2026-07-06T00:00:00Z",
    )
    assert opt_out["ttl_seconds"] is None
    assert opt_out["expires_at"] is None


def test_agent_question_notification_is_exempt_from_default_ttl(store):
    rec = store.create_notification(
        producer="agent_question.v1",
        title="Question",
        now="2026-07-06T00:00:00Z",
    )

    assert rec["ttl_seconds"] is None
    assert rec["expires_at"] is None


def test_agent_question_create_refresh_answer_and_list(store):
    envelope = {
        "schema_version": 1,
        "question_id": "q-1",
        "producer_stream_id": "hostb:codex-a",
        "producer_provider": "codex",
        "spec_id": "example__prompt_protocol",
        "dedup_key": "agent-question:hostb:codex-a:q-1",
        "title": "Choose",
        "body": "Pick one",
        "context": None,
        "response_mode": "single_choice",
        "options": [{"label": "Proceed", "value": "proceed"}],
        "default_action": None,
        "ttl_seconds": None,
        "created_at": "2026-07-03T00:00:00Z",
        "updated_at": "2026-07-03T00:00:00Z",
        "answered_at": None,
        "answer": None,
    }
    actions = [
        {
            "kind": "yes_no",
            "action_id": "a0",
            "label": "Proceed",
            "choice": True,
            "value": {"schema_version": 1, "question_id": "q-1", "answer": "proceed"},
        }
    ]

    question = store.create_agent_question(envelope=envelope, actions=actions)
    notification_id = question["notification_id"]
    assert question["state"] == "open"
    assert store.get_notification(notification_id)["dedup_key"] == envelope["dedup_key"]
    assert store.get_notification(notification_id)["expires_at"] is None

    refreshed_envelope = dict(envelope)
    refreshed_envelope["body"] = "Pick one now"
    refreshed = store.create_agent_question(envelope=refreshed_envelope, actions=actions)
    assert refreshed["notification_id"] == notification_id
    assert refreshed["envelope"]["body"] == "Pick one now"
    assert len(store.list_agent_questions(producer_stream_id="hostb:codex-a")) == 1
    assert len(store.list_agent_questions(spec_id="example__prompt_protocol", open_only=True)) == 1

    record = store.resolve_notification(
        notification_id,
        action_kind="yes_no",
        by="operator",
        choice=True,
        action_id="a0",
        label="Proceed",
        value={"schema_version": 1, "question_id": "q-1", "answer": "proceed"},
    )
    answer = {
        "notification_id": notification_id,
        "action_id": "a0",
        "action_kind": "yes_no",
        "label": "Proceed",
        "by": "operator",
        "at": record["resolved_at"],
        "choice": True,
        "value": {"schema_version": 1, "question_id": "q-1", "answer": "proceed"},
    }
    answered = store.answer_agent_question_for_notification(notification_id, answer)
    assert answered["state"] == "answered"
    assert answered["answer"]["value"]["answer"] == "proceed"
    assert store.get_agent_question("q-1")["answer"]["action_id"] == "a0"
    assert store.list_agent_questions(open_only=True) == []


def test_list_agent_questions_filters_multiple_producer_stream_ids(store):
    first = _create_store_question(
        store,
        question_id="q-first",
        producer_stream_id="hosta:child-a",
    )
    second = _create_store_question(
        store,
        question_id="q-second",
        producer_stream_id="hosta:child-b",
    )
    _create_store_question(
        store,
        question_id="q-unrelated",
        producer_stream_id="hosta:unrelated",
    )

    answer = {
        "notification_id": second["notification_id"],
        "action_kind": "yes_no",
        "by": "operator",
        "at": "2026-07-03T00:00:01Z",
        "selections": ["proceed"],
    }
    store.answer_agent_question_for_notification(second["notification_id"], answer)

    rows = store.list_agent_questions(
        producer_stream_ids=["hosta:child-a", "hosta:child-b"],
        open_only=True,
    )

    assert [row["question_id"] for row in rows] == ["q-first"]
    assert store.list_agent_questions(producer_stream_id="hosta:child-b")[0]["question_id"] == "q-second"
    assert store.list_agent_questions(producer_stream_ids=[]) == []


def _agent_question_envelope(
    question_id: str = "q-store",
    *,
    producer_stream_id: str = "hostb:codex-a",
    response_mode: str = "single_choice",
    options: list[dict[str, str]] | None = None,
) -> dict:
    if options is None:
        options = [{"label": "Proceed", "value": "proceed"}]
    return {
        "schema_version": 1,
        "question_id": question_id,
        "producer_stream_id": producer_stream_id,
        "producer_provider": "codex",
        "spec_id": "example__question_protocol",
        "dedup_key": f"agent-question:{producer_stream_id}:{question_id}",
        "title": "Choose",
        "body": "Pick one",
        "context": None,
        "response_mode": response_mode,
        "options": options,
        "default_action": None,
        "ttl_seconds": None,
        "created_at": "2026-07-03T00:00:00Z",
        "updated_at": "2026-07-03T00:00:00Z",
        "answered_at": None,
        "answer": None,
    }


def _agent_question_actions(question_id: str, options: list[dict[str, str]]) -> list[dict]:
    return [
        {
            "kind": "yes_no",
            "action_id": f"a{index}",
            "label": option["label"],
            "choice": index == 0,
            "value": {"schema_version": 1, "question_id": question_id, "answer": option["value"]},
        }
        for index, option in enumerate(options)
    ]


def _create_store_question(
    store: NotificationStore,
    *,
    question_id: str = "q-store",
    producer_stream_id: str = "hostb:codex-a",
    response_mode: str = "single_choice",
    options: list[dict[str, str]] | None = None,
) -> dict:
    envelope = _agent_question_envelope(
        question_id,
        producer_stream_id=producer_stream_id,
        response_mode=response_mode,
        options=options,
    )
    actions = _agent_question_actions(question_id, envelope["options"])
    return store.create_agent_question(envelope=envelope, actions=actions)


def test_agent_question_answer_persists_selections_and_note(store):
    options = [
        {"label": "Alpha", "value": "alpha"},
        {"label": "Beta", "value": "beta"},
    ]
    multi = _create_store_question(
        store, question_id="q-multi", response_mode="multi_choice", options=options
    )
    answer = {
        "notification_id": multi["notification_id"],
        "action_id": "a0",
        "action_kind": "yes_no",
        "label": "Alpha",
        "by": "operator",
        "at": "2026-07-03T00:00:01Z",
        "selections": ["alpha", "beta"],
        "note": "ship both",
    }

    answered = store.answer_agent_question_for_notification(multi["notification_id"], answer)

    assert answered["answer"]["selections"] == ["alpha", "beta"]
    assert answered["answer"]["note"] == "ship both"

    single = _create_store_question(store, question_id="q-single")
    single_answer = {
        "notification_id": single["notification_id"],
        "action_id": "a0",
        "action_kind": "yes_no",
        "label": "Proceed",
        "by": "operator",
        "at": "2026-07-03T00:00:02Z",
        "selections": ["proceed"],
        "note": "ok",
    }
    single_answered = store.answer_agent_question_for_notification(
        single["notification_id"], single_answer
    )
    assert single_answered["answer"]["value"]["answer"] == "proceed"
    assert single_answered["answer"]["choice"] is True


def test_agent_question_answer_rejects_selections_not_in_options(store):
    question = _create_store_question(store)

    with pytest.raises(InvalidNotification):
        store.answer_agent_question_for_notification(
            question["notification_id"],
            {
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "selections": ["bogus"],
            },
        )

    assert store.get_agent_question("q-store")["answer"] is None


def test_agent_question_answer_rejects_selection_cardinality_for_mode(store):
    single = _create_store_question(store, question_id="q-single")

    with pytest.raises(InvalidNotification):
        store.answer_agent_question_for_notification(
            single["notification_id"],
            {
                "notification_id": single["notification_id"],
                "action_kind": "yes_no",
                "selections": ["proceed", "proceed"],
            },
        )

    assert store.get_agent_question("q-single")["answer"] is None

    multi = _create_store_question(
        store,
        question_id="q-multi",
        response_mode="multi_choice",
        options=[{"label": "Alpha", "value": "alpha"}],
    )
    with pytest.raises(InvalidNotification):
        store.resolve_notification(
            multi["notification_id"],
            action_kind="yes_no",
            by="operator",
            choice=True,
            selections=[],
        )
    assert store.get_notification(multi["notification_id"])["state"] == "open"


def test_agent_question_answer_rejects_non_string_note(store):
    question = _create_store_question(store)

    with pytest.raises(InvalidNotification):
        store.answer_agent_question_for_notification(
            question["notification_id"],
            {
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "selections": ["proceed"],
                "note": {"text": "bad"},
            },
        )

    assert store.get_agent_question("q-store")["answer"] is None


def test_agent_question_resolved_notification_cascades_to_dismissed(store):
    question = _create_store_question(store, question_id="q-dismiss")

    record = store.resolve_notification(
        question["notification_id"],
        action_kind="resolved",
        by="operator",
        note="moot",
    )

    dismissed = store.get_agent_question("q-dismiss")
    assert record["state"] == STATE_RESOLVED
    assert dismissed["state"] == QUESTION_STATE_DISMISSED
    assert dismissed["answer"]["action_kind"] == "resolved"
    assert dismissed["answer"]["note"] == "moot"
    assert store.list_agent_questions(open_only=True) == []


def test_agent_question_resolve_open_dedup_cascades_to_dismissed(store):
    question = _create_store_question(store, question_id="q-dedup")

    record = store.resolve_open_dedup(
        producer="agent_question.v1",
        dedup_key=question["dedup_key"],
        by="operator",
    )

    assert record is not None
    assert record["notification_id"] == question["notification_id"]
    assert store.get_agent_question("q-dedup")["state"] == QUESTION_STATE_DISMISSED


def test_reconcile_closes_open_question_with_terminal_linked_notification(store):
    question = _create_store_question(store, question_id="q-reconcile")
    store.resolve_notification(
        question["notification_id"],
        action_kind="resolved",
        by="operator",
        note="already closed",
    )
    with store._lock:
        store._conn.execute(
            "UPDATE agent_questions SET state = ?, answer = NULL WHERE question_id = ?",
            ("open", "q-reconcile"),
        )
        store._conn.commit()

    reconciled = store.reconcile_agent_questions_with_terminal_notifications(
        now="2026-07-06T16:00:00Z"
    )

    assert reconciled == ["q-reconcile"]
    question = store.get_agent_question("q-reconcile")
    assert question["state"] == QUESTION_STATE_DISMISSED
    assert question["answer"]["note"] == "already closed"


def test_agent_question_rejects_open_dedup_key_conflict(store):
    envelope = {
        "schema_version": 1,
        "question_id": "q-1",
        "producer_stream_id": "hostb:codex-a",
        "producer_provider": "codex",
        "spec_id": "example__prompt_protocol",
        "dedup_key": "agent-question:shared",
        "title": "Choose",
        "body": "Pick one",
        "context": None,
        "response_mode": "single_choice",
        "options": [{"label": "Proceed", "value": "proceed"}],
        "default_action": None,
        "ttl_seconds": None,
        "created_at": "2026-07-03T00:00:00Z",
        "updated_at": "2026-07-03T00:00:00Z",
        "answered_at": None,
        "answer": None,
    }
    actions = [
        {
            "kind": "yes_no",
            "action_id": "a0",
            "label": "Proceed",
            "choice": True,
            "value": {"schema_version": 1, "question_id": "q-1", "answer": "proceed"},
        }
    ]

    first = store.create_agent_question(envelope=envelope, actions=actions)
    second = dict(envelope)
    second["question_id"] = "q-2"
    second_actions = [dict(actions[0])]
    second_actions[0]["value"] = dict(actions[0]["value"])
    second_actions[0]["value"]["question_id"] = "q-2"

    with pytest.raises(InvalidNotification, match="dedup_key already belongs"):
        store.create_agent_question(envelope=second, actions=second_actions)

    assert store.get_agent_question("q-2") is None
    assert len(store.list_agent_questions(open_only=True)) == 1
    assert len(store.list_notifications(states=[STATE_OPEN])) == 1
    assert store.list_agent_questions(open_only=True)[0]["notification_id"] == first["notification_id"]


def test_create_assigns_action_ids_by_position_and_preserves_supplied_ids(store):
    rec = store.create_notification(
        producer="p",
        title="t",
        actions=[
            {"kind": "ack"},
            {"kind": "yes_no", "action_id": "custom-yes"},
            {"kind": "ack"},
        ],
    )

    assert rec["actions"] == [
        {"kind": "ack", "action_id": "a0"},
        {"kind": "yes_no", "action_id": "custom-yes"},
        {"kind": "ack", "action_id": "a2"},
    ]


def test_create_rejects_duplicate_action_ids(store):
    with pytest.raises(InvalidNotification, match="duplicate action_id"):
        store.create_notification(
            producer="p",
            title="t",
            actions=[
                {"kind": "ack", "action_id": "same"},
                {"kind": "yes_no", "action_id": "same"},
            ],
        )


def test_create_rejects_supplied_action_id_colliding_with_auto_position(store):
    with pytest.raises(InvalidNotification, match="duplicate action_id"):
        store.create_notification(
            producer="p",
            title="t",
            actions=[
                {"kind": "ack", "action_id": "a1"},
                {"kind": "ack"},
            ],
        )


def test_create_rejects_empty_action_id(store):
    with pytest.raises(InvalidNotification, match="action_id must be a non-empty string"):
        store.create_notification(
            producer="p",
            title="t",
            actions=[{"kind": "ack", "action_id": " "}],
        )


def test_get_missing_returns_none(store):
    assert store.get_notification("nope") is None


def test_create_injected_id_is_used(store):
    rec = store.create_notification(producer="p", title="t", notification_id="fixed-id")
    assert rec["notification_id"] == "fixed-id"


def test_list_states_filter_limit_and_ordering(store):
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    a = store.create_notification(producer="p", title="a", now=_iso(base))
    b = store.create_notification(producer="p", title="b", now=_iso(base + timedelta(minutes=1)))
    c = store.create_notification(producer="p", title="c", now=_iso(base + timedelta(minutes=2)))

    # Newest-first by created_at.
    ids = [r["notification_id"] for r in store.list_notifications()]
    assert ids == [c["notification_id"], b["notification_id"], a["notification_id"]]

    # Limit caps the result.
    limited = store.list_notifications(limit=2)
    assert [r["notification_id"] for r in limited] == [
        c["notification_id"],
        b["notification_id"],
    ]

    # States filter.
    store.resolve_notification(b["notification_id"], action_kind="ack", by="op")
    open_ids = {r["notification_id"] for r in store.list_notifications(states=[STATE_OPEN])}
    assert open_ids == {a["notification_id"], c["notification_id"]}
    acked_ids = [r["notification_id"] for r in store.list_notifications(states=[STATE_ACKED])]
    assert acked_ids == [b["notification_id"]]


def test_list_same_created_at_tie_breaks_by_notification_id(store):
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    # Inject ids so the tie-break is deterministic. The tie-break is
    # notification_id DESC (a stable column, unlike rowid which SQLite reuses
    # after deletes), so the lexicographically-larger id sorts first.
    low = store.create_notification(
        producer="p", title="low", notification_id="id-aaa", now=_iso(base)
    )
    high = store.create_notification(
        producer="p", title="high", notification_id="id-bbb", now=_iso(base)
    )
    ids = [r["notification_id"] for r in store.list_notifications()]
    # Same created_at -> newest-first by notification_id DESC.
    assert ids == [high["notification_id"], low["notification_id"]]


def test_count_open(store):
    store.create_notification(producer="p", title="a")
    b = store.create_notification(producer="p", title="b")
    assert store.count_open() == 2
    store.resolve_notification(b["notification_id"], action_kind="resolved", by="op")
    assert store.count_open() == 1


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def test_create_rejects_empty_producer_and_title(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="", title="t")
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="p", title="")


def test_create_rejects_bad_severity(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="p", title="t", severity="urgent")


def test_create_rejects_bad_action_kind(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(
            producer="p", title="t", actions=[{"kind": "explode"}]
        )


def test_create_accepts_resolved_action_kind(store):
    rec = store.create_notification(producer="p", title="t", actions=[{"kind": "resolved", "label": "Resolve"}])
    assert rec["actions"] == [{"kind": "resolved", "label": "Resolve", "action_id": "a0"}]


def test_create_rejects_non_dict_action(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="p", title="t", actions=["ack"])


def test_create_rejects_spawn_worker_missing_provider(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(
            producer="p",
            title="t",
            actions=[{"kind": "spawn_worker", "prompt": "do the thing"}],
        )


def test_create_rejects_spawn_worker_missing_prompt_and_spec(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(
            producer="p",
            title="t",
            actions=[{"kind": "spawn_worker", "provider": "claude"}],
        )


def test_create_accepts_spawn_worker_with_spec_id(store):
    rec = store.create_notification(
        producer="p",
        title="t",
        actions=[{"kind": "spawn_worker", "provider": "claude", "spec_id": "spec_x"}],
    )
    assert rec["actions"][0]["spec_id"] == "spec_x"


def test_create_accepts_run_command_action(store):
    action = {
        "kind": "run_command",
        "label": "Accept",
        "command_id": "demo_ok",
        "args": {"date": "2026-05-29", "hours": 8},
        "timeout_seconds": 30,
        "on_failure": {
            "kind": "spawn_worker",
            "provider": "codex",
            "prompt": "Investigate failure",
        },
    }
    rec = store.create_notification(producer="p", title="t", actions=[action])
    assert rec["actions"] == [{**action, "action_id": "a0"}]


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "run_command"},
        {"kind": "run_command", "command_id": ""},
        {"kind": "run_command", "command_id": "   "},
        {"kind": "run_command", "command_id": 123},
        {"kind": "run_command", "command_id": "demo_ok", "args": []},
        {"kind": "run_command", "command_id": "demo_ok", "timeout_seconds": 0},
        {"kind": "run_command", "command_id": "demo_ok", "timeout_seconds": -1},
        {"kind": "run_command", "command_id": "demo_ok", "timeout_seconds": True},
        {"kind": "run_command", "command_id": "demo_ok", "timeout_seconds": float("nan")},
        {"kind": "run_command", "command_id": "demo_ok", "on_failure": "spawn"},
        {
            "kind": "run_command",
            "command_id": "demo_ok",
            "on_failure": {"kind": "ack"},
        },
        {
            "kind": "run_command",
            "command_id": "demo_ok",
            "on_failure": {"kind": "spawn_worker", "prompt": "missing provider"},
        },
        {
            "kind": "run_command",
            "command_id": "demo_ok",
            "on_failure": {"kind": "spawn_worker", "provider": "codex"},
        },
    ],
)
def test_create_rejects_malformed_run_command_action(store, action):
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="p", title="t", actions=[action])


# ----------------------------------------------------------------------
# TTL / expires_at
# ----------------------------------------------------------------------


def test_create_computes_expires_at_for_positive_ttl(store):
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    rec = store.create_notification(
        producer="p", title="t", ttl_seconds=60, now=_iso(now)
    )
    assert rec["ttl_seconds"] == 60
    assert notifications_store._parse_iso(rec["expires_at"]) == now + timedelta(seconds=60)


def test_create_no_expires_at_without_ttl(store):
    rec = store.create_notification(producer="p", title="t", ttl_seconds=0)
    assert rec["expires_at"] is None


# ----------------------------------------------------------------------
# Dedup
# ----------------------------------------------------------------------


def test_dedup_collapses_open_rows_and_refreshes(store):
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    first = store.create_notification(
        producer="p",
        title="orig title",
        body="orig body",
        severity="info",
        dedup_key="cond-1",
        now=_iso(base),
    )
    second = store.create_notification(
        producer="p",
        title="new title",
        body="new body",
        severity="warning",
        dedup_key="cond-1",
        now=_iso(base + timedelta(minutes=5)),
    )
    # Same row.
    assert second["notification_id"] == first["notification_id"]
    # Exactly one row total.
    assert len(store.list_notifications()) == 1
    # Presentation fields refreshed.
    assert second["title"] == "new title"
    assert second["body"] == "new body"
    assert second["severity"] == "warning"
    # created_at preserved, updated_at bumped, still open.
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"] == _iso(base + timedelta(minutes=5))
    assert second["updated_at"] != first["updated_at"]
    assert second["state"] == STATE_OPEN


def test_dedup_different_key_makes_two_rows(store):
    store.create_notification(producer="p", title="a", dedup_key="cond-1")
    store.create_notification(producer="p", title="b", dedup_key="cond-2")
    assert len(store.list_notifications()) == 2


def test_dedup_after_resolve_inserts_fresh_row(store):
    first = store.create_notification(producer="p", title="a", dedup_key="cond-1")
    store.resolve_notification(first["notification_id"], action_kind="ack", by="op")
    second = store.create_notification(producer="p", title="b", dedup_key="cond-1")
    assert second["notification_id"] != first["notification_id"]
    assert second["state"] == STATE_OPEN
    assert len(store.list_notifications()) == 2


def test_dedup_none_key_never_collapses(store):
    a = store.create_notification(producer="p", title="a")
    b = store.create_notification(producer="p", title="a")
    assert a["notification_id"] != b["notification_id"]
    assert len(store.list_notifications()) == 2


# ----------------------------------------------------------------------
# Resolve
# ----------------------------------------------------------------------


def test_resolve_ack(store):
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    rec = store.create_notification(producer="p", title="t")
    out = store.resolve_notification(
        rec["notification_id"], action_kind="ack", by="Example User", now=_iso(now)
    )
    assert out["state"] == STATE_ACKED
    assert out["resolution"]["by"] == "Example User"
    assert out["resolution"]["at"] == _iso(now)
    assert out["resolution"]["action_kind"] == "ack"
    assert out["resolution"]["canonical_intent"]["notification_id"] == rec["notification_id"]
    assert out["resolved_at"] == _iso(now)
    assert out["updated_at"] == _iso(now)


def test_resolve_yes_no_carries_choice(store):
    rec = store.create_notification(producer="p", title="t")
    out = store.resolve_notification(
        rec["notification_id"], action_kind="yes_no", by="op", choice=True
    )
    assert out["state"] == STATE_ANSWERED
    assert out["resolution"]["choice"] is True
    assert out["resolution"]["action_kind"] == "yes_no"


def test_resolve_spawn_worker_carries_stream_id(store):
    rec = store.create_notification(producer="p", title="t")
    out = store.resolve_notification(
        rec["notification_id"],
        action_kind="spawn_worker",
        by="op",
        spawned_stream_id="hosta:codex-hosta-xyz",
    )
    assert out["state"] == STATE_SPAWNED
    assert out["resolution"]["spawned_stream_id"] == "hosta:codex-hosta-xyz"


def test_resolve_generic_resolved(store):
    rec = store.create_notification(producer="p", title="t")
    out = store.resolve_notification(rec["notification_id"], action_kind="resolved", by="op")
    assert out["state"] == STATE_RESOLVED
    assert out["resolution"]["action_kind"] == "resolved"


def test_resolve_missing_raises(store):
    with pytest.raises(NotificationNotFound):
        store.resolve_notification("nope", action_kind="ack", by="op")


def test_resolve_identical_replays_and_different_intent_conflicts(store):
    rec = store.create_notification(producer="p", title="t")
    store.resolve_notification(rec["notification_id"], action_kind="ack", by="op")
    replay = store.resolve_notification(rec["notification_id"], action_kind="ack", by="other")
    assert replay["_resolution_replayed"] is True
    with pytest.raises(NotificationResolutionConflict):
        store.resolve_notification(rec["notification_id"], action_kind="resolved", by="op")


def test_resolution_provenance_is_additive_and_replay_immutable(store):
    created = _create_store_question(store, question_id="q-provenance")
    provenance = {
        "actor_class": "verified_agent_relay",
        "actor_stream_id": "hosta:relay-seat",
        "actor_client": "agent-orch",
        "actor_verified": True,
        "claimed_by": "operator",
    }
    record = store.resolve_notification(
        created["notification_id"],
        action_kind="yes_no",
        by="agent_relay:hosta:relay-seat",
        choice=True,
        selections=["proceed"],
        action_id="a0",
        value={"schema_version": 1, "question_id": "q-provenance", "answer": "proceed"},
        actor_provenance=provenance,
    )

    assert {key: record["resolution"][key] for key in provenance} == provenance
    answer = store.get_agent_question("q-provenance")["answer"]
    assert {key: answer[key] for key in provenance} == provenance

    replay = store.resolve_notification(
        created["notification_id"],
        action_kind="yes_no",
        by="client:pentacle",
        choice=True,
        selections=["proceed"],
        action_id="a0",
        value={"schema_version": 1, "question_id": "q-provenance", "answer": "proceed"},
        actor_provenance={
            "actor_class": "unverified_direct_client",
            "actor_stream_id": None,
            "actor_client": "pentacle",
            "actor_verified": False,
        },
    )
    assert replay["_resolution_replayed"] is True
    assert replay["resolution"]["actor_class"] == "verified_agent_relay"
    assert replay["resolution"]["actor_stream_id"] == "hosta:relay-seat"
    assert replay["resolution"]["by"] == "agent_relay:hosta:relay-seat"


def test_legacy_resolution_deserializes_as_unknown_not_verified(store):
    rec = store.create_notification(producer="p", title="legacy")
    store.resolve_notification(rec["notification_id"], action_kind="ack", by="operator")
    store._conn.execute(
        "UPDATE notifications SET resolution = ? WHERE notification_id = ?",
        (json.dumps({"by": "operator", "at": "2026-08-12T00:00:00Z", "action_kind": "ack"}), rec["notification_id"]),
    )
    store._conn.commit()

    resolution = store.get_notification(rec["notification_id"])["resolution"]
    assert resolution["by"] == "operator"
    assert resolution["actor_class"] == "legacy_unknown"
    assert resolution["actor_stream_id"] is None
    assert resolution["actor_verified"] is False
    assert resolution["actor_class"] != "direct_operator"


def test_question_answer_terminalizes_notification_and_question_atomically(store):
    created = _create_store_question(store, question_id="q-atomic")
    record = store.resolve_notification(
        created["notification_id"],
        action_kind="yes_no",
        by="operator",
        choice=True,
        selections=["proceed"],
        action_id="a0",
        value={"schema_version": 1, "question_id": "q-atomic", "answer": "proceed"},
    )
    question = store.get_agent_question("q-atomic")
    assert record["state"] == STATE_ANSWERED
    assert record["resolution"]["delivery_status"] == "pending"
    assert question["state"] == "answered"
    assert question["answer"]["selections"] == ["proceed"]


@pytest.mark.parametrize("failing_table", ["notifications", "agent_questions"])
def test_question_resolution_rolls_back_both_rows_when_either_update_fails(
    store, failing_table
):
    created = _create_store_question(store, question_id=f"q-rollback-{failing_table}")
    store._conn.execute(
        f"""
        CREATE TRIGGER fail_{failing_table}_update
        BEFORE UPDATE ON {failing_table}
        BEGIN
            SELECT RAISE(ABORT, 'injected update failure');
        END
        """
    )
    store._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.resolve_notification(
            created["notification_id"],
            action_kind="yes_no",
            by="operator",
            choice=True,
            selections=["proceed"],
            action_id="a0",
            value={"schema_version": 1, "answer": "proceed"},
        )
    store._conn.commit()
    assert store.get_notification(created["notification_id"])["state"] == STATE_OPEN
    assert store.get_agent_question(f"q-rollback-{failing_table}")["state"] == "open"


def test_external_resolution_claim_replay_conflict_and_restart_indeterminate(tmp_path):
    path = tmp_path / "external.db"
    store = NotificationStore(path)
    record = store.create_notification(
        producer="p",
        title="spawn",
        actions=[
            {
                "kind": "spawn_worker",
                "action_id": "spawn",
                "provider": "codex",
                "host": "hostc",
                "prompt": "go",
            }
        ],
    )
    intent = {
        "notification_id": record["notification_id"],
        "action_id": "spawn",
        "action_kind": "spawn_worker",
        "choice": None,
        "value": None,
        "selections": [],
        "text": None,
        "custom_text": None,
        "note": None,
        "effective_spawn_spec": {
            "provider": "codex",
            "host": "hostc",
            "prompt": "go",
            "spec_id": None,
            "role": None,
            "phase": None,
            "model": None,
        },
    }
    assert store.claim_external_resolution(
        record["notification_id"], canonical_intent=intent, by="op", action_kind="spawn_worker"
    )["state"] == "claimed"
    assert store.claim_external_resolution(
        record["notification_id"], canonical_intent=intent, by="op", action_kind="spawn_worker"
    )["state"] == "in_progress"
    changed = dict(intent)
    changed["effective_spawn_spec"] = {**intent["effective_spawn_spec"], "prompt": "different"}
    with pytest.raises(NotificationResolutionConflict):
        store.claim_external_resolution(
            record["notification_id"], canonical_intent=changed, by="op", action_kind="spawn_worker"
        )
    store.close()

    restarted = NotificationStore(path)
    assert restarted.recover_claimed_external_resolutions() == 1
    replay = restarted.claim_external_resolution(
        record["notification_id"], canonical_intent=intent, by="op", action_kind="spawn_worker"
    )
    assert replay["state"] == "indeterminate"


def test_resolve_yes_no_without_bool_choice_raises(store):
    rec = store.create_notification(producer="p", title="t")
    with pytest.raises(InvalidNotification):
        store.resolve_notification(rec["notification_id"], action_kind="yes_no", by="op")
    with pytest.raises(InvalidNotification):
        store.resolve_notification(
            rec["notification_id"], action_kind="yes_no", by="op", choice="yes"
        )


def test_resolve_bad_action_kind_raises(store):
    rec = store.create_notification(producer="p", title="t")
    with pytest.raises(InvalidNotification):
        store.resolve_notification(rec["notification_id"], action_kind="bogus", by="op")


# ----------------------------------------------------------------------
# run_command claim / finish
# ----------------------------------------------------------------------


def _run_command_result(command_id: str = "demo_ok", exit_code: int = 0) -> dict:
    return {
        "command_id": command_id,
        "exit_code": exit_code,
        "stdout_tail": "stdout tail",
        "stderr_tail": "stderr tail",
        "timed_out": False,
        "ran_at": "2026-05-20T12:01:00+00:00",
    }


def test_claim_run_command_open_to_running(store):
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    rec = store.create_notification(
        producer="p",
        title="t",
        actions=[{"kind": "run_command", "command_id": "demo_ok"}],
    )

    out = store.claim_run_command(rec["notification_id"], by="op", now=_iso(now))

    assert out["state"] == STATE_RUNNING
    assert out["resolution"] == {
        "claimed_by": "op",
        "claimed_at": _iso(now),
        "action_kind": "run_command",
    }
    assert out["resolved_at"] is None
    assert out["updated_at"] == _iso(now)


def test_claim_run_command_missing_raises(store):
    with pytest.raises(NotificationNotFound):
        store.claim_run_command("nope", by="op")


def test_claim_run_command_when_already_running_or_terminal_raises(store):
    running = store.create_notification(
        producer="p",
        title="running",
        actions=[{"kind": "run_command", "command_id": "demo_ok"}],
    )
    store.claim_run_command(running["notification_id"], by="op")
    with pytest.raises(NotificationTerminalState) as running_exc:
        store.claim_run_command(running["notification_id"], by="op2")
    assert running_exc.value.args[0] == STATE_RUNNING

    terminal = store.create_notification(
        producer="p",
        title="terminal",
        actions=[{"kind": "ack"}],
    )
    store.resolve_notification(terminal["notification_id"], action_kind="ack", by="op")
    with pytest.raises(NotificationTerminalState) as terminal_exc:
        store.claim_run_command(terminal["notification_id"], by="op2")
    assert terminal_exc.value.args[0] == STATE_ACKED


def test_claim_run_command_race_only_one_claim_wins(store):
    rec = store.create_notification(
        producer="p",
        title="t",
        actions=[{"kind": "run_command", "command_id": "demo_ok"}],
    )

    first = store.claim_run_command(rec["notification_id"], by="op")

    assert first["state"] == STATE_RUNNING
    with pytest.raises(NotificationTerminalState):
        store.claim_run_command(rec["notification_id"], by="op2")


@pytest.mark.parametrize(
    ("terminal_state", "result"),
    [
        (STATE_DONE, _run_command_result()),
        (
            STATE_FAILED,
            {
                **_run_command_result(command_id="demo_fail", exit_code=1),
                "spawned_stream_id": "hosta:codex-hosta-failure",
            },
        ),
    ],
)
def test_finish_run_command_persists_done_and_failed(store, terminal_state, result):
    now = datetime(2026, 5, 20, 12, 2, 0, tzinfo=timezone.utc)
    rec = store.create_notification(
        producer="p",
        title="t",
        actions=[{"kind": "run_command", "command_id": result["command_id"]}],
    )
    store.claim_run_command(rec["notification_id"], by="runner")

    out = store.finish_run_command(
        rec["notification_id"],
        terminal_state=terminal_state,
        result=result,
        by="runner",
        now=_iso(now),
    )

    assert out["state"] == terminal_state
    assert out["resolved_at"] == _iso(now)
    assert out["updated_at"] == _iso(now)
    assert out["resolution"]["claimed_by"] == "runner"
    assert out["resolution"]["by"] == "runner"
    assert out["resolution"]["at"] == _iso(now)
    assert out["resolution"]["action_kind"] == "run_command"
    assert out["resolution"]["result"] == result


def test_finish_run_command_when_not_running_raises(store):
    open_rec = store.create_notification(producer="p", title="open")
    with pytest.raises(NotificationTerminalState) as open_exc:
        store.finish_run_command(
            open_rec["notification_id"],
            terminal_state=STATE_DONE,
            result=_run_command_result(),
            by="runner",
        )
    assert open_exc.value.args[0] == STATE_OPEN

    acked = store.create_notification(producer="p", title="acked")
    store.resolve_notification(acked["notification_id"], action_kind="ack", by="op")
    with pytest.raises(NotificationTerminalState) as terminal_exc:
        store.finish_run_command(
            acked["notification_id"],
            terminal_state=STATE_DONE,
            result=_run_command_result(),
            by="runner",
        )
    assert terminal_exc.value.args[0] == STATE_ACKED


def test_finish_run_command_missing_raises(store):
    with pytest.raises(NotificationNotFound):
        store.finish_run_command(
            "nope",
            terminal_state=STATE_DONE,
            result=_run_command_result(),
            by="runner",
        )


def test_finish_run_command_bad_terminal_state_raises(store):
    rec = store.create_notification(
        producer="p",
        title="t",
        actions=[{"kind": "run_command", "command_id": "demo_ok"}],
    )
    store.claim_run_command(rec["notification_id"], by="runner")

    with pytest.raises(ValueError):
        store.finish_run_command(
            rec["notification_id"],
            terminal_state=STATE_ACKED,
            result=_run_command_result(),
            by="runner",
        )


def test_list_accepts_run_command_states_and_filters(store):
    running = store.create_notification(
        producer="p",
        title="running",
        actions=[{"kind": "run_command", "command_id": "demo_ok"}],
    )
    done = store.create_notification(
        producer="p",
        title="done",
        actions=[{"kind": "run_command", "command_id": "demo_ok"}],
    )
    failed = store.create_notification(
        producer="p",
        title="failed",
        actions=[{"kind": "run_command", "command_id": "demo_fail"}],
    )
    acked = store.create_notification(producer="p", title="acked")

    store.claim_run_command(running["notification_id"], by="runner")
    store.claim_run_command(done["notification_id"], by="runner")
    store.finish_run_command(
        done["notification_id"],
        terminal_state=STATE_DONE,
        result=_run_command_result(),
        by="runner",
    )
    store.claim_run_command(failed["notification_id"], by="runner")
    store.finish_run_command(
        failed["notification_id"],
        terminal_state=STATE_FAILED,
        result=_run_command_result(command_id="demo_fail", exit_code=1),
        by="runner",
    )
    store.resolve_notification(acked["notification_id"], action_kind="ack", by="op")

    filtered = store.list_notifications(states=[STATE_RUNNING, STATE_DONE, STATE_FAILED])
    assert {r["notification_id"] for r in filtered} == {
        running["notification_id"],
        done["notification_id"],
        failed["notification_id"],
    }


def test_existing_states_still_filter_after_run_command_states_added(store):
    open_rec = store.create_notification(producer="p", title="open")
    acked = store.create_notification(producer="p", title="acked")
    store.resolve_notification(acked["notification_id"], action_kind="ack", by="op")

    assert [r["notification_id"] for r in store.list_notifications(states=[STATE_OPEN])] == [
        open_rec["notification_id"]
    ]
    assert [r["notification_id"] for r in store.list_notifications(states=[STATE_ACKED])] == [
        acked["notification_id"]
    ]


# ----------------------------------------------------------------------
# Expiry
# ----------------------------------------------------------------------


def test_expire_due_marks_only_due_open_rows(store):
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    # Due: ttl in the past relative to expire-now.
    due = store.create_notification(
        producer="p", title="due", ttl_seconds=60, now=_iso(now - timedelta(minutes=5))
    )
    # Not yet due.
    future = store.create_notification(
        producer="p", title="future", ttl_seconds=3600, now=_iso(now)
    )
    # No ttl -> never expires.
    no_ttl = store.create_notification(producer="p", title="no-ttl", now=_iso(now))

    expired = store.expire_due(now=_iso(now))
    assert expired == [due["notification_id"]]

    assert store.get_notification(due["notification_id"])["state"] == STATE_EXPIRED
    assert store.get_notification(due["notification_id"])["resolution"] == {
        "by": "system",
        "at": _iso(now),
        "action_kind": "expired",
        "actor_class": "system",
        "actor_stream_id": None,
        "actor_client": None,
        "actor_verified": False,
    }
    assert store.get_notification(future["notification_id"])["state"] == STATE_OPEN
    assert store.get_notification(no_ttl["notification_id"])["state"] == STATE_OPEN


def test_expire_due_ignores_already_resolved(store):
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    rec = store.create_notification(
        producer="p", title="t", ttl_seconds=60, now=_iso(now - timedelta(minutes=5))
    )
    store.resolve_notification(rec["notification_id"], action_kind="ack", by="op")
    assert store.expire_due(now=_iso(now)) == []
    assert store.get_notification(rec["notification_id"])["state"] == STATE_ACKED


# ----------------------------------------------------------------------
# Retention
# ----------------------------------------------------------------------


def test_prune_never_deletes_open(store):
    s = NotificationStore(":memory:", resolved_keep=0, resolved_max_age_days=0)
    try:
        for i in range(10):
            s.create_notification(producer="p", title=f"open-{i}")
        before = s.count_open()
        deleted = s.prune_resolved()
        assert deleted == 0
        assert s.count_open() == before == 10
    finally:
        s.close()


def test_prune_keep_zero_preserves_running_and_prunes_terminal_run_command_rows():
    s = NotificationStore(":memory:", resolved_keep=0, resolved_max_age_days=3650)
    try:
        base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        running = s.create_notification(
            producer="p",
            title="running",
            actions=[{"kind": "run_command", "command_id": "demo_ok"}],
            now=_iso(base),
        )
        s.claim_run_command(running["notification_id"], by="runner", now=_iso(base))

        done = s.create_notification(
            producer="p",
            title="done",
            actions=[{"kind": "run_command", "command_id": "demo_ok"}],
            now=_iso(base + timedelta(minutes=1)),
        )
        s.claim_run_command(done["notification_id"], by="runner")
        s.finish_run_command(
            done["notification_id"],
            terminal_state=STATE_DONE,
            result=_run_command_result(),
            by="runner",
            now=_iso(base + timedelta(minutes=2)),
        )

        failed = s.create_notification(
            producer="p",
            title="failed",
            actions=[{"kind": "run_command", "command_id": "demo_fail"}],
            now=_iso(base + timedelta(minutes=3)),
        )
        s.claim_run_command(failed["notification_id"], by="runner")
        s.finish_run_command(
            failed["notification_id"],
            terminal_state=STATE_FAILED,
            result=_run_command_result(command_id="demo_fail", exit_code=1),
            by="runner",
            now=_iso(base + timedelta(minutes=4)),
        )

        deleted = s.prune_resolved(now=_iso(base + timedelta(hours=1)))

        assert deleted == 2
        assert s.get_notification(running["notification_id"])["state"] == STATE_RUNNING
        assert s.get_notification(done["notification_id"]) is None
        assert s.get_notification(failed["notification_id"]) is None
    finally:
        s.close()


def test_prune_bounds_resolved_to_keep_n():
    s = NotificationStore(":memory:", resolved_keep=3, resolved_max_age_days=3650)
    try:
        base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        ids = []
        for i in range(8):
            ts = _iso(base + timedelta(minutes=i))
            rec = s.create_notification(producer="p", title=f"n-{i}", now=ts)
            s.resolve_notification(rec["notification_id"], action_kind="ack", by="op", now=ts)
            ids.append(rec["notification_id"])
        deleted = s.prune_resolved(now=_iso(base + timedelta(hours=1)))
        assert deleted == 5
        # The 3 most-recently-resolved survive.
        remaining = {r["notification_id"] for r in s.list_notifications()}
        assert remaining == set(ids[-3:])
    finally:
        s.close()


def test_prune_age_based_via_injected_now():
    s = NotificationStore(":memory:", resolved_keep=1000, resolved_max_age_days=30)
    try:
        base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        old = s.create_notification(producer="p", title="old", now=_iso(base - timedelta(days=60)))
        s.resolve_notification(
            old["notification_id"], action_kind="ack", by="op", now=_iso(base - timedelta(days=60))
        )
        recent = s.create_notification(producer="p", title="recent", now=_iso(base - timedelta(days=5)))
        s.resolve_notification(
            recent["notification_id"], action_kind="ack", by="op", now=_iso(base - timedelta(days=5))
        )
        deleted = s.prune_resolved(now=_iso(base))
        assert deleted == 1
        remaining = {r["notification_id"] for r in s.list_notifications()}
        assert remaining == {recent["notification_id"]}
    finally:
        s.close()


def test_prune_is_idempotent():
    s = NotificationStore(":memory:", resolved_keep=2, resolved_max_age_days=30)
    try:
        base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(6):
            ts = _iso(base + timedelta(minutes=i))
            rec = s.create_notification(producer="p", title=f"n-{i}", now=ts)
            s.resolve_notification(rec["notification_id"], action_kind="ack", by="op", now=ts)
        first = s.prune_resolved(now=_iso(base + timedelta(hours=1)))
        assert first == 4
        second = s.prune_resolved(now=_iso(base + timedelta(hours=1)))
        assert second == 0
    finally:
        s.close()


# ----------------------------------------------------------------------
# Restart durability
# ----------------------------------------------------------------------


def test_restart_durability(tmp_path):
    path = tmp_path / "notifications.db"
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)

    s1 = NotificationStore(path)
    try:
        open_a = s1.create_notification(producer="p", title="open-a", now=_iso(base))
        open_b = s1.create_notification(
            producer="p", title="open-b", severity="critical", now=_iso(base + timedelta(minutes=1))
        )
        acked = s1.create_notification(producer="p", title="acked", now=_iso(base + timedelta(minutes=2)))
        s1.resolve_notification(
            acked["notification_id"], action_kind="ack", by="op", now=_iso(base + timedelta(minutes=3))
        )
        answered = s1.create_notification(
            producer="p", title="answered", now=_iso(base + timedelta(minutes=4))
        )
        s1.resolve_notification(
            answered["notification_id"],
            action_kind="yes_no",
            by="op",
            choice=False,
            now=_iso(base + timedelta(minutes=5)),
        )
        expected = {
            open_a["notification_id"]: STATE_OPEN,
            open_b["notification_id"]: STATE_OPEN,
            acked["notification_id"]: STATE_ACKED,
            answered["notification_id"]: STATE_ANSWERED,
        }
    finally:
        s1.close()

    s2 = NotificationStore(path)
    try:
        records = {r["notification_id"]: r for r in s2.list_notifications()}
        assert set(records) == set(expected)
        for nid, state in expected.items():
            assert records[nid]["state"] == state
        assert s2.count_open() == 2
        # Decoded JSON columns survive the round-trip.
        assert records[answered["notification_id"]]["resolution"]["choice"] is False
        assert records[open_b["notification_id"]]["severity"] == "critical"
    finally:
        s2.close()


# ----------------------------------------------------------------------
# Open-time health
# ----------------------------------------------------------------------


def test_open_store_enables_wal_on_disk(tmp_path):
    path = tmp_path / "wal.db"
    s = NotificationStore(path)
    try:
        mode = s._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        s.close()


def test_retention_env_overrides(monkeypatch):
    monkeypatch.setenv("PENTACLE_NOTIFICATIONS_RESOLVED_KEEP", "5")
    monkeypatch.setenv("PENTACLE_NOTIFICATIONS_RESOLVED_MAX_AGE_DAYS", "7")
    s = NotificationStore(":memory:")
    try:
        assert s.resolved_keep == 5
        assert s.resolved_max_age_days == 7
    finally:
        s.close()
    # Explicit arg wins over env.
    s2 = NotificationStore(":memory:", resolved_keep=99)
    try:
        assert s2.resolved_keep == 99
        assert s2.resolved_max_age_days == 7
    finally:
        s2.close()


# ----------------------------------------------------------------------
# QA fix round: validation, expiry boundary, prune robustness, lifecycle,
# dedup refresh contract, threaded dedup safety.
# ----------------------------------------------------------------------


def test_list_rejects_unknown_state(store):
    # A typo'd state filter must surface as ValueError, not a silent [].
    with pytest.raises(ValueError):
        store.list_notifications(states=["bogus"])


def test_list_rejects_negative_limit(store):
    # SQLite treats a negative LIMIT as unlimited; reject it as a footgun.
    with pytest.raises(ValueError):
        store.list_notifications(limit=-1)


def test_list_limit_zero_returns_empty(store):
    store.create_notification(producer="p", title="a")
    # limit=0 follows natural SQLite LIMIT 0 semantics: empty result.
    assert store.list_notifications(limit=0) == []


def test_create_rejects_bool_ttl(store):
    # True/False are ints in Python; they must not coerce to ttl=1/0.
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="p", title="t", ttl_seconds=True)


def test_create_rejects_negative_ttl(store):
    with pytest.raises(InvalidNotification):
        store.create_notification(producer="p", title="t", ttl_seconds=-5)


def test_create_zero_ttl_stores_null_no_expiry(store):
    # 0 means "no expiry": stored as NULL (None), not a coerced sentinel.
    rec = store.create_notification(producer="p", title="t", ttl_seconds=0)
    assert rec["ttl_seconds"] is None
    assert rec["expires_at"] is None


def test_timestamps_use_trailing_z(store):
    # House shape: timestamps end in Z (matches schedule_store).
    rec = store.create_notification(producer="p", title="t")
    assert rec["created_at"].endswith("Z")
    assert rec["updated_at"].endswith("Z")


def test_expire_due_empty_is_idempotent(store):
    # No due rows -> [] and a second call is still [].
    store.create_notification(producer="p", title="no-ttl")
    assert store.expire_due() == []
    assert store.expire_due() == []


def test_expire_due_boundary_expires_at_equals_now(store):
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    # ttl=60 created 60s before now -> expires_at == now exactly. The <=
    # boundary must treat this as expired.
    rec = store.create_notification(
        producer="p", title="boundary", ttl_seconds=60, now=_iso(now - timedelta(seconds=60))
    )
    assert notifications_store._parse_iso(rec["expires_at"]) == now
    expired = store.expire_due(now=_iso(now))
    assert expired == [rec["notification_id"]]
    assert store.get_notification(rec["notification_id"])["state"] == STATE_EXPIRED


def test_prune_tolerates_non_open_row_with_null_resolved_at(store):
    # Seed a terminal row with a NULL resolved_at directly (a malformed/legacy
    # row) plus a real open row; prune must not crash and must not touch open.
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    open_rec = store.create_notification(producer="p", title="open", now=_iso(base))
    with store._lock:
        store._conn.execute(
            "INSERT INTO notifications ("
            "notification_id, created_at, updated_at, producer, severity, title, "
            "body, dedup_key, state, actions, resolution, ttl_seconds, expires_at, "
            "resolved_at) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, '[]', NULL, NULL, NULL, NULL)",
            ("orphan", _iso(base), _iso(base), "p", "info", "orphan", STATE_RESOLVED),
        )
        store._conn.commit()
    deleted = store.prune_resolved(now=_iso(base + timedelta(hours=1)))
    # The orphan (NULL resolved_at) is sorted last and within keep-N here, so
    # it survives; the key assertion is no crash and open row untouched.
    assert isinstance(deleted, int)
    assert store.get_notification(open_rec["notification_id"])["state"] == STATE_OPEN


def test_operation_after_close_raises(store):
    store.close()
    with pytest.raises(RuntimeError):
        store.get_notification("anything")


def test_dedup_refresh_resets_actions_when_omitted(store):
    # Current contract: refreshing an open dedup row with actions omitted
    # resets actions to [] (the refresh always rewrites the actions column).
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    first = store.create_notification(
        producer="p",
        title="orig",
        dedup_key="cond-1",
        actions=[{"kind": "ack"}],
        now=_iso(base),
    )
    assert first["actions"] == [{"kind": "ack", "action_id": "a0"}]
    second = store.create_notification(
        producer="p", title="orig", dedup_key="cond-1", now=_iso(base + timedelta(minutes=1))
    )
    assert second["notification_id"] == first["notification_id"]
    assert second["actions"] == []


def test_dedup_refresh_renormalizes_actions_by_refreshed_order(store):
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    first = store.create_notification(
        producer="p",
        title="orig",
        dedup_key="cond-1",
        actions=[{"kind": "ack"}],
        now=_iso(base),
    )
    second = store.create_notification(
        producer="p",
        title="refreshed",
        dedup_key="cond-1",
        actions=[{"kind": "yes_no"}, {"kind": "ack"}],
        now=_iso(base + timedelta(minutes=1)),
    )

    assert second["notification_id"] == first["notification_id"]
    assert second["actions"] == [
        {"kind": "yes_no", "action_id": "a0"},
        {"kind": "ack", "action_id": "a1"},
    ]


def test_dedup_refresh_updates_presentation_fields(store):
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    first = store.create_notification(
        producer="p",
        title="orig",
        body="orig body",
        severity="info",
        dedup_key="cond-1",
        now=_iso(base),
    )
    second = store.create_notification(
        producer="p",
        title="new",
        body="new body",
        severity="critical",
        dedup_key="cond-1",
        now=_iso(base + timedelta(minutes=1)),
    )
    assert second["notification_id"] == first["notification_id"]
    assert second["title"] == "new"
    assert second["body"] == "new body"
    assert second["severity"] == "critical"


def test_dedup_refresh_tracks_firing_history_and_daily_resurface(store):
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    first = store.create_notification(
        producer="p",
        title="first",
        dedup_key="cond-1",
        now=_iso(base),
    )
    second = store.create_notification(
        producer="p",
        title="second",
        dedup_key="cond-1",
        now=_iso(base + timedelta(hours=1)),
    )
    third = store.create_notification(
        producer="p",
        title="third",
        dedup_key="cond-1",
        now=_iso(base + timedelta(days=1, minutes=1)),
    )

    assert first["notification_id"] == second["notification_id"] == third["notification_id"]
    assert second["should_resurface"] is False
    assert third["should_resurface"] is True
    assert third["firing_count"] == 3
    assert third["first_fired_at"] == _iso(base)
    assert third["last_fired_at"] == _iso(base + timedelta(days=1, minutes=1))
    assert third["firing_history"] == [
        _iso(base),
        _iso(base + timedelta(hours=1)),
        _iso(base + timedelta(days=1, minutes=1)),
    ]


def test_dedup_refresh_caps_firing_history(store, monkeypatch):
    monkeypatch.setattr("_shared.notifications_store.DEFAULT_FIRING_HISTORY_LIMIT", 3)
    base = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    for idx in range(5):
        rec = store.create_notification(
            producer="p",
            title=f"n-{idx}",
            dedup_key="cond-1",
            now=_iso(base + timedelta(minutes=idx)),
        )

    assert rec["firing_count"] == 5
    assert rec["firing_history"] == [
        _iso(base + timedelta(minutes=2)),
        _iso(base + timedelta(minutes=3)),
        _iso(base + timedelta(minutes=4)),
    ]


def test_resolve_open_dedup_marks_matching_open_row_resolved(store):
    rec = store.create_notification(producer="p", title="t", dedup_key="cond-1")
    out = store.resolve_open_dedup(producer="p", dedup_key="cond-1", by="system")

    assert out["notification_id"] == rec["notification_id"]
    assert out["state"] == STATE_RESOLVED
    assert out["resolution"]["action_kind"] == "resolved"
    assert store.resolve_open_dedup(producer="p", dedup_key="cond-1", by="system") is None


def test_resolve_open_dedup_expected_id_does_not_follow_rollover(store):
    first = store.create_notification(producer="p", title="first", dedup_key="cond-roll")
    store.resolve_notification(first["notification_id"], action_kind="resolved", by="other")
    replacement = store.create_notification(producer="p", title="replacement", dedup_key="cond-roll")

    assert store.resolve_open_dedup(
        producer="p",
        dedup_key="cond-roll",
        by="stale-authority",
        expected_notification_id=first["notification_id"],
    ) is None
    assert store.get_notification(replacement["notification_id"])["state"] == STATE_OPEN


def test_threaded_dedup_yields_single_open_row(tmp_path):
    # SELECT-then-upsert must stay inside the lock: ~8 threads racing on the
    # same (producer, dedup_key) must leave exactly one open row and never let
    # an IntegrityError escape (the partial unique index would otherwise fire).
    path = tmp_path / "threaded.db"
    s = NotificationStore(path)
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            s.create_notification(
                producer="racer",
                title=f"t-{i}",
                dedup_key="same-cond",
            )
        except Exception as exc:  # pragma: no cover - only on regression
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        open_rows = s.list_notifications(states=[STATE_OPEN])
        assert len(open_rows) == 1
    finally:
        s.close()
