"""Lane reads find observations by index, not by decoding every row (R-020).

Each test traces the real statement, reads its plan, and proves the statement
returns the same rows with and without its index (the R-018 pattern).
"""
import asyncio
import json

from store import Store
from store_work_index import effective_membership_ids_conn
from store_work_lanes import unblocks_conn

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


MALFORMED = '{"member": {"status": "blocked"'


def _malformed(indexed, *, existing):
    """Write one malformed record_json and read the lanes, with or without the two indexes.

    ``existing`` writes the row before the indexes are built, as an older store would hold it.
    """
    def run(conn):
        import sqlite3
        from store_work_index import ensure_work_index_schema
        _seed(conn)
        conn.execute("DROP INDEX v2_work_item_observations_epic")
        conn.execute("DROP INDEX v2_work_item_observations_status")
        outcome = {}

        def attempt(name, fn):
            try:
                fn()
                outcome[name] = "ok"
            except sqlite3.OperationalError as exc:
                conn.rollback()
                outcome[name] = str(exc)
        write = lambda: (conn.execute("INSERT INTO v2_work_item_observations VALUES ('spec_x__bad',?)", (MALFORMED,)),
                         conn.commit())
        if existing:
            attempt("write", write)
        if indexed:
            attempt("build", lambda: ensure_work_index_schema(conn))
        if not existing:
            attempt("write", write)
        attempt("membership read", lambda: effective_membership_ids_conn(conn))
        attempt("unblocks read", lambda: unblocks_conn(conn))
        outcome["indexes"] = sorted(r[1] for r in conn.execute("PRAGMA index_list(v2_work_item_observations)")
                                    if not r[1].startswith("sqlite_autoindex"))
        return outcome

    async def go():
        store = Store(":memory:"); store.start()
        try:
            return await store.submit(run)
        finally:
            store.stop()
    return asyncio.run(go())


def test_a_malformed_record_json_on_write_and_on_read_with_and_without_the_indexes():
    """Only the index apply writes record_json, always json.dumps; this pins the behavior anyway."""
    bad = "malformed JSON"
    # Today (no indexes): the write succeeds, then both lane reads fail on it.
    assert _malformed(False, existing=False) == {
        "write": "ok", "membership read": bad, "unblocks read": bad, "indexes": []}
    # With the indexes: the write is refused, and the reads stay healthy.
    assert _malformed(True, existing=False) == {
        "build": "ok", "write": bad, "membership read": "ok", "unblocks read": "ok",
        "indexes": ["v2_work_item_observations_epic", "v2_work_item_observations_status"]}
    # A store already holding a malformed row cannot build the indexes.
    assert _malformed(True, existing=True) == {
        "write": "ok", "build": bad, "membership read": bad, "unblocks read": bad, "indexes": []}
