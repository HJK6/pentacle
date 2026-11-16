"""`agent-orch usage --host` on a satellite reads the satellite itself locally.

Regression for the 2026-10-07 CLI round (pentacle 5e1f6c7): on Amaterasu the
readback resolved the local host as "thoth", so `--host amaterasu` was treated
as remote and ssh'd to itself (transport_error); on Merlin, with no
machines.json, the only known host was "thoth" ("unknown host 'merlin'").
"""

from __future__ import annotations

import json

import pytest

from agent_orch import config as agent_config
from agent_orch import usage_readback as ur

NOW_MS = 1_791_000_000_000


@pytest.fixture
def satellite(tmp_path, monkeypatch):
    """A satellite home: ~/.agent-orch/config.json names the host; no collector state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_ORCH_HOST_ID", raising=False)
    monkeypatch.delenv("PENTACLE_HOST_ID", raising=False)
    monkeypatch.setenv("PENTACLE_USAGE_HISTORY_PATH", str(tmp_path / "usage_history.jsonl"))

    def make(host: str, machines: list | None = None) -> dict:
        (tmp_path / ".agent-orch").mkdir(exist_ok=True)
        (tmp_path / ".agent-orch" / "config.json").write_text(json.dumps({"local_host_id": host}))
        env = {"PENTACLE_USAGE_STATE_PATH": str(tmp_path / "missing_usage_state.json"),
               "PENTACLE_MACHINES_FILE": str(tmp_path / "missing_machines.json")}
        if machines is not None:
            env["PENTACLE_MACHINES_JSON"] = json.dumps(machines)
        return env

    return make


def _pluck() -> dict:
    return {"oauth_account_uuid": "acct-A", "cache_account_uuid": "acct-A", "fetched_at_ms": NOW_MS - 60_000,
            "seven_day_pct": 42, "seven_day_resets": "2026-10-11T07:00:00Z",
            "five_hour_pct": 3, "five_hour_resets": None}


class Calls:
    def __init__(self):
        self.local: list[str] = []

    def local_runner(self, target, command, *, input_text=None, **kw):
        self.local.append(command)
        if input_text == ur.CLAUDE_CACHE_PLUCK:
            return 0, json.dumps(_pluck()), ""
        return 0, json.dumps({"pct": 6}), ""

    @staticmethod
    def ssh_runner(target, command, **kw):
        raise AssertionError(f"ssh used for the local host: {target}")


def test_local_host_id_follows_agent_orch_config(satellite, monkeypatch):
    satellite("merlin")
    assert agent_config.local_host_id() == "merlin"
    assert ur.configured_local_host({}) == "merlin"
    assert ur.configured_local_host({"PENTACLE_HOST_ID": "thoth"}) == "thoth"  # explicit override wins
    monkeypatch.setenv("AGENT_ORCH_HOST_ID", "amaterasu")
    assert agent_config.local_host_id() == "amaterasu"


def test_satellite_without_registry_reads_itself(satellite):
    env = satellite("merlin")
    calls = Calls()
    rows = ur.read_host("merlin", env=env, runner=calls.ssh_runner, local_runner=calls.local_runner, now_ms=NOW_MS)
    by = {r["provider"]: r for r in rows}
    assert by["claude"]["outcome"] == "ok" and by["claude"]["pct"] == 42 and by["claude"]["source"] == "claude-cache"
    assert by["codex"]["outcome"] == "ok" and by["codex"]["pct"] == 6
    assert all(r["host"] == "merlin" for r in rows)
    assert len(calls.local) == 2


def test_satellite_registry_entry_for_itself_is_local(satellite):
    env = satellite("amaterasu", machines=[
        {"name": "merlin", "ssh_target": "u@merlin"},
        {"name": "amaterasu", "ssh_target": "u@amaterasu"},
    ])
    calls = Calls()
    rows = ur.read_host("amaterasu", env=env, runner=calls.ssh_runner, local_runner=calls.local_runner,
                        now_ms=NOW_MS)
    assert {r["provider"]: r["pct"] for r in rows} == {"claude": 42, "codex": 6}


def test_satellite_still_reads_other_hosts_over_ssh(satellite):
    env = satellite("amaterasu", machines=[{"name": "merlin", "ssh_target": "u@merlin"},
                                           {"name": "amaterasu", "ssh_target": "u@amaterasu"}])
    seen = []

    def ssh_runner(target, command, *, input_text=None, **kw):
        seen.append(target)
        return (0, json.dumps(_pluck()), "") if input_text else (0, json.dumps({"pct": 9}), "")

    rows = ur.read_host("merlin", env=env, runner=ssh_runner, local_runner=Calls.ssh_runner, now_ms=NOW_MS)
    assert set(seen) == {"u@merlin"} and {r["host"] for r in rows} == {"merlin"}


def test_collector_host_keeps_the_cadence_file(satellite, tmp_path):
    env = satellite("thoth", machines=[{"name": "thoth", "ssh_target": None}])
    state = tmp_path / "usage_state.json"
    state.write_text(json.dumps({"claude_health": {"outcome": "ok"}, "codex_health": {"outcome": "ok"},
                                 "claude_fable_lkg": [{"id": "claude", "pct": 97}], "codex_lkg": {"pct": 15}}))
    env["PENTACLE_USAGE_STATE_PATH"] = str(state)
    rows = ur.read_host("thoth", env=env, runner=Calls.ssh_runner, local_runner=Calls.ssh_runner)
    assert all(r["source"] == "cadence-file" for r in rows)


def test_run_local_executes_without_ssh():
    assert ur.run_local("ignored-target", "cat", input_text="ping") == (0, "ping", "")
    code, _, err = ur.run_local("ignored-target", "sleep 5", read_timeout=0.2)
    assert code == ur.EXIT_SSH_TIMEOUT and "timed out" in err
