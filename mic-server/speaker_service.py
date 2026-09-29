"""Policy-checked spoken conversations, sharing the listener's recognition fence."""
from collections import OrderedDict
import os
from pathlib import Path
import threading
import time
import uuid

from resident_speaker import ResidentSpeaker, ClipBank
from voice_rules import Rules

BUG_REF = 'voice-speaker-service-202609'


class SpeakerService:
    def __init__(self, speaker=None, rules=None, clips=None, clock=time.monotonic, emit=None):
        self.speaker = speaker or ResidentSpeaker()
        self.rules = rules or Rules()
        self.clips = clips or ClipBank(self.speaker, os.environ.get('MIC_VOICE_CLIP_DIR', str(self.speaker.output_dir/'clips')))
        self.clock = clock
        self.emit = emit or (lambda payload: print(payload, flush=True))
        self.lock = threading.RLock()
        self.conversations = OrderedDict()
        self.last = None
        self.error = None
        self.silent = False
        self.stop_event = threading.Event()
        self.worker = None
        self.ready = False

    def start(self):
        try:
            self.speaker.start()
            self.clips.load(self.rules.snapshot()['clips'])
            self.ready = True
            self.error = None
        except Exception as exc:
            self.error = str(exc)
        if self.worker is None:
            self.worker = threading.Thread(target=self._run, name='voice-conversations', daemon=True)
            self.worker.start()

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=2)
        self.speaker.close()

    def _run(self):
        while not self.stop_event.wait(.1):
            self.tick()

    def _outcome(self, outcome, reason=None, **fields):
        self.last = dict(outcome=outcome, **({'reason': reason} if reason else {}), **fields)
        self.emit(dict(subsystem='voice_speaker', bug_ref=BUG_REF, **self.last))
        return dict(self.last)

    def _conversation(self, cid, rules):
        if not isinstance(cid, str) or cid not in self.conversations:
            return None, 'unknown_conversation'
        conversation = self.conversations[cid]
        if self.clock() >= conversation['opened']+rules['replies']['conversation_ceiling_seconds']:
            conversation['closed'] = 'expired_conversation'
        if conversation['closed']:
            return None, conversation['closed']
        if conversation['lines'] >= rules['replies']['lines_per_conversation']:
            conversation['closed'] = 'exhausted_conversation'
            return None, conversation['closed']
        return conversation, None

    def open(self, origin, listener, meeting=False):
        with self.lock:
            rules = self.rules.snapshot()
            if origin not in rules['origins']:
                self._outcome('refused', 'origin_not_allowed')
                return None
            # Bound the process-local book; never evict an open capability.
            for cid, item in list(self.conversations.items()):
                if item['closed'] or self.clock() >= item['opened']+rules['replies']['conversation_ceiling_seconds']:
                    if len(self.conversations) >= 1024:
                        del self.conversations[cid]
            if len(self.conversations) >= 1024:
                self._outcome('refused', 'conversation_capacity')
                return None
            cid = uuid.uuid4().hex
            item = dict(opened=self.clock(), lines=0, last_line=None, closed=None,
                        origin=origin, listener=listener, late=False, meeting=meeting)
            self.conversations[cid] = item
            self._clip('acknowledgement', item, rules)
            return cid

    def _suppressed(self, output, meeting, rules):
        if self.silent and output in rules['modes']['silent']['suppresses']:
            return 'silent_mode'
        if meeting and output in rules['modes']['meeting']['suppresses']:
            return 'meeting_mode'
        return None

    def _play(self, listener, operation):
        if listener is None:
            return self._outcome('refused', 'listener_busy')
        with listener.wake.lock:
            # Both local-action speech and conversational output use this stamp.
            if not listener.running or listener.state != 'LISTENING' or listener.recognition_stamp()[1]:
                return self._outcome('refused', 'listener_busy')
            listener.suppress_recognition_until(self.clock()+36)
        try:
            receipt = operation(time.time()+30)
        except Exception as exc:
            # An uncertain player completion retains the existing bounded fence.
            return self._outcome('refused', 'speaker_error', error=str(exc))
        listener.suppress_recognition_until(self.clock()+1)
        return self._outcome('spoken', receipt=receipt)

    def _clip(self, group, item, rules):
        reason = self._suppressed('clips', item['meeting'], rules)
        if reason:
            return self._outcome('suppressed', reason, clip=group)
        return self._play(item['listener'], lambda deadline: self.clips.play(group, rules['clips'][group], deadline))

    def speak(self, payload, listener, meeting=False):
        with self.lock:
            if not isinstance(payload, dict):
                return self._outcome('refused', 'invalid_request')
            final = payload.get('final', False)
            text, kind = payload.get('text'), payload.get('kind')
            if type(final) is not bool or not isinstance(text,str) or not text.strip() or not isinstance(kind,str):
                return self._outcome('refused', 'invalid_request')
            rules = self.rules.snapshot()
            item, reason = self._conversation(payload.get('conversation_id'), rules)
            if reason:
                return self._outcome('refused', reason)
            if item['origin'] not in rules['origins']:
                return self._outcome('refused', 'origin_not_allowed')
            if len(text) > rules['replies']['characters_per_line']:
                return self._outcome('refused', 'text_too_long')
            if kind != 'reply':
                if not kind.startswith('announcement:') or kind[13:] not in rules['announcements']:
                    return self._outcome('refused', 'announcement_not_allowed')
            action = payload.get('action')
            if action is not None and (not isinstance(action,str) or action not in rules['actions']):
                return self._outcome('refused', 'action_not_allowed')
            reason = self._suppressed('lines', meeting, rules)
            if item['last_line'] is not None and self.clock()-item['last_line'] < rules['replies']['minimum_gap_seconds']:
                reason = reason or 'minimum_gap'
            if reason:
                result = self._outcome('suppressed', reason)
            else:
                result = self._play(listener, lambda deadline: self.speaker.speak(text, deadline))
            if result['outcome'] in ('spoken', 'suppressed'):
                item['lines'] += 1
                item['last_line'] = self.clock()
                if final:
                    item['closed'] = 'closed_conversation'
                elif item['lines'] >= rules['replies']['lines_per_conversation']:
                    item['closed'] = 'exhausted_conversation'
            return result

    def turn_ended(self, cid, meeting=False):
        with self.lock:
            rules = self.rules.snapshot()
            item, reason = self._conversation(cid, rules)
            if reason:
                return self._outcome('suppressed', reason)
            if item['lines']:
                return self._outcome('suppressed', 'line_already_accepted')
            item['closed'] = 'closed_conversation'
            item['meeting'] = meeting
            return self._clip('fallback', item, rules)

    def tick(self):
        with self.lock:
            rules = self.rules.snapshot()
            for cid, item in list(self.conversations.items()):
                current, _ = self._conversation(cid, rules)
                if not current or item['lines'] or item['late']:
                    continue
                if self.clock()-item['opened'] >= rules['replies']['kickoff_deadline_seconds']:
                    item['late'] = True
                    self._clip('late_kickoff', item, rules)

    def reload(self):
        # Validate provisioned clip references before publishing any new policy.
        with self.lock:
            return self.rules.reload(validate_extra=lambda rules: self.clips.load(rules['clips']))

    def status(self):
        # Inference must not hold up HTTP status or mode transitions. Outcome values are replaced atomically.
        return dict(**self.speaker.snapshot(), service_ready=self.ready, service_error=self.error,
                    rules=self.rules.status(), last_outcome=self.last)


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = SpeakerService()
        return _service
