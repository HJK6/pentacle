"""A real host repin can drain one managed home without draining other homes."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace

import pytest

from test_account_repin import host_journey
from sessions import VerbError


async def occupancy_journey(tmp_path, monkeypatch, provider="codex", platform="macos"):
    """Real host registry + daemon verifier; only process/tmux observations are synthetic."""
    import provider_cli_occupancy as occupancy
    j = await host_journey(tmp_path, monkeypatch, provider, platform)
    store, runtime, host, selected, _, _, _, _, cell, _ = j
    host.config["cells"][provider+":other"] = copy.deepcopy(cell)
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
        paths={42:selected.executable, 52:other.executable}, probes=[], started=started)
    for root, name, bound in ((40,"selected-seat",selected), (50,"other-seat",other)):
        await store.open_session(selected.host, name, provider=provider, session_generation=name+"-generation",
            pane_pid=str(root), account_binding=bound.public(), observer_binding={"pane_started_at":started,
                "generation":name+"-generation", "executable":bound.executable})
    actual_run = host.run
    def running(argv, **kwargs):
        if argv[0] == "ps":
            if "pid=,comm=" in argv:
                output = f"42 {selected.executable}\n52 {other.executable}\n"
            else:
                output = "".join(f"{root} 1 {os.getuid()} {root} {root} {started} /bin/zsh\n"
                    f"{root+2} {root} {os.getuid()} {root} {root} {started} {state.commands[root+2]}\n"
                    for root in (40,50))
            return SimpleNamespace(returncode=0, stdout=output)
        if argv[0] == "tmux":
            name=argv[argv.index("-t")+1][1:]; root=40 if name=="selected-seat" else 50
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
