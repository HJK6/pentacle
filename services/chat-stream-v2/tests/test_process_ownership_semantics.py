"""Real producer/persistence/consumer boundaries; transports only record actions."""
import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

import assistant_restore as ar
from tests.test_assistant_restore import Rig, LOCAL, SEAT, G0


def environment(monkeypatch, zone):
    if zone == "unset":
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", zone)
    monkeypatch.setenv("LC_ALL", "C")


def producer_start(pid):
    # The actual producer inherits its environment. Never normalize its token.
    return " ".join(subprocess.check_output(
        ["ps", "-o", "lstart=", "-p", str(pid)], text=True, timeout=5).split())


@pytest.mark.requires("ps birth readable")
@pytest.mark.parametrize("producer", ["unset", "", "UTC0", "CST6CDT"])
@pytest.mark.parametrize("consumer", ["unset", "", "UTC0", "CST6CDT"])
def test_live_child_survives_store_reopen_and_restore(tmp_path, monkeypatch, producer, consumer):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])

    async def run():
        rig = Rig(tmp_path, monkeypatch)
        try:
            environment(monkeypatch, producer)
            await rig.start()
            from spawnctl import SpawnCtl
            token = await SpawnCtl._pane_started_at(
                SimpleNamespace(sessions=rig.sessions, hosts=None), str(child.pid))
            assert token == producer_start(child.pid)
            await rig.store.update_session(LOCAL, "fd", observer_binding={
                "executable": "claude", "pane_pid": str(child.pid),
                "pane_started_at": token, "generation": G0})
            assert rig.store.stop()
            environment(monkeypatch, consumer)
            rig.store.start()
            # Reconstruct the actual consumer and use the actual OS reader.
            rig.restore = rig.owner()
            rig.restore._ps = ar.run_ps
            assert (await rig.store.fetch_session(LOCAL, "fd"))["observer_binding"]["pane_started_at"] == token
            proof = await rig.restore.input_death_proof(SEAT, G0)
            status = await rig.restore.advance()
            assert proof[0] is False and child.poll() is None
            assert status["state"] == "waiting_evidence"
            assert await rig.episodes() == [] and rig.spawnctl.sent == []
        finally:
            await rig.composite.stop()
            rig.store.stop()
    try:
        asyncio.run(run())
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.requires("ps birth readable")
def test_unknown_historical_zone_cannot_authorize_manual_restore_or_input_replay(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])

    async def run():
        rig = Rig(tmp_path, monkeypatch)
        sent = []
        async def dispatch(route):
            sent.append(route)
            return {"delivery": "landed"}
        try:
            await rig.start()
            environment(monkeypatch, "JST-9")
            token = producer_start(child.pid)
            environment(monkeypatch, "UTC0")
            await rig.store.update_session(LOCAL, "fd", observer_binding={
                "executable": "claude", "pane_pid": str(child.pid),
                "pane_started_at": token, "generation": G0})
            rig.restore._ps = ar.run_ps
            rig.composite.input_death_proof = rig.restore.input_death_proof
            rig.composite.dispatch = dispatch
            proof = await rig.restore.input_death_proof(SEAT, G0)
            assert proof[:3] == (False, "waiting_evidence", "process_identity_incomparable")
            assert (await rig.restore.advance())["reason"] == "process_identity_incomparable"
            # Manual restore obeys the same proof; it is no bypass.
            await rig.restore.manual("restore", "unknown-zone")
            assert await rig.episodes() == [] and rig.spawnctl.sent == []
            message = {"optimistic_id": "identity-input", "text": "synthetic retained input"}
            first = await rig.composite.accept_input(message)
            if rig.composite._worker:
                await rig.composite._worker
            route = await rig.store.get_assistant_composite_route(
                stream_id=rig.composite.config.stream_id, input_identity="identity-input")
            assert route["dispatch_id"] is None and sent == []
            assert route["error_code"] == "assistant_recovery_process_identity_incomparable"
            again = await rig.composite.accept_input(message)
            assert again["duplicate"] and again["event_id"] == first["event_id"]
            assert child.poll() is None
        finally:
            await rig.composite.stop()
            rig.store.stop()
    try:
        asyncio.run(run())
    finally:
        child.terminate()
        child.wait(timeout=5)


@pytest.mark.requires("ps birth readable")
def test_definitively_absent_owned_child_still_restores_once(tmp_path, monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"])

    async def run():
        rig = Rig(tmp_path, monkeypatch)
        try:
            await rig.start()
            environment(monkeypatch, "UTC0")
            token = producer_start(child.pid)
            await rig.store.update_session(LOCAL, "fd", observer_binding={
                "executable": "claude", "pane_pid": str(child.pid),
                "pane_started_at": token, "generation": G0})
            child.terminate()
            child.wait(timeout=5)
            assert rig.store.stop()
            environment(monkeypatch, "unset")
            rig.store.start()
            rig.restore = rig.owner()
            rig.restore._ps = ar.run_ps
            assert (await rig.restore.input_death_proof(SEAT, G0))[0] is True
            assert (await rig.restore.advance())["state"] == "restored"
            await rig.restore.advance()
            assert len(rig.spawnctl.sent) == 1 and len(await rig.episodes()) == 1
        finally:
            await rig.composite.stop()
            rig.store.stop()
    try:
        asyncio.run(run())
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("kernel", ["gone", "present", "permission", "unexpected"])
def test_silent_ps_cannot_override_kernel_presence_or_permission(monkeypatch, kernel):
    from tests.test_process_identity_table import Process
    calls = []
    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return Process(1, b"")
    def existence(pid, sig):
        assert (pid, sig) == (990001, 0)
        if kernel == "gone": raise ProcessLookupError()
        if kernel == "permission": raise PermissionError()
        if kernel == "unexpected": raise OSError("unavailable")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ar.os, "kill", existence)
    values = asyncio.run(ar.run_ps("990001"))
    expected = "gone" if kernel == "gone" else "unknown"
    assert [ar.classify_ps(*value, "Wed Sep 9 11:00:00 2026") for value in values] == [expected, expected]
    assert calls[0][1]["TZ"] == "UTC0" and "TZ" not in calls[1][1]
    assert all(env["LC_ALL"] == "C" for _, env in calls)
