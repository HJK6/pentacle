"""Private peer qualification is closed and inert unless explicitly valid."""
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from machines import _machine_from_dict, load_machines
from queued_send_now import PINS, route_for


PIN = {"version": "fixture-native", "executable_sha256": "f" * 64}


def peer(qualification):
    return _machine_from_dict({"name": "peer-a", "ssh_target": "peer.invalid",
        "queued_send_now_qualifications": qualification})


def row(provider="claude"):
    return {"host": "peer-a", "provider": provider,
            "account_binding": {"host": "peer-a", "provider": provider, **PIN}}


def test_private_config_qualifies_only_its_bound_peer_and_provider():
    machine = peer({"claude": PIN})
    assert machine.queued_send_now_qualifications == (("claude", PIN["version"], PIN["executable_sha256"]),)
    assert route_for(row(), "coordinator", {"peer-a": machine}) == "claude-native"
    assert route_for(row("codex"), "coordinator", {"peer-a": machine}) is None
    assert route_for(row(), "coordinator", {"peer-b": machine}) is None
    assert route_for(row(), "coordinator", {"peer-a": replace(machine, name="peer-b")}) is None
    assert route_for(row(), "coordinator", {"peer-a": replace(machine, ssh_target=None)}) is None


@pytest.mark.parametrize("qualification", [
    None, {}, [], True, {"other": PIN}, {"claude": None},
    {"claude": {"version": PIN["version"]}},
    {"claude": {**PIN, "unknown": True}},
    {"claude": {**PIN, "executable_sha256": "not-a-digest"}},
    {"claude": {**PIN, "version": ""}},
    {"claude": {**PIN, "version": "x" * 129}},
    {"claude": PIN, "codex": {**PIN, "unknown": True}},
])
def test_absent_or_malformed_qualification_keeps_peer_available_but_action_closed(qualification):
    machine = peer(qualification)
    assert machine.name == "peer-a" and machine.ssh_target == "peer.invalid"
    assert getattr(machine, "queued_send_now_qualifications", ()) == ()
    assert route_for(row(), "coordinator", {"peer-a": machine}) is None


def test_no_qualification_default_cannot_enable_remote_or_change_local_pins():
    machine = _machine_from_dict({"name": "peer-a", "ssh_target": "peer.invalid"})
    assert route_for(row(), "coordinator", {"peer-a": machine}) is None
    for provider, (version, digest) in PINS.items():
        local = {"host": "coordinator", "provider": provider,
                 "account_binding": {"version": version, "executable_sha256": digest}}
        assert route_for(local, "coordinator") is not None


def test_snapshot_projects_private_config_without_live_io_or_mutating_the_row():
    from server import Server
    projection = SimpleNamespace(local_host="coordinator", queued_send_now=object(),
        hosts=SimpleNamespace(peers={"peer-a": peer({"claude": PIN})}),
        _composite_for=lambda _sid: None)
    original = {**row(), "stream_id": "peer-a:owned", "capabilities": {"other": True}}
    projected = Server._project_assistant_composite_session(projection, original)
    assert projected["capabilities"] == {"other": True, "queued_send_now": True}
    assert original["capabilities"] == {"other": True}
    projection.hosts.peers = {"peer-a": peer({"claude": {**PIN, "unknown": True}})}
    assert Server._project_assistant_composite_session(projection, original)["capabilities"] == {
        "other": True, "queued_send_now": False}


def test_file_and_isolated_env_machine_loaders_preserve_only_valid_qualification(tmp_path):
    payload = {"machines": [{"name": "peer-a", "ssh_target": "peer.invalid",
        "queued_send_now_qualifications": {"claude": PIN}}]}
    file = tmp_path / "machines.json"
    file.write_text(json.dumps(payload))
    for env in ({"PENTACLE_MACHINES_FILE": str(file)},
                {"PENTACLE_MACHINES_JSON": json.dumps(payload)}):
        machines = load_machines(env)
        assert route_for(row(), "coordinator", {m.name: m for m in machines}) == "claude-native"
    payload["machines"][0]["queued_send_now_qualifications"]["unknown"] = PIN
    file.write_text(json.dumps(payload))
    machines = load_machines({"PENTACLE_MACHINES_FILE": str(file)})
    assert route_for(row(), "coordinator", {m.name: m for m in machines}) is None
