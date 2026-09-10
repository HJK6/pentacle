"""Public host configuration must work without a built-in fleet."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

import machines


def _fleet():
    return {"machines": [
        {"name": "hub", "ssh_target": None},
        {"name": "travel", "ssh_target": "travel-ssh", "claude_bin": "claude",
         "codex_bin": "codex", "cwd": "/tmp/work", "projects_root": "/tmp/projects",
         "agent_orch_bin_dir": "/tmp/bin"},
    ]}


def test_tools_follow_configured_machine_allowlist(monkeypatch):
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", json.dumps(_fleet()))
    monkeypatch.delenv("PENTACLE_SMOKE_HOSTS", raising=False)
    monkeypatch.delenv("PENTACLE_SATELLITE_HOSTS", raising=False)
    assert machines.configured_host_names("PENTACLE_SMOKE_HOSTS") == ("hub", "travel")
    assert machines.configured_host_names("PENTACLE_SATELLITE_HOSTS", remote_only=True) == ("travel",)


def test_explicit_target_list_overrides_machine_config(monkeypatch):
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", json.dumps(_fleet()))
    monkeypatch.setenv("PENTACLE_SMOKE_HOSTS", " travel , hub ")
    assert machines.configured_host_names("PENTACLE_SMOKE_HOSTS") == ("travel", "hub")


@pytest.mark.parametrize("value", ["", " , ", "travel,travel"])
def test_empty_or_duplicate_explicit_targets_rejected(monkeypatch, value):
    monkeypatch.setenv("PENTACLE_SMOKE_HOSTS", value)
    with pytest.raises(ValueError):
        machines.configured_host_names("PENTACLE_SMOKE_HOSTS")


def test_local_identity_uses_env_then_config(monkeypatch):
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", json.dumps(_fleet()))
    monkeypatch.delenv("PENTACLE_HOST_ID", raising=False)
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    assert machines.configured_local_host() == "hub"
    monkeypatch.setenv("AGENT_ORCH_HOST_ID", "office")
    assert machines.configured_local_host() == "office"
    monkeypatch.setenv("PENTACLE_HOST_ID", "coordinator")
    assert machines.configured_local_host() == "coordinator"


def test_marker_host_is_opt_in_and_uses_custom_identity(monkeypatch):
    env = dict(os.environ)
    env.pop("PENTACLE_AUTH_CONTEXT_MARKER_HOST", None)
    code = "import spawnctl; print(repr(spawnctl.AUTH_CONTEXT_MARKER_HOST))"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "''"
    env["PENTACLE_AUTH_CONTEXT_MARKER_HOST"] = "travel"
    code = "from spawnctl import SpawnCtl; assert SpawnCtl._uses_auth_context_marker('travel', 'claude'); assert not SpawnCtl._uses_auth_context_marker('office', 'claude'); assert not SpawnCtl._uses_auth_context_marker('travel', 'codex')"
    subprocess.run([sys.executable, "-c", code], env=env, check=True)


def test_pin_rejects_no_satellites_before_opening_store(monkeypatch):
    from tools import event_push_pin
    monkeypatch.setattr(event_push_pin, "SATELLITE_HOSTS", ())
    def forbidden(*args, **kwargs):
        pytest.fail("must reject empty target set before opening Store")
    monkeypatch.setattr(event_push_pin, "Store", forbidden)
    with pytest.raises(ValueError, match="satellite"):
        asyncio.run(event_push_pin._run("unused", stage="a" * 40, rollback=False))


def test_smoke_rejects_empty_matrix_before_sessions(monkeypatch, tmp_path):
    from tools import spawn_fleet_smoke
    monkeypatch.setattr(spawn_fleet_smoke, "HOSTS", ())
    with pytest.raises(ValueError, match="host|cell"):
        spawn_fleet_smoke.run_matrix("ws://unused", tmp_path / "token", 1)
