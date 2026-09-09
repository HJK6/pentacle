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
    child = os.fork()
    if child == 0:  # pragma: no cover - child process
        os.close(r)
        os.setsid()
        grandchild = os.fork()
        if grandchild == 0:  # pragma: no cover - grandchild
            os.close(w)
            os.execv(sys.executable, [sys.executable, "-c", "import time; time.sleep(300)"])
        os.write(w, str(grandchild).encode())
        os.close(w)
        os._exit(0)
    os.close(w)
    os.waitpid(child, 0)
    pid = os.read(r, 32).decode()
    os.close(r)
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
def test_sigkill_by_pid_recovers_close_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions_mod, "CLOSE_GRACEFUL_CONFIRM_S", 0.2)
    monkeypatch.setattr(sessions_mod, "CLOSE_KILL_CONFIRM_S", 2.0)
    pid = _spawn_orphan()
    assert prockill.pid_alive(pid)

    async def go() -> None:
        tmux = LadderTmux(pid=pid, dies_on_graceful=False)  # tmux never confirms death
        result, store, alerts = await _close(tmux, "s-sigkill")
        try:
            assert result["failed"] is False
            assert result["session"]["status"] == "closed"
            assert result["survivors"] == []
            # The escalation actually ran: the child process is gone.
            assert not prockill.pid_alive(pid)
            assert alerts.emitted == []          # a clean kill raises no alert
        finally:
            store.stop()

    try:
        asyncio.run(go())
    finally:
        try:
            os.kill(int(pid), 9)
        except (ProcessLookupError, ValueError):
            pass


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
                assert survivor["reap_reason"] == "process_observed_alive"
                assert real_pid_alive(str(pid))

                reap = await store.get_session_reap(f"{HOST}:s-live-survivor")
                assert reap is not None
                assert reap["reap_status"] == "unknown"
                assert reap["survivors"][0]["reap_reason"] == "process_observed_alive"
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

    async def go() -> None:
        tmux = LadderTmux(pid="999999", dies_on_graceful=False)
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
            assert "no deliverable signal" in result["reason"]
            # The row is LEFT OPEN — never closed over a maybe-live process.
            row = await store.fetch_session(HOST, "s-failed")
            assert row["status"] == "open"
            assert [k for k, _ in alerts.emitted] == ["close_failed"]
        finally:
            store.stop()

    asyncio.run(go())


def test_close_persists_descendant_survivor_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    async def records() -> dict[int, dict[str, object]]:
        return {
            100: {
                "pid": 100, "ppid": 1, "uid": 501, "pgid": 100, "sid": 100,
                "start_id": "Mon Jan 1 00:00:00 2026", "command": "target-shell",
            },
            101: {
                "pid": 101, "ppid": 100, "uid": 501, "pgid": 100, "sid": 100,
                "start_id": "Mon Jan 1 00:00:01 2026", "command": "target-child",
            },
        }

    async def boot() -> str:
        return "boot-1"

    monkeypatch.setattr(sessions_mod.prockill, "process_records", records)
    monkeypatch.setattr(sessions_mod.prockill, "boot_id", boot)
    monkeypatch.setattr(sessions_mod.prockill, "pid_alive", lambda pid: str(pid) == "101")

    async def go() -> None:
        tmux = LadderTmux(pid="100", dies_on_graceful=True)
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
            "boot-1",
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
    observed = Sessions._reap_readback(
        [101], {}, {}, inventory_complete=False
    )
    assert observed["survivors"][0]["reap_reason"] == "process_observed_alive"
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
            result = await sessions.close(HOST, "parent")
            assert result["failed"] is False
            assert result["live_children"] == [{
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
            result = await sessions.close(HOST, "parent")
            assert result["live_children"] == [{
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
            result = await sessions.close(HOST, "parent")
            child = await store.fetch_session(HOST, "child")
            assert result["live_children"]
            assert child is not None and child["status"] == "open"
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
