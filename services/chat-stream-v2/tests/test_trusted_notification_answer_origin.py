from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from notification_answer_fixture import fixture
from store import Store
from test_notify_answer_resolution import _seed_live_shaped_question


def _run(coro):
    return asyncio.run(coro)


def test_answer_producer_projects_one_same_id_correction_after_fixed_proof(tmp_path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as (notify, queue, _comms, provider, _sessions, store):
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            notify._broadcast = broadcast
            question = await _seed_live_shaped_question(
                notify, question_id="q-synthetic-trusted-answer",
            )
            resolved = await notify.notification({
                "type": "notification.resolve",
                "request_id": "synthetic-trusted-answer",
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "choice": True,
                "selections": ["approve"],
                "note": "Keep the reviewed source boundary.",
                "_auth_context": {
                    "connection_client": "pentacle-mobile",
                    "operator_authenticated": True,
                },
            })
            assert resolved["type"] == "notification.resolve.ok"
            assert provider.pastes == []
            assert [frame for frame in frames if frame.get("type") == "chat.event"] == []

            assert await queue.drain_once(force=True) == 1
            assert len(provider.pastes) == 1
            corrections = [frame for frame in frames if frame.get("type") == "chat.event"]
            assert len(corrections) == 1

            notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='notification_answer'",
            ).fetchone()))
            tell = await store.get_tell_delivery(notice["tell_id"])
            event_id = tell["delivery"]["proof_event_id"]
            correction = corrections[0]["event"]
            assert correction["daemon_seq"] == event_id
            assert correction["text"] == notice["body"] == provider.pastes[0]
            assert correction["raw"]["daemon_notice"] == {
                "schema_version": 1,
                "kind": "notification_answer",
                "notice_id": notice["notice_id"],
                "stream_id": notice["recipient_stream_id"],
                "session_generation": json.loads(notice["metadata"])["producer_session_generation"],
                "event_id": event_id,
                "body_sha256": hashlib.sha256(notice["body"].encode()).hexdigest(),
            }

            tail = await store.fetch_session_event_tail(notice["recipient_stream_id"], limit=500)
            retained = next(item for item in tail if item["daemon_seq"] == event_id)
            assert retained == correction

    _run(scenario())


def test_answer_projection_fails_open_for_user_binding_and_missing_fixed_pair(tmp_path) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as (notify, queue, _comms, _provider, _sessions, store):
            question = await _seed_live_shaped_question(
                notify, question_id="q-synthetic-answer-controls",
            )
            await notify.notification({
                "type": "notification.resolve",
                "request_id": "synthetic-answer-controls",
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "choice": True,
                "selections": ["approve"],
                "_auth_context": {"operator_authenticated": True},
            })
            assert await queue.drain_once(force=True) == 1
            notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='notification_answer'",
            ).fetchone()))
            tell = await store.get_tell_delivery(notice["tell_id"])
            event_id = tell["delivery"]["proof_event_id"]
            exact = await store.fetch_projected_session_event(
                notice["recipient_stream_id"], event_id,
            )
            assert exact["raw"]["daemon_notice"]["kind"] == "notification_answer"

            explicit_user = {
                **exact,
                "raw": {"source": "structured", "request_id": "operator-copy"},
            }
            projected_user = (await store.project_session_events([explicit_user]))[0]
            assert "daemon_notice" not in projected_user.get("raw", {})

            await store.submit(lambda conn: (
                conn.execute(
                    "DELETE FROM v2_tell_deliveries WHERE tell_id=?",
                    (notice["tell_id"],),
                ),
                conn.commit(),
            ))
            missing = await store.fetch_projected_session_event(
                notice["recipient_stream_id"], event_id,
            )
            assert "daemon_notice" not in missing.get("raw", {})

    _run(scenario())


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_event",
        "wrong_stream",
        "wrong_generation",
        "wrong_body",
        "wrong_digest",
        "wrong_watermark",
        "unconfirmed",
        "malformed_metadata",
    ),
)
def test_answer_projection_rejects_each_mismatched_fixed_tuple(tmp_path, mutation: str) -> None:
    async def scenario() -> None:
        async with fixture(tmp_path) as (notify, queue, _comms, _provider, _sessions, store):
            question = await _seed_live_shaped_question(
                notify, question_id=f"q-synthetic-{mutation}",
            )
            await notify.notification({
                "type": "notification.resolve",
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "choice": True,
                "selections": ["approve"],
                "_auth_context": {"operator_authenticated": True},
            })
            assert await queue.drain_once(force=True) == 1
            notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='notification_answer'",
            ).fetchone()))
            tell = await store.get_tell_delivery(notice["tell_id"])
            event_id = tell["delivery"]["proof_event_id"]

            def corrupt(conn) -> None:
                row = conn.execute(
                    "SELECT reply FROM v2_tell_deliveries WHERE tell_id=?",
                    (notice["tell_id"],),
                ).fetchone()
                envelope = json.loads(row["reply"])
                metadata = json.loads(notice["metadata"])
                if mutation == "wrong_event":
                    envelope["reply"]["proof_event_id"] = event_id + 1
                    envelope["delivery"]["proof_event_id"] = event_id + 1
                elif mutation == "wrong_stream":
                    metadata["producer_stream_id"] = "hosta:other"
                elif mutation == "wrong_generation":
                    metadata["producer_session_generation"] = "replacement-generation"
                elif mutation == "wrong_body":
                    conn.execute(
                        "UPDATE v2_outbound_notices SET body=body || ' changed' WHERE notice_id=?",
                        (notice["notice_id"],),
                    )
                elif mutation == "wrong_digest":
                    envelope["payload_digest"] = "0" * 64
                elif mutation == "wrong_watermark":
                    envelope["reply"]["proof_watermark"] = event_id
                    envelope["delivery"]["proof_watermark"] = event_id
                elif mutation == "unconfirmed":
                    envelope["reply"]["confirmation_status"] = "pending"
                elif mutation == "malformed_metadata":
                    metadata.pop("question_id")
                conn.execute(
                    "UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?",
                    (json.dumps(envelope, separators=(",", ":")), notice["tell_id"]),
                )
                conn.execute(
                    "UPDATE v2_outbound_notices SET metadata=? WHERE notice_id=?",
                    (json.dumps(metadata, separators=(",", ":"), sort_keys=True), notice["notice_id"]),
                )
                conn.commit()

            await store.submit(corrupt)
            projected = await store.fetch_projected_session_event(
                notice["recipient_stream_id"], event_id,
            )
            assert "daemon_notice" not in projected.get("raw", {})

    _run(scenario())


def test_answer_proof_survives_retention_restart_and_generation_replacement(
    tmp_path, monkeypatch,
) -> None:
    db_path = tmp_path / "sessions.db"

    async def first_process() -> tuple[str, int, str, dict]:
        async with fixture(tmp_path) as (notify, queue, _comms, _provider, sessions, store):
            question = await _seed_live_shaped_question(
                notify, question_id="q-synthetic-retained-proof",
            )
            await notify.notification({
                "type": "notification.resolve",
                "notification_id": question["notification_id"],
                "action_kind": "yes_no",
                "choice": True,
                "selections": ["approve"],
                "_auth_context": {"operator_authenticated": True},
            })
            assert await queue.drain_once(force=True) == 1
            notice = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='notification_answer'",
            ).fetchone()))
            tell = await store.get_tell_delivery(notice["tell_id"])
            event_id = tell["delivery"]["proof_event_id"]
            retained = await store.fetch_projected_session_event(
                notice["recipient_stream_id"], event_id,
            )

            monkeypatch.setattr("store.TELL_RETENTION", 1)
            for index in range(3):
                await store.put_tell_delivery(
                    f"unrelated-retention-{index}",
                    {"payload_digest": str(index), "reply": {}, "delivery": {}},
                )
            assert await store.get_tell_delivery(notice["tell_id"]) is not None

            original = sessions.get(notice["recipient_stream_id"])
            await sessions.mark_closed(
                "hosta", "v2-test", expected_generation=original["session_generation"],
            )
            await sessions.open("hosta", "v2-test", provider="codex", visibility="visible")
            retained_after_reopen = (await store.project_session_events([retained]))[0]
            assert retained_after_reopen["raw"]["daemon_notice"]["event_id"] == event_id
            copied_id = await store.append_session_event(
                notice["recipient_stream_id"],
                {
                    **retained,
                    "daemon_seq": None,
                    "session_id": sessions.get(notice["recipient_stream_id"])["session_generation"],
                    "raw": {"source": "structured"},
                },
                identity="replacement-generation-answer-copy",
                limit=500,
            )
            copied = await store.fetch_projected_session_event(
                notice["recipient_stream_id"], int(copied_id),
            )
            assert "daemon_notice" not in copied.get("raw", {})
            return notice["recipient_stream_id"], event_id, notice["tell_id"], retained

    stream_id, event_id, tell_id, retained = _run(first_process())

    async def after_restart() -> None:
        store = Store(str(db_path))
        store.start()
        try:
            assert await store.get_tell_delivery(tell_id) is not None
            restarted = (await store.project_session_events([retained]))[0]
            assert restarted["raw"]["daemon_notice"]["event_id"] == event_id
            assert restarted["stream_id"] == stream_id
        finally:
            store.stop()

    _run(after_restart())
