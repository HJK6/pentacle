"""Store.stop honours one deadline and never yields a second SQLite owner (F11).

Two hazards: `stop()` used to block on `queue.put(sentinel)` before its timed
join, so a full queue behind a held callback made the timeout meaningless; and
after a timed-out join it cleared ownership anyway, so `start()` could open a
second worker and connection while the first still ran. Every test here uses a
real Store worker; held callbacks release themselves after a few seconds so a
regression fails instead of hanging.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import store as store_mod
from store import Store

pytestmark = pytest.mark.timeout(60)

SELF_RELEASE_S = 3.0


class Held:
    """One callback held inside a real Store worker; optionally the bounded
    queue filled behind it. Tracks which worker/connection pairs are inside
    a callback at once."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.release = threading.Event()
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.inside: set[tuple[int, int]] = set()
        self.max_owners = 0
        self.tasks: list[asyncio.Future] = []

    def track(self, fn):
        def wrapped(conn):
            key = (threading.get_ident(), id(conn))
            with self.lock:
                self.inside.add(key)
                self.max_owners = max(self.max_owners, len(self.inside))
            try:
                return fn(conn)
            finally:
                with self.lock:
                    self.inside.discard(key)
        return wrapped

    async def hold(self, *, fill: bool = True, write_after_release: str | None = None) -> None:
        def held(conn):
            self.entered.set()
            self.release.wait(SELF_RELEASE_S)
            if write_after_release is not None:
                conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, 'committed')", (write_after_release,))
                conn.commit()
            return "executed-held"

        self.tasks.append(asyncio.ensure_future(self.store.submit(self.track(held))))
        assert await asyncio.to_thread(self.entered.wait, 2), "held callback never entered"
        if fill:
            def queued(conn):
                conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('queued', 'ran')")
                conn.commit()
                return "executed-queued"
            while self.store.pending() < self.store._queue.maxsize:
                self.tasks.append(asyncio.ensure_future(self.store.submit(self.track(queued))))
                await asyncio.sleep(0)
            assert self.store.pending() == self.store._queue.maxsize

    async def settle(self) -> list:
        self.release.set()
        done, pending = await asyncio.wait(self.tasks, timeout=5)
        assert not pending, "a submitted future never settled"
        return [t.exception() if t.exception() is not None else t.result() for t in self.tasks]


def _timed_stop(store: Store, timeout: float) -> tuple[bool, float]:
    started = time.monotonic()
    result = store.stop(timeout=timeout)
    return result, time.monotonic() - started


# --------------------------------------------------------------------------- #
# Deadline
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("timeout", [0.1, 0.1, 0.1, 0.2], ids=["t0.1-a", "t0.1-b", "t0.1-c", "t0.2"])
def test_full_queue_stop_returns_within_deadline_while_callback_held(timeout):
    async def go():
        store = Store(":memory:", max_pending=1)
        store.start()
        held = Held(store)
        await held.hold()
        stopped, elapsed = await asyncio.to_thread(_timed_stop, store, timeout)
        still_held = not held.release.is_set()
        outcomes = await held.settle()
        return store, stopped, elapsed, still_held, outcomes

    store, stopped, elapsed, still_held, outcomes = asyncio.run(go())
    assert still_held and elapsed <= 0.5, elapsed
    assert stopped is False
    # Released: both the running and the queued callable ran to their real outcome.
    assert outcomes == ["executed-held", "executed-queued"]
    assert store.stop(timeout=2) is True
    assert store._thread is None


def test_empty_queue_stop_completes_and_is_idempotent():
    store = Store(":memory:")
    store.start()
    worker = store._thread
    stopped, elapsed = _timed_stop(store, 0.5)
    assert stopped is True and elapsed < 0.5
    assert not worker.is_alive() and store._thread is None
    assert store.stop(timeout=0.5) is True
    never_started = Store(":memory:")
    assert never_started.stop(timeout=0) is True


def test_zero_timeout_signals_without_waiting_and_worker_exits_after_release():
    async def go():
        store = Store(":memory:", max_pending=1)
        store.start()
        worker = store._thread
        held = Held(store)
        await held.hold()
        stopped, elapsed = _timed_stop(store, 0)
        assert stopped is False and elapsed < 0.1, elapsed
        with pytest.raises(RuntimeError, match="not running"):
            await store.submit(lambda conn: None)
        outcomes = await held.settle()
        await asyncio.to_thread(worker.join, 2)
        assert not worker.is_alive(), "closing worker must exit once its queue drains"
        assert outcomes == ["executed-held", "executed-queued"]
        assert store.stop(timeout=0) is True and store._thread is None

    asyncio.run(go())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timeout_is_rejected_not_an_indefinite_wait(bad):
    store = Store(":memory:")
    store.start()
    try:
        with pytest.raises(ValueError):
            store.stop(timeout=bad)
        assert store._thread is not None and not store._closing
    finally:
        assert store.stop(timeout=1) is True
    # A negative timeout is an already-expired budget, not an error.
    assert Store(":memory:").stop(timeout=-1) is True


def test_transition_lock_pressure_shares_the_one_deadline():
    async def go():
        store = Store(":memory:", max_pending=1)
        store.start()
        held = Held(store)
        await held.hold()
        store._lock.acquire()
        threading.Timer(0.15, store._lock.release).start()
        stopped, elapsed = await asyncio.to_thread(_timed_stop, store, 0.3)
        # Lock wait and join share 0.3 s rather than each getting a full timeout.
        assert stopped is False and elapsed < 0.45, elapsed
        await held.settle()
        assert store.stop(timeout=2) is True

        # The lock is never released inside the budget: refuse to wait past it.
        store.start()
        store._lock.acquire()
        try:
            stopped, elapsed = await asyncio.to_thread(_timed_stop, store, 0.1)
        finally:
            store._lock.release()
        assert stopped is False and elapsed < 0.3, elapsed
        assert store.stop(timeout=2) is True

    asyncio.run(go())


def test_repeated_and_concurrent_stops_stay_bounded_and_queue_one_sentinel():
    async def go():
        store = Store(":memory:", max_pending=4)
        store.start()
        held = Held(store)
        await held.hold(fill=False)
        held.tasks.append(asyncio.ensure_future(store.submit(lambda conn: "executed-queued")))
        await asyncio.sleep(0.05)
        results = await asyncio.gather(*(asyncio.to_thread(_timed_stop, store, 0.15) for _ in range(4)))
        assert all(stopped is False and elapsed <= 0.5 for stopped, elapsed in results), results
        assert store.pending() == 2, "one queued callable plus exactly one stop sentinel"
        outcomes = await held.settle()
        assert outcomes == ["executed-held", "executed-queued"]
        assert store.stop(timeout=2) is True

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Ownership and lifecycle
# --------------------------------------------------------------------------- #

def test_timed_out_owner_is_retained_and_start_cannot_open_a_second_owner(tmp_path):
    db = tmp_path / "owner.db"

    async def go():
        store = Store(str(db), max_pending=2)
        store.start()
        original = store._thread
        held = Held(store)
        await held.hold(fill=False)
        held.tasks.append(asyncio.ensure_future(store.submit(held.track(lambda conn: "executed-queued"))))
        await asyncio.sleep(0.05)
        stopped, elapsed = await asyncio.to_thread(_timed_stop, store, 0.1)
        assert stopped is False and elapsed <= 0.5
        assert store._thread is original and original.is_alive() and store._closing
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="still stopping"):
            await asyncio.to_thread(store.start)
        assert time.monotonic() - started < 0.5, "refusal must be prompt"
        with pytest.raises(RuntimeError, match="not running"):
            await store.submit(lambda conn: None)
        await asyncio.sleep(0.2)  # a second worker, if any, would run the queued callable now
        assert store._thread is original and held.max_owners == 1
        outcomes = await held.settle()
        assert outcomes == ["executed-held", "executed-queued"] and held.max_owners == 1
        await asyncio.to_thread(original.join, 2)
        # Restart after full teardown: a fresh worker, queue and readiness.
        store.start()
        assert store._thread is not original and store._thread.is_alive() and not store._closing
        await store.put("after-restart", "yes")
        assert await store.get("after-restart") == "yes"
        assert store.stop(timeout=2) is True

    asyncio.run(go())


def test_other_store_unaffected_by_a_timed_out_store():
    async def go():
        a, b = Store(":memory:", max_pending=1), Store(":memory:")
        a.start()
        b.start()
        held = Held(a)
        await held.hold()
        assert (await asyncio.to_thread(_timed_stop, a, 0.1))[0] is False
        await b.put("k", "v")
        assert await b.get("k") == "v"
        await held.settle()
        assert a.stop(timeout=2) is True and b.stop(timeout=2) is True

    asyncio.run(go())


def test_worker_that_dies_unexpectedly_leaves_no_stale_sentinel_for_a_restart():
    """QA reject 1: a worker killed by an unexpected error (here its result is
    delivered to a closed loop) must close admission before draining, so a
    later stop queues no sentinel and a restarted worker serves requests."""
    store = Store(":memory:")
    store.start()
    worker = store._thread
    entered, release = threading.Event(), threading.Event()
    hook, errors = threading.excepthook, []
    threading.excepthook = lambda args: errors.append(args.exc_value)

    def held(conn):
        entered.set()
        release.wait(SELF_RELEASE_S)
        return "yes"

    async def submit_then_close_loop():
        task = asyncio.ensure_future(store.submit(held))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()

    try:
        asyncio.run(submit_then_close_loop())  # the caller's loop is now closed
        release.set()
        worker.join(2)
    finally:
        threading.excepthook = hook
    assert not worker.is_alive() and errors, "worker should have died delivering to a closed loop"
    assert store._closing, "a dead worker must leave admission closed"

    async def refused():
        with pytest.raises(RuntimeError, match="not running"):
            await store.submit(lambda conn: None)
    asyncio.run(refused())
    assert store.stop(timeout=1) is True and store._queue.empty()
    store.start()

    async def serve():
        await asyncio.wait_for(store.put("k", "v"), 2)
        assert await asyncio.wait_for(store.get("k"), 2) == "v"
    asyncio.run(serve())
    assert store._thread.is_alive() and store.stop(timeout=2) is True


def test_concurrent_starts_create_exactly_one_worker():
    store = Store(":memory:")
    barrier = threading.Barrier(8)
    seen: list = []

    def start():
        barrier.wait()
        store.start()
        seen.append(store._thread)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(seen) == 8 and len({id(t) for t in seen}) == 1
    assert store.stop(timeout=2) is True


def test_submit_stop_race_never_strands_a_future():
    async def go():
        for _ in range(20):
            store = Store(":memory:", max_pending=64)
            store.start()
            stopper = asyncio.ensure_future(asyncio.to_thread(store.stop, 1.0))
            futures = []
            for i in range(200):
                try:
                    futures.append(asyncio.ensure_future(store.submit(lambda conn, i=i: i)))
                except RuntimeError:
                    pass
                if i % 20 == 0:
                    await asyncio.sleep(0)
            assert await stopper is True
            done, pending = await asyncio.wait(futures, timeout=2) if futures else (set(), set())
            assert not pending
            for f in done:
                assert f.exception() is None or isinstance(f.exception(), RuntimeError)
            assert store._queue.empty()

    asyncio.run(go())


def test_initialization_failure_fails_queued_callers_and_allows_restart(monkeypatch):
    gate, release = threading.Event(), threading.Event()
    original = store_mod.prepare_history_schema

    def failing(*args, **kwargs):
        gate.set()
        release.wait(SELF_RELEASE_S)
        raise RuntimeError("injected init failure")

    monkeypatch.setattr(store_mod, "prepare_history_schema", failing)

    async def go():
        store = Store(":memory:")
        starter = asyncio.ensure_future(asyncio.to_thread(store.start))
        assert await asyncio.to_thread(gate.wait, 2)
        queued = asyncio.ensure_future(store.submit(lambda conn: "should-not-run"))
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(RuntimeError, match="injected init failure"):
            await starter
        with pytest.raises(RuntimeError, match="failed to start"):
            await asyncio.wait_for(queued, 2)
        assert store.progress.snapshot()["pending_count"] == 0
        assert store.stop(timeout=1) is True and store._thread is None
        monkeypatch.setattr(store_mod, "prepare_history_schema", original)
        store.start()
        await store.put("k", "v")
        assert await store.get("k") == "v"
        assert store.stop(timeout=2) is True

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Caller truth
# --------------------------------------------------------------------------- #

def test_cancelled_awaiter_is_not_rollback_the_write_commits(tmp_path):
    db = tmp_path / "truth.db"

    async def go():
        store = Store(str(db), max_pending=1)
        store.start()
        held = Held(store)
        await held.hold(fill=False, write_after_release="held-write")
        awaiter = held.tasks[0]
        awaiter.cancel()
        assert (await asyncio.to_thread(_timed_stop, store, 0.1))[0] is False
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            await awaiter
        assert await asyncio.to_thread(store.stop, 2) is True
        # The cancelled caller's write actually committed; read it back after restart.
        store.start()
        assert await store.get("held-write") == "committed"
        assert store.stop(timeout=2) is True

    asyncio.run(go())
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT v FROM kv WHERE k='held-write'").fetchone()[0] == "committed"


def test_progress_accounting_survives_a_timed_out_stop():
    async def go():
        store = Store(":memory:", max_pending=2)
        store.start()
        held = Held(store)
        await held.hold()
        assert (await asyncio.to_thread(_timed_stop, store, 0.1))[0] is False
        snap = store.progress.snapshot()
        assert snap["pending_count"] == 2 and snap["current_started_mono_s"] is not None
        await held.settle()
        assert store.stop(timeout=2) is True
        snap = store.progress.snapshot()
        assert snap["enqueued_seq"] == snap["started_seq"] == snap["finished_seq"] == 3
        assert snap["pending_count"] == 0 and snap["current_started_mono_s"] is None

    asyncio.run(go())


# --------------------------------------------------------------------------- #
# main.shutdown seam
# --------------------------------------------------------------------------- #

class _Quick:
    async def stop(self, **_kwargs):
        return None


class _Server:
    def __init__(self):
        self.spawn_ready = asyncio.Event()

    async def close(self, *, deadline):
        return None


class _Spawns:
    async def drain_background_spawns(self, *, timeout_s):
        return {}


async def _shutdown(daemon_main, budget, store):
    await daemon_main.shutdown(budget, server=_Server(), spawnctl=_Spawns(), tasks=[], composites=[],
                               lane_rulings=None, notify=_Quick(), assets=_Quick(), lifecycle=_Quick(),
                               store=store, watchdog=None)


def _heartbeat(ticks: list, stop: asyncio.Event):
    async def beat():
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)
    return asyncio.ensure_future(beat())


@pytest.mark.parametrize("budget_s", [0.6, 0.0], ids=["budget", "exhausted"])
def test_main_shutdown_stops_a_held_store_off_loop_within_the_budget(budget_s):
    import main as daemon_main
    from shutdown_budget import ShutdownBudget

    async def go():
        store = Store(":memory:", max_pending=1)
        store.start()
        worker = store._thread
        held = Held(store)
        await held.hold()
        ticks, stop_beat = [], asyncio.Event()
        beat = _heartbeat(ticks, stop_beat)
        budget = ShutdownBudget(max(budget_s, 0.01))
        if budget_s == 0.0:
            await asyncio.sleep(0.05)  # the absolute deadline has already passed
        started = time.monotonic()
        await _shutdown(daemon_main, budget, store)
        ended = time.monotonic()
        stop_beat.set()
        await beat
        assert not held.release.is_set()
        assert ended <= budget.deadline + 0.15, (ended - budget.deadline)
        during = [t for t in ticks if started <= t <= ended]
        if budget_s:
            assert len(during) >= 10, "event loop starved while the store stopped"
            gaps = [b - a for a, b in zip(during, during[1:])]
            assert max(gaps) < 0.2, max(gaps)
        # Expired, not completed: the worker still owns the database.
        assert store._thread is worker and worker.is_alive()
        with pytest.raises(RuntimeError, match="still stopping"):
            store.start()
        await held.settle()
        assert await asyncio.to_thread(store.stop, 2) is True

    asyncio.run(go())


def test_cancelled_main_shutdown_does_not_clear_a_live_owner():
    import main as daemon_main
    from shutdown_budget import ShutdownBudget

    async def go():
        store = Store(":memory:", max_pending=1)
        store.start()
        worker = store._thread
        held = Held(store)
        await held.hold()
        task = asyncio.ensure_future(_shutdown(daemon_main, ShutdownBudget(2.0), store))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store._thread is worker and worker.is_alive() and store._closing
        with pytest.raises(RuntimeError, match="still stopping"):
            store.start()
        assert await held.settle() == ["executed-held", "executed-queued"]
        assert await asyncio.to_thread(store.stop, 3) is True

    asyncio.run(go())


def test_main_shutdown_runs_store_stop_off_loop_after_the_other_steps():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
    tail = source[source.index("async def shutdown("):]
    assert tail.index("server.close(") < tail.index("store.stop(")
    assert "asyncio.to_thread(" in tail[tail.index("store_deadline"):tail.index("store.stop(")]


# --------------------------------------------------------------------------- #
# Process rehearsal: a disposable real daemon, SIGTERM with a full Store queue
# --------------------------------------------------------------------------- #

_DAEMON = r'''
import asyncio, json, os, sys, threading, time
from pathlib import Path
root, mode, service = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
sys.path[:0] = [service, str(Path(service).parent)]
import main
from server import Server
from store import Store

def event(name, **data):
    with open(root / "events.jsonl", "a") as f:
        f.write(json.dumps({"event": name, "mono_s": time.monotonic(), **data}) + "\n")

owned = {}
original_init = Store.__init__
def store_init(self, path=":memory:", **kw):
    original_init(self, path, **kw)
    if str(path) == str(root / "sessions.db"):
        owned["store"] = self
Store.__init__ = store_init

release = threading.Event()
def held(conn):
    conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('rehearsal-held', 'committed-before-hold')")
    conn.commit()
    event("store_held")
    event("store_released", explicit=release.wait(30))
    return "held"
def queued(conn):
    conn.execute("INSERT OR REPLACE INTO kv (k, v) VALUES ('rehearsal-queued', 'ran')")
    conn.commit()
    event("queued_ran")
    return "queued"

async def fill():
    await asyncio.sleep(0.5)  # let startup's own store work finish first
    store = owned["store"]
    store._queue.maxsize = 1  # fixture: shrink the bounded queue so one item fills it
    asyncio.ensure_future(store.submit(held))
    deadline = time.monotonic() + 5
    while store.progress.snapshot()["current_started_mono_s"] is None and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    fut = asyncio.ensure_future(store.submit(queued))
    fut.add_done_callback(lambda f: f.cancelled() or f.exception())
    while store.pending() < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    event("ready", pid=os.getpid(), pending=store.pending())

original_configure = Server.configure_error_alerts
async def configure(self, *a, **kw):
    result = await original_configure(self, *a, **kw)
    asyncio.ensure_future(fill())
    return result
Server.configure_error_alerts = configure

def control():
    while not (root / "release").exists():
        time.sleep(0.01)
    release.set()
if mode == "complete":
    threading.Thread(target=control, daemon=True).start()

args = main.parse_args(["--host", "127.0.0.1", "--port", "0", "--db", str(root / "sessions.db"),
    "--notifications-db", str(root / "notifications.db"), "--assets-db", str(root / "assets.db"),
    "--blob-root", str(root / "blobs"), "--local-host", "fixture-stop", "--tmux-bin", str(root / "tmux-private"),
    "--spawn-command", "/bin/false", "--claude-bin", "/bin/false", "--codex-bin", "/bin/false",
    "--spawn-cwd", str(root), "--projects-root", str(root / "projects")])
for key in vars(args):
    if key.startswith("disable_"):
        setattr(args, key, True)
main.configure_logging(20)
code = main.run_bounded(main.run(args))
event("run_returned", code=code, worker_alive=bool(owned["store"]._thread and owned["store"]._thread.is_alive()))
sys.exit(code)
'''


def _events(root: Path) -> list[dict]:
    path = root / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _wait_event(root: Path, name: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for row in _events(root):
            if row["event"] == name:
                return row
        time.sleep(0.02)
    raise AssertionError(f"daemon never reached {name}: {_events(root)}")


@pytest.mark.timeout(90)
@pytest.mark.parametrize("mode", ["expire", "complete"])
def test_real_daemon_sigterm_with_a_full_store_queue(tmp_path, mode):
    tmux = shutil.which("tmux")
    assert tmux, "supported-VM prerequisite: actual tmux executable on PATH"
    root = tmp_path / "daemon"
    root.mkdir(mode=0o700)
    for name in ("home", "projects", "tmp"):
        (root / name).mkdir(mode=0o700)
    (root / "tmux-private").write_text(f'#!/bin/sh\nexec {tmux} -S {root / "tmux.sock"} "$@"\n')
    (root / "tmux-private").chmod(0o700)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PENTACLE_", "AGENT_ORCH_", "V2_", "COSMO_", "TMUX"))}
    budget_s = 2.0
    env.update({"HOME": str(root / "home"), "XDG_CONFIG_HOME": str(root / "home/.config"),
                "XDG_CACHE_HOME": str(root / "home/.cache"), "TMPDIR": str(root / "tmp"),
                "PYTHONDONTWRITEBYTECODE": "1", "PENTACLE_HOST_ID": "fixture-stop",
                "PENTACLE_V2_SHUTDOWN_BUDGET_S": str(budget_s),
                "PENTACLE_DAEMON_PROGRESS_PATH": str(root / "daemon-progress.json"),
                "PENTACLE_DAEMON_INSTANCE_ID": "fixture-stop-installation",
                "PENTACLE_ERROR_ALERTS_MODE": "record-only",
                "PENTACLE_MACHINES_JSON": json.dumps([{"name": "fixture-stop", "ssh_target": None,
                    "codex_bin": "/bin/false", "claude_bin": "/bin/false", "cwd": str(root),
                    "projects_root": str(root / "projects")}])})
    service = Path(__file__).resolve().parents[1]
    log = open(root / "daemon.log", "w")
    proc = subprocess.Popen([sys.executable, "-c", _DAEMON, str(root), mode, str(service)], env=env,
                            cwd=service, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        ready = _wait_event(root, "ready", 30)
        assert ready["pending"] == 1
        sigterm_at = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        if mode == "complete":
            time.sleep(0.3)
            (root / "release").touch()
        proc.wait(timeout=budget_s + 8)
        exited_at = time.monotonic()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(5)
        log.close()
        subprocess.run([tmux, "-S", str(root / "tmux.sock"), "kill-server"], capture_output=True, timeout=5)
    text = (root / "daemon.log").read_text()
    events = {row["event"]: row for row in _events(root)}
    assert proc.returncode == 0, text[-4000:]
    # Process exit stays inside the shutdown budget plus loop teardown grace.
    assert exited_at - sigterm_at <= budget_s + 1.5, (exited_at - sigterm_at, text[-4000:])
    expired = "store stop deadline" in text and "still stopping" in text
    completed = "store stopped: worker exited" in text
    with sqlite3.connect(f"file:{root / 'sessions.db'}?mode=ro", uri=True) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        rows = dict(conn.execute("SELECT k, v FROM kv WHERE k LIKE 'rehearsal-%'").fetchall())
    assert rows.get("rehearsal-held") == "committed-before-hold"
    if mode == "expire":
        assert expired and not completed, text[-4000:]
        assert events["run_returned"]["worker_alive"] is True
        assert "queued_ran" not in events and "rehearsal-queued" not in rows
    else:
        assert completed and not expired, text[-4000:]
        assert events["run_returned"]["worker_alive"] is False
        assert events["store_released"]["explicit"] is True
        assert rows.get("rehearsal-queued") == "ran"
