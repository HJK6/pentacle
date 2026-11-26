"""A graceful drain never loses a committed spawn intent.

Journey: a public same-key spawn is held at a named boundary, the real
`drain_background_spawns` cancels its obligation, a distinct daemon instance
reconciles the Store, reads `spawn_status` for the original request id and
refires with the same key and payload.
Subject: `_spawn_impl` teardown and `Store.settle_spawn_claim`.
Faults: cancellation after the intent commits but before the writer returns;
a queued intent write behind a held Store worker; a second cancel or a Store
failure at the settle; a keyed claim interrupted after its commit; a cancel
inside ordinary exception cleanup; a same-owner replacement with a new nonce.
Controls: cancellation before intent submission and after the in-memory flag.
Runner: Python 3.13 and pytest, in-memory SQLite, the real Store worker and a
per-name tmux fake. Barriers only: no sleeps and no elapsed-time thresholds;
`asyncio.wait_for` limits are stuck detectors.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from sessions import Sessions
from spawnctl import SpawnCtl, VerbError
from store import Store

HOST = "localhost"
KEY = "settle-key"
PAYLOAD = {"objective": "Exercise the interrupted spawn contract", "command": "stub"}
GONE_REASON = "spawn_interrupted: daemon restarted before the session row was persisted"
EMPTY_REASON = "spawn_interrupted: daemon restarted before spawn intent was persisted"
STUCK_S = 30.0


class HostTmux:
    """One host's tmux namespace, shared by every daemon instance in a test."""

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.created = 0
        self.killed: list[str] = []

    async def has_session(self, name: str) -> bool:
        return name in self.live

    async def new_session(self, name: str, _command: str, cwd=None, env=None) -> None:
        self.live.add(name)
        self.created += 1

    async def capture(self, _name: str) -> str:
        return "READY"

    async def pane_pid(self, _name: str) -> str:
        return "1234"

    async def session_state(self, name: str) -> str:
        return "alive" if name in self.live else "gone"

    async def kill_session(self, name: str) -> None:
        self.killed.append(name)
        self.live.discard(name)


def new_ctl(store: Store, tmux: HostTmux, instance_id: str) -> SpawnCtl:
    ctl = SpawnCtl(store, Sessions(store, tmux=tmux, local_host=HOST), tmux=tmux)
    ctl.instance_id = instance_id
    return ctl


async def stuck(awaitable):
    return await asyncio.wait_for(awaitable, STUCK_S)


async def rows(store: Store, sql: str, *args: Any) -> list[dict[str, Any]]:
    return await store.submit(lambda c: [dict(r) for r in c.execute(sql, args).fetchall()])


async def reservations(store: Store) -> list[dict[str, Any]]:
    return await rows(store, "SELECT * FROM v2_stream_reservations")


async def open_rows(store: Store) -> list[dict[str, Any]]:
    return await rows(store, "SELECT host, session_name FROM sessions WHERE status='open'")


class Hold:
    """Acknowledge entry at one boundary, then hold until cancelled."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.released = asyncio.Event()
        self.cancelled = False
        self.observed: dict[str, Any] = {}

    async def wait(self) -> None:
        self.reached.set()
        try:
            await self.released.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def hold_intent(store: Store, hold: Hold, *, after_commit: bool) -> None:
    """Hold the target spawn's intent write before submission or after commit."""
    real = store.record_spawn_intent

    async def wrapper(host, name, payload, **kw):
        store.record_spawn_intent = real  # one shot: later writers run unheld
        if not after_commit:
            await hold.wait()
            return await real(host, name, payload, **kw)
        result = await real(host, name, payload, **kw)
        hold.observed["writer_result"] = result
        hold.observed["request_id"] = kw.get("request_id")
        hold.observed["nonce"] = kw.get("nonce")
        await hold.wait()
        return result

    store.record_spawn_intent = wrapper


def hold_after_flag(ctl: SpawnCtl, hold: Hold) -> None:
    async def held(*_a, **_k):
        await hold.wait()

    ctl._spawn_fenced = held  # type: ignore[method-assign]


async def drain_held_spawn(ctl: SpawnCtl, hold: Hold, msg: dict[str, Any]) -> dict[str, Any]:
    """Start a public spawn, wait for the barrier, drain, and settle the client."""
    client = asyncio.create_task(ctl.spawn(dict(msg), HOST))
    await stuck(hold.reached.wait())
    obligations = [t for t in ctl._background_spawns if not t.done()]
    assert len(obligations) == 1
    at_barrier = await reservations(ctl.store)
    drained = await stuck(ctl.drain_background_spawns())
    assert drained == {"cancelled": 1, "unfinished": 0}
    assert obligations[0].done() and obligations[0].cancelled()
    assert hold.cancelled
    client.cancel()
    await asyncio.gather(client, return_exceptions=True)
    return {"at_barrier": at_barrier, "drained": drained}


def spawn_msg(request_id: str) -> dict[str, Any]:
    return {**PAYLOAD, "idempotency_key": KEY, "request_id": request_id}


async def status(ctl: SpawnCtl, target: str) -> dict[str, Any]:
    return await ctl.spawn_status({"target": target}, HOST)


async def refire_then_replay(store: Store, tmux: HostTmux, ctl: SpawnCtl) -> None:
    """One fresh admission, then a same-key repeat that replays it."""
    created = tmux.created
    first = await stuck(ctl.spawn(spawn_msg("refire-1"), HOST))
    assert first["type"] == "spawn.ok" and first.get("state") != "starting", first
    assert tmux.created == created + 1
    repeat = await stuck(ctl.spawn(spawn_msg("refire-2"), HOST))
    assert repeat["stream_id"] == first["stream_id"]
    assert tmux.created == created + 1
    live = await open_rows(store)
    assert [f"{r['host']}:{r['session_name']}" for r in live] == [first["stream_id"]]
    assert tmux.live == {first["stream_id"].split(":", 1)[1]}


async def recover_retained(store: Store, tmux: HostTmux, original: str) -> None:
    """A distinct instance confirms the pane gone and records the failure."""
    recovery = new_ctl(store, tmux, "daemon-C")
    assert await stuck(recovery.reconcile_spawn_intents()) == {"adopted": 0, "released": 1}
    outcome = (await rows(store, "SELECT * FROM v2_spawn_outcomes WHERE request_id=?", original))[0]
    assert outcome["state"] == "failed"
    assert outcome["reason"] == GONE_REASON
    found = await status(recovery, original)
    assert found["found"] is True
    assert [(o["request_id"], o["state"], o["reason"], o["error_code"]) for o in found["outcomes"]] == [
        (original, "failed", "spawn_interrupted", "spawn_interrupted")
    ]
    assert found["reservations"] == []
    assert await reservations(store) == []


# -- Post-commit barrier (primary RED) and its two controls ---------------------


def test_cancel_after_commit_before_writer_returns_retains_the_intent() -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")
            hold = Hold()
            hold_intent(store, hold, after_commit=True)
            seen = await drain_held_spawn(ctl, hold, spawn_msg("orig"))
            # The writer committed and returned positively before the hold.
            assert hold.observed["writer_result"] is True
            assert hold.observed["request_id"] == "orig"
            [held] = seen["at_barrier"]
            assert held["payload"] is not None and held["owner_instance_id"] == "daemon-A"
            assert tmux.created == 0
            # Retention is the primary assertion: exact row, payload, owner cleared.
            [kept] = await reservations(store)
            assert (kept["host"], kept["session_name"], kept["request_id"]) == (
                held["host"], held["session_name"], "orig")
            assert kept["nonce"] == hold.observed["nonce"] == held["nonce"]
            assert kept["payload"] == held["payload"]
            assert kept["owner_instance_id"] is None
            assert await rows(store, "SELECT * FROM v2_spawn_outcomes") == []
            assert await open_rows(store) == []
            # A healthy refire while retained returns the original handle.
            other = new_ctl(store, tmux, "daemon-B")
            again = await stuck(other.spawn(spawn_msg("while-retained"), HOST))
            assert again["state"] == "starting"
            assert again["stream_id"] == f"{HOST}:{held['session_name']}"
            assert "duplicate_in_flight_same_idempotency_key" in str(again)
            assert tmux.created == 0
            await recover_retained(store, tmux, "orig")
            await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-D"))
        finally:
            store.stop()

    asyncio.run(go())


def test_control_cancel_before_intent_submission_releases_the_empty_claim() -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")
            hold = Hold()
            hold_intent(store, hold, after_commit=False)
            seen = await drain_held_spawn(ctl, hold, spawn_msg("orig"))
            [held] = seen["at_barrier"]
            assert held["payload"] is None and held["request_id"] == "orig"
            assert await reservations(store) == []
            assert await rows(store, "SELECT * FROM v2_spawn_outcomes") == []
            assert tmux.created == 0
            other = new_ctl(store, tmux, "daemon-B")
            assert await stuck(other.reconcile_spawn_intents()) == {"adopted": 0, "released": 0}
            assert (await status(other, "orig"))["found"] is False
            await refire_then_replay(store, tmux, other)
        finally:
            store.stop()

    asyncio.run(go())


def test_control_cancel_after_flag_retains_the_intent() -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")
            hold = Hold()
            hold_after_flag(ctl, hold)
            seen = await drain_held_spawn(ctl, hold, spawn_msg("orig"))
            [held] = seen["at_barrier"]
            assert held["payload"] is not None
            [kept] = await reservations(store)
            assert kept["payload"] == held["payload"] and kept["owner_instance_id"] is None
            assert tmux.created == 0
            await recover_retained(store, tmux, "orig")
            await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-D"))
        finally:
            store.stop()

    asyncio.run(go())


# -- Held Store worker with the intent write queued -----------------------------


class WorkerHold:
    """Occupy the single Store worker so later submissions queue behind it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = threading.Event()
        self.task: asyncio.Future | None = None

    async def start(self, store: Store) -> None:
        loop = asyncio.get_running_loop()

        def block(_conn) -> None:
            loop.call_soon_threadsafe(self.started.set)
            self.gate.wait(STUCK_S)  # stuck detector only

        self.task = asyncio.ensure_future(store.submit(block))
        await stuck(self.started.wait())

    async def release(self) -> None:
        self.gate.set()
        if self.task is not None:
            await stuck(self.task)


async def rows_unavailable(store: Store) -> bool:
    """The held worker has not run the queued write yet (read off-worker)."""
    return store._queue.qsize() >= 1


def observe_settle(store: Store) -> asyncio.Event:
    """Independently observe the teardown reaching the settle operation."""
    reached = asyncio.Event()
    real = store.settle_spawn_claim

    async def wrapper(*a, **kw):
        reached.set()
        return await real(*a, **kw)

    store.settle_spawn_claim = wrapper
    return reached


@pytest.mark.parametrize("commits", [True, False], ids=["committed", "rolled-back"])
def test_queued_intent_write_is_settled_by_its_committed_outcome(commits: bool) -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        worker = WorkerHold()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")
            queued = asyncio.Event()
            waiter_cancelled = asyncio.Event()
            real = store.record_spawn_intent

            async def wrapper(host, name, payload, **kw):
                store.record_spawn_intent = real
                if not commits:
                    # A real in-transaction refusal: the writer rolls back.
                    kw["expected_spec_sources"] = ((f"{HOST}:absent-source", "changed"),)
                await worker.start(store)
                # Runs once this task is suspended on the already-queued write.
                asyncio.get_running_loop().call_soon(queued.set)
                try:
                    return await real(host, name, payload, **kw)
                except asyncio.CancelledError:
                    waiter_cancelled.set()
                    raise

            store.record_spawn_intent = wrapper
            settle_reached = observe_settle(store)
            client = asyncio.create_task(ctl.spawn(spawn_msg("orig"), HOST))
            await stuck(queued.wait())
            [obligation] = [t for t in ctl._background_spawns if not t.done()]
            drain = asyncio.create_task(ctl.drain_background_spawns())
            # Cancellation reaches the writer's waiter while the write is still
            # queued; teardown then needs the held worker, so release it before
            # waiting for the drain.
            await stuck(waiter_cancelled.wait())
            assert not obligation.done() and not drain.done()
            assert await rows_unavailable(store)
            await worker.release()
            assert await stuck(drain) == {"cancelled": 1, "unfinished": 0}
            assert settle_reached.is_set()
            assert obligation.cancelled()
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)
            kept = await reservations(store)
            assert tmux.created == 0 and await rows(store, "SELECT * FROM v2_spawn_outcomes") == []
            if commits:
                [row] = kept
                assert row["payload"] is not None and row["owner_instance_id"] is None
                await recover_retained(store, tmux, "orig")
            else:
                assert kept == []
                assert (await status(ctl, "orig"))["found"] is False
            await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-D"))
        finally:
            await worker.release()
            store.stop()

    asyncio.run(go())


# -- Second cancel or Store failure at the settle decision ----------------------


@pytest.mark.parametrize("fault", ["second-cancel", "store-failure"])
@pytest.mark.parametrize("timing", ["before-dispatch", "after-commit"])
@pytest.mark.parametrize("intent", ["committed", "empty"])
def test_interrupted_settle_never_falls_back(fault: str, timing: str, intent: str) -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")
            hold = Hold()
            hold_intent(store, hold, after_commit=intent == "committed")
            client = asyncio.create_task(ctl.spawn(spawn_msg("orig"), HOST))
            await stuck(hold.reached.wait())
            [obligation] = [t for t in ctl._background_spawns if not t.done()]
            [fixture] = await reservations(store)
            at_settle = Hold()
            settled: list[str] = []
            real = store.settle_spawn_claim

            async def interrupted(*a, **kw):
                store.settle_spawn_claim = real
                if timing == "after-commit":
                    settled.append(await real(*a, **kw))
                if fault == "store-failure":
                    at_settle.reached.set()
                    raise RuntimeError("store unavailable at settle")
                await at_settle.wait()

            store.settle_spawn_claim = interrupted
            drain = asyncio.create_task(ctl.drain_background_spawns())
            await stuck(at_settle.reached.wait())
            if fault == "second-cancel":
                obligation.cancel()
            assert await stuck(drain) == {"cancelled": 1, "unfinished": 0}
            # The original interruption is preserved either way.
            assert obligation.cancelled()
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)
            after = await reservations(store)
            assert tmux.created == 0 and tmux.killed == []
            assert await rows(store, "SELECT * FROM v2_spawn_outcomes") == []
            if timing == "before-dispatch":
                assert after == [fixture]
            elif intent == "committed":
                assert settled == ["retained"]
                assert after == [{**fixture, "owner_instance_id": None}]
            else:
                assert settled == ["released"] and after == []
            # A distinct instance recovers from whatever survived.
            recovery = new_ctl(store, tmux, "daemon-C")
            result = await stuck(recovery.reconcile_spawn_intents())
            if after:
                assert result == {"adopted": 0, "released": 1}
                [outcome] = await rows(store, "SELECT * FROM v2_spawn_outcomes")
                assert outcome["request_id"] == "orig" and outcome["state"] == "failed"
                assert outcome["reason"] == (GONE_REASON if intent == "committed" else EMPTY_REASON)
            assert await reservations(store) == []
            await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-D"))
        finally:
            store.stop()

    asyncio.run(go())


# -- Claim interrupted after its commit, and a cancel inside exception cleanup ---


@pytest.mark.parametrize("keyed", [True, False], ids=["keyed", "keyless"])
def test_claim_interrupted_after_commit_goes_through_the_settle(keyed: bool) -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")
            hold = Hold()
            method = "atomic_claim_or_replay" if keyed else "reserve_stream_id"
            real = getattr(store, method)

            async def wrapper(*a, **kw):
                setattr(store, method, real)
                result = await real(*a, **kw)
                hold.observed["result"] = result
                await hold.wait()
                return result

            setattr(store, method, wrapper)
            settle_reached = observe_settle(store)
            # Without a key or request id the spawn takes the keyless claim.
            msg = spawn_msg("orig") if keyed else {**PAYLOAD, "session_name": "plain"}
            seen = await drain_held_spawn(ctl, hold, msg)
            claimed = hold.observed["result"]
            assert (claimed["status"] == "claimed") if keyed else (claimed is True)
            [held] = seen["at_barrier"]
            original = held["request_id"]
            assert held["payload"] is None and original and (original == "orig") == keyed
            assert settle_reached.is_set()
            assert await reservations(store) == []
            assert tmux.created == 0
            other = new_ctl(store, tmux, "daemon-B")
            assert (await status(other, original))["found"] is False
            if keyed:
                await refire_then_replay(store, tmux, other)
            else:
                again = await stuck(other.spawn(dict(msg), HOST))
                assert again["type"] == "spawn.ok" and again["stream_id"] == f"{HOST}:plain"
                assert tmux.created == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_cancel_inside_ordinary_exception_cleanup_goes_through_the_settle() -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            tmux = HostTmux()
            ctl = new_ctl(store, tmux, "daemon-A")

            async def fails_after_flag(*_a, **_k):
                raise VerbError("synthetic_failure", "ordinary error after the intent")

            ctl._spawn_fenced = fails_after_flag  # type: ignore[method-assign]
            hold = Hold()
            real = store.spawn_cancelled

            async def held_query(*a, **kw):
                store.spawn_cancelled = real
                await hold.wait()
                return await real(*a, **kw)

            store.spawn_cancelled = held_query
            settle_reached = observe_settle(store)
            seen = await drain_held_spawn(ctl, hold, spawn_msg("orig"))
            [held] = seen["at_barrier"]
            assert held["payload"] is not None
            assert settle_reached.is_set()
            [kept] = await reservations(store)
            assert kept["payload"] == held["payload"] and kept["owner_instance_id"] is None
            assert await rows(store, "SELECT * FROM v2_spawn_outcomes") == []
            assert tmux.created == 0
            await recover_retained(store, tmux, "orig")
            await refire_then_replay(store, tmux, new_ctl(store, tmux, "daemon-D"))
        finally:
            store.stop()

    asyncio.run(go())


# -- Store settle fence ----------------------------------------------------------


async def seed(store: Store, *, nonce: str, owner: str, payload: str | None,
               tmux_created: int = 0, pane_pid: int | None = None) -> None:
    def op(conn):
        conn.execute(
            "INSERT INTO v2_stream_reservations (host, session_name, request_id, expires_at, payload, "
            "tmux_created, nonce, owner_instance_id, pane_pid) VALUES (?,?,?,?,?,?,?,?,?)",
            (HOST, "seat", "req", 9e12, payload, tmux_created, nonce, owner, pane_pid),
        )
        conn.commit()
    await store.submit(op)


def settle(store: Store, nonce: str = "old", owner: str = "daemon-A"):
    return store.settle_spawn_claim(HOST, "seat", request_id="req", nonce=nonce, owner_instance_id=owner)


@pytest.mark.parametrize("payload", [None, '{"open_fields":{}}'], ids=["empty", "payload"])
def test_settle_leaves_a_same_owner_replacement_with_a_new_nonce(payload) -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            await seed(store, nonce="new", owner="daemon-A", payload=payload)
            [before] = await reservations(store)
            assert await settle(store, nonce="old") == "untouched"
            [after] = await reservations(store)
            assert after == before
            assert (after["owner_instance_id"], after["payload"], after["nonce"]) == ("daemon-A", payload, "new")
            # Control: the matching nonce is authorized.
            assert await settle(store, nonce="new") == ("released" if payload is None else "retained")
            remaining = await reservations(store)
            assert remaining == ([] if payload is None else [{**before, "owner_instance_id": None}])
        finally:
            store.stop()

    asyncio.run(go())


@pytest.mark.parametrize("case", ["missing", "other-owner", "tombstoned", "bound-empty", "pid-empty"])
def test_settle_touches_nothing_it_does_not_own(case: str) -> None:
    async def go():
        store = Store(":memory:")
        store.start()
        try:
            if case != "missing":
                await seed(store, nonce="old", owner="daemon-B" if case == "other-owner" else "daemon-A",
                           payload=None, tmux_created=int(case == "bound-empty"),
                           pane_pid=4242 if case == "pid-empty" else None)
            if case == "tombstoned":
                await store.submit(lambda c: (c.execute(
                    "INSERT INTO v2_spawn_outcomes (host, session_name, state, request_id, updated_at) "
                    "VALUES (?,?,?,?,?)", (HOST, "seat", "cancelled", "req", "2026-11-26T00:00:00Z")), c.commit()))
            before = await reservations(store)
            assert await settle(store) == "untouched"
            assert await reservations(store) == before
        finally:
            store.stop()

    asyncio.run(go())
