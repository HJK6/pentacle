"""Presentation equality preserves lifecycle/destructive-operation fences."""
import os
import time
import pytest
from sessions import Sessions
from spawnctl import SpawnCtl
from store import Store
from test_ingest_provider_root_binding import BIRTH


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [(BIRTH, "released"), (BIRTH.replace("Sep 9", "Sep  9"), "released"),
    ("Wed Sep 9 11:00:01 2026", "deferred"), (" ", "deferred"), (7, "deferred")])
async def test_cancelled_spawn_legacy_start_comparison(monkeypatch, stored, expected):
    import prockill
    class Terminal:
        killed = 0
        async def session_state(self, name): return "gone" if self.killed else "alive"
        async def pane_pid(self, name): return "42420"
        async def pane_identity(self, name): return {"pane_id": "%1"}
        async def kill_pane(self, pane): self.killed += 1
    async def record(pid):
        return {"pid": int(pid), "uid": os.getuid(), "start_id": BIRTH, "command": "/synthetic/provider"}
    monkeypatch.setattr(prockill, "process_record", record)
    store = Store(":memory:"); store.start()
    try:
        terminal = Terminal(); ctl = SpawnCtl(store, Sessions(store, local_host="local"), tmux=terminal)
        result = await ctl._cleanup_cancelled_spawn({"host":"local", "session_name":"cancelled", "request_id":"cancel-one",
            "pane_pid":"42420", "pane_started_at":stored})
        assert result == expected and terminal.killed == int(expected == "released")
    finally: store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,signal", [(BIRTH, True), (BIRTH.replace("Sep 9", "Sep  9"), True),
    ("Wed Sep 9 11:00:01 2026", False), ("", False), (" ", False)])
async def test_reconciler_reap_compares_nonempty_canonical_starts(stored, signal):
    from test_session_reconciler import _ClosedSurvivorHosts, _FakeTmux, _proof
    from reconciler import SessionReconciler
    store = Store(":memory:"); store.start()
    try:
        sessions = Sessions(store, local_host="example-host")
        await store.open_session("example-host", "closed-survivor", pane_status="pane_dead")
        await store.update_session("example-host", "closed-survivor", status="closed", closed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        proof = _proof(pid=100, session="closed-survivor", pane="%1", tty="/dev/ttys001", socket="/tmp/synthetic-socket",
                       command="target-shell", start_id=BIRTH)
        proof["host"] = "example-host"
        proof["start_id"] = stored  # Historical snapshots keep their original presentation.
        await store.upsert_session_reap("example-host:closed-survivor", reap_status="survivors",
            survivors=[{"pid":100,"command":"target-shell","first_seen":"2026-09-09T00:00:00Z","ownership_proof_v2":proof}])
        terminal = _FakeTmux((0,"closed-survivor\t100\t%1\t/dev/ttys001\t/tmp/synthetic-socket\n"))
        hosts = _ClosedSurvivorHosts(terminal, "100 1 501 100 100 " + BIRTH + " target-shell\n")
        hosts.local_host = "example-host"
        hosts.peers = {"example-peer": object()}
        await SessionReconciler(sessions, hosts).reconcile_once()
        assert hosts.signals == ([("example-host",[100],15)] if signal else [])
    finally: store.stop()
