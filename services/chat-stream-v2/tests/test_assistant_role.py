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


@pytest.mark.parametrize("handoff", [False, True])
def test_daff_spawn_and_handoff_persist_hidden_and_replay_once(monkeypatch, handoff):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", "partner-assistant")

    async def check(store, sessions):
        tmux = LiveTmux()
        sessions.tmux = tmux
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        msg = {"objective": "Test hidden partner lifecycle", "role": "partner-assistant",
               "visibility": "default", "command": "stub", "request_id": "hidden-partner",
               "ready_marker": "READY", "_auth_context": OPERATOR}
        if handoff:
            await store.open_session(HOST, "old", role="partner-assistant", provider="claude",
                                     effective_model="claude-opus-5-5", effective_effort="medium")
            await sessions.refresh()
            msg.update(handoff=True, handoff_from_stream_id=f"{HOST}:old")
        reply = await asyncio.wait_for(ctl.spawn(dict(msg), HOST), 5)
        await asyncio.wait_for(asyncio.gather(*list(ctl._background_spawns)), 5)
        assert reply["type"] == "spawn.ok"
        row = sessions.get(reply["stream_id"])
        assert row["visibility"] == "hidden"
        assert row["role"] == "partner-assistant"
        replay = await asyncio.wait_for(ctl.spawn(dict(msg), HOST), 5)
        assert replay["replayed"] is True
        assert replay["stream_id"] == reply["stream_id"]
        assert tmux.created == 1
    scenario(check)


def test_daff_hidden_launch_matches_primary_hidden_seat_instructions(monkeypatch, tmp_path):
    import launch
    import shlex

    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", "partner-assistant")

    async def check(store, sessions):
        machine = launch.local_machine(HOST, cwd=str(tmp_path), claude_bin="/bin/echo",
                                       projects_root=str(tmp_path / "projects"), agent_orch_bin_dir="/bin")
        ctl = SpawnCtl(store, sessions, tmux=LiveTmux(), machine=machine)
        for role, visible in (("partner-assistant", False), ("assistant", True)):
            msg = {"role": role, "provider": "claude", "visibility": "default",
                   "_auth_context": OPERATOR, "_defer_launch_token_stage": True}
            async with sessions.assistant.spawn(msg, HOST):
                command, _, _ = await ctl._resolve_launch(msg, HOST, "fixture")
            assert (launch.VISIBLE_SESSION_METADATA_INSTRUCTION in shlex.split(command)) is visible
    scenario(check)


def test_daff_unauthorized_promotion_still_denied_before_normalization(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", "partner-assistant")

    async def check(store, sessions):
        msg = {"role": "partner-assistant", "visibility": "default",
               "_auth_context": {"token_verified": True, "stream_id": f"{HOST}:worker"}}
        with pytest.raises(VerbError) as caught:
            async with sessions.assistant.spawn(msg, HOST):
                pytest.fail("unauthorized role promotion")
        assert caught.value.code == "role_authority_denied"
        assert msg["visibility"] == "default"
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


FD_SPEC = "spec_example__fd_recovery"


class _FdCatalog:
    @staticmethod
    def resolution_for(spec_id):
        return "resolved" if str(spec_id or "").removeprefix("spec_") == FD_SPEC.removeprefix("spec_") else "zero_matches"

    @staticmethod
    def canonical_spec_identity(spec_id):
        value = str(spec_id or "").removeprefix("spec_")
        return f"spec_{value}" if value else None


def _front_desk_composite(store, sessions, *, bound=None):
    """Primary composite with no role env: the FD seat is an ordinary `lead`."""
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    config = AssistantCompositeConfig(
        enabled=True, stream_id="fixture-chat:assistant", rebind_authorized_spec_ids=frozenset({FD_SPEC}),
        **({"direct_primary_stream_id": bound[0], "direct_primary_generation": bound[1]} if bound else {}),
    )
    composite = AssistantComposite(store, config=config)
    sessions.assistant.front_desk = composite
    return composite


def _spy_operator_facing(monkeypatch):
    import launch
    seen = []
    real = launch.build_launch

    def spy(*args, **kwargs):
        seen.append(kwargs.get("operator_facing"))
        return real(*args, **kwargs)
    monkeypatch.setattr(launch, "build_launch", spy)
    return seen


@pytest.mark.parametrize("requested", ["default", None])
def test_front_desk_handoff_persists_hidden_without_role_env(monkeypatch, tmp_path, requested):
    from test_spawn_resume import ResumeTmux, _install_successful_spawn, _machine
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    monkeypatch.delenv("PENTACLE_ASSISTANT_DAFF_ROLE", raising=False)
    operator_facing = _spy_operator_facing(monkeypatch)

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            old = await store.open_session("localhost", "old", provider="claude", role="lead",
                                           visibility="hidden", pane_status="pane_alive",
                                           effective_model="claude-opus-5-5", effective_effort="medium")
            tmux = ResumeTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            await sessions.refresh()
            _front_desk_composite(store, sessions, bound=("localhost:old", old["session_generation"]))
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=_machine(tmp_path))
            _install_successful_spawn(ctl, sessions, tmux)
            msg = {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium", "handoff": True,
                   "handoff_from_stream_id": "localhost:old", "request_id": "fd-succession",
                   "_auth_context": OPERATOR}
            if requested:
                msg["visibility"] = requested
            result = await ctl.spawn(msg, "localhost")
            assert result["type"] == "spawn.ok"
            assert sessions.get(result["stream_id"])["visibility"] == "hidden"
            assert result["session"]["visibility"] == "hidden"
            assert operator_facing == [False]
        finally:
            store.stop()
    asyncio.run(run())


def test_front_desk_recovery_spec_hides_parentless_seat_before_binding_loads(monkeypatch, tmp_path):
    from test_spawn_resume import ResumeTmux, _install_successful_spawn, _machine
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    monkeypatch.delenv("PENTACLE_ASSISTANT_DAFF_ROLE", raising=False)
    operator_facing = _spy_operator_facing(monkeypatch)

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("localhost", "parent", provider="claude", role="lead",
                                     visibility="default", pane_status="pane_alive")
            tmux = ResumeTmux()
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            await sessions.refresh()
            _front_desk_composite(store, sessions)  # binding not loaded: nothing bound
            ctl = SpawnCtl(store, sessions, tmux=tmux, machine=_machine(tmp_path), specs=_FdCatalog())
            _install_successful_spawn(ctl, sessions, tmux)
            base = {"provider": "claude", "model": "claude-opus-5-5", "effort": "medium",
                    "spec_id": FD_SPEC, "visibility": "default", "_auth_context": OPERATOR}
            top = await ctl.spawn({**base, "request_id": "fd-recovery"}, "localhost")
            assert sessions.get(top["stream_id"])["visibility"] == "hidden"
            child = await ctl.spawn({**base, "request_id": "fd-child", "parent_stream_id": "localhost:parent",
                                     "role": "lead"}, "localhost")
            assert sessions.get(child["stream_id"])["visibility"] == "default"
            assert operator_facing[0] is False
            assert operator_facing[1:] == [False]  # a parented seat is never operator-facing
        finally:
            store.stop()
    asyncio.run(run())
