"""Generated-only archive workload; the default is a small correctness run.

PENTACLE_FD_SCALE_BYTES (33554432), PENTACLE_FD_SCALE_ATTEMPTS (40),
PENTACLE_FD_SCALE_RATE (0, unthrottled), and PENTACLE_FD_SCALE_SEED (20261011)
parameterize the workload. Attempts must be a positive multiple of ten.
PENTACLE_FD_SCALE_REPORT optionally saves the complete JSON measurement report.
Large archives and all timing/CPU/latency acceptance belong to fleet intake.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
import contextvars
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
from urllib.parse import unquote, urlparse

import pytest

import store as store_module
from fd_turn_budget import FdTurnBudgetConfig
from ingest import _identity_key
from store import SESSION_EVENT_TAIL_DDL, Store
from test_fd_turn_budget import (archive_sources, budget_store, normalized,
                                peer_receipt, peer_wire)


STREAM = "fixture:fd"
LIFECYCLE = "2026-10-10T00:00:00Z"
PHASES = ("healthy_new_generation", "archived_identity", "duplicate_mix_90_percent",
          "permanent_gap", "pending_receipt", "restart_and_replay_same_k")


def _encoded(event):
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def generate_archive(path: Path, *, target_bytes: int, seed: int) -> dict:
    """Build invented history using the production archive's exact table DDL.

    Retention copies the table DDL, not the hot store's additional indices: its
    INTEGER PRIMARY KEY and UNIQUE(stream,lifecycle,event_key) are deliberate.
    Generation checks file pages per bounded batch, so overshoot is one batch.
    """
    if target_bytes < 65536:
        raise ValueError("archive target must be at least 65536 bytes")
    rng = random.Random(seed)
    sizes = (256, 1024, 8192, 65536)
    weights = (55, 25, 15, 5)
    body_counts, stream_counts = Counter(), Counter()
    body_min, body_max, body_sum = None, 0, 0
    rows = 0
    # These are invented prose fragments, never derived from a real transcript.
    text = "Synthetic orchard fixture: count amber pears and describe blue crates. "
    with sqlite3.connect(path) as conn:
        conn.execute(SESSION_EVENT_TAIL_DDL)
        while conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0] < target_bytes:
            batch = []
            for _ in range(128):
                rows += 1
                sid = STREAM if rows % 4 != 0 else f"synthetic:other-{(rows // 4) % 257:03d}"
                size = rng.choices(sizes, weights=weights, k=1)[0]
                body = (text * (size // len(text) + 1))[:size]
                event = {"kind": "USER" if rows % 7 == 0 else "ASSIST_TEXT",
                         "stream_id": sid, "timestamp": LIFECYCLE, "text": body,
                         "raw": {"message_id": f"generated-{seed}-{rows}",
                                 "synthetic_sequence": rows}}
                raw = _encoded(event)
                identity = f"synthetic-identity-{seed}-{rows}"
                batch.append((rows, sid, LIFECYCLE, hashlib.sha256(raw.encode()).hexdigest(),
                              raw, LIFECYCLE, 1791590400.0 + rows, identity))
                body_counts[str(size)] += 1
                stream_counts[sid] += 1
                nbytes = len(raw.encode())
                body_min = nbytes if body_min is None else min(body_min, nbytes)
                body_max = max(body_max, nbytes)
                body_sum += nbytes
            conn.executemany("INSERT INTO session_event_tail "
                             "(event_id,stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity) "
                             "VALUES (?,?,?,?,?,?,?,?)", batch)
            conn.commit()
        indexes = [dict(name=row[1], unique=bool(row[2]), origin=row[3],
                        columns=[col[2] for col in conn.execute(f'PRAGMA index_info("{row[1]}")')])
                   for row in conn.execute("PRAGMA index_list(session_event_tail)")]
    assert rows == sum(body_counts.values()) == sum(stream_counts.values())
    assert len(stream_counts) > 32 and stream_counts[STREAM] > rows // 2
    return {"generated_only": True, "seed": seed, "target_bytes": target_bytes,
            "actual_file_bytes": path.stat().st_size, "rows": rows,
            "same_stream_lifecycle_rows": stream_counts[STREAM],
            "stream_cohort_rows": dict(sorted(stream_counts.items())),
            "body_payload_size_counts": dict(sorted(body_counts.items())),
            "encoded_body_bytes": {"min": body_min, "max": body_max, "total": body_sum},
            "distinct_identities": rows, "indexes": indexes,
            "identity_examples": {"present": f"synthetic-identity-{seed}-1",
                                  "absent": f"synthetic-identity-{seed}-absent"}}


class ArchiveProbe:
    """Instrument actual archive opens/SQL and off-thread reader hand-offs."""
    def __init__(self, monkeypatch, archive):
        self.archive = archive.resolve()
        self.lane = contextvars.ContextVar("fd_scale_lane", default=("setup", "setup"))
        self.opens = Counter()
        self.queries = Counter()
        self.handoffs = Counter()
        self.recovery_queries = {}
        self.reader_threads = set()
        self._connect = store_module.sqlite3.connect
        self._to_thread = store_module.asyncio.to_thread
        probe = self

        class TracedConnection(sqlite3.Connection):
            def execute(conn, sql, parameters=(), /):
                phase, lane = probe.lane.get()
                if getattr(conn, "_scale_archive", False):
                    probe.queries[(phase, lane)] += 1
                if lane == "recovery" and sql.lstrip().upper().startswith("SELECT"):
                    key = (conn._scale_database, sql)
                    probe.recovery_queries.setdefault(key, tuple(parameters))
                return super().execute(sql, parameters)

        def connect(database, *args, **kwargs):
            raw = os.fspath(database)
            resolved = Path(unquote(urlparse(raw).path) if raw.startswith("file:") else raw).resolve()
            if resolved == probe.archive:
                probe.opens[probe.lane.get()] += 1
            kwargs.setdefault("factory", TracedConnection)
            conn = probe._connect(database, *args, **kwargs)
            if isinstance(conn, TracedConnection):
                conn._scale_archive = resolved == probe.archive
                conn._scale_database = str(resolved)
            return conn

        async def to_thread(func, /, *args, **kwargs):
            name = getattr(func, "__name__", "")
            if name == "_fd_archive_evidence":
                probe.handoffs[probe.lane.get()] += 1
                def run():
                    probe.reader_threads.add(threading.get_ident())
                    return func(*args, **kwargs)
                return await probe._to_thread(run)
            return await probe._to_thread(func, *args, **kwargs)

        monkeypatch.setattr(store_module.sqlite3, "connect", connect)
        monkeypatch.setattr(store_module.asyncio, "to_thread", to_thread)

    @contextmanager
    def during(self, phase, lane):
        token = self.lane.set((phase, lane))
        try:
            yield
        finally:
            self.lane.reset(token)

    def phase(self, name):
        return {"ingest_archive_opens": self.opens[(name, "ingest")],
                "ingest_archive_queries": self.queries[(name, "ingest")],
                "ingest_reader_handoffs": self.handoffs[(name, "ingest")],
                "recovery_archive_opens": self.opens[(name, "recovery")],
                "recovery_archive_queries": self.queries[(name, "recovery")],
                "recovery_reader_handoffs": self.handoffs[(name, "recovery")]}

    def query_plans(self):
        """EXPLAIN captured recovery SQL afterward, outside the charged reader."""
        plans = []
        for (database, sql), parameters in sorted(self.recovery_queries.items()):
            with self._connect(Path(database).as_uri() + "?mode=ro", uri=True) as conn:
                plan = [list(row) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
            plans.append({"database": "archive" if Path(database) == self.archive else "hot",
                          "sql": sql, "plan": plan})
        return plans


class ScaleWorkload:
    def __init__(self, store, probe, *, attempts, rate):
        self.store = store
        self.probe = probe
        self.attempts = attempts
        self.rate = rate
        self.sequence = 0
        self.phase = "setup"
        self.current = None
        self.phases = []
        self.lifecycle = None

    def event(self, kind, *, body="Invented scale dispatch"):
        self.sequence += 1
        item = normalized(kind, self.sequence, body=body)
        item["timestamp"] = (datetime(2026, 10, 10, tzinfo=timezone.utc) +
                             timedelta(seconds=self.sequence)).isoformat().replace("+00:00", "Z")
        return item

    async def generation(self, suffix):
        row = await self.store.open_session("fixture", "fd", provider="claude",
            pane_status="pane_alive", pane_pid="123", created_at=LIFECYCLE,
            session_generation="synthetic-scale-" + suffix)
        binding = {"stream_id": STREAM, "generation": row["session_generation"]}
        self.store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=False), env_binding=binding)
        self.lifecycle = {"pane_pid": "123", "session_created_at": row["created_at"],
                          "generation": row["session_generation"]}
        return binding

    def begin(self, name):
        self.phase = name
        self.current = {"phase": name, "delivery_attempts": 0, "single_calls": 0,
                        "batch_calls": 0, "zero_insert_batches": 0,
                        "inserted_sources": 0, "receipt_attempts": 0}
        self.started = time.monotonic()

    async def deliver(self, items, *, batch=False):
        with self.probe.during(self.phase, "ingest"):
            if batch:
                result = await self.store.append_session_events_lifecycle_cas([
                    {"stream_id": STREAM, "event": item, "identity": _identity_key(item),
                     "lifecycle": self.lifecycle} for item in items], limit=100)
                assert result is not None and len(result) == len(items)
                inserted = sum(type(item) is int for item in result)
                self.current["batch_calls"] += 1
                self.current["zero_insert_batches"] += int(inserted == 0)
            else:
                assert len(items) == 1
                # Instrument the named Store admission floor. Generic receipt
                # projection is a separate caller outside this observer contract.
                result = await self.store.append_session_event(
                    STREAM, items[0], identity=_identity_key(items[0]), limit=100)
                inserted = int(result is not None and result is not False)
                self.current["single_calls"] += 1
        self.current["delivery_attempts"] += len(items)
        self.current["inserted_sources"] += inserted
        if self.rate:
            delay = self.current["delivery_attempts"] / self.rate - (time.monotonic() - self.started)
            if delay > 0:
                await asyncio.sleep(delay)

    async def receipt(self):
        with self.probe.during(self.phase, "recovery"):
            await peer_receipt(self.store)
        self.current["receipt_attempts"] += 1

    async def finish(self):
        self.current.update(self.probe.phase(self.phase))
        self.current["elapsed_seconds_measured_small_run"] = time.monotonic() - self.started
        state = await self.store.fd_turn_budget_status(STREAM)
        self.current["state"] = state
        self.current["key"] = [STREAM, self.lifecycle["generation"], self.lifecycle["session_created_at"]]
        self.phases.append(self.current)
        return state


@pytest.mark.asyncio
async def test_generated_archive_six_phase_workload(budget_store, tmp_path, monkeypatch, record_property):
    store, _ = budget_store
    target = int(os.environ.get("PENTACLE_FD_SCALE_BYTES", 32 * 1024 * 1024))
    attempts = int(os.environ.get("PENTACLE_FD_SCALE_ATTEMPTS", "40"))
    rate = float(os.environ.get("PENTACLE_FD_SCALE_RATE", "0"))
    seed = int(os.environ.get("PENTACLE_FD_SCALE_SEED", "20261011"))
    assert attempts >= 10 and attempts % 10 == 0
    assert math.isfinite(rate) and rate >= 0
    archive = Path(store.path).with_name("sessions_archive.db")
    facts = generate_archive(archive, target_bytes=target, seed=seed)
    # The invented archived rows predate this fresh generation. Preserve the
    # shared event-id floor just as a real hot/archive pair does after retention.
    await store.submit(lambda conn: (conn.execute("INSERT INTO sqlite_sequence(name,seq) "
        "SELECT 'session_event_tail',? WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='session_event_tail')",
        (facts["rows"],)), conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='session_event_tail'",
        (facts["rows"],)), conn.commit()))
    probe = ArchiveProbe(monkeypatch, archive)
    driver = ScaleWorkload(store, probe, attempts=attempts, rate=rate)
    reopened = None
    try:
        await driver.generation("healthy")
        driver.begin(PHASES[0])
        healthy = []
        for n in range(attempts):
            item = driver.event("USER" if n % 8 == 0 else "TOOL_USE")
            healthy.append(item)
            await driver.deliver([item], batch=bool(n % 2))
        await driver.finish()

        await archive_sources(store)
        facts["identity_examples"]["replayed"] = _identity_key(healthy[0])
        driver.begin(PHASES[1])
        for n in range(attempts):
            await driver.deliver([healthy[0]], batch=bool(n % 2))
        await driver.finish()

        await driver.generation("duplicates")
        driver.begin(PHASES[2])
        for _ in range(attempts // 10):
            item = driver.event("USER")
            await driver.deliver([item, item], batch=True)  # intra-batch duplicate
            await driver.deliver([item])
            for _ in range(3):
                await driver.deliver([item, item], batch=True)  # zero-insert batches
            await driver.deliver([item])
        state = await driver.finish()

        state["continuity_gap"] = "cursor_evidence_unavailable"
        await store.put(store._fd_cursor_key(STREAM), _encoded(state))
        driver.begin(PHASES[3])
        for n in range(attempts):
            await driver.deliver([driver.event("TOOL_USE")], batch=bool(n % 2))
        await driver.finish()

        binding = await driver.generation("pending")
        driver.begin(PHASES[4])
        pending = [driver.event("USER", body=peer_wire())]
        pending.extend(driver.event("TOOL_USE") for _ in range(6))
        for item in pending:
            await driver.deliver([item])
        for n in range(attempts - len(pending)):
            await driver.deliver([pending[-1]], batch=bool(n % 2))
        before_receipt = await store.fd_turn_budget_status(STREAM)
        driver.current["before_exact_receipt_recovery"] = (before_receipt or {}).get("recovery")
        # Move one exact proof to the generated archive, leaving the cursor hot.
        def archive_anchor(conn):
            conn.execute("ATTACH DATABASE ? AS scale_archive", (str(archive),))
            conn.execute("INSERT INTO scale_archive.session_event_tail SELECT * FROM session_event_tail WHERE identity=?",
                         (_identity_key(pending[0]),))
            conn.execute("DELETE FROM session_event_tail WHERE identity=?", (_identity_key(pending[0]),))
            conn.commit()
            conn.execute("DETACH DATABASE scale_archive")
        await store.submit(archive_anchor)
        await driver.receipt()
        before_restart = await driver.finish()

        store.stop()
        assert store._thread is None
        reopened = Store(store.path)
        reopened.start()
        reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=False), env_binding=binding)
        driver.store = reopened
        driver.begin(PHASES[5])
        for n in range(attempts):
            await driver.deliver([pending[-1]], batch=bool(n % 2))
        await driver.receipt()  # a duplicate receipt cannot renew the allowance
        after_restart = await driver.finish()

        facts["recovery_query_plans"] = probe.query_plans()
        facts["reader_thread_count"] = len(probe.reader_threads)
        facts["parameters"] = {"attempts_per_phase": attempts, "rate_per_second": rate}
        facts["phases"] = driver.phases
        facts["scope"] = "small generated correctness run; no fleet performance acceptance"
        report = json.dumps(facts, sort_keys=True, indent=2)
        report_path = Path(os.environ.get("PENTACLE_FD_SCALE_REPORT", str(tmp_path / "scale-facts.json")))
        report_path.write_text(report + "\n")
        record_property("fd_scale_facts", report)
        print("FD_TURN_BUDGET_SCALE_FACTS=" + json.dumps(facts, sort_keys=True))

        # Evaluate all phase measurements together, after writing RED evidence.
        assert [phase["phase"] for phase in driver.phases] == list(PHASES)
        assert all(phase["delivery_attempts"] == attempts for phase in driver.phases)
        assert driver.phases[2]["zero_insert_batches"] > 0
        assert driver.phases[2]["inserted_sources"] == attempts // 10
        violations = [{"phase": phase["phase"], key: phase[key]}
            for phase in driver.phases
            for key in ("ingest_archive_opens", "ingest_archive_queries", "ingest_reader_handoffs")
            if phase[key] != 0]
        assert not violations, violations
        assert not driver.phases[4]["before_exact_receipt_recovery"], "ineligible anchor spent recovery"
        recovery = before_restart.get("recovery")
        assert recovery and recovery["spent"] == 100000
        assert 0 <= recovery["used"] <= 100000
        assert recovery["status"] in {"succeeded", "failed"}
        if recovery["status"] == "failed":
            assert before_restart["continuity_gap"]
            assert after_restart["continuity_gap"]
        assert after_restart.get("recovery") == recovery
        assert driver.phases[4]["key"] == driver.phases[5]["key"]
        assert sum(phase["recovery_reader_handoffs"] for phase in driver.phases) == 1
        for query in facts["recovery_query_plans"]:
            assert "json_" not in query["sql"].lower()
            if query["database"] == "archive":
                assert all("INTEGER PRIMARY KEY" in row[3] for row in query["plan"])
    finally:
        if reopened is not None:
            reopened.stop()
            assert reopened._thread is None
