"""Committed send results remain reconcilable for installed clients.

Some installed clients recognize accepted state but not the newer canonical
committed_pending_proof delivery. These checks use the real receipt/dispatch
path; provider input remains isolated behind the existing fake tmux boundary.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_send_semantics import FakeTmux, HOST, NAME, _new_comms, _open


TARGET = f"{HOST}:{NAME}"
AUTH = {"operator_principal": "operator:compat-test", "operator_authenticated": True}


@pytest.mark.parametrize("reason", ["body_maybe_pasted", "enter_failed", "submit_unconfirmed"])
def test_pending_result_exposes_accepted_without_changing_delivery_or_ledger(
    tmp_path: Path, reason: str,
) -> None:
    async def go() -> None:
        comms, store, sessions = _new_comms(FakeTmux(), tmp_path)
        try:
            await _open(comms, sessions)
            plan = await comms._prepare_send_plan({
                "stream_id": TARGET, "request_id": "send-compat-pending",
                "optimistic_id": "optimistic_compat_1", "text": "fixture caption",
                "_auth_context": AUTH,
            })
            result = await comms._send_result(
                plan, request_id="send-compat-pending", receipt_id="receipt-compat",
                state="accepted", delivery="committed_pending_proof",
                submission_confirmed=False, reason=reason, attempts=1,
                action_committed=True, confirmation_pending=True,
            )
            assert result.get("state") == "accepted"
            assert result["delivery"] == "committed_pending_proof"
            assert result["submission_confirmed"] is False
            assert result["action_committed"] is True
            assert result["confirmation_pending"] is True
            assert result["do_not_resubmit"] is True
            assert result["reason"] == reason
            assert result["attempts"] == 1
            receipt = await store.get_send_receipt(TARGET, "send-compat-pending")
            assert receipt is not None
            assert (receipt["state"], receipt["delivery"], receipt["submission_confirmed"]) == (
                "accepted", "committed_pending_proof", False,
            )
            assert receipt["optimistic_id"] == "optimistic_compat_1"
            assert receipt["receipt_id"] == "receipt-compat"
        finally:
            store.stop()
    asyncio.run(go())


def test_same_request_pending_replay_exposes_accepted_without_second_dispatch(tmp_path: Path) -> None:
    async def go() -> None:
        tmux = FakeTmux(fail_phase="body_maybe_pasted")
        comms, store, sessions = _new_comms(tmux, tmp_path)
        try:
            await _open(comms, sessions)
            msg = {
                "stream_id": TARGET, "request_id": "send-compat-replay",
                "optimistic_id": "optimistic_compat_replay_1", "text": "fixture caption",
                "_auth_context": AUTH,
            }
            first = await comms.send(msg)
            replay = await comms.send(msg)
            assert len(tmux.pastes) == 1
            assert replay["coalesced"] is True
            for result in (first, replay):
                assert result.get("state") == "accepted"
                assert result["delivery"] == "committed_pending_proof"
                assert result["submission_confirmed"] is False
                assert result["do_not_resubmit"] is True
            receipt = await store.get_send_receipt(TARGET, msg["request_id"])
            assert receipt is not None and receipt["state"] == "accepted"
            assert receipt["delivery"] == "committed_pending_proof"
            assert receipt["submission_confirmed"] is False
            assert receipt["reason"] == "coalesced_replay:send-compat-replay"
        finally:
            store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("delivery", ["landed", "not_landed"])
def test_terminal_result_controls_keep_their_existing_shape(tmp_path: Path, delivery: str) -> None:
    async def go() -> None:
        comms, store, sessions = _new_comms(FakeTmux(), tmp_path)
        try:
            await _open(comms, sessions)
            plan = await comms._prepare_send_plan({"stream_id": TARGET, "text": "control"})
            result = await comms._send_result(
                plan, request_id="send-compat-control", receipt_id="receipt-control",
                state=delivery, delivery=delivery, submission_confirmed=delivery == "landed",
            )
            assert "state" not in result
            assert result["delivery"] == delivery
            assert result["submission_confirmed"] is (delivery == "landed")
            assert "do_not_resubmit" not in result
        finally:
            store.stop()
    asyncio.run(go())
