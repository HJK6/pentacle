"""Identical-answer proofs for the store-latency fixes 1-4.

Each fix trades repeated work for a guarded reuse; these tests pin the answer to
the pre-fix path and show the work actually dropped (scan counters), per
spec_pentacle__store_latency_work_observation_apply. They are written to FAIL
under the negative-control mutants (a reused-but-stale delivery table; a lineage
cache whose generation/delete/handoff triggers are removed).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from store import Store
from store_work_lane_episodes import resolved_route_dispatch_map

from test_read_projection_deliveries import corpus
from test_work_lane_episodes import RecordingSink, episodes, member, seed_observations, NOW, OLD

BIRTH = "2026-10-01T00:00:00Z"


def _select_trace(conn, store_statements):
    conn.set_trace_callback(lambda sql: store_statements.append(sql))


# --- Fix 1: one route scan per pass, identical per-lane dispatch sets ---------

PER_LANE_SQL = (
    "SELECT r.dispatch_id FROM v2_assistant_composite_routes AS r "
    "WHERE r.stream_id=? AND r.routing_state='resolved' AND ("
    "json_extract(COALESCE(NULLIF(r.route_json,''),'{}'),'$.lane_id')=? "
    "OR EXISTS (SELECT 1 FROM v2_assistant_composite_operations AS o "
    "WHERE o.stream_id=r.stream_id AND o.dispatch_id=r.dispatch_id "
    "AND o.lane_id=? AND o.operation='lane.admit'))"
)

ROUTE_COLS = ("route_id,stream_id,input_identity,payload_digest,input_request_id,body,routing_state,"
              "dispatch_id,route_target,route_target_generation,route_json,created_at,updated_at")


def _route(conn, route_id, stream, dispatch, state, route_json):
    conn.execute(
        f"INSERT INTO v2_assistant_composite_routes({ROUTE_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (route_id, stream, f"ident-{route_id}", f"digest-{route_id}", f"req-{route_id}", "body",
         state, dispatch, stream, "gen", route_json, OLD, OLD))


def _admit(conn, op_id, stream, dispatch, lane, operation="lane.admit"):
    conn.execute(
        "INSERT INTO v2_assistant_composite_operations(operation_id,stream_id,dispatch_id,lane_id,"
        "operation,payload_digest,created_at) VALUES (?,?,?,?,?,?,?)",
        (op_id, stream, dispatch, lane, operation, "digest", OLD))


def _seed_routes(conn):
    A, B = "asm:a", "asm:b"
    _route(conn, "r1", A, "d1", "resolved", json.dumps({"lane_id": "L1"}))        # L1 by route_json
    _route(conn, "r2", A, "d2", "resolved", "")                                   # L2 by lane.admit
    _route(conn, "r3", A, "d3", "resolved", json.dumps({"lane_id": "L2"}))        # L2 by route_json
    _route(conn, "r4", A, "d4", "queued", json.dumps({"lane_id": "L1"}))          # unresolved: ignored
    _route(conn, "r5", B, "d5", "resolved", json.dumps({"lane_id": "L3"}))        # other stream
    _admit(conn, "o2", A, "d2", "L2")                                             # binds d2 -> L2
    _admit(conn, "ox", A, "d1", "L1", operation="lane.bind")                      # not lane.admit: ignored
    conn.commit()


def _count(statements, needle):
    return sum(1 for s in statements
               if s.lstrip().upper().startswith("SELECT") and needle in s)


def test_resolved_route_dispatch_map_matches_per_lane_statement():
    lanes = [("asm:a", "L1"), ("asm:a", "L2"), ("asm:b", "L3"), ("asm:a", "L-none")]

    def body(conn):
        _seed_routes(conn)
        mp = resolved_route_dispatch_map(conn)
        for stream, lane in lanes:
            per_lane = {r["dispatch_id"] for r in conn.execute(PER_LANE_SQL, (stream, lane, lane))}
            assert (mp.get((stream, lane)) or set()) == per_lane, (stream, lane)
        assert mp.get(("asm:a", "L1")) == {"d1"}
        assert mp.get(("asm:a", "L2")) == {"d2", "d3"}
        assert mp.get(("asm:b", "L3")) == {"d5"}
        assert ("asm:a", "L-none") not in mp

    async def run():
        store = Store()
        store.start()
        try:
            await store.submit(body)
        finally:
            store.stop()
    asyncio.run(run())


def test_work_lane_rows_scans_routes_twice_regardless_of_lane_count():
    # The cost claim lives in work_lane_rows_conn, not the standalone helper: it
    # must scan the routes table a fixed two times for a whole multi-lane pass
    # (reverting to the per-lane statement would scale with lane count).
    from test_work_lanes import run
    from store_work_lanes import work_lane_rows_conn

    async def go(env):
        for i in range(4):
            await env.adopt(key=f"stream:{i}", state="paused", lead=False, owner="operator")

        def measure(conn):
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            try:
                rows = work_lane_rows_conn(conn, include_done=True)
            finally:
                conn.set_trace_callback(None)
            return len(rows), _count(statements, "v2_assistant_composite_routes")

        lane_count, route_scans = await env.store.submit(measure)
        assert lane_count >= 4
        assert route_scans == 2, f"route scans for {lane_count} lanes: {route_scans}"

    run(go)


def test_reconcile_skips_done_lanes_keeps_clears_and_always_drains():
    # Identical episode outcomes across active/paused/done, and the post-loop
    # sink drain runs for an unemitted opening on a SKIPPED done lane.
    from test_work_lanes import run

    async def go(env):
        active = (await env.adopt(key="stream:a", state="paused", lead=False,
                                  owner="operator", members=["spec_demo__span"]))["lane"]
        done_open = (await env.adopt(key="stream:d", state="paused", lead=False, owner="operator"))["lane"]
        done_cleared = (await env.adopt(key="stream:e", state="paused", lead=False, owner="operator"))["lane"]
        done_quiet = (await env.adopt(key="stream:f", state="paused", lead=False, owner="operator"))["lane"]
        await seed_observations(env.store, [member()])

        def force(conn):
            for lane in (done_open["lane_id"], done_cleared["lane_id"], done_quiet["lane_id"]):
                conn.execute("UPDATE v2_assistant_composite_lanes SET work_state='done' WHERE lane_id=?", (lane,))
            # done_open: an UNcleared, unemitted opening -> lane is NOT skipped,
            # its predicate is False for done so it must be cleared, then drained.
            conn.execute(
                "INSERT INTO v2_work_lane_episodes(episode_id,lane_id,kind,sequence,opened_at,cleared_at,emitted_ref,fact_json)"
                " VALUES (?,?,?,?,?,NULL,NULL,?)",
                (f"{done_open['lane_id']}:stale:1", done_open["lane_id"], "stale", 1, OLD,
                 json.dumps({"lane_id": done_open["lane_id"], "kind": "stale", "episode_id": f"{done_open['lane_id']}:stale:1", "opened_at": OLD})))
            # done_cleared: a CLEARED but still-unemitted opening and NO uncleared
            # episode -> the lane IS skipped in the loop, yet the post-loop drain
            # must still emit it.
            conn.execute(
                "INSERT INTO v2_work_lane_episodes(episode_id,lane_id,kind,sequence,opened_at,cleared_at,emitted_ref,fact_json)"
                " VALUES (?,?,?,?,?,?,NULL,?)",
                (f"{done_cleared['lane_id']}:completed:1", done_cleared["lane_id"], "completed", 1, OLD, OLD,
                 json.dumps({"lane_id": done_cleared["lane_id"], "kind": "completed", "episode_id": f"{done_cleared['lane_id']}:completed:1", "opened_at": OLD})))
            conn.commit()
        await env.store.submit(force)

        sink = RecordingSink()
        await env.store.reconcile_work_lane_episodes(sink, now_iso=NOW)

        rows = {(r["lane_id"], r["kind"]): r for r in await episodes(env.store)}
        drained = {c.get("lane_id") for c in sink.calls}

        # done_open: processed (uncleared episode present) -> stale cleared, drained.
        assert rows[(done_open["lane_id"], "stale")]["cleared_at"] == NOW
        assert done_open["lane_id"] in drained
        # done_cleared: skipped in the loop (no uncleared episode) -> episode row
        # untouched, but its unemitted opening is still drained post-loop.
        assert rows[(done_cleared["lane_id"], "completed")]["cleared_at"] == OLD
        assert done_cleared["lane_id"] in drained
        # done_quiet: skipped and nothing to do -> no episode, not drained.
        assert not any(lane == done_quiet["lane_id"] for lane, _ in rows)
        # active lane still opens its completion and drains it.
        assert rows[(active["lane_id"], "completed")]["cleared_at"] is None
        assert active["lane_id"] in drained

    run(go)


# --- Fix 2: one delivery table per request, rebuilt only on an intervening write

async def _page(store, cursor, *, limit=1, stream="host-a:proof", fresh=False):
    async def read():
        return await store.fetch_session_event_page(
            stream, before_daemon_seq=cursor, limit=limit)
    if fresh:
        from unittest.mock import patch
        from tell_proof import ProjectionDeliveries
        with patch.object(store, "_history_deliveries", lambda conn, shared: ProjectionDeliveries()):
            return await read()
    return await read()


async def _walk(store, *, limit=3, stream="host-a:proof", fresh=False):
    """Mirror the server's page walk, optionally constructing fresh lookups."""
    out: list = []
    cursor = None
    while True:
        page = await _page(store, cursor, limit=limit, stream=stream, fresh=fresh)
        if not page:
            break
        out = page + out
        cursor = page[0]["daemon_seq"]
        if len(page) < limit:
            break
    return out


def _delivery_builds(statements):
    return sum(1 for s in statements if "FROM v2_tell_deliveries" in s and "sql_target" in s)


def test_history_delivery_table_built_once_for_an_unchanged_request():
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            # Fresh-per-page is the oracle.
            fresh = await _walk(store, fresh=True)
            # One shared cache across the whole request: identical events, and the
            # delivery table is built exactly once for the unchanged request.
            statements: list[str] = []
            await store.submit(lambda c: c.set_trace_callback(statements.append))
            reused = await _walk(store)
            await store.submit(lambda c: c.set_trace_callback(None))
            assert reused == fresh
            assert _delivery_builds(statements) == 1
        finally:
            store.stop()
    asyncio.run(run())


def _peer_event(seq, text):
    return {"kind": "TELL", "stream_id": "host-a:px", "text": f"[from host-b:s] [tell:{seq}]\n{text}",
            "timestamp": BIRTH, "raw": {"peer_wire_text": f"[from host-b:s] [tell:{seq}]\n{text}",
                                        "sender": "host-b:s", "peer_payload": seq},
            "message_envelope": {"kind": "peer_delivery"}, "daemon_seq": seq}


def test_history_reflects_a_between_page_delivery_write_not_a_stale_table():
    # The hazard the guard exists for: a delivery write lands between the server's
    # paged reads. A page read AFTER it, on the SAME cache, must reflect it exactly
    # as a fresh-per-page read would -- never serve the stale pre-write table.
    async def run():
        store = Store()
        store.start()
        try:
            def seed(conn):
                conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at)"
                             " VALUES('host-a','px','codex','default','open',?)", (BIRTH,))
                conn.execute("INSERT INTO v2_session_generations VALUES('host-a','px','gen')")
                # Oldest event is a peer TELL whose origin resolves via delivery D.
                peer_text = "the peer line"
                ev = {"kind": "TELL", "stream_id": "host-a:px",
                      "text": f"[from host-b:s] [tell:peer]\n{peer_text}", "timestamp": BIRTH,
                      "raw": {"peer_wire_text": f"[from host-b:s] [tell:peer]\n{peer_text}",
                              "sender": "host-b:s", "peer_payload": "peer"},
                      "message_envelope": {"kind": "peer_delivery"}}
                seq = conn.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at)"
                                   " VALUES(?,?,?,?,?,?)", ("host-a:px", BIRTH, "peer", json.dumps(ev), BIRTH, 1.0)).lastrowid
                delivery = {"tell_id": "peer", "to_stream_id": "host-a:px",
                            "text": f"[from host-b:s] [tell:peer]\n{peer_text}",
                            "proof_generation": "gen", "proof_session_created_at": BIRTH,
                            "proof_event_id": seq, "proof_watermark": 0, "proof_watermark_state": "reachable",
                            "request_payload_hash": "peer", "sender_kind": "peer", "sender_verified": True,
                            "from_stream_id": "host-b:s", "sender_generation": "sg"}
                conn.execute("INSERT INTO v2_tell_deliveries VALUES('peer',?,1)",
                             (json.dumps({"payload_digest": "peer", "delivery": delivery}),))
                # Newer filler events so the peer event lands on a later page.
                for i in range(3):
                    f = {"kind": "USER", "stream_id": "host-a:px", "text": f"u{i}", "timestamp": BIRTH}
                    conn.execute("INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at)"
                                 " VALUES(?,?,?,?,?,?)", ("host-a:px", BIRTH, f"u{i}", json.dumps(f), BIRTH, 1.0))
                conn.commit()
                return seq
            peer_seq = await store.submit(seed)

            statements: list[str] = []
            await store.submit(lambda c: c.set_trace_callback(statements.append))
            # Page 1 (newest fillers) builds the delivery table while D is present.
            page1 = await _page(store, None, limit=2, stream="host-a:px")
            builds_before = _delivery_builds(statements)
            assert builds_before == 1
            assert await store.submit(lambda c: c.delivery_lookup.table is not None)

            # A delivery write (delete D) lands between pages.
            await store.submit(lambda c: (c.execute("DELETE FROM v2_tell_deliveries WHERE tell_id='peer'"), c.commit()))

            cursor = page1[0]["daemon_seq"]
            # Reused read on the SAME cache after the delete: trace it alone to
            # prove the table was rebuilt (one build on this page).
            statements.clear()
            reused_page = await _page(store, cursor, limit=3, stream="host-a:px")
            await store.submit(lambda c: c.set_trace_callback(None))
            assert _delivery_builds(statements) == 1
            # Fresh-per-page oracle under the same post-delete state.
            fresh_page = await _page(store, cursor, fresh=True, limit=3, stream="host-a:px")

            # The peer event is in this page; its projection must match the oracle
            # (origin no longer a verified peer, since D was deleted). A stale table
            # would keep the verified-peer provenance and diverge.
            assert any(e["daemon_seq"] == peer_seq for e in reused_page)
            assert reused_page == fresh_page
        finally:
            store.stop()
    asyncio.run(run())


def test_history_reuse_after_legacy_fallback_is_identical():
    # An uncertain delivery row flips ProjectionDeliveries to the legacy path; a
    # reused legacy-flagged object must still return the fresh-per-page answer.
    async def run():
        store = Store()
        store.start()
        try:
            await corpus(store)
            # A valid JSON row whose duplicate key makes SQLite json_extract and
            # Python's unique-object parse disagree flips ProjectionDeliveries to
            # the legacy path, without the raw garbage that would crash the inline
            # json_extract oracle itself.
            await store.submit(lambda c: (c.execute(
                "INSERT INTO v2_tell_deliveries VALUES('dup',?,1)",
                ('{"delivery":{"to_stream_id":"host-b:other","text":"a","text":"b"}}',)), c.commit()))
            cursor = None
            fresh: list = []
            reused: list = []
            while True:
                pf = await _page(store, cursor, fresh=True, limit=3)
                pr = await _page(store, cursor, limit=3)
                assert pf == pr
                if not pr:
                    break
                fresh = pf + fresh
                reused = pr + reused
                cursor = pr[0]["daemon_seq"]
                if len(pr) < 3:
                    break
            assert reused == fresh
        finally:
            store.stop()
    asyncio.run(run())


# --- Fix 3: lineage cache keyed on a trigger-maintained version ---------------

def _lineage_scans(statements):
    return sum(1 for s in statements
               if s.lstrip().upper().startswith("SELECT") and "FROM sessions s" in s
               and "v2_session_generations" in s)


async def _seed_lineage(store):
    def write(conn):
        for i in range(4):
            conn.execute("INSERT INTO sessions(host,session_name,provider,visibility,status,created_at,parent_stream_id,handoff_from_stream_id)"
                         " VALUES('host-a',?,'codex','default','open',?,?,NULL)",
                         (f"seat-{i}", BIRTH, "host-a:seat-0" if i else None))
            conn.execute("INSERT INTO v2_session_generations VALUES('host-a',?,?)", (f"seat-{i}", f"gen-{i}"))
        conn.commit()
    await store.submit(write)


async def _lineage_and_scans(store):
    """Return (rows, lineage_scan_count) for one list_session_lineage call."""
    statements: list[str] = []
    await store.submit(lambda c: c.set_trace_callback(statements.append))
    rows = await store.list_session_lineage()
    await store.submit(lambda c: c.set_trace_callback(None))
    return rows, _lineage_scans(statements)


async def _direct_lineage(store):
    """The ordered direct-SQL oracle, bypassing the cache."""
    def q(conn):
        return [{
            "stream_id": f"{r['host']}:{r['session_name']}",
            "parent_stream_id": r["parent_stream_id"],
            "handoff_from_stream_id": r["handoff_from_stream_id"],
            "visibility": r["visibility"],
            "session_generation": str(r["generation"]) if r["generation"] is not None else "",
        } for r in conn.execute(
            "SELECT s.host,s.session_name,s.parent_stream_id,s.handoff_from_stream_id,s.visibility,g.generation "
            "FROM sessions s LEFT JOIN v2_session_generations g ON g.host=s.host AND g.session_name=s.session_name "
            "ORDER BY s.rowid")]
    return await store.submit(q)


def test_session_lineage_cache_identity_invalidation_and_scan_elimination():
    async def run():
        store = Store()
        store.start()
        try:
            await _seed_lineage(store)

            async def assert_matches_oracle():
                rows, _ = await _lineage_and_scans(store)
                assert rows == await _direct_lineage(store)

            # Cold read scans once; a warm read with no mutation does not scan.
            _, cold = await _lineage_and_scans(store)
            assert cold == 1
            _, warm = await _lineage_and_scans(store)
            assert warm == 0
            await assert_matches_oracle()

            # A non-lineage column change (observer_binding) does NOT invalidate.
            await store.update_session("host-a", "seat-1", observer_binding=json.dumps({"x": 1}))
            _, scans = await _lineage_and_scans(store)
            assert scans == 0

            # --- the three mutant-catching mutations: generation, delete, handoff ---
            # Generation-only upsert (stands in for a reopen generation bump).
            await store.submit(lambda c: (c.execute("UPDATE v2_session_generations SET generation='gen-1b' WHERE session_name='seat-1'"), c.commit()))
            _, scans = await _lineage_and_scans(store)
            assert scans == 1
            await assert_matches_oracle()
            assert next(r for r in await _direct_lineage(store) if r["stream_id"] == "host-a:seat-1")["session_generation"] == "gen-1b"

            # Handoff change via the real update_session path.
            await store.update_session("host-a", "seat-2", handoff_from_stream_id="host-a:seat-0")
            _, scans = await _lineage_and_scans(store)
            assert scans == 1
            await assert_matches_oracle()

            # A row DELETE (stands in for a reaper / retention delete).
            await store.submit(lambda c: (c.execute("DELETE FROM sessions WHERE session_name='seat-3'"),
                                          c.execute("DELETE FROM v2_session_generations WHERE session_name='seat-3'"),
                                          c.commit()))
            _, scans = await _lineage_and_scans(store)
            assert scans == 1
            after_delete = await store.list_session_lineage()
            assert not any(r["stream_id"] == "host-a:seat-3" for r in after_delete)
            await assert_matches_oracle()

            # Real writers: visibility/parent via update_session; a brand-new seat.
            await store.update_session("host-a", "seat-1", visibility="hidden", parent_stream_id="host-a:seat-2")
            _, scans = await _lineage_and_scans(store)
            assert scans == 1
            await assert_matches_oracle()

            await store.open_session("host-a", "late", provider="codex", pane_pid="9")
            _, scans = await _lineage_and_scans(store)
            assert scans == 1
            assert any(r["stream_id"] == "host-a:late" for r in await store.list_session_lineage())

            # A status-only close leaves the projected rows unchanged, so it does
            # NOT invalidate and rows stay identical.
            before_close = await store.list_session_lineage()
            await store.mark_closed("host-a", "late", closed_at=NOW, pane_status="pane_dead")
            after_close, scans = await _lineage_and_scans(store)
            assert scans == 0
            assert after_close == before_close
            await assert_matches_oracle()
        finally:
            store.stop()
    asyncio.run(run())


def test_lineage_ddl_is_lazy_created_on_first_read_not_at_init():
    """The version table + triggers are absent after init and created by the
    first list_session_lineage read. Stores that never read lineage (nearly
    every test, and every non question/asset code path) therefore do not pay the
    per-init DDL that regressed gate wall-time. Identity is unaffected: the cold
    read full-scans and caches the current truth."""
    async def run():
        store = Store()
        store.start()
        try:
            def lineage_objects(conn):
                return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")
                        if "lineage" in r[0]}
            # After init, before any lineage read: none of the DDL exists.
            assert await store.submit(lineage_objects) == set()
            # The first read creates the version table and all six triggers.
            await _seed_lineage(store)
            rows = await store.list_session_lineage()
            assert "_lineage_cache_version" in await store.submit(lineage_objects)
            triggers = await store.submit(lambda c: {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'") if "lineage" in r[0]})
            assert triggers == {
                "_lineage_sessions_ai", "_lineage_sessions_ad", "_lineage_sessions_au",
                "_lineage_generations_ai", "_lineage_generations_ad", "_lineage_generations_au"}
            # Identity holds on the cold read, and a warm read still serves cache.
            assert rows == await _direct_lineage(store)
            _, warm = await _lineage_and_scans(store)
            assert warm == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_lineage_reads_survive_a_store_restart():
    """A stop()/start() restart gives the Store thread a NEW connection (an empty
    :memory: schema). The lazy-DDL flag and the row cache must reset per
    connection, or the first lineage read skips creation and raises
    `no such table: _lineage_cache_version`, or serves a stale cross-connection
    cache. Regression guard for the cycle-3 reviewer's finding."""
    async def run():
        store = Store()  # :memory: — the restart re-creates the schema from scratch
        store.start()
        try:
            await _seed_lineage(store)
            assert await store.list_session_lineage() == await _direct_lineage(store)
        finally:
            store.stop()
        store.start()
        try:
            # Must not raise, and must reflect the NEW (empty) DB, not a leaked cache.
            assert await store.list_session_lineage() == await _direct_lineage(store)
            # The new connection re-created the triggers: a lineage write invalidates.
            await store.open_session("host-a", "fresh", provider="codex", pane_pid="1")
            _, scans = await _lineage_and_scans(store)
            assert scans == 1
            assert any(r["stream_id"] == "host-a:fresh" for r in await store.list_session_lineage())
        finally:
            store.stop()
    asyncio.run(run())


# --- Fix 4: read-timing receipt is optional, still validated when supplied ----

def test_read_timing_receipt_optional_but_validated(tmp_path):
    from deploy.deploy import _load_read_timing_receipt, DeployError

    assert _load_read_timing_receipt(None, "a" * 40) == {}

    bad = tmp_path / "receipt.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DeployError):
        _load_read_timing_receipt(bad, "a" * 40)
