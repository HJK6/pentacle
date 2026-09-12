"""Deterministic lifecycle barriers around real QA send admission."""

import asyncio
import pytest
import qa_dispatch
from sessions import VerbError
from test_qa_dispatch_counter import LEAD, SPEC, state, send_msg, report_msg, issue


def test_send_commission_binds_generation_current_at_commission(monkeypatch):
    """A lifecycle change before qa_admit must not persist the stale generation."""
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, _, comms, ledger, server = await state()
        original = qa_dispatch.admit
        raced = False

        async def reopen_before_admit(store_arg, msg, **kwargs):
            nonlocal raced
            if kwargs.get("reviewer") == "localhost:qa1" and not raced:
                raced = True
                await store.update_session("localhost", "qa1", status="closed")
                await sessions.open(
                    "localhost",
                    "qa1",
                    role="qa",
                    provider="claude",
                    parent_stream_id=LEAD,
                    spec_id=SPEC,
                    spec_ids=[SPEC],
                    bootstrap_state="ready",
                    session_generation="reopened-reviewer",
                )
            return await original(store_arg, msg, **kwargs)

        monkeypatch.setattr(qa_dispatch, "admit", reopen_before_admit)
        try:
            delivered = await comms.send(send_msg("qa1"))
            assert delivered["delivery"] == "landed"
            current = await store.fetch_session("localhost", "qa1")
            commission = (await issue(server, "show"))["commissions"][0]
            assert commission["generation"] == current["session_generation"], {
                "commission_generation": commission["generation"],
                "delivered_generation": current["session_generation"],
            }
            await ledger.ingest(report_msg("qa1"))
            adjudicated = await issue(
                server,
                "adjudicate",
                report_id="reject-qa1",
                adjudicated_valid=True,
                reason="current-generation review",
            )
            assert adjudicated["type"].endswith(".ok"), adjudicated
        finally:
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["closed", "spec"])
def test_send_revalidates_target_in_commission_transaction(monkeypatch, change):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, _, tmux, comms, _, server = await state()
        original = qa_dispatch.admit

        async def change_before_admit(store_arg, msg, **kwargs):
            fields = (
                {"status": "closed"}
                if change == "closed"
                else {"spec_ids": ["spec_other"], "spec_id": "spec_other"}
            )
            await store.update_session("localhost", "qa1", **fields)
            return await original(store_arg, msg, **kwargs)

        monkeypatch.setattr(qa_dispatch, "admit", change_before_admit)
        try:
            with pytest.raises(VerbError):
                await comms.send(send_msg("qa1"))
            assert not tmux.pastes
            assert not (await issue(server, "show"))["commissions"]
        finally:
            store.stop()

    asyncio.run(run())


def test_send_reopened_after_admission_refuses_before_input(monkeypatch):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, tmux, comms, _, _ = await state()
        original = qa_dispatch.admit

        async def reopen_after_admit(store_arg, msg, **kwargs):
            result = await original(store_arg, msg, **kwargs)
            await store.update_session("localhost", "qa1", status="closed")
            await sessions.open(
                "localhost",
                "qa1",
                role="qa",
                provider="claude",
                parent_stream_id=LEAD,
                spec_id=SPEC,
                spec_ids=[SPEC],
                bootstrap_state="ready",
                session_generation="later-reviewer",
            )
            return result

        monkeypatch.setattr(qa_dispatch, "admit", reopen_after_admit)
        try:
            result = await comms.send(send_msg("qa1"))
            assert result["delivery"] == "not_landed", result
            assert result["reason"] == "qa_reviewer_generation_conflict", result
            assert not tmux.pastes
        finally:
            store.stop()

    asyncio.run(run())
