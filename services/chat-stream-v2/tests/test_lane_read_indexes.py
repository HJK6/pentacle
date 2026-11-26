"""Lane reads find observations and routes by index, not by decoding every row (R-020).

Each test traces the real statement, reads its plan, and proves the statement
returns the same rows with and without its index (the R-018 pattern).
"""
import asyncio
import json

from store import Store
from store_work_index import effective_membership_ids_conn
from store_work_lane_episodes import lead_reported_done_conn, resolved_route_dispatch_map
from store_work_lanes import unblocks_conn, work_lane_rows_conn

LANES = ("lane-a", "lane-b")
STREAM = "fixture:assistant"


def _observation(spec_id, epic=None, status="active", blocked_by=None, title="Item"):
    last_good = {} if epic is None else {"_epic": epic}
    if blocked_by is not None:
        last_good["_blocked_by"] = blocked_by
    return spec_id, json.dumps({"member": {"spec_id": spec_id, "status": status, "title": title,
                                           "observation": {"quality": "fresh"}}, "last_good": last_good})


OBSERVATIONS = dict((
    _observation("spec_a__one", "epic_a"),
    _observation("spec_a__two", "epic_a", status="blocked", blocked_by=["spec_b__one", "spec_a__one"]),
    _observation("spec_b__one", "epic_b", status="blocked", blocked_by="spec_a__one"),
    _observation("spec_c__numeric", 7),
    _observation("spec_c__null", None, status="blocked", blocked_by=["spec_a__two"]),
    _observation("spec_c__unscoped", "epic_elsewhere", status="done"),
))


def _seed(conn):
    conn.execute("INSERT INTO v2_work_index_state VALUES (1,?)", (json.dumps({
        "available": True, "goal_observations": {
            "epic_a": {"facts": {"lane_id": "lane-a"}, "quality": "fresh"},
            "epic_b": {"facts": {"lane_id": "lane-b"}, "quality": "fresh"},
            "epic_gone": {"facts": {"lane_id": "lane-missing"}, "quality": "fresh"}}}),))
    conn.executemany("INSERT INTO v2_work_item_observations VALUES (?,?)", OBSERVATIONS.items())
    for lane_id in LANES:
        conn.execute("INSERT INTO v2_assistant_composite_lanes (lane_id,stream_id,phase,summary,version,created_at,"
                     "updated_at,work_state,owner_kind,title,members_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (lane_id, STREAM, "execution", "", 1, "2026-11-26T00:00:00Z", "2026-11-26T00:00:00Z",
                      "active", "fd", lane_id, json.dumps(["spec_c__unscoped"])))
    routes = (("r1", "resolved", {"lane_id": "lane-a"}), ("r2", "resolved", {"lane_id": "lane-b"}),
              ("r3", "queued", {"lane_id": "lane-a"}), ("r4", "resolved", None), ("r5", "resolved", {}),
              ("r6", "resolved", {"lane_id": "lane-a"}))
    for n, (route_id, state, route) in enumerate(routes):
        conn.execute("INSERT INTO v2_assistant_composite_routes (route_id,stream_id,input_identity,payload_digest,"
                     "input_request_id,body,routing_state,dispatch_id,route_json,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (route_id, STREAM if route_id != "r6" else "other:assistant", f"in-{n}", "d", f"q-{n}", "b",
                      state, f"dispatch-{route_id}", "" if route is None else json.dumps(route),
                      "2026-11-26T00:00:00Z", "2026-11-26T00:00:00Z"))
    # A lane.admit receipt binds r4's dispatch to lane-b although its route names no lane.
    conn.execute("INSERT INTO v2_assistant_composite_operations (operation_id,stream_id,dispatch_id,lane_id,"
                 "operation,payload_digest,created_at) VALUES ('op-1',?,'dispatch-r4','lane-b','lane.admit','d',"
                 "'2026-11-26T00:00:00Z')", (STREAM,))
    conn.commit()


def _traced(conn, read):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        result = read()
    finally:
        conn.set_trace_callback(None)
    return result, statements


def _plan(conn, statements, marker):
    statement = next(s for s in statements if marker in s)
    return [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statement)]


def _per_lane_routes(conn, lane_id):
    return lead_reported_done_conn(conn, {"stream_id": STREAM, "lane_id": lane_id, "completion_report_id": None})


def _with_and_without(index, read, marker):
    def run(conn):
        _seed(conn)
        indexed, statements = _traced(conn, lambda: read(conn))
        plan = _plan(conn, statements, marker)
        conn.execute(f"DROP INDEX {index}")
        return indexed, read(conn), plan

    async def go():
        store = Store(":memory:"); store.start()
        try:
            return await store.submit(run)
        finally:
            store.stop()
    return asyncio.run(go())


def test_epic_membership_read_uses_the_partial_epic_index_and_resolves_the_same_lanes():
    indexed, unindexed, plan = _with_and_without(
        "v2_work_item_observations_epic", effective_membership_ids_conn, "$.last_good._epic")
    assert plan == ["SCAN v2_work_item_observations USING INDEX v2_work_item_observations_epic"], plan
    assert indexed == unindexed
    assert indexed["assigned"] == {"spec_a__one": "lane-a", "spec_a__two": "lane-a", "spec_b__one": "lane-b"}


def test_unblocks_read_searches_the_status_index_and_returns_the_same_entries():
    indexed, unindexed, plan = _with_and_without(
        "v2_work_item_observations_status", unblocks_conn, "$.member.status")
    assert plan[0] == "SEARCH v2_work_item_observations USING INDEX v2_work_item_observations_status (<expr>=?)", plan
    assert indexed == unindexed
    assert set(indexed) == {"spec_b__one", "spec_a__one", "spec_a__two"}


def test_single_lane_route_read_searches_the_lane_index_and_matches_the_pass_map():
    def run(conn):
        _seed(conn)
        route_map = resolved_route_dispatch_map(conn)
        statements = {lane_id: next(s for s in _traced(conn, lambda: _per_lane_routes(conn, lane_id))[1]
                                    if "AS r WHERE r.stream_id=" in s) for lane_id in LANES}
        plans = {[row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + s)][0] for s in statements.values()}
        indexed = {lane_id: {r[0] for r in conn.execute(s)} for lane_id, s in statements.items()}
        conn.execute("DROP INDEX idx_v2_assistant_composite_routes_lane")
        unindexed = {lane_id: {r[0] for r in conn.execute(s)} for lane_id, s in statements.items()}
        return route_map, plans, indexed, unindexed

    async def go():
        store = Store(":memory:"); store.start()
        try:
            return await store.submit(run)
        finally:
            store.stop()
    route_map, plans, indexed, unindexed = asyncio.run(go())
    assert plans == {"SEARCH r USING INDEX idx_v2_assistant_composite_routes_lane (stream_id=? AND routing_state=?)"}
    assert indexed == unindexed == {"lane-a": {"dispatch-r1"}, "lane-b": {"dispatch-r2", "dispatch-r4"}}
    # The per-lane statement associates the same resolved dispatches as the multi-lane pass map.
    assert indexed == {lane_id: route_map.get((STREAM, lane_id), set()) for lane_id in LANES}


def test_a_single_lane_read_runs_no_global_route_scan():
    def run(conn):
        _seed(conn)
        _, one = _traced(conn, lambda: work_lane_rows_conn(conn, lane_id="lane-a"))
        _, every = _traced(conn, lambda: work_lane_rows_conn(conn))
        return one, every

    async def go():
        store = Store(":memory:"); store.start()
        try:
            return await store.submit(run)
        finally:
            store.stop()
    one, every = asyncio.run(go())
    global_scan = "FROM v2_assistant_composite_routes AS r WHERE r.routing_state='resolved'"
    assert not any(global_scan in s for s in one)
    assert any(global_scan in s for s in every), "the multi-lane pass keeps its one map"
