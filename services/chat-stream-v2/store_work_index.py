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
from _shared.work_goals import advance_goals, detail_revision, goal_id

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


def effective_membership_ids_conn(conn) -> dict:
    """Canonical membership without decoding observation records on the writer.

    SQLite extracts only the retained source epic identity. Full member facts
    are decoded separately by readers that actually render/aggregate them.
    """
    state = _state_conn(conn)
    lanes = [dict(row) for row in conn.execute(
        "SELECT lane_id,members_json FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL")]
    known_lanes = {lane["lane_id"] for lane in lanes}
    scoped, wire_ids, assigned = {}, {}, {}
    for identity, record in state.get("goal_observations", {}).items():
        facts = record.get("facts") or {}
        if facts.get("lane_id") not in known_lanes:
            continue
        wire_id = goal_id(identity)
        if wire_id in wire_ids:
            raise ValueError("work_goal_id_collision")
        wire_ids[wire_id] = identity
        scoped[identity] = record
    if scoped:
        for identity, epic in conn.execute(
                "SELECT spec_id,json_extract(record_json,'$.last_good._epic') "
                "FROM v2_work_item_observations "
                "WHERE json_type(record_json,'$.last_good._epic')='text'"):
            goal = scoped.get(epic)
            if goal is not None:
                assigned[identity] = goal["facts"]["lane_id"]
    membership = {}
    for lane in lanes:
        identity = lane["lane_id"]
        legacy_order = [spec for spec in member_ids(lane) if spec not in assigned or assigned[spec] == identity]
        derived = sorted(spec for spec, target in assigned.items() if target == identity)
        membership[identity] = list(dict.fromkeys([*legacy_order, *derived]))
    return {"state": state, "goals": scoped, "assigned": assigned, "membership": membership}


def effective_membership_conn(conn) -> dict:
    """Enrich the canonical identity resolver for member/goal projection; no I/O."""
    snapshot = effective_membership_ids_conn(conn)
    state, scoped = snapshot["state"], snapshot["goals"]
    records = {row[0]: _observed_record(json.loads(row[1]), state) for row in conn.execute(
        "SELECT spec_id,record_json FROM v2_work_item_observations")}
    members = {}
    for identity, record in records.items():
        member = dict(record["member"])
        last_good = record.get("last_good") or {}
        epic = last_good.get("_epic")
        # Diagnostics keep the current missing/ambiguous status. Goal counts
        # retain the known source contribution through settle and restart.
        if last_good.get("status") is not None:
            member["_goal_status"] = last_good["status"]
        goal = scoped.get(epic)
        if goal is not None:
            member["goal_id"] = goal_id(epic)
            if goal["quality"] != "fresh" and member["observation"]["quality"] == "fresh":
                member["observation"] = {**member["observation"], "quality": "stale",
                                         "error": goal.get("error") or "work_goal_source_uncertain"}
        if record.get("progress_changed_at"):
            member["_progress_changed_at"] = record["progress_changed_at"]
        member["_progress_at"] = record.get("progress_at")
        members[identity] = member
    return {**snapshot, "records": records, "members": members}


def effective_member_ids(conn, lane: dict, snapshot: dict | None = None) -> list[str]:
    if not lane.get("lane_id"):
        return member_ids(lane)  # Explicit report-context selection, not a lane.
    snapshot = snapshot if snapshot is not None else effective_membership_ids_conn(conn)
    return snapshot["membership"].get(lane["lane_id"], [])


def members_conn(conn, lane: dict, snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else effective_membership_conn(conn)
    return [snapshot["members"].get(identity) or unresolved_member(identity)
            for identity in effective_member_ids(conn, lane, snapshot)]


def enrich_goals_conn(conn, lane: dict, snapshot: dict | None = None) -> None:
    snapshot = snapshot if snapshot is not None else effective_membership_conn(conn)
    lane_id = lane["lane_id"]
    goals = {identity: record for identity, record in snapshot["goals"].items()
             if record["facts"]["lane_id"] == lane_id}
    ids = effective_member_ids(conn, lane, snapshot)
    members = []
    for identity in ids:
        record = snapshot["records"].get(identity) or {}
        member = snapshot["members"].get(identity) or unresolved_member(identity)
        observation = member.get("observation") or {}
        members.append((identity, (record.get("last_good") or {}).get("_epic"), member.get("goal_id"),
                        record.get("source_hash"), observation.get("quality"), observation.get("error"),
                        member.get("checkpoint_revision"), member.get("checkpoint_policy_revision")))
    goal_inputs = [(identity, record.get("source_hash"), record["quality"], record.get("error"))
                   for identity, record in goals.items()]
    lane["_goals"] = goals
    lane["_detail_revision"] = detail_revision(members, goal_inputs, snapshot["state"])



def _state_conn(conn) -> dict:
    row = conn.execute("SELECT state_json FROM v2_work_index_state WHERE singleton=1").fetchone()
    return json.loads(row[0]) if row else dict(EMPTY_STATE)



def index_status_conn(conn) -> dict:
    state = _state_conn(conn)
    result = {key: state.get(key) for key in INDEX_FIELDS}
    if result.get("error") is None:
        if any(record.get("quality") != "fresh" for record in state.get("goal_observations", {}).values()):
            result["error"] = "work_goal_source_uncertain"
        elif state.get("goal_source_errors"):
            result["error"] = "work_goal_source_invalid"
    return result


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


def progress_backfill_conn(conn) -> dict | None:
    """One-time progress times for records written before they were kept.

    Only stored stage changes and checked-count rises count; null means unknown.
    """
    if not conn.execute("SELECT 1 FROM v2_work_item_observations "
                        "WHERE json_type(record_json,'$.progress_at') IS NULL LIMIT 1").fetchone():
        return None
    rows = conn.execute(
        "SELECT json_extract(payload_json,'$.spec_id'),MAX(created_at) FROM v2_work_lane_events "
        "WHERE operation='item_change' AND (json_extract(payload_json,'$.prior.status') "
        "IS NOT json_extract(payload_json,'$.next.status') OR json_extract(payload_json,'$.next.ac_checked') "
        "> json_extract(payload_json,'$.prior.ac_checked')) GROUP BY 1")
    return {spec_id: stamp[:19] + "Z" for spec_id, stamp in rows if spec_id and stamp}


def _prepare_records(rows, state: dict, scan: dict, *, settle_s: float, sweep: bool, known_lanes: set[str],
                     backfill: dict | None = None):
    """Compute a delta without retaining a second decoded population or a cache."""
    known_ids = set()
    delta = {}
    accepted = False
    now = float(scan["scanned_at"])
    goals = advance_goals(state.get("goal_observations", {}), scan, known_lanes)

    def advance(spec_id, stored):
        nonlocal accepted
        previous = stored
        if stored is not None and "progress_at" not in stored:
            previous = {**stored, "progress_at": (backfill or {}).get(spec_id)}
        candidate = _candidate(scan, spec_id, previous)
        if candidate["quality"] == "fresh":
            facts = candidate["facts"]
            epic = facts.get("_epic")
            goal = goals.get(epic)
            # Legacy unscoped charters keep their explicit-lane compatibility.
            # Invalid newly assigned/scoped references retain last-good facts.
            if ((epic and (goal is None or not goal.get("facts")))
                    or facts.get("_epic_error") and (goal is None or (goal.get("facts") or {}).get("lane_id") is not None)):
                candidate = {"quality": "stale", "error": "work_goal_reference_unresolved"}
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
        if record != stored:
            delta[spec_id] = (json.dumps(record, separators=(",", ":")),
                              transition, record["member"]["obs_rev"])

    for spec_id, raw in rows:
        known_ids.add(spec_id)
        advance(spec_id, _observed_record(json.loads(raw), state))
    for spec_id in scan["candidates"].keys() - known_ids:
        advance(spec_id, None)
    known_ids.update(scan["candidates"])
    return state, known_ids, delta, accepted, goals, known_lanes


def _prepare_file(path: str, scan: dict, *, settle_s: float, sweep: bool):
    """Read one coherent SQLite snapshot, then release it before CPU preparation."""
    with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        state = _state_conn(conn)
        rows = conn.execute("SELECT spec_id,record_json FROM v2_work_item_observations").fetchall()
        known_lanes = {row[0] for row in conn.execute(
            "SELECT lane_id FROM v2_assistant_composite_lanes WHERE work_state IS NOT NULL")}
        backfill = progress_backfill_conn(conn)
    return _prepare_records(rows, state, scan, settle_s=settle_s, sweep=sweep, known_lanes=known_lanes,
                            backfill=backfill)


class WorkIndexStoreMixin:
    _work_index_fault = None

    async def work_owed(self, *, now=None, root_binding=None):
        from work_owed import owed_dispositions_conn
        return await self.submit(lambda conn: owed_dispositions_conn(conn, now=now, root_binding=root_binding))

    async def lead_close_context(self, stream, generation, *, pending_only=True, observation_available=True):
        from work_owed import lead_close_context_conn
        return await self.submit(lambda conn: lead_close_context_conn(conn, stream, generation, pending_only=pending_only,
                                                                     observation_available=observation_available))

    async def front_desk_disposition_custody(self, stream, generation):
        from work_owed import front_desk_custody_conn
        def write(conn):
            front_desk_custody_conn(conn, stream, generation)
            conn.commit()
        return await self.submit(write)

    async def work_unattended(self, *, now: float | None = None) -> dict:
        from work_unattended import unattended_work_conn
        return await self.submit(lambda conn: unattended_work_conn(conn, now=now))

    async def work_index_status(self) -> dict[str, Any]:
        return await self.submit(index_status_conn)

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
                previous_membership = effective_membership_ids_conn(conn)["membership"]
                holders: dict[str, list[dict]] = {}
                for lane in lanes:
                    for spec_id in member_ids(lane):
                        holders.setdefault(spec_id, []).append(lane)
                current = prepared
                # Assignment metadata shares this JSON row but does not change
                # the observation snapshot used by off-loop preparation.
                from work_owed import STATE_KEY, reconcile_dispositions_conn
                def observation_state(value):
                    return {key: field for key, field in value.items() if key != STATE_KEY}
                known_lanes = {lane["lane_id"] for lane in lanes}
                if (current is None or observation_state(current[0]) != observation_state(state)
                        or current[5] != known_lanes):
                    # Memory Stores and a concurrent accepted scan use the same
                    # oracle here. Never publish a delta against a changed base.
                    current = _prepare_records(conn.execute(
                        "SELECT spec_id,record_json FROM v2_work_item_observations").fetchall(),
                        state, scan, settle_s=settle_s, sweep=sweep, known_lanes=known_lanes,
                        backfill=progress_backfill_conn(conn))
                _, known_ids, delta, accepted, goals, _ = current
                # Membership may have changed while the read-only snapshot was
                # prepared. Resolve new unknown members against this transaction.
                for spec_id in holders.keys() - known_ids:
                    record, transition = advance_observation(spec_id, None, _candidate(scan, spec_id, None),
                        now=now, settle_s=settle_s, sweep=sweep)
                    delta[spec_id] = (json.dumps(record, separators=(",", ":")),
                                      transition, record["member"]["obs_rev"])
                changes = 0
                transitions = []
                for spec_id in sorted(delta):
                    raw, transition, revision = delta[spec_id]
                    conn.execute("INSERT INTO v2_work_item_observations(spec_id,record_json) VALUES (?,?) "
                                 "ON CONFLICT(spec_id) DO UPDATE SET record_json=excluded.record_json",
                                 (spec_id, raw))
                    if fault is not None:
                        fault()
                    if transition:
                        transitions.append((spec_id, transition, revision))
                state.update(available=scan["available"], root_configured=scan["root_configured"],
                             error=scan.get("error"), _last_scan_at=now,
                             goal_observations=goals, goal_source_errors=scan.get("goal_errors", {}))
                if accepted or (scan["available"] and state.get("snapshot_at") is None):
                    state["snapshot_at"] = timestamp(now)
                if sweep:
                    state["last_sweep_at"] = timestamp(now)
                conn.execute("INSERT INTO v2_work_index_state(singleton,state_json) VALUES (1,?) "
                             "ON CONFLICT(singleton) DO UPDATE SET state_json=excluded.state_json",
                             (json.dumps(state, separators=(",", ":")),))
                snapshot = effective_membership_ids_conn(conn)
                # Source-derived replacement is the same completion boundary as
                # explicit set_members, even if the replacement is all terminal.
                for lane in lanes:
                    identity = lane["lane_id"]
                    if set(previous_membership.get(identity, [])) != set(snapshot["membership"].get(identity, [])):
                        conn.execute("UPDATE v2_work_lane_episodes SET cleared_at=? "
                                     "WHERE lane_id=? AND kind='completed' AND cleared_at IS NULL",
                                     (timestamp(now), identity))
                by_lane = {lane["lane_id"]: lane for lane in lanes}
                for spec_id, transition, revision in transitions:
                    changes += 1
                    for lane_id, identities in snapshot["membership"].items():
                        if spec_id not in identities:
                            continue
                        lane = by_lane[lane_id]
                        event_id = f"item:{lane_id}:{spec_id}:{revision}"
                        _insert_event(conn, event_id=event_id, lane=lane, operation="item_change",
                                      source_id=event_id, actor="daemon:index", generation=None,
                                      prior=lane["work_state"], nxt=lane["work_state"], expected=None,
                                      payload=transition, stamp=timestamp(now))
                reconcile_dispositions_conn(conn, now)
                conn.commit()
                return {"changed": changes, "stale_scan": False}
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(apply)
