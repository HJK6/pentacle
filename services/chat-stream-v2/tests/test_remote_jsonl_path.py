from types import SimpleNamespace
from unittest.mock import patch

from launch import jsonl_path_for, local_machine, machine_from_config


def test_linux_peer_cwd_is_not_resolved_on_mac_coordinator():
    cfg = SimpleNamespace(name="linux-peer", cwd="/home/example/agent-workspace",
        claude_bin="/home/example/bin/claude", codex_bin="/home/example/bin/codex",
        tmux_bin="/usr/bin/tmux", projects_root="/home/example/.claude/projects",
        ssh_target="owned-linux-peer")
    machine = machine_from_config(cfg)
    with patch("launch.os.path.realpath", side_effect=AssertionError("local lookup of peer path")):
        result = jsonl_path_for(machine, cfg.cwd, "launch-uuid")
    assert result == "/home/example/.claude/projects/-home-example-agent-workspace/launch-uuid.jsonl"


def test_local_launch_keeps_host_realpath_resolution():
    machine = local_machine("local", cwd="/alias/work", projects_root="/owned/projects")
    with patch("launch.os.path.realpath", return_value="/canonical/work") as realpath:
        result = jsonl_path_for(machine, "/alias/work", "launch-uuid")
    realpath.assert_called_once_with("/alias/work")
    assert result == "/owned/projects/-canonical-work/launch-uuid.jsonl"
