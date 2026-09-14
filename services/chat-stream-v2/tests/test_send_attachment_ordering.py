"""Real Comms regressions at the provider-ingestion/materialization boundary."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from test_send_semantics import FakeTmux, FakeBlobStore, VALID, PNG, _new_comms, _run, HOST, NAME

STREAM = f"{HOST}:{NAME}"
AUTH = {"operator_principal": "operator:op-1", "operator_authenticated": True}


def _message(request_id: str, caption: str = "photo caption") -> dict:
    return {"stream_id": STREAM, "request_id": request_id, "text": caption,
            "optimistic_id": "optimistic-photo", "attachments": [VALID], "_auth_context": AUTH}


@pytest.mark.parametrize("caption", ["", "photo caption"])
def test_provider_ingestion_before_landed_receipt_is_projected(tmp_path: Path, caption: str) -> None:
    async def go() -> None:
        class IngestingTmux(FakeTmux):
            async def paste(self, name: str, text: str) -> None:
                # Provider USER arrives during paste, before Comms can append landed.
                receipt = await store.get_send_receipt(STREAM, "send-early-photo")
                assert receipt["state"] == "accepted"
                event = await store.stamp_event_with_send_receipt(
                    {"kind": "USER", "stream_id": STREAM, "text": text})
                events.append(event)
                await store.append_session_event(STREAM, event, identity="provider-user", limit=50)
                await super().paste(name, text)
        events = []
        tmux = IngestingTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            result = await comms.send(_message("send-early-photo", caption))
            assert result["submission_confirmed"] is True
            assert len(events) == len(tmux.pastes) == 1
            event = events[0]
            assert event.get("optimistic_id") == "optimistic-photo", event
            assert event["request_id"] == "send-early-photo"
            assert event["text"] == caption
            assert event["attachments"][0]["key"] == VALID["key"]
            assert event["receipt_state"] == "accepted"
            tail = await store.fetch_session_event_tail(STREAM, limit=50)
            assert len(tail) == 1
            assert tail[0]["text"] == caption
            assert tail[0]["optimistic_id"] == "optimistic-photo"
            assert tail[0]["attachments"][0]["key"] == VALID["key"]
        finally:
            store.stop()
    _run(go())


def test_materialized_failure_allows_one_concurrent_image_retry(tmp_path: Path) -> None:
    async def go() -> None:
        tmux = FakeTmux(fail_phase="not_started")
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            failed = await comms.send(_message("send-failed-photo"))
            assert failed["delivery"] == "not_landed"
            assert len(tmux.pastes) == 1
            assert VALID["key"] in tmux.pastes[0], "failure must follow actual materialization"
            tmux.fail_phase = None
            results = await asyncio.gather(
                comms.send(_message("retry-photo-a")),
                comms.send(_message("retry-photo-b")),
            )
            assert len(tmux.pastes) == 2, "one failed paste plus exactly one fresh retry"
            assert sum(not result.get("coalesced", False) for result in results) == 1
            assert any(result["submission_confirmed"] for result in results)
            # A later retry observes the terminal successful outcome.
            landed = await comms.send(_message("retry-photo-c"))
            assert landed.get("coalesced") is True
            assert landed["submission_confirmed"] is True
            assert len(tmux.pastes) == 2
        finally:
            store.stop()
    _run(go())


def test_concurrent_image_retries_share_pre_materialization_claim(tmp_path: Path) -> None:
    async def go() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        class BlockingBlobs(FakeBlobStore):
            async def read_verified(self, sha: str, *, max_bytes: int) -> bytes:
                entered.set()
                await release.wait()
                return await super().read_verified(sha, max_bytes=max_bytes)
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=BlockingBlobs())
        try:
            await sessions.open(HOST, NAME, provider="claude")
            first = asyncio.create_task(comms.send(_message("send-blocked-photo")))
            await asyncio.wait_for(entered.wait(), 2)
            duplicate = await comms.send(_message("retry-blocked-photo"))
            assert duplicate.get("coalesced") is True
            assert tmux.pastes == []
            release.set()
            assert (await first)["submission_confirmed"] is True
            assert len(tmux.pastes) == 1
        finally:
            release.set()
            store.stop()
    _run(go())


def test_same_caption_different_image_is_a_distinct_payload(tmp_path: Path) -> None:
    async def go() -> None:
        second_png = PNG + b"second"
        second_sha = hashlib.sha256(second_png).hexdigest()
        tmux = FakeTmux()
        blobs = FakeBlobStore({VALID["key"]: PNG, second_sha: second_png})
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=blobs)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            await comms.send(_message("send-image-one"))
            second = _message("send-image-two")
            second["attachments"] = [{**VALID, "key": second_sha}]
            result = await comms.send(second)
            assert not result.get("coalesced")
            assert result["submission_confirmed"] is True
            assert len(tmux.pastes) == 2
        finally:
            store.stop()
    _run(go())


@pytest.mark.parametrize("difference", ["actor", "target", "missing_optimistic_id"])
def test_attachment_logical_identity_fences(tmp_path: Path, difference: str) -> None:
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            first = _message("send-fence-a")
            second = _message("send-fence-b")
            if difference == "actor":
                second["_auth_context"] = {**AUTH, "operator_principal": "operator:op-2"}
            elif difference == "target":
                await sessions.open(HOST, "other-target", provider="claude")
                second["stream_id"] = f"{HOST}:other-target"
            else:
                first.pop("optimistic_id")
                second.pop("optimistic_id")
            assert (await comms.send(first))["submission_confirmed"] is True
            result = await comms.send(second)
            assert not result.get("coalesced")
            assert result["submission_confirmed"] is True
            assert len(tmux.pastes) == 2
            # Exact request replay coalesces even without a logical ID.
            assert (await comms.send(second)).get("coalesced") is True
            assert len(tmux.pastes) == 2
        finally:
            store.stop()
    _run(go())


@pytest.mark.parametrize("caption", ["", "photo caption"])
def test_attachment_ingest_confirms_real_durable_submission(tmp_path: Path, monkeypatch, caption: str) -> None:
    """No pane echo or proof-success seam: real ingest, persisted proof and display."""
    from ingest import append_ingested_event
    from submission_events import DurableUserEventProof, EventWatermark
    import comms as comms_module

    # Bound a failing test; the real matcher and proof reader stay installed.
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def go() -> None:
        broadcasts = []
        async def broadcast(frame):
            broadcasts.append(frame)
        class ProviderTmux(FakeTmux):
            async def paste(self, name: str, text: str) -> None:
                await super().paste(name, text)
                # An accepted final-wire receipt alone must never prove delivery.
                assert not (await proof.lookup(
                    STREAM, expected_text=text, watermark=watermark)).proven
                await append_ingested_event(store, broadcast, {
                    "kind": "USER", "stream_id": STREAM, "host": HOST,
                    "session_name": NAME, "provider": "claude", "text": text,
                    "timestamp": "2026-09-14T19:41:38.757265+00:00",
                    "raw": {"jsonl_record_uuid": "provider-photo-user", "jsonl_event_index": 0},
                }, recent_limit=50)
        # An idle composer has no echoed submission; only durable USER can prove it.
        tmux = ProviderTmux(submit_on_paste=False)
        comms, store, sessions = _new_comms(tmux, tmp_path)
        proof = DurableUserEventProof(store, local_host=HOST)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            watermark = await proof.watermark(STREAM)
            result = await comms.send(_message("send-durable-photo", caption))
            tail = await store.fetch_session_event_tail(STREAM, limit=50)
            assert len(tail) == len(broadcasts) == 1
            assert tail[0]["text"] == caption
            assert tail[0]["optimistic_id"] == "optimistic-photo"
            assert tail[0]["attachments"][0]["key"] == VALID["key"]
            observed = await proof.lookup(STREAM, expected_text=tmux.pastes[0], watermark=watermark)
            assert observed.proven, {"proof": observed.audit_fields(), "result": result, "event": tail[0]}
            assert observed.event_id == tail[0]["daemon_seq"]
            assert result["submission_confirmed"] is True
            assert result["delivery"] == "landed"
            for wrong_text in (caption, tmux.pastes[0].replace(VALID["key"], "f" * 64)):
                assert not (await proof.lookup(
                    STREAM, expected_text=wrong_text, watermark=watermark)).proven
                assert not comms._tail_event_matches_submission(tail[0], STREAM, wrong_text)
            assert not (await proof.lookup(
                STREAM, expected_text=tmux.pastes[0],
                watermark=EventWatermark(STREAM, observed.event_id, "reachable"))).proven
            assert not (await proof.lookup(
                f"{HOST}:other", expected_text=tmux.pastes[0],
                watermark=EventWatermark(f"{HOST}:other", 0, "reachable"))).proven
            # Reopening the same stream must not inherit its prior USER proof.
            row = await store.fetch_session(HOST, NAME)
            assert await store.mark_closed(
                HOST, NAME, closed_at="2026-09-14T20:00:00Z", pane_status="pane_dead",
                expected_generation=row["session_generation"], close_kind="test")
            await store.open_session(HOST, NAME, provider="claude")
            assert not (await proof.lookup(
                STREAM, expected_text=tmux.pastes[0], watermark=watermark)).proven
        finally:
            store.stop()
    _run(go())


@pytest.mark.parametrize("kind", ["ASSIST", "TELL", "TOOL_RESULT"])
def test_non_user_projected_event_cannot_prove_durable_user(tmp_path: Path, kind: str) -> None:
    from submission_events import DurableUserEventProof, EventWatermark, provider_text_digest
    async def go() -> None:
        comms, store, sessions = _new_comms(FakeTmux(), tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            await store.append_session_event(STREAM, {
                "stream_id": STREAM, "kind": kind, "text": "caption",
                "provider_text_digest": provider_text_digest("provider wire"),
            }, identity="wrong-kind", limit=50)
            proof = DurableUserEventProof(store, local_host=HOST)
            assert not (await proof.lookup(
                STREAM, expected_text="provider wire",
                watermark=EventWatermark(STREAM, 0, "reachable"))).proven
        finally:
            store.stop()
    _run(go())


@pytest.mark.parametrize("digest", [None, "", {}, "A" * 64, "f" * 64])
def test_malformed_proof_metadata_never_falls_back_to_caption(tmp_path: Path, digest) -> None:
    from submission_events import DurableUserEventProof, EventWatermark
    async def go() -> None:
        comms, store, sessions = _new_comms(FakeTmux(), tmp_path)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            event = {"stream_id": STREAM, "kind": "USER", "text": "caption",
                     "provider_text_digest": digest}
            await store.append_session_event(STREAM, event, identity="malformed", limit=50)
            proof = DurableUserEventProof(store, local_host=HOST)
            assert not (await proof.lookup(
                STREAM, expected_text="caption",
                watermark=EventWatermark(STREAM, 0, "reachable"))).proven
            assert not comms._tail_event_matches_submission(event, STREAM, "caption")
        finally:
            store.stop()
    _run(go())


def test_ingress_discards_producer_supplied_proof_digest(tmp_path: Path) -> None:
    from ingest import append_ingested_event
    from submission_events import DurableUserEventProof, EventWatermark, provider_text_digest
    async def go() -> None:
        comms, store, sessions = _new_comms(FakeTmux(), tmp_path)
        broadcasts = []
        async def broadcast(frame):
            broadcasts.append(frame)
        try:
            await sessions.open(HOST, NAME, provider="claude")
            await append_ingested_event(store, broadcast, {
                "stream_id": STREAM, "kind": "USER", "text": "unrelated text",
                "provider_text_digest": provider_text_digest("forged expected content"),
            }, recent_limit=50)
            tail = await store.fetch_session_event_tail(STREAM, limit=50)
            assert "provider_text_digest" not in tail[0]
            assert "provider_text_digest" not in broadcasts[0]["event"]
            assert not comms._tail_event_matches_submission(tail[0], STREAM, "forged expected content")
            assert not (await DurableUserEventProof(store, local_host=HOST).lookup(
                STREAM, expected_text="forged expected content",
                watermark=EventWatermark(STREAM, 0, "reachable"))).proven
        finally:
            store.stop()
    _run(go())
