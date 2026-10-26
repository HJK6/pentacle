"""Regression coverage for a durable ``spawn.ok`` starting handle.

The inducing seam is deterministic: a provider pane is created, never reaches
the bounded readiness predicate, and every ownership-fenced rollback kill times
out. The requester still receives its durable starting handle without relying
on timing or a live provider account.
"""

from __future__ import annotations

import tmux_transport

import asyncio
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import prockill  # noqa: E402
import spawnctl as spawnctl_mod  # noqa: E402
from _shared import spawn_profiles  # noqa: E402
from comms import Comms  # noqa: E402
from reconciler import SessionReconciler  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402
from prockill import canonical_start  # noqa: E402
from tests.test_codex_boot_ready import _pane as codex_pane  # noqa: E402

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


class AdmissionBarrierTmux(StartingPaneTmux):
    """Holds the provider-readiness capture of one pane until the test releases it."""

    def __init__(self, target: str) -> None:
        super().__init__()
        self.target = target
        self.readiness_entered = asyncio.Event()
        self.readiness_release = asyncio.Event()

    async def capture(self, name: str) -> str:
        if name != self.target:
            return await super().capture(name)
        self.readiness_entered.set()
        await self.readiness_release.wait()
        return codex_pane("› explain this codebase")


async def _first(**waits: object) -> str:
    """Name the first of several awaitables to finish; caller-owned futures stay pending."""
    tasks = {asyncio.ensure_future(aw): name for name, aw in waits.items()}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        if not asyncio.isfuture(waits[tasks[task]]):
            task.cancel()
    return tasks[next(iter(done))]


def test_desktop_v2_admission_returns_starting_before_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A desktop V2 spawn is admitted after its committed row and before readiness.

    Event barriers, not a deadline, order the three stages: the real session-row
    commit is held, then the real Codex readiness poll is held. An earlier 0.1 s
    ``wait_for`` timed validation, pane binding and identity lookup as well, and
    its fixture carried a stale ``profile_default`` tuple.
    """
    defaults = tmp_path / "spawn_defaults.json"
    shutil.copyfile(spawn_profiles.SPAWN_DEFAULTS_PATH, defaults)
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", defaults)
    spawn_profiles.load_spawn_config.cache_clear()
    request = {
        "objective": "Exercise the existing spawn contract",
        "command": "provider",
        **spawn_profiles.resolve_spawn(provider="codex", spawn_profile="desktop_manual", host=HOST),
        "session_name": "desktop-start",
        "request_id": "desktop-start-request",
    }
    assert request["resolution_source"] == "profile_default"

    real_process_record = prockill.process_record

    async def pane_record(pid: str) -> dict | None:
        if str(pid) != "4242":
            return await real_process_record(pid)
        return {"pid": 4242, "uid": os.getuid(), "command": "provider",
                "start_id": canonical_start("Mon Oct 26 15:00:00 2026")}

    monkeypatch.setattr(prockill, "process_record", pane_record)
    db_path = tmp_path / "admission.db"

    async def run() -> None:
        store = Store(str(db_path))
        store.start()
        tmux = AdmissionBarrierTmux("desktop-start")
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        ctl = SpawnCtl(store, sessions, tmux=tmux)
        row_entered, row_release = asyncio.Event(), asyncio.Event()
        real_open_session = store.open_session

        async def gated_open_session(host: str, name: str, *args: object, **kwargs: object) -> dict:
            if (host, name) == (HOST, "desktop-start"):
                row_entered.set()
                await row_release.wait()
            return await real_open_session(host, name, *args, **kwargs)

        monkeypatch.setattr(store, "open_session", gated_open_session)
        admissions: list[asyncio.Future] = []
        real_spawn_fenced = ctl._spawn_fenced

        async def observed_spawn_fenced(*args: object, admission: asyncio.Future | None = None, **kwargs: object) -> dict:
            admissions.append(admission)
            return await real_spawn_fenced(*args, admission=admission, **kwargs)

        monkeypatch.setattr(ctl, "_spawn_fenced", observed_spawn_fenced)
        request_task = asyncio.create_task(ctl.spawn(request, HOST))
        try:
            # Stage 1: pane created and bound, session row held. Not yet admitted.
            assert await _first(row=row_entered.wait(), reply=request_task) == "row"
            assert len(admissions) == 1 and admissions[0] is not None
            assert not admissions[0].done() and not request_task.done()
            assert await store.fetch_session(HOST, "desktop-start") is None
            reservation = [r for r in await store.reservations(include_expired=True)
                           if (r["host"], r["session_name"]) == (HOST, "desktop-start")]
            assert len(reservation) == 1
            assert reservation[0]["request_id"] == "desktop-start-request"
            assert reservation[0]["nonce"] == tmux.nonce != ""

            # Stage 2: row committed, readiness poll held. Admitted as starting.
            row_release.set()
            background = asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            assert await _first(readiness=tmux.readiness_entered.wait(), background=background) == "readiness"
            assert admissions[0].done() and admissions[0].result()["state"] == "starting"
            reply = await request_task
            assert reply["type"] == "spawn.ok" and reply["state"] == "starting"
            assert reply["request_id"] == "desktop-start-request"
            assert reply["stream_id"] == reply["session"]["stream_id"] == f"{HOST}:desktop-start"
            assert reply["session"]["state"] == "starting"
            live = sessions.get(f"{HOST}:desktop-start")
            assert live is not None and live["status"] == "open" and live["bootstrap_state"] == "starting"
            fetched = await store.fetch_session(HOST, "desktop-start")
            assert fetched is not None and fetched["status"] == "open"
            with sqlite3.connect(db_path) as conn:
                committed = conn.execute(
                    "SELECT status, bootstrap_state FROM sessions WHERE host=? AND session_name=?",
                    (HOST, "desktop-start"),
                ).fetchone()
                generation = conn.execute(
                    "SELECT generation FROM v2_session_generations WHERE host=? AND session_name=?",
                    (HOST, "desktop-start"),
                ).fetchone()
            assert committed == ("open", "starting")
            assert generation is not None and generation[0] == reply["session"]["session_generation"]
            assert not background.done()
            assert await store.get_spawn_outcome(HOST, "desktop-start") is None

            # Stage 3: readiness released. The same background spawn finishes ready.
            tmux.readiness_release.set()
            await background
            outcome = await store.get_spawn_outcome(HOST, "desktop-start")
            assert outcome is not None and outcome["state"] == "delivered"
            assert sessions.get(f"{HOST}:desktop-start")["bootstrap_state"] == "ready"
            assert not ctl._background_spawns
        finally:
            row_release.set()
            tmux.readiness_release.set()
            owned = [request_task, *ctl._background_spawns]
            for task in owned:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
            store.stop()

    try:
        asyncio.run(run())
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


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
