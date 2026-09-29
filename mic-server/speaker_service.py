"""Policy-checked spoken conversations, sharing the listener's recognition fence."""
from collections import OrderedDict
import os
import re
import math
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
        self.ack_threads = []

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
        for thread in self.ack_threads:
            thread.join(timeout=31)
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

    def open(self, origin, listener, meeting=False, *, acknowledge=True):
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
            item = dict(cid=cid, opened=self.clock(), lines=0, last_line=None, closed=None,
                        origin=origin, listener=listener, late=False, meeting=meeting,
                        timing=dict(capture_ended_at=None, acknowledgement_started_at=None, routed_at=None,
                                    claimed_at=None, delivered_at=None, line_accepted_at=None, first_audio_at=None),
                        delivery_pending=True, ack_done=threading.Event(), ack_epoch=None)
            self.conversations[cid] = item
        if acknowledge:
            self._clip('acknowledgement', item, rules)
        else:
            item['ack_done'].set()
        return cid

    def mark(self, cid, stage, at=None):
        item = self.conversations.get(cid)
        if not item or stage not in item['timing']:
            return False
        stamp = time.time() if at is None else at
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0 or stamp > time.time()+5:
            return False
        if item['timing'][stage] is None:
            item['timing'][stage] = stamp
            if stage == 'delivered_at':
                item['delivery_pending'] = False
            self.emit(dict(subsystem='voice_latency', bug_ref='voice-reply-latency-202609',
                           conversation_id=cid, stage=stage, at=stamp, load=os.getloadavg()))
        return True

    def capture_ended(self, origin, listener, meeting=False):
        cid = self.open(origin, listener, meeting, acknowledge=False)
        if not cid:
            return {}
        self.mark(cid, 'capture_ended_at')
        item = self.conversations[cid]
        item['ack_done'].clear()
        rules = self.rules.snapshot()
        # Capture publishes immediately; playback cannot hold routing or a client claim.
        def acknowledge():
            self._clip('acknowledgement', item, rules)
        self.ack_threads = [thread for thread in self.ack_threads if thread.is_alive()]
        thread = threading.Thread(target=acknowledge, name='capture-ack', daemon=True)
        self.ack_threads.append(thread)
        thread.start()
        return dict(conversation_id=cid, voice_reply=self.contract(cid))

    def contract(self, cid=None):
        limits = self.rules.snapshot()['replies']
        return dict(helper=f"bart-say --conversation-id {cid or '<conversation_id>'} --kind reply --text <literal-line>",
                    **{key:limits[key] for key in ('sentences_per_line','words_per_line','characters_per_line')})

    def _suppressed(self, output, meeting, rules):
        if self.silent and output in rules['modes']['silent']['suppresses']:
            return 'silent_mode'
        if meeting and output in rules['modes']['meeting']['suppresses']:
            return 'meeting_mode'
        return None

    def _play(self, listener, operation, after=None, on_stopped=None):
        if listener is None:
            return self._outcome('refused', 'listener_busy')
        with listener.wake.lock:
            # Both local-action speech and conversational output use this stamp.
            stamp = listener.recognition_stamp()
            own_ack_fence = after is not None and after['ack_epoch'] == stamp[0]
            if not listener.running or listener.state != 'LISTENING' or (stamp[1] and not own_ack_fence):
                return self._outcome('refused', 'listener_busy')
            listener.suppress_recognition_until(self.clock()+36)
        try:
            receipt = operation(time.time()+30)
        except Exception as exc:
            # An uncertain player completion retains the existing bounded fence.
            return self._outcome('refused', 'speaker_error', error=str(exc))
        with listener.wake.lock:
            listener.suppress_recognition_until(self.clock()+1)
            if on_stopped:
                on_stopped(listener.recognition_stamp()[0])
        return self._outcome('spoken', receipt=receipt)

    def _clip(self, group, item, rules):
        try:
            reason = self._suppressed('clips', item['meeting'], rules)
            if reason:
                return self._outcome('suppressed', reason, clip=group)
            def play(deadline):
                def started():
                    if group == 'acknowledgement':
                        self.mark(item['cid'], 'acknowledgement_started_at')
                return self.clips.play(group, rules['clips'][group], deadline, on_start=started)
            stopped = (lambda epoch: item.update(ack_epoch=epoch)) if group == 'acknowledgement' else None
            return self._play(item['listener'], play, on_stopped=stopped)
        finally:
            if group == 'acknowledgement':
                item['ack_done'].set()

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
            sentence_text = re.sub(r'(?<=\d)\.(?=\d)', '', text)
            counts = dict(characters_per_line=len(text), words_per_line=len(text.split()),
                          sentences_per_line=len([s for s in re.split(r'[.!?]+', sentence_text) if s.strip()]))
            for name, measured in counts.items():
                limit = rules['replies'][name]
                if measured > limit:
                    return self._outcome('refused', 'text_too_long' if name == 'characters_per_line' else name,
                                         limit_name=name, limit=limit, measured=measured)
            if kind != 'reply':
                if not kind.startswith('announcement:') or kind[13:] not in rules['announcements']:
                    return self._outcome('refused', 'announcement_not_allowed')
            action = payload.get('action')
            if action is not None and (not isinstance(action,str) or action not in rules['actions']):
                return self._outcome('refused', 'action_not_allowed')
            if item.get('in_flight'):
                return self._outcome('refused', 'speaker_busy')
            item['in_flight'] = True
            reason = self._suppressed('lines', meeting, rules)
            if item['last_line'] is not None and self.clock()-item['last_line'] < rules['replies']['minimum_gap_seconds']:
                reason = reason or 'minimum_gap'
        if reason:
            self.mark(payload['conversation_id'], 'line_accepted_at')
            result = self._outcome('suppressed', reason)
        else:
            item['ack_done'].wait(30)
            cid = payload['conversation_id']
            def render(deadline):
                self.mark(cid, 'line_accepted_at')
                return self.speaker.speak(text, deadline, on_first_frame=lambda: self.mark(cid, 'first_audio_at'))
            result = self._play(listener, render, after=item)
        with self.lock:
            item['in_flight'] = False
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
            if item.get('in_flight'):
                return self._outcome('suppressed', 'line_pending')
            if item['lines']:
                return self._outcome('suppressed', 'line_already_accepted')
            item['closed'] = 'closed_conversation'
            item['meeting'] = meeting
        return self._clip('fallback', item, rules)

    def tick(self):
        pending = []
        with self.lock:
            rules = self.rules.snapshot()
            if not rules['replies']['late_kickoff_enabled']:
                return
            for cid, item in list(self.conversations.items()):
                current, _ = self._conversation(cid, rules)
                if not current or item['lines'] or item['late'] or item.get('in_flight'):
                    continue
                if self.clock()-item['opened'] >= rules['replies']['kickoff_deadline_seconds']:
                    item['late'] = True
                    pending.append(item)
        for item in pending:
            self._clip('late_kickoff', item, rules)

    def reload(self):
        # Validate provisioned clip references before publishing any new policy.
        with self.lock:
            return self.rules.reload(validate_extra=lambda rules: self.clips.load(rules['clips']))

    def status(self):
        # Inference must not hold up HTTP status or mode transitions. Outcome values are replaced atomically.
        return dict(**self.speaker.snapshot(), service_ready=self.ready, service_error=self.error,
                    rules=self.rules.status(), last_outcome=self.last,
                    last_conversation=self.timing_status())

    def timing_status(self):
        if not self.conversations:
            return None
        cid = next(reversed(self.conversations))
        item = self.conversations[cid]
        return dict(conversation_id=cid, timing=dict(item['timing']), delivery_pending=item['delivery_pending'])


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = SpeakerService()
        return _service
