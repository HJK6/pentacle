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
