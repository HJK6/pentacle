"""Read-only, call-local proofs that a retained replay prefix has no writes.

Unknown proof inputs use the ordinary ingestion path. This module never creates
segments, stamps receipts, repairs proofs, or retains a dependency cache.
"""
from __future__ import annotations

import sqlite3
import time

from retention import history_query


MAX_EVENTS = 128
MAX_RECEIPTS = 4096
MAX_SECONDS = 0.25


def stored_prefix(conn, events, lifecycle, *, prepared=False, identity=None):
    from ingest import _identity_key
    from tell_proof import delivery_wire
    from message_envelopes import parse_peer_delivery

    if not events or not isinstance(lifecycle, dict):
        return 0
    sid = str(events[0].get("stream_id") or "")
    host, sep, name = sid.partition(":")
    if not sep or not all(lifecycle.get(k) for k in ("pane_pid", "session_created_at", "generation")):
        return 0
    deadline = time.monotonic() + MAX_SECONDS
    skipped = 0
    receipts = None
    conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    try:
        conn.execute("SAVEPOINT replay_noop")
        live = conn.execute(
            "SELECT 1 FROM sessions s JOIN v2_session_generations g "
            "ON s.host=g.host AND s.session_name=g.session_name "
            "WHERE s.host=? AND s.session_name=? AND s.status='open' "
            "AND CAST(s.pane_pid AS TEXT)=? AND s.created_at=? AND g.generation=?",
            (host, name, lifecycle["pane_pid"], lifecycle["session_created_at"], lifecycle["generation"]),
        ).fetchone()
        if live is None:
            return 0
        for event in events[:MAX_EVENTS]:
            if time.monotonic() >= deadline or event.get("stream_id") != sid:
                break
            raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
            if event.get("kind") in {"USER", "TELL"} and not prepared:
                break  # Always run the original input split, even on full replay.
            # A sufficient duplicate proof under the insert's identity UNIQUE
            # key. A key-only duplicate or legacy-segment case can fall back;
            # absence here is never permission to drop an event.
            existing = conn.execute(
                "SELECT 1 FROM session_event_tail WHERE stream_id=? "
                "AND session_created_at=? AND identity=?",
                (sid, lifecycle["session_created_at"], identity or _identity_key(event)),
            ).fetchone()
            if existing is None:
                break
            wire = delivery_wire(event)
            if receipts is None:
                # The full historical reader is essential: archived send
                # attempts are still eligible for receipt stamping/repair.
                rows = history_query(conn,
                    "SELECT receipt_id, "
                    "json_extract(meta_json,'$._input_proof.wire_text') AS wire, "
                    "json_extract(meta_json,'$._input_proof.proof_event_id') AS bound, "
                    "(SELECT count(*) FROM json_each(meta_json) WHERE key='_input_proof') AS roots, "
                    "(SELECT count(*) FROM json_each(meta_json,'$._input_proof') "
                    " WHERE key='wire_text') AS wires, "
                    "(SELECT count(*) FROM json_each(meta_json,'$._input_proof') "
                    " WHERE key='proof_event_id') AS bounds "
                    "FROM v2_send_receipts WHERE to_stream_id=? "
                    "AND json_type(meta_json,'$._input_proof')='object' LIMIT ?",
                    (sid, MAX_RECEIPTS + 1), receipt_stream=sid).fetchall()
                if len(rows) > MAX_RECEIPTS:
                    break
                attempts = {}
                for row in rows:
                    # SQL and the original Python decoder must agree. Duplicate
                    # object keys or changed wires on one receipt are uncertain.
                    if (row['roots'] != 1 or row['wires'] != 1 or row['bounds'] > 1
                            or not isinstance(row['wire'], str) or "\x00" in row['wire']):
                        return skipped
                    prior = attempts.get(row['receipt_id'])
                    if prior is not None and prior[0] != row['wire']:
                        return skipped
                    attempts[row['receipt_id']] = (row['wire'], row['bound'] is not None
                                                   or (prior is not None and prior[1]))
                receipts = [text for text, bound in attempts.values() if not bound]
            # A later bound row settles this receipt id. Keeping an earlier
            # append-only unbound row in this set would redo already-bound work.
            if isinstance(wire, str) and any(candidate in wire for candidate in receipts):
                break
            if event.get("kind") in {"USER", "TELL"}:
                peer = parse_peer_delivery(wire) if isinstance(wire, str) else None
                tell_id = raw.get("tell_id") or (peer or {}).get("id")
                if tell_id or (isinstance(wire, str) and wire.startswith("[pentacle-notice:")):
                    break  # Tell promotion and its final-proof revalidation stay original.
            skipped += 1
            # Routing observations may write and initiate a notification. Do
            # not carry a later no-op decision across that effect boundary.
            if event.get("provider") == "claude" and event.get("kind") in {"ASSIST", "ASSIST_TEXT"}:
                break
        return skipped
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return 0
    finally:
        conn.set_progress_handler(None, 0)
        # No writes are made here; RELEASE preserves a caller's transaction.
        try:
            conn.execute("RELEASE replay_noop")
        except sqlite3.Error:
            pass  # SQLITE_INTERRUPT can already have rolled back the savepoint.
