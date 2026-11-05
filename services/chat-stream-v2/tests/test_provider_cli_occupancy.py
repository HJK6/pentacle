"""A real host repin can drain one managed home without draining other homes."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace

import pytest

from test_account_repin import host_journey
from sessions import VerbError


async def occupancy_journey(tmp_path, monkeypatch, provider="codex", platform="macos", *, launch_type="native"):
    """Real host registry + daemon verifier; only process/tmux observations are synthetic."""
    import provider_cli_occupancy as occupancy
    j = await host_journey(tmp_path, monkeypatch, provider, platform)
    store, runtime, host, selected, _, _, _, _, cell, _ = j
    host.config["cells"][provider+":other"] = copy.deepcopy(cell)
    native_path=cell['executable']
    if launch_type!='native':
        from pathlib import Path
        target=tmp_path/('codex.js' if launch_type=='js' else '2.1.312')
        target.write_text('pinned script fixture' if launch_type=='js' else 'new fixture')
        link=tmp_path/'provider-link';link.symlink_to(target)
        host.config['cells'][provider+':other']['executable']=str(link)
        import hashlib
        host.config['cells'][provider+':other']['executable_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
        if launch_type=='js':
            native=tmp_path/'codex';native.write_text('new fixture');native_path=str(native)
    permit = {"action":"enroll", "expected_generation":"unenrolled", "generation":"other-initial",
        "clearance_id":"c"*64, "host_binding":None, "central_intent":"d"*64, "reserved_at_ns":1}
    host.handle("mutation-reserve", provider=provider, alias="other", expected_generation="unenrolled",
        generation="other-initial", operation_id="fixture-other-owner", reconcile=permit)
    host.handle("enroll", provider=provider, alias="other", expected_generation="unenrolled",
        generation="other-initial", operation_id="fixture-other-owner", reconcile={"_clearance":"c"*64})
    other = host.registry.active(provider, "other", selected.host)
    await runtime.run(lambda a:a.register(other))
    started = "Thu Oct 29 10:00:00 2026"
    state = SimpleNamespace(store=store, runtime=runtime, host=host, selected=selected, other=other,
        commands={42:selected.executable+" normal prompt", 52:other.executable+" normal prompt"},
        paths={42:selected.executable, 52:other.executable}, probes=[], started=started,
        native_names={42:selected.executable,52:other.executable}, extra_records='', parents={42:40,52:50},
        roots={40:'selected-seat',50:'other-seat'},pin=j[-1])
    if launch_type=='symlink':
        state.paths[52]=str(target);state.native_names[52]=str(target)
    if launch_type=='js':
        node=tmp_path/'node';node.write_text('node fixture')
        state.paths.update({50:'/fixture/shell',51:str(node),52:native_path})
        state.native_names[52]=native_path
        state.parents[52]=51
        state.extra_records=f'51 50 {os.getuid()} 50 50 {started} node {target} opaque quotes\n'
        state.argv={51:['node',str(target),"operator's unbalanced '\\ prompt"]}
        monkeypatch.setattr(occupancy,'_kernel_argv',lambda pid:state.argv[pid],raising=False)
        monkeypatch.setattr(occupancy,'_executed_file',lambda pid,path,cache:occupancy._file(path,cache),raising=False)
        monkeypatch.setattr(occupancy,'_helper_os',lambda:'linux',raising=False)
    for root, name, bound in ((40,"selected-seat",selected), (50,"other-seat",other)):
        await store.open_session(selected.host, name, provider=provider, session_generation=name+"-generation",
            pane_pid=str(root), account_binding=bound.public(), observer_binding={"pane_started_at":started,
                "generation":name+"-generation", "executable":bound.executable})
    actual_run = host.run
    def running(argv, **kwargs):
        if argv[0] == "ps":
            if "pid=,comm=" in argv:
                output = ''.join(f'{pid} {path}\n' for pid,path in state.native_names.items())
            else:
                output = "".join(f"{root} 1 {os.getuid()} {root} {root} {started} /bin/zsh\n"
                    f"{root+2} {state.parents[root+2]} {os.getuid()} {root} {root} {started} {state.commands[root+2]}\n"
                    for root in state.roots)
                output+=state.extra_records
            return SimpleNamespace(returncode=0, stdout=output)
        if argv[0] == "tmux":
            name=argv[argv.index("-t")+1][1:]; root=next(r for r,n in state.roots.items() if n==name)
            return SimpleNamespace(returncode=0, stdout=f"{root}|%{root}|/dev/ttys{root}|/fixture/tmux.sock|{name}\n")
        if argv[0] in {"cat","sysctl"}:
            return SimpleNamespace(returncode=0, stdout="fixture-boot\n")
        return actual_run(argv, **kwargs)
    def probe(pid):
        assert pid in state.paths, "synthetic PID must never reach host procfs/libproc"
        state.probes.append(pid)
        return state.paths[pid]
    host.run = running
    monkeypatch.setattr(occupancy, "_native_executable", probe, raising=False)
    return state


@pytest.mark.parametrize("prompt", ["normal multiword prompt", "unbalanced ' single", 'unbalanced " double',
    "operator's apostrophe", "trailing backslash \\"])
@pytest.mark.parametrize("provider,platform", [(p,s) for p in ("codex","claude") for s in ("macos","wsl")])
def test_quote_display_is_not_executable_identity(tmp_path, monkeypatch, prompt,provider,platform):
    import provider_cli_occupancy as occupancy
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,provider,platform)
        try:
            s.commands[52]=s.other.executable+" "+prompt
            launchers=await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider)
            value=occupancy.status(s.host,s.selected,launchers)
            assert value["missing"]==[], value
            assert len(value["managed"])==2 and value["unmanaged"]==[]
            assert (await s.runtime.executor.verify_launchers(s.selected))["missing"]==[]
            from prockill import command_fingerprint
            assert next(r for r in value["managed"] if r["stream_id"].endswith(":other-seat"))["process"]["command_fingerprint"]==command_fingerprint(s.commands[52])
            assert set(s.probes)=={42,52}
        finally:s.store.stop()
    asyncio.run(run())


def test_displayed_executable_cannot_attest_wrong_native_path(tmp_path,monkeypatch):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            s.paths[52]="/fixture/wrong-native"
            launchers=await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider)
            value=occupancy.status(s.host,s.selected,launchers)
            assert value["missing"]==["host-a:other-seat"],value
            assert value["unmanaged"]==[{"pid":52}]
            with pytest.raises(AccountError) as refused:await s.runtime.executor.verify_launchers(s.selected)
            assert refused.value.context["reason"]=="launcher_unverified"
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("provider,platform",[(p,s) for p in ("codex","claude") for s in ("macos","wsl")])
@pytest.mark.parametrize("proof", ["managed-other-home", "unmanaged", "same-home", "stale-birth", "unknown-descendant",
    "missing-tree","pane-mismatch","different-uid","observer-generation","missing-boot"])
def test_native_repin_distinguishes_managed_home_from_unattributed_process(proof, tmp_path, monkeypatch,provider,platform):
    async def run():
        store,runtime,host,old,request,calls,native,logins,cell,pin = await host_journey(tmp_path,monkeypatch,provider,platform)
        try:
            # Enroll the other fixture through the same clearance boundary.
            host.config["cells"][provider+":other"] = copy.deepcopy(cell)
            permit = {"action":"enroll", "expected_generation":"unenrolled", "generation":"other-initial",
                "clearance_id":"c"*64, "host_binding":None, "central_intent":"d"*64, "reserved_at_ns":1}
            host.handle("mutation-reserve",provider=provider,alias="other",expected_generation="unenrolled",
                generation="other-initial",operation_id="fixture-other-owner",reconcile=permit)
            host.handle("enroll",provider=provider,alias="other",expected_generation="unenrolled",
                generation="other-initial",operation_id="fixture-other-owner",reconcile={"_clearance":"c"*64})
            other = host.registry.active(provider,"other",old.host)
            await runtime.run(lambda a:a.register(other))
            bound = old if proof == "same-home" else other
            import provider_cli_occupancy as occupancy
            def probe(pid):
                assert pid==42, "synthetic PID must never reach host procfs/libproc"
                return bound.executable
            monkeypatch.setattr(occupancy,"_native_executable",probe)
            started = "Thu Oct 29 10:00:00 2026"
            if proof != "unmanaged":
                await store.open_session(old.host,"managed-other",provider=provider,session_generation="other-seat-gen",
                    pane_pid="42",account_binding=bound.public(),observer_binding={
                        "pane_pid":"42", "pane_started_at":"Wed Oct 28 10:00:00 2026" if proof=="stale-birth" else started,
                        "generation":"stale" if proof=="observer-generation" else "other-seat-gen", "executable":bound.executable})
                if proof=="observer-generation":
                    # Normal open deliberately stamps its current generation.
                    # Model a retained stale observation after reopen instead.
                    def stale_observer(conn):
                        value=json.loads(conn.execute("SELECT observer_binding FROM sessions WHERE host=? AND session_name=?",
                            (old.host,"managed-other")).fetchone()[0])
                        value["generation"]="stale"
                        conn.execute("UPDATE sessions SET observer_binding=? WHERE host=? AND session_name=?",
                            (json.dumps(value),old.host,"managed-other"));conn.commit()
                    await store.submit(stale_observer)
            actual_run = host.run
            def running(argv,**kwargs):
                if argv[0] == "ps":
                    if "pid=,comm=,args=" in argv:
                        return SimpleNamespace(returncode=0,stdout="42 "+provider+" "+bound.executable+"\n")
                    if "pid=,comm=" in argv:
                        output = "42 "+bound.executable+"\n"
                        if proof == "unknown-descendant":
                            output += "43 "+provider+"\n"
                        return SimpleNamespace(returncode=0,stdout=output)
                    table = f"42 1 {os.getuid()+1 if proof=='different-uid' else os.getuid()} 42 42 {started} {bound.executable}\n"
                    if proof=="missing-tree":table=""
                    if proof == "unknown-descendant":
                        table += f"43 42 {os.getuid()} 42 42 {started} {provider} app-server\n"
                    return SimpleNamespace(returncode=0,stdout=table)
                if argv[0] == "tmux":
                    if proof=="pane-mismatch":return SimpleNamespace(returncode=0,stdout="43|%43|/dev/ttys043|/fixture/tmux.sock|managed-other\n")
                    return SimpleNamespace(returncode=0,stdout="42|%42|/dev/ttys042|/fixture/tmux.sock|managed-other\n")
                if argv[0] in {"cat","sysctl"}:
                    return SimpleNamespace(returncode=0,stdout="" if proof=="missing-boot" else "fixture-boot\n")
                return actual_run(argv,**kwargs)
            host.run = running
            native.clear()
            if proof == "managed-other-home":
                result = await runtime.operation(request)
                assert result["result"]["outcome"] == "repinned"
                assert host.registry.active(provider,old.alias,old.host).home == old.home
                assert host.registry.active(provider,"other",old.host).key == other.key
                assert len([argv for argv in native if "Reply exactly OK" in argv]) == 1
            else:
                with pytest.raises(VerbError):
                    await runtime.operation(request)
                assert host.registry.active(provider,old.alias,old.host).key == old.key
                assert not [argv for argv in native if "Reply exactly OK" in argv]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['success','spaces','load','symbol','call','zero','negative','full','oversized',
    'missing-nul','early-nul','relative','empty'])
def test_darwin_native_probe_boundary(monkeypatch,case):
    import ctypes
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    monkeypatch.setattr(occupancy.sys,'platform','darwin')
    path=b'/fixture/native provider' if case=='spaces' else b'/fixture/native'
    seen=[]
    def call(pid,buffer,size):
        assert pid==42 and size==4096
        assert probe.argtypes==(ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32)
        assert probe.restype==ctypes.c_int
        seen.append(pid)
        if case=='call':raise OSError('denied')
        if case in {'zero','negative','full','oversized'}:return {'zero':0,'negative':-1,'full':4096,'oversized':4097}[case]
        raw={'relative':b'relative','empty':b'','early-nul':b'/fixture\0hidden'}.get(case,path)
        ctypes.memmove(buffer,raw,len(raw))
        if case=='missing-nul':buffer[len(raw)]=b'x'
        return len(raw)
    class Probe:
        def __call__(self,*args):return call(*args)
    probe=Probe()
    def load(name,*,use_errno):
        assert name=='/usr/lib/libproc.dylib' and use_errno is True
        if case=='load':raise OSError('absent')
        return SimpleNamespace() if case=='symbol' else SimpleNamespace(proc_pidpath=probe)
    monkeypatch.setattr(occupancy.ctypes,'CDLL',load)
    if case in {'success','spaces'}:
        assert occupancy._native_executable(42)==os.fsdecode(path)
        assert seen==[42]
    else:
        with pytest.raises(AccountError,match='account_repin_occupancy_unavailable'):
            occupancy._native_executable(42)


@pytest.mark.parametrize('case', ['success','spaces','enoent','eacces','deleted','relative','empty','nul'])
def test_linux_native_probe_boundary(monkeypatch,case):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    monkeypatch.setattr(occupancy.sys,'platform','linux')
    def readlink(path):
        assert path=='/proc/42/exe'
        if case in {'enoent','eacces'}:raise OSError(2 if case=='enoent' else 13,'fixture')
        return {'spaces':'/fixture/native provider','deleted':'/fixture/native (deleted)',
            'relative':'relative','empty':'','nul':'/fixture/\0native'}.get(case,'/fixture/native')
    monkeypatch.setattr(occupancy.os,'readlink',readlink)
    monkeypatch.setattr(occupancy.ctypes,'CDLL',lambda *a,**k:pytest.fail('Linux must not load libproc'))
    if case in {'success','spaces'}:
        assert occupancy._native_executable(42)==('/fixture/native provider' if case=='spaces' else '/fixture/native')
    else:
        with pytest.raises(AccountError,match='account_repin_occupancy_unavailable'):
            occupancy._native_executable(42)


@pytest.mark.parametrize('pid', [0,-1,True,'42','42/../self',None])
def test_invalid_probe_pid_never_reaches_os(monkeypatch,pid):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    monkeypatch.setattr(occupancy.ctypes,'CDLL',lambda *a,**k:pytest.fail('invalid PID reached libproc'))
    monkeypatch.setattr(occupancy.os,'readlink',lambda *a:pytest.fail('invalid PID reached procfs'))
    with pytest.raises(AccountError,match='account_repin_occupancy_unavailable'):occupancy._native_executable(pid)


def test_unsupported_probe_os_fails_closed(monkeypatch):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    monkeypatch.setattr(occupancy.sys,'platform','unsupported')
    with pytest.raises(AccountError,match='account_repin_occupancy_unavailable'):occupancy._native_executable(42)


@pytest.mark.parametrize('boundary', ['root','candidate'])
@pytest.mark.parametrize('field', ['exit','pid','uid','start_id','ppid','pgid','sid','command'])
def test_native_path_fence_rejects_changed_process(tmp_path,monkeypatch,boundary,field):
    import provider_cli_occupancy as occupancy
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            original=occupancy._records; reads=[]
            def records(host):
                value=original(host); reads.append(True)
                if len(reads)>1:
                    pid=50 if boundary=='root' else 52
                    if field=='exit':value.pop(pid)
                    else:value[pid][field]={'start_id':'2026-10-28T10:00:00','command':'changed'}.get(field,999)
                return value
            monkeypatch.setattr(occupancy,'_records',records)
            value=occupancy.status(s.host,s.selected,await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider))
            # A census containing a native PID without a record fails closed
            # for the entire observation, including otherwise valid peers.
            global_failure=field=='exit' and boundary=='candidate'
            assert set(value['missing'])==({'host-a:other-seat','host-a:selected-seat'} if global_failure else {'host-a:other-seat'})
            assert [r['process']['pid'] for r in value['managed']]==([] if global_failure else [42])
            assert value['unmanaged']==([{'pid':42},{'pid':52}] if global_failure else [{'pid':52}])
            assert len(reads)==2  # Shared snapshot, not one full census per launcher.
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['boot','boot-missing','tree','native-missing','native-added','native-error',
    'records-error','boot-error','pane-pid','pane-id','tty','socket','session','pane-error'])
def test_native_path_fence_rejects_changed_census_or_pane(tmp_path,monkeypatch,change):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            original_records=occupancy._records; original_native=occupancy._native_pids; original_boot=occupancy._boot
            counts={'records':0,'native':0,'boot':0}; panes={}; original_run=s.host.run
            def records(host):
                counts['records']+=1; result=original_records(host)
                if counts['records']>1:
                    if change=='records-error':raise AccountError('account_repin_occupancy_unavailable')
                    if change=='tree':result[52]['ppid']=1
                    if change=='native-added':result[53]=dict(result[52],pid=53)
                return result
            def native(host,binding):
                counts['native']+=1; result=original_native(host,binding)
                if counts['native']>1:
                    if change=='native-error':raise AccountError('account_repin_occupancy_unavailable')
                    if change=='native-missing':result.remove(52)
                    if change=='native-added':result.add(53)
                return result
            def boot(host):
                counts['boot']+=1
                if counts['boot']>1:
                    if change=='boot-error':raise AccountError('account_repin_occupancy_unavailable')
                    if change in {'boot','boot-missing'}:return 'changed' if change=='boot' else ''
                return original_boot(host)
            def running(argv,**kwargs):
                result=original_run(argv,**kwargs)
                if argv[0]=='tmux':
                    name=argv[argv.index('-t')+1];panes[name]=panes.get(name,0)+1
                    if name=='=other-seat' and panes[name]>1:
                        fields=result.stdout.strip().split('|')
                        index={'pane-pid':0,'pane-id':1,'tty':2,'socket':3,'session':4}.get(change)
                        if index is not None:fields[index]='changed';result.stdout='|'.join(fields)+'\n'
                        if change=='pane-error':result.returncode=1
                return result
            monkeypatch.setattr(occupancy,'_records',records);monkeypatch.setattr(occupancy,'_native_pids',native)
            monkeypatch.setattr(occupancy,'_boot',boot);s.host.run=running
            value=occupancy.status(s.host,s.selected,await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider))
            assert 'host-a:other-seat' in value['missing']
            assert 52 not in [r['process']['pid'] for r in value['managed']]
            assert {'pid':52} in value['unmanaged']
            if change=='native-added':assert {'pid':53} in value['unmanaged']
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['binding','home','generation','observer-generation','observer-executable',
    'birth','empty-generation','duplicate','probe-error','noncanonical-path'])
def test_native_proof_preserves_launcher_predicates(tmp_path,monkeypatch,case):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch)
        try:
            launchers=await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider)
            row=next(r for r in launchers if r['stream_id']=='host-a:other-seat')
            if case=='binding':row['binding']['executable']='/fixture/stale'
            elif case=='home':row['binding']['home']='/fixture/stale-home'
            elif case=='generation':row['binding']['generation']='stale'
            elif case=='observer-generation':row['observer_generation']='stale'
            elif case=='observer-executable':row['observer_executable']='/fixture/stale'
            elif case=='birth':row['pane_started_at']='Wed Oct 28 10:00:00 2026'
            elif case=='empty-generation':row['session_generation']=''
            elif case=='duplicate':launchers.append(copy.deepcopy(row))
            elif case=='noncanonical-path':s.paths[52]=s.other.executable+'/../'+s.other.executable.rsplit('/',1)[-1]
            else:
                original=occupancy._native_executable
                def denied(pid):
                    if pid==52:raise AccountError('account_repin_occupancy_unavailable')
                    return original(pid)
                monkeypatch.setattr(occupancy,'_native_executable',denied)
            value=occupancy.status(s.host,s.selected,launchers)
            assert 'host-a:other-seat' in value['missing']
            if case!='duplicate':
                assert [r['process']['pid'] for r in value['managed']]==[42]
                assert value['unmanaged']==[{'pid':52}]
            async def descriptors(*args):return launchers
            monkeypatch.setattr(s.runtime.executor,'launcher_descriptors',descriptors)
            with pytest.raises(AccountError) as refused:await s.runtime.executor.verify_launchers(s.selected)
            assert refused.value.context['reason']=='launcher_unverified'
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('provider,launch_type', [('claude','symlink'),('codex','symlink'),('codex','js')])
def test_canonical_native_and_linux_wrapper_are_managed(tmp_path,monkeypatch,provider,launch_type):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,provider,'wsl' if launch_type=='js' else 'macos',launch_type=launch_type)
        try:
            launchers=await occupancy.descriptors(s.runtime,s.selected.host,provider)
            value=s.host.handle('launcher-status',provider=provider,alias=s.selected.alias,
                expected_generation=s.selected.generation,operation_id='fixture-canonical',pin=s.pin,
                reconcile={'managed_launchers':launchers})
            try:await s.runtime.executor.verify_launchers(s.selected)
            except AccountError as exc:print('daemon refusal',exc.context)
            assert value['missing']==[],value
            assert len(value['managed'])==2 and value['unmanaged']==[]
            assert (await s.runtime.executor.verify_launchers(s.selected))['missing']==[]
            assert next(r for r in value['managed'] if r['stream_id']=='host-a:other-seat')['process']['pid']==52
        finally:s.store.stop()
    asyncio.run(run())


def fixture_status(s,launchers,pin):
    return s.host.handle('launcher-status',provider=s.selected.provider,alias=s.selected.alias,
        expected_generation=s.selected.generation,operation_id='fixture-proof',pin=pin,
        reconcile={'managed_launchers':launchers})


@pytest.mark.parametrize('case', ['wrong-native-hash','wrong-script','missing-script','script-in-prompt',
    'node-options','relative-script','sibling','outside-pane','missing-wrapper','two-native','provider-looking-wrapper',
    'missing-pin','stale-version','wrong-platform','wrong-provider','interpreter-only','macos-wrapper'])
def test_wrapper_requires_script_ancestry_and_independent_native_pin(tmp_path,monkeypatch,case):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,platform='wsl',launch_type='js')
        try:
            pin=copy.deepcopy(s.pin)
            if case=='wrong-native-hash':
                from pathlib import Path
                Path(s.paths[52]).write_text('wrong native bytes')
            elif case=='wrong-script':s.argv[51][1]=s.paths[51]
            elif case=='missing-script':s.argv[51]=['node']
            elif case=='script-in-prompt':s.argv[51]=['node',s.paths[51],'--prompt',s.other.executable]
            elif case=='node-options':s.argv[51].insert(1,'--inspect')
            elif case=='relative-script':s.argv[51][1]='codex.js'
            elif case=='sibling':s.parents[52]=50
            elif case=='outside-pane':s.parents[52]=40
            elif case=='missing-wrapper':s.extra_records=''
            elif case=='two-native':
                s.native_names[53]=s.paths[52];s.paths[53]=s.paths[52]
                s.extra_records+=f'53 51 {os.getuid()} 50 50 {s.started} codex\n'
            elif case=='provider-looking-wrapper':
                s.native_names[51]=s.other.executable  # Proven support may be excluded, never guessed away.
                s.argv[51]=['node',s.paths[51]]  # Unproved support must not manufacture uniqueness.
            elif case=='missing-pin':pin=None
            elif case=='stale-version':pin['providers']['codex']['version']='9.9.9'
            elif case=='wrong-platform':
                for entry in pin['providers'].values():entry['artifacts'].pop('linux-x64')
            elif case=='wrong-provider':pin['providers']['codex']['artifacts']['linux-x64']['native_sha256']='a'*64
            elif case=='interpreter-only':
                s.native_names.pop(52);s.native_names[51]=s.other.executable
            elif case=='macos-wrapper':monkeypatch.setattr(occupancy,'_helper_os',lambda:'darwin')
            launchers=await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider)
            value=fixture_status(s,launchers,pin)
            assert 'host-a:other-seat' in value['missing'],value
            assert 52 not in [r['process']['pid'] for r in value['managed']]
            actual=s.runtime.executor.execute
            async def execute(account,action,operation,*args,**kwargs):
                if action=='launcher-status':kwargs['pin']=pin
                return await actual(account,action,operation,*args,**kwargs)
            s.runtime.executor.execute=execute
            with pytest.raises(AccountError) as refused:await s.runtime.executor.verify_launchers(s.selected)
            assert refused.value.context['reason']=='launcher_unverified'
            with pytest.raises(AccountError,match='account_repin_home_busy'):
                occupancy.assert_drained(s.host,s.other,launchers)
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case', ['script-hash','native-hash','link-retarget','wrapper-image','argv','birth','uid','ancestry','pin'])
def test_wrapper_file_and_process_fence(tmp_path,monkeypatch,case):
    import provider_cli_occupancy as occupancy
    from pathlib import Path
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,platform='wsl',launch_type='js')
        try:
            original=occupancy._records;count=0
            def records(host):
                nonlocal count
                count+=1
                if count==2:
                    if case=='script-hash':Path(s.other.executable).resolve().write_text('changed script')
                    elif case=='native-hash':Path(s.paths[52]).write_text('changed native')
                    elif case=='link-retarget':
                        Path(s.other.executable).unlink();Path(s.other.executable).symlink_to(s.paths[52])
                    elif case=='wrapper-image':Path(s.paths[51]).write_text('changed interpreter')
                    elif case=='argv':s.argv[51].append('changed opaque prompt')
                    elif case=='birth':s.extra_records=s.extra_records.replace('Thu Oct 29','Wed Oct 28')
                    elif case=='uid':s.extra_records=s.extra_records.replace(f'51 50 {os.getuid()}',f'51 50 {os.getuid()+1}')
                    elif case=='ancestry':s.extra_records=s.extra_records.replace('51 50','51 40')
                    elif case=='pin':s.pin['revision']+=1
                return original(host)
            monkeypatch.setattr(occupancy,'_records',records)
            value=fixture_status(s,await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider),s.pin)
            assert 'host-a:other-seat' in value['missing']
            assert 52 not in [r['process']['pid'] for r in value['managed']]
            assert {'pid':52} in value['unmanaged']
            assert count==2
        finally:s.store.stop()
    asyncio.run(run())


def test_proven_provider_looking_wrapper_is_support_not_second_native(tmp_path,monkeypatch):
    import provider_cli_occupancy as occupancy
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,platform='wsl',launch_type='js')
        try:
            s.native_names[51]=s.other.executable
            value=await s.runtime.executor.verify_launchers(s.selected)
            assert value['unmanaged']==[] and value['missing']==[]
            assert {r['process']['pid'] for r in value['managed']}=={42,52}
        finally:s.store.stop()
    asyncio.run(run())


def test_unknown_undiscovered_pane_is_never_drained(tmp_path,monkeypatch):
    import provider_cli_occupancy as occupancy
    from provider_accounts import AccountError
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,platform='wsl',launch_type='js')
        try:
            s.native_names.pop(52)
            launchers=await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider)
            value=occupancy.status(s.host,s.other,launchers,mutation_state=False)
            assert value['missing']==['host-a:other-seat'] and value['unmanaged']==[]
            with pytest.raises(AccountError,match='account_repin_home_busy'):
                occupancy.assert_drained(s.host,s.other,launchers)
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('case',['same-hash-wrong-target','bad-binding-hash','retarget','cycle','dangling'])
def test_native_symlink_target_and_hash_are_both_required(tmp_path,monkeypatch,case):
    import provider_cli_occupancy as occupancy
    from pathlib import Path
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,'claude',launch_type='symlink')
        try:
            link=Path(s.other.executable)
            if case=='same-hash-wrong-target':
                other=tmp_path/'different-native';other.write_bytes(Path(s.paths[52]).read_bytes());s.paths[52]=str(other)
            elif case=='bad-binding-hash':link.resolve().write_text('unpinned bytes')
            else:
                link.unlink();link.symlink_to(s.selected.executable if case=='retarget' else link if case=='cycle' else tmp_path/'absent')
            value=fixture_status(s,await occupancy.descriptors(s.runtime,s.selected.host,s.selected.provider),s.pin)
            assert 'host-a:other-seat' in value['missing']
            assert 52 not in [r['process']['pid'] for r in value['managed']]
        finally:s.store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('raw',[b'',b'node',b'node\0',b'node\0/script\0',b'node\0/script\0opaque \'\\\0',b'x'*65537])
def test_kernel_argv_is_bounded_structured_data(tmp_path,monkeypatch,raw):
    import io
    import provider_cli_occupancy as occupancy
    monkeypatch.setattr(occupancy,'_helper_os',lambda:'linux')
    def opened(path,mode):
        assert path=='/proc/42/cmdline' and mode=='rb'
        return io.BytesIO(raw)
    monkeypatch.setattr(occupancy,'open',opened,raising=False)
    if raw and raw.endswith(b'\0') and len(raw)<=65536:
        assert occupancy._kernel_argv(42)==[os.fsdecode(x) for x in raw[:-1].split(b'\0')]
    else:
        with pytest.raises(ValueError):occupancy._kernel_argv(42)


@pytest.mark.parametrize('same_image',[True,False])
def test_linux_hashes_running_image_and_refuses_same_path_replacement(tmp_path,monkeypatch,same_image):
    import builtins
    import provider_cli_occupancy as occupancy
    original=tmp_path/'running';original.write_bytes(b'identical pinned bytes')
    replacement=tmp_path/'replacement';replacement.write_bytes(original.read_bytes())
    seen=[]
    def opened(path,mode):
        seen.append(str(path))
        return builtins.open(original if str(path)=='/proc/42/exe' else path,mode)
    monkeypatch.setattr(occupancy,'open',opened,raising=False)
    monkeypatch.setattr(occupancy,'_helper_os',lambda:'linux')
    if same_image:assert occupancy._executed_file(42,str(original),{})['path']==str(original)
    else:
        with pytest.raises(ValueError):occupancy._executed_file(42,str(replacement),{})
    assert '/proc/42/exe' in seen


def test_resolved_configured_native_without_descriptor_stays_unmanaged(tmp_path,monkeypatch):
    import provider_cli_occupancy as occupancy
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,'claude',launch_type='symlink')
        try:
            versioned=tmp_path/'2.2.999';versioned.write_text('unknown native fixture')
            link=tmp_path/'unknown-launcher';link.symlink_to(versioned)
            s.host.config['cells']['claude:unregistered']={'executable':str(link)}
            s.native_names[99]=str(versioned)
            s.extra_records=f'99 1 {os.getuid()} 99 99 {s.started} unknown\n'
            value=fixture_status(s,await occupancy.descriptors(s.runtime,s.selected.host,'claude'),s.pin)
            assert value['missing']==[] and value['unmanaged']==[{'pid':99}]
            assert {r['process']['pid'] for r in value['managed']}=={42,52}
        finally:s.store.stop()
    asyncio.run(run())


def test_two_claude_aliases_can_share_target_with_distinct_pane_proofs(tmp_path,monkeypatch):
    import provider_cli_occupancy as occupancy
    async def run():
        s=await occupancy_journey(tmp_path,monkeypatch,'claude',launch_type='symlink')
        try:
            s.host.config['cells']['claude:third']=copy.deepcopy(s.host.config['cells']['claude:other'])
            permit={'action':'enroll','expected_generation':'unenrolled','generation':'third-generation',
                'clearance_id':'c'*64,'host_binding':None,'central_intent':'d'*64,'reserved_at_ns':1}
            s.host.handle('mutation-reserve',provider='claude',alias='third',expected_generation='unenrolled',
                generation='third-generation',operation_id='fixture-third-owner',reconcile=permit)
            s.host.handle('enroll',provider='claude',alias='third',expected_generation='unenrolled',
                generation='third-generation',operation_id='fixture-third-owner',reconcile={'_clearance':'c'*64})
            third=s.host.registry.active('claude','third',s.selected.host)
            await s.runtime.run(lambda a:a.register(third))
            s.roots[60]='third-seat';s.parents[62]=60;s.paths[62]=s.paths[52]
            s.native_names[62]=s.native_names[52];s.commands[62]=third.executable+" operator's quote"
            await s.store.open_session(s.selected.host,'third-seat',provider='claude',session_generation='third-seat-generation',
                pane_pid='60',account_binding=third.public(),observer_binding={'pane_started_at':s.started,
                    'generation':'third-seat-generation','executable':third.executable})
            value=await s.runtime.executor.verify_launchers(s.other)
            assert value['missing']==[] and value['unmanaged']==[] and len(value['managed'])==3
            rows={r['binding']['alias']:r for r in value['managed']}
            assert rows['other']['process']['pid']==52 and rows['third']['process']['pid']==62
            assert rows['other']['binding']['generation']!=rows['third']['binding']['generation']
        finally:s.store.stop()
    asyncio.run(run())
