"""Opt-in assistant uses existing role/session lifecycle; no separate identity store."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store

HOST = "hosta"
OPERATOR = {"operator_authenticated": True, "operator_principal": "operator:test"}

class GoneTmux:
    async def has_session(self, name):
        return False
    async def pane_pid(self, name):
        return ""

def scenario(fn):
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=GoneTmux(), local_host=HOST)
            await fn(store, sessions)
        finally:
            store.stop()
    asyncio.run(run())

@pytest.mark.parametrize("kind", ["session_close", "operator_close", "idle_reap", "self_close_backlog_sweep"])
def test_assistant_ordinary_close_refused_before_kill(monkeypatch, kind):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        await store.open_session(HOST, "helper", role="assistant")
        await sessions.refresh()
        with pytest.raises(VerbError, match="protected") as exc:
            await sessions.close(HOST, "helper", close_kind=kind, operator_override=True, operator_confirm=True)
        assert exc.value.code == "close_protected"
        assert (await store.fetch_session(HOST, "helper"))["status"] == "open"
    scenario(check)

def test_default_off_preserves_ordinary_close(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    async def check(store, sessions):
        await store.open_session(HOST, "helper", role="assistant")
        await sessions.refresh()
        await sessions.close(HOST, "helper", operator_confirm=True)
        assert (await store.fetch_session(HOST, "helper"))["status"] == "closed"
    scenario(check)

@pytest.mark.parametrize("old,new", [(None,"assistant"),("assistant","lead")])
def test_agent_cannot_grant_or_remove_protected_role(monkeypatch, old, new):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        await store.open_session(HOST, "helper", role=old)
        await sessions.refresh()
        with pytest.raises(VerbError) as exc:
            await sessions.set_role(HOST, "helper", new, auth_context={"token_verified":True,"stream_id":"hosta:helper"})
        assert exc.value.code == "role_authority_denied"
    scenario(check)

def test_duplicate_role_grant_refused_and_holder_survives_restart(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        await store.open_session(HOST, "first", role="assistant")
        await store.open_session(HOST, "second", role="lead")
        await sessions.refresh()
        restarted = Sessions(store, local_host=HOST)
        await restarted.refresh()
        with pytest.raises(VerbError) as exc:
            await restarted.set_role(HOST, "second", "assistant", auth_context=OPERATOR)
        assert exc.value.code == "assistant_exists"
        assert (await store.fetch_session(HOST, "second"))["role"] == "lead"
    scenario(check)

def test_spawn_self_promotion_refused_before_boot(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        ctl = SpawnCtl(store, sessions, tmux=GoneTmux())
        ctl._spawn_impl = AsyncMock(return_value={"type":"spawn.ok"})
        with pytest.raises(VerbError) as exc:
            await ctl.spawn({"role":"assistant","objective":"Test protected role","_auth_context":{"token_verified":True,"stream_id":"hosta:worker"}}, HOST)
        assert exc.value.code == "role_authority_denied"
        ctl._spawn_impl.assert_not_awaited()
    scenario(check)


class LiveTmux(GoneTmux):
    def __init__(self):
        self.live = set()
        self.created = 0
    async def has_session(self, name):
        return name in self.live
    async def new_session(self, name, command, cwd=None, env=None):
        self.live.add(name)
        self.created += 1
    async def capture(self, name):
        return "READY"
    async def pane_pid(self, name):
        return "1234"
    async def session_state(self, name):
        return "alive" if name in self.live else "gone"
    async def kill_session(self, name):
        self.live.discard(name)

def test_concurrent_activation_and_idempotent_replay(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        tmux = LiveTmux()
        sessions.tmux = tmux
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        base = {"objective":"Test assistant activation","role":"assistant","command":"stub","_auth_context":OPERATOR}
        replies = await asyncio.gather(*[ctl.spawn({**base,"request_id":f"activate-{i}"}, HOST) for i in range(2)], return_exceptions=True)
        success = [r for r in replies if isinstance(r,dict)]
        failures = [r for r in replies if isinstance(r,VerbError)]
        assert len(success) == len(failures) == 1
        assert failures[0].code == "assistant_exists"
        replay = await ctl.spawn({**base,"request_id":f"activate-{replies.index(success[0])}"}, HOST)
        assert replay["stream_id"] == success[0]["stream_id"]
        assert replay["replayed"] is True
        assert tmux.created == 1
        assert len([r for r in sessions.list_open() if r.get("role")=="assistant"]) == 1
    scenario(check)

def test_pending_spawn_blocks_replacement_after_restart(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        assert await store.reserve_stream_id(HOST,"pending",ttl_s=180,request_id="pending-1",nonce="n1")
        assert await store.record_spawn_intent(HOST,"pending",{"open_fields":{"role":"assistant"}},request_id="pending-1",nonce="n1")
        restarted = Sessions(store, tmux=LiveTmux(), local_host=HOST)
        await restarted.refresh()
        ctl = SpawnCtl(store,restarted,tmux=restarted.tmux)
        with pytest.raises(VerbError) as exc:
            await ctl.spawn({"objective":"Test unresolved spawn","role":"assistant","command":"stub","request_id":"new","_auth_context":OPERATOR}, HOST)
        assert exc.value.code == "assistant_exists"
        assert restarted.tmux.created == 0
    scenario(check)

def test_dead_open_assistant_handoff_preserves_role_and_closes_predecessor(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store, sessions):
        await store.open_session(HOST,"old",role="assistant",provider="claude",effective_model="claude-opus-4-8",effective_effort="high")
        await sessions.refresh()
        tmux = LiveTmux()
        sessions.tmux = tmux
        ctl = SpawnCtl(store,sessions,tmux=tmux)
        reply = await ctl.spawn({"objective":"Test dead assistant recovery","handoff":True,"handoff_from_stream_id":"hosta:old","command":"stub","request_id":"recovery-1","ready_marker":"READY","_auth_context":OPERATOR}, HOST)
        assert reply["type"] == "spawn.ok"
        # The admission response can precede managed predecessor cleanup.
        await asyncio.gather(*list(ctl._background_spawns))
        assert (await store.fetch_session(HOST,"old"))["status"] == "closed"
        holders = [r for r in sessions.list_open() if r.get("role")=="assistant"]
        assert len(holders)==1 and holders[0]["stream_id"]==reply["stream_id"]
    scenario(check)

def test_own_handoff_allowed_but_worker_impersonation_refused(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store,sessions):
        await store.open_session(HOST,"old",role="assistant")
        policy=sessions.assistant
        msg={"handoff":True,"handoff_from_stream_id":"hosta:old","_auth_context":{"token_verified":True,"stream_id":"hosta:old"}}
        async with policy.spawn(msg,HOST):
            await policy.available(HOST,predecessor="hosta:old")
        with pytest.raises(VerbError) as exc:
            async with policy.spawn({**msg,"_auth_context":{"token_verified":True,"stream_id":"hosta:worker"}},HOST):
                pytest.fail("unrelated worker must not rotate assistant")
        assert exc.value.code=="role_authority_denied"
    scenario(check)

def test_close_rpc_cannot_supply_managed_kind(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store,sessions):
        from server import Server
        await store.open_session(HOST,"helper",role="assistant")
        await sessions.refresh()
        server=Server(store=store,sessions=sessions,local_host=HOST)
        with pytest.raises(VerbError) as exc:
            await server._on_close({"stream_id":"hosta:helper","close_kind":"handed_off","operator_confirm":True,"_auth_context":OPERATOR})
        assert exc.value.code=="close_protected"
    scenario(check)


def test_confirmed_dead_assistant_stays_visible_for_recovery(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store,sessions):
        await store.open_session(HOST,"helper",role="assistant")
        await sessions.refresh()
        row=sessions.get("hosta:helper")
        result=await sessions.mark_reconciled_dead(HOST,"helper",presumed_dead_at="2026-09-12T12:00:00Z",closed_at="2026-09-12T12:01:00Z",expected_generation=sessions._row_generation(row))
        assert result is None
        assert (await store.fetch_session(HOST,"helper"))["status"]=="open"
        assert any(r["stream_id"]=="hosta:helper" for r in sessions.list_open())
    scenario(check)

def test_schedule_admission_enforces_role_authority(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def check(store,sessions):
        ctl=SpawnCtl(store,sessions,tmux=GoneTmux())
        ctl._admit_schedule=AsyncMock(return_value={"ok":True})
        with pytest.raises(VerbError) as exc:
            await ctl.admit_schedule({"role":"assistant","_auth_context":{"token_verified":True,"stream_id":"hosta:worker"}},HOST,admission_name="scheduled")
        assert exc.value.code=="role_authority_denied"
        ctl._admit_schedule.assert_not_awaited()
        await store.open_session(HOST,"helper",role="assistant")
        result=await ctl.admit_schedule({"handoff":True,"handoff_from_stream_id":"hosta:helper","_auth_context":{"token_verified":True,"stream_id":"hosta:helper"}},HOST,admission_name="scheduled")
        assert result=={"ok":True}
    scenario(check)
