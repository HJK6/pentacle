"""Cancelled remote creation retains cleanup ownership across restart.

Uses the actual spawn producer and durable Store, with an independently
completing peer operation. All destructive sinks are recording-only doubles.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading

import pytest

import spawn_creation
from machines import MachineConfig
from sessions import Sessions
from spawnctl import PANE_NONCE_ENV, SpawnCtl
from store import Store
from tests.test_spawn_cancel_fence_holes import ReconTmux


LOCAL = "fixture-local"
PEER = "fixture-peer"


class PendingPeer(ReconTmux):
    def __init__(self):
        super().__init__()
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()
        self.operation = None
        self.unknown = False

    async def new_session(self, name, command, cwd=None, env=None):
        async def peer_command():
            self.accepted.set()
            await self.release.wait()
            await super(PendingPeer, self).new_session(name, command, cwd=cwd, env=env)
        self.operation = asyncio.create_task(peer_command())
        # Terminating the local SSH waiter cannot revoke an already accepted
        # remote command. Only this fixture owns and drains the peer task.
        await asyncio.shield(self.operation)

    async def session_state(self, name):
        return "unreachable" if self.unknown else await super().session_state(name)

    async def paste(self, name, text):
        raise AssertionError("cancelled work must never receive a brief")


class PeerHosts:
    local_host = LOCAL
    peers = {PEER: MachineConfig(name=PEER, ssh_target="fixture-peer", agent_orch_bin_dir="/fixture/bin")}

    def __init__(self, tmux):
        self.tmux = tmux
        self.online = True

    def is_online(self, host):
        return host == PEER and self.online

    async def ensure_reachable(self, host, what):
        assert host == PEER and self.online

    def tmux_for(self, host):
        assert host == PEER
        return self.tmux

    async def run_command(self, host, *args, **kwargs):
        # No actual ps or signal target in the fixture.
        return 1, ""


def test_cancelled_pending_remote_creation_remains_owned_after_restart(tmp_path: Path):
    async def run():
        tmux = PendingPeer()
        hosts = PeerHosts(tmux)
        name = "delayed-cancel"
        database = str(tmp_path / "store.db")
        store = None
        controllers = []
        def start(owner):
            current = Store(database)
            current.start()
            sessions = Sessions(current, tmux=tmux, local_host=LOCAL, hosts=hosts)
            ctl = SpawnCtl(current, sessions, tmux=tmux, hosts=hosts, owner_instance_id=owner)
            controllers.append(ctl)
            return current, ctl
        try:
            store, ctl = start("before-restart")
            caller = asyncio.create_task(ctl.spawn({
                "host": PEER, "session_name": name, "request_id": "cancel-request",
                "idempotency_key": "cancel-key", "command": "fixture-command",
                "objective": "Qualify delayed cancelled remote creation ownership",
            }, LOCAL))
            await asyncio.wait_for(tmux.accepted.wait(), 5)
            reservation = (await store.reservations(include_expired=True))[0]
            nonce = reservation["nonce"]
            assert nonce and json.loads(reservation["payload"])["open_fields"]["session_generation"]
            reply = await ctl.spawn_cancel({"host": PEER, "target": "cancel-request"}, LOCAL)
            assert reply["state"] == "cancelled"
            background = tuple(ctl._background_spawns)
            for pending in background:
                pending.cancel()
            await asyncio.gather(*background, caller, return_exceptions=True)
            assert not tmux.operation.done()
            store.stop()
            store, ctl = start("after-restart")
            # Offline and ambiguous transport retain the obligation.
            hosts.online = False
            await ctl.reconcile_spawn_intents(recurring=False)
            assert await store.reservations(include_expired=True)
            hosts.online = True
            tmux.unknown = True
            await ctl.reconcile_spawn_intents(recurring=False)
            assert await store.reservations(include_expired=True)
            tmux.unknown = False
            # The same human name is insufficient authority to kill a foreign
            # nonce. This fixture-owned pane is removed only by the fixture.
            tmux.live.add(name)
            tmux.nonces[name] = "foreign-nonce"
            await ctl.reconcile_spawn_intents(recurring=False)
            assert name in tmux.live and tmux.kill_calls == 0
            tmux.live.remove(name)
            await ctl.reconcile_spawn_intents(recurring=False)
            kept_during_absence = bool(await store.reservations(include_expired=True))
            # Finish the ORIGINAL peer command after the daemon has reopened
            # and observed absence. It carries the actual producer nonce.
            tmux.release.set()
            await asyncio.wait_for(tmux.operation, 5)
            assert tmux.nonces[name] == nonce
            await ctl.reconcile_spawn_intents(recurring=False)
            cleaned_late = name not in tmux.live
            kills_after_cleanup = tmux.kill_calls
            await ctl.reconcile_spawn_intents(recurring=False)
            outcome = await store.get_spawn_outcome_by_request_id("cancel-request", host=PEER)
            assert outcome["state"] == "cancelled" and await store.fetch_session(PEER, name) is None
            assert tmux.kill_calls == kills_after_cleanup
            # The original waiter lost its ACK. Cleanup must not manufacture
            # terminal creation evidence even after matching-pane destruction.
            kept_unknown_after_cleanup = bool(await store.reservations(include_expired=True))
            assert (kept_during_absence, cleaned_late, kept_unknown_after_cleanup) == (True, True, True)
            assert not await store.reserve_stream_id(PEER, name, ttl_s=60, request_id="replacement")
            # Retaining cleanup ownership of this name must not create a
            # host-wide ordinary worker hold or leak a successful new spawn.
            other = await ctl.spawn({"host": PEER, "session_name": "unrelated", "request_id": "other",
                                     "command": "fixture-command", "objective": "Qualify unrelated worker admission"}, LOCAL)
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            assert other["type"] == "spawn.ok"
            assert (await store.fetch_session(PEER, "unrelated"))["status"] == "open"
            assert [r["session_name"] for r in await store.reservations(include_expired=True)] == [name]
        finally:
            tmux.release.set()
            if tmux.operation:
                await asyncio.gather(tmux.operation, return_exceptions=True)
            for ctl in controllers:
                for pending in tuple(ctl._background_spawns):
                    pending.cancel()
                await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            if store:
                store.stop()
    asyncio.run(run())


async def reserved(store, *, legacy=False):
    assert await store.reserve_stream_id(
        PEER, "held", ttl_s=60, request_id="request", nonce="nonce", owner_instance_id="owner",
    )
    payload = {"open_fields": {"session_generation": "generation"}, "brief": "original"}
    if not legacy:
        payload[spawn_creation.KEY] = spawn_creation.initial()
    assert await store.record_spawn_intent(PEER, "held", payload, request_id="request", nonce="nonce")


def identity(**overrides):
    return dict(request_id="request", nonce="nonce", owner_instance_id="owner", **overrides)


def test_attempt_set_seals_at_cancel_and_only_complete_set_releases(tmp_path):
    async def run():
        store = Store(str(tmp_path / "set.db"))
        store.start()
        try:
            await reserved(store)
            for attempt in ("first", "second"):
                assert await store.issue_remote_creation(PEER, "held", **identity(attempt_id=attempt))
            assert not await store.issue_remote_creation(PEER, "held", **identity(attempt_id="first"))
            assert await store.acknowledge_remote_creation(PEER, "held", **identity(attempt_id="first"))
            stale = spawn_creation.witness((await store.reservations(include_expired=True))[0]["payload"])
            assert (await store.cancel_reservation_fenced(PEER, "held", "request"))["status"] == "cancelled"
            assert not await store.issue_remote_creation(PEER, "held", **identity(attempt_id="late"))
            assert await store.cancelled_creation_snapshot(PEER, "held", **identity()) is None
            assert not await store.release_cancelled_remote_creation(
                PEER, "held", **identity(observed_witness=stale))
            # ACK is independent of admission and succeeds after cancellation.
            for _ in range(2):
                assert await store.acknowledge_remote_creation(PEER, "held", **identity(attempt_id="second"))
            complete = await store.cancelled_creation_snapshot(PEER, "held", **identity())
            assert complete is not None
            assert not await store.release_cancelled_remote_creation(
                PEER, "held", **identity(observed_witness=stale))
            assert await store.release_cancelled_remote_creation(
                PEER, "held", **identity(observed_witness=complete))
            assert not await store.release_cancelled_remote_creation(
                PEER, "held", **identity(observed_witness=complete))
            assert await store.spawn_cancelled(PEER, "held", "request")
        finally:
            store.stop()
    asyncio.run(run())


def test_ack_and_release_cannot_cross_owner_or_successor_identity(tmp_path):
    async def run():
        store = Store(str(tmp_path / "owner.db"))
        store.start()
        try:
            await reserved(store)
            assert await store.issue_remote_creation(PEER, "held", **identity(attempt_id="first"))
            assert await store.claim_spawn_intent(PEER, "held", request_id="request", prior_owner="owner", owner_instance_id="new-owner")
            assert not await store.acknowledge_remote_creation(PEER, "held", **identity(attempt_id="first"))
            assert (await store.cancel_reservation_fenced(PEER, "held", "request"))["status"] == "cancelled"
            assert not await store.release_cancelled_remote_creation(
                PEER, "held", **identity(observed_witness=spawn_creation.initial()))
            assert not await store.acknowledge_remote_creation(
                PEER, "held", request_id="request", nonce="nonce", owner_instance_id="new-owner", attempt_id="first")
            value = spawn_creation.witness((await store.reservations(include_expired=True))[0]["payload"])
            assert value["attempts"] == [{"id": "first", "owner": "owner", "terminal": False}]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("value", [None, {}, {"version": True, "attempts": []},
    {"version": 2, "attempts": []}, {"version": 1, "attempts": ""},
    {"version": 1, "attempts": [{"id": "one", "owner": "o", "terminal": "yes"}]},
    {"version": 1, "attempts": [{"id": "one", "owner": "o", "terminal": True}] * 2},
    {"version": 1, "attempts": [{"id": "one", "terminal": True}]},
])
def test_missing_or_malformed_witness_is_unknown(value):
    payload = {spawn_creation.KEY: value}
    assert spawn_creation.witness(payload) is None
    assert spawn_creation.cleanup_status(payload)["state"] == "unknown"


def test_payload_refresh_preserves_attempts_and_never_backfills_legacy(tmp_path):
    async def run():
        for legacy in (False, True):
            store = Store(str(tmp_path / f"refresh-{legacy}.db"))
            store.start()
            try:
                await reserved(store, legacy=legacy)
                if not legacy:
                    assert await store.issue_remote_creation(PEER, "held", **identity(attempt_id="pending"))
                # A receipt refresh must preserve history even if its caller
                # omits it or accidentally presents a fresh empty set.
                for supplied in ({}, {spawn_creation.KEY: spawn_creation.initial()}):
                    assert await store.record_spawn_intent(PEER, "held", {"brief": "updated", **supplied}, request_id="request", nonce="nonce")
                    row = (await store.reservations(include_expired=True))[0]
                    payload = json.loads(row["payload"])
                    assert payload["brief"] == "updated"
                    if legacy:
                        assert spawn_creation.KEY not in payload
                    else:
                        assert spawn_creation.witness(payload)["attempts"][0]["id"] == "pending"
                assert (await store.cancel_reservation_fenced(PEER, "held", "request"))["status"] == "cancelled"
                assert await store.cancelled_creation_snapshot(PEER, "held", **identity()) is None
            finally:
                store.stop()
    asyncio.run(run())


def test_ack_commit_settles_before_cancelled_caller_returns(tmp_path):
    async def run():
        store = Store(str(tmp_path / "settle.db"))
        store.start()
        entered, release = threading.Event(), threading.Event()
        try:
            await reserved(store)
            assert await store.issue_remote_creation(PEER, "held", **identity(attempt_id="one"))
            def block(conn):
                entered.set()
                assert release.wait(3)
            blocker = asyncio.create_task(store.submit(block))
            assert await asyncio.to_thread(entered.wait, 2)
            ack = asyncio.create_task(store.acknowledge_remote_creation(PEER, "held", **identity(attempt_id="one")))
            await asyncio.sleep(0)
            ack.cancel()
            await asyncio.sleep(0)
            assert not ack.done(), "caller cancellation escaped before its queued ACK settled"
            release.set()
            await blocker
            with pytest.raises(asyncio.CancelledError):
                await ack
            value = spawn_creation.witness((await store.reservations(include_expired=True))[0]["payload"])
            assert value["attempts"][0]["terminal"] is True
        finally:
            release.set()
            store.stop()
    asyncio.run(run())


def test_cancelled_release_observes_absence_again_after_terminal_ack(tmp_path):
    async def run():
        store = Store(str(tmp_path / "fresh.db"))
        store.start()
        tmux = PendingPeer()
        hosts = PeerHosts(tmux)
        ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=LOCAL, hosts=hosts),
                       tmux=tmux, hosts=hosts, owner_instance_id="owner")
        calls = []
        try:
            await reserved(store)
            assert await store.issue_remote_creation(PEER, "held", **identity(attempt_id="one"))
            assert (await store.cancel_reservation_fenced(PEER, "held", "request"))["status"] == "cancelled"
            original = tmux.session_state
            async def observe(name):
                calls.append(name)
                if len(calls) == 1:
                    # A previously read absence cannot cover a terminal ACK
                    # followed by a foreign pane appearing under the name.
                    assert await store.acknowledge_remote_creation(PEER, "held", **identity(attempt_id="one"))
                    tmux.live.add(name)
                    tmux.nonces[name] = "foreign"
                    return "gone"
                return await original(name)
            tmux.session_state = observe
            row = (await store.reservations(include_expired=True))[0]
            assert await ctl._cleanup_cancelled_spawn(row) == "deferred"
            assert len(calls) == 2 and tmux.kill_calls == 0 and "held" in tmux.live
            assert await ctl._cleanup_cancelled_spawn(row) == "deferred"
            assert tmux.kill_calls == 0
            tmux.live.remove("held")  # fixture-owned foreign control disappears
            assert await ctl._cleanup_cancelled_spawn(row) == "released"
            assert not await store.reservations(include_expired=True)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("cancel_first", [False, True])
def test_issuance_and_cancellation_serialize_without_late_registration(tmp_path, cancel_first):
    async def run():
        store = Store(str(tmp_path / "race.db"))
        store.start()
        try:
            await reserved(store)
            issue = lambda: store.issue_remote_creation(PEER, "held", **identity(attempt_id="racing"))
            cancel = lambda: store.cancel_reservation_fenced(PEER, "held", "request")
            calls = (cancel, issue) if cancel_first else (issue, cancel)
            results = await asyncio.gather(*(call() for call in calls))
            issued = results[1] if cancel_first else results[0]
            assert issued is (not cancel_first)
            row = (await store.reservations(include_expired=True))[0]
            value = spawn_creation.witness(row["payload"])
            assert len(value["attempts"]) == int(issued)
            snapshot = await store.cancelled_creation_snapshot(PEER, "held", **identity())
            assert (snapshot is not None) is cancel_first
        finally:
            store.stop()
    asyncio.run(run())


def test_cancelled_unresolved_creation_still_blocks_cli_maintenance(tmp_path, monkeypatch):
    from tests.test_provider_cli_maintenance import ready_daemon, rpc
    async def run():
        async with ready_daemon(tmp_path, monkeypatch) as (daemon, store, runtime, host, selected, row, request):
            assert await store.reserve_stream_id(selected.host, "cleanup-only", ttl_s=60,
                                                 request_id="cancelled", nonce="nonce", owner_instance_id="old")
            assert await store.record_spawn_intent(selected.host, "cleanup-only",
                                                   {spawn_creation.KEY: spawn_creation.initial()})
            assert await store.issue_remote_creation(selected.host, "cleanup-only", request_id="cancelled",
                                                      nonce="nonce", owner_instance_id="old", attempt_id="pending")
            assert (await store.cancel_reservation_fenced(selected.host, "cleanup-only", "cancelled"))["status"] == "cancelled"
            calls = []
            async def prohibited(*args, **kwargs):
                calls.append(args)
                raise AssertionError("unknown creation must fence physical CLI maintenance")
            runtime.executor.execute = prohibited
            reply = await rpc(daemon, "spawn_freeze", **request)
            assert reply["error_code"] == "provider_cli_maintenance_launch_pending"
            assert not calls
            assert await store.reservations(include_expired=True)
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["before_issue", "after_issue", "before_ack", "after_ack"])
def test_cancel_restart_at_creation_boundaries_keeps_truthful_obligation(tmp_path, boundary):
    async def run():
        tmux = PendingPeer()
        tmux.release.set()
        hosts = PeerHosts(tmux)
        database = str(tmp_path / "boundary.db")
        store = Store(database)
        store.start()
        ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=LOCAL, hosts=hosts),
                       tmux=tmux, hosts=hosts, owner_instance_id="old")
        entered, release = asyncio.Event(), asyncio.Event()
        method = "issue_remote_creation" if "issue" in boundary else "acknowledge_remote_creation"
        original = getattr(store, method)
        async def pause(*args, **kwargs):
            if boundary.startswith("before"):
                entered.set()
                await release.wait()
            result = await original(*args, **kwargs)
            if boundary.startswith("after"):
                entered.set()
                await release.wait()
            return result
        setattr(store, method, pause)
        caller = asyncio.create_task(ctl.spawn({"host": PEER, "session_name": "boundary", "request_id": "boundary-request",
                                               "command": "fixture-command", "objective": "Qualify creation cancellation boundary"}, LOCAL))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert (await ctl.spawn_cancel({"host": PEER, "target": "boundary-request"}, LOCAL))["state"] == "cancelled"
            background = tuple(ctl._background_spawns)
            for task in background:
                task.cancel()
            await asyncio.gather(*background, caller, return_exceptions=True)
            assert tmux.created == int("ack" in boundary)
            store.stop()
            store = Store(database)
            store.start()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=LOCAL, hosts=hosts),
                           tmux=tmux, hosts=hosts, owner_instance_id="new")
            result = await ctl.reconcile_spawn_intents(recurring=False)
            unknown = boundary in {"after_issue", "before_ack"}
            assert bool(await store.reservations(include_expired=True)) == unknown
            assert result["released"] == int(not unknown)
            assert "boundary" not in tmux.live
            assert await store.fetch_session(PEER, "boundary") is None
            assert (await store.get_spawn_outcome_by_request_id("boundary-request", host=PEER))["state"] == "cancelled"
        finally:
            release.set()
            if not caller.done():caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            if tmux.operation:await asyncio.gather(tmux.operation, return_exceptions=True)
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("ack_fails", [False, True])
def test_completed_remote_cancel_releases_only_when_ack_persisted(tmp_path, ack_fails):
    async def run():
        tmux = PendingPeer()
        tmux.release.set()
        hosts = PeerHosts(tmux)
        database = str(tmp_path / "completed.db")
        store = Store(database)
        store.start()
        entered, release = asyncio.Event(), asyncio.Event()
        ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=LOCAL, hosts=hosts), tmux=tmux, hosts=hosts, owner_instance_id="owner")
        original_bind = ctl._commit_pane_bound
        async def bind(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_bind(*args, **kwargs)
        ctl._commit_pane_bound = bind
        if ack_fails:
            async def fail_ack(*args, **kwargs):
                raise RuntimeError("fixture persistence unavailable")
            store.acknowledge_remote_creation = fail_ack
        try:
            task = asyncio.create_task(ctl.spawn({"host": PEER, "session_name": "completed", "request_id": "done-request", "command": "fixture-command", "objective": "Qualify completed remote creation cancellation"}, LOCAL))
            await asyncio.wait_for(entered.wait(), 5)
            assert (await ctl.spawn_cancel({"host": PEER, "target": "done-request"}, LOCAL))["state"] == "cancelled"
            release.set()
            assert (await asyncio.wait_for(task, 5))["state"] == "cancelled"
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            assert "completed" not in tmux.live and tmux.kill_calls == 1
            status = await ctl.spawn_status({"host": PEER, "target": "done-request"}, LOCAL)
            assert status["outcomes"][0]["state"] == "cancelled"
            if ack_fails:
                assert status["reservations"][0]["cleanup"]["state"] == "unknown"
            else:
                assert not status["reservations"]
            store.stop()
            store = Store(database)
            store.start()
            assert bool(await store.reservations(include_expired=True)) == ack_fails
        finally:
            release.set()
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            store.stop()
    asyncio.run(run())
