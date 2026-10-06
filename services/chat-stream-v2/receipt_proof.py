"""Evidence-only receipt reconciliation; private preimages never leave verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

HOT_ROW_LIMIT = 60_000
ARCHIVE_USER_LIMIT = 5_000
ARCHIVE_SECONDS = 3.0
PENDING_DELIVERIES = frozenset({"accepted", "committed_pending_proof", "proof_pending", "proof_unavailable"})
IDENTITY_FIELDS = ("to_stream_id", "request_id", "receipt_id", "wire_digest", "display_text",
                   "content_kind", "attachments_json", "optimistic_id", "from_stream_id",
                   "actor_stream_id", "actor_trusted", "meta_json")


def verified_original_generation(value, target, request_id):
    if type(value) is not dict or set(value) != {"logical_id", "target", "generation", "sequence"}:
        return None
    logical, committed, generation, sequence = (value[k] for k in
                                               ("logical_id", "target", "generation", "sequence"))
    if (type(logical) is not str or not 1 <= len(logical) <= 256 or
            type(committed) is not str or committed != target or
            type(generation) is not str or not 1 <= len(generation) <= 128 or
            type(sequence) is not int or not 1 <= sequence <= 1000):
        return None
    try:
        expected = "dot-email-" + hashlib.sha256(
            (logical + committed + generation + str(sequence)).encode()).hexdigest()[:40]
        return generation if hmac.compare_digest(expected, request_id) else None
    except (ValueError, TypeError, UnicodeError):
        return None


def epoch(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def immutable_identity(row):
    return tuple(row[k] for k in IDENTITY_FIELDS)


def pending(row):
    try:
        return (row["state"] == "accepted" and row["delivery"] in PENDING_DELIVERIES
                and row["reason"] != "front_desk_held" and row["content_kind"] == "text"
                and json.loads(row["attachments_json"]) == []
                and bool(row["wire_digest"]) and bool(row["display_text"]))
    except (ValueError, TypeError):
        return False


def proved_pending_precedence(rows):
    """Return prior proved row only across identical pending-only history."""
    proofs = [i for i, row in enumerate(rows) if row["state"] == "landed"
              and row["reason"] == "late_user_proof" and row["submission_confirmed"]]
    if not proofs:
        return None, False
    i = proofs[-1]
    proof = rows[i]
    followers = rows[i + 1:]
    if followers and all(pending(r) and immutable_identity(r) == immutable_identity(proof)
                         for r in followers):
        return proof, False
    return None, bool(followers)


def scan_archive(path: Path, target: str, birth: str, claim: float):
    if not path.is_file():
        return [], True
    deadline = time.monotonic() + ARCHIVE_SECONDS
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=ARCHIVE_SECONDS)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            rows = []
            cursor = conn.execute(
                "SELECT * FROM session_event_tail WHERE stream_id=? AND session_created_at=? "
                "AND recorded_at>=? AND json_extract(event_json,'$.kind')='USER'",
                (target, birth, claim))
            for row in cursor:
                if len(rows) >= ARCHIVE_USER_LIMIT or time.monotonic() >= deadline:
                    return [], False
                rows.append({**dict(row), "proof_store": "archive"})
            return rows, True
    except (sqlite3.Error, OSError, ValueError):
        return [], False


def decide(rows, receipt, birth, claim, digest):
    groups = {}
    for row in rows:
        try:
            event = json.loads(row["event_json"])
            if event.get("kind") != "USER" or float(row["recorded_at"]) < claim:
                continue
            key = row.get("identity") or (row.get("proof_store"), row["event_id"])
            groups.setdefault(key, []).append((row, event))
        except (ValueError, TypeError, KeyError):
            return False, "lookup_incomplete"
    matches = 0
    for copies in groups.values():
        signatures, candidate = [], False
        for row, event in copies:
            text = event.get("text", "")
            when = epoch(event.get("timestamp") or row.get("event_ts"))
            if type(text) is not str:
                return False, "lookup_incomplete"
            wire = digest(text)
            match = text == receipt["display_text"] or wire == receipt["wire_digest"]
            candidate |= match
            signatures.append((wire, when, row["session_created_at"], event.get("stream_id"),
                               event.get("request_id"), event.get("receipt_id")))
        if not candidate:
            continue
        if len(set(signatures)) != 1:
            return False, "conflicting_copies"
        _, when, lifecycle, target, request_id, receipt_id = signatures[0]
        if lifecycle != birth or target != receipt["to_stream_id"]:
            return False, "lifecycle_unproven"
        if when is None or int(when) < claim:
            return False, "event_time_unproven"
        if ((request_id and request_id != receipt["request_id"]) or
                (receipt_id and receipt_id != receipt["receipt_id"])):
            return False, "stamped_other_request"
        matches += 1
    return (matches == 1, "proved" if matches == 1 else
            "ambiguous_identities" if matches > 1 else "no_proof")
