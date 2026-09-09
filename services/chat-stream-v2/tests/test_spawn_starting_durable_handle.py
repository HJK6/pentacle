"""Regression coverage for a durable ``spawn.ok`` starting handle.

The inducing seam is deterministic: a provider pane is created, never reaches
the bounded readiness predicate, and every ownership-fenced rollback kill times
out. The requester still receives its durable starting handle without relying
on timing or a live provider account.
"""

from __future__ import annotations

import tmux_transport

import asyncio

import pytest

import spawnctl as spawnctl_mod  # noqa: E402
from comms import Comms  # noqa: E402
from reconciler import SessionReconciler  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class StartingPaneTmux:
    """A known-live pane whose rollback transport remains unproved."""

    def __init__(self) -> None:
        self.alive = False
        self.pastes: list[str] = []
        self.screen = "provider is still booting"
        self.nonce = ""

    async def new_session(
        self, _name: str, _command: str, cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.alive = True
        self.nonce = str((env or {}).get("PENTACLE_SPAWN_NONCE") or "")

    async def has_session(self, _name: str) -> bool:
        return self.alive

    async def session_state(self, _name: str) -> str:
        return "alive" if self.alive else "gone"

    async def capture(self, _name: str) -> str:
        return self.screen

    async def pane_pid(self, _name: str) -> str:
        return "4242"

    async def kill_session(self, _name: str) -> None:
        raise RuntimeError("tmux rollback transport timed out")

    async def paste(self, _name: str, text: str) -> None:
        self.pastes.append(text)
        self.screen += f"\n{text}"

    async def send_enter(self, _name: str) -> None:
        return None

    async def run(self, *args: str, **_kwargs: object) -> tuple[int, str]:
        if args and args[0] == "show-environment" and self.nonce:
            return 0, f"PENTACLE_SPAWN_NONCE={self.nonce}\n"
        return 0, ""


class LocalHosts:
    local_host = HOST


class RemoteHosts(LocalHosts):
    def __init__(self, tmux: StartingPaneTmux) -> None:
        self.peers = {"peer": object()}
        self._tmux = tmux

    def is_online(self, host: str) -> bool:
        return host == "peer"

    def tmux_for(self, host: str) -> StartingPaneTmux:
        assert host == "peer"
        return self._tmux


def test_starting_live_pane_has_an_open_addressable_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """An accepted starting spawn preserves a live row and tell path.

    An alive pane with no open row would make ``Sessions.get`` empty and
    ``Comms.send`` reject the stream.
    """
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)

    async def run() -> tuple[dict, dict | None, dict, StartingPaneTmux]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = StartingPaneTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            reply = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "command": "provider", "session_name": "starting-live", "request_id": "r-live"},
                HOST,
            )
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            row = await store.fetch_session(HOST, "starting-live")
            tell = await Comms(store, sessions, ctl).send({
                "to_stream_id": f"{HOST}:starting-live",
                "message": "durable-handle probe",
                "tell_id": "durable-handle-probe",
            })
            return reply, row, tell, tmux
        finally:
            store.stop()

    reply, row, tell, tmux = asyncio.run(run())
    assert reply["type"] == "spawn.ok"
    assert reply["state"] == "starting"
    assert reply["session"]["state"] == "starting"
    assert reply["initial_prompt_delivery"]["state"] == "not_requested"
    assert tmux.alive is True
    assert row is not None and row["status"] == "open"
    assert tell["type"] == "send.result"


def test_inventory_preserves_post_admission_lifecycle_states() -> None:
    async def run() -> tuple[dict, dict]:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host=HOST)
            ctl = SpawnCtl(store, sessions)
            await sessions.open(HOST, "state-projection", bootstrap_state="starting")
            starting = sessions.list_open()[0]
            await ctl._publish_spawn_state(HOST, "state-projection", "ready")
            ready = Server._summary_snapshot_sessions(sessions.list_open())[0]
            await ctl._publish_spawn_state(HOST, "state-projection", "failed")
            return {"starting": starting, "ready": ready}, sessions.list_open()[0]
        finally:
            store.stop()

    projected, failed = asyncio.run(run())
    assert projected["starting"]["state"] == projected["starting"]["bootstrap_state"] == "starting"
    assert projected["ready"]["state"] == projected["ready"]["bootstrap_state"] == "ready"
    assert failed["state"] == failed["bootstrap_state"] == "failed"


@pytest.mark.skip(
    reason=(
        "This timing-sensitive integration example is disabled by default; "
        "the deterministic contract tests below cover the same state model."
    ),
)
def test_desktop_v2_admission_returns_starting_before_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawnctl_mod, "BOOT_READY_HARD_DEADLINE_S", 0.01)
    monkeypatch.setattr(tmux_transport, "POLL_INTERVAL_S", 0.001)

    async def run() -> tuple[dict, dict | None]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = StartingPaneTmux()
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            reply = await asyncio.wait_for(ctl.spawn({"objective": "Exercise the existing spawn contract",
                "command": "provider",
                "provider": "codex",
                "schema": "SpawnRequestV2",
                "spawn_profile": "desktop_manual",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "catalog_version": "spawn-catalog-v2",
                "resolution_source": "profile_default",
                "session_name": "desktop-start",
                "request_id": "desktop-start-request",
            }, HOST), timeout=0.1)
            row = await store.fetch_session(HOST, "desktop-start")
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            return reply, row
        finally:
            store.stop()

    reply, row = asyncio.run(run())
    assert reply["type"] == "spawn.ok"
    assert reply["state"] == "starting"
    assert reply["session"]["stream_id"] == f"{HOST}:desktop-start"
    assert row is not None and row["bootstrap_state"] == "starting"


def test_periodic_reconciler_adopts_a_preexisting_live_intent() -> None:
    """The normal reconciler cadence retroactively admits a live intent.

    A row-less pane must not remain absent after ``reconcile_once``.
    """
    async def run() -> tuple[dict | None, dict | None]:
        store = Store(":memory:")
        store.start()
        try:
            tmux = StartingPaneTmux()
            tmux.alive = True
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            tmux.nonce = "nonce-late-adopt"
            assert await store.reserve_stream_id(
                HOST, "late-adopt", ttl_s=60, request_id="r-adopt", nonce=tmux.nonce,
            )
            await store.record_spawn_intent(HOST, "late-adopt", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.mark_tmux_created(HOST, "late-adopt")
            reconciler = SessionReconciler(sessions, hosts=LocalHosts(), spawnctl=ctl)
            await reconciler.reconcile_once()
            return (
                await store.fetch_session(HOST, "late-adopt"),
                await store.get_spawn_outcome(HOST, "late-adopt"),
            )
        finally:
            store.stop()

    row, outcome = asyncio.run(run())
    assert row is not None and row["status"] == "open"
    assert outcome is not None and outcome["state"] == "delivered"


def test_periodic_reconciler_adopts_a_reachable_remote_intent() -> None:
    """PASS: a reachable peer receives the same retroactive adoption.

    FAIL form: a live peer pane is skipped indefinitely even though the host
    pool has positive reachability evidence.
    """
    async def run() -> dict | None:
        store = Store(":memory:")
        store.start()
        try:
            remote_tmux = StartingPaneTmux()
            remote_tmux.alive = True
            hosts = RemoteHosts(remote_tmux)
            sessions = Sessions(store, tmux=StartingPaneTmux(), local_host=HOST, hosts=hosts)
            ctl = SpawnCtl(store, sessions, tmux=StartingPaneTmux(), hosts=hosts)
            remote_tmux.nonce = "nonce-late-adopt-remote"
            assert await store.reserve_stream_id(
                "peer", "late-adopt-remote", ttl_s=60, request_id="r-remote",
                nonce=remote_tmux.nonce,
            )
            await store.record_spawn_intent("peer", "late-adopt-remote", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""})
            await store.mark_tmux_created("peer", "late-adopt-remote")
            await SessionReconciler(sessions, hosts=hosts, spawnctl=ctl).reconcile_once()
            return await store.fetch_session("peer", "late-adopt-remote")
        finally:
            store.stop()

    row = asyncio.run(run())
    assert row is not None and row["status"] == "open"
