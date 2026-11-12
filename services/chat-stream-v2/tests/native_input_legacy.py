"""Unchanged split oracle from public base 76564a24 (before lookup repair).

Keep this function frozen: equality must compare against the original behavior.
"""
import json
from tell_proof import delivery_wire, operator_dispatch, send_proofs


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
