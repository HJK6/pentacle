"""Policy-checked spoken conversations, sharing the listener's recognition fence."""
from collections import OrderedDict
import json
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
        self.silent_source = None
        self.silent_changed_at = None
        # Silent mode persists across a service restart (spec: the flag survives a restart).
        self.silent_state_path = Path(os.environ['MIC_VOICE_SILENT_STATE']) if os.environ.get('MIC_VOICE_SILENT_STATE') else (self.speaker.output_dir/'silent_state.json')
        self._load_silent()
        self.stop_event = threading.Event()
        self.worker = None
        self.ready = False
        self.ack_threads = []

    def _load_silent(self):
        # A missing or malformed state file leaves silent mode off; never fail startup on it.
        try:
            data = json.loads(self.silent_state_path.read_text())
            if isinstance(data, dict) and type(data.get('silent')) is bool:
                self.silent = data['silent']
                self.silent_source = data.get('source') if isinstance(data.get('source'), str) else None
                self.silent_changed_at = data.get('changed_at') if type(data.get('changed_at')) in (int, float) else None
        except (OSError, ValueError, TypeError):
            pass

    def _save_silent(self):
        try:
            self.silent_state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.silent_state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(dict(silent=self.silent, source=self.silent_source, changed_at=self.silent_changed_at)))
            temporary.replace(self.silent_state_path)
        except OSError:
            pass

    def set_silent(self, on, source, listener=None, meeting=False):
        # Silent mode is independent of mic mode and meeting mode; neither sets the other.
        if source not in ('voice', 'web', 'mobile', 'restore'):
            return self._outcome('refused', 'invalid_source')
        on = bool(on)
        with self.lock:
            self.silent = on
            self.silent_source = source
            self.silent_changed_at = time.time()
            self._save_silent()
        # Turning it off plays the confirmation clip; turning it on plays none.
        if not on and listener is not None:
            self._play_clip('silent_off', listener, meeting)
        return dict(silent=self.silent, source=self.silent_source, changed_at=self.silent_changed_at)

    def _play_clip(self, group, listener, meeting=False):
        # A standalone clip (mode confirmation) with no backing conversation.
        rules = self.rules.snapshot()
        item = dict(cid=None, listener=listener, meeting=meeting)
        return self._clip(group, item, rules)

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
        self._maybe_expire_window(conversation)
        if self.clock() >= conversation['opened']+rules['replies']['conversation_ceiling_seconds']:
            conversation['closed'] = 'expired_conversation'
        if conversation['closed']:
            return None, conversation['closed']
        # Allowance counts accepted lines since the last delivered answer. Reaching the
        # limit refuses further lines; closure is deferred only while a question window is open.
        if conversation['lines_since_answer'] >= rules['replies']['lines_per_conversation']:
            if not conversation['window']:
                conversation['closed'] = 'exhausted_conversation'
            return None, 'exhausted_conversation'
        return conversation, None

    def _window_event(self, event, conversation_id, line_id, **extra):
        # Answer-window telemetry, keyed to this spec so each journey is auditable.
        self.emit(dict(subsystem='voice.answer_window', bug_ref='spec_pentacle__voice_answer_window_2026_09',
                       event=event, conversation_id=conversation_id, line_id=line_id, **extra))

    def _maybe_expire_window(self, conversation):
        # A window that ends with no answer closes the conversation only if the allowance
        # limit had already been reached when it opened (deferred closure); otherwise the
        # conversation stays open for a wake-word turn.
        window = conversation.get('window')
        if window and window['deadline'] is not None and self.clock() >= window['deadline'] and not window['answered']:
            conversation['window'] = None
            if window['close_on_expire'] and not conversation['closed']:
                conversation['closed'] = 'exhausted_conversation'
            self._window_event('expired', conversation['cid'], window['line_id'],
                               closed=conversation['closed'] if window['close_on_expire'] else None)

    def _other_window_open(self, cid):
        # True when a conversation OTHER than cid already holds a live answer window
        # (one Bart answer window service-wide at a time).
        for other_cid, item in self.conversations.items():
            if other_cid == cid:
                continue
            self._maybe_expire_window(item)
            if item.get('window'):
                return True
        return False

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
                        lines_since_answer=0, questions=0, last_line_id=None, window=None,
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

    def _clip(self, group, item, rules, after=None):
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
            return self._play(item['listener'], play, after=after, on_stopped=stopped)
        finally:
            if group == 'acknowledgement':
                item['ack_done'].set()

    def speak(self, payload, listener, meeting=False):
        with self.lock:
            if not isinstance(payload, dict):
                return self._outcome('refused', 'invalid_request')
            final = payload.get('final', False)
            expects_answer = payload.get('expects_answer', False)
            text, kind = payload.get('text'), payload.get('kind')
            if type(final) is not bool or type(expects_answer) is not bool or not isinstance(text,str) or not text.strip() or not isinstance(kind,str):
                return self._outcome('refused', 'invalid_request')
            # A line cannot be both final and a question; this renders nothing and opens no window.
            if final and expects_answer:
                return self._outcome('refused', 'final_and_expects_answer')
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
            # A question is refused once the per-conversation question ceiling is reached.
            if expects_answer and item['questions'] >= rules['replies']['questions_per_conversation']:
                return self._outcome('refused', 'questions_per_conversation',
                                     limit=rules['replies']['questions_per_conversation'], measured=item['questions'])
            if item.get('in_flight'):
                return self._outcome('refused', 'speaker_busy')
            item['in_flight'] = True
            lid = uuid.uuid4().hex
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
            # A question's own listening tone follows immediately, inside the 1s post-line
            # self-fence; record this line's post-render fence epoch so that tone plays
            # through its own fence (same own-fence credential used for ack -> reply).
            record_fence = (lambda epoch: item.update(ack_epoch=epoch)) if expects_answer else None
            result = self._play(listener, render, after=item, on_stopped=record_fence)
        open_window = False
        window_refused = None
        with self.lock:
            item['in_flight'] = False
            if result['outcome'] in ('spoken', 'suppressed'):
                item['lines'] += 1
                item['lines_since_answer'] += 1
                item['last_line'] = self.clock()
                item['last_line_id'] = lid
                result['line_id'] = lid
                if expects_answer:
                    item['questions'] += 1
                reached = item['lines_since_answer'] >= rules['replies']['lines_per_conversation']
                if final:
                    item['closed'] = 'closed_conversation'
                elif expects_answer and result['outcome'] == 'spoken':
                    if self._other_window_open(item['cid']):
                        # One Bart answer window service-wide at a time; the second is refused.
                        window_refused = 'answer_window_busy'
                        if reached:
                            item['closed'] = 'exhausted_conversation'
                    else:
                        # A spoken question defers closure until its window ends.
                        item['window'] = dict(line_id=lid, opens_at=None, deadline=None,
                                              close_on_expire=reached, answered=False, tone=rules['replies']['listening_tone'])
                        open_window = True
                elif reached:
                    item['closed'] = 'exhausted_conversation'
        if open_window:
            la = getattr(listener, 'voice_actions', None)
            if la is not None and callable(getattr(la, 'waiting', None)) and la.waiting():
                # Never overlap a local-action answer window; the second window is refused.
                with self.lock:
                    if item.get('window') and item['window']['line_id'] == lid:
                        item['window'] = None
                window_refused = 'local_action_window'
            else:
                # After the question finishes playing, play the listening tone and open the window.
                # The tone carries this line's own-fence credential (after=item) so it is not
                # refused as listener_busy by the question's own 1s post-line recognition fence.
                tone_played = False
                if rules['replies']['listening_tone']:
                    tone_played = self._clip('listening_tone', item, rules, after=item)['outcome'] == 'spoken'
                with self.lock:
                    window = item.get('window')
                    if window and window['line_id'] == lid:
                        window['opens_at'] = self.clock()
                        window['deadline'] = self.clock()+rules['replies']['answer_window_seconds']
                self._window_event('opened', payload['conversation_id'], lid,
                                   tone=rules['replies']['listening_tone'], tone_played=tone_played,
                                   window_seconds=rules['replies']['answer_window_seconds'])
        if window_refused:
            self._window_event('refused', payload['conversation_id'], lid, reason=window_refused)
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

    def answer_window(self, cid):
        # The open window for a conversation, if any, after cleaning a lapsed one.
        with self.lock:
            item = self.conversations.get(cid)
            if not item:
                return None
            self._maybe_expire_window(item)
            window = item.get('window')
            if not window:
                return None
            return dict(conversation_id=cid, line_id=window['line_id'],
                        ready=window['opens_at'] is not None and self.clock() >= window['opens_at'],
                        expires_in=max(0, round((window['deadline'] or self.clock())-self.clock())))

    def answer_delivered(self, cid, line_id):
        # A delivered answer sets the allowance count to zero and keeps the conversation open.
        with self.lock:
            item = self.conversations.get(cid)
            if not item:
                return False
            self._maybe_expire_window(item)
            window = item.get('window')
            if not window or window['line_id'] != line_id or window['opens_at'] is None or self.clock() < window['opens_at']:
                return False
            window['answered'] = True
            item['window'] = None
            item['lines_since_answer'] = 0
        self._window_event('answered', cid, line_id)
        return True

    def _ready_window_conversation(self):
        for cid in reversed(self.conversations):
            item = self.conversations[cid]
            self._maybe_expire_window(item)
            window = item.get('window')
            if window and window['opens_at'] is not None and self.clock() >= window['opens_at']:
                return cid, item, window
        return None

    def answer_captured(self, meeting=False):
        """The operator answered an open Bart question without the wake word.

        Returns delivery metadata (conversation_id + answer_to=line_id) for the wake
        claim, acknowledges on the same conversation as any routed request, resets the
        allowance and closes the window, keeping the conversation open. Returns None when
        no window is ready, so the caller falls back to a fresh room-mic request.
        """
        with self.lock:
            found = self._ready_window_conversation()
            if not found:
                return None
            cid, item, window = found
            line_id = window['line_id']
            window['answered'] = True
            item['window'] = None
            item['lines_since_answer'] = 0
        self._window_event('answered', cid, line_id)
        # Capture publishes immediately; the acknowledgement must not hold the claim.
        self.mark(cid, 'capture_ended_at')
        item['ack_done'].clear()
        rules = self.rules.snapshot()
        def acknowledge():
            self._clip('acknowledgement', item, rules)
        self.ack_threads = [thread for thread in self.ack_threads if thread.is_alive()]
        thread = threading.Thread(target=acknowledge, name='answer-ack', daemon=True)
        self.ack_threads.append(thread)
        thread.start()
        self.mark(cid, 'routed_at')
        return dict(conversation_id=cid, answer_to=line_id)

    def cancel_answer_window(self):
        """A fresh wake word closes an open answer window; the conversation stays as is."""
        with self.lock:
            found = self._ready_window_conversation()
            if not found:
                # Also drop a not-yet-ready window (tone still playing) on a fresh wake.
                for item in self.conversations.values():
                    if item.get('window'):
                        item['window'] = None
                        return True
                return False
            found[1]['window'] = None
            return True

    def has_open_window(self):
        # One Bart answer window at a time, and never overlapping a local-action window.
        with self.lock:
            for item in self.conversations.values():
                self._maybe_expire_window(item)
                if item.get('window'):
                    return True
            return False

    def answer_window_status(self):
        # Surfaced to /status and the mic panel: whether the microphone is waiting for an answer to Bart.
        with self.lock:
            for cid in reversed(self.conversations):
                item = self.conversations[cid]
                self._maybe_expire_window(item)
                window = item.get('window')
                if window:
                    return dict(waiting=True, conversation_id=cid, line_id=window['line_id'],
                                ready=window['opens_at'] is not None and self.clock() >= window['opens_at'],
                                expires_in=max(0, round((window['deadline'] or self.clock())-self.clock())))
            return dict(waiting=False)

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
                    silent=self.silent, silent_source=self.silent_source, silent_changed_at=self.silent_changed_at,
                    answer_window=self.answer_window_status(),
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
