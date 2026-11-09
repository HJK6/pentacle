"""Actual usage-fence path: bounded projection and frozen-wire compatibility."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time

import pytest

from event_push import EventPush
from store import Store
from usage_fence_legacy import LegacyFences


HOST = "host-a"
BIRTH = "2026-10-01T00:00:00Z"
HISTORY_TABLES = (
    "session_event_tail", "v2_tell_deliveries", "v2_send_receipts",
    "v2_agent_report_state", "v2_usage", "v2_capacity", "v2_assistant_composite_routes",
)


async def _seed(store, scale):
    """Real schema, linked open histories, and unrelated retained populations."""
    def write(conn):
        opened = []
        for i in reversed(range(23)):
            host = (HOST, "host-b", "host-d")[i % 3]
            provider = "other" if i == 22 else ("codex" if i % 2 else "claude")
            name = f"seat-{i:03d}"
            conn.execute(
                "INSERT INTO sessions(host,session_name,provider,visibility,status,created_at,pane_pid,account_binding) "
                "VALUES(?,?,?,'hidden','open',?,?,?)",
                (host, name, provider, BIRTH, str(7000 + i),
                 json.dumps({"provider": provider, "alias": "fixture"})),
            )
            conn.execute("INSERT INTO v2_session_generations VALUES(?,?,?)", (host, name, f"gen-{i}"))
            opened.append((host, name))
        closed = 500 * scale
        conn.executemany(
            "INSERT INTO sessions(host,session_name,provider,visibility,status,created_at,pane_pid) "
            "VALUES(?,?,'codex','hidden','closed',?,'6000')",
            (("host-b" if i % 2 else "host-d", f"closed-{i:05d}", BIRTH) for i in range(closed)),
        )
        def events():
            for i in range(50_000 * scale):
                j = i % closed
                host = "host-b" if j % 2 else "host-d"
                stream = f"{host}:closed-{j:05d}"
                event = {"kind": "ASSIST_TEXT", "stream_id": stream, "text": "Retained fixture history.",
                         "timestamp": BIRTH, "raw": {"source": "fixture"}}
                yield (stream, BIRTH, f"history-{i}", json.dumps(event), BIRTH, 1.0)
            for host, name in opened:
                stream = f"{host}:{name}"
                for i, kind in enumerate(("USER", "ASSIST_TEXT", "TELL")):
                    event = {"kind": kind, "stream_id": stream, "text": f"Linked {kind} input for {name}.",
                             "timestamp": BIRTH, "raw": {"source": "fixture"}}
                    yield (stream, BIRTH, f"linked-{i}", json.dumps(event), BIRTH, 1.0)
        conn.executemany(
            "INSERT INTO session_event_tail(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at) "
            "VALUES(?,?,?,?,?,?)", events(),
        )
        conn.executemany(
            "INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES(?,?,?)",
            ((f"tell-{i}", json.dumps({"delivery": {"to_stream_id": f"host-b:closed-{i % closed:05d}",
              "text": f"Retained peer input {i}", "proof_generation": "old-generation",
              "proof_watermark": i, "sender_kind": "peer", "sender_verified": True}}), 1.0)
             for i in range(10_000 * scale)),
        )
        conn.commit()
        return {"scale": scale, "sessions": conn.execute("SELECT count(*) FROM sessions").fetchone()[0],
                "open": len(opened), "closed": closed,
                "events": conn.execute("SELECT count(*) FROM session_event_tail").fetchone()[0],
                "tells": conn.execute("SELECT count(*) FROM v2_tell_deliveries").fetchone()[0]}
    return await store.submit(write)


async def _measure(store, action):
    statements = []
    tables = set()
    deadline = time.monotonic() + 30
    def begin(conn):
        conn.set_trace_callback(statements.append)
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        def observe(action, table, *_):
            if action == sqlite3.SQLITE_READ:
                tables.add(table)
            return sqlite3.SQLITE_OK
        conn.set_authorizer(observe)
    def end(conn):
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 0)
        conn.set_authorizer(None)
    await store.submit(begin)
    try:
        start = time.perf_counter()
        result = await action()
        elapsed = (time.perf_counter() - start) * 1000
    finally:
        await store.submit(end)
    return result, elapsed, statements, tables


@pytest.mark.parametrize("scale", [1, 10])
def test_complete_fence_lookup_has_no_history_work(tmp_path, scale, record_testsuite_property):
    def record(name, value):
        record_testsuite_property(f"{name}_{scale}", value)

    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            population = await _seed(store, scale)
            await store.submit(lambda c: c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())
            with (tmp_path / "sessions.db").open("rb") as fixture:
                population["db_sha256"] = hashlib.file_digest(fixture, "sha256").hexdigest()
            record("usage_fence_fixture", json.dumps(population, sort_keys=True))
            sink = EventPush(store, None, None, recent_limit=20)
            result, first_ms, statements, tables = await _measure(store, lambda: sink._usage_fences(HOST))
            history_reads = [s for s in statements if any(t in s.lower() for t in HISTORY_TABLES)]
            evidence = {"population": population, "first_ms": first_ms, "statements": len(statements),
                        "history_statements": len(history_reads), "read_tables": sorted(tables), "fences": len(result)}
            # The frozen path is measured uncensored on the same immutable DB;
            # it is also the independent ordered wire/optional-field oracle.
            old = LegacyFences(store)
            old_times = []
            for _ in range(3):
                expected, ms, old_sql, _ = await _measure(store, lambda: old._usage_fences(HOST))
                assert result == expected
                assert len(old_sql) > 3
                old_times.append(ms)
            evidence["old_ms"] = old_times
            record("usage_fence_benchmark", json.dumps(evidence, sort_keys=True))
            assert result, evidence
            assert len(statements) <= 3 and not history_reads, evidence
            assert tables == {"sessions", "v2_session_generations"}, evidence
            selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            assert len(selects) == 1
            plan = await store.submit(lambda c: [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + selects[0])])
            assert any("SEARCH s" in d and "(host=?)" in d for d in plan), plan
            assert any("SEARCH g" in d and "host=? AND session_name=?" in d for d in plan), plan
            evidence["query_plan"] = plan
            times = []
            for _ in range(10):
                actual, ms, sql, read_tables = await _measure(store, lambda: sink._usage_fences(HOST))
                assert actual == result and len(sql) <= 3
                assert read_tables == {"sessions", "v2_session_generations"}
                assert not any(t in s.lower() for s in sql for t in HISTORY_TABLES)
                times.append(ms)
            evidence.update({"old_ms": old_times, "repeat_ms": times,
                             "fence_digest": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()})
            (tmp_path / "usage-fence-benchmark.json").write_text(json.dumps(evidence, indent=2))
            # Fixed wall-clock acceptance is evaluated from these retained
            # receipts on the qualified host. CI guards query/table work,
            # independently of runner load and clock noise.
            record("usage_fence_acceptance", json.dumps(evidence, sort_keys=True))
        finally:
            store.stop()
    asyncio.run(run())


async def _add_session(store, name, *, host=HOST, provider="codex", pane="7001",
                       birth=BIRTH, generation="gen-a", binding=None, status="open"):
    def write(conn):
        conn.execute(
            "INSERT INTO sessions(host,session_name,provider,visibility,status,created_at,pane_pid,account_binding) "
            "VALUES(?,?,?,'hidden',?,?,?,?)",
            (host, name, provider, status, birth, pane, binding),
        )
        if generation is not None:
            conn.execute("INSERT INTO v2_session_generations VALUES(?,?,?)", (host, name, generation))
        conn.commit()
    await store.submit(write)


async def _same_wire(store, host=HOST):
    expected = await LegacyFences(store)._usage_fences(host)
    actual = await EventPush(store, None, None, recent_limit=20)._usage_fences(host)
    assert actual == expected
    return actual


@pytest.mark.parametrize("binding", [None, "", "null", "false", "0", '""', "[]", "{}",
                                      "5", '"fixture"', '["fixture"]', '{"alias":"fixture"}', "{broken"])
def test_frozen_ordered_wire_and_binding_decode(tmp_path, binding):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            # Deliberate insertion order differs from host/name index order.
            await _add_session(store, "seat-z", binding=binding)
            await _add_session(store, "seat-a", provider="claude")
            await _add_session(store, "seat-m", provider="other")
            await _add_session(store, "seat-z", host="host-b")
            await _add_session(store, "closed", status="closed")
            for name, overrides in [
                ("no-generation", {"generation": None}), ("empty-generation", {"generation": ""}),
                ("no-pane", {"pane": None}), ("empty-pane", {"pane": ""}),
                ("empty-time", {"birth": ""}), ("no-provider", {"provider": None}),
            ]:
                await _add_session(store, name, **overrides)
            rows = await _same_wire(store)
            assert [r["stream_id"] for r in rows] == [f"{HOST}:seat-z", f"{HOST}:seat-a"]
            decoded = binding
            if binding:
                try:
                    decoded = json.loads(binding)
                except ValueError:
                    pass
            assert ("account_binding" in rows[0]) == bool(decoded)
            if decoded:
                assert rows[0]["account_binding"] == decoded
            assert await _same_wire(store, "host-empty") == []
            assert [r["stream_id"] for r in await _same_wire(store, "host-b")] == ["host-b:seat-z"]
        finally:
            store.stop()
    asyncio.run(run())


def test_refresh_and_read_snapshot_keep_generation_fields_together(tmp_path):
    async def run():
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            await _add_session(store, "reused")
            old = await _same_wire(store)
            # Pin the reader's snapshot, commit a complete lifecycle change on
            # another connection, then read through the actual Store path.
            await store.submit(lambda c: c.execute("BEGIN"))
            await store.submit(lambda c: c.execute("SELECT created_at FROM sessions").fetchall())
            with sqlite3.connect(db) as writer:
                writer.execute("UPDATE sessions SET pane_pid='8002',created_at='2026-11-01T00:00:00Z'")
                writer.execute("UPDATE v2_session_generations SET generation='gen-b'")
            assert await _same_wire(store) == old
            await store.submit(lambda c: c.execute("COMMIT"))
            current = await _same_wire(store)
            assert current == [{"stream_id": f"{HOST}:reused", "session_generation": "gen-b",
                                "provider": "codex", "source_pane_pid": "8002",
                                "session_created_at": "2026-11-01T00:00:00Z"}]
            def close(conn):
                conn.execute("UPDATE sessions SET status='closed'")
                conn.commit()
            await store.submit(close)
            assert await _same_wire(store) == []
        finally:
            store.stop()
    asyncio.run(run())


def test_missing_created_time_is_rejected_by_actual_schema(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await _add_session(store, "missing-time", birth=None)
            await store.submit(lambda c: c.rollback())
            assert await _same_wire(store) == []
        finally:
            store.stop()
    asyncio.run(run())


def test_real_host_stats_preserves_auth_empty_and_projection_failure(tmp_path, monkeypatch):
    from machine_stats import WIRE_VERSION
    monkeypatch.setenv("PENTACLE_EVENT_PUSH_SECRET", "fixture-secret")
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        samples = []
        async def stats(host, value):
            samples.append((host, value))
        try:
            await store.put("event_push.target_sha", "a" * 40)
            await _add_session(store, "seat")
            sink = EventPush(store, None, None, recent_limit=20, host_stats_handler=stats)
            msg = {"request_id": "stats", "push_secret": "fixture-secret", "host": HOST,
                   "wire_version": WIRE_VERSION, "satellite_sha": "a" * 40,
                   "stats": {"host": HOST, "cpu_load_1m": 1.0, "memory_used_bytes": 20,
                             "memory_total_bytes": 100, "disk_used_bytes": 200,
                             "disk_total_bytes": 1000, "uptime_seconds": 42}}
            assert (await sink.handle_host_stats({**msg, "push_secret": "wrong"}))["error"] == "unauthorized"
            assert (await sink.handle_host_stats({**msg, "host": ""}))["error"] == "bad_stats"
            assert (await sink.handle_host_stats({**msg, "wire_version": -1}))["error"] == "unsupported_version"
            assert (await sink.handle_host_stats({**msg, "satellite_sha": "b" * 40}))["error"] == "satellite_version_mismatch"
            assert samples == []
            reply = await sink.handle_host_stats(msg)
            assert reply["type"] == "host.stats.ok"
            assert reply["usage_fences"] == await _same_wire(store)
            # A real SQLite read failure affects only the fence projection;
            # the secret/version store reads and authenticated stats still run.
            def deny_sessions(conn):
                conn.set_authorizer(lambda action, table, *_:
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "sessions"
                    else sqlite3.SQLITE_OK)
            await store.submit(deny_sessions)
            try:
                failed = await sink.handle_host_stats(msg)
                assert failed["type"] == "host.stats.ok" and "usage_fences" not in failed
            finally:
                await store.submit(lambda c: c.set_authorizer(None))
            def close(conn):
                conn.execute("UPDATE sessions SET status='closed'")
                conn.commit()
            await store.submit(close)
            assert (await sink.handle_host_stats(msg))["usage_fences"] == []
            assert len(samples) == 3
        finally:
            store.stop()
    asyncio.run(run())
