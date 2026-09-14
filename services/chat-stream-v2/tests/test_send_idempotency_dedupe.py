"""Idempotent-replay coverage for daemon-v2 send (desktop fourfold-send incident).

Regression for spec_pentacle__desktop_duplicate_send_2026_09: one operator
desktop message produced four provider USER records because the client's retry
path keeps the optimistic_id STABLE and rotates only the request_id, while the
daemon send lane keyed idempotency on request_id alone and "deliberately adds no
dedupe". A retry (rotated request_id) therefore landed a fresh delivery each time.

The fix collapses sends that share the client's logical-message identity
(to_stream_id + actor + optimistic_id + wire_digest) within a short recency
window, and coalesces any re-submission of the same physical request_id at any
age. Intentional identical messages get a fresh optimistic_id (the client counter
increments) and remain deliverable; a post-restart identical send (counter reset
to _1) outside the window is a NEW logical message and is delivered.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_send_semantics import FakeTmux, FakeBlobStore, VALID, _new_comms, _open, HOST, NAME, _run  # noqa: F401
from store import Store, iso_now


STREAM = f"{HOST}:{NAME}"
AUTH = {"operator_principal": "operator:op-1", "operator_authenticated": True}
BODY = "teh bottom space on teh frevenue graph looks off"


async def _open_claude(sessions) -> None:
    await sessions.open(HOST, NAME, provider="claude")


def test_rotated_request_id_retry_collapses_to_one_delivery(tmp_path: Path) -> None:
    """The exact incident: four retry-<ms> request_ids sharing one optimistic_id
    and body must produce ONE physical paste, and every request_id still gets its
    own correlated receipt resolving to the single landed delivery."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            base = {"stream_id": STREAM, "text": BODY,
                    "optimistic_id": "optimistic_bart_v2-d33b331d_1", "_auth_context": AUTH}
            rids = ["retry-1789356725441-i1zgeh", "retry-1789356725967-sh2xt0",
                    "retry-1789356726159-p9dsil", "retry-1789356726334-1r8b8s"]
            results = [await comms.send({**base, "request_id": rid}) for rid in rids]
            assert len(tmux.pastes) == 1, f"expected ONE delivery, got {len(tmux.pastes)}"
            assert results[0]["submission_confirmed"] is True
            # Per-request receipt correlation preserved: each request_id resolves.
            for rid in rids:
                rec = await store.get_send_receipt(STREAM, rid)
                assert rec is not None, f"missing correlated receipt for {rid}"
        finally:
            store.stop()

    _run(go())


def test_concurrent_overlapping_retries_coalesce(tmp_path: Path) -> None:
    """Two overlapping sends of the same logical message must coalesce before
    either finishes admission: only one physical paste."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            base = {"stream_id": STREAM, "text": BODY,
                    "optimistic_id": "optimistic_concurrent_1", "_auth_context": AUTH}
            await asyncio.gather(
                comms.send({**base, "request_id": "retry-a"}),
                comms.send({**base, "request_id": "retry-b"}),
            )
            assert len(tmux.pastes) == 1, f"expected ONE delivery, got {len(tmux.pastes)}"
        finally:
            store.stop()

    _run(go())


async def _preinsert_receipt(store, *, optimistic_id, request_id, state, delivery,
                             created_at, confirmed, actor="operator:op-1", body=BODY):
    await store.append_send_receipt(
        to_stream_id=STREAM, request_id=request_id, receipt_id=f"receipt-{request_id}",
        state=state, wire_text=body, display_text=body, attachments=[],
        delivery=delivery, submission_confirmed=confirmed, optimistic_id=optimistic_id,
        created_at=created_at, actor_stream_id=actor, actor_trusted=True,
    )


def test_new_optimistic_id_same_text_is_delivered(tmp_path: Path) -> None:
    """Intentional identical messages carry a fresh optimistic_id (the client
    counter increments): both must be delivered, no text dedupe."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            await comms.send({"stream_id": STREAM, "text": BODY, "_auth_context": AUTH,
                              "optimistic_id": "optimistic_x_1", "request_id": "send-1"})
            await comms.send({"stream_id": STREAM, "text": BODY, "_auth_context": AUTH,
                              "optimistic_id": "optimistic_x_2", "request_id": "send-2"})
            assert len(tmux.pastes) == 2, f"both intentional sends must deliver, got {len(tmux.pastes)}"
        finally:
            store.stop()

    _run(go())


def test_post_restart_same_text_after_window_is_delivered(tmp_path: Path) -> None:
    """A client restart resets the optimistic counter to _1; an identical-text
    message OUTSIDE the recency window is a NEW logical send and is delivered."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            # A landed receipt from the pre-restart session, long outside the window.
            await _preinsert_receipt(
                store, optimistic_id="optimistic_bart_v2-d33b331d_1",
                request_id="retry-old", state="landed", delivery="landed",
                created_at="2000-01-01T00:00:00Z", confirmed=True)
            result = await comms.send({
                "stream_id": STREAM, "text": BODY, "_auth_context": AUTH,
                "optimistic_id": "optimistic_bart_v2-d33b331d_1", "request_id": "send-new"})
            assert len(tmux.pastes) == 1, "post-restart identical send must be delivered"
            assert not result.get("coalesced")
            assert result["submission_confirmed"] is True
        finally:
            store.stop()

    _run(go())


def test_prior_not_landed_does_not_block_retry(tmp_path: Path) -> None:
    """A genuinely failed (not_landed) prior attempt must NOT suppress a retry."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            await _preinsert_receipt(
                store, optimistic_id="optimistic_y_1", request_id="retry-failed",
                state="not_landed", delivery="not_landed",
                created_at=iso_now(), confirmed=False)
            result = await comms.send({
                "stream_id": STREAM, "text": BODY, "_auth_context": AUTH,
                "optimistic_id": "optimistic_y_1", "request_id": "retry-after-fail"})
            assert len(tmux.pastes) == 1, "retry after a failed attempt must be delivered"
            assert not result.get("coalesced")
        finally:
            store.stop()

    _run(go())


def test_committed_pending_proof_prior_replays_not_resubmit(tmp_path: Path) -> None:
    """A prior committed_pending_proof (submission proof not yet surfaced) is a
    real in-flight delivery: a rotated-request_id retry replays it, no re-paste."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            await _preinsert_receipt(
                store, optimistic_id="optimistic_z_1", request_id="retry-pending",
                state="accepted", delivery="committed_pending_proof",
                created_at=iso_now(), confirmed=False)
            result = await comms.send({
                "stream_id": STREAM, "text": BODY, "_auth_context": AUTH,
                "optimistic_id": "optimistic_z_1", "request_id": "retry-after-pending"})
            assert len(tmux.pastes) == 0, "must not re-paste an in-flight committed send"
            assert result.get("coalesced") is True
            assert result.get("do_not_resubmit") is True
            # The retry's own request_id still resolves, pointing at the replay.
            rec = await store.get_send_receipt(STREAM, "retry-after-pending")
            assert rec is not None and rec["reason"].startswith("coalesced_replay:")
        finally:
            store.stop()

    _run(go())


def test_failed_preclaim_superseded_by_not_landed_allows_retry(tmp_path: Path) -> None:
    """Store-level regression (QA cycle-1 finding): the claim appends an 'accepted'
    row BEFORE materialization; when the send then fails and records a not_landed
    receipt, a rotated retry of the same logical identity must NOT coalesce onto the
    stale accepted preclaim — the latest outcome (not_landed) is authoritative."""
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            kw = dict(to_stream_id=STREAM, wire_text=BODY, display_text=BODY,
                      attachments=[], optimistic_id="optimistic_fail_1",
                      actor_stream_id="operator:op-1", actor_trusted=True,
                      window_floor="2000-01-01T00:00:00Z")
            c1 = await store.claim_or_coalesce_send(request_id="retry-1", receipt_id="rc1", **kw)
            assert c1["coalesced"] is False, "first send must win the claim"
            # Simulate materialize/submit failure: send() appends a not_landed row.
            await store.append_send_receipt(
                to_stream_id=STREAM, request_id="retry-1", receipt_id="rc1",
                state="not_landed", wire_text=BODY, display_text=BODY, attachments=[],
                delivery="not_landed", submission_confirmed=False,
                optimistic_id="optimistic_fail_1", actor_stream_id="operator:op-1",
                actor_trusted=True)
            c2 = await store.claim_or_coalesce_send(request_id="retry-2", receipt_id="rc2", **kw)
            assert c2["coalesced"] is False, "retry after a genuinely failed send must re-deliver"
        finally:
            store.stop()

    _run(go())


def test_attachment_failure_then_recovery_retry_delivers(tmp_path: Path) -> None:
    """End-to-end regression: an attachment send whose blob fetch fails records a
    not_landed receipt (no paste); after the blob recovers, a rotated-request_id
    retry of the same logical send must produce a fresh delivery, not coalesce."""
    async def go() -> None:
        tmux = FakeTmux()
        blobs = FakeBlobStore(error=ValueError("missing blob"))
        comms, store, sessions = _new_comms(tmux, tmp_path, blob_store=blobs)
        try:
            await _open(comms, sessions)  # claude provider
            base = {"stream_id": STREAM, "message": "caption", "attachments": [VALID],
                    "optimistic_id": "optimistic_att_1", "_auth_context": AUTH}
            r1 = await comms.send({**base, "request_id": "retry-att-1"})
            assert r1["delivery"] == "not_landed"
            assert tmux.pastes == [], "failed fetch must not paste"
            # Blob becomes available; operator retries the same logical message.
            blobs.error = None
            r2 = await comms.send({**base, "request_id": "retry-att-2"})
            assert not r2.get("coalesced"), "retry after failure must not coalesce"
            assert r2["delivery"] == "landed"
            assert len(tmux.pastes) == 1, "recovered retry must deliver exactly once"
        finally:
            store.stop()

    _run(go())


def test_mobile_launch_id_scheme_unaffected(tmp_path: Path) -> None:
    """Mobile/launch sends use send-<..> request_ids with distinct
    optimistic_<stream>_launch-<..>_N ids per logical message: each delivers."""
    async def go() -> None:
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open_claude(sessions)
            await comms.send({"stream_id": STREAM, "text": "launch one", "_auth_context": AUTH,
                              "optimistic_id": "optimistic_bart_launch-abc_1", "request_id": "send-l1"})
            await comms.send({"stream_id": STREAM, "text": "launch two", "_auth_context": AUTH,
                              "optimistic_id": "optimistic_bart_launch-abc_2", "request_id": "send-l2"})
            assert len(tmux.pastes) == 2, f"distinct launch sends must each deliver, got {len(tmux.pastes)}"
        finally:
            store.stop()

    _run(go())
