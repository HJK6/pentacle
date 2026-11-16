"""Legacy CHECK migration preserves receipts/indexes and reversible current data."""
import json
import sqlite3

from store import Store
from store_work_lanes import WORK_LANE_EVENTS_DDL, ensure_work_lane_schema
from work_lane_migration import rollback_progress
from test_work_lanes import run

PREVIOUS_DDL = WORK_LANE_EVENTS_DDL.replace("'lead_handoff','set_members','item_change'", "'lead_handoff'")


def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM v2_work_lane_events ORDER BY created_at,event_id")]


def test_migrate_existing_events_replay_confirmation_indexes_and_rollback(tmp_path):
    path = str(tmp_path / "fixture.db")
    ids = {}
    async def seed(env):
        lane = (await env.adopt(state="paused", lead=False, owner="operator", emit_started=True))["lane"]
        ids["lane"] = lane["lane_id"]
        confirmation = env.confirm("fixture-confirmation", lane["lane_id"], "set_owner:fd")
        await env.op("set_owner", {"to": "fd", "operator_confirmation": confirmation},
                     lane=lane["lane_id"], version=1, request_id="owner-receipt")
    run(seed, path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        before = snapshot(conn)
        conn.execute("BEGIN IMMEDIATE")
        rollback_progress(conn, PREVIOUS_DDL)
        conn.commit()
        assert "'set_members'" not in conn.execute("SELECT sql FROM sqlite_master WHERE name='v2_work_lane_events'").fetchone()[0]
        conn.execute("BEGIN IMMEDIATE")
        ensure_work_lane_schema(conn)
        conn.commit()
        assert snapshot(conn) == before
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(v2_work_lane_events)")}
        assert {"idx_v2_work_lane_events_consumed_question", "idx_v2_work_lane_events_lane"} <= indexes
        assert conn.execute("SELECT publication_event_id FROM v2_work_lane_events WHERE event_id='owner-receipt'").fetchone()[0] is not None
        try:
            conn.execute("INSERT INTO v2_work_lane_events SELECT 'duplicate-confirm',lane_id,stream_id,operation,'another-source',"
                         "actor_stream_id,actor_generation,prior_state,next_state,expected_lane_version,payload_json,"
                         "payload_digest,update_kind,update_id,publication_event_id,consumed_question_id,created_at "
                         "FROM v2_work_lane_events WHERE event_id='owner-receipt'")
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            raise AssertionError("confirmation uniqueness lost")
    finally:
        conn.close()
    async def membership(env):
        lane = (await env.store.get_work_lane(ids["lane"]))["lane"]
        payload = {"members": ["spec_demo__unknown"], "no_spec_reason": None}
        result = await env.op("set_members", payload, lane=lane["lane_id"], version=lane["version"], request_id="new-members")
        assert result["duplicate"] is False
        assert (await env.op("set_members", payload, lane=lane["lane_id"], version=lane["version"], request_id="new-members"))["duplicate"]
    run(membership, path)
    conn = sqlite3.connect(path)
    try:
        before = snapshot(conn)
        conn.execute("BEGIN IMMEDIATE")
        assert rollback_progress(conn, PREVIOUS_DDL) == 1
        conn.commit()
        assert conn.execute("SELECT event_id FROM v2_work_lane_events_progress_rollback").fetchone()[0] == "new-members"
        # An unrelated current edit after rollback must survive re-upgrade.
        conn.execute("UPDATE v2_assistant_composite_lanes SET summary='Current operator text' WHERE lane_id=?", (ids["lane"],))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ensure_work_lane_schema(conn)
        conn.commit()
        assert snapshot(conn) == before
        assert conn.execute("SELECT summary FROM v2_assistant_composite_lanes WHERE lane_id=?", (ids["lane"],)).fetchone()[0] == "Current operator text"
    finally:
        conn.close()
