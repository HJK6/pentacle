from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import history_repair

from claude_jsonl_norm import normalize_claude_jsonl_record
from history_repair import (
    RepairRefused,
    _event_references,
    _scope_from_snapshot,
    annotate_message_envelope,
    apply_manifest,
    freeze_manifest,
    manifest_sha256,
    rollback_manifest,
    scope_packet_sha256,
)
from store_routing import _outbound_notice_digest


HOST = "fixture-host"
SESSION = "v2-history-test"
STREAM = f"{HOST}:{SESSION}"
SESSION_CREATED_AT = "2026-01-02T03:04:05Z"
SESSION_GENERATION = "fixture-generation-1"
DATABASE_SNAPSHOT_SHA256 = "b" * 64
QUEUED_UUID = "00000000-0000-4000-8000-000000000001"
META_UUID = "00000000-0000-4000-8000-000000000002"
SOURCE_UUID = "synthetic-source-record"
NOTICE_ID = "notification-answer-00000000-0000-4000-8000-000000000003"
NOTIFICATION_ID = "00000000-0000-4000-8000-000000000003"
TIMESTAMP = "2026-01-02T03:05:00Z"
ADJUSTED_TIMESTAMP = "2026-01-02T03:05:30Z"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _source_rows() -> tuple[list[dict[str, Any]], str, str]:
    notice = (
        f"[pentacle-notice:{NOTICE_ID}]\n"
        "[notification.answer]\n"
        f"notification_id={NOTIFICATION_ID}\n"
        "answer=done\nchoice=True\nby=operator"
    )
    prompt = f'<pasted_content id="8d21">\n{notice}\n</pasted_content id="8d21">'
    queued = {
        "uuid": QUEUED_UUID,
        "sessionId": "synthetic-provider-session",
        "type": "attachment",
        "timestamp": TIMESTAMP,
        "attachment": {
            "type": "queued_command",
            "origin": {"kind": "human"},
            "source_uuid": SOURCE_UUID,
            "prompt": prompt,
        },
    }
    image_meta = {
        "uuid": META_UUID,
        "sessionId": "synthetic-provider-session",
        "type": "user",
        "timestamp": "2026-01-02T03:06:00Z",
        "isMeta": True,
        "turnCompanion": True,
        "message": {
            "content": "[Image: original 10x20, displayed at 5x10. "
            "Multiply coordinates by 2 to map to original image.]",
        },
    }
    return [queued, image_meta], prompt, notice


def _old_event(record: dict[str, Any], *, kind: str, text: str) -> dict[str, Any]:
    raw = {
        "provider": "claude",
        "uuid": record["uuid"],
        "jsonl_record_uuid": record["uuid"],
        "jsonl_event_index": 0,
        "host": HOST,
        "session_name": SESSION,
        "source_session_identity": record.get("sessionId"),
        "source": "claude-jsonl",
        "subtype": "queued-command" if record["uuid"] == QUEUED_UUID else None,
        "queued_at": TIMESTAMP if record["uuid"] == QUEUED_UUID else None,
        "user_delivery_state": "sent" if record["uuid"] == QUEUED_UUID else None,
        "legacy_ingest_audit": {"fixture": "preserve-me"} if record["uuid"] == QUEUED_UUID else None,
    }
    raw = {key: value for key, value in raw.items() if value is not None}
    return {
        "host": HOST,
        "session_name": SESSION,
        "session_id": record.get("sessionId") or "",
        "stream_id": STREAM,
        "provider": "claude",
        "timestamp": record["timestamp"],
        "kind": kind,
        "text": text,
        "raw": raw,
    }


def _identity(record: dict[str, Any], kind: str) -> str:
    return _canonical_json([STREAM, "claude", record["uuid"], 0, kind])


def _event_row(conn: sqlite3.Connection, *, event_id: int, record: dict[str, Any], event: dict[str, Any]) -> None:
    event_json = _canonical_json(event)
    conn.execute(
        """INSERT INTO session_event_tail(
               event_id,stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            event_id,
            STREAM,
            SESSION_CREATED_AT,
            _sha(event_json),
            event_json,
            event["timestamp"],
            1.25,
            _identity(record, event["kind"]),
        ),
    )


def _database(source_rows: list[dict[str, Any]], prompt: str, notice: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE sessions(host TEXT, session_name TEXT, created_at TEXT, PRIMARY KEY(host,session_name));
        CREATE TABLE v2_session_generations(host TEXT, session_name TEXT, generation TEXT,
            PRIMARY KEY(host,session_name));
        CREATE TABLE session_event_tail(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id TEXT NOT NULL,
            session_created_at TEXT NOT NULL,
            event_key TEXT NOT NULL,
            event_json TEXT NOT NULL,
            event_ts TEXT,
            recorded_at REAL NOT NULL,
            identity TEXT,
            UNIQUE(stream_id,session_created_at,event_key));
        CREATE UNIQUE INDEX idx_history_repair_identity
            ON session_event_tail(stream_id,session_created_at,identity);
        CREATE TABLE v2_outbound_notices(
            notice_id TEXT PRIMARY KEY, kind TEXT, dedupe_key TEXT, recipient_stream_id TEXT, tell_id TEXT,
            body TEXT, payload_digest TEXT, source_stream_id TEXT, episode_id TEXT, metadata TEXT, proof_binding TEXT);
        CREATE TABLE v2_tell_deliveries(tell_id TEXT PRIMARY KEY, reply TEXT NOT NULL);
        CREATE TABLE event_projection_refs(event_id INTEGER REFERENCES session_event_tail(event_id));
        """
    )
    conn.execute("INSERT INTO sessions VALUES(?,?,?)", (HOST, SESSION, SESSION_CREATED_AT))
    conn.execute(
        "INSERT INTO v2_session_generations VALUES(?,?,?)",
        (HOST, SESSION, SESSION_GENERATION),
    )
    metadata_json = _canonical_json({
        "notification_id": NOTIFICATION_ID,
        "producer_session_generation": SESSION_GENERATION,
    })
    dedupe_key = f"history-fixture:{NOTICE_ID}"
    payload_digest = _outbound_notice_digest(
        "notification_answer", dedupe_key, STREAM, NOTICE_ID, notice,
        metadata_json, "", "",
    )
    conn.execute(
        "INSERT INTO v2_outbound_notices VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            NOTICE_ID, "notification_answer", dedupe_key, STREAM, NOTICE_ID,
            notice, payload_digest, "", "", metadata_json, None,
        ),
    )
    delivery = {
        "tell_id": NOTICE_ID,
        "to_stream_id": STREAM,
        "text": notice,
        "notification_answer_generation": SESSION_GENERATION,
        "attachments": [],
    }
    conn.execute(
        "INSERT INTO v2_tell_deliveries VALUES(?,?)",
        (NOTICE_ID, json.dumps({"delivery": delivery})),
    )
    queue_record, meta_record = source_rows
    prior_event = {
        "host": HOST,
        "session_name": SESSION,
        "session_id": "synthetic-provider-session",
        "stream_id": STREAM,
        "provider": "claude",
        "timestamp": ADJUSTED_TIMESTAMP,
        "kind": "SYSTEM",
        "text": "synthetic earlier event timestamp",
        "raw": {"provider": "claude", "audit_marker": "preceding row"},
    }
    _event_row(
        conn,
        event_id=8999,
        record={"uuid": "00000000-0000-4000-8000-000000000009"},
        event=prior_event,
    )
    _event_row(
        conn,
        event_id=9001,
        record=queue_record,
        event={**_old_event(queue_record, kind="USER", text=prompt), "timestamp": ADJUSTED_TIMESTAMP},
    )
    # This legacy row is valid JSON with a matching event_key but is not in the
    # current canonical serializer format. Rollback must restore these exact bytes.
    legacy_event_json = json.dumps(
        json.loads(conn.execute("SELECT event_json FROM session_event_tail WHERE event_id=9001").fetchone()[0]),
        sort_keys=True,
        ensure_ascii=False,
    )
    conn.execute(
        "UPDATE session_event_tail SET event_json=?,event_key=? WHERE event_id=9001",
        (legacy_event_json, _sha(legacy_event_json)),
    )
    _event_row(
        conn,
        event_id=9002,
        record=meta_record,
        event=_old_event(meta_record, kind="USER", text=meta_record["message"]["content"]),
    )
    _event_row(
        conn,
        event_id=9003,
        record={"uuid": "00000000-0000-4000-8000-000000000004"},
        event={
            "host": HOST,
            "session_name": SESSION,
            "session_id": SESSION,
            "stream_id": STREAM,
            "provider": "claude",
            "timestamp": "2026-01-02T03:07:00Z",
            "kind": "TOOL_RESULT",
            "text": "synthetic retained tool result",
            "raw": {"provider": "claude", "uuid": "00000000-0000-4000-8000-000000000004"},
        },
    )
    conn.commit()
    return conn


def _scope_packet(
    source_rows: list[dict[str, Any]],
    record_digests: dict[str, str],
    source_jsonl_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "target": {
            "host": HOST,
            "session_name": SESSION,
            "stream_id": STREAM,
            "session_created_at": SESSION_CREATED_AT,
            "session_generation": SESSION_GENERATION,
        },
        "source": {
            "jsonl_sha256": source_jsonl_sha256,
            "records": [
                {
                    "class": "queued_notice",
                    "source_uuid": source_rows[0]["uuid"],
                    "source_line_number": 1,
                    "source_sha256": record_digests[source_rows[0]["uuid"]],
                    "attachment_source_uuid": SOURCE_UUID,
                },
                {
                    "class": "image_meta",
                    "source_uuid": source_rows[1]["uuid"],
                    "source_line_number": 2,
                    "source_sha256": record_digests[source_rows[1]["uuid"]],
                },
            ],
        },
        "database_snapshot_sha256": DATABASE_SNAPSHOT_SHA256,
    }


def _freeze(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    source_rows, _prompt, _notice = _source_rows()
    source_lines = {
        record["uuid"]: (_canonical_json(record) + "\n").encode("utf-8")
        for record in source_rows
    }
    digests = {source_uuid: _sha(raw_line) for source_uuid, raw_line in source_lines.items()}
    source_jsonl_sha256 = _sha(b"".join(source_lines[record["uuid"]] for record in source_rows))
    scope = _scope_packet(source_rows, digests, source_jsonl_sha256)
    manifest = freeze_manifest(
        conn,
        source_rows,
        scope_packet=scope,
        expected_scope_sha256=scope_packet_sha256(scope),
        source_record_sha256_by_uuid=digests,
        source_jsonl_sha256=source_jsonl_sha256,
        database_snapshot_sha256=DATABASE_SNAPSHOT_SHA256,
        normalizer=normalize_claude_jsonl_record,
        annotator=annotate_message_envelope,
    )
    return manifest, digests, source_rows


def _table_bytes(conn: sqlite3.Connection) -> dict[str, tuple[tuple[Any, ...], ...]]:
    table_names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    result: dict[str, tuple[tuple[Any, ...], ...]] = {}
    for table in table_names:
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        quoted = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
        rows = conn.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid').fetchall()
        result[table] = tuple(tuple(row) for row in rows)
    return result


def test_history_repair_freezes_only_source_proven_rows_and_keeps_identity() -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        manifest, digests, _ = _freeze(conn)
        assert manifest["target"]["stream_id"] == STREAM
        assert manifest["target"]["session_created_at"] == SESSION_CREATED_AT
        assert manifest["target"]["session_generation"] == SESSION_GENERATION
        actions = manifest["actions"]
        assert [action["operation"] for action in actions].count("replace") == 1
        assert [action["operation"] for action in actions].count("delete") == 1
        replacement = next(action for action in actions if action["operation"] == "replace")
        deletion = next(action for action in actions if action["operation"] == "delete")
        assert replacement["event_id"] == 9001
        assert replacement["identity"] == _identity(source_rows[0], "USER")
        assert replacement["source_sha256"] == digests[QUEUED_UUID]
        assert replacement["old_event_json_sha256"] == replacement["old_event_key"]
        assert replacement["old_event_json"] != _canonical_json(replacement["old_event"])
        assert replacement["new_event_json_sha256"] == replacement["new_event_key"]
        assert replacement["new_event"]["text"] == notice
        assert replacement["new_event"]["raw"]["provider_content"] == prompt
        assert replacement["new_event"]["raw"]["subtype"] == "queued-command"
        assert replacement["new_event"]["timestamp"] == ADJUSTED_TIMESTAMP
        assert replacement["new_event"]["raw"]["queued_at"] == TIMESTAMP
        assert replacement["new_event"]["raw"]["user_delivery_state"] == "sent"
        assert replacement["new_event"]["raw"]["legacy_ingest_audit"] == {"fixture": "preserve-me"}
        assert replacement["new_event"]["message_envelope"]["kind"] == "notification_answer"
        assert deletion["event_id"] == 9002
        assert deletion["identity"] == _identity(source_rows[1], "USER")
    finally:
        conn.close()


def test_history_repair_apply_twice_then_restore_byte_equal_table_contents(tmp_path: Path) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        manifest, _digests, _ = _freeze(conn)
        original_tables = _table_bytes(conn)
        preimages = tmp_path / "history-preimages.json"
        expected_manifest_sha256 = manifest_sha256(manifest)

        first = apply_manifest(
            conn,
            manifest,
            expected_manifest_sha256=expected_manifest_sha256,
            source_jsonl_sha256=manifest["source"]["jsonl_sha256"],
            preimages_path=preimages,
        )
        second = apply_manifest(
            conn,
            manifest,
            expected_manifest_sha256=expected_manifest_sha256,
            source_jsonl_sha256=manifest["source"]["jsonl_sha256"],
            preimages_path=preimages,
        )
        assert first["status"] == "applied"
        assert second["status"] == "already_applied"

        retained = conn.execute("SELECT * FROM session_event_tail WHERE event_id=9001").fetchone()
        corrected = json.loads(retained["event_json"])
        assert retained["event_key"] == _sha(retained["event_json"])
        assert corrected["text"] == notice
        assert retained["identity"] == _identity(source_rows[0], "USER")
        assert retained["event_ts"] == ADJUSTED_TIMESTAMP
        assert retained["recorded_at"] == 1.25
        assert corrected["raw"]["queued_at"] == TIMESTAMP
        assert corrected["raw"]["user_delivery_state"] == "sent"
        assert corrected["raw"]["legacy_ingest_audit"] == {"fixture": "preserve-me"}
        assert conn.execute("SELECT 1 FROM session_event_tail WHERE event_id=9002").fetchone() is None
        assert conn.execute("SELECT 1 FROM session_event_tail WHERE event_id=9003").fetchone() is not None

        rollback = rollback_manifest(
            conn,
            manifest,
            expected_manifest_sha256=expected_manifest_sha256,
            source_jsonl_sha256=manifest["source"]["jsonl_sha256"],
            preimages_path=preimages,
        )
        assert rollback["status"] == "rolled_back"
        assert _table_bytes(conn) == original_tables
        assert json.loads(preimages.read_text())["manifest_sha256"] == expected_manifest_sha256
    finally:
        conn.close()


def test_history_repair_refuses_changed_source_or_dangling_meta_reference(tmp_path: Path) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        manifest, _digests, _ = _freeze(conn)
        manifest_sha = manifest_sha256(manifest)
        preimages = tmp_path / "history-preimages.json"
        with pytest.raises(RepairRefused, match="source.*digest"):
            apply_manifest(
                conn,
                manifest,
                expected_manifest_sha256=manifest_sha,
                source_jsonl_sha256="c" * 64,
                preimages_path=preimages,
            )
        conn.execute("INSERT INTO event_projection_refs(event_id) VALUES(9002)")
        conn.commit()
        before = _table_bytes(conn)
        with pytest.raises(RepairRefused, match="reference"):
            apply_manifest(
                conn,
                manifest,
                expected_manifest_sha256=manifest_sha,
                source_jsonl_sha256=manifest["source"]["jsonl_sha256"],
                preimages_path=preimages,
            )
        assert _table_bytes(conn) == before
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("v2_outbound_notices", "proof_binding", '{"proof_event_id":9002}'),
        ("v2_outbound_notices", "metadata", '{"event_id":9002}'),
        ("v2_tell_deliveries", "reply", '{"delivery":{},"source_event_id":9002}'),
    ],
)
def test_reference_scan_covers_live_binding_and_reply_columns(
    table: str, column: str, value: str,
) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        conn.execute(f'UPDATE "{table}" SET "{column}"=?', (value,))
        refs = _event_references(conn, 9002)
        assert f"{table}.{column}" in refs
    finally:
        conn.close()


def test_reference_scan_checks_other_tail_event_json_mirrors() -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        mirror = {
            "host": HOST,
            "session_name": SESSION,
            "session_id": SESSION,
            "stream_id": STREAM,
            "provider": "claude",
            "timestamp": "2026-01-02T03:08:00Z",
            "kind": "SYSTEM",
            "text": "synthetic mirror reference",
            "raw": {"mirrored_from": {"event_id": 9002}},
        }
        _event_row(
            conn,
            event_id=9004,
            record={"uuid": "00000000-0000-4000-8000-000000000005"},
            event=mirror,
        )
        assert "session_event_tail.event_json" in _event_references(conn, 9002)
    finally:
        conn.close()


def test_freeze_refuses_source_jsonl_index_that_differs_from_stored_identity() -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        row = conn.execute("SELECT event_json FROM session_event_tail WHERE event_id=9001").fetchone()
        event = json.loads(row[0])
        event["raw"]["jsonl_event_index"] = 5
        event_json = _canonical_json(event)
        conn.execute(
            "UPDATE session_event_tail SET event_json=?,event_key=?,identity=? WHERE event_id=9001",
            (
                event_json,
                _sha(event_json),
                _canonical_json([STREAM, "claude", QUEUED_UUID, 5, "USER"]),
            ),
        )
        conn.commit()
        with pytest.raises(RepairRefused, match="queued source identity mismatch: jsonl_event_index"):
            _freeze(conn)
    finally:
        conn.close()


def test_scope_seed_pins_existing_session_generation(tmp_path: Path) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    source_path = tmp_path / "source.jsonl"
    source_path.write_bytes(b"".join(
        json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        for row in source_rows
    ))
    seed = {
        "target": {
            "host": HOST,
            "session_name": SESSION,
            "stream_id": STREAM,
            "session_created_at": SESSION_CREATED_AT,
            "session_generation": SESSION_GENERATION,
        },
        "queued_notice": {
            "source_uuid": QUEUED_UUID,
            "attachment_source_uuid": SOURCE_UUID,
        },
    }
    try:
        scope, records, _digests = _scope_from_snapshot(
            conn, seed=seed, source_path=source_path, snapshot_sha256=DATABASE_SNAPSHOT_SHA256,
        )
        assert scope["target"]["session_generation"] == SESSION_GENERATION
        assert len(records) == 2
        wrong_seed = json.loads(json.dumps(seed))
        wrong_seed["target"]["session_generation"] = "changed-generation"
        with pytest.raises(RepairRefused, match="session generation changed"):
            _scope_from_snapshot(
                conn, seed=wrong_seed, source_path=source_path,
                snapshot_sha256=DATABASE_SNAPSHOT_SHA256,
            )
    finally:
        conn.close()


def test_already_applied_refuses_changed_generation_and_operator_binding(tmp_path: Path) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        manifest, _digests, _ = _freeze(conn)
        manifest_sha = manifest_sha256(manifest)
        preimages = tmp_path / "history-preimages.json"
        apply_manifest(
            conn, manifest, expected_manifest_sha256=manifest_sha,
            source_jsonl_sha256=manifest["source"]["jsonl_sha256"], preimages_path=preimages,
        )
        conn.execute(
            "UPDATE v2_session_generations SET generation='changed-generation' WHERE host=? AND session_name=?",
            (HOST, SESSION),
        )
        conn.commit()
        with pytest.raises(RepairRefused, match="generation changed"):
            apply_manifest(
                conn, manifest, expected_manifest_sha256=manifest_sha,
                source_jsonl_sha256=manifest["source"]["jsonl_sha256"], preimages_path=preimages,
            )
        conn.execute(
            "UPDATE v2_session_generations SET generation=? WHERE host=? AND session_name=?",
            (SESSION_GENERATION, HOST, SESSION),
        )
        conn.execute(
            "UPDATE v2_outbound_notices SET proof_binding=? WHERE notice_id=?",
            ('{"audit":"changed"}', NOTICE_ID),
        )
        conn.commit()
        with pytest.raises(RepairRefused, match="operator send/attachment binding changed"):
            apply_manifest(
                conn, manifest, expected_manifest_sha256=manifest_sha,
                source_jsonl_sha256=manifest["source"]["jsonl_sha256"], preimages_path=preimages,
            )
    finally:
        conn.close()


def test_already_rolled_back_refuses_changed_event_hash(tmp_path: Path) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        manifest, _digests, _ = _freeze(conn)
        manifest_sha = manifest_sha256(manifest)
        preimages = tmp_path / "history-preimages.json"
        apply_manifest(
            conn, manifest, expected_manifest_sha256=manifest_sha,
            source_jsonl_sha256=manifest["source"]["jsonl_sha256"], preimages_path=preimages,
        )
        rollback_manifest(
            conn, manifest, expected_manifest_sha256=manifest_sha,
            source_jsonl_sha256=manifest["source"]["jsonl_sha256"], preimages_path=preimages,
        )
        conn.execute(
            "UPDATE session_event_tail SET event_json='{}',event_key=? WHERE event_id=9001",
            (_sha("{}"),),
        )
        conn.commit()
        with pytest.raises(RepairRefused, match="CAS precondition failed"):
            rollback_manifest(
                conn, manifest, expected_manifest_sha256=manifest_sha,
                source_jsonl_sha256=manifest["source"]["jsonl_sha256"], preimages_path=preimages,
            )
    finally:
        conn.close()


@pytest.mark.parametrize("fields", [("session_id",), ("source_session_identity",),
                                    ("session_id", "source_session_identity")])
def test_metadata_freeze_refuses_contradicting_provider_session(fields: tuple[str, ...]) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        event = json.loads(conn.execute(
            "SELECT event_json FROM session_event_tail WHERE event_id=9002",
        ).fetchone()[0])
        for field in fields:
            if field == "session_id":
                event[field] = "different-provider-session"
            else:
                event["raw"][field] = "different-provider-session"
        event_json = _canonical_json(event)
        conn.execute("UPDATE session_event_tail SET event_json=?,event_key=? WHERE event_id=9002",
                     (event_json, _sha(event_json)))
        conn.commit()
        with pytest.raises(RepairRefused, match="provider.session identity"):
            _freeze(conn)
    finally:
        conn.close()


@pytest.mark.parametrize("retry_drift", [None, "preimage", "generation", "operator_binding"])
def test_interrupted_rollback_reconciles_only_exact_restored_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_drift: str | None,
) -> None:
    source_rows, prompt, notice = _source_rows()
    conn = _database(source_rows, prompt, notice)
    try:
        manifest, _digests, _ = _freeze(conn)
        original = _table_bytes(conn)
        preimages = tmp_path / "history-preimages.json"
        kwargs = {"expected_manifest_sha256": manifest_sha256(manifest),
                  "source_jsonl_sha256": manifest["source"]["jsonl_sha256"],
                  "preimages_path": preimages}
        apply_manifest(conn, manifest, **kwargs)
        def fail_receipt_write(*_args: Any) -> None:
            raise OSError("injected receipt write failure")
        with monkeypatch.context() as fault:
            fault.setattr(history_repair, "_atomic_write_json", fail_receipt_write)
            with pytest.raises(OSError, match="injected receipt write failure"):
                rollback_manifest(conn, manifest, **kwargs)
        assert _table_bytes(conn) == original
        assert json.loads(preimages.read_text())["status"] == "applied"
        if retry_drift == "preimage":
            saved = json.loads(preimages.read_text())
            saved["rows"][0]["event_json"] = "{}"
            preimages.write_text(json.dumps(saved))
        elif retry_drift == "generation":
            conn.execute("UPDATE v2_session_generations SET generation='changed-generation'")
            conn.commit()
        elif retry_drift == "operator_binding":
            conn.execute("UPDATE v2_outbound_notices SET proof_binding='{}'")
            conn.commit()
        if retry_drift is not None:
            with pytest.raises(RepairRefused):
                rollback_manifest(conn, manifest, **kwargs)
            assert json.loads(preimages.read_text())["status"] == "applied"
        else:
            assert rollback_manifest(conn, manifest, **kwargs)["status"] == "already_rolled_back"
            assert json.loads(preimages.read_text())["status"] == "rolled_back"
            assert rollback_manifest(conn, manifest, **kwargs)["status"] == "already_rolled_back"
            assert _table_bytes(conn) == original
    finally:
        conn.close()
