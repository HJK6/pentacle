"""Isolated recovery-tool contract over a disposable SQLite database."""

import asyncio
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest

from store import Store


TOOL = Path(__file__).resolve().parents[1] / "tools" / "recover_assistant_composite_delivery.py"
STREAM = "fixture-chat:assistant"


def fixture_db(tmp_path):
    path = tmp_path / "sessions.db"
    async def setup():
        store = Store(str(path))
        store.start()
        try:
            await store.ensure_assistant_composite_projection(stream_id=STREAM)
        finally:
            store.stop()
    asyncio.run(setup())
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE v2_assistant_composite_recovery_runs")
    conn.execute("DROP TABLE v2_assistant_composite_recovery_audit")
    for idx, (state, actor) in enumerate((("intent", "fixture-root:visible"),
                                           ("committed_pending", "fixture-root:visible"),
                                           ("uncertain", "other:actor"))):
        name = f"route-{idx}"
        conn.execute("INSERT INTO v2_assistant_composite_routes "
            "(route_id,stream_id,input_identity,payload_digest,input_request_id,event_id,body,attachments_json,"
            "routing_state,delivery_state,dispatch_id,route_target,route_target_generation,route_json,created_at,updated_at) "
            "VALUES (?,?,?,'d','req',?,'fixture','[]','resolved',?,?,'fixture-root:visible','gen-1',?,?,'2026-01-01T00:00:00Z')",
            (name, STREAM, f"input-{idx}", idx+1, state, f"dispatch-{idx}",
             '{ "admission_mode": "direct_primary", "landed_by_publication": "prior-key", "a": 1 }'
             if idx == 1 else '{"admission_mode":"direct_primary"}',
             '2026-01-01T00:00:00Z'))
        cur = conn.execute("INSERT INTO session_event_tail "
            "(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity) "
            "VALUES (?,'2026-01-01T00:00:00Z',?,?, '2026-01-01T00:01:00Z',0,?)",
            (STREAM, f"key-{idx}", json.dumps({"raw":{"actor_stream_id":actor}}), f"final-{idx}"))
        conn.execute("INSERT INTO v2_assistant_composite_publications "
            "(publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,reply_to_message_id,"
            "publish_kind,event_id,created_at) VALUES (?,?, 'p', ?, ?, ?,'prose',?,'2026-01-01T00:01:00Z')",
            (f"publish:dispatch-{idx}", STREAM, '{"response_state":"final"}',
             f"dispatch-{idx}", f"input-{idx}", cur.lastrowid))
    conn.commit()
    conn.close()
    return path


def call(path, *args):
    return subprocess.run([sys.executable, str(TOOL), "--db", str(path), "--stream", STREAM, *args],
                          text=True, capture_output=True)


def test_dry_run_preview_is_read_only_without_audit_tables(tmp_path):
    path = fixture_db(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    preview_file = tmp_path / "preview.json"
    run = call(path, "--dry-run", "--out", str(preview_file))
    assert run.returncode == 0, run.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    preview = json.loads(preview_file.read_text())
    assert preview["candidate_ids"] == ["route-0", "route-1"]
    assert preview["skipped"] == [{"route_id":"route-2","delivery_state":"uncertain","reason":"actor_mismatch"}]
    assert preview["runs_needing_attention"] == "no audit tables (no runs yet)"
    with closing(sqlite3.connect(path)) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='v2_assistant_composite_recovery_runs'").fetchone()
    immutable = call(path, "--dry-run", "--immutable", "--out", str(tmp_path / "immutable.json"))
    assert immutable.returncode == 0, immutable.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_apply_verify_rollback_is_byte_exact_and_repeat_safe(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        preimages = conn.execute("SELECT route_id,routing_state,delivery_state,error_code,updated_at,route_json "
                                 "FROM v2_assistant_composite_routes ORDER BY route_id").fetchall()
    applied = call(path, "--apply", "--run-id", "apply-1", "--backup-dir", str(tmp_path),
                   "--expect-ids", str(preview_file))
    assert applied.returncode == 0, applied.stderr
    applied_result = json.loads(applied.stdout)
    backup = tmp_path / "sessions-pre-apply-1.db"
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == applied_result["backup_sha256"]
    assert backup.stat().st_mode & 0o777 == 0o600
    assert call(path, "--verify", "--run-id", "apply-1").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_audit WHERE action='promote'").fetchone()[0] == 2
        assert conn.execute("SELECT delivery_state FROM v2_assistant_composite_routes WHERE route_id='route-2'").fetchone()[0] == "uncertain"
        conn.execute("UPDATE v2_assistant_composite_routes SET updated_at='unrelated-change' WHERE route_id='route-2'")
        conn.commit()
    stale = call(path, "--apply", "--run-id", "stale", "--backup-dir", str(tmp_path),
                 "--expect-ids", str(preview_file))
    assert stale.returncode == 2
    assert not (tmp_path / "sessions-pre-stale.db").exists()
    empty_preview = tmp_path / "empty.json"
    assert call(path, "--dry-run", "--out", str(empty_preview)).returncode == 0
    assert call(path, "--apply", "--run-id", "noop", "--backup-dir", str(tmp_path),
                "--expect-ids", str(empty_preview)).returncode == 0
    rollback = call(path, "--rollback", "--run-id", "apply-1", "--new-run-id", "rollback-1")
    assert rollback.returncode == 0, rollback.stderr
    assert "--verify --run-id rollback-1" in rollback.stdout
    assert call(path, "--verify", "--run-id", "rollback-1").returncode == 0
    assert call(path, "--verify", "--run-id", "apply-1").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        restored = conn.execute("SELECT route_id,routing_state,delivery_state,error_code,updated_at,route_json "
                                "FROM v2_assistant_composite_routes ORDER BY route_id").fetchall()
    assert restored[:2] == preimages[:2]
    assert restored[2] == (*preimages[2][:4], "unrelated-change", preimages[2][5])
    again = tmp_path / "again.json"
    assert call(path, "--dry-run", "--out", str(again)).returncode == 0
    assert json.loads(again.read_text())["candidate_ids"] == ["route-0", "route-1"]
    assert call(path, "--apply", "--run-id", "reapply", "--backup-dir", str(tmp_path),
                "--expect-ids", str(again)).returncode == 0


def test_preconditions_fail_closed_before_backup_or_run(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    held = sqlite3.connect(path)
    held.execute("SELECT count(*) FROM v2_assistant_composite_routes").fetchone()
    try:
        refusal = call(path, "--apply", "--run-id", "held", "--backup-dir", str(tmp_path),
                       "--expect-ids", str(preview_file))
        assert refusal.returncode == 2 and "db_held_open" in refusal.stderr
    finally:
        held.close()
    assert call(path, "--apply", "--immutable", "--run-id", "immutable", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 2
    bad = json.loads(preview_file.read_text())
    bad["candidate_ids"].pop()
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps(bad))
    refusal = call(path, "--apply", "--run-id", "mismatch", "--backup-dir", str(tmp_path),
                   "--expect-ids", str(bad_file))
    assert refusal.returncode == 2 and "expect_ids_mismatch" in refusal.stderr
    assert not (tmp_path / "sessions-pre-mismatch.db").exists()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_runs").fetchone()[0] == 0


def test_existing_backup_and_schema_version_refuse_without_run(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    backup = tmp_path / "sessions-pre-existing.db"
    backup.write_bytes(b"owned backup")
    refused = call(path, "--apply", "--run-id", "existing", "--backup-dir", str(tmp_path),
                   "--expect-ids", str(preview_file))
    assert refused.returncode == 2 and "backup_exists" in refused.stderr
    assert backup.read_bytes() == b"owned backup"
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_runs").fetchone()[0] == 0
        conn.execute("PRAGMA user_version=9")
        conn.commit()
    refused = call(path, "--dry-run", "--out", str(tmp_path / "version.json"))
    assert refused.returncode == 2 and "user_version_mismatch" in refused.stderr


@pytest.mark.parametrize("field,value", [
    ("updated_at", "changed"),
    ("route_json", '{ "admission_mode": "direct_primary", "landed_by_publication": "prior-key", "a": 2 }'),
])
def test_cas_preimage_change_aborts_whole_run(tmp_path, field, value):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"UPDATE v2_assistant_composite_routes SET {field}=? WHERE route_id='route-1'", (value,))
        conn.commit()
    failed = call(path, "--apply", "--run-id", "cas", "--backup-dir", str(tmp_path),
                  "--expect-ids", str(preview_file))
    assert failed.returncode == 2 and "preimage_mismatch:route-1" in failed.stderr
    assert (tmp_path / "sessions-pre-cas.db").exists()
    with closing(sqlite3.connect(path)) as conn:
        run = conn.execute("SELECT state,abort_reason,applied_count FROM v2_assistant_composite_recovery_runs WHERE run_id='cas'").fetchone()
        assert run == ("aborted", "preimage_mismatch:route-1", 0)
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_audit").fetchone()[0] == 0
        assert conn.execute("SELECT delivery_state FROM v2_assistant_composite_routes WHERE route_id='route-0'").fetchone()[0] == "intent"


def test_candidate_set_change_refuses_before_backup(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE v2_assistant_composite_routes SET delivery_state='landed' WHERE route_id='route-1'")
        conn.commit()
    failed = call(path, "--apply", "--run-id", "changed", "--backup-dir", str(tmp_path),
                  "--expect-ids", str(preview_file))
    assert failed.returncode == 2 and "expect_ids_mismatch" in failed.stderr
    assert not (tmp_path / "sessions-pre-changed.db").exists()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_runs").fetchone()[0] == 0


def test_apply_t2_refuses_run_abandoned_after_t1(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute("SELECT route_id,delivery_state,route_json FROM v2_assistant_composite_routes "
                              "ORDER BY route_id").fetchall()
    spec = importlib.util.spec_from_file_location("ccm_recovery_t2_state", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.insert_run
    def abandoned_after_t1(conn, **kwargs):
        original(conn, **kwargs)
        conn.execute(f"UPDATE {module.RUNS} SET state='abandoned',abort_reason='reconciled' WHERE run_id=?",
                     (kwargs["run_id"],))
        conn.commit()
    module.insert_run = abandoned_after_t1
    with pytest.raises(module.Refused, match="run_not_prepared"):
        module.run_apply(path, STREAM, "state-seam", preview_file, tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT route_id,delivery_state,route_json FROM v2_assistant_composite_routes "
                            "ORDER BY route_id").fetchall() == before
        assert conn.execute(f"SELECT count(*) FROM {module.AUDIT}").fetchone()[0] == 0
        assert conn.execute(f"SELECT state,abort_reason FROM {module.RUNS} WHERE run_id='state-seam'").fetchone() == (
            "abandoned", "reconciled")


def test_apply_t2_refuses_new_candidate_after_backup_preview(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    spec = importlib.util.spec_from_file_location("ccm_recovery_t2_set", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.insert_run
    def new_candidate_before_t1(conn, **kwargs):
        conn.execute("UPDATE session_event_tail SET event_json=? WHERE identity='final-2'",
                     (json.dumps({"raw": {"actor_stream_id": "fixture-root:visible"}}),))
        conn.commit()
        original(conn, **kwargs)
    module.insert_run = new_candidate_before_t1
    with pytest.raises(module.Refused, match="expect_ids_mismatch"):
        module.run_apply(path, STREAM, "set-seam", preview_file, tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT route_id,delivery_state FROM v2_assistant_composite_routes "
                            "ORDER BY route_id").fetchall() == [
                                ("route-0", "intent"), ("route-1", "committed_pending"),
                                ("route-2", "uncertain")]
        assert conn.execute(f"SELECT count(*) FROM {module.AUDIT}").fetchone()[0] == 0
        assert conn.execute(f"SELECT state,abort_reason,applied_count FROM {module.RUNS} "
                            "WHERE run_id='set-seam'").fetchone() == (
                                "aborted", "expect_ids_mismatch", 0)


@pytest.mark.parametrize("field,value", [
    ("routing_state", "deferred"), ("delivery_state", "uncertain"),
    ("error_code", "changed"), ("updated_at", "changed"),
    ("route_json", '{"changed":true}'),
])
def test_rollback_aborts_on_postimage_mismatch(tmp_path, field, value):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    assert call(path, "--apply", "--run-id", "apply", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute("SELECT delivery_state FROM v2_assistant_composite_routes WHERE route_id='route-0'").fetchone()[0]
        conn.execute(f"UPDATE v2_assistant_composite_routes SET {field}=? WHERE route_id='route-1'", (value,))
        conn.commit()
    failed = call(path, "--rollback", "--run-id", "apply", "--new-run-id", "rollback")
    assert failed.returncode == 2 and "postimage_mismatch:route-1" in failed.stderr
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT state FROM v2_assistant_composite_recovery_runs WHERE run_id='apply'").fetchone()[0] == "applied"
        assert conn.execute("SELECT state FROM v2_assistant_composite_recovery_runs WHERE run_id='rollback'").fetchone()[0] == "aborted"
        assert conn.execute("SELECT delivery_state FROM v2_assistant_composite_routes WHERE route_id='route-0'").fetchone()[0] == before
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_audit WHERE action='rollback'").fetchone()[0] == 0


def test_crash_after_prepared_requires_reconcile(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        before_routes = conn.execute("SELECT * FROM v2_assistant_composite_routes ORDER BY route_id").fetchall()
        before_audit = conn.execute("SELECT * FROM v2_assistant_composite_recovery_audit").fetchall() if (
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='v2_assistant_composite_recovery_audit'")
            .fetchone()) else []
    spec = importlib.util.spec_from_file_location("ccm_recovery_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import pytest
    with pytest.raises(RuntimeError, match="test_crash_after_T1"):
        module.run_apply(path, STREAM, "crash", preview_file, tmp_path, crash_after="after_T1")
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT * FROM v2_assistant_composite_routes ORDER BY route_id").fetchall() == before_routes
        assert conn.execute("SELECT * FROM v2_assistant_composite_recovery_audit").fetchall() == before_audit
    before_dry_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    dry = call(path, "--dry-run", "--out", str(tmp_path / "after-crash.json"))
    assert dry.returncode == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_dry_hash
    assert any(run["state"] == "prepared" for run in json.loads((tmp_path / "after-crash.json").read_text())["runs_needing_attention"])
    assert call(path, "--apply", "--run-id", "blocked", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 2
    assert call(path, "--reconcile", "--run-id", "crash").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT state FROM v2_assistant_composite_recovery_runs WHERE run_id='crash'").fetchone()[0] == "abandoned"


def test_crash_after_apply_commit_is_listed_then_verified(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    spec = importlib.util.spec_from_file_location("ccm_recovery_tool_after_t2", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="test_crash_after_T2_commit"):
        module.run_apply(path, STREAM, "after-t2", preview_file, tmp_path, crash_after="after_T2_commit")
    after = tmp_path / "after-t2.json"
    assert call(path, "--dry-run", "--out", str(after)).returncode == 0
    assert {"run_id":"after-t2","mode":"apply","state":"applied","verify_outcome":None} in json.loads(after.read_text())["runs_needing_attention"]
    assert call(path, "--verify", "--run-id", "after-t2").returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT verify_outcome FROM v2_assistant_composite_recovery_runs WHERE run_id='after-t2'").fetchone()[0] == "ok"


def test_reconcile_refuses_prepared_run_with_audit_rows(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    assert call(path, "--apply", "--run-id", "tampered", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE v2_assistant_composite_recovery_runs SET state='prepared' WHERE run_id='tampered'")
        conn.commit()
    refused = call(path, "--reconcile", "--run-id", "tampered")
    assert refused.returncode == 2 and "prepared_run_has_audit_rows" in refused.stderr
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT state FROM v2_assistant_composite_recovery_runs WHERE run_id='tampered'").fetchone()[0] == "prepared"


def test_tool_predicate_predicts_unchanged_startup_recovery(tmp_path):
    path = fixture_db(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE v2_assistant_composite_routes SET delivery_state='intent' WHERE route_id='route-2'")
        for idx, state in ((3, "committed_pending"), (4, "uncertain")):
            conn.execute("INSERT INTO v2_assistant_composite_routes "
                "(route_id,stream_id,input_identity,payload_digest,input_request_id,event_id,body,attachments_json,"
                "routing_state,delivery_state,dispatch_id,route_target,route_target_generation,route_json,created_at,updated_at) "
                "VALUES (?,?,?,'d','req',?,'fixture','[]','resolved',?,?,'fixture-root:visible','gen-1',"
                "'{\"admission_mode\":\"direct_primary\"}','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z')",
                (f"route-{idx}", STREAM, f"input-{idx}", idx+1, state, f"dispatch-{idx}"))
        event = conn.execute("INSERT INTO session_event_tail "
            "(stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity) "
            "VALUES (?,'2026-01-01T00:00:00Z','key-4',?, '2026-01-01T00:01:00Z',0,'final-4')",
            (STREAM, json.dumps({"raw":{"actor_stream_id":"fixture-root:visible"}})))
        conn.execute("INSERT INTO v2_assistant_composite_publications "
            "(publication_key,stream_id,payload_digest,canonical_payload_json,dispatch_id,reply_to_message_id,"
            "publish_kind,event_id,created_at) VALUES ('publish:dispatch-4',?,'p','{\"response_state\":\"final\"}',"
            "'dispatch-4','input-4','prose',?,'2026-01-01T00:01:00Z')", (STREAM, event.lastrowid))
        conn.commit()
    preview_file = tmp_path / "legacy.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    preview = json.loads(preview_file.read_text())
    assert preview["candidate_ids"] == ["route-0", "route-1", "route-4"]
    assert preview["skipped"] == [
        {"route_id":"route-2","delivery_state":"intent","reason":"actor_mismatch"},
        {"route_id":"route-3","delivery_state":"committed_pending","reason":"no_exact_final"},
    ]
    assert preview["expected_startup_retained_uncertain"] == 2
    assert call(path, "--apply", "--run-id", "legacy", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 0
    async def restart():
        store = Store(str(path))
        store.start()
        try:
            return await store.recover_assistant_composite_routes(stream_id=STREAM)
        finally:
            store.stop()
    assert asyncio.run(restart()) == {"requeued_classifying":0,"retained_uncertain":2}
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT route_id,delivery_state FROM v2_assistant_composite_routes ORDER BY route_id").fetchall()
    assert rows == [("route-0","landed"),("route-1","landed"),("route-2","uncertain"),
                    ("route-3","uncertain"),("route-4","landed")]


def test_audit_ddl_only_on_first_writable_command(tmp_path):
    path = fixture_db(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert call(path, "--dry-run", "--out", str(tmp_path / "first.json")).returncode == 0
    assert call(path, "--dry-run", "--immutable", "--out", str(tmp_path / "second.json")).returncode == 0
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'v2_assistant_composite_recovery_%'").fetchone()[0] == 0
    wrong = tmp_path / "wrong.json"
    payload = json.loads((tmp_path / "first.json").read_text())
    payload["candidate_ids"] = []
    wrong.write_text(json.dumps(payload))
    assert call(path, "--apply", "--run-id", "refused", "--backup-dir", str(tmp_path),
                "--expect-ids", str(wrong)).returncode == 2
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "v2_assistant_composite_recovery_runs", "v2_assistant_composite_recovery_audit"}
    async def reopen():
        store = Store(str(path))
        store.start()
        store.stop()
    asyncio.run(reopen())


def test_verify_mismatch_keeps_committed_apply(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    assert call(path, "--apply", "--run-id", "apply", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 0
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE v2_assistant_composite_routes SET route_json='{\"changed\":true}' WHERE route_id='route-0'")
        conn.commit()
    mismatch = call(path, "--verify", "--run-id", "apply")
    assert mismatch.returncode == 1
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT state,verify_outcome FROM v2_assistant_composite_recovery_runs WHERE run_id='apply'").fetchone() == ("applied", "mismatch")


def test_existing_run_commands_refuse_other_open_stream_and_closed_target(tmp_path):
    path = fixture_db(tmp_path)
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    assert call(path, "--apply", "--run-id", "scope-apply", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 0
    spec = importlib.util.spec_from_file_location("ccm_recovery_scope", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with closing(sqlite3.connect(path)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        other = list(conn.execute("SELECT * FROM sessions WHERE host='fixture-chat' "
                                  "AND session_name='assistant'").fetchone())
        other[columns.index("session_name")] = "other"
        conn.execute("INSERT INTO sessions(" + ",".join(columns) + ") VALUES (" +
                     ",".join("?" for _ in columns) + ")", other)
        conn.execute("UPDATE sessions SET status='closed' WHERE host='fixture-chat' "
                     "AND session_name='assistant'")
        conn.commit()
        before_routes = conn.execute("SELECT route_id,delivery_state,route_json "
                                     "FROM v2_assistant_composite_routes ORDER BY route_id").fetchall()
        before_audit = conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_audit").fetchone()[0]
    cases = (("--verify", "scope-apply"), ("--rollback", "scope-apply"),
             ("--reconcile", "scope-prepared"))
    for operation, run_id in cases:
        if operation == "--reconcile":
            conn = module.connect(path, "rw")
            try:
                module.insert_run(conn, run_id="scope-prepared", mode="apply", stream=STREAM,
                                  path=path, ids=[])
            finally:
                conn.close()
        suffix = ["--new-run-id", "scope-rb"] if operation == "--rollback" else []
        wrong = subprocess.run([sys.executable, str(TOOL), "--db", str(path),
                                "--stream", "fixture-chat:other", operation,
                                "--run-id", run_id, *suffix], text=True, capture_output=True)
        assert wrong.returncode == 2 and "run_stream_mismatch" in wrong.stderr
        closed = subprocess.run([sys.executable, str(TOOL), "--db", str(path),
                                 "--stream", STREAM, operation, "--run-id", run_id,
                                 *suffix], text=True, capture_output=True)
        assert closed.returncode == 2 and "composite_session_not_open" in closed.stderr
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT route_id,delivery_state,route_json "
                            "FROM v2_assistant_composite_routes ORDER BY route_id").fetchall() == before_routes
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_audit").fetchone()[0] == before_audit
        assert conn.execute("SELECT state FROM v2_assistant_composite_recovery_runs "
                            "WHERE run_id='scope-apply'").fetchone()[0] == "applied"
        assert conn.execute("SELECT state FROM v2_assistant_composite_recovery_runs "
                            "WHERE run_id='scope-prepared'").fetchone()[0] == "prepared"
        assert conn.execute("SELECT count(*) FROM v2_assistant_composite_recovery_runs "
                            "WHERE run_id='scope-rb'").fetchone()[0] == 0


def test_projection_after_recovery_and_rollback(tmp_path):
    path = fixture_db(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("UPDATE session_event_tail SET event_json=? WHERE identity='final-2'",
                     (json.dumps({"raw":{"actor_stream_id":"fixture-root:visible"}}),))
        conn.commit()
    async def projected():
        store = Store(str(path))
        store.start()
        try:
            return await store.assistant_composite_activity(stream_id=STREAM, input_ids=["input-2"])
        finally:
            store.stop()
    before = asyncio.run(projected())
    assert before["input-2"]["response_state"] == "uncertain"
    preview_file = tmp_path / "preview.json"
    assert call(path, "--dry-run", "--out", str(preview_file)).returncode == 0
    assert call(path, "--apply", "--run-id", "apply", "--backup-dir", str(tmp_path),
                "--expect-ids", str(preview_file)).returncode == 0
    after_apply = asyncio.run(projected())
    assert after_apply["input-2"]["response_state"] == "answered"
    assert call(path, "--rollback", "--run-id", "apply", "--new-run-id", "rollback").returncode == 0
    assert asyncio.run(projected())["input-2"]["response_state"] == "uncertain"
