"""Durable file observations on the existing Store's single SQLite writer."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

SERVICES_ROOT = str(Path(__file__).resolve().parents[1])
if SERVICES_ROOT not in sys.path:
    sys.path.insert(0, SERVICES_ROOT)

from _shared.work_observations import advance_observation, timestamp, unresolved_member

DDL = (
    "CREATE TABLE IF NOT EXISTS v2_work_item_observations (spec_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS v2_work_index_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), state_json TEXT NOT NULL)",
)
INDEX_FIELDS = ("available", "root_configured", "snapshot_at", "last_sweep_at", "error")
EMPTY_STATE = {"available": False, "root_configured": False, "snapshot_at": None,
               "last_sweep_at": None, "error": "work_index_unavailable"}


def ensure_work_index_schema(conn) -> None:
    for statement in DDL:
        conn.execute(statement)


def member_ids(lane: dict) -> list[str]:
    return json.loads(lane.get("members_json") or "[]")


def members_conn(conn, lane: dict) -> list[dict]:
    members = []
    state = _state_conn(conn)
    for spec_id in member_ids(lane):
        row = conn.execute("SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (spec_id,)).fetchone()
        members.append(_observed_record(json.loads(row[0]), state)["member"] if row else unresolved_member(spec_id))
    return members


def _state_conn(conn) -> dict:
    row = conn.execute("SELECT state_json FROM v2_work_index_state WHERE singleton=1").fetchone()
    return json.loads(row[0]) if row else dict(EMPTY_STATE)


def _candidate(scan: dict, spec_id: str, prior: dict | None) -> dict:
    if not scan["available"]:
        return {"quality": "stale", "error": scan.get("error") or "work_root_unavailable"}
    candidates = scan["candidates"].get(spec_id, [])
    if len(candidates) > 1:
        return {"quality": "ambiguous", "error": "work_declared_id_ambiguous"}
    if candidates:
        return candidates[0]
    if prior and prior.get("path") in scan["errors"]:
        return scan["errors"][prior["path"]]
    return {"quality": "missing", "error": "work_file_missing"}


def _observed_record(record: dict, state: dict) -> dict:
    """Fresh members share the last committed scan clock, including after boot."""
    observation = record["member"]["observation"]
    if observation["quality"] == "fresh" and state.get("snapshot_at") is not None:
        observation["observed_at"] = state["snapshot_at"]
    return record


def _prepare_records(rows, state: dict, scan: dict, *, settle_s: float, sweep: bool):
    """Compute a delta without retaining a second decoded population or a cache."""
    known_ids = set()
    delta = {}
    accepted = False
    now = float(scan["scanned_at"])

    def advance(spec_id, previous):
        nonlocal accepted
        candidate = _candidate(scan, spec_id, previous)
        record, transition = advance_observation(spec_id, previous, candidate,
            now=now, settle_s=settle_s, sweep=sweep)
        accepted |= candidate["quality"] == "fresh"
        if previous is not None:
            prior_quality = previous["member"]["observation"]["quality"]
            quality = record["member"]["observation"]["quality"]
            if prior_quality == quality == "fresh":
                # The committed snapshot advances this clock for all fresh rows.
                previous["member"]["observation"]["observed_at"] = record["member"]["observation"]["observed_at"]
            elif (quality in ("missing", "ambiguous") and prior_quality == quality
                  and previous.get("incident") == record.get("incident")
                  and previous["bad_sweeps"] >= 2
                  and now - previous["bad_since"] >= settle_s):
                # Once settled, counting more sweeps cannot change the decision.
                record["bad_sweeps"] = previous["bad_sweeps"]
        if record != previous:
            delta[spec_id] = (json.dumps(record, separators=(",", ":")),
                              transition, record["member"]["obs_rev"])

    for spec_id, raw in rows:
        known_ids.add(spec_id)
        advance(spec_id, _observed_record(json.loads(raw), state))
    for spec_id in scan["candidates"].keys() - known_ids:
        advance(spec_id, None)
    known_ids.update(scan["candidates"])
    return state, known_ids, delta, accepted


def _prepare_file(path: str, scan: dict, *, settle_s: float, sweep: bool):
    """Read one coherent SQLite snapshot, then release it before CPU preparation."""
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        state = _state_conn(conn)
        rows = conn.execute("SELECT spec_id,record_json FROM v2_work_item_observations").fetchall()
    return _prepare_records(rows, state, scan, settle_s=settle_s, sweep=sweep)


class WorkIndexStoreMixin:
    _work_index_fault = None

    async def work_unattended(self, *, now: float | None = None) -> dict:
        from work_unattended import unattended_work_conn
        return await self.submit(lambda conn: unattended_work_conn(conn, now=now))

    async def work_index_status(self) -> dict[str, Any]:
        def read(conn):
            state = _state_conn(conn)
            return {key: state.get(key) for key in INDEX_FIELDS}
        return await self.submit(read)

    async def mark_work_index_pending(self) -> None:
        """Boot availability only: keep last-good facts, history and clocks intact."""
        def mark(conn):
            state = _state_conn(conn)
            state.update(available=False, error="work_index_unavailable")
            conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "
                         "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",
                         (json.dumps(state, separators=(",", ":")),))
            conn.commit()
        await self.submit(mark)

    async def reconcile_work_observations(self, scan: dict, *, settle_s: float, sweep: bool) -> dict:
        """A serialized snapshot + history commit; membership is read in this transaction."""
        from store_work_lanes import _insert_event
        now = float(scan["scanned_at"])
        fault = self._work_index_fault
        worker = self._thread
        prepared = None
        if self.path not in ("", ":memory:"):
            # Also waits for schema initialization before opening the reader.
            await self.submit(lambda conn: None)
            prepared = await asyncio.to_thread(_prepare_file, self.path, scan,
                                                settle_s=settle_s, sweep=sweep)
        if worker is not self._thread:
            raise RuntimeError("work index Store changed during preparation")

        def apply(conn):
            if worker is not self._thread:
                raise RuntimeError("work index Store changed during preparation")
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = _state_conn(conn)
                if now <= state.get("_last_scan_at", float("-inf")):
                    conn.rollback()
                    return {"changed": 0, "stale_scan": True}
                lanes = [dict(row) for row in conn.execute(
                    "SELECT * FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL")]
                holders: dict[str, list[dict]] = {}
                for lane in lanes:
                    for spec_id in member_ids(lane):
                        holders.setdefault(spec_id, []).append(lane)
                current = prepared
                if current is None or current[0] != state:
                    # Memory Stores and a concurrent accepted scan use the same
                    # oracle here. Never publish a delta against a changed base.
                    current = _prepare_records(conn.execute(
                        "SELECT spec_id,record_json FROM v2_work_item_observations"),
                        state, scan, settle_s=settle_s, sweep=sweep)
                _, known_ids, delta, accepted = current
                # Membership may have changed while the read-only snapshot was
                # prepared. Resolve new unknown members against this transaction.
                for spec_id in holders.keys() - known_ids:
                    record, transition = advance_observation(spec_id, None, _candidate(scan, spec_id, None),
                        now=now, settle_s=settle_s, sweep=sweep)
                    delta[spec_id] = (json.dumps(record, separators=(",", ":")),
                                      transition, record["member"]["obs_rev"])
                changes = 0
                for spec_id in sorted(delta):
                    raw, transition, revision = delta[spec_id]
                    conn.execute("INSERT INTO v2_work_item_observations(spec_id,record_json) VALUES (?,?) "
                                 "ON CONFLICT(spec_id) DO UPDATE SET record_json=excluded.record_json",
                                 (spec_id, raw))
                    if fault is not None:
                        fault()
                    if transition:
                        changes += 1
                        for lane in holders.get(spec_id, []):
                            event_id = f"item:{lane['lane_id']}:{spec_id}:{revision}"
                            _insert_event(conn, event_id=event_id, lane=lane, operation="item_change",
                                          source_id=event_id, actor="daemon:index", generation=None,
                                          prior=lane["work_state"], nxt=lane["work_state"], expected=None,
                                          payload=transition, stamp=timestamp(now))
                state.update(available=scan["available"], root_configured=scan["root_configured"],
                             error=scan.get("error"), _last_scan_at=now)
                if accepted or (scan["available"] and state.get("snapshot_at") is None):
                    state["snapshot_at"] = timestamp(now)
                if sweep:
                    state["last_sweep_at"] = timestamp(now)
                conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "
                             "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",
                             (json.dumps(state, separators=(",", ":")),))
                conn.commit()
                return {"changed": changes, "stale_scan": False}
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(apply)
