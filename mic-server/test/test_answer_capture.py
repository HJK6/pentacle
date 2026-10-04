"""No-wake-word answer capture and delivery tagging on the wake path (no audio device)."""
import pytest
from .test_wake_voice import wake_listener, hear


class StubService:
    def __init__(self, meta):
        self.ready = False
        self.meta = meta
        self.captured = 0
        self.cancelled = 0

    def answer_window_status(self):
        return {'ready': self.ready} if self.ready else {'waiting': False}

    def answer_captured(self, meeting=False):
        self.captured += 1
        return self.meta

    def cancel_answer_window(self):
        self.cancelled += 1
        return True


def wired(monkeypatch, meta):
    module, listener = wake_listener(monkeypatch)
    stub = StubService(meta)
    listener.answer_window = lambda: stub.answer_window_status().get('ready', False)
    listener.on_answer = stub.answer_captured
    listener.cancel_answer_window = stub.cancel_answer_window
    listener.on_capture_end = lambda: {}
    return module, listener, stub


def test_answer_without_wake_word_is_delivered_tagged(monkeypatch):
    _, listener, stub = wired(monkeypatch, {'conversation_id': 'C', 'answer_to': 'L'})
    stub.ready = True
    hear(listener, 'the answer is yes')
    assert listener.capture_origin == 'answer' and listener.state == 'CAPTURING'
    hear(listener, 'over')
    claim = listener.wake.claim()
    assert claim['text'] == 'the answer is yes'
    assert claim['conversation_id'] == 'C' and claim['answer_to'] == 'L'
    assert stub.captured == 1


def test_lapsed_window_falls_back_to_fresh_request(monkeypatch):
    # answer_captured returns None (the window lapsed before "over").
    _, listener, stub = wired(monkeypatch, None)
    stub.ready = True
    hear(listener, 'yes please')
    hear(listener, 'over')
    claim = listener.wake.claim()
    assert claim['text'] == 'yes please'
    assert 'answer_to' not in claim and not claim.get('conversation_id')


def test_fresh_wake_closes_the_window_and_starts_new_request(monkeypatch):
    _, listener, stub = wired(monkeypatch, {'conversation_id': 'C', 'answer_to': 'L'})
    stub.ready = True
    hear(listener, 'Hey Bart what is the time')
    assert stub.cancelled == 1
    assert listener.capture_origin == 'wake'
    assert stub.captured == 0


def test_bare_over_does_not_start_an_answer_capture(monkeypatch):
    _, listener, stub = wired(monkeypatch, {'conversation_id': 'C', 'answer_to': 'L'})
    stub.ready = True
    hear(listener, 'over')
    assert listener.state == 'LISTENING' and listener.capture_origin is None


def test_no_capture_when_window_not_ready(monkeypatch):
    _, listener, stub = wired(monkeypatch, {'conversation_id': 'C', 'answer_to': 'L'})
    stub.ready = False
    hear(listener, 'some room chatter')
    assert listener.state == 'LISTENING' and listener.capture_origin is None
    assert listener.wake.claim() is None
