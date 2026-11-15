"""Validation for deliberate proactive publications, within the write transaction."""
import json
import re
import time
import unicodedata

NOTE_TYPES = {"result", "problem", "reminder", "relay", "started", "change", "other"}
NOTE_FIELDS = ("note_type", "ref", "note_reason")
REPEAT_WINDOW_S = 24 * 3600
# Range reads pinned to idx_v2_assistant_publications_window: bounded by the
# window, never by the composite's total history (the dispatch index would be).
REPEAT_GUARD_SQL = ("SELECT event_id,canonical_payload_json FROM v2_assistant_composite_publications "
                    "INDEXED BY idx_v2_assistant_publications_window "
                    "WHERE stream_id=? AND created_at>? AND dispatch_id=''")
OTHER_NOTES_SQL = ("SELECT created_at,event_id,json_extract(canonical_payload_json,'$.ref'),"
                   "json_extract(canonical_payload_json,'$.note_reason') FROM v2_assistant_composite_publications "
                   "INDEXED BY idx_v2_assistant_publications_window "
                   "WHERE stream_id=? AND created_at>? AND dispatch_id='' "
                   "AND json_extract(canonical_payload_json,'$.note_type')='other' ORDER BY created_at DESC")


def normalize(text):
    return " ".join(re.sub(r"[*_~]", "", unicodedata.normalize("NFKC", text).lower()).split())


def window_start(seconds, now=None):
    """The publications.created_at stamp (sub-second ISO) `seconds` before now."""
    cut = (time.time() if now is None else now) - seconds
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cut))
    return f"{whole}.{int(cut * 1_000_000) % 1_000_000:06d}Z"


def recent_proactive_notes(conn, stream_id, now=None):
    return conn.execute(REPEAT_GUARD_SQL, (stream_id, window_start(REPEAT_WINDOW_S, now))).fetchall()


def validate(conn, stream_id, payload):
    note_type = payload.get("note_type")
    if not isinstance(note_type, str) or note_type not in NOTE_TYPES:
        raise ValueError("assistant_publish_note_type_required")
    reason = payload.get("note_reason")
    if (note_type == "other") != ("note_reason" in payload) or note_type == "other" and (
            not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200):
        raise ValueError("assistant_publish_reason_required")
    ref = payload.get("ref")
    if not isinstance(ref, str) or not re.fullmatch(r"(?:message|lane|report|wake|tell):\S+", ref):
        raise ValueError("assistant_publish_ref_required")
    kind, record_id = ref.split(":", 1)
    if (note_type == "reminder" and kind not in {"wake", "message"}
            or note_type == "relay" and kind not in {"tell", "message"}
            or note_type == "started" and kind not in {"tell", "message", "lane"}):
        raise ValueError("assistant_publish_ref_kind_not_allowed")
    if kind == "message":
        from retention import history_query
        found = history_query(conn, "SELECT 1 FROM v2_assistant_composite_routes WHERE stream_id=? "
                              "AND (dispatch_id=? OR input_identity=?) LIMIT 1",
                              (stream_id, record_id, record_id)).fetchone() is not None
    else:
        found = None
    queries = {
        "lane": (("SELECT 1 FROM v2_assistant_composite_lanes WHERE lane_id=? AND work_state IS NOT NULL", (record_id,)),),
        "report": (("SELECT 1 FROM v2_reports WHERE report_id=?", (record_id,)),
                   ("SELECT 1 FROM v2_assistant_composite_terminal_reports WHERE report_id=?", (record_id,))),
        "wake": (("SELECT 1 FROM v2_watch_wake WHERE id=?", (record_id,)),
                 ("SELECT 1 FROM v2_schedules WHERE schedule_id=?", (record_id,)),
                 ("SELECT 1 FROM v2_recurring_schedules WHERE recurring_id=?", (record_id,))),
        "tell": (("SELECT 1 FROM v2_tell_deliveries WHERE tell_id=?", (record_id,)),),
    }
    if found is None:
        found = any(conn.execute(query, args).fetchone() for query, args in queries[kind])
    if not found:
        raise ValueError("assistant_publish_ref_not_found")
    if note_type in {"started", "change"}:
        for row in recent_proactive_notes(conn, stream_id):
            prior = json.loads(row["canonical_payload_json"])
            if prior.get("note_type") == note_type and prior.get("ref") == ref:
                raise ValueError(f"assistant_publish_repeat:{row['event_id']}")
    target = normalize(payload.get("message") or "")
    rows = conn.execute(
        "SELECT p.event_id,e.event_json FROM v2_assistant_composite_publications p "
        "JOIN session_event_tail e ON e.event_id=p.event_id "
        "WHERE p.stream_id=? AND e.recorded_at>=? ORDER BY p.event_id DESC",
        (stream_id, time.time() - 3600))
    for row in rows:
        if normalize(str(json.loads(row["event_json"]).get("text") or "")) == target:
            raise ValueError(f"assistant_publish_duplicate_text:{row['event_id']}")
