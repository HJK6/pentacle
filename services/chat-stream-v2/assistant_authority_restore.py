"""Durable recovery of the assistant's independent ruling authority."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from assistant_restore import SERVICE_ACTOR
from store_watch_wake import coalesce_notice_conn

log = logging.getLogger(__name__)
SERVICE_CAPABILITY = object()
SHAPE_KEY = 'assistant.authority.shape'
ACTOR = 'daemon:authority-restore'

# A sibling is needed: the existing bound-seat table requires a Claude resume
# id and assumes one stream throughout the episode. Authority replacement is a
# fresh spawn, and its frozen payload and successor stream must survive restart.
DDL = '''CREATE TABLE IF NOT EXISTS v2_authority_restore_episode (
 episode_id INTEGER PRIMARY KEY AUTOINCREMENT, stream_id TEXT NOT NULL,
 generation TEXT NOT NULL, state TEXT NOT NULL, trigger TEXT NOT NULL,
 attempt_seq INTEGER NOT NULL DEFAULT 0, budget_epoch INTEGER NOT NULL DEFAULT 1,
 budget_used INTEGER NOT NULL DEFAULT 0, spawn_key TEXT, attempt_generation TEXT,
 payload TEXT, attempt_started_at REAL, next_attempt_at REAL, successor TEXT,
 last_error TEXT, errors TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
 updated_at REAL NOT NULL, UNIQUE(stream_id,generation))'''
AUDIT_DDL = '''CREATE TABLE IF NOT EXISTS v2_authority_restore_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, episode_id INTEGER, attempt_seq INTEGER,
 event TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL)'''


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class AuthorityRestore:
    def __init__(self, server: Any, *, inhibit_path: Path | None, enabled: bool = True,
                 clock=time.time) -> None:
        self.server, self.store = server, server.store
        self.inhibit_path, self.enabled, self.clock = inhibit_path, enabled, clock
        self.lock = asyncio.Lock()
        self.sent: set[str] = set()
        self.loss: tuple[str, str] | None = None
        self.loss_since = 0.0
        self.observations = 0
        self.last_state = 'healthy'
        self.task: asyncio.Task | None = None

    async def initialize(self) -> None:
        def op(conn):
            with conn:
                conn.execute(DDL)
                conn.execute(AUDIT_DDL)
        await self.store.submit(op)

    async def shape(self) -> dict | None:
        raw = await self.store.get(SHAPE_KEY)
        try:
            return json.loads(raw) if raw else None
        except (ValueError, TypeError):
            return None

    def inhibited(self) -> bool:
        try:
            return bool(self.inhibit_path and self.inhibit_path.exists())
        except OSError:
            return True

    async def primary(self) -> dict | None:
        composite = self.server.assistant_composite
        if not composite or not composite.enabled or not composite.config.direct_primary:
            return None
        binding = await composite.binding()
        if not binding.get('stream_id') or not binding.get('generation'):
            return None
        return binding

    async def current(self, stream: str, generation: str) -> dict | None:
        row = await self.store.submit(lambda conn: conn.execute(
            'SELECT * FROM v2_authority_restore_episode WHERE stream_id=? AND generation=?',
            (stream, generation)).fetchone())
        return dict(row) if row else None

    async def update(self, episode: dict, fields: dict, event: str, detail: dict | None = None) -> dict | None:
        now = self.clock()
        def op(conn):
            with conn:
                columns = list(fields)
                count = conn.execute('UPDATE v2_authority_restore_episode SET ' +
                    ','.join(f'{k}=?' for k in columns) + ',updated_at=? WHERE episode_id=? AND state=? AND attempt_seq=?',
                    (*[fields[k] for k in columns], now, episode['episode_id'], episode['state'], episode['attempt_seq'])).rowcount
                if not count:
                    return None
                row = dict(conn.execute('SELECT * FROM v2_authority_restore_episode WHERE episode_id=?',
                                        (episode['episode_id'],)).fetchone())
                conn.execute('INSERT INTO v2_authority_restore_audit(episode_id,attempt_seq,event,detail,created_at) VALUES(?,?,?,?,?)',
                             (row['episode_id'], row['attempt_seq'], event, canonical(detail or {}), now))
                return row
        return await self.store.submit(op)

    async def notice(self, event: str, key: str, body: dict) -> None:
        primary = await self.primary()
        if not primary:
            return
        notice_id = f'authority-restore:{event}:{key}'
        notice = dict(notice_id=notice_id, kind='assistant_lane_ruling_result', dedupe_key=notice_id,
            recipient_stream_id=primary['stream_id'], tell_id=notice_id, source_stream_id=ACTOR,
            episode_id=None, created_at=None, body='[Assistant lane ruling] ' + canonical({'event': event, **body}),
            metadata={'authority_generation': primary['generation']})
        def op(conn):
            with conn:
                coalesce_notice_conn(conn, notice, None)
        await self.store.submit(op)

    async def status(self) -> dict:
        binding = await self.server.lane_rulings.binding()
        shape = await self.shape()
        generation = binding.get('generation') or ((shape or {}).get('generation') if (shape or {}).get('stream_id') == binding.get('stream_id') else '')
        episode = await self.current(binding.get('stream_id', ''), generation or '')
        state = self.last_state
        if episode and episode['state'] in ('pending', 'spawning', 'unresolved', 'exhausted'):
            state = 'restoring' if episode['state'] in ('pending', 'spawning') else episode['state']
        elif not self.enabled or binding['state'] == 'disabled':
            state = 'disabled'
        elif binding['state'] == 'ready':
            state = 'healthy'
        return {'state': state, 'episode_id': episode['episode_id'] if episode else None,
                'attempt_seq': episode['attempt_seq'] if episode else 0,
                'budget_epoch': episode['budget_epoch'] if episode else 0,
                'next_attempt_at': episode['next_attempt_at'] if episode else None,
                'last_error': episode['last_error'] if episode else None,
                'updated_at': episode['updated_at'] if episode else None}

    async def observe(self, stream: str, generation: str, evidence: str, seat: dict | None) -> None:
        shape = await self.shape()
        if not generation and shape and shape.get('stream_id') == stream:
            generation = str(shape.get('generation') or '')
        key = (stream, generation)
        if self.loss != key:
            self.loss, self.loss_since, self.observations = key, self.clock(), 0
        self.observations += 1
        raw = await self.store.get('assistant.authority.loss_notice')
        try:
            previous = json.loads(raw) if raw else {}
        except ValueError:
            previous = {}
        same_epoch = previous.get('open') and previous.get('stream_id') == stream and previous.get('generation') == generation
        epoch = int(previous.get('epoch') or 0) + (0 if same_epoch else 1)
        if not await self.primary():
            self.last_state = 'ineligible'
            return
        if stream.partition(':')[0] != self.server.local_host:
            self.last_state = 'ineligible'
            return
        existing = await self.current(stream, generation)
        if existing and existing['state'] == 'exhausted':
            await self.notice('authority_restore_exhausted', str(existing['episode_id']),
                {'episode': existing['episode_id'], 'errors': json.loads(existing['errors'])})
        if existing and existing['state'] == 'unresolved':
            await self.notice('authority_restore_unresolved', str(existing['episode_id']),
                {'spawn_key': existing['spawn_key'], 'stream': existing['successor'],
                 'evidence': existing['last_error']})
        if existing and existing['state'] in ('pending', 'spawning', 'unresolved'):
            self.schedule()
            if existing['trigger'] == 'manual':
                self.last_state = 'restoring'
                return
        if not self.enabled:
            self.last_state = 'disabled'
            return
        if self.inhibited():
            self.last_state = 'inhibited'
            await self.notice('authority_restore_inhibited', f'{stream}:{generation}:{epoch}', {'marker': str(self.inhibit_path)})
            return
        reason = ('no_recorded_shape' if not shape or shape.get('stream_id') != stream else
                  'shape_generation_mismatch' if shape.get('generation') != generation else '')
        if reason:
            self.last_state = 'ineligible'
            contingency = ('agent-orch assistant authority --restore --brief-file <path>' if seat else
                'Keep the inhibit marker; an authorized non-FD owner spawns a successor, then the front desk runs '
                'agent-orch assistant authority --set <live successor> --brief-file <path>, reads state ready, and removes the marker.')
            await self.notice('authority_restore_ineligible', f'{stream}:{generation}:{epoch}:{reason}',
                              {'reason': reason, 'contingency': contingency})
            return
        self.last_state = 'restoring'
        if self.observations >= 2 and self.clock() - self.loss_since >= 60:
            await self.create(stream, generation, 'auto')
        self.schedule()

    def clear_loss(self) -> None:
        self.loss, self.observations, self.last_state = None, 0, 'healthy'

    async def recovered(self) -> None:
        if self.loss:
            episode = await self.current(*self.loss)
            if episode and episode['state'] in ('pending', 'spawning', 'unresolved', 'exhausted'):
                await self.update(episode, {'state': 'superseded', 'last_error': 'holder_revived'}, 'superseded')
        self.clear_loss()

    async def pane_state(self, name: str) -> str:
        probe = getattr(getattr(self.server.sessions, 'tmux', None), 'session_state', None)
        if not callable(probe):
            return 'unknown'
        try:
            state = await probe(name)
            return state if state in ('alive', 'gone') else 'unknown'
        except Exception:
            return 'unknown'

    async def create(self, stream: str, generation: str, trigger: str) -> dict | None:
        now = self.clock()
        def op(conn):
            with conn:
                conn.execute('INSERT OR IGNORE INTO v2_authority_restore_episode '
                    '(stream_id,generation,state,trigger,created_at,updated_at) VALUES(?,?,\'pending\',?,?,?)',
                    (stream, generation, trigger, now, now))
                row = conn.execute('SELECT * FROM v2_authority_restore_episode WHERE stream_id=? AND generation=?',
                                   (stream, generation)).fetchone()
                return dict(row) if row else None
        return await self.store.submit(op)

    def schedule(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        try:
            async with self.lock:
                await self.step()
        except Exception:
            log.exception('authority restore pass failed')

    async def step(self) -> None:
        if not self.loss or not await self.primary():
            return
        stream, generation = self.loss
        episode = await self.current(stream, generation)
        if not episode or episode['state'] not in ('pending', 'spawning', 'unresolved'):
            return
        binding = await self.server.lane_rulings.binding()
        if binding['stream_id'] != stream or (binding['generation'] and binding['generation'] != generation):
            await self.update(episode, {'state': 'superseded', 'last_error': 'binding_moved'}, 'superseded')
            return
        host, _, name = stream.partition(':')
        old_seat = await self.store.fetch_session(host, name)
        if (old_seat and old_seat.get('status') == 'open' and old_seat.get('pane_status') == 'pane_alive'
                and self.server.lane_rulings._authority_loss_evidence(old_seat) is None):
            await self.update(episode, {'state': 'superseded', 'last_error': 'holder_revived'}, 'superseded')
            return
        if self.server.lane_rulings._authority_loss_evidence(old_seat) is None:
            return
        if episode['state'] == 'pending':
            if episode['next_attempt_at'] and self.clock() < episode['next_attempt_at']:
                return
            if episode['trigger'] == 'auto' and (not self.enabled or self.inhibited()):
                return
            previous = await self.store.submit(lambda conn: [json.loads(r[0]) for r in conn.execute(
                "SELECT detail FROM v2_authority_restore_audit WHERE episode_id=? AND event='attempt_started'", (episode['episode_id'],))])
            for attempt in previous:
                generation = attempt.get('attempt_generation')
                if not generation:
                    continue
                rows = await self.store.submit(lambda conn: [dict(r) for r in conn.execute(
                    'SELECT host,session_name FROM v2_session_generations WHERE generation=?', (generation,))])
                for row in rows:
                    seat = await self.store.fetch_session(row['host'], row['session_name'])
                    if seat and seat.get('status') == 'open' and await self.pane_state(row['session_name']) != 'gone':
                        return
            count = await self.store.submit(lambda conn: conn.execute(
                "SELECT count(*) FROM v2_authority_restore_audit WHERE event='attempt_started' AND created_at>?",
                (self.clock()-86400,)).fetchone()[0])
            if count >= 6:
                await self.notice('authority_restore_rate_limited', f'{stream}:{generation}', {})
                return
            shape = await self.shape()
            primary = await self.primary()
            try:
                content = Path(shape['brief_path']).read_bytes()
                brief = content.decode('utf-8')
            except (OSError, UnicodeError, TypeError, KeyError):
                used = episode['budget_used'] + 1
                errors = json.loads(episode['errors']) + ['brief_unreadable']
                exhausted = used >= 3
                updated = await self.update(episode, {'state': 'exhausted' if exhausted else 'pending',
                    'attempt_seq': episode['attempt_seq'] + 1, 'budget_used': used,
                    'last_error': 'brief_unreadable', 'errors': canonical(errors),
                    'next_attempt_at': None if exhausted else self.clock() + (60 if used == 1 else 180)},
                    'attempt_failed', {'reason': 'brief_unreadable'})
                if updated and exhausted:
                    await self.notice('authority_restore_exhausted', str(episode['episode_id']),
                                      {'episode': episode['episode_id'], 'errors': errors})
                return
            seq = episode['attempt_seq'] + 1
            spawn_key = f"authority-restore:{episode['episode_id']}:{seq}"
            payload = {'type': 'spawn', 'request_id': spawn_key, 'idempotency_key': spawn_key,
                '_auth_context': {'service_authenticated': True, 'service_actor': SERVICE_ACTOR},
                '_session_generation': uuid.uuid4().hex, 'host': self.server.local_host,
                'provider': shape['provider'], 'model': shape.get('model'), 'effort': shape.get('effort'),
                'role': shape.get('role'), 'visibility': shape.get('visibility'), 'spec_id': shape.get('spec_id'),
                'parent_stream_id': primary['stream_id'], 'initial_prompt': brief, 'self_close_on_completion': False,
                'objective': 'Restore the assistant ruling authority'}
            episode = await self.update(episode, {'state': 'spawning', 'attempt_seq': seq,
                'budget_used': episode['budget_used'] + 1, 'spawn_key': spawn_key,
                'attempt_generation': payload['_session_generation'], 'payload': canonical(payload),
                'attempt_started_at': self.clock(), 'next_attempt_at': None}, 'attempt_started',
                {'brief_sha256': hashlib.sha256(content).hexdigest(),
                 'spawn_key': spawn_key, 'attempt_generation': payload['_session_generation'],
                 'brief_changed': hashlib.sha256(content).hexdigest() != shape['brief_sha256']})
            if not episode:
                return
        await self.reconcile(episode)

    async def fail(self, episode: dict, reason: str) -> None:
        used = episode['budget_used']
        errors = json.loads(episode['errors']) + [reason]
        exhausted = used >= 3
        fields = {'state': 'exhausted' if exhausted else 'pending', 'last_error': reason,
                  'errors': canonical(errors), 'next_attempt_at': None if exhausted else self.clock() + (60 if used <= 1 else 180)}
        updated = await self.update(episode, fields, 'attempt_failed', {'reason': reason})
        if updated and exhausted:
            await self.notice('authority_restore_exhausted', str(episode['episode_id']),
                              {'episode': episode['episode_id'], 'errors': errors})

    async def reconcile(self, episode: dict) -> str:
        key = episode['spawn_key']
        payload = json.loads(episode['payload'])
        status = await self.server.spawnctl.spawn_status({'target': key, 'host': self.server.local_host}, self.server.local_host)
        outcomes = [o for o in status.get('outcomes', []) if key in (o.get('idempotency_key'), o.get('request_id'))]
        reservations = [o for o in status.get('reservations', []) if key in (o.get('idempotency_key'), o.get('request_id'))]
        expected_hash = self.server.spawnctl._spawn_payload_hash(payload)
        if any(o.get('request_payload_hash') != expected_hash for o in outcomes):
            log.error('authority restore payload mismatch key=%s', key)
            await self.mark_unresolved(episode, None, 'payload_mismatch')
            return 'still_unresolved'
        delivered = next((o for o in outcomes if o.get('state') == 'delivered'), None)
        failed = next((o for o in outcomes if o.get('state') in ('failed', 'cancelled')), None)
        streams = [o.get('stream_id') for o in outcomes + reservations if o.get('stream_id')]
        rows = await self.store.submit(lambda conn: [dict(r) for r in conn.execute(
            "SELECT host,session_name FROM v2_session_generations WHERE generation=?", (episode['attempt_generation'],))])
        streams += [f"{r['host']}:{r['session_name']}" for r in rows]
        candidates = list(dict.fromkeys(streams))
        successor = next((s for s in candidates if s.startswith(self.server.local_host + ':')), None)
        seat = await self.store.fetch_session(*successor.split(':', 1)) if successor else None
        exact = bool(seat and seat.get('session_generation') == episode['attempt_generation'])
        is_open = exact and seat.get('status') == 'open'
        pane_state = await self.pane_state(successor.partition(':')[2]) if successor and is_open else 'unknown'
        alive = is_open and seat.get('pane_status') == 'pane_alive' and pane_state == 'alive'
        proof = delivered and delivered.get('stream_id') == successor
        if proof and is_open and alive and seat.get('bootstrap_state') in ('ready', 'started'):
            try:
                await self.server.lane_rulings.configure(successor, actor_stream_id=ACTOR,
                    actor_generation=episode['attempt_generation'], service=SERVICE_CAPABILITY,
                    brief_file=(await self.shape())['brief_path'], expected_old=(episode['stream_id'], episode['generation']),
                    frozen_brief_sha=hashlib.sha256(payload['initial_prompt'].encode()).hexdigest(), restore_episode=episode)
            except ValueError:
                log.exception('authority restore bind failed')
                return 'still_unresolved'
            return 'adopted'
        if (proof and exact and (seat.get('status') != 'open' or seat.get('pane_status') == 'pane_dead' or pane_state == 'gone')):
            await self.fail(episode, 'holder_lost')
            return 'attempt_failed'
        if failed and not reservations and not is_open:
            await self.fail(episode, str(failed.get('reason') or 'spawn_failed'))
            return 'attempt_failed'
        if not outcomes and not reservations and not exact and key not in self.sent:
            async with self.server.lane_rulings._authority_lock:
                current_episode = await self.current(episode['stream_id'], episode['generation'])
                current_binding = await self.server.lane_rulings.binding()
                primary = await self.primary()
                if (not current_episode or current_episode['state'] not in ('spawning', 'unresolved') or
                    current_episode['attempt_seq'] != episode['attempt_seq'] or
                    current_binding['stream_id'] != episode['stream_id'] or
                    (current_binding['generation'] and current_binding['generation'] != episode['generation']) or
                    current_binding['state'] != 'unavailable' or not primary):
                    return 'still_unresolved'
                if primary['stream_id'] != payload['parent_stream_id']:
                    await self.fail(current_episode, 'primary_moved')
                    return 'attempt_failed'
                self.sent.add(key)
                try:
                    await self.server.spawnctl.spawn(payload, self.server.local_host)
                except Exception as exc:
                    log.exception('authority restore spawn failed; reconcile durable state')
                    if getattr(exc, 'code', None):
                        after = await self.server.spawnctl.spawn_status({'target': key, 'host': self.server.local_host}, self.server.local_host)
                        current = await self.store.submit(lambda conn: conn.execute(
                            'SELECT 1 FROM v2_session_generations WHERE generation=?', (episode['attempt_generation'],)).fetchone())
                        if not after.get('outcomes') and not after.get('reservations') and not current:
                            await self.fail(episode, str(exc.code))
                            return 'attempt_failed'
            return 'never_admitted'
        await self.mark_unresolved(episode, successor, 'attempt_unresolved')
        return 'still_unresolved'

    async def mark_unresolved(self, episode: dict, successor: str | None, reason: str) -> None:
        if episode['state'] == 'spawning' and self.clock() - episode['attempt_started_at'] >= 180:
            changed = await self.update(episode, {'state': 'unresolved', 'successor': successor,
                'last_error': reason}, 'still_unresolved', {'spawn_key': episode['spawn_key']})
            if changed:
                await self.notice('authority_restore_unresolved', str(episode['episode_id']),
                    {'spawn_key': episode['spawn_key'], 'stream': successor, 'evidence': reason})

    async def manual(self, action: str, *, brief_file: str | None = None) -> dict:
        async with self.lock:
            binding = await self.server.lane_rulings.binding()
            stream = binding['stream_id']
            if not await self.primary() or binding['state'] != 'unavailable':
                raise ValueError('assistant_authority_loss_unproven')
            host, _, name = stream.partition(':')
            if host != self.server.local_host:
                raise ValueError('assistant_authority_nonlocal')
            seat = await self.store.fetch_session(host, name)
            evidence = self.server.lane_rulings._authority_loss_evidence(seat)
            if not evidence:
                raise ValueError('assistant_authority_loss_unproven')
            shape = await self.shape()
            generation = binding['generation'] or ((shape or {}).get('generation') if (shape or {}).get('stream_id') == stream else '')
            if brief_file:
                if not seat:
                    raise ValueError('shape_row_unavailable')
                if seat.get('session_generation') != generation:
                    raise ValueError('shape_generation_conflict')
                primary = await self.primary()
                await self.server.lane_rulings.record_dead_shape(stream, generation, seat, brief_file,
                                                                  recorded_by=primary['stream_id'])
                shape = await self.shape()
            if not shape or shape.get('stream_id') != stream or shape.get('generation') != generation:
                raise ValueError('assistant_authority_shape_ineligible')
            self.loss = (stream, generation)
            episode = await self.current(stream, generation)
            if action == 'retry':
                if not episode:
                    raise ValueError('assistant_authority_no_episode')
                if episode['state'] in ('spawning', 'unresolved'):
                    disposition = await self.reconcile(episode)
                    if disposition != 'attempt_failed':
                        raise ValueError('attempt_unresolved')
                    episode = await self.current(stream, generation)
                if episode['state'] not in ('exhausted', 'pending') or not episode['last_error']:
                    raise ValueError('attempt_unresolved')
                await self.update(episode, {'state': 'pending', 'trigger': 'manual',
                    'budget_epoch': episode['budget_epoch'] + 1, 'budget_used': 0,
                    'next_attempt_at': None}, 'retry')
            else:
                await self.create(stream, generation, 'manual')
        self.schedule()
        return await self.status()
