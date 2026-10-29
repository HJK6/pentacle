"""Frozen ab74aff reads and recovery updates over the Phase 1 schema."""

import asyncio
import json

from store import Store


STREAM = "fixture-chat:assistant"
SOURCE = "fixture-root:visible"
GENERATION = "fixture-generation"

# These statements are copied verbatim from ab74aff store.py and store_routing.py.
OLD_OPEN_ROUTE = (
    "SELECT 1 FROM v2_assistant_composite_routes r "
    "WHERE r.stream_id=? AND r.routing_state='resolved' "
    "AND r.route_target=? AND r.route_target_generation=? "
    "AND r.dispatch_id IS NOT NULL "
    "AND r.delivery_state IN ('intent','committed_pending','uncertain','landed') "
    "AND json_extract(r.route_json,'$.admission_mode')='direct_primary' "
    "AND NOT EXISTS (SELECT 1 FROM v2_assistant_composite_publications p "
    "WHERE p.stream_id=r.stream_id AND p.dispatch_id=r.dispatch_id "
    "AND p.publish_kind='prose' "
    "AND json_extract(p.canonical_payload_json,'$.response_state')='final') "
    "LIMIT 1"
)
OLD_WATERMARK = (
    "SELECT DISTINCT json_extract(canonical_payload_json,'$.mirrored_from.event_ts') "
    "FROM v2_assistant_composite_publications "
    "WHERE stream_id=? AND publish_kind='status' AND dispatch_id='' "
    "AND json_extract(canonical_payload_json,'$.mirrored_from.stream_id')=? "
    "AND json_extract(canonical_payload_json,'$.mirrored_from.generation')=?"
)
OLD_ACTIVITY_LIVE = """ AND (r.route_id IN (SELECT route_id FROM v2_assistant_composite_routes WHERE stream_id=? ORDER BY created_at DESC LIMIT 2000)
                    OR (r.routing_state!='routing_failed' AND COALESCE(r.delivery_state,'')!='failed' AND NOT EXISTS
                        (SELECT 1 FROM v2_assistant_composite_publications p WHERE p.stream_id=r.stream_id AND p.reply_to_message_id=r.input_identity))
                    OR (EXISTS (SELECT 1 FROM v2_assistant_composite_publications p WHERE p.stream_id=r.stream_id AND p.reply_to_message_id=r.input_identity AND json_extract(p.canonical_payload_json,'$.response_state')='acknowledged')
                        AND NOT EXISTS (SELECT 1 FROM v2_assistant_composite_publications p WHERE p.stream_id=r.stream_id AND p.reply_to_message_id=r.input_identity AND (p.publish_kind='result' OR json_extract(p.canonical_payload_json,'$.response_state')='final'))))"""
OLD_RECOVER_CLASSIFY = """UPDATE v2_assistant_composite_routes
                       SET routing_state='queued', lease_owner=NULL, lease_until=NULL,
                           error_code='recovered_classification', updated_at=?
                       WHERE stream_id=? AND routing_state='classifying'"""
OLD_RECOVER_DELIVERY = """UPDATE v2_assistant_composite_routes
                       SET delivery_state='uncertain',
                           error_code='recovery_delivery_evidence_required',
                           lease_owner=NULL, lease_until=NULL, updated_at=?
                       WHERE stream_id=?
                         AND routing_state IN ('resolved','fallback_dispatched')
                         AND delivery_state IN ('intent','committed_pending')"""
OLD_MIRROR_LOOKUP = (
    "SELECT t.event_id,t.event_json FROM v2_assistant_composite_publications p "
    "JOIN session_event_tail t ON t.event_id=p.event_id "
    "WHERE p.publication_key=? AND p.publish_kind='status' AND p.dispatch_id=''"
)


def test_ab74aff_queries_over_new_schema():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            def check(conn):
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'v2_assistant_composite_%'")}
                assert {"v2_assistant_composite_publication_links",
                        "v2_assistant_composite_recovery_runs",
                        "v2_assistant_composite_recovery_audit"} <= tables
                conn.execute(
                    "INSERT INTO v2_assistant_composite_routes "
                    "(route_id,stream_id,input_identity,payload_digest,input_request_id,body,"
                    "routing_state,delivery_state,dispatch_id,route_target,route_target_generation,"
                    "route_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("route-covered", STREAM, "covered-id", "digest", "request", "question",
                     "resolved", "landed", "dispatch-covered", SOURCE, GENERATION,
                     json.dumps({"admission_mode": "direct_primary",
                                 "landed_by_publication": "publication", "landed_evidence": "own_final",
                                 "landed_at": "2026-10-01T00:00:00Z", "prior_delivery_state": "intent",
                                 "prior_error_code": None, "recovered_by_run": "fixture-run"}),
                     "2026-10-01T00:00:00Z", "2026-10-01T00:00:00Z"))
                conn.execute(
                    "INSERT INTO v2_assistant_composite_publication_links "
                    "(stream_id,covered_message_id,publication_key,covered_dispatch_id,link_kind,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (STREAM, "covered-id", "publication", "dispatch-covered", "burst",
                     "2026-10-01T00:00:00Z"))
                assert conn.execute(OLD_OPEN_ROUTE, (STREAM, SOURCE, GENERATION)).fetchone() is not None
                assert list(conn.execute(OLD_WATERMARK, (STREAM, SOURCE, GENERATION))) == []
                assert [r[0] for r in conn.execute(
                    "SELECT r.input_identity FROM v2_assistant_composite_routes r WHERE r.stream_id=?" +
                    OLD_ACTIVITY_LIVE, (STREAM, STREAM))] == ["covered-id"]
                assert conn.execute(OLD_MIRROR_LOOKUP, ("mirror:0",)).fetchone() is None
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                assert conn.execute(OLD_RECOVER_CLASSIFY, ("2026-10-02T00:00:00Z", STREAM)).rowcount == 0
                assert conn.execute(OLD_RECOVER_DELIVERY, ("2026-10-02T00:00:00Z", STREAM)).rowcount == 0
                conn.commit()
                state = conn.execute("SELECT delivery_state FROM v2_assistant_composite_routes "
                                     "WHERE route_id='route-covered'").fetchone()[0]
                assert state == "landed"
            await store.submit(check)
        finally:
            store.stop()
    asyncio.run(run())
