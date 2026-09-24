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


def test_collection_ignores_operator_machine_config():
    """Tools resolve host identity at import; a remote-only operator config must not break collection."""
    env = {key: value for key, value in os.environ.items()
           if key not in {"PENTACLE_HOST_ID", "AGENT_ORCH_HOST_ID", "TMUX"}}
    env["PENTACLE_MACHINES_JSON"] = json.dumps({"machines": [
        {"name": "hub", "ssh_target": "hub-ssh"}, {"name": "travel", "ssh_target": "travel-ssh"}]})
    service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/test_orphan_reaper.py", "tests/test_d2_live_delivery.py"],
        cwd=service_dir, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


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
    monkeypatch.setattr(spawn_fleet_smoke, "smoke_plan", lambda: {"hosts": (), "cells": ()})
    with pytest.raises(ValueError, match="host|cell"):
        spawn_fleet_smoke.run_matrix("ws://unused", tmp_path / "token", 1)


def test_smoke_plan_uses_explicit_file_and_filters_retired_hosts(monkeypatch, tmp_path):
    from tools import spawn_fleet_smoke
    path = tmp_path / "machines.json"
    path.write_text(json.dumps({"machines": [
        {"name": "thoth", "ssh_target": None},
        {"name": "bart", "ssh_target": "bart-ssh"},
        {"name": "merlin", "ssh_target": "merlin-ssh"},
        {"name": "amaterasu", "ssh_target": "amaterasu-ssh"},
    ]}))
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(path))
    monkeypatch.delenv("PENTACLE_MACHINES_JSON", raising=False)
    monkeypatch.delenv("PENTACLE_SMOKE_HOSTS", raising=False)
    plan = spawn_fleet_smoke.smoke_plan()
    assert plan["source"] == str(path)
    assert plan["hosts"] == ("thoth", "merlin", "amaterasu")
    assert plan["excluded_absent"] == ("daffodil",)

    monkeypatch.setenv("PENTACLE_SMOKE_HOSTS", "unknown")
    with pytest.raises(ValueError, match="unknown|configured"):
        spawn_fleet_smoke.smoke_plan()
    monkeypatch.delenv("PENTACLE_SMOKE_HOSTS")
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", json.dumps(_fleet()))
    with pytest.raises(ValueError, match="PENTACLE_MACHINES_JSON"):
        spawn_fleet_smoke.smoke_plan()
    monkeypatch.delenv("PENTACLE_MACHINES_JSON")
    path.unlink()
    with pytest.raises((ValueError, FileNotFoundError), match="machine|file|exist"):
        spawn_fleet_smoke.smoke_plan()
    monkeypatch.delenv("PENTACLE_MACHINES_FILE")
    with pytest.raises(ValueError, match="PENTACLE_MACHINES_FILE"):
        spawn_fleet_smoke.smoke_plan()


def test_smoke_dry_run_never_enters_connection_or_matrix(monkeypatch, tmp_path, capsys):
    from tools import spawn_fleet_smoke
    path = tmp_path / "machines.json"
    path.write_text(json.dumps({"machines": [
        {"name": "thoth", "ssh_target": None},
        {"name": "bart", "ssh_target": "bart-ssh"},
        {"name": "merlin", "ssh_target": "merlin-ssh"},
        {"name": "amaterasu", "ssh_target": "amaterasu-ssh"},
    ]}))
    monkeypatch.setenv("PENTACLE_MACHINES_FILE", str(path))
    monkeypatch.delenv("PENTACLE_MACHINES_JSON", raising=False)
    monkeypatch.delenv("PENTACLE_SMOKE_HOSTS", raising=False)
    def forbidden(*_args, **_kwargs):
        pytest.fail("dry run entered a live connection or matrix")
    monkeypatch.setattr(spawn_fleet_smoke, "_operator_connection", forbidden)
    monkeypatch.setattr(spawn_fleet_smoke, "run_matrix", forbidden)
    assert spawn_fleet_smoke.main(["--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source"] == str(path)
    assert result["hosts"] == ["thoth", "merlin", "amaterasu"]
    assert len(result["cells"]) == 12


def test_quota_cell_with_failed_teardown_reports_leak():
    from tools import spawn_fleet_smoke
    def rpc(payload, _expected):
        if payload["type"] == "spawn_catalog_get":
            return {"type": "spawn_catalog_get.ok", "profiles": {
                "desktop_manual": {"codex": ("gpt-6-sol", "medium")}},
                "catalog_version": "test"}
        return {"stream_id": "thoth:v2-fleet-smoke-codex-example"}
    def event_timeout(_stream_id, _marker):
        raise TimeoutError("event timeout")
    def close_failure(_stream_id):
        raise RuntimeError("pane remained open")
    with pytest.raises(RuntimeError, match="teardown: pane remained open"):
        spawn_fleet_smoke.run_cell(
            "thoth", "codex", "prompted", rpc=rpc,
            wait_ready=lambda _stream_id: None,
            wait_event=event_timeout,
            verify_teardown=close_failure,
            register_owned=lambda *_args: None,
            close_owned=close_failure,
            capture_pane=lambda *_args: "You've hit your usage limit. Try again at 11:00.",
        )


@pytest.mark.parametrize("hosts", ["hub", "unknown", "travel,hub"])
def test_explicit_satellites_reject_local_and_unknown_names(monkeypatch, hosts):
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", json.dumps(_fleet()))
    monkeypatch.setenv("PENTACLE_SATELLITE_HOSTS", hosts)
    with pytest.raises(ValueError, match="remote"):
        machines.configured_host_names("PENTACLE_SATELLITE_HOSTS", remote_only=True)


def test_explicit_satellite_subset_uses_configured_remote(monkeypatch):
    monkeypatch.setenv("PENTACLE_MACHINES_JSON", json.dumps(_fleet()))
    monkeypatch.setenv("PENTACLE_SATELLITE_HOSTS", "travel")
    assert machines.configured_host_names("PENTACLE_SATELLITE_HOSTS", remote_only=True) == ("travel",)
