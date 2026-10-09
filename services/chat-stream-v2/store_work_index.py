"""Durable file observations on the existing Store's single SQLite writer."""
from __future__ import annotations

import json
import sys
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
    for spec_id in member_ids(lane):
        row = conn.execute("SELECT record_json FROM v2_work_item_observations WHERE spec_id=?", (spec_id,)).fetchone()
        members.append(json.loads(row[0])["member"] if row else unresolved_member(spec_id))
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


class WorkIndexStoreMixin:
    _work_index_fault = None

    async def work_index_status(self) -> dict[str, Any]:
        def read(conn):
            state = _state_conn(conn)
            return {key: state.get(key) for key in INDEX_FIELDS}
        return await self.submit(read)

    async def reconcile_work_observations(self, scan: dict, *, settle_s: float, sweep: bool) -> dict:
        """A serialized snapshot + history commit; membership is read in this transaction."""
        from store_work_lanes import _insert_event
        now = float(scan["scanned_at"])
        fault = self._work_index_fault
        def apply(conn):
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
                records = {row[0]: json.loads(row[1]) for row in conn.execute(
                    "SELECT spec_id,record_json FROM v2_work_item_observations")}
                ids = records.keys() | scan["candidates"].keys() | holders.keys()
                changes = 0
                accepted = False
                for spec_id in sorted(ids):
                    candidate = _candidate(scan, spec_id, records.get(spec_id))
                    record, transition = advance_observation(spec_id, records.get(spec_id), candidate,
                                                            now=now, settle_s=settle_s, sweep=sweep)
                    accepted |= candidate["quality"] == "fresh"
                    conn.execute("INSERT INTO v2_work_item_observations(spec_id,record_json) VALUES (?,?) "
                                 "ON CONFLICT(spec_id) DO UPDATE SET record_json=excluded.record_json",
                                 (spec_id, json.dumps(record, separators=(",", ":"))))
                    if fault is not None:
                        fault()
                    if transition:
                        changes += 1
                        for lane in holders.get(spec_id, []):
                            event_id = f"item:{lane['lane_id']}:{spec_id}:{record['member']['obs_rev']}"
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
