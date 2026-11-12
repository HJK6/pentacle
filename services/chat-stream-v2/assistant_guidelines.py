"""Supply current assistant guidelines to the bound seats, when enabled."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Reuse the same resolver and front-matter parser as agent-orch spawn.
_AGENT_ORCH = str(Path(__file__).resolve().parents[1] / 'agent-orch')
if _AGENT_ORCH not in sys.path:
    sys.path.insert(0, _AGENT_ORCH)
from agent_orch.role_baseline import resolve_memory_repo_path, _strip_front_matter
from agent_orch.config import load_config

log = logging.getLogger('chat_streamd_v2.assistant_guidelines')
DEFAULT_FRONT_DESK = ('agents/assistant_baseline.md', 'docs/reference/assistant_bart_context.md')
DEFAULT_ADVISOR = ('agents/assistant_authority_baseline.md',)
DELIVERY_TOKEN = object()


def read_set(paths: tuple[str, ...], config: Any = None) -> tuple[str, str] | None:
    try:
        config = config or (SimpleNamespace() if os.environ.get('AGENT_ORCH_MEMORY_REPO') is not None else load_config())
        root = resolve_memory_repo_path(config)
    except (OSError, ValueError) as exc:
        log.warning('guidelines memory repository configuration unavailable: %s', exc)
        return None
    if root is None:
        log.warning('guidelines memory repository unavailable')
        return None
    root = root.resolve()
    parts = []
    for item in paths:
        try:
            path = (root / item).resolve()
            if not path.is_relative_to(root) or Path(item).is_absolute():
                raise ValueError('path outside memory repository')
            parts.append(_strip_front_matter(path.read_text(encoding='utf-8')))
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            log.warning('guidelines file %s unavailable: %s', item, exc)
            return None
    text = ''.join(parts)
    return hashlib.sha256(text.encode('utf-8')).hexdigest(), text


class AssistantGuidelines:
    def __init__(self, store: Any, composite: Any, rulings: Any, comms: Any, *, env=None):
        self.store, self.composite, self.rulings, self.comms = store, composite, rulings, comms
        env = os.environ if env is None else env
        self.enabled = str(env.get('PENTACLE_ASSISTANT_GUIDELINES_ENABLED', '')).lower() in {'1', 'true', 'yes', 'on'}
        self.paths = {
            'front_desk': tuple(filter(None, env.get('PENTACLE_ASSISTANT_GUIDELINES_FRONT_DESK_FILES', os.pathsep.join(DEFAULT_FRONT_DESK)).split(os.pathsep))),
            'advisor': tuple(filter(None, env.get('PENTACLE_ASSISTANT_GUIDELINES_ADVISOR_FILES', os.pathsep.join(DEFAULT_ADVISOR)).split(os.pathsep))),
        }
        self._fd_lock = asyncio.Lock()

    def load(self, seat):
        return read_set(self.paths[seat]) if self.paths[seat] else None

    async def _read_record(self, seat):
        raw = await self.store.get('assistant.guidelines.' + seat)
        try:
            value = json.loads(raw) if raw else {}
            return value if isinstance(value, dict) else {}
        except (ValueError, TypeError):
            return {}

    async def _write_record(self, seat, record):
        # The current record retains the pending tell ID and the previous
        # confirmed delivery. The tell ledger owns delivery history.
        await self.store.put('assistant.guidelines.' + seat, json.dumps(record, sort_keys=True))

    async def _record(self, seat, stream, generation, identity, *, tell_id, status):
        prior = await self._read_record(seat)
        previous = None
        if (prior.get('stream_id'), prior.get('generation')) == (stream, generation):
            if prior.get('delivery_status') == 'delivered':
                previous = {'identity': prior.get('identity'), 'delivered_at': prior.get('delivered_at')}
            else:
                previous = prior.get('previous_delivery')
        await self._write_record(seat, {
            'stream_id': stream, 'generation': generation, 'identity': identity,
            'delivered_at': time.time(), 'delivery_status': status, 'tell_id': tell_id,
            'pending': False, 'previous_delivery': previous,
        })

    async def _reconcile(self, seat, record):
        if record.get('delivery_status') not in {'committed_pending_proof', 'pasted_unsubmitted', 'proof_unavailable'}:
            return record
        get = getattr(self.store, 'get_tell_delivery', None)
        promote = getattr(self.store, 'promote_tell_delivery', None)
        if not callable(get) or not callable(promote):
            return record
        prior = await get(record.get('tell_id'))
        if prior is not None:
            prior = await promote(record['tell_id'], prior['payload_digest'], reconcile=True) or prior
            if (prior.get('reply') or {}).get('delivery_status') == 'delivered':
                record['delivery_status'] = 'delivered'
                await self._write_record(seat, record)
        return record

    async def inspect(self, stream):
        if not self.enabled:
            return None
        fd = await self.composite.binding()
        advisor = await self.rulings.binding()
        for role, binding in (('front_desk', fd), ('advisor', advisor)):
            if stream != binding.get('stream_id') or not binding.get('generation'):
                continue
            record = await self._read_record(role)
            if record.get('delivery_status') in {'committed_pending_proof', 'pasted_unsubmitted', 'proof_unavailable'}:
                get = getattr(self.store, 'get_tell_delivery', None)
                receipt = await get(record.get('tell_id')) if callable(get) else None
                if receipt and (receipt.get('reply') or {}).get('delivery_status') == 'delivered':
                    record = {**record, 'delivery_status': 'delivered'}
            current = self.load(role)
            matching_seat = (record.get('stream_id'), record.get('generation')) == (stream, binding['generation'])
            delivered = (record if record.get('delivery_status') == 'delivered'
                         else record.get('previous_delivery') or {}) if matching_seat else {}
            return {'seat': role, 'identity': delivered.get('identity'),
                    'delivered_at': delivered.get('delivered_at'),
                    'delivery_status': record.get('delivery_status') if matching_seat else None,
                    'matches_current': bool(current and delivered.get('identity') == current[0])}
        return None

    async def is_current(self, seat, stream, generation):
        binding = await (self.composite.binding() if seat == 'front_desk' else self.rulings.binding())
        if seat == 'advisor' and binding.get('state') != 'ready':
            return False
        return (binding.get('stream_id'), binding.get('generation')) == (stream, generation)

    async def front_desk(self, *, stream=None, generation=None, previous=None, current=None):
        if not self.enabled:
            return
        async with self._fd_lock:
            binding = await self.composite.binding()
            target, gen = binding.get('stream_id'), binding.get('generation')
            if not target or not gen:
                if stream is None:
                    await self.store.enqueue_composite_tell(name=self.composite.config.name,
                        tell_id='guidelines:pending:' + self.composite.config.name,
                        from_stream_id='daemon:guidelines', body='guidelines:refresh', request_id='')
                return False
            if stream is not None and (stream, generation) != (target, gen):
                return False
            record = await self._reconcile('front_desk', await self._read_record('front_desk'))
            if ((record.get('stream_id'), record.get('generation')) == (target, gen)
                    and record.get('delivery_status') in {'committed_pending_proof', 'pasted_unsubmitted', 'proof_unavailable'}):
                if previous is not None and current is not None and current * 2 < previous:
                    record['pending'] = True
                    await self._write_record('front_desk', record)
                return False
            loaded = self.load('front_desk')
            if loaded is None:
                if (record.get('stream_id'), record.get('generation')) == (target, gen):
                    record['pending'] = True
                    await self._write_record('front_desk', record)
                return False
            identity, text = loaded
            same = (record.get('stream_id'), record.get('generation')) == (target, gen)
            compacted = previous is not None and current is not None and current * 2 < previous
            needed = not same or record.get('identity') != identity or compacted or record.get('pending')
            if not needed:
                return True
            if same and time.time() - float(record.get('delivered_at') or 0) < 600:
                if not record.get('pending'):
                    record['pending'] = True
                    await self._write_record('front_desk', record)
                return
            from front_desk_digest import WAKE_TOKEN
            tell_id = 'guidelines:' + uuid.uuid4().hex
            try:
                reply = await self.comms.tell({'to_stream_id': target, 'from_stream_id': 'daemon:guidelines',
                    'text': f'[role baseline: assistant]\nguidelines {identity[:12]}\n{text}',
                    'tell_id': tell_id, 'sanitize': True,
                    '_front_desk_wake_token': WAKE_TOKEN, '_guidelines_token': DELIVERY_TOKEN,
                    '_guidelines_role': 'front_desk', '_guidelines_generation': gen})
                if reply.get('delivery_status') in {'delivered', 'committed_pending_proof', 'pasted_unsubmitted', 'proof_unavailable'}:
                    await self._record('front_desk', target, gen, identity, tell_id=tell_id,
                                       status=reply['delivery_status'])
                    return reply['delivery_status'] == 'delivered'
            except Exception:
                log.exception('front desk guidelines delivery failed target=%s', target)

    async def advisor_for_target(self, target):
        if not self.enabled:
            return None
        binding = await self.rulings.binding()
        if binding.get('state') != 'ready' or target != binding.get('stream_id') or not binding.get('generation'):
            return None
        loaded = self.load('advisor')
        if loaded is None:
            return None
        record = await self._reconcile('advisor', await self._read_record('advisor'))
        if ((record.get('stream_id'), record.get('generation')) == (target, binding['generation'])
                and record.get('delivery_status') in {'committed_pending_proof', 'pasted_unsubmitted', 'proof_unavailable'}):
            from sessions import VerbError
            raise VerbError('guidelines_delivery_unconfirmed', 'previous advisor guidelines delivery is unconfirmed')
        identity, text = loaded
        return binding['generation'], identity, f'[role baseline: assistant_authority]\nguidelines {identity[:12]}\n{text}'

    async def advisor_record(self, target, generation, identity, tell_id, status):
        await self._record('advisor', target, generation, identity, tell_id=tell_id, status=status)
