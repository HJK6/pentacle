"""Wake capture abandonment and re-arm, without devices, playback or wall sleeps."""
import queue
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from .test_wake_voice import wake_listener, hear


def setup_capture(monkeypatch):
    monkeypatch.setenv('MIC_LOCAL_ACTIONS', 'false')
    module, listener = wake_listener(monkeypatch)
    now = [100.0]
    listener._clock = lambda: now[0]
    events, effects, logs = [], [], []
    listener.on_event = lambda *event: events.append(event)
    listener._log = logs.append
    listener.on_capture_end = lambda: effects.append('capture_end') or {}
    listener.on_silent = lambda *_: effects.append('silent')
    monkeypatch.setattr(module, 'copy_to_clipboard', lambda *_: effects.append('clipboard'))
    monkeypatch.setattr(listener.voice_actions, 'submit', lambda *_a, **_k: effects.append('submit'))
    monkeypatch.setattr(listener.voice_actions, 'submit_answer', lambda *_: effects.append('answer'))
    listener.vad_model = lambda *_: SimpleNamespace(item=lambda: 0.0)
    return module, listener, now, events, effects, logs


def process_tick(listener, now, at, flowing=False):
    """Run the real process loop through one empty/flowing input iteration."""
    stop = threading.Event()
    original = listener.audio_q

    class TickDone(Exception):
        pass

    class Input:
        first = True

        def get(self, timeout):
            if not self.first:
                stop.set()
                # End before a second iteration: an empty-queue fallback must
                # not hide a missing timeout check on flowing audio (or vice versa).
                raise TickDone
            self.first = False
            now[0] = at
            if flowing:
                return listener.recognition_stamp()[0], np.zeros(1600, dtype=np.float32)
            raise queue.Empty

    listener.audio_q = Input()
    try:
        try:
            listener._process_loop(stop)
        except TickDone:
            pass
    finally:
        listener.audio_q = original


def assert_dropped(listener, events, effects, logs):
    assert listener.state == 'LISTENING'
    assert listener.capture_origin is None
    assert listener.captured_texts == []
    assert listener.wake.claim() is None
    assert effects == []
    assert events.count(('state', 'LISTENING')) == 1
    assert not any(e[0] in ('wake_completed', 'mode_phrase') for e in events)
    assert sum('idle timeout' in line for line in logs) == 1


@pytest.mark.parametrize('flowing', [False, True])
@pytest.mark.parametrize('suppressed', [False, True])
def test_no_segment_expires_on_real_process_loop(monkeypatch, flowing, suppressed):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr')
    if suppressed:
        listener.suppress_recognition_until(float('inf'))
    process_tick(listener, now, 129.99, flowing)
    assert listener.state == 'CAPTURING'
    process_tick(listener, now, 130.01, flowing)
    assert_dropped(listener, events, effects, logs)
    process_tick(listener, now, 200.0, flowing)
    assert events.count(('state', 'LISTENING')) == 1


def test_accepted_speech_refreshes_and_normal_over_completes(monkeypatch):
    module, listener, now, events, effects, logs = setup_capture(monkeypatch)
    assert module.AWAKE_TIMEOUT == 10.0
    hear(listener, 'Hey Zephyr first')
    for at in (116.0, 132.0, 148.0, 164.0):
        process_tick(listener, now, at, True)
        assert listener.state == 'CAPTURING'
        hear(listener, 'more')
    process_tick(listener, now, 193.99)
    assert listener.state == 'CAPTURING'
    hear(listener, 'over')
    assert listener.wake.claim()['text'] == 'first more more more more'
    assert listener.wake.claim() is None
    assert effects == ['capture_end']
    assert not any('idle timeout' in line for line in logs)


@pytest.mark.parametrize('rejected', ['empty', 'short', 'suppressed', 'stale'])
def test_rejected_audio_does_not_refresh(monkeypatch, rejected):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr')
    now[0] = 129.0
    listener._transcribe = lambda _: '' if rejected == 'empty' else 'must not refresh'
    if rejected == 'suppressed':
        listener.suppress_recognition_until(float('inf'))
    listener._handle_utterance(np.ones(100 if rejected == 'short' else 8000),
                              recognition_epoch=-1 if rejected == 'stale' else None)
    process_tick(listener, now, 130.01)
    assert_dropped(listener, events, effects, logs)


def test_repeated_leading_wake_rearms_and_preserves_completed_pending(monkeypatch):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    listener.wake.complete('already completed')
    generation = listener.wake.generation
    hear(listener, 'Hey Zephyr discard this')
    now[0] = 125.0
    hear(listener, 'HEY, Zephyr! keep this')
    assert listener.captured_texts == ['keep this']
    assert effects == []
    assert listener.wake.generation == generation
    assert listener.wake.claim()['text'] == 'already completed'
    process_tick(listener, now, 154.99)
    assert listener.state == 'CAPTURING'
    hear(listener, 'over')
    assert listener.wake.claim()['text'] == 'keep this'
    assert listener.wake.claim() is None
    assert effects == ['capture_end']


@pytest.mark.parametrize('text', ['I said Hey Zephyr yesterday', 'Hey, Zephyrish', 'Hey, Zefyr'])
def test_only_existing_anchored_wake_matcher_rearms(monkeypatch, text):
    _, listener, *_ = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr keep first')
    hear(listener, text)
    hear(listener, 'over')
    assert listener.wake.claim()['text'] == 'keep first ' + text


def test_separate_local_wake_is_absorbed_inside_wake_capture(monkeypatch):
    _, listener, *_ = setup_capture(monkeypatch)
    listener.voice_actions.enabled = True
    listener.voice_actions.policy = 'separate'
    hear(listener, 'Hey Zephyr original')
    hear(listener, 'Hey Pentacle quoted local wake')
    assert listener.capture_origin == 'wake'
    hear(listener, 'over')
    assert listener.wake.claim()['text'] == 'original Hey Pentacle quoted local wake'


def test_full_pending_queue_refuses_rearm_without_extending_deadline(monkeypatch):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr original')
    for i in range(8):
        listener.wake.complete(f'completed{i}')
    now[0] = 125.0
    hear(listener, 'Hey Zephyr refused')
    assert listener.captured_texts == ['original']
    assert listener.wake.snapshot()['error']
    process_tick(listener, now, 130.01)
    assert listener.state == 'LISTENING'
    assert [listener.wake.claim()['text'] for _ in range(8)] == [f'completed{i}' for i in range(8)]
    assert effects == []


def test_shared_local_actions_keep_unbounded_local_capture(monkeypatch):
    _, listener, now, *_ = setup_capture(monkeypatch)
    listener.voice_actions.enabled = True
    listener.voice_actions.policy = 'shared'
    epoch = listener.recognition_stamp()[0]
    hear(listener, 'Hey Zephyr local request')
    assert listener.capture_origin == 'local_action'
    process_tick(listener, now, 10000.0, True)
    assert listener.state == 'CAPTURING'
    assert listener.recognition_stamp()[0] == epoch


def test_expiry_preserves_completed_pending(monkeypatch):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    listener.wake.complete('already completed')
    generation = listener.wake.generation
    hear(listener, 'Hey Zephyr unfinished')
    process_tick(listener, now, 130.01)
    assert listener.state == 'LISTENING'
    assert listener.wake.generation == generation
    assert listener.wake.claim()['text'] == 'already completed'
    assert listener.wake.claim() is None
    assert effects == []


@pytest.mark.parametrize('fresh_wake', [False, True])
def test_expiry_blocks_inflight_asr_from_reopening_or_entering_capture(monkeypatch, fresh_wake):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr abandoned')
    entered, release = threading.Event(), threading.Event()

    def transcribe(_):
        entered.set()
        assert release.wait(3)
        return 'Hey Zephyr stale request'

    listener._transcribe = transcribe
    worker = threading.Thread(target=listener._handle_utterance, args=(np.ones(8000),))
    worker.start()
    try:
        assert entered.wait(2)
        process_tick(listener, now, 130.01)
        assert listener.state == 'LISTENING'
        if fresh_wake:
            hear(listener, 'Hey Zephyr fresh request')
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert listener.captured_texts == (['fresh request'] if fresh_wake else [])
        assert listener.state == ('CAPTURING' if fresh_wake else 'LISTENING')
        assert effects == []
    finally:
        release.set()
        worker.join(3)


def test_expiry_drops_queued_old_audio_but_keeps_post_wake_fifo_audio(monkeypatch):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr abandoned')
    old_epoch = listener.recognition_stamp()[0]
    process_tick(listener, now, 130.01)
    assert listener.state == 'LISTENING'
    epoch = listener.recognition_stamp()[0]
    stop = threading.Event()
    items = iter([(old_epoch, np.full(8000, 1)),
                  (epoch, np.full(8000, 2)),
                  (epoch, np.full(8000, 3))])
    transcribed = []

    class Input:
        def get(self, timeout):
            try:
                return next(items)
            except StopIteration:
                stop.set()
                raise queue.Empty

    listener._utterance_q = Input()
    def transcribe(audio):
        key = int(audio[0])
        transcribed.append(key)
        return {1: 'Hey Zephyr stale', 2: 'Hey Zephyr fresh', 3: 'content'}[key]
    listener._transcribe = transcribe
    listener._transcribe_worker(stop)
    assert transcribed == [2, 3]
    assert listener.captured_texts == ['fresh', 'content']
    hear(listener, 'over')
    assert listener.wake.claim()['text'] == 'fresh content'


@pytest.mark.parametrize('origin', ['manual', 'answer', 'local_action', 'followup'])
def test_other_origins_have_no_idle_timeout_and_keep_over(monkeypatch, origin):
    _, listener, now, events, effects, logs = setup_capture(monkeypatch)
    listener.state = 'CAPTURING'
    listener.capture_origin = origin
    listener.captured_texts = ['original']
    listener.followup_id = 'fixture-followup'
    process_tick(listener, now, 10000.0, True)
    assert listener.state == 'CAPTURING'
    hear(listener, 'Hey Zephyr replacement')
    assert listener.captured_texts == (['replacement'] if origin == 'followup'
                                       else ['original', 'Hey Zephyr replacement'])
    hear(listener, 'over')
    assert listener.state == 'LISTENING'
    assert effects == ({'manual': ['clipboard'], 'answer': ['capture_end'],
                        'local_action': ['capture_end', 'submit'],
                        'followup': ['capture_end']}[origin])
    if origin in ('answer', 'followup'):
        assert listener.wake.claim()['text'] == ('replacement' if origin == 'followup'
                                                else 'original Hey Zephyr replacement')


def test_segment_cap_still_completes_wake_capture(monkeypatch):
    _, listener, *_ = setup_capture(monkeypatch)
    hear(listener, 'Hey Zephyr')
    for i in range(60):
        hear(listener, f'word{i}')
    assert listener.state == 'LISTENING'
    assert listener.wake.claim()['text'] == ' '.join(f'word{i}' for i in range(60))


@pytest.mark.parametrize('flowing', [False, True])
def test_awake_timeout_still_returns_to_listening(monkeypatch, flowing):
    module, listener, now, *_ = setup_capture(monkeypatch)
    listener.state = 'AWAKE'
    listener.awake_since = module.time.time() - 11.0
    process_tick(listener, now, 100.0, flowing)
    assert listener.state == 'LISTENING'
