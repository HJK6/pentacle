"""No-device acceptance for silent mode controls on the speaker service."""
import json
from .test_speaker_service import service, opened, line
from speaker_service import SpeakerService
from voice_rules import Rules, DEFAULTS
import copy


def test_set_silent_records_source_and_timestamp(service):
    before = service.status()
    assert before['silent'] is False and before['silent_source'] is None
    result = service.set_silent(True, 'web')
    assert result['silent'] is True and result['source'] == 'web'
    status = service.status()
    assert status['silent'] is True and status['silent_source'] == 'web'
    assert isinstance(status['silent_changed_at'], float)


def test_turning_on_plays_nothing_and_off_plays_confirmation(service):
    renders = len(service.speaker.renderer.calls)
    service.set_silent(True, 'voice', listener=service.test_listener)
    # Turning it on plays no clip; nothing is spoken.
    assert service.last is None
    service.set_silent(False, 'voice', listener=service.test_listener)
    # Turning it off plays the confirmation clip (a pre-rendered clip, not a fresh render).
    assert service.last['outcome'] == 'spoken'
    assert service.last['receipt']['text'] == 'Silent mode is off.'
    assert len(service.speaker.renderer.calls) == renders


def test_invalid_source_refused(service):
    assert service.set_silent(True, 'calendar')['reason'] == 'invalid_source'
    assert service.silent is False


def test_submitted_line_suppressed_and_not_replayed(service):
    cid = opened(service)
    service.set_silent(True, 'web')
    renders = len(service.speaker.renderer.calls)
    result = line(service, cid)
    assert result['outcome'] == 'suppressed' and result['reason'] == 'silent_mode'
    assert 'line_id' in result
    assert len(service.speaker.renderer.calls) == renders
    # Clearing silent mode never replays the suppressed line.
    service.set_silent(False, 'web', listener=service.test_listener)
    assert len(service.speaker.renderer.calls) == renders
    service.test_clock[0] += 4
    assert line(service, cid)['outcome'] == 'spoken'


def test_silent_persists_across_service_restart(service, tmp_path, monkeypatch):
    path = tmp_path / 'silent.json'
    monkeypatch.setenv('MIC_VOICE_SILENT_STATE', str(path))
    first = SpeakerService(speaker=service.speaker, clips=service.clips, clock=service.clock, emit=lambda e: None)
    first.set_silent(True, 'mobile')
    assert json.loads(path.read_text())['silent'] is True
    # A fresh service (a restart) reads the persisted flag.
    second = SpeakerService(speaker=service.speaker, clips=service.clips, clock=service.clock, emit=lambda e: None)
    assert second.silent is True and second.silent_source == 'mobile'


def test_silent_is_independent_of_meeting(service):
    cid = opened(service)
    # Meeting active with silent off: the line is spoken (meeting suppresses no speech).
    assert line(service, cid, kind='reply', text='Ready.')['outcome'] == 'spoken'
    status = service.status()
    assert status['silent'] is False
    # Setting silent does not touch meeting state and vice versa (meeting is external state).
    service.set_silent(True, 'web')
    assert service.status()['silent'] is True
