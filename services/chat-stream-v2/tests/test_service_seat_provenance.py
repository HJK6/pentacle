"""Service-seat boot lifecycle and tell provenance contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from comms import Comms
from ledger import Ledger
from main import _sync_service_seats
from mirror import Mirror
from reconciler import ReconcileConfig, SessionReconciler
from server import Server
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from uiverbs import UIVerbs


SEAT = "local:slack-bridge"
TARGET = "local:target"


class Pane:
    def __init__(self) -> None:
        self.pastes: list[str] = []

    async def has_session(self, name: str) -> bool:
        return name == "target"

    async def capture(self, name: str) -> str:
        return self.pastes[-1] if self.pastes else ""

    async def paste(self, name: str, text: str) -> None:
        self.pastes.append(text)

    async def run(self, *args, **kwargs):
        return 0, "target\t123\n"


class Hosts:
    local_host = "local"
    peers = {}

    def is_online(self, host: str) -> bool:
        return host == "local"


async def setup(path=":memory:"):
    store = Store(str(path))
    store.start()
    pane = Pane()
    sessions = Sessions(store, tmux=pane, local_host="local")
    await sessions.open("local", "target", provider="stub", pane_status="pane_alive")
    ctl = SpawnCtl(store, sessions, tmux=pane)
    comms = Comms(store, sessions, ctl)
    ledger = Ledger(store, sessions=sessions, comms=comms)
    return store, sessions, pane, comms, ledger


async def boot(sessions, ids=SEAT):
    await _sync_service_seats(sessions, ids)
    return await sessions.store.fetch_session("local", "slack-bridge")


async def grant(store, sessions):
    return await UIVerbs(store, sessions, SpawnCtl(store, sessions)).grant_token({"stream_id": SEAT})


async def tell(comms, ledger, *, tell_id="tell-1", auth=None, sender=SEAT, body="body"):
    msg = {"tell_id": tell_id, "from_stream_id": sender, "to_stream_id": TARGET, "message": body}
    if auth is not None:
        msg["_auth_context"] = auth
    await comms.tell(msg)
    result = await ledger.ledger_get({"tell_id": tell_id})
    return result["tell"]


class LocalPeer:
    remote_address = ("127.0.0.1", 54321)


async def wire_tell(store, sessions, comms, ledger, *, token=None, tell_id="tell-1"):
    server = Server(store=store, sessions=sessions, comms=comms, ledger=ledger, local_host="local")
    msg = {"type": "tell", "tell_id": tell_id, "from_stream_id": SEAT,
           "to_stream_id": TARGET, "message": "wire body"}
    if token is not None:
        msg["stream_token"] = token
    result = (await server._dispatch(json.dumps(msg), websocket=LocalPeer()))[0]
    assert result["type"] == "tell.ok"
    return (await ledger.ledger_get({"tell_id": tell_id}))["tell"]


def test_service_seat_row_opens_at_boot_and_survives_reconcile():
    async def run():
        store, sessions, pane, _, _ = await setup()
        try:
            row = await boot(sessions)
            assert (row["status"], row["provider"], row["visibility"], row["role"], row["pane_status"], row["objective"]) == ("open", "service", "hidden", "service", "paneless", "service seat")
            before = row["session_generation"]
            frames = []
            async def broadcast(frame):
                frames.append(frame)
            mirror = Mirror(store, sessions, pane, broadcast, local_host="local")
            await mirror.run_pass()
            assert SEAT not in mirror._obs
            assert not any(frame.get("type") == "session.died" and frame.get("stream_id") == SEAT for frame in frames)
            assert (await store.fetch_session("local", "slack-bridge"))["pane_status"] == "paneless"
            await SessionReconciler(sessions, Hosts(), config=ReconcileConfig(threshold_checks=1)).reconcile_once()
            row = await boot(sessions)
            assert row["session_generation"] == before
            assert row["status"] == "open"
        finally:
            store.stop()
    asyncio.run(run())


def test_service_seat_not_presumed_dead():
    async def run():
        store, sessions, _, _, _ = await setup()
        try:
            await boot(sessions)
            reconciler = SessionReconciler(sessions, Hosts(), config=ReconcileConfig(threshold_checks=1))
            for _ in range(2):
                result = await reconciler.reconcile_once()
                assert result["presumed_dead"] == 0
            row = await sessions.store.fetch_session("local", "slack-bridge")
            assert row["presumed_dead_at"] is None and row["status"] == "open"
        finally:
            store.stop()
    asyncio.run(run())


def test_service_seat_grant_token_once():
    async def run():
        store, sessions, _, _, _ = await setup()
        try:
            await boot(sessions)
            first = await grant(store, sessions)
            second = await grant(store, sessions)
            assert first["type"] == "grant_token.ok"
            assert second["error"] == "token_already_set"
        finally:
            store.stop()
    asyncio.run(run())


def test_service_seat_removed_from_env_closes_row_and_expires_token():
    async def run():
        store, sessions, _, _, _ = await setup()
        try:
            await boot(sessions)
            issued = await grant(store, sessions)
            token_hash = hashlib.sha256(issued["stream_token"].encode()).hexdigest()
            await boot(sessions, "")
            row = await store.fetch_session("local", "slack-bridge")
            assert row["status"] == "closed"
            assert await store.stream_id_for_token_hash(token_hash) is None
        finally:
            store.stop()
    asyncio.run(run())


def test_service_seat_readded_allows_one_new_grant():
    async def run():
        store, sessions, _, _, _ = await setup()
        try:
            await boot(sessions)
            old = await grant(store, sessions)
            await boot(sessions, "")
            await boot(sessions)
            fresh = await grant(store, sessions)
            assert fresh["type"] == "grant_token.ok"
            assert fresh["stream_token"] != old["stream_token"]
            assert (await grant(store, sessions))["error"] == "token_already_set"
        finally:
            store.stop()
    asyncio.run(run())


def test_service_seat_token_revocation_is_immediate():
    async def run():
        store, sessions, _, comms, ledger = await setup()
        try:
            await boot(sessions)
            issued = await grant(store, sessions)
            token_hash = hashlib.sha256(issued["stream_token"].encode()).hexdigest()
            assert await store.stream_id_for_token_hash(token_hash) == SEAT
            await store.update_session("local", "slack-bridge", token_hash=None, token_hash_version=None)
            assert await store.stream_id_for_token_hash(token_hash) is None
            row = await wire_tell(store, sessions, comms, ledger, token=issued["stream_token"])
            assert row["actor_stream_id"] == SEAT and row["actor_trusted"] is False
        finally:
            store.stop()
    asyncio.run(run())


def test_ledger_get_exposes_actor_trusted_false_for_tokenless_claim():
    async def run():
        store, sessions, _, comms, ledger = await setup()
        try:
            row = await wire_tell(store, sessions, comms, ledger)
            assert row["actor_stream_id"] == SEAT and row["actor_trusted"] is False
        finally:
            store.stop()
    asyncio.run(run())


def test_ledger_get_exposes_actor_trusted_true_for_verified_seat():
    async def run():
        store, sessions, _, comms, ledger = await setup()
        try:
            await boot(sessions)
            issued = await grant(store, sessions)
            row = await wire_tell(store, sessions, comms, ledger, token=issued["stream_token"])
            assert row["actor_stream_id"] == SEAT and row["actor_trusted"] is True
        finally:
            store.stop()
    asyncio.run(run())


def test_wrong_seat_loopback_tell_records_untrusted_actor():
    async def run():
        store, sessions, _, comms, ledger = await setup()
        try:
            await sessions.open("local", "other", provider="stub", pane_status="pane_alive")
            issued = await UIVerbs(store, sessions, SpawnCtl(store, sessions)).grant_token({"stream_id": "local:other"})
            row = await wire_tell(store, sessions, comms, ledger, token=issued["stream_token"])
            assert row["actor_stream_id"] == SEAT and row["actor_trusted"] is False
        finally:
            store.stop()
    asyncio.run(run())


def test_inbound_audit_exposes_actor_fields():
    async def run():
        store, sessions, _, comms, ledger = await setup()
        try:
            await boot(sessions)
            issued = await grant(store, sessions)
            row = await wire_tell(store, sessions, comms, ledger, token=issued["stream_token"])
            inbound = await ledger.inbound_audit({"stream_id": TARGET, "limit": 10})
            assert inbound["frames"][0]["actor_stream_id"] == row["actor_stream_id"]
            assert inbound["frames"][0]["actor_trusted"] is True
        finally:
            store.stop()
    asyncio.run(run())


def test_tell_ledger_text_is_wire_text():
    async def run():
        store, _, pane, comms, ledger = await setup()
        try:
            row = await tell(comms, ledger, body="wire text")
            assert row["text"] == pane.pastes[0]
        finally:
            store.stop()
    asyncio.run(run())


def test_tell_id_retry_after_handoff_is_conflict():
    async def run():
        store, sessions, _, comms, _ = await setup()
        try:
            await tell(comms, Ledger(store, sessions=sessions, comms=comms), body="same")
            await store.mark_closed("local", "target", closed_at="2026-10-20T00:00:00Z", pane_status="pane_dead")
            await sessions.refresh()
            await sessions.open("local", "successor", provider="stub", pane_status="pane_alive", handoff_from_stream_id=TARGET)
            with pytest.raises(VerbError) as exc:
                await comms.tell({"tell_id": "tell-1", "from_stream_id": SEAT, "to_stream_id": TARGET, "message": "same"})
            assert exc.value.code == "tell_id_conflict"
        finally:
            store.stop()
    asyncio.run(run())
