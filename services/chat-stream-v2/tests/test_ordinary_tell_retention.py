"""Unresolved ordinary tells retain their original no-second-paste custody."""
import asyncio

import pytest

import store as store_module
from comms import Comms
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_tell_late_proof_controls import append, setup


@pytest.mark.parametrize("state", ["committed_pending_proof", "proof_pending", "proof_unavailable",
    "pasted_unsubmitted", "pending_input", "persisted_suppressed"])
def test_unresolved_rows_survive_retention_while_delivered_rows_expire(monkeypatch, state):
    monkeypatch.setattr(store_module, "TELL_RETENTION", 8)

    async def run():
        store = Store(":memory:"); store.start()
        try:
            envelope = {"payload_digest": "original-digest", "reply": {"tell_id": "pending",
                "delivery_status": state}, "delivery": {"tell_id": "pending", "text": "original payload",
                "delivery_status": state, "proof_generation": "original-generation",
                "proof_session_created_at": "original-birth", "proof_watermark": 17,
                "proof_watermark_state": "reachable", "request_payload_hash": "original-digest"}}
            await store.put_tell_delivery("pending", envelope)
            original = await store.get_tell_delivery("pending")
            await store.put_tell_delivery("inconsistent", {"reply": {"delivery_status": state},
                "delivery": {"delivery_status": "delivered"}})
            await store.put_tell_delivery("old-delivered", {"reply": {"delivery_status": "delivered"}})
            for i in range(10):
                await store.put_tell_delivery(f"new-{i}", {"reply": {"delivery_status": "delivered"}})
            assert await store.get_tell_delivery("old-delivered") is None
            assert await store.get_tell_delivery("new-9") is not None
            assert await store.get_tell_delivery("pending") == original
            assert await store.get_tell_delivery("inconsistent") is not None
        finally:
            store.stop()
    asyncio.run(run())


def test_retention_restart_and_same_id_reconcile_preserve_exact_custody(monkeypatch, tmp_path):
    monkeypatch.setattr(store_module, "TELL_RETENTION", 8)
    import comms as comms_module
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def run():
        path = tmp_path / "retention.db"
        store, sessions, comms, tmux, request = await setup(path)
        try:
            assert (await comms.tell(request))["delivery_status"] == "committed_pending_proof"
            original = await store.get_tell_delivery(request["tell_id"])
            for i in range(10):
                await store.put_tell_delivery(f"new-{i}", {"reply": {"delivery_status": "delivered"}})
            assert await store.get_tell_delivery(request["tell_id"]) == original
        finally:
            store.stop()
        store = Store(str(path)); store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="localhost"); await sessions.refresh()
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            assert await store.get_tell_delivery(request["tell_id"]) == original
            assert (await comms.tell(request))["delivery_status"] == "committed_pending_proof"
            await append(store, request["stream_id"], tmux.pastes[0])
            replay = await comms.tell(request)
            saved = await store.get_tell_delivery(request["tell_id"])
            assert replay["duplicate"] and replay["delivery_status"] == "delivered"
            assert replay["ledger_row_id"] == original["ledger_row_id"]
            for key in ("tell_id", "text", "proof_generation", "proof_session_created_at",
                        "proof_watermark", "proof_watermark_state", "request_payload_hash"):
                assert saved["delivery"][key] == original["delivery"][key]
            assert saved["payload_digest"] == original["payload_digest"]
            assert (await comms.tell(request))["proof_event_id"] == replay["proof_event_id"]
            assert len(tmux.pastes) == 1 and tmux.enter_only == 0
        finally:
            store.stop()
    asyncio.run(run())
