"""Alternate rendering is liveness evidence, not independent signal authority."""
import copy
from types import SimpleNamespace

import pytest

import prockill
from reconciler import SessionReconciler

BOOT = "{ sec = 1780000000, usec = 123 } Fri May 29 01:46:40 2026"
LOCAL_BOOT = "{ sec = 1780000000, usec = 123 } Thu May 28 20:46:40 2026"
UTC = "Wed Sep 9 16:00:00 2026"
LOCAL = "Wed Sep 9 11:00:00 2026"


def candidate():
    current = {"pid": 42420, "ppid": 1, "uid": 501, "pgid": 42420, "sid": 42420,
               "start_id": UTC, "command": "owned-synthetic"}
    proof = prockill.identity_proof(host="local", boot=LOCAL_BOOT,
        record={**current, "start_id": LOCAL}, tmux_session="owned", tmux_pane="%42",
        tty="/dev/synthetic", tmux_socket="/tmp/synthetic-socket")
    proof["captured_at"] = "2026-09-09T16:00:02Z"
    current["birth_observation"] = {"host": "local", "utc": UTC, "local": LOCAL,
        "boot_before": BOOT, "boot_after": LOCAL_BOOT}
    owner = object.__new__(SessionReconciler)
    owner.sessions = SimpleNamespace(local_host="local")
    owner.hosts = SimpleNamespace(local_host="local")
    return owner, proof, current


@pytest.mark.parametrize("change,reason", [
    ("none", None),
    ("after_capture", "capture_clock_incomparable"),
    ("equal_second", "capture_clock_incomparable"),
    ("missing_capture", "capture_clock_incomparable"),
    ("offset_capture", "capture_clock_incomparable"),
    ("missing_observation", "identity_drift"),
    ("unknown_zone", "identity_drift"),
    ("wrong_inventory_host", "identity_drift"),
    ("remote_capture", "clock_provenance_unknown"),
    ("linux_uuid", "clock_provenance_unknown"),
    ("changed_boot_sec", "boot_clock_changed"),
    ("changed_boot_usec", "boot_clock_changed"),
    ("clock_step_during_read", "clock_provenance_unknown"),
    ("foreign_uid", "identity_drift"),
    ("foreign_command", "identity_drift"),
    ("foreign_lineage", "identity_drift"),
    ("foreign_proof_host", "identity_drift"),
    ("foreign_pane", "identity_drift"),
])
def test_alternate_destructive_comparison_keeps_every_fence(change, reason):
    owner, proof, current = candidate()
    boot = BOOT
    panes = []
    if change == "after_capture":
        proof["captured_at"] = "2026-09-09T15:59:59Z"
    elif change == "equal_second":
        proof["captured_at"] = "2026-09-09T16:00:00Z"
    elif change == "missing_capture":
        proof["captured_at"] = ""
    elif change == "offset_capture":
        proof["captured_at"] = "2026-09-09T16:00:02+00:00"
    elif change == "missing_observation":
        del current["birth_observation"]
    elif change == "unknown_zone":
        proof["start_id"] = "Thu Sep 10 01:00:00 2026"
    elif change == "wrong_inventory_host":
        current["birth_observation"]["host"] = "remote"
    elif change == "remote_capture":
        owner.sessions.local_host = "coordinator"
        owner.hosts.local_host = "coordinator"
    elif change == "linux_uuid":
        boot = proof["boot_id"] = "12345678-1234-1234-1234-123456789abc"
        current["birth_observation"].update(boot_before=boot, boot_after=boot)
    elif change == "changed_boot_sec":
        boot = BOOT.replace("1780000000", "1780000001")
    elif change == "changed_boot_usec":
        boot = BOOT.replace("123", "124")
    elif change == "clock_step_during_read":
        current["birth_observation"]["boot_after"] = BOOT.replace("1780000000", "1780000010")
    elif change == "foreign_uid":
        current["uid"] += 1
    elif change == "foreign_command":
        current["command"] = "foreign"
    elif change == "foreign_lineage":
        current["ppid"] = 2
    elif change == "foreign_proof_host":
        proof["host"] = "remote"
    elif change == "foreign_pane":
        panes = [{"session_name": "owned", "pane_pid": 42420, "pane_id": "%99",
                  "tty": "/dev/other", "tmux_socket": "/tmp/other"}]
    original = copy.deepcopy(proof)
    safe, deferred = owner._identity_safe_records("local", "owned", [
        {"pid": 42420, "ownership_proof_v2": proof}], panes, {42420: current}, boot)
    assert bool(safe) == (reason is None)
    assert [row["reap_reason"] for row in deferred] == ([] if reason is None else [reason])
    assert proof == original  # Consumer-only: persisted birth/boot are never rewritten.


def test_linux_exact_rendering_authority_is_preserved():
    owner, proof, current = candidate()
    boot = proof["boot_id"] = "12345678-1234-1234-1234-123456789abc"
    current["start_id"] = proof["start_id"]
    del current["birth_observation"]
    safe, deferred = owner._identity_safe_records("local", "owned", [
        {"pid": 42420, "ownership_proof_v2": proof}], [], {42420: current}, boot)
    assert len(safe) == 1 and deferred == []


@pytest.mark.parametrize("other,same", [
    (LOCAL_BOOT, True),
    (BOOT.replace("1780000000", "1780000001"), False),
    (BOOT.replace("123", "124"), False),
    ("{ sec = bad, usec = 123 } trailing", False),
    ("{ sec = 1780000000, usec = 1000000 } trailing", False),
    ("", False),
])
def test_numeric_darwin_boot_comparison(other, same):
    assert prockill.boot_matches(BOOT, other) is same


@pytest.mark.requires("tmux usable", "ps birth readable")
@pytest.mark.parametrize("producer,consumer", [("unset", "UTC0"), ("UTC0", "unset")])
def test_saved_sessions_capture_reopens_and_reaps_with_recording_sink(tmp_path, monkeypatch, producer, consumer):
    """Actual private tmux/ps/boot/Sessions→Store→reaper; only signal transport is fake."""
    import asyncio
    from collections import Counter
    import os
    import subprocess
    import sys
    from sessions import Sessions
    from store import Store
    from tmux_transport import Tmux
    from tests.tmux_isolation import isolate_tmux
    from tests.test_process_ownership_semantics import environment

    wrapper, _socket = isolate_tmux(tmp_path, monkeypatch)
    terminal = Tmux(wrapper)
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])
    deliveries = []

    class Hosts:
        local_host = "local"
        def tmux_for(self, host):
            assert host == "local"
            return terminal
        async def run_command(self, host, *argv, timeout=10):
            assert host == "local"
            result = await asyncio.create_subprocess_exec(*argv,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
            try:
                out, _err = await asyncio.wait_for(result.communicate(), timeout)
            except BaseException:
                await prockill.terminate_and_reap(result)
                raise
            return result.returncode, out.decode()
        def signal_pids(self, host, pids, sig):
            assert host == "local" and pids == [root]
            assert sentinel.pid not in pids
            deliveries.append((pids, int(sig)))
            return True  # Record only. No signal is delivered, even to the subject.

    async def run():
        nonlocal root
        store = Store(str(tmp_path / "saved-reap.db"))
        store.start()
        try:
            environment(monkeypatch, producer)
            rc, _ = await terminal.run("new-session", "-d", "-s", "owned", "exec sleep 90")
            assert rc == 0
            root = int(await terminal.pane_pid("owned"))
            # Birth/capture second equality must not accidentally qualify an
            # alternate. Let the disposable process precede capture by >1s.
            await asyncio.sleep(1.05)
            sessions = Sessions(store, tmux=terminal, local_host="local")
            pane = await terminal.close_pane_identity("owned")
            assert pane is not None
            tracked, records, proofs, complete = await sessions._capture_tree_identity("owned", str(root), pane)
            assert complete and tracked == [root] and root in proofs
            # Persist only readback from a fully anchored capture.
            cohort = prockill.CapturedTree(tracked, records, proofs, complete)
            try:
                await cohort.prepare()
                assert await terminal.close_pane_identity("owned") == pane
                assert await cohort.authorize(root, int(pane["server_pid"]))
                captured = await cohort.readback()
                assert captured["reap_status"] == "survivors"
                proofs = {item["pid"]: item["ownership_proof_v2"]
                          for item in captured["survivors"]}
            finally:
                cohort.close()
            original_proof = copy.deepcopy(proofs[root])
            await store.open_session("local", "owned", pane_status="pane_dead")
            await store.update_session("local", "owned", status="closed", closed_at=__import__("v2_runtime").iso_now())
            await store.upsert_session_reap("local:owned", reap_status="survivors", survivors=[
                {"pid": root, "ownership_proof_v2": proofs[root]}])
            assert store.stop()
            environment(monkeypatch, consumer)
            store.start()
            foreign_before = await prockill.process_record(str(sentinel.pid))
            owner = SessionReconciler(sessions, Hosts())
            current = await owner._reap_inventory("local")
            assert current is not None
            raw_matches = prockill.birth_matches(original_proof["start_id"], current[1][root]["start_id"])
            await owner._reconcile_closed_survivors(Counter())
            row = await store.get_session_reap("local:owned")
            assert row["survivors"][0]["ownership_proof_v2"] == original_proof
            if raw_matches or sys.platform == "darwin":
                assert deliveries == [([root], 15)]
            else:
                assert deliveries == []
                assert row["survivors"][0]["reap_reason"] == "clock_provenance_unknown"
            assert sentinel.poll() is None
            assert await prockill.process_record(str(sentinel.pid)) == foreign_before
            assert await terminal.has_session("owned")
        finally:
            await terminal.run("-N", "kill-server")
            rc, _ = await terminal.run("-N", "has-session", "-t", "=owned")
            assert rc != 0
            store.stop()

    root = -1
    try:
        asyncio.run(run())
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)


@pytest.mark.parametrize("case,reason", [
    ("qualified", None),
    ("offset_collision", "capture_clock_incomparable"),
    ("equal_second", "capture_clock_incomparable"),
    ("missing_capture", "capture_clock_incomparable"),
    ("untrusted_host", "clock_provenance_unknown"),
    ("linux", "clock_provenance_unknown"),
    ("clock_step", "boot_clock_changed"),
    ("clock_step_during_read", "clock_provenance_unknown"),
    ("snapshot_pid_reuse", "identity_drift"),
    ("malformed_snapshot", "identity_drift"),
    ("malformed_boot", "clock_provenance_unknown"),
])
def test_saved_alternate_proof_reaches_only_the_allowed_signal_sink(tmp_path, case, reason):
    import asyncio
    from collections import Counter
    import json
    from sessions import Sessions
    from store import Store
    from v2_runtime import iso_now

    async def run():
        _owner, proof, current = candidate()
        if case == "offset_collision":
            proof["captured_at"] = "2026-09-09T15:59:59Z"
        elif case == "equal_second":
            proof["captured_at"] = "2026-09-09T16:00:00Z"
        elif case == "missing_capture":
            proof["captured_at"] = ""
        boot = BOOT
        if case == "linux":
            boot = proof["boot_id"] = "12345678-1234-1234-1234-123456789abc"
        elif case == "clock_step":
            # A backward-step replacement may appear older than capture. Boot
            # sec/usec provenance must still refuse it before signal intent.
            boot = BOOT.replace("1780000000", "1780000010")
        elif case == "malformed_boot":
            boot = "{ sec = unreadable, usec = 123 }"
        utc = f"42420 1 501 42420 42420 {UTC} owned-synthetic\n"
        local = utc.replace(UTC, LOCAL)
        snapshot = {"utc": utc, "local": local, "utc_after": utc,
                    "boot_before": boot, "boot_after": boot}
        if case == "clock_step_during_read":
            snapshot["boot_after"] = BOOT.replace("1780000000", "1780000010")
        elif case == "snapshot_pid_reuse":
            snapshot["utc_after"] = utc.replace("16:00:00", "16:00:01")
        elif case == "malformed_snapshot":
            snapshot["utc"] = None
        deliveries = []
        class Terminal:
            async def run(self, *args, **kwargs): return 0, ""
        class Hosts:
            local_host = "remote" if case == "untrusted_host" else "local"
            def tmux_for(self, host): return Terminal()
            async def run_command(self, host, *args, **kwargs):
                assert host == "local"
                if args[0] == "ps": return 0, utc
                if args[0] in {"sysctl", "cat"}: return 0, boot
                assert args == ("python3", "-c", prockill.comparison_inventory_probe())
                return 0, json.dumps(snapshot)
            def signal_pids(self, host, pids, sig):
                deliveries.append((host, pids, int(sig)))
                return True  # The sole destructive counterpart records intent.
        store = Store(str(tmp_path / "synthetic-reap.db"))
        store.start()
        try:
            await store.open_session("local", "owned")
            await store.update_session("local", "owned", status="closed", closed_at=iso_now())
            await store.upsert_session_reap("local:owned", reap_status="survivors", survivors=[
                {"pid": 42420, "ownership_proof_v2": proof}])
            assert store.stop()
            store.start()
            owner = SessionReconciler(Sessions(store, local_host="local"), Hosts())
            await owner._reconcile_closed_survivors(Counter())
            row = await store.get_session_reap("local:owned")
            assert deliveries == ([("local", [42420], 15)] if reason is None else [])
            if reason:
                assert row["survivors"][0]["reap_reason"] == reason
            assert row["survivors"][0]["ownership_proof_v2"] == proof
        finally:
            store.stop()
    asyncio.run(run())
