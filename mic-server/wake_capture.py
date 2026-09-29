"""Bounded, process-local wake completions; claims never automatically requeue."""
from collections import deque
import threading
import uuid


class WakeCaptures:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.lock = threading.RLock()
        self.generation = str(uuid.uuid4())
        self.pending = deque()
        self.last_claim = None
        self.error = None

    def invalidate(self):
        with self.lock:
            self.generation = str(uuid.uuid4())
            self.pending.clear()
            self.error = None

    def can_start(self):
        with self.lock:
            if len(self.pending) >= 8:
                self.error = 'Wake queue full; deliver pending messages before speaking another.'
                return False
            self.error = None
            return True

    def complete(self, text, action=None, capture_id=None, metadata=None):
        with self.lock:
            if text.strip():
                if len(self.pending) >= 8:
                    self.error = "Wake queue full; repeat the request after delivery resumes."
                    return False
                # One listener capture at a time; admission reserves its capacity.
                item = dict(id=capture_id or str(uuid.uuid4()), generation=self.generation, text=text.strip())
                item.update(metadata or {})
                if item.get('conversation_id'):
                    from speaker_service import get_service
                    get_service().mark(item['conversation_id'], 'routed_at')
                if action is not None:
                    item['action'] = action
                self.pending.append(item)
                return True

    def claim(self, actions_version=0):
        with self.lock:
            if not self.pending:
                return None
            if self.pending[0].get('action') and (type(actions_version) is not int or actions_version not in (1, 2) or self.pending[0]['action'].get('version', 1) > actions_version):
                self.error = 'Refresh the web client to deliver local actions.'
                return None
            item = self.pending.popleft()
            self.last_claim = dict(item, outcome='claimed; chat delivery unconfirmed')
            self.error = None
            return dict(self.last_claim)

    def snapshot(self):
        with self.lock:
            return dict(enabled=self.enabled, generation=self.generation,
                        pending_count=len(self.pending), pending_ids=[item['id'] for item in self.pending],
                        error=self.error, last_claim={key: value for key, value in (self.last_claim or {}).items() if key != 'text'})

    def review(self):
        with self.lock:
            return dict(self.last_claim) if self.last_claim else None

    def note_action_outcome(self, capture_id, outcome, receipt=None):
        with self.lock:
            if self.last_claim and self.last_claim['id'] == capture_id:
                self.last_claim['action_outcome'] = outcome
                self.last_claim['action_receipt'] = receipt
