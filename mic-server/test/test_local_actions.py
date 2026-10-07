import pytest


@pytest.fixture(autouse=True)
def configured_synthetic_hosts(monkeypatch):
    import local_actions
    monkeypatch.setattr(local_actions, 'HOSTS', {'samplehost', 'otherhost', 'thirdhost'})
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_DEFAULT_HOST', 'samplehost')


def test_missing_task_never_spawns():
    from local_actions import validate_decision
    decision = {'route':'spawn_agent','task':'','model':'','effort':'','host':'','assets':[],'reply':''}
    assert validate_decision(decision, 'Spawn me an agent')['route'] == 'clarify'

import io
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock


def decision(**fields):
    return dict(route='spawn_agent', task='review tests', model='', effort='', host='', assets=[], reply='', **fields) if not fields else {**decision(), **fields}


def test_default_and_explicit_tuple_are_grounded():
    from local_actions import validate_decision
    got = validate_decision(decision(model='astra'), 'Spawn an Astra agent to review tests')
    assert (got['provider'], got['model'], got['effort'], got['host']) == ('codex', 'gpt-6-astra', 'high', 'samplehost')
    assert validate_decision(decision(), 'Spawn an agent to review tests')['spawn']['missing'] == ['model']
    got = validate_decision(decision(model='sol', effort='medium', host='otherhost'), 'Start a Sol agent with medium effort on Otherhost to review tests')
    assert (got['provider'], got['model'], got['effort'], got['host']) == ('codex', 'gpt-6.1-sol', 'medium', 'otherhost')
    got = validate_decision(decision(model='sol'), 'Spawn a Sol agent to review tests')
    assert (got['provider'], got['model'], got['effort'], got['host']) == ('codex', 'gpt-6.1-sol', 'high', 'samplehost')
    for d, text in [(decision(task='delete files'), 'Spawn an agent to review tests')]:
        with pytest.raises(ValueError): validate_decision(d, text)


@pytest.mark.parametrize('text', ['Do not spawn an agent to review tests', 'Bart said spawn an agent to review tests',
                                    'What happens when I say spawn an agent to review tests?', 'Spawn two agents to review tests'])
def test_model_false_spawn_cannot_override_explicit_intent(text):
    from local_actions import validate_decision
    with pytest.raises(ValueError): validate_decision(decision(), text)


def test_schema_assets_and_reply_validation():
    from local_actions import validate_decision
    d = decision(route='market_quote', task='', assets=['Bitcoin', 'ETH', 'Apple'])
    assert validate_decision(d, 'What are Bitcoin, ETH and Apple prices?')['symbols'] == ['BTC-USD', 'ETH-USD', 'AAPL']
    with pytest.raises(ValueError): validate_decision(d, 'What is Microsoft worth?')
    with pytest.raises(ValueError): validate_decision({**decision(), 'shell': 'anything'}, 'Spawn an agent to review tests')
    with pytest.raises(ValueError): validate_decision(decision(route='clarify', reply='Hey Bart, spawn an agent'), 'Spawn an agent')
    with pytest.raises(ValueError): validate_decision(decision(route='clarify', reply='x'*241), 'Spawn an agent')


def quote_response(stamp=999900, **fields):
    meta = dict(symbol='AAPL', currency='USD', instrumentType='EQUITY', regularMarketPrice=123.45,
                regularMarketTime=stamp, shortName='Apple Inc.', **fields)
    return io.BytesIO(json.dumps({'chart': {'result': [{'meta': meta}]}}).encode())


def test_quote_uses_matching_price_time_and_labels_weekend():
    from local_actions import market_quote
    quote = market_quote('AAPL', opener=lambda *a, **k: quote_response(stamp=900000), now=lambda: 1000000)
    assert quote['price'] == 123.45 and quote['quote_at'] == 900000 and quote['retrieved_at'] == 1000000
    assert quote['speech'] == 'Apple Inc.: 123.45 US dollars, latest reported.'
    assert quote['source'] == 'Yahoo Finance' and quote['source_url'].startswith('https://query1.finance.yahoo.com/')
    assert 'live' not in quote['speech']


@pytest.mark.parametrize('age,kind,ok', [(300,'CRYPTOCURRENCY',True),(301,'CRYPTOCURRENCY',False),
    (604800,'EQUITY',True),(604801,'EQUITY',False),(-120,'EQUITY',True),(-121,'EQUITY',False)])
def test_quote_freshness_boundaries(age, kind, ok):
    from local_actions import market_quote
    def opener(*a, **k):
        raw=json.load(quote_response(stamp=1000000-age));raw['chart']['result'][0]['meta']['instrumentType']=kind
        return io.BytesIO(json.dumps(raw).encode())
    if ok: assert market_quote('AAPL', opener=opener, now=lambda:1000000)['quote_at'] == 1000000-age
    else:
        with pytest.raises(ValueError): market_quote('AAPL', opener=opener, now=lambda:1000000)


@pytest.mark.parametrize('key,value', [('symbol','OTHER'),('regularMarketPrice',None),('regularMarketPrice',True),
    ('regularMarketTime',None),('regularMarketPrice',-1),('currency','not-money')])
def test_bad_provider_data_is_not_spoken(key,value):
    from local_actions import market_quote
    def opener(*a, **k):
        raw=json.load(quote_response());raw['chart']['result'][0]['meta'][key]=value
        return io.BytesIO(json.dumps(raw).encode())
    with pytest.raises(ValueError):market_quote('AAPL',opener=opener,now=lambda:1000000)


def fake_actions(monkeypatch, classifier=None, speaker=None):
    from wake_capture import WakeCaptures
    from voice_actions import VoiceActions
    monkeypatch.delenv('MIC_LOCAL_ACTIONS', raising=False)
    listener=SimpleNamespace(running=True,state='LISTENING',wake=WakeCaptures(True),recognition_stamp=lambda: (0, False),_emit=Mock(),suppress_recognition_until=Mock())
    actions=VoiceActions(listener,classifier=classifier or (lambda text: {'route':'clarify','reply':'What task? Repeat the whole request.'}),speaker=speaker or Mock())
    return actions,listener,dict(id='a'*32,generation=listener.wake.generation,text='Spawn an agent')


def test_missing_task_speaks_without_claim_or_agent(monkeypatch):
    actions,listener,item=fake_actions(monkeypatch)
    actions._process(item)
    assert actions.speaker.call_count==1 and not listener.wake.pending
    assert actions.snapshot()['last']['state']=='spoken'


def test_off_during_inference_discards_result(monkeypatch):
    actions,listener,item=fake_actions(monkeypatch)
    def classifier(text):
        listener.wake.invalidate();listener.running=False
        return {'route':'spawn_agent','task':'review tests'}
    actions.classifier=classifier;actions._process(item)
    assert not listener.wake.pending and not actions.speaker.called
    assert actions.snapshot()['last']['state']=='cancelled'


def test_typed_claim_requires_new_client_and_outcome_is_once(monkeypatch):
    from local_actions import validate_decision
    actions,listener,item=fake_actions(monkeypatch,classifier=lambda text:validate_decision(decision(model='astra'),'Spawn an Astra agent to review tests'))
    actions._process(item)
    assert listener.wake.claim() is None and len(listener.wake.pending)==1
    claim=listener.wake.claim(actions_version=2)
    assert claim['action']['model']=='gpt-6-astra'
    assert actions.outcome(claim['id'],claim['generation'],'spawned')
    assert not actions.outcome(claim['id'],claim['generation'],'spawned')
    assert actions.queue.qsize()==1


def test_remote_failure_keeps_full_recognition_fence(monkeypatch):
    def fail(*args): raise TimeoutError('remote acknowledgement lost')
    actions,listener,item=fake_actions(monkeypatch,speaker=fail)
    start=time.monotonic()
    with pytest.raises(TimeoutError):actions._say(item,'Which task?')
    assert listener.suppress_recognition_until.call_count==1
    assert listener.suppress_recognition_until.call_args.args[0]>=start+35


def test_speaker_text_is_stdin_data_and_deadline_is_enforced(monkeypatch, tmp_path):
    from voice_speaker import speak,REMOTE
    script=tmp_path/'speak.sh';script.write_text('#!/bin/sh\n');script.chmod(0o700)
    monkeypatch.setenv('MIC_VOICE_SPEAKER_MODE','ssh')
    monkeypatch.setenv('MIC_VOICE_SPEAKER_SCRIPT',str(script))
    monkeypatch.setenv('MIC_VOICE_SPEAKER_SSH','speaker-peer')
    text='A quote with apostrophe\' and $(touch /tmp/no) `never execute`'
    def run(argv,**kwargs):
        assert text not in ' '.join(argv)
        data=json.loads(kwargs['input']);assert data['text']==text and data['script']==str(script)
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(id=data['id'],played=True,stopped=True)))
    assert speak(text,time.time()+30,run=run)['stopped']
    # Execute the actual fixed remote program with an expired request: no subprocess.
    import subprocess,sys
    result=subprocess.run([sys.executable,'-c',REMOTE],input=json.dumps(dict(text='test',id='a'*32,deadline=time.time()-1)),text=True,capture_output=True)
    assert result.returncode!=0 and 'expired' in result.stderr


def test_delayed_asr_and_speaker_audio_are_rejected_but_archived(monkeypatch,tmp_path):
    import numpy as np
    from .test_listener_lifecycle import install_listener_import_fakes,import_fresh_module
    from audio_buffer import AudioBuffer
    install_listener_import_fakes(monkeypatch)
    module=import_fresh_module('always_on');listener=module.AlwaysOnListener();listener.running=True
    listener.wake.enabled=True;listener._emit_callback_health=lambda *args:None
    buffer=AudioBuffer(tmp_path);listener.audio_buffer=buffer
    try:
        def transcribe(audio):
            listener.suppress_recognition_until(time.monotonic()+30)
            return 'Hey Bart spawn an agent to review tests'
        listener._transcribe=transcribe
        listener._handle_utterance(np.ones(8000))
        assert listener.state=='LISTENING'
        listener._audio_callback(np.zeros((480,1),dtype=np.float32),480,None,None)
        assert listener.audio_q.empty() and buffer.seen_frames==480
        buffer.flush()
        assert list(buffer.rolling.glob('*.wav'))
    finally:buffer.close()


def test_queued_old_epoch_rejected_after_playback_ends(monkeypatch):
    import numpy as np
    from .test_listener_lifecycle import install_listener_import_fakes,import_fresh_module
    install_listener_import_fakes(monkeypatch)
    module=import_fresh_module('always_on');listener=module.AlwaysOnListener();listener.running=True
    old=listener.recognition_stamp()[0]
    listener.suppress_recognition_until(time.monotonic()-1)
    assert listener.recognition_stamp()[1] is False
    listener._transcribe=Mock(return_value='Hey Bart spawn an agent')
    listener._handle_utterance(np.ones(8000),recognition_epoch=old)
    assert not listener._transcribe.called


def test_remote_timeout_kills_only_owned_playback_group(monkeypatch, tmp_path):
    import subprocess,sys,os,signal
    from pathlib import Path
    from voice_speaker import REMOTE
    child=SimpleNamespace(pid=987654,returncode=None,wait=Mock())
    child.communicate=Mock(side_effect=subprocess.TimeoutExpired('speaker',25))
    popen=Mock(return_value=child);kill=Mock();unlink=Mock()
    monkeypatch.setattr(subprocess,'Popen',popen);monkeypatch.setattr(os,'killpg',kill,raising=False)
    monkeypatch.setattr(signal,'SIGKILL',9,raising=False)
    monkeypatch.setattr(Path,'unlink',unlink)
    monkeypatch.setattr(sys,'stdin',io.StringIO(json.dumps(dict(id='b'*32,text='What task?',script=str(tmp_path/'placeholder'),deadline=time.time()+30))))
    with pytest.raises(subprocess.TimeoutExpired):exec(REMOTE,{})
    assert popen.call_args.kwargs['start_new_session'] is True
    kill.assert_called_once_with(987654,signal.SIGKILL)
    child.wait.assert_called_once_with(timeout=2)
    unlink.assert_called_once()


def test_model_wait_does_not_block_http_off_or_status(monkeypatch):
    import threading
    import mic_server
    from http.server import HTTPServer
    from urllib.request import Request,urlopen
    actions,listener,item=fake_actions(monkeypatch)
    listener.whisper_model=None;listener.voice_actions=actions;listener.capture_origin=None
    entered,release=threading.Event(),threading.Event()
    def classifier(text):
        entered.set();assert release.wait(5)
        return {'route':'spawn_agent','task':'review tests'}
    actions.classifier=classifier
    worker=threading.Thread(target=actions._process,args=(item,));worker.start();assert entered.wait(1)
    monkeypatch.setattr(mic_server,'always_on_listener',listener)
    monkeypatch.setattr(mic_server,'state',dict(mic_server.state,mode='on',last_error=None))
    def stop(): listener.running=False;listener.wake.invalidate()
    listener.stop=stop
    monkeypatch.setattr(mic_server,'stop_all',stop)
    server=HTTPServer(('127.0.0.1',0),mic_server.MicHandler);thread=threading.Thread(target=server.serve_forever);thread.start()
    try:
        base=f'http://127.0.0.1:{server.server_port}'
        with urlopen(base+'/status',timeout=1) as r: assert json.load(r)['local_actions']['last']['state']=='classifying'
        with urlopen(Request(base+'/mode/off',data=b'{}',headers={'Content-Type':'application/json'}),timeout=1) as r:assert r.status==200
        assert worker.is_alive() and not listener.running
        release.set();worker.join(2);assert not listener.wake.pending
    finally:
        release.set();worker.join(2);server.shutdown();server.server_close();thread.join(2)


@pytest.mark.parametrize('text,fields', [
    ('Start a unicorn agent to review tests', {}),
    ('Start an agent on Saturn to review tests', {}),
    ('Start an agent with extreme effort to review tests', {}),
])
def test_explicit_options_cannot_be_omitted_or_defaulted(text, fields):
    from local_actions import validate_decision
    try:
        result = validate_decision(decision(**fields), text)
    except ValueError:
        return
    assert result['route'] != 'spawn_agent'


@pytest.mark.parametrize('text,task', [('Spawn an agent','agent'),('Spawn an agent!!!','!!!'),('Spawn me an agent','me')])
def test_task_must_be_actual_work_after_spawn_header(text,task):
    from local_actions import validate_decision
    with pytest.raises(ValueError):validate_decision(decision(task=task),text)


def test_task_mentions_are_not_launch_options():
    from local_actions import validate_decision
    task='review the Otherhost and Sol documentation'
    got=validate_decision(decision(task=task, model='astra'),'Start an Astra agent to '+task)
    assert got['host']=='samplehost' and got['model']=='gpt-6-astra'


@pytest.mark.parametrize('asset', ['Ford','FORD','ford','Acme'])
def test_unknown_company_is_not_a_guessed_symbol(asset):
    from local_actions import validate_decision
    text='What is '+('Ford' if asset.casefold()=='ford' else asset)+' stock worth?'
    with pytest.raises(ValueError):validate_decision(decision(route='market_quote',assets=[asset]),text)


@pytest.mark.parametrize('asset,text,symbol', [('MSFT','What is msft worth?','MSFT'),('F','Price of ticker f','F'),('BRK.B','Price of BRK.B','BRK.B')])
def test_explicit_tickers_and_known_symbols_remain_valid(asset,text,symbol):
    from local_actions import validate_decision
    assert validate_decision(decision(route='market_quote',assets=[asset]),text)['symbols']==[symbol]


def test_unknown_playback_serializes_next_job_and_keeps_off_responsive(monkeypatch):
    import voice_actions
    clock=[100.0]
    monkeypatch.setattr(voice_actions.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(voice_actions.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    calls=[]
    def speaker(*args):
        calls.append(clock[0])
        if len(calls)==1:raise TimeoutError()
    actions,listener,item=fake_actions(monkeypatch,speaker=speaker)
    with pytest.raises(TimeoutError):actions._say(item,'First')
    actions._say(item,'Second')
    assert len(calls)==2 and calls[1]>=136
    deadlines=[c.args[0] for c in listener.suppress_recognition_until.call_args_list]
    assert deadlines[-1]>=137
    # Off during an outstanding uncertainty fence discards queued speech immediately.
    actions.speaker=Mock(side_effect=TimeoutError())
    clock[0]=200
    with pytest.raises(TimeoutError):actions._say(item,'Third')
    def stop(seconds):listener.running=False;clock[0]+=seconds
    monkeypatch.setattr(voice_actions.time,'sleep',stop)
    actions._say(item,'Fourth')
    assert actions.speaker.call_count==1 and clock[0]<201


def test_model_warming_is_bounded_idle_and_disabled_when_off(monkeypatch):
    actions,listener,item=fake_actions(monkeypatch)
    actions.enabled=True;actions.warmer=Mock()
    actions._warm_if_due(100)
    actions.warmer.assert_called_once()
    actions._warm_if_due(399)
    assert actions.warmer.call_count==1
    actions._warm_if_due(400)
    assert actions.warmer.call_count==2
    listener.running=False
    actions._warm_if_due(800)
    assert actions.warmer.call_count==2
    listener.running=True;listener.wake.invalidate()
    actions._warm_if_due(801)
    assert actions.warmer.call_count==3


def test_warm_request_preloads_without_action_or_generated_text():
    from local_actions import warm_model,MODEL
    def opener(request,timeout):
        body=json.loads(request.data)
        assert request.full_url=='http://127.0.0.1:11434/api/generate'
        assert body['model']==MODEL and body['prompt']=='' and body['keep_alive']=='30m'
        assert body['options']['num_ctx']==4096 and timeout==40
        return io.BytesIO(b'{"done":true}')
    warm_model(opener)


def test_warm_failure_is_visible_and_rate_limited(monkeypatch):
    actions,listener,item=fake_actions(monkeypatch)
    actions.enabled=True;actions.warmer=Mock(side_effect=TimeoutError())
    actions._warm_if_due(100)
    assert actions.snapshot()['warm_error']
    actions._warm_if_due(101)
    assert actions.warmer.call_count==1 and not listener.wake.pending


def test_task_flags_and_quotes_are_preserved_verbatim():
    from local_actions import validate_decision
    with pytest.raises(ValueError):
        validate_decision(decision(task='run pytest q'),'Start an agent to run pytest -q')
    task="run pytest -q and print 'Done!'"
    got=validate_decision(decision(task=task.casefold(), model='astra'),'Start an Astra agent to '+task)
    assert got['task']==task


@pytest.mark.parametrize('symbol,name', [('BTC-USD','Bitcoin'),('ETH-USD','Ethereum')])
def test_crypto_speech_is_only_name_price_and_currency(symbol,name):
    from local_actions import market_quote
    def opener(*args,**kwargs):
        meta=dict(symbol=symbol,currency='USD',instrumentType='CRYPTOCURRENCY',regularMarketPrice=123.45,regularMarketTime=999999,shortName=name+' USD')
        return io.BytesIO(json.dumps({'chart':{'result':[{'meta':meta}]}}).encode())
    quote=market_quote(symbol,opener=opener,now=lambda:1000000)
    assert quote['speech']==name+': 123.45 US dollars.'
    assert quote['source']=='Yahoo Finance' and quote['quote_at']==999999 and quote['retrieved_at']==1000000


def test_local_speaker_uses_host_config_and_keeps_text_as_data(monkeypatch, tmp_path):
    import sys
    from voice_speaker import speak
    script=tmp_path/'speak.sh';script.write_text('#!/bin/sh\n');script.chmod(0o700)
    monkeypatch.setenv('MIC_VOICE_SPEAKER_MODE','local')
    monkeypatch.setenv('MIC_VOICE_SPEAKER_SCRIPT',str(script))
    monkeypatch.delenv('MIC_VOICE_SPEAKER_SSH',raising=False)
    text='Local voice check $(never execute)'
    def run(argv,**kwargs):
        assert argv[:2]==[sys.executable,'-c']
        data=json.loads(kwargs['input']);assert data['text']==text and data['script']==str(script)
        assert text not in ' '.join(argv)
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(id=data['id'],played=True,stopped=True)))
    assert speak(text,time.time()+30,run=run)['stopped']


def test_local_spawn_uses_configured_default_and_refuses_missing_default(monkeypatch):
    from local_actions import validate_decision
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_DEFAULT_HOST','thirdhost')
    got=validate_decision(decision(model='sol'), 'Spawn a Sol agent to review tests')
    assert got['host']=='thirdhost'
    got=validate_decision(decision(model='sol',host='thirdhost'), 'Spawn a Sol agent on Thirdhost to review tests')
    assert got['host']=='thirdhost'
    monkeypatch.delenv('MIC_LOCAL_ACTIONS_DEFAULT_HOST')
    got=validate_decision(decision(model='sol'),'Spawn a Sol agent to review tests')
    assert got['route']=='clarify' and 'host' in got['spawn']['missing']
    assert 'host' not in got['spawn']['fields']


@pytest.mark.parametrize('mode', ['local', 'ssh'])
def test_speaker_refuses_missing_script_without_invoking_process(monkeypatch, mode):
    from voice_speaker import speak
    monkeypatch.setenv('MIC_VOICE_SPEAKER_MODE', mode)
    monkeypatch.setenv('MIC_VOICE_SPEAKER_SSH', 'synthetic-peer')
    monkeypatch.delenv('MIC_VOICE_SPEAKER_SCRIPT', raising=False)
    run = Mock()
    with pytest.raises(ValueError, match='absolute path'):
        speak('synthetic response', time.time()+30, run=run)
    run.assert_not_called()


def test_local_speaker_refuses_nonexecutable_placeholder(monkeypatch, tmp_path):
    from voice_speaker import speak
    script = tmp_path/'placeholder'
    script.write_text('not executable')
    script.chmod(0o600)
    monkeypatch.setenv('MIC_VOICE_SPEAKER_MODE', 'local')
    monkeypatch.setenv('MIC_VOICE_SPEAKER_SCRIPT', str(script))
    run = Mock()
    with pytest.raises(ValueError, match='executable'):
        speak('synthetic response', time.time()+30, run=run)
    run.assert_not_called()


def test_fixed_speaker_program_cleans_owned_wav_and_confirms_receipt(monkeypatch, tmp_path, capsys):
    import os, signal, subprocess, sys, uuid
    from pathlib import Path
    from voice_speaker import REMOTE
    identifier = uuid.uuid4().hex
    wav = Path('/tmp')/('pentacle-voice-'+identifier+'.wav')
    script = tmp_path/'placeholder'
    script.write_text('#!/bin/sh\n')
    script.chmod(0o700)
    child = SimpleNamespace(pid=987654, returncode=0, wait=Mock(),
                            communicate=Mock(return_value=(b'', b'')))
    def fake_popen(argv, **kwargs):
        assert argv == [str(script), 'synthetic response', str(wav)]
        assert kwargs['start_new_session'] is True
        wav.write_bytes(b'synthetic WAV stand-in')
        return child
    kill = Mock()
    monkeypatch.setattr(subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(os, 'killpg', kill, raising=False)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(dict(
        id=identifier, text='synthetic response', script=str(script), deadline=time.time()+30))))
    try:
        exec(REMOTE, {})
        assert json.loads(capsys.readouterr().out) == dict(stopped=True, played=True, id=identifier)
        assert not wav.exists()
        kill.assert_called_once_with(child.pid, signal.SIGKILL)
        child.wait.assert_called_once_with(timeout=2)
    finally:
        wav.unlink(missing_ok=True)


def test_unconfigured_host_actions_fail_closed(monkeypatch):
    import local_actions
    monkeypatch.setattr(local_actions, 'HOSTS', set())
    monkeypatch.delenv('MIC_LOCAL_ACTIONS_DEFAULT_HOST', raising=False)
    result = local_actions.validate_decision(decision(model='sol'), 'Spawn a Sol agent to review tests')
    assert result['route'] == 'clarify'
    assert 'host' in result['spawn']['missing']
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_DEFAULT_HOST', 'unknownhost')
    with pytest.raises(ValueError, match='host'):
        local_actions.validate_decision(decision(model='sol'), 'Spawn a Sol agent to review tests')


def test_configured_allowlist_is_read_from_explicit_environment():
    import os, subprocess, sys
    from pathlib import Path
    script = "import json,local_actions as m; d=dict(route='spawn_agent',task='review tests',model='sol',effort='',host='',assets=[],reply=''); r=m.validate_decision(d,'Spawn a Sol agent to review tests'); print(json.dumps(dict(host=r['host'],allowed=sorted(m.HOSTS))))"
    result = subprocess.run([sys.executable,'-c',script],text=True,capture_output=True,check=True,
        env={'PATH':os.defpath,'PYTHONPATH':str(Path(__file__).resolve().parents[1]),
             'MIC_LOCAL_ACTIONS_HOSTS':' Samplehost,Otherhost ', 'MIC_LOCAL_ACTIONS_DEFAULT_HOST':'samplehost'})
    assert json.loads(result.stdout) == dict(host='samplehost',allowed=['otherhost','samplehost'])


def test_separate_local_action_wake_uses_product_name(monkeypatch):
    from .test_wake_voice import wake_listener, hear
    monkeypatch.setenv('MIC_LOCAL_ACTIONS','true')
    monkeypatch.setenv('MIC_LOCAL_ACTIONS_WAKE','separate')
    _,listener=wake_listener(monkeypatch)
    hear(listener,'Hey Pentacle spawn an agent')
    assert listener.capture_origin=='local_action'
    hear(listener,'over')
    assert listener.voice_actions.queue.get_nowait()['text']=='spawn an agent'
    hear(listener,'Hey Bart ordinary conversation')
    assert listener.capture_origin=='wake'
