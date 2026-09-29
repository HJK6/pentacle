"""Reply-latency regressions on the real no-device capture/speaker path."""
import copy
import json
import time
import pytest
from .test_speaker_service import service, opened, line
from .test_wake_voice import wake_listener, hear


def test_capture_end_acknowledges_before_claim(monkeypatch, service):
    import speaker_service
    monkeypatch.setattr(speaker_service, "get_service", lambda: service)
    _, listener = wake_listener(monkeypatch)
    listener.running = True
    # Runtime's callback contract is installed here, not an acknowledgement mock.
    listener.on_capture_end = lambda: service.capture_ended('room_mic', listener)
    hear(listener, 'Hey Bart say hello')
    hear(listener, 'over')
    until = time.monotonic()+1
    while not service.last and time.monotonic()<until:
        time.sleep(.01)
    assert service.last and service.last['outcome']=='spoken'
    assert service.last['receipt']['text'] in service.rules.snapshot()['clips']['acknowledgement']
    assert listener.wake.snapshot()['pending_count']==1
    claim = listener.wake.claim()
    assert claim['conversation_id'] in service.conversations
    assert listener.wake.claim() is None


def test_shipped_policy_has_no_late_filler(service):
    cid = opened(service)
    before = service.last
    service.test_clock[0] += 20
    service.tick()
    assert service.last is before
    assert service.conversations[cid]['lines']==0


@pytest.mark.parametrize('text,key,actual', [
    ('a'*301,'characters_per_line',301),
    (' '.join(['go']*41),'words_per_line',41),
    ('Ready. Read chat. Thanks.','sentences_per_line',3),
])
def test_refusal_reports_loaded_limit(service,text,key,actual):
    cid = opened(service)
    result = line(service,cid,text=text)
    assert result['outcome']=='refused'
    assert result['limit_name']==key
    assert result['limit']==service.rules.snapshot()['replies'][key]
    assert result['measured']==actual


@pytest.mark.parametrize('local,silent', [(False,False),(True,False),(False,True)])
def test_capture_ack_respects_silence_and_precedes_router(monkeypatch,service,local,silent):
    import speaker_service
    monkeypatch.setattr(speaker_service,'get_service',lambda:service)
    monkeypatch.setenv('MIC_LOCAL_ACTIONS',str(local).lower())
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_WAKE','shared')
    _,listener=wake_listener(monkeypatch)
    listener.running=True
    service.silent=silent
    listener.on_capture_end=lambda:service.capture_ended('room_mic',listener)
    routed=[]
    listener.voice_actions.classifier=lambda text:routed.append(text)
    hear(listener,'Hey Bart hello')
    hear(listener,'over')
    deadline=time.monotonic()+1
    while service.last is None and time.monotonic()<deadline:
        time.sleep(.01)
    assert service.last['outcome']==('suppressed' if silent else 'spoken')
    assert routed==[]  # No worker has run; acknowledgement already completed.
    if not local:
        claim=listener.wake.claim()
        assert claim['text']=='hello' and claim['conversation_id']
    else:
        queued=listener.voice_actions.queue.get_nowait()
        assert queued['metadata']['conversation_id']


@pytest.mark.parametrize('key,text,limit', [
    ('characters_per_line','x'*300,300),
    ('words_per_line',' '.join(['go']*40),40),
    ('sentences_per_line','Ready. Read chat.',2),
])
def test_limits_accept_boundary_and_follow_configured_rules(service,tmp_path,monkeypatch,key,text,limit):
    from voice_rules import DEFAULTS,Rules
    policy=copy.deepcopy(DEFAULTS)
    path=tmp_path/'rules.json'
    path.write_text(json.dumps(policy))
    monkeypatch.setenv('MIC_VOICE_RULES_FILE',str(path))
    service.rules=Rules()
    assert line(service,opened(service),text=text)['outcome']=='spoken'
    policy['replies'][key]=limit-1
    path.write_text(json.dumps(policy))
    assert service.reload()
    assert service.contract()[key]==limit-1
    result=line(service,opened(service),text=text)
    assert result['outcome']=='refused' and result['measured']==limit


def test_timing_is_per_conversation_and_delivery_stays_pending_until_provider_root(service):
    cid=opened(service)
    for stage in ('capture_ended_at','routed_at','claimed_at'):
        assert service.mark(cid,stage)
    timing=service.status()['last_conversation']
    assert timing['delivery_pending'] is True
    assert timing['timing']['delivered_at'] is None
    assert service.mark(cid,'delivered_at')
    assert line(service,cid)['outcome']=='spoken'
    timing=service.status()['last_conversation']
    assert timing['delivery_pending'] is False
    assert all(stamp is not None for stamp in timing['timing'].values())
    assert not service.mark('unknown','delivered_at')
    assert not service.mark(cid,'arbitrary')


def test_reply_waits_for_own_ack_without_bypassing_new_capture(service):
    import threading
    entered, release = threading.Event(), threading.Event()
    epoch = [0]
    listener = service.test_listener
    listener.recognition_stamp = lambda: (epoch[0], service.test_clock[0] < listener.until)
    def suppress(stamp):
        epoch[0] += 1
        listener.until = stamp
    listener.suppress_recognition_until = suppress
    original = service.clips.play
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    service.clips.play = blocked
    metadata=service.capture_ended('room_mic',service.test_listener)
    cid=metadata['conversation_id']
    assert entered.wait(1)
    results=[]
    worker=threading.Thread(target=lambda:results.append(line(service,cid)))
    worker.start()
    assert service.turn_ended(cid)['reason']=='line_pending'
    release.set()
    worker.join(2)
    assert not worker.is_alive() and results[0]['outcome']=='spoken'
    # A subsequent capture's recognition generation cannot inherit this privilege.
    cid=opened(service)
    service.test_listener.suppress_recognition_until(service.test_clock[0]+20)
    assert line(service,cid)['reason']=='listener_busy'
