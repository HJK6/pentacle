"""Cursor provider acceptance: no live model calls."""
import pytest
from _shared import spawn_profiles


def test_cursor_catalog_and_effort_contract():
    assert spawn_profiles.resolve_spawn(provider='cursor', model='auto', effort='default')['model'] == 'auto'
    for model, effort, code in [('auto', 'high', 'spawn_effort_unsupported'), ('grok-4.7', 'default', 'spawn_model_unsupported')]:
        with pytest.raises(spawn_profiles.SpawnProfileError) as error:
            spawn_profiles.resolve_spawn(provider='cursor', model=model, effort=effort)
        assert error.value.code == code

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import uuid

import launch
import cursor_admission
from boot_ready import (cursor_prompt_ready, cursor_prompt_submitted,
                        cursor_prompt_in_active_draft, submission_proven_after)
from cursor_receipts import high_water, receipt_key
from cursor_stream_norm import normalize_cursor_stream_records
from provider_accounts import transcript_allowed
from machines import MachineConfig
from ingest import _provider_command_matches, _native_session_identity_from_fd, _StreamIngest, _close_stream

SID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
CHAT = '11111111-2222-4333-8444-555555555555'
FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def fake_agent(tmp_path):
    binary = tmp_path / 'fake agent'
    binary.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time, subprocess, signal
from pathlib import Path
args=sys.argv[1:]
mode=os.environ.get('CURSOR_FAKE_MODE','ok')
chat='11111111-2222-4333-8444-555555555555'
if args[0]=='--version':
 print('bad' if mode=='version' else '2026.10.22-27dcf5f'); sys.exit(0)
if args[0]=='status':
 if mode=='status_timeout':time.sleep(30)
 if mode=='status_bad':print('bad');sys.exit(0)
 if mode=='status_missing':print('{}');sys.exit(0)
 if mode=='status_exit':sys.exit(1)
 print(json.dumps({'isAuthenticated':mode!='signed_out'}));sys.exit(0)
if args[0]=='create-chat':
 if mode=='create_timeout':time.sleep(30)
 print('bad' if mode=='create_bad' else chat,flush=True)
 sys.exit(0 if os.isatty(0) and os.isatty(1) else 9)
# Model the pinned commander's option boundary, including last-option-wins.
options={};operands=[];i=0
while i<len(args):
 arg=args[i];i+=1
 if arg=='--':operands.extend(args[i:]);break
 name,sep,value=arg.partition('=')
 if name in ('--workspace','--resume','--output-format','--model'):
  if not sep:value=args[i];i+=1
  options[name]=value
 elif arg in ('-p','--trust','--force','--disable-auto-update','--stream-partial-output'):pass
 elif arg.startswith('-') and len(arg)>1:
  print('unknown option',file=sys.stderr);sys.exit(1)
 else:operands.append(arg)
prompt=' '.join(operands)
Path(os.environ['CURSOR_FAKE_CALLS']).open('a').write(json.dumps({'argv':args,'options':options,'prompt':prompt,'pid':os.getpid(),'pgid':os.getpgrp()})+'\\n')
def emit(d):print(json.dumps(d),flush=True)
emit({'type':'system','subtype':'init','session_id':'wrong' if prompt=='mismatch' else chat})
emit({'type':'user','session_id':chat,'message':{'content':[{'type':'text','text':prompt}]}})
if prompt=='plan_error':
 print('ActionRequiredError: secret detail',file=sys.stderr,flush=True);sys.exit(1)
if prompt.startswith('slow'):
 sub=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
 Path(os.environ['CURSOR_FAKE_CALLS']+'.child').write_text(str(sub.pid))
 def stop(*_):
  sub.terminate();sub.wait();sys.exit(143)
 signal.signal(signal.SIGTERM,stop)
 time.sleep(20)
emit({'type':'assistant','message':{'content':[{'type':'text','text':'OK'}]}})
emit({'type':'result','subtype':'success','is_error':False,'session_id':chat})
''')
    binary.chmod(0o700)
    return binary


def plan(machine, **kwargs):
    return launch.build_launch(machine, provider='cursor', tmux_session='cursor-test',
                               launch_model='auto', launch_effort='default', **kwargs)


def test_admission_and_launch_identity(tmp_path, fake_agent):
    machine = launch.local_machine('local', cwd=str(tmp_path), cursor_agent_bin=str(fake_agent))
    p = plan(machine)
    assert p.cursor_binding['chat_id'] == CHAT
    assert p.cursor_binding['cwd'] == str(tmp_path.resolve())
    assert p.jsonl_path.endswith(p.session_id + '.jsonl')
    assert p.cursor_binding['transcript_root'] == str(tmp_path / '.cursor/sessions')
    assert '--chat-id ' + CHAT in p.command
    assert 'AGENT_ORCH_STREAM_ID=local:cursor-test' in p.command
    assert ' exec ' in p.command


@pytest.mark.parametrize('mode,code', [('signed_out','cursor_not_authenticated'),('status_bad','cursor_status_unreadable'),
    ('status_missing','cursor_status_unreadable'),('status_exit','cursor_status_unreadable'),
    ('create_bad','cursor_chat_create_failed'),('version','cursor_version_mismatch')])
def test_admission_failures(tmp_path, fake_agent, monkeypatch, mode, code):
    monkeypatch.setenv('CURSOR_FAKE_MODE',mode)
    machine = launch.local_machine('local', cwd=str(tmp_path), cursor_agent_bin=str(fake_agent))
    with pytest.raises(ValueError, match=code):plan(machine)


def test_admission_timeouts_are_bounded(tmp_path, fake_agent, monkeypatch):
    machine = launch.local_machine('local', cwd=str(tmp_path), cursor_agent_bin=str(fake_agent))
    real = cursor_admission.subprocess.run
    def short(*args, **kwargs):
        kwargs['timeout'] = .15
        return real(*args, **kwargs)
    monkeypatch.setattr(cursor_admission.subprocess,'run',short)
    monkeypatch.setenv('CURSOR_FAKE_MODE','status_timeout')
    with pytest.raises(ValueError, match='cursor_status_unreadable'):plan(machine)
    monkeypatch.setenv('CURSOR_FAKE_MODE','create_timeout')
    with pytest.raises(ValueError, match='cursor_chat_create_failed'):
        cursor_admission._create_chat(str(fake_agent),str(tmp_path),timeout=.15)


def test_excluded_launches_and_bad_paths(tmp_path, fake_agent):
    machine = launch.local_machine('local',cwd=str(tmp_path),cursor_agent_bin=str(fake_agent))
    with pytest.raises(ValueError,match='provider_executable_missing'):
        plan(replace(machine,cursor_agent_bin=str(tmp_path/'absent')))
    with pytest.raises(ValueError,match='cursor_cwd_invalid'):plan(machine,cwd=str(tmp_path/'absent'))
    with pytest.raises(ValueError,match='resume_unsupported_provider'):plan(machine,resume_session_id=SID)
    with pytest.raises(ValueError,match='cursor_named_accounts_unsupported'):plan(machine,account_binding=object())
    cfg=MachineConfig('remote','example.invalid',cwd='/work')
    with pytest.raises(ValueError,match='cursor_remote_unsupported'):launch.machine_from_config(cfg,provider='cursor')


def test_config_paths_roundtrip(tmp_path):
    from machines import _machine_from_dict
    cfg=_machine_from_dict({'name':'local','cwd':str(tmp_path),'cursor_agent_bin':'/pinned/cursor-agent',
                            'cursor_repl':'/app/cursor_repl.py','cursor_version':'test'})
    machine=launch.local_machine_from_config(cfg)
    assert (machine.cursor_agent_bin,machine.cursor_repl,machine.cursor_version)==('/pinned/cursor-agent','/app/cursor_repl.py','test')


def _transcript(tmp_path, cwd):
    path=cwd/'.cursor/sessions'/f'{SID}.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'type':'pentacle_repl','version':1,'session_id':SID,'chat_id':CHAT})+'\n')
    path.chmod(0o600)
    binding={'provider':'cursor','cwd':str(cwd),'transcript_root':str(path.parent),
             'session_id':SID,'jsonl_path':str(path),'chat_id':CHAT}
    return path,binding


@pytest.mark.parametrize('place',['equal','subdir','outside'])
def test_transcript_is_bound_to_seat_not_machine_cwd(tmp_path,place):
    machine=tmp_path/'machine';machine.mkdir()
    seat=machine if place=='equal' else (machine/'sub' if place=='subdir' else tmp_path/'outside')
    seat.mkdir(exist_ok=True)
    path,binding=_transcript(tmp_path,seat)
    assert transcript_allowed(str(path),'cursor',binding)
    assert not transcript_allowed(str(path),'cursor')
    assert not transcript_allowed(str(path),'cursor',{**binding,'provider':'codex'})
    assert not transcript_allowed(str(path),'cursor',{**binding,'session_id':'other'})
    assert not transcript_allowed(str(path),'cursor',{**binding,'transcript_root':str(machine)})
    if seat!=machine:
        foreign,_=_transcript(tmp_path,machine)
        assert not transcript_allowed(str(foreign),'cursor',binding)
    path.chmod(0o644)
    assert not transcript_allowed(str(path),'cursor',binding)
    path.chmod(0o600)
    target=path.with_name('target.jsonl');path.rename(target);path.symlink_to(target)
    assert not transcript_allowed(str(path),'cursor',binding)


def test_symlink_parent_and_wrong_owner_refuse(tmp_path,monkeypatch):
    path,binding=_transcript(tmp_path,tmp_path)
    sessions=path.parent; relocated=tmp_path/'relocated';sessions.rename(relocated);sessions.symlink_to(relocated)
    assert not transcript_allowed(str(path),'cursor',binding)
    sessions.unlink();relocated.rename(sessions)
    monkeypatch.setattr(os,'getuid',lambda: path.stat().st_uid+1)
    assert not transcript_allowed(str(path),'cursor',binding)


def test_receipt_predicates_do_not_admit_queue_starts_or_old_prompts():
    baseline='» #1 other\n» #2 queued-prompt\n'
    after=baseline+'▶ #2\n─ #2 done 10 ms\n! #2 other\n'
    assert not submission_proven_after(after,baseline,'queued-prompt','cursor')
    assert submission_proven_after(after+'» #3 queued-prompt\n',baseline,'queued-prompt','cursor')
    assert not submission_proven_after(after+'» #3 unrelated\n',baseline,'queued-prompt','cursor')
    prompt='\n \n  '+('long ' * 50)+'\nsecond line'
    receipt='» #3 '+receipt_key(prompt)+'\n'
    assert len(receipt_key(prompt))<=120
    assert submission_proven_after(receipt,baseline,prompt,'cursor')
    assert cursor_prompt_ready('─ #1 done 1 ms\n ›  \n')
    assert not cursor_prompt_ready('! identity mismatch\n›\n')
    assert cursor_prompt_in_active_draft('› unfinished prompt','unfinished prompt')
    assert high_water(baseline)==2


def test_native_identity_and_root_pair(tmp_path):
    path,binding=_transcript(tmp_path,tmp_path)
    with path.open() as f:assert _native_session_identity_from_fd(f.fileno(),'cursor')==SID
    python=os.path.realpath(sys.executable);repl=str(Path(launch.__file__).with_name('cursor_repl.py'))
    root={'python':python,'repl':repl}
    assert _provider_command_matches(shlex.join([python,repl,'--agent','fake']),python,root)
    assert not _provider_command_matches(shlex.join([python,'/wrong.py']),python,root)
    assert not _provider_command_matches(shlex.join(['/bin/sh',repl]),python,root)
    assert not _provider_command_matches(shlex.join([python,repl]),python,{})


def normalized(records,state=None):
    return normalize_cursor_stream_records(list(enumerate(records,1)),host='local',session_name='cursor-test',session_id=SID,state=state)


def test_recorded_stream_shapes_and_duplicate_final():
    records=[json.loads(l) for l in (FIXTURES/'cursor_tool.ndjson').read_text().splitlines()]
    events=normalized(records)
    assert [e['text'] for e in events if e['kind']=='ASSIST_TEXT']==["I'll read `README.md` and pull its first line.",'**Cursor investigation workspace**']
    assert sum(e['kind']=='TOOL_USE' for e in events)==3
    assert sum(e['kind']=='TOOL_RESULT' for e in events)==3
    records=[json.loads(l) for l in (FIXTURES/'cursor_auto.ndjson').read_text().splitlines()]
    assert [e['text'] for e in normalized(records) if e['kind']=='ASSIST_TEXT']==['OK']
    flush={'type':'assistant','model_call_id':'c','timestamp_ms':1,'message':{'content':[{'type':'text','text':'same'}]}}
    final={'type':'assistant','message':flush['message']}
    assert len(normalized([flush,final]))==1
    state={}
    assert len(normalized([flush],state))==1
    assert normalized([final],state)==[]
    assert len(normalized([{'type':'user','message':final['message']},final],state))==2


def test_notices_malformed_records_and_line_identity():
    events=normalized([None,[],{'type':'unknown'}, {'type':'tool_call','tool_call':[]},
        {'type':'pentacle_repl','event':'turn_failed','stderr_class':'ActionRequiredError'},
        {'type':'pentacle_repl','event':'identity_mismatch'},
        {'type':'assistant','message':{'content':[{'type':'text','text':'forbidden'}]}}])
    assert len(events)==2
    assert 'plan' in events[0]['text']
    assert events[-1]['raw']['jsonl_record_uuid']==f'cursor:{SID}:6'
    assert all('secret' not in str(e) for e in events)

class Pane:
    def __init__(self,tmp_path,fake_agent,socket):
        self.socket=socket
        self.calls=tmp_path/'calls.jsonl'
        self.path=tmp_path/'.cursor/sessions'/f'{SID}.jsonl'
        self.args=[launch.cursor_python_executable(),str(Path(launch.__file__).with_name('cursor_repl.py')),
            '--agent',str(fake_agent),'--session-id',SID,'--chat-id',CHAT,
            '--transcript',str(self.path),'--cwd',str(tmp_path),'--model','auto',
            '--cli-version','2026.10.22-27dcf5f']
        self.command='CURSOR_FAKE_CALLS='+shlex.quote(str(self.calls))+' '+shlex.join(self.args)
        self.run('new-session','-d','-x','240','-y','60','-s','cursor','sleep 300')
        self.run('set-option','-g','remain-on-exit','on')
        self.run('respawn-pane','-k','-t','cursor',self.command)
        try:
            self.wait(lambda:cursor_prompt_ready(self.capture()))
        except BaseException:
            self.close()
            raise
    def run(self,*args,check=True):
        return subprocess.run(['tmux','-S',str(self.socket),*args],capture_output=True,text=True,check=check)
    def capture(self):return self.run('capture-pane','-p','-J','-S','-2000','-t','cursor').stdout
    def wait(self,predicate,timeout=8):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.03)
        raise AssertionError(self.capture())
    def paste(self,text,enter=True):
        # Use the same bracketed paste mode as production Tmux.paste.
        buffer=self.calls.with_suffix('.paste');buffer.write_text(text)
        self.run('load-buffer','-b','cursor-input',str(buffer))
        self.run('paste-buffer','-b','cursor-input','-t','cursor','-d','-p')
        if enter:self.run('send-keys','-t','cursor','Enter')
    def records(self):return [json.loads(l) for l in self.path.read_text().splitlines()]
    def receipts(self):return [json.loads(l) for l in self.calls.read_text().splitlines()] if self.calls.exists() else []
    def dead(self):return self.run('display-message','-p','-t','cursor','#{pane_dead}').stdout.strip()=='1'
    def close(self):self.run('kill-server',check=False)


@pytest.fixture
def pane(tmp_path,fake_agent):
    p=None
    socket=Path('/tmp') / ('cursor-' + uuid.uuid4().hex + '.sock')
    try:
        p=Pane(tmp_path,fake_agent,socket)
        yield p
    finally:
        # Also clean the server if construction/boot assertion failed.
        subprocess.run(['tmux','-S',str(socket),'kill-server'],capture_output=True)


def test_tmux_multiline_long_and_repeated_admissions(pane):
    header=pane.records()[0]
    assert header['session_id']==SID and header['chat_id']==CHAT
    assert pane.path.stat().st_mode & 0o777 == 0o600
    for i,message in enumerate(['one','\n \n'+('x'*151)+'\nsecond line','same','same'],1):
        baseline=pane.capture()
        pane.paste(message)
        pane.wait(lambda:len(pane.receipts())==i and cursor_prompt_ready(pane.capture()))
        assert submission_proven_after(pane.capture(),baseline,message,'cursor')
        assert pane.receipts()[-1]['prompt']==message.strip()
    assert high_water(pane.capture())==4
    before=pane.path.read_bytes();pane.paste('/exit');pane.wait(pane.dead)
    assert pane.path.read_bytes().startswith(before)
    assert pane.records()[-1]['event']=='exit'


@pytest.mark.parametrize('message',['- item','--model=other model','--workspace=/other',
                                  '--resume=other-chat','--version','-h','--'])
def test_tmux_leading_hyphen_prompt_remains_data(pane,message):
    pane.paste(message)
    pane.wait(lambda:any(r.get('type')=='result' or r.get('event')=='turn_failed'
                         for r in pane.records()))
    assert not any(r.get('event')=='turn_failed' for r in pane.records())
    call=pane.receipts()[-1]
    assert call['prompt']==message
    assert call['options']['--model']=='auto'
    assert call['options']['--workspace']==str(pane.path.parents[2])
    assert call['options']['--resume']==CHAT
    assert call['options']['--output-format']=='stream-json'
    users=[r for r in pane.records() if r.get('type')=='user']
    assert users[-1]['message']['content'][0]['text']==message


def test_tmux_busy_queue_receipt_and_ctrl_c_cleanup(pane):
    pane.paste('slow first')
    pane.wait(lambda:len(pane.receipts())==1)
    child_pid_file=Path(str(pane.calls)+'.child')
    pane.wait(child_pid_file.exists)
    baseline=pane.capture();pane.paste('queued-prompt')
    pane.wait(lambda:high_water(pane.capture())==2,timeout=2)
    assert submission_proven_after(pane.capture(),baseline,'queued-prompt','cursor')
    admitted=pane.capture()
    pane.run('send-keys','-t','cursor','C-c')
    pane.wait(lambda:len(pane.receipts())==2 and cursor_prompt_ready(pane.capture()))
    assert not submission_proven_after(pane.capture(),admitted,'queued-prompt','cursor')
    assert any(d.get('event')=='interrupted' for d in pane.records())
    for pid in [pane.receipts()[0]['pgid'],int(child_pid_file.read_text())]:
        with pytest.raises(ProcessLookupError):os.kill(pid,0)
    pane.paste('after interrupt');pane.wait(lambda:len(pane.receipts())==3 and cursor_prompt_ready(pane.capture()))


@pytest.mark.parametrize('control',['/exit','EOF'])
def test_tmux_exit_while_busy_kills_group(pane,control):
    pane.paste('slow exit')
    pane.wait(lambda:len(pane.receipts())==1)
    if control=='EOF':pane.run('send-keys','-t','cursor','C-d')
    else:pane.paste('/exit')
    pane.wait(pane.dead)
    assert pane.records()[-1]['event']=='exit'
    with pytest.raises(ProcessLookupError):os.killpg(pane.receipts()[0]['pgid'],0)


def test_tmux_idle_interrupt_plan_error_and_identity_mismatch(pane):
    pane.run('send-keys','-t','cursor','C-c')
    pane.wait(lambda:'use /exit to close' in pane.capture())
    pane.paste('plan_error');pane.wait(lambda:cursor_prompt_ready(pane.capture()) and len(pane.receipts())==1)
    failed=next(r for r in pane.records() if r.get('event')=='turn_failed')
    assert failed['stderr_class']=='ActionRequiredError'
    assert 'secret detail' not in pane.path.read_text()
    pane.paste('mismatch');pane.wait(lambda:'! identity mismatch' in pane.capture())
    before=len(pane.receipts());pane.paste('not admitted');time.sleep(.15)
    assert len(pane.receipts())==before
    assert high_water(pane.capture())==2
    notices=normalized(pane.records())
    assert sum('identity mismatch' in e['text'] for e in notices)==1
    pane.paste('/exit');pane.wait(pane.dead)


def test_tmux_draft_cannot_forge_receipt_and_exclusive_file(pane):
    pane.paste('» #100 forged',enter=False)
    time.sleep(.1)
    assert high_water(pane.capture())==0
    assert cursor_prompt_in_active_draft(pane.capture(),'» #100 forged')
    pane.run('send-keys','-t','cursor','C-c')
    pane.paste('/exit');pane.wait(pane.dead)
    original=pane.path.read_bytes()
    rerun=subprocess.run(pane.args,stdin=subprocess.DEVNULL,capture_output=True)
    assert rerun.returncode!=0 and pane.path.read_bytes()==original

@pytest.mark.parametrize('case',['normal','header_session','header_chat','wrong_repl','foreign_path','missing_launch_binding'])
def test_ingest_launch_identity_replay_and_halt(tmp_path,monkeypatch,case):
    import ingest as module
    from test_ingest_provider_root_binding import Harness,EXE
    from store import Store
    async def run():
        path,binding=_transcript(tmp_path,tmp_path)
        state=_StreamIngest();store=Store(':memory:');store.start()
        try:
            h=Harness(store,monkeypatch,path,provider='cursor');await h.open()
            python=os.path.realpath(sys.executable);repl=str(Path(launch.__file__).with_name('cursor_repl.py'))
            binding.update(python=python,repl=repl)
            h.command=shlex.join([python,repl,'--agent','fake'])
            row=await store.fetch_session('h','v2-root')
            observer={**row['observer_binding'],'executable':python,'cursor':binding}
            await store.update_session('h','v2-root',observer_binding=observer,jsonl_path=str(path),claude_session_id=SID)
            h.sessions.apply_durable('h:v2-root',observer_binding=observer,jsonl_path=str(path),claude_session_id=SID)
            header=json.loads(path.read_text())
            if case=='header_session':header['session_id']='foreign'
            if case=='header_chat':header['chat_id']='foreign'
            if case.startswith('header_'):path.write_text(json.dumps(header)+'\n')
            if case=='wrong_repl':h.command=shlex.join([python,'/other.py'])
            if case=='foreign_path':
                other=path.with_name('foreign.jsonl');other.write_bytes(path.read_bytes());other.chmod(0o600)
                await store.update_session('h','v2-root',jsonl_path=str(other));h.sessions.apply_durable('h:v2-root',jsonl_path=str(other))
            if case=='missing_launch_binding':
                observer.pop('cursor');await store.update_session('h','v2-root',observer_binding=observer);h.sessions.apply_durable('h:v2-root',observer_binding=observer)
            pump=h.ingest()
            assert await h.run(pump,state)==0  # idle/header only or refused
            with path.open('a') as f:
                f.write('malformed\n')
                f.write(json.dumps({'type':'user','message':{'content':[{'type':'text','text':'one'}]}})+'\n')
                f.write(json.dumps({'type':'assistant','model_call_id':'flush','timestamp_ms':1,'message':{'content':[{'type':'text','text':'OK'}]}})+'\n')
                f.write(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'OK'}]}})+'\n')
            inserted=await h.run(pump,state)
            if case!='normal':
                assert inserted==0
                assert not await store.fetch_session_event_tail('h:v2-root',limit=50)
                return
            assert inserted==2
            events=await store.fetch_session_event_tail('h:v2-root',limit=50)
            assert [e['kind'] for e in events]==['USER','ASSIST_TEXT']
            assert events[0]['raw']['jsonl_record_uuid']==f'cursor:{SID}:3'
            _close_stream(state);state=_StreamIngest()
            assert await h.run(h.ingest(),state)==0  # durable line-identity replay
            with path.open('a') as f:
                f.write(json.dumps({'type':'pentacle_repl','event':'identity_mismatch'})+'\n')
                f.write(json.dumps({'type':'user','message':{'content':[{'type':'text','text':'forbidden'}]}})+'\n')
            assert await h.run(pump,state)==1
            assert await h.run(pump,state)==0
            assert len(await store.fetch_session_event_tail('h:v2-root',limit=50))==3
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


def test_daemon_resolution_carries_launch_binding_and_refusal_codes(tmp_path,fake_agent,monkeypatch):
    from spawnctl import SpawnCtl
    from types import SimpleNamespace
    from sessions import VerbError
    machine=launch.local_machine('local',cwd=str(tmp_path),cursor_agent_bin=str(fake_agent))
    ctl=SpawnCtl(store=None,sessions=SimpleNamespace(local_host='local'),tmux=None,machine=machine)
    command,resolution,overrides=asyncio.run(ctl._resolve_launch({'provider':'cursor','model':'auto','effort':'default'},'local','cursor-test'))
    assert resolution['actual_launch_tuple']=={'provider':'cursor','model':'auto','effort':'default'}
    binding=overrides['observer_binding']['cursor']
    assert binding['chat_id']==CHAT and binding['session_id']==overrides['claude_session_id']
    assert overrides['jsonl_path']==binding['jsonl_path']
    assert overrides['observer_binding']['executable']==launch.cursor_python_executable()
    assert '--resume' not in command  # native resume is per turn, never public resume
    for msg,host,code in [({'account':'named'},'local','cursor_named_accounts_unsupported'),({},'remote','cursor_remote_unsupported')]:
        with pytest.raises(VerbError) as e:
            asyncio.run(ctl._resolve_launch({'provider':'cursor','model':'auto','effort':'default',**msg},host,'cursor-test'))
        assert e.value.code==code
    monkeypatch.setenv('CURSOR_FAKE_MODE','signed_out')
    with pytest.raises(VerbError) as e:
        asyncio.run(ctl._resolve_launch({'provider':'cursor','model':'auto','effort':'default'},'local','cursor-test'))
    assert e.value.code=='cursor_not_authenticated'


def test_admission_probes_leave_event_loop_responsive(tmp_path,fake_agent,monkeypatch):
    from spawnctl import SpawnCtl
    from types import SimpleNamespace
    machine=launch.local_machine('local',cwd=str(tmp_path),cursor_agent_bin=str(fake_agent))
    ctl=SpawnCtl(store=None,sessions=SimpleNamespace(local_host='local'),tmux=None,machine=machine)
    original=cursor_admission.prepare_cursor
    def delayed(*args,**kwargs):time.sleep(.15);return original(*args,**kwargs)
    monkeypatch.setattr(cursor_admission,'prepare_cursor',delayed)
    async def run():
        task=asyncio.create_task(ctl._resolve_launch({'provider':'cursor','model':'auto','effort':'default'},'local','cursor-test'))
        await asyncio.sleep(.03)
        assert not task.done()
        await task
    asyncio.run(run())

@pytest.mark.requires('process start identity readable')
def test_real_pane_root_and_durable_ingest_journey(pane):
    """Unmocked ps + real pane + real append file + real event store."""
    from prockill import process_record
    from sessions import Sessions
    from store import Store
    from ingest import Ingest
    async def run():
        pid=pane.run('display-message','-p','-t','cursor','#{pane_pid}').stdout.strip()
        record=await process_record(pid)
        assert record, 'real provider process identity unavailable'
        binding={'provider':'cursor','session_id':SID,'chat_id':CHAT,'cwd':str(pane.path.parents[2]),
                 'transcript_root':str(pane.path.parent),'jsonl_path':str(pane.path),
                 'python':launch.cursor_python_executable(),'repl':str(Path(launch.__file__).with_name('cursor_repl.py'))}
        assert _provider_command_matches(record['command'],binding['python'],binding)
        store=Store(':memory:');store.start();state=_StreamIngest()
        sessions=Sessions(store,local_host='local');frames=[]
        class Transport:
            async def pane_pid(self,_):return pid
        async def broadcast(frame):frames.append(frame)
        try:
            assert await store.reserve_stream_id('local','cursor',ttl_s=60,request_id='owned',nonce='nonce')
            assert await store.record_spawn_intent('local','cursor',{'open_fields':{'session_generation':'gen'}},request_id='owned',nonce='nonce')
            assert await store.commit_tmux_created_fenced('local','cursor',request_id='owned',nonce='nonce',pane_pid=pid,pane_started_at=record['start_id'])
            await sessions.open('local','cursor',fence='owned',provider='cursor',pane_pid=pid,session_generation='gen',
                observer_binding={'executable':binding['python'],'cursor':binding},jsonl_path=str(pane.path),claude_session_id=SID)
            pump=Ingest(store,sessions,Transport(),broadcast,local_host='local',recent_limit=100)
            row=sessions.get('local:cursor')
            assert await pump._provider_root(row,'cursor') is not None
            assert await pump._ingest_stream(row,state,100)==0
            before=pane.capture();pane.paste('real journey\nsecond line')
            pane.wait(lambda:len(pane.receipts())==1 and cursor_prompt_ready(pane.capture()))
            assert submission_proven_after(pane.capture(),before,'real journey\nsecond line','cursor')
            assert await pump._ingest_stream(row,state,100)==2
            assert [e['kind'] for e in await store.fetch_session_event_tail('local:cursor',limit=100)]==['USER','ASSIST_TEXT']
            _close_stream(state);state=_StreamIngest()
            assert await pump._ingest_stream(row,state,100)==0
            assert len([f for f in frames if f.get('type')=='chat.event'])==2
        finally:_close_stream(state);store.stop()
    asyncio.run(run())
