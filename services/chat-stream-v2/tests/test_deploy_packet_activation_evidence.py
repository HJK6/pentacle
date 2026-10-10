"""Packet predicates run against complete files, real sockets and process readbacks."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest

PACKET = Path(__file__).resolve().parents[1] / 'deploy/packet'
ROOT = PACKET.parents[3]

def load(name):
    spec = importlib.util.spec_from_file_location('packet_' + name.replace('-', '_'), PACKET / (name + '.py'))
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

fw = load('full-window')
ae = load('activation-evidence')
from deploy.packet.log_scan import freeze, lines, IncompleteScan
from .test_deploy_script import deploy_mod

STAMP = '2026-01-01T12:00:00.000Z'
END = '2026-01-01T12:00:02Z'
CONN = 'a' * 32

def diag(event, conn=CONN, **extra):
    return STAMP + ' INFO conn_diag ' + json.dumps(dict(schema=1,event=event,conn_id=conn,transport='loopback',**extra)) + '\n'

def scan(tmp_path, content, attempts=None, proof=None):
    log = tmp_path/'source.log';log.write_text(content)
    offsets = tmp_path/'offsets.json';offsets.write_text(json.dumps([dict(path=str(log),size=0,inode=log.stat().st_ino,captured_utc=STAMP)]))
    runs = tmp_path/'attempts.json';runs.write_text(json.dumps(dict(attempts=attempts or [])))
    return fw.scan(ROOT,offsets,END,runs,tmp_path/'window.log',tmp_path/'receipt.json',watchdog_proof=proof)

def attempt(**changes):
    return dict(connected=True,endpoint='ws://127.0.0.1:7791',start=STAMP,end='2026-01-01T12:00:00.500Z',close_sent_code=1000,close_received_code=1000,**changes)

BOOT = 'chat_streamd_v2 listening on private\n'

def test_same_millisecond_clean_closes_need_no_attribution(tmp_path):
    content = BOOT+diag('connect')+diag('connect', 'b'*32)+diag('close',close_sent_code=1000,close_received_code=1000)
    result = scan(tmp_path,content,[attempt()])
    assert result['bind_expected'] and result['normal_unbound_attempts'] == [0]
    assert result['excluded_records'] == 0

@pytest.mark.parametrize('identity', [None, 'c'*32, CONN])
def test_true_abnormal_close_always_fails_without_exclusion(tmp_path, identity):
    content = BOOT+diag('connect')+diag('close',close_sent_code=1011,close_received_code=1011)
    row = attempt(welcome_conn_id=identity);row.update(close_sent_code=1011,close_received_code=1011)
    result = scan(tmp_path,content,[row])
    assert not result['bind_expected'] and result['classifier']['failure_count']==1
    assert result['excluded_records']==0
    assert bool(result['attribution']) == (identity==CONN)


def test_exact_id_ignores_concurrent_connection(tmp_path):
    content=BOOT+diag('connect')+diag('connect','b'*32)+diag('close',close_sent_code=1000,close_received_code=1000)
    result=scan(tmp_path,content,[attempt(welcome_conn_id=CONN)])
    assert result['bind_expected'] and result['attribution'][0]['conn_id']==CONN

@pytest.mark.parametrize('changes', [{'welcome_conn_id':'c'*32}, {'welcome_conn_id':CONN,'endpoint':'ws://wrong'}, {'welcome_conn_id':CONN,'close_received_code':None}])
def test_contradictory_identity_fails(tmp_path, changes):
    row=attempt();row.update(changes)
    result=scan(tmp_path,BOOT+diag('connect')+diag('close',close_sent_code=1000,close_received_code=1000),[row])
    assert not result['bind_expected'] and result['unattributed_attempts']==[0]


def test_failure_after_old_byte_cap_and_capped_examples(tmp_path):
    body=BOOT+(STAMP+' INFO '+('é'*1000)+'\n')*2600
    body+=''.join(diag('close',f'{index:032x}',close_code=1011) for index in range(140))
    result=scan(tmp_path,body)
    assert result['bytes_read']>4*1024*1024 and result['scan_complete']
    assert result['classifier']['failure_count']==140 and len(result['classifier']['failures'])==100
    assert not result['bind_expected']


def test_stream_split_utf8_and_lines(tmp_path, monkeypatch):
    import deploy.packet.log_scan as module
    monkeypatch.setattr(module,'CHUNK_BYTES',3)
    path=tmp_path/'utf8.log';path.write_text('aé界\n'+('é'*10)+'\n')
    assert ''.join(lines(freeze(path)))==path.read_text()

@pytest.mark.parametrize('fault',['rotation','truncate','oversized','unfinished','invalid_utf8'])
def test_incomplete_ranges_fail(tmp_path, fault):
    path=tmp_path/'source';path.write_text('x\n');item=freeze(path)
    if fault=='rotation':path.rename(tmp_path/'old');path.write_text('x\n')
    elif fault=='truncate':path.write_text('')
    elif fault=='oversized':path.write_text('x'*(1024*1024+1)+'\n');item=freeze(path)
    elif fault=='unfinished':path.write_text('partial');item=freeze(path)
    else:path.write_bytes(b'\xff\n');item=freeze(path)
    with pytest.raises((IncompleteScan,UnicodeError)):
        list(lines(item))


def test_watchdog_warning_requires_all_proofs(tmp_path):
    def write(name,value):
        p=tmp_path/name;p.write_text(json.dumps(value));return p
    policy=write('policy.json',{'watchdog':'absent','overlay':'none'})
    dep=write('dep.json',dict(watchdog_importable=False,watchdog_installed=False,overlay_empty=True))
    fallback=write('fallback.json',dict(passed=True,polling_observed=True,dependency_sha256=ae.digest(dep)))
    proof=write('proof.json',dict(policy_path=str(policy),policy_sha256=ae.digest(policy),dependency_path=str(dep),dependency_sha256=ae.digest(dep),fallback_path=str(fallback),fallback_sha256=ae.digest(fallback)))
    warning=STAMP+" WARNING _shared.specs_service specs watcher attach failed; falling back to polling: No module named 'watchdog'\n"
    assert scan(tmp_path,BOOT+warning,proof=proof)['bind_expected']
    assert not scan(tmp_path,BOOT+warning)['bind_expected']
    assert not scan(tmp_path,BOOT+warning.replace("No module named 'watchdog'",'permission denied'),proof=proof)['bind_expected']
    dep.write_text(json.dumps(dict(watchdog_importable=True,watchdog_installed=True,overlay_empty=True)))
    r=json.loads(proof.read_text());r['dependency_sha256']=ae.digest(dep);proof.write_text(json.dumps(r))
    assert not scan(tmp_path,BOOT+warning,proof=proof)['bind_expected']


def test_real_process_start_normalization_and_wrong_venv(tmp_path):
    if sys.platform!='darwin':pytest.skip('macOS lsof process evidence required')
    script=tmp_path/'wait.py';script.write_text('import _cffi_backend\nprint("ready",flush=True)\ninput()\n')
    argv=[sys.executable,'-u',str(script)]
    process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ready'
        facts=ae.collect(process.pid,argv,argv[0],argv,True);h='a'*64
        assert ae.validate_process(facts,argv,h,h,process.pid,facts['start_before']+'    ')['passed']
        for changes in ({'pid':process.pid+1},{'start_after':'stale'},{'open_files':['/other/.venv/lib/python3.13/site-packages/a.so']},{'launch_program':'/other/.venv/bin/python'}):
            with pytest.raises(AssertionError):ae.validate_process({**facts,**changes},argv,h,h,process.pid,facts['start_before'])
        with pytest.raises(AssertionError):ae.validate_process(facts,argv,h,'b'*64,process.pid,facts['start_before'])
    finally:
        process.communicate('\n',timeout=10)
    assert process.returncode==0


def test_scrub_is_child_only():
    scrub=load('scrub-gate');source=dict(TMUX='secret',TMUX_PANE='secret',PENTACLE_ASSISTANT_TOKEN='secret',PATH='retained')
    clean,removed=scrub.scrub_environment(source)
    assert clean=={'PATH':'retained'} and len(removed)==3
    assert source['TMUX']=='secret'


def test_pin_census_includes_active_beyond_fifty(tmp_path):
    with sqlite3.connect(tmp_path/'sessions.db') as db:db.execute('create table kv(k,v)')
    with sqlite3.connect(tmp_path/'notifications.db') as db:
        db.execute('create table notifications(notification_id,state,error_context,firing_count,last_fired_at)')
        for i in range(51):db.execute('insert into notifications values(?,?,?,?,?)',(str(i),'open',json.dumps(dict(code='pin_drift',condition='recovered' if i<50 else 'active',episode_id=str(i))),1,0))
    out=tmp_path/'out.json'
    subprocess.run([sys.executable,str(PACKET/'pin-readback.py'),'--state-root',str(tmp_path),'--output',str(out)],capture_output=True,text=True,check=True)
    result=json.loads(out.read_text())
    assert result['pin_drift_census']==dict(complete=True,total=51,active=1)
    assert result['active_pin_drift_facts'][0]['notification_id']=='50'


@pytest.mark.parametrize('code', [1002,1006,1011,4000])
def test_other_abnormal_closes_fail_even_without_probe_attempts(tmp_path, code):
    result=scan(tmp_path,BOOT+diag('close',close_code=code))
    assert not result['bind_expected'] and result['abnormal_close_count']==1


def test_deploy_guard_reads_late_failure_and_detects_rotation(tmp_path, monkeypatch):
    log=tmp_path/'daemon.log';log.write_text('old\n')
    monkeypatch.setattr(deploy_mod,'_daemon_log_paths',lambda _: (log,))
    offsets=deploy_mod._capture_log_offsets((log,))
    with log.open('a') as output:
        output.write(BOOT+('INFO '+('x'*1000)+'\n')*4300+diag('close',close_code=1011))
    result=deploy_mod._scan_slow_consumer_window(deploy_mod.SERVICES['chat-streamd-v2'],offsets)
    assert result['scan_complete'] and result['failure_count']==1 and result['outcome']=='failed'
    log.rename(tmp_path/'prior');log.write_text('old\n'+BOOT)
    result=deploy_mod._scan_slow_consumer_window(deploy_mod.SERVICES['chat-streamd-v2'],offsets)
    assert not result['scan_complete'] and result['outcome']=='failed'


@pytest.mark.asyncio
async def test_actual_server_retry_inventory_store_and_complete_window(tmp_path):
    import asyncio
    import io
    import logging
    import time
    from datetime import datetime, timezone
    from websockets.asyncio.client import connect
    from server import Server
    from notification_answer_fixture import fixture
    probe=load('health-probe')
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    buffer=io.StringIO();handler=logging.StreamHandler(buffer)
    formatter=logging.Formatter('%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s','%Y-%m-%dT%H:%M:%S')
    formatter.converter=time.gmtime;handler.setFormatter(formatter)
    logger=logging.getLogger('chat_streamd_v2.server');level=logger.level;logger.setLevel(logging.INFO);logger.addHandler(handler)
    server=None;release=None
    start=datetime.now(timezone.utc).isoformat()
    try:
        async with fixture(tmp_path,host='fixture') as (_,_,comms,_,sessions,store):
            server=Server(store=store,sessions=sessions,comms=comms,local_host='fixture',binds=['127.0.0.1'],port=0,dot_tls_port=0,dot_tls_binds=[],dot_tls_cert='',dot_tls_key='')
            server.runtime_sha='';server.spawn_ready.clear();server.inventory_ready.clear()
            port=await server.bind();url=f'ws://127.0.0.1:{port}'
            async def ready():
                while sum('end' in row for row in probe.ATTEMPTS)<2:
                    await asyncio.sleep(.01)
                server.runtime_sha=sha;server.spawn_ready.set();server.inventory_ready.set()
            release=asyncio.create_task(ready())
            async with connect(url) as peer:
                assert json.loads(await peer.recv())['type']=='welcome'
                result=await asyncio.wait_for(asyncio.to_thread(probe.probe,sha,url),20)
                await release
                # The concurrent socket exercises a real Store-backed read after startup.
                await peer.send(json.dumps({'type':'hello','client':'agent-orch','subscribe':{'snapshot':False}}))
                await peer.send(json.dumps({'type':'list_sessions','request_id':'packet-inventory'}))
                while True:
                    frame=json.loads(await asyncio.wait_for(peer.recv(),3))
                    if frame.get('type')=='list_sessions.ok':break
                assert any(row['session_name']=='v2-test' for row in frame['active'])
                assert (await asyncio.wait_for(store.fetch_session('fixture','v2-test'),3))['status']=='open'
            await server.close()
        assert result['runtime_sha']==sha and sum(row['failed'] for row in probe.ATTEMPTS)>=2
        assert all(row['close_sent_code']==row['close_received_code']==1000 for row in probe.ATTEMPTS)
        end=datetime.now(timezone.utc).isoformat()
        log=tmp_path/'boot.log';log.write_text(BOOT+buffer.getvalue())
        offsets=tmp_path/'offsets.json';offsets.write_text(json.dumps([dict(path=str(log),inode=log.stat().st_ino,size=0,captured_utc=start)]))
        attempts=tmp_path/'attempts.json';attempts.write_text(json.dumps({'attempts':probe.ATTEMPTS}))
        result=fw.scan(ROOT,offsets,end,attempts,tmp_path/'selected.log',tmp_path/'receipt.json',expected_endpoint=url)
        assert result['scan_complete'] and result['bind_expected'] and result['excluded_records']==0
    finally:
        if release and not release.done():release.cancel();await asyncio.gather(release,return_exceptions=True)
        if server:await server.close()
        logger.removeHandler(handler);logger.setLevel(level)
    assert server._ws_server is None
