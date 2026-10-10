#!/usr/bin/env python3
"""External synthetic U4 fault harness; does not modify product sources.

The launch cell executes actual main.run + Server + Store. Only fault injection,
control, and measurement are test-owned. An independent process reads the product
progress path and can send to the packet's isolated HTTP sink. First-bind absence
is reported unavailable, never fabricated healthy. This is not a private checker
or carrier-delivery implementation.
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, signal, socket, subprocess, sys
import shutil, tempfile, threading, time, traceback, urllib.request
from pathlib import Path

BASE = '70fb76a1ef8d0db53a26b67d89f3c162e70570c6'
HERE = Path(__file__).resolve().parent
SINK_SCRIPT = HERE / 'u4_sink_server.py'
TMUX = Path(shutil.which('tmux') or 'tmux')
SCENARIO = 'held'

def dump(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.chmod(0o600)
    tmp.replace(path)

def scrub():
    return {k:v for k,v in os.environ.items()
            if not k.startswith(('PENTACLE_', 'AGENT_ORCH_')) and k not in ('TMUX','TMUX_PANE')}

def launch_daemon(source, root):
    sys.path[:0] = [str(source/'services/chat-stream-v2'), str(source/'services')]
    import main
    from server import Server
    from store import Store
    root = root.resolve()
    events=[]; lock=threading.Lock(); owned={}; barriers={}; stopping=threading.Event()
    def event(name, **data):
        with lock:
            row={'event':name,'mono_s':time.monotonic(),'wall_s':time.time(), **data}
            events.append(row); dump(root/'events.json', events)
        return row
    original_init=Store.__init__
    def store_init(self,*a,**kw):
        original_init(self,*a,**kw)
        if a and str(a[0]) == str(root/'sessions.db'): owned['store']=self
    Store.__init__=store_init
    original_bind=Server.bind
    async def bind(self):
        port=await original_bind(self); owned['server']=self; owned['loop']=asyncio.get_running_loop()
        event('bound', port=port, pid=os.getpid())
        return port
    Server.bind=bind
    original_configure=Server.configure_error_alerts
    async def configure(self,*a,**kw):
        result=await original_configure(self,*a,**kw)
        event('ready',port=self.port,store_thread_alive=self.store._thread.is_alive())
        return result
    Server.configure_error_alerts=configure
    def hold_main():
        gate=barriers['main']; event('main_held', thread=threading.current_thread().name)
        released=gate.wait(30)
        event('main_released', explicit_release=released)
    async def hold_store():
        def callback(conn):
            event('store_held',thread=threading.current_thread().name,sqlite_probe=conn.execute('SELECT 1').fetchone()[0])
            released=barriers['store'].wait(30)
            event('store_released',explicit_release=released)
            return 1
        await owned['store'].submit(callback)
        event('store_hold_await_completed')
    async def queue_read():
        enqueue=event('queued_read_enqueued')['mono_s']
        def callback(conn):
            event('queued_read_started',queue_wait_s=time.monotonic()-enqueue)
            return conn.execute('SELECT 42').fetchone()[0]
        value=await owned['store'].submit(callback)
        event('queued_read_completed',value=value)
    # Test-only fault injection into the real publisher, after its last publish.
    try:
        from loop_watchdog import LoopWatchdog
    except ImportError:
        LoopWatchdog = None
    if LoopWatchdog is not None:
        original_exchange = LoopWatchdog._exchange
        def exchange(self, snapshot):
            result = original_exchange(self, snapshot)
            gate = barriers.get('publisher')
            if gate is not None and not gate.is_set():
                event('publisher_held', thread=threading.current_thread().name)
                released = gate.wait(30)
                event('publisher_released', explicit_release=released)
            return result
        LoopWatchdog._exchange = exchange
    def control():
        seen = None
        while not stopping.is_set():
            try:
                request=json.loads((root/'control-request.json').read_text())
                if request['id']==seen: stopping.wait(.02); continue
                seen=request['id']; cmd=request['command']
                if cmd in ('hold_main','hold_store','hold_publisher'):
                    which=cmd.removeprefix('hold_');barriers[which]=threading.Event()
                    if which=='main': owned['loop'].call_soon_threadsafe(hold_main)
                    elif which=='store': asyncio.run_coroutine_threadsafe(hold_store(),owned['loop'])
                elif cmd.startswith('release_'):
                    gate=barriers.get(cmd.removeprefix('release_'))
                    if gate is not None: gate.set()
                elif cmd=='queue_read': asyncio.run_coroutine_threadsafe(queue_read(),owned['loop'])
                elif cmd!='status': raise ValueError('bad_control')
                dump(root/'control-response.json',{'id':seen,'mono_s':time.monotonic(),'pending':owned.get('store').pending() if owned.get('store') else None})
            except FileNotFoundError: pass
            stopping.wait(.02)
    controller=threading.Thread(target=control,name='fixture-controller',daemon=True);controller.start()
    args=main.parse_args(['--host','127.0.0.1','--port','0','--db',str(root/'sessions.db'),
        '--notifications-db',str(root/'notifications.db'),'--assets-db',str(root/'assets.db'),
        '--blob-root',str(root/'blobs'),'--local-host','fixture-u4','--tmux-bin',str(root/'tmux-private'),
        '--spawn-command','/bin/false','--claude-bin','/bin/false','--codex-bin','/bin/false',
        '--spawn-cwd',str(root),'--projects-root',str(root/'projects')])
    for key in vars(args):
        if key.startswith('disable_'):setattr(args,key,True)
    main.configure_logging(30)
    try: return main.run_bounded(main.run(args))
    finally:
        for gate in barriers.values():gate.set()
        stopping.set();controller.join(1)
        event('daemon_cleanup',controller_alive=controller.is_alive(),store_thread_alive=bool(owned.get('store') and owned['store']._thread))

def checker(root, url):
    """Actual process polling using the independently contract-tested fixture core.

    Only its caller is the packet's actual isolated HTTP sink. This is neither
    the sender's private CLI nor an installed checker/launchd implementation.
    """
    from u4_checker_model import AtomicJournal, SyntheticChecker, read_owner_json
    path=root/'daemon-progress.json'
    model=SyntheticChecker(AtomicJournal(root/'checker-journal.json'),time.monotonic)
    observations=[]
    while not (root/'stop-checker').exists():
        now=time.monotonic();row={'mono_s':now,'checker_pid':os.getpid()}
        calls=[]
        def caller(body):
            request=urllib.request.Request(url,json.dumps(body).encode(),{'Content-Type':'application/json'})
            with urllib.request.urlopen(request,timeout=2) as response: receipt=json.load(response)
            calls.append({'payload':body,'receipt':receipt,'returned_mono_s':time.monotonic()})
            return receipt
        try:
            state=model.poll_file(path)
            if model.state['instance_id'] is None:
                row['state']='first_bind_unavailable'
            else:
                if model.state['last_snapshot'] is not None:row['progress']=model.state['last_snapshot']
                row['cause']=model.state['current_cause']
                AtomicJournal(path.with_name(path.name+'.episodes.json')).save(model.handoff())
                for condition in ('active','recovery'):model.deliver(condition,caller)
                row['episode_id']=(model.active or model.state['last_terminal'] or {}).get('episode_id')
                row['state']='active' if row['cause'] and model.active else 'recovered' if model.recovery else 'startup_grace' if now-model.state['boot_seen']<5 else 'healthy'
                try:
                    ack=read_owner_json(path.with_name(path.name+'.ack.json'))
                    if model.recovery and ack.get('terminal')==model.recovery['episode_id']:
                        model.acknowledge_terminal(ack['terminal'])
                except FileNotFoundError:pass
                if calls:row['submissions']=calls
        except Exception as exc:
            row['state']='observer_error';row['error']=type(exc).__name__+':'+str(exc)
        observations.append(row);dump(root/'checker.json',observations);time.sleep(1)
    return 0

def control(root, cmd):
    ident=time.monotonic_ns();dump(root/'control-request.json',{'id':ident,'command':cmd})
    until=time.monotonic()+2
    while time.monotonic()<until:
        try:
            value=json.loads((root/'control-response.json').read_text())
            if value['id']==ident:return value
        except FileNotFoundError:pass
        time.sleep(.02)
    raise RuntimeError('control_timeout_'+cmd)

def events(root):
    try:return json.loads((root/'events.json').read_text())
    except FileNotFoundError:return []

async def wait_event(root,name,timeout=15,after=0):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        matches=[e for e in events(root) if e['event']==name and e['mono_s']>=after]
        if matches:return matches[-1]
        await asyncio.sleep(.05)
    raise RuntimeError('missing_event_'+name)

async def request(ws, ident, timeout=1):
    start=time.monotonic();await ws.send(json.dumps({'type':'ping','request_id':ident}))
    while True:
        reply=json.loads(await asyncio.wait_for(ws.recv(),timeout))
        if reply.get('request_id')==ident:
            if reply.get('type')!='pong':raise RuntimeError('not_pong')
            return {'sent_mono_s':start,'received_mono_s':time.monotonic(),'latency_s':time.monotonic()-start}

async def run(source, output):
    from websockets.asyncio.client import connect
    result={'base':BASE,'source':str(source),'source_identity':{str(f.relative_to(source)):hashlib.sha256(f.read_bytes()).hexdigest() for f in [source/'services/chat-stream-v2/main.py',source/'services/chat-stream-v2/store.py',source/'services/chat-stream-v2/server.py',source/'services/chat-stream-v2/loop_watchdog.py',source/'services/chat-stream-v2/alerts.py',source/'services/chat-stream-v2/error_adapters.py'] if f.exists()},'synthetic_only':True,'classification':'HARNESS_ERROR','cells':[],
            'proof_boundary':'Actual main.run/Server/Store plus test-owned barrier injection. Portable observer is an independent synthetic checker, not sender-private checker or carrier proof.'}
    processes=[];handles=[];runtime=None;process_stopped=False;started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='u4held-',dir='/tmp') as td:
        root=Path(td).resolve();runtime=str(root);root.chmod(0o700)
        try:
            for name in ('home','projects','runtime','tmp'): (root/name).mkdir(mode=0o700)
            (root/'tmux-private').write_text('#!/bin/sh\nexec '+str(TMUX)+' -S '+str(root/'tmux.sock')+' "$@"\n');(root/'tmux-private').chmod(0o700)
            env=scrub();env.update({'HOME':str(root/'home'),'XDG_CONFIG_HOME':str(root/'home/.config'),
                'XDG_CACHE_HOME':str(root/'home/.cache'),'TMPDIR':str(root/'tmp'),'PYTHONDONTWRITEBYTECODE':'1',
                'PENTACLE_HOST_ID':'fixture-u4','PENTACLE_DAEMON_PROGRESS_PATH':str(root/'daemon-progress.json'),
                'PENTACLE_DAEMON_INSTANCE_ID':'fixture-u4-installation','PENTACLE_ERROR_ALERTS_MODE':'record-only',
                'PENTACLE_MACHINES_JSON':json.dumps([{'name':'fixture-u4','ssh_target':None,'codex_bin':'/bin/false','claude_bin':'/bin/false','cwd':str(root),'projects_root':str(root/'projects')}])})
            def launch(name,args,stdout_pipe=False):
                out=subprocess.PIPE if stdout_pipe else open(root/(name+'.log'),'w');err=open(root/(name+'.stderr'),'w');handles.extend([err]+([] if stdout_pipe else [out]))
                p=subprocess.Popen(args,env=env,cwd=source/'services/chat-stream-v2',stdout=out,stderr=err,text=True,start_new_session=False);processes.append((name,p));return p
            sink=launch('sink',[sys.executable,str(SINK_SCRIPT),'--receipt',str(root/'sink-receipt.json')],True)
            url=json.loads(await asyncio.wait_for(asyncio.to_thread(sink.stdout.readline),5))['url']
            watch=launch('checker',[sys.executable,__file__,'--role','checker','--root',str(root),'--sink',url])
            daemon=launch('daemon',[sys.executable,__file__,'--role','daemon','--root',str(root),'--source',str(source)])
            ready=await wait_event(root,'ready');result['daemon_ready']=ready
            result['processes']={name:p.pid for name,p in processes}
            async with connect('ws://127.0.0.1:'+str(ready['port']),ping_interval=None,close_timeout=2) as ws:
                idle=[]
                for i in range(20):idle.append(await request(ws,'idle-'+str(i)));await asyncio.sleep(.35)
                result['healthy_idle']={'pings':idle,'ping_latency_s':{'p50':sorted(p['latency_s'] for p in idle)[9],'p95':sorted(p['latency_s'] for p in idle)[18],'max':max(p['latency_s'] for p in idle)},'scope':'20 empty-private-daemon ping controls, not a fleet-size concurrency fixture','sink_receipts':json.loads((root/'sink-receipt.json').read_text()) if (root/'sink-receipt.json').exists() else []}
                if SCENARIO == 'held':
                    control(root,'hold_main');held=await wait_event(root,'main_held')
                    ping_start=time.monotonic();await ws.send(json.dumps({'type':'ping','request_id':'held-main'}));main_responded=False
                    try:
                        while True:
                            reply=json.loads(await asyncio.wait_for(ws.recv(),.5))
                            if reply.get('request_id')=='held-main':main_responded=True;break
                    except TimeoutError:pass
                    await asyncio.sleep(max(0,held['mono_s']+6.5-time.monotonic()))
                    main_end=time.monotonic();pre_main_release=json.loads((root/'sink-receipt.json').read_text()) if (root/'sink-receipt.json').exists() else []
                    main_progress=(root/'daemon-progress.json').exists();control(root,'release_main');released=await wait_event(root,'main_released')
                    await request(ws,'after-main',2)
                    result['cells'].append({'name':'held_MAIN','held':held,'release':released,'duration_s':released['mono_s']-held['mono_s'],
                        'main_ping_responded_while_held':main_responded,'progress_snapshot_present_while_held':main_progress,
                        'receipt_before_release':any(held['mono_s']<r['accepted_mono_s']<main_end and r['payload']['condition']=='active' for r in pre_main_release),'acceptance_passed':any(held['mono_s']<r['accepted_mono_s']<main_end and r['payload']['condition']=='active' for r in pre_main_release),
                        'valid_fault':released['explicit_release'] and released['mono_s']-held['mono_s']>5 and not main_responded})
                    await asyncio.sleep(4)
                    control(root,'hold_store');held=await wait_event(root,'store_held');control(root,'queue_read');queued=await wait_event(root,'queued_read_enqueued')
                    pings=[];pending=[]
                    while time.monotonic()<held['mono_s']+6.5:
                        pings.append(await request(ws,'held-store-'+str(len(pings)),.2));pending.append(control(root,'status'));await asyncio.sleep(.25)
                    pre_store_release=json.loads((root/'sink-receipt.json').read_text()) if (root/'sink-receipt.json').exists() else []
                    early_start=any(e['event']=='queued_read_started' for e in events(root));store_progress=(root/'daemon-progress.json').exists()
                    control(root,'release_store');released=await wait_event(root,'store_released');finished=await wait_event(root,'queued_read_completed');queue_start=await wait_event(root,'queued_read_started')
                    result['cells'].append({'name':'held_Store_MAIN_responsive_and_queued_read','held':held,'release':released,'duration_s':released['mono_s']-held['mono_s'],
                        'pings_while_held':pings,'pending_samples':pending,'queued_read_enqueued':queued,'queued_read_started':queue_start,'queued_read_completed':finished,
                        'queue_started_before_release':early_start,'progress_snapshot_present_while_held':store_progress,
                        'receipt_before_release':any(held['mono_s']<r['accepted_mono_s']<released['mono_s'] for r in pre_store_release),
                        'acceptance_passed':any(held['mono_s']<r['accepted_mono_s']<released['mono_s'] for r in pre_store_release),
                        'valid_fault':released['explicit_release'] and released['mono_s']-held['mono_s']>5 and not early_start and min(s['pending'] for s in pending)>=1 and len(pings)>10})
                    await asyncio.sleep(3)
                    if store_progress:
                        until=time.monotonic()+8
                        while time.monotonic()<until:
                            try:
                                receipts=json.loads((root/'sink-receipt.json').read_text())
                                terminal=next(r['payload']['episode_id'] for r in reversed(receipts) if r['payload']['condition']=='recovered')
                                ack=json.loads((root/'daemon-progress.json.ack.json').read_text())
                                if ack['terminal']==terminal and len(receipts)==4:break
                            except (FileNotFoundError,StopIteration):pass
                            await asyncio.sleep(.1)
                        else:raise RuntimeError('terminal_handoff_ack_timeout')
                else:
                    if SCENARIO=='publisher':
                        control(root,'hold_publisher');held=await wait_event(root,'publisher_held')
                    else:
                        os.kill(daemon.pid,signal.SIGSTOP);process_stopped=True
                        stopped_pid,stopped_status=os.waitpid(daemon.pid,os.WUNTRACED)
                        if stopped_pid!=daemon.pid or not os.WIFSTOPPED(stopped_status):raise RuntimeError('process_not_stopped')
                        held={'event':'process_held','mono_s':time.monotonic(),'wall_s':time.time(),'pid':daemon.pid,'stopsig':os.WSTOPSIG(stopped_status)}
                    pings=[];reads=[];main_responded=False
                    if SCENARIO=='publisher':
                        while time.monotonic()<held['mono_s']+6.5:
                            pings.append(await request(ws,'publisher-'+str(len(pings)),.2))
                            enqueue=time.monotonic();control(root,'queue_read')
                            completed=await wait_event(root,'queued_read_completed',timeout=.2,after=enqueue)
                            reads.append({'enqueued_mono_s':enqueue,'completed':completed,'elapsed_s':completed['mono_s']-enqueue})
                            await asyncio.sleep(.2)
                    else:
                        await ws.send(json.dumps({'type':'ping','request_id':'process-held'}))
                        try:
                            while True:
                                reply=json.loads(await asyncio.wait_for(ws.recv(),.5))
                                if reply.get('request_id')=='process-held':main_responded=True;break
                        except TimeoutError:pass
                        await asyncio.sleep(max(0,held['mono_s']+6.5-time.monotonic()))
                    receipts=json.loads((root/'sink-receipt.json').read_text()) if (root/'sink-receipt.json').exists() else []
                    release_at=time.monotonic()
                    accepted=[r for r in receipts if r['payload']['condition']=='active' and held['mono_s']<r['accepted_mono_s']<release_at]
                    if SCENARIO=='publisher':
                        control(root,'release_publisher');released=await wait_event(root,'publisher_released')
                    else:
                        released={'event':'process_released','mono_s':release_at,'wall_s':time.time(),'explicit_release':True}
                        os.kill(daemon.pid,signal.SIGCONT);process_stopped=False
                    await request(ws,'after-freeze',2)
                    result['cells'].append({'name':'held_'+SCENARIO,'held':held,'release':released,'duration_s':released['mono_s']-held['mono_s'],
                        'pings_while_held':pings,'store_reads_while_held':reads,'main_ping_responded_while_held':main_responded,
                        'receipt_before_release':bool(accepted),'accepted_before_release':accepted,'acceptance_passed':bool(accepted),
                        'valid_fault':released['explicit_release'] and released['mono_s']-held['mono_s']>5 and ((len(pings)>10 and len(reads)>10 and max(r['elapsed_s'] for r in reads)<.2) if SCENARIO=='publisher' else not main_responded)})
                    until=time.monotonic()+8
                    while time.monotonic()<until:
                        try:
                            receipts=json.loads((root/'sink-receipt.json').read_text())
                            terminal=next(r['payload']['episode_id'] for r in reversed(receipts) if r['payload']['condition']=='recovered')
                            ack=json.loads((root/'daemon-progress.json.ack.json').read_text())
                            if ack['terminal']==terminal and len(receipts)==2:break
                        except (FileNotFoundError,StopIteration):pass
                        await asyncio.sleep(.1)
                    else:raise RuntimeError('terminal_handoff_ack_timeout')
            result['classification']='PRODUCT_RED' if all(c['valid_fault'] for c in result['cells']) and not all(c['acceptance_passed'] for c in result['cells']) else 'PASS' if all(c['acceptance_passed'] for c in result['cells']) else 'HARNESS_ERROR'
            result['failed_product_predicates']=[c['name']+': no accepted independent submission before fault release' for c in result['cells'] if not c['acceptance_passed']]
        except Exception as exc:
            result['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
        finally:
            if process_stopped:
                os.kill(daemon.pid,signal.SIGCONT);process_stopped=False
            if (root/'control-response.json').exists():
                for name in ('main','store','publisher'):
                    try:control(root,'release_'+name)
                    except Exception:pass
            (root/'stop-checker').touch()
            cleanup=[]
            for name,p in reversed(processes):
                if name=='daemon' and p.poll() is None:p.send_signal(signal.SIGTERM)
                if name=='sink' and p.poll() is None:p.terminate()
                try:p.wait(timeout=15 if name=='daemon' else 3)
                except subprocess.TimeoutExpired:p.kill();p.wait();cleanup.append({'name':name,'forced_kill':True})
                cleanup.append({'name':name,'pid':p.pid,'returncode':p.returncode,'alive':p.poll() is None})
            for h in handles:h.close()
            result['cleanup']=cleanup;result['events']=events(root)
            for name in ('checker.json','checker-journal.json','sink-receipt.json','daemon-progress.json.episodes.json','daemon-progress.json.ack.json','daemon.log','daemon.stderr','checker.stderr','sink.stderr'):
                if (root/name).exists():
                    content=(root/name).read_text();(output.parent/(output.stem+'-'+name)).write_text(content)
                    if name.endswith('.json'):result[name.removesuffix('.json')]=json.loads(content)
            subprocess.run([str(TMUX),'-S',str(root/'tmux.sock'),'kill-server'],env=scrub(),capture_output=True,timeout=3)
    # Assert product progress rather than substituting harness heartbeat counters.
    for cell in result['cells']:
        snapshots=[r['progress'] for r in result.get('checker',[]) if 'progress' in r and cell['held']['mono_s']<=r['progress']['sample_mono_s']<=cell['release']['mono_s']]
        cell['observed_loop_seq']=[r['loop_seq'] for r in snapshots]
        cell['loop_seq_advanced_while_held']=len(set(cell['observed_loop_seq']))>1
        cell['observed_store_progress']=[r['store'] for r in snapshots]
    receipts=result.get('sink-receipt',[])
    count=2 if SCENARIO=='held' else 1
    result['scenario']=SCENARIO
    result['acceptance']={
        'faults_proven':len(result['cells'])==count and all(c['valid_fault'] for c in result['cells']),
        'quiet_healthy_idle':not result.get('healthy_idle',{}).get('sink_receipts',[]),
        'receipts_before_all_releases':len(result['cells'])==count and all(c['receipt_before_release'] for c in result['cells']),
        'one_active_and_recovery_each':len(receipts)==2*count and len({r['payload']['episode_id'] for r in receipts})==count and all(sum(1 for r in receipts if r['payload']['episode_id']==eid and r['payload']['condition']==condition)==1 for eid in {r['payload']['episode_id'] for r in receipts} for condition in ('active','recovered')),
        'terminal_handoff_acknowledged':bool(receipts) and result.get('daemon-progress.json.ack',{}).get('terminal')==receipts[-1]['payload']['episode_id'],
        'checker_no_errors':not any(row['state']=='observer_error' for row in result.get('checker',[])),
        'owned_processes_clean':all(not row.get('alive') and not row.get('forced_kill') and (row.get('name')=='sink' or row.get('returncode')==0) for row in result.get('cleanup',[])),
    }
    if SCENARIO=='held':
        result['acceptance'].update({
            'main_progress_stale_while_held':bool(result['cells']) and len(result['cells'][0]['observed_loop_seq'])>=5 and len(set(result['cells'][0]['observed_loop_seq']))==1,
            'store_only_loop_progress_advances':len(result['cells'])==2 and result['cells'][1]['loop_seq_advanced_while_held'],
        })
    else:
        result['acceptance']['stale_sample_detected_by_independent_process']=any(row.get('cause')=='unavailable' and row.get('state')=='active' and any(c['held']['mono_s']<row['mono_s']<c['release']['mono_s'] and any(r['payload']['episode_id']==row.get('episode_id') for r in c['accepted_before_release']) for c in result['cells']) for row in result.get('checker',[]))
    if result['classification']!='HARNESS_ERROR':result['classification']='PASS' if all(result['acceptance'].values()) else 'PRODUCT_RED'
    result['runtime_removed']=not Path(runtime).exists();result['elapsed_s']=time.monotonic()-started
    result['raw_exit_code']=1 if result['classification']=='PRODUCT_RED' else 0 if result['classification']=='PASS' else 2
    result['harness_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    dump(output,result);print(json.dumps(result,indent=2));return result['raw_exit_code']

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--role',default='run');p.add_argument('--scenario',choices=['held','publisher','process'],default='held');p.add_argument('--root',type=Path);p.add_argument('--source',type=Path);p.add_argument('--tmux-bin',type=Path,default=TMUX);p.add_argument('--sink-script',type=Path,default=SINK_SCRIPT);p.add_argument('--sink');p.add_argument('--output',type=Path,default=HERE/'u4-held-baseline.json');a=p.parse_args()
    TMUX=a.tmux_bin;SINK_SCRIPT=a.sink_script;SCENARIO=a.scenario
    if a.role=='daemon':code=launch_daemon(a.source,a.root)
    elif a.role=='checker':code=checker(a.root,a.sink)
    else:code=asyncio.run(run(a.source.resolve(),a.output.resolve()))
    sys.exit(code)
