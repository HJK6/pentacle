"""Regression coverage for the mobile/desktop agent-question answer path."""

from __future__ import annotations

import asyncio, hashlib, json
from contextlib import closing
from pathlib import Path

from _shared.notifications_store import open_store
from notify import Notify  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _handoff_approval_material(envelope: dict, actions: object) -> str:
    context = envelope["context"]
    visible = {key: envelope.get(key) for key in ("question_id", "title", "body", "context", "response_mode", "options", "allow_custom", "ttl_seconds", "default_action", "dedup_key", "creator")}
    visible["context"], visible["actions"] = context, actions
    return hashlib.sha256(json.dumps(visible, separators=(",", ":"), sort_keys=True).encode()).hexdigest()

def _assert_handoff_approval_claimable(notifications_db_path: Path, question: dict) -> None:
    envelope, answer = question["envelope"], question["answer"]
    context, creator = envelope["context"], envelope["creator"]
    provenance = answer["operator_auth"]
    with closing(open_store(str(notifications_db_path))) as store:
        notification = store.get_notification(question["notification_id"])
    actions = notification["actions"]
    assert (
        question["state"] == "answered" and question["producer_stream_id"] == context["source_stream_id"]
        and creator["stream_id"] == context["source_stream_id"] and creator["auth"] == "stream_token"
        and creator["server_stamped"] is True and envelope["response_mode"] == "single_choice"
        and envelope["allow_custom"] is False and envelope["ttl_seconds"] is None
        and envelope["default_action"] is None and envelope["dedup_key"] is None
        and set(envelope) <= {"schema_version", "question_id", "title", "body", "dedup_key", "producer_stream_id", "producer_provider", "spec_id", "response_mode", "options", "allow_custom", "ttl_seconds", "default_action", "context", "creator", "approval_material_sha256", "created_at", "updated_at", "answer", "answered_at"}
        and envelope["options"] == [{"label": "Approve", "value": "approve"}, {"label": "Deny", "value": "deny"}]
        and answer["selections"] == ["approve"] and not answer.get("custom_text")
        and provenance["transport"] == "v2" and provenance["operator_trusted"] is True
        and provenance["server_stamped"] is True and isinstance(provenance["credential_id"], str)
        and isinstance(provenance["client_kind"], str) and set(context) == {"schema", "source_stream_id", "source", "requested", "reason"}
        and context["schema"] == "HandoffModelChangeApprovalV1" and isinstance(context["source"], dict)
        and isinstance(context["requested"], dict) and isinstance(context["reason"], str) and bool(context["reason"].strip())
    ), "v1 handoff approval claim predicate no longer holds"
    material = _handoff_approval_material(envelope, actions)
    assert envelope.get("approval_material_sha256") == material
    assert provenance.get("question_id") == question.get("question_id") and provenance.get("approval_material_sha256") == material

class _AnswerTellComms:
    def __init__(self) -> None:
        self.tells: list[dict] = []
        self.store = _AnswerTellDeliveryStore()

    async def tell(self, message: dict) -> dict:
        self.tells.append(message)
        return {"duplicate": False, "delivery_status": "delivered"}


class _RetryableAnswerTellComms(_AnswerTellComms):
    def __init__(self) -> None:
        super().__init__()
        self.delivery_status = "pasted_unsubmitted"

    async def tell(self, message: dict) -> dict:
        self.tells.append(message)
        return {"duplicate": False, "delivery_status": self.delivery_status}


class _AnswerTellDeliveryStore:
    """Small v2-local tell-ledger double for Notify's startup sweep."""

    def __init__(self) -> None:
        self.answer_tell_ids: list[str] = []
        self.v1_foreign_tell_ids: list[str] = []

    async def list_delivered_tell_ids(self, *, prefix: str, limit: int) -> list[str]:
        return [tell_id for tell_id in self.answer_tell_ids if tell_id.startswith(prefix)][:limit]


async def _seed_live_shaped_question(
    notify: Notify,
    *,
    allow_custom: bool = False,
    question_id: str = "q-live-shaped-answer",
    producer_stream_id: str = "hosta:v2-test",
) -> dict:
    envelope = {
        "schema_version": 1,
        "question_id": question_id,
        "title": "Approve the live-shaped handoff",
        "body": "Approve?",
        "context": None,
        "created_at": "2026-08-06T11:00:06.427590Z",
        "updated_at": "2026-08-06T11:00:06.444977Z",
        "dedup_key": f"agent-question:hosta:v2-test:{question_id}",
        "producer_stream_id": producer_stream_id,
        "producer_provider": None,
        "spec_id": "spec_example_2026_01",
        "response_mode": "single_choice",
        "options": [
            {"label": "Approve", "value": "approve"},
            {"label": "Decline", "value": "decline"},
        ],
        "ttl_seconds": None,
    }
    if allow_custom:
        envelope["allow_custom"] = True
    actions = [
        {
            "kind": "yes_no",
            "action_id": "a0",
            "label": "Approve",
            "choice": True,
            "value": {
                "schema_version": 1,
                "question_id": question_id,
                "answer": "approve",
            },
        },
        {
            "kind": "yes_no",
            "action_id": "a1",
            "label": "Decline",
            "choice": False,
            "value": {
                "schema_version": 1,
                "question_id": question_id,
                "answer": "decline",
            },
        },
    ]
    return await notify._db.call(
        "create_agent_question", envelope=envelope, actions=actions
    )


async def _seed_answered_v1_handoff_approval(
    notify: Notify, *, question_id: str
) -> tuple[dict, dict]:
    """Seed the v1-exclusive approval row that must never enter v2 delivery."""
    source = {"provider": "codex", "model": "gpt-5.6-luna", "effort": "low"}
    requested = {"provider": "codex", "model": "gpt-5.6-terra", "effort": "medium"}
    envelope = {
        "schema_version": 1,
        "question_id": question_id,
        "title": "Approve v1 handoff",
        "body": "Change model?",
        "dedup_key": None,
        "producer_stream_id": "hosta:v1-handoff-source",
        "response_mode": "single_choice",
        "options": [
            {"label": "Approve", "value": "approve"},
            {"label": "Deny", "value": "deny"},
        ],
        "allow_custom": False,
        "ttl_seconds": None,
        "default_action": None,
        "context": {
            "schema": "HandoffModelChangeApprovalV1",
            "source_stream_id": "hosta:v1-handoff-source",
            "source": source,
            "requested": requested,
            "reason": "capacity",
        },
        "creator": {
            "stream_id": "hosta:v1-handoff-source",
            "auth": "stream_token",
            "server_stamped": True,
            "at": "2026-08-13T00:00:00Z",
        },
    }
    actions = [
        {
            "kind": "yes_no",
            "action_id": "a0",
            "label": "Approve",
            "choice": True,
            "value": {"schema_version": 1, "question_id": question_id, "answer": "approve"},
        },
        {
            "kind": "yes_no",
            "action_id": "a1",
            "label": "Deny",
            "choice": False,
            "value": {"schema_version": 1, "question_id": question_id, "answer": "deny"},
        },
    ]
    envelope["approval_material_sha256"] = _handoff_approval_material(envelope, actions)
    question = await notify._db.call("create_agent_question", envelope=envelope, actions=actions)
    record = await notify._db.call(
        "resolve_notification",
        question["notification_id"],
        action_kind="yes_no",
        by="operator",
        choice=True,
        selections=["approve"],
    )
    await notify._db.call(
        "stamp_agent_question_answer_operator_auth",
        question_id,
        {
            "transport": "v2",
            "credential_id": "test-credential",
            "client_kind": "pentacle",
            "operator_trusted": True,
            "server_stamped": True,
            "question_id": question_id,
            "approval_material_sha256": envelope["approval_material_sha256"],
        },
    )
    answered = await notify._db.call("get_agent_question", question_id)
    assert answered is not None and answered["state"] == "answered"
    return answered, record


async def _seed_answered_external_question(
    notify: Notify, *, question_id: str, dedup_key: str
) -> tuple[dict, dict]:
    """Seed a shared-store row that v2 did not create or answer."""
    envelope = {
        "schema_version": 1,
        "question_id": question_id,
        "title": "External answered prompt",
        "body": "This row is owned by another daemon transaction.",
        "dedup_key": dedup_key,
        "producer_stream_id": "hosta:v1-external",
        "response_mode": "single_choice",
        "options": [
            {"label": "Approve", "value": "approve"},
            {"label": "Deny", "value": "deny"},
        ],
        "allow_custom": False,
        "ttl_seconds": None,
    }
    actions = [
        {
            "kind": "yes_no",
            "action_id": "a0",
            "label": "Approve",
            "choice": True,
            "value": {"schema_version": 1, "question_id": question_id, "answer": "approve"},
        },
        {
            "kind": "yes_no",
            "action_id": "a1",
            "label": "Deny",
            "choice": False,
            "value": {"schema_version": 1, "question_id": question_id, "answer": "deny"},
        },
    ]
    question = await notify._db.call("create_agent_question", envelope=envelope, actions=actions)
    record = await notify._db.call(
        "resolve_notification",
        question["notification_id"],
        action_kind="yes_no",
        by="v1-operator",
        choice=True,
        selections=["approve"],
        action_id="a0",
        label="Approve",
        value={"schema_version": 1, "question_id": question_id, "answer": "approve"},
    )
    answered = await notify._db.call("get_agent_question", question_id)
    assert answered is not None and answered["state"] == "answered"
    return answered, record


async def _seed_cycle_3_foreign_answer_matrix(
    notify: Notify,
) -> tuple[dict[str, dict], list[dict]]:
    handoff, handoff_record = await _seed_answered_v1_handoff_approval(
        notify, question_id="q-v1-handoff-cycle3"
    )
    generic, generic_record = await _seed_answered_external_question(
        notify,
        question_id="q-v1-agent-question-cycle3",
        dedup_key="agent-question:hosta:v1:q-v1-agent-question-cycle3",
    )
    unknown, unknown_record = await _seed_answered_external_question(
        notify,
        question_id="q-unknown-kind-cycle3",
        dedup_key="future-question-kind:q-unknown-kind-cycle3",
    )
    v1_delivered, v1_delivered_record = await _seed_answered_external_question(
        notify,
        question_id="q-v1-delivered-cycle3",
        dedup_key="agent-question:hosta:v1:q-v1-delivered-cycle3",
    )
    return (
        {
            handoff["question_id"]: handoff,
            generic["question_id"]: generic,
            unknown["question_id"]: unknown,
            v1_delivered["question_id"]: v1_delivered,
        },
        [handoff_record, generic_record, unknown_record, v1_delivered_record],
    )


def test_exact_notification_lookup_recovers_old_answer_without_the_global_page(tmp_path: Path) -> None:
    async def go():
        notify = Notify(db_path=str(tmp_path / 'notifications.db'))
        await notify.start()
        try:
            question = await _seed_live_shaped_question(notify)
            nid = question['notification_id']
            answered = await notify.notification({
                'type': 'notification.resolve', 'request_id': 'answer',
                'notification_id': nid, 'action_kind': 'yes_no', 'selections': ['approve'],
                '_auth_context': {'operator_authenticated': True},
            })
            assert answered['type'] == 'notification.resolve.ok'
            for i in range(101):
                await notify._db.call('create_notification', producer='unrelated', title=str(i))
            page = await notify.notification({'type': 'notification.list', 'request_id': 'page'})
            assert nid not in [r['notification_id'] for r in page['notifications']]
            found = await notify.notification({'type': 'notification.list', 'request_id': 'lookup',
                                               'notification_ids': [nid, nid, 'missing']})
            assert found['type'] == 'notification.list.ok'
            assert [r['notification_id'] for r in found['notifications']] == [nid]
            assert found['notifications'][0]['question']['answer']['selections'] == ['approve']
            empty = await notify.notification({'type': 'notification.list', 'request_id': 'empty', 'notification_ids': []})
            assert empty['notifications'] == []
            for invalid in (None, 'not-an-array', [''], [42], [False], ['x'] * 101):
                reply = await notify.notification({'type': 'notification.list', 'request_id': 'invalid',
                                                   'notification_ids': invalid})
                assert reply['type'] == 'notification.error'
                assert reply['error'] == 'notification_invalid'
        finally:
            await notify.stop()
    _run(go())


def test_selection_answer_derives_yes_no_choice_after_await_timeout(tmp_path: Path) -> None:
    """The exact client payload must answer a live-shaped question.

    Desktop and mobile submit ``selections`` and the stored action kind, but do
    not submit ``choice``. The producer may also have just observed an
    ``--await-answer`` timeout; that timeout must not invalidate the card.
    """

    async def go() -> None:
        notify = Notify(db_path=str(tmp_path / "notifications.db"))
        await notify.start()
        try:
            question = await _seed_live_shaped_question(notify)
            notification_id = str(question["notification_id"])

            timed_out = await notify.notification(
                {
                    "type": "notification.await",
                    "request_id": "await-before-answer",
                    "notification_id": notification_id,
                    "timeout": 0.01,
                }
            )
            assert timed_out["type"] == "notification.await.timeout"
            assert timed_out["reason"] == "await_timeout"

            resolved = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "client-shaped-answer",
                    "notification_id": notification_id,
                    "action_kind": "yes_no",
                    "selections": ["approve"],
                    "_auth_context": {"operator_authenticated": True},
                }
            )

            assert resolved["type"] == "notification.resolve.ok"
            assert resolved["notification"]["resolution"]["action_kind"] == "yes_no"
            assert resolved["notification"]["resolution"]["choice"] is True
            assert resolved["notification"]["resolution"]["selections"] == ["approve"]

            answered = await notify._db.call(
                "get_agent_question", "q-live-shaped-answer"
            )
            assert answered["state"] == "answered"
            assert answered["answered_at"] is not None
            assert answered["answer"]["selections"] == ["approve"]
            assert answered["answer"]["choice"] is True
        finally:
            await notify.stop()

    _run(go())


def test_successful_answer_tell_consumes_the_answered_question(tmp_path: Path) -> None:
    async def scenario() -> None:
        comms = _AnswerTellComms()
        notify = Notify(str(tmp_path / "notifications.db"), comms=comms)
        await notify.start()
        try:
            question = await _seed_live_shaped_question(notify)
            resolved = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "answer-delivery-consumes-question",
                    "notification_id": question["notification_id"],
                    "action_kind": "yes_no",
                    "choice": True,
                    "_auth_context": {
                        "connection_client": "pentacle-mobile",
                        "operator_authenticated": True,
                    },
                }
            )

            assert resolved["type"] == "notification.resolve.ok"
            consumed = await notify._db.call("get_agent_question", question["question_id"])
            assert consumed["state"] == "consumed"
            assert consumed["answer"]["selections"] == ["approve"]
            assert len(comms.tells) == 1
        finally:
            await notify.stop()

    _run(scenario())


def test_replayed_unconfirmed_answer_tell_does_not_begin_a_new_delivery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        comms = _RetryableAnswerTellComms()
        notify = Notify(str(tmp_path / "retryable-answer.db"), comms=comms)
        await notify.start()
        try:
            question = await _seed_live_shaped_question(notify, question_id="q-retryable-answer")
            request = {
                "type": "notification.resolve",
                "request_id": "retryable-answer",
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "choice": True,
                "_auth_context": {
                    "connection_client": "pentacle-mobile",
                    "operator_authenticated": True,
                },
            }
            await notify.notification(request)
            unanswered_delivery = await notify._db.call("get_agent_question", question["question_id"])
            assert unanswered_delivery["state"] == "answered"

            comms.delivery_status = "delivered"
            replayed = await notify.notification(request)
            still_answered = await notify._db.call("get_agent_question", question["question_id"])
            assert replayed["replayed"] is True
            assert still_answered == unanswered_delivery
            assert len(comms.tells) == 1
        finally:
            await notify.stop()

    _run(scenario())


def test_startup_consumes_only_a_previously_v2_delivered_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "notifications.db"
        initial = Notify(str(db_path))
        await initial.start()
        try:
            question = await _seed_live_shaped_question(initial)
            await initial.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "answer-without-live-tell",
                    "notification_id": question["notification_id"],
                    "action_kind": "yes_no",
                    "choice": True,
                    "_auth_context": {
                        "connection_client": "pentacle-mobile",
                        "operator_authenticated": True,
                    },
                }
            )
            answered = await initial._db.call("get_agent_question", question["question_id"])
            assert answered["state"] == "answered"
        finally:
            await initial.stop()

        comms = _AnswerTellComms()
        comms.store.answer_tell_ids.append(
            f"notification-answer-{question['notification_id']}"
        )
        recovered = Notify(str(db_path), comms=comms)
        await recovered.start()
        try:
            consumed = await recovered._db.call("get_agent_question", question["question_id"])
            assert consumed["state"] == "consumed"
            assert comms.tells == []
        finally:
            await recovered.stop()

    _run(scenario())


def test_v1_handoff_approval_is_untouched_by_v2_answer_delivery_paths(tmp_path: Path) -> None:
    """A v1 approval stays claimable: no v2 tell and no consumed_at stamp."""
    async def startup_reconciliation() -> None:
        db_path = tmp_path / "v1-handoff-startup.db"
        initial = Notify(str(db_path))
        await initial.start()
        try:
            before, _ = await _seed_answered_v1_handoff_approval(
                initial, question_id="q-v1-handoff-startup"
            )
        finally:
            await initial.stop()

        comms = _AnswerTellComms()
        recovered = Notify(str(db_path), comms=comms)
        await recovered.start()
        try:
            after = await recovered._db.call("get_agent_question", before["question_id"])
            assert after == before
            assert comms.tells == []
        finally:
            await recovered.stop()
        _assert_handoff_approval_claimable(db_path, before)

    async def live_answer_tell() -> None:
        comms = _AnswerTellComms()
        notify = Notify(str(tmp_path / "v1-handoff-live.db"), comms=comms)
        await notify.start()
        try:
            before, record = await _seed_answered_v1_handoff_approval(
                notify, question_id="q-v1-handoff-live"
            )
            await notify._after_resolution(record)
            after = await notify._db.call("get_agent_question", before["question_id"])
            assert after == before
            assert comms.tells == []
        finally:
            await notify.stop()
        _assert_handoff_approval_claimable(tmp_path / "v1-handoff-live.db", before)

    _run(startup_reconciliation())
    _run(live_answer_tell())


def test_startup_sweep_leaves_every_foreign_answered_class_byte_untouched(
    tmp_path: Path,
) -> None:
    """Only v2's own delivered-tell ledger may select a startup cleanup row."""

    async def scenario() -> None:
        db_path = tmp_path / "cycle3-foreign-startup.db"
        initial = Notify(str(db_path))
        await initial.start()
        try:
            before, _records = await _seed_cycle_3_foreign_answer_matrix(initial)
        finally:
            await initial.stop()

        comms = _AnswerTellComms()
        # This models a v1 delivery receipt: it is deliberately not a row in
        # v2's local ``v2_tell_deliveries`` ledger and therefore cannot prove
        # that v2 delivered this answer.
        comms.store.v1_foreign_tell_ids.append(
            "v1-notification-answer-q-v1-delivered-cycle3"
        )
        recovered = Notify(str(db_path), comms=comms)
        await recovered.start()
        try:
            after = {
                question_id: await recovered._db.call("get_agent_question", question_id)
                for question_id in before
            }
            assert after == before
            assert comms.tells == []
            assert comms.store.v1_foreign_tell_ids == [
                "v1-notification-answer-q-v1-delivered-cycle3"
            ]
        finally:
            await recovered.stop()

    _run(scenario())


def test_answer_tell_consumption_ignores_foreign_answered_rows(tmp_path: Path) -> None:
    """A v2 answer-delivery transaction never starts from a foreign answer."""

    async def scenario() -> None:
        comms = _AnswerTellComms()
        notify = Notify(str(tmp_path / "cycle3-foreign-live.db"), comms=comms)
        await notify.start()
        try:
            before, records = await _seed_cycle_3_foreign_answer_matrix(notify)
            for record in records:
                await notify._after_resolution(record)
            after = {
                question_id: await notify._db.call("get_agent_question", question_id)
                for question_id in before
            }
            assert after == before
            assert comms.tells == []
        finally:
            await notify.stop()

    _run(scenario())


def test_replayed_prompt_answers_do_not_begin_a_v2_delivery_transaction(
    tmp_path: Path,
) -> None:
    """A public replay keeps its idempotent reply without re-delivering it."""

    async def scenario() -> None:
        comms = _AnswerTellComms()
        notify = Notify(str(tmp_path / "replayed-answer.db"), comms=comms)
        await notify.start()
        try:
            foreign_before, _ = await _seed_answered_external_question(
                notify,
                question_id="q-v1-agent-question-public-replay",
                dedup_key="agent-question:hosta:v1:q-v1-agent-question-public-replay",
            )
            foreign_replay = await notify.prompt(
                {
                    "type": "prompt.answer",
                    "request_id": "v1-public-replay",
                    "question_id": foreign_before["question_id"],
                    "selections": ["approve"],
                    "_auth_context": {"operator_authenticated": True},
                }
            )
            foreign_after = await notify._db.call(
                "get_agent_question", foreign_before["question_id"]
            )
            assert foreign_replay["type"] == "prompt.answer.ok"
            assert foreign_replay["already_answered"] is True
            assert foreign_after == foreign_before
            assert comms.tells == []

            question = await _seed_live_shaped_question(
                notify, question_id="q-v2-consumed-public-replay"
            )
            await notify.prompt(
                {
                    "type": "prompt.answer",
                    "request_id": "v2-original-delivery",
                    "question_id": question["question_id"],
                    "selections": ["approve"],
                    "_auth_context": {"operator_authenticated": True},
                }
            )
            consumed_before = await notify._db.call(
                "get_agent_question", question["question_id"]
            )
            assert consumed_before["state"] == "consumed"
            assert len(comms.tells) == 1

            consumed_replay = await notify.prompt(
                {
                    "type": "prompt.answer",
                    "request_id": "v2-consumed-public-replay",
                    "question_id": question["question_id"],
                    "selections": ["approve"],
                    "_auth_context": {"operator_authenticated": True},
                }
            )
            consumed_after = await notify._db.call(
                "get_agent_question", question["question_id"]
            )
            assert consumed_replay["type"] == "prompt.answer.ok"
            assert consumed_replay["already_answered"] is True
            assert consumed_after == consumed_before
            assert len(comms.tells) == 1
        finally:
            await notify.stop()

    _run(scenario())


def test_custom_only_answer_keeps_the_question_answerable(tmp_path: Path) -> None:
    async def go() -> None:
        notify = Notify(db_path=str(tmp_path / "custom-answer.db"))
        await notify.start()
        try:
            question = await _seed_live_shaped_question(notify, allow_custom=True)
            invalid = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "custom-action-id",
                    "notification_id": question["notification_id"],
                    "action_kind": "yes_no",
                    "action_id": "a0",
                    "custom_text": "Need a different approval path",
                    "_auth_context": {"operator_authenticated": True},
                }
            )
            assert invalid["type"] == "notification.error"
            assert invalid["error_code"] == "notification_invalid"

            resolved = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "custom-answer",
                    "notification_id": question["notification_id"],
                    "action_kind": "yes_no",
                    "custom_text": "Need a different approval path",
                    "_auth_context": {"operator_authenticated": True},
                }
            )

            assert resolved["type"] == "notification.resolve.ok"
            assert resolved["notification"]["resolution"]["action_kind"] == "resolved"
            answered = await notify._db.call(
                "get_agent_question", "q-live-shaped-answer"
            )
            assert answered["state"] == "answered"
            assert answered["answer"]["custom_text"] == "Need a different approval path"
        finally:
            await notify.stop()

    _run(go())


def test_decline_selection_persists_false_choice(tmp_path: Path) -> None:
    async def go() -> None:
        notify = Notify(db_path=str(tmp_path / "decline-answer.db"))
        await notify.start()
        try:
            question = await _seed_live_shaped_question(
                notify, question_id="q-live-shaped-decline"
            )
            resolved = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "decline-answer",
                    "notification_id": question["notification_id"],
                    "action_kind": "yes_no",
                    "selections": ["decline"],
                    "_auth_context": {"operator_authenticated": True},
                }
            )

            assert resolved["type"] == "notification.resolve.ok"
            assert resolved["notification"]["resolution"]["choice"] is False
            answered = await notify._db.call(
                "get_agent_question", "q-live-shaped-decline"
            )
            assert answered["state"] == "answered"
            assert answered["answer"]["selections"] == ["decline"]
            assert answered["answer"]["choice"] is False
        finally:
            await notify.stop()

    _run(go())


def test_unknown_explicit_action_id_is_rejected_without_mutating_question(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        notify = Notify(db_path=str(tmp_path / "unknown-action.db"))
        await notify.start()
        try:
            question = await _seed_live_shaped_question(notify)
            invalid = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "unknown-action-id",
                    "notification_id": question["notification_id"],
                    "action_kind": "yes_no",
                    "action_id": "unknown",
                    "choice": True,
                    "_auth_context": {"operator_authenticated": True},
                }
            )

            assert invalid["type"] == "notification.error"
            assert invalid["error_code"] == "notification_invalid"
            still_open = await notify._db.call(
                "get_agent_question", "q-live-shaped-answer"
            )
            assert still_open["state"] == "open"
        finally:
            await notify.stop()

    _run(go())


def test_prompt_and_notification_resolution_derive_server_provenance(tmp_path: Path) -> None:
    async def go() -> None:
        notify = Notify(db_path=str(tmp_path / "provenance.db"))
        await notify.start()
        try:
            direct_question = await _seed_live_shaped_question(
                notify, question_id="q-direct-provenance"
            )
            direct = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "direct-provenance",
                    "notification_id": direct_question["notification_id"],
                    "action_kind": "yes_no",
                    "selections": ["approve"],
                    "by": "operator",
                    "_auth_context": {
                        "connection_client": "pentacle",
                        "token_verified": False,
                        "operator_authenticated": False,
                    },
                }
            )
            assert direct["type"] == "notification.error"
            assert direct["error_code"] == "question_unauthorized"
            assert (await notify._db.call(
                "get_agent_question", direct_question["question_id"]
            ))["state"] == "open"

            delegated_question = await _seed_live_shaped_question(
                notify,
                question_id="q-delegated-notification-provenance",
                producer_stream_id="hosta:closed-original-producer",
            )
            delegated = await notify.notification(
                {
                    "type": "notification.resolve",
                    "request_id": "delegated-notification-provenance",
                    "notification_id": delegated_question["notification_id"],
                    "action_kind": "yes_no",
                    "selections": ["approve"],
                    "by": "operator",
                    "_auth_context": {
                        "stream_id": "hostc:delegated-seat",
                        "connection_client": "agent-orch",
                        "token_verified": True,
                        "operator_authenticated": False,
                    },
                }
            )
            assert delegated["type"] == "notification.resolve.ok"
            resolution = delegated["notification"]["resolution"]
            assert resolution["actor_class"] == "verified_agent_relay"
            assert resolution["actor_stream_id"] == "hostc:delegated-seat"
            assert resolution["actor_verified"] is True
            assert resolution["by"] == "agent_relay:hostc:delegated-seat"

            relay_question = await _seed_live_shaped_question(
                notify, question_id="q-relay-provenance",
                producer_stream_id="hosta:closed-original-producer-2",
            )
            relay = await notify.prompt(
                {
                    "type": "prompt.answer",
                    "request_id": "relay-provenance",
                    "question_id": relay_question["question_id"],
                    "selections": ["approve"],
                    "by": "operator",
                    "_auth_context": {
                        "stream_id": "hosta:relay-seat",
                        "token_verified": True,
                        "operator_authenticated": False,
                        "connection_client": "agent-orch",
                    },
                }
            )
            assert relay["type"] == "prompt.answer.ok"
            answer = relay["question"]["answer"]
            assert answer["actor_class"] == "verified_agent_relay"
            assert answer["actor_stream_id"] == "hosta:relay-seat"
            assert answer["actor_client"] == "agent-orch"
            assert answer["actor_verified"] is True
            assert answer["claimed_by"] == "operator"
            assert answer["by"] == "agent_relay:hosta:relay-seat"

            owner_cancel_question = await _seed_live_shaped_question(
                notify, question_id="q-owner-cancel",
                producer_stream_id="hosta:owner-seat",
            )
            owner_cancel = await notify.prompt({
                "type": "prompt.cancel",
                "request_id": "owner-cancel",
                "question_id": owner_cancel_question["question_id"],
                "_auth_context": {
                    "stream_id": "hosta:owner-seat",
                    "token_verified": True,
                    "operator_authenticated": False,
                    "connection_client": "agent-orch",
                },
            })
            assert owner_cancel["type"] == "prompt.cancel.ok"
            assert owner_cancel["question"]["answered_at"] == owner_cancel["question"]["answer"]["at"]

            cancel_question = await _seed_live_shaped_question(
                notify,
                question_id="q-cancel-provenance",
                producer_stream_id="hosta:closed-original-producer-3",
            )
            cancelled = await notify.prompt(
                {
                    "type": "prompt.cancel",
                    "request_id": "cancel-provenance",
                    "question_id": cancel_question["question_id"],
                    "_auth_context": {
                        "stream_id": "hostc:delegated-seat",
                        "connection_client": "agent-orch",
                        "token_verified": True,
                        "operator_authenticated": False,
                    },
                }
            )
            assert cancelled["type"] == "prompt.error"
            assert cancelled["error_code"] == "question_unauthorized"
            assert (await notify._db.call(
                "get_agent_question", cancel_question["question_id"]
            ))["state"] == "open"

            operator_question = await _seed_live_shaped_question(
                notify, question_id="q-operator-provenance",
                producer_stream_id="hosta:another-producer",
            )
            operator_answer = await notify.notification({
                "type": "notification.resolve",
                "request_id": "operator-provenance",
                "notification_id": operator_question["notification_id"],
                "action_kind": "yes_no",
                "selections": ["approve"],
                "_auth_context": {
                    "connection_client": "pentacle",
                    "token_verified": False,
                    "operator_authenticated": True,
                },
            })
            resolution = operator_answer["notification"]["resolution"]
            assert resolution["actor_class"] == "direct_operator"
            assert resolution["actor_verified"] is True

            dedup_question = await _seed_live_shaped_question(
                notify, question_id="q-dedup-bypass"
            )
            dedup = await notify.notification({
                "type": "notification.resolve_by_dedup",
                "request_id": "dedup-bypass",
                "producer": "agent_question.v1",
                "dedup_key": dedup_question["dedup_key"],
                "_auth_context": {
                    "connection_client": "agent-orch",
                    "token_verified": False,
                    "operator_authenticated": False,
                },
            })
            assert dedup["type"] == "notification.error"
            assert dedup["error_code"] == "question_unauthorized"
            assert (await notify._db.call(
                "get_agent_question", dedup_question["question_id"]
            ))["state"] == "open"

            rollover_question = await _seed_live_shaped_question(
                notify, question_id="q-dedup-rollover-1",
                producer_stream_id="hosta:rollover-owner-1",
            )
            rollover_notification = await notify._db.call(
                "get_notification", rollover_question["notification_id"]
            )
            original_call = notify._db.call
            replacement: dict | None = None

            async def rollover_call(method, /, *args, **kwargs):
                nonlocal replacement
                if method == "resolve_open_dedup" and replacement is None:
                    assert kwargs["expected_notification_id"] == rollover_question["notification_id"]
                    await original_call(
                        "resolve_notification",
                        rollover_question["notification_id"],
                        action_kind="resolved",
                        by="concurrent-terminalizer",
                    )
                    envelope = dict(rollover_question["envelope"])
                    envelope.update({
                        "question_id": "q-dedup-rollover-2",
                        "producer_stream_id": "hosta:rollover-owner-2",
                    })
                    replacement = await original_call(
                        "create_agent_question",
                        envelope=envelope,
                        actions=rollover_notification["actions"],
                    )
                return await original_call(method, *args, **kwargs)

            notify._db.call = rollover_call
            rollover = await notify.notification({
                "type": "notification.resolve_by_dedup",
                "request_id": "dedup-rollover",
                "producer": "agent_question.v1",
                "dedup_key": rollover_question["dedup_key"],
                "_auth_context": {
                    "stream_id": "hosta:rollover-owner-1",
                    "connection_client": "agent-orch",
                    "token_verified": True,
                    "operator_authenticated": False,
                },
            })
            notify._db.call = original_call
            assert rollover["type"] == "notification.resolve_by_dedup.ok"
            assert rollover["resolved"] is False
            assert replacement is not None
            assert (await notify._db.call(
                "get_agent_question", replacement["question_id"]
            ))["state"] == "open"

            no_match_key = "agent-question:no-match-rollover"
            no_match_replacement: dict | None = None

            async def no_match_call(method, /, *args, **kwargs):
                nonlocal no_match_replacement
                if (
                    method == "get_latest_by_dedup"
                    and kwargs.get("dedup_key") == no_match_key
                    and no_match_replacement is None
                ):
                    result = await original_call(method, *args, **kwargs)
                    assert result is None
                    envelope = dict(rollover_question["envelope"])
                    envelope.update({
                        "question_id": "q-dedup-no-match-created",
                        "producer_stream_id": "hosta:no-match-producer",
                        "dedup_key": no_match_key,
                    })
                    no_match_replacement = await original_call(
                        "create_agent_question",
                        envelope=envelope,
                        actions=rollover_notification["actions"],
                    )
                    return result
                return await original_call(method, *args, **kwargs)

            notify._db.call = no_match_call
            no_match = await notify.notification({
                "type": "notification.resolve_by_dedup",
                "request_id": "dedup-no-match-created",
                "producer": "agent_question.v1",
                "dedup_key": no_match_key,
                "_auth_context": {
                    "connection_client": "agent-orch",
                    "token_verified": False,
                    "operator_authenticated": False,
                },
            })
            notify._db.call = original_call
            assert no_match["type"] == "notification.resolve_by_dedup.ok"
            assert no_match["resolved"] is False
            assert no_match_replacement is not None
            assert (await notify._db.call(
                "get_agent_question", no_match_replacement["question_id"]
            ))["state"] == "open"
        finally:
            await notify.stop()

    _run(go())
