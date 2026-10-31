"""Native tell proof records the original delivery without a caller replay."""
import asyncio
from types import SimpleNamespace

import pytest

import comms as comms_module
from codex_rollout_norm import normalize_codex_rollout_record
from provider_cli_initial import input_obligations
from test_tell_late_proof_controls import setup


@pytest.mark.parametrize("ingress", ["single", "batch"])
@pytest.mark.parametrize("case", ["matching", "wrong-id", "wrong-text", "wrong-generation", "missing-anchor", "absent"])
def test_ingest_records_only_exact_original_tell_proof(monkeypatch, ingress, case):
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)

    async def run():
        store, sessions, comms, tmux, request = await setup()
        sender = "localhost:v2-sender"
        request.update(from_stream_id=sender, _auth_context={"stream_id": sender, "token_verified": True})
        try:
            first = await comms.tell(request)
            assert first["delivery_status"] == "committed_pending_proof"
            original = await store.get_tell_delivery(request["tell_id"])
            if case in {"missing-anchor", "wrong-generation"}:
                def change(conn):
                    import json
                    value = json.loads(conn.execute("SELECT reply FROM v2_tell_deliveries WHERE tell_id=?",
                        (request["tell_id"],)).fetchone()[0])
                    if case == "missing-anchor": value["delivery"].pop("proof_generation")
                    else: value["delivery"]["proof_generation"] = "different-generation"
                    conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?",
                                 (json.dumps(value), request["tell_id"]))
                    conn.commit()
                await store.submit(change)
            wire = tmux.pastes[0]
            if case == "wrong-id": wire = wire.replace(request["tell_id"], "other-tell-id")
            if case == "wrong-text": wire = wire.replace(request["message"], "different input")
            events = normalize_codex_rollout_record({"type": "response_item", "timestamp": "2026-10-31T22:00:00Z",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": wire}]}}, host="localhost",
                session_name="v2-late-proof", session_id="native-session")
            event = events[0]
            assert event["kind"] == "TELL"
            async def ingest():
                if ingress == "single":
                    return await store.append_session_event(request["stream_id"], event,
                        identity="native-exact-input", limit=500)
                return (await store.append_session_events_lifecycle_cas([{"stream_id": request["stream_id"],
                    "event": event, "identity": "native-exact-input"}], limit=500))[0]
            if case != "absent":
                seq = await ingest()
                assert seq
            saved = await store.get_tell_delivery(request["tell_id"])
            obligations = await store.submit(lambda conn: input_obligations(SimpleNamespace(conn=conn)))
            if case == "matching":
                assert saved["reply"]["delivery_status"] == "delivered"
                assert not obligations
                assert saved["reply"]["proof_event_id"] == seq
                assert saved["ledger_row_id"] == original["ledger_row_id"]
                assert await ingest() is None
                assert await store.get_tell_delivery(request["tell_id"]) == saved
            else:
                assert saved["reply"]["delivery_status"] == "committed_pending_proof"
                assert len(obligations) == 1
            replay = await comms.tell(request)
            assert replay["duplicate"]
            assert len(tmux.pastes) == 1 and tmux.enter_only == 0
        finally:
            store.stop()
    asyncio.run(run())
