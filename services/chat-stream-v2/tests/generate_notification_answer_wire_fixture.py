"""Generate the synthetic trusted-answer wire fixture through production paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from notification_answer_fixture import fixture
from test_notify_answer_resolution import _seed_live_shaped_question


NOTIFICATION_ID = "2bfc6d29-8c75-41ab-bc39-3dd80677bf24"
GENERATION_ID = "4cb727e8487b45caa0e8ccb905667928"


async def generate() -> dict:
    with tempfile.TemporaryDirectory(prefix="notification-answer-wire-") as directory:
        fixture_context = fixture(Path(directory), host="fixture-host")
        with patch("store.uuid.uuid4", return_value=uuid.UUID(GENERATION_ID)):
            values = await fixture_context.__aenter__()
        notify, queue, _comms, provider, sessions, store = values
        try:
                frames: list[dict] = []
                before_proof: list[dict] = []

                async def broadcast(frame: dict) -> None:
                    frames.append(frame)

                original_user = provider.user

                async def capture_before_proof(text: str) -> None:
                    await original_user(text)
                    tail = await store.fetch_session_event_tail("fixture-host:v2-test", limit=500)
                    before_proof.append(next(event for event in tail if event["text"] == text))

                provider.user = capture_before_proof
                notify._broadcast = broadcast
                with patch(
                    "_shared.notifications_store.uuid.uuid4",
                    return_value=uuid.UUID(NOTIFICATION_ID),
                ):
                    question = await _seed_live_shaped_question(
                        notify, producer_stream_id="fixture-host:v2-test", question_id="q-synthetic-trusted-answer",
                    )
                await notify.notification({
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
                assert await queue.drain_once(force=True) == 1
                correction = next(frame for frame in frames if frame.get("type") == "chat.event")
                notice = await store.submit(lambda conn: dict(conn.execute(
                    "SELECT * FROM v2_outbound_notices WHERE kind='notification_answer'",
                ).fetchone()))
                tell = await store.get_tell_delivery(notice["tell_id"])
                replay_event = await store.fetch_projected_session_event(
                    notice["recipient_stream_id"], tell["delivery"]["proof_event_id"],
                )

                identical_copy_id = await store.append_session_event(
                    notice["recipient_stream_id"],
                    {
                        **before_proof[0],
                        "daemon_seq": None,
                        "raw": {"source": "structured"},
                    },
                    identity="synthetic-receiptless-user-copy",
                    limit=500,
                )
                identical_user = await store.fetch_projected_session_event(
                    notice["recipient_stream_id"], int(identical_copy_id),
                )
                bound_copy_id = await store.append_session_event(
                    notice["recipient_stream_id"],
                    {
                        **identical_user,
                        "daemon_seq": None,
                        "request_id": "synthetic-operator-copy",
                        "raw": {"source": "structured", "request_id": "synthetic-operator-copy"},
                    },
                    identity="synthetic-explicit-user-copy",
                    limit=500,
                )
                bound_copy = await store.fetch_projected_session_event(
                    notice["recipient_stream_id"], int(bound_copy_id),
                )

                return {
                    "schema_version": 1,
                    "provenance": {
                        "classification": "synthetic-known-data",
                        "generator": "services/chat-stream-v2/tests/generate_notification_answer_wire_fixture.py",
                        "path": "Notify.notification -> OutboundNoticeQueue -> Comms proof -> Store projector -> Notify broadcast",
                    },
                    "notification_id": question["notification_id"],
                    "question_id": question["question_id"],
                    "session_generation": sessions.get("fixture-host:v2-test")["session_generation"],
                    "notice": {
                        key: notice[key]
                        for key in ("notice_id", "kind", "recipient_stream_id", "tell_id", "body")
                    },
                    "proof": {
                        key: tell["delivery"][key]
                        for key in ("proof_watermark", "proof_event_id", "proof_state")
                    },
                    "before_proof": {"type": "chat.event", "event": before_proof[0]},
                    "correction": correction,
                    "replay": {"type": "chat.event", "event": replay_event},
                    "receiptless_identical_user": {
                        "type": "chat.event", "event": identical_user,
                    },
                    "explicit_user_copy": {"type": "chat.event", "event": bound_copy},
                }
        finally:
            await fixture_context.__aexit__(None, None, None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(asyncio.run(generate()), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
