"""Operator intent closes an offline row; only remote evidence finishes its reap."""
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from presence import PresenceConfig, RemotePresence
from reconciler import SessionReconciler
from server import Server
from sessions import Sessions
from store import Store
from sessions import VerbError
from spawnctl import SpawnCtl


class Peer:
    local_host = "hosta"
    peers = {"hostb": object()}

    def __init__(self):
        self.online = False
        self.alive = True
        self.kills = 0

    def known(self, host):
        return host in {"hosta", "hostb"}

    def is_local(self, host):
        return host == self.local_host

    def is_online(self, host):
        return self.is_local(host) or self.online

    async def probe_once(self, host):
        return self.is_online(host)

    def tmux_for(self, host):
        return self

    async def session_state(self, name):
        return ("alive" if self.alive else "gone") if self.online else "unreachable"

    async def pane_identity(self, name):
        return {"pane_pid": "123", "pane_id": "%1", "session_name": name}

    async def kill_session(self, name):
        self.kills += 1
        self.alive = False

    async def run(self, *args, **kwargs):
        return (0, "v2-offline\t123\n") if self.alive else (1, "no server running")


async def build(path=":memory:"):
    store = Store(path)
    store.start()
    peer = Peer()
    sessions = Sessions(store, tmux=None, hosts=peer, local_host="hosta")
    row = await store.open_session("hostb", "v2-offline", pane_pid="123", pane_status="pane_alive")
    await sessions.refresh()
    server = Server(store=store, sessions=sessions, local_host="hosta")
    server.notify = AsyncMock()
    return store, peer, sessions, server, row


def frame(**fields):
    return {"stream_id": "hostb:v2-offline", "request_id": "offline-close-test",
            "_auth_context": {"operator_authenticated": True,
                              "operator_principal": "operator:test",
                              "connection_client": "desktop", "transport": "v2"}, **fields}


def test_operator_confirmation_closes_offline_row_and_inspect_exposes_intent():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            reply = await server._on_close(frame(operator_confirm=True))
            assert reply["type"] == "close.ok", reply
            assert reply["reap_status"] == "deferred_host_offline"
            closed = await store.fetch_session("hostb", "v2-offline")
            assert closed["close_kind"] == "operator_offline_close"
            assert closed["closed_at"] == closed["dead_open_closed_at"]
            assert closed["pane_status"] != "pane_dead"
            assert await store.list_sessions("open") == []
            assert sessions.get("hostb:v2-offline") is None
            inspected = await server._on_inspect_stream(frame(event_tail=0))
            assert inspected["deferred_reap"]["done_at"] is None
            assert inspected["deferred_reap"]["attempts"] == 0
            assert inspected["close_audit"]["closed_by"] == "operator:test"
            assert peer.kills == 0
            server.notify.create_internal_notification.assert_awaited_once()
        finally:
            store.stop()
    asyncio.run(run())


def test_without_confirmation_offline_close_still_refuses():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            reply = await server._on_close(frame(force=True))
            assert reply["type"] == "close.failed"
            assert reply["reason"] == "ssh_unreachable"
            assert (await store.fetch_session("hostb", "v2-offline"))["status"] == "open"
            assert peer.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_closed_offline_row_is_reaped_on_simulated_host_return():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await store.mark_closed("hostb", "v2-offline", closed_at="2026-09-10T00:00:00Z",
                                    pane_status="unknown", close_kind="operator_offline_close",
                                    expected_generation=row["session_generation"])
            await sessions.refresh()
            presence = RemotePresence(sessions, peer, config=PresenceConfig(list_timeout_s=0.1))
            reconciler = SessionReconciler(sessions, peer, presence=presence)
            await reconciler.reconcile_once()
            assert peer.kills == 0
            peer.online = True
            await reconciler.reconcile_once()
            assert peer.kills == 1
            deferred = await store.get_deferred_reap("hostb:v2-offline")
            assert deferred["done_at"]
            assert (await store.fetch_session("hostb", "v2-offline"))["status"] == "closed"
            await reconciler.reconcile_once()
            assert peer.kills == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_pending_intent_survives_restart_and_migration_is_idempotent(tmp_path):
    async def run():
        path = str(tmp_path / "sessions.db")
        store, peer, sessions, server, row = await build(path)
        await server._on_close(frame(operator_confirm=True))
        store.stop()
        for _ in range(2):
            store = Store(path)
            store.start()
            try:
                deferred = await store.get_deferred_reap("hostb:v2-offline")
                assert deferred["generation"] == row["session_generation"]
                assert deferred["attempts"] == 0
                assert await store.list_sessions("open") == []
                assert (await store.latest_close_audit("hostb:v2-offline"))["request_id"] == "offline-close-test"
            finally:
                store.stop()
    asyncio.run(run())


def test_failed_intent_insert_rolls_back_close_and_audit():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await store.submit(lambda conn: conn.execute(
                "CREATE TRIGGER reject_deferred BEFORE INSERT ON v2_deferred_reap "
                "BEGIN SELECT RAISE(ABORT, 'fixture disk failure'); END"
            ))
            with pytest.raises(sqlite3.IntegrityError, match="fixture disk failure"):
                await server._on_close(frame(operator_confirm=True))
            # Force a subsequent commit too: a queued failure must not leave
            # its half-close transaction available for another call to commit.
            await store.put("transaction-test", "committed")
            assert (await store.fetch_session("hostb", "v2-offline"))["status"] == "open"
            assert await store.latest_close_audit("hostb:v2-offline") is None
            assert await store.get_deferred_reap("hostb:v2-offline") is None
        finally:
            store.stop()
    asyncio.run(run())


def test_retries_are_capped_and_exhaustion_still_blocks_adoption(monkeypatch):
    monkeypatch.setattr("sessions.CLOSE_GRACEFUL_CONFIRM_S", 0)
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await server._on_close(frame(operator_confirm=True))
            # Offline passes consume no attempt budget.
            reconciler = SessionReconciler(sessions, peer)
            await reconciler.reconcile_once()
            assert (await store.get_deferred_reap("hostb:v2-offline"))["attempts"] == 0
            peer.online = True
            peer.kill_session = AsyncMock()  # command returns but pane survives
            for _ in range(7):
                await reconciler.reconcile_once()
            deferred = await store.get_deferred_reap("hostb:v2-offline")
            assert deferred["attempts"] == 5
            assert deferred["exhausted_at"] and deferred["done_at"] is None
            assert deferred["last_error"] == "pane_still_alive_after_kill"
            assert peer.kill_session.await_count == 5
            assert await store.open_session("hostb", "v2-offline") is None
            with pytest.raises(VerbError):
                await sessions.open("hostb", "v2-offline")
            spawn = object.__new__(SpawnCtl)
            spawn.store = store
            assert await spawn._adopt_interrupted_spawn("hostb", "v2-offline", "stale", {}, peer) == "deferred"
            assert (await store.fetch_session("hostb", "v2-offline"))["status"] == "closed"
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("state", ["gone", "unreachable", "replacement"])
def test_reap_requires_death_evidence_and_never_kills_replacement(state):
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await server._on_close(frame(operator_confirm=True))
            peer.online = True
            if state != "replacement":
                peer.session_state = AsyncMock(return_value=state)
            else:
                peer.pane_identity = AsyncMock(return_value={"pane_pid": "999", "pane_id": "%2"})
            await SessionReconciler(sessions, peer).reconcile_once()
            deferred = await store.get_deferred_reap("hostb:v2-offline")
            assert bool(deferred["done_at"]) == (state == "gone")
            assert peer.kills == 0
            if state == "replacement":
                assert deferred["last_error"] == "pane_identity_changed"
        finally:
            store.stop()
    asyncio.run(run())


def test_confirmation_does_not_grant_close_authority_or_bypass_generation():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            with pytest.raises(VerbError, match="close requires"):
                await server._on_close(frame(operator_confirm=True, _auth_context={}))
            reply = await server._on_close(frame(operator_confirm=True, expected_generation="stale"))
            assert reply["type"] == "close.already_closed"
            assert (await store.fetch_session("hostb", "v2-offline"))["status"] == "open"
            assert await store.get_deferred_reap("hostb:v2-offline") is None
            assert peer.kills == 0
        finally:
            store.stop()
    asyncio.run(run())


def test_repeated_confirmed_close_preserves_original_request_and_attempts():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await server._on_close(frame(operator_confirm=True))
            before = await store.get_deferred_reap("hostb:v2-offline")
            reply = await server._on_close(frame(operator_confirm=True, request_id="retry"))
            assert reply["type"] == "close.already_closed"
            assert reply["reap_status"] == "deferred_host_offline"
            assert await store.get_deferred_reap("hostb:v2-offline") == before
            assert (await store.latest_close_audit("hostb:v2-offline"))["request_id"] == "offline-close-test"
        finally:
            store.stop()
    asyncio.run(run())


def test_cli_wire_close_and_inspect_round_trip(monkeypatch):
    from agent_orch import wsclient

    async def run():
        store, peer, sessions, server, row = await build()
        class Wire:
            async def send(self, text):
                payload = json.loads(text)
                assert payload["operator_confirm"] is True
                self.reply = await server._on_close(frame(**payload))
                self.reply["request_id"] = payload["request_id"]

            async def recv(self):
                return json.dumps(self.reply)

            async def close(self):
                pass

        monkeypatch.setattr(wsclient, "_connect_rpc_ready", AsyncMock(return_value=Wire()))
        try:
            reply = await wsclient.close_once(None, "hostb:v2-offline", operator_confirm=True)
            assert reply["type"] == "close.ok"
            assert reply["reap_status"] == "deferred_host_offline"
            inspected = await server._on_inspect_stream(frame(event_tail=0))
            assert inspected["session"]["close_kind"] == "operator_offline_close"
            assert inspected["deferred_reap"]["done_at"] is None
        finally:
            store.stop()
    asyncio.run(run())


def test_reap_precedes_adoption_and_old_intent_stays_fenced_after_completion():
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await server._on_close(frame(operator_confirm=True))
            peer.online = True
            async def adopt(**kwargs):
                assert peer.kills == 1
                assert (await store.get_deferred_reap("hostb:v2-offline"))["done_at"]
                spawn = object.__new__(SpawnCtl)
                spawn.store = store
                intent = {"open_fields": {"session_generation": row["session_generation"]}}
                assert await spawn._adopt_interrupted_spawn("hostb", "v2-offline", "old", intent, peer) == "deferred"
            spawnctl = AsyncMock()
            spawnctl.reconcile_spawn_intents.side_effect = adopt
            await SessionReconciler(sessions, peer, spawnctl=spawnctl).reconcile_once()
            spawnctl.reconcile_spawn_intents.assert_awaited_once()
        finally:
            store.stop()
    asyncio.run(run())


def test_persisted_identity_lease_fences_replacement_on_later_attempt(monkeypatch):
    monkeypatch.setattr("sessions.CLOSE_GRACEFUL_CONFIRM_S", 0)
    async def run():
        store, peer, sessions, server, row = await build()
        try:
            await server._on_close(frame(operator_confirm=True))
            peer.online = True
            peer.kill_session = AsyncMock()
            await SessionReconciler(sessions, peer).reconcile_once()
            peer.pane_identity = AsyncMock(return_value={"pane_pid": "123", "pane_id": "%2"})
            # A new reconciler still reads the durable lease from attempt one.
            await SessionReconciler(sessions, peer).reconcile_once()
            assert peer.kill_session.await_count == 1
            assert (await store.get_deferred_reap("hostb:v2-offline"))["last_error"] == "pane_identity_changed"
        finally:
            store.stop()
    asyncio.run(run())
