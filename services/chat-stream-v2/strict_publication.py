"""Strict composite publication checks and existing-pass no-reply reconciliation."""
from datetime import datetime, timezone
import json
import logging
import re
import shlex
import time
import unicodedata

UNANSWERED_AFTER_S = 300


class PublicationRefusal(ValueError):
    def __init__(self, code, detail):
        super().__init__(code)
        self.detail = detail


def desk_input(route):
    payload = json.loads(route['route_json'] or '{}')
    if 'desk_input' in payload:
        return payload['desk_input'] is True
    # Historical routes predate the durable classification. Use authenticated
    # route provenance, never a body prefix controlled by the sender.
    import desk_stamp
    principal = str(dict(route).get('actor_stream_id') or '')
    return principal.startswith('operator:') and principal[9:] in desk_stamp.load()


def normalized_text(text):
    text = unicodedata.normalize('NFKC', str(text)).lower()
    return ' '.join(re.sub(r'[*_~]', '', text).split())


def validate_publication(conn, stream_id, dispatch_id, payload):
    if dispatch_id:
        route = conn.execute('SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND dispatch_id=?',
                             (stream_id, dispatch_id)).fetchone()
        if route and desk_input(route):
            raise PublicationRefusal('assistant_publish_desk_dispatch',
                                     'Desk input has no dispatch reply; use a relay note referencing the message.')
        return
    note_type, ref = payload.get('note_type'), payload.get('ref')
    if not isinstance(note_type, str) or note_type not in {'result', 'problem', 'reminder', 'relay'}:
        raise ValueError('assistant_publish_note_type_required')
    if not isinstance(ref, str) or not re.fullmatch(r'(message|lane|report|wake|tell):\S+', ref):
        raise ValueError('assistant_publish_ref_required')
    kind, identifier = ref.split(':', 1)
    if ((note_type == 'reminder' and kind not in {'wake', 'message'})
            or (note_type == 'relay' and kind not in {'tell', 'message'})):
        raise ValueError('assistant_publish_ref_kind_not_allowed')
    if kind == 'message':
        exists = conn.execute('SELECT 1 FROM v2_assistant_composite_routes WHERE stream_id=? AND dispatch_id=?',
                              (stream_id, identifier)).fetchone() or conn.execute(
            'SELECT 1 FROM v2_assistant_composite_routes WHERE stream_id=? AND input_identity=?',
            (stream_id, identifier)).fetchone()
    else:
        table, column = {'lane': ('v2_assistant_composite_lanes', 'lane_id'),
                         'report': ('v2_reports', 'report_id'), 'wake': ('v2_watch_wake', 'id'),
                         'tell': ('v2_tell_deliveries', 'tell_id')}[kind]
        exists = conn.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (identifier,)).fetchone()
        if kind == 'wake' and not exists:
            exists = conn.execute('SELECT 1 FROM v2_schedules WHERE schedule_id=?', (identifier,)).fetchone()
    if not exists:
        raise ValueError('assistant_publish_ref_not_found')
    cutoff = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat().replace('+00:00', 'Z')
    text = normalized_text(payload.get('message', ''))
    for earlier in conn.execute('SELECT p.event_id,p.canonical_payload_json,e.event_json FROM v2_assistant_composite_publications p '
                                'LEFT JOIN session_event_tail e ON e.event_id=p.event_id '
                                'WHERE p.stream_id=? AND p.created_at>=? AND p.merged_into_event_id IS NULL ORDER BY p.event_id', (stream_id, cutoff)):
        prior_text = (json.loads(earlier['event_json']).get('text') if earlier['event_json']
                      else json.loads(earlier['canonical_payload_json']).get('message'))
        if prior_text is not None and text == normalized_text(prior_text):
            raise PublicationRefusal('assistant_publish_duplicate_text', f"Duplicate of event {earlier['event_id']}")


def dispatch_commands(route):
    dispatch = route['dispatch_id']
    common = (f"--composite-stream-id {shlex.quote(route['stream_id'])} --dispatch-id {shlex.quote(dispatch)} "
              f"--reply-to-message-id {shlex.quote(route['input_identity'])} ")
    if route['reply_to_question_id']:
        common += f"--reply-to-question-id {shlex.quote(route['reply_to_question_id'])} "
    return (f"agent-orch assistant publish --request-id {shlex.quote('publish:' + dispatch + ':ack')} "
            + common + "--publish-kind status --response-state acknowledged --message 'I am working on your message.'",
            f"agent-orch assistant publish --request-id {shlex.quote('publish:' + dispatch)} "
            + common + "--publish-kind prose --response-state final --message '<your final answer>'")


async def mark_unanswered(store, composite, broadcast, now=None):
    if composite is None or not composite.enabled:
        return
    now = time.time() if now is None else now
    def insert(conn):
        if not store._strict_publication_state_conn(conn, composite.config.stream_id)['enabled']:
            return []
        # Read candidates; append_unanswered_event rechecks eligibility in
        # its write transaction so a publication cannot race the marker.
        result = []
        rows = conn.execute("SELECT * FROM v2_assistant_composite_routes r WHERE stream_id=? "
            "AND delivery_state='landed' AND dispatch_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM v2_assistant_composite_publications p "
            "WHERE p.stream_id=r.stream_id AND p.dispatch_id=r.dispatch_id)",
            (composite.config.stream_id,)).fetchall()
        for route in rows:
            if desk_input(route):
                continue
            timings = json.loads(route['route_json'] or '{}').get('timings', {})
            landed = timings.get('landed_at') or route['updated_at']
            if not landed:
                continue
            try:
                age = now - datetime.fromisoformat(landed.replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError):
                continue
            if age < UNANSWERED_AFTER_S:
                continue
            identity = 'dispatch-unanswered:' + route['dispatch_id']
            if conn.execute('SELECT 1 FROM session_event_tail WHERE identity=?', (identity,)).fetchone():
                continue
            result.append((dict(route), identity))
        return result
    # Eligibility is rechecked by the store append operation below.
    for route, identity in await store.submit(insert):
        event = {'stream_id': composite.config.stream_id, 'provider': 'composite', 'kind': 'ASSIST_TEXT',
            'text': 'No reply from Bart yet to this message.', 'publish_kind': 'unanswered',
            'reply_to_message_id': route['input_identity'], 'attachments': [],
            'timestamp': datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z'),
            'raw': {'assistant_composite': True, 'dispatch_id': route['dispatch_id'],
                    'publish_kind': 'unanswered', 'reply_to_message_id': route['input_identity']}}
        event_id = await store.append_unanswered_event(route, event, identity)
        if event_id is not None:
            event['daemon_seq'] = event_id
            composite._advance_summary(event)
            if broadcast is not None:
                await broadcast({'type': 'chat.event', 'event': event})


async def reconcile_unanswered(server):
    composites = list(getattr(server, 'assistant_composites', {}).values())
    primary = getattr(server, 'assistant_composite', None)
    if primary is not None and primary not in composites:
        composites.append(primary)
    for composite in composites:
        try:
            await mark_unanswered(server.store, composite, server.broadcast)
        except Exception:
            # A failed marker pass must not block existing ruling releases.
            logging.getLogger(__name__).exception("unanswered reconciliation failed; will retry")
