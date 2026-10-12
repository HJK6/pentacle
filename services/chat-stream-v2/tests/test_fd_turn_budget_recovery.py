"""Synthetic, file-backed single-allowance recovery contract.

No sockets, service processes, private archives, or real identities are used.
The reader and SQLite hooks measure work directly, never through latency.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import sqlite3
import threading

import pytest

import fd_turn_budget
import store as store_module
from fd_turn_budget import FdTurnBudgetConfig
from store import SESSION_EVENT_TAIL_DDL, Store
from test_fd_turn_budget import (
    append, budget_store, counts, normalized, peer_receipt, peer_wire,
)


LIMIT = 100_000
SID = "fixture:fd"
pytestmark = pytest.mark.timeout(20)


def make_archive(store):
    archive = Path(store.path).with_name("sessions_archive.db")
    with sqlite3.connect(archive) as conn:
        conn.execute(SESSION_EVENT_TAIL_DDL)
    return archive


async def pending_turn(store, *, enabled=True, binding=None, completed=False):
    if binding is not None:
        store.configure_fd_turn_budget(
            config=FdTurnBudgetConfig(enabled=enabled), env_binding=binding)
    await append(store, normalized("USER", 0, body=peer_wire()))
    for n in range(1, 7):
        await append(store, normalized("TOOL_USE", n))
    if completed:
        await append(store, normalized("USER", 7))
    state = await store.fd_turn_budget_status(SID)
    assert state["continuity_gap"] == "peer_provenance_pending"
    assert state["ordinal"] == 0
    assert await counts(store) == (0, 0)
    return state


def recovery_state(state, *, status=None):
    claim = state.get("recovery")
    assert isinstance(claim, dict), "recovery must persist its one lifetime allowance"
    assert claim["spent"] == LIMIT
    assert type(claim["used"]) is int and 0 <= claim["used"] <= LIMIT
    assert claim["status"] in {"claimed", "succeeded", "failed"}
    if status is not None:
        assert claim["status"] == status
    return claim


def raw_cursor(store, connect=sqlite3.connect):
    # An independent connection proves visibility outside the claiming writer.
    with connect(store.path) as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?",
                           (store._fd_cursor_key(SID),)).fetchone()
    return json.loads(row[0]) if row else None


def spy_reader(store, monkeypatch, *, replacement=None):
    calls = []
    reader = store._fd_archive_evidence
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        calls.append({"thread": threading.get_ident(),
                      "claim": raw_cursor(store, connect)})
        if replacement is not None:
            return replacement(reader, *args, **kwargs)
        return reader(*args, **kwargs)

    monkeypatch.setattr(store, "_fd_archive_evidence", tracked)
    return calls


class ReaderSQL:
    """Capture exact executed reader SQL and actual opened archive targets."""
    def __init__(self, store, monkeypatch):
        self.archive = Path(store.path).with_name("sessions_archive.db")
        self.connect = sqlite3.connect
        self.opens = []
        self.statements = []

        def traced(database, *args, **kwargs):
            conn = self.connect(database, *args, **kwargs)
            target = str(database)
            if str(self.archive) in target or (str(store.path) in target and "mode=ro" in target):
                self.opens.append((target, threading.get_ident()))
                conn.set_trace_callback(
                    lambda sql: self.statements.append((target, sql, threading.get_ident())))
            return conn

        monkeypatch.setattr(store_module.sqlite3, "connect", traced)

    def event_queries(self):
        return [(target, sql) for target, sql, _ in self.statements
                if sql.lstrip().upper().startswith("SELECT")
                and "session_event_tail" in sql.lower()]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_kind", ["tell", "send"])
async def test_recovery_waits_for_exact_durable_receipt(budget_store, monkeypatch, receipt_kind):
    store, _ = budget_store
    make_archive(store)
    calls = spy_reader(store, monkeypatch)
    await pending_turn(store)
    assert calls == [], "an unproved pending anchor must not schedule a reader"
    assert not (await store.fd_turn_budget_status(SID)).get("recovery")

    # Neither a different anchor nor a matching anchor with different content
    # makes the pending source's exact provenance durable.
    await peer_receipt(store, anchor="unrelated-peer", body="Other synthetic dispatch")
    await store.append_send_receipt(
        to_stream_id=SID, request_id="peer-1", receipt_id="synthetic-wrong-body",
        state="landed", wire_text="Wrong synthetic body", display_text="Wrong synthetic body",
        attachments=[], delivery="landed", submission_confirmed=True,
        from_stream_id="fixture:peer", actor_stream_id="fixture:peer", actor_trusted=True)
    assert calls == []
    assert not (await store.fd_turn_budget_status(SID)).get("recovery")
    assert await counts(store) == (0, 0)

    if receipt_kind == "tell":
        await peer_receipt(store)
    else:
        await store.append_send_receipt(
            to_stream_id=SID, request_id="peer-1", receipt_id="synthetic-send-proof",
            state="landed", wire_text=peer_wire(), display_text="Peer one",
            attachments=[], delivery="landed", submission_confirmed=True,
            from_stream_id="fixture:peer", actor_stream_id="fixture:peer", actor_trusted=True)
    assert len(calls) == 1
    state = await store.fd_turn_budget_status(SID)
    recovery_state(state, status="succeeded")
    assert not state["continuity_gap"]
    assert await counts(store) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,completed", [(True, False), (False, False), (True, True)])
async def test_single_recovery_claim_is_durable_before_offthread_work(
        budget_store, monkeypatch, enabled, completed):
    store, binding = budget_store
    make_archive(store)
    await pending_turn(store, enabled=enabled, binding=binding, completed=completed)
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
    main_thread = threading.get_ident()
    store_thread = await store.submit(lambda _conn: threading.get_ident())
    calls = spy_reader(store, monkeypatch)
    sql = ReaderSQL(store, monkeypatch)
    await peer_receipt(store)

    assert len(calls) == 1
    assert calls[0]["thread"] not in {main_thread, store_thread}
    recovery_state(calls[0]["claim"], status="claimed")
    state = await store.fd_turn_budget_status(SID)
    claim = recovery_state(state, status="succeeded")
    assert claim["used"] > 0
    assert not state["continuity_gap"]
    assert state["ordinal"] == (2 if completed else 1)
    assert await counts(store) == (1, int(enabled and not completed))
    for _target, _sql, thread in sql.statements:
        assert thread not in {main_thread, store_thread}
    before = json.dumps(claim, sort_keys=True)
    await peer_receipt(store)
    await append(store, normalized("TOOL_USE", 8))
    assert len(calls) == 1
    assert json.dumps(recovery_state(await store.fd_turn_budget_status(SID)), sort_keys=True) == before
    assert await counts(store) == (1, int(enabled and not completed))


@pytest.mark.asyncio
async def test_one_allowance_survives_restart_receipt_replay_and_later_gap(budget_store, monkeypatch):
    store, binding = budget_store
    make_archive(store)
    await pending_turn(store)
    calls = spy_reader(store, monkeypatch)
    await peer_receipt(store)
    claim = dict(recovery_state(await store.fd_turn_budget_status(SID), status="succeeded"))
    assert len(calls) == 1
    path = store.path
    store.stop()
    reopened = Store(path)
    reopened.start()
    try:
        reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
        reopened_calls = spy_reader(reopened, monkeypatch)
        await peer_receipt(reopened)
        for n in range(7):
            await append(reopened, normalized("USER", n, body=peer_wire())
                         if n == 0 else normalized("TOOL_USE", n))
        assert recovery_state(await reopened.fd_turn_budget_status(SID)) == claim
        assert reopened_calls == []

        from comms import Comms
        second_wire = Comms._peer_delivery_wire(
            {"from_stream_id": "fixture:peer",
             "_auth_context": {"token_verified": True, "stream_id": "fixture:peer"}},
            "tell", "second-peer", "Second synthetic dispatch")
        await append(reopened, normalized("USER", 20, body=second_wire))
        await peer_receipt(reopened, anchor="second-peer", body="Second synthetic dispatch")
        for n in range(21, 28):
            await append(reopened, normalized("TOOL_USE", n))
        state = await reopened.fd_turn_budget_status(SID)
        assert state["continuity_gap"]
        assert recovery_state(state) == claim
        assert reopened_calls == []
        assert await counts(reopened) == (1, 1)
    finally:
        reopened.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reader_crash", "allowance_exhausted"])
async def test_failed_recovery_is_sticky_across_restarts(budget_store, monkeypatch, failure):
    store, binding = budget_store
    make_archive(store)
    await pending_turn(store)

    def fail(_reader, *_args, **_kwargs):
        if failure == "allowance_exhausted":
            budget = _args[0]["budget"]
            budget.charge(LIMIT - budget.used)
            budget.charge(1)
        raise RuntimeError("invented recovery crash after durable claim")

    calls = spy_reader(store, monkeypatch, replacement=fail)
    await peer_receipt(store)
    assert len(calls) == 1
    recovery_state(calls[0]["claim"], status="claimed")
    state = await store.fd_turn_budget_status(SID)
    assert state["continuity_gap"]
    claim = dict(recovery_state(state, status="failed"))
    assert claim.get("reason")
    if failure == "allowance_exhausted":
        assert claim["used"] == LIMIT
    assert await counts(store) == (0, 0)
    path = store.path
    store.stop()
    for _ in range(3):
        reopened = Store(path)
        reopened.start()
        try:
            reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
            reads = spy_reader(reopened, monkeypatch)
            await peer_receipt(reopened)
            await append(reopened, normalized("TOOL_USE", 8))
            assert reads == []
            current = await reopened.fd_turn_budget_status(SID)
            assert current["continuity_gap"] == state["continuity_gap"]
            assert recovery_state(current) == claim
            assert await counts(reopened) == (0, 0)
        finally:
            reopened.stop()


@pytest.mark.asyncio
async def test_cancelled_recovery_claim_is_never_renewed(budget_store, monkeypatch):
    store, binding = budget_store
    make_archive(store)
    await pending_turn(store)
    reached_reader = threading.Event()
    release_reader = threading.Event()
    finished_reader = threading.Event()
    original_reader = store._fd_archive_evidence
    claims = []

    def blocked(*args, **kwargs):
        claims.append(raw_cursor(store))
        reached_reader.set()
        try:
            assert release_reader.wait(5), "synthetic reader was not released by cancellation cleanup"
            return original_reader(*args, **kwargs)
        finally:
            finished_reader.set()

    monkeypatch.setattr(store, "_fd_archive_evidence", blocked)
    task = asyncio.create_task(peer_receipt(store))
    try:
        assert await asyncio.to_thread(reached_reader.wait, 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        release_reader.set()
        # Join the exact owned reader before stopping the owned Store.
        assert await asyncio.to_thread(finished_reader.wait, 5)
    assert len(claims) == 1
    recovery_state(claims[0], status="claimed")
    path = store.path
    store.stop()
    reopened = Store(path)
    reopened.start()
    try:
        reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
        calls = spy_reader(reopened, monkeypatch)
        await peer_receipt(reopened)
        await append(reopened, normalized("TOOL_USE", 8))
        assert calls == []
        state = await reopened.fd_turn_budget_status(SID)
        assert state["continuity_gap"]
        recovery_state(state)
        assert await counts(reopened) == (0, 0)
    finally:
        reopened.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["binding", "generation", "lifecycle", "cursor", "source", "source_disappears"])
async def test_recovery_commit_revalidates_all_original_evidence(budget_store, monkeypatch, changed):
    store, binding = budget_store
    make_archive(store)
    await pending_turn(store)
    original = store._fd_archive_evidence
    connect = sqlite3.connect
    reader_calls = []

    def raced(*args, **kwargs):
        result = original(*args, **kwargs)
        reader_calls.append(threading.get_ident())
        if changed == "binding":
            store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),
                                           env_binding={"stream_id": "fixture:elsewhere", "generation": "other"})
        else:
            with connect(store.path) as conn:
                if changed == "generation":
                    conn.execute("UPDATE v2_session_generations SET generation='replacement-generation' "
                                 "WHERE host='fixture' AND session_name='fd'")
                elif changed == "lifecycle":
                    conn.execute("UPDATE sessions SET created_at='2026-10-11T00:00:00Z' "
                                 "WHERE host='fixture' AND session_name='fd'")
                elif changed == "cursor":
                    row = conn.execute("SELECT v FROM kv WHERE k=?", (store._fd_cursor_key(SID),)).fetchone()
                    value = json.loads(row[0])
                    value["commit_fence_marker"] = "changed after reader"
                    conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(value), store._fd_cursor_key(SID)))
                elif changed == "source_disappears":
                    conn.execute("DELETE FROM session_event_tail WHERE event_id=(SELECT max(event_id) "
                                 "FROM session_event_tail WHERE stream_id=?)", (SID,))
                else:
                    row = conn.execute("SELECT event_id,event_json FROM session_event_tail "
                                       "WHERE stream_id=? ORDER BY event_id DESC LIMIT 1", (SID,)).fetchone()
                    value = json.loads(row[1])
                    value["raw"]["tool_use_id"] = "synthetic-mutated-source"
                    conn.execute("UPDATE session_event_tail SET event_json=? WHERE event_id=?",
                                 (json.dumps(value), row[0]))
        return result

    monkeypatch.setattr(store, "_fd_archive_evidence", raced)
    await peer_receipt(store)
    assert len(reader_calls) == 1
    assert await counts(store) == (0, 0)
    state = await store.fd_turn_budget_status(SID)
    assert state["continuity_gap"]
    assert state["ordinal"] == 0
    assert recovery_state(state)["status"] != "succeeded"
    if changed == "cursor":
        assert state["commit_fence_marker"] == "changed after reader"


@pytest.mark.asyncio
async def test_mixed_hot_archive_recovery_uses_pk_then_refuses_unfenced_sources(budget_store, monkeypatch):
    store, binding = budget_store
    archive = make_archive(store)
    state = await pending_turn(store)
    moved_id = state["pending_proofs"][1][0]

    def move(conn):
        conn.execute("ATTACH DATABASE ? AS synthetic_archive", (str(archive),))
        conn.execute("INSERT INTO synthetic_archive.session_event_tail SELECT * FROM session_event_tail WHERE event_id=?", (moved_id,))
        conn.execute("DELETE FROM session_event_tail WHERE event_id=?", (moved_id,))
        conn.commit()
        conn.execute("DETACH DATABASE synthetic_archive")

    await store.submit(move)
    sql = ReaderSQL(store, monkeypatch)
    calls = spy_reader(store, monkeypatch)
    await peer_receipt(store)
    state = await store.fd_turn_budget_status(SID)
    claim = dict(recovery_state(state, status="failed"))
    assert claim["reason"] == "recovery_archive_fence_unavailable"
    assert 0 < claim["used"] <= LIMIT
    assert state["continuity_gap"] == "recovery_archive_fence_unavailable"
    assert state["ordinal"] == 0 and await counts(store) == (0, 0)
    assert len(calls) == 1
    archive_queries = [(target, query) for target, query in sql.event_queries() if str(archive) in target]
    assert archive_queries, "generated archived source must actually be consulted"
    for target, query in sql.event_queries():
        assert not re.search(r"\bjson_\w+\s*\(", query, re.I)
        assert re.search(r"\bevent_id\s*(?:=|IN\s*\()", query, re.I), query
        with sql.connect(target, uri=target.startswith("file:")) as conn:
            plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + query)]
        assert any("INTEGER PRIMARY KEY" in detail for detail in plan), plan
        assert not any("SCAN session_event_tail" in detail for detail in plan), plan

    path = store.path
    store.stop()
    reopened = Store(path)
    reopened.start()
    try:
        reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
        replayed = spy_reader(reopened, monkeypatch)
        await peer_receipt(reopened)
        await append(reopened, normalized("TOOL_USE", 8))
        assert replayed == []
        current = await reopened.fd_turn_budget_status(SID)
        assert current["continuity_gap"] == "recovery_archive_fence_unavailable"
        assert recovery_state(current) == claim
        assert current["ordinal"] == 0 and await counts(reopened) == (0, 0)
    finally:
        reopened.stop()


@pytest.mark.asyncio
async def test_oversized_source_is_not_fetched_or_decoded_by_recovery(budget_store, monkeypatch):
    store, _ = budget_store
    make_archive(store)
    await append(store, normalized("USER", 0, body=peer_wire()))
    huge = normalized("TOOL_USE", 1)
    huge["raw"]["synthetic_large_evidence"] = "z" * (LIMIT + 1)
    await append(store, huge)
    source_id = (await store.fd_turn_budget_status(SID))["cursor"]
    sql = ReaderSQL(store, monkeypatch)
    loads = json.loads
    decoded = []

    def checked_loads(value, *args, **kwargs):
        if isinstance(value, (str, bytes)) and "synthetic_large_evidence" in str(value):
            decoded.append(threading.get_ident())
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(json, "loads", checked_loads)
    await peer_receipt(store)
    state = await store.fd_turn_budget_status(SID)
    assert state["continuity_gap"]
    recovery_state(state, status="failed")
    assert decoded == [], "oversized source was decoded before its raw-byte charge was admitted"
    for _target, query in sql.event_queries():
        if re.search(rf"\bevent_id\s*=\s*{source_id}\b", query, re.I):
            projection = query[:query.upper().index(" FROM ")]
            without_sizes = re.sub(
                r"length\s*\(\s*CAST\s*\(\s*event_json\s+AS\s+BLOB\s*\)\s*\)",
                "", projection, flags=re.I)
            assert not re.search(r"\bevent_json\b|(?:SELECT|,)\s*\*", without_sizes, re.I), query
    assert await counts(store) == (0, 0)


def test_recovery_budget_precharges_bytes_and_never_exceeds_allowance():
    budget = fd_turn_budget.RecoveryBudget()
    assert budget.limit == LIMIT and budget.used == 0
    budget.charge(LIMIT - 3)
    assert budget.used == LIMIT - 3
    with pytest.raises(fd_turn_budget.RecoveryBudgetExceeded):
        budget.charge(4)
    assert budget.used <= LIMIT


def test_recovery_budget_reserves_last_vm_instructions_before_running():
    budget = fd_turn_budget.RecoveryBudget()
    budget.charge(LIMIT - 3)
    with sqlite3.connect(":memory:") as conn:
        budget.attach(conn)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute("WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
                         "SELECT n+1 FROM numbers WHERE n<1000) SELECT sum(n) FROM numbers").fetchone()
    assert LIMIT - 3 <= budget.used <= LIMIT


@pytest.mark.asyncio
async def test_every_pending_anchor_requires_its_exact_receipt_before_spending(budget_store, monkeypatch):
    store, _ = budget_store
    make_archive(store)
    from comms import Comms
    second = Comms._peer_delivery_wire(
        {"from_stream_id": "fixture:peer",
         "_auth_context": {"token_verified": True, "stream_id": "fixture:peer"}},
        "tell", "second-peer", "Second synthetic dispatch")
    await append(store, normalized("USER", 0, body=peer_wire()))
    await append(store, normalized("USER", 1, body=second))
    calls = spy_reader(store, monkeypatch)
    await peer_receipt(store)
    state = await store.fd_turn_budget_status(SID)
    assert calls == []
    assert not state.get("recovery")
    assert state["continuity_gap"] == "peer_provenance_pending"
    await peer_receipt(store, anchor="second-peer", body="Second synthetic dispatch")
    state = await store.fd_turn_budget_status(SID)
    assert len(calls) == 1
    recovery_state(state, status="succeeded")
    assert not state["continuity_gap"] and state["ordinal"] == 2
    assert await counts(store) == (0, 0)


@pytest.mark.asyncio
async def test_concurrent_eligible_receipts_claim_once(budget_store, monkeypatch):
    store, _ = budget_store
    make_archive(store)
    await pending_turn(store)
    calls = spy_reader(store, monkeypatch)
    await asyncio.gather(*(peer_receipt(store) for _ in range(8)))
    assert len(calls) == 1
    recovery_state(calls[0]["claim"], status="claimed")
    state = await store.fd_turn_budget_status(SID)
    recovery_state(state, status="succeeded")
    assert not state["continuity_gap"]
    assert await counts(store) == (1, 1)


@pytest.mark.asyncio
async def test_recovery_claim_survives_cursor_replacement_and_old_key_rebind(budget_store, monkeypatch):
    store, binding = budget_store
    make_archive(store)
    old_pending = await pending_turn(store)
    calls = spy_reader(store, monkeypatch)
    await peer_receipt(store)
    old_claim = dict(recovery_state(await store.fd_turn_budget_status(SID), status="succeeded"))
    assert len(calls) == 1

    newer = await store.open_session("fixture", "fd", provider="claude", pane_status="pane_alive", pane_pid="124")
    assert newer["session_generation"] != binding["generation"]
    newer_binding = {"stream_id": SID, "generation": newer["session_generation"]}
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=newer_binding)
    await append(store, normalized("USER", 20))
    current = await store.fd_turn_budget_status(SID)
    assert current["generation"] == newer_binding["generation"]
    assert not current["continuity_gap"]

    # Restore a saved old cursor and its exact authoritative K to reproduce a
    # rebind/restart. Its per-K spend record must survive cursor replacement.
    def restore_old_cursor(conn):
        conn.execute("UPDATE v2_session_generations SET generation=? WHERE host='fixture' AND session_name='fd'",
                     (binding["generation"],))
        conn.execute("UPDATE sessions SET created_at=? WHERE host='fixture' AND session_name='fd'",
                     (old_pending["lifecycle"],))
        conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(old_pending), store._fd_cursor_key(SID)))
        conn.commit()

    await store.submit(restore_old_cursor)
    path = store.path
    store.stop()
    reopened = Store(path)
    reopened.start()
    try:
        reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
        reads = spy_reader(reopened, monkeypatch)
        await peer_receipt(reopened)
        await append(reopened, normalized("TOOL_USE", 8))
        assert reads == []
        state = await reopened.fd_turn_budget_status(SID)
        assert state["continuity_gap"]
        assert recovery_state(state) == old_claim
        assert await counts(reopened) == (1, 1)
    finally:
        reopened.stop()


@pytest.mark.asyncio
async def test_duplicate_receipt_observer_reads_are_fixed_indexed_and_bounded(
        budget_store, monkeypatch):
    store, _ = budget_store
    make_archive(store)
    await pending_turn(store)
    await peer_receipt(store)
    claim = recovery_state(await store.fd_turn_budget_status(SID), status="succeeded")
    calls = spy_reader(store, monkeypatch)
    preparations = []
    prepare = store._fd_prepare

    def tracked_prepare(*args, **kwargs):
        preparations.append(True)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(store, "_fd_prepare", tracked_prepare)
    retry = store._fd_retry_pending
    callback_sql = []

    async def traced_retry(*args, **kwargs):
        statements = []
        await store.submit(lambda conn: conn.set_trace_callback(statements.append))
        try:
            return await retry(*args, **kwargs)
        finally:
            await store.submit(lambda conn: conn.set_trace_callback(None))
            callback_sql.append(statements)

    monkeypatch.setattr(store, "_fd_retry_pending", traced_retry)
    await peer_receipt(store)

    def grow_history(conn):
        source = conn.execute("SELECT * FROM session_event_tail WHERE identity NOT LIKE 'fd-turn-budget:%' LIMIT 1").fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(session_event_tail)") if row[1] != "event_id"]
        placeholders = ",".join("?" for _ in columns)
        for n in range(2000):
            row = {key: source[key] for key in columns}
            row["identity"] = f"generated-retained-history-{n}"
            row["event_key"] = f"generated-retained-key-{n}"
            conn.execute(f"INSERT INTO session_event_tail ({','.join(columns)}) VALUES ({placeholders})",
                         [row[key] for key in columns])
        conn.commit()

    await store.submit(grow_history)
    await peer_receipt(store)
    assert calls == [] and preparations == []
    assert recovery_state(await store.fd_turn_budget_status(SID)) == claim
    assert len(callback_sql) == 2
    selected = [[sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
                for statements in callback_sql]
    assert 0 < len(selected[0]) == len(selected[1])
    for statements in callback_sql:
        assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", sql, re.I)
                       for sql in statements), statements
        assert not any("session_event_tail" in sql.lower() for sql in statements), statements
    for sql in selected[0] + selected[1]:
        def inspect(conn):
            return ([row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)],
                    len(conn.execute(sql).fetchall()))
        plan, rows = await store.submit(inspect)
        assert rows <= 1, (sql, rows)
        assert any("SEARCH " in detail for detail in plan), (sql, plan)
        assert not any("SCAN " in detail for detail in plan), (sql, plan)


@pytest.mark.asyncio
async def test_utf8_source_decode_and_hash_bytes_are_charged_before_each_operation(
        budget_store, monkeypatch):
    """Raw UTF-8 costs bytes for both decode and hash, never character count."""
    import hashlib
    store, _ = budget_store
    make_archive(store)
    await append(store, normalized("USER", 0, body=peer_wire()))
    for n in range(1, 7):
        item = normalized("TOOL_USE", n)
        item["raw"]["synthetic_multibyte"] = "雪" * 200
        await append(store, item)

    def unicode_bodies(conn):
        row = conn.execute("SELECT v FROM kv WHERE k=?", (store._fd_cursor_key(SID),)).fetchone()
        state = json.loads(row[0])
        blobs = []
        for proof in state["pending_proofs"]:
            source = conn.execute("SELECT event_json FROM session_event_tail WHERE event_id=?", (proof[0],)).fetchone()
            text = json.dumps(json.loads(source[0]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            raw = text.encode("utf-8")
            blobs.append(raw)
            conn.execute("UPDATE session_event_tail SET event_json=? WHERE event_id=?", (text, proof[0]))
            proof[2] = hashlib.sha256(raw).hexdigest()
        state["cursor_hash"] = state["pending_proofs"][-1][2]
        conn.execute("UPDATE kv SET v=? WHERE k=?", (json.dumps(state), store._fd_cursor_key(SID)))
        conn.commit()
        return set(blobs)

    source_blobs = await store.submit(unicode_bodies)
    assert any(len(blob) > len(blob.decode("utf-8")) for blob in source_blobs)
    events = []
    active = {}
    charge = fd_turn_budget.RecoveryBudget.charge
    loads = json.loads
    sha256 = hashlib.sha256
    reader = store._fd_archive_evidence

    def charged(budget, amount):
        result = charge(budget, amount)
        if active.get(threading.get_ident()) is budget:
            events.append(("charge", amount))
        return result

    def loaded(value, *args, **kwargs):
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if threading.get_ident() in active and isinstance(raw, bytes) and raw in source_blobs:
            events.append(("decode", len(raw)))
        return loads(value, *args, **kwargs)

    def hashed(value=b"", *args, **kwargs):
        if threading.get_ident() in active and isinstance(value, bytes) and value in source_blobs:
            events.append(("hash", len(value)))
        return sha256(value, *args, **kwargs)

    def tracked(snapshot, *args, **kwargs):
        active[threading.get_ident()] = snapshot["budget"]
        try:
            return reader(snapshot, *args, **kwargs)
        finally:
            active.pop(threading.get_ident())

    monkeypatch.setattr(fd_turn_budget.RecoveryBudget, "charge", charged)
    monkeypatch.setattr(json, "loads", loaded)
    monkeypatch.setattr(hashlib, "sha256", hashed)
    monkeypatch.setattr(store, "_fd_archive_evidence", tracked)
    await peer_receipt(store)
    state = await store.fd_turn_budget_status(SID)
    claim = recovery_state(state, status="succeeded")
    assert not state["continuity_gap"] and await counts(store) == (1, 1)
    assert sum(kind == "decode" for kind, _amount in events) >= len(source_blobs)
    assert sum(kind == "hash" for kind, _amount in events) >= len(source_blobs)
    # VM instructions arrive as single-unit charges. They cannot stand in for
    # precharged raw bytes; each observed decode/hash consumes byte credit.
    credit = 0
    for kind, amount in events:
        if kind == "charge" and amount > 1:
            credit += amount
        elif kind in {"decode", "hash"}:
            credit -= amount
            assert credit >= 0, "raw evidence was processed before its full byte charge"
    assert claim["used"] >= 2 * sum(len(blob) for blob in source_blobs)


@pytest.mark.asyncio
async def test_archived_source_change_after_read_rejects_recovery_commit(budget_store, monkeypatch):
    store, _ = budget_store
    archive = make_archive(store)
    state = await pending_turn(store)
    moved_id = state["pending_proofs"][1][0]

    def move(conn):
        conn.execute("ATTACH DATABASE ? AS synthetic_archive", (str(archive),))
        conn.execute("INSERT INTO synthetic_archive.session_event_tail SELECT * FROM session_event_tail WHERE event_id=?", (moved_id,))
        conn.execute("DELETE FROM session_event_tail WHERE event_id=?", (moved_id,))
        conn.commit()
        conn.execute("DETACH DATABASE synthetic_archive")

    await store.submit(move)
    reader = store._fd_archive_evidence
    connect = sqlite3.connect
    calls = []

    def raced(*args, **kwargs):
        result = reader(*args, **kwargs)
        calls.append(True)
        with connect(archive) as conn:
            row = conn.execute("SELECT event_json FROM session_event_tail WHERE event_id=?", (moved_id,)).fetchone()
            item = json.loads(row[0])
            item["raw"]["tool_use_id"] = "synthetic-archive-source-changed"
            conn.execute("UPDATE session_event_tail SET event_json=? WHERE event_id=?", (json.dumps(item), moved_id))
        return result

    monkeypatch.setattr(store, "_fd_archive_evidence", raced)
    await peer_receipt(store)
    assert calls == [True]
    state = await store.fd_turn_budget_status(SID)
    assert state["continuity_gap"] == "recovery_archive_fence_unavailable"
    assert state["ordinal"] == 0
    assert recovery_state(state, status="failed")["reason"] == "recovery_archive_fence_unavailable"
    assert await counts(store) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_for_commit", [None, 5])
async def test_store_owned_commit_shares_reader_vm_and_byte_allowance(
        budget_store, monkeypatch, remaining_for_commit):
    store, _ = budget_store
    make_archive(store)
    await pending_turn(store)
    main_thread = threading.get_ident()
    store_thread = await store.submit(lambda _conn: threading.get_ident())
    reader = store._fd_archive_evidence
    effects = store._fd_effects_conn
    charge = fd_turn_budget.RecoveryBudget.charge
    watched = []
    charges = []
    effect_threads = []
    reader_used = []

    def charged(budget, amount):
        result = charge(budget, amount)
        if watched and budget is watched[0]:
            charges.append((threading.get_ident(), amount))
        return result

    def tracked(snapshot, *args, **kwargs):
        watched.append(snapshot["budget"])
        result = reader(snapshot, *args, **kwargs)
        if remaining_for_commit is not None:
            budget = snapshot["budget"]
            budget.charge(budget.limit - budget.used - remaining_for_commit)
        reader_used.append(snapshot["budget"].used)
        return result

    def committed(conn, *args, **kwargs):
        effect_threads.append(threading.get_ident())
        return effects(conn, *args, **kwargs)

    monkeypatch.setattr(fd_turn_budget.RecoveryBudget, "charge", charged)
    monkeypatch.setattr(store, "_fd_archive_evidence", tracked)
    monkeypatch.setattr(store, "_fd_effects_conn", committed)
    await peer_receipt(store)
    state = await store.fd_turn_budget_status(SID)
    claim = recovery_state(state)
    assert len(watched) == len(reader_used) == 1
    assert claim["used"] == sum(amount for _thread, amount in charges)
    assert reader_used[0] < claim["used"] <= LIMIT
    assert any(thread not in {main_thread, store_thread} and amount == 1
               for thread, amount in charges), "reader VM work was not charged"
    assert any(thread == store_thread and amount == 1
               for thread, amount in charges), "commit VM work was not charged"
    assert not any(thread == main_thread for thread, _amount in charges)
    if remaining_for_commit is None:
        assert all(amount == 1 for thread, amount in charges if thread == store_thread), \
            "commit must compare retained scalar/raw facts without decoding or hashing bodies"
        assert any(thread not in {main_thread, store_thread} and amount > 1
                   for thread, amount in charges), "reader body bytes were not charged"
        assert effect_threads == [store_thread]
        assert claim["status"] == "succeeded" and not state["continuity_gap"]
        assert await counts(store) == (1, 1)
    else:
        assert effect_threads == []
        assert claim["status"] == "failed"
        assert claim["reason"] == "recovery_allowance_exhausted"
        assert state["continuity_gap"] and state["ordinal"] == 0
        assert await counts(store) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["body", "sender", "anchor", "digest", "malformed"])
async def test_present_inexact_tell_receipt_spends_once_and_stays_unknown(budget_store, monkeypatch, mismatch):
    from comms import Comms, payload_digest
    store, _ = budget_store
    make_archive(store)
    await pending_turn(store)
    calls = spy_reader(store, monkeypatch)
    sender = "fixture:other-peer" if mismatch == "sender" else "fixture:peer"
    anchor = "wrong-anchor" if mismatch == "anchor" else "peer-1"
    body = "Wrong synthetic body" if mismatch == "body" else "Peer one"
    wire = Comms._peer_delivery_wire(
        {"from_stream_id": sender,
         "_auth_context": {"token_verified": True, "stream_id": sender}},
        "tell", anchor, body)
    envelope = {
        "payload_digest": "0" * 64 if mismatch == "digest" else payload_digest(SID, "Peer one"),
        "reply": {"tell_id": "peer-1", "to_stream_id": SID, "delivery_status": "delivered"},
        "delivery": {"tell_id": "peer-1", "to_stream_id": SID, "text": wire, "delivery_status": "delivered"},
    }
    if mismatch == "malformed":
        encoded = '{"delivery": broken synthetic JSON'
        await store.submit(lambda conn: (
            conn.execute("INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES (?,?,0)",
                         ("peer-1", encoded)), conn.commit()))
        await store._fd_retry_pending(SID)
    else:
        await store.put_tell_delivery("peer-1", envelope)
    assert len(calls) == 1
    recovery_state(calls[0]["claim"], status="claimed")
    state = await store.fd_turn_budget_status(SID)
    claim = dict(recovery_state(state, status="failed"))
    assert claim["used"] > 0 and claim["reason"]
    assert state["continuity_gap"] and state["ordinal"] == 0
    assert await counts(store) == (0, 0)
    for _ in range(3):
        await store._fd_retry_pending(SID)
    assert len(calls) == 1
    assert recovery_state(await store.fd_turn_budget_status(SID)) == claim


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", ["missing", "oversized_ascii", "oversized_utf8"])
async def test_missing_or_byte_oversized_exact_tell_never_schedules_or_spends(
        budget_store, monkeypatch, evidence):
    store, _ = budget_store
    make_archive(store)
    await pending_turn(store)
    calls = spy_reader(store, monkeypatch)
    if evidence != "missing":
        padding = "x" * 66_000 if evidence == "oversized_ascii" else "雪" * 23_000
        encoded = json.dumps({"synthetic_padding": padding}, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) > 65_536
        if evidence == "oversized_utf8":
            assert len(encoded) < 65_536
        await store.submit(lambda conn: (
            conn.execute("INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES (?,?,0)",
                         ("peer-1", encoded)), conn.commit()))
    statements = []
    await store.submit(lambda conn: conn.set_trace_callback(statements.append))
    try:
        await store._fd_retry_pending(SID)
    finally:
        await store.submit(lambda conn: conn.set_trace_callback(None))
    assert calls == []
    state = await store.fd_turn_budget_status(SID)
    assert state["continuity_gap"] == "peer_provenance_pending" and not state.get("recovery")
    assert state["ordinal"] == 0 and await counts(store) == (0, 0)
    for query in statements:
        if query.lstrip().upper().startswith("SELECT") and "v2_tell_deliveries" in query:
            projection = query[:query.upper().index(" FROM ")]
            assert re.search(r"length\s*\(\s*CAST\s*\(\s*reply\s+AS\s+BLOB", projection, re.I)
            without_size = re.sub(r"length\s*\(\s*CAST\s*\(\s*reply\s+AS\s+BLOB\s*\)\s*\)",
                                  "", projection, flags=re.I)
            assert not re.search(r"\breply\b|SELECT\s+\*", without_size, re.I)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_reader", "after_reader"])
@pytest.mark.parametrize("receipt_kind", ["tell", "send"])
async def test_receipt_change_after_claim_is_refused_without_retry(
        budget_store, monkeypatch, phase, receipt_kind):
    store, _ = budget_store
    make_archive(store)
    await pending_turn(store)
    connect = sqlite3.connect

    def mutate():
        with connect(store.path) as conn:
            if receipt_kind == "tell":
                conn.execute("UPDATE v2_tell_deliveries SET reply='{}' WHERE tell_id='peer-1'")
            else:
                conn.execute("UPDATE v2_send_receipts SET actor_trusted=0 WHERE to_stream_id=? AND request_id='peer-1'",
                             (SID,))

    def race(reader, *args, **kwargs):
        if phase == "before_reader":
            mutate()
        result = reader(*args, **kwargs)
        if phase == "after_reader":
            mutate()
        return result

    calls = spy_reader(store, monkeypatch, replacement=race)
    if receipt_kind == "tell":
        await peer_receipt(store)
    else:
        await store.append_send_receipt(
            to_stream_id=SID, request_id="peer-1", receipt_id="synthetic-send-proof",
            state="landed", wire_text=peer_wire(), display_text="Peer one",
            attachments=[], delivery="landed", submission_confirmed=True,
            from_stream_id="fixture:peer", actor_stream_id="fixture:peer", actor_trusted=True)
    assert len(calls) == 1
    recovery_state(calls[0]["claim"], status="claimed")
    state = await store.fd_turn_budget_status(SID)
    claim = dict(recovery_state(state, status="failed"))
    assert state["continuity_gap"] and state["ordinal"] == 0
    assert await counts(store) == (0, 0)
    await store._fd_retry_pending(SID)
    assert len(calls) == 1
    assert recovery_state(await store.fd_turn_budget_status(SID)) == claim


@pytest.mark.asyncio
@pytest.mark.parametrize('after_effects', [False, True])
async def test_recovery_effect_failure_rolls_back_and_preserves_spent_claim(budget_store, monkeypatch, after_effects):
    store, _ = budget_store
    await pending_turn(store)
    original = store._fd_effects_conn
    def fail(conn, **kwargs):
        if after_effects:
            original(conn, **kwargs)
        raise RuntimeError('synthetic recovery effect failure')
    monkeypatch.setattr(store, '_fd_effects_conn', fail)
    await peer_receipt(store)
    state = await store.fd_turn_budget_status(SID)
    assert state['continuity_gap'] and state['ordinal'] == 0
    recovery_state(state, status='failed')
    assert await counts(store) == (0, 0)
    assert await store.submit(lambda c:c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0]) == 7
    assert not await store.submit(lambda c:c.in_transaction)
    await peer_receipt(store)
    assert await counts(store) == (0, 0)
