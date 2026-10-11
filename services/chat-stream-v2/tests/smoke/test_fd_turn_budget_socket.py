"""Owned private socket, actual event.push/Comms, and immediate retro CLI."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest
import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from claude_jsonl_norm import normalize_claude_jsonl_records, normalize_claude_jsonl_record
from comms import Comms
from event_push import EventPush, WIRE_VERSION
from fd_turn_budget import FdTurnBudgetConfig
from ingest import append_ingested_event
from outbound_notices import OutboundNoticeQueue
from server import Server
from sessions import Sessions
from store import Store, SESSION_EVENT_TAIL_DDL, SESSIONS_DDL
from _shared.operator_auth import OperatorCredentialRegistry


@pytest.mark.parametrize("mode", ["record_only", "enabled", "stale_revision", "stale_generation"])
def test_fd_budget_socket_delivery_and_immediate_collector(tmp_path, monkeypatch, mode):
    enabled = mode != "record_only"
    eligible = mode == "enabled"
    for key in list(os.environ):
        if key.startswith(("PENTACLE_","AGENT_ORCH_")) or key in {"TMUX","TMUX_PANE"}:
            monkeypatch.delenv(key,raising=False)

    async def run():
        store=Store(str(tmp_path/"owned.db"));store.start()
        server=composite=None
        input_calls=[]
        input_events=[]
        try:
            row=await store.open_session("fixture","fd",provider="claude",pane_status="pane_alive",pane_pid="123")
            binding={"stream_id":"fixture:fd","generation":row["session_generation"]}
            store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=enabled),env_binding=binding)
            cfg=AssistantCompositeConfig.from_env({"PENTACLE_ASSISTANT_COMPOSITE_ENABLED":"1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID":"fixture:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID":"fixture:fd",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION":binding["generation"]})
            composite=AssistantComposite(store,config=cfg);await composite.load_binding()
            assert composite.front_desk_digest.enabled()  # Default, no disabling override.
            class Provider:
                async def capture(self,name): return "Synthetic busy provider"
                async def run(self,*args,**kwargs):
                    input_calls.append(list(args));return 0,""
                async def paste(self,name,body):
                    input_calls.append("paste+Enter")
                    event=normalize_claude_jsonl_record({"type":"user","uuid":"notice-input","timestamp":"2026-10-10T00:00:07Z",
                        "message":{"content":body}},host="fixture",session_name="fd")[0]
                    input_events.append(event)
                    await append_ingested_event(store,server.broadcast,event,recent_limit=100)
            tmux=Provider();sessions=Sessions(store,tmux=tmux,local_host="fixture");await sessions.refresh()
            comms=Comms(store,sessions,SimpleNamespace(tmux=tmux),attachment_root=tmp_path/"attachments")
            async def ingress(**kwargs):return await composite.suppress_routine_backend_ingress(**kwargs)
            comms.assistant_ingress_policy=ingress
            queue=OutboundNoticeQueue(store,comms)
            @asynccontextmanager
            async def lock(notice):
                async with composite._binding_lock:
                    async with sessions._lifecycle_lock("fixture","fd"):
                        yield
            queue.register_kind("fd_turn_budget",lock_factory=lock)
            server=Server(host="127.0.0.1",port=0,store=store,sessions=sessions,comms=comms,local_host="fixture",
                dot_principal_stream_ids=[],dot_tls_port=0,dot_tls_cert="",dot_tls_key="",dot_tls_binds=["127.0.0.1"],
                dot_read_enabled=False,seat_operator_authority=False)
            server.operator_credential_registry=OperatorCredentialRegistry(tmp_path/"credentials.json")
            class Alerts:
                def emit(self,*a,**kw):pass
                async def record(self,*a,**kw):pass
            push=EventPush(store,server.broadcast,Alerts(),recent_limit=100,enabled=True,sessions=sessions)
            async def secret():return "fixture-push-secret"
            push._secret=secret
            server.handlers.update(push.wire_handlers())
            port=await server.bind()
            records=[{"type":"user","uuid":"user-1","timestamp":"2026-10-10T00:00:00Z","message":{"content":"Fixture operator turn"}}]
            records += [{"type":"assistant","uuid":f"tool-{i}","timestamp":f"2026-10-10T00:00:0{i}Z",
                "message":{"content":[{"type":"tool_use","id":f"call-{i}","name":"fixture","input":{}}]}} for i in range(1,7)]
            events=normalize_claude_jsonl_records(records,host="fixture",session_name="fd")
            async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()
                async def rpc(n, payload=None):
                    await ws.send(json.dumps({"type":"event.push","request_id":n,"push_secret":"fixture-push-secret",
                        "wire_version":WIRE_VERSION,"host":"fixture","satellite_sha":"","satellite_pid":4321,"events":events if payload is None else payload,"high_water":{}}))
                    async with asyncio.timeout(5):
                        while True:
                            response=json.loads(await ws.recv())
                            if response.get("request_id")==n:return response
                first=await rpc(1)
                assert first["type"]=="event.push.ok" and first["inserted"]==7,first
                notice=await store.submit(lambda c: (dict(r) if (r:=c.execute("SELECT * FROM v2_outbound_notices WHERE kind='fd_turn_budget'").fetchone()) else None))
                if mode == "stale_revision":
                    await store.submit(lambda c: (c.execute("INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES (?,?,?,1,'2026-10-10T00:00:00Z')",(cfg.name,binding['stream_id'],binding['generation'])), c.commit()))
                elif mode == "stale_generation":
                    await store.open_session("fixture","fd",provider="claude",pane_status="pane_alive",pane_pid="124")
                delivered=await queue.deliver_now(notice["notice_id"]) if notice else False
                rows=await store.submit(lambda c:[dict(r) for r in c.execute("SELECT kind,notice_id,delivered_at,terminal_reason FROM v2_outbound_notices")])
                assert delivered is eligible, {"notices":rows,"input_calls":input_calls}
                replay=await rpc(2)
                assert replay["inserted"]==0
                if eligible:
                    notice_replay = await rpc(3, input_events)
                    assert notice_replay["inserted"] == 0
                    assert not await queue.deliver_now(notice["notice_id"])
                    state = await store.fd_turn_budget_status("fixture:fd")
                    assert state["ordinal"] == 2 and state["tool_ids"] == []
                    assert state["first"] is None and state["latest"] is None and not state["crossed"]
            assert input_calls==(["paste+Enter"] if eligible else [])
            assert len(rows)==(1 if enabled else 0)
            assert not eligible or (rows[0]["delivered_at"] and rows[0]["terminal_reason"] is None)
            assert not any(r["kind"] == "front_desk_held" for r in rows)
            if mode.startswith("stale"):
                assert rows[0]["terminal_reason"] == "fd_turn_budget_stale_or_disabled"
            if mode == "record_only":
                assert (await store.fd_turn_budget_status("fixture:fd"))["ordinal"] == 1
            tell=await store.get_tell_delivery(notice["tell_id"]) if notice else None
            assert not eligible or tell["reply"]["delivery_status"]=="delivered"
            event_rows=await store.submit(lambda c:[json.loads(r[0]) for r in c.execute("SELECT event_json FROM session_event_tail WHERE json_extract(event_json,'$.kind')='fd_turn_budget'")])
            assert len(event_rows)==1
            control_rows = []
            if eligible:
                control = await queue.enqueue(kind="reconciler",dedupe_key="ordinary-control",tell_id="ordinary-control",recipient_stream_id="fixture:fd",body="Ordinary synthetic update")
                assert not await queue.deliver_now(control["notice_id"])
                control_rows = await store.submit(lambda c:[dict(r) for r in c.execute("SELECT kind,notice_id,delivered_at,terminal_reason FROM v2_outbound_notices WHERE notice_id!=?",(notice["notice_id"],))])
                assert len([r for r in control_rows if r["kind"] == "front_desk_held"]) == 1
                assert any(r["kind"] == "reconciler" and r["terminal_reason"] == "persisted_suppressed" for r in control_rows)
                assert input_calls == ["paste+Enter"]
                assert (await store.fd_turn_budget_status("fixture:fd"))["ordinal"] == 2
            memory=tmp_path/"memory"
            for folder in ("completed","deprecated"):(memory/"work"/folder).mkdir(parents=True)
            token=tmp_path/"token";token.write_text("fixture-unused-collector-token")
            archive=tmp_path/"archive.db"
            with sqlite3.connect(archive) as conn:
                conn.execute(SESSIONS_DDL);conn.execute(SESSION_EVENT_TAIL_DDL)
            config=tmp_path/"retro.json"
            config.write_text(json.dumps({"memory_root":str(memory),"state_root":str(tmp_path/"retro-state"),
                "ws_url":f"ws://127.0.0.1:{port}","token_path":str(token),"host":"fixture","isolated":True,
                "timezone":"America/Chicago","primary_store":store.path,"primary_archive":str(archive)}))
            process=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).resolve().parents[2]/"tools/daily_retro.py"),
                "collect","--config",str(config),"--now","2026-10-10T05:00:00-05:00",stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            output,error=await process.communicate()
            assert process.returncode==0,error.decode()
            packet=json.loads(output)
            primary=packet["primary"]
            assert primary["turns_over_budget"]["count"]==1
            assert primary["turns_over_budget"]["turn_ids"]==[event_rows[0]["turn_id"]]
            if os.environ.get("FD_BUDGET_SOCKET_RECEIPT"):
                Path(os.environ["FD_BUDGET_SOCKET_RECEIPT"] + "." + mode + ".json").write_text(json.dumps({"first":first,"replay":replay,
                    "mode":mode,"ordinary_control":control_rows,"event":event_rows[0],"notice":rows[0] if rows else None,"tell":tell,"primary":primary,"input_calls":input_calls},indent=2)+"\n")
        finally:
            if server:await server.close()
            if composite:await composite.stop()
            store.stop()
            assert store._thread is None
            if server:assert server._ws_server is None
    asyncio.run(run())
