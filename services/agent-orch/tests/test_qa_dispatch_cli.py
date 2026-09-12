"""CLI serialization into the daemon-owned QA admission contract."""

import pytest
from agent_orch import cli
from agent_orch.config import Config
from test_cli_spawn import _install_spawn_fakes
from test_cli_send_auto_inbox import _install_send_fakes

SPEC = "spec_pentacle__qa_counter_test"
QA = ["--qa-spec-id", SPEC, "--qa-surface", "admission", "--qa-cycle", "1"]


@pytest.mark.parametrize(
    "verb,extra,fields",
    [
        (
            "adjudicate",
            ["report-1", "--cycle", "1", "--valid", "--reason", "in scope"],
            {"report_id": "report-1", "adjudicated_valid": True},
        ),
        (
            "adjudicate",
            ["report-1", "--cycle", "1", "--void", "--reason", "withdrawn"],
            {"adjudicated_valid": False},
        ),
        (
            "diagnose",
            [
                "--cycle",
                "1",
                "--diagnosis-id",
                "pivot-1",
                "--diagnosis",
                "cause",
                "--pivot",
                "new method",
            ],
            {"diagnosis_id": "pivot-1", "pivot": "new method"},
        ),
        ("show", [], {}),
    ],
)
def test_spec_issue_wire(monkeypatch, tmp_path, verb, extra, fields):
    calls = []

    async def rpc(config, payload, timeout):
        calls.append(payload)
        return {"type": payload["type"] + ".ok"}

    monkeypatch.setattr(
        cli, "load_config", lambda: Config("ws://unused", "", "localhost", tmp_path)
    )
    monkeypatch.setattr(
        cli, "discover_leader_stream_id_short", lambda config: "localhost:lead"
    )
    monkeypatch.setattr(cli, "coordination_once", rpc)
    args = cli.build_parser().parse_args(
        ["spec-issue", verb, "--spec-id", SPEC, "--surface", "admission", *extra]
    )
    assert args.func(args) == 0
    assert calls[0]["type"] == "coordination.spec_issue." + verb
    assert calls[0]["spec_id"] == SPEC and calls[0]["surface"] == "admission"
    assert all(calls[0][k] == v for k, v in fields.items())


def test_qa_send_fields_reach_wire(monkeypatch):
    captured = {}
    _install_send_fakes(monkeypatch, captured)
    args = cli.build_parser().parse_args(
        ["send", "bart:qa", "7", "Review surface", *QA]
    )
    assert args.func(args) == 0
    assert {
        k: captured["request"][k] for k in ("qa_spec_id", "qa_surface", "qa_cycle")
    } == {"qa_spec_id": SPEC, "qa_surface": "admission", "qa_cycle": 1}


@pytest.mark.parametrize("scheduled", [False, True])
def test_qa_spawn_and_schedule_fields_reach_wire(monkeypatch, tmp_path, scheduled):
    _install_spawn_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli, "discover_leader_stream_id_short", lambda config: "merlin:lead"
    )
    calls = []

    async def rpc(config, payload, timeout):
        calls.append(payload)
        return (
            {"type": "schedule.insert.ok", "schedule": {"schedule_id": "sched-test"}}
            if scheduled
            else {"type": "spawn.ok", "session": {"stream_id": "merlin:child"}}
        )

    monkeypatch.setattr(cli, "schedule_once" if scheduled else "spawn_once", rpc)
    args = cli.build_parser().parse_args(
        [
            "spawn",
            "--provider",
            "codex",
            "--role",
            "qa",
            "--parent",
            "merlin:lead",
            "--objective",
            "Review surface",
            *QA,
            *(["--delay", "10m"] if scheduled else []),
        ]
    )
    assert args.func(args) == 0
    assert calls[0]["qa_spec_id"] == SPEC
    assert calls[0]["qa_surface"] == "admission" and calls[0]["qa_cycle"] == 1
