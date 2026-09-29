"""No-device acceptance for the spoken-conversation service."""
import json
import time
import pytest
from .test_wake_voice import wake_listener, hear
from . import test_mic_server_coordination as harness


def test_claim_opens_conversation_and_speak_journey(monkeypatch, service):
    _, listener = wake_listener(monkeypatch)
    listener.running = True
    hear(listener, 'Hey Bart please check my day')
    hear(listener, 'Over.')
    case = harness.MicServerTestCase(methodName='runTest')
    case.setUp()
    try:
        import mic_server
        monkeypatch.setattr(mic_server, "get_service", lambda: service)
        mic_server.always_on_listener = listener
        mic_server.state['mode'] = 'on'
        code, data = case.post('/wake/claim', {})
        assert code == 200
        cid = data['claim']['conversation_id']
        assert cid
        listener.suppress_recognition_until(0)
        code, outcome = case.post('/speak', dict(conversation_id=cid, kind='reply', text='It is ready. The detail is in chat.', final=True))
        assert code == 200 and outcome['outcome'] == 'spoken'
        assert case.post('/speak', dict(conversation_id=cid, kind='reply', text='Again.'))[1]['reason'] == 'closed_conversation'
    finally:
        case.tearDown()

from pathlib import Path
from types import SimpleNamespace
import threading
import wave
from resident_speaker import ResidentSpeaker, NullSink, ClipBank, render_clips, PlayerSink
from speaker_service import SpeakerService
from voice_rules import Rules, DEFAULTS
import copy


class Renderer:
    def __init__(self, root):
        self.root = root
        self.ready = False
        self.model_loads = 0
        self.calls = []
    def start(self):
        if not self.ready:
            self.ready = True
            self.model_loads += 1
    def render(self, text, deadline):
        self.start()
        self.calls.append(text)
        path = self.root / (str(len(self.calls))+'.wav')
        with wave.open(str(path), 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
            w.writeframes(b'\x00\x01'*2400)
        return dict(path=str(path), duration=.1)
    def close(self):
        self.ready = False


@pytest.fixture
def service(tmp_path):
    clock = [time.monotonic()]
    renderer = Renderer(tmp_path)
    speaker = ResidentSpeaker(renderer=renderer, sink=NullSink(), output_dir=tmp_path)
    root = tmp_path/'clips'
    render_clips(speaker, root, DEFAULTS['clips'])
    bank = ClipBank(speaker, root)
    bank.load(DEFAULTS['clips'])
    events = []
    service = SpeakerService(speaker=speaker, clips=bank, clock=lambda: clock[0], emit=events.append)
    listener = SimpleNamespace(running=True, state='LISTENING', wake=SimpleNamespace(lock=threading.RLock()), until=0)
    listener.recognition_stamp = lambda: (0, clock[0] < listener.until)
    listener.suppress_recognition_until = lambda stamp: setattr(listener, 'until', stamp)
    service.test_clock, service.test_listener, service.events = clock, listener, events
    service.ready = True
    return service


def opened(service):
    cid = service.open('room_mic', service.test_listener)
    service.test_clock[0] += 4
    return cid


def line(service, cid, **changes):
    payload = dict(conversation_id=cid, kind='reply', text='It is ready. The detail is in chat.')
    payload.update(changes)
    return service.speak(payload, service.test_listener)


def test_resident_sentence_rendering_and_real_null_files(service):
    cid = opened(service)
    before = len(service.speaker.renderer.calls)
    result = line(service, cid)
    assert result['outcome'] == 'spoken'
    assert service.speaker.renderer.calls[before:] == ['It is ready.', 'The detail is in chat.']
    assert len(result['receipt']['files']) == 2
    assert all(Path(f['path']).is_file() for f in result['receipt']['files'])
    assert service.speaker.renderer.model_loads == 1
    service.test_clock[0] += 4
    assert line(service, cid)['outcome'] == 'spoken'
    assert service.status()['model_loads'] == 1


def test_clips_rotate_without_repetition(service):
    bank = service.clips
    chosen = [bank.choose('acknowledgement', DEFAULTS['clips']['acknowledgement']) for _ in range(20)]
    assert all(a != b for a,b in zip(chosen,chosen[1:]))
    assert len(set(chosen)) == 6


def test_final_suppressed_closes_and_silent_skips_all_audio(service):
    cid = opened(service)
    service.silent = True
    count = len(service.speaker.renderer.calls)
    assert line(service, cid, final=True)['outcome'] == 'suppressed'
    assert line(service, cid)['reason'] == 'closed_conversation'
    service.open('room_mic', service.test_listener)
    assert service.last['reason'] == 'silent_mode'
    assert len(service.speaker.renderer.calls) == count


def test_fourth_line_closes_and_ceiling_expires(service):
    cid = opened(service)
    for _ in range(4):
        assert line(service,cid)['outcome'] == 'spoken'
        service.test_clock[0] += 4
    assert line(service,cid)['reason'] == 'exhausted_conversation'
    cid = opened(service)
    service.test_clock[0] += 43200
    assert line(service,cid)['reason'] == 'expired_conversation'


def test_fallback_once_and_none_after_accepted_line(service):
    cid = opened(service)
    assert service.turn_ended(cid)['outcome'] == 'spoken'
    assert service.last['receipt']['text'] == "I've replied in chat."
    first = service.speaker.last
    assert service.turn_ended(cid)['outcome'] == 'suppressed'
    assert service.speaker.last is first
    service.test_clock[0] += 4
    cid = opened(service)
    assert line(service,cid)['outcome'] == 'spoken'
    assert service.turn_ended(cid)['reason'] == 'line_already_accepted'


def test_late_kickoff_once_and_not_after_line(service):
    cid = opened(service)
    service.test_clock[0] += 15
    service.tick()
    assert service.last['receipt']['text'] == 'Still working on it.'
    receipt = service.last
    service.test_clock[0] += 15
    service.tick()
    assert service.last is receipt
    cid = opened(service)
    line(service,cid)
    service.test_clock[0] += 20
    service.tick()
    assert service.last['outcome'] == 'spoken' and 'text' not in service.last['receipt']


@pytest.mark.parametrize('changed,reason', [
    ({'conversation_id':'unknown'}, 'unknown_conversation'),
    ({'text':'a'*301}, 'text_too_long'),
    ({'kind':'announcement:done'}, 'announcement_not_allowed'),
    ({'action':'open_desktop'}, 'action_not_allowed'),
    ({'final':'true'}, 'invalid_request'),
    ({'text': ''}, 'invalid_request'),
    ({'kind': []}, 'invalid_request'),
])
def test_refusals_render_nothing(service,changed,reason):
    cid = opened(service)
    count = len(service.speaker.renderer.calls)
    payload = dict(conversation_id=cid,kind='reply',text='Ready.')
    payload.update(changed)
    assert service.speak(payload,service.test_listener)['reason'] == reason
    assert len(service.speaker.renderer.calls) == count


def test_origin_busy_and_gap(service):
    assert service.open('mobile_dictation',service.test_listener) is None
    assert service.last['reason'] == 'origin_not_allowed'
    cid = opened(service)
    service.test_listener.state = 'CAPTURING'
    assert line(service,cid)['reason'] == 'listener_busy'
    service.test_listener.state = 'LISTENING'
    assert line(service,cid)['outcome'] == 'spoken'
    assert line(service,cid)['reason'] == 'minimum_gap'


def test_rules_path_reload_announcement_and_invalid_fallback(service,tmp_path,monkeypatch):
    path = tmp_path/'rules.json'
    policy = copy.deepcopy(DEFAULTS)
    path.write_text(json.dumps(policy))
    monkeypatch.setenv('MIC_VOICE_RULES_FILE',str(path))
    service.rules = Rules()
    cid = opened(service)
    assert line(service,cid,kind='announcement:ready')['reason'] == 'announcement_not_allowed'
    policy['announcements'] = ['ready']
    policy['replies']['characters_per_line'] = 10
    path.write_text(json.dumps(policy))
    assert service.reload()
    assert line(service,cid)['reason'] == 'text_too_long'
    assert service.speak(dict(conversation_id=cid,kind='announcement:ready',text='Ready.'),service.test_listener)['outcome'] == 'spoken'
    version = service.rules.version
    path.write_text('{invalid')
    assert not service.reload()
    assert service.rules.version == version
    assert service.status()['rules']['error']
    assert service.rules.snapshot()['replies']['characters_per_line'] == 10


def test_meeting_does_not_suppress(service):
    cid = opened(service)
    assert service.speak(dict(conversation_id=cid,kind='reply',text='Ready.'),service.test_listener,meeting=True)['outcome'] == 'spoken'
    assert 'meeting' in service.status()['rules']['modes']


def test_rule_schema_rejects_bad_limits(tmp_path):
    path = tmp_path/'rules.json'
    path.write_text(json.dumps(DEFAULTS))
    rules = Rules(path)
    for key, value in [('characters_per_line',True),('lines_per_conversation',1.5),('minimum_gap_seconds',-1),('conversation_ceiling_seconds',float('nan'))]:
        policy = copy.deepcopy(DEFAULTS)
        policy['replies'][key] = value
        path.write_text(json.dumps(policy))
        assert not rules.reload()
        assert rules.snapshot() == DEFAULTS


def test_player_is_blocked_by_suite(tmp_path):
    with pytest.raises(pytest.fail.Exception, match='real audio'):
        PlayerSink().consume(tmp_path/'never.wav',time.time()+1)


def test_status_stays_responsive_during_inference(service):
    entered, release = threading.Event(), threading.Event()
    original = service.speaker.renderer.render
    def blocked(text, deadline):
        entered.set()
        assert release.wait(2)
        return original(text, deadline)
    cid = opened(service)
    service.speaker.renderer.render = blocked
    worker = threading.Thread(target=lambda: line(service,cid))
    worker.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert service.status()['ready']
        assert time.monotonic()-started < .1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


def test_sentence_two_renders_while_first_is_consumed(tmp_path):
    consuming, finished = threading.Event(), threading.Event()
    renderer = Renderer(tmp_path)
    original = renderer.render
    def render(text, deadline):
        if text == 'Second.':
            assert consuming.wait(1)
            assert not finished.is_set()
            finished.set()
        return original(text,deadline)
    renderer.render = render
    class Sink(NullSink):
        def consume(self,path,deadline):
            if Path(path).stem == '1':
                consuming.set()
                assert finished.wait(1)
            return super().consume(path,deadline)
    speaker = ResidentSpeaker(renderer=renderer,sink=Sink(),output_dir=tmp_path)
    assert speaker.speak('First. Second.',time.time()+5)['rendered']


def test_unprovisioned_rule_clips_leave_last_policy_active(service,tmp_path):
    path = tmp_path/'rules.json'
    policy = copy.deepcopy(DEFAULTS)
    path.write_text(json.dumps(policy))
    service.rules = Rules(path)
    version = service.rules.version
    policy['clips']['fallback'] = ['A new phrase.']
    path.write_text(json.dumps(policy))
    assert not service.reload()
    assert service.rules.version == version
    assert service.rules.snapshot() == DEFAULTS
    assert service.rules.error


def test_worker_reuses_one_model_and_closes_owned_process(tmp_path,monkeypatch):
    """Real worker/IPC with model and WAV writer counterparts, no model download."""
    import sys
    from resident_speaker import KokoroWorker
    modules = tmp_path/'modules'
    modules.mkdir()
    (modules/'onnxruntime.py').write_text('''class SessionOptions:
    def add_session_config_entry(self,*args): pass
class InferenceSession:
    def __init__(self,*args,**kwargs): pass
''')
    (modules/'kokoro_onnx.py').write_text('''import os
from pathlib import Path
class Kokoro:
    @classmethod
    def from_session(cls,*args):
        p=Path(os.environ['COUNT_MODEL_LOADS'])
        p.write_text(p.read_text()+'1' if p.exists() else '1')
        return cls()
    def get_voices(self): return ['bm_george']
    def create(self,*args,**kwargs): return [0]*2400,24000
''')
    (modules/'soundfile.py').write_text('''import wave
def write(path,samples,rate):
    with wave.open(path,'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate)
        w.writeframes(b'\\x00\\x01'*len(samples))
''')
    model = tmp_path/'model';model.write_text('fixture')
    monkeypatch.setenv('MIC_KOKORO_PYTHON',sys.executable)
    monkeypatch.setenv('MIC_KOKORO_MODEL',str(model))
    monkeypatch.setenv('MIC_KOKORO_VOICES',str(model))
    monkeypatch.setenv('PYTHONPATH',str(modules))
    count = tmp_path/'loads';monkeypatch.setenv('COUNT_MODEL_LOADS',str(count))
    worker = KokoroWorker(tmp_path/'output')
    try:
        first = worker.render('First.',time.time()+5)
        child = worker.child
        second = worker.render('Second.',time.time()+5)
        assert worker.child is child and count.read_text() == '1'
        assert Path(first['path']).is_file() and Path(second['path']).is_file()
        assert worker.model_loads == 1
    finally:
        worker.close()
    assert child.poll() is not None


def test_http_refuses_nonlocal_speaker_calls(service,monkeypatch):
    import mic_server
    handler = object.__new__(mic_server.MicHandler)
    handler.path = '/speak'
    handler.client_address = ('192.0.2.1',1234)
    handler._read_body = lambda: dict(conversation_id='unknown',kind='reply',text='Ready.')
    replies = []
    handler._json = lambda data,code=200: replies.append((code,data))
    monkeypatch.setattr(mic_server,'get_service',lambda: service)
    handler.do_POST()
    assert replies == [(403,dict(outcome='refused',reason='loopback_only'))]


def test_turn_ended_after_nonfinal_line_preserves_completion_capability(service):
    cid = opened(service)
    assert line(service,cid)['outcome'] == 'spoken'
    receipt = service.speaker.last
    assert service.turn_ended(cid)['reason'] == 'line_already_accepted'
    assert service.speaker.last is receipt
    service.test_clock[0] += 4
    assert line(service,cid,final=True)['outcome'] == 'spoken'
    assert line(service,cid)['reason'] == 'closed_conversation'


@pytest.mark.parametrize('configured', [None, 'null', 'typo', 'PLAYER', ''])
def test_only_explicit_player_configuration_can_select_audio(configured,monkeypatch,tmp_path):
    if configured is None:
        monkeypatch.delenv('MIC_SPEAKER_SINK', raising=False)
    else:
        monkeypatch.setenv('MIC_SPEAKER_SINK', configured)
    speaker = ResidentSpeaker(renderer=Renderer(tmp_path),output_dir=tmp_path)
    assert isinstance(speaker.sink, NullSink)
    speaker.speak('Captured safely.',time.time()+5)
    assert speaker.last['played'] is False


def test_silent_local_action_reports_suppression_without_rendering(service,monkeypatch):
    import speaker_service
    from voice_speaker import speak
    from .test_local_actions import fake_actions
    monkeypatch.setattr(speaker_service,'get_service',lambda: service)
    monkeypatch.setenv('MIC_VOICE_SPEAKER_MODE','resident')
    service.silent = True
    actions,listener,item = fake_actions(monkeypatch,speaker=speak)
    before = len(service.speaker.renderer.calls)
    assert actions._say(item,'Captured safely.') is False
    last = actions.snapshot()['last']
    assert last['state'] == 'suppressed' and last['reason'] == 'silent_mode'
    assert len(service.speaker.renderer.calls) == before
