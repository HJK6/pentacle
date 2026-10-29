"""Retained receipts are repaired once; startup never reports premature success."""
import asyncio
import threading

import pytest

import store_exchange
from store import Store


def test_unchanged_restart_does_not_revisit_receipts(tmp_path, monkeypatch):
    async def run():
        path = str(tmp_path / "store.db")
        store = Store(path)
        store.start()
        try:
            await store.open_session("test-host", "parent")
            await store.open_session("test-host", "child", parent_stream_id="test-host:parent")
            binding = await store.exchange_binding("test-host:parent", "test-host:child")
            observation = store_exchange.source(binding, "tell", "retained", "retained text", "2026-01-01T00:00:00Z")
            await store.put_tell_delivery("retained", {"delivery": {"exchange": observation}})
        finally:
            assert store.stop()
        store = Store(path)
        store.start()
        assert store.stop()
        examined = []
        original = store_exchange.append
        def counted(conn, observation):
            examined.append(observation)
            return original(conn, observation)
        monkeypatch.setattr(store_exchange, "append", counted)
        store = Store(path)
        try:
            store.start()
            assert examined == [], "unchanged startup revisited retained receipt payloads"
        finally:
            assert store.stop()
    asyncio.run(run())


def test_start_timeout_is_not_ready_and_keeps_worker_owner(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    initialize = store_exchange.initialize
    def blocked(conn):
        entered.set()
        assert release.wait(20), "test did not release initialization"
        initialize(conn)
    monkeypatch.setattr(store_exchange, "initialize", blocked)
    store = Store()
    try:
        with pytest.raises(TimeoutError, match="initializ"):
            store.start()
        assert entered.is_set() and not store._ready.is_set()
        owner = store._thread
        assert owner is not None and owner.is_alive()
        release.set()
        store.start()
        assert store._thread is owner
        asyncio.run(store.put("usable", "yes"))
    finally:
        release.set()
        assert store.stop()


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "legacy.db"
    real = store_exchange.repair
    monkeypatch.setattr(store_exchange, "repair", lambda conn: 0)
    async def setup():
        store = Store(str(path))
        store.start()
        try:
            await store.open_session("test-host", "parent", session_generation="p" * 32)
            await store.open_session("test-host", "child", parent_stream_id="test-host:parent", session_generation="c" * 32)
            return await store.exchange_binding("test-host:parent", "test-host:child")
        finally:
            assert store.stop()
    binding = asyncio.run(setup())
    monkeypatch.setattr(store_exchange, "repair", real)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn, binding, path
    finally:
        conn.close()


def receipt(conn, binding, kind, identity, text="retained", status="done"):
    import json
    observation = store_exchange.source(binding, kind, identity, text, "2026-01-01T00:00:00Z")
    if kind == "tell":
        conn.execute("INSERT INTO v2_tell_deliveries VALUES(?,?,1)",
                     (identity, json.dumps({"delivery": {"exchange": observation}})))
    elif kind == "report":
        conn.execute("INSERT INTO v2_reports(report_id,from_stream_id,session_generation,status,ingested_at,created_at,exchange_json) VALUES(?,?,?,?,?,1,?)",
                     (identity, binding["child"], binding["child_generation"], status, observation["ts"], json.dumps(observation)))
    else:
        conn.execute("INSERT INTO v2_spawn_outcomes(host,session_name,request_id,state,updated_at,delivery_receipt) VALUES('test-host',?,?,'delivered',1,?)",
                     (identity, identity, json.dumps({"exchange": observation})))
    return observation


def projected(conn):
    return [tuple(row) for row in conn.execute("SELECT kind,ref_id,text FROM v2_child_exchange ORDER BY kind,ref_id")]


def test_legacy_all_sources_resume_and_unchanged_restart(legacy, monkeypatch):
    conn, binding, _ = legacy
    for kind in ("tell", "report", "brief"):
        for i in range(105):
            receipt(conn, binding, kind, f"{i:04d}")
    conn.commit()
    store_exchange.repair(conn)
    before = projected(conn)
    assert len(before) == 315
    def no_historical_decode(value, *args, **kwargs):
        pytest.fail("unchanged repair decoded historical JSON")
    monkeypatch.setattr(store_exchange.json, "loads", no_historical_decode)
    store_exchange.repair(conn)
    assert projected(conn) == before


@pytest.mark.parametrize("kind", ["tell", "report", "brief"])
def test_old_source_update_and_new_insert_are_caught(legacy, kind):
    import json
    conn, binding, _ = legacy
    receipt(conn, binding, kind, "a-old")
    receipt(conn, binding, kind, "z-last")
    table, column, predicate = {
        "tell": ("v2_tell_deliveries", "reply", "tell_id='a-old'"),
        "report": ("v2_reports", "exchange_json", "report_id='a-old'"),
        "brief": ("v2_spawn_outcomes", "delivery_receipt", "session_name='a-old'"),
    }[kind]
    conn.execute(f"UPDATE {table} SET {column}='{{}}' WHERE {predicate}")
    conn.commit()
    store_exchange.repair(conn)
    observation = store_exchange.source(binding, kind, "a-old", "new evidence", "2026-01-02T00:00:00Z")
    if kind == "tell":
        conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id='a-old'", (json.dumps({"delivery": {"exchange": observation}}),))
    elif kind == "report":
        conn.execute("UPDATE v2_reports SET exchange_json=? WHERE report_id='a-old'", (json.dumps(observation),))
    else:
        conn.execute("UPDATE v2_spawn_outcomes SET delivery_receipt=? WHERE session_name='a-old'", (json.dumps({"exchange": observation}),))
    receipt(conn, binding, kind, "0-inserted-below-cursor")
    conn.commit()
    store_exchange.repair(conn)
    assert {row[1] for row in projected(conn)} == {"a-old", "z-last", "0-inserted-below-cursor"}
    store_exchange.repair(conn)
    assert len(projected(conn)) == 3


def test_deleted_projections_recover_without_restoring_invalid_pair(legacy):
    conn, binding, _ = legacy
    for kind in ("tell", "report", "brief"):
        receipt(conn, binding, kind, kind)
    conn.commit()
    store_exchange.repair(conn)
    before = projected(conn)
    conn.execute("DELETE FROM v2_child_exchange")
    conn.execute("DELETE FROM v2_agent_report_state")
    conn.commit()
    store_exchange.repair(conn)
    assert projected(conn) == before
    assert conn.execute("SELECT status FROM v2_agent_report_state").fetchone()[0] == "done"
    conn.execute("UPDATE sessions SET status='closed' WHERE session_name='child'")
    conn.commit()
    store_exchange.repair(conn)
    assert projected(conn) == []
    assert conn.execute("SELECT count(*) FROM v2_agent_report_state").fetchone()[0] == 0


def test_legacy_latest_report_and_existing_transition_timestamp(legacy):
    conn, binding, _ = legacy
    # Natural report-id order differs from acceptance/ledger order.
    receipt(conn, binding, "report", "z-older", status="done")
    receipt(conn, binding, "report", "a-newer", status="progress")
    conn.commit()
    store_exchange.repair(conn)
    state = tuple(conn.execute("SELECT status,ts FROM v2_agent_report_state").fetchone())
    assert state == ("progress", "2026-01-01T00:00:00Z")
    conn.execute("UPDATE v2_agent_report_state SET ts='preserved-transition'")
    receipt(conn, binding, "report", "next", status="progress")
    conn.commit()
    store_exchange.repair(conn)
    assert conn.execute("SELECT ts FROM v2_agent_report_state").fetchone()[0] == "preserved-transition"


@pytest.mark.parametrize("kind", ["tell", "report", "brief"])
@pytest.mark.parametrize("mode", ["legacy", "pending", "rebuild"])
@pytest.mark.parametrize("payload_bytes", [1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1, 2 * 1024 * 1024 + 1])
def test_receipt_budget_boundaries_and_singletons(legacy, monkeypatch, kind, mode, payload_bytes):
    import hashlib
    import json
    conn, binding, _ = legacy
    if mode == "pending":
        store_exchange.repair(conn)
    table, keys, payload = store_exchange._SOURCES[kind]
    expected = {}
    # Oversized first, between and last; adjacent large keys must never share a
    # transaction. Escaping and multibyte text make serialized size significant.
    for identity, large in [("a", True), ("b", False), ("c", True), ("d", True)]:
        text = '界\\"\n' * 2000
        observation = receipt(conn, binding, kind, identity, text=text)
        expected[identity] = (text.encode()[:8192].decode(errors="ignore"),
                              int(len(text.encode()) > 8192), hashlib.sha256(text.encode()).hexdigest())
        if large:
            value = ({"delivery": {"exchange": observation}} if kind == "tell" else
                     {"exchange": observation} if kind == "brief" else observation)
            value["padding"] = ""
            encoded = json.dumps(value, ensure_ascii=False)
            value["padding"] = "x" * (payload_bytes - len(encoded.encode()))
            encoded = json.dumps(value, ensure_ascii=False)
            assert len(encoded.encode()) == payload_bytes
            conn.execute(f"UPDATE {table} SET {payload}=? WHERE {keys[-1]}=?", (encoded, identity))
    conn.commit()
    sources = list(conn.execute(f"SELECT {payload} FROM {table} ORDER BY {','.join(keys)}"))
    batches = capture_repair_batches(conn, monkeypatch, rebuild=mode == "rebuild")
    assert sum(n for n, _ in batches) == 4
    assert all(n <= 100 and (size <= store_exchange._REPAIR_BYTES or n == 1) for n, size in batches)
    if payload_bytes > store_exchange._REPAIR_BYTES:
        assert sum(size == payload_bytes and n == 1 for n, size in batches) == 3
    assert {r[0]: tuple(r)[1:] for r in conn.execute(
        "SELECT ref_id,text,truncated,payload_sha256 FROM v2_child_exchange")} == expected
    assert [tuple(r) for r in sources] == [tuple(r) for r in conn.execute(
        f"SELECT {payload} FROM {table} ORDER BY {','.join(keys)}")]
    assert conn.execute("SELECT count(*) FROM v2_exchange_repair_pending").fetchone()[0] == 0
    if kind == "report":
        assert tuple(conn.execute("SELECT status,ts FROM v2_agent_report_state").fetchone()) == (
            "done", "2026-01-01T00:00:00Z")
    sequences = [tuple(r) for r in conn.execute("SELECT ref_id,seq FROM v2_child_exchange ORDER BY ref_id")]
    if mode == "rebuild":
        conn.execute("UPDATE v2_child_exchange SET text='damaged'")
        conn.commit()
        capture_repair_batches(conn, monkeypatch, rebuild=True)
        assert sequences == [tuple(r) for r in conn.execute("SELECT ref_id,seq FROM v2_child_exchange ORDER BY ref_id")]
        assert {r[0]: tuple(r)[1:] for r in conn.execute(
            "SELECT ref_id,text,truncated,payload_sha256 FROM v2_child_exchange")} == expected
    def no_decode(*args):
        pytest.fail("unchanged restart decoded an accounted receipt")
    monkeypatch.setattr(store_exchange, "_decoded", no_decode)
    store_exchange.repair(conn)


def capture_repair_batches(conn, monkeypatch, *, rebuild=False):
    # Count processed keys, including absent/NULL sources, rather than only
    # successful projections or nonzero JSON payloads.
    batches, current = [], [0, 0]
    project = store_exchange._project_receipt
    def counted(conn, kind, row, rebuilding):
        current[1] += len((row[0] or "").encode())
        return project(conn, kind, row, rebuilding)
    def trace(sql):
        if sql.startswith("DELETE FROM v2_exchange_repair_pending WHERE"):
            current[0] += 1
        if sql.upper() == "COMMIT" and current[0]:
            batches.append(tuple(current))
            current[:] = [0, 0]
    with monkeypatch.context() as patch:
        patch.setattr(store_exchange, "_project_receipt", counted)
        conn.set_trace_callback(trace)
        try:
            store_exchange.repair(conn, rebuild=rebuild)
        finally:
            conn.set_trace_callback(None)
    assert current == [0, 0]
    return batches


@pytest.mark.parametrize("kind", ["tell", "report", "brief"])
@pytest.mark.parametrize("mode", ["legacy", "pending", "rebuild"])
@pytest.mark.parametrize("predecessor", ["null", "missing", "no_exchange", "fenced"])
def test_zero_or_unprojected_predecessor_stays_out_of_singleton(legacy, monkeypatch, kind, mode, predecessor):
    conn, binding, _ = legacy
    if mode == "pending":
        store_exchange.repair(conn)
    table, keys, payload = store_exchange._SOURCES[kind]
    lookup = f"{keys[-1]}=?"
    receipt(conn, binding, kind, "a-zero")
    if predecessor in ("null", "no_exchange"):
        conn.execute(f"UPDATE {table} SET {payload}=? WHERE {lookup}",
                     (("" if kind == "tell" else None) if predecessor == "null" else "{}", "a-zero"))
    elif predecessor == "missing":
        conn.execute(f"DELETE FROM {table} WHERE {lookup}", ("a-zero",))
        # Only pending mode can contain a missing source key.
        store_exchange._initialize_repair(conn, False)
        conn.execute("INSERT OR IGNORE INTO v2_exchange_repair_pending VALUES(?,?,?)",
                     (kind, "test-host" if kind == "brief" else "a-zero", "a-zero" if kind == "brief" else ""))
    else:
        stale = dict(binding, epoch="stale")
        conn.execute(f"DELETE FROM {table} WHERE {lookup}", ("a-zero",))
        receipt(conn, stale, kind, "a-zero")
    receipt(conn, binding, kind, "b-large", text="x" * (2 * 1024 * 1024))
    receipt(conn, binding, kind, "c-later")
    conn.commit()
    batches = capture_repair_batches(conn, monkeypatch, rebuild=mode == "rebuild")
    large = [(n, size) for n, size in batches if size > store_exchange._REPAIR_BYTES]
    assert len(large) == 1 and large[0][0] == 1
    assert {row[1] for row in projected(conn)} == {"b-large", "c-later"}
    assert conn.execute("SELECT count(*) FROM v2_exchange_repair_pending").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["tell", "report", "brief"])
@pytest.mark.parametrize("mode", ["legacy", "pending", "rebuild"])
@pytest.mark.parametrize("fence", ["closed", "reparented", "generation", "no_exchange"])
def test_oversized_lifecycle_invalidated_receipt_is_drained(legacy, monkeypatch, kind, mode, fence):
    conn, binding, _ = legacy
    if mode == "pending":
        store_exchange.repair(conn)
    receipt(conn, binding, kind, "a-large", text="x" * (2 * 1024 * 1024))
    if fence == "generation":
        conn.execute("UPDATE v2_session_generations SET generation='new' WHERE session_name='child'")
    elif fence == "closed":
        conn.execute("UPDATE sessions SET status='closed' WHERE session_name='child'")
    elif fence == "reparented":
        conn.execute("UPDATE sessions SET parent_stream_id='test-host:other' WHERE session_name='child'")
    else:
        table, _, payload = store_exchange._SOURCES[kind]
        conn.execute(f"UPDATE {table} SET {payload}=?", ("{}" + " " * (2 * 1024 * 1024),))
    conn.commit()
    batches = capture_repair_batches(conn, monkeypatch, rebuild=mode == "rebuild")
    assert len(batches) == 1 and batches[0][0] == 1 and batches[0][1] > store_exchange._REPAIR_BYTES
    assert projected(conn) == []
    assert conn.execute("SELECT count(*) FROM v2_exchange_repair_pending").fetchone()[0] == 0


def test_oversized_identity_conflict_still_rolls_back(legacy):
    conn, binding, _ = legacy
    receipt(conn, binding, "tell", "conflict", text="x" * (2 * 1024 * 1024))
    conn.commit()
    store_exchange.repair(conn)
    conn.execute("UPDATE v2_child_exchange SET payload_sha256='conflict'")
    conn.execute("UPDATE v2_tell_deliveries SET created_at=created_at WHERE tell_id='conflict'")
    conn.commit()
    with pytest.raises(ValueError, match="exchange_ref_conflict"):
        store_exchange.repair(conn)
    assert conn.execute("SELECT count(*) FROM v2_exchange_repair_pending").fetchone()[0] == 1


def test_oversized_pending_store_start_is_usable(legacy, monkeypatch):
    import hashlib
    conn, binding, path = legacy
    store_exchange.repair(conn)
    text = "界" * (1024 * 1024)
    receipt(conn, binding, "tell", "startup", text=text)
    conn.commit()
    source = conn.execute("SELECT reply FROM v2_tell_deliveries").fetchone()[0]
    async def use_store():
        store = Store(str(path))
        try:
            store.start()
            await store.put("usable", "yes")
            assert await store.get("usable") == "yes"
        finally:
            assert store.stop()
    asyncio.run(use_store())
    assert tuple(conn.execute("SELECT text,truncated,payload_sha256 FROM v2_child_exchange").fetchone()) == (
        text.encode()[:8192].decode(errors="ignore"), 1, hashlib.sha256(text.encode()).hexdigest())
    assert conn.execute("SELECT reply FROM v2_tell_deliveries").fetchone()[0] == source
    assert conn.execute("SELECT count(*) FROM v2_exchange_repair_pending").fetchone()[0] == 0
    def no_decode(*args):
        pytest.fail("unchanged Store restart decoded an accounted receipt")
    monkeypatch.setattr(store_exchange, "_decoded", no_decode)
    asyncio.run(use_store())


def test_explicit_corruption_rebuild(legacy):
    conn, binding, _ = legacy
    for kind in ("tell", "report", "brief"):
        receipt(conn, binding, kind, kind)
    conn.commit()
    store_exchange.repair(conn)
    before = projected(conn)
    conn.execute("UPDATE v2_child_exchange SET text='corrupt'")
    conn.commit()
    store_exchange.repair(conn, rebuild=True)
    assert projected(conn) == before


@pytest.mark.parametrize("count,text_bytes", [(205, 8), (12, 200_000)])
def test_each_committed_batch_bounds_rows_and_json_bytes(legacy, monkeypatch, count, text_bytes):
    conn, binding, _ = legacy
    for i in range(count):
        receipt(conn, binding, "tell", f"{i:04d}", text="x" * text_bytes)
    conn.commit()
    batches, current = [], [0, 0]
    loads = store_exchange.json.loads
    def counted(value, *args, **kwargs):
        current[0] += 1
        current[1] += len(value.encode()) if isinstance(value, str) else len(value or b"")
        return loads(value, *args, **kwargs)
    def trace(sql):
        if sql.upper() == "COMMIT" and current[0]:
            batches.append(tuple(current))
            current[:] = [0, 0]
    monkeypatch.setattr(store_exchange.json, "loads", counted)
    conn.set_trace_callback(trace)
    try:
        store_exchange.repair(conn)
    finally:
        conn.set_trace_callback(None)
    assert sum(n for n, _ in batches) == count
    assert all(n <= 100 and size <= 1024 * 1024 for n, size in batches)
    assert len(batches) >= 3
    assert current == [0, 0]
    assert len(projected(conn)) == count


def test_source_rollback_and_deletion_do_not_create_evidence(legacy):
    conn, binding, _ = legacy
    store_exchange.repair(conn)
    receipt(conn, binding, "tell", "rolled-back")
    conn.rollback()
    receipt(conn, binding, "tell", "deleted")
    conn.commit()
    conn.execute("DELETE FROM v2_tell_deliveries WHERE tell_id='deleted'")
    conn.commit()
    store_exchange.repair(conn)
    assert projected(conn) == []


@pytest.mark.parametrize("kind", ["tell", "report", "brief"])
def test_empty_source_key_is_not_skipped(legacy, kind):
    conn, binding, _ = legacy
    receipt(conn, binding, kind, "")
    conn.commit()
    store_exchange.repair(conn)
    assert projected(conn) == [(kind, "", "retained")]


@pytest.mark.parametrize("kind,oversized", [("tell", False), ("tell", True), ("report", True), ("brief", True)])
@pytest.mark.parametrize("boundary", ["projection", "checkpoint", "before_commit", "after_commit"])
@pytest.mark.parametrize("pending", [False, True])
def test_abrupt_exit_at_progress_boundaries_resumes(legacy, tmp_path, boundary, pending, kind, oversized):
    import os
    import subprocess
    import sys
    from pathlib import Path
    conn, binding, path = legacy
    if pending:
        store_exchange.repair(conn)
    for i in range(105):
        receipt(conn, binding, kind, f"{i:04d}", text="x" * (2 * 1024 * 1024) if oversized and i == 0 else "retained")
    conn.commit()
    program = r'''
import os,sqlite3,sys
import store_exchange
boundary=sys.argv[2]
checkpoint_sql="DELETE FROM v2_exchange_repair_pending" if sys.argv[3]=="True" else "UPDATE v2_exchange_repair_progress"
armed=False
class CrashConnection(sqlite3.Connection):
    def commit(self):
        if armed and boundary=='before_commit': os._exit(73)
        super().commit()
        if armed and boundary=='after_commit': os._exit(73)
conn=sqlite3.connect(sys.argv[1],factory=CrashConnection)
conn.row_factory=sqlite3.Row
append=store_exchange.append
execute=None
def observed(conn,observation):
    global armed
    result=append(conn,observation)
    armed=True
    if boundary=='projection': os._exit(73)
    return result
def trace(sql):
    if armed and boundary=='checkpoint' and checkpoint_sql in sql:
        os._exit(73)
conn.set_trace_callback(trace)
store_exchange.append=observed
store_exchange.repair(conn)
raise RuntimeError('crash boundary was never reached')
'''
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PENTACLE_", "AGENT_ORCH_", "TMUX", "COSMO_", "V2_"))}
    env["PYTHONPATH"] = str(Path(store_exchange.__file__).parent)
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", program, str(path), boundary, str(pending)], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 73, result.stderr
    partial = projected(conn)
    assert len(partial) == ((1 if oversized else 100) if boundary == "after_commit" else 0)
    if oversized:
        progress = conn.execute("SELECT key1 FROM v2_exchange_repair_progress WHERE source=?", (kind,)).fetchone()[0]
        if not pending:
            assert progress == (("test-host" if kind == "brief" else "0000") if boundary == "after_commit" else None)
        dirty = conn.execute("SELECT count(*) FROM v2_exchange_repair_pending WHERE source=?", (kind,)).fetchone()[0]
        assert dirty == ((104 if boundary == "after_commit" else 105) if pending else 0)
        assert conn.execute("SELECT count(*) FROM v2_agent_report_state").fetchone()[0] == int(kind == "report" and boundary == "after_commit")
    store_exchange.repair(conn)
    assert len(projected(conn)) == 105
    assert len({(kind, ref) for kind, ref, _ in projected(conn)}) == 105
    store_exchange.repair(conn)
    assert len(projected(conn)) == 105


@pytest.mark.parametrize("statuses", [("progress", "progress"), ("done", "aborted")])
def test_exchange_rebuild_preserves_healthy_report_transition(legacy, statuses):
    conn, binding, _ = legacy
    receipt(conn, binding, "report", "first", status=statuses[0])
    receipt(conn, binding, "report", "second", status=statuses[1])
    conn.execute("UPDATE v2_reports SET ingested_at='2026-01-02T00:00:00Z' WHERE report_id='second'")
    conn.execute("INSERT INTO v2_agent_report_state VALUES(?,?,?,?)",
                 (binding["child"], binding["child_generation"], statuses[1], "2026-01-01T00:00:00Z"))
    conn.commit()
    store_exchange.repair(conn)
    sequences = list(conn.execute("SELECT ref_id,seq FROM v2_child_exchange ORDER BY ref_id"))
    conn.execute("UPDATE v2_child_exchange SET text='damaged' WHERE ref_id='first'")
    conn.commit()
    store_exchange.repair(conn, rebuild=True)
    assert conn.execute("SELECT status,ts FROM v2_agent_report_state").fetchone()[:] == (statuses[1], "2026-01-01T00:00:00Z")
    assert [tuple(row) for row in conn.execute("SELECT ref_id,seq FROM v2_child_exchange ORDER BY ref_id")] == [tuple(row) for row in sequences]
    assert all(text == "retained" for _, _, text in projected(conn))


def test_archive_unavailable_keeps_repaired_evidence_and_fails_start(legacy, tmp_path):
    import sqlite3
    from retention import RetentionError
    conn, binding, path = legacy
    receipt(conn, binding, "tell", "retained")
    conn.commit()
    store = Store(str(path), archive_path=str(tmp_path / "absent.db"))
    try:
        with pytest.raises((sqlite3.OperationalError, RetentionError)):
            store.start()
        assert store._start_error is not None
    finally:
        assert store.stop()
    assert projected(conn) == [("tell", "retained", "retained")]
    store = Store(str(path))
    try:
        store.start()
        assert asyncio.run(store.get("absent-key")) is None
    finally:
        assert store.stop()


def test_version_change_rechecks_legacy_without_resetting_healthy_roster(legacy, monkeypatch):
    conn, binding, _ = legacy
    receipt(conn, binding, "report", "kept")
    conn.commit()
    store_exchange.repair(conn)
    conn.execute("DROP TRIGGER v2_exchange_repair_deleted")
    conn.execute("DELETE FROM v2_child_exchange")
    conn.execute("UPDATE v2_agent_report_state SET ts='original-transition'")
    conn.commit()
    monkeypatch.setattr(store_exchange, "_REPAIR_VERSION", store_exchange._REPAIR_VERSION + 1)
    store_exchange.repair(conn)
    assert projected(conn) == [("report", "kept", "retained")]
    assert conn.execute("SELECT ts FROM v2_agent_report_state").fetchone()[0] == "original-transition"


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit", "completion"])
def test_flagged_rebuild_resumes_after_crash_with_same_sequences(legacy, tmp_path, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path
    conn, binding, path = legacy
    for i in range(100):
        receipt(conn, binding, "tell", f"{i:04d}")
    conn.commit()
    store_exchange.repair(conn)
    sequences = [tuple(row) for row in conn.execute("SELECT ref_id,seq FROM v2_child_exchange ORDER BY ref_id")]
    conn.execute("UPDATE v2_child_exchange SET text='damaged'")
    conn.commit()
    program = r'''
import os,sqlite3,sys
import store_exchange
boundary=sys.argv[2]
armed=False
class CrashConnection(sqlite3.Connection):
    def commit(self):
        if armed and boundary=='before_commit': os._exit(73)
        super().commit()
        if armed and boundary=='after_commit': os._exit(73)
conn=sqlite3.connect(sys.argv[1],factory=CrashConnection)
conn.row_factory=sqlite3.Row
append=store_exchange.append
def observed(conn,observation):
    global armed
    result=append(conn,observation)
    armed=True
    return result
def trace(sql):
    if armed and boundary=='completion' and 'SET complete=1,rebuilding=0' in sql:
        os._exit(73)
conn.set_trace_callback(trace)
store_exchange.append=observed
store_exchange.repair(conn,rebuild=True)
raise RuntimeError('crash boundary was never reached')
'''
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PENTACLE_", "AGENT_ORCH_", "TMUX", "COSMO_", "V2_"))}
    env["PYTHONPATH"] = str(Path(store_exchange.__file__).parent)
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-c", program, str(path), boundary], env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 73, result.stderr
    assert {text for _, _, text in projected(conn)} == ({"damaged"} if boundary == "before_commit" else {"retained"})
    store_exchange.repair(conn)
    assert {text for _, _, text in projected(conn)} == {"retained"}
    assert [tuple(row) for row in conn.execute("SELECT ref_id,seq FROM v2_child_exchange ORDER BY ref_id")] == sequences


def test_rebuild_stale_source_cannot_replace_current_epoch(legacy):
    import json
    conn, binding, _ = legacy
    observation = receipt(conn, binding, "tell", "same-id")
    conn.commit()
    store_exchange.repair(conn)
    conn.execute("UPDATE sessions SET parent_stream_id=NULL WHERE session_name='child'")
    conn.execute("UPDATE sessions SET parent_stream_id='test-host:parent' WHERE session_name='child'")
    current = store_exchange.direct_pair(conn, "test-host:parent", "test-host:child")
    replacement = store_exchange.source(current, "tell", "same-id", "current epoch", observation["ts"])
    store_exchange.append(conn, replacement)
    conn.commit()
    assert current["epoch"] != binding["epoch"]
    store_exchange.repair(conn, rebuild=True)
    assert projected(conn) == [("tell", "same-id", "current epoch")]


def test_socket_restart_keeps_replay_identity_and_never_redelivers(tmp_path):
    import hashlib
    import json
    import websockets
    from comms import Comms
    from ledger import Ledger
    from routing_integrity import RoutingIntegrity
    from server import Server
    from sessions import Sessions
    from spawnctl import SpawnCtl
    from store import STREAM_TOKEN_HASH_VERSION
    from tests.test_tell_ledger_row_id import EchoTmux

    class CounterTmux(EchoTmux):
        def __init__(self):
            super().__init__()
            self.pastes = 0
        async def paste(self, name, text):
            self.pastes += 1
            await super().paste(name, text)

    async def run():
        path = str(tmp_path / "socket-store.db")
        tmux = CounterTmux()
        first_tell, first_page = None, None
        stopped, closed = 0, 0
        for boot in range(2):
            store = Store(path)
            daemon = None
            store.start()
            assert store._ready.is_set() and store._start_error is None
            try:
                sessions = Sessions(store, tmux=tmux, local_host="test-host")
                if not boot:
                    await sessions.open("test-host", "parent", provider="codex")
                    await sessions.open("test-host", "child", provider="codex", parent_stream_id="test-host:parent")
                    for name in ("parent", "child"):
                        await store.grant_stream_token("test-host", name, hashlib.sha256((name + "-token").encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
                else:
                    await sessions.refresh()
                ctl = SpawnCtl(store, sessions, tmux=tmux)
                comms = Comms(store, sessions, ctl)
                ledger = Ledger(store, sessions=sessions, routing_integrity=RoutingIntegrity(store, sessions))
                daemon = Server(port=0, store=store, sessions=sessions, spawnctl=ctl,
                                comms=comms, ledger=ledger, local_host="test-host")
                port = await daemon.bind()
                async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                    assert json.loads(await ws.recv())["type"] == "welcome"
                    async def rpc(payload):
                        await ws.send(json.dumps(payload))
                        # The fake pane has no native USER proof; Comms owns a 4 s
                        # bounded proof wait before its truthful pending receipt.
                        async with asyncio.timeout(10):
                            while True:
                                frame = json.loads(await ws.recv())
                                assert frame.get("type") != "error", frame
                                if frame.get("request_id") == payload["request_id"] or frame.get("type", "").startswith(payload["type"] + "."):
                                    return frame
                    tell = dict(type="tell", request_id="tell-request", tell_id="socket-tell",
                                stream_id="test-host:child", from_stream_id="test-host:parent",
                                stream_token="parent-token", message="Synthetic retained message")
                    told = await rpc(tell)
                    assert told["type"] == "tell.ok", told
                    if not boot:
                        first_tell = told
                    else:
                        assert told["duplicate"] and told["ledger_row_id"] == first_tell["ledger_row_id"]
                    report = await rpc(dict(type="report", request_id="report-request", report_id="socket-report",
                                            from_stream_id="test-host:child", stream_token="child-token",
                                            msg_id=0, status="progress", summary="Synthetic progress", findings=[], next_action="continue"))
                    assert report["type"] == "report.ok", report
                    page = await rpc(dict(type="thread.read", request_id="thread-request", child_stream_id="test-host:child",
                                          stream_token="parent-token"))
                    assert page["type"] == "thread.read.ok", page
                    if not boot:
                        first_page = page["rows"]
                        assert {row["kind"] for row in first_page} == {"tell", "report"}
                        await store.submit(lambda c: (c.execute("DELETE FROM v2_child_exchange"), c.commit()))
                    else:
                        assert page["rows"] == first_page
                        assert tmux.pastes == 1
            finally:
                if daemon:
                    await daemon.close()
                    assert daemon._ws_server is None
                    closed += 1
                assert store.stop()
                assert store._thread is None
                stopped += 1
        assert closed == stopped == 2
    asyncio.run(run())


@pytest.mark.parametrize("value", ["{invalid", "[]", "null", '"scalar"'])
def test_malformed_or_nonobject_payloads_keep_legacy_semantics(legacy, value):
    conn, binding, _ = legacy
    for kind in ("tell", "report", "brief"):
        receipt(conn, binding, kind, kind)
    conn.execute("UPDATE v2_tell_deliveries SET reply=?", (value,))
    conn.execute("UPDATE v2_reports SET exchange_json=?", (value,))
    conn.execute("UPDATE v2_spawn_outcomes SET delivery_receipt=?", (value,))
    conn.commit()
    store_exchange.repair(conn)
    assert projected(conn) == []
    assert conn.execute("SELECT status FROM v2_agent_report_state").fetchone()[0] == "done"
    store_exchange.repair(conn)
    assert projected(conn) == []
