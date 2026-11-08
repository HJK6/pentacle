"""Evidence-only reconciliation of an ordinary tell's original native input."""
from __future__ import annotations

import json
import hashlib

from submission_events import EventProof, provider_text_digest
from retention import history_query
from message_envelopes import strip_peer_delivery


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


def retained_proof(conn, payload):
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
    # The retained history may be older than the current 500-event live tail.
    for row in conn.execute(
            "SELECT event_id,event_json FROM session_event_tail "
            "WHERE stream_id=? AND session_created_at=? AND event_id>? ORDER BY event_id",
            (target, birth, watermark)):
        try:
            event = json.loads(row["event_json"])
            candidate_wire = delivery_wire(event)
            if (delivery.get("proof_event_id") is None and event.get("kind") in {"USER", "TELL"}
                    and isinstance(candidate_wire, str) and candidate_wire != text and text in candidate_wire
                    and not operator_dispatch(conn, target, candidate_wire, generation)):
                return None  # An unsplit occurrence cannot be reassigned to a later quotation.
            if (event.get("stream_id", target) == target
                    and (event.get("kind") == "USER" or (event.get("kind") == "TELL"
                         and isinstance((event.get("raw") or {}).get("peer_wire_text"), str)))
                    and delivery_wire(event) == text
                    and ("provider_text_digest" not in event or event["provider_text_digest"] in {
                        provider_text_digest(text), provider_text_digest(strip_peer_delivery(text))})
                    and not operator_dispatch(conn, target, text, generation)
                    and (delivery.get("proof_event_id") is None or delivery["proof_event_id"] == row["event_id"])):
                matches.append((row["event_id"], event.get("timestamp")))
        except (TypeError, ValueError, AttributeError):
            continue
    if len(matches) != 1:
        return None
    event_id, timestamp = matches[0]
    # Bare operator/system text lacks the unique peer tell-id envelope. Another
    # overlapping identical input makes ownership ambiguous, even with one USER.
    for row in conn.execute("SELECT tell_id,reply FROM v2_tell_deliveries"):
        if row["tell_id"] == delivery.get("tell_id"):
            continue
        try:
            other = json.loads(row["reply"]).get("delivery") or {}
            lower = other.get("proof_watermark")
            upper = other.get("proof_event_id")
            if (other.get("to_stream_id") == target
                    and other.get("proof_generation") == generation
                    and type(lower) is int and lower < event_id
                    and not (type(upper) is int and upper <= watermark)
                    and provider_text_digest(other.get("text")) == provider_text_digest(text)):
                return None
        except (TypeError, ValueError, AttributeError):
            return None  # Unverifiable ownership never establishes delivery.
    return EventProof("proven", target, watermark, event_id=event_id,
                      event_ts=str(timestamp) if timestamp else None).audit_fields()


def send_proofs(conn, target, text):
    """Read only daemon pre-input receipt JSON; public meta is voice-only."""
    attempts = {}
    rows = history_query(conn,
        "SELECT rowid,* FROM v2_send_receipts WHERE to_stream_id=? "
        "AND json_type(meta_json,'$._input_proof')='object' "
        "AND instr(?,json_extract(meta_json,'$._input_proof.wire_text'))>0 ORDER BY rowid LIMIT 513",
        (target, text)).fetchall()
    if len(rows) > 512:
        return  # Unbounded/ambiguous history is never an origin grant.
    for row in rows:
        try:
            proof = json.loads(row["meta_json"]).get("_input_proof")
        except (TypeError, ValueError):
            continue
        if not isinstance(proof, dict):
            continue
        key = row["receipt_id"]
        previous = attempts.get(key)
        # Later completion may not overwrite an already-consumed occurrence.
        if previous is None or proof.get("proof_event_id") is not None:
            attempts[key] = (row, proof)
    for row, proof in attempts.values():
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


def split_native_input(conn, event):
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
    prior = conn.execute(
        "SELECT event_json FROM session_event_tail WHERE stream_id=? AND session_created_at=? "
        "AND json_extract(event_json,'$.raw.native_input_identity')=? "
        "AND json_extract(event_json,'$.raw.native_input_text')=? LIMIT 1",
        (target, seat["created_at"], identity, text),
    ).fetchone()
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


def input_origin(conn, event, event_id):
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
    payloads = [json.loads(row["reply"]) for row in conn.execute(
        "SELECT reply FROM v2_tell_deliveries WHERE json_extract(reply,'$.delivery.to_stream_id')=? "
        "AND json_extract(reply,'$.delivery.text')=?", (target, text))]
    payloads.extend(payload for payload, _ in send_proofs(conn, target, text))
    for payload in payloads:
        delivery = payload.get('delivery') or {}
        if delivery.get('text') != text or delivery.get('proof_generation') != generation:
            continue
        proof = retained_proof(conn, payload)
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
