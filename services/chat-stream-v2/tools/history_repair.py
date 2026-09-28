"""One-time, scope-packet-bound repair of two Claude history row classes.

This is intentionally not a general migration framework.  It only replaces a
source-proven queued notification USER row and removes source-proven Claude
image-coordinate metadata USER rows.  A separately reviewed external scope
packet selects the stream, generation and source UUIDs; the manifest SHA is a
required apply/rollback guard.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from claude_jsonl_norm import normalize_claude_jsonl_record  # noqa: E402
from message_envelopes import annotate_message_envelope, match_message_envelope  # noqa: E402
from store_routing import _outbound_notice_digest  # noqa: E402


SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_COORDINATE_META = re.compile(
    r"\[Image: original [0-9]+x[0-9]+, displayed at [0-9]+x[0-9]+\. "
    r"Multiply coordinates by [0-9]+(?:\.[0-9]+)? to map to original image\.\]"
)
_ALLOWED_CLASSES = frozenset({"queued_notice", "image_meta"})
_EVENT_COLUMNS = (
    "event_id", "stream_id", "session_created_at", "event_key", "event_json",
    "event_ts", "recorded_at", "identity",
)


class RepairRefused(RuntimeError):
    """The frozen evidence or current database did not satisfy a repair guard."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def event_json_bytes(event: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(event)


def event_key(event: Mapping[str, Any]) -> str:
    return _sha256_bytes(event_json_bytes(event))


def scope_packet_sha256(scope_packet: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json_bytes(scope_packet))


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return _sha256_bytes(canonical_json_bytes(manifest))


def raw_jsonl_line_sha256(raw_line: bytes) -> str:
    """Hash exact source bytes, including LF/CRLF when present; no JSON re-encode."""
    return _sha256_bytes(raw_line)


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RepairRefused(f"{label} must be a lowercase SHA-256 digest")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepairRefused(f"{label} must be an object")
    return dict(value)


def _validate_scope_packet(
    scope_packet: Mapping[str, Any], *, expected_scope_sha256: str,
    source_jsonl_sha256: str, database_snapshot_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scope = _mapping(scope_packet, "scope packet")
    if scope.get("schema_version") != SCHEMA_VERSION:
        raise RepairRefused("unsupported scope packet schema")
    expected_scope = _require_sha(expected_scope_sha256, "expected scope SHA")
    if scope_packet_sha256(scope) != expected_scope:
        raise RepairRefused("scope packet SHA mismatch")

    target = _mapping(scope.get("target"), "scope target")
    required_target = ("host", "session_name", "stream_id", "session_created_at", "session_generation")
    if any(not isinstance(target.get(key), str) or not target[key] for key in required_target):
        raise RepairRefused("scope target is incomplete")
    if target["stream_id"] != f"{target['host']}:{target['session_name']}":
        raise RepairRefused("scope stream/session binding mismatch")

    source = _mapping(scope.get("source"), "scope source")
    if _require_sha(source.get("jsonl_sha256"), "scope source JSONL SHA") != source_jsonl_sha256:
        raise RepairRefused("source JSONL digest mismatch")
    if _require_sha(scope.get("database_snapshot_sha256"), "scope database snapshot SHA") != database_snapshot_sha256:
        raise RepairRefused("database snapshot digest mismatch")

    records = source.get("records")
    if not isinstance(records, list) or not records:
        raise RepairRefused("scope packet has no source records")
    queued_count = 0
    seen: set[str] = set()
    normalized_records: list[dict[str, Any]] = []
    for raw_item in records:
        item = _mapping(raw_item, "scope source record")
        row_class = item.get("class")
        source_uuid = item.get("source_uuid")
        if row_class not in _ALLOWED_CLASSES:
            raise RepairRefused("scope packet contains an unsupported row class")
        if not isinstance(source_uuid, str) or not source_uuid or source_uuid in seen:
            raise RepairRefused("scope packet source UUID is missing or duplicated")
        seen.add(source_uuid)
        _require_sha(item.get("source_sha256"), "source record SHA")
        line_number = item.get("source_line_number")
        if isinstance(line_number, bool) or not isinstance(line_number, int) or line_number < 1:
            raise RepairRefused("source line number must be positive")
        if row_class == "queued_notice":
            queued_count += 1
            if not isinstance(item.get("attachment_source_uuid"), str) or not item["attachment_source_uuid"]:
                raise RepairRefused("queued notice attachment source binding is missing")
        normalized_records.append(item)
    if queued_count != 1:
        raise RepairRefused("scope packet must select exactly one queued notice record")
    normalized_records.sort(key=lambda item: (item["source_line_number"], item["source_uuid"]))
    return target, normalized_records


def _read_session_binding(conn: sqlite3.Connection, target: Mapping[str, Any]) -> tuple[str, str]:
    host, session_name = target["host"], target["session_name"]
    try:
        session = conn.execute(
            "SELECT created_at FROM sessions WHERE host=? AND session_name=?",
            (host, session_name),
        ).fetchone()
        generation = conn.execute(
            "SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?",
            (host, session_name),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RepairRefused("database lacks required session generation tables") from exc
    if session is None or generation is None:
        raise RepairRefused("target session generation is missing")
    created_at = str(session["created_at"] if isinstance(session, sqlite3.Row) else session[0] or "")
    actual_generation = str(generation["generation"] if isinstance(generation, sqlite3.Row) else generation[0] or "")
    if created_at != target["session_created_at"] or actual_generation != target["session_generation"]:
        raise RepairRefused("target session generation changed")
    return created_at, actual_generation


def _source_record_by_uuid(source_records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in source_records:
        if not isinstance(record, dict):
            raise RepairRefused("source records must be raw JSON objects")
        source_uuid = record.get("uuid")
        if not isinstance(source_uuid, str) or not source_uuid or source_uuid in result:
            raise RepairRefused("source record UUID is missing or duplicated")
        result[source_uuid] = record
    return result


def _verify_source_shape(item: Mapping[str, Any], record: Mapping[str, Any]) -> str:
    source_uuid = str(item["source_uuid"])
    if record.get("uuid") != source_uuid:
        raise RepairRefused("source UUID mismatch")
    if item["class"] == "queued_notice":
        attachment = record.get("attachment")
        if (record.get("type") != "attachment" or not isinstance(attachment, dict)
                or attachment.get("type") != "queued_command"
                or not isinstance(attachment.get("origin"), dict)
                or attachment["origin"].get("kind") != "human"
                or attachment.get("source_uuid") != item.get("attachment_source_uuid")
                or not isinstance(attachment.get("prompt"), str)
                or not attachment["prompt"]):
            raise RepairRefused("queued notice source/attachment binding mismatch")
        return attachment["prompt"]
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if (record.get("type") != "user" or record.get("isMeta") is not True
            or record.get("turnCompanion") is not True or not isinstance(content, str)
            or _IMAGE_COORDINATE_META.fullmatch(content) is None):
        raise RepairRefused("source record no longer matches image metadata predicate")
    return content


def _rows_for_source_uuid(
    conn: sqlite3.Connection, *, stream_id: str, session_created_at: str, source_uuid: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_id,stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity "
        "FROM session_event_tail WHERE stream_id=? AND session_created_at=? ORDER BY event_id",
        (stream_id, session_created_at),
    ).fetchall()
    matches = []
    for raw_row in rows:
        row = dict(raw_row) if isinstance(raw_row, sqlite3.Row) else dict(zip(_EVENT_COLUMNS, raw_row))
        try:
            event = json.loads(row["event_json"])
        except (TypeError, ValueError) as exc:
            raise RepairRefused("target session contains invalid event JSON") from exc
        raw = event.get("raw") if isinstance(event, dict) else None
        if isinstance(raw, dict) and raw.get("jsonl_record_uuid") == source_uuid:
            matches.append(row)
    return matches


def _event_for_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        event = json.loads(str(row["event_json"]))
    except (TypeError, ValueError) as exc:
        raise RepairRefused("target event JSON is invalid") from exc
    if not isinstance(event, dict):
        raise RepairRefused("target event JSON is not an object")
    return event


def _identity_key(event: Mapping[str, Any]) -> str:
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    record_uuid = raw.get("jsonl_record_uuid")
    if record_uuid is None:
        raise RepairRefused("source event has no durable JSONL UUID")
    identity = [
        str(event.get("stream_id") or ""),
        str(event.get("provider") or ""),
        str(record_uuid),
        raw.get("jsonl_event_index"),
        str(event.get("kind") or ""),
    ]
    return json.dumps(identity, separators=(",", ":"))


def _validate_existing_row(
    row: Mapping[str, Any], *, target: Mapping[str, Any], source_uuid: str,
    source_text: str, row_class: str, source_record: Mapping[str, Any],
) -> dict[str, Any]:
    event = _event_for_row(row)
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    source_session_id = str(source_record.get("sessionId") or source_record.get("session_id") or "")
    if (event.get("session_id") != source_session_id
            or raw.get("source_session_identity") != (source_session_id or None)):
        raise RepairRefused("database/source provider-session identity mismatch")
    if (row["stream_id"] != target["stream_id"]
            or row["session_created_at"] != target["session_created_at"]
            or event.get("stream_id") != target["stream_id"]
            or event.get("provider") != "claude"
            or event.get("kind") != "USER"
            or raw.get("provider") != "claude"
            or raw.get("jsonl_record_uuid") != source_uuid
            or raw.get("uuid") != source_uuid
            or row.get("identity") != _identity_key(event)):
        raise RepairRefused("database identity/generation binding mismatch")
    if event.get("text") != source_text:
        raise RepairRefused("database/source event content mismatch")
    old_json = str(row["event_json"])
    old_hash = _sha256_bytes(old_json.encode("utf-8"))
    if row["event_key"] != old_hash:
        raise RepairRefused("database event_key does not match event_json")
    if row_class == "queued_notice" and raw.get("subtype") != "queued-command":
        raise RepairRefused("queued notice subtype binding mismatch")
    if row.get("event_ts") != event.get("timestamp"):
        raise RepairRefused("database event_ts differs from stored event timestamp")
    return event


def _parse_timestamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _latest_timestamp(current: str | None, candidate: Any) -> str | None:
    candidate_text = str(candidate or "")
    if not candidate_text:
        return current
    if current is None:
        return candidate_text
    current_epoch = _parse_timestamp_epoch(current)
    candidate_epoch = _parse_timestamp_epoch(candidate_text)
    if current_epoch is None:
        return candidate_text
    if candidate_epoch is None:
        return current
    return candidate_text if candidate_epoch >= current_epoch else current


def _expected_queued_timestamp(
    conn: sqlite3.Connection, *, row: Mapping[str, Any], source_timestamp: Any,
) -> str:
    original = str(source_timestamp or "")
    if not original or _parse_timestamp_epoch(original) is None:
        raise RepairRefused("queued source timestamp is missing or invalid")
    prior_rows = conn.execute(
        "SELECT event_json FROM session_event_tail "
        "WHERE stream_id=? AND session_created_at=? AND event_id<? ORDER BY event_id",
        (row["stream_id"], row["session_created_at"], row["event_id"]),
    ).fetchall()
    latest: str | None = None
    for prior in prior_rows:
        raw_json = prior["event_json"] if isinstance(prior, sqlite3.Row) else prior[0]
        try:
            prior_event = json.loads(raw_json)
        except (TypeError, ValueError) as exc:
            raise RepairRefused("prior target event has invalid JSON timestamp audit") from exc
        if isinstance(prior_event, dict):
            latest = _latest_timestamp(latest, prior_event.get("timestamp"))
    original_epoch = _parse_timestamp_epoch(original)
    latest_epoch = _parse_timestamp_epoch(latest)
    if latest is not None and latest_epoch is None:
        raise RepairRefused("prior target event timestamp is invalid")
    return latest if latest_epoch is not None and original_epoch is not None and original_epoch < latest_epoch else original


def _check_stable_identity(old: Mapping[str, Any], new: Mapping[str, Any], row_identity: str) -> None:
    for key in ("host", "session_name", "session_id", "stream_id", "provider", "timestamp", "kind"):
        if old.get(key) != new.get(key):
            raise RepairRefused(f"replacement changed stable event binding: {key}")
    old_raw = old.get("raw") if isinstance(old.get("raw"), dict) else {}
    new_raw = new.get("raw") if isinstance(new.get("raw"), dict) else {}
    for key in (
        "provider", "uuid", "jsonl_record_uuid", "jsonl_event_index", "parent_uuid",
        "subtype", "queued_at", "host", "session_name", "source_session_identity",
    ):
        if old_raw.get(key) != new_raw.get(key):
            raise RepairRefused(f"replacement changed source identity binding: {key}")
    if _identity_key(new) != row_identity:
        raise RepairRefused("replacement durable identity differs from stored row")


def _hash_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    return _sha256_bytes(str(value).encode("utf-8"))


def _notice_binding_snapshot(
    conn: sqlite3.Connection, *, target: Mapping[str, Any], event_id: int,
    source_attachment_uuid: str, event: Mapping[str, Any],
) -> dict[str, Any]:
    text = event.get("text")
    tag = event.get("message_envelope")
    if (not isinstance(text, str) or not isinstance(tag, dict)
            or tag.get("kind") != "notification_answer" or not isinstance(tag.get("id"), str)):
        raise RepairRefused("queued source did not normalize to a registered notification answer")
    matched = match_message_envelope(text)
    if (not isinstance(matched, dict) or matched.get("kind") != "notification_answer"
            or matched.get("id") != tag["id"]):
        raise RepairRefused("notice registry tag does not match normalized display text")
    try:
        notice = conn.execute(
            "SELECT notice_id,kind,dedupe_key,recipient_stream_id,tell_id,body,payload_digest,"
            "source_stream_id,episode_id,metadata,proof_binding "
            "FROM v2_outbound_notices WHERE notice_id=?", (tag["id"],),
        ).fetchone()
        delivery_row = conn.execute(
            "SELECT reply FROM v2_tell_deliveries WHERE tell_id=?", (tag["id"],),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RepairRefused("database lacks notification binding tables") from exc
    if notice is None or delivery_row is None:
        raise RepairRefused("notification send binding is missing")
    notice = dict(notice) if isinstance(notice, sqlite3.Row) else dict(zip(
        ("notice_id", "kind", "dedupe_key", "recipient_stream_id", "tell_id", "body", "payload_digest",
         "source_stream_id", "episode_id", "metadata", "proof_binding"), notice
    ))
    delivery_json = delivery_row["reply"] if isinstance(delivery_row, sqlite3.Row) else delivery_row[0]
    try:
        delivery_record = json.loads(delivery_json)
    except (TypeError, ValueError) as exc:
        raise RepairRefused("notification delivery binding is invalid") from exc
    delivery = delivery_record.get("delivery") if isinstance(delivery_record, dict) else None
    if not isinstance(delivery, dict):
        raise RepairRefused("notification delivery payload is missing")
    try:
        metadata = json.loads(notice["metadata"]) if isinstance(notice["metadata"], str) else notice["metadata"]
    except (TypeError, ValueError) as exc:
        raise RepairRefused("notification metadata binding is invalid") from exc
    if not isinstance(metadata, dict):
        raise RepairRefused("notification metadata binding is missing")
    if not isinstance(notice["metadata"], str):
        raise RepairRefused("notification metadata JSON bytes are missing")
    expected_payload_digest = _outbound_notice_digest(
        str(notice["kind"]), str(notice["dedupe_key"]),
        str(notice["recipient_stream_id"]), str(notice["tell_id"]),
        str(notice["body"]), notice["metadata"],
        str(notice["source_stream_id"] or ""), str(notice["episode_id"] or ""),
    )
    if notice["payload_digest"] != expected_payload_digest:
        raise RepairRefused("notification payload_digest does not match store routing digest")
    if (notice["kind"] != "notification_answer"
            or notice["recipient_stream_id"] != target["stream_id"]
            or notice["tell_id"] != tag["id"]
            or notice["body"] != text
            or delivery.get("tell_id") != tag["id"]
            or delivery.get("to_stream_id") != target["stream_id"]
            or delivery.get("text") != text
            or delivery.get("notification_answer_generation") != target["session_generation"]
            or metadata.get("producer_session_generation") != target["session_generation"]):
        raise RepairRefused("operator notification send binding mismatch")
    if metadata.get("producer_stream_id") not in (None, target["stream_id"]):
        raise RepairRefused("notification producer stream binding mismatch")
    attachments = delivery.get("attachments", [])
    if attachments not in (None, []):
        raise RepairRefused("notification attachment binding does not match source record")
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if (raw.get("provider_content") is None
            or raw.get("provider_content") == text
            or raw.get("envelope_source") != raw.get("provider_content")):
        raise RepairRefused("queued provider source bytes are not preserved")

    proof_binding = notice.get("proof_binding")
    if proof_binding:
        try:
            proof = json.loads(proof_binding) if isinstance(proof_binding, str) else proof_binding
        except (TypeError, ValueError) as exc:
            raise RepairRefused("notice proof binding is invalid") from exc
        if isinstance(proof, dict):
            bound_event = proof.get("event_id", proof.get("proof_event_id"))
            if bound_event is not None and int(bound_event) != int(event_id):
                raise RepairRefused("notification proof event binding mismatch")
    return {
        "notice_id": notice["notice_id"],
        "notice_kind": notice["kind"],
        "recipient_stream_id": notice["recipient_stream_id"],
        "tell_id": notice["tell_id"],
        "notice_body_sha256": _hash_optional_text(notice["body"]),
        "notice_payload_digest": notice["payload_digest"],
        "notice_metadata_sha256": _sha256_bytes(canonical_json_bytes(metadata)),
        "notice_proof_binding_sha256": _hash_optional_text(
            canonical_json_bytes(proof_binding).decode("utf-8") if isinstance(proof_binding, (dict, list)) else proof_binding
        ),
        "delivery_reply_sha256": _hash_optional_text(delivery_json),
        "delivery_text_sha256": _hash_optional_text(delivery.get("text")),
        "delivery_generation": delivery.get("notification_answer_generation"),
        "attachment_count": len(attachments or []),
        "source_attachment_uuid": source_attachment_uuid,
    }


def _foreign_key_violations(conn: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    try:
        return tuple(tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall())
    except sqlite3.Error as exc:
        raise RepairRefused("could not inspect database foreign keys") from exc


def _json_references_event_id(value: Any, event_id: int) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key).lower()
            if "event_id" in key_text or "event_ids" in key_text:
                if nested == event_id or nested == str(event_id):
                    return True
                if isinstance(nested, list) and any(item == event_id or item == str(event_id) for item in nested):
                    return True
            if _json_references_event_id(nested, event_id):
                return True
    elif isinstance(value, list):
        return any(_json_references_event_id(item, event_id) for item in value)
    return False


def _event_references_many(conn: sqlite3.Connection, event_ids: Iterable[int]) -> dict[int, list[str]]:
    target_ids = {int(value) for value in event_ids}
    refs: dict[int, set[str]] = {event_id: set() for event_id in target_ids}
    if not target_ids:
        return {}
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    for table in tables:
        quoted_table = '"' + str(table).replace('"', '""') + '"'
        info = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
        columns = [str(row[1]) for row in info]
        if not columns:
            continue
        quoted_columns = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
        for row in conn.execute(f"SELECT {quoted_columns} FROM {quoted_table}"):
            values = dict(zip(columns, row))
            # The candidate tail row's event_id is its primary key, not a mirror.
            # All other tail rows remain in scope for JSON/direct references.
            if table == "session_event_tail" and values.get("event_id") in target_ids:
                continue
            for column, value in values.items():
                low = column.lower()
                label = f"{table}.{column}"
                if ("event_id" in low or "eventid" in low) and (value is not None):
                    for event_id in target_ids:
                        if value == event_id or value == str(event_id):
                            refs[event_id].add(label)
                if not isinstance(value, str):
                    continue
                if not any(token in low for token in (
                    "json", "event", "payload", "metadata", "reply", "route",
                    "proof", "binding", "evidence", "reference", "refs",
                )):
                    continue
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError):
                    continue
                for event_id in target_ids:
                    if _json_references_event_id(decoded, event_id):
                        refs[event_id].add(label)
    return {event_id: sorted(columns) for event_id, columns in refs.items()}


def _event_references(conn: sqlite3.Connection, event_id: int) -> list[str]:
    return _event_references_many(conn, [event_id]).get(int(event_id), [])


def _manifest_action_state(conn: sqlite3.Connection, action: Mapping[str, Any]) -> str:
    row = conn.execute(
        "SELECT event_id,stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity "
        "FROM session_event_tail WHERE event_id=?", (action["event_id"],),
    ).fetchone()
    if row is None:
        identity_rows = conn.execute(
            "SELECT event_id FROM session_event_tail WHERE stream_id=? AND session_created_at=? AND identity=?",
            (action["stream_id"], action["session_created_at"], action["identity"]),
        ).fetchall()
        if action["operation"] == "delete" and not identity_rows:
            return "applied"
        return "missing_or_rebound"
    row = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(_EVENT_COLUMNS, row))
    if (row["stream_id"] != action["stream_id"]
            or row["session_created_at"] != action["session_created_at"]
            or row["identity"] != action["identity"]
            or row["event_ts"] != action.get("event_ts")
            or row["recorded_at"] != action.get("recorded_at")):
        return "identity_mismatch"
    digest = _sha256_bytes(str(row["event_json"]).encode("utf-8"))
    if action["operation"] == "replace":
        if (digest == action["old_event_json_sha256"] and row["event_key"] == action["old_event_key"]):
            return "before"
        if (digest == action["new_event_json_sha256"] and row["event_key"] == action["new_event_key"]):
            return "applied"
        return "content_mismatch"
    if digest == action["old_event_json_sha256"] and row["event_key"] == action["old_event_key"]:
        return "before"
    return "content_mismatch"


def _validate_action_manifest(action: Mapping[str, Any]) -> None:
    row_class = action.get("class")
    operation = action.get("operation")
    if row_class not in _ALLOWED_CLASSES:
        raise RepairRefused("manifest contains unsupported row class")
    expected_operation = "replace" if row_class == "queued_notice" else "delete"
    if operation != expected_operation:
        raise RepairRefused("manifest operation does not match fixed row class")
    _require_sha(action.get("source_sha256"), "manifest source record SHA")
    _require_sha(action.get("old_event_json_sha256"), "manifest old event JSON SHA")
    _require_sha(action.get("old_event_key"), "manifest old event_key")
    if "event_ts" not in action or "recorded_at" not in action:
        raise RepairRefused("manifest is missing stored timestamp CAS values")
    if action["old_event_json_sha256"] != action["old_event_key"]:
        raise RepairRefused("manifest preimage event_key/hash mismatch")
    old_json = action.get("old_event_json")
    if not isinstance(old_json, str) or _sha256_bytes(old_json.encode("utf-8")) != action["old_event_json_sha256"]:
        raise RepairRefused("manifest exact old event JSON preimage digest mismatch")
    try:
        decoded_old_json = json.loads(old_json)
    except ValueError as exc:
        raise RepairRefused("manifest exact old event JSON preimage is invalid") from exc
    if decoded_old_json != action.get("old_event"):
        raise RepairRefused("manifest old event payload differs from exact JSON preimage")
    if operation == "replace":
        _require_sha(action.get("new_event_json_sha256"), "manifest replacement JSON SHA")
        _require_sha(action.get("new_event_key"), "manifest replacement event_key")
        if action["new_event_json_sha256"] != action["new_event_key"]:
            raise RepairRefused("manifest replacement event_key/hash mismatch")
        new_event = action.get("new_event")
        if not isinstance(new_event, dict) or event_key(new_event) != action["new_event_key"]:
            raise RepairRefused("manifest replacement payload digest mismatch")


def freeze_manifest(
    connection: sqlite3.Connection,
    source_records: Sequence[dict[str, Any]],
    *,
    scope_packet: Mapping[str, Any],
    expected_scope_sha256: str,
    source_record_sha256_by_uuid: Mapping[str, str],
    source_jsonl_sha256: str,
    database_snapshot_sha256: str,
    normalizer: Callable[..., list[dict[str, Any]]] = normalize_claude_jsonl_record,
    annotator: Callable[[dict[str, Any]], dict[str, Any]] = annotate_message_envelope,
) -> dict[str, Any]:
    """Freeze only scope-packet-selected rows from raw source record dictionaries."""
    source_sha = _require_sha(source_jsonl_sha256, "source JSONL SHA")
    database_sha = _require_sha(database_snapshot_sha256, "database snapshot SHA")
    scope = _mapping(scope_packet, "scope packet")
    target, selected = _validate_scope_packet(
        scope,
        expected_scope_sha256=expected_scope_sha256,
        source_jsonl_sha256=source_sha,
        database_snapshot_sha256=database_sha,
    )
    created_at, generation = _read_session_binding(connection, target)
    raw_by_uuid = _source_record_by_uuid(source_records)
    digest_by_uuid = dict(source_record_sha256_by_uuid)
    expected_uuids = {item["source_uuid"] for item in selected}
    if set(raw_by_uuid) != expected_uuids or set(digest_by_uuid) != expected_uuids:
        raise RepairRefused("source record set differs from frozen scope packet")
    expected_database_rows: dict[str, dict[str, Any]] | None = None
    if "database_rows" in scope:
        packet_rows = scope.get("database_rows")
        if not isinstance(packet_rows, list):
            raise RepairRefused("scope database row evidence must be a list")
        expected_database_rows = {}
        for packet_row in packet_rows:
            evidence = _mapping(packet_row, "scope database row evidence")
            source_uuid = evidence.get("source_uuid")
            if source_uuid not in expected_uuids or source_uuid in expected_database_rows:
                raise RepairRefused("scope database row evidence has an unexpected or duplicate UUID")
            if evidence.get("status") not in {"matched", "missing"}:
                raise RepairRefused("scope database row evidence status is invalid")
            expected_database_rows[str(source_uuid)] = evidence
        if set(expected_database_rows) != expected_uuids:
            raise RepairRefused("scope database row evidence does not cover selected source UUIDs")

    actions: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    for item in selected:
        source_uuid = item["source_uuid"]
        record = raw_by_uuid[source_uuid]
        expected_record_sha = _require_sha(item["source_sha256"], "scope record SHA")
        actual_record_sha = _require_sha(digest_by_uuid[source_uuid], "source record SHA")
        if actual_record_sha != expected_record_sha:
            raise RepairRefused("source record digest mismatch")
        source_text = _verify_source_shape(item, record)
        rows = _rows_for_source_uuid(
            connection,
            stream_id=target["stream_id"],
            session_created_at=created_at,
            source_uuid=source_uuid,
        )
        packet_evidence = expected_database_rows.get(source_uuid) if expected_database_rows is not None else None
        if packet_evidence is not None:
            if packet_evidence["status"] == "missing" and rows:
                raise RepairRefused("database row census changed after scope packet freeze")
            if packet_evidence["status"] == "matched":
                if len(rows) != 1:
                    raise RepairRefused("database row evidence changed after scope packet freeze")
                actual_evidence = _row_evidence(rows[0])
                if any(packet_evidence.get(key) != value for key, value in actual_evidence.items()):
                    raise RepairRefused("database row evidence changed after scope packet freeze")
        if not rows:
            unverified.append({
                "class": item["class"], "source_uuid": source_uuid,
                "source_sha256": actual_record_sha, "reason": "matching database row missing",
            })
            continue
        if len(rows) != 1:
            raise RepairRefused("source UUID has multiple database rows in target generation")
        old_row = rows[0]
        old_event = _validate_existing_row(
            old_row,
            target=target,
            source_uuid=source_uuid,
            source_text=source_text,
            row_class=item["class"],
            source_record=record,
        )
        old_json = str(old_row["event_json"])
        old_digest = _sha256_bytes(old_json.encode("utf-8"))
        base_action: dict[str, Any] = {
            "class": item["class"],
            "operation": "replace" if item["class"] == "queued_notice" else "delete",
            "source_uuid": source_uuid,
            "source_line_number": int(item["source_line_number"]),
            "source_sha256": actual_record_sha,
            "jsonl_event_index": (old_event.get("raw") or {}).get("jsonl_event_index"),
            "stream_id": target["stream_id"],
            "session_created_at": created_at,
            "session_generation": generation,
            "event_id": int(old_row["event_id"]),
            "identity": str(old_row["identity"]),
            "event_ts": old_row["event_ts"],
            "recorded_at": old_row["recorded_at"],
            "old_event_key": str(old_row["event_key"]),
            "old_event_json_sha256": old_digest,
            "old_event_json": old_json,
            "old_event": old_event,
        }
        if item["class"] == "queued_notice":
            normalized = normalizer(
                record, host=target["host"], session_name=target["session_name"],
            )
            if not isinstance(normalized, list) or len(normalized) != 1 or not isinstance(normalized[0], dict):
                raise RepairRefused("queued source did not normalize to exactly one event")
            annotated = annotator(normalized[0])
            if not isinstance(annotated, dict):
                raise RepairRefused("message envelope annotator returned an invalid event")
            old_raw = old_event.get("raw") if isinstance(old_event.get("raw"), dict) else {}
            normalized_raw = normalized[0].get("raw") if isinstance(normalized[0].get("raw"), dict) else {}
            annotated_raw = annotated.get("raw") if isinstance(annotated.get("raw"), dict) else {}
            if (
                old_event.get("host") != annotated.get("host")
                or old_event.get("session_name") != annotated.get("session_name")
                or old_event.get("session_id") != annotated.get("session_id")
                or old_event.get("stream_id") != annotated.get("stream_id")
                or old_event.get("provider") != annotated.get("provider")
                or old_event.get("kind") != annotated.get("kind")
            ):
                raise RepairRefused("queued source top-level identity differs from stored event")
            for identity_key in (
                "provider", "uuid", "jsonl_record_uuid", "jsonl_event_index", "parent_uuid",
                "subtype", "queued_at", "host", "session_name", "source_session_identity",
            ):
                if old_raw.get(identity_key) != normalized_raw.get(identity_key):
                    raise RepairRefused(f"queued source identity mismatch: {identity_key}")
            source_timestamp = str(record.get("timestamp") or "")
            if (old_raw.get("source_session_identity")
                    != str(record.get("sessionId") or record.get("session_id") or "")
                    or old_raw.get("parent_uuid") != record.get("parentUuid")):
                raise RepairRefused("queued source session/parent identity mismatch")
            if (old_raw.get("queued_at") != source_timestamp
                    or normalized[0].get("timestamp") != source_timestamp
                    or old_event.get("timestamp") != _expected_queued_timestamp(
                        connection, row=old_row, source_timestamp=source_timestamp,
                    )):
                raise RepairRefused("queued source timestamp does not match stored monotonic adjustment")
            replacement = copy.deepcopy(old_event)
            replacement["text"] = annotated.get("text")
            if "provider_wrapper" in annotated:
                replacement["provider_wrapper"] = copy.deepcopy(annotated["provider_wrapper"])
            else:
                replacement.pop("provider_wrapper", None)
            replacement_raw = replacement.get("raw")
            if not isinstance(replacement_raw, dict):
                replacement_raw = {}
                replacement["raw"] = replacement_raw
            for key in ("provider_content", "envelope_source"):
                if key in annotated_raw:
                    replacement_raw[key] = copy.deepcopy(annotated_raw[key])
                else:
                    replacement_raw.pop(key, None)
            if "message_envelope" in annotated:
                replacement["message_envelope"] = copy.deepcopy(annotated["message_envelope"])
            else:
                replacement.pop("message_envelope", None)
            _check_stable_identity(old_event, replacement, str(old_row["identity"]))
            if (replacement.get("kind") != "USER"
                    or (replacement.get("raw") or {}).get("subtype") != "queued-command"
                    or (replacement.get("raw") or {}).get("provider_content") != source_text):
                raise RepairRefused("queued notice normalized with changed provider or operator binding")
            binding = _notice_binding_snapshot(
                connection,
                target=target,
                event_id=int(old_row["event_id"]),
                source_attachment_uuid=str(item["attachment_source_uuid"]),
                event=replacement,
            )
            replacement_json = event_json_bytes(replacement).decode("utf-8")
            replacement_digest = _sha256_bytes(replacement_json.encode("utf-8"))
            base_action.update({
                "source_attachment_uuid": item["attachment_source_uuid"],
                "binding": binding,
                "new_event": replacement,
                "new_event_key": replacement_digest,
                "new_event_json_sha256": replacement_digest,
            })
        else:
            normalized = normalizer(
                record, host=target["host"], session_name=target["session_name"],
            )
            if normalized:
                raise RepairRefused("image metadata source still emits chat events")
            if old_event.get("timestamp") != str(record.get("timestamp") or ""):
                raise RepairRefused("image metadata source timestamp differs from stored event")
        actions.append(base_action)

    actions.sort(key=lambda action: (action["event_id"], action["source_uuid"]))
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "scope_packet_sha256": _require_sha(expected_scope_sha256, "scope packet SHA"),
        "source": {
            "jsonl_sha256": source_sha,
            "record_count": len(selected),
            "record_sha256_by_uuid": {
                item["source_uuid"]: digest_by_uuid[item["source_uuid"]]
                for item in selected
            },
        },
        "database_snapshot_sha256": database_sha,
        "actions": actions,
        "unverified": unverified,
    }
    if not actions:
        raise RepairRefused("scope packet has no source-backed database rows to repair")
    return manifest


def _validate_manifest(
    manifest: Mapping[str, Any], *, expected_manifest_sha256: str,
    source_jsonl_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    saved = _mapping(manifest, "repair manifest")
    if saved.get("schema_version") != SCHEMA_VERSION:
        raise RepairRefused("unsupported repair manifest schema")
    expected = _require_sha(expected_manifest_sha256, "expected manifest SHA")
    actual = manifest_sha256(saved)
    if actual != expected:
        raise RepairRefused("expected manifest SHA mismatch")
    source = _mapping(saved.get("source"), "manifest source")
    if _require_sha(source.get("jsonl_sha256"), "manifest source JSONL SHA") != source_jsonl_sha256:
        raise RepairRefused("source JSONL digest mismatch")
    target = _mapping(saved.get("target"), "manifest target")
    actions = saved.get("actions")
    if not isinstance(actions, list) or not actions:
        raise RepairRefused("manifest has no actions")
    normalized_actions: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for raw_action in actions:
        action = _mapping(raw_action, "manifest action")
        _validate_action_manifest(action)
        event_id_value = action.get("event_id")
        if isinstance(event_id_value, bool) or not isinstance(event_id_value, int) or event_id_value <= 0:
            raise RepairRefused("manifest event ID is invalid")
        if event_id_value in seen_ids:
            raise RepairRefused("manifest event ID is duplicated")
        seen_ids.add(event_id_value)
        if (action.get("stream_id") != target.get("stream_id")
                or action.get("session_created_at") != target.get("session_created_at")
                or action.get("session_generation") != target.get("session_generation")):
            raise RepairRefused("manifest action generation differs from target")
        normalized_actions.append(action)
    return target, normalized_actions, actual


def _preimage_row(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT event_id,stream_id,session_created_at,event_key,event_json,event_ts,recorded_at,identity "
        "FROM session_event_tail WHERE event_id=?", (event_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, sqlite3.Row) else dict(zip(_EVENT_COLUMNS, row))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(value)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _load_preimages(path: Path, manifest_sha: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepairRefused("preimage file is unreadable") from exc
    if not isinstance(saved, dict) or saved.get("manifest_sha256") != manifest_sha:
        raise RepairRefused("preimage file is bound to a different manifest")
    if not isinstance(saved.get("rows"), list):
        raise RepairRefused("preimage file has no row snapshots")
    return saved


def _begin_savepoint(conn: sqlite3.Connection, name: str) -> bool:
    outer = conn.in_transaction
    if not outer:
        # Own the writer before guard reads establish a WAL snapshot. A deferred
        # snapshot cannot upgrade after another writer commits, even with the
        # existing busy timeout. Nested callers retain their transaction policy.
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"SAVEPOINT {name}")
    return outer


def _finish_savepoint(conn: sqlite3.Connection, name: str, outer: bool) -> None:
    conn.execute(f"RELEASE SAVEPOINT {name}")
    if not outer:
        conn.commit()


def _abort_savepoint(conn: sqlite3.Connection, name: str, outer: bool) -> None:
    try:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        if not outer:
            conn.rollback()
    except sqlite3.Error:
        conn.rollback()


def _assert_binding_unchanged(
    conn: sqlite3.Connection, target: Mapping[str, Any], action: Mapping[str, Any],
) -> None:
    event = action.get("new_event")
    if not isinstance(event, dict):
        raise RepairRefused("queued replacement payload is missing")
    current = _notice_binding_snapshot(
        conn,
        target=target,
        event_id=int(action["event_id"]),
        source_attachment_uuid=str(action["source_attachment_uuid"]),
        event=event,
    )
    if current != action.get("binding"):
        raise RepairRefused("operator send/attachment binding changed since freeze")


def _assert_live_guards(
    conn: sqlite3.Connection,
    target: Mapping[str, Any],
    actions: Sequence[Mapping[str, Any]],
    *,
    expected_state: str,
    check_deleted_references: bool = True,
) -> tuple[tuple[Any, ...], ...]:
    _read_session_binding(conn, target)
    states = [_manifest_action_state(conn, action) for action in actions]
    if any(state != expected_state for state in states):
        raise RepairRefused(f"CAS precondition failed: expected {expected_state} rows")
    for action in actions:
        if action["operation"] == "replace":
            _assert_binding_unchanged(conn, target, action)
    delete_ids = [int(action["event_id"]) for action in actions if action["operation"] == "delete"]
    if check_deleted_references and delete_ids:
        references = _event_references_many(conn, delete_ids)
        for event_id, columns in references.items():
            if columns:
                raise RepairRefused(
                    f"refusing metadata change with event references for {event_id}: " + ",".join(columns)
                )
    return _foreign_key_violations(conn)


def apply_manifest(
    connection: sqlite3.Connection,
    manifest: Mapping[str, Any],
    *,
    expected_manifest_sha256: str,
    source_jsonl_sha256: str,
    preimages_path: str | Path,
) -> dict[str, Any]:
    """CAS-apply a frozen manifest in one transaction; preimages stay external."""
    target, actions, manifest_sha = _validate_manifest(
        manifest,
        expected_manifest_sha256=expected_manifest_sha256,
        source_jsonl_sha256=source_jsonl_sha256,
    )
    path = Path(preimages_path).expanduser().resolve()
    saved_preimages = _load_preimages(path, manifest_sha)
    states = [_manifest_action_state(connection, action) for action in actions]
    required_state = "applied" if all(state == "applied" for state in states) else "before"
    if any(state not in {"before", "applied"} for state in states):
        raise RepairRefused("CAS precondition failed for one or more target rows")
    if required_state == "applied":
        if saved_preimages is None:
            raise RepairRefused("already-applied rows have no external rollback preimages")
        if saved_preimages.get("status") not in {"applied", "prepared"}:
            raise RepairRefused("preimage receipt is not applicable to an already-applied manifest")
    elif saved_preimages is not None and saved_preimages.get("status") in {"applied", "rolled_back"}:
        raise RepairRefused("manifest already has a terminal external preimage receipt")

    if required_state == "before" and saved_preimages is None:
        before_rows = [_preimage_row(connection, int(action["event_id"])) for action in actions]
        if any(row is None for row in before_rows):
            raise RepairRefused("a target row disappeared before preimage capture")
        saved_preimages = {
            "schema_version": SCHEMA_VERSION,
            "manifest_sha256": manifest_sha,
            "status": "prepared",
            "foreign_key_violations_before": [list(row) for row in _foreign_key_violations(connection)],
            "rows": before_rows,
        }
        if path.exists():
            raise RepairRefused("preimage destination already exists")
        _atomic_write_json(path, saved_preimages)
    elif required_state == "before" and saved_preimages.get("status") != "prepared":
        raise RepairRefused("preimage receipt is not in prepared state")

    outer = _begin_savepoint(connection, "history_repair_apply")
    try:
        before_fk = _assert_live_guards(
            connection, target, actions, expected_state=required_state,
        )
        if required_state == "applied":
            _finish_savepoint(connection, "history_repair_apply", outer)
            if saved_preimages is not None and saved_preimages.get("status") != "applied":
                saved_preimages["status"] = "applied"
                _atomic_write_json(path, saved_preimages)
            return {"status": "already_applied", "manifest_sha256": manifest_sha, "row_count": len(actions)}
        for action in actions:
            if action["operation"] == "replace":
                new_json = event_json_bytes(action["new_event"]).decode("utf-8")
                new_key = _sha256_bytes(new_json.encode("utf-8"))
                if new_key != action["new_event_key"]:
                    raise RepairRefused("replacement payload changed after manifest freeze")
                cursor = connection.execute(
                    """UPDATE session_event_tail SET event_key=?,event_json=?
                       WHERE event_id=? AND stream_id=? AND session_created_at=?
                         AND event_key=? AND event_json=? AND identity=?
                         AND event_ts IS ? AND recorded_at IS ?""",
                    (
                        new_key, new_json, action["event_id"],
                        action["stream_id"], action["session_created_at"],
                        action["old_event_key"], _canonical_old_json(action), action["identity"],
                        action["event_ts"], action["recorded_at"],
                    ),
                )
            else:
                cursor = connection.execute(
                    """DELETE FROM session_event_tail
                       WHERE event_id=? AND stream_id=? AND session_created_at=?
                         AND event_key=? AND event_json=? AND identity=?
                         AND event_ts IS ? AND recorded_at IS ?""",
                    (
                        action["event_id"], action["stream_id"], action["session_created_at"],
                        action["old_event_key"], _canonical_old_json(action), action["identity"],
                        action["event_ts"], action["recorded_at"],
                    ),
                )
            if cursor.rowcount != 1:
                raise RepairRefused("CAS write did not affect exactly one target row")
        after_fk = _foreign_key_violations(connection)
        if not set(after_fk).issubset(set(before_fk)):
            raise RepairRefused("history repair introduced a foreign-key violation")
        for action in actions:
            if _manifest_action_state(connection, action) != "applied":
                raise RepairRefused("post-apply event content/key invariant failed")
        _finish_savepoint(connection, "history_repair_apply", outer)
    except BaseException:
        _abort_savepoint(connection, "history_repair_apply", outer)
        raise
    saved_preimages["status"] = "applied"
    _atomic_write_json(path, saved_preimages)
    return {"status": "applied", "manifest_sha256": manifest_sha, "row_count": len(actions)}


def _canonical_old_json(action: Mapping[str, Any]) -> str:
    old_event = action.get("old_event")
    if not isinstance(old_event, dict):
        raise RepairRefused("manifest old event payload is missing")
    old_json = action.get("old_event_json")
    if not isinstance(old_json, str):
        raise RepairRefused("manifest exact old event JSON preimage is missing")
    if _sha256_bytes(old_json.encode("utf-8")) != action.get("old_event_json_sha256"):
        raise RepairRefused("manifest old event payload digest mismatch")
    try:
        if json.loads(old_json) != old_event:
            raise RepairRefused("manifest old event differs from exact stored JSON")
    except ValueError as exc:
        raise RepairRefused("manifest exact stored JSON preimage is invalid") from exc
    return old_json


def rollback_manifest(
    connection: sqlite3.Connection,
    manifest: Mapping[str, Any],
    *,
    expected_manifest_sha256: str,
    source_jsonl_sha256: str,
    preimages_path: str | Path,
) -> dict[str, Any]:
    """Restore exact event row preimages under CAS; never edits notice state."""
    target, actions, manifest_sha = _validate_manifest(
        manifest,
        expected_manifest_sha256=expected_manifest_sha256,
        source_jsonl_sha256=source_jsonl_sha256,
    )
    path = Path(preimages_path).expanduser().resolve()
    saved_preimages = _load_preimages(path, manifest_sha)
    if saved_preimages is None:
        raise RepairRefused("rollback preimages are missing")
    already_rolled_back = saved_preimages.get("status") == "rolled_back"
    if not already_rolled_back and saved_preimages.get("status") != "applied":
        raise RepairRefused("rollback requires an applied preimage receipt")
    rows = saved_preimages["rows"]
    if len(rows) != len(actions) or any(
        not isinstance(row, dict) or set(row) != set(_EVENT_COLUMNS) for row in rows
    ):
        raise RepairRefused("rollback preimage row set does not match manifest")
    for action, old_row in zip(actions, rows):
        if (old_row.get("event_id") != action["event_id"]
                or old_row.get("stream_id") != action["stream_id"]
                or old_row.get("session_created_at") != action["session_created_at"]
                or old_row.get("identity") != action["identity"]
                or old_row.get("event_key") != action["old_event_key"]
                or old_row.get("event_ts") != action["event_ts"]
                or old_row.get("recorded_at") != action["recorded_at"]
                or _sha256_bytes(str(old_row.get("event_json") or "").encode("utf-8"))
                != action["old_event_json_sha256"]):
            raise RepairRefused("rollback preimage differs from frozen row")
    outer = _begin_savepoint(connection, "history_repair_rollback")
    try:
        restored = already_rolled_back or all(
            _manifest_action_state(connection, action) == "before" for action in actions
        )
        before_fk = _assert_live_guards(
            connection, target, actions,
            expected_state="before" if restored else "applied",
            check_deleted_references=not restored,
        )
        if not restored:
            for action, old_row in zip(actions, rows):
                if action["operation"] == "replace":
                    new_json = event_json_bytes(action["new_event"]).decode("utf-8")
                    new_key = _sha256_bytes(new_json.encode("utf-8"))
                    if new_key != action["new_event_key"]:
                        raise RepairRefused("replacement payload changed after manifest freeze")
                    cursor = connection.execute(
                        """UPDATE session_event_tail SET event_key=?,event_json=?
                           WHERE event_id=? AND stream_id=? AND session_created_at=?
                             AND event_key=? AND event_json=? AND identity=?
                             AND event_ts IS ? AND recorded_at IS ?""",
                        (
                            old_row["event_key"], old_row["event_json"], action["event_id"],
                            action["stream_id"], action["session_created_at"],
                            new_key, new_json, action["identity"],
                            action["event_ts"], action["recorded_at"],
                        ),
                    )
                else:
                    columns = tuple(old_row.keys())
                    quoted = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
                    marks = ",".join("?" for _ in columns)
                    cursor = connection.execute(
                        f"INSERT INTO session_event_tail({quoted}) VALUES({marks})",
                        tuple(old_row[column] for column in columns),
                    )
                if cursor.rowcount != 1:
                    raise RepairRefused("rollback did not restore exactly one target row")
            after_fk = _foreign_key_violations(connection)
            if not set(after_fk).issubset(set(before_fk)):
                raise RepairRefused("rollback introduced a foreign-key violation")
            for action in actions:
                if _manifest_action_state(connection, action) != "before":
                    raise RepairRefused("rollback row/key invariant failed")
        _finish_savepoint(connection, "history_repair_rollback", outer)
    except BaseException:
        _abort_savepoint(connection, "history_repair_rollback", outer)
        raise
    if not already_rolled_back:
        saved_preimages["status"] = "rolled_back"
        _atomic_write_json(path, saved_preimages)
    return {"status": "already_rolled_back" if restored else "rolled_back",
            "manifest_sha256": manifest_sha, "row_count": len(actions)}


def _sqlite_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _sqlite_readwrite(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise RepairRefused("database file does not exist")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, int], str]:
    whole = hashlib.sha256()
    records: list[dict[str, Any]] = []
    digests: dict[str, str] = {}
    line_numbers: dict[str, int] = {}
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            whole.update(raw_line)
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, ValueError) as exc:
                raise RepairRefused(f"source JSONL is invalid at line {line_number}") from exc
            if not isinstance(record, dict):
                continue
            source_uuid = record.get("uuid")
            if not isinstance(source_uuid, str) or not source_uuid:
                continue
            if source_uuid in digests:
                raise RepairRefused("source JSONL contains duplicate record UUIDs")
            records.append(record)
            digests[source_uuid] = raw_jsonl_line_sha256(raw_line)
            line_numbers[source_uuid] = line_number
    return records, digests, line_numbers, whole.hexdigest()


def _row_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    event_json = str(row["event_json"])
    return {
        "event_id": int(row["event_id"]),
        "session_created_at": str(row["session_created_at"]),
        "identity": row["identity"],
        "event_key": str(row["event_key"]),
        "event_json_sha256": _sha256_bytes(event_json.encode("utf-8")),
        "kind": _event_for_row(row).get("kind"),
    }


def _scope_from_snapshot(
    conn: sqlite3.Connection, *, seed: Mapping[str, Any], source_path: Path,
    snapshot_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    target_seed = _mapping(seed.get("target"), "scope seed target")
    required = (
        "host", "session_name", "stream_id", "session_created_at", "session_generation",
    )
    if any(not isinstance(target_seed.get(key), str) or not target_seed[key] for key in required):
        raise RepairRefused("scope seed target is incomplete")
    if target_seed["stream_id"] != f"{target_seed['host']}:{target_seed['session_name']}":
        raise RepairRefused("scope seed stream/session binding mismatch")
    session = conn.execute(
        "SELECT created_at FROM sessions WHERE host=? AND session_name=?",
        (target_seed["host"], target_seed["session_name"]),
    ).fetchone()
    generation = conn.execute(
        "SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?",
        (target_seed["host"], target_seed["session_name"]),
    ).fetchone()
    if session is None or generation is None:
        raise RepairRefused("scope seed target has no current generation")
    session_created_at = str(session["created_at"] if isinstance(session, sqlite3.Row) else session[0])
    session_generation = str(generation["generation"] if isinstance(generation, sqlite3.Row) else generation[0])
    if (target_seed["session_created_at"] != session_created_at
            or target_seed["session_generation"] != session_generation):
        raise RepairRefused("scope seed session generation changed")
    source_records, source_digests, line_numbers, source_sha = _read_jsonl(source_path)
    by_uuid = {record["uuid"]: record for record in source_records}
    queue_seed = _mapping(seed.get("queued_notice"), "scope seed queued notice")
    queue_uuid = queue_seed.get("source_uuid")
    attachment_uuid = queue_seed.get("attachment_source_uuid")
    if not isinstance(queue_uuid, str) or not queue_uuid or not isinstance(attachment_uuid, str) or not attachment_uuid:
        raise RepairRefused("scope seed queued notice identity is incomplete")
    queue_matches = [record for record in source_records if record.get("uuid") == queue_uuid]
    if len(queue_matches) != 1:
        raise RepairRefused("scope seed queued notice source is missing or duplicated")
    queued = queue_matches[0]
    queue_item = {
        "class": "queued_notice",
        "source_uuid": queue_uuid,
        "source_line_number": line_numbers[queue_uuid],
        "source_sha256": source_digests[queue_uuid],
        "attachment_source_uuid": attachment_uuid,
    }
    # Validate the known route before including its digest in the packet.
    _verify_source_shape(queue_item, queued)
    selected = [queue_item]
    for record in source_records:
        msg = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = msg.get("content")
        if (record.get("type") == "user" and record.get("isMeta") is True
                and record.get("turnCompanion") is True and isinstance(content, str)
                and _IMAGE_COORDINATE_META.fullmatch(content)):
            source_uuid = str(record["uuid"])
            selected.append({
                "class": "image_meta",
                "source_uuid": source_uuid,
                "source_line_number": line_numbers[source_uuid],
                "source_sha256": source_digests[source_uuid],
            })

    selected.sort(key=lambda item: (item["source_line_number"], item["source_uuid"]))
    target = {
        "host": target_seed["host"],
        "session_name": target_seed["session_name"],
        "stream_id": target_seed["stream_id"],
        "session_created_at": session_created_at,
        "session_generation": session_generation,
    }
    db_rows: list[dict[str, Any]] = []
    for item in selected:
        rows = _rows_for_source_uuid(
            conn,
            stream_id=target["stream_id"],
            session_created_at=session_created_at,
            source_uuid=item["source_uuid"],
        )
        if len(rows) > 1:
            raise RepairRefused("source UUID has multiple database rows in target generation")
        if not rows:
            db_rows.append({"source_uuid": item["source_uuid"], "status": "missing"})
            continue
        source_record = by_uuid[item["source_uuid"]]
        source_text = _verify_source_shape(item, source_record)
        _validate_existing_row(
            rows[0], target=target, source_uuid=item["source_uuid"],
            source_text=source_text, row_class=item["class"],
            source_record=source_record,
        )
        db_rows.append({
            "source_uuid": item["source_uuid"],
            "status": "matched",
            **_row_evidence(rows[0]),
        })
    scope = {
        "schema_version": SCHEMA_VERSION,
        "target": target,
        "source": {"jsonl_sha256": source_sha, "records": selected},
        "database_snapshot_sha256": snapshot_sha256,
        "database_rows": db_rows,
        "census": {
            "queued_source_rows": 1,
            "image_meta_source_rows": len(selected) - 1,
            "image_meta_database_rows": sum(
                1 for row in db_rows if row["status"] == "matched"
                and next(item for item in selected if item["source_uuid"] == row["source_uuid"])["class"] == "image_meta"
            ),
            "unmatched_database_rows": sum(1 for row in db_rows if row["status"] == "missing"),
        },
    }
    chosen_records = [by_uuid[item["source_uuid"]] for item in selected]
    selected_digests = {item["source_uuid"]: item["source_sha256"] for item in selected}
    return scope, chosen_records, selected_digests


def _backup_readonly(source_path: Path, backup_path: Path) -> str:
    if backup_path.exists():
        raise RepairRefused("database snapshot destination already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = _sqlite_readonly(source_path)
    destination = sqlite3.connect(backup_path, timeout=30)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    os.chmod(backup_path, 0o400)
    return _hash_file(backup_path)


def _require_external_path(path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    try:
        path.resolve().relative_to(repo_root)
    except ValueError:
        return
    raise RepairRefused("raw history artifacts must be written outside the repository")


def _load_json_file(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, ValueError) as exc:
        raise RepairRefused(f"could not read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise RepairRefused("JSON artifact root must be an object")
    return value, data


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    _require_external_path(path)
    if path.exists():
        raise RepairRefused("artifact destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))
    os.chmod(path, 0o400)


def _print_summary(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _cli_census(args: argparse.Namespace) -> None:
    seed_path = Path(args.seed).expanduser().resolve()
    seed, seed_bytes = _load_json_file(seed_path)
    seed_sha = _sha256_bytes(seed_bytes)
    if seed_sha != _require_sha(args.expected_seed_sha256, "expected seed SHA"):
        raise RepairRefused("scope seed SHA mismatch")
    snapshot_path = Path(args.snapshot_out).expanduser().resolve()
    scope_path = Path(args.scope_out).expanduser().resolve()
    _require_external_path(snapshot_path)
    _require_external_path(scope_path)
    snapshot_sha = _backup_readonly(Path(args.database).expanduser().resolve(), snapshot_path)
    source_path = Path(args.source_jsonl).expanduser().resolve()
    conn = _sqlite_readonly(snapshot_path)
    try:
        conn.row_factory = sqlite3.Row
        scope, _records, _digests = _scope_from_snapshot(
            conn, seed=seed, source_path=source_path, snapshot_sha256=snapshot_sha,
        )
    finally:
        conn.close()
    scope_data = canonical_json_bytes(scope)
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    if scope_path.exists():
        raise RepairRefused("scope packet destination already exists")
    scope_path.write_bytes(scope_data)
    os.chmod(scope_path, 0o400)
    _print_summary({
        "status": "censused_read_only",
        "scope_packet_sha256": _sha256_bytes(scope_data),
        "database_snapshot_sha256": snapshot_sha,
        "source_jsonl_sha256": scope["source"]["jsonl_sha256"],
        "source_record_count": len(scope["source"]["records"]),
        "database_row_count": sum(row["status"] == "matched" for row in scope["database_rows"]),
        "unverified_source_count": scope["census"]["unmatched_database_rows"],
    })


def _cli_freeze(args: argparse.Namespace) -> None:
    scope_path = Path(args.scope_packet).expanduser().resolve()
    scope, raw_scope_bytes = _load_json_file(scope_path)
    expected_scope = _require_sha(args.expected_scope_sha256, "expected scope SHA")
    actual_file_sha = _sha256_bytes(raw_scope_bytes)
    if actual_file_sha != expected_scope or scope_packet_sha256(scope) != expected_scope:
        raise RepairRefused("scope packet SHA mismatch")
    snapshot_path = Path(args.database_snapshot).expanduser().resolve()
    snapshot_sha = _hash_file(snapshot_path)
    source_path = Path(args.source_jsonl).expanduser().resolve()
    records, digests, line_numbers, source_sha = _read_jsonl(source_path)
    if source_sha != scope.get("source", {}).get("jsonl_sha256"):
        raise RepairRefused("source JSONL digest mismatch")
    selected_records = []
    selected_digests: dict[str, str] = {}
    for item in scope["source"]["records"]:
        source_uuid = item["source_uuid"]
        if source_uuid not in digests or line_numbers[source_uuid] != item["source_line_number"]:
            raise RepairRefused("source record location changed since census")
        selected_records.append(next(row for row in records if row.get("uuid") == source_uuid))
        selected_digests[source_uuid] = digests[source_uuid]
    conn = _sqlite_readonly(snapshot_path)
    try:
        conn.row_factory = sqlite3.Row
        manifest = freeze_manifest(
            conn,
            selected_records,
            scope_packet=scope,
            expected_scope_sha256=expected_scope,
            source_record_sha256_by_uuid=selected_digests,
            source_jsonl_sha256=source_sha,
            database_snapshot_sha256=snapshot_sha,
        )
    finally:
        conn.close()
    manifest_data = canonical_json_bytes(manifest)
    manifest_path = Path(args.manifest_out).expanduser().resolve()
    _require_external_path(manifest_path)
    if manifest_path.exists():
        raise RepairRefused("manifest destination already exists")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest_data)
    os.chmod(manifest_path, 0o400)
    _print_summary({
        "status": "frozen_read_only",
        "manifest_sha256": _sha256_bytes(manifest_data),
        "scope_packet_sha256": expected_scope,
        "action_count": len(manifest["actions"]),
        "action_counts": {
            op: sum(1 for action in manifest["actions"] if action["operation"] == op)
            for op in ("replace", "delete")
        },
        "unverified_source_count": len(manifest["unverified"]),
    })


def _cli_mutate(args: argparse.Namespace, *, rollback: bool) -> None:
    manifest_path = Path(args.manifest).expanduser().resolve()
    manifest, raw_manifest_bytes = _load_json_file(manifest_path)
    expected_manifest = _require_sha(args.expected_manifest_sha256, "expected manifest SHA")
    if _sha256_bytes(raw_manifest_bytes) != expected_manifest or manifest_sha256(manifest) != expected_manifest:
        raise RepairRefused("expected manifest SHA mismatch")
    source_path = Path(args.source_jsonl).expanduser().resolve()
    _records, _digests, _locations, source_sha = _read_jsonl(source_path)
    if source_sha != manifest.get("source", {}).get("jsonl_sha256"):
        raise RepairRefused("source JSONL digest mismatch")
    preimages_path = Path(args.preimages).expanduser().resolve()
    _require_external_path(preimages_path)
    connection = _sqlite_readwrite(Path(args.database).expanduser().resolve())
    connection.row_factory = sqlite3.Row
    try:
        if rollback:
            receipt = rollback_manifest(
                connection,
                manifest,
                expected_manifest_sha256=expected_manifest,
                source_jsonl_sha256=source_sha,
                preimages_path=preimages_path,
            )
        else:
            receipt = apply_manifest(
                connection,
                manifest,
                expected_manifest_sha256=expected_manifest,
                source_jsonl_sha256=source_sha,
                preimages_path=preimages_path,
            )
    finally:
        connection.close()
    _print_summary(receipt)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    census = subparsers.add_parser("census", help="read-only source/DB census and SQLite backup")
    census.add_argument("--database", required=True)
    census.add_argument("--source-jsonl", required=True)
    census.add_argument("--seed", required=True)
    census.add_argument("--expected-seed-sha256", required=True)
    census.add_argument("--snapshot-out", required=True)
    census.add_argument("--scope-out", required=True)
    freeze = subparsers.add_parser("freeze", help="freeze a hash-bound manifest from a read-only DB snapshot")
    freeze.add_argument("--database-snapshot", required=True)
    freeze.add_argument("--source-jsonl", required=True)
    freeze.add_argument("--scope-packet", required=True)
    freeze.add_argument("--expected-scope-sha256", required=True)
    freeze.add_argument("--manifest-out", required=True)
    for name in ("apply", "rollback"):
        command = subparsers.add_parser(name, help=f"CAS {name} a previously frozen manifest")
        command.add_argument("--database", required=True)
        command.add_argument("--source-jsonl", required=True)
        command.add_argument("--manifest", required=True)
        command.add_argument("--expected-manifest-sha256", required=True)
        command.add_argument("--preimages", required=True)
        command.add_argument("--writer-quiescent", action="store_true", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "census":
            _cli_census(args)
        elif args.command == "freeze":
            _cli_freeze(args)
        else:
            _cli_mutate(args, rollback=args.command == "rollback")
    except (RepairRefused, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
