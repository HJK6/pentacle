"""Pure, synthetic turn-budget observer contract; no provider or live data."""
from __future__ import annotations

import json

import pytest

from fd_turn_budget import FdTurnBudgetConfig, decode_state, initial_state, observe, turn_key


def event(kind, n, *, seconds=None, tool_id=None, **raw):
    return {"kind": kind, "stream_id": "fixture:fd",
            "timestamp": f"2026-10-10T00:{(seconds or n)//60:02d}:{(seconds or n)%60:02d}Z",
            "raw": {**raw, **({"tool_use_id": tool_id or f"tool-{n}"} if kind == "TOOL_USE" else {})}}


def fold(events, *, config=None, generation="gen-1", state=None):
    state = state or initial_state("fixture:fd", generation, "created-1")
    found = []
    for cursor, item in enumerate(events, state["cursor"] + 1):
        state, crossing = observe(item, state, config=config or FdTurnBudgetConfig(),
            identity=f"event-{cursor}", cursor=cursor,
            boundary=f"dispatch-{cursor}" if item["kind"] == "USER" else None)
        if crossing:
            found.append(crossing)
    return state, found


def test_defaults_are_record_only_and_thresholds_strict():
    assert FdTurnBudgetConfig() == FdTurnBudgetConfig(calls=5, seconds=60, enabled=False)
    state, found = fold([event("USER", 0), *[event("TOOL_USE", i, seconds=i*6) for i in range(1, 6)]])
    assert found == [] and len(state["tool_ids"]) == 5
    assert fold([event("USER", 0), event("ASSIST_TEXT", 1), event("TOOL_RESULT", 2, seconds=61)])[1] == []


@pytest.mark.parametrize("enabled", [False, True])
def test_six_calls_first_crossing_only_and_immutable(enabled):
    state, found = fold([event("USER", 0), *[event("TOOL_USE", i) for i in range(1, 17)]],
                        config=FdTurnBudgetConfig(enabled=enabled))
    assert len(found) == 1
    crossing = found[0]
    assert crossing["turn_id"] == ["fixture:fd", "gen-1", 1]
    assert (crossing["calls"], crossing["seconds"], crossing["dimension"]) == (6, 5, "calls")
    assert crossing["record_only"] is (not enabled)
    assert len(state["tool_ids"]) == 6


def test_seconds_threshold_and_tie_dimension():
    items = [event("USER", 0), *[event("TOOL_USE", i) for i in range(1, 5)], event("ASSIST_TEXT", 5, seconds=62)]
    assert fold(items)[1][0]["dimension"] == "seconds"
    items = [event("USER", 0), *[event("TOOL_USE", i) for i in range(1, 6)], event("TOOL_USE", 6, seconds=62)]
    assert fold(items)[1][0]["dimension"] == "calls"


def test_duplicate_tool_id_is_not_nested_or_repeated_command():
    items = [event("USER", 0), *[event("TOOL_USE", i, tool_id="one", tool_input={"command":"echo a; echo b"}) for i in range(1, 10)]]
    state, found = fold(items)
    assert not found and state["tool_ids"] == ["one"]


def test_result_and_thinking_never_start_own_span():
    state, found = fold([event("USER", 0), event("TOOL_RESULT", 1), event("THINKING", 2, seconds=200)])
    assert not found and state["first"] is None and state["latest"] is None


def test_two_dispatches_reset_count_and_no_budget_notice_self_trigger():
    state, found = fold([event("USER", 0), *[event("TOOL_USE", i) for i in range(1, 7)], event("USER", 7)])
    assert len(found) == 1 and state["ordinal"] == 2
    assert state["tool_ids"] == [] and state["first"] is None
    state, found = fold([event("TOOL_USE", i) for i in range(8,14)], state=state)
    assert found[0]["turn_id"][-1] == 2


def test_equal_timestamp_and_same_cursor_replay_stable():
    state, found = fold([event("USER", 0), *[event("TOOL_USE", i, seconds=1) for i in range(1, 7)]])
    assert found[0]["seconds"] == 0
    replay, crossing = observe(event("USER", 0), state, config=FdTurnBudgetConfig(), identity="event-1", cursor=1, boundary="dispatch-1")
    assert replay == state and crossing is None


def test_actual_generation_and_stream_are_parts_of_key():
    assert len({turn_key("a:fd", "same", 1), turn_key("b:fd", "same", 1), turn_key("a:fd", "new", 1)}) == 3
    old, _ = fold([event("USER", 0), event("TOOL_USE", 1)])
    new = initial_state("fixture:fd", "gen-2", "created-2")
    assert new["ordinal"] == 0 and not new["tool_ids"] and old["generation"] != new["generation"]


def test_missing_tool_id_reports_gap_and_next_proved_turn_recovers():
    broken = event("TOOL_USE", 1)
    broken["raw"].pop("tool_use_id")
    state, found = fold([event("USER", 0), broken, *[event("TOOL_USE", i) for i in range(2,20)]])
    assert not found and state["gap"] == "tool_use_id_invalid" and len(state["tool_ids"]) <= 6
    state, found = fold([event("USER", 20), *[event("TOOL_USE", i) for i in range(21,27)]], state=state)
    assert len(found) == 1 and state["gap"] is None


def test_unproved_boundary_poison_does_not_guess_later_ordinal():
    state = initial_state("fixture:fd", "gen-1", "created-1")
    state, crossing = observe(event("TELL", 1), state, config=FdTurnBudgetConfig(), identity="x", cursor=1, provenance_gap="peer_provenance_unavailable")
    state, found = fold([event("USER", 2), *[event("TOOL_USE", i) for i in range(3,10)]], state=state)
    assert crossing is None and not found and state["ordinal"] == 0 and state["continuity_gap"]


def test_sidechain_and_system_excluded_and_bare_tell_does_not_open_turn():
    state, found = fold([event("SYSTEM", 0), event("TELL", 1), event("TOOL_USE", 2, is_sidechain=True)])
    assert not found and state["ordinal"] == 0


@pytest.mark.parametrize("value", [None, "bad", "2026-10-10T01:00:00"])
def test_invalid_timestamp_is_explicit_gap(value):
    e = event("ASSIST_TEXT", 1); e["timestamp"] = value
    state, found = fold([event("USER", 0), e])
    assert not found and state["gap"] == "work_timestamp_invalid"


def test_decode_rejects_corrupt_lost_cursor_and_round_trips():
    state, _ = fold([event("USER", 0), event("TOOL_USE", 1)])
    assert decode_state(json.dumps(state), "fixture:fd", "gen-1", "created-1") == state
    for field, value in (("ordinal", -1), ("cursor_identity", None), ("tool_ids", ["x", "x"]), ("generation", "other")):
        with pytest.raises(ValueError):
            decode_state(json.dumps({**state, field:value}), "fixture:fd", "gen-1", "created-1")


@pytest.mark.parametrize("kwargs", [{"calls":-1},{"calls":True},{"seconds":float("nan")},{"enabled":"true"}])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        FdTurnBudgetConfig(**kwargs)


# Store integration: authoritative generation, atomic source/cursor/effects, and
# the existing durable notice queue. All stores are disposable and owned here.
import asyncio
import hashlib
from pathlib import Path
import sqlite3

import pytest_asyncio

from claude_jsonl_norm import normalize_claude_jsonl_record
from codex_rollout_norm import normalize_codex_rollout_record
from ingest import _identity_key, append_ingested_event
from outbound_notices import OutboundNoticeQueue
from store import Store
from assistant_composite import AssistantCompositeConfig


@pytest_asyncio.fixture
async def budget_store(tmp_path):
    store = Store(str(tmp_path / "owned.db")); store.start()
    row = await store.open_session("fixture", "fd", provider="claude", pane_status="pane_alive", pane_pid="123")
    binding = {"stream_id":"fixture:fd", "generation":row["session_generation"]}
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
    yield store, binding
    store.stop()
    assert store._thread is None


def normalized(kind, n, *, provider="claude", body="Fixture dispatch", session="fd", **extra):
    stamp = f"2026-10-10T00:{n//60:02d}:{n%60:02d}Z"
    if provider == "claude":
        record = {"type":"user" if kind == "USER" else "assistant", "uuid":f"record-{n}", "timestamp":stamp,
                  "message":{"content":body if kind == "USER" else [{"type":"tool_use","id":f"tool-{n}","name":"fixture","input":{}}]}, **extra}
        return normalize_claude_jsonl_record(record, host="fixture", session_name=session)[0]
    payload = ({"type":"message","id":f"record-{n}","role":"user","content":[{"type":"input_text","text":body}]}
               if kind == "USER" else {"type":"function_call","id":f"record-{n}","call_id":f"tool-{n}","name":"fixture","arguments":"{}"})
    return normalize_codex_rollout_record({"type":"response_item","timestamp":stamp,"payload":{**payload,**extra}}, host="fixture", session_name=session)[0]


async def append(store, item):
    async def broadcast(_): pass
    return await append_ingested_event(store, broadcast, item, recent_limit=100)


async def turn(store, start=0, *, provider="claude", session="fd"):
    await append(store, normalized("USER", start, provider=provider, session=session))
    for n in range(start+1,start+7):
        await append(store, normalized("TOOL_USE", n, provider=provider, session=session))


async def counts(store):
    return await store.submit(lambda c: (
        c.execute("SELECT count(*) FROM session_event_tail WHERE json_extract(event_json,'$.kind')='fd_turn_budget'").fetchone()[0],
        c.execute("SELECT count(*) FROM v2_outbound_notices WHERE kind='fd_turn_budget'").fetchone()[0]))


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude","codex"])
async def test_real_normalizers_store_replay_and_disabled_enable(budget_store, provider):
    store,binding = budget_store
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=False), env_binding=binding)
    await turn(store,provider=provider)
    assert await counts(store) == (1,0)
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
    await turn(store,provider=provider)
    for n in range(7,17): await append(store, normalized("TOOL_USE", n, provider=provider))
    assert await counts(store) == (1,0)
    await turn(store,20,provider=provider)
    assert await counts(store) == (2,1)


@pytest.mark.asyncio
async def test_store_restart_preserves_current_turn_and_ordinal(budget_store):
    store,binding = budget_store
    await turn(store)
    path=store.path; store.stop()
    reopened=Store(path); reopened.start()
    try:
        reopened.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
        await turn(reopened)
        assert await counts(reopened) == (1,1)
        await turn(reopened,20)
        state = await reopened.fd_turn_budget_status("fixture:fd")
        assert state["ordinal"] == 2 and not state["continuity_gap"]
        assert await counts(reopened) == (2,2)
    finally: reopened.stop()


@pytest.mark.asyncio
async def test_actual_new_generation_counts_same_source_again(budget_store):
    store,binding = budget_store
    await turn(store)
    row = await store.open_session("fixture","fd",provider="claude",pane_status="pane_alive")
    assert row["session_generation"] != binding["generation"]
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding={"stream_id":"fixture:fd","generation":row["session_generation"]})
    await turn(store)
    assert await counts(store) == (2,2)
    state = await store.fd_turn_budget_status("fixture:fd")
    assert state["ordinal"] == 1 and state["generation"] == row["session_generation"]


@pytest.mark.asyncio
async def test_unbound_stream_and_same_generation_binding_return(budget_store):
    store,binding = budget_store
    await store.open_session("fixture","other",provider="claude")
    await turn(store,session="other")
    assert await counts(store) == (0,0)
    await turn(store)
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),env_binding={})
    await append(store,normalized("TOOL_USE",7,session="other"))
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),env_binding=binding)
    await turn(store)
    await turn(store,20)
    assert await counts(store) == (2,2)


@pytest.mark.asyncio
async def test_missing_legacy_cursor_and_lost_cursor_are_gaps(budget_store):
    store,binding=budget_store
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(),env_binding={})
    await turn(store)
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),env_binding=binding)
    await turn(store,20)
    assert await counts(store) == (0,0)
    assert (await store.fd_turn_budget_status("fixture:fd"))["continuity_gap"] == "ordinal_history_unproven"
    await store.put(store._fd_cursor_key("fixture:fd"), "broken")
    await turn(store,40)
    assert "cursor" in (await store.fd_turn_budget_status("fixture:fd"))["continuity_gap"]
    assert await counts(store) == (0,0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["before_observe","before_notice","after_observe"])
async def test_atomic_source_cursor_event_notice_rollback(budget_store, monkeypatch, failure):
    store,_=budget_store
    await append(store,normalized("USER",0))
    for n in range(1,6): await append(store,normalized("TOOL_USE",n))
    before=await store.fd_turn_budget_status("fixture:fd")
    import store_routing
    original=store._fd_observe_conn
    if failure == "before_notice":
        def fail(*a,**kw): raise RuntimeError("synthetic failure")
        monkeypatch.setattr(store_routing,"_insert_outbound_notice_conn",fail)
    else:
        def fail(*a,**kw):
            if failure == "after_observe": original(*a,**kw)
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr(store,"_fd_observe_conn",fail)
    with pytest.raises(RuntimeError,match="synthetic failure"):
        await append(store,normalized("TOOL_USE",6))
    assert await counts(store)==(0,0)
    assert await store.fd_turn_budget_status("fixture:fd") == before
    assert await store.submit(lambda c:c.execute("SELECT count(*) FROM session_event_tail").fetchone()[0])==6
    monkeypatch.undo()
    await append(store,normalized("TOOL_USE",6))
    assert await counts(store)==(1,1)


@pytest.mark.asyncio
async def test_notice_boundary_no_self_trigger_and_delivery_guard(budget_store):
    store,binding=budget_store
    await turn(store)
    notice=await store.submit(lambda c:dict(c.execute("SELECT * FROM v2_outbound_notices WHERE kind='fd_turn_budget'").fetchone()))
    assert await store.fd_turn_budget_notice_valid(notice)
    await append(store,normalized("USER",7,body=notice["body"]))
    assert await counts(store)==(1,1)
    state=await store.fd_turn_budget_status("fixture:fd")
    assert state["ordinal"]==2 and state["tool_ids"]==[] and state["first"] is None
    await append(store,normalized("USER",8,body=notice["body"]))
    assert (await store.fd_turn_budget_status("fixture:fd"))["ordinal"]==2
    class NeverSend:
        async def deliver_outbound_notice(self,*a,**kw): raise AssertionError("stale notice sent")
    queue=OutboundNoticeQueue(store,NeverSend())
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=False),env_binding=binding)
    assert not await queue.deliver_now(notice["notice_id"])
    terminal=await store.submit(lambda c:c.execute("SELECT terminal_reason FROM v2_outbound_notices WHERE notice_id=?",(notice["notice_id"],)).fetchone()[0])
    assert terminal=="fd_turn_budget_stale_or_disabled"


@pytest.mark.asyncio
async def test_peer_receipt_late_then_work_recovers_without_guess(budget_store):
    store,binding=budget_store
    from comms import Comms,payload_digest
    msg={"from_stream_id":"fixture:peer","_auth_context":{"token_verified":True,"stream_id":"fixture:peer"}}
    wire=Comms._peer_delivery_wire(msg,"tell","peer-1","Fixture peer dispatch")
    await append(store,normalized("USER",0,body=wire))
    state=await store.fd_turn_budget_status("fixture:fd")
    assert state["continuity_gap"]=="peer_provenance_pending" and state["ordinal"]==0
    await store.put_tell_delivery("peer-1",{"payload_digest":payload_digest("fixture:fd","Fixture peer dispatch"),
        "reply":{"tell_id":"peer-1","to_stream_id":"fixture:fd","delivery_status":"delivered"},
        "delivery":{"tell_id":"peer-1","to_stream_id":"fixture:fd","text":wire,"delivery_status":"delivered"}})
    for n in range(1,7): await append(store,normalized("TOOL_USE",n))
    state=await store.fd_turn_budget_status("fixture:fd")
    assert state["ordinal"]==1 and not state["continuity_gap"]
    assert await counts(store)==(1,1)


@pytest.mark.asyncio
async def test_native_codex_direction_is_proven_without_tell_id(budget_store):
    store,_=budget_store
    incoming=normalized("USER",0,provider="codex",type="agent_message",recipient="/root",author="/root/child",text="Fixture inbound")
    assert incoming["kind"]=="TELL" and not incoming["raw"].get("tell_id")
    await append(store,incoming)
    for n in range(1,7): await append(store,normalized("TOOL_USE",n,provider="codex"))
    assert await counts(store)==(1,1)


@pytest.mark.asyncio
async def test_archived_replay_and_missing_archive_report_gap(budget_store):
    store,binding=budget_store
    await turn(store)
    archive=Path(store.path).with_name("sessions_archive.db")
    from store import SESSION_EVENT_TAIL_DDL
    def move(conn):
        conn.execute("ATTACH DATABASE ? AS fixture_archive",(str(archive),))
        conn.execute(SESSION_EVENT_TAIL_DDL.replace("session_event_tail","fixture_archive.session_event_tail",1))
        conn.execute("INSERT INTO fixture_archive.session_event_tail SELECT * FROM session_event_tail")
        conn.execute("DELETE FROM session_event_tail");conn.commit();conn.execute("DETACH DATABASE fixture_archive")
    await store.submit(move)
    await turn(store)
    assert await counts(store)==(0,1)
    state=await store.fd_turn_budget_status("fixture:fd")
    assert state["ordinal"]==1 and not state["continuity_gap"] and state["archive_seen"]
    archive.unlink()
    from retention import RetentionError
    # Ingress now refuses missing paired history before the observer runs.
    with pytest.raises(RetentionError, match="history archive path unavailable"):
        await turn(store,20)
    # Exercise the accepted-event floor independently; it must retain the gap.
    item = normalized("USER", 20)
    await store.append_session_event("fixture:fd", item, identity="missing-archive-source", limit=100)
    assert (await store.fd_turn_budget_status("fixture:fd"))["continuity_gap"]=="archive_unavailable"
    assert await counts(store)==(0,1)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider",["claude","codex"])
@pytest.mark.parametrize("wake",["peer","notice","digest","timed"])
async def test_real_wake_shapes_use_existing_durable_provenance(budget_store,provider,wake):
    store,binding=budget_store
    from comms import Comms
    from message_envelopes import build_message_envelope,build_external_work_due_body
    anchor="d2:"+"9"*64
    if wake=="peer":
        body="Proved peer dispatch"
        msg={"from_stream_id":"fixture:peer","_auth_context":{"token_verified":True,"stream_id":"fixture:peer"}}
        wire=Comms._peer_delivery_wire(msg,"send",anchor,body)
        await store.append_send_receipt(to_stream_id="fixture:fd",request_id=anchor,receipt_id="receipt-1",state="landed",
            wire_text=wire,display_text=body,attachments=[],delivery="landed",submission_confirmed=True,
            from_stream_id="fixture:peer",actor_stream_id="fixture:peer",actor_trusted=True)
    else:
        if wake=="digest":
            body=build_message_envelope("lane_digest",notice_id=anchor,lanes=[{"stream_id":"fixture:peer","text":"Fixture"}],evaluated_at="2026-10-10T00:00:00Z")
        elif wake=="timed":
            body=build_external_work_due_body("fixture-timed",label="Fixture",queue_ref="fixture",episode=1,reasons=["due"],last_verified_age_s=61,blocked_age_s=None)
        else:
            body="Fixture durable notice"
        queue=OutboundNoticeQueue(store)
        notice=await queue.enqueue(kind="lane_digest" if wake=="digest" else "external_work_due" if wake=="timed" else "report",
            dedupe_key=anchor,recipient_stream_id="fixture:fd",body=body,tell_id=anchor,
            metadata={"recipient_generation":binding["generation"]})
        wire=Comms._peer_delivery_wire({"_durable_notice_proof":True},"send",anchor,notice["body"])
    await append(store,normalized("USER",0,provider=provider,body=wire))
    for n in range(1,7): await append(store,normalized("TOOL_USE",n,provider=provider))
    assert await counts(store)==(1,1)
    assert (await store.fd_turn_budget_status("fixture:fd"))["ordinal"]==1


@pytest.mark.asyncio
async def test_historical_claude_queued_peer_identity_consumed_once(budget_store):
    store,_=budget_store
    from comms import Comms
    msg={"from_stream_id":"fixture:peer","_auth_context":{"token_verified":True,"stream_id":"fixture:peer"}}
    wire=Comms._peer_delivery_wire(msg,"send","peer-queued","Queued peer")
    await store.append_send_receipt(to_stream_id="fixture:fd",request_id="peer-queued",receipt_id="receipt-q",state="landed",
        wire_text=wire,display_text="Queued peer",attachments=[],delivery="landed",submission_confirmed=True,
        from_stream_id="fixture:peer",actor_stream_id="fixture:peer",actor_trusted=True)
    for n in (0,1):
        item=normalize_claude_jsonl_record({"type":"attachment","uuid":f"queue-{n}","timestamp":"2026-10-10T00:00:00Z",
            "attachment":{"type":"queued_command","origin":{"kind":"human"},"prompt":wire}},host="fixture",session_name="fd")[0]
        assert item["kind"]=="USER"
        await append(store,item)
    state=await store.fd_turn_budget_status("fixture:fd")
    assert state["ordinal"]==1 and not state["continuity_gap"]


@pytest.mark.asyncio
async def test_stale_lifecycle_batch_has_no_observer_effects(budget_store):
    store,binding=budget_store
    row=await store.fetch_session("fixture","fd")
    e=normalized("USER",0)
    result=await store.append_session_events_lifecycle_cas([{"stream_id":"fixture:fd","event":e,"identity":_identity_key(e),
        "lifecycle":{"pane_pid":"123","session_created_at":row["created_at"],"generation":"wrong"}}],limit=100)
    from store import ENTRY_DROPPED
    assert result==[ENTRY_DROPPED]
    assert await store.fd_turn_budget_status("fixture:fd") is None
    entries=[]
    for n in range(7):
        e=normalized("USER" if n==0 else "TOOL_USE",n)
        entries.append({"stream_id":"fixture:fd","event":e,"identity":_identity_key(e),
            "lifecycle":{"pane_pid":"123","session_created_at":row["created_at"],"generation":binding["generation"]}})
    result=await store.append_session_events_lifecycle_cas(entries,limit=100)
    assert len(result)==7 and all(isinstance(n,int) for n in result)
    assert await counts(store)==(1,1)


@pytest.mark.asyncio
async def test_binding_changes_across_prepare_await_cannot_enqueue(budget_store,monkeypatch):
    store,binding=budget_store
    await append(store,normalized("USER",0))
    for n in range(1,6):await append(store,normalized("TOOL_USE",n))
    original=store._fd_prepare
    async def change(entries):
        result=await original(entries)
        store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),env_binding={"stream_id":"fixture:elsewhere","generation":"other"})
        return result
    monkeypatch.setattr(store,"_fd_prepare",change)
    await append(store,normalized("TOOL_USE",6))
    assert await counts(store)==(0,0)


@pytest.mark.asyncio
async def test_delivery_binding_revision_change_suppresses_same_seat(budget_store):
    store,binding=budget_store
    await turn(store)
    notice=await store.submit(lambda c:dict(c.execute("SELECT * FROM v2_outbound_notices WHERE kind='fd_turn_budget'").fetchone()))
    def change(conn):
        conn.execute("INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES (?,?,?,1,'2026-10-10T00:00:00Z')",(AssistantCompositeConfig().name,binding["stream_id"],binding["generation"]))
        conn.commit()
    await store.submit(change)
    assert not await store.fd_turn_budget_notice_valid(notice)


@pytest.mark.asyncio
async def test_missing_peer_receipt_stays_gap_without_fabricated_ordinal(budget_store):
    store,_=budget_store
    await append(store,normalized("USER",0,body="[from fixture:fake] [tell:invented]\nFake peer"))
    for n in range(1,7):await append(store,normalized("TOOL_USE",n))
    assert await counts(store)==(0,0)
    state=await store.fd_turn_budget_status("fixture:fd")
    assert state["ordinal"]==0 and state["continuity_gap"]=="peer_provenance_pending"


def test_regressed_tools_remain_bounded():
    state, found = fold([event('USER', 0), event('ASSIST_TEXT', 1, seconds=100),
        *[event('TOOL_USE', n, seconds=20) for n in range(2, 102)]])
    assert len(state['tool_ids']) == 6 and state['gap'] == 'work_timestamp_regressed' and not found


async def peer_receipt(store, *, anchor='peer-1', body='Peer one'):
    from comms import Comms, payload_digest
    wire = Comms._peer_delivery_wire({'from_stream_id':'fixture:peer',
        '_auth_context':{'token_verified':True,'stream_id':'fixture:peer'}}, 'tell', anchor, body)
    await store.put_tell_delivery(anchor, {'payload_digest':payload_digest('fixture:fd', body),
        'reply':{'tell_id':anchor,'to_stream_id':'fixture:fd','delivery_status':'delivered'},
        'delivery':{'tell_id':anchor,'to_stream_id':'fixture:fd','text':wire,'delivery_status':'delivered'}})
    return wire


def peer_wire():
    from comms import Comms
    return Comms._peer_delivery_wire({'from_stream_id':'fixture:peer',
        '_auth_context':{'token_verified':True,'stream_id':'fixture:peer'}}, 'tell', 'peer-1', 'Peer one')


@pytest.mark.asyncio
@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('corruption', ['missing', 'list', 'binding', 'notice_list', 'notice_nested', 'tell_list', 'tell_nested'])
async def test_malformed_evidence_preserves_both_source_floors(budget_store, batch, corruption):
    store, binding = budget_store
    await append(store, normalized('USER', 0))
    item = normalized('TOOL_USE', 1)
    if corruption in {'missing', 'list'}:
        state = await store.fd_turn_budget_status('fixture:fd')
        state.pop('continuity_gap')
        await store.put(store._fd_cursor_key('fixture:fd'), json.dumps(state if corruption == 'missing' else [1]))
    elif corruption == 'binding':
        await store.submit(lambda c: (c.execute("INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES (?,'fixture:fd','',1,'2026-10-10T00:00:00Z')",(AssistantCompositeConfig().name,)), c.commit()))
    elif corruption.startswith('notice'):
        notice = await OutboundNoticeQueue(store).enqueue(kind='report', dedupe_key='bad', recipient_stream_id='fixture:fd', body='Fixture notice', tell_id='bad')
        meta = [] if corruption == 'notice_list' else {'fd_turn_budget_binding': []}
        await store.submit(lambda c: (c.execute('UPDATE v2_outbound_notices SET metadata=?', (json.dumps(meta),)), c.commit()))
        item = normalized('USER', 1, body=notice['body'])
    else:
        payload = [] if corruption == 'tell_list' else {'delivery': []}
        await store.submit(lambda c: (c.execute('INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES (?,?,0)', ('peer-1',json.dumps(payload))), c.commit()))
        item = normalized('USER', 1, body=peer_wire())
    before = await store.submit(lambda c: c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0])
    if batch:
        await store.append_session_events_lifecycle_cas([{'stream_id':'fixture:fd','event':item,'identity':_identity_key(item)}], limit=100)
    else:
        await append(store, item)
    after = await store.submit(lambda c: c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0])
    assert after == before + 1
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap']
    assert (await counts(store))[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('pending', [False, True])
async def test_nonadjacent_dispatch_dedup_in_batch_and_recovery(budget_store, pending):
    store, _ = budget_store
    wire = peer_wire() if pending else await peer_receipt(store)
    items = [normalized('USER',0,body=wire), normalized('USER',1), normalized('USER',2,body=wire)]
    await store.append_session_events_lifecycle_cas([{'stream_id':'fixture:fd','event':e,'identity':_identity_key(e)} for e in items], limit=100)
    if pending:
        await peer_receipt(store)
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['ordinal'] == 2 and not state['continuity_gap']


@pytest.mark.asyncio
async def test_sidechain_copy_does_not_consume_dispatch(budget_store):
    store, _ = budget_store
    wire = await peer_receipt(store)
    await append(store, normalized('USER',0,body=wire,isSidechain=True))
    await append(store, normalized('USER',1,body=wire))
    for n in range(2,8):
        await append(store, normalized('TOOL_USE',n))
    assert await counts(store) == (1,1)
    assert (await store.fd_turn_budget_status('fixture:fd'))['ordinal'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['prefix', 'interior', 'archive'])
async def test_pending_history_loss_never_recovers_healthy(budget_store, damage):
    from store import SESSION_EVENT_TAIL_DDL
    store, _ = budget_store
    archive = Path(store.path).with_name('sessions_archive.db')
    with sqlite3.connect(archive) as conn:
        conn.execute(SESSION_EVENT_TAIL_DDL)
    await append(store, normalized('USER',0,body=peer_wire()))
    await append(store, normalized('USER',1))
    await append(store, normalized('TOOL_USE',2))
    if damage == 'archive':
        archive.unlink()
    else:
        ordinal = 0 if damage == 'prefix' else 1
        await store.submit(lambda c: (c.execute('DELETE FROM session_event_tail WHERE identity=?',(_identity_key(normalized('USER',ordinal,body=peer_wire() if ordinal == 0 else 'Fixture dispatch')),)), c.commit()))
    await peer_receipt(store)
    for n in range(3,9):
        await append(store, normalized('TOOL_USE',n))
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap']
    assert await counts(store) == (0,0)


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,completed', [(True,False),(False,False),(True,True)])
async def test_receipt_recovery_notifies_only_enabled_current_turn(budget_store, enabled, completed):
    store, binding = budget_store
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=enabled), env_binding=binding)
    await append(store, normalized('USER',0,body=peer_wire()))
    for n in range(1,7):
        await append(store, normalized('TOOL_USE',n))
    if completed:
        await append(store, normalized('USER',7))
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding=binding)
    await peer_receipt(store)  # No extra provider work is needed to reconcile proof.
    assert await counts(store) == (1,int(enabled and not completed))
    await peer_receipt(store)
    await append(store, normalized('TOOL_USE',8))
    assert await counts(store) == (1,int(enabled and not completed))


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_binding", [False, True])
@pytest.mark.parametrize("archived", [False, True])
async def test_late_binding_new_generation_does_not_guess_origin(budget_store, retained_binding, archived):
    store, _ = budget_store
    await append(store, normalized('USER',0))
    if not retained_binding:
        store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding={})
    row = await store.open_session('fixture','fd',provider='claude',pane_status='pane_alive',pane_pid='124')
    await append(store, normalized('USER',10))
    await append(store, normalized('TOOL_USE',11))
    if archived:
        await archive_sources(store)
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True), env_binding={'stream_id':'fixture:fd','generation':row['session_generation']})
    await turn(store,20)
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap'] == 'ordinal_history_unproven'
    assert await counts(store) == (0,0)


async def archive_sources(store):
    from store import SESSION_EVENT_TAIL_DDL
    archive = Path(store.path).with_name('sessions_archive.db')
    def move(conn):
        conn.execute('ATTACH DATABASE ? AS owned_archive',(str(archive),))
        conn.execute(SESSION_EVENT_TAIL_DDL.replace('session_event_tail','owned_archive.session_event_tail',1))
        conn.execute('INSERT INTO owned_archive.session_event_tail SELECT * FROM session_event_tail')
        conn.execute('DELETE FROM session_event_tail'); conn.commit(); conn.execute('DETACH DATABASE owned_archive')
    await store.submit(move)
    return archive


@pytest.mark.asyncio
async def test_archive_cursor_snapshot_must_match_committing_cursor(budget_store, monkeypatch):
    store, _ = budget_store
    await append(store, normalized('USER',0))
    await archive_sources(store)
    original = store._fd_prepare
    async def race(entries):
        prepared = await original(entries)
        monkeypatch.setattr(store, '_fd_prepare', original)
        await append(store, normalized('TOOL_USE',1))
        await store.submit(lambda c: (c.execute('DELETE FROM session_event_tail'), c.commit()))
        return prepared
    monkeypatch.setattr(store, '_fd_prepare', race)
    await append(store, normalized('TOOL_USE',2))
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap'] == 'cursor_evidence_unavailable'


@pytest.mark.asyncio
async def test_corrupt_archived_source_cannot_prove_replay(budget_store):
    store, _ = budget_store
    await turn(store)
    archive = await archive_sources(store)
    with sqlite3.connect(archive) as conn:
        conn.execute("UPDATE session_event_tail SET event_json='broken' WHERE json_extract(event_json,'$.kind')='TOOL_USE'")
    await turn(store)
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap']


@pytest.mark.asyncio
async def test_main_observer_accepts_durable_binding_over_env(budget_store):
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    import ast
    store, binding = budget_store
    cfg = AssistantCompositeConfig.from_env({'PENTACLE_ASSISTANT_COMPOSITE_ENABLED':'1',
        'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID':'fixture:assistant','PENTACLE_ASSISTANT_ROUTER_ENDPOINT':'ssh://fixture/path',
        'PENTACLE_ASSISTANT_ROUTER_ACTION_PATH':'/fixture','PENTACLE_ASSISTANT_ASTRA_STREAM_ID':'fixture:astra',
        'PENTACLE_ASSISTANT_LUNA_STREAM_ID':'fixture:luna'})
    await store.submit(lambda c: (c.execute("INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES (?,?,?,1,'2026-10-10T00:00:00Z')",(AssistantCompositeConfig().name,binding['stream_id'],binding['generation'])), c.commit()))
    composite = AssistantComposite(store,config=cfg)
    await composite.load_binding()
    assert composite.config.direct_primary and not composite.env_config.direct_primary
    tree = ast.parse(Path(__file__).parents[1].joinpath('main.py').read_text())
    owners = [node for node in ast.walk(tree) if isinstance(node, ast.If) and any(
        isinstance(child,ast.Call) and isinstance(child.func,ast.Attribute) and child.func.attr == 'configure_fd_turn_budget'
        for statement in node.body for child in ast.walk(statement))]
    assert any(ast.unparse(node.test) == 'assistant_composite.enabled' for node in owners)
    store.configure_fd_turn_budget(config=FdTurnBudgetConfig(enabled=True),env_binding=composite._env_binding())
    await turn(store)
    assert await counts(store) == (1,1)


@pytest.mark.asyncio
async def test_pending_receipt_retry_generation_race_has_no_new_source_or_effect(budget_store, monkeypatch):
    store, binding = budget_store
    await append(store, normalized('USER',0,body=peer_wire()))
    for n in range(1,7):
        await append(store, normalized('TOOL_USE',n))
    before = await store.submit(lambda c:c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0])
    original = store._fd_prepare
    async def race(entries):
        prepared = await original(entries)
        monkeypatch.setattr(store,'_fd_prepare',original)
        await store.open_session('fixture','fd',provider='claude',pane_status='pane_alive',pane_pid='124')
        return prepared
    monkeypatch.setattr(store,'_fd_prepare',race)
    await peer_receipt(store)
    assert await store.submit(lambda c:c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0]) == before
    assert await counts(store) == (0,0)


@pytest.mark.asyncio
async def test_late_send_receipt_reconciles_same_current_turn(budget_store):
    store, _ = budget_store
    await append(store, normalized('USER',0,body=peer_wire()))
    for n in range(1,7):
        await append(store, normalized('TOOL_USE',n))
    await store.append_send_receipt(to_stream_id='fixture:fd',request_id='peer-1',receipt_id='send-proof',state='landed',
        wire_text=peer_wire(),display_text='Peer one',attachments=[],delivery='landed',submission_confirmed=True,
        from_stream_id='fixture:peer',actor_stream_id='fixture:peer',actor_trusted=True)
    assert await counts(store) == (1,1)


@pytest.mark.asyncio
async def test_pending_history_is_bounded_even_if_proof_never_arrives(budget_store):
    store, _ = budget_store
    await append(store, normalized('USER',0,body=peer_wire()))
    for n in range(1,150):
        await append(store, normalized('TOOL_USE',n))
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['continuity_gap'] and 'pending_proofs' not in state and 'pending_state' not in state
    assert len(json.dumps(state)) < 2048 and await counts(store) == (0,0)


@pytest.mark.asyncio
async def test_pending_turn_after_crossing_ignores_only_valid_derived_record(budget_store):
    store, _ = budget_store
    await turn(store)
    await append(store, normalized('USER',10,body=peer_wire()))
    await peer_receipt(store)
    for n in range(11,17):
        await append(store, normalized('TOOL_USE',n))
    state = await store.fd_turn_budget_status('fixture:fd')
    assert state['ordinal'] == 2 and not state['continuity_gap']
    assert await counts(store) == (2,2)


@pytest.mark.asyncio
@pytest.mark.parametrize('unrelated', [False,True])
async def test_pending_retry_uses_current_transaction_interval(budget_store, monkeypatch, unrelated):
    store, _ = budget_store
    await append(store, normalized('USER',0,body=peer_wire()))
    original = store._fd_prepare
    async def racing(entries):
        prepared = await original(entries)
        monkeypatch.setattr(store,'_fd_prepare',original)
        await append(store, normalized('TOOL_USE',1))
        return prepared
    monkeypatch.setattr(store,'_fd_prepare',racing)
    if unrelated:
        await peer_receipt(store,anchor='other',body='Other peer')
        assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap'] == 'peer_provenance_pending'
    await peer_receipt(store)
    for n in range(2,7):
        await append(store, normalized('TOOL_USE',n))
    assert not (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap']
    assert await counts(store) == (1,1)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('cursor',[]),('cursor',2**100),('pending_proofs','invalid')])
@pytest.mark.parametrize('receipt_kind', ['tell','send'])
async def test_receipt_retry_validates_cursor_before_query(budget_store, field, value, receipt_kind):
    store, _ = budget_store
    await append(store, normalized('USER',0,body=peer_wire()))
    state = await store.fd_turn_budget_status('fixture:fd'); state[field] = value
    await store.put(store._fd_cursor_key('fixture:fd'),json.dumps(state))
    if receipt_kind == 'tell':
        await peer_receipt(store)
        assert await store.get_tell_delivery('peer-1')
    else:
        await store.append_send_receipt(to_stream_id='fixture:fd',request_id='peer-1',receipt_id='proof',state='landed',
            wire_text=peer_wire(),display_text='Peer one',attachments=[],delivery='landed',submission_confirmed=True,
            from_stream_id='fixture:peer',actor_stream_id='fixture:peer',actor_trusted=True)
        assert await store.get_send_receipt('fixture:fd','peer-1')
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap'] == 'cursor_corrupt'
    await append(store, normalized('TOOL_USE',1))
    assert (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap']
    assert await counts(store) == (0,0)


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_receipt', [False,True])
@pytest.mark.parametrize('caption', [False,True])
async def test_archive_replay_preserves_immutable_source_through_receipt_projection(budget_store, initial_receipt, caption):
    store, _ = budget_store
    async def receipt(state):
        await store.append_send_receipt(to_stream_id='fixture:fd',request_id='peer-1',receipt_id='proof',state=state,
            wire_text=peer_wire(),display_text=('Caption '+state) if caption else 'Peer one',
            attachments=[{'name':'fixture.txt'}] if caption else [],delivery=state,submission_confirmed=state=='landed',
            from_stream_id='fixture:peer',actor_stream_id='fixture:peer',actor_trusted=True,
            optimistic_id='fixture-'+state,meta_json=json.dumps({'voice':{'duration_s':1 if state == 'accepted' else 2}}))
    if initial_receipt:
        await receipt('accepted')
    incoming = normalized('USER',0,body=peer_wire())
    await append(store,incoming)
    for n in range(1,7):
        await append(store,normalized('TOOL_USE',n))
    await receipt('landed')
    assert not (await store.fd_turn_budget_status('fixture:fd'))['continuity_gap']
    await archive_sources(store)
    await append(store,incoming)
    state = await store.fd_turn_budget_status('fixture:fd')
    assert not state['continuity_gap'] and state['ordinal'] == 1
    assert await counts(store) == (0,1)  # One immutable crossing is in the archive.


@pytest.mark.parametrize('delivery', ['false', 'true'])
@pytest.mark.asyncio
async def test_observer_off_ingest_has_zero_archive_or_provenance_reads(budget_store, monkeypatch, delivery):
    store, binding = budget_store
    cfg = FdTurnBudgetConfig.from_env({'PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED': 'false',
                                      'PENTACLE_FD_TURN_BUDGET_ENABLED': delivery})
    store.configure_fd_turn_budget(config=cfg, env_binding=binding)
    def forbidden(*args, **kwargs):
        raise AssertionError('observer-off performed an archive/provenance read')
    monkeypatch.setattr(store, '_fd_archive_evidence', forbidden)
    monkeypatch.setattr(store, '_fd_dispatch_conn', forbidden)
    await turn(store)
    # Replay and receipt-triggered pending recovery also stay off.
    await append(store, normalized('USER', 0))
    await store._fd_retry_pending(binding['stream_id'])
    assert await store.append_session_event(binding['stream_id'], normalized('USER', 0), limit=100, identity=_identity_key(normalized('USER', 0))) is None
    assert await counts(store) == (0, 0)
    assert await store.fd_turn_budget_status(binding['stream_id']) is None
    assert await store.submit(lambda c: c.execute('SELECT count(*) FROM session_event_tail').fetchone()[0]) == 7


@pytest.mark.asyncio
async def test_observer_on_delivery_off_preserves_record_only(budget_store):
    store, binding = budget_store
    cfg = FdTurnBudgetConfig.from_env({'PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED': 'true',
                                      'PENTACLE_FD_TURN_BUDGET_ENABLED': 'false'})
    store.configure_fd_turn_budget(config=cfg, env_binding=binding)
    await turn(store)
    assert await counts(store) == (1, 0)


def test_runtime_observer_defaults_off_and_validates_switch():
    assert FdTurnBudgetConfig.from_env({}).observer_enabled is False
    assert FdTurnBudgetConfig.from_env({'PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED': 'true'}).observer_enabled is True
    with pytest.raises(ValueError):
        FdTurnBudgetConfig.from_env({'PENTACLE_FD_TURN_BUDGET_OBSERVER_ENABLED': 'invalid'})
