"""Active-v2 report and prompt receipt contract.

Prompt fixtures use only temporary databases and in-process addressed sessions;
they cannot create a production operator-facing question or card.
"""

from __future__ import annotations

import asyncio

import pytest

from ledger import (
    Ledger,
    child_report_ready_tell_id,
    child_report_ready_text,
)
from notify import Notify
from sessions import Sessions, VerbError
from store import ReportProvenanceChanged, ReportReplayConflict, Store


def _legacy_child_report_ready_tell_id(report_id: object) -> str:
    return "child-report-ready-" + str(report_id).replace("/", "-")


def _delivery_envelope(*, tell_id: str, source: str, target: str, text: str,
                       status: str = "delivered") -> dict:
    delivered = status == "delivered"
    ack = "2026-08-26T15:00:00Z" if delivered else None
    reply = {
        "type": "tell.ok", "tell_id": tell_id, "to_stream_id": target,
        "delivery_status": status, "submission_confirmed": delivered,
        "delivery_ack_at": ack,
    }
    delivery = {
        "tell_id": tell_id, "from_stream_id": source, "to_stream_id": target,
        "text": text, "delivery_status": status,
        "submission_confirmed": delivered, "delivery_ack_at": ack,
        "delivered_at": ack,
    }
    return {"payload_digest": "fixture", "reply": reply, "delivery": delivery}


class _ReceiptQueue:
    def __init__(self, store: Store, *, status: str = "delivered") -> None:
        self.store = store
        self.status = status
        self.comms = object()
        self.calls: list[dict] = []

    async def enqueue(self, **fields: object) -> dict:
        self.calls.append(dict(fields))
        return {"created": len(self.calls) == 1, "notice_id": fields["tell_id"]}

    async def deliver_now(self, _notice_id: str, **_kwargs: object) -> bool:
        call = self.calls[-1]
        if self.status == "missing":
            return False
        await self.store.put_tell_delivery(
            str(call["tell_id"]),
            _delivery_envelope(
                tell_id=str(call["tell_id"]), source=str(call["source_stream_id"]),
                target=str(call["recipient_stream_id"]), text=str(call["body"]),
                status=self.status,
            ),
        )
        return self.status == "delivered"


class _PromptComms:
    def __init__(self, store: Store, *, status: str = "delivered") -> None:
        self.store = store
        self.status = status
        self.calls: list[dict] = []

    async def deliver_outbound_notice(self, message: dict, **_kwargs: object) -> dict:
        self.calls.append(dict(message))
        tell_id = str(message["tell_id"])
        envelope = _delivery_envelope(
            tell_id=tell_id, source=str(message["from_stream_id"]),
            target=str(message["to_stream_id"]), text=str(message["message"]),
            status=self.status,
        )
        row_id = await self.store.put_tell_delivery(tell_id, envelope)
        return {**envelope["reply"], "ledger_row_id": row_id}

    async def tell(self, _message: dict) -> dict:
        return {"type": "tell.ok", "delivery_status": "delivered"}


async def _lineage(store: Store) -> Sessions:
    sessions = Sessions(store, local_host="hosta")
    await sessions.open("hosta", "parent", provider="shell")
    await sessions.open(
        "hosta", "child", provider="shell", visibility="hidden",
        parent_stream_id="hosta:parent",
    )
    return sessions


def _report(report_id: str, *, summary: str = "finished") -> dict:
    return {
        "type": "report", "report_id": report_id,
        "from_stream_id": "hosta:child", "caller_stream_id": "hosta:child",
        "msg_id": 7, "status": "done", "summary": summary,
        "findings": [], "next_action": "continue",
    }


def test_report_store_conflict_exceptions_keep_controlled_messages() -> None:
    expected = {"session_generation": "old"}
    current = {"session_generation": "new"}
    changed = ReportProvenanceChanged(expected, current)
    assert str(changed) == "report provenance changed during write"
    assert changed.expected == expected
    assert changed.current == current

    replay = ReportReplayConflict("report/a")
    assert str(replay) == "report_id_replay_conflict"
    assert replay.report_id == "report/a"


def test_report_first_replay_conflict_and_canonical_delivery_are_durable() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = await _lineage(store)
            queue = _ReceiptQueue(store)
            ledger = Ledger(store, sessions=sessions, outbound=queue)
            first = await ledger.report(_report("report/a"))
            replay = await ledger.report(_report("report/a"))
            assert first["ledger_row_id"] == replay["ledger_row_id"]
            assert first["notice_delivery"] == replay["notice_delivery"]
            assert first["to_stream_id"] == "hosta:parent"
            receipt = first["notice_delivery"]
            assert receipt["delivery_status"] == "delivered"
            assert receipt["to_stream_id"] == "hosta:parent"
            assert isinstance(receipt["ledger_row_id"], int)
            assert receipt["delivery_ack_at"]
            assert len(queue.calls) == 2
            with pytest.raises(VerbError) as raised:
                await ledger.report(_report("report/a", summary="changed"))
            assert raised.value.code == "report_id_replay_conflict"
            assert (await store.get_report("report/a"))["summary"] == "finished"
            assert len(queue.calls) == 2
        finally:
            store.stop()
    asyncio.run(run())


def test_report_ids_are_injective_and_foreign_actor_fails_before_mutation() -> None:
    assert child_report_ready_tell_id("report/a") != child_report_ready_tell_id("report-a")
    legal = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-"
    assert len({child_report_ready_tell_id(f"r{char}") for char in legal}) == len(legal)

    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = await _lineage(store)
            queue = _ReceiptQueue(store)
            payload = _report("foreign-report")
            payload["caller_stream_id"] = "hosta:foreign"
            with pytest.raises(VerbError) as raised:
                await Ledger(store, sessions=sessions, outbound=queue).report(payload)
            assert raised.value.code == "from_stream_id_forbidden"
            assert await store.get_report("foreign-report") is None
            assert queue.calls == []
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "expected"),
    [("proof_unavailable", "proof_unavailable"), ("missing", "failed")],
)
def test_report_unproven_or_missing_receipt_never_claims_delivery(status: str, expected: str) -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = await _lineage(store)
            reply = await Ledger(
                store, sessions=sessions, outbound=_ReceiptQueue(store, status=status)
            ).report(_report(f"report-{status}"))
            assert reply["notice_delivery"]["delivery_status"] == expected
            assert reply["notice_delivery"]["to_stream_id"] == "hosta:parent"
            if status == "missing":
                assert reply["notice_delivery"]["error_code"] == "notice_receipt_missing"
            else:
                assert reply["notice_delivery"].get("delivery_ack_at") is None
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("legacy_target", ["hosta:parent", "hosta:wrong-parent"])
def test_persisted_legacy_report_receipt_is_reused_only_for_exact_target(legacy_target: str) -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = await _lineage(store)
            queue = _ReceiptQueue(store)
            ledger = Ledger(store, sessions=sessions, outbound=queue)
            row = {
                "report_id": "legacy/report", "ledger_row_id": 41,
                "from_stream_id": "hosta:child", "to_stream_id": "hosta:parent",
                "msg_id": 8, "status": "done", "summary": "legacy",
            }
            legacy_id = _legacy_child_report_ready_tell_id(row["report_id"])
            text = child_report_ready_text(row)
            await store.put_tell_delivery(
                legacy_id,
                _delivery_envelope(
                    tell_id=legacy_id, source="hosta:child", target=legacy_target, text=text
                ),
            )
            before = await store.get_tell_delivery(legacy_id)
            receipt = await ledger._announce_child_report_ready(row)
            assert await store.get_tell_delivery(legacy_id) == before
            assert queue.calls == []
            if legacy_target == "hosta:parent":
                assert receipt["delivery_status"] == "delivered"
                assert receipt["tell_id"] == legacy_id
            else:
                assert receipt == {
                    "delivery_status": "failed", "to_stream_id": "hosta:parent",
                    "tell_id": legacy_id,
                    "error_code": "notice_receipt_target_mismatch",
                }
                assert await store.get_tell_delivery(
                    child_report_ready_tell_id(row["report_id"])
                ) is None
        finally:
            store.stop()
    asyncio.run(run())


def _prompt(
    question_id: str = "q-item4", *, body: str = "Proceed?",
    producer: str = "hosta:parent", from_stream: str = "hosta:parent",
) -> dict:
    options = [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}]
    return {
        "type": "prompt.ask", "request_id": f"request-{question_id}",
        "from_stream_id": from_stream,
        "_auth_context": {"stream_id": from_stream, "token_verified": True},
        "envelope": {
            "schema_version": 1, "question_id": question_id,
            "title": "Deploy window", "body": body,
            "dedup_key": f"item4:{question_id}", "response_mode": "single_choice",
            "producer_stream_id": producer, "options": options,
        },
        "actions": [
            {"kind": "yes_no", "action_id": f"a{i}", "choice": i == 0,
             "value": {"schema_version": 1, "question_id": question_id, "answer": o["value"]}}
            for i, o in enumerate(options)
        ],
    }


def test_prompt_auth_eligibility_and_replay_use_only_disposable_stores(tmp_path) -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        notify = None
        try:
            sessions = await _lineage(store)
            comms = _PromptComms(store)
            notify = Notify(
                str(tmp_path / "notifications.db"), comms=comms,
                sessions=sessions, notice_store=store,
            )
            await notify.start()
            # Forged identity: the token owner is not the claimed producer.
            forged = _prompt("q-forged", producer="hosta:parent", from_stream="hosta:foreign")
            refused = await notify.prompt(forged)
            assert refused["error_code"] == "stream_ownership_unverified"
            assert (await notify.prompt(
                {"type": "prompt.status", "question_id": "q-forged"}
            ))["error_code"] == "question_not_found"
            # A hidden child cannot ask the operator: it is told to ask its parent.
            hidden = await notify.prompt(
                _prompt("q-child", producer="hosta:child", from_stream="hosta:child")
            )
            assert hidden["error_code"] == "ask_parent"
            assert hidden["parent_stream_id"] == "hosta:parent"
            assert (await notify.prompt(
                {"type": "prompt.status", "question_id": "q-child"}
            ))["error_code"] == "question_not_found"
            # A verified, operator-visible producer may ask; no parent notice.
            first = await notify.prompt(_prompt())
            assert first["question"]["state"] == "open"
            assert "notice_delivery" not in first and "to_stream_id" not in first
            # Identical replay is idempotent; a differing envelope conflicts.
            replay = await notify.prompt(_prompt())
            assert first["question"]["notification_id"] == replay["question"]["notification_id"]
            conflict = await notify.prompt(_prompt(body="Different?"))
            assert conflict["error_code"] == "prompt_question_replay_conflict"
            # No parent notice is delivered on any of these paths.
            assert comms.calls == []
            status = await notify.prompt({"type": "prompt.status", "question_id": "q-item4"})
            assert status["question"]["envelope"]["body"] == "Proceed?"
        finally:
            if notify is not None:
                await notify.stop()
            store.stop()
    asyncio.run(run())
