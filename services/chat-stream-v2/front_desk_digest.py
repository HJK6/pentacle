"""Front-desk-only admission, backed by the existing notice outbox.

Held rows cannot be claimed for delivery. Wake inputs pass through unchanged;
only deadline batches use the ordinary lane_digest delivery path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

from message_envelopes import build_message_envelope
from store_routing import _insert_outbound_notice_conn
from v2_runtime import env_number, iso_now

log = logging.getLogger('chat_streamd_v2.front_desk_digest')
HELD_KIND = 'front_desk_held'
DIGEST_TOKEN = object()


class FrontDeskDigest:
    def __init__(self, store, config):
        self.store = store
        self.config = config

    def binding(self):
        cfg = self.config()
        if cfg.enabled and cfg.name == 'bart' and cfg.direct_primary:
            return cfg.direct_primary_stream_id, cfg.direct_primary_generation
        return None

    def matches(self, target):
        binding = self.binding()
        return bool(binding and target == binding[0])

    @staticmethod
    def deadline_s():
        return max(0.0, env_number(os.environ, 'PENTACLE_FRONT_DESK_DIGEST_S', 3600.0, float))

    async def ingress(self, *, target_stream_id, body, msg, verb):
        if not self.matches(target_stream_id):
            return None
        if msg.get('_front_desk_digest_token') is DIGEST_TOKEN:
            return None
        kind = msg.get('_outbound_notice_kind')
        text = re.sub(r'^\[pentacle-notice:[^\]]+\]\s*', '', body).lstrip()
        drop = kind == 'tree_idle' or (
            text.startswith('context_advisory:') and not text.startswith('context_advisory: '+target_stream_id+' '))
        if kind == 'assistant_lane_ruling_result':
            try:
                ruling = json.loads(text.split('] ', 1)[1])
            except (ValueError, IndexError):
                ruling = {}
            if ruling.get('state') == 'done' and ruling.get('action') == 'spawn' and not ruling.get('conditions'):
                drop = True
            else:
                return None
        auth = msg.get('_auth_context') or {}
        wake = (auth.get('operator_authenticated') is True
                or msg.get('_assistant_operator_authenticated') is True
                or re.match(r'^(GATE|BLOCKER)\b', text, re.I)
                or kind in {'report', 'notification_answer', 'wake', 'wake_urgent', 'wake_missed'}
                or text.startswith('[child_report_ready'))
        if not drop and wake:
            return None
        identity = str(msg.get('tell_id') or msg.get('request_id') or hashlib.sha256(body.encode()).hexdigest())
        nid = 'frontdesk-held:'+hashlib.sha256((target_stream_id+'\0'+verb+'\0'+identity).encode()).hexdigest()
        if not drop:
            await self.store.enqueue_outbound_notice(notice_id=nid, tell_id=nid,
                kind=HELD_KIND, dedupe_key=nid, recipient_stream_id=target_stream_id,
                source_stream_id=msg.get('from_stream_id'), body=body,
                metadata={'root_generation':self.binding()[1]})
        log.info('subsystem=front_desk_digest bug_ref=front_desk_wake_reduction action=%s target=%s',
                 'drop' if drop else 'hold', target_stream_id)
        return {'type':verb+'.ok', 'delivery_status':'persisted', 'submission_confirmed':False,
                'action_committed':True, 'assistant_backend_ingress':'persisted_suppressed'}

    async def _rows(self, target):
        return await self.store.submit(lambda conn: [dict(row) for row in conn.execute(
            "SELECT * FROM v2_outbound_notices WHERE kind=? AND recipient_stream_id=? "
            "AND delivered_at IS NULL AND terminal_at IS NULL ORDER BY created_at,notice_id", (HELD_KIND,target))])

    def _block(self, rows):
        nid='d2:'+hashlib.sha256('\0'.join(r['notice_id'] for r in rows).encode()).hexdigest()
        body=build_message_envelope('lane_digest', notice_id=nid,
            lanes=[{'stream_id':r.get('source_stream_id'), 'text':r['body']} for r in rows],
            evaluated_at=rows[-1]['created_at'])
        return nid, body

    async def tick(self, now=None):
        """Atomically fold due held rows into the existing lane_digest outbox."""
        from datetime import datetime
        binding = self.binding()
        if not binding:
            return
        target, generation = binding
        now = time.time() if now is None else now
        deadline_s = self.deadline_s()
        def op(conn):
            with conn:
                pending = [dict(row) for row in conn.execute(
                    "SELECT * FROM v2_outbound_notices WHERE kind=? AND recipient_stream_id=? "
                    "AND delivered_at IS NULL AND terminal_at IS NULL ORDER BY created_at,notice_id",
                    (HELD_KIND, target))
                    if json.loads(row['metadata']).get('root_generation') == generation]
                if not pending:
                    return
                oldest = datetime.fromisoformat(pending[0]['created_at'].replace('Z', '+00:00')).timestamp()
                if now < oldest + deadline_s:
                    return
                nid, body = self._block(pending)
                _insert_outbound_notice_conn(conn, notice_id=nid, tell_id=nid, kind='lane_digest', dedupe_key=nid,
                    recipient_stream_id=target, body=body,
                    metadata={'root_generation': generation, 'front_desk_digest': True})
                for row in pending:
                    conn.execute("UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason='folded_into_digest' WHERE notice_id=?",
                                 (iso_now(), row['notice_id']))
        await self.store.submit(op)
