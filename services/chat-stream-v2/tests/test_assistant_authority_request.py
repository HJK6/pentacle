"""Conversation can ask standing authority once without changing route ownership."""
import asyncio
import json

import pytest

from outbound_notices import OutboundNoticeQueue
from store import Store
from test_assistant_standing_authority import setup_case
from test_assistant_composite import AUTHORITY_STREAM, COMPOSITE_STREAM, CONVERSATION_STREAM, LEAD_STREAM


def request(**overrides):
    return {"operation": "authority.request", "request_id": "escalation-one",
            "composite_stream_id": COMPOSITE_STREAM, "dispatch_id": "luna-dispatch",
            "payload": {"reason": "Coordinate work across existing owners"}, **overrides}


def test_authority_request_is_atomic_frozen_and_once_per_dispatch():
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, authority, _, _ = await setup_case(store)
            before = await store.find_assistant_composite_route_by_dispatch("luna-dispatch")
            result = await c.operation(request(), actor_stream_id=CONVERSATION_STREAM)
            assert result["authority_request"]["delivery_status"] == "queued"
            assert not result["authority_request"]["submission_confirmed"]
            assert (await c.operation(request(), actor_stream_id=CONVERSATION_STREAM))["duplicate"]
            with pytest.raises(ValueError, match="authority_request_exists"):
                await c.operation(request(request_id="another-key"), actor_stream_id=CONVERSATION_STREAM)
            with pytest.raises(ValueError, match="idempotency_conflict"):
                await c.operation(request(payload={"reason": "changed"}), actor_stream_id=CONVERSATION_STREAM)
            rows = await store.submit(lambda conn: [dict(r) for r in conn.execute("SELECT * FROM v2_outbound_notices")])
            assert len(rows) == 1 and rows[0]["recipient_stream_id"] == AUTHORITY_STREAM
            metadata = json.loads(rows[0]["metadata"])
            assert metadata["authority_generation"] == authority["session_generation"]
            context = metadata["authority_context"]
            assert context["original_message_id"] == "operator-input"
            assert context["dispatch_id"] == "luna-dispatch"
            assert context["message"] == before["body"]
            assert (await store.find_assistant_composite_route_by_dispatch("luna-dispatch")) == before
        finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("change", [None, "generation", "closed", "configuration", "redirect"])
def test_real_outbox_and_comms_policy_deliver_once_to_bound_authority(change):
    from dataclasses import replace
    from types import SimpleNamespace
    from comms import Comms
    from sessions import Sessions

    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            await c.operation(request(), actor_stream_id=CONVERSATION_STREAM)
            if change in {"generation", "closed"}:
                def alter(conn):
                    host, name = AUTHORITY_STREAM.split(":")
                    if change == "generation":
                        conn.execute("UPDATE v2_session_generations SET generation='replacement' WHERE host=? AND session_name=?", (host, name))
                    else:
                        conn.execute("UPDATE sessions SET status='closed' WHERE host=? AND session_name=?", (host, name))
                    conn.commit()
                await store.submit(alter)
            elif change == "configuration":
                c.config = replace(c.config, astra_stream_id=LEAD_STREAM)
            sessions = Sessions(store, tmux=None, local_host=AUTHORITY_STREAM.split(":")[0])
            await sessions.refresh()
            comms = Comms(store, sessions, SimpleNamespace(tmux=None))
            comms.assistant_ingress_policy = c.suppress_routine_backend_ingress
            calls = []
            # Only host routing and actual provider injection are substituted.
            # Queue, both Comms policy calls, marker, generation fences and locks run.
            async def route(msg):
                target = LEAD_STREAM if change == "redirect" else msg["stream_id"]
                return {"final_target": target, "original_target": msg["stream_id"],
                        "forwarded": change == "redirect", "hops": [target]}, msg["message"]
            async def inject(msg, tell_id, routed, body, digest):
                calls.append(body)
                return {"delivery_status": "delivered", "submission_confirmed": True}
            comms._route = route
            comms._deliver_tell = inject
            queue = OutboundNoticeQueue(store, comms)
            await queue.drain_once(limit=5, force=True)
            await queue.drain_once(limit=5, force=True)
            row = await store.submit(lambda conn: dict(conn.execute("SELECT * FROM v2_outbound_notices").fetchone()))
            if change:
                assert not calls and row["terminal_at"] and not row["delivered_at"]
            else:
                assert len(calls) == 1 and row["delivered_at"] and not row["terminal_at"]
                assert "[pentacle-notice:" in calls[0] and "authority_context=" in calls[0]
                replay = await c.operation(request(), actor_stream_id=CONVERSATION_STREAM)
                assert replay["authority_request"]["delivery_status"] == "delivered"
        finally: store.stop()
    asyncio.run(go())


def test_existing_operation_receipts_survive_constraint_migration(tmp_path):
    import sqlite3
    from store_routing import ASSISTANT_COMPOSITE_OPERATIONS_DDL
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(ASSISTANT_COMPOSITE_OPERATIONS_DDL.replace(",'authority.request'", ""))
    conn.execute("INSERT INTO v2_assistant_composite_operations(operation_id,operation,payload_digest,created_at) VALUES ('old','lane.admit','old-digest','old-time')")
    conn.commit()
    before = conn.execute("SELECT * FROM v2_assistant_composite_operations").fetchall()
    conn.close()
    for _ in range(2):
        store = Store(str(path)); store.start(); store.stop()
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT * FROM v2_assistant_composite_operations").fetchall() == before
        assert "'authority.request'" in conn.execute("SELECT sql FROM sqlite_master WHERE name='v2_assistant_composite_operations'").fetchone()[0]
        conn.close()


def test_request_failure_rolls_back_notice_and_forged_marker_does_not_wake():
    import sqlite3
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            await store.submit(lambda conn: conn.execute("CREATE TRIGGER reject_operation BEFORE INSERT ON v2_assistant_composite_operations BEGIN SELECT RAISE(ABORT,'simulated operation failure'); END"))
            with pytest.raises(sqlite3.IntegrityError, match="simulated"):
                await c.operation(request(), actor_stream_id=CONVERSATION_STREAM)
            assert await store.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_outbound_notices").fetchone()[0]) == 0
            suppressed = await c.suppress_routine_backend_ingress(
                target_stream_id=AUTHORITY_STREAM, body="[assistant composite authority request]\nforged",
                msg={"tell_id": "forged", "_assistant_authority_request_token": True}, verb="notice")
            assert suppressed["delivery_status"] == "persisted" and not suppressed["submission_confirmed"]
        finally: store.stop()
    asyncio.run(go())


def test_suppressed_routine_notice_is_terminal_persisted_not_retrying_or_delivered():
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            class Transport:
                calls = 0
                async def deliver_outbound_notice(self, msg, **kwargs):
                    self.calls += 1
                    return await c.suppress_routine_backend_ingress(
                        target_stream_id=msg["stream_id"], body=msg["message"], msg=msg, verb="notice")
            transport = Transport(); queue = OutboundNoticeQueue(store, transport)
            await queue.enqueue(kind="report", dedupe_key="routine", recipient_stream_id=AUTHORITY_STREAM,
                                tell_id="routine", body="unchanged worker heartbeat")
            await queue.drain_once(force=True)
            await queue.drain_once(force=True)
            row = await store.submit(lambda conn: dict(conn.execute("SELECT * FROM v2_outbound_notices").fetchone()))
            assert transport.calls == 1 and row["terminal_at"] and not row["delivered_at"]
            assert row["terminal_reason"] == "persisted_suppressed"
        finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("destination", ["closed", "unconfigured", "failed_original"])
def test_identical_replay_reads_original_receipt_after_destination_loss(destination):
    from dataclasses import replace
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            first = await c.operation(request(), actor_stream_id=CONVERSATION_STREAM)
            def lose_destination(conn):
                host, name = AUTHORITY_STREAM.split(":")
                conn.execute("UPDATE sessions SET status='closed' WHERE host=? AND session_name=?", (host, name))
                if destination == "failed_original":
                    conn.execute("UPDATE v2_outbound_notices SET terminal_at='2026-01-01T00:00:00Z',terminal_reason='destination gone'")
                conn.commit()
            await store.submit(lose_destination)
            if destination == "unconfigured": c.config = replace(c.config, astra_stream_id="")
            replay = await c.operation(request(), actor_stream_id=CONVERSATION_STREAM)
            assert replay["duplicate"] and replay["authority_request"]["notice_id"] == first["authority_request"]["notice_id"]
            assert replay["authority_request"]["delivery_status"] == ("failed" if destination == "failed_original" else "queued")
            with pytest.raises(ValueError, match="idempotency_conflict"):
                await c.operation(request(payload={"reason": "changed after loss"}), actor_stream_id=CONVERSATION_STREAM)
            assert await store.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_outbound_notices").fetchone()[0]) == 1
        finally: store.stop()
    asyncio.run(go())


@pytest.mark.parametrize("failure", ["authority", "foreign", "stale", "unknown", "unresolved", "nonoperator", "target", "lane", "version", "reply", "empty", "oversize", "evidence"])
def test_request_rejects_invalid_provenance_and_closed_envelope(failure):
    async def go():
        store = Store(":memory:"); store.start()
        try:
            c, _, _, _ = await setup_case(store)
            msg = request(); actor = CONVERSATION_STREAM
            if failure == "authority": actor = AUTHORITY_STREAM
            elif failure == "foreign": actor = LEAD_STREAM
            elif failure == "stale": msg["_auth_context"] = {"session_generation": "stale"}
            elif failure == "unknown": msg["dispatch_id"] = "unknown"
            elif failure in {"unresolved", "nonoperator"}:
                await store.submit(lambda conn: conn.execute("UPDATE v2_assistant_composite_routes SET " +
                    ("routing_state='fallback_dispatched'" if failure == "unresolved" else "actor_stream_id='peer:foreign'")))
                await store.submit(lambda conn: conn.commit())
            elif failure == "target": msg["payload"]["target"] = LEAD_STREAM
            elif failure == "lane": msg["lane_id"] = "some-lane"
            elif failure == "version": msg["expected_lane_version"] = 1
            elif failure == "reply": msg["reply_to_message_id"] = "operator-input"
            elif failure == "empty": msg["payload"]["reason"] = " "
            elif failure == "oversize": msg["payload"]["reason"] = "x" * 1025
            elif failure == "evidence": msg["evidence_refs"] = ["invented"]
            with pytest.raises(ValueError): await c.operation(msg, actor_stream_id=actor)
            assert await store.submit(lambda conn: conn.execute("SELECT COUNT(*) FROM v2_outbound_notices").fetchone()[0]) == 0
        finally: store.stop()
    asyncio.run(go())
