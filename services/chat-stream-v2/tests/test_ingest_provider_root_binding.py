"""Provider-root binding, lifecycle transfer and restart regression matrix."""
import asyncio
import json
import os
from pathlib import Path
import pytest
import ingest as module
from ingest import Ingest,_StreamIngest,_close_stream
from sessions import Sessions
from store import Store

BIRTH="Wed Sep 9 11:00:00 2026"
PID="8123"
EXE="/provider/codex"


def test_existing_database_gains_only_empty_observer_authority(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    async def seed():
        store = Store(str(path)); store.start()
        try:
            await store.open_session("h", "legacy", provider="codex", pane_pid=PID)
        finally:
            store.stop()
    asyncio.run(seed())
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE sessions DROP COLUMN observer_binding")
    async def check():
        store = Store(str(path)); store.start()
        try:
            row = await store.fetch_session("h", "legacy")
            assert row["status"] == "open" and row["pane_pid"] == PID
            assert row.get("observer_binding") is None
        finally:
            store.stop()
    asyncio.run(check())
    asyncio.run(check())  # Additive migration is idempotent; no proof backfill.


def write_log(path,identity="native-a",turn="one"):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({"type":"session_meta","payload":{"id":identity}})+"\n"+
        json.dumps({"type":"response_item","timestamp":"2026-09-09T16:20:00Z",
          "payload":{"type":"message","id":turn,"role":"user","content":[{"type":"input_text","text":"Operator goal "+turn}]}})+"\n")


class Harness:
    def __init__(self,store,monkeypatch,path,provider="codex"):
        self.store=store; self.sessions=Sessions(store,local_host="h"); self.provider=provider
        self.path=path;self.pid=PID;self.birth=BIRTH;self.command=EXE
        self.descriptors=[(PID,"12","w",path)];self.calls=[]
        async def record(pid):
            return {"pid":int(pid),"uid":os.getuid(),"start_id":self.birth,"command":self.command}
        async def execute(*args):
            self.calls.append(args)
            return 0,"\n".join(f"p{pid}\nf{fd}\na{access}\ntREG\nD{hex(path.stat().st_dev)}\ni{path.stat().st_ino}\nn{path}" for pid,fd,access,path in self.descriptors)
        monkeypatch.setattr(module,"process_record",record)
        monkeypatch.setattr(module,"_exec",execute)
    async def pane_pid(self,name):return self.pid
    async def open(self):
        assert await self.store.reserve_stream_id("h","v2-root",ttl_s=60,request_id="spawn",nonce="nonce")
        assert await self.store.record_spawn_intent("h","v2-root",{"open_fields":{"session_generation":"gen-a"}},request_id="spawn",nonce="nonce")
        assert await self.store.commit_tmux_created_fenced("h","v2-root",request_id="spawn",nonce="nonce",pane_pid=PID,pane_started_at=BIRTH)
        await self.sessions.open("h","v2-root",fence="spawn",provider=self.provider,pane_pid=PID,
            created_at="2026-09-09T16:19:32Z",session_generation="gen-a",observer_binding={"executable":EXE})
        assert await self.store.release_stream_id_fenced("h","v2-root","spawn")
    def ingest(self):
        return Ingest(self.store,self.sessions,self,lambda frame:asyncio.sleep(0),local_host="h",recent_limit=500)
    async def run(self,ingest,state):
        return await ingest._ingest_stream(self.sessions.get("h:v2-root"),state,500)


@pytest.mark.parametrize("case",["readonly","helper","ambiguous","wrong_command","pid_reuse","missing_birth"])
def test_first_bind_rejects_unproven_sources(tmp_path,monkeypatch,case):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open()
            if case=="readonly":h.descriptors=[(PID,"12","r",path)]
            if case=="helper":h.descriptors=[("9000","12","w",path)]
            if case=="ambiguous":
                other=path.with_name("b.jsonl");write_log(other,"native-b")
                h.descriptors.append((PID,"13","w",other))
            if case=="wrong_command":h.command="/helper --provider "+EXE
            if case=="pid_reuse":h.birth="Wed Sep 9 11:00:01 2026"
            if case=="missing_birth":
                await store.update_session("h","v2-root",observer_binding=None)
                await h.sessions.refresh()
            assert await h.run(h.ingest(),state)==0
            assert await store.fetch_session_event_tail("h:v2-root",limit=500)==[]
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


def test_binding_survives_reservation_release_writer_gap_and_restart(tmp_path,monkeypatch):
    async def run():
        db=tmp_path/"sessions.db";store=Store(db);store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open()
            alias=path.with_name("alias.jsonl");os.link(path,alias)
            h.descriptors.append((PID,"13","w",alias))
            assert await h.run(h.ingest(),state)==1
            row=await store.fetch_session("h","v2-root")
            assert row["observer_binding"]["generation"]=="gen-a"
            assert row["observer_binding"]["pane_started_at"]==BIRTH
            assert row["observer_binding"]["transcript"]["session_id"]=="native-a"
            assert await store.reservations(include_expired=True)==[]
            _close_stream(state);store.stop()
            store=Store(db);store.start();h.store=store;h.sessions=Sessions(store,local_host="h");await h.sessions.refresh()
            h.descriptors=[];state=_StreamIngest()
            with path.open("a") as log:
                log.write(json.dumps({"type":"response_item","timestamp":"2026-09-09T16:21:00Z",
                    "payload":{"type":"message","id":"two","role":"user","content":[{"type":"input_text","text":"Second goal"}]}})+"\n")
            h.calls.clear()
            assert await h.run(h.ingest(),state)==1
            assert h.calls==[],"durable binding must not require a current writer"
            assert len(await store.fetch_session_event_tail("h:v2-root",limit=500))==2
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("change",["birth","native","inode","generation"])
def test_binding_revokes_on_identity_change(tmp_path,monkeypatch,change):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open();ingest=h.ingest()
            assert await h.run(ingest,state)==1
            if change=="birth":h.birth="Wed Sep 9 11:00:01 2026"
            if change=="native":write_log(path,"native-b","foreign")
            if change=="inode":
                other=path.with_name("replacement");write_log(other,"native-b","foreign");os.replace(other,path)
            if change=="generation":
                await store.update_session("h","v2-root",status="closed")
                await h.sessions.open("h","v2-root",provider="codex",pane_pid=PID,session_generation="gen-b")
            assert await h.run(ingest,state)==0
            assert await h.run(h.ingest(),_StreamIngest())==0
            tail=await store.fetch_session_event_tail("h:v2-root",limit=500)
            assert all(event.get("session_id")!="native-b" for event in tail)
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


def test_first_open_cannot_follow_replaced_descriptor_path(tmp_path,monkeypatch):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open()
            original=module._open_descriptor
            def replace_before_open(name):
                other=path.with_name("other");write_log(other,"foreign","foreign");os.replace(other,path)
                return original(name)
            monkeypatch.setattr(module,"_open_descriptor",replace_before_open)
            assert await h.run(h.ingest(),state)==0
            assert await store.fetch_session_event_tail("h:v2-root",limit=500)==[]
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


def test_reopen_between_read_and_admission_does_not_restamp_old_batch(tmp_path,monkeypatch):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open()
            original=store.fetch_open_session_lifecycle
            async def reopen(sid,**kwargs):
                await store.update_session("h","v2-root",status="closed")
                await h.sessions.open("h","v2-root",provider="codex",pane_pid=PID,session_generation="gen-b")
                return await original(sid,**kwargs)
            monkeypatch.setattr(store,"fetch_open_session_lifecycle",reopen)
            assert await h.run(h.ingest(),state)==0
            assert await store.fetch_session_event_tail("h:v2-root",limit=500)==[]
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


NEWPID="9099"
NEWBIRTH="Wed Sep 9 12:34:56 2026"


def _append_turn(path,turn,stamp="2026-09-09T16:22:00Z"):
    with path.open("a") as log:
        log.write(json.dumps({"type":"response_item","timestamp":stamp,
            "payload":{"type":"message","id":turn,"role":"user",
                       "content":[{"type":"input_text","text":"Operator goal "+turn}]}})+"\n")


async def _first_bind(h,store,state):
    """Bind the transcript once, then simulate an in-place provider replacement:
    same rollout inode/session, SAME generation, new pane process; the reconciler
    has refreshed sessions.pane_pid to the new pane while observer_binding still
    names the process minted at spawn."""
    assert await h.run(h.ingest(),state)==1
    h.pid=NEWPID;h.birth=NEWBIRTH
    h.descriptors=[(NEWPID,"12","w",h.path)]
    await store.update_session("h","v2-root",pane_pid=NEWPID)
    await h.sessions.refresh()


def test_inplace_same_rollout_replacement_rebinds_and_recovers_gap(tmp_path,monkeypatch):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open()
            await _first_bind(h,store,state)
            before=await store.fetch_session_event_tail("h:v2-root",limit=500)
            assert len(before)==1
            _append_turn(path,"two")
            # Same generation, same rollout, proven new pane → rebind + ingest the
            # gap. Before the fix _provider_root rejects on the stale binding PID
            # and the stream is closed every pass, freezing server history.
            assert await h.run(h.ingest(),state)==1
            binding=(await store.fetch_session("h","v2-root"))["observer_binding"]
            assert binding["pane_pid"]==NEWPID and binding["pane_started_at"]==NEWBIRTH
            assert binding["generation"]=="gen-a"
            assert binding["transcript"]["session_id"]=="native-a"
            tail=await store.fetch_session_event_tail("h:v2-root",limit=500)
            assert len(tail)==2,"gap turn recovered under the rebound pane"
            # Replay-from-start on the same rollout must not duplicate the pre-gap
            # event (durable identity dedup).
            assert len(tail)==len({json.dumps(e.get("raw",{}).get("jsonl_record_uuid")) +
                                   str(e.get("raw",{}).get("jsonl_event_index")) for e in tail})
            # Fresh ongoing ingestion continues under the rebound pane.
            _append_turn(path,"three",stamp="2026-09-09T16:23:00Z")
            assert await h.run(h.ingest(),state)==1
            assert len(await store.fetch_session_event_tail("h:v2-root",limit=500))==3
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


def test_inplace_rebind_refuses_cross_generation_binding(tmp_path,monkeypatch):
    """A binding whose generation no longer matches the row must never be
    within-generation rebound — that is the normal reset/first-bind path's job.
    (A real generation change rebuilds the binding and drops the transcript;
    here we prove the guard directly with a mismatched-but-anchored binding.)"""
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path);await h.open()
            await _first_bind(h,store,state)
            row=dict(h.sessions.get("h:v2-root"));row["session_generation"]="gen-b"
            assert isinstance(row["observer_binding"].get("transcript"),dict)
            assert row["observer_binding"]["generation"]=="gen-a"
            out=await h.ingest()._maybe_rebind_observer_pane(row,"v2-root")
            assert out is row,"cross-generation rebind must not fire"
            assert (await store.fetch_session("h","v2-root"))["observer_binding"]["pane_pid"]==PID
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("case",["dead_pid","wrong_command","wrong_uid","ambiguous",
                                 "foreign_inode","foreign_session","tmux_mismatch",
                                 "no_transcript","cas_lost"])
def test_inplace_rebind_refuses_unproven_replacement(tmp_path,monkeypatch,case):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".codex"/"sessions"/"a.jsonl";write_log(path)
            h=Harness(store,monkeypatch,path)
            if case=="no_transcript":
                # Never let the first transcript bind, so there is no proven
                # rollout anchor to rebind against.
                await h.open()
                await store.update_session("h","v2-root",pane_pid=NEWPID)
                h.pid=NEWPID;h.birth=NEWBIRTH;h.descriptors=[(NEWPID,"12","w",path)]
                await h.sessions.refresh()
            else:
                await h.open();await _first_bind(h,store,state)
            _append_turn(path,"two")
            if case=="dead_pid":
                async def record(pid):return None
                monkeypatch.setattr(module,"process_record",record)
            if case=="wrong_command":h.command="/somewhere/other-codex"
            if case=="wrong_uid":
                async def record(pid):return {"pid":int(pid),"uid":os.getuid()+9999,
                    "start_id":h.birth,"command":h.command}
                monkeypatch.setattr(module,"process_record",record)
            if case=="ambiguous":
                other=path.with_name("b.jsonl");write_log(other,"native-b")
                h.descriptors=[(NEWPID,"12","w",path),(NEWPID,"13","w",other)]
            if case=="foreign_inode":
                other=path.with_name("foreign.jsonl");write_log(other,"native-foreign","xturn")
                h.descriptors=[(NEWPID,"12","w",other)]
            if case=="foreign_session":
                # SAME inode as the bound transcript, but the rollout's session
                # identity has changed — the last-line-of-defense session_id check
                # must refuse (inode reuse cannot cross-bind another session).
                write_log(path,"native-b","foreign")
            if case=="tmux_mismatch":
                async def pane(name):return "5555"
                h.pane_pid=pane
            if case=="cas_lost":
                # The compare-and-swap lost to a concurrent binding change (or the
                # durable row's pane_pid still lags the registry): rebind must
                # leave the row untouched and refuse this pass.
                async def norebind(*a,**k):return None
                monkeypatch.setattr(store,"rebind_observer_pane",norebind)
            assert await h.run(h.ingest(),_StreamIngest())==0
            binding=(await store.fetch_session("h","v2-root")).get("observer_binding") or {}
            # No rebind occurred: the stale spawn PID is untouched.
            if case!="no_transcript":
                assert binding.get("pane_pid")==PID and binding.get("pane_started_at")==BIRTH
            tail=await store.fetch_session_event_tail("h:v2-root",limit=500)
            assert all(e.get("session_id") not in ("native-b","native-foreign") for e in tail)
            assert all("Operator goal two" not in json.dumps(e) for e in tail),"gap not ingested under an unproven pane"
        finally:_close_stream(state);store.stop()
    asyncio.run(run())


def test_claude_root_binding_preserves_history_without_new_engagement(tmp_path,monkeypatch):
    async def run():
        store=Store(":memory:");store.start();state=_StreamIngest()
        try:
            path=tmp_path/".claude"/"projects"/"a.jsonl";path.parent.mkdir(parents=True)
            def event(turn,stamp):
                return json.dumps({"type":"user","sessionId":"native-a","uuid":turn,"timestamp":stamp,
                    "message":{"role":"user","content":"Operator goal "+turn}})+"\n"
            path.write_text(event("history","2026-09-08T16:20:00Z"))
            h=Harness(store,monkeypatch,path,provider="claude");await h.open();ingest=h.ingest()
            assert await h.run(ingest,state)==1
            assert h.sessions.get("h:v2-root").get("operator_activity_at") is None
            assert (await store.fetch_session("h","v2-root"))["observer_binding"]["transcript"]["session_id"]=="native-a"
            with path.open("a") as log:log.write(event("fresh","2026-09-09T16:20:00Z"))
            assert await h.run(ingest,state)==1
            assert h.sessions.get("h:v2-root")["operator_activity_at"] is not None
            assert len(await store.fetch_session_event_tail("h:v2-root",limit=500))==2
        finally:_close_stream(state);store.stop()
    asyncio.run(run())
