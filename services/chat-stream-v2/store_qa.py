"""Generation-bound QA commissions and adjudicated reject cycles (Store worker only)."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

from store_specs import _session_row, normalize_spec_ids


class QaError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.extra = code, extra


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_qa_surfaces (
        spec_id TEXT NOT NULL, surface TEXT NOT NULL, cycle INTEGER NOT NULL CHECK(cycle > 0),
        PRIMARY KEY(spec_id,surface))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_qa_commissions (
        reviewer TEXT NOT NULL, generation TEXT NOT NULL, msg_id INTEGER NOT NULL,
        coordinator TEXT NOT NULL, coordinator_generation TEXT NOT NULL,
        spec_id TEXT NOT NULL, surface TEXT NOT NULL, cycle INTEGER NOT NULL,
        payload_hash TEXT NOT NULL, report_id TEXT UNIQUE, adjudicated_valid INTEGER NOT NULL DEFAULT 0,
        reason TEXT, actor TEXT, created_at REAL NOT NULL,
        PRIMARY KEY(reviewer,generation,msg_id))""")
    conn.execute("""CREATE INDEX IF NOT EXISTS v2_qa_active
        ON v2_qa_commissions(spec_id,surface,cycle,adjudicated_valid)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS v2_qa_adjudications (
        id INTEGER PRIMARY KEY, report_id TEXT NOT NULL, adjudicated_valid INTEGER NOT NULL,
        actor TEXT NOT NULL, actor_generation TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL)"""
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS v2_qa_diagnoses (
        diagnosis_id TEXT PRIMARY KEY, spec_id TEXT NOT NULL, surface TEXT NOT NULL,
        cycle INTEGER NOT NULL, diagnosis TEXT NOT NULL, pivot TEXT NOT NULL,
        actor TEXT NOT NULL, actor_generation TEXT NOT NULL, report_ids TEXT NOT NULL,
        created_at REAL NOT NULL, UNIQUE(spec_id,surface,cycle))""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(v2_schedules)")}
    for column, kind in (
        ("qa_spec_id", "TEXT"),
        ("qa_surface", "TEXT"),
        ("qa_cycle", "INTEGER"),
        ("qa_owner_generation", "TEXT"),
    ):
        if column not in columns:
            conn.execute(f"ALTER TABLE v2_schedules ADD COLUMN {column} {kind}")


def _session(conn, stream):
    if not isinstance(stream, str) or ":" not in stream:
        return None
    return _session_row(
        conn,
        conn.execute(
            "SELECT * FROM sessions WHERE host=? AND session_name=?",
            stream.split(":", 1),
        ).fetchone(),
    )


def _text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise QaError(
            "qa_invalid_request",
            f"{field} must be nonempty text of at most {limit} characters",
        )
    return value


def _scope(msg):
    spec = _text(msg.get("spec_id"), "spec_id", 256)
    surface = msg.get("surface")
    if (
        not isinstance(surface, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", surface) is None
    ):
        raise QaError("qa_invalid_request", "surface must be a stable lowercase slug")
    cycle = msg.get("cycle", 1)
    if type(cycle) is not int or cycle < 1:
        raise QaError("qa_invalid_request", "cycle must be a positive integer")
    return spec, surface, cycle


def _current(conn, spec, surface):
    row = conn.execute(
        "SELECT cycle FROM v2_qa_surfaces WHERE spec_id=? AND surface=?",
        (spec, surface),
    ).fetchone()
    return row[0] if row else 1


def _rejects(conn, spec, surface, cycle):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM v2_qa_commissions WHERE spec_id=? AND surface=? "
            "AND cycle=? AND adjudicated_valid=1 ORDER BY report_id",
            (spec, surface, cycle),
        )
    ]


def _check_cycle(conn, spec, surface, cycle, enforce=True):
    current = _current(conn, spec, surface)
    if cycle != current:
        raise QaError(
            "qa_cycle_conflict",
            "expected cycle is stale",
            cycle=current,
            spec_id=spec,
            surface=surface,
        )
    reports = _rejects(conn, spec, surface, cycle)
    if enforce and len(reports) >= 2:
        raise QaError(
            "qa_dispatch_reject_limit",
            "record spec-issue diagnose with a diagnosis and pivot before another QA commission",
            spec_id=spec,
            surface=surface,
            cycle=cycle,
            report_ids=[r["report_id"] for r in reports],
        )


def _authorized_lineage(conn, actor, commission):
    """Current ancestors and explicit handoff chains; never a shared tag alone."""
    origin = commission["coordinator"]
    if actor["stream_id"] == origin:
        return actor["session_generation"] == commission["coordinator_generation"]
    seen = set()
    cursor = _session(conn, origin)
    # Only the original coordinator generation supplies its ancestor edge.
    if cursor and cursor["session_generation"] == commission["coordinator_generation"]:
        while (
            cursor
            and cursor.get("parent_stream_id")
            and cursor["stream_id"] not in seen
        ):
            seen.add(cursor["stream_id"])
            parent = cursor["parent_stream_id"]
            if parent == actor["stream_id"]:
                return True
            cursor = _session(conn, parent)
    seen = set()
    cursor = actor
    while cursor and cursor["stream_id"] not in seen:
        seen.add(cursor["stream_id"])
        prior = cursor.get("handoff_from_stream_id")
        if not prior:
            return False
        if prior == origin:
            original = _session(conn, origin)
            return bool(
                original
                and original["session_generation"]
                == commission["coordinator_generation"]
            )
        cursor = _session(conn, prior)
    return False


class QaStoreMixin:
    def _qa_actor(self, conn, actor, generation, spec):
        row = _session(conn, actor)
        if (
            not row
            or row.get("status") != "open"
            or row.get("session_generation") != generation
        ):
            raise QaError(
                "qa_unauthorized", "caller lifecycle is not open and verified"
            )
        if str(row.get("role") or "").lower() not in {"lead", "nexus"}:
            raise QaError(
                "qa_unauthorized", "a lead or Nexus must commission/adjudicate QA"
            )
        resolver = self._spec_identity_resolver
        if not callable(resolver) or resolver(spec) != spec:
            raise QaError(
                "qa_spec_mismatch", "spec_id must be a resolved canonical identity"
            )
        specs = normalize_spec_ids(
            row.get("qualified_spec_ids") or row.get("spec_ids"), row.get("spec_id")
        )
        if spec not in {resolver(s) for s in specs}:
            raise QaError("qa_spec_mismatch", "caller does not carry this spec")
        return row

    async def qa_admit(
        self,
        *,
        scope,
        actor,
        actor_generation,
        reviewer,
        generation,
        msg_id,
        payload_hash,
        target_specs,
        enforce=True,
        check_only=False,
        existing_reviewer=False,
    ):
        spec, surface, cycle = _scope(scope)
        if type(msg_id) is not int or msg_id < 0:
            raise QaError("qa_invalid_request", "msg_id must be a nonnegative integer")

        def op(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                owner = self._qa_actor(conn, actor, actor_generation, spec)
                reviewer_generation = generation
                reviewer_specs = target_specs
                if existing_reviewer:
                    target = _session(conn, reviewer)
                    if not target or target.get("status") != "open":
                        raise QaError(
                            "qa_reviewer_unavailable", "QA reviewer is not open"
                        )
                    reviewer_generation = target.get("session_generation")
                    reviewer_specs = normalize_spec_ids(
                        target.get("spec_ids"), target.get("spec_id")
                    )
                canonical_targets = {
                    self._spec_identity_resolver(value) for value in reviewer_specs
                }
                if spec not in canonical_targets or reviewer == actor:
                    raise QaError(
                        "qa_spec_mismatch",
                        "independent reviewer must carry the commissioned spec",
                    )
                if not check_only:
                    prior = conn.execute(
                        "SELECT * FROM v2_qa_commissions WHERE reviewer=? AND generation=? AND msg_id=?",
                        (reviewer, reviewer_generation, msg_id),
                    ).fetchone()
                    if prior:
                        expected = (
                            actor,
                            actor_generation,
                            spec,
                            surface,
                            cycle,
                            payload_hash,
                        )
                        actual = tuple(
                            prior[k]
                            for k in (
                                "coordinator",
                                "coordinator_generation",
                                "spec_id",
                                "surface",
                                "cycle",
                                "payload_hash",
                            )
                        )
                        if expected != actual:
                            raise QaError(
                                "qa_commission_conflict",
                                "review commission cannot be rebound",
                            )
                        conn.commit()
                        return dict(prior)
                _check_cycle(conn, spec, surface, cycle, enforce)
                if not check_only:
                    _text(reviewer_generation, "reviewer generation", 256)
                    conn.execute(
                        "INSERT OR IGNORE INTO v2_qa_surfaces VALUES (?,?,1)",
                        (spec, surface),
                    )
                    conn.execute(
                        "INSERT INTO v2_qa_commissions "
                        "(reviewer,generation,msg_id,coordinator,coordinator_generation,spec_id,surface,cycle,payload_hash,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            reviewer,
                            reviewer_generation,
                            msg_id,
                            actor,
                            owner["session_generation"],
                            spec,
                            surface,
                            cycle,
                            payload_hash,
                            time.time(),
                        ),
                    )
                conn.commit()
                return {
                    "spec_id": spec,
                    "surface": surface,
                    "cycle": cycle,
                    "coordinator_generation": owner["session_generation"],
                    "generation": reviewer_generation,
                }
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(op)

    async def qa_issue(self, verb, msg, *, actor="", actor_generation=""):
        spec, surface, cycle = _scope(msg)

        def op(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                if verb == "show":
                    current = _current(conn, spec, surface)
                    rejects = _rejects(conn, spec, surface, current)
                    result = {
                        "spec_id": spec,
                        "surface": surface,
                        "cycle": current,
                        "report_ids": [r["report_id"] for r in rejects],
                        "commissions": [
                            dict(r)
                            for r in conn.execute(
                                "SELECT * FROM v2_qa_commissions WHERE spec_id=? AND surface=? ORDER BY created_at",
                                (spec, surface),
                            )
                        ],
                        "diagnoses": [
                            dict(r)
                            for r in conn.execute(
                                "SELECT * FROM v2_qa_diagnoses WHERE spec_id=? AND surface=? ORDER BY cycle",
                                (spec, surface),
                            )
                        ],
                    }
                else:
                    caller = self._qa_actor(conn, actor, actor_generation, spec)
                    if verb == "adjudicate":
                        report_id = _text(msg.get("report_id"), "report_id", 256)
                        valid = msg.get("adjudicated_valid")
                        if type(valid) is not bool:
                            raise QaError(
                                "qa_invalid_request",
                                "adjudicated_valid must be boolean",
                            )
                        reason = _text(msg.get("reason"), "reason")
                        report = conn.execute(
                            "SELECT * FROM v2_reports WHERE report_id=?", (report_id,)
                        ).fetchone()
                        if (
                            not report
                            or report["status"] != "done"
                            or report["qa_verdict"] != "reject"
                        ):
                            raise QaError(
                                "qa_report_invalid",
                                "a durable done reject report is required",
                            )
                        evidence = json.loads(report["extras"] or "{}").get(
                            "qa_review", {}
                        )
                        if (
                            not isinstance(evidence, dict)
                            or not isinstance(evidence.get("reviewed_scope"), str)
                            or not evidence["reviewed_scope"].strip()
                            or evidence.get("candidate_identity")
                            != report["target_sha"]
                            or re.fullmatch(
                                r"[a-f0-9]{64}",
                                str(evidence.get("gate_evidence_digest") or ""),
                            )
                            is None
                            or re.fullmatch(
                                r"[a-fA-F0-9]{40}", str(report["target_sha"] or "")
                            )
                            is None
                        ):
                            raise QaError(
                                "qa_report_invalid",
                                "report must bind candidate, reviewed scope and gate evidence digest",
                            )
                        commission = conn.execute(
                            "SELECT * FROM v2_qa_commissions WHERE reviewer=? AND generation=? AND msg_id=?",
                            (
                                report["from_stream_id"],
                                report["session_generation"],
                                report["msg_id"],
                            ),
                        ).fetchone()
                        if not commission or tuple(
                            commission[k] for k in ("spec_id", "surface", "cycle")
                        ) != (spec, surface, cycle):
                            raise QaError(
                                "qa_report_binding_mismatch",
                                "report generation/commission does not match scope",
                            )
                        if report["from_stream_id"] == actor or not _authorized_lineage(
                            conn, caller, commission
                        ):
                            raise QaError(
                                "qa_unauthorized",
                                "caller does not own this QA commission",
                            )
                        if commission["report_id"] not in (None, report_id):
                            raise QaError(
                                "qa_commission_report_conflict",
                                "commission already selected its representative report",
                                report_id=commission["report_id"],
                            )
                        changed = (
                            commission["report_id"] is None
                            or bool(commission["adjudicated_valid"]) != valid
                        )
                        if changed:
                            conn.execute(
                                "UPDATE v2_qa_commissions SET report_id=?,adjudicated_valid=?,reason=?,actor=? WHERE reviewer=? AND generation=? AND msg_id=?",
                                (
                                    report_id,
                                    int(valid),
                                    reason,
                                    actor,
                                    commission["reviewer"],
                                    commission["generation"],
                                    commission["msg_id"],
                                ),
                            )
                            conn.execute(
                                "INSERT INTO v2_qa_adjudications(report_id,adjudicated_valid,actor,actor_generation,reason,created_at) VALUES(?,?,?,?,?,?)",
                                (
                                    report_id,
                                    int(valid),
                                    actor,
                                    actor_generation,
                                    reason,
                                    time.time(),
                                ),
                            )
                        result = {
                            "report_id": report_id,
                            "adjudicated_valid": valid,
                            "spec_id": spec,
                            "surface": surface,
                            "cycle": cycle,
                            "changed": changed,
                        }
                    elif verb == "diagnose":
                        ident = _text(msg.get("diagnosis_id"), "diagnosis_id", 128)
                        diagnosis = _text(msg.get("diagnosis"), "diagnosis")
                        pivot = _text(msg.get("pivot"), "pivot")
                        prior = conn.execute(
                            "SELECT * FROM v2_qa_diagnoses WHERE diagnosis_id=?",
                            (ident,),
                        ).fetchone()
                        if prior:
                            if tuple(
                                prior[k]
                                for k in (
                                    "spec_id",
                                    "surface",
                                    "cycle",
                                    "diagnosis",
                                    "pivot",
                                    "actor",
                                    "actor_generation",
                                )
                            ) != (
                                spec,
                                surface,
                                cycle,
                                diagnosis,
                                pivot,
                                actor,
                                actor_generation,
                            ):
                                raise QaError(
                                    "qa_diagnosis_conflict",
                                    "diagnosis id has different content",
                                )
                            result = dict(prior)
                        else:
                            _check_cycle(conn, spec, surface, cycle, False)
                            rejects = _rejects(conn, spec, surface, cycle)
                            if len(rejects) < 2:
                                raise QaError(
                                    "qa_diagnosis_not_required",
                                    "two valid rejects are required",
                                )
                            if not all(
                                _authorized_lineage(conn, caller, r) for r in rejects
                            ):
                                raise QaError(
                                    "qa_unauthorized",
                                    "caller does not own the counted commissions",
                                )
                            ids = json.dumps([r["report_id"] for r in rejects])
                            conn.execute(
                                "INSERT INTO v2_qa_diagnoses VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (
                                    ident,
                                    spec,
                                    surface,
                                    cycle,
                                    diagnosis,
                                    pivot,
                                    actor,
                                    actor_generation,
                                    ids,
                                    time.time(),
                                ),
                            )
                            conn.execute(
                                "UPDATE v2_qa_surfaces SET cycle=cycle+1 WHERE spec_id=? AND surface=?",
                                (spec, surface),
                            )
                            result = {
                                "diagnosis_id": ident,
                                "spec_id": spec,
                                "surface": surface,
                                "cycle": cycle,
                                "report_ids": ids,
                            }
                        result["next_cycle"] = cycle + 1
                    else:
                        raise QaError("qa_invalid_request", "unknown spec-issue verb")
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

        return await self.submit(op)
