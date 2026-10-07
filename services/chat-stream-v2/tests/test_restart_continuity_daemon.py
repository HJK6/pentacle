"""In-process pins for the daemon restart-continuity fixes
(spec_pentacle__daemon_restart_continuity_2026_10). The real-process journeys
are `tests/soak/test_restart_continuity.py`; each fix here maps to a RED cell:

  H2 (S1/S2/S3/S4-SIGTERM): in-flight spawns hand off before the store stops.
  H1 (exposed by the H2 fix): an `indeterminate` interruption outcome whose
      reservation is retained is reconciled, and a same-key replay is `starting`.
  S2: an adopted pane whose brief was never pasted gets it exactly once.
"""
from __future__ import annotations

import asyncio
import inspect
import time

import pytest

import spawnctl as spawnctl_mod
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from submission_events import EventProof, EventWatermark

HOST = "testhost"
READY_SCREEN = "─" * 20 + "\n❯\n" + "─" * 20 + "\n  ⏵⏵ bypass permissions on\n"


class Tmux:
    def __init__(self, screen: str = READY_SCREEN) -> None:
        self.screen = screen
        self.pasted: list[str] = []

    async def has_session(self, name: str) -> bool:
        return True

    async def session_state(self, name: str) -> str:
        return "alive"

    async def capture(self, name: str) -> str:
        return self.screen

    async def pane_pid(self, name: str) -> str:
        return "4242"

    async def paste(self, name: str, text: str) -> None:
        self.pasted.append(text)

    async def kill_session(self, name: str) -> None:
        raise AssertionError("never kill an adopted pane")

    async def run(self, *args, **kwargs):
        return 0, ""


class Proof:
    def __init__(self, tmux: Tmux) -> None:
        self.tmux = tmux

    async def watermark(self, stream_id: str) -> EventWatermark:
        return EventWatermark(stream_id, 7, "reachable")

    async def wait(self, stream_id, *, expected_text, watermark, timeout_s) -> EventProof:
        state = "proven" if expected_text in self.tmux.pasted else "pending"
        return EventProof(state, stream_id, watermark.daemon_seq)

    lookup = wait


def _ctl(db: Store, tmux: Tmux, instance: str = "") -> SpawnCtl:
    sessions = Sessions(db, tmux=tmux, local_host=HOST)
    return SpawnCtl(db, sessions, tmux=tmux, submission_proof=Proof(tmux), owner_instance_id=instance)


# --------------------------------------------------------------------------- #
# H2: bounded drain before the store stops
# --------------------------------------------------------------------------- #


def test_shutdown_drain_lets_interrupted_spawn_write_durable_handoff():
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            ctl = _ctl(db, Tmux())
            started = asyncio.Event()

            async def admitted_spawn():
                started.set()
                try:
                    await asyncio.sleep(3600)  # waiting on provider boot
                except asyncio.CancelledError:
                    await db.set_spawn_outcome(HOST, "seat", "indeterminate", request_id="r1",
                                               reason="spawn_interrupted: request cancelled after admission")
                    raise

            task = asyncio.create_task(admitted_spawn())
            ctl._background_spawns.add(task)
            await started.wait()
            result = await ctl.drain_background_spawns(timeout_s=5)
            assert result == {"cancelled": 1, "unfinished": 0}
            outcome = await db.get_spawn_outcome(HOST, "seat")
            assert outcome["state"] == "indeterminate"  # written while the store ran
        finally:
            db.stop()
    asyncio.run(run())


def test_shutdown_drain_lets_an_in_progress_paste_land_before_cancelling():
    """S3/S2-SIGTERM: a spawn that has persisted its pre-paste watermark must not
    be cancelled before the paste, or adoption could never deliver it."""
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            tmux = Tmux()
            ctl = _ctl(db, tmux)
            entered = asyncio.Event()

            async def pasting_spawn():
                task = asyncio.current_task()
                ctl._delivery_critical.add(task)
                try:
                    entered.set()
                    await asyncio.sleep(0.2)  # watermark write + paste in flight
                    await tmux.paste("seat", "brief")
                finally:
                    ctl._delivery_critical.discard(task)
                await asyncio.sleep(3600)  # submission proof wait: cancellable

            task = asyncio.create_task(pasting_spawn())
            ctl._background_spawns.add(task)
            await entered.wait()
            result = await ctl.drain_background_spawns(timeout_s=4)
            assert result == {"cancelled": 1, "unfinished": 0}
            assert tmux.pasted == ["brief"]
        finally:
            db.stop()
    asyncio.run(run())


def test_shutdown_drain_holds_one_absolute_deadline():
    """Final QA B2: a critical section that never finishes plus a cancellation
    that never completes must not stretch the drain past its single deadline."""
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            ctl = _ctl(db, Tmux())
            entered = asyncio.Event()
            release = asyncio.Event()

            async def stuck_spawn():
                task = asyncio.current_task()
                ctl._delivery_critical.add(task)
                entered.set()
                while not release.is_set():  # paste never returns; cancellation swallowed
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue

            task = asyncio.create_task(stuck_spawn())
            ctl._background_spawns.add(task)
            try:
                await entered.wait()
                started = time.monotonic()
                result = await ctl.drain_background_spawns(timeout_s=0.3)
                elapsed = time.monotonic() - started
            finally:
                release.set()
                await task
            assert result == {"cancelled": 1, "unfinished": 1}
            assert elapsed < 0.3 + 0.1, elapsed
        finally:
            db.stop()
    asyncio.run(run())


def _stalled_shutdown(calls, budget_s, *, only_server=False):
    """Every shutdown step stalls and swallows cancellation (or, with
    `only_server`, only the real Server.close's accepted send, consent expiry
    and BOTH listeners stall)."""
    import main as daemon_main
    from server import Server
    from shutdown_budget import ShutdownBudget

    async def stall(name):
        calls.append(name)
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                continue

    class Listener:
        def __init__(self, name):
            self.name = name

        def close(self):
            calls.append(self.name)

        async def wait_closed(self):
            await stall(self.name + "-wait")

    async def quick(name):
        calls.append(name)

    step = quick if only_server else stall

    class Part:
        def __init__(self, name):
            self.name = name

        def stop(self, **_kwargs):
            return step(self.name)

    class Drain:
        def drain_background_spawns(self, *, timeout_s):
            calls.append(("drain-timeout", timeout_s))
            return step("spawn-drain")

    class StoreStub:
        def stop(self, timeout):
            calls.append(("store-timeout", timeout))
            time.sleep(timeout)  # a worker that never drains: join runs to its timeout

    async def scenario():
        server = Server.__new__(Server)
        server.spawn_ready = asyncio.Event()
        server._consent_expiry_task = asyncio.ensure_future(stall("consent-expiry"))
        server._detached_send_tasks = {asyncio.ensure_future(stall("accepted-send"))}
        server._client_writer_tasks = {}
        server._tls_ws_server, server._ws_server = Listener("tls"), Listener("plain")
        await asyncio.sleep(0)
        budget = ShutdownBudget(budget_s)
        await daemon_main.shutdown(
            budget, server=server, spawnctl=Drain(), tasks=[asyncio.ensure_future(step("background-task"))],
            composites=[Part("composite")], lane_rulings=Part("lane-rulings"), notify=Part("notify"),
            assets=Part("assets"), lifecycle=Part("lifecycle"), store=StoreStub())
        return budget

    return daemon_main, scenario


STEPS = ("spawn-drain", "background-tasks", "composite", "lane-rulings", "server", "notify", "assets", "lifecycle")


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.parametrize("only_server", [False, True], ids=["every-step-stalled", "server-close-stalled"])
def test_stalled_shutdown_completes_within_the_budget(only_server):
    """Cycle 2 B2: one absolute deadline covers the spawn drain, background
    tasks, composites, lane rulings, Server.close (accepted sends, consent
    expiry, BOTH listeners), notify, assets, lifecycle and store.stop; loop
    teardown never waits on a task that ignores cancellation."""
    calls: list = []
    budget_s = 0.6
    daemon_main, scenario = _stalled_shutdown(calls, budget_s, only_server=only_server)
    started = time.monotonic()
    budget = daemon_main.run_bounded(scenario())
    elapsed = time.monotonic() - started
    assert elapsed <= budget_s + daemon_main.LOOP_TEARDOWN_GRACE_S + 0.25, elapsed
    names = [c for c in calls if isinstance(c, str)]
    drain_timeout = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "drain-timeout")
    store_timeout = next(c[1] for c in calls if isinstance(c, tuple) and c[0] == "store-timeout")
    assert drain_timeout <= budget_s - budget.reserve_s
    assert calls[-1][0] == "store-timeout"
    if only_server:
        # Server.close reached both listeners inside its share; nothing after it was starved.
        assert {"accepted-send", "consent-expiry", "tls", "plain"} <= set(names), calls
        assert budget.abandoned in ([], ["server"])
        assert {"notify", "assets", "lifecycle"} <= set(names)
        assert store_timeout > 0
    else:
        assert budget.abandoned == list(STEPS), budget.abandoned
        assert 0 < store_timeout <= budget.reserve_s + 0.05  # the reserve is all that is left


def test_shutdown_budget_is_at_most_15s_and_env_may_only_lower_it():
    from shutdown_budget import SHUTDOWN_BUDGET_ENV as KEY, SHUTDOWN_BUDGET_MAX_S, configured_budget_s
    from main import LOOP_TEARDOWN_GRACE_S
    assert SHUTDOWN_BUDGET_MAX_S + LOOP_TEARDOWN_GRACE_S + 1 < 20  # launchd ExitTimeOut
    assert configured_budget_s({}) == 15.0
    assert configured_budget_s({KEY: "6.5"}) == 6.5
    for raw in ("30", "x", "0", "-1", "nan", "inf"):
        assert configured_budget_s({KEY: raw}) == 15.0, raw


def test_store_stop_honours_its_timeout():
    db = Store(":memory:")
    db.start()
    started = time.monotonic()
    db.stop(timeout=0.5)
    assert time.monotonic() - started < 0.5
    db.stop(timeout=0.5)  # idempotent


def test_main_shutdown_drains_spawns_before_server_and_store_stop():
    from pathlib import Path
    source = (Path(spawnctl_mod.__file__).parent / "main.py").read_text()
    tail = source[source.index("async def shutdown("):]
    drain = tail.index("spawnctl.drain_background_spawns(")
    assert tail.index("server.spawn_ready.clear()") < drain
    assert drain < tail.index("server.close(") < tail.index("store.stop(")


# --------------------------------------------------------------------------- #
# H1: a retained `indeterminate` handle is reconciled, and replays as starting
# --------------------------------------------------------------------------- #


def _reserve(db: Store, name: str, request_id: str, *, owner: str = "dead-instance") -> None:
    async def go():
        assert await db.reserve_stream_id(HOST, name, ttl_s=180, request_id=request_id, nonce="n1",
                                          owner_instance_id=owner, idempotency_key="k1")
        assert await db.record_spawn_intent(HOST, name, {"open_fields": {}, "brief": "",
                                                         "delivery_receipt": {"state": "not_requested"}},
                                            request_id=request_id, nonce="n1")
    return go()


def test_reconcile_does_not_treat_retained_indeterminate_as_settled():
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            ctl = _ctl(db, Tmux(), instance="new-instance")
            await _reserve(db, "seat", "r1")
            await db.set_spawn_outcome(HOST, "seat", "indeterminate", request_id="r1",
                                       reason="spawn_interrupted: request cancelled after admission")
            visited = []

            async def one(res):
                visited.append(res["request_id"])
                return "deferred"

            ctl._reconcile_one_spawn_intent = one  # type: ignore[method-assign]
            await ctl.reconcile_spawn_intents()
            assert visited == ["r1"]
        finally:
            db.stop()
    asyncio.run(run())


def test_reconcile_still_skips_delivered_and_failed():
    """Regression control: definite outcomes stay settled."""
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            ctl = _ctl(db, Tmux(), instance="new-instance")
            for name, state in (("a", "delivered"), ("b", "failed")):
                await _reserve(db, name, f"r-{name}")
                await db.set_spawn_outcome(HOST, name, state, request_id=f"r-{name}", reason=state)
            visited = []

            async def one(res):
                visited.append(res["request_id"])
                return "deferred"

            ctl._reconcile_one_spawn_intent = one  # type: ignore[method-assign]
            await ctl.reconcile_spawn_intents()
            assert visited == []
        finally:
            db.stop()
    asyncio.run(run())


def test_same_key_replay_of_reconciling_interruption_is_starting():
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            ctl = _ctl(db, Tmux())
            await _reserve(db, "seat", "r1")
            row = {"session_name": "seat", "request_id": "r1", "state": "indeterminate",
                   "reason": "spawn_interrupted: request cancelled after admission"}
            reply = await ctl._reply_from_replay(HOST, "r2", "terminal", row, "k1")
            assert reply["type"] == "spawn.ok" and reply["state"] == "starting"
            assert reply["request_id"] == "r1" and reply["stream_id"] == f"{HOST}:seat"
            # Without a retained handle the stored outcome replays truthfully as before.
            await db.release_stream_id_fenced(HOST, "seat", "r1")
            with pytest.raises(VerbError) as exc:
                await ctl._reply_from_replay(HOST, "r2", "terminal", row, "k1")
            assert exc.value.code == "spawn_interrupted"
        finally:
            db.stop()
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# S2: adoption delivers a provably-unpasted brief once
# --------------------------------------------------------------------------- #

BRIEF = "Read /tmp/pentacle-prompt-stage/pentacle-initial-prompt-x.txt and follow the complete prompt exactly."


def _adopt(receipt: dict, *, screen: str = READY_SCREEN, expires_at: float | None = None):
    async def run():
        db = Store(":memory:")
        db.start()
        try:
            tmux = Tmux(screen)
            ctl = _ctl(db, tmux)
            await _reserve(db, "seat", "r1")
            await db.open_session(HOST, "seat", provider="claude", visibility="hidden", pane_status="pane_alive")
            resume = {"request_id": "r1", "nonce": "n1", "intent": {"brief": BRIEF, "delivery_receipt": receipt},
                      "expires_at": time.time() + 180 if expires_at is None else expires_at}
            # Controls also run on the pre-fix base, which has no `resume` seam.
            kwargs = {"resume": resume} if "resume" in inspect.signature(ctl._settle_adoption).parameters else {}
            result = await ctl._settle_adoption(HOST, "seat", BRIEF, tmux, delivery_receipt=receipt, **kwargs)
            reservation = (await db.reservations(include_expired=True))[0]
            return result, tmux.pasted, reservation
        finally:
            db.stop()
    return asyncio.run(run())


def test_adoption_delivers_never_pasted_brief_exactly_once(monkeypatch):
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.2)
    receipt = {"state": "requested", "transport": "staged", "stage_status": "written"}
    (state, evidence, reason), pasted, reservation = _adopt(receipt)
    assert state == "delivered" and evidence == "event_store"  # the one paste, proved post-watermark
    assert pasted == [BRIEF]
    # The pre-paste watermark is persisted first: a crash after it is the
    # post-paste (USER-event-proved, never re-pasted) adoption case.
    assert '"proof_watermark_state": "reachable"' in reservation["payload"] or \
        '"proof_watermark_state":"reachable"' in reservation["payload"]


def test_adoption_defers_unready_provider_without_pasting(monkeypatch):
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.2)
    receipt = {"state": "requested", "transport": "staged"}
    (state, evidence, _reason), pasted, _res = _adopt(receipt, screen="Claude booting...")
    assert (state, evidence) == ("indeterminate", "provider_booting") and pasted == []


@pytest.mark.parametrize("receipt", [
    {"state": "requested", "transport": "staged", "proof_watermark": 3, "proof_watermark_state": "reachable"},
    {"state": "requested", "transport": "native_argv"},
])
def test_adoption_never_pastes_when_a_paste_may_have_happened(monkeypatch, receipt):
    """Regression control: a recorded pre-paste watermark (or native argv)
    keeps the existing proof-only adoption; nothing is pasted."""
    monkeypatch.setattr(spawnctl_mod, "ADOPTION_BOOT_DEADLINE_S", 0.2)

    async def no_transcript(*_a, **_k):
        return "no_transcript"

    monkeypatch.setattr(SpawnCtl, "_bounded_adoption_transcript_status", no_transcript)
    (_state, _evidence, _reason), pasted, _res = _adopt(receipt)
    assert pasted == []
