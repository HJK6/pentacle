from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_orch import cli
from agent_orch.config import Config


def _spawn_args(**overrides):
    data = {"objective": "Exercise the existing spawn contract",
        "provider": "codex",
        "model": None,
        "effort": None,
        "host": None,
        "role": None,
        "phase": None,
        "spec_id": None,
        "visibility": None,
        "parent": None,
        "handoff": True,
        "confirm_model_change": False,
        "disposition_waived_reason": None,
        "resume": None,
        "top_level": False,
        "at": None,
        "delay": None,
        "allow_past_time": False,
        "allow_far_future": False,
        "reparent_children": None,
        "self_close_on_completion": None,
        "initial_prompt": "scheduled prompt",
        "initial_prompt_file": None,
        "timeout": 1.0,
        "request_id": None,
        "idempotency_key": None,
    }
    data.update(overrides)
    return Namespace(**data)


def _install_spawn_fakes(monkeypatch, tmp_path: Path, captured: dict, response: dict | None = None) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:codex-leader")
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "codex",
            "effective_model": "gpt-5.6-sol",
            "effective_effort": "high",
        },
    )

    async def fake_schedule_once(_config, payload, timeout):
        captured["payload"] = payload
        captured["timeout"] = timeout
        return dict(
            response
            or {
                "type": "schedule.insert.ok",
                "schedule_id": "sch-1",
                "fires_at_utc": payload["fires_at_utc"],
                "target_host": "hosta",
            }
        )

    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)


def test_spawn_handoff_at_inserts_schedule_without_spawning(monkeypatch, tmp_path, capsys):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)
    fixed = datetime(2026, 5, 20, 20, 0, 0, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", FrozenDateTime)

    rc = cli.spawn(_spawn_args(at="2026-05-20T18:30:00-05:00"))

    assert rc == 0
    assert captured["payload"]["type"] == "schedule.insert"
    assert captured["payload"]["fires_at_utc"] == "2026-05-20T23:30:00Z"
    assert captured["payload"]["created_by_stream_id"] == "hosta:codex-leader"
    assert captured["payload"]["handoff_from_stream_id"] == "hosta:codex-leader"
    assert captured["payload"]["handoff"] is True
    assert "requested_model" in captured["payload"] and captured["payload"]["requested_model"] is None
    assert captured["payload"]["visibility"] == "default"
    assert "parent_stream_id" not in captured["payload"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["schedule_id"] == "sch-1"


def test_spawn_handoff_at_carries_explicit_spec_ids(monkeypatch, tmp_path, capsys):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)

    rc = cli.spawn(
        _spawn_args(
            at="2026-05-20T18:30:00-05:00",
            allow_past_time=True,
            spec_id=["example__one", "example__two"],
        )
    )

    assert rc == 0
    assert captured["payload"]["type"] == "schedule.insert"
    assert captured["payload"]["spec_id"] == "example__one"
    assert captured["payload"]["spec_ids"] == ["example__one", "example__two"]
    json.loads(capsys.readouterr().out)


def test_spawn_handoff_at_carries_claude_model(monkeypatch, tmp_path, capsys):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "claude",
            "effective_model": "claude-opus-4-8",
            "effective_effort": "high",
        },
    )

    rc = cli.spawn(_spawn_args(provider="claude", model="opus", at="2026-05-20T18:30:00-05:00", allow_past_time=True))

    assert rc == 0
    assert captured["payload"]["type"] == "schedule.insert"
    assert captured["payload"]["model"] == "claude-opus-4-8"
    assert captured["payload"]["effort"] == "high"
    json.loads(capsys.readouterr().out)


def test_spawn_handoff_delay_resolves_once_at_submit_time(monkeypatch, tmp_path, capsys):
    fixed = datetime(2026, 5, 20, 20, 0, 0, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(cli, "datetime", FrozenDateTime)

    assert cli.spawn(_spawn_args(delay="3h")) == 0
    first = captured["payload"]["fires_at_utc"]
    capsys.readouterr()
    assert cli.spawn(_spawn_args(delay="3h")) == 0
    second = captured["payload"]["fires_at_utc"]

    assert first == "2026-05-20T23:00:00Z"
    assert second == first


def test_spawn_handoff_at_long_inline_prompt_uploads_prompt_blob(monkeypatch, tmp_path, capsys):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)
    fixed = datetime(2026, 5, 20, 20, 0, 0, tzinfo=timezone.utc)
    prompt = "x" * (cli.INITIAL_PROMPT_INLINE_CAP_BYTES + 1)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    async def fake_upload_prompt_blob_once(config, data, timeout):
        captured["upload"] = {"config": config, "data": data, "timeout": timeout}
        return {"type": "upload_prompt_blob.ok", "prompt_blob_sha": "sha-inline-prompt", "size_bytes": len(data)}

    monkeypatch.setattr(cli, "datetime", FrozenDateTime)
    monkeypatch.setattr(cli, "upload_prompt_blob_once", fake_upload_prompt_blob_once)

    rc = cli.spawn(_spawn_args(at="2026-05-20T18:30:00-05:00", initial_prompt=prompt))

    assert rc == 0
    assert captured["upload"]["data"] == prompt.encode("utf-8")
    assert captured["payload"]["type"] == "schedule.insert"
    assert captured["payload"]["initial_prompt_blob_sha"] == "sha-inline-prompt"
    assert "initial_prompt" not in captured["payload"]
    assert json.loads(capsys.readouterr().out)["schedule_id"] == "sch-1"


@pytest.mark.parametrize(
    ("overrides", "expected_parent"),
    [
        ({}, "hosta:codex-leader"),
        ({"parent": "hosta:explicit-parent"}, "hosta:explicit-parent"),
        ({"top_level": True}, None),
    ],
)
def test_fresh_spawn_at_creates_all_lineage_shapes(
    monkeypatch, tmp_path, capsys, overrides, expected_parent,
):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)

    rc = cli.spawn(_spawn_args(
        handoff=False,
        at="2026-05-20T18:30:00-05:00",
        allow_past_time=True,
        **overrides,
    ))

    assert rc == 0
    payload = captured["payload"]
    assert payload["created_by_stream_id"] == "hosta:codex-leader"
    assert payload["from_stream_id"] == "hosta:codex-leader"
    assert payload.get("parent_stream_id") == expected_parent
    assert payload["handoff"] is False
    json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"resume": "session-1"}, "resume_not_schedulable"),
        ({"confirm_model_change": True}, "handoff_only_flag"),
        ({"idempotency_key": "caller-key"}, "idempotency_key_not_schedulable"),
        (
            {"top_level": True, "self_close_on_completion": True},
            "self_close_requires_parent",
        ),
        (
            {"top_level": True, "self_close_on_completion": False},
            "self_close_requires_parent",
        ),
    ],
)
def test_fresh_schedule_rejections_have_fixed_codes(
    monkeypatch, tmp_path, capsys, overrides, error_code,
):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)
    rc = cli.spawn(_spawn_args(
        handoff=False, delay="2m", initial_prompt=None, **overrides,
    ))
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == error_code
    assert "payload" not in captured


def test_schedule_validation_precedes_prompt_blob_upload(monkeypatch, tmp_path, capsys):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)
    uploads: list[bytes] = []

    async def upload(_config, data, timeout):
        uploads.append(data)
        return {"type": "upload_prompt_blob.ok", "prompt_blob_sha": "f" * 64}

    monkeypatch.setattr(cli, "upload_prompt_blob_once", upload)
    timing = {"at": "not-a-time"}
    rc = cli.spawn(_spawn_args(
        handoff=False,
        initial_prompt="x" * (cli.INITIAL_PROMPT_INLINE_CAP_BYTES + 1),
        **timing,
    ))
    assert rc == 2
    assert uploads == []
    assert "payload" not in captured
    capsys.readouterr()


def test_spawn_parser_fresh_schedule_matrix_is_total_over_live_actions():
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions
        if isinstance(action, cli.argparse._SubParsersAction)
    )
    spawn_parser = subparsers.choices["spawn"]
    surfaces = [spawn_parser, *spawn_parser._mutually_exclusive_groups]
    live_flags = {
        flag
        for surface in surfaces
        for action in surface._actions
        for flag in action.option_strings
        if flag.startswith("--") and flag != "--help"
    }
    groups = {
        "branch": {"--handoff"},
        "serialized": {
            "--objective", "--provider", "--model", "--effort", "--host", "--role", "--phase",
            "--spec-id", "--visibility", "--disposition-waived",
            "--no-reparent-children", "--parent", "--top-level",
            "--self-close-on-completion", "--no-self-close-on-completion",
            "--no-watch",
            "--initial-prompt", "--initial-prompt-file",
        },
        "timing": {"--at", "--delay"},
        "client_only": {"--timeout", "--allow-past-time", "--allow-far-future"},
        "request_identity": {"--request-id"},
        "rejected": {"--resume", "--confirm-model-change", "--idempotency-key"},
    }
    classified = [flag for values in groups.values() for flag in values]
    assert set(classified) == live_flags
    assert len(classified) == len(set(classified))


def test_spawn_at_and_delay_rejected(monkeypatch, tmp_path, capsys):
    captured: dict = {}
    _install_spawn_fakes(monkeypatch, tmp_path, captured)

    rc = cli.spawn(_spawn_args(at="2026-05-20T18:30:00-05:00", delay="3h"))

    assert rc == 2
    assert "--at and --delay are mutually exclusive" in capsys.readouterr().err
    assert "payload" not in captured


def test_submit_time_bounds_and_overrides():
    now = datetime(2026, 5, 20, 20, 0, 0, tzinfo=timezone.utc)

    near = _spawn_args(at="2026-05-20T20:01:00+00:00")
    try:
        cli._resolve_schedule_fire_time(near, now=now)
    except ValueError as exc:
        assert "now + 60s" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("near-past bound accepted")

    far = _spawn_args(at="2027-05-21T20:00:01+00:00")
    try:
        cli._resolve_schedule_fire_time(far, now=now)
    except ValueError as exc:
        assert "now + 365d" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("far-future bound accepted")

    assert cli._resolve_schedule_fire_time(_spawn_args(at="2026-05-20T20:01:00+00:00", allow_past_time=True), now=now) == "2026-05-20T20:01:00Z"
    assert cli._resolve_schedule_fire_time(_spawn_args(at="2027-05-21T20:00:01+00:00", allow_far_future=True), now=now) == "2027-05-21T20:00:01Z"


def test_schedule_list_json_renders_all_states(monkeypatch, tmp_path, capsys):
    rows = [
        {"schedule_id": f"sch-{state}", "state": state, "fires_at_utc": "2026-05-21T00:00:00Z"}
        for state in cli.SCHEDULE_STATES
    ]
    async def fake_schedule_once(_config, payload, timeout):
        return {"type": "schedule.list.ok", "schedules": rows}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)

    rc = cli.schedule_list(Namespace(json=True, state=None, timeout=30.0))

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert [row["state"] for row in printed["schedules"]] == list(cli.SCHEDULE_STATES)


def test_schedule_list_human_prints_full_schedule_id(monkeypatch, tmp_path, capsys):
    full_id = "12345678-1234-5678-9abc-123456789abc"
    async def fake_schedule_once(_config, payload, timeout):
        return {
            "type": "schedule.list.ok",
            "schedules": [
                {
                    "schedule_id": full_id,
                    "state": "pending",
                    "fires_at_utc": "2026-05-21T00:00:00Z",
                    "target_host": "hosta",
                    "provider": "codex",
                    "prompt_preview": "finish stage",
                }
            ],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)

    rc = cli.schedule_list(Namespace(json=False, state=None, timeout=30.0))

    assert rc == 0
    output = capsys.readouterr().out
    assert full_id in output
    assert "12345678 " not in output


def test_schedule_get_prints_inline_and_blob_discriminators(monkeypatch, tmp_path, capsys):
    responses = iter(
        [
            {"type": "schedule.get.ok", "schedule": {"schedule_id": "inline", "state": "pending", "prompt_storage": "inline", "initial_prompt_b64": "aW5saW5l"}},
            {"type": "schedule.get.ok", "schedule": {"schedule_id": "blob", "state": "pending", "prompt_storage": "blob", "initial_prompt_b64": "YmxvYg=="}},
        ]
    )
    async def fake_schedule_once(_config, payload, timeout):
        return next(responses)

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)

    assert cli.schedule_get(Namespace(json=True, schedule_id="inline", timeout=30.0)) == 0
    assert cli.schedule_get(Namespace(json=True, schedule_id="blob", timeout=30.0)) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[0]["schedule"]["prompt_storage"] == "inline"
    assert lines[1]["schedule"]["prompt_storage"] == "blob"


def test_schedule_cancel_and_run_terminal_errors_return_nonzero(monkeypatch, tmp_path, capsys):
    async def fake_schedule_once(_config, payload, timeout):
        return {"type": "schedule.error", "error": "schedule_terminal_state", "state": "fired"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)

    assert cli.schedule_cancel(Namespace(schedule_id="sch", timeout=30.0)) == 1
    assert cli.schedule_run(Namespace(schedule_id="sch", timeout=30.0)) == 1
    output = capsys.readouterr().out
    assert output.count("schedule_terminal_state") == 2


def test_schedule_mutations_and_receipt_carry_actor_and_request_ids(monkeypatch, tmp_path, capsys):
    calls: list[dict] = []

    async def fake_schedule_once(_config, payload, timeout):
        calls.append(dict(payload))
        return {"type": f"{payload['type']}.ok"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "schedule_once", fake_schedule_once)
    request_id = "00000000-0000-4000-8000-000000000044"
    cancel = cli.build_parser().parse_args([
        "schedule", "cancel", "sched-1", "--from", "hosta:owner", "--request-id", request_id,
    ])
    assert cancel.func(cancel) == 0
    assert calls[-1]["from_stream_id"] == "hosta:owner"
    assert calls[-1]["request_id"] == request_id

    receipt = cli.build_parser().parse_args([
        "schedule", "receipt", request_id, "--phase", "terminal", "--from", "hosta:owner",
    ])
    assert receipt.func(receipt) == 0
    assert calls[-1]["operation_request_id"] == request_id
    assert calls[-1]["phase"] == "terminal"
    capsys.readouterr()


@pytest.mark.parametrize(
    ("argv", "expected_phases"),
    [
        (
            [
                "spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex", "--handoff", "--delay", "2m",
                "--request-id", "00000000-0000-4000-8000-000000000101",
            ],
            ["row_committed"],
        ),
        (
            [
                "schedule", "cancel", "sched-1", "--from", "hosta:owner",
                "--request-id", "00000000-0000-4000-8000-000000000102",
            ],
            ["terminal"],
        ),
        (
            [
                "schedule", "reschedule", "sched-1", "--delay", "2m",
                "--from", "hosta:owner", "--request-id",
                "00000000-0000-4000-8000-000000000103",
            ],
            ["row_committed"],
        ),
        (
            [
                "schedule", "run", "sched-1", "--from", "hosta:owner",
                "--request-id", "00000000-0000-4000-8000-000000000104",
            ],
            ["spawn_delivered", "terminal"],
        ),
    ],
)
@pytest.mark.parametrize("transport_error", [TimeoutError("timed out"), RuntimeError("link dropped")])
def test_public_schedule_mutations_emit_reachable_measured_receipt_recovery(
    monkeypatch, tmp_path, capsys, argv, expected_phases, transport_error,
):
    captured: dict = {}

    async def fail_after_send(_config, payload, timeout):
        captured["payload"] = dict(payload)
        raise transport_error

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hosta:owner")
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "codex", "effective_model": "gpt-5.6-sol", "effective_effort": "high",
        },
    )
    monkeypatch.setattr(cli, "schedule_once", fail_after_send)

    parsed = cli.build_parser().parse_args(argv)
    assert parsed.func(parsed) in {65, 67}

    request_id = captured["payload"]["request_id"]
    response = json.loads(capsys.readouterr().out)
    assert response["request_id"] == request_id
    assert response["recovery"] == {
        "surface": "schedule",
        "operation_request_id": request_id,
        "measured_receipt_phases": expected_phases,
        "receipt_commands": [
            f"agent-orch schedule receipt {request_id} --phase {phase}"
            for phase in expected_phases
        ],
    }


def test_public_schedule_read_omits_unusable_receipt_guidance(monkeypatch, tmp_path, capsys):
    async def fail_read(_config, _payload, timeout):
        raise RuntimeError("link dropped")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "", "hosta", tmp_path))
    monkeypatch.setattr(cli, "schedule_once", fail_read)

    parsed = cli.build_parser().parse_args(["schedule", "list", "--json"])
    assert parsed.func(parsed) == 65
    captured = capsys.readouterr()
    assert "recovery" not in json.loads(captured.out)
    assert "receipt" not in captured.err
    assert "<request-id>" not in captured.err

