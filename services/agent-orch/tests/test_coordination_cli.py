from __future__ import annotations

import json
from agent_orch import cli
from agent_orch.config import Config


def test_hold_acquire_cli_sends_coordination_rpc(monkeypatch, capsys, tmp_path):
    calls: list[dict] = []

    async def fake_coordination_once(config, payload, timeout):
        calls.append({"config": config, "payload": payload, "timeout": timeout})
        return {"type": "coordination.hold.acquire.ok", "request_id": "hold", "hold": {"id": "hold-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-owner")
    monkeypatch.setattr(cli, "coordination_once", fake_coordination_once)

    args = cli.build_parser().parse_args(
        ["hold", "acquire", "daemon:com.pentacle.chat-streamd", "--reason", "freeze", "--ttl", "60"]
    )

    assert args.func(args) == 0
    payload = calls[0]["payload"]
    assert payload["type"] == "coordination.hold.acquire"
    assert payload["resource"] == "daemon:com.pentacle.chat-streamd"
    assert payload["reason"] == "freeze"
    assert payload["from_stream_id"] == "hosta:codex-owner"
    assert "expires_at" in payload
    assert json.loads(capsys.readouterr().out)["type"] == "coordination.hold.acquire.ok"


def test_hold_release_cli_uses_a_listed_hold_id(monkeypatch, capsys, tmp_path):
    calls: list[dict] = []

    async def fake_coordination_once(config, payload, timeout):
        calls.append(payload)
        return {"type": "coordination.hold.release.ok", "request_id": "release", "hold": {"id": "hold-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-owner")
    monkeypatch.setattr(cli, "coordination_once", fake_coordination_once)

    hold_id = "hold-" + "a" * 32
    args = cli.build_parser().parse_args(["hold", "release", hold_id])
    assert args.func(args) == 0
    assert calls[0]["hold_id"] == hold_id
    assert "resource" not in calls[0]
    assert json.loads(capsys.readouterr().out)["type"] == "coordination.hold.release.ok"


def test_hold_release_cli_keeps_hold_prefixed_resource(monkeypatch, capsys, tmp_path):
    calls: list[dict] = []

    async def fake_coordination_once(config, payload, timeout):
        calls.append(payload)
        return {"type": "coordination.hold.release.ok", "request_id": "release"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "coordination_once", fake_coordination_once)
    args = cli.build_parser().parse_args(["hold", "release", "hold-maintenance"])
    args.from_stream_id = "hosta:codex-test"
    assert args.func(args) == 0
    assert calls[0]["resource"] == "hold-maintenance"
    assert "hold_id" not in calls[0]
    assert json.loads(capsys.readouterr().out)["type"] == "coordination.hold.release.ok"


def test_oblige_cli_sends_expires_at(monkeypatch, capsys, tmp_path):
    calls: list[dict] = []

    async def fake_coordination_once(config, payload, timeout):
        calls.append({"payload": payload, "timeout": timeout})
        return {"type": "coordination.obligation.create.ok", "request_id": "obl", "obligation": {"id": "obl-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-owner")
    monkeypatch.setattr(cli, "coordination_once", fake_coordination_once)

    args = cli.build_parser().parse_args(
        [
            "oblige",
            "hosta:target",
            "fix frontmatter in work/in_progress/repo__topic/spec.md",
            "--spec-id",
            "repo__topic",
            "--expires-in",
            "3600",
            "--from",
            "hosta:memory-cadence",
            "--timeout",
            "20",
        ]
    )

    assert args.func(args) == 0
    payload = calls[0]["payload"]
    assert payload["type"] == "coordination.obligation.create"
    assert payload["target_stream"] == "hosta:target"
    assert payload["spec_id"] == "repo__topic"
    assert payload["from_stream_id"] == "hosta:memory-cadence"
    assert "expires_at" in payload
    # --timeout stays the RPC timeout, not the obligation deadline.
    assert calls[0]["timeout"] == 20.0
    assert json.loads(capsys.readouterr().out)["type"] == "coordination.obligation.create.ok"


def test_oblige_cli_without_expires_in_omits_expires_at(monkeypatch, capsys, tmp_path):
    calls: list[dict] = []

    async def fake_coordination_once(config, payload, timeout):
        calls.append({"payload": payload})
        return {"type": "coordination.obligation.create.ok", "request_id": "obl", "obligation": {"id": "obl-1"}}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-owner")
    monkeypatch.setattr(cli, "coordination_once", fake_coordination_once)

    args = cli.build_parser().parse_args(["oblige", "hosta:target", "do the thing"])
    assert args.func(args) == 0
    assert "expires_at" not in calls[0]["payload"]
    capsys.readouterr()


def test_spec_issue_cli_list_and_clear_payloads(monkeypatch, capsys, tmp_path):
    calls: list[dict] = []

    async def fake_coordination_once(config, payload, timeout):
        calls.append({"payload": payload})
        rpc = str(payload["type"])
        if rpc.endswith("list"):
            return {"type": "coordination.spec_issue.list.ok", "request_id": "l", "spec_issues": []}
        return {"type": "coordination.spec_issue.clear.ok", "request_id": "c", "cleared": True}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-owner")
    monkeypatch.setattr(cli, "coordination_once", fake_coordination_once)

    listed = cli.build_parser().parse_args(["spec-issue", "list", "--stream", "hosta:target"])
    assert listed.func(listed) == 0
    assert calls[0]["payload"]["type"] == "coordination.spec_issue.list"
    assert calls[0]["payload"]["stream"] == "hosta:target"

    cleared = cli.build_parser().parse_args(
        ["spec-issue", "clear", "--stream", "hosta:target", "--spec-id", "repo__topic", "--from", "hosta:memory-cadence"]
    )
    assert cleared.func(cleared) == 0
    payload = calls[1]["payload"]
    assert payload["type"] == "coordination.spec_issue.clear"
    assert payload["stream"] == "hosta:target"
    assert payload["spec_id"] == "repo__topic"
    assert payload["from_stream_id"] == "hosta:memory-cadence"
    capsys.readouterr()


def test_report_cli_sends_discharges(monkeypatch, tmp_path):
    calls: dict[str, dict] = {}

    async def fake_report_once(config, request, timeout):
        calls["request"] = request
        return {"type": "report.ok", "request_id": "report-1", "report_id": "report-1", "ledger_row_id": 1}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-owner")
    monkeypatch.setattr(cli, "report_once", fake_report_once)

    args = cli.build_parser().parse_args(
        [
            "report",
            "--status",
            "done",
            "--result",
            json.dumps({"summary": "ok", "findings": [], "next_action": "nexus_verify"}),
            "--discharges",
            "obl-1",
            "--discharges",
            "obl-2",
        ]
    )

    assert args.func(args) == 0
    assert calls["request"]["discharges"] == ["obl-1", "obl-2"]
