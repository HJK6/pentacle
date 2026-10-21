"""Per-host Codex boot admission: FIFO, bounded waits, and no permit leaks."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import spawnctl as spawnctl_mod
from inventory import InventoryEmitter
from server import Server
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from _shared import spawn_profiles
from _shared.spawn_profiles import SpawnProfileError, boot_limits, catalog


def test_daemon_stats_exposes_zero_and_queued_boot_depth() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            ctl = SpawnCtl(store, sessions)
            semaphore = ctl._codex_boot_semaphore("hosta", 1)
            await semaphore.acquire()
            waiter = asyncio.create_task(semaphore.acquire())
            await asyncio.sleep(0)
            server = Server(spawnctl=ctl, sessions=sessions, local_host="hosta")
            assert (await server._on_daemon_stats({}))["stats"]["boot_queue_depth"] == {"hosta": 1}
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            semaphore.release()
            assert (await server._on_daemon_stats({}))["stats"]["boot_queue_depth"] == {"hosta": 0}
        finally:
            store.stop()

    asyncio.run(run())


def test_slow_queued_write_does_not_delay_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    class StoreDouble:
        def __init__(self) -> None:
            self.queued_written = asyncio.Event()
            self.release_queued_write = asyncio.Event()

        async def set_spawn_outcome(self, _host, name, state, **_kwargs) -> None:
            if name == "slow" and state == "queued":
                self.queued_written.set()
                await self.release_queued_write.wait()

    class SessionsDouble:
        local_host = "hosta"

        def get(self, _stream_id: str) -> dict:
            return {}

    async def run() -> None:
        store = StoreDouble()
        ctl = SpawnCtl(store, SessionsDouble())
        monkeypatch.setattr(spawnctl_mod, "boot_limits", lambda _host: (2, 1))
        monkeypatch.setattr(ctl, "_publish_spawn_state", lambda *_args, **_kwargs: _done())
        semaphore = ctl._codex_boot_semaphore("hosta", 2)
        await semaphore.acquire()
        await semaphore.acquire()
        slow = asyncio.create_task(ctl._acquire_codex_boot_permit(
            "hosta", "slow", "slow-r", {}, idempotency_key="", payload_hash="", admission=None,
        ))
        await store.queued_written.wait()
        semaphore.release()
        semaphore.release()
        fast = await asyncio.wait_for(ctl._acquire_codex_boot_permit(
            "hosta", "fast", "fast-r", {}, idempotency_key="", payload_hash="", admission=None,
        ), 0.1)
        assert not slow.done()
        fast.release()
        store.release_queued_write.set()
        (await slow).release()

    async def _done() -> None:
        return None

    asyncio.run(run())
def test_fifo_cap_and_per_host_independence() -> None:
    async def run() -> None:
        ctl = SpawnCtl(None, None)
        admitted: list[str] = []

        async def acquire(host: str, request_id: str):
            semaphore = ctl._codex_boot_semaphore(host, 1)
            await semaphore.acquire()
            admitted.append(request_id)
            return semaphore

        first = await acquire("hostb", "a1")
        second_task = asyncio.create_task(acquire("hostb", "a2"))
        third_task = asyncio.create_task(acquire("hostb", "a3"))
        other_host = await acquire("hosta", "b1")
        await asyncio.sleep(0)

        assert admitted == ["a1", "b1"]
        assert ctl.boot_queue_depths()["hostb"] == 2

        first.release()
        second = await second_task
        assert admitted == ["a1", "b1", "a2"]
        assert ctl.boot_queue_depths()["hostb"] == 1

        second.release()
        third = await third_task
        assert admitted == ["a1", "b1", "a2", "a3"]
        third.release()
        other_host.release()
        assert ctl.boot_queue_depths() == {"hostb": 0, "hosta": 0}

    asyncio.run(run())


def test_queue_timeout_and_cancellation_do_not_leak_a_permit() -> None:
    async def run() -> None:
        ctl = SpawnCtl(None, None)
        semaphore = ctl._codex_boot_semaphore("hostb", 1)
        await semaphore.acquire()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(semaphore.acquire(), 0.01)
        assert ctl.boot_queue_depths() == {"hostb": 0}

        blocked = asyncio.create_task(semaphore.acquire())
        await asyncio.sleep(0)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert ctl.boot_queue_depths() == {"hostb": 0}

        semaphore.release()
        await semaphore.acquire()
        semaphore.release()
        assert ctl.boot_queue_depths() == {"hostb": 0}

    asyncio.run(run())


def test_cancellation_during_fifo_admission_releases_the_transferred_permit() -> None:
    """A cancellation racing FIFO admission must not strand the newly granted slot."""

    async def run() -> None:
        ctl = SpawnCtl(None, None)
        semaphore = ctl._codex_boot_semaphore("hosta", 1)
        await semaphore.acquire()
        waiter = asyncio.create_task(semaphore.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        semaphore.release()
        await semaphore.acquire()
        semaphore.release()
        assert ctl.boot_queue_depths() == {"hosta": 0}

    asyncio.run(run())


def test_boot_limits_are_catalogued_and_positive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    policy = {
        "schema_version": 1,
        "max_concurrent_boots": 3,
        "spawn_queue_timeout_seconds": 180,
        "providers": {
            "claude": {"model": "claude-opus-4-8", "effort": "high"},
            "codex": {"model": "gpt-5.6-sol", "effort": "high"},
        },
        "host_overrides": {
            "hostb": {"max_concurrent_boots": 2},
            "hosta": {"max_concurrent_boots": 8},
        },
        "profiles": {
            "agent_orch": {"handoff": {
                "inheritance": "source_effective",
                "missing_effective": "reject",
                "tuple_change": "warn_and_proceed",
                "confirmation_flag": "--confirm-model-change",
                "confirmation_flag_effect": "suppress_warning",
            }},
        },
    }
    path = tmp_path / "spawn_defaults.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(spawn_profiles, "SPAWN_DEFAULTS_PATH", path)
    spawn_profiles.load_spawn_config.cache_clear()
    try:
        assert boot_limits() == (3, 180)
        assert boot_limits("hostb") == (2, 180)
        assert boot_limits("hosta") == (8, 180)
        assert catalog()["spawn_defaults"] == {
            "schema_version": 1,
            "max_concurrent_boots": 3,
            "spawn_queue_timeout_seconds": 180,
            "providers": policy["providers"],
            "host_overrides": policy["host_overrides"],
            "profiles": policy["profiles"],
        }

        policy["max_concurrent_boots"] = 0
        path.write_text(json.dumps(policy), encoding="utf-8")
        spawn_profiles.load_spawn_config.cache_clear()
        with pytest.raises(SpawnProfileError, match="positive integer"):
            boot_limits()
    finally:
        spawn_profiles.load_spawn_config.cache_clear()


def test_restart_evicts_an_unadmitted_queue_handle() -> None:
    class NeverTouchedTmux:
        async def session_state(self, _name: str) -> str:  # pragma: no cover - must not be called
            raise AssertionError("queued work must be evicted before tmux reconciliation")

    async def run() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            assert await store.reserve_stream_id(
                "hostb", "v2-queued", ttl_s=180, request_id="queued-request",
            )
            await store.record_spawn_intent(
                "hostb", "v2-queued", {"open_fields": {"objective": "Exercise interrupted spawn adoption", }, "brief": ""},
            )
            await store.set_spawn_outcome(
                "hostb", "v2-queued", "queued", request_id="queued-request",
                delivery_receipt={
                    "state": "queued",
                    "queue_handle": {
                        "request_id": "queued-request", "host": "hostb",
                    },
                },
            )
            sessions = Sessions(store, tmux=NeverTouchedTmux(), local_host="hostb")
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            sessions.set_inventory_emitter(InventoryEmitter(sessions, broadcast, min_interval_s=0))
            await sessions.open(
                "hostb", "v2-queued", bootstrap_state="queued", fence="queued-request",
            )
            ctl = SpawnCtl(store, sessions, tmux=NeverTouchedTmux())
            assert await ctl.reconcile_spawn_intents() == {"adopted": 0, "released": 1}
            return await ctl.await_spawn({"spawn_request_id": "queued-request"}), frames
        finally:
            store.stop()

    reply, frames = asyncio.run(run())
    assert reply["type"] == "await_spawn.error"
    assert reply["error_code"] == "spawn_queue_evicted"
    assert [
        row.get("state") for frame in frames for row in frame["sessions"]
        if row["session_name"] == "v2-queued"
    ] == ["failed"]


def test_spawn_returns_a_durable_queue_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IdleTmux:
        async def has_session(self, _name: str) -> bool:
            return False

    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdleTmux()
            sessions = Sessions(store, tmux=tmux, local_host="hostb")
            inventory: list[dict] = []

            async def broadcast(frame: dict) -> None:
                inventory.append(frame)

            sessions.set_inventory_emitter(InventoryEmitter(sessions, broadcast, min_interval_s=0))
            ctl = SpawnCtl(store, sessions, tmux=tmux)
            gates = {name: asyncio.Event() for name in ("first", "second", "third")}
            started: list[str] = []

            async def resolve(*_args: object, **_kwargs: object):
                return "provider", {"resolved_launch_tuple": {"provider": "codex"}}, {
                    "provider": "codex", "effective_model": "gpt-5.6-sol",
                    "effective_effort": "high",
                }

            async def fenced(host, name, request_id, *_args, **_kwargs):
                started.append(name)
                await gates[name].wait()
                await store.set_spawn_outcome(
                    host, name, "delivered", request_id=request_id,
                    delivery_receipt={"state": "delivered"},
                )
                await ctl._publish_spawn_state(host, name, "ready")
                return {"type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}", "session": {}}

            monkeypatch.setattr(ctl, "_resolve_launch", resolve)
            monkeypatch.setattr(ctl, "_spawn_fenced", fenced)
            monkeypatch.setattr(ctl, "_clear_auth_context_marker", lambda *_args: _true())

            first = asyncio.create_task(ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "first", "request_id": "r1"}, "hostb",
            ))
            while started != ["first"]:
                await asyncio.sleep(0)
            second = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "second", "request_id": "r2"}, "hostb",
            )
            third = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "third", "request_id": "r3"}, "hostb",
            )
            assert second["type"] == third["type"] == "spawn.ok"
            assert second["state"] == third["state"] == "queued"
            assert second["queue_handle"] == {"request_id": "r2", "host": "hostb"}
            assert third["queue_handle"] == {"request_id": "r3", "host": "hostb"}
            assert (await store.fetch_session("hostb", "second"))["bootstrap_state"] == "queued"

            queued = await ctl.await_spawn({"spawn_request_id": "r3"})
            assert queued["type"] == "await_spawn.ok"
            assert queued["state"] == "queued"
            assert queued["queue_handle"] == {"request_id": "r3", "host": "hostb"}

            gates["first"].set()
            await first
            while started != ["first", "second"]:
                await asyncio.sleep(0)
            updated = await ctl.await_spawn({"spawn_request_id": "r3"})
            assert updated["queue_handle"] == {"request_id": "r3", "host": "hostb"}

            gates["second"].set()
            while started != ["first", "second", "third"]:
                await asyncio.sleep(0)
            gates["third"].set()
            while ctl._background_spawns:
                await asyncio.sleep(0)
            states = [
                row.get("state") for frame in inventory for row in frame["sessions"]
                if row["session_name"] == "third"
            ]
            assert states[-3:] == ["queued", "starting", "ready"]
            assert ctl.boot_queue_depths()["hostb"] == 0
        finally:
            store.stop()

    async def _true() -> bool:
        return True

    monkeypatch.setattr(spawnctl_mod, "boot_limits", lambda _host: (1, 1))
    asyncio.run(run())


def test_preserved_boot_binding_indeterminate_exit_releases_the_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live pane kept for reconciliation must not continue consuming boot capacity."""

    class IdleTmux:
        async def has_session(self, _name: str) -> bool:
            return False

    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = IdleTmux()
            ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host="hosta"), tmux=tmux)

            async def resolve(*_args: object, **_kwargs: object):
                return "provider", {"resolved_launch_tuple": {"provider": "codex"}}, {
                    "provider": "codex", "effective_model": "gpt-5.6-sol",
                    "effective_effort": "high",
                }

            async def fenced(host, name, request_id, *_args, **_kwargs):
                if name == "preserved":
                    return {
                        "type": "spawn.indeterminate",
                        "ok": False,
                        "reason": "boot_binding_indeterminate",
                        "stream_id": f"{host}:{name}",
                    }
                return {"type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}", "session": {}}

            monkeypatch.setattr(ctl, "_resolve_launch", resolve)
            monkeypatch.setattr(ctl, "_spawn_fenced", fenced)
            monkeypatch.setattr(ctl, "_clear_auth_context_marker", lambda *_args: _true())

            preserved = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "preserved", "request_id": "preserved-r"}, "hosta",
            )
            assert preserved["type"] == "spawn.indeterminate"
            assert ctl.boot_queue_depths()["hosta"] == 0

            next_reply = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "next", "request_id": "next-r"}, "hosta",
            )
            assert next_reply["type"] == "spawn.ok"
        finally:
            store.stop()

    async def _true() -> bool:
        return True

    monkeypatch.setattr(spawnctl_mod, "boot_limits", lambda _host: (1, 1))
    asyncio.run(run())


def test_queued_spawn_timeout_is_terminal_and_releases_its_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IdleTmux:
        async def has_session(self, _name: str) -> bool:
            return False

    async def run() -> None:
        store = Store(":memory:")
        store.start()
        # Declared before `try` so the failure-safe teardown below can always
        # unblock the fake spawn and drain, whichever assertion raised first.
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        first: asyncio.Task | None = None
        ctl: SpawnCtl | None = None
        leaked: set[asyncio.Task] = set()
        try:
            tmux = IdleTmux()
            sessions = Sessions(store, tmux=tmux, local_host="hosta")
            inventory: list[dict] = []

            async def broadcast(frame: dict) -> None:
                inventory.append(frame)

            sessions.set_inventory_emitter(InventoryEmitter(sessions, broadcast, min_interval_s=0))
            ctl = SpawnCtl(store, sessions, tmux=tmux)

            async def resolve(*_args: object, **_kwargs: object):
                return "provider", {"resolved_launch_tuple": {"provider": "codex"}}, {
                    "provider": "codex", "effective_model": "gpt-5.6-sol",
                    "effective_effort": "high",
                }

            async def fenced(host, name, request_id, *_args, **_kwargs):
                if name == "first":
                    first_started.set()
                    await release_first.wait()
                await store.set_spawn_outcome(
                    host, name, "delivered", request_id=request_id,
                    delivery_receipt={"state": "delivered"},
                )
                return {"type": "spawn.ok", "ok": True, "stream_id": f"{host}:{name}", "session": {}}

            monkeypatch.setattr(ctl, "_resolve_launch", resolve)
            monkeypatch.setattr(ctl, "_spawn_fenced", fenced)
            monkeypatch.setattr(ctl, "_clear_auth_context_marker", lambda *_args: _true())

            first = asyncio.create_task(ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "first", "request_id": "first-r"}, "hosta",
            ))
            await first_started.wait()
            obligations_before = set(ctl._background_spawns)
            queued = await ctl.spawn(
                {"objective": "Exercise the existing spawn contract", "provider": "codex", "session_name": "expired", "request_id": "expired-r"}, "hosta",
            )
            assert queued["type"] == "spawn.ok"
            assert queued["state"] == "queued"

            # The queued reply returns early; the spawn obligation keeps running
            # as a background task that, on queue timeout, writes the `failed`
            # outcome, THEN publishes `failed` to inventory, THEN closes the row,
            # THEN (in its `finally`) releases the reservation. `await_spawn`
            # reports the terminal error as soon as the outcome row exists, i.e.
            # BEFORE those later cleanup steps, so it is not a cleanup barrier.
            # Wait for the obligation itself to finish before asserting on
            # reservation / inventory / closed state. If the obligation already
            # finished (its done-callback dropped it from the set) there is
            # nothing to wait for and the cleanup below is already complete.
            expired_obligation = ctl._background_spawns - obligations_before
            assert len(expired_obligation) <= 1, expired_obligation
            if expired_obligation:
                finished, _ = await asyncio.wait(expired_obligation, timeout=10)
                assert finished == expired_obligation, "queued spawn obligation did not finish within 10s"
            outcome = await ctl.await_spawn({"spawn_request_id": "expired-r"})
            assert outcome["type"] == "await_spawn.error", outcome
            assert outcome["error_code"] == "spawn_queue_timeout"
            assert all(row["session_name"] != "expired" for row in await store.reservations())
            states = [
                row.get("state") for frame in inventory for row in frame["sessions"]
                if row["session_name"] == "expired"
            ]
            assert states == ["queued", "failed"]
            assert (await store.fetch_session("hosta", "expired"))["status"] == "closed"

            release_first.set()
            await first
        finally:
            # Failure-safe teardown. `ctl.spawn` returns its admitted/queued
            # reply early while `_spawn_impl` (incl. the request-id-fenced
            # reservation release) keeps running as a background task in
            # `ctl._background_spawns`; a late `release_stream_id_fenced` after
            # `store.stop()` raises `RuntimeError: store is not running` (the CI
            # boot-throttle flake), so drain against a LIVE store. The drain
            # must not depend on the body having reached `release_first.set()`:
            # an earlier assertion would otherwise leave `first` parked in the
            # fake spawn forever and turn the real AssertionError into a
            # pytest-timeout (the queued-spawn cleanup race). Always unblock the
            # fake first, bound the drain, and cancel whatever still remains so
            # the original exception (if any) propagates as itself.
            release_first.set()
            pending = {
                task for task in (first, *(ctl._background_spawns if ctl is not None else ()))
                if task is not None and not task.done()
            }
            if pending:
                _finished, leaked = await asyncio.wait(pending, timeout=5)
                for task in leaked:
                    task.cancel()
                if leaked:
                    await asyncio.wait(leaked, timeout=5)
            store.stop()
        assert not leaked, f"spawn tasks still pending at teardown: {leaked}"

    async def _true() -> bool:
        return True

    monkeypatch.setattr(spawnctl_mod, "boot_limits", lambda _host: (1, 0.01))
    asyncio.run(run())
