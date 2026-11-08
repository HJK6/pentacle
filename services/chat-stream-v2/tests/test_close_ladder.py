from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

import prockill  # noqa: E402
from ledger import Ledger  # noqa: E402
from reconciler import SessionReconciler  # noqa: E402
import sessions as sessions_mod  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from store import Store  # noqa: E402

HOST = "localhost"


class RecordingAlerts:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    def emit(self, kind: str, **fields: object) -> None:
        self.emitted.append((kind, fields))

    async def record(self, kind: str, **fields: object) -> None:
        self.emitted.append((kind, fields))
        return None


class LadderTmux:
    def __init__(self, *, pid: str = "", dies_on_graceful: bool = False) -> None:
        self.pid = pid
        self.alive = True
        self.dies_on_graceful = dies_on_graceful
        self.kills = 0

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def pane_pid(self, name: str) -> str:
        return self.pid

    async def pane_identity(self, name: str) -> dict[str, str] | None:
        if not self.pid:
            return None
        return {
            "pane_pid": self.pid,
            "pane_id": "%1",
            "tty": "/dev/ttys001",
            "tmux_socket": "/tmp/tmux-example/default",
            "session_name": name,
        }

    async def kill_session(self, name: str) -> None:
        self.kills += 1
        if self.dies_on_graceful:
            self.alive = False


class RecordingWitness:
    """Synthetic kernel handle for synthetic process tables; never calls kill."""
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminal = False
        self.closed = False
        self.sent: list[int] = []

    def exited(self) -> bool:
        return self.terminal

    def send(self, sig: int) -> bool:
        if self.exited():
            return False
        self.sent.append(sig)
        return True

    def close(self) -> None:
        self.closed = True


class AnchoredLadderTmux(LadderTmux):
    """Explicit synthetic pane authority, only for synthetic process tables."""
    async def close_pane_identity(self, name: str) -> dict[str, str] | None:
        pane = await self.pane_identity(name)
        if pane is None:
            return None
        return {**pane, "session_id": "$1", "server_pid": "1", "server_version": "3.7c",
                "pane_dead": "0", "pane_pipe": "0", "pane_dead_status": "",
                "pane_dead_signal": ""}


class DecisionCaptureTmux(LadderTmux):
    def __init__(self, captures: list[str]) -> None:
        super().__init__(dies_on_graceful=True)
        self.captures = list(captures)
        self.capture_calls = 0

    async def capture_checked(self, _name: str, **kwargs: object) -> tuple[bool, str]:
        self.capture_calls += 1
        if self.captures:
            value = self.captures.pop(0)
        else:
            value = "ready\n"
        return True, value


class RetryPeerTmux:
    """The first peer kill loses its transport; the next bounded attempt wins."""

    def __init__(self) -> None:
        self.alive = True
        self.state_calls = 0
        self.kill_calls = 0
        self.first_kill = asyncio.Event()

    async def session_state(self, _name: str) -> str:
        self.state_calls += 1
        return "alive" if self.alive else "gone"

    async def kill_session(self, _name: str) -> None:
        self.kill_calls += 1
        if self.kill_calls == 1:
            self.first_kill.set()
        if self.kill_calls == 1:
            raise VerbError("tmux_timeout", "transient peer tmux timeout")
        self.alive = False


class BlockingPeerTmux:
    def __init__(self) -> None:
        self.alive = True
        self.state_started = asyncio.Event()
        self.release_state = asyncio.Event()
        self.kill_calls = 0
        self.state_calls = 0

    async def session_state(self, _name: str) -> str:
        self.state_calls += 1
        if self.state_calls == 1:
            self.state_started.set()
            await self.release_state.wait()
        return "alive" if self.alive else "gone"

    async def kill_session(self, _name: str) -> None:
        self.kill_calls += 1
        self.alive = False


class ConcurrentPeerTmux:
    def __init__(self) -> None:
        self.alive = True
        self.kill_calls = 0
        self.state_calls = 0

    async def session_state(self, _name: str) -> str:
        self.state_calls += 1
        state = "alive" if self.alive else "gone"
        await asyncio.sleep(0)
        return state

    async def kill_session(self, _name: str) -> None:
        self.kill_calls += 1
        self.alive = False


class IdentityRetryPeerTmux(RetryPeerTmux):
    def __init__(self) -> None:
        super().__init__()
        self.identity_version = 1

    async def pane_identity(self, name: str) -> dict[str, str]:
        return {
            "pane_pid": str(self.identity_version),
            "pane_id": f"%{self.identity_version}",
            "tty": f"/dev/ttys00{self.identity_version}",
            "tmux_socket": "/tmp/tmux-example/default",
            "session_name": name,
        }


class BlockingLocalTmux:
    def __init__(self) -> None:
        self.alive = True
        self.has_started = asyncio.Event()
        self.release_has = asyncio.Event()
        self.has_calls = 0
        self.kill_calls = 0

    async def has_session(self, _name: str) -> bool:
        self.has_calls += 1
        if self.has_calls == 1:
            self.has_started.set()
            await self.release_has.wait()
        return self.alive

    async def pane_pid(self, _name: str) -> str:
        return ""

    async def pane_identity(self, _name: str) -> None:
        return None

    async def kill_session(self, _name: str) -> None:
        self.kill_calls += 1
        self.alive = False


class PeerCloseHosts:
    local_host = "hosta"

    def __init__(self, tmux: object) -> None:
        self.tmux = tmux

    def known(self, host: str) -> bool:
        return host == "hostb"

    def is_local(self, host: str) -> bool:
        return host == self.local_host

    async def probe_once(self, host: str) -> bool:
        return host == "hostb"

    def tmux_for(self, host: str) -> object:
        assert host == "hostb"
        return self.tmux


async def _seed(store: Store, name: str) -> None:
    await store.open_session(HOST, name, visibility="visible")


async def _close(tmux: LadderTmux, name: str) -> tuple[dict, Store, RecordingAlerts]:
    store = Store(":memory:")
    store.start()
    alerts = RecordingAlerts()
    sessions = Sessions(store, tmux=tmux, local_host=HOST, alerts=alerts)
    await _seed(store, name)
    await sessions.refresh()
    result = await sessions.close(HOST, name)
    return result, store, alerts


def test_graceful_kill_confirms_close_ok() -> None:
    async def go() -> None:
        tmux = LadderTmux(pid="", dies_on_graceful=True)
        result, store, alerts = await _close(tmux, "s-graceful")
        try:
            assert result["failed"] is False
            assert "degraded" not in result
            assert result["session"]["status"] == "closed"
            assert tmux.kills == 1                       # graceful only, no escalation
            assert alerts.emitted == []
            row = await store.fetch_session(HOST, "s-graceful")
            assert row["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def _spawn_orphan() -> str:
    r, w = os.pipe()
    ready_r, ready_w = os.pipe()
    os.set_inheritable(ready_w, True)
    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        os.close(r)
        os.close(ready_r)
        os.setsid()
        grandchild = os.fork()
        if grandchild == 0:  # pragma: no cover - grandchild
            os.close(w)
            os.execv(sys.executable, [sys.executable, "-c",
                f"import os,time; os.write({ready_w}, b'r'); os.close({ready_w}); time.sleep(300)"])
        os.close(ready_w)
        os.write(w, str(grandchild).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    os.close(ready_w)
    os.waitpid(child, 0)
    pid = os.read(r, 32).decode()
    os.close(r)
    try:
        assert os.read(ready_r, 1) == b"r"  # capture after exec, not the pre-exec shell
    finally:
        os.close(ready_r)
    return pid


def _spawn_sigterm_ignoring_process() -> int:
    ready_r, ready_w = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        os.close(ready_r)
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(ready_w, b"ready")
        os.close(ready_w)
        while True:
            signal.pause()
    os.close(ready_w)
    try:
        assert os.read(ready_r, len(b"ready")) == b"ready"
    finally:
        os.close(ready_r)
    return child


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # deliberate fork+exec of an orphan
def test_unanchored_orphan_refuses_force_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0.2)
    monkeypatch.setattr(sessions_mod, "CLOSE_KILL_CONFIRM_S", 2.0)
    pid = _spawn_orphan()
    assert prockill.pid_alive(pid)
    cleanup = prockill.ExitWitness(int(pid))

    async def go() -> None:
        tmux = LadderTmux(pid=pid, dies_on_graceful=False)  # tmux never confirms death
        result, store, alerts = await _close(tmux, "s-sigkill")
        try:
            assert result["failed"] is True
            assert result["reason"] == "logical_ownership_unproven"
            assert result["session"]["status"] == "open"
            assert result["reap_status"] == "unknown"
            assert all("ownership_proof_v2" not in item for item in result["survivors"])
            # An owned test orphan is not thereby a logically anchored tmux pane.
            assert not cleanup.exited()
            assert [kind for kind, _ in alerts.emitted] == ["close_failed"]
        finally:
            store.stop()

    try:
        asyncio.run(go())
    finally:
        try:
            cleanup.send(signal.SIGKILL)
        finally:
            cleanup.close()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")  # deliberate child-process fixture
def test_close_reports_real_sigterm_ignoring_process_as_alive_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pid_alive = prockill.pid_alive
    pid = _spawn_sigterm_ignoring_process()
    try:
        os.kill(pid, signal.SIGTERM)
        assert real_pid_alive(str(pid))

        liveness_probes: list[str] = []

        async def incomplete_records() -> dict[int, dict[str, object]]:
            # Keep the process real while forcing the same incomplete inventory
            # branch that formerly mislabeled stale pre-kill snapshots.
            return {}

        def observe_pid(candidate: str) -> bool:
            liveness_probes.append(str(candidate))
            return real_pid_alive(str(candidate))

        monkeypatch.setattr(sessions_mod.prockill, "process_records", incomplete_records)
        monkeypatch.setattr(sessions_mod.prockill, "pid_alive", observe_pid)

        async def go() -> None:
            tmux = LadderTmux(pid=str(pid), dies_on_graceful=True)
            result, store, _alerts = await _close(tmux, "s-live-survivor")
            try:
                assert result["failed"] is False
                assert result["session"]["status"] == "closed"
                assert result["reap_status"] == "unknown"
                assert len(result["survivors"]) == 1
                survivor = result["survivors"][0]
                assert survivor["pid"] == pid
                assert survivor["reap_reason"] == "logical_ownership_unproven"
                assert real_pid_alive(str(pid))

                reap = await store.get_session_reap(f"{HOST}:s-live-survivor")
                assert reap is not None
                assert reap["reap_status"] == "unknown"
                assert reap["survivors"][0]["reap_reason"] == "logical_ownership_unproven"
                assert liveness_probes == [str(pid)]
            finally:
                store.stop()

        asyncio.run(go())
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


def test_dstate_carcass_marks_closed_and_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0.2)
    monkeypatch.setattr(sessions_mod, "CLOSE_KILL_CONFIRM_S", 0.2)
    # SIGKILL is delivered but the pid never clears — a kernel D-state husk.
    monkeypatch.setattr(prockill, "pid_alive", lambda pid: True)

    async def records():
        return {999999: {"pid": 999999, "ppid": 1, "uid": 501, "pgid": 999999,
                         "sid": 999999, "start_id": "Mon Jan 1 00:00:00 2026", "command": "husk"}}
    async def boot(): return "boot-1"
    monkeypatch.setattr(prockill, "ExitWitness", RecordingWitness)
    monkeypatch.setattr(prockill, "process_records", records)
    monkeypatch.setattr(prockill, "boot_id", boot)

    async def go() -> None:
        tmux = AnchoredLadderTmux(pid="999999", dies_on_graceful=False)
        result, store, alerts = await _close(tmux, "s-carcass")
        try:
            # A husk that can never run userspace again: the row is closed as
            # fact, and the operator is alerted with the carcass pid.
            assert result["failed"] is False
            assert result["session"]["status"] == "closed"
            row = await store.fetch_session(HOST, "s-carcass")
            assert row["status"] == "closed"
            assert [k for k, _ in alerts.emitted] == ["close_carcass"]
            assert alerts.emitted[0][1]["pane_pid"] == "999999"
        finally:
            store.stop()

    asyncio.run(go())


def test_no_deliverable_signal_is_close_failed_row_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0.2)

    async def go() -> None:
        # tmux won't confirm the kill AND there is no pane pid to signal.
        tmux = LadderTmux(pid="", dies_on_graceful=False)
        result, store, alerts = await _close(tmux, "s-failed")
        try:
            assert result["failed"] is True
            assert result["reason"] == "logical_ownership_unproven"
            # The row is LEFT OPEN — never closed over a maybe-live process.
            row = await store.fetch_session(HOST, "s-failed")
            assert row["status"] == "open"
            assert [k for k, _ in alerts.emitted] == ["close_failed"]
        finally:
            store.stop()

    asyncio.run(go())


def test_close_persists_descendant_survivor_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    root_gone = False
    handles = {}
    async def records() -> dict[int, dict[str, object]]:
        table = {
            100: {
                "pid": 100, "ppid": 1, "uid": 501, "pgid": 100, "sid": 100,
                "start_id": "Mon Jan 1 00:00:00 2026", "command": "target-shell",
            },
            101: {
                "pid": 101, "ppid": 100, "uid": 501, "pgid": 100, "sid": 100,
                "start_id": "Mon Jan 1 00:00:01 2026", "command": "target-child",
            },
        }
        if root_gone:
            table.pop(100)
            table[101]["ppid"] = 1
        return table

    async def boot() -> str:
        return "12345678-1234-1234-1234-123456789abc"

    monkeypatch.setattr(sessions_mod.prockill, "process_records", records)
    monkeypatch.setattr(sessions_mod.prockill, "boot_id", boot)
    monkeypatch.setattr(sessions_mod.prockill, "pid_alive", lambda pid: str(pid) == "101")

    class SurvivorWitness(RecordingWitness):
        def __init__(self, pid):
            super().__init__(pid)
            handles[pid] = self
    monkeypatch.setattr(prockill, "ExitWitness", SurvivorWitness)

    class Tmux(AnchoredLadderTmux):
        async def kill_session(self, name):
            nonlocal root_gone
            root_gone = True
            handles[100].terminal = True
            await super().kill_session(name)

    async def go() -> None:
        tmux = Tmux(pid="100", dies_on_graceful=True)
        result, store, _alerts = await _close(tmux, "s-survivor-proof")
        try:
            assert result["failed"] is False
            assert result["reap_status"] == "survivors"
            reap = await store.get_session_reap(f"{HOST}:s-survivor-proof")
            assert reap is not None
            assert reap["reap_status"] == "survivors"
            survivor = reap["survivors"][0]
            assert survivor["pid"] == 101
            assert set(survivor["ownership_proof_v2"]) == {
                "version", "host", "uid", "boot_id", "pid", "start_id", "ppid",
                "pgid", "sid", "tmux_socket", "tmux_session", "tmux_pane", "tty",
                "captured_at", "command_fingerprint",
            }
        finally:
            store.stop()

    asyncio.run(go())


def test_close_incomplete_process_inventory_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing_records() -> dict[int, dict[str, object]]:
        return {}

    probes: list[str] = []

    def dead_pid(pid: str) -> bool:
        probes.append(str(pid))
        return False

    monkeypatch.setattr(sessions_mod.prockill, "process_records", missing_records)
    monkeypatch.setattr(sessions_mod.prockill, "pid_alive", dead_pid)

    async def go() -> None:
        tmux = LadderTmux(pid="100", dies_on_graceful=True)
        result, store, _alerts = await _close(tmux, "s-incomplete-inventory")
        try:
            assert result["failed"] is False
            assert result["reap_status"] == "unknown"
            reap = await store.get_session_reap(f"{HOST}:s-incomplete-inventory")
            assert reap is not None
            assert reap["reap_status"] == "unknown"
            assert reap["survivors"] == []
            assert probes == ["100"]
        finally:
            store.stop()

    asyncio.run(go())


def test_reap_reason_separates_inventory_incomplete_from_process_observed_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(":memory:")
    store.start()
    try:
        sessions = Sessions(store, tmux=None, local_host=HOST)
        reconciler = SessionReconciler(sessions, object())
        safe, deferred = reconciler._identity_safe_records(
            HOST,
            "s-legacy-incomplete",
            [{"pid": 101, "reap_reason": "inventory_incomplete"}],
            [],
            {},
            "12345678-1234-1234-1234-123456789abc",
        )
        assert safe == []
        assert deferred[0]["reap_reason"] == "inventory_incomplete"
    finally:
        store.stop()

    probes: list[str] = []

    def live_pid(pid: str) -> bool:
        probes.append(str(pid))
        return True

    monkeypatch.setattr(sessions_mod.prockill, "pid_alive", live_pid)
    async def no_records(): return {}
    monkeypatch.setattr(prockill, "process_records", no_records)
    observed = asyncio.run(prockill.CapturedTree([101], {}, {}, False).readback())
    assert observed["survivors"][0]["reap_reason"] == "logical_ownership_unproven"
    assert observed["survivors"][0]["reap_reason"] != "inventory_incomplete"
    assert probes == ["101"]


def test_close_without_pane_at_entry_stays_unknown() -> None:
    async def go() -> None:
        tmux = LadderTmux(pid="", dies_on_graceful=False)
        tmux.alive = False
        result, store, _alerts = await _close(tmux, "s-no-pane-at-entry")
        try:
            assert result["failed"] is False
            assert result["reap_status"] == "unknown"
            reap = await store.get_session_reap(f"{HOST}:s-no-pane-at-entry")
            assert reap is not None and reap["reap_status"] == "unknown"
        finally:
            store.stop()

    asyncio.run(go())


def test_peer_close_retries_transient_tmux_failure_and_confirms_death() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = RetryPeerTmux()
        hosts = PeerCloseHosts(tmux)
        sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
        try:
            await store.open_session(
                "hostb", "v2-close-retry", visibility="hidden", pane_status="pane_alive",
            )
            await sessions.refresh()
            result = await sessions.close("hostb", "v2-close-retry", "report_terminate")
            assert result["failed"] is False
            assert result["session"]["status"] == "closed"
            assert tmux.kill_calls == 2
            row = await store.fetch_session("hostb", "v2-close-retry")
            assert row is not None and row["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_peer_terminating_report_uses_close_retry_once() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = RetryPeerTmux()
        hosts = PeerCloseHosts(tmux)
        sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
        try:
            await store.open_session(
                "hostb", "v2-report-close", visibility="hidden", pane_status="pane_alive",
            )
            await sessions.refresh()
            reply = await Ledger(store, sessions=sessions).report({
                "from_stream_id": "hostb:v2-report-close",
                "report_id": "report-close-retry",
                "msg_id": 0,
                "status": "done",
                "summary": "peer complete",
                "findings": [],
                "next_action": "none",
                "terminate": True,
            })
            assert reply["durability_ack"] is True
            assert reply["closed"] is True
            assert tmux.kill_calls == 2
            again = await sessions.close("hostb", "v2-report-close", "reconciler_backstop")
            assert again["already_closed"] is True
            assert tmux.kill_calls == 2
            row = await store.fetch_session("hostb", "v2-report-close")
            assert row is not None and row["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_peer_close_does_not_close_reopened_generation() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = BlockingPeerTmux()
        hosts = PeerCloseHosts(tmux)
        sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
        name = "v2-close-reopen"
        try:
            first = await store.open_session("hostb", name, visibility="hidden")
            await sessions.refresh()
            close_task = asyncio.create_task(sessions.close("hostb", name, "stale-close"))
            await tmux.state_started.wait()
            reopen_task = asyncio.create_task(
                sessions.open("hostb", name, visibility="hidden")
            )
            await asyncio.sleep(0)
            tmux.release_state.set()
            await close_task
            await reopen_task
            current = await store.fetch_session("hostb", name)
            assert current is not None
            assert current["status"] == "open"
            assert current["session_generation"] != first["session_generation"]
            assert tmux.kill_calls == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_local_close_does_not_close_reopened_generation() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = BlockingLocalTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        name = "s-local-close-reopen"
        try:
            first = await store.open_session(HOST, name, visibility="hidden")
            await sessions.refresh()
            close_task = asyncio.create_task(sessions.close(HOST, name, "stale-local-close"))
            await tmux.has_started.wait()
            reopen_task = asyncio.create_task(sessions.open(HOST, name, visibility="hidden"))
            tmux.release_has.set()
            await close_task
            await reopen_task
            current = await store.fetch_session(HOST, name)
            assert current is not None and current["status"] == "open"
            assert current["session_generation"] != first["session_generation"]
            assert tmux.kill_calls == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_concurrent_peer_closes_issue_one_kill() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = ConcurrentPeerTmux()
        hosts = PeerCloseHosts(tmux)
        sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
        name = "v2-close-concurrent"
        try:
            await store.open_session("hostb", name, visibility="hidden")
            await sessions.refresh()
            await asyncio.gather(
                sessions.close("hostb", name, "close-1"),
                sessions.close("hostb", name, "close-2"),
            )
            assert tmux.kill_calls == 1
            current = await store.fetch_session("hostb", name)
            assert current is not None and current["status"] == "closed"
        finally:
            store.stop()

    asyncio.run(go())


def test_peer_close_retry_cannot_close_reopened_generation() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = RetryPeerTmux()
        hosts = PeerCloseHosts(tmux)
        sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
        name = "v2-close-retry-reopen"
        try:
            await store.open_session("hostb", name, visibility="hidden")
            await sessions.refresh()
            close_task = asyncio.create_task(sessions.close("hostb", name, "stale-retry"))
            await tmux.first_kill.wait()
            reopened = await store.open_session("hostb", name, visibility="hidden")
            await close_task
            current = await store.fetch_session("hostb", name)
            assert current is not None and current["status"] == "open"
            assert current["session_generation"] == reopened["session_generation"]
        finally:
            store.stop()

    asyncio.run(go())


def test_peer_close_retry_stops_when_pane_identity_changes() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = IdentityRetryPeerTmux()
        hosts = PeerCloseHosts(tmux)
        sessions = Sessions(store, tmux=None, local_host="hosta", hosts=hosts)
        name = "v2-close-identity-reuse"
        try:
            await store.open_session("hostb", name, visibility="hidden")
            await sessions.refresh()
            close_task = asyncio.create_task(sessions.close("hostb", name, "identity-reuse"))
            await tmux.first_kill.wait()
            tmux.identity_version = 2
            result = await close_task
            current = await store.fetch_session("hostb", name)
            assert result["failed"] is True
            assert result["reason"] == "pane_identity_changed"
            assert tmux.kill_calls == 1
            assert current is not None and current["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_close_reports_live_direct_child() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = LadderTmux(dies_on_graceful=True)
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            await store.open_session(HOST, "parent", visibility="hidden")
            await store.open_session(
                HOST, "child", visibility="hidden", parent_stream_id=f"{HOST}:parent",
            )
            await sessions.refresh()
            with pytest.raises(VerbError) as refused:
                await sessions.close(HOST, "parent")
            assert refused.value.code == "close_live_children"
            assert tmux.kills == 0
            assert refused.value.extra["live_children"] == [{
                "stream_id": f"{HOST}:child",
                "host": HOST,
                "status": "open",
                "parent_stream_id": f"{HOST}:parent",
            }]
        finally:
            store.stop()

    asyncio.run(go())


def test_close_omits_closed_child() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = LadderTmux(dies_on_graceful=True)
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            await store.open_session(HOST, "parent", visibility="hidden")
            await store.open_session(
                HOST, "child", visibility="hidden", parent_stream_id=f"{HOST}:parent",
            )
            await store.mark_closed(
                HOST, "child", closed_at="2026-08-28T00:00:00Z", pane_status="pane_dead",
            )
            await sessions.refresh()
            result = await sessions.close(HOST, "parent")
            assert result["live_children"] == []
        finally:
            store.stop()

    asyncio.run(go())


def test_close_reports_cross_host_child() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = LadderTmux(dies_on_graceful=True)
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            await store.open_session(HOST, "parent", visibility="hidden")
            await store.open_session(
                "hostb", "child", visibility="hidden", parent_stream_id=f"{HOST}:parent",
            )
            await sessions.refresh()
            with pytest.raises(VerbError) as refused:
                await sessions.close(HOST, "parent")
            assert refused.value.code == "close_live_children"
            assert tmux.kills == 0
            assert refused.value.extra["live_children"] == [{
                "stream_id": "hostb:child",
                "host": "hostb",
                "status": "open",
                "parent_stream_id": f"{HOST}:parent",
            }]
        finally:
            store.stop()

    asyncio.run(go())


def test_close_does_not_mutate_live_child() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            tmux = LadderTmux(dies_on_graceful=True)
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            await store.open_session(HOST, "parent", visibility="hidden")
            await store.open_session(
                HOST, "child", visibility="hidden", parent_stream_id=f"{HOST}:parent",
            )
            await sessions.refresh()
            with pytest.raises(VerbError) as refused:
                await sessions.close(HOST, "parent")
            child = await store.fetch_session(HOST, "child")
            assert refused.value.extra["live_children"]
            assert child is not None and child["status"] == "open"
            assert (await store.fetch_session(HOST, "parent"))["status"] == "open"
        finally:
            store.stop()

    asyncio.run(go())


def test_reap_fenced_on_wedged_unknown() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        alerts = RecordingAlerts()
        tmux = LadderTmux(dies_on_graceful=True)
        sessions = Sessions(store, tmux=tmux, local_host=HOST, alerts=alerts)
        try:
            await store.open_session(HOST, "wedged", visibility="hidden")
            await sessions.refresh()
            sessions.apply_live(f"{HOST}:wedged", capture_liveness="wedged_unknown")
            result = await sessions.reap_idle(HOST, "wedged")
            assert result["failed"] is True
            assert result["fenced"] is True
            assert tmux.kills == 0
            assert alerts.emitted[-1][0] == "reap_fenced"
        finally:
            store.stop()

    asyncio.run(go())


def test_reap_fenced_on_transport_unknown() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        alerts = RecordingAlerts()
        tmux = LadderTmux(dies_on_graceful=True)
        sessions = Sessions(store, tmux=tmux, local_host=HOST, alerts=alerts)
        try:
            await store.open_session(HOST, "transport", visibility="hidden")
            await sessions.refresh()
            sessions.apply_live(f"{HOST}:transport", capture_liveness="transport_unknown")
            result = await sessions.reap_idle(HOST, "transport")
            assert result["failed"] is True
            assert result["fenced"] is True
            assert tmux.kills == 0
            assert alerts.emitted[-1][0] == "reap_fenced"
        finally:
            store.stop()


def test_idle_capture_permits_reap() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        tmux = DecisionCaptureTmux(["ready\n"])
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        try:
            await store.open_session(HOST, "idle", visibility="hidden")
            await sessions.refresh()
            sessions.apply_live(f"{HOST}:idle", capture_liveness="idle")
            result = await sessions.reap_idle(HOST, "idle")
            assert result["failed"] is False
            assert tmux.kills == 1
        finally:
            store.stop()

    asyncio.run(go())


def test_reap_reprobes_stale_idle_before_kill() -> None:
    async def go() -> None:
        store = Store(":memory:")
        store.start()
        alerts = RecordingAlerts()
        tmux = DecisionCaptureTmux(["   \n", "\n"])
        sessions = Sessions(store, tmux=tmux, local_host=HOST, alerts=alerts)
        try:
            await store.open_session(HOST, "stale-idle", visibility="hidden")
            await sessions.refresh()
            sessions.apply_live(
                f"{HOST}:stale-idle", capture_liveness="idle", working=False,
            )
            result = await sessions.reap_idle(HOST, "stale-idle")
            assert result["failed"] is True
            assert result["fenced"] is True
            assert tmux.capture_calls == 2
            assert tmux.kills == 0
            assert sessions.get(f"{HOST}:stale-idle")["capture_liveness"] == "wedged_unknown"
            assert alerts.emitted[-1][0] == "reap_fenced"
        finally:
            store.stop()

    asyncio.run(go())


def test_force_close_refuses_stale_captured_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Sessions→prockill boundary must refuse A replaced by B."""
    from types import SimpleNamespace
    captured = {"pid": 990001, "ppid": 1, "uid": 501, "pgid": 990001,
                "sid": 990001, "start_id": "Mon Jan 1 00:00:00 2026", "command": "owned-A"}
    proof = prockill.identity_proof(host=HOST, boot="boot-1", record=captured,
        tmux_session="s-stale", tmux_pane="%1", tty="/dev/test", tmux_socket="/tmp/test")
    async def capture(*_args):
        return [990001], {990001: captured}, {990001: proof}, True
    async def current():
        return {990001: {**captured, "start_id": "Mon Jan 1 00:00:01 2026", "command": "foreign-B"}}
    async def tree(_root):
        return ["990001", "990002"]  # late foreign descendant must never be signalled
    async def boot(): return "boot-1"
    async def not_gone(*_args): return False
    deliveries = []
    monkeypatch.setattr(prockill, "process_records", current)
    monkeypatch.setattr(prockill, "process_tree", tree)
    monkeypatch.setattr(prockill, "boot_id", boot)
    monkeypatch.setattr(prockill.os, "kill", lambda pid, sig: deliveries.append((pid, sig)) if sig else None)
    monkeypatch.setattr(prockill, "ExitWitness", RecordingWitness)
    tmux = AnchoredLadderTmux(pid="990001")
    receiver = SimpleNamespace(tmux=tmux, _capture_tree_identity=capture,
        _confirm_pane_gone=not_gone)
    state, _, _ = asyncio.run(Sessions._terminate_pane(receiver, "s-stale"))
    assert deliveries == [], f"stale instance reached destructive sink: {deliveries}"
    assert state == "failed"


def _cohort_rig(monkeypatch, *, complete=True):
    original = {
        990001: {"pid": 990001, "ppid": 1, "uid": 501, "pgid": 990001,
                 "sid": 990001, "start_id": "Mon Jan 1 00:00:00 2026", "command": "owned-A"},
        990002: {"pid": 990002, "ppid": 990001, "uid": 501, "pgid": 990001,
                 "sid": 990001, "start_id": "Mon Jan 1 00:00:01 2026", "command": "owned-child"},
    }
    now = {pid: dict(rec) for pid, rec in original.items()}
    state = {"boot": "boot-1", "records": now}
    handles = {}
    def witness(pid):
        handles[pid] = RecordingWitness(pid)
        return handles[pid]
    async def records(): return state["records"]
    async def boot(): return state["boot"]
    monkeypatch.setattr(prockill, "ExitWitness", witness)
    monkeypatch.setattr(prockill, "process_records", records)
    monkeypatch.setattr(prockill, "boot_id", boot)
    proofs = {pid: prockill.identity_proof(host=HOST, boot="boot-1", record=rec,
              tmux_session="owned", tmux_pane="%1", tty="/dev/test", tmux_socket="/tmp/test")
              for pid, rec in original.items()}
    cohort = prockill.CapturedTree(list(original), original, proofs, complete)
    cohort.anchored = True  # component tests supply synthetic logical authority explicitly
    return cohort, state, handles


@pytest.mark.parametrize("field,value", [
    ("pid", 999998), ("uid", 502), ("pgid", 990003), ("sid", 990003),
    ("start_id", "Mon Jan 1 00:00:02 2026"), ("start_id", ""),
    ("command", "foreign-B"), ("ppid", 990003), ("boot", "boot-2"),
])
def test_close_cohort_refuses_changed_authority(monkeypatch, field, value):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        try:
            await cohort.prepare()
            if field == "boot": state["boot"] = value
            else: state["records"][990001][field] = value
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert 990001 not in sent and 990001 in refused
            assert handles[990001].sent == []
        finally:
            cohort.close()
        assert all(h.closed for h in handles.values())
    asyncio.run(go())


@pytest.mark.parametrize("fault", ["missing_birth", "missing_proof", "partial", "unreadable", "registration", "registration_drift"])
def test_close_cohort_missing_capture_authority_never_falls_back(monkeypatch, fault):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch, complete=fault != "partial")
        if fault == "missing_birth": cohort.records[990001]["start_id"] = ""
        if fault == "missing_proof": cohort.proofs.clear()
        if fault == "unreadable": state["records"] = {}
        if fault == "registration":
            def denied(_pid): raise PermissionError("synthetic registration denied")
            monkeypatch.setattr(prockill, "ExitWitness", denied)
        if fault == "registration_drift": state["records"][990001]["command"] = "foreign"
        try:
            await cohort.prepare()
            if fault == "registration_drift": state["records"][990001]["command"] = "owned-A"
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert 990001 not in sent and 990001 in refused
            assert all(not h.sent for pid, h in handles.items() if pid == 990001)
        finally:
            cohort.close()
    asyncio.run(go())


def test_close_cohort_reparents_only_after_captured_parent_exit(monkeypatch):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        try:
            await cohort.prepare()
            state["records"][990002]["ppid"] = 1
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert 990002 in refused and not handles[990002].sent
            handles[990001].terminal = True
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert sent == {990002} and refused == set()
            reap = await cohort.readback()
            assert [x["pid"] for x in reap["survivors"]] == [990002]
            assert reap["survivors"][0]["ownership_proof_v2"]["ppid"] == 1
        finally:
            cohort.close()
    asyncio.run(go())


def test_close_cohort_never_signals_late_or_foreign_process(monkeypatch):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        try:
            await cohort.prepare()
            state["records"][990003] = {**state["records"][990002], "pid": 990003, "ppid": 990002}
            state["records"][990004] = {**state["records"][990002], "pid": 990004, "ppid": 1}
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert sent == {990001, 990002} and refused == set()
            assert set(handles) == {990001, 990002}
            reap = await cohort.readback()
            by_pid = {x["pid"]: x for x in reap["survivors"]}
            assert 990004 not in by_pid
            assert "ownership_proof_v2" in by_pid[990003]
            # A late descendant observed below a drifted instance stays unknown.
            state["records"][990002]["command"] = "foreign-child"
            reap = await cohort.readback()
            by_pid = {x["pid"]: x for x in reap["survivors"]}
            assert reap["reap_status"] == "unknown"
            assert "ownership_proof_v2" not in by_pid[990003]
        finally:
            cohort.close()
    asyncio.run(go())


@pytest.mark.parametrize("phase", ["grace", "fresh_read", "final_check"])
def test_close_cohort_exit_before_delivery_sends_nothing(monkeypatch, phase):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        try:
            await cohort.prepare()
            root = handles[990001]
            if phase == "grace": root.terminal = True
            elif phase == "fresh_read":
                async def records():
                    root.terminal = True
                    return state["records"]
                monkeypatch.setattr(prockill, "process_records", records)
            else:
                def final_exit(_sig):
                    root.terminal = True
                    return False
                root.send = final_exit
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert 990001 not in sent and 990001 not in refused
            assert root.sent == []
        finally:
            cohort.close()
    asyncio.run(go())


@pytest.mark.parametrize("where", ["prepare", "grace"])
def test_close_cancellation_closes_every_registered_witness(monkeypatch, where):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        async def capture(*_args): return list(cohort.pids), cohort.records, cohort.proofs, True
        async def cancelled(*_args): raise asyncio.CancelledError
        if where == "prepare": monkeypatch.setattr(prockill, "process_records", cancelled)
        from types import SimpleNamespace
        tmux = AnchoredLadderTmux(pid="990001")
        if where == "grace": tmux.kill_session = cancelled
        receiver = SimpleNamespace(tmux=tmux, _capture_tree_identity=capture)
        with pytest.raises(asyncio.CancelledError):
            await Sessions._terminate_pane(receiver, "owned")
        assert set(handles) == {990001, 990002}
        assert all(h.closed and not h.sent for h in handles.values())
    asyncio.run(go())


def test_close_refusal_persists_open_obligation_and_retry_completes(monkeypatch):
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0)
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        # Only root participates here. The child's record is foreign to this test's cohort.
        state["records"].pop(990002)
        state["boot"] = ""
        tmux = AnchoredLadderTmux(pid="990001")
        store = Store(":memory:"); store.start()
        alerts = RecordingAlerts()
        sessions = Sessions(store=store, tmux=tmux, local_host=HOST, alerts=alerts)
        try:
            await _seed(store, "retry")
            result = await sessions.close(HOST, "retry")
            assert result["failed"] and result["reason"] == "logical_ownership_unproven"
            assert (await store.fetch_session(HOST, "retry"))["status"] == "open"
            reap = await store.get_session_reap(f"{HOST}:retry")
            assert reap["reap_status"] == "unknown"
            assert reap["survivors"][0]["reap_reason"] == "logical_ownership_unproven"
            assert all(not h.sent and h.closed for h in handles.values())
            state["boot"] = "boot-1"
            def witness(pid):
                handle = RecordingWitness(pid)
                def send(sig):
                    handle.sent.append(sig); handle.terminal = True
                    return True
                handle.send = send
                handles[pid] = handle
                return handle
            monkeypatch.setattr(prockill, "ExitWitness", witness)
            result = await sessions.close(HOST, "retry")
            assert not result["failed"] and result["session"]["status"] == "closed"
            assert handles[990001].sent == [signal.SIGKILL]
            repeated = await sessions.close(HOST, "retry")
            assert repeated["already_closed"] and handles[990001].sent == [signal.SIGKILL]
        finally:
            store.stop()
    asyncio.run(go())


def test_linux_witness_delivers_only_through_retained_pidfd(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(prockill, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(prockill.os, "pidfd_open", lambda pid, flags: calls.append(("open", pid)) or 42, raising=False)
    monkeypatch.setattr(prockill.select, "select", lambda *_args: ([], [], []))
    monkeypatch.setattr(prockill.signal, "pidfd_send_signal", lambda fd, sig: calls.append(("pidfd", fd, sig)), raising=False)
    monkeypatch.setattr(prockill.os, "kill", lambda *_args: pytest.fail("numeric PID delivery on Linux"))
    monkeypatch.setattr(prockill.os, "close", lambda fd: calls.append(("close", fd)))
    witness = prockill.ExitWitness(990001)
    assert witness.send(signal.SIGKILL)
    witness.close()
    assert calls == [("open", 990001), ("pidfd", 42, signal.SIGKILL), ("close", 42)]


@pytest.mark.parametrize("exit_at_final_check", [False, True])
def test_darwin_witness_final_check_and_explicit_nonatomic_residual(monkeypatch, exit_at_final_check):
    from types import SimpleNamespace
    calls = []
    state = {"armed": False, "reused_after_check": False}
    class Queue:
        def control(self, changes, count, timeout):
            calls.append("register" if changes else "check")
            if state["armed"] and exit_at_final_check:
                return [SimpleNamespace(ident=990001, filter=1, flags=0, fflags=2)]
            if state["armed"]:
                # Documents the accepted Darwin residual: an exit/reuse after
                # this check cannot be made atomic with the following kill.
                state["reused_after_check"] = True
            return []
        def close(self): calls.append("close")
    monkeypatch.setattr(prockill, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(prockill, "select", SimpleNamespace(kqueue=Queue,
        kevent=lambda *args, **kwargs: object(), KQ_FILTER_PROC=1, KQ_NOTE_EXIT=2,
        KQ_EV_ADD=4, KQ_EV_ENABLE=8, KQ_EV_ERROR=16))
    monkeypatch.setattr(prockill.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    witness = prockill.ExitWitness(990001)
    calls.clear(); state["armed"] = True
    try:
        assert witness.send(signal.SIGKILL) is (not exit_at_final_check)
        if exit_at_final_check:
            assert calls == ["check"] and witness.exited()
        else:
            assert calls == ["check", ("kill", 990001, signal.SIGKILL)]
            assert state["reused_after_check"]  # no atomicity claim
    finally:
        witness.close()


def test_close_witness_observation_error_cannot_create_readback_proof(monkeypatch):
    async def go():
        cohort, state, handles = _cohort_rig(monkeypatch)
        try:
            await cohort.prepare()
            def denied(): raise PermissionError("synthetic observation denied")
            handles[990001].exited = denied
            sent, refused = await cohort.signal(signal.SIGKILL)
            assert 990001 not in sent and 990001 in refused
            reap = await cohort.readback()
            root = next(x for x in reap["survivors"] if x["pid"] == 990001)
            assert "ownership_proof_v2" not in root
            assert reap["reap_status"] == "unknown"
        finally:
            cohort.close()
    asyncio.run(go())


@pytest.mark.parametrize("missing_pane_identity", [False, True])
def test_disposable_private_tmux_force_close_preserves_foreign_sentinel(
    monkeypatch, isolated_tmux_env, missing_pane_identity,
):
    """Real pane/process tree, with only graceful teardown made unresponsive."""
    from tmux_transport import Tmux
    import subprocess
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0.05)
    monkeypatch.setattr(sessions_mod, "CLOSE_KILL_CONFIRM_S", 3)
    async def go():
        tmux = Tmux(isolated_tmux_env)
        store = Store(":memory:"); store.start()
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        pane_pid = ""
        captured = []
        deliveries = []
        original_send = prockill.ExitWitness.send
        def record_send(witness, sig):
            assert witness.pid in captured
            assert witness.pid != sentinel.pid
            result = original_send(witness, sig)
            if result: deliveries.append(witness.pid)
            return result
        monkeypatch.setattr(prockill.ExitWitness, "send", record_send)
        try:
            # exec pins the pane's command before capture; its child is owned.
            await tmux.new_session("fence-owned", "exec " + sys.executable +
                " -c 'import subprocess,time; subprocess.Popen([\"sleep\",\"60\"]); time.sleep(60)'")
            pane_pid = await tmux.pane_pid("fence-owned")
            # Wait for the owned child to appear, using actual records.
            for _ in range(50):
                table = await prockill.process_records()
                if any(x["ppid"] == int(pane_pid) for x in table.values()): break
                await asyncio.sleep(0.02)
            else: pytest.fail("disposable pane did not create its child")
            original_kill = tmux.kill_session
            calls = 0
            async def wedged_grace(name):
                nonlocal calls
                calls += 1
                if calls > 1: await original_kill(name)
            monkeypatch.setattr(tmux, "kill_session", wedged_grace)
            if missing_pane_identity:
                async def no_identity(_name): return None
                monkeypatch.setattr(tmux, "close_pane_identity", no_identity)
            sessions = Sessions(store=store, tmux=tmux, local_host=HOST)
            capture = sessions._capture_tree_identity
            async def record_capture(*args):
                result = await capture(*args)
                captured.extend(result[0])
                return result
            monkeypatch.setattr(sessions, "_capture_tree_identity", record_capture)
            await _seed(store, "fence-owned")
            result = await sessions.close(HOST, "fence-owned")
            if missing_pane_identity:
                assert result["failed"] and result["reason"] == "logical_ownership_unproven"
                assert not deliveries and result["reap_status"] == "unknown"
                assert all("ownership_proof_v2" not in item for item in result["survivors"])
                assert sentinel.poll() is None
                assert await tmux.has_session("fence-owned")
                return
            assert not result["failed"], result
            assert result["session"]["status"] == "closed"
            assert len(captured) >= 2 and set(deliveries) == set(captured)
            assert sentinel.poll() is None
            assert not prockill.pid_alive(pane_pid)
            assert not await tmux.has_session("fence-owned")
            # A terminal child's exit witness is enough even before wait/reap.
            assert result["reap_status"] == "reaped" and not result["survivors"]
        finally:
            await tmux.run("kill-server")
            sentinel.terminate(); sentinel.wait(timeout=5)
            store.stop()
    asyncio.run(go())


def test_close_tmux_timeout_still_uses_independent_captured_authority(monkeypatch):
    async def go():
        cohort, _state, handles = _cohort_rig(monkeypatch)
        async def capture(*_args): return list(cohort.pids), cohort.records, cohort.proofs, True
        async def timeout(*_args): raise VerbError("tmux_timeout", "synthetic wedged tmux")
        from types import SimpleNamespace
        def witness(pid):
            handle = RecordingWitness(pid)
            def send(sig):
                handle.sent.append(sig); handle.terminal = True
                return True
            handle.send = send
            handles[pid] = handle
            return handle
        monkeypatch.setattr(prockill, "ExitWitness", witness)
        tmux = AnchoredLadderTmux(pid="990001")
        tmux.kill_session = timeout
        receiver = SimpleNamespace(tmux=tmux, _capture_tree_identity=capture)
        state, _, reap = await Sessions._terminate_pane(receiver, "owned")
        assert state == "ok" and reap["reap_status"] == "reaped"
        assert all(h.sent == [signal.SIGKILL] and h.closed for h in handles.values())
    asyncio.run(go())


@pytest.mark.parametrize("scenario", [
    "missing", "dead", "status_zero", "signal", "pipe", "unknown_version", "partial",
    "same_server_reuse", "other_parent", "hidden_window", "wrong_root", "wrong_session",
    "drift_pane_id", "drift_pane_pid", "drift_session_id", "drift_tty", "drift_socket",
    "drift_server", "drift_version", "post_missing", "exit_during_registration",
])
@pytest.mark.parametrize("graceful", [False, True])
def test_unanchored_close_never_signals_or_persists_new_authority(monkeypatch, scenario, graceful):
    """Actual close/capture/readback; synthetic inventory and recording-only handles."""
    from collections import defaultdict
    from tmux_transport import Tmux
    root, child, server = 441234567, 441234568, 441234569
    def rec(pid, ppid):
        return {"pid": pid, "ppid": ppid, "uid": 501, "pgid": root, "sid": root,
                "start_id": "Mon Jan 1 00:00:00 2026", "command": "recording-fixture"}
    state = {"records": {root: rec(root, server), child: rec(child, root)}}
    handles, deliveries = {}, []
    class Witness(RecordingWitness):
        def exited(self): return self.pid not in state["records"]
        def send(self, sig):
            deliveries.append((self.pid, int(sig)))
            state["records"].pop(self.pid, None)
            return True
    def witness(pid):
        handles[pid] = Witness(pid)
        if scenario == "exit_during_registration" and pid == child:
            state["records"].pop(root, None)
            state["records"][child]["ppid"] = 1
        return handles[pid]
    async def records(): return {pid: dict(row) for pid, row in state["records"].items()}
    async def boot(): return "boot-fixture"
    monkeypatch.setattr(prockill, "ExitWitness", witness)
    monkeypatch.setattr(prockill, "process_records", records)
    monkeypatch.setattr(prockill, "boot_id", boot)
    monkeypatch.setattr(prockill, "pid_alive", lambda pid: int(pid) in state["records"])
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0)
    monkeypatch.setattr(sessions_mod, "CLOSE_KILL_CONFIRM_S", 0)
    class BindingTmux(Tmux):
        def __init__(self):
            super().__init__()
            self.alive, self.reads, self.kills = True, 0, 0
        async def has_session(self, name): return self.alive
        async def pane_pid(self, name): return str(root)
        async def kill_session(self, name):
            self.kills += 1
            if scenario == "same_server_reuse":
                # The unrelated root exits during grace; its child reparents.
                state["records"].pop(root, None)
                state["records"][child]["ppid"] = 1
            if graceful: self.alive = False
        async def run(self, *args, **kwargs):
            fmt = args[-1]
            close_read = "#{session_id}" in fmt
            if close_read: self.reads += 1
            fields = {"pane_pid": str(root), "pane_id": "%1", "pane_tty": "/dev/test",
                      "socket_path": "/tmp/owned-fixture", "session_name": "anchor-refusal",
                      "session_id": "$7", "pid": str(server), "version": "3.7c",
                      "pane_dead": "0", "pane_pipe": "0", "pane_dead_status": "",
                      "pane_dead_signal": ""}
            if scenario in {"dead", "same_server_reuse"}: fields["pane_dead"] = "1"
            if scenario in {"status_zero", "same_server_reuse"}: fields["pane_dead_status"] = "0"
            if scenario == "signal": fields["pane_dead_signal"] = "term"
            if scenario == "exit_during_registration" and self.reads > 1:
                fields["pane_dead_status"] = "0"
            if scenario == "pipe": fields["pane_pipe"] = "1"
            if scenario == "unknown_version": fields["version"] = "unqualified"
            if scenario == "wrong_root": fields["pane_pid"] = str(root + 10)
            if scenario == "wrong_session": fields["session_name"] = "replacement"
            if scenario == "other_parent": state["records"][root]["ppid"] = server + 1
            if close_read and (scenario == "missing" or scenario == "post_missing" and self.reads > 1):
                return 1, "unavailable"
            if close_read and scenario == "partial": return 0, "incomplete|fields\n"
            drift = {"drift_pane_id": ("pane_id", "%2"), "drift_pane_pid": ("pane_pid", str(root + 10)),
                     "drift_session_id": ("session_id", "$8"), "drift_tty": ("pane_tty", "/dev/other"),
                     "drift_socket": ("socket_path", "/tmp/replacement"),
                     "drift_server": ("pid", str(server + 1)), "drift_version": ("version", "3.4")}
            if close_read and self.reads > 1 and scenario in drift:
                key, value = drift[scenario]; fields[key] = value
            for key, value in fields.items(): fmt = fmt.replace("#{" + key + "}", value)
            if close_read and scenario == "hidden_window": fmt += "\n" + fmt.replace("%1", "%2")
            return 0, fmt + "\n"
    async def go():
        store = Store(":memory:"); store.start()
        tmux = BindingTmux()
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        try:
            await _seed(store, "anchor-refusal")
            result = await sessions.close(HOST, "anchor-refusal")
            assert deliveries == [], (scenario, deliveries)
            changed = (scenario.startswith("drift_") or scenario in {
                "post_missing", "exit_during_registration", "wrong_root"})
            succeeded = graceful and not changed
            assert result["failed"] is not succeeded
            if not succeeded: assert result["reason"] == "logical_ownership_unproven"
            if changed: assert tmux.kills == 0  # never retarget a changed binding
            assert result["reap_status"] == "unknown"
            reap = await store.get_session_reap(f"{HOST}:anchor-refusal")
            assert reap is not None and reap["reap_status"] == "unknown"
            for item in reap["survivors"]:
                assert "ownership_proof_v2" not in item
                assert item["reap_reason"] == "logical_ownership_unproven"
            if succeeded:
                # Continue from the actual persisted close, through the ordinary reaper.
                reaper = SessionReconciler(sessions, None)
                async def inventory(_host): return [], await records(), "boot-fixture"
                async def signal_pids(_host, pids, sig):
                    deliveries.extend((pid, int(sig)) for pid in pids)
                    return True
                monkeypatch.setattr(reaper, "_reap_inventory", inventory)
                monkeypatch.setattr(reaper, "_signal_pids", signal_pids)
                await reaper._reconcile_closed_survivors(defaultdict(int))
                after = await store.get_session_reap(f"{HOST}:anchor-refusal")
                assert deliveries == []
                assert all("ownership_proof_v2" not in item and item["reap_reason"] ==
                           "ownership_proof_missing" for item in after["survivors"])
            assert all(handle.closed for handle in handles.values())
        finally:
            store.stop()
    asyncio.run(go())


def test_unanchored_graceful_close_stays_unknown_when_guessed_cohort_is_gone(monkeypatch):
    root = 441234567
    now = {root: {"pid": root, "ppid": 1, "uid": 501, "pgid": root, "sid": root,
                  "start_id": "Mon Jan 1 00:00:00 2026", "command": "unowned-recording-root"}}
    class Tmux(LadderTmux):
        async def kill_session(self, name):
            await super().kill_session(name)
            now.clear()
            now[441234599] = {"pid": 441234599, "ppid": 1, "uid": 501, "pgid": 441234599,
                              "sid": 441234599, "start_id": "Mon Jan 1 00:00:00 2026",
                              "command": "unrelated-inventory-row"}
    class Witness(RecordingWitness):
        def exited(self): return self.pid not in now
    async def records(): return {pid: dict(rec) for pid, rec in now.items()}
    async def boot(): return "boot-fixture"
    monkeypatch.setattr(prockill, "process_records", records)
    monkeypatch.setattr(prockill, "boot_id", boot)
    monkeypatch.setattr(prockill, "ExitWitness", Witness)
    monkeypatch.setattr(prockill, "pid_alive", lambda _pid: False)
    async def go():
        result, store, _ = await _close(Tmux(pid=str(root), dies_on_graceful=True), "unknown-gone")
        try:
            assert not result["failed"] and result["reap_status"] == "unknown"
            reap = await store.get_session_reap(f"{HOST}:unknown-gone")
            assert reap["reap_status"] == "unknown" and reap["survivors"] == []
        finally:
            store.stop()
    asyncio.run(go())


def test_unanchored_close_preserves_existing_proof_without_upgrading_or_widening(monkeypatch):
    from collections import defaultdict
    root, child = 441234567, 441234568
    current = {pid: {"pid": pid, "ppid": 1 if pid == root else root, "uid": 501,
                     "pgid": root, "sid": root, "start_id": "Mon Jan 1 00:00:01 2026",
                     "command": "foreign-now"} for pid in (root, child)}
    old_record = {**current[root], "start_id": "Mon Jan 1 00:00:00 2026", "command": "old-owned"}
    old_proof = prockill.identity_proof(host=HOST, boot="boot-fixture", record=old_record,
        tmux_session="preserve-proof", tmux_pane="%1", tty="/dev/test", tmux_socket="/tmp/test")
    old_item = {"pid": root, "command": "old-owned", "first_seen": "2026-01-01T00:00:00Z",
                "ownership_proof_v2": old_proof}
    async def records(): return current
    async def boot(): return "boot-fixture"
    monkeypatch.setattr(prockill, "process_records", records)
    monkeypatch.setattr(prockill, "boot_id", boot)
    monkeypatch.setattr(prockill, "ExitWitness", RecordingWitness)
    monkeypatch.setattr(prockill, "pid_alive", lambda pid: int(pid) in current)
    async def go():
        store = Store(":memory:"); store.start()
        tmux = LadderTmux(pid=str(root), dies_on_graceful=True)
        sessions = Sessions(store, tmux=tmux, local_host=HOST)
        try:
            await _seed(store, "preserve-proof")
            await store.upsert_session_reap(f"{HOST}:preserve-proof", reap_status="survivors",
                                           survivors=[old_item], attempts=2)
            result = await sessions.close(HOST, "preserve-proof")
            assert not result["failed"] and result["reap_status"] == "unknown"
            reap = await store.get_session_reap(f"{HOST}:preserve-proof")
            by_pid = {item["pid"]: item for item in reap["survivors"]}
            assert by_pid[root] == old_item
            assert "ownership_proof_v2" not in by_pid[child]
            reaper = SessionReconciler(sessions, None)
            async def inventory(_host): return [], current, "boot-fixture"
            signals = []
            async def signal_pids(_host, pids, sig): signals.extend(pids); return True
            monkeypatch.setattr(reaper, "_reap_inventory", inventory)
            monkeypatch.setattr(reaper, "_signal_pids", signal_pids)
            await reaper._reconcile_closed_survivors(defaultdict(int))
            assert signals == []  # old proof drifts; new child has no authority
        finally:
            store.stop()
    asyncio.run(go())
