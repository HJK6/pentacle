"""Incident-bound regression tests: foreign first-bind and reminder parity."""
import asyncio
from pathlib import Path
import pytest
import ingest as ingest_module
from ingest import Ingest, _StreamIngest
from sessions import Sessions
from store import Store
from ledger import NudgeJob
from test_nudges import Harness

FIXTURES = Path(__file__).parent / "fixtures"


def test_foreign_descendant_transcript_cannot_first_bind(monkeypatch):
    async def run():
        store = Store(":memory:"); store.start()
        class Tmux:
            async def pane_pid(self, name): return "8123"
        async def tree(pid): return ["8123", "9000"]
        async def transcripts(pids):
            return [str(FIXTURES / "codex_rollout_foreign_session.jsonl")]
        monkeypatch.setattr(ingest_module, "process_tree", tree)
        monkeypatch.setattr(ingest_module, "_open_transcripts", transcripts)
        try:
            sessions = Sessions(store, local_host="h")
            await sessions.open("h", "v2-new", provider="codex", pane_pid="8123", created_at="2026-09-09T16:19:32Z")
            ingest = Ingest(store, sessions, Tmux(), lambda frame: asyncio.sleep(0), local_host="h", recent_limit=500)
            row = sessions.get("h:v2-new")
            count = await ingest._ingest_stream(row, _StreamIngest(), 500)
            assert count == 0, "foreign descendant transcript admitted to a new generation"
            assert await store.fetch_session_event_tail("h:v2-new", limit=500) == []
        finally: store.stop()
    asyncio.run(run())


def test_old_history_does_not_restore_new_generation_engagement():
    async def run():
        store = Store(":memory:"); store.start()
        try:
            sessions = Sessions(store, local_host="h")
            await sessions.open("h", "v2-new", provider="codex", created_at="2026-09-09T16:19:32Z")
            await store.append_session_event("h:v2-new", {"kind":"USER", "text":"D2 actor READY", "timestamp":"2026-09-08T22:24:44Z"}, identity="old-history", limit=500)
            sessions.restore_genuine_activity("h:v2-new", await store.fetch_session_event_tail("h:v2-new",limit=500))
            assert sessions.get("h:v2-new").get("genuine_activity_at") is None
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("count", [0, 1])
def test_codex_waits_for_two_real_user_turns(count):
    async def run():
        store = Store(":memory:"); store.start()
        try:
            h = Harness(store)
            await h.open("new", provider="codex", user_event_count=0)
            for n in range(count):
                await store.append_session_event("hosta:new", {"kind":"USER", "provider":"codex", "text":"Investigate this task", "timestamp":f"2026-08-01T00:00:0{n}Z"}, identity=f"user-{n}", limit=500)
            await h.qualify("new", user_activity=False)
            result = await h.job.run_pass()
            assert result.sent == 0, "Codex bypassed the real-USER grace"
        finally: store.stop()
    asyncio.run(run())


def test_remote_current_generation_idle_evidence_is_eligible():
    async def run():
        store = Store(":memory:"); store.start()
        class Comms:
            def __init__(self): self.messages=[]
            async def tell(self, msg): self.messages.append(msg)
        try:
            h=Harness(store)
            await h.open("remote", host="hostb", provider="claude")
            sid="hostb:remote"
            row=h.sessions.get(sid)
            from presence import RemotePresence, PresenceConfig
            from test_remote_presence import _CheckedPreviewTmux, _FakeHosts, _observe_and_apply
            transport=_CheckedPreviewTmux((0,"remote\t1234\n"), h.tmux.IDLE)
            hosts=_FakeHosts("hosta",{"hostb":transport},online={"hostb":True})
            presence=RemotePresence(h.sessions,hosts,config=PresenceConfig(max_rows_per_pass=1))
            await _observe_and_apply(presence)
            observed=h.sessions.get(sid)
            assert observed["online"] is True and observed["working"] is False
            assert transport.checked_calls == 1
            h.sessions.restore_genuine_activity(sid, await store.fetch_session_event_tail(sid, limit=64))
            comms=Comms(); job=NudgeJob(h.sessions, comms, store)
            result=await job.run_pass()
            assert result.sent == 1, "remote ready operator seat excluded for lacking local_mirror"
        finally: store.stop()
    asyncio.run(run())


def _actual_provider_descriptor_case(tmp_path, mode, expected):
    """Run the real ps/lsof path against an owned process and foreign fixture."""
    import json
    import os
    import subprocess
    import sys
    import time
    from prockill import process_records
    transcript=tmp_path/".codex"/"sessions"/"foreign.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes((FIXTURES/"codex_rollout_foreign_session.jsonl").read_bytes())
    ready=tmp_path/"ready"
    actor_executable=subprocess.check_output(["ps","-p",str(os.getpid()),"-o","comm="],text=True).strip()
    # Darwin comm is an executable path; Linux comm is only a basename.
    if not os.path.isabs(actor_executable):
        actor_executable=os.path.realpath(sys.executable)
    proc=subprocess.Popen([actor_executable,"-c",
        "import pathlib,sys,time; f=open(sys.argv[1],sys.argv[3]); pathlib.Path(sys.argv[2]).touch(); time.sleep(30)",
        str(transcript),str(ready),mode])
    async def run():
        store=Store(":memory:"); store.start()
        class Tmux:
            async def pane_pid(self,name): return str(proc.pid)
        try:
            started_at=subprocess.check_output(["ps","-p",str(proc.pid),"-o","lstart="],text=True).strip()
            sessions=Sessions(store,local_host="h")
            await sessions.open("h","v2-descriptor",provider="codex",pane_pid=str(proc.pid),
                observer_binding={"executable":os.path.realpath(actor_executable),
                    "pane_started_at":started_at})
            row=sessions.get("h:v2-descriptor")
            ingest=Ingest(store,sessions,Tmux(),lambda frame:asyncio.sleep(0),local_host="h",recent_limit=500)
            state=_StreamIngest()
            try:
                assert await ingest._ingest_stream(row,state,500)==expected
                assert len(await store.fetch_session_event_tail("h:v2-descriptor",limit=500))==expected
            finally:
                from ingest import _close_stream
                _close_stream(state)
        finally: store.stop()
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and time.monotonic()<deadline: time.sleep(.01)
        assert ready.exists(), "owned fixture did not open its read descriptor"
        asyncio.run(run())
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
    assert proc.poll() is not None


def test_actual_readonly_provider_descriptor_cannot_first_bind(tmp_path):
    _actual_provider_descriptor_case(tmp_path,"r",0)


def test_actual_writable_provider_descriptor_binds(tmp_path):
    _actual_provider_descriptor_case(tmp_path,"a",1)


@pytest.mark.parametrize("provider,receipt,environment,expected", [
    ("codex", None, True, False),
    ("codex", "canonical-send-receipt", True, True),
    ("codex", None, False, True),
    ("claude", None, True, True),
])
def test_native_codex_agents_bootstrap_is_not_an_operator_turn(provider, receipt, environment, expected):
    from sessions import operator_user_epoch
    text = "# AGENTS.md instructions for /home/example\n\nRead local instructions."
    if environment:
        text += "\n<environment_context>\n<cwd>/home/example</cwd>\n</environment_context>"
    event = {"kind": "USER", "provider": provider, "text": text,
             "timestamp": "2026-09-09T20:31:52.339Z", "raw": {
                 "codex_record_type": "response_item", "codex_payload_type": "message"}}
    if receipt:
        event["receipt_id"] = receipt
    assert (operator_user_epoch(event, "2026-09-09T20:25:54Z") is not None) is expected


def test_spawn_retains_readable_current_process_birth():
    import os
    import subprocess
    from spawnctl import SpawnCtl
    expected=subprocess.check_output(["ps","-p",str(os.getpid()),"-o","lstart="],text=True).strip()
    async def run():
        store=Store(":memory:"); store.start()
        try:
            sessions=Sessions(store,local_host="h")
            ctl=SpawnCtl(store,sessions)
            assert await ctl._pane_started_at(str(os.getpid()),host="h") == " ".join(expected.split())
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("text,raw", [
    ("Please run: agent-orch title \"goal\"", {}),
    ("<environment_context>bootstrap</environment_context>", {}),
    ("peer task", {"from_stream_id":"h:peer"}),
    ("sidechain task", {"is_sidechain":True}),
])
def test_non_operator_user_events_do_not_satisfy_grace(text,raw):
    async def run():
        store=Store(":memory:"); store.start()
        try:
            h=Harness(store)
            await h.open("new",provider="claude",user_event_count=0)
            for n in range(2):
                await store.append_session_event("hosta:new",{"kind":"USER","provider":"claude",
                    "text":text,"raw":raw,"timestamp":f"2026-08-01T00:00:0{n}Z"},identity=f"nonuser-{n}",limit=500)
            await h.qualify("new", user_activity=False)
            assert (await h.job.run_pass()).sent == 0
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("host", ["hosta", "hostb"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("turns", [0,1,2])
def test_provider_host_real_user_grace_matrix(host,provider,turns):
    import time
    from datetime import datetime,timezone
    from presence import RemotePresence,PresenceConfig
    from test_remote_presence import _CheckedPreviewTmux,_FakeHosts,_observe_and_apply
    async def run():
        store=Store(":memory:");store.start()
        class Comms:
            async def tell(self,msg): pass
        try:
            h=Harness(store)
            await h.open("parity",host=host,provider=provider,user_event_count=0)
            sid=f"{host}:parity"
            for n in range(turns):
                event={"kind":"USER","provider":provider,"text":f"Operator task {n}",
                    "timestamp":datetime.fromtimestamp(time.time()-2+n,timezone.utc).isoformat()}
                await store.append_session_event(sid,event,identity=f"operator-{n}",limit=500)
                h.sessions.apply_genuine_activity_event(sid,event)
            if host=="hosta":
                await h.observe()
            else:
                transport=_CheckedPreviewTmux((0,"parity\t1234\n"),h.tmux.IDLE)
                hosts=_FakeHosts("hosta",{host:transport},online={host:True})
                presence=RemotePresence(h.sessions,hosts,config=PresenceConfig(max_rows_per_pass=1))
                await _observe_and_apply(presence)
            result=await NudgeJob(h.sessions,Comms(),store).run_pass()
            assert result.sent == int(turns>=2)
        finally: store.stop()
    asyncio.run(run())


def test_remote_restart_restores_user_grace_and_preserves_cooldown(tmp_path,monkeypatch):
    import time
    import ledger
    from presence import RemotePresence
    from test_remote_presence import _CheckedPreviewTmux,_FakeHosts,_observe_and_apply
    async def run():
        db=tmp_path/"restart.db";store=Store(db);store.start()
        class Comms:
            async def tell(self,msg):pass
        async def capture(sessions):
            transport=_CheckedPreviewTmux((0,"remote\t1234\n"),"⏵⏵ bypass permissions on (bypass)\n❯ \n")
            hosts=_FakeHosts("hosta",{"hostb":transport},online={"hostb":True})
            await _observe_and_apply(RemotePresence(sessions,hosts))
        try:
            h=Harness(store);await h.open("remote",host="hostb",provider="claude")
            await capture(h.sessions)
            assert (await NudgeJob(h.sessions,Comms(),store).run_pass()).sent==1
            before=time.time();store.stop();store=Store(db);store.start()
            sessions=Sessions(store,local_host="hosta");await sessions.refresh();await capture(sessions)
            job=NudgeJob(sessions,Comms(),store)
            assert (await job.run_pass()).sent==0
            monkeypatch.setattr(ledger.time,"time",lambda:before+3601)
            assert (await job.run_pass()).sent==1
        finally:store.stop()
    asyncio.run(run())


def test_reminder_responses_cannot_rearm_status(monkeypatch):
    import time
    import ledger
    from datetime import datetime,timezone
    async def run():
        store=Store(":memory:");store.start()
        try:
            h=Harness(store);await h.open("feedback",title="Owned goal");await h.qualify("feedback")
            assert (await h.job.run_pass()).sent==1
            later=time.time()+3601
            monkeypatch.setattr(ledger.time,"time",lambda:later)
            for n,(kind,text) in enumerate([("USER","Please update your status card: agent-orch status"),("TOOL_USE","status"),("ASSIST_TEXT","Updated")]):
                event={"kind":kind,"provider":"claude","text":text,"timestamp":datetime.fromtimestamp(later,timezone.utc).isoformat()}
                await store.append_session_event("hosta:feedback",event,identity=f"feedback-{n}",limit=500)
                h.sessions.apply_genuine_activity_event("hosta:feedback",event)
            assert (await h.job.run_pass()).sent==0
        finally:store.stop()
    asyncio.run(run())
