"""Generation-bound QA commissions and adjudicated reject cycles (Store worker only)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import sqlite3
import time
import threading
from typing import Any

from store_specs import _session_row, normalize_spec_ids


class QaError(ValueError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.extra = code, extra


SPEC_SOURCE_FIELDS = (
    "created_at", "session_generation", "status", "role", "parent_stream_id",
    "handoff_from_stream_id", "spec_id", "spec_ids", "qualified_spec_ids",
    "spec_binding_provenance", "requested_model", "requested_effort",
    "effective_model", "effective_effort", "provider",
)


def spec_binding_source(row):
    """Exact consumed lifecycle/binding inputs, excluding mutable telemetry."""
    return None if row is None else {key: row.get(key) for key in SPEC_SOURCE_FIELDS}


class SpecInputsUnavailable(ValueError):
    pass


class _SpecCommitConnection:
    """Operation-local abandonment guard, including nested context commits."""
    def __init__(self, conn, check):
        self._conn, self._check = conn, check

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, *args, **kwargs):
        self._check()
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._check()
        return self._conn.executemany(*args, **kwargs)

    def commit(self):
        self._check()
        return self._conn.commit()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            try:
                self._check()
            except BaseException:
                self._conn.rollback()
                raise
        return self._conn.__exit__(kind, value, traceback)


class _SpecLookup:
    def __init__(self, values):
        self.values = dict(values)

    def __call__(self, value):
        if value not in self.values:
            raise SpecInputsUnavailable("spec_identity_unavailable")
        return self.values[value]



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
    async def _resolve_spec_inputs(self, ids, *, warn_single=False):
        """Bound submissions, including single-only callers, before to_thread.

        An abandoned worker retains its slot until completion. Missing batch
        entries and exceptions are unavailable, never synchronous fallbacks.
        """
        ids = list(dict.fromkeys(ids))
        if not ids:
            return _SpecLookup({})
        single = self._spec_identity_resolver
        batch = getattr(self, "_spec_identities_resolver", None)
        if not callable(single) and not callable(batch) and warn_single:
            return _SpecLookup({value: None for value in ids})
        if not callable(single) and not callable(batch):
            raise SpecInputsUnavailable("spec_identity_unavailable")
        active = getattr(self, "_spec_workers", 0)
        if active >= 8:
            raise SpecInputsUnavailable("spec_resolution_overloaded")
        self._spec_workers = active + 1
        def singles(values):
            result = {}
            for value in values:
                try:
                    result[value] = single(value)
                except Exception:
                    if not warn_single:
                        raise
                    result[value] = None
            return result
        async def run():
            try:
                service = getattr(batch, "__self__", None)
                if service is not None and hasattr(service, "spec_resolution_snapshot"):
                    from _shared.specs_service import spec_resolution_view
                    view = await spec_resolution_view(service)
                    values = {value: view.canonical_spec_identity(value) for value in ids}
                else:
                    values = await asyncio.to_thread(
                        batch if callable(batch) else singles, ids)
                if not isinstance(values, dict) or any(value not in values for value in ids):
                    raise SpecInputsUnavailable("spec_identity_unavailable")
                return _SpecLookup(values)
            except Exception:
                raise SpecInputsUnavailable("spec_identity_unavailable") from None
            finally:
                self._spec_workers -= 1
        task = asyncio.create_task(run())
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return await asyncio.shield(task)

    async def _submit_spec_commit(self, callback):
        """Do not execute an abandoned queued binding/report/QA write."""
        caller = asyncio.current_task()
        abandoned = threading.Event()
        def check():
            if abandoned.is_set() or (caller is not None and caller.cancelling()):
                raise asyncio.CancelledError()
        def checked(conn):
            check()
            try:
                return callback(_SpecCommitConnection(conn, check))
            except BaseException:
                conn.rollback()
                raise
        try:
            return await self.submit(checked)
        except asyncio.CancelledError:
            abandoned.set()
            raise

    def _qa_source(self, conn, spec, surface, actor, reviewer=None, report_id=None):
        commissions = [dict(row) for row in conn.execute(
            "SELECT * FROM v2_qa_commissions WHERE spec_id=? AND surface=? ORDER BY reviewer,generation,msg_id",
            (spec, surface))]
        pending = [actor, reviewer, *(row["coordinator"] for row in commissions)]
        sources = {}
        while pending:
            stream = pending.pop()
            if not stream or stream in sources:
                continue
            row = _session(conn, stream)
            sources[stream] = spec_binding_source(row)
            if row:
                pending.extend((row.get("parent_stream_id"), row.get("handoff_from_stream_id")))
        report = conn.execute("SELECT * FROM v2_reports WHERE report_id=?", (report_id,)).fetchone() if report_id else None
        return (sources, commissions, _current(conn, spec, surface), dict(report) if report else None,
                [dict(row) for row in conn.execute(
                    "SELECT * FROM v2_qa_diagnoses WHERE spec_id=? AND surface=? ORDER BY cycle", (spec, surface))])

    async def _qa_spec_lookup(self, spec, actor, reviewer=None, target_specs=(), *, surface="", report_id=None):
        def capture(conn):
            return self._qa_source(conn, spec, surface, actor, reviewer, report_id)
        source = await self.submit(capture)
        caller, target = source[0].get(actor) or {}, source[0].get(reviewer) or {}
        ids = [spec, *normalize_spec_ids(
            caller.get("qualified_spec_ids") or caller.get("spec_ids"), caller.get("spec_id"))]
        ids += normalize_spec_ids(target.get("spec_ids"), target.get("spec_id")) if reviewer else list(target_specs or ())
        lookup = await self._resolve_spec_inputs(ids)
        def check(conn):
            if capture(conn) != source:
                raise SpecInputsUnavailable("spec_inputs_changed")
        lookup.check = check
        return lookup

    def _qa_actor(self, conn, actor, generation, spec, resolver=None):
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
        prepare_only=False,
    ):
        scope, target_specs = copy.deepcopy(scope), copy.deepcopy(target_specs)
        spec, surface, cycle = _scope(scope)
        if type(msg_id) is not int or msg_id < 0:
            raise QaError("qa_invalid_request", "msg_id must be a nonnegative integer")
        if existing_reviewer:
            # A caller may have read this seat before admission began. Bind the
            # current open generation once, then retain it across every await
            # and retry; a later reopen must never inherit this commission.
            target = await self.submit(lambda conn: _session(conn, reviewer))
            if not target or target.get("status") != "open":
                raise QaError("qa_reviewer_unavailable", "QA reviewer is not open")
            generation = target.get("session_generation")
        def op(conn, *, transaction=True, validate_only=False):
            if transaction:
                conn.execute("BEGIN IMMEDIATE")
            try:
                resolve.check(conn)
                owner = self._qa_actor(conn, actor, actor_generation, spec, resolve)
                reviewer_generation = generation
                reviewer_specs = target_specs
                if existing_reviewer:
                    target = _session(conn, reviewer)
                    if not target or target.get("status") != "open":
                        raise QaError(
                            "qa_reviewer_unavailable", "QA reviewer is not open"
                        )
                    if target.get("session_generation") != generation:
                        raise QaError("qa_reviewer_unavailable", "QA reviewer generation changed")
                    reviewer_generation = target.get("session_generation")
                    reviewer_specs = normalize_spec_ids(
                        target.get("spec_ids"), target.get("spec_id")
                    )
                canonical_targets = {
                    resolve(value) for value in reviewer_specs
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
                        if transaction:
                            conn.commit()
                        return dict(prior)
                _check_cycle(conn, spec, surface, cycle, enforce)
                if not check_only and not validate_only:
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
                if transaction:
                    conn.commit()
                return {
                    "spec_id": spec,
                    "surface": surface,
                    "cycle": cycle,
                    "coordinator_generation": owner["session_generation"],
                    "generation": reviewer_generation,
                }
            except BaseException:
                if transaction:
                    conn.rollback()
                raise

        for attempt in range(2):
            try:
                resolve = await self._qa_spec_lookup(
                    spec, actor, reviewer if existing_reviewer else None, target_specs, surface=surface)
                if prepare_only:
                    await self._submit_spec_commit(lambda conn: op(conn, validate_only=True))
                    # One operation-local callback: the intent writer owns the
                    # transaction and repeats every current authority check.
                    return lambda conn: op(conn, transaction=False)
                return await self._submit_spec_commit(op)
            except SpecInputsUnavailable as exc:
                if attempt:
                    raise QaError("qa_spec_unavailable", str(exc)) from None

    async def qa_issue(self, verb, msg, *, actor="", actor_generation=""):
        msg = copy.deepcopy(msg)
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
                    resolve.check(conn)
                    caller = self._qa_actor(conn, actor, actor_generation, spec, resolve)
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

        if verb == "show":
            return await self.submit(op)
        for attempt in range(2):
            try:
                resolve = await self._qa_spec_lookup(spec, actor, surface=surface, report_id=msg.get("report_id"))
                return await self._submit_spec_commit(op)
            except SpecInputsUnavailable as exc:
                if attempt:
                    raise QaError("qa_spec_unavailable", str(exc)) from None
