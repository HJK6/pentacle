from __future__ import annotations

import base64
import json

import pytest

from agent_orch import cli
from agent_orch.config import Config

RULE = {
    "recurring_id": "11111111-1111-4111-8111-111111111111", "name": "Nightly retro",
    "recurrence_text": "daily at 02:00 America/Chicago", "state": "active", "tz": "America/Chicago",
    "next_fire_at_utc": "2026-10-21T07:00:00.000000Z", "next_fire_local": "2026-10-21T02:00:00-05:00",
    "missed_count": 0, "last_result": "missed", "last_result_reason": "late",
}


@pytest.fixture
def calls(monkeypatch, tmp_path):
    captured: list[dict] = []

    async def fake_schedule_once(_config, payload, timeout):
        captured.append(dict(payload))
        verb = payload["type"]
        if verb == "schedule.recurring.list":
            return {"type": f"{verb}.ok", "recurring": [RULE]}
        if verb == "schedule.recurring.get":
            return {"type": f"{verb}.ok", "recurring": {
                **RULE, "initial_prompt_b64": base64.b64encode(b"the prompt").decode()}}
        return {"type": f"{verb}.ok", "recurring": RULE}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:leader")
    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)
    return captured


def run(argv: list[str]) -> int:
    args = cli.build_parser().parse_args(["schedule", "recurring", *argv])
    return args.func(args)


ADD = [
    "add", "--name", "Nightly retro", "--every", "daily", "--at-time", "02:00", "--provider", "codex",
    "--model", "gpt-5.6-sol", "--effort", "high", "--spec-id", "spec_example__recurring",
    "--objective", "Nightly retro", "--initial-prompt", "Run the retro.",
]


def test_add_sends_the_resolved_template_with_default_parent_and_no_tz(calls, capsys):
    assert run([*ADD, "--visibility", "hidden"]) == 0
    payload = calls[-1]
    assert payload["type"] == "schedule.recurring.create"
    assert (payload["name"], payload["every"], payload["at_time"]) == ("Nightly retro", "daily", "02:00")
    assert "tz" not in payload  # the daemon defaults the zone
    assert payload["parent"] == "assistant" and payload["self_close_on_completion"] is True
    assert payload["spec_id"] == "spec_example__recurring"
    assert payload["initial_prompt"] == "Run the retro."
    assert (payload["requested_provider"], payload["requested_model"]) == ("codex", "gpt-5.6-sol")
    assert payload["from_stream_id"] == "hosta:leader" and payload["request_id"]
    assert json.loads(capsys.readouterr().out)["recurring"]["recurring_id"] == RULE["recurring_id"]


def test_add_parent_none_needs_no_objective_and_weekly_needs_days(calls, capsys):
    at = ADD.index("--objective")
    argv = ADD[:at] + ADD[at + 2:]
    assert run([*argv, "--parent", "none"]) == 0
    assert calls[-1]["parent"] == "none" and "self_close_on_completion" not in calls[-1]
    assert run([*argv, "--parent", "assistant"]) == 2  # parented children need an objective
    weekly = [("weekly" if value == "daily" else value) for value in ADD]
    assert run(weekly) == 2
    assert run([*weekly, "--days", "mon,wed", "--tz", "UTC"]) == 0
    assert calls[-1]["days"] == "mon,wed" and calls[-1]["tz"] == "UTC"
    capsys.readouterr()


def test_list_renders_zone_text_and_last_result(calls, capsys):
    assert run(["list"]) == 0
    out = capsys.readouterr().out
    assert "Nightly retro" in out and "daily at 02:00 America/Chicago" in out
    assert "2026-10-21T02:00:00-05:00" in out and "2026-10-21T07:00:00.000000Z" in out
    assert "missed:late" in out


def test_get_prints_json_then_the_prompt(calls, capsys):
    assert run(["get", RULE["recurring_id"]]) == 0
    out = capsys.readouterr().out
    assert "initial_prompt_b64" not in out and out.rstrip().endswith("the prompt")


def test_set_sends_only_provided_fields_and_simple_verbs_map_to_daemon_verbs(calls, capsys):
    assert run(["set", RULE["recurring_id"], "--at-time", "03:30", "--tz", "UTC"]) == 0
    payload = calls[-1]
    assert payload["type"] == "schedule.recurring.update" and payload["recurring_id"] == RULE["recurring_id"]
    assert payload["at_time"] == "03:30" and payload["tz"] == "UTC"
    assert not {"provider", "model", "parent", "initial_prompt", "spec_ids"} & set(payload)
    assert run(["set", RULE["recurring_id"], "--model", "gpt-5.6-luna", "--initial-prompt", "new"]) == 0
    assert calls[-1]["model"] == "gpt-5.6-luna" and calls[-1]["initial_prompt"] == "new"
    for verb, daemon in (("pause", "pause"), ("resume", "resume"), ("run", "run"), ("rm", "delete")):
        assert run([verb, RULE["recurring_id"]]) == 0
        assert calls[-1]["type"] == f"schedule.recurring.{daemon}"
        assert calls[-1]["recurring_id"] == RULE["recurring_id"] and calls[-1]["request_id"]
    capsys.readouterr()


def test_create_timeout_names_the_receipt_recovery(monkeypatch, tmp_path, capsys):
    async def timeout(_config, payload, timeout):
        raise TimeoutError("slow")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:leader")
    monkeypatch.setattr(cli, "schedule_once", timeout)
    assert run(ADD) == 67
    captured = capsys.readouterr()
    response = json.loads(captured.out)
    assert response["recovery"]["measured_receipt_phases"] == ["row_committed"]
    assert "row_committed" in captured.err
