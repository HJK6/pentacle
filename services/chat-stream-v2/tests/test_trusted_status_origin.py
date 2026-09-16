from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest

from comms import Comms
from ledger import NUDGE_CARD_TEXT, NudgeConfig, NudgeJob
from outbound_notices import (
    NOTICE_KIND_STATUS_CARD,
    OutboundNoticeConfig,
    OutboundNoticeQueue,
)
from spawnctl import SpawnCtl
from store import Store
from submission_events import DurableUserEventProof
from tests.test_nudges import HOST, Harness, _card
from tests.test_tell_immediate_delivery import StoreTailProofCodex


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("title", "expected_kind"),
    (("Trusted status", "status_card"), ("", "status_card_combined")),
)
def test_status_nudge_fixes_one_event_proof_and_projects_same_id_correction(
    tmp_path, title: str, expected_kind: str,
) -> None:
    db = str(tmp_path / f"{expected_kind}.db")

    async def _first_process() -> tuple[str, int, list[dict]]:
        store = Store(db)
        store.start()
        try:
            setup = Harness(store, NudgeConfig(status_stale_s=0.0, engaged_window_s=3600.0))
            await setup.open(
                "trusted-status",
                provider="codex",
                title=title,
                status_card=_card(time.time() - 3600),
                user_event_count=0,
            )
            await setup.qualify("trusted-status")
            await setup.qualify("trusted-status")
            stream_id = f"{HOST}:trusted-status"

            tmux = StoreTailProofCodex(store, stream_id, append_on_paste=True)
            tmux.screen = "busy command remains active"
            key_events: list[str] = []
            original_run = tmux.run

            async def record_keys(*args, **kwargs):
                if args[:1] == ("send-keys",):
                    key_events.append(str(args[-1]))
                return await original_run(*args, **kwargs)

            tmux.run = record_keys
            spawnctl = SpawnCtl(store, setup.sessions, tmux=tmux)
            comms = Comms(
                store,
                setup.sessions,
                spawnctl,
                submission_proof=DurableUserEventProof(store, local_host=HOST),
            )
            queue = OutboundNoticeQueue(
                store,
                comms,
                config=OutboundNoticeConfig(lease_s=0.1, max_attempts=3),
                owner="trusted-status-test",
            )
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            job = NudgeJob(
                setup.sessions,
                comms,
                store,
                NudgeConfig(status_stale_s=0.0, engaged_window_s=3600.0),
                outbound=queue,
                broadcast=broadcast,
            )
            result = await job.run_pass()
            assert (result.sent, result.errors, len(tmux.pastes)) == (1, 0, 1)
            assert key_events == []
            assert NUDGE_CARD_TEXT in tmux.pastes[0]
            assert tmux.pastes[0].startswith(f"[pentacle-notice:nudge:{expected_kind}:")

            row = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind=?",
                (expected_kind,),
            ).fetchone()))
            binding = json.loads(row["proof_binding"])
            assert binding == {
                "body_sha256": hashlib.sha256(row["body"].encode()).hexdigest(),
                "kind": expected_kind,
                "notice_id": row["notice_id"],
                "pre_input_watermark": binding["pre_input_watermark"],
                "proof_event_id": binding["proof_event_id"],
                "recipient_stream_id": stream_id,
                "session_generation": binding["session_generation"],
            }
            assert binding["proof_event_id"] > binding["pre_input_watermark"]
            assert binding["session_generation"]

            assert len(frames) == 1
            assert frames[0]["type"] == "chat.event"
            assert frames[0]["event"]["daemon_seq"] == binding["proof_event_id"]
            assert frames[0]["event"]["stream_id"] == stream_id
            assert frames[0]["event"]["text"] == row["body"]
            projected = frames[0]["event"]["raw"]["daemon_notice"]
            assert projected == {
                "schema_version": 1,
                "kind": expected_kind,
                "notice_id": row["notice_id"],
                "stream_id": stream_id,
                "session_generation": binding["session_generation"],
                "event_id": binding["proof_event_id"],
                "body_sha256": binding["body_sha256"],
            }

            tail = await store.fetch_session_event_tail(stream_id, limit=500)
            proof_event = next(event for event in tail if event["daemon_seq"] == binding["proof_event_id"])
            assert proof_event == frames[0]["event"]
            await store.submit(lambda conn: (
                conn.execute(
                    "DELETE FROM v2_tell_deliveries WHERE tell_id=?",
                    (row["tell_id"],),
                ),
                conn.commit(),
            ))
            assert await store.get_tell_delivery(row["tell_id"]) is None
            retained = await store.fetch_session_event_tail(stream_id, limit=500)
            assert next(
                event for event in retained
                if event["daemon_seq"] == binding["proof_event_id"]
            )["raw"]["daemon_notice"]["notice_id"] == row["notice_id"]
            return stream_id, binding["proof_event_id"], frames
        finally:
            store.stop()

    stream_id, event_id, frames = _run(_first_process())
    assert len(frames) == 1

    async def _after_restart() -> tuple[dict, dict]:
        store = Store(db)
        store.start()
        try:
            tail = await store.fetch_session_event_tail(stream_id, limit=500)
            restarted = next(event for event in tail if event["daemon_seq"] == event_id)
            retained_original = {
                **restarted,
                "raw": {"source": "structured"},
            }
            original = await store.fetch_session(HOST, "trusted-status")
            assert original is not None
            assert await store.mark_closed(
                HOST,
                "trusted-status",
                closed_at="2026-09-16T02:20:00Z",
                pane_status="pane_dead",
                expected_generation=original["session_generation"],
            ) is not None
            replacement = Harness(store)
            await replacement.open(
                "trusted-status",
                provider="codex",
                title="Replacement",
                created_at="2026-09-16T02:21:00Z",
                user_event_count=0,
            )
            outbound = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind=?",
                (expected_kind,),
            ).fetchone()))
            copied_event_id = await store.append_session_event(
                stream_id,
                {
                    "stream_id": stream_id,
                    "host": HOST,
                    "provider": "codex",
                    "session_name": "trusted-status",
                    "kind": "USER",
                    "text": outbound["body"],
                    "timestamp": "2026-09-16T02:21:01Z",
                    "raw": {"source": "structured"},
                },
                identity="replacement-generation-copy",
                limit=500,
            )
            copied = await store.fetch_projected_session_event(
                stream_id, int(copied_event_id),
            )
            retained_after_reopen = (
                await store.project_session_events([retained_original])
            )[0]
            return retained_after_reopen, copied
        finally:
            store.stop()

    retained_after_reopen, replacement_copy = _run(_after_restart())
    assert retained_after_reopen["raw"]["daemon_notice"]["event_id"] == event_id
    assert "daemon_notice" not in replacement_copy.get("raw", {})


def test_projector_strips_unbound_reserved_raw_and_never_transfers_proof(tmp_path) -> None:
    async def _case() -> None:
        store = Store(str(tmp_path / "forged.db"))
        store.start()
        try:
            harness = Harness(store)
            await harness.open("forged", title="forged")
            stream_id = f"{HOST}:forged"
            forged = {
                "stream_id": stream_id,
                "host": HOST,
                "provider": "claude",
                "session_name": "forged",
                "kind": "USER",
                "text": NUDGE_CARD_TEXT,
                "timestamp": "2026-09-16T02:00:00Z",
                "raw": {"source": "structured", "daemon_notice": {
                    "schema_version": 1,
                    "kind": "status_card",
                    "notice_id": "copied",
                    "stream_id": stream_id,
                    "session_generation": "wrong",
                    "event_id": 1,
                    "body_sha256": hashlib.sha256(NUDGE_CARD_TEXT.encode()).hexdigest(),
                }},
            }
            event_id = await store.append_session_event(
                stream_id, forged, identity="forged", limit=500,
            )
            projected = await store.fetch_session_event_tail(stream_id, limit=10)
            row = next(item for item in projected if item["daemon_seq"] == event_id)
            assert "daemon_notice" not in row.get("raw", {})
        finally:
            store.stop()

    _run(_case())


def test_status_binding_rejects_wrong_proof_stream_watermark_event_and_digest(
    tmp_path,
) -> None:
    async def _case() -> None:
        store = Store(str(tmp_path / "wrong-proof.db"))
        store.start()
        try:
            setup = Harness(store)
            await setup.open(
                "wrong-proof", provider="codex", title="Wrong proof",
                user_event_count=0,
            )
            stream_id = f"{HOST}:wrong-proof"
            tmux = StoreTailProofCodex(store, stream_id, append_on_paste=True)
            comms = Comms(
                store,
                setup.sessions,
                SpawnCtl(store, setup.sessions, tmux=tmux),
                submission_proof=DurableUserEventProof(store, local_host=HOST),
            )
            queue = OutboundNoticeQueue(
                store,
                comms,
                config=OutboundNoticeConfig(lease_s=1.0, max_attempts=3),
                owner="wrong-proof-test",
            )
            notice_id = "nudge:status_card:wrong-proof-fixture"
            await queue.enqueue(
                kind=NOTICE_KIND_STATUS_CARD,
                dedupe_key="wrong-proof-fixture",
                recipient_stream_id=stream_id,
                tell_id=notice_id,
                body=NUDGE_CARD_TEXT,
            )
            claimed = await store.claim_outbound_notice(
                notice_id, owner=queue.owner, lease_s=1.0, force=True,
            )
            proof = await comms.deliver_outbound_notice(
                {
                    "tell_id": claimed["tell_id"],
                    "stream_id": stream_id,
                    "to_stream_id": stream_id,
                    "message": claimed["body"],
                    "urgent": False,
                },
                check_existing=False,
            )
            bad_proofs = [
                {**proof, "to_stream_id": f"{HOST}:other"},
                {**proof, "proof_watermark": proof["proof_event_id"]},
                {**proof, "proof_event_id": proof["proof_event_id"] + 1},
            ]
            for bad in bad_proofs:
                assert await store.complete_trusted_status_notice(
                    notice_id, owner=queue.owner, proof=bad,
                ) is None
            pending = await store.outbound_notice_for_dedupe("wrong-proof-fixture")
            assert pending["proof_binding"] is None

            await store.submit(lambda conn: (
                conn.execute(
                    "UPDATE v2_tell_deliveries SET reply=json_set(reply, '$.payload_digest', ?) "
                    "WHERE tell_id=?",
                    ("0" * 64, notice_id),
                ),
                conn.commit(),
            ))
            assert await store.complete_trusted_status_notice(
                notice_id, owner=queue.owner, proof=proof,
            ) is None
            pending = await store.outbound_notice_for_dedupe("wrong-proof-fixture")
            assert pending["proof_binding"] is None
        finally:
            store.stop()

    _run(_case())


def test_delayed_proof_promotes_same_notice_without_reinjection(tmp_path) -> None:
    async def _case() -> tuple[int, int, int]:
        store = Store(str(tmp_path / "delayed.db"))
        store.start()
        try:
            setup = Harness(store, NudgeConfig(status_stale_s=0.0, engaged_window_s=3600.0))
            await setup.open(
                "delayed",
                provider="codex",
                title="Delayed",
                status_card=_card(time.time() - 3600),
                user_event_count=0,
            )
            await setup.qualify("delayed")
            await setup.qualify("delayed")
            stream_id = f"{HOST}:delayed"
            tmux = StoreTailProofCodex(store, stream_id, append_on_paste=False)
            comms = Comms(
                store,
                setup.sessions,
                SpawnCtl(store, setup.sessions, tmux=tmux),
                submission_proof=DurableUserEventProof(store, local_host=HOST),
            )
            queue = OutboundNoticeQueue(
                store,
                comms,
                config=OutboundNoticeConfig(lease_s=0.1, max_attempts=3),
                owner="delayed-proof-test",
            )
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            job = NudgeJob(
                setup.sessions,
                comms,
                store,
                NudgeConfig(status_stale_s=0.0, engaged_window_s=3600.0),
                outbound=queue,
                broadcast=broadcast,
            )
            first = await job.run_pass()
            row = await store.submit(lambda conn: dict(conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE kind='status_card'"
            ).fetchone()))
            assert (first.sent, first.pending, len(tmux.pastes)) == (0, 1, 1)
            assert row["proof_binding"] is None and row["delivered_at"] is None

            event_id = await store.append_session_event(
                stream_id,
                {
                    "stream_id": stream_id,
                    "host": HOST,
                    "provider": "codex",
                    "session_name": "delayed",
                    "kind": "USER",
                    "text": row["body"],
                    "timestamp": "2026-09-16T02:10:00Z",
                },
                identity="delayed-status-proof",
                limit=500,
            )
            assert await queue.deliver_now(row["notice_id"]) is True
            completed = await store.outbound_notice_for_dedupe(row["dedupe_key"])
            binding = json.loads(completed["proof_binding"])
            assert binding["proof_event_id"] == event_id
            return len(tmux.pastes), len(frames), int(completed["attempts"])
        finally:
            store.stop()

    assert _run(_case()) == (1, 1, 2)


@pytest.mark.parametrize("kind", ("status_card", "status_card_combined"))
def test_status_queue_kinds_are_explicitly_passive(kind: str) -> None:
    class RecordingComms:
        def __init__(self) -> None:
            self.messages: list[dict] = []

        async def deliver_outbound_notice(
            self, message: dict, *, check_existing: bool = False,
        ) -> dict:
            self.messages.append(dict(message))
            return {
                "delivery_status": "committed_pending_proof",
                "proof_state": "pending",
            }

    async def _case() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            comms = RecordingComms()
            queue = OutboundNoticeQueue(
                store,
                comms,
                config=OutboundNoticeConfig(lease_s=0.1, max_attempts=3),
                owner=f"passive-{kind}",
            )
            notice_id = f"passive-{kind}"
            await queue.enqueue(
                kind=kind,
                dedupe_key=notice_id,
                recipient_stream_id="fixture-host:busy",
                tell_id=notice_id,
                body=NUDGE_CARD_TEXT,
            )
            assert await queue.deliver_now(notice_id) is False
            assert len(comms.messages) == 1
            return comms.messages[0]
        finally:
            store.stop()

    assert _run(_case())["urgent"] is False
