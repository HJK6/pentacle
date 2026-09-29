"""One bounded worker for local routing, quotes and speech; no model-owned executor."""
import os
import queue
import threading
import time
import uuid

from local_actions import MODEL, PROMPT_VERSION, classify, classify_followup, market_quote, warm_model
from voice_speaker import speak


class VoiceActions:
    def __init__(self, listener, classifier=classify, quoter=market_quote, speaker=speak, warmer=warm_model, followup_classifier=classify_followup):
        self.listener = listener
        self.enabled = os.environ.get('MIC_LOCAL_ACTIONS', '').lower() in ('1', 'true', 'yes')
        self.policy = os.environ.get('MIC_LOCAL_ACTIONS_WAKE', 'separate')
        if self.policy not in ('separate', 'shared'):
            raise ValueError('MIC_LOCAL_ACTIONS_WAKE must be separate or shared')
        self.classifier, self.quoter, self.speaker = classifier, quoter, speaker
        self.queue = queue.Queue(maxsize=4)
        self.last = None
        self.lock = threading.RLock()
        self.worker = None
        self.warmer = warmer
        self.warm_at = None
        self.warm_generation = None
        self.warm_error = None
        self.playback_until = 0.0
        self.pending = None
        self.asking = False
        self.revision = 0
        self.followup_classifier = followup_classifier
        self.followup_seconds = float(os.environ.get('MIC_LOCAL_FOLLOWUP_SECONDS', '60'))
        if not 10 <= self.followup_seconds <= 180:
            raise ValueError('MIC_LOCAL_FOLLOWUP_SECONDS must be between 10 and 180')

    def start(self):
        if self.enabled and self.worker is None:
            self.worker = threading.Thread(target=self._run, name='local-voice-actions', daemon=True)
            self.worker.start()

    def submit(self, text, generation, metadata=None):
        item = dict(id=uuid.uuid4().hex, generation=generation, text=text, revision=self.revision, metadata=metadata or {})
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            self._status(item, 'error', error='Local action queue is full; repeat later.')
            return False

    def reset_dialogue(self):
        # All caller-visible dialogue transitions share the existing wake lock.
        with self.listener.wake.lock:
            if self.pending is not None or self.asking or getattr(self.listener, 'capture_origin', None) == 'followup':
                self.revision += 1
            self.asking = False
            self.pending = None
            if getattr(self.listener, 'capture_origin', None) == 'followup':
                self.listener.capture_origin = None
                self.listener.captured_texts = []
                self.listener.state = 'LISTENING'

    def expire(self):
        with self.listener.wake.lock:
            pending = self.pending
            if pending and (not self.valid(pending) or (pending['state'] == 'waiting' and time.monotonic() >= pending['deadline'])):
                self.reset_dialogue()
                self._status(pending, 'expired')

    def waiting(self):
        self.expire()
        pending = self.pending
        return pending if pending and pending['state'] == 'waiting' and time.monotonic() >= pending['opens_at'] else None

    def submit_answer(self, text, pending_id):
        with self.listener.wake.lock:
            pending = self.waiting()
            if not pending or pending['id'] != pending_id or not text.strip() or len(text) > 4000:
                return False
            pending['state'] = 'in_flight'
            pending['answers'] += 1
            item = dict(pending, text=text, followup=True)
            try:
                self.queue.put_nowait(item)
                return True
            except queue.Full:
                self.reset_dialogue()
                self._status(item, 'error', error='Local action queue is full; repeat later.')
                return False

    def _ask(self, item, decision, prior=None):
        # Candidate state stays worker-local until stopped playback is established.
        with self.listener.wake.lock:
            if not self.valid(item):
                return
            self.pending = None
            if prior and not decision.get('progress') and time.monotonic() >= prior['deadline']:
                self._status(item, 'expired')
                return
            self.asking = True
        try:
            spoken = self._say(item, decision['reply'])
        finally:
            self.asking = False
        if not spoken:
            return
        with self.listener.wake.lock:
            if not self.valid(item):
                return
            deadline = self.playback_until+self.followup_seconds
            if prior and not decision.get('progress'):
                deadline = prior['deadline']
            if deadline <= self.playback_until:
                self._status(item, 'expired')
                return
            self.pending = dict(id=item['id'], generation=item['generation'], revision=item.get('revision', self.revision),
                                original=prior['original'] if prior else item['text'], spawn=decision['spawn'],
                                question=decision['reply'], answers=prior['answers'] if prior else 0,
                                opens_at=self.playback_until, deadline=deadline, state='waiting')
            self._status(item, 'waiting', question=decision['reply'])

    def _status(self, item, state, **fields):
        with self.lock:
            previous = self.last if self.last and self.last['id'] == item['id'] else {}
            self.last = dict(previous, id=item['id'], generation=item['generation'], state=state, at=time.time(), **fields)
        self.listener._emit('local_action', self.last)

    def valid(self, item):
        return self.listener.running and item['generation'] == self.listener.wake.generation and item.get('revision', self.revision) == self.revision

    def _warm_if_due(self, now):
        if not self.enabled or not self.listener.running:
            return
        generation = self.listener.wake.generation
        if self.warm_at is not None and generation == self.warm_generation and now-self.warm_at < 300:
            return
        self.warm_at, self.warm_generation = now, generation
        try:
            self.warmer()
            self.warm_error = None
        except Exception:
            self.warm_error = 'Local model warm-up failed; requests will retry inference.'

    def _run(self):
        while True:
            self._warm_if_due(time.monotonic())
            try:
                item = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            if not self.valid(item):
                continue
            try:
                if 'speech' in item:
                    self._say(item, item['speech'])
                else:
                    self._process(item)
            except Exception as exc:
                self._status(item, 'error', error=str(exc)[:300])

    def _process(self, item):
        self._status(item, 'classifying')
        start = time.monotonic()
        try:
            decision = self.followup_classifier(item['text'], dict(item['spawn'], original=item['original'], question=item['question'])) if item.get('followup') else self.classifier(item['text'])
        except ValueError as exc:
            self._status(item, 'validation_error', subsystem='voice_actions', bug_ref='voice-physical-20260913', validation_error=str(exc)[:240])
            decision = dict(route='clarify', reply=item['question'], spawn=item['spawn'], progress=False) if item.get('followup') else dict(route='clarify', reply=str(exc)[:240])
        except Exception:
            decision = dict(route='clarify', reply='The local model is unavailable. Please try the complete request again.')
        if not self.valid(item):
            self._status(item, 'cancelled')
            return
        route = decision['route']
        if item.get('followup') and route not in ('spawn_agent', 'clarify', 'unrelated'):
            route = 'unrelated'
        if item.get('followup'):
            if route == 'unrelated':
                with self.listener.wake.lock:
                    if self.valid(item):
                        self.pending = dict(item, state='waiting') if item['answers'] < 2 else None
                        self._status(item, 'waiting' if self.pending else 'dropped', route='unrelated')
                return
            if route != 'spawn_agent' and item['answers'] >= 2:
                with self.listener.wake.lock:
                    self.pending = None
                self._say(item, 'Please start again with the full request.')
                return
        self._status(item, 'routed', route=route, latency_ms=round((time.monotonic()-start)*1000))
        if route in ('spawn_agent', 'bart'):
            with self.listener.wake.lock:
                if not self.valid(item):
                    return
                self.pending = None
                action = dict(version=2, **{k: v for k, v in decision.items() if k not in ('progress', 'sources')}) if route == 'spawn_agent' else None
                accepted = self.listener.wake.complete(item.get('original', item['text']), action=action, capture_id=item['id'], metadata=item.get('metadata'))
                self._status(item, 'pending_client' if accepted else 'error', route=route, field_sources=decision.get('sources'), error=None if accepted else self.listener.wake.error)
        elif route == 'market_quote':
            self._status(item, 'fetching_quotes', route=route)
            quotes = []
            for symbol in decision['symbols']:
                if not self.valid(item):
                    return
                try:
                    quotes.append(self.quoter(symbol))
                except Exception:
                    self._say(item, f'I could not retrieve a verified recent quote for {symbol}. Please try again later.')
                    return
            self._status(item, 'quotes_ready', route=route, quotes=quotes)
            self._say(item, ' '.join(q['speech'] for q in quotes))
        elif decision.get('spawn'):
            self._ask(item, decision, prior=item if item.get('followup') else None)
        else:
            if item.get('followup'):
                with self.listener.wake.lock:
                    self.pending = None
            self._say(item, decision['reply'])

    def _say(self, item, text):
        # Never speak over an operator's manual/next capture. No lock held while waiting.
        # A lost receipt can leave the prior remote group playing until its deadline.
        # Wait through that bound before permitting any later receipt to shorten a fence.
        while self.valid(item) and time.monotonic() < self.playback_until:
            time.sleep(.05)
        idle_deadline = time.monotonic()+10
        while self.valid(item) and (self.listener.state != 'LISTENING' or self.listener.recognition_stamp()[1]) and time.monotonic() < idle_deadline:
            time.sleep(.05)
        with self.listener.wake.lock:
            if not self.valid(item):
                return
            if self.listener.state != 'LISTENING' or self.listener.recognition_stamp()[1]:
                self._status(item, 'error', error='Speech deferred because the microphone is busy.')
                return
            deadline = time.time()+30
            self.playback_until = time.monotonic()+36
            self.listener.suppress_recognition_until(self.playback_until)
        self._status(item, 'speaking', response=text)
        try:
            receipt = self.speaker(text, deadline)
        except Exception:
            # Even a disconnected SSH process may leave remote playback alive.
            # The remote UTC deadline and skew allowance remain the fence.
            raise
        else:
            self.playback_until = time.monotonic()+1
            self.listener.suppress_recognition_until(self.playback_until)
            if isinstance(receipt, dict) and receipt.get('suppressed'):
                self._status(item, 'suppressed', response=text, reason=receipt.get('reason'))
                return False
            self._status(item, 'spoken', response=text)
            return True

    def outcome(self, capture_id, generation, outcome, receipt=None):
        # Match the actual destructive claim; client input cannot speak arbitrary text.
        claim = self.listener.wake.review()
        if not claim or claim.get('id') != capture_id or claim.get('generation') != generation or not claim.get('action'):
            raise ValueError('Unknown local action claim')
        allowed = {'spawned': 'The agent has started.', 'queued': 'The agent is queued to start.',
                   'unconfirmed': 'The spawn is unconfirmed. Check Pentacle before repeating the request.',
                   'unavailable': 'That agent configuration is unavailable. Repeat the full request with a supported model and host.'}
        if outcome not in allowed:
            raise ValueError('Unknown spawn outcome')
        if claim.get('action_outcome'):
            return False
        if receipt is not None:
            import json
            if not isinstance(receipt, dict) or set(receipt)-{'stream_id','state','idempotency_key','host','model','effort','effort_source'} or len(json.dumps(receipt)) > 1200 or receipt.get('idempotency_key') != 'voice:'+capture_id:
                raise ValueError('Invalid spawn receipt')
        self.listener.wake.note_action_outcome(capture_id, outcome, receipt)
        if self.valid(dict(generation=generation)):
            try:
                self.queue.put_nowait(dict(id=capture_id, generation=generation, speech=allowed[outcome]))
            except queue.Full:
                return False
        return True

    def snapshot(self):
        with self.listener.wake.lock:
            self.expire()
            p = self.pending
            pending = dict(id=p['id'], state=p['state'], ready=bool(self.listener.running and p['state'] == 'waiting' and time.monotonic() >= p['opens_at'] and not self.listener.recognition_stamp()[1]), missing=p['spawn']['missing'], question=p['question'],
                           answers=p['answers'], expires_in=max(0, round(p['deadline']-time.monotonic()))) if p else None
        with self.lock:
            return dict(enabled=self.enabled, version=1, wake_policy=self.policy, model=MODEL,
                        prompt_version=PROMPT_VERSION, pending=pending, warm_error=self.warm_error, queued=self.queue.qsize(), last=dict(self.last) if self.last else None)
