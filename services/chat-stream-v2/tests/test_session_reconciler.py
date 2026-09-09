"""Focused v2 reconciler lifecycle regression tests."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

from presence import PresenceConfig, RemotePresence  # noqa: E402
import prockill  # noqa: E402
from comms import Comms  # noqa: E402
from ledger import Ledger  # noqa: E402
from reconciler import ReconcileConfig, SessionReconciler  # noqa: E402
from server import Server  # noqa: E402
from sessions import Sessions, VerbError  # noqa: E402
from store import Store  # noqa: E402


class _FakeTmux:
    def __init__(self, result: tuple[int, str]) -> None:
        self.result = result
        self.calls = 0

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        self.calls += 1
        rc, output = self.result
        if '-F' in args and '|' in args[args.index('-F') + 1]:
            output = output.replace('\t', '|')
        return rc, output

    async def session_state(self, _name: str) -> str:
        rc, output = self.result
        if rc == 0:
            return "alive"
        if rc == 1 and "no server" in output:
            return "gone"
        return "unreachable"


class _FakeHosts:
    local_host = "hosta"

    def __init__(self, tmux: _FakeTmux, *, online: bool) -> None:
        self.peers = {"hostb": object()}
        self._tmux = tmux
        self.online = online

    def is_online(self, host: str) -> bool:
        return host == self.local_host or self.online

    def tmux_for(self, host: str) -> _FakeTmux:
        return self._tmux


class _WireTmux:
    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        return 0, ""

    async def session_state(self, _name: str) -> str:
        return "alive"

    async def pane_identity(self, _name: str) -> dict[str, str]:
        return {"pane_pid": "123", "pane_id": "%1", "tty": "/dev/ttys001", "tmux_socket": "default"}

    async def capture(self, _name: str) -> str:
        return ""

    async def paste(self, _name: str, _text: str) -> None:
        return None


class _WireHosts(_FakeHosts):
    def __init__(self, tmux: _WireTmux) -> None:
        super().__init__(tmux, online=True)  # type: ignore[arg-type]

    def known(self, host: str) -> bool:
        return host in {self.local_host, "hostb"}

    def is_local(self, host: str) -> bool:
        return host == self.local_host

    async def ensure_reachable(self, _host: str, _verb: str) -> None:
        return None


class _WireSpawnCtl:
    def __init__(self, tmux: _WireTmux) -> None:
        self.tmux = tmux

    async def _await_marker(self, *args: object, **kwargs: object) -> bool:
        return True


class _LocalInventoryTmux:
    def __init__(self) -> None:
        self.pane_calls = 0

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        if args[:1] == ("list-panes",):
            self.pane_calls += 1
            return 0, "live-local\t99\n"
        return 0, ""


async def _open_remote(tmux: _FakeTmux, *, online: bool = True):
    store = Store(":memory:")
    store.start()
    sessions = Sessions(store, tmux=None, local_host="hosta")
    await store.open_session(
        "hostb", "v2-ghost", pane_pid="123", pane_status="pane_alive"
    )
    await sessions.refresh()
    hosts = _FakeHosts(tmux, online=online)
    presence = RemotePresence(
        sessions,
        hosts,
        config=PresenceConfig(list_timeout_s=0.1),
    )
    reconciler = SessionReconciler(
        sessions,
        hosts,
        presence=presence,
        config=ReconcileConfig(interval_s=60, threshold_checks=2),
    )
    return store, sessions, hosts, reconciler


async def _insert_live_reservation(
    store: Store, *, ttl_s: float, request_id: str,
) -> None:
    """Install the canonical reservation while a close probe is suspended.

    The public reservation admission rejects an already-open name. This is the
    intentional close-time race: use its durable row shape to prove the close
    transaction reads reservations at the decision, rather than from a
    pre-probe snapshot.
    """
    def _op(conn) -> None:
        conn.execute(
            "INSERT INTO v2_stream_reservations "
            "(host, session_name, request_id, expires_at) VALUES (?,?,?,?)",
            ("hostb", "v2-ghost", request_id, time.time() + ttl_s),
        )
        conn.commit()

    await store.submit(_op)


def test_reboot_shaped_absent_tmux_closes_row_after_consecutive_evidence() -> None:
    async def run() -> None:
        tmux = _FakeTmux((1, "no server running on socket"))
        store, sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            first = await reconciler.reconcile_once()
            assert first["closed"] == 0
            assert (await store.fetch_session("hostb", "v2-ghost"))["status"] == "open"

            second = await reconciler.reconcile_once()
            assert second["closed"] == 1
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None
            assert row["status"] == "closed"
            assert row["pane_status"] == "pane_dead"
            assert row["presumed_dead_at"]
            assert sessions.list_open() == []
            reap = await store.get_session_reap("hostb:v2-ghost")
            assert reap is not None
            assert reap["reap_status"] == "unknown"
            assert reap["survivors"] == []
            assert tmux.calls == 3
        finally:
            store.stop()

    asyncio.run(run())


def test_reconciler_tick_runs_pin_drift_hook_on_the_existing_cadence(monkeypatch) -> None:
    calls: list[str] = []

    class _OnePass:
        cfg = ReconcileConfig(interval_s=60)

        async def reconcile_once(self) -> dict:
            calls.append("reconcile")
            return {}

        async def on_reconcile_tick(self) -> None:
            calls.append("pin_drift")

    async def _stop_after_normal_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    worker = _OnePass()
    monkeypatch.setattr(asyncio, "sleep", _stop_after_normal_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(SessionReconciler.run_forever(worker))
    assert calls == ["reconcile", "pin_drift"]


def test_remote_reprobe_alive_cancels_stale_death_episode() -> None:
    class LiveOnReprobeTmux(_FakeTmux):
        async def session_state(self, _name: str) -> str:
            return "alive"

    async def run() -> None:
        tmux = LiveOnReprobeTmux((1, "no server running on socket"))
        store, sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            first = await reconciler.reconcile_once()
            second = await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert first["closed"] == 0
            assert second["closed"] == 0
            assert second["remote_reprobe_alive"] == 1
            assert row is not None and row["status"] == "open"
            assert row["presumed_dead_at"] is None
            server = Server(
                store=store,
                sessions=sessions,
                ledger=Ledger(store, sessions=sessions),
                local_host="hosta",
            )
            report = (await server._dispatch(json.dumps({
                "type": "report", "request_id": "restart-report",
                "report_id": "restart-report", "from_stream_id": "hostb:v2-ghost",
                "msg_id": 0, "status": "done", "summary": "still alive",
                "findings": [], "next_action": "leader_proceed",
            })))[0]
            assert report["type"] == "report.ok"
        finally:
            store.stop()

    asyncio.run(run())


def test_live_reservation_acquired_during_exact_reprobe_fences_close() -> None:
    class BlockingGoneTmux(_FakeTmux):
        def __init__(self) -> None:
            super().__init__((1, "no server running on socket"))
            self.reprobe_started = asyncio.Event()
            self.resume_reprobe = asyncio.Event()

        async def session_state(self, _name: str) -> str:
            self.reprobe_started.set()
            await self.resume_reprobe.wait()
            return "gone"

    async def run() -> None:
        tmux = BlockingGoneTmux()
        store, _sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            first = await reconciler.reconcile_once()
            assert first["closed"] == 0

            closing = asyncio.create_task(reconciler.reconcile_once())
            await asyncio.wait_for(tmux.reprobe_started.wait(), timeout=1)
            await _insert_live_reservation(
                store, ttl_s=60, request_id="race-reservation",
            )
            tmux.resume_reprobe.set()
            second = await closing

            row = await store.fetch_session("hostb", "v2-ghost")
            assert second["remote_reprobe_gone"] == 1
            assert second["closed"] == 0
            assert row is not None and row["status"] == "open"
        finally:
            store.stop()

    asyncio.run(run())


def test_released_or_expired_reservation_allows_fresh_gone_close() -> None:
    async def closes_after(reservation_ttl_s: float, request_id: str) -> None:
        tmux = _FakeTmux((1, "no server running on socket"))
        store, _sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            await _insert_live_reservation(
                store, ttl_s=reservation_ttl_s, request_id=request_id,
            )
            if reservation_ttl_s > 0:
                await store.release_stream_id_fenced("hostb", "v2-ghost", request_id)
            first = await reconciler.reconcile_once()
            second = await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert first["closed"] == 0
            assert second["remote_reprobe_gone"] == 1
            assert second["closed"] == 1
            assert row is not None and row["status"] == "closed"
        finally:
            store.stop()

    async def run() -> None:
        await closes_after(60, "released-reservation")
        await closes_after(-1, "expired-reservation")

    asyncio.run(run())


def test_reconciled_dead_cas_fences_a_changed_generation() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            old = await store.open_session("hostb", "v2-ghost")
            await store.open_session(
                "hostb", "v2-ghost", session_generation="replacement-generation",
            )
            marked = await store.mark_reconciled_dead(
                "hostb", "v2-ghost",
                expected_generation=str(old["session_generation"]),
                presumed_dead_at="2026-09-06T06:00:00Z",
                closed_at="2026-09-06T06:00:01Z",
            )
            row = await store.fetch_session("hostb", "v2-ghost")
            assert marked is None
            assert row is not None and row["status"] == "open"
            assert row["session_generation"] == "replacement-generation"
        finally:
            store.stop()

    asyncio.run(run())


def test_ssh_unreachable_never_reaps_or_marks_presumed_dead() -> None:
    async def run() -> None:
        tmux = _FakeTmux((0, "v2-ghost\t123\n"))
        store, _sessions, _hosts, reconciler = await _open_remote(tmux, online=False)
        try:
            await reconciler.reconcile_once()
            await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None
            assert row["status"] == "open"
            assert row["presumed_dead_at"] is None
            assert tmux.calls == 0
        finally:
            store.stop()


def test_unclassified_rc_one_never_reaps() -> None:
    async def run() -> None:
        tmux = _FakeTmux((1, "permission denied opening tmux socket"))
        store, _sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            await reconciler.reconcile_once()
            await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None and row["status"] == "open"
            assert row["presumed_dead_at"] is None
        finally:
            store.stop()

    asyncio.run(run())


def test_rotating_presence_batch_never_closes_unselected_custom_rows() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            names = [f"custom-{index:03d}" for index in range(500)]
            for name in names:
                await store.open_session("hostb", name, pane_status="pane_alive")
            await sessions.refresh()
            listing = "".join(f"{name}\t{1000 + index}\n" for index, name in enumerate(names))
            tmux = _FakeTmux((0, listing))
            hosts = _FakeHosts(tmux, online=True)
            presence = RemotePresence(
                sessions, hosts, config=PresenceConfig(max_rows_per_pass=200),
            )
            reconciler = SessionReconciler(
                sessions, hosts, presence=presence,
                config=ReconcileConfig(max_rows_per_pass=200, threshold_checks=2),
            )
            for _ in range(3):
                await reconciler.reconcile_once()
            rows = await store.list_sessions("open")
            assert len(rows) == 500
        finally:
            store.stop()

    asyncio.run(run())


def test_rotating_presence_batch_accumulates_death_evidence_across_passes() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            names = [f"custom-dead-{index:03d}" for index in range(500)]
            for name in names:
                await store.open_session("hostb", name, pane_status="pane_alive")
            await sessions.refresh()
            tmux = _FakeTmux((1, "no server running on socket"))
            hosts = _FakeHosts(tmux, online=True)
            presence = RemotePresence(
                sessions, hosts, config=PresenceConfig(max_rows_per_pass=200),
            )
            reconciler = SessionReconciler(
                sessions, hosts, presence=presence,
                config=ReconcileConfig(max_rows_per_pass=200, threshold_checks=2),
            )
            for _ in range(12):
                await reconciler.reconcile_once()
            assert await store.list_sessions("open") == []
        finally:
            store.stop()

    asyncio.run(run())


def test_death_episode_cannot_cross_a_respawned_generation() -> None:
    async def run() -> None:
        tmux = _FakeTmux((1, "no server running on socket"))
        store, sessions, hosts, reconciler = await _open_remote(tmux)
        try:
            old = await store.fetch_session("hostb", "v2-ghost")
            assert old is not None
            await reconciler.reconcile_once()
            await store.open_session(
                "hostb", "v2-ghost", created_at="2099-01-01T00:00:00Z",
                pane_pid="999", pane_status="pane_alive",
            )
            await sessions.refresh()
            new = await store.fetch_session("hostb", "v2-ghost")
            assert new is not None
            assert old["session_generation"] != new["session_generation"]
            await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None and row["status"] == "open"
            assert row["created_at"] == "2099-01-01T00:00:00Z"
            assert tmux.calls == 2
        finally:
            store.stop()

    asyncio.run(run())


def test_stale_local_pane_dead_is_not_fresh_reap_evidence() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "stale-local", pane_status="pane_dead")
            await sessions.refresh()
            reconciler = SessionReconciler(
                sessions, _FakeHosts(_FakeTmux((0, "")), online=True),
                config=ReconcileConfig(threshold_checks=2),
            )
            await reconciler.reconcile_once()
            await reconciler.reconcile_once()
            row = await store.fetch_session("hosta", "stale-local")
            assert row is not None and row["status"] == "open"
            assert row["presumed_dead_at"] is None
        finally:
            store.stop()

    asyncio.run(run())


def test_fresh_local_inventory_reaps_dead_row_even_with_remote_presence_enabled() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            local_tmux = _LocalInventoryTmux()
            sessions = Sessions(store, tmux=local_tmux, local_host="hosta")
            await store.open_session("hosta", "stale-local", pane_status="pane_dead")
            await store.open_session("hosta", "live-local", pane_status="pane_alive")
            await sessions.refresh()
            remote_tmux = _FakeTmux((0, ""))
            hosts = _FakeHosts(remote_tmux, online=True)
            presence = RemotePresence(
                sessions, hosts, config=PresenceConfig(list_timeout_s=0.1),
            )
            reconciler = SessionReconciler(
                sessions, hosts, presence=presence,
                config=ReconcileConfig(interval_s=60, threshold_checks=2),
            )

            first = await reconciler.reconcile_once()
            second = await reconciler.reconcile_once()

            assert first["closed"] == 0
            assert second["closed"] == 1
            dead = await store.fetch_session("hosta", "stale-local")
            live = await store.fetch_session("hosta", "live-local")
            assert dead is not None and dead["status"] == "closed"
            assert live is not None and live["status"] == "open"
            assert local_tmux.pane_calls == 2
        finally:
            store.stop()

    asyncio.run(run())


def test_local_observation_precedes_presence_capture() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=_LocalInventoryTmux(), local_host="hosta")
            await store.open_session("hosta", "live-local", pane_status="pane_alive")
            await sessions.refresh()
            hosts = _FakeHosts(_FakeTmux((0, "")), online=True)
            presence = RemotePresence(sessions, hosts)
            reconciler = SessionReconciler(sessions, hosts, presence=presence)
            trace: list[str] = []
            observe_local = reconciler._observe_local

            async def tracked_local(rows: list[dict]) -> dict[str, tuple[str, str]]:
                trace.append("local")
                return await observe_local(rows)

            async def tracked_capture() -> int:
                trace.append("capture")
                return 0

            reconciler._observe_local = tracked_local  # type: ignore[method-assign]
            presence.capture_previews = tracked_capture  # type: ignore[method-assign]
            await reconciler.reconcile_once()
            assert trace.index("local") < trace.index("capture")
        finally:
            store.stop()

    asyncio.run(run())


def test_remote_dead_pane_closes_after_two_passes() -> None:
    class ZombieTmux(_FakeTmux):
        async def session_state(self, _name: str) -> str: return "gone"

    async def run() -> None:
        tmux = ZombieTmux((0, "v2-other\t99\n"))
        store, sessions, hosts, original = await _open_remote(tmux)
        alerts: list[tuple[str, dict]] = []
        alert_sink = type("Alerts", (), {"emit": lambda _self, kind, **fields: alerts.append((kind, fields))})()
        reconciler = SessionReconciler(
            sessions, hosts, presence=original.presence, alerts=alert_sink,
            config=ReconcileConfig(threshold_checks=2),
        )
        try:
            first = await reconciler.reconcile_once()
            second = await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert first["closed"] == 0
            assert second["closed"] == 1
            assert row is not None and row["status"] == "closed"
            assert row["presumed_dead_at"]
            kinds = [kind for kind, _fields in alerts]
            assert kinds.count("reconciler_session_dead") == 1
            assert "reconciler_child_dead" not in kinds
        finally:
            store.stop()

    asyncio.run(run())


def test_terminal_report_authorization_is_generation_scoped() -> None:
    async def run() -> None:
        tmux = _FakeTmux((1, "no server running on socket"))
        store, sessions, hosts, original = await _open_remote(tmux)
        outbound = AsyncMock()
        reconciler = SessionReconciler(
            sessions, hosts, presence=original.presence, outbound=outbound,
            config=ReconcileConfig(threshold_checks=2),
        )
        try:
            await store.update_session(
                "hostb", "v2-ghost", parent_stream_id="hosta:parent",
                self_close_on_completion=True,
            )
            sessions.apply_durable(
                "hostb:v2-ghost", parent_stream_id="hosta:parent",
                self_close_on_completion=True,
            )
            await Ledger(store, sessions=sessions, outbound=AsyncMock()).ingest({
                "report_id": "terminal-child", "from_stream_id": "hostb:v2-ghost",
                "msg_id": 1, "status": "done", "summary": "complete",
                "findings": [], "next_action": "leader_proceed",
            })
            await reconciler.reconcile_once()
            await reconciler.reconcile_once()
            outbound.enqueue.assert_not_awaited()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None and row["status"] == "closed"
            await store.open_session(
                "hostb", "v2-ghost", session_generation="generation-b",
                parent_stream_id="hosta:parent", self_close_on_completion=True,
            )
            await sessions.refresh()
            await reconciler.reconcile_once()
            await reconciler.reconcile_once()
            outbound.enqueue.assert_awaited_once()
        finally:
            store.stop()

    asyncio.run(run())


class _ClosedSurvivorHosts(_FakeHosts):
    def __init__(self, tmux: _FakeTmux, process_text: str) -> None:
        super().__init__(tmux, online=True)
        self.process_text = process_text
        self.boot = "boot-1"
        self.signals: list[tuple[str, list[int], int]] = []

    async def run_command(self, host: str, *args: str, **kwargs: object) -> tuple[int, str]:
        if args[:2] == ("ps", "-eo"):
            return 0, self.process_text
        if args == ("sysctl", "-n", "kern.boottime") or args == (
            "cat", "/proc/sys/kernel/random/boot_id"
        ):
            return 0, self.boot
        return 0, ""

    def signal_pids(self, host: str, pids: list[int], sig: int) -> bool:
        self.signals.append((host, pids, int(sig)))
        # A complete post-signal inventory still contains unrelated processes;
        # an empty ps result is intentionally modeled as unavailable below.
        self.process_text = "999 1 501 999 999 Mon Jan 1 00:00:00 2026 unrelated\n"
        self._tmux.result = (0, "")
        return True


def _proof(
    *, pid: int, session: str, pane: str, tty: str, socket: str,
    command: str, start_id: str,
) -> dict:
    record = {
        "pid": pid, "ppid": 1, "uid": 501, "pgid": pid, "sid": pid,
        "start_id": start_id, "command": command,
    }
    return prockill.identity_proof(
        host="hosta", boot="boot-1", record=record,
        tmux_session=session, tmux_pane=pane, tty=tty, tmux_socket=socket,
    )


def test_closed_survivor_reap_excludes_foreign_tmux_tree() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "closed-survivor", pane_status="pane_dead")
            await store.update_session(
                "hosta", "closed-survivor", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hosta:closed-survivor", reap_status="survivors",
                survivors=[{
                    "pid": 100, "command": "target-shell", "first_seen": "2026-08-06T00:00:00Z",
                    "ownership_proof_v2": _proof(
                        pid=100, session="closed-survivor", pane="%1",
                        tty="/dev/ttys001", socket="/tmp/tmux-501/default",
                        command="target-shell", start_id="Mon Jan 1 00:00:00 2026",
                    ),
                }],
            )
            tmux = _FakeTmux((
                0,
                "closed-survivor\t100\t%1\t/dev/ttys001\t/tmp/tmux-501/default\n"
                "foreign-gate\t200\t%2\t/dev/ttys002\t/tmp/tmux-501/default\n",
            ))
            processes = (
                "100 1 501 100 100 Mon Jan 1 00:00:00 2026 target-shell\n"
                "101 100 501 100 100 Mon Jan 1 00:00:01 2026 target-child\n"
                "200 1 501 200 200 Mon Jan 1 00:00:00 2026 foreign-shell\n"
                "201 200 501 200 200 Mon Jan 1 00:00:01 2026 foreign-child\n"
            )
            hosts = _ClosedSurvivorHosts(tmux, processes)
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_reaped"] == 1
            assert hosts.signals == [("hosta", [100, 101], 15)]
            reap = await store.get_session_reap("hosta:closed-survivor")
            assert reap is not None and reap["reap_status"] == "reaped"
            assert reap["attempts"] == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_closed_survivor_inventory_failure_consumes_no_attempt() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "inventory-fails")
            await store.update_session(
                "hosta", "inventory-fails", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hosta:inventory-fails", reap_status="survivors",
                survivors=[{"pid": 100, "command": "target-shell"}],
            )
            hosts = _ClosedSurvivorHosts(_FakeTmux((255, "ssh unreachable")), "")
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_inventory_unavailable"] == 1
            reap = await store.get_session_reap("hosta:inventory-fails")
            assert reap is not None and reap["attempts"] == 0
            assert hosts.signals == []
        finally:
            store.stop()

    asyncio.run(run())


def test_empty_successful_process_inventory_stays_fail_closed() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "empty-process-inventory")
            await store.update_session(
                "hosta", "empty-process-inventory", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hosta:empty-process-inventory", reap_status="survivors",
                survivors=[{
                    "pid": 100, "command": "target-shell", "first_seen": "2026-08-06T00:00:00Z",
                    "ownership_proof_v2": _proof(
                        pid=100, session="empty-process-inventory", pane="%1",
                        tty="/dev/ttys001", socket="/tmp/tmux-501/default",
                        command="target-shell", start_id="Mon Jan 1 00:00:00 2026",
                    ),
                }],
            )
            hosts = _ClosedSurvivorHosts(_FakeTmux((0, "")), "")
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_inventory_unavailable"] == 1
            assert counters["session_reap_reaped"] == 0
            reap = await store.get_session_reap("hosta:empty-process-inventory")
            assert reap is not None
            assert reap["reap_status"] == "survivors"
            assert reap["attempts"] == 0
            assert hosts.signals == []
        finally:
            store.stop()

    asyncio.run(run())


def test_session_reap_readback_is_an_array_and_honors_due_time() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("hosta", "due-gate")
            await store.update_session(
                "hosta", "due-gate", status="closed", closed_at="2026-08-06T00:00:00Z"
            )
            await store.upsert_session_reap(
                "hosta:due-gate", reap_status="survivors",
                survivors={"pid": 123},  # type: ignore[arg-type]
                next_attempt_at="2099-01-01T00:00:00Z",
            )
            row = await store.get_session_reap("hosta:due-gate")
            assert row is not None and row["survivors"] == []
            assert await store.list_session_reap_unreaped(
                now="2026-08-06T12:00:00Z"
            ) == []
        finally:
            store.stop()

    asyncio.run(run())


def test_session_reap_survivor_array_has_bounded_overflow_marker() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session("hosta", "bounded-reap")
            await store.update_session(
                "hosta", "bounded-reap", status="closed", closed_at="2026-08-06T00:00:00Z"
            )
            await store.upsert_session_reap(
                "hosta:bounded-reap", reap_status="survivors",
                survivors=[{"pid": index, "command": "sleep"} for index in range(1, 400)],
            )
            row = await store.get_session_reap("hosta:bounded-reap")
            assert row is not None
            assert len(row["survivors"]) == 256
            assert row["survivors"][-1]["reap_reason"] == "survivor_limit_exceeded"
        finally:
            store.stop()

    asyncio.run(run())


def test_reconciler_applies_live_overlay_to_remote_rows() -> None:
    async def run() -> None:
        tmux = _FakeTmux((0, "v2-ghost\t123\n"))
        store, sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            await reconciler.reconcile_once()
            row = sessions.get("hostb:v2-ghost")
            assert row is not None
            assert row["online"] is True
            assert row["pane_status"] == "pane_alive"
            assert row["pane_pid"] == "123"
        finally:
            store.stop()

    asyncio.run(run())


def test_closed_survivor_pid_reuse_is_deferred_without_signal() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "closed-survivor", pane_status="pane_dead")
            await store.update_session(
                "hosta", "closed-survivor", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hosta:closed-survivor", reap_status="survivors",
                survivors=[{
                    "pid": 100, "command": "target-shell", "first_seen": "2026-08-06T00:00:00Z",
                    "ownership_proof_v2": _proof(
                        pid=100, session="closed-survivor", pane="%1",
                        tty="/dev/ttys001", socket="/tmp/tmux-501/default",
                        command="target-shell", start_id="Mon Jan 1 00:00:00 2026",
                    ),
                }],
            )
            tmux = _FakeTmux((
                0,
                "closed-survivor\t300\t%9\t/dev/ttys009\t/tmp/tmux-501/default\n",
            ))
            processes = (
                "100 1 501 100 100 Mon Jan 1 00:00:00 2026 target-shell\n"
                "300 1 501 300 300 Mon Jan 1 00:10:00 2026 target-shell\n"
            )
            hosts = _ClosedSurvivorHosts(tmux, processes)
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_survivors"] == 1
            assert hosts.signals == []
            reap = await store.get_session_reap("hosta:closed-survivor")
            assert reap is not None and reap["attempts"] == 0
            assert reap["reap_status"] == "survivors"
            assert reap["survivors"][0]["reap_reason"] == "identity_drift"
        finally:
            store.stop()

    asyncio.run(run())


def test_closed_survivor_foreign_tmux_session_binding_is_deferred() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "target-session", pane_status="pane_dead")
            await store.update_session(
                "hosta", "target-session", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hosta:target-session", reap_status="survivors",
                survivors=[{
                    "pid": 100, "command": "target-shell", "first_seen": "2026-08-06T00:00:00Z",
                    "ownership_proof_v2": _proof(
                        pid=100, session="foreign-session", pane="%1",
                        tty="/dev/ttys001", socket="/tmp/tmux-501/default",
                        command="target-shell", start_id="Mon Jan 1 00:00:00 2026",
                    ),
                }],
            )
            hosts = _ClosedSurvivorHosts(
                _FakeTmux((0, "")),
                "100 1 501 100 100 Mon Jan 1 00:00:00 2026 target-shell\n",
            )
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_survivors"] == 1
            assert hosts.signals == []
            reap = await store.get_session_reap("hosta:target-session")
            assert reap is not None
            assert reap["survivors"][0]["reap_reason"] == "identity_drift"
        finally:
            store.stop()

    asyncio.run(run())


def test_unknown_remote_empty_reap_is_not_promoted_to_reaped() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hostb", "remote-closed")
            await store.update_session(
                "hostb", "remote-closed", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hostb:remote-closed", reap_status="unknown", survivors=[]
            )
            hosts = _ClosedSurvivorHosts(
                _FakeTmux((0, "")),
                "100 1 501 100 100 Mon Jan 1 00:00:00 2026 unrelated\n",
            )
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_reaped"] == 0
            assert hosts.signals == []
            reap = await store.get_session_reap("hostb:remote-closed")
            assert reap is not None and reap["reap_status"] == "unknown"
        finally:
            store.stop()

    asyncio.run(run())


def test_exhausted_unknown_empty_reap_stays_unknown() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "exhausted-unknown")
            await store.update_session(
                "hosta", "exhausted-unknown", status="closed",
                closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            await store.upsert_session_reap(
                "hosta:exhausted-unknown", reap_status="unknown", survivors=[],
                attempts=5, exhausted_at="2026-08-06T00:00:00Z",
            )
            hosts = _ClosedSurvivorHosts(
                _FakeTmux((0, "")),
                "100 1 501 100 100 Mon Jan 1 00:00:00 2026 unrelated\n",
            )
            reconciler = SessionReconciler(sessions, hosts)
            counters = await reconciler.reconcile_once()
            assert counters["session_reap_reaped"] == 0
            assert counters["session_reap_exhausted"] == 1
            reap = await store.get_session_reap("hosta:exhausted-unknown")
            assert reap is not None and reap["reap_status"] == "unknown"
        finally:
            store.stop()

    asyncio.run(run())


def test_status_queries_unmanaged_agent_host_without_registry_rows() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            tmux = _FakeTmux((0, "v2-unmanaged\t300\n"))
            hosts = _FakeHosts(tmux, online=True)
            presence = RemotePresence(sessions, hosts)
            reconciler = SessionReconciler(sessions, hosts, presence=presence)
            await reconciler.reconcile_once()
            status = await reconciler.status(host="hostb")
            assert status["counts"]["unmanaged_tree"] == 1
            assert status["details"][0]["session_name"] == "v2-unmanaged"
        finally:
            store.stop()

    asyncio.run(run())


def test_malformed_live_row_mixed_with_valid_row_never_advances_death_threshold() -> None:
    async def run():
        tmux = _FakeTmux((0, 'v2-other|300\nv2-ghost_123\n'))
        store, _sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            for _ in range(4):  # Beyond the configured two-observation threshold.
                await reconciler.reconcile_once()
                assert reconciler._last_observations['hostb'].state == 'ambiguous'
                assert (await reconciler.status(host='hostb'))['counts']['row_open_session_dead'] == 0
            row = await store.fetch_session('hostb', 'v2-ghost')
            assert row['status'] == 'open' and row['presumed_dead_at'] is None
            assert row['pane_status'] == 'pane_alive'
            assert await store.get_session_reap('hostb:v2-ghost') is None
        finally:
            store.stop()
    asyncio.run(run())


def test_ambiguous_empty_inventory_never_reaps() -> None:
    async def run() -> None:
        tmux = _FakeTmux((0, "usage-check-1\t9\n"))
        store, _sessions, _hosts, reconciler = await _open_remote(tmux)
        try:
            await reconciler.reconcile_once()
            await reconciler.reconcile_once()
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None
            assert row["status"] == "open"
            assert row["presumed_dead_at"] is None
            assert (await reconciler.status(host="hostb"))["counts"] == {
                "row_open_session_dead": 0,
                "row_closed_tree_alive": 0,
                "row_open_host_unreachable": 0,
                "unmanaged_tree": 0,
            }
        finally:
            store.stop()

    asyncio.run(run())


def test_reconcile_status_reports_durable_closed_survivor() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=None, local_host="hosta")
            await store.open_session("hosta", "closed-survivor", visibility="hidden")
            await store.update_session(
                "hosta", "closed-survivor", status="closed", closed_at="2026-08-06T00:00:00Z"
            )
            await store.upsert_session_reap(
                "hosta:closed-survivor",
                reap_status="survivors",
                survivors=[{"pid": 123, "command": "sleep"}],
                attempts=2,
                updated_at="2026-08-06T00:00:01Z",
            )
            reconciler = SessionReconciler(sessions, _FakeHosts(_FakeTmux((0, "")), online=True))
            status = await reconciler.status(host="hosta")
            assert status["counts"]["row_closed_tree_alive"] == 1
            assert status["details"][0]["reap_status"] == "survivors"
        finally:
            store.stop()

    asyncio.run(run())


def test_reconcile_status_is_a_read_only_supported_verb() -> None:
    class FakeReconciler:
        async def status(self, *, host: str | None = None) -> dict:
            return {
                "host": host,
                "counts": {
                    "row_open_session_dead": 0,
                    "row_closed_tree_alive": 0,
                    "row_open_host_unreachable": 0,
                    "unmanaged_tree": 0,
                },
                "details": [],
            }

    async def run() -> None:
        server = Server(reconciler=FakeReconciler())
        reply = await server._dispatch(
            '{"type":"reconcile.status","request_id":"status-1","host":"hostb"}'
        )
        assert reply[0]["type"] == "reconcile.status.ok"
        assert reply[0]["request_id"] == "status-1"
        assert reply[0]["host"] == "hostb"

    asyncio.run(run())


def test_reconciled_remote_row_can_restore_and_still_supports_tell_report() -> None:
    async def run() -> None:
        tmux = _FakeTmux((0, "v2-ghost\t123\n"))
        store, sessions, hosts, _reconciler = await _open_remote(tmux)
        sessions.hosts = hosts
        try:
            old = await store.fetch_session("hostb", "v2-ghost")
            assert old is not None
            await store.update_session("hostb", "v2-ghost", title="reconciled title")
            await sessions.mark_reconciled_dead(
                "hostb", "v2-ghost",
                expected_generation=str(old["session_generation"]),
                presumed_dead_at="2026-08-09T05:19:00Z",
                closed_at="2026-08-09T05:19:01Z",
            )
            restored = await sessions.restore_reconciled(
                "hostb", "v2-ghost",
            )
            assert restored["restored"] is True
            assert restored["liveness"] == "alive"
            row = await store.fetch_session("hostb", "v2-ghost")
            assert row is not None and row["status"] == "open"
            assert row["pane_status"] == "pane_alive"
            assert row["presumed_dead_at"] is None
            assert row["title"] == "reconciled title"

            wire_tmux = _WireTmux()
            wire_hosts = _WireHosts(wire_tmux)
            sessions.hosts = wire_hosts
            wire_comms = Comms(store, sessions, _WireSpawnCtl(wire_tmux), hosts=wire_hosts)
            wire_server = Server(
                store=store,
                sessions=sessions,
                comms=wire_comms,
                ledger=Ledger(store, sessions=sessions, comms=wire_comms),
                local_host="hosta",
            )
            tell = (await wire_server._dispatch(json.dumps({
                "type": "tell", "request_id": "restore-tell",
                "tell_id": "restore-tell", "stream_id": "hostb:v2-ghost",
                "message": "reconnect after restore",
            })))[0]
            report = (await wire_server._dispatch(json.dumps({
                "type": "report", "request_id": "restore-report",
                "report_id": "restore-report", "from_stream_id": "hostb:v2-ghost",
                "msg_id": 0, "status": "done", "summary": "restored comms",
                "findings": [], "next_action": "leader_proceed",
            })))[0]
            assert tell["type"] == "tell.ok"
            assert report["type"] == "report.ok"

            await sessions.mark_closed("hostb", "v2-ghost", "operator")
            try:
                await sessions.restore_reconciled("hostb", "v2-ghost")
            except VerbError as exc:
                assert exc.code == "restore_not_reconciler_closed"
            else:  # pragma: no cover - restore must not revive operator closes
                raise AssertionError("operator close was incorrectly restorable")
        finally:
            store.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Terminal-report rejection cases: flagged Codex seats that finish without a
# valid report.
# ---------------------------------------------------------------------------

async def _open_self_close_seat(
    store: Store, name: str, *, self_close: bool = True, generation: str | None = None,
):
    """A hidden, pane-alive seat with a parent — the rejection-case shape. Optional
    self_close flag and generation so the negatives can vary one axis."""
    kwargs: dict = {
        "parent_stream_id": "hosta:leader", "visibility": "hidden",
        "self_close_on_completion": self_close, "pane_status": "pane_alive",
    }
    if generation is not None:
        kwargs["session_generation"] = generation
    await store.open_session("hosta", name, **kwargs)


def _sweep_reconciler(store: Store) -> tuple[SessionReconciler, "Sessions"]:
    sessions = Sessions(store, tmux=None, local_host="hosta")
    hosts = _FakeHosts(_FakeTmux((0, "")), online=True)
    return SessionReconciler(sessions, hosts), sessions


_INVALID_TERMINAL_REPORT = {
    # `--result` that never parsed to a valid ReportPayloadV1: no summary. This is
    # A `--status done` envelope with an unusable body.
    "report_id": "rejection-bad", "msg_id": 1, "status": "done",
}


def test_rejection_ledger_records_terminal_rejection_for_self_close_seat() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "rejection-seat")
            await sessions.refresh()
            ledger = Ledger(store, sessions=sessions, outbound=AsyncMock())
            with pytest.raises(Exception):
                await ledger.ingest({**_INVALID_TERMINAL_REPORT, "from_stream_id": "hosta:rejection-seat"})
            row = await store.fetch_session("hosta", "rejection-seat")
            generation = reconciler._row_generation(row)
            rejection = await store.find_report_rejection(
                "hosta:rejection-seat", session_generation=generation, statuses=("done", "error", "aborted"),
            )
            assert rejection is not None and rejection["status"] == "done"
        finally:
            store.stop()

    asyncio.run(run())


def test_rejection_sweep_spares_self_close_seat_on_terminal_rejection() -> None:
    # A schema-rejected
    # terminal report is NOT a completion signal. The sweep must SPARE the seat
    # report is not a completion signal, so the seat can re-file instead of
    # being reaped on the rejection alone.
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "rejection-seat")
            await sessions.refresh()
            ledger = Ledger(store, sessions=sessions, outbound=AsyncMock())
            with pytest.raises(Exception):
                await ledger.ingest({**_INVALID_TERMINAL_REPORT, "from_stream_id": "hosta:rejection-seat"})
            counters = {"self_close_swept": 0, "closed": 0}
            rows = await store.list_sessions("open")
            await reconciler._sweep_self_close_backlog(rows, counters)
            assert counters["self_close_swept"] == 0
            assert counters["closed"] == 0
            row = await store.fetch_session("hosta", "rejection-seat")
            assert row is not None and row["status"] == "open"
        finally:
            store.stop()

    asyncio.run(run())


def test_rejection_not_recorded_for_non_self_close_seat() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            _reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "plain-seat", self_close=False)
            await sessions.refresh()
            ledger = Ledger(store, sessions=sessions, outbound=AsyncMock())
            with pytest.raises(Exception):
                await ledger.ingest({**_INVALID_TERMINAL_REPORT, "from_stream_id": "hosta:plain-seat"})
            rejection = await store.find_report_rejection("hosta:plain-seat")
            assert rejection is None
        finally:
            store.stop()

    asyncio.run(run())


def test_rejection_sweep_ignores_stale_generation() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "rejection-seat", generation="generation-current")
            await sessions.refresh()
            # A rejection recorded against a PRIOR generation must not reap the
            # live successor.
            await store.record_report_rejection(
                "hosta:rejection-seat", session_generation="generation-stale",
                status="done", reason="stale",
            )
            counters = {"self_close_swept": 0, "closed": 0}
            rows = await store.list_sessions("open")
            await reconciler._sweep_self_close_backlog(rows, counters)
            assert counters["closed"] == 0
            row = await store.fetch_session("hosta", "rejection-seat")
            assert row is not None and row["status"] == "open"
        finally:
            store.stop()

    asyncio.run(run())


def test_rejection_does_not_close_row_that_dropped_self_close() -> None:
    # A recorded rejection must not authorize closing a seat whose current
    # row is no longer self_close, even when the parent is gone (predicate B).
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "drop-seat")  # parent hosta:leader is absent
            await sessions.refresh()
            ledger = Ledger(store, sessions=sessions, outbound=AsyncMock())
            with pytest.raises(Exception):
                await ledger.ingest({**_INVALID_TERMINAL_REPORT, "from_stream_id": "hosta:drop-seat"})
            # Seat drops the self-close flag after the rejection is on record.
            await store.update_session("hosta", "drop-seat", self_close_on_completion=False)
            await sessions.refresh()
            counters = {"self_close_swept": 0, "closed": 0}
            rows = await store.list_sessions("open")
            await reconciler._sweep_self_close_backlog(rows, counters)
            assert counters["closed"] == 0
            row = await store.fetch_session("hosta", "drop-seat")
            assert row is not None and row["status"] == "open"
        finally:
            store.stop()

    asyncio.run(run())


def test_unknown_envelope_field_records_rejection_for_self_close_seat() -> None:
    # An unknown envelope field rejects a
    # terminal report before payload validation — it must still be recorded.
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "env-seat")
            await sessions.refresh()
            ledger = Ledger(store, sessions=sessions, outbound=AsyncMock())
            with pytest.raises(Exception):
                await ledger.ingest({
                    "report_id": "rejection-env", "from_stream_id": "hosta:env-seat",
                    "caller_stream_id": "hosta:env-seat", "msg_id": 1, "status": "done",
                    "summary": "done", "findings": [], "next_action": "leader_proceed",
                    "bounce_required": True,  # unknown envelope field
                })
            row = await store.fetch_session("hosta", "env-seat")
            generation = reconciler._row_generation(row)
            rejection = await store.find_report_rejection(
                "hosta:env-seat", session_generation=generation, statuses=("done", "error", "aborted"),
            )
            assert rejection is not None and rejection["status"] == "done"
        finally:
            store.stop()

    asyncio.run(run())


def test_rejection_generation_reads_store_not_cache() -> None:
    # The rejection generation must come from the store row,
    # not a stale Sessions cache, so it matches the sweep's `_row_generation`.
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            _reconciler, sessions = _sweep_reconciler(store)
            await _open_self_close_seat(store, "gen-seat", generation="gen-store")
            await sessions.refresh()
            # Poison the in-memory cache with a different (stale) generation.
            sessions.apply_durable("hosta:gen-seat", session_generation="gen-cache")
            ledger = Ledger(store, sessions=sessions, outbound=AsyncMock())
            with pytest.raises(Exception):
                await ledger.ingest({**_INVALID_TERMINAL_REPORT, "from_stream_id": "hosta:gen-seat"})
            assert await store.find_report_rejection("hosta:gen-seat", session_generation="gen-store") is not None
            assert await store.find_report_rejection("hosta:gen-seat", session_generation="gen-cache") is None
        finally:
            store.stop()

    asyncio.run(run())
