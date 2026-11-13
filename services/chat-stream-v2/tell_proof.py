"""Evidence-only reconciliation of an ordinary tell's original native input."""
from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
from bisect import bisect_right

from submission_events import EventProof, provider_text_digest
from retention import history_query, RECEIPT_PROOF_PREDICATE, RECEIPT_PROOF_WIRE
from message_envelopes import strip_peer_delivery


def _lookup_string(value):
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite_constant(value):
    raise ValueError("nonstandard JSON constant")


class TellDeliveryParseCache:
    """Connection-scoped memo of v2_tell_deliveries reply parses.

    Each `ProjectionDeliveries` rebuild can reuse unchanged strict parses. Keyed
    tell_id -> [reply_text, parsed, text_digest]: a changed reply (text
    differs) or a new tell_id misses and reparses with the identical strict
    hooks (dropping the digest with it), and tell_ids absent from a completed
    scan are pruned. A failed strict parse is never stored, so it keeps raising
    exactly as the uncached path does. Parsed payloads are consumed read-only by
    the delivery/origin readers, so the pristine parse is shared without a
    copy. `text_digest` is the per-ROW `provider_text_digest(delivery.text)`,
    filled on first use after the row passed validation and valid only for the
    reply it was computed from; it is distinct from the per-CALL input digests
    `retained_proof` computes. Store-owned connection state, never touched from
    the asyncio thread; a fresh connection gets a fresh cache.
    """
    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, list] = {}

    def decode(self, tell_id: str, reply_text: str):
        entry = self._entries.get(tell_id)
        if entry is None or entry[0] != reply_text:
            parsed = json.loads(reply_text, object_pairs_hook=_unique_object,
                                parse_constant=_nonfinite_constant)
            entry = [reply_text, parsed, None]
            self._entries[tell_id] = entry
        return entry[1]

    def text_digest(self, tell_id: str, text):
        """Row digest for the reply `decode` last returned for `tell_id`."""
        entry = self._entries[tell_id]
        if entry[2] is None:
            entry[2] = provider_text_digest(text)
        return entry[2]

    def prune(self, live_tell_ids) -> None:
        # A completed scan leaves `_entries` a superset of the live ids, so an
        # unequal count is exactly a stale (since-deleted) tell_id.
        if len(self._entries) != len(live_tell_ids):
            live = set(live_tell_ids)
            self._entries = {tell: entry for tell, entry in self._entries.items() if tell in live}


# Reuse bounds: a stream larger than these is read per call exactly as before.
_TAIL_REUSE_MAX_ROWS = 4096
_TAIL_REUSE_MAX_BYTES = 8 * 1024 * 1024

_UNPARSED, _UNPARSEABLE = object(), object()


class _TailEvent:
    """One retained tail row; the parse is memoised for the life of a window.

    `retained_proof` only reads the parsed event, so sharing it between
    candidates is safe; an unparseable row stays unparseable (the failure is
    memoised and re-raised as the ValueError the loop already skips).
    """
    __slots__ = ("event_id", "event_json", "_event")

    def __init__(self, event_id, event_json):
        self.event_id = event_id
        self.event_json = event_json
        self._event = _UNPARSED

    def event(self):
        cached = self._event
        if cached is _UNPARSEABLE:
            raise ValueError("unparseable tail event")
        if cached is _UNPARSED:
            try:
                cached = json.loads(self.event_json)
            except (TypeError, ValueError):
                self._event = _UNPARSEABLE
                raise
            self._event = cached
        return cached


class _TailWindow:
    """Every tail event of one (stream, lifecycle) after `floor`, ascending."""
    __slots__ = ("floor", "events", "ids", "size")

    def __init__(self, floor):
        self.floor = floor
        self.events = []
        self.ids = []
        self.size = 0


class ProjectionReuse:
    """Tail reads shared only within one projection call.

    The total_changes guard also drops windows after writes within a private
    projection. Store readers give each call a new instance, so neither another
    connection's writes nor archive changes can outlive a call in this memo.
    """

    def __init__(self):
        self._changes = None
        self._tail = {}

    def _fresh(self, conn):
        changes = conn.total_changes
        if changes != self._changes:
            self._tail = {}
            self._changes = changes

    def tail_after(self, conn, target, birth, watermark):
        """The `_TailEvent`s with event_id > `watermark`, ascending, or None.

        One window per (stream, lifecycle) holds every event after the lowest
        watermark asked for so far; a candidate that needs earlier events reads
        only the gap below the window and lowers its floor, so no row is read
        twice in a generation.
        """
        if not _lookup_string(target) or not _lookup_string(birth):
            return None
        self._fresh(conn)
        key = (target, birth)
        window = self._tail.get(key, _UNPARSED)
        if window is None:
            return None
        if window is _UNPARSED or watermark < window.floor:
            # The byte bound spans every window this generation holds.
            held = sum(other.size for name, other in self._tail.items() if other is not None and name != key)
            if window is _UNPARSED:
                window = self._tail[key] = self._read_tail(conn, key, _TailWindow(watermark), None, held)
            else:
                window = self._tail[key] = self._read_tail(conn, key, window, watermark, held)
        if window is None:
            return None
        return window.events[bisect_right(window.ids, watermark):]

    @staticmethod
    def _read_tail(conn, key, window, lower, held):
        """Extend `window` down to `lower` (or fill a new one); None past the bounds."""
        target, birth = key
        floor = window.floor if lower is None else lower
        sql = ("SELECT event_id,event_json FROM session_event_tail "
               "WHERE stream_id=? AND session_created_at=? AND event_id>?")
        params = [target, birth, floor]
        if lower is not None:
            sql += " AND event_id<=?"
            params.append(window.floor)
        try:
            rows = conn.execute(sql + " ORDER BY event_id LIMIT ?",
                                (*params, _TAIL_REUSE_MAX_ROWS + 1 - len(window.events))).fetchall()
        except sqlite3.Error:
            return None
        size = window.size + sum(len(row["event_json"] or "") for row in rows)
        if len(window.events) + len(rows) > _TAIL_REUSE_MAX_ROWS or held + size > _TAIL_REUSE_MAX_BYTES:
            return None
        window.events = [_TailEvent(row["event_id"], row["event_json"]) for row in rows] + window.events
        window.ids = [event.event_id for event in window.events]
        window.size = size
        window.floor = floor
        return window


class DeliveryLookup:
    """One delivery table for one Store connection, used only by its thread.

    TEMP triggers observe local writes, including writes later rolled back.
    main.data_version observes other connections' commits. The caller samples
    it inside its read savepoint and never shares an outer transaction's table.
    """

    def __init__(self, conn):
        self.table = None
        self.version = None
        self.dirty = True
        self.disabled = False
        opened = False
        try:
            conn.execute("SAVEPOINT delivery_lookup_install")
            opened = True
            conn.create_function("delivery_lookup_changed", 0, self._changed)
            for action in ("insert", "update", "delete"):
                conn.execute(
                    f"CREATE TEMP TRIGGER delivery_lookup_{action} AFTER {action.upper()} "
                    "ON main.v2_tell_deliveries BEGIN SELECT delivery_lookup_changed(); END")
            conn.execute("RELEASE delivery_lookup_install")
        except BaseException:
            if opened:
                conn.execute("ROLLBACK TO delivery_lookup_install")
                conn.execute("RELEASE delivery_lookup_install")
            self._disable()

    def _disable(self):
        self.table = None
        if not self.disabled:
            self.disabled = True
            # Warning handlers must never turn invalidation into a failed write.
            try:
                logging.getLogger("chat_streamd_v2.store").warning(
                    "Delivery lookup sharing disabled for this connection", exc_info=True)
            except BaseException:
                pass

    def _invalidate(self):
        self.dirty = True

    def _changed(self):
        try:
            self._invalidate()
        except BaseException:
            self._disable()

    @staticmethod
    def _data_version(conn):
        return conn.execute("PRAGMA main.data_version").fetchone()[0]

    def get(self, conn, *, shared):
        if self.disabled or not shared:
            return ProjectionDeliveries()
        version = self._data_version(conn)
        if self.table is None or self.dirty or self.version != version:
            self.table = ProjectionDeliveries()
            self.version = version
            self.dirty = False
        else:
            self.table.reuse = ProjectionReuse()
        return self.table


class ProjectionDeliveries:
    """Lazy, read-only delivery lookup with a sticky SQL fallback.

    Store readers can retain the lookup across calls. Parser/equality uncertainty
    keeps this object on the original SQL path until the lookup is rebuilt.
    The attached tail reuse belongs to the current projection call only.
    """

    def __init__(self):
        self._by_target = None
        self._by_input = None
        self._legacy = False
        self.reuse = ProjectionReuse()

    def rows_for(self, conn, target, text):
        if not _lookup_string(target) or not _lookup_string(text):
            self._legacy = True
        if self._legacy:
            return None
        if self._by_target is None:
            by_target, by_input = {}, {}
            cache = getattr(conn, "delivery_parse_cache", None)
            live_tell_ids = [] if cache is not None else None
            cursor = None
            try:
                cursor = conn.execute(
                    "SELECT tell_id,reply,json_extract(reply,'$.delivery.to_stream_id') AS sql_target,"
                    "json_extract(reply,'$.delivery.text') AS sql_text FROM v2_tell_deliveries")
                for row in cursor:
                    if not isinstance(row["reply"], str):
                        raise ValueError("non-text JSON")
                    if cache is not None:
                        live_tell_ids.append(row["tell_id"])
                        payload = cache.decode(row["tell_id"], row["reply"])
                    else:
                        payload = json.loads(row["reply"], object_pairs_hook=_unique_object,
                                             parse_constant=_nonfinite_constant)
                    if not isinstance(payload, dict):
                        raise ValueError("non-object JSON root")
                    delivery = payload.get("delivery")
                    if delivery is not None and not isinstance(delivery, dict):
                        raise ValueError("non-object delivery")
                    delivery = delivery or {}
                    for key, column in (("to_stream_id", "sql_target"), ("text", "sql_text")):
                        value = delivery.get(key)
                        if value is not None and not _lookup_string(value):
                            raise ValueError("unclean delivery lookup value")
                        extracted = row[column]
                        if type(extracted) is not type(value) or extracted != value:
                            raise ValueError("SQLite/Python delivery lookup disagreement")
                    item = {"tell_id": row["tell_id"], "payload": payload, "delivery": delivery,
                            "text_digest": (cache.text_digest(row["tell_id"], delivery.get("text"))
                                            if cache is not None
                                            else provider_text_digest(delivery.get("text")))}
                    by_target.setdefault(delivery.get("to_stream_id"), []).append(item)
                    by_input.setdefault((delivery.get("to_stream_id"), delivery.get("text")), []).append(payload)
            except (sqlite3.Error, TypeError, ValueError, AttributeError, RecursionError):
                self._legacy = True
                return None
            finally:
                if cursor is not None:
                    cursor.close()
            if cache is not None:
                cache.prune(live_tell_ids)
            self._by_target, self._by_input = by_target, by_input
        return self._by_target.get(target, [])

    def candidates(self, conn, target, text):
        if self.rows_for(conn, target, text) is None:
            return None
        return list(self._by_input.get((target, text), []))


def finalize_prepared(conn, original, result):
    """Only the exact original payload and pre-input anchors may complete custody."""
    old = original.get("delivery") or {}
    final = result.get("delivery") or {}
    reply = result.get("reply") or {}
    anchors = ("tell_id", "to_stream_id", "text", "request_payload_hash", "proof_generation",
               "proof_session_created_at", "proof_watermark", "proof_watermark_state", "proof_watermark_reason",
               "sender_verified", "sender_kind", "sender_generation")
    if (original.get("payload_digest") != result.get("payload_digest")
            or old.get("delivery_status") != "proof_unavailable" or old.get("submission_attempts") != 0
            or old.get("notification_answer_generation")
            or final.get("delivery_status") not in {"delivered", "committed_pending_proof", "pasted_unsubmitted"}
            or reply.get("delivery_status") != final.get("delivery_status")
            or reply.get("tell_id") != final.get("tell_id")
            or reply.get("to_stream_id") != final.get("to_stream_id")
            or any(old.get(k) != final.get(k) for k in anchors)):
        return None
    target = old.get("to_stream_id") or ""
    host, _, name = target.partition(":")
    current = conn.execute("SELECT s.created_at,g.generation FROM sessions s JOIN v2_session_generations g "
        "ON g.host=s.host AND g.session_name=s.session_name WHERE s.host=? AND s.session_name=?",
        (host, name)).fetchone()
    if current is None or (current["created_at"], current["generation"]) != (
            old.get("proof_session_created_at"), old.get("proof_generation")):
        return None
    return result


def _tail_events(conn, target, birth, watermark, reuse):
    """Tail events after `watermark`: the request's shared window, else its own read."""
    window = reuse.tail_after(conn, target, birth, watermark) if reuse is not None else None
    if window is not None:
        yield from window
        return
    for row in conn.execute(
            "SELECT event_id,event_json FROM session_event_tail "
            "WHERE stream_id=? AND session_created_at=? AND event_id>? ORDER BY event_id",
            (target, birth, watermark)):
        yield _TailEvent(row["event_id"], row["event_json"])


def retained_proof(conn, payload, *, deliveries=None):
    """Run inside the promotion transaction; never infer missing original anchors."""
    delivery = payload.get("delivery") or {}
    target = delivery.get("to_stream_id")
    generation = delivery.get("proof_generation")
    birth = delivery.get("proof_session_created_at")
    watermark = delivery.get("proof_watermark")
    text = delivery.get("text")
    if (not isinstance(target, str) or ":" not in target or not generation or not birth
            or type(watermark) is not int or watermark < 0
            or delivery.get("proof_watermark_state") != "reachable"
            or not isinstance(text, str) or not text
            or delivery.get("request_payload_hash") != payload.get("payload_digest")):
        return None
    host, name = target.split(":", 1)
    current = conn.execute(
        "SELECT s.created_at,g.generation FROM sessions s JOIN v2_session_generations g "
        "ON g.host=s.host AND g.session_name=s.session_name "
        "WHERE s.host=? AND s.session_name=?", (host, name)).fetchone()
    if current is None or (current["created_at"], current["generation"]) != (birth, generation):
        return None
    matches = []
    # Per-CALL input digests: each distinct representation of `text` is hashed at
    # most once, and only if a comparison below actually needs it.
    memo = {}

    def raw_input_digest():
        if "raw" not in memo:
            memo["raw"] = provider_text_digest(text)
        return memo["raw"]

    def accepted_input_digests():
        if "stripped" not in memo:
            stripped = strip_peer_delivery(text)
            memo["stripped"] = raw_input_digest() if stripped == text else provider_text_digest(stripped)
        return {raw_input_digest(), memo["stripped"]}

    # The retained history may be older than the current 500-event live tail.
    for tail in _tail_events(conn, target, birth, watermark,
                             deliveries.reuse if deliveries is not None else None):
        try:
            event = tail.event()
            candidate_wire = delivery_wire(event)
            if (delivery.get("proof_event_id") is None and event.get("kind") in {"USER", "TELL"}
                    and isinstance(candidate_wire, str) and candidate_wire != text and text in candidate_wire
                    and not operator_dispatch(conn, target, candidate_wire, generation)):
                return None  # An unsplit occurrence cannot be reassigned to a later quotation.
            if (event.get("stream_id", target) == target
                    and (event.get("kind") == "USER" or (event.get("kind") == "TELL"
                         and isinstance((event.get("raw") or {}).get("peer_wire_text"), str)))
                    and delivery_wire(event) == text
                    and ("provider_text_digest" not in event or event["provider_text_digest"] in accepted_input_digests())
                    and not operator_dispatch(conn, target, text, generation)
                    and (delivery.get("proof_event_id") is None or delivery["proof_event_id"] == tail.event_id)):
                matches.append((tail.event_id, event.get("timestamp")))
        except (TypeError, ValueError, AttributeError):
            continue
    if len(matches) != 1:
        return None
    event_id, timestamp = matches[0]
    # Bare operator/system text lacks the unique peer tell-id envelope. Another
    # overlapping identical input makes ownership ambiguous, even with one USER.
    rows = deliveries.rows_for(conn, target, text) if deliveries is not None else None
    for row in (rows if rows is not None else conn.execute("SELECT tell_id,reply FROM v2_tell_deliveries")):
        if row["tell_id"] == delivery.get("tell_id"):
            continue
        try:
            other = row["delivery"] if rows is not None else json.loads(row["reply"]).get("delivery") or {}
            lower = other.get("proof_watermark")
            upper = other.get("proof_event_id")
            if (other.get("to_stream_id") == target
                    and other.get("proof_generation") == generation
                    and type(lower) is int and lower < event_id
                    and not (type(upper) is int and upper <= watermark)
                    and (row["text_digest"] if rows is not None else provider_text_digest(other.get("text")))
                    == raw_input_digest()):
                return None
        except (TypeError, ValueError, AttributeError):
            return None  # Unverifiable ownership never establishes delivery.
    return EventProof("proven", target, watermark, event_id=event_id,
                      event_ts=str(timestamp) if timestamp else None).audit_fields()


def send_proofs(conn, target, text):
    """Read only daemon pre-input receipt JSON; public meta is voice-only."""
    attempts = {}
    no_input = set()
    rows = history_query(conn,
        "SELECT rowid,* FROM v2_send_receipts WHERE to_stream_id=? "
        f"AND {RECEIPT_PROOF_PREDICATE} "
        f"AND instr(?,{RECEIPT_PROOF_WIRE})>0 ORDER BY rowid LIMIT 513",
        (target, text)).fetchall()
    if len(rows) > 512:
        return  # Unbounded/ambiguous history is never an origin grant.
    for row in rows:
        try:
            meta = json.loads(row["meta_json"])
            proof = meta.get("_input_proof")
        except (TypeError, ValueError):
            continue
        key = row["receipt_id"]
        # A durable not_started outcome disqualifies the earlier pre-input
        # anchor. Other not_landed outcomes can still have physically pasted.
        if meta.get("pool_input_not_started") is True:
            no_input.add(key)
            continue
        if not isinstance(proof, dict):
            continue
        previous = attempts.get(key)
        # Later completion may not overwrite an already-consumed occurrence.
        if previous is None or proof.get("proof_event_id") is not None:
            attempts[key] = (row, proof)
    for row, proof in attempts.values():
        if row["receipt_id"] in no_input:
            continue
        length = proof.get("wire_length")
        if type(length) is not int or length <= 0 or not isinstance(text, str):
            continue
        expected = proof.get("wire_text")
        if (not isinstance(expected, str) or len(expected) != length
                or hashlib.sha256(expected.encode()).hexdigest() != proof.get("wire_sha256")):
            continue
        positions = [expected] if expected in text else []
        if positions:
            wire = positions[0]
            yield {"payload_digest": proof["wire_sha256"], "delivery": {
                **proof, "tell_id": f"send:{row['receipt_id']}", "text": wire,
                "request_payload_hash": proof["wire_sha256"], "_send_receipt_id": row["receipt_id"],
            }}, row


def operator_dispatch(conn, target, text, generation):
    """An authenticated dispatch owns its exact wire even if it resembles a tell."""
    return conn.execute(
        "SELECT dispatch_id FROM v2_assistant_composite_routes WHERE route_target=? "
        "AND route_target_generation=? AND routing_state='resolved' "
        "AND json_extract(route_json,'$.admission_mode')='direct_primary' "
        "AND json_extract(route_json,'$.direct_envelope.wire_body')=? LIMIT 1",
        (target, generation, text),
    ).fetchone()


def delivery_wire(event):
    raw = event.get('raw') if isinstance(event.get('raw'), dict) else {}
    return raw.get('peer_wire_text') if isinstance(raw.get('peer_wire_text'), str) else event.get('text')


def split_native_input(conn, event, *, lookup_deadline=None):
    """Locate only unique, unconsumed exact delivery spans in an admitted input.

    The original record and full text remain in raw. Textual headers are never
    a boundary oracle. Leftovers retain unknown origin until dispatch proof.
    """
    if event.get('kind') not in {'USER', 'TELL'}:
        return [event]
    text = delivery_wire(event)
    target = event.get('stream_id')
    source_raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if not isinstance(text, str) or not target:
        return [event]
    host, _, name = target.partition(':')
    seat = conn.execute('SELECT s.created_at,g.generation FROM sessions s JOIN v2_session_generations g '
        'ON g.host=s.host AND g.session_name=s.session_name WHERE s.host=? AND s.session_name=?',
        (host, name)).fetchone()
    if seat is None or operator_dispatch(conn, target, text, seat['generation']):
        return [event]
    from ingest import _identity_key
    identity = _identity_key(event)
    from native_input_lookup import prior_input
    prior = prior_input(conn, target, seat["created_at"], identity, text, deadline=lookup_deadline)
    retained_cuts = (json.loads(prior[0]).get("raw") or {}).get("native_segment_spans") if prior else None
    spans = []
    ambiguous = False
    payloads = [json.loads(row["reply"]) for row in conn.execute(
        "SELECT reply FROM v2_tell_deliveries WHERE json_extract(reply,'$.delivery.to_stream_id')=? "
        "AND instr(?,json_extract(reply,'$.delivery.text'))>0", (target, text))]
    payloads.extend(payload for payload, _ in send_proofs(conn, target, text))
    for payload in payloads:
        d = payload.get('delivery') or {}
        wire = d.get('text')
        if (not isinstance(wire, str) or not wire or wire not in text
                or d.get('proof_event_id') is not None
                or d.get('proof_generation') != seat['generation']
                or d.get('proof_session_created_at') != seat['created_at']
                or d.get('proof_watermark_state') != 'reachable'
                or type(d.get('proof_watermark')) is not int
                or d['proof_watermark'] < 0
                or d.get('request_payload_hash') != payload.get('payload_digest')
                or not (d.get('sender_verified') is True or wire.startswith('[pentacle-notice:'))):
            continue
        if text.count(wire) != 1:
            ambiguous = True
            continue
        start = text.index(wire)
        spans.append((start, start + len(wire), wire))
    for row in conn.execute(
        "SELECT route_json FROM v2_assistant_composite_routes WHERE route_target=? "
        "AND route_target_generation=? AND routing_state='resolved' "
        "AND json_extract(route_json,'$.admission_mode')='direct_primary'", (target, seat["generation"])):
        wire = ((json.loads(row[0]).get("direct_envelope") or {}).get("wire_body"))
        if isinstance(wire, str) and wire and text.count(wire) == 1:
            start = text.index(wire)
            # A quoted operator wire inside a peer payload remains peer payload.
            if not any(a <= start and start + len(wire) <= b for a, b, _ in spans):
                spans.append((start, start + len(wire), wire))
    spans.sort()
    if ambiguous or any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
        return [{**event, 'raw': {**source_raw, 'input_provenance': 'unknown',
                 'subsystem': 'mixed_input', 'bug_ref': 'ambiguous_delivery_span'}}]
    if not retained_cuts and (not spans or (len(spans) == 1 and spans[0][:2] == (0, len(text)))):
        return [event]
    from message_envelopes import parse_peer_delivery
    cuts = []
    cursor = 0
    for start, end, wire in spans:
        if cursor < start:
            cuts.append((cursor, start, text[cursor:start]))
        cuts.append((start, end, wire))
        cursor = end
    if cursor < len(text):
        cuts.append((cursor, len(text), text[cursor:]))
    if retained_cuts:
        cuts = [(start, end, text[start:end]) for start, end in retained_cuts]
    result = []
    for ordinal, (start, end, part) in enumerate(cuts):
        raw = {k: v for k, v in source_raw.items()
               if k not in {'sender', 'tell_id', 'peer_payload', 'peer_wire_text', 'enqueued_at'}}
        raw.update(native_input_version=1, legacy_input_events=raw.get("legacy_input_events") or [event],
                   native_segment_span=[start, end],
                   native_segment_ordinal=ordinal, native_input_text=text, native_input_identity=identity,
                   native_segment_spans=[[a, b] for a, b, _ in cuts],
                   jsonl_event_index=f"{raw.get('jsonl_event_index', 0)}:{start}:{end}")
        match = parse_peer_delivery(part)
        kind, body = 'USER', part
        if match:
            kind, body = 'TELL', match['payload']
            raw.update(sender=match['sender'], tell_id=match['id'], peer_payload=body, peer_wire_text=part)
        result.append({**event, 'kind': kind, 'text': body, 'raw': raw})
    return result


def input_origin(conn, event, event_id, *, deliveries=None):
    """Read-only classification against exact dispatch/delivery occurrence."""
    text = delivery_wire(event)
    target = event.get('stream_id')
    if not target or not isinstance(text, str):
        return {'kind': 'unknown', 'verified': False}
    host, _, name = target.partition(':')
    row = conn.execute('SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?',
                       (host, name)).fetchone()
    generation = row[0] if row else None
    if generation and operator_dispatch(conn, target, text, generation):
        return {'kind': 'operator', 'verified': True, 'generation': None}
    matches = []
    payloads = deliveries.candidates(conn, target, text) if deliveries is not None else None
    if payloads is None:
        payloads = [json.loads(row["reply"]) for row in conn.execute(
            "SELECT reply FROM v2_tell_deliveries WHERE json_extract(reply,'$.delivery.to_stream_id')=? "
            "AND json_extract(reply,'$.delivery.text')=?", (target, text))]
    payloads.extend(payload for payload, _ in send_proofs(conn, target, text))
    for payload in payloads:
        delivery = payload.get('delivery') or {}
        if delivery.get('text') != text or delivery.get('proof_generation') != generation:
            continue
        proof = (retained_proof(conn, payload) if deliveries is None
                 else retained_proof(conn, payload, deliveries=deliveries))
        if proof is None or proof.get('proof_event_id') != event_id:
            continue
        kind = delivery.get('sender_kind')
        if delivery.get('sender_verified') is True and kind in {'peer', 'operator', 'service'}:
            if kind == 'peer':
                from message_envelopes import parse_peer_delivery
                candidate = parse_peer_delivery(text)
                sender_generation = delivery.get('sender_generation')
                if (not candidate or candidate['sender'] != delivery.get('from_stream_id')
                        or not isinstance(sender_generation, str) or not sender_generation):
                    continue
            matches.append({'kind': kind, 'verified': True,
                'sender': delivery.get('from_stream_id'), 'generation': delivery.get('sender_generation')})
        elif text.startswith('[pentacle-notice:') and conn.execute(
            'SELECT 1 FROM v2_outbound_notices WHERE recipient_stream_id=? AND tell_id=? AND body=?',
            (target, delivery.get('tell_id'), text)).fetchone():
            matches.append({'kind': 'service', 'verified': True, 'generation': None})
    return matches[0] if len(matches) == 1 else {'kind': 'unknown', 'verified': False}
