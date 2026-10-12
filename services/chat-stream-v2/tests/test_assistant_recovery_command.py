"""Recovery orchestration over the unchanged socket/store authority contracts."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import time

import pytest
import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
import assistant_lane_rulings
from server import Server
from notify import Notify
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from test_consent import Ceremony
from test_lifecycle_authority import Env, IdleTmux, OPERATOR
from test_question_contract_d3 import _ask

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "assistant_recovery.py"
module_spec = importlib.util.spec_from_file_location("assistant_recovery_command", SCRIPT)
recovery = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(recovery)

OLD, NEW, CHILD, ADVISOR = "node-a:old", "node-b:new", "node-b:child", "node-a:advisor"
SPEC, CHAT = "spec_example__recovery", "fixture-chat:assistant"
OWNERSHIP_TABLE=next(value for key,value in vars(assistant_lane_rulings).items()
                     if key.endswith("LANE_OWNERSHIP_DDL")).split()[5]


class FixtureGateway:
    actor = NEW

    def __init__(self, store, server, port, token, inputs, ceremony):
        self.store, self.server, self.port, self.token = store, server, port, token
        self.inputs, self.ceremony = inputs, ceremony
        self.calls, self.route_calls = [], []

    async def rpc(self, payload):
        self.calls.append(payload["type"])
        async with websockets.connect(f"ws://{self.server.host}:{self.port}") as socket:
            await socket.recv()
            await socket.send(json.dumps({"type":"hello","client":"recovery-fixture",
                "from_stream_id":self.actor,"stream_token":self.token}))
            while True:
                frame = json.loads(await socket.recv())
                if frame["type"] == "snapshot":
                    break
            msg = dict(payload)
            if not msg["type"].startswith("assistant."):
                msg.update(from_stream_id=self.actor,stream_token=self.token)
            await socket.send(json.dumps(msg))
            async with asyncio.timeout(5):
                while True:
                    frame = json.loads(await socket.recv())
                    if frame.get("request_id") == payload["request_id"]:
                        return frame

    async def snapshot(self):
        state = recovery.snapshot(self.inputs)
        binding = await self.rpc({"type":"assistant.binding","request_id":str(time.time_ns()),
                                  "composite_stream_id":CHAT})
        state["binding"] = {key:binding[key] for key in ("stream_id","generation","revision")}
        authority=await self.rpc({"type":"assistant.authority","action":"read","request_id":str(time.time_ns())})
        state["fixed_authority"]={key:authority.get(key) for key in ("state","stream_id","generation","source")}
        return state

    async def routes(self, inputs):
        self.route_calls.append(inputs)
        return {"passed":True,"routes":[]}

    async def approve(self, journal):
        intent = journal["receipts"]["consent"]["intent"]
        reply = await self.ceremony.call("consent.open",intent_id=intent["request_id"],key_id=self.ceremony.key_id)
        await self.ceremony.approve(reply["challenge"])


@asynccontextmanager
async def fixture(tmp_path, *, tmux=None, authority=False, questions=False, bind_host="127.0.0.1"):
    db = tmp_path / "sessions.db"
    store = Store(str(db)); store.start()
    tmux = tmux or IdleTmux()
    sessions = Sessions(store,tmux=tmux,local_host="node-a")
    server = Server(host=bind_host,port=0,store=store,sessions=sessions,local_host="node-a")
    composite = None
    notify = None
    try:
        for sid in (OLD,NEW,CHILD,ADVISOR):
            host,name = sid.split(":",1)
            tmux.live.add(name)
            await store.open_session(host,name,provider="codex",role="lead",visibility="default",
                pane_status="pane_alive",bootstrap_state="ready",effective_model="gpt-6.1-sol",effective_effort="medium",
                parent_stream_id=OLD if sid in (CHILD,ADVISOR) else None,
                spec_id=SPEC if sid == NEW else None,
                qualified_spec_ids=[SPEC] if sid == NEW else [],
                spec_binding_provenance=[{"spec_id":SPEC,"provenance":"spawn_explicit","granting_principal":"operator",
                                         "granted_at":"2026-01-01T00:00:00Z"}] if sid == NEW else [])
        old = await store.fetch_session("node-a","old")
        token = "fixture-successor-token"
        await store.grant_stream_token("node-b","new",hashlib.sha256(token.encode()).hexdigest(),STREAM_TOKEN_HASH_VERSION)
        config = AssistantCompositeConfig.from_env({"PENTACLE_ASSISTANT_COMPOSITE_ENABLED":"1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID":CHAT,"PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID":OLD,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION":old["session_generation"],
            "PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS":json.dumps([SPEC]),
            "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID":ADVISOR if authority else ""})
        config=replace(config,name="fixture",title="Fixture")
        async def dispatch(route):
            return {"delivery":"landed"}
        composite = AssistantComposite(store,config=config,dispatch=dispatch)
        await composite.load_binding();await composite.ensure_projection()
        server.assistant_composite = composite
        env = Env(store,sessions,server,tmux)
        ceremony = Ceremony(env,tmp_path)
        await ceremony.enroll()
        await store.lifecycle_authority_mutate({"action":"designate","target_stream_id":OLD,
            "target_generation":old["session_generation"],"expected_revision":0,"request_id":"initial-designation",
            "reason":"Fixture initial designation"},{**OPERATOR,"_consent_id":"fixture-initial"},sessions.assistant.role)
        await sessions.refresh()
        port = await server.bind()
        inputs = {"sessions_db":str(db),"binding_name":"fixture","predecessor":OLD,
                  "predecessor_generation":old["session_generation"],"authority_membership_table":OWNERSHIP_TABLE}
        if questions:
            notify=Notify(str(tmp_path/"notifications.db"),sessions=sessions)
            server.notify=notify;server.handlers.update(notify.wire_handlers())
            await notify.start()
            async def closed(sid,*,session_generation=None,reason=""):
                await notify.expire_questions_for_closed_producer(sid,generation=session_generation)
            sessions.set_awaiter_resolver(closed)
            inputs["notifications_db"]=str(tmp_path/"notifications.db")
        g = FixtureGateway(store,server,port,token,inputs,ceremony)
        manifest = {"operation_id":"fixture-recovery","predecessor":OLD,"predecessor_generation":old["session_generation"],
            "composite_stream":CHAT,"authorization":{k:"fixture-grant" for k in ("rebind","retire","routes","rollback")},
            "target":{"host":"node-b","provider":"codex","model":"gpt-6.1-sol","effort":"medium"},"routes":[]}
        yield store,server,composite,g,manifest,recovery.Recovery(manifest,g,tmp_path/"journal.json")
    finally:
        if composite:await composite.stop()
        await server.close()
        if notify:await notify.stop()
        store.stop()


def test_plan_is_read_only_and_does_not_create_journal(tmp_path):
    async def run():
        async with fixture(tmp_path) as (_,_,_,g,_,r):
            result = await r.plan()
            assert result["mutations"] == 0 and result["children"] == 2
            assert g.calls == ["assistant.binding","assistant.authority"] and not r.path.exists()
    asyncio.run(run())


@pytest.mark.parametrize("already_closed",[False,True])
def test_recovery_restores_custom_watch_and_cold_wakes_without_old_token(tmp_path,already_closed):
    async def run():
        async with fixture(tmp_path) as (store,_,composite,g,m,r):
            oldgen=m["predecessor_generation"];now=time.time()
            await store.register_watch_wake("watch",OLD,oldgen,{"request_id":"custom","child_stream_id":CHILD,
                "triggers":["quiet=5"],"repeat":False},now=now)
            await store.register_watch_wake("wake",OLD,oldgen,{"request_id":"future","due_at":now+3600,
                "note":"fixture future","urgent":True},now=now)
            cancelled=await store.register_watch_wake("wake",OLD,oldgen,{"request_id":"explicit-cancel","due_at":now+7200},now=now)
            await store.cancel_watch_wake("wake",OLD,oldgen,cancelled["id"],request_id="user-cancel")
            if already_closed:
                await store.update_session("node-a","old",status="closed",closed_at="2026-01-01T00:00:00Z",pane_status="pane_dead")
            pending=await r.advance()
            assert pending["state"] == "answering_pending_consent"
            assert (await composite.binding())["stream_id"] == NEW
            assert (await store.lifecycle_authority_current())["stream_id"] == OLD
            await g.approve(r.j)
            done=await recovery.Recovery(m,g,r.path).advance()
            assert done["state"] == "recovered_pending_mirror_verification"
            for host,name in (("node-b","child"),("node-a","advisor")):
                assert (await store.fetch_session(host,name))["parent_stream_id"] == NEW
            gen=(await store.fetch_session("node-b","new"))["session_generation"]
            watches=await store.list_watch_wake("watch",NEW,gen)
            wakes=await store.list_watch_wake("wake",NEW,gen)
            assert any("quiet=5" in w["triggers"] and not w["default"] for w in watches)
            assert len(wakes)==1 and wakes[0]["due_at"]==pytest.approx(now+3600,abs=0.000001,rel=0) and wakes[0]["urgent"] is True
            repeated=await recovery.Recovery(m,g,r.path).advance()
            assert repeated["lifecycle_ready"] and len(await store.list_watch_wake("wake",NEW,gen))==1
    asyncio.run(run())


def test_warm_rollback_before_consent_and_dead_rollback_refusal(tmp_path):
    async def run():
        async with fixture(tmp_path) as (store,_,composite,_,_,r):
            await r.advance()
            result=await r.rollback()
            assert result["state"]=="rolled_back_before_designation"
            assert (await composite.binding())["stream_id"]==OLD
            await store.update_session("node-a","old",status="closed",pane_status="pane_dead")
            with pytest.raises(recovery.Refused,match="roll_forward"):
                await r.rollback()
    asyncio.run(run())


def test_owner_only_inputs_and_route_field_conflict(tmp_path):
    path=tmp_path/"manifest.json";path.write_text("{}");path.chmod(0o644)
    with pytest.raises(recovery.Refused,match="owner_only"):
        recovery.private_read(path)
    path.chmod(0o600);assert recovery.private_read(path)=={}
    config=tmp_path/"route.json";config.write_text('{"destination":"foreign","unrelated":7}')
    with pytest.raises(recovery.Refused,match="route_field_conflict"):
        recovery.route_files({"routes":[{"id":"example","path":str(config),"format":"json",
            "fields":[{"keys":["destination"],"before":OLD,"after":NEW}]}]})
    assert json.loads(config.read_text())["unrelated"]==7


def test_preconsent_correlated_reply_and_single_canonical_final(tmp_path):
    async def run():
        async with fixture(tmp_path) as (store,_,composite,g,_,r):
            await r.advance()
            await composite.accept_input({"text":"fixture question","request_id":"input-rpc","optimistic_id":"input-one"},
                                         operator_principal="operator:fixture")
            route=None
            for _ in range(100):
                route=await store.get_assistant_composite_route(stream_id=CHAT,input_identity="input-one")
                if route and route["routing_state"]=="resolved":break
                await asyncio.sleep(0.01)
            assert route["route_target"]==NEW
            payload={"type":"assistant.publish","request_id":"publish:"+route["dispatch_id"],"composite_stream_id":CHAT,
                "dispatch_id":route["dispatch_id"],"reply_to_message_id":"input-one","publish_kind":"prose",
                "response_state":"final","message":"fixture answer","attachment_ids":[],"evidence_refs":[]}
            reply=await g.rpc(payload)
            assert reply["type"]=="assistant.publish.ok",reply
            assert (await g.rpc(payload))["duplicate"] is True
            events=await store.fetch_session_event_tail(CHAT,limit=20)
            assert sum(e["kind"]=="ASSIST_TEXT" and e["text"]=="fixture answer" for e in events)==1
            assert (await store.lifecycle_authority_current())["stream_id"]==OLD
    asyncio.run(run())


def test_fired_and_delivered_wakes_are_dispositioned_not_replayed(tmp_path):
    async def run():
        async with fixture(tmp_path) as (store,_,_,_,m,_):
            now=time.time();gen=m["predecessor_generation"]
            await store.register_watch_wake("wake",OLD,gen,{"request_id":"fired","due_at":now+1},now=now)
            await store.evaluate_watch_wake({},now=now+2)
            inputs={"sessions_db":str(tmp_path/"sessions.db"),"binding_name":"fixture","predecessor":OLD,
                    "predecessor_generation":gen,"authority_membership_table":OWNERSHIP_TABLE}
            plan=recovery.retained_plan(recovery.snapshot(inputs))
            assert plan[0]["disposition"]=="enqueued_delivery_uncertain"
            def submitted(conn):
                conn.execute("UPDATE v2_outbound_notices SET delivered_at='fixture-submitted' WHERE kind='wake'")
                conn.commit()
            await store.submit(submitted)
            plan=recovery.retained_plan(recovery.snapshot(inputs))
            assert plan[0]["disposition"]=="submitted_execution_uncertain"
    asyncio.run(run())


def test_busy_retirement_remains_incomplete_without_force(tmp_path):
    async def run():
        async with fixture(tmp_path,tmux=IdleTmux(working={"old"})) as (store,_,composite,g,m,r):
            await r.advance();await g.approve(r.j)
            with pytest.raises(recovery.Refused,match="retire"):
                await recovery.Recovery(m,g,r.path).advance()
            assert (await composite.binding())["stream_id"]==NEW
            assert (await store.fetch_session("node-a","old"))["status"]=="open"
    asyncio.run(run())


@pytest.mark.parametrize("decision",["approve","deny","unavailable"])
def test_enabled_distinct_fixed_authority_retirement_admission(tmp_path,decision):
    async def run():
        async with fixture(tmp_path,authority=True) as (store,server,_,g,m,r):
            await r.advance();await g.approve(r.j)
            gen=m["predecessor_generation"]
            newgen=(await store.fetch_session("node-b","new"))["session_generation"]
            def tracked(conn):
                conn.execute(f"INSERT INTO {OWNERSHIP_TABLE} VALUES (?,?,?,?,?,?)",
                    (OLD,gen,time.time(),NEW,newgen,"fixture-admission"))
                conn.commit()
            await store.submit(tracked)
            with pytest.raises(recovery.Refused,match="retire"):
                await r.advance()
            pending=r.j["pending"];assert pending["code"]=="close.pending_ruling"
            rows=await store.submit(lambda c:[dict(v) for v in c.execute("SELECT ruling_request_id,state FROM v2_assistant_lane_rulings WHERE action='session_close'")])
            assert len(rows)==1 and rows[0]["state"]=="pending"
            if decision=="unavailable":
                await store.update_session("node-a","advisor",pane_status="pane_dead")
                assert (await server.lane_rulings.binding())["state"]=="unavailable"
            else:
                advisor=await store.fetch_session("node-a","advisor")
                reply=await server._on_assistant_ruling({"type":"assistant.ruling","request_id":"fixture-decision",
                    "ruling_request_id":rows[0]["ruling_request_id"],"ruling":decision,"reason":"Fixture authority decision",
                    "_auth_context":{"token_verified":True,"stream_id":ADVISOR,"session_generation":advisor["session_generation"]}})
                assert reply["state"]==("done" if decision=="approve" else "denied")
            assert (await store.fetch_session("node-a","old"))["status"]==("closed" if decision=="approve" else "open")
    asyncio.run(run())


def test_question_expiry_recreates_obligation_and_preserves_options(tmp_path):
    async def run():
        async with fixture(tmp_path,questions=True) as (store,server,_,g,m,r):
            request=_ask("q-original",producer=OLD,body="Prepare the fixture.\n\nDone continues; Not yet keeps it pending.",
                options=[{"label":"Done","value":"done"},{"label":"Not yet","value":"notyet"}])
            asked=await server.notify.prompt(request)
            assert asked["type"]=="prompt.ask.ok",asked
            await r.advance();await g.approve(r.j)
            done=await r.advance()
            assert done["lifecycle_ready"]
            assert r.j["question_dispositions"]["question:q-original"]=="recreated"
            state=recovery.snapshot(g.inputs)
            assert state["questions"][0]["state"]=="expired"
            assert state["questions"][0]["resolution"]["reason"]=="asker_closed"
            successor_questions=await server.notify._db.call("list_agent_questions",producer_stream_id=NEW)
            assert len(successor_questions)==1
            assert [o["label"] for o in successor_questions[0]["envelope"]["options"]]==["Done","Not yet"]
    asyncio.run(run())


def test_start_uses_one_stable_authenticated_top_level_intent(tmp_path,monkeypatch):
    calls=[]
    response={"state":"ready","admitted_count":1,"stream_id":NEW,
            "session":{"parent_stream_id":None,"visibility":"default","session_generation":"fixture-generation",
                       "effective_model":"gpt-6.1-sol","effective_effort":"medium"}}
    async def launched(payload):
        calls.append(payload)
        assert "parent_stream_id" not in payload and "handoff" not in payload
        assert payload["visibility"]=="default" and payload["role"]=="lead"
        return response
    async def run():
        async with fixture(tmp_path) as (_,_,_,g,m,r):
            m["authorization"]["spawn"]="fixture-commission"
            m["spawn_spec_ids"]=[SPEC]
            m["target"].update(python="python3",script="/private/recovery.py",manifest="/private/manifest.json",
                               journal="/private/journal.json")
            r=recovery.Recovery(m,g,tmp_path/"start.json")
            g.spawn=launched
            first=await r.start()
            second=await recovery.Recovery(m,g,r.path).start()
            assert first==second and len(calls)==1
            assert calls[0]["request_id"]==calls[0]["idempotency_key"]
    asyncio.run(run())


def test_pending_child_spawn_fences_retirement(tmp_path):
    async def run():
        async with fixture(tmp_path) as (store,_,composite,g,m,r):
            await r.advance();await g.approve(r.j)
            assert await store.reserve_stream_id("node-b","pending",ttl_s=60,request_id="pending-fixture")
            assert await store.record_spawn_intent("node-b","pending",{"open_fields":{"parent_stream_id":OLD}},request_id="pending-fixture")
            with pytest.raises(recovery.Refused,match="retire"):
                await r.advance()
            assert r.j["pending"]["code"]=="close_pending_spawn"
            assert (await store.fetch_session("node-a","old"))["status"]=="open"
            assert (await composite.binding())["stream_id"]==NEW
            assert (await r.plan())["pending_spawns"][0]["request_id"]=="pending-fixture"
    asyncio.run(run())


def test_top_level_spawn_retains_executor_wire_authentication(tmp_path,monkeypatch):
    async def run():
        async with fixture(tmp_path) as (store,server,_,g,m,_):
            captured=[]
            class Spawner:
                async def spawn(self,payload,host):
                    captured.append(payload)
                    return {"type":"spawn.ok","state":"ready","admitted_count":1,"stream_id":"node-b:created",
                        "session":{"parent_stream_id":None,"visibility":"default","session_generation":"created-generation",
                                   "effective_model":"gpt-6.1-sol","effective_effort":"medium"}}
            server.spawnctl=Spawner()
            m.update(endpoint=f"ws://{server.host}:{g.port}",spawn_spec_ids=[SPEC])
            m["authorization"]["spawn"]="fixture-commission"
            m["target"].update(python="python3",script="/private/recovery.py",manifest="/private/manifest.json",journal="/private/journal.json")
            monkeypatch.setenv("AGENT_ORCH_WS_URL",m["endpoint"])
            monkeypatch.setenv("AGENT_ORCH_STREAM_ID",NEW)
            monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN",g.token)
            monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE",raising=False)
            monkeypatch.setenv("AGENT_ORCH_TOKEN","")
            real=recovery.Gateway(m)
            result=await recovery.Recovery(m,real,tmp_path/"authenticated-start.json").start()
            assert result["state"]=="ready" and len(captured)==1
            assert captured[0]["_auth_context"]["token_verified"] is True
            assert captured[0]["_auth_context"]["stream_id"]==NEW
            assert "parent_stream_id" not in captured[0]
    asyncio.run(run())


def test_operation_receipts_omit_credentials_and_device_records():
    projected=recovery.receipt_projection({"type":"consent.request.ok","stream_token":"private-placeholder",
        "intent":{"request_id":"fixture-intent","state":"pending","target_stream_id":NEW,
                  "audience_key_ids":["private-key-placeholder"]},
        "session":{"stream_id":NEW,"device_id":"private-device-placeholder","token_hash":"private-placeholder"}})
    encoded=json.dumps(projected)
    assert "private-" not in encoded
    assert projected["intent"]["request_id"]=="fixture-intent"
