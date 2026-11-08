"""Compatible prior consumes future witnesses without creating new ones.

All panes and destructive sinks are recording doubles; databases are owned.
The separate cross-version proof uses actual writer-produced databases.
"""
import asyncio
import json

import pytest

import spawn_creation
from machines import MachineConfig
from sessions import Sessions
from spawnctl import SpawnCtl, VerbError
from store import Store
from tests.test_spawn_cancel_fence_holes import ReconTmux


LOCAL, PEER = "fixture-local", "fixture-peer"
MISSING = object()


class Hosts:
    peers = {PEER: MachineConfig(name=PEER, ssh_target="fixture-peer", agent_orch_bin_dir="/fixture/bin")}

    def __init__(self, tmux):
        self.tmux = tmux

    def is_online(self, host):
        return host == PEER

    async def ensure_reachable(self, host, what):
        assert host == PEER

    def tmux_for(self, host):
        assert host == PEER
        return self.tmux

    async def run_command(self, *args, **kwargs):
        return 1, ""  # no process inspection or signal target


def controller(store, tmux, owner="owner"):
    hosts = Hosts(tmux)
    return SpawnCtl(store, Sessions(store, tmux=tmux, local_host=LOCAL, hosts=hosts),
                    tmux=tmux, hosts=hosts, owner_instance_id=owner)


async def intent(store, value=MISSING):
    assert await store.reserve_stream_id(PEER, "owned", ttl_s=60, request_id="request",
                                         nonce="nonce", owner_instance_id="owner")
    payload = {"open_fields": {"session_generation": "generation"}, "brief": ""}
    if value is not MISSING:
        payload[spawn_creation.KEY] = value
    assert await store.record_spawn_intent(PEER, "owned", payload, request_id="request", nonce="nonce")


def witness(terminal=False):
    return {"version": 1, "attempts": [{"id": "attempt", "owner": "writer", "terminal": terminal}]}


@pytest.mark.parametrize("value", [None, {}, {"version": 2, "attempts": []}, witness(), witness(True),
                                   {"version": 1, "attempts": []}])
def test_reader_refuses_physical_dispatch_for_any_witness_member(tmp_path, value):
    async def run():
        store = Store(str(tmp_path / "reader.db")); store.start()
        class NoCreation(ReconTmux):
            async def new_session(self, *args, **kwargs):
                self.created += 1
                raise AssertionError("reader dispatched a witnessed creation")
        tmux = NoCreation()
        ctl = controller(store, tmux)
        try:
            await intent(store, value)
            before = (await store.reservations(include_expired=True))[0]["payload"]
            # Every retry/resume creation converges on this exact boundary.
            for attempt in (0, 1):
                with pytest.raises(VerbError, match="witness-capable writer"):
                    await ctl._spawn_fenced(PEER, "owned", "request", "fixture-command", "", "",
                                             {}, [False], [False], {}, {}, tmux, {}, "nonce",
                                             attempt=attempt, total_attempts=2)
            assert tmux.created == 0
            assert (await store.reservations(include_expired=True))[0]["payload"] == before
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("raw", [None, "broken-json", "[]", "null"])
def test_reader_refuses_unreadable_or_missing_current_intent(tmp_path, raw):
    async def run():
        store = Store(str(tmp_path / "missing.db")); store.start()
        try:
            await intent(store)
            await store.submit(lambda conn: conn.execute(
                "UPDATE v2_stream_reservations SET payload=?", (raw,)))
            assert not await store.owns_unwitnessed_spawn_intent(
                PEER, "owned", "request", "nonce", owner_instance_id="owner")
        finally:
            store.stop()
    asyncio.run(run())


def test_reader_dispatch_check_binds_current_identity_owner_and_cancel(tmp_path):
    async def run():
        store = Store(str(tmp_path / "identity.db")); store.start()
        async def allowed(name="owned", request="request", nonce="nonce", owner="owner"):
            return await store.owns_unwitnessed_spawn_intent(PEER, name, request, nonce, owner_instance_id=owner)
        try:
            await intent(store)
            assert await allowed()
            assert not await allowed(name="other")
            assert not await allowed(request="other")
            assert not await allowed(nonce="other")
            assert not await allowed(owner="other")
            assert (await store.cancel_reservation_fenced(PEER, "owned", "request"))["status"] == "cancelled"
            assert not await allowed()
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("value", [MISSING, None, {"version": 2, "attempts": []}, witness(), witness(True)])
def test_cancelled_future_or_historical_record_survives_reader_restart(tmp_path, value):
    async def run():
        path = str(tmp_path / "restart.db")
        store = Store(path); store.start()
        tmux = ReconTmux()
        try:
            await intent(store, value)
            assert (await store.cancel_reservation_fenced(PEER, "owned", "request"))["status"] == "cancelled"
            store.stop()
            store = Store(path); store.start()
            ctl = controller(store, tmux, owner="reader-after-restart")
            # Foreign same-name pane remains untouched.
            tmux.live.add("owned"); tmux.nonces["owned"] = "foreign"
            await ctl.reconcile_spawn_intents(recurring=False)
            assert tmux.kill_calls == 0 and tmux.live == {"owned"}
            # Actual exact late identity is cleanup-only, never admitted.
            tmux.nonces["owned"] = "nonce"
            await ctl.reconcile_spawn_intents(recurring=True)
            assert tmux.kill_calls == 1 and not tmux.live
            await ctl.reconcile_spawn_intents(recurring=True)
            status = await ctl.spawn_status({"host": PEER, "target": "request"}, LOCAL)
            assert status["outcomes"][0]["state"] == "cancelled"
            complete = value is not MISSING and spawn_creation.terminal(spawn_creation.witness({spawn_creation.KEY: value}))
            assert bool(status["reservations"]) == (not complete)
            if not complete:
                assert status["reservations"][0]["cleanup"]["state"] == "unknown"
            assert await store.fetch_session(PEER, "owned") is None
            assert tmux.created == 0 and tmux.kill_calls == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_reader_payload_refresh_preserves_every_future_value(tmp_path):
    async def run():
        store = Store(str(tmp_path / "refresh.db")); store.start()
        try:
            await intent(store, witness())
            assert await store.record_spawn_intent(PEER, "owned", {"brief": "updated"}, request_id="request", nonce="nonce")
            payload = json.loads((await store.reservations(include_expired=True))[0]["payload"])
            assert payload[spawn_creation.KEY] == witness()
        finally:
            store.stop()
    asyncio.run(run())


def test_reader_normal_remote_spawn_produces_no_witness(tmp_path):
    async def run():
        store = Store(str(tmp_path / "normal.db")); store.start()
        tmux = ReconTmux(); ctl = controller(store, tmux)
        recorded = []
        original = store.record_spawn_intent
        async def observe(host, name, payload, **kwargs):
            result = await original(host, name, payload, **kwargs)
            rows = await store.reservations(include_expired=True)
            recorded.extend(json.loads(row["payload"]) for row in rows if row.get("payload"))
            return result
        store.record_spawn_intent = observe
        try:
            reply = await ctl.spawn({"host": PEER, "session_name": "normal", "request_id": "normal-request",
                                     "command": "fixture-command", "objective": "Qualify legacy reader normal remote spawn"}, LOCAL)
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            assert reply["type"] == "spawn.ok" and tmux.created == 1
            assert (await store.fetch_session(PEER, "normal"))["status"] == "open"
            assert not await store.reservations(include_expired=True)
            assert recorded and all(spawn_creation.KEY not in payload for payload in recorded)
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("resume", [False, True])
def test_reader_remote_retry_and_resume_produce_no_witness(tmp_path, monkeypatch, resume):
    import spawnctl as module
    from tests.test_spawn_auth_context import ClaudeBootTmux, _claude_resolve
    monkeypatch.setattr(module, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    async def run():
        store = Store(str(tmp_path / "retry.db")); store.start()
        tmux = ClaudeBootTmux("Loading Claude…"); ctl = controller(store, tmux)
        ctl._resolve_launch = _claude_resolve
        if resume:
            async def transcript_discovery(*args):
                return "resumed", "/fixture/transcript.jsonl", "", None
            ctl._resolve_resume_target = transcript_discovery
        observed = []
        original = store.record_spawn_intent
        async def record(host, name, payload, **kwargs):
            result = await original(host, name, payload, **kwargs)
            observed.extend(json.loads(row["payload"]) for row in await store.reservations(include_expired=True)
                            if row.get("payload"))
            return result
        store.record_spawn_intent = record
        try:
            message = {"host": PEER, "request_id": "retry-request", "provider": "claude",
                       "initial_prompt": "never delivered", "objective": "Qualify reader remote retry without witness production"}
            if resume:
                message["resume_session_id"] = "12345678-1234-1234-1234-123456789abc"
            else:
                message["session_name"] = "retry"
            with pytest.raises(VerbError) as error:
                await ctl.spawn(message, LOCAL)
            assert error.value.code == "boot_not_ready"
            assert tmux.new_sessions == module.PROMPT_SPAWN_RETRY_ATTEMPTS > 1
            assert tmux.pastes == 0
            assert observed and all(spawn_creation.KEY not in payload for payload in observed)
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())
