"""Offline, audited promotion of exact assistant composite final-delivery evidence.

The daemon must be stopped for every operation on its database. Dry-run opens
the file read-only, performs no DDL, and is safe on a pre-stop immutable copy.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store_routing import (ASSISTANT_COMPOSITE_RECOVERY_RUNS_DDL,
                           ASSISTANT_COMPOSITE_RECOVERY_AUDIT_DDL)

TOOL_VERSION = "1"
SCHEMA_VERSION = 1
RUNS = "v2_assistant_composite_recovery_runs"
AUDIT = "v2_assistant_composite_recovery_audit"
FIELDS = ("routing_state", "delivery_state", "error_code", "updated_at", "route_json")
RUN_ID = re.compile(r"[A-Za-z0-9_.-]{1,120}\Z")
PRECONDITIONS_JSON = json.dumps({"quick_check": "ok", "user_version": SCHEMA_VERSION,
                                 "composite_session": "open", "lsof": "clear"}, sort_keys=True)
EXACT_FINAL = ("p.stream_id=r.stream_id AND p.dispatch_id=r.dispatch_id "
               "AND p.reply_to_message_id=r.input_identity AND ((p.publish_kind='prose' AND "
               "json_extract(p.canonical_payload_json,'$.response_state')='final') OR p.publish_kind='result')")
PROMOTE_SQL = """UPDATE v2_assistant_composite_routes SET delivery_state='landed',error_code=NULL,
    route_json=json_set(route_json,'$.landed_by_publication',?,'$.landed_evidence','own_final',
    '$.landed_at',?,'$.prior_delivery_state',?,'$.prior_error_code',?,'$.recovered_by_run',?),updated_at=?
    WHERE route_id=? AND routing_state='resolved' AND delivery_state=? AND error_code IS ?
    AND updated_at=? AND route_json=?"""
RESTORE_SQL = """UPDATE v2_assistant_composite_routes SET routing_state=?,delivery_state=?,error_code=?,
    updated_at=?,route_json=? WHERE route_id=? AND routing_state=? AND delivery_state=?
    AND error_code IS ? AND updated_at=? AND route_json=?"""


class Refused(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def ids_digest(ids):
    return digest(json.dumps(ids).encode())


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def connect(path, mode):
    if not path.is_file():
        raise Refused("db_not_regular_file")
    suffix = "immutable=1" if mode == "immutable" else "mode=" + mode
    conn = sqlite3.connect("file:" + quote(str(path.resolve())) + "?" + suffix,
                           uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def preconditions(path, stream, *, immutable=False):
    if not path.is_file():
        raise Refused("db_not_regular_file")
    if not immutable:
        files = [str(path), str(path) + "-wal", str(path) + "-shm"]
        probe = subprocess.run(["lsof", "-t", *files], capture_output=True, text=True)
        if probe.returncode == 0 and probe.stdout.strip():
            raise Refused("db_held_open")
        if probe.returncode not in (0, 1):
            raise Refused("lsof_failed")
    conn = connect(path, "immutable" if immutable else "ro")
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise Refused("quick_check_failed")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise Refused("user_version_mismatch")
        required = {"sessions", "session_event_tail", "v2_assistant_composite_routes",
                    "v2_assistant_composite_publications"}
        if not required <= table_names(conn):
            raise Refused("base_tables_missing")
        host, sep, name = stream.partition(":")
        if not sep or not host or not name:
            raise Refused("stream_invalid")
        session = conn.execute("SELECT status,provider FROM sessions WHERE host=? AND session_name=?",
                               (host, name)).fetchone()
        if session is None or tuple(session) != ("open", "composite"):
            raise Refused("composite_session_not_open")
    finally:
        conn.close()


def preview(conn, stream, *, db, open_mode):
    candidates = []
    query = ("SELECT r.route_id,r.input_identity,r.dispatch_id,r.routing_state,r.delivery_state,"
             "r.error_code,r.updated_at,r.route_json,p.publication_key,p.created_at AS evidence_created_at "
             "FROM v2_assistant_composite_routes r JOIN v2_assistant_composite_publications p ON " + EXACT_FINAL +
             " JOIN session_event_tail e ON e.event_id=p.event_id WHERE r.stream_id=? "
             "AND r.routing_state='resolved' AND json_extract(r.route_json,'$.admission_mode')='direct_primary' "
             "AND r.delivery_state IN ('intent','committed_pending','uncertain') "
             "AND json_extract(e.event_json,'$.raw.actor_stream_id')=r.route_target ORDER BY r.route_id,p.created_at")
    seen = set()
    for raw in conn.execute(query, (stream,)):
        row = dict(raw)
        if row["route_id"] in seen:
            continue
        seen.add(row["route_id"])
        row["route_json_sha256"] = digest((row.pop("route_json") or "").encode())
        candidates.append(row)
    ids = sorted(seen)
    skipped = []
    for raw in conn.execute(
        "SELECT r.route_id,r.delivery_state,EXISTS(SELECT 1 FROM v2_assistant_composite_publications p "
        "WHERE " + EXACT_FINAL + ") AS has_exact_final FROM v2_assistant_composite_routes r "
        "WHERE r.stream_id=? AND r.delivery_state IN ('intent','committed_pending','uncertain') ORDER BY r.route_id",
        (stream,),
    ):
        if raw["route_id"] not in seen:
            skipped.append({"route_id": raw["route_id"], "delivery_state": raw["delivery_state"],
                            "reason": "actor_mismatch" if raw["has_exact_final"] else "no_exact_final"})
    tables = table_names(conn)
    runs = "no audit tables (no runs yet)"
    if {RUNS, AUDIT} <= tables:
        runs = [dict(row) for row in conn.execute(
            f"SELECT run_id,mode,state,verify_outcome FROM {RUNS} WHERE state='prepared' "
            "OR (state='applied' AND verify_outcome IS NULL) ORDER BY started_at")]
    return {"tool_version": TOOL_VERSION, "db": str(db.resolve()), "open_mode": open_mode,
            "stream_id": stream, "generated_at": now(), "candidates": candidates, "skipped": skipped,
            "counts": {"candidates": len(candidates), "skipped": len(skipped)},
            "candidate_ids": ids, "candidate_ids_sha256": ids_digest(ids),
            "expected_startup_retained_uncertain": sum(row["delivery_state"] in ("intent", "committed_pending")
                                                        for row in skipped),
            "runs_needing_attention": runs}


def open_writable(path, stream, *, initialize=False):
    preconditions(path, stream)
    conn = connect(path, "rw")
    if initialize:
        conn.execute(ASSISTANT_COMPOSITE_RECOVERY_RUNS_DDL)
        conn.execute(ASSISTANT_COMPOSITE_RECOVERY_AUDIT_DDL)
    return conn


def require_no_prepared(conn):
    if conn.execute(f"SELECT 1 FROM {RUNS} WHERE state='prepared' LIMIT 1").fetchone():
        raise Refused("prepared_run_present_run_reconcile_first")


def insert_run(conn, *, run_id, mode, stream, path, ids, backup_path=None, backup_sha=None, target=None):
    stamp = now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"INSERT INTO {RUNS}(run_id,mode,target_run_id,stream_id,tool_version,db_path,"
                     "backup_path,backup_sha256,preconditions_json,candidate_ids_json,candidate_count,state,started_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,'prepared',?)",
                     (run_id, mode, target, stream, TOOL_VERSION, str(path.resolve()),
                      str(backup_path) if backup_path else None, backup_sha, PRECONDITIONS_JSON,
                      json.dumps(ids), len(ids), stamp))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def abort_run(conn, run_id, reason):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(f"UPDATE {RUNS} SET state='aborted',abort_reason=?,finished_at=? "
                     "WHERE run_id=? AND state='prepared'",
                     (reason, now(), run_id))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def audit_insert(conn, run_id, source, prior, post, action, stamp):
    conn.execute(f"INSERT INTO {AUDIT}(run_id,route_id,input_identity,dispatch_id,action,"
                 "prior_routing_state,prior_delivery_state,prior_error_code,prior_updated_at,prior_route_json,"
                 "new_routing_state,new_delivery_state,new_error_code,new_updated_at,new_route_json,"
                 "evidence_publication_key,evidence_created_at,created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, source["route_id"], source["input_identity"], source["dispatch_id"], action,
                  *prior, *post, source["evidence_publication_key"], source["evidence_created_at"], stamp))


def run_apply(path, stream, run_id, expect_file, backup_dir, *, crash_after=None):
    conn = open_writable(path, stream, initialize=True)
    try:
        require_no_prepared(conn)
        if conn.execute(f"SELECT 1 FROM {RUNS} WHERE run_id=?", (run_id,)).fetchone():
            raise Refused("run_id_exists")
        expected = json.loads(expect_file.read_text())
        current = preview(conn, stream, db=path, open_mode="rw")
        if (expected.get("stream_id") != stream or expected.get("candidate_ids") != current["candidate_ids"] or
                expected.get("candidate_ids_sha256") != current["candidate_ids_sha256"] or
                expected.get("candidate_ids_sha256") != ids_digest(expected.get("candidate_ids", [])) or
                [row.get("route_id") for row in expected.get("candidates", [])] != current["candidate_ids"] or
                [(row.get("publication_key"), row.get("evidence_created_at"))
                 for row in expected.get("candidates", [])] !=
                [(row["publication_key"], row["evidence_created_at"]) for row in current["candidates"]]):
            raise Refused("expect_ids_mismatch")
        if not current["candidate_ids"]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(f"INSERT INTO {RUNS}(run_id,mode,stream_id,tool_version,db_path,preconditions_json,"
                             "candidate_ids_json,candidate_count,state,started_at,finished_at) "
                             "VALUES (?,'apply',?,?,?,?, '[]',0,'noop',?,?)",
                             (run_id, stream, TOOL_VERSION, str(path.resolve()), PRECONDITIONS_JSON, now(), now()))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return {"state": "noop", "run_id": run_id}
        if not backup_dir.is_dir():
            raise Refused("backup_dir_missing")
        backup_path = backup_dir / f"sessions-pre-{run_id}.db"
        if backup_path.exists():
            raise Refused("backup_exists")
        try:
            fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise Refused("backup_exists") from exc
        os.close(fd)
        backup = sqlite3.connect(backup_path)
        try:
            conn.backup(backup)
        finally:
            backup.close()
        os.chmod(backup_path, 0o600)
        backup_sha = file_digest(backup_path)
        backup_conn = connect(backup_path, "immutable")
        try:
            copied = preview(backup_conn, stream, db=backup_path, open_mode="immutable")
        finally:
            backup_conn.close()
        if copied["candidate_ids"] != current["candidate_ids"]:
            raise Refused("backup_candidate_mismatch")
        insert_run(conn, run_id=run_id, mode="apply", stream=stream, path=path,
                   ids=current["candidate_ids"], backup_path=backup_path, backup_sha=backup_sha)
        if crash_after == "after_T1":
            raise RuntimeError("test_crash_after_T1")
        conn.execute("BEGIN IMMEDIATE")
        try:
            run = conn.execute(f"SELECT state,candidate_ids_json FROM {RUNS} WHERE run_id=?",
                               (run_id,)).fetchone()
            if run is None or run["state"] != "prepared":
                raise Refused("run_not_prepared")
            transactional = preview(conn, stream, db=path, open_mode="rw")
            if (transactional["candidate_ids"] != expected["candidate_ids"] or
                    transactional["candidate_ids_sha256"] != expected["candidate_ids_sha256"] or
                    json.loads(run["candidate_ids_json"]) != expected["candidate_ids"]):
                raise Refused("expect_ids_mismatch")
            for candidate in expected["candidates"]:
                raw = conn.execute("SELECT routing_state,delivery_state,error_code,updated_at,route_json "
                                   "FROM v2_assistant_composite_routes WHERE route_id=?",
                                   (candidate["route_id"],)).fetchone()
                if raw is None or (tuple(raw[:4]) + (digest(raw[4].encode()),)) != (
                    candidate["routing_state"], candidate["delivery_state"], candidate["error_code"],
                    candidate["updated_at"], candidate["route_json_sha256"]):
                    raise Refused("preimage_mismatch:" + candidate["route_id"])
                prior = tuple(raw)
                stamp = now()
                changed = conn.execute(PROMOTE_SQL, (
                    candidate["publication_key"], stamp, candidate["delivery_state"], candidate["error_code"],
                    run_id, stamp, candidate["route_id"], candidate["delivery_state"], candidate["error_code"],
                    candidate["updated_at"], raw[4],
                )).rowcount
                if changed != 1:
                    raise Refused("cas_miss:" + candidate["route_id"])
                post = tuple(conn.execute("SELECT routing_state,delivery_state,error_code,updated_at,route_json "
                                          "FROM v2_assistant_composite_routes WHERE route_id=?",
                                          (candidate["route_id"],)).fetchone())
                audit_insert(conn, run_id, {**candidate, "evidence_publication_key": candidate["publication_key"]},
                             prior, post, "promote", stamp)
            conn.execute(f"UPDATE {RUNS} SET state='applied',applied_count=?,finished_at=? WHERE run_id=?",
                         (len(expected["candidates"]), now(), run_id))
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            abort_run(conn, run_id, str(exc))
            raise
        if crash_after == "after_T2_commit":
            raise RuntimeError("test_crash_after_T2_commit")
        return {"state": "applied", "run_id": run_id, "applied_count": len(expected["candidates"]),
                "backup_sha256": backup_sha}
    finally:
        conn.close()


def audit_rows(conn, run_id):
    return [dict(row) for row in conn.execute(f"SELECT * FROM {AUDIT} WHERE run_id=? AND action='promote' ORDER BY route_id",
                                               (run_id,))]


def require_run_stream(stream, run):
    # open_writable has checked this stream's stopped-file preconditions.
    # Equality makes those checks apply to the run's actual target too.
    if run["stream_id"] != stream:
        raise Refused("run_stream_mismatch")


def run_verify(path, stream, run_id):
    conn = open_writable(path, stream)
    try:
        run = conn.execute(f"SELECT * FROM {RUNS} WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise Refused("unknown_run")
        require_run_stream(stream, run)
        target_id = run_id if run["mode"] == "apply" else run["target_run_id"]
        target = conn.execute(f"SELECT * FROM {RUNS} WHERE run_id=?", (target_id,)).fetchone()
        if target is None:
            raise Refused("target_run_missing")
        require_run_stream(stream, target)
        rows = audit_rows(conn, target_id)
        if len(rows) != target["candidate_count"]:
            raise Refused("audit_count_mismatch")
        ids = set(json.loads(target["candidate_ids_json"]))
        current_ids = set(preview(conn, stream, db=path, open_mode="rw")["candidate_ids"])
        applied = run["mode"] == "apply" and run["state"] == "applied"
        restored = (run["mode"] == "apply" and run["state"] == "rolled_back") or (
            run["mode"] == "rollback" and run["state"] == "applied")
        if not applied and not restored:
            raise Refused("verify_not_applicable")
        ok = True
        for audit in rows:
            current = conn.execute("SELECT routing_state,delivery_state,error_code,updated_at,route_json "
                                   "FROM v2_assistant_composite_routes WHERE route_id=?", (audit["route_id"],)).fetchone()
            prefix = "new_" if applied else "prior_"
            expected = tuple(audit[prefix + name] for name in FIELDS)
            ok &= current is not None and tuple(current) == expected
        ok &= not bool(ids & current_ids) if applied else ids <= current_ids
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"UPDATE {RUNS} SET verify_outcome=?,verified_at=? WHERE run_id=?",
                         ("ok" if ok else "mismatch", now(), run_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {"run_id": run_id, "verify_outcome": "ok" if ok else "mismatch"}
    finally:
        conn.close()


def run_rollback(path, stream, run_id, new_run_id):
    conn = open_writable(path, stream)
    try:
        require_no_prepared(conn)
        target = conn.execute(f"SELECT * FROM {RUNS} WHERE run_id=?", (run_id,)).fetchone()
        if target is None or target["mode"] != "apply" or target["state"] != "applied":
            raise Refused("target_run_not_applied")
        require_run_stream(stream, target)
        rows = audit_rows(conn, run_id)
        if len(rows) != target["candidate_count"]:
            raise Refused("audit_count_mismatch")
        insert_run(conn, run_id=new_run_id, mode="rollback", stream=stream, path=path,
                   ids=[row["route_id"] for row in rows], target=run_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                prior = tuple(row["prior_" + name] for name in FIELDS)
                post = tuple(row["new_" + name] for name in FIELDS)
                changed = conn.execute(RESTORE_SQL, (*prior, row["route_id"], *post)).rowcount
                if changed != 1:
                    raise Refused("postimage_mismatch:" + row["route_id"])
                audit_insert(conn, new_run_id, row, post, prior, "rollback", now())
            conn.execute(f"UPDATE {RUNS} SET state='rolled_back',finished_at=? WHERE run_id=?", (now(), run_id))
            conn.execute(f"UPDATE {RUNS} SET state='applied',applied_count=?,finished_at=? WHERE run_id=?",
                         (len(rows), now(), new_run_id))
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            abort_run(conn, new_run_id, str(exc))
            raise
        return {"state": "applied", "run_id": new_run_id, "next": f"--verify --run-id {new_run_id}"}
    finally:
        conn.close()


def run_reconcile(path, stream, run_id):
    conn = open_writable(path, stream)
    try:
        run = conn.execute(f"SELECT state,stream_id FROM {RUNS} WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["state"] != "prepared":
            raise Refused("run_not_prepared")
        require_run_stream(stream, run)
        if conn.execute(f"SELECT 1 FROM {AUDIT} WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
            raise Refused("prepared_run_has_audit_rows")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"UPDATE {RUNS} SET state='abandoned',abort_reason='reconciled',finished_at=? WHERE run_id=?",
                         (now(), run_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {"run_id": run_id, "state": "abandoned"}
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--stream", required=True)
    operations = parser.add_mutually_exclusive_group(required=True)
    for name in ("dry-run", "apply", "verify", "rollback", "reconcile"):
        operations.add_argument("--" + name, action="store_true")
    parser.add_argument("--immutable", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--new-run-id")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--expect-ids", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.immutable and not args.dry_run:
            raise Refused("immutable_only_with_dry_run")
        if args.dry_run:
            preconditions(args.db, args.stream, immutable=args.immutable)
            conn = connect(args.db, "immutable" if args.immutable else "ro")
            try:
                result = preview(conn, args.stream, db=args.db,
                                 open_mode="immutable" if args.immutable else "ro")
            finally:
                conn.close()
            if args.out:
                args.out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        else:
            if not args.run_id or not RUN_ID.fullmatch(args.run_id):
                raise Refused("run_id_invalid")
            if args.apply:
                if args.expect_ids is None or args.backup_dir is None:
                    raise Refused("expect_ids_and_backup_dir_required")
                result = run_apply(args.db, args.stream, args.run_id, args.expect_ids, args.backup_dir)
            elif args.verify:
                result = run_verify(args.db, args.stream, args.run_id)
            elif args.rollback:
                if not args.new_run_id or not RUN_ID.fullmatch(args.new_run_id):
                    raise Refused("new_run_id_invalid")
                result = run_rollback(args.db, args.stream, args.run_id, args.new_run_id)
            else:
                result = run_reconcile(args.db, args.stream, args.run_id)
        print(json.dumps(result, sort_keys=True))
        return 1 if result.get("verify_outcome") == "mismatch" else 0
    except (Refused, sqlite3.Error, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
