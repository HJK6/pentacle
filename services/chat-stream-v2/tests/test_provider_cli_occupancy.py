"""A real host repin can drain one managed home without draining other homes."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace

import pytest

from test_account_repin import host_journey
from sessions import VerbError


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
