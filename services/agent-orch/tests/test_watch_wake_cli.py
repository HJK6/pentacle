"""Payload tests for wake and watch coordination commands."""
import pytest

from agent_orch import cli
from agent_orch.wsclient import RPC_RETRY_ELIGIBLE_TYPES


@pytest.mark.parametrize("argv,expected", [
    (["wake", "--in", "2m", "--note", "resume", "--urgent"],
     {"type": "wake.register", "in": "2m", "note": "resume", "urgent": True}),
    (["wake", "--at", "2099-01-01T01:00:00+01:00"],
     {"type": "wake.register", "at": "2099-01-01T01:00:00+01:00"}),
    (["wake", "list"], {"type": "wake.list"}),
    (["wake", "cancel", "owned"], {"type": "wake.cancel", "id": "owned"}),
    (["watch", "hosta:child", "--on", "idle,end,quiet=5", "--repeat"],
     {"type": "watch.register", "child_stream_id": "hosta:child", "on": "idle,end,quiet=5", "repeat": True}),
    (["watch", "list"], {"type": "watch.list"}),
    (["watch", "cancel", "owned"], {"type": "watch.cancel", "id": "owned"}),
])
def test_payload_and_stable_request_id(monkeypatch, argv, expected):
    seen = []
    monkeypatch.setattr(cli, "_coordination_request", lambda args, payload: seen.append(payload) or 0)
    args = cli.build_parser().parse_args([*argv, "--request-id", "retry"])
    assert args.func(args) == 0
    assert {k: seen[0][k] for k in expected} == expected
    assert seen[0]["request_id"] == "retry"
    assert expected["type"] in RPC_RETRY_ELIGIBLE_TYPES


def test_spawn_opt_out_parser():
    assert cli.build_parser().parse_args(["spawn", "--no-watch"]).no_watch is True
    assert cli.build_parser().parse_args(["spawn"]).no_watch is False
