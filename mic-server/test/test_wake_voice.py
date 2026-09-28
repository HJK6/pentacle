import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from .test_voice_capture_finish import listener_module


def wake_listener(monkeypatch):
    monkeypatch.setenv('MIC_WAKE_CAPTURE', 'true')
    module = listener_module(monkeypatch)
    listener = module.AlwaysOnListener()
    listener.on_event = lambda *args: None
    return module, listener


def hear(listener, text):
    listener._transcribe = lambda audio: text
    listener._handle_utterance(np.ones(8000))


def test_natural_wake_tail_over_and_no_clipboard(monkeypatch):
    module, listener = wake_listener(monkeypatch)
    copied = []
    monkeypatch.setattr(module, 'copy_to_clipboard', copied.append)
    hear(listener, 'HEY, Bart! Please help me plan today.')
    assert listener.state == 'CAPTURING'
    assert listener.capture_origin == 'wake'
    hear(listener, 'Go over this carefully.')
    hear(listener, 'Over.')
    claim = listener.wake.claim()
    assert claim['text'] == 'Please help me plan today. Go over this carefully.'
    assert copied == []
    assert listener.wake.claim() is None


@pytest.mark.parametrize('text', ['noise in the room', 'I said Hey Bart yesterday', 'hey bar', 'Hey Bartender', 'Hey, Bort!', 'Bart start copying'])
def test_room_speech_is_not_captured_or_logged(monkeypatch, text):
    _, listener = wake_listener(monkeypatch)
    events = []
    listener.on_event = lambda *args: events.append(args)
    hear(listener, text)
    assert listener.state == 'LISTENING'
    assert all(text not in str(event) for event in events)


def test_manual_capture_is_separate_and_cannot_replace_wake(monkeypatch):
    module, listener = wake_listener(monkeypatch)
    copied = []
    monkeypatch.setattr(module, 'copy_to_clipboard', copied.append)
    hear(listener, 'Hey Bart first message')
    with pytest.raises(ValueError, match='wake'):
        listener._execute_command('start_copy')
    hear(listener, 'over')
    listener._execute_command('start_copy')
    hear(listener, 'manual words')
    hear(listener, 'over')
    assert copied == ['manual words']
    assert listener.wake.claim()['text'] == 'first message'


def test_full_queue_refuses_capture_and_claims_are_atomic(monkeypatch):
    _, listener = wake_listener(monkeypatch)
    for i in range(8):
        hear(listener, f'Hey Bart message {i}')
        hear(listener, 'over')
    hear(listener, 'Hey Bart ninth message')
    assert listener.state == 'LISTENING'
    assert listener.wake.snapshot()['error']
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: listener.wake.claim(), range(12)))
    claims = [item for item in results if item]
    assert len(claims) == 8
    assert len({item['id'] for item in claims}) == 8
    assert listener.wake.snapshot()['pending_count'] == 0
    assert listener.wake.last_claim in claims


def test_off_invalidates_pending_and_delayed_asr_across_on(monkeypatch):
    _, listener = wake_listener(monkeypatch)
    hear(listener, 'Hey Bart queued')
    hear(listener, 'over')
    generation = listener.wake.snapshot()['generation']
    entered, release = threading.Event(), threading.Event()
    def delayed(audio):
        entered.set()
        assert release.wait(3)
        return 'Hey Bart stale speech'
    listener._transcribe = delayed
    worker = threading.Thread(target=listener._handle_utterance, args=(np.ones(8000),))
    worker.start()
    assert entered.wait(2)
    listener.stop()
    # New generation represents resumed input without loading real models.
    release.set()
    worker.join(3)
    assert listener.wake.snapshot()['generation'] != generation
    assert listener.state == 'LISTENING'
    assert listener.wake.claim() is None


def test_empty_capture_has_no_delivery(monkeypatch):
    _, listener = wake_listener(monkeypatch)
    hear(listener, 'Hey Bart')
    hear(listener, 'over')
    assert listener.wake.claim() is None


def test_http_claim_off_and_manual_completion_isolation(monkeypatch):
    from .test_mic_server_coordination import MicServerTestCase
    import mic_server
    case = MicServerTestCase()
    case.setUp()
    try:
        module, listener = wake_listener(monkeypatch)
        monkeypatch.setattr(module, 'copy_to_clipboard', lambda text: None)
        mic_server.always_on_listener = listener
        listener.on_event = mic_server.always_on_event
        listener.running = True
        mic_server.state['mode'] = 'on'
        listener._execute_command('start_copy')
        hear(listener, 'manual words')
        hear(listener, 'over')
        hear(listener, 'Hey Bart wake words')
        assert case.post('/copy/start')[0] == 409
        assert case.post('/copy/stop')[1]['copied'] == 'manual words'
        assert listener.state == 'CAPTURING'
        hear(listener, 'over')
        assert case.get('/status')[1]['on_last_copied'] == 'manual words'
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: case.post('/wake/claim'), range(4)))
        assert sum(bool(body.get('claim')) for _, body in responses) == 1
        review = case.get('/wake/last-claim')[1]['claim']
        assert review['text'] == 'wake words'
        hear(listener, 'Hey Bart clear me')
        hear(listener, 'over')
        old_generation = listener.wake.generation
        assert case.post('/mode/off')[0] == 200
        assert case.post('/wake/claim')[0] == 409
        assert listener.wake.generation != old_generation
        assert listener.wake.snapshot()['pending_count'] == 0
        assert listener.wake.review() == review
        from wake_capture import WakeCaptures
        assert WakeCaptures(True).review() is None
    finally:
        case.tearDown()


def test_stop_then_start_generates_fresh_lifecycle(monkeypatch):
    _, listener = wake_listener(monkeypatch)
    listener.start()
    first = listener.wake.generation
    listener.stop()
    stopped = listener.wake.generation
    listener.start()
    try:
        assert first != stopped != listener.wake.generation
    finally:
        listener.stop()


@pytest.mark.parametrize('enabled,state,group,expected', [
    (True, 'LISTENING', None, 'Hey Bart'),
    (True, 'CALIBRATING', 'wake', 'Hey Bart'),
    (True, 'CALIBRATING', 'end_copy', None),
    (True, 'CAPTURING', None, None),
    (True, 'MEETING', None, None),
    (False, 'LISTENING', None, None),
    (False, 'CALIBRATING', 'wake', None),
])
def test_name_hint_only_applies_to_opted_in_wake_recognition(monkeypatch, enabled, state, group, expected):
    from types import SimpleNamespace
    _, listener = wake_listener(monkeypatch)
    listener.wake.enabled = enabled
    listener.state = state
    listener.cal_group = group
    calls = []
    def transcribe(path, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(text='  actual decoded text  ')], None
    listener.whisper_model = SimpleNamespace(transcribe=transcribe)
    assert listener._transcribe(np.ones(8000)) == 'actual decoded text'
    assert calls[0].get('hotwords') == expected
    assert calls[0]['beam_size'] == 1
    assert calls[0]['condition_on_previous_text'] is False
