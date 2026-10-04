"""In-service matching of silent/meeting voice phrases on the wake path (no audio device)."""
import copy
import pytest
from .test_wake_voice import wake_listener, hear
from voice_rules import DEFAULTS


def wired(monkeypatch, modes=None):
    module, listener = wake_listener(monkeypatch)
    silent_calls = []
    meeting = {'start': 0, 'stop': 0}
    listener.mode_phrases = lambda: copy.deepcopy(modes if modes is not None else DEFAULTS['modes'])
    listener.on_silent = silent_calls.append
    listener.on_meeting_start = lambda: meeting.__setitem__('start', meeting['start'] + 1)
    listener.on_meeting_stop = lambda: meeting.__setitem__('stop', meeting['stop'] + 1)
    return module, listener, silent_calls, meeting


def say(listener, line):
    hear(listener, 'Hey Bart ' + line)
    hear(listener, 'over')


def test_silent_on_and_off_phrases_handled_in_service(monkeypatch):
    _, listener, silent, _ = wired(monkeypatch)
    say(listener, 'silent mode on')
    assert silent == [True]
    # Handled in-service: nothing is delivered to Bart.
    assert listener.wake.claim() is None
    say(listener, 'silent mode off')
    assert silent == [True, False]
    assert listener.wake.claim() is None


def test_meeting_start_and_end_phrases_handled_in_service(monkeypatch):
    _, listener, silent, meeting = wired(monkeypatch)
    say(listener, 'start meeting')
    assert meeting['start'] == 1 and listener.meeting_active is True
    assert listener.wake.claim() is None
    say(listener, 'end meeting')
    assert meeting['stop'] == 1 and listener.meeting_active is False
    assert silent == []


@pytest.mark.parametrize('line', ['silent movie night', 'take meeting notes', 'what does silent mean', 'the meeting went well'])
def test_near_misses_do_not_switch_modes_and_reach_bart(monkeypatch, line):
    _, listener, silent, meeting = wired(monkeypatch)
    say(listener, line)
    assert silent == [] and meeting == {'start': 0, 'stop': 0}
    # A near miss is an ordinary room-mic line delivered to Bart.
    assert listener.wake.claim()['text'] == line


def test_without_wiring_meeting_phrase_is_sent_to_bart(monkeypatch):
    # Step-2 classification: with the wake-capture path and no in-service mode matching,
    # a meeting phrase is NOT executed locally -- it is delivered to Bart as text (the defect
    # this spec fixes by sourcing mode phrases from the rules Modes section).
    _, listener = wake_listener(monkeypatch)
    say(listener, 'start meeting')
    assert listener.meeting_active is False
    assert listener.wake.claim()['text'] == 'start meeting'
