from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_orch import cli
from agent_orch import schema
from agent_orch.config import Config


def _cli_subprocess_env() -> dict[str, str]:
    """Env forcing a child interpreter to import the same `agent_orch` this test imported.

    A bare `subprocess.run([sys.executable, "-c", "from agent_orch.cli import main"])`
    inherits sys.path from site-packages only, never the conftest path inserts. It therefore
    resolves `agent_orch` from whatever editable install happens to exist on the host — a
    different working tree than the one under test. That made the two spawn `--help` tests
    red on any host without the install (carried for weeks as "pre-existing on main") and
    falsely green everywhere else, because they were exercising the ambient checkout rather
    than the tree being validated. See `spec_example__event_handling`.
    """
    package_root = Path(cli.__file__).resolve().parents[1]
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{package_root}{os.pathsep}{existing}" if existing else str(package_root)
    return env


def _spawn_args(tmp_path: Path, role: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        objective="Exercise the existing spawn contract", provider="codex",
        model=None,
        effort=None,
        host=None,
        role=role,
        phase="code_dev",
        spec_id=None,
        visibility="hidden",
        parent="hostc:claude-leader",
        handoff=False,
        disposition_waived_reason=None,
        initial_prompt=None,
        initial_prompt_file=None,
        timeout=1.0,
        self_close_on_completion=True,
    )


def _config(tmp_path: Path, memory_repo_path: Path | None = None) -> Config:
    return Config(
        ws_url="ws://unused",
        token="",
        host_id="hostc",
        runtime_dir=tmp_path / "runtime",
        memory_repo_path=memory_repo_path,
    )


def _install_spawn_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    memory_repo_path: Path | None = None,
    response: dict[str, object] | None = None,
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo_path))
    async def fake_spawn_once(_config, payload, timeout):
        return dict(response or {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}})

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)


def _printed_json(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def _expected_schemas() -> dict[str, object]:
    return {
        "inbox_v1": {
            "required_fields": list(schema.REQUIRED_INBOX_FIELDS),
        },
    }


def test_spawn_success_ack_includes_schemas_from_schema_constants(monkeypatch, tmp_path: Path, capsys) -> None:
    _install_spawn_fakes(monkeypatch, tmp_path)

    result = cli.spawn(_spawn_args(tmp_path))
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["schemas"] == _expected_schemas()


def test_default_spawn_rpc_timeout_covers_daemon_proof_budget(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    captured: dict[str, float] = {}

    async def fake_spawn_once(_config, _payload, timeout):
        captured["timeout"] = timeout
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path)
    args.timeout = None

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured["timeout"] == cli.SPAWN_RPC_TIMEOUT_DEFAULT_S == 185.0


@pytest.mark.parametrize("handoff", [False, True])
def test_spawn_and_handoff_without_manifest_need_no_target_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys, handoff: bool,
) -> None:
    captured: list[dict[str, object]] = []

    async def fake_spawn_once(_config, payload, timeout):
        captured.append(dict(payload))
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path)
    args.handoff = handoff
    if handoff:
        args.parent = None
        monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostc:leader")
        monkeypatch.setattr(
            cli,
            "_handoff_source_row",
            lambda *_args, **_kwargs: {
                "provider": "codex",
                "effective_model": "gpt-5.6-luna",
                "effective_effort": "max",
                "role": "worker",
            },
        )

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured[0].get("target_sha") is None
    assert captured[0].get("agent_orch_attestation") is None

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex", "--target-sha", "f" * 40])


def test_spawn_help_lists_spec_id_flag() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_orch.cli import main; raise SystemExit(main(['spawn', '--help']))",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_subprocess_env(),
    )

    assert result.returncode == 0, result.stderr
    assert "--spec-id" in result.stdout


def test_spawn_parser_rejects_malformed_spec_id_values() -> None:
    parser = cli.build_parser()

    good = parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex", "--spec-id", "example__dashboard"])
    assert good.spec_id == ["example__dashboard"]

    for value in ["", " ", ".hidden", "..", "a/b", "a\\b", "a__b__c", "a__", "__b", "foo", "a\x00b"]:
        with pytest.raises(SystemExit):
            parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex", "--spec-id", value])


def test_spawn_payload_includes_spec_id(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.spec_id = "example__dashboard"

    result = cli.spawn(args)
    printed = _printed_json(capsys)

    assert result == 0
    assert captured["payload"]["spec_id"] == "example__dashboard"
    assert captured["payload"]["objective_supported"] is True
    assert printed["schemas"] == _expected_schemas()


def test_spawn_payload_includes_repeated_spec_ids(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.spec_id = ["example__one", "example__two"]

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured["payload"]["spec_id"] == "example__one"
    assert captured["payload"]["spec_ids"] == ["example__one", "example__two"]


def test_spawn_payload_includes_claude_model(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:claude-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.provider = "claude"
    args.model = "opus"

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured["payload"]["model"] == "claude-opus-4-8"
    assert captured["payload"]["effort"] == "high"
    assert captured["payload"]["resolution_source"] == "explicit_override"


def test_spawn_payload_omits_model_when_unset(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)

    assert cli.spawn(_spawn_args(tmp_path)) == 0
    _printed_json(capsys)
    assert captured["payload"]["model"] == "gpt-5.6-sol"
    assert captured["payload"]["effort"] == "high"
    assert captured["payload"]["resolution_source"] == "profile_default"


def test_spawn_payload_includes_codex_model_and_effort(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda *_args, **_kwargs: {"agent_orch_daemon_capabilities": {"flags": ["codex_model_override"]}})
    args = _spawn_args(tmp_path)
    args.provider = "codex"
    args.model = "gpt-5.6-sol"
    args.effort = "high"

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured["payload"]["model"] == "gpt-5.6-sol"
    assert captured["payload"]["effort"] == "high"


def test_codex_model_override_is_resolved_client_side(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    captured: dict[str, object] = {}
    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}
    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.model = "gpt-5.6-sol"

    assert cli.spawn(args) == 0
    capsys.readouterr()
    assert captured["payload"]["model"] == "gpt-5.6-sol"


def test_spawn_rejects_model_for_unsupported_provider(tmp_path: Path, capsys) -> None:
    args = _spawn_args(tmp_path)
    args.provider = "other"
    args.model = "model"

    assert cli.spawn(args) == 2
    captured = capsys.readouterr()
    assert "--model requires --provider claude or codex" in captured.err


def test_spawn_payload_includes_generated_idempotency_key(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    ids = iter(
        [
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
            uuid.UUID("22222222-2222-2222-2222-222222222222"),
        ]
    )
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: next(ids))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)

    result = cli.spawn(_spawn_args(tmp_path))
    _printed_json(capsys)

    assert result == 0
    # request_id stays a per-attempt uuid, but the default idempotency_key is a
    # deterministic sha256[:32] of the logical spawn inputs (NOT the volatile
    # request_id) so a cross-process retry of the same logical spawn reuses the
    # same key and the daemon dedups it instead of minting a duplicate.
    assert captured["payload"]["request_id"] == "spawn-11111111-1111-1111-1111-111111111111"
    key = captured["payload"]["idempotency_key"]
    assert key != captured["payload"]["request_id"]
    assert len(key) == 32 and all(c in "0123456789abcdef" for c in key)


@pytest.mark.parametrize("request_id", ["qa-fixed-request", None, ""])
def test_spawn_request_identity_survives_reinvocation(monkeypatch, tmp_path: Path, capsys, request_id) -> None:
    captured = []
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured.append(payload.copy())
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    for _ in range(2):
        args = _spawn_args(tmp_path)
        argv = ["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex"]
        if request_id is not None:
            argv.extend(["--request-id", request_id])
        args.request_id = cli.build_parser().parse_args(argv).request_id
        assert cli.spawn(args) == 0
        _printed_json(capsys)

    if request_id:
        # An explicit --request-id is the caller's stable handle: it is both the
        # request_id AND the default idempotency_key across reinvocation.
        assert [payload["request_id"] for payload in captured] == [request_id, request_id]
        for payload in captured:
            assert payload["idempotency_key"] == request_id
    else:
        # No --request-id: request_id is a fresh per-attempt uuid (differs), but
        # the default idempotency_key is the deterministic inputs hash and is
        # IDENTICAL across reinvocation -- this is the retry-dedup fix.
        assert all(payload["request_id"].startswith("spawn-") for payload in captured)
        assert captured[0]["request_id"] != captured[1]["request_id"]
        assert captured[0]["idempotency_key"] == captured[1]["idempotency_key"]
        assert captured[0]["idempotency_key"] != captured[0]["request_id"]


def test_spawn_default_key_printed_to_stderr_before_rpc(monkeypatch, tmp_path: Path, capsys) -> None:
    """The derived key is printed as `spawn key: <key>` on stderr BEFORE the RPC,
    so a caller interrupted before the reply can retry/cancel/status by key."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        # Key must already be on stderr by the time the RPC is invoked.
        assert f"spawn key: {payload['idempotency_key']}" in capsys.readouterr().err
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    assert cli.spawn(_spawn_args(tmp_path)) == 0


def test_spawn_default_key_changes_when_brief_changes(monkeypatch, tmp_path: Path, capsys) -> None:
    """Different logical spawns (distinct briefs) derive distinct default keys."""
    captured = []
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured.append(payload.copy())
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    for brief in ("brief-a", "brief-b"):
        args = _spawn_args(tmp_path)
        args.initial_prompt = brief
        assert cli.spawn(args) == 0
        _printed_json(capsys)
    assert captured[0]["idempotency_key"] != captured[1]["idempotency_key"]


def test_spawn_payload_accepts_idempotency_key_override(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.idempotency_key = "manual-key"

    result = cli.spawn(args)
    _printed_json(capsys)

    assert result == 0
    assert captured["payload"]["idempotency_key"] == "manual-key"


def test_spawn_cancel_verb_sends_target_and_host(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_once(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn_cancel.ok", "state": "cancelled", "stream_id": "hosta:v2-x"}

    monkeypatch.setattr(cli, "spawn_cancel_once", fake_once)
    # `spawn cancel <key>` is rewritten to the spawn-cancel verb by main().
    assert cli.main(["spawn", "cancel", "abc123", "--host", "hosta"]) == 0
    assert captured["payload"]["type"] == "spawn_cancel"
    assert captured["payload"]["target"] == "abc123"
    assert captured["payload"]["host"] == "hosta"


def test_spawn_status_verb_sends_target(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_once(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn_status.ok", "found": False, "outcomes": [], "reservations": [], "hold": None}

    monkeypatch.setattr(cli, "spawn_status_once", fake_once)
    assert cli.main(["spawn", "status", "key-xyz"]) == 0
    assert captured["payload"]["type"] == "spawn_status"
    assert captured["payload"]["target"] == "key-xyz"


def test_spawn_cancel_verb_nonzero_on_error(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_once(_config, payload, timeout):
        return {"type": "spawn_cancel.error", "error_code": "cancel_after_bind", "stream_id": "hosta:v2-y"}

    monkeypatch.setattr(cli, "spawn_cancel_once", fake_once)
    assert cli.main(["spawn-cancel", "boundkey"]) == 1


def test_spawn_freeze_and_unfreeze_verbs(monkeypatch, tmp_path: Path, capsys) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_once(_config, payload, timeout):
        calls.append(payload)
        return {"type": "spawn_freeze.ok", "host": payload.get("host")}

    monkeypatch.setattr(cli, "spawn_freeze_once", fake_once)
    assert cli.main(["spawn", "freeze", "--host", "hosta", "--reason", "B1", "--ttl", "120"]) == 0
    assert cli.main(["spawn", "unfreeze", "--host", "hosta"]) == 0
    assert calls[0]["type"] == "spawn_freeze" and calls[0]["reason"] == "B1" and calls[0]["ttl_s"] == 120.0
    assert calls[1]["type"] == "spawn_unfreeze"


def test_spawn_direct_defaults_parent_to_caller_without_wrapper_workspace(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostc:codex-caller")

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        captured["timeout"] = timeout
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path)
    args.parent = None

    result = cli.spawn(args)
    printed = _printed_json(capsys)

    assert result == 0
    assert captured["payload"]["type"] == "spawn"
    assert captured["payload"]["parent_stream_id"] == "hostc:codex-caller"
    assert captured["payload"]["self_close_on_completion"] is True
    assert captured["timeout"] == 1.0
    assert printed["schemas"] == _expected_schemas()


@pytest.mark.parametrize(
    ("parent", "role", "top_level", "requested_visibility", "expected_visibility"),
    [
        (None, "nexus", True, None, "default"),
        ("hostc:codex-parent", "nexus", False, None, "default"),
        (None, "lead", True, None, "default"),
        ("hostc:codex-parent", "lead", False, None, None),
        (None, "lead", True, "nested", "nested"),
        ("hostc:codex-parent", "nexus", False, "hidden", "hidden"),
    ],
)
def test_spawn_visibility_payload_defaults_and_explicit_overrides(
    monkeypatch, tmp_path: Path, capsys, parent, role, top_level, requested_visibility, expected_visibility
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path, role=role)
    args.parent = parent
    args.top_level = top_level
    args.visibility = requested_visibility

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured["payload"]["visibility"] == expected_visibility
    if top_level:
        assert "parent_stream_id" not in captured["payload"]


def test_spawn_role_qa_includes_role_baseline_when_memory_file_exists(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    baseline = agents / "qa_baseline.md"
    baseline.write_text("QA body.\nKeep this exact.\n", encoding="utf-8")
    _install_spawn_fakes(monkeypatch, tmp_path, memory_repo)

    result = cli.spawn(_spawn_args(tmp_path, role="qa"))
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["role_baseline"] == {
        "role": "qa",
        "source_path": str(baseline.resolve()),
        "content": "QA body.\nKeep this exact.\n",
    }
    assert printed["schemas"] == _expected_schemas()


def test_spawn_role_baseline_is_prepended_to_initial_prompt(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    baseline = agents / "lead_baseline.md"
    baseline.write_text("---\nid: lead\n---\nBASELINE\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo))

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)

    args = _spawn_args(tmp_path, role="lead")
    args.initial_prompt = "TASK"
    assert cli.spawn(args) == 0
    printed = _printed_json(capsys)

    assert captured["payload"]["initial_prompt"] == "BASELINE\n\nTASK"
    assert printed["role_baseline"]["content"] == "BASELINE\n"


def test_spawn_role_baseline_uploads_composed_prompt_bytes(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    (agents / "lead_baseline.md").write_text("BASELINE\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo))

    async def fake_upload_prompt_blob_once(_config, data, timeout):
        captured["uploaded"] = data
        return {
            "type": "upload_prompt_blob.ok",
            "prompt_blob_sha": hashlib.sha256(data).hexdigest(),
        }

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "upload_prompt_blob_once", fake_upload_prompt_blob_once)
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    prompt = "TASK " * (cli.INITIAL_PROMPT_INLINE_CAP_BYTES // 4)
    expected = ("BASELINE\n\n" + prompt).encode("utf-8")
    args = _spawn_args(tmp_path, role="lead")
    args.initial_prompt = prompt

    assert cli.spawn(args) == 0
    _printed_json(capsys)

    assert captured["uploaded"] == expected
    assert captured["payload"]["initial_prompt_blob_sha"] == hashlib.sha256(expected).hexdigest()
    assert "initial_prompt" not in captured["payload"]


def test_spawn_role_baseline_file_prompt_uploads_composed_prompt_bytes(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    (agents / "lead_baseline.md").write_text("BASELINE\n", encoding="utf-8")
    prompt_file = tmp_path / "prompt.txt"
    prompt = "TASK " * (cli.INITIAL_PROMPT_INLINE_CAP_BYTES // 4)
    prompt_file.write_text(prompt, encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo))

    async def fake_upload_prompt_blob_once(_config, data, timeout):
        captured["uploaded"] = data
        return {
            "type": "upload_prompt_blob.ok",
            "prompt_blob_sha": hashlib.sha256(data).hexdigest(),
        }

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "upload_prompt_blob_once", fake_upload_prompt_blob_once)
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path, role="lead")
    args.initial_prompt = None
    args.initial_prompt_file = str(prompt_file)

    assert cli.spawn(args) == 0
    _printed_json(capsys)

    expected = ("BASELINE\n\n" + prompt).encode("utf-8")
    assert captured["uploaded"] == expected
    assert captured["payload"]["initial_prompt_blob_sha"] == hashlib.sha256(expected).hexdigest()
    assert "initial_prompt" not in captured["payload"]


def test_spawn_role_baseline_is_initial_prompt_when_no_prompt_is_given(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    (agents / "lead_baseline.md").write_text("BASELINE\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo))

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path, role="lead")
    args.initial_prompt = None

    assert cli.spawn(args) == 0
    _printed_json(capsys)

    assert captured["payload"]["initial_prompt"] == "BASELINE\n"


def test_spawn_indeterminate_role_baseline_ack_is_explicit(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    (memory_repo / "agents").mkdir(parents=True)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo))

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.indeterminate", "stream_id": "hostc:codex-child"}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path, role="lead")

    assert cli.spawn(args) == 3
    captured_output = capsys.readouterr()
    printed = json.loads(captured_output.out)

    assert printed["role_baseline"] is None
    assert "role_baseline missing for role 'lead'" in captured_output.err


def test_handoff_default_role_loads_baseline_and_records_role_source(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    (agents / "qa_baseline.md").write_text("QA BASELINE\n", encoding="utf-8")
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path, memory_repo))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostc:retiring")
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "codex",
            "effective_model": "gpt-5.6-luna",
            "effective_effort": "max",
            "role": "qa",
        },
    )

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:successor"}}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    args = _spawn_args(tmp_path)
    args.parent = None
    args.handoff = True
    args.initial_prompt = "NEXT TASK"

    assert cli.spawn(args) == 0
    printed = _printed_json(capsys)

    assert captured["payload"]["role"] == "qa"
    assert captured["payload"]["initial_prompt"] == "QA BASELINE\n\nNEXT TASK"
    assert printed["role_source"] == "handoff"
    assert printed["role_baseline"]["role"] == "qa"


def test_spawn_arbitrary_role_with_baseline_file_proves_generic_lookup(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    agents = memory_repo / "agents"
    agents.mkdir(parents=True)
    role = "arch_review"
    baseline = agents / f"{role}_baseline.md"
    baseline_body = "Architecture review body.\nProves lookup is generic.\n"
    baseline.write_text(baseline_body, encoding="utf-8")
    _install_spawn_fakes(monkeypatch, tmp_path, memory_repo)

    result = cli.spawn(_spawn_args(tmp_path, role=role))
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["role_baseline"]["role"] == role
    assert printed["role_baseline"]["source_path"] == str(baseline.resolve())
    assert printed["role_baseline"]["content"] == baseline_body


def test_spawn_role_doc_qa_missing_baseline_sets_null(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    (memory_repo / "agents").mkdir(parents=True)
    _install_spawn_fakes(monkeypatch, tmp_path, memory_repo)

    result = cli.spawn(_spawn_args(tmp_path, role="doc_qa"))
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    stderr = captured.err

    assert result == 0
    assert printed["role_baseline"] is None
    assert "role_baseline missing for role 'doc_qa'" in stderr


def test_spawn_role_dev_missing_baseline_sets_null(monkeypatch, tmp_path: Path, capsys) -> None:
    memory_repo = tmp_path / "memory"
    (memory_repo / "agents").mkdir(parents=True)
    _install_spawn_fakes(monkeypatch, tmp_path, memory_repo)

    result = cli.spawn(_spawn_args(tmp_path, role="dev"))
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["role_baseline"] is None


def test_spawn_without_role_omits_role_baseline(monkeypatch, tmp_path: Path, capsys) -> None:
    _install_spawn_fakes(monkeypatch, tmp_path)

    result = cli.spawn(_spawn_args(tmp_path))
    printed = _printed_json(capsys)

    assert result == 0
    assert "role_baseline" not in printed
    assert printed["schemas"] == _expected_schemas()


def test_spawn_starting_is_a_durable_success(monkeypatch, tmp_path: Path, capsys) -> None:
    response = {
        "type": "spawn.ok",
        "state": "starting",
        "session": {
            "stream_id": "hostc:codex-child",
            "spawn_readiness": "pending",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "spawn_profile": "agent_orch",
            "catalog_version": "spawn-catalog-v2",
            "resolution_source": "profile_default",
        },
    }
    _install_spawn_fakes(monkeypatch, tmp_path, response=response)

    result = cli.spawn(_spawn_args(tmp_path))
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["type"] == "spawn.ok"
    assert printed["state"] == "starting"
    assert printed["session"]["stream_id"] == "hostc:codex-child"
    assert printed["session"]["spawn_readiness"] == "pending"
    assert {
        key: printed["session"][key]
        for key in ("spawn_profile", "catalog_version", "resolution_source")
    } == {
        "spawn_profile": "agent_orch",
        "catalog_version": "spawn-catalog-v2",
        "resolution_source": "profile_default",
    }


def test_spawn_failed_response_prints_unchanged(monkeypatch, tmp_path: Path, capsys) -> None:
    failed_response = {"ok": False, "error": "daemon_died", "reason": "connection_reset"}
    _install_spawn_fakes(monkeypatch, tmp_path, response=failed_response)

    result = cli.spawn(_spawn_args(tmp_path, role="qa"))
    printed = _printed_json(capsys)

    assert result == 1
    assert printed == failed_response


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_stderr"),
    [
        (TimeoutError("slow"), 67, "timeout waiting for spawn response"),
        (PermissionError("nope"), 66, "auth failed"),
        (OSError("down"), 64, "chat_streamd unreachable"),
        (RuntimeError("dropped"), 65, "connection dropped after request may have been sent"),
    ],
)
def test_spawn_direct_maps_transport_exit_codes(monkeypatch, tmp_path: Path, capsys, exc, expected_code, expected_stderr) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_spawn_once(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)

    result = cli.spawn(_spawn_args(tmp_path))

    assert result == expected_code
    captured = capsys.readouterr()
    assert expected_stderr in captured.err
    printed = json.loads(captured.out)
    assert printed["request_id"].startswith("spawn-")
    assert printed["type"] == "spawn.error"
    assert "raw_error" in printed


def test_await_spawn_by_request_id_uses_direct_daemon_rpc(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_await_spawn_once(_config, payload, timeout=30.0):
        captured["payload"] = payload
        captured["timeout"] = timeout
        return {"type": "await_spawn.ok", "ok": True, "stream_id": "hostc:codex-child", "reconciled": True}

    monkeypatch.setattr(cli, "await_spawn_once", fake_await_spawn_once)
    args = SimpleNamespace(workspace=str(tmp_path), request_id="spawn-123", stream_id=None, timeout=4.0)

    result = cli.await_spawn(args)
    printed = _printed_json(capsys)

    assert result == 0
    assert captured["payload"] == {"type": "await_spawn", "timeout": 4.0, "spawn_request_id": "spawn-123"}
    assert captured["timeout"] == 30.0
    assert printed["stream_id"] == "hostc:codex-child"


def test_await_spawn_by_stream_id_uses_direct_daemon_rpc(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_await_spawn_once(_config, payload, timeout=30.0):
        captured["payload"] = payload
        return {"type": "await_spawn.error", "ok": False, "error": "spawn_reconcile_timeout", "stream_id": "hostc:codex-child"}

    monkeypatch.setattr(cli, "await_spawn_once", fake_await_spawn_once)
    args = SimpleNamespace(workspace=str(tmp_path), request_id=None, stream_id="hostc:codex-child", timeout=1.0)

    result = cli.await_spawn(args)
    printed = _printed_json(capsys)

    assert result == 1
    assert captured["payload"] == {"type": "await_spawn", "timeout": 1.0, "stream_id": "hostc:codex-child"}
    assert printed["error"] == "spawn_reconcile_timeout"


def test_await_spawn_parser_accepts_request_id_and_stream_id() -> None:
    parser = cli.build_parser()

    request_args = parser.parse_args(["await-spawn", "--request-id", "spawn-1"])
    stream_args = parser.parse_args(["await-spawn", "--stream-id", "hostc:codex-child"])

    assert request_args.func is cli.await_spawn
    assert request_args.request_id == "spawn-1"
    assert stream_args.stream_id == "hostc:codex-child"


# ---------------------------------------------------------------------------
# Spawn completion behavior tests.
# Cover the `--self-close-on-completion` spawn flag.
# ---------------------------------------------------------------------------


def test_spawn_help_lists_self_close_on_completion_flag() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agent_orch.cli import main; raise SystemExit(main(['spawn', '--help']))",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_subprocess_env(),
    )

    assert result.returncode == 0, result.stderr
    assert "--self-close-on-completion" in result.stdout


def test_spawn_help_subprocess_imports_the_tree_under_test() -> None:
    # Guards the fix above: if a child interpreter ever resolves agent_orch from an ambient
    # install again, the two spawn --help tests silently validate the wrong working tree.
    result = subprocess.run(
        [sys.executable, "-c", "import agent_orch; print(agent_orch.__file__)"],
        capture_output=True,
        text=True,
        check=False,
        env=_cli_subprocess_env(),
    )

    assert result.returncode == 0, result.stderr
    child_pkg = Path(result.stdout.strip()).resolve()
    assert child_pkg == Path(cli.__file__).resolve().parents[0] / "__init__.py"


_PROMPTED_SPAWN_CONTRACT_CHILD = r"""
import os
from pathlib import Path
from types import SimpleNamespace

from agent_orch import cli
from agent_orch.config import Config

runtime = Path(os.environ["SPAWN_CONTRACT_RUNTIME"])
cli.load_config = lambda: Config(
    ws_url="ws://unused",
    token="",
    host_id="hostc",
    runtime_dir=runtime,
    memory_repo_path=None,
)
cli.resolve_spawn = lambda **_kwargs: {
    "provider": "codex",
    "model": None,
    "effort": None,
}
case = os.environ["SPAWN_CONTRACT_CASE"]
if case == "pre_send":
    cli._initial_prompt_payload = lambda _args, _config: (_ for _ in ()).throw(
        RuntimeError("upload unavailable before send")
    )

async def fake_spawn_once(_config, payload, timeout):
    request_id = payload["request_id"]
    if case == "post_send":
        raise RuntimeError("disconnect after send")
    return {
        "type": "spawn.ok",
        "ok": True,
        "spawn_request_id": request_id,
        "session": {
            "stream_id": "hostc:codex-child",
            "spawn_readiness": "pending",
            "spawn_readiness_reason": "readiness_timeout",
        },
        "initial_prompt_delivery": {
            "delivery_status": "delivered",
            "spawn_request_id": request_id,
            "stream_id": "hostc:codex-child",
            "tell_id": "tell-contract",
            "ledger_row_id": 41,
            "acknowledged_at": "2026-08-01T00:00:00Z",
        },
    }

cli.spawn_once = fake_spawn_once
args = SimpleNamespace(
    objective="Exercise subprocess spawn delivery",
    provider="codex",
    model=None,
    effort=None,
    host=None,
    role=None,
    phase="code_dev",
    spec_id=None,
    visibility="hidden",
    parent="hostc:claude-leader",
    handoff=False,
    disposition_waived_reason=None,
    initial_prompt="prompt obligation probe",
    initial_prompt_file=None,
    timeout=1.0,
    self_close_on_completion=True,
)
raise SystemExit(cli.spawn(args))
"""


def _run_prompted_spawn_contract_child(tmp_path: Path, case: str) -> subprocess.CompletedProcess[str]:
    env = _cli_subprocess_env()
    env["SPAWN_CONTRACT_RUNTIME"] = str(tmp_path / case)
    env["SPAWN_CONTRACT_CASE"] = case
    return subprocess.run(
        [sys.executable, "-c", _PROMPTED_SPAWN_CONTRACT_CHILD],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_prompted_spawn_subprocess_preserves_raw_and_pipeline_outcomes(tmp_path: Path) -> None:
    pre_send = _run_prompted_spawn_contract_child(tmp_path, "pre_send")
    assert pre_send.returncode == 5
    pre_send_json = json.loads(pre_send.stdout)
    assert pre_send_json["type"] == "spawn.error"
    assert pre_send_json["no_spawn_attempted"] is True
    assert pre_send_json["error_code"] == "initial_prompt_upload_failed"
    assert "upload failed" in pre_send.stderr

    post_send = _run_prompted_spawn_contract_child(tmp_path, "post_send")
    post_send_json = json.loads(post_send.stdout)
    assert post_send.returncode != 0
    assert post_send_json["type"] == "spawn.error"
    assert post_send_json["request_id"].startswith("spawn-")
    assert post_send_json["error_code"] == "connection_dropped"
    assert "disconnect after send" in post_send.stderr

    delivered = _run_prompted_spawn_contract_child(tmp_path, "delivered")
    delivered_json = json.loads(delivered.stdout)
    assert delivered.returncode == 0
    # A successful spawn now emits exactly the pre-RPC `spawn key:` line on
    # stderr (the retry/cancel handle) and nothing else.
    stderr_lines = [ln for ln in delivered.stderr.splitlines() if ln.strip()]
    assert len(stderr_lines) == 1
    assert re.fullmatch(r"spawn key: [0-9a-f]{32}", stderr_lines[0]), stderr_lines
    assert delivered_json["type"] == "spawn.ok"
    assert delivered_json["initial_prompt_delivery"]["delivery_status"] == "delivered"
    assert delivered_json["spawn_request_id"].startswith("spawn-")

    env = _cli_subprocess_env()
    env.update(
        {
            "PYTHON_BIN": sys.executable,
            "PYTHON_CHILD": _PROMPTED_SPAWN_CONTRACT_CHILD,
            "SPAWN_CONTRACT_RUNTIME": str(tmp_path / "pipeline"),
            "SPAWN_CONTRACT_CASE": "delivered",
        }
    )
    pipeline = subprocess.run(
        [
            "bash",
            "-c",
            '"$PYTHON_BIN" -c "$PYTHON_CHILD" | jq -c \'{type,spawn_request_id,stream_id:.session.stream_id}\'; '
            'statuses=("${PIPESTATUS[@]}"); printf "PIPESTATUS=%s,%s\\n" "${statuses[0]}" "${statuses[1]}" >&2; '
            'exit "${statuses[1]}"',
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    projected = json.loads(pipeline.stdout)
    assert pipeline.returncode == 0
    assert "PIPESTATUS=0,0" in pipeline.stderr
    assert projected["type"] == "spawn.ok"
    assert projected["spawn_request_id"].startswith("spawn-")
    assert projected["stream_id"] == "hostc:codex-child"

    postprocessor_failure = subprocess.run(
        [
            "bash",
            "-c",
            '"$PYTHON_BIN" -c "$PYTHON_CHILD" | jq -e \'.missing\'; '
            'statuses=("${PIPESTATUS[@]}"); printf "PIPESTATUS=%s,%s\\n" "${statuses[0]}" "${statuses[1]}" >&2; '
            'exit "${statuses[1]}"',
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert postprocessor_failure.returncode == 1
    assert "PIPESTATUS=0,1" in postprocessor_failure.stderr


def test_spawn_parser_self_close_on_completion_tracks_absent_and_explicit_values() -> None:
    parser = cli.build_parser()

    # Absence remains semantically disabled while preserving schedule-row
    # tri-state truth (NULL versus an explicit --no-* override).
    default_args = parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex"])
    assert default_args.self_close_on_completion is None

    # Explicit enable still works.
    set_args = parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex", "--self-close-on-completion"])
    assert set_args.self_close_on_completion is True

    # Opt-out keeps the worker open for follow-up sends.
    opt_out = parser.parse_args(["spawn", "--objective", "Exercise the existing spawn contract", "--provider", "codex", "--no-self-close-on-completion"])
    assert opt_out.self_close_on_completion is False


def test_spawn_payload_includes_self_close_by_default_for_parented_spawn(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)  # has parent="hostc:claude-leader", no explicit flag
    args.self_close_on_completion = True  # argparse default

    result = cli.spawn(args)
    _printed_json(capsys)

    assert result == 0
    assert captured["payload"]["self_close_on_completion"] is True


@pytest.mark.parametrize("scheduled", [False, True], ids=["immediate", "scheduled"])
@pytest.mark.parametrize("self_close", [True, False], ids=["opt-in", "opt-out"])
def test_handoff_self_close_flag_is_serialized_on_both_spawn_paths(
    monkeypatch, tmp_path: Path, capsys, scheduled: bool, self_close: bool,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostc:leader")
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "codex",
            "effective_model": "gpt-5.6-luna",
            "effective_effort": "max",
            "role": "worker",
        },
    )

    async def fake_transport(_config, payload, timeout):
        captured["payload"] = dict(payload)
        if scheduled:
            return {"type": "schedule.insert.ok", "schedule": {}}
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:successor"}}

    flag = "--self-close-on-completion" if self_close else "--no-self-close-on-completion"
    if scheduled:
        monkeypatch.setattr(cli, "schedule_once", fake_transport)
        argv = [
            "spawn", "--objective", "Exercise the existing spawn contract", "--handoff", "--visibility", "hidden", "--delay", "1m",
            "--allow-past-time",
            flag,
        ]
    else:
        monkeypatch.setattr(cli, "spawn_once", fake_transport)
        argv = ["spawn", "--objective", "Exercise the existing spawn contract", "--handoff", "--visibility", "hidden", flag]

    assert cli.spawn(cli.build_parser().parse_args(argv)) == 0
    _printed_json(capsys)
    assert captured["payload"]["self_close_on_completion"] is self_close


def test_spawn_payload_serializes_false_when_opted_out(monkeypatch, tmp_path: Path, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.self_close_on_completion = False  # --no-self-close-on-completion

    result = cli.spawn(args)
    _printed_json(capsys)

    assert result == 0
    assert captured["payload"]["self_close_on_completion"] is False


def test_spawn_payload_omits_self_close_for_leaderless_spawn(monkeypatch, tmp_path: Path, capsys) -> None:
    """Even with the flag on, a spawn with no parent/handoff lineage (no leader
    discoverable) must not carry self_close_on_completion — otherwise a
    top-level operator session could wrongly self-close (Case A.5 precedence)."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: None)

    async def fake_send(_config, payload, timeout):
        captured["payload"] = payload
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_send)
    args = _spawn_args(tmp_path)
    args.parent = None  # no explicit parent; discovery monkeypatched to None
    args.self_close_on_completion = True

    result = cli.spawn(args)
    _printed_json(capsys)

    assert result == 0
    assert "self_close_on_completion" not in captured["payload"]


def test_prompted_readiness_timeout_with_delivery_is_success(monkeypatch, tmp_path: Path, capsys) -> None:
    receipt = {
        "request_id": "prompt-ready-owner",
        "tell_id": "initial-prompt-prompt-ready-owner",
        "ledger_row_id": 41,
        "to_stream_id": "hostc:codex-child",
        "delivery_status": "delivered",
        "delivery_ack_at": "2026-08-01T00:00:01Z",
        "transport": "direct",
    }
    response = {
        "type": "spawn.ok",
        "ok": True,
        "session": {
            "stream_id": "hostc:codex-child",
            "spawn_readiness": "pending",
            "spawn_readiness_reason": "readiness_timeout",
        },
        "initial_prompt_delivery": receipt,
    }
    _install_spawn_fakes(monkeypatch, tmp_path, response=response)
    args = _spawn_args(tmp_path)
    args.initial_prompt = "own this brief"

    result = cli.spawn(args)
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["type"] == "spawn.ok"
    assert printed["initial_prompt_delivery"] == receipt


def test_prompted_spawn_admission_retains_the_durable_handle(monkeypatch, tmp_path: Path, capsys) -> None:
    _install_spawn_fakes(
        monkeypatch,
        tmp_path,
        response={"type": "spawn.ok", "ok": True, "state": "starting", "session": {"stream_id": "hostc:codex-child"}},
    )
    args = _spawn_args(tmp_path)
    args.initial_prompt = "own this brief"

    result = cli.spawn(args)
    printed = _printed_json(capsys)

    assert result == 0
    assert printed["type"] == "spawn.ok"
    assert printed["state"] == "starting"
    assert printed["session"]["stream_id"] == "hostc:codex-child"


def test_open_row_with_failed_bootstrap_surfaces_indeterminate(
    monkeypatch, tmp_path: Path, capsys,
) -> None:
    _install_spawn_fakes(
        monkeypatch,
        tmp_path,
        response={
            "type": "spawn.indeterminate",
            "ok": True,
            "stream_id": "hostc:codex-child",
            "session": {
                "stream_id": "hostc:codex-child",
                "status": "open",
                "closed_at": None,
                "bootstrap_state": "failed",
            },
            "initial_prompt_delivery": {"state": "indeterminate"},
        },
    )

    assert cli.spawn(_spawn_args(tmp_path)) == 3
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    stderr = captured.err
    assert printed["type"] == "spawn.indeterminate"
    assert "hostc:codex-child" in stderr
    assert "agent-orch inspect hostc:codex-child" in stderr


def test_closed_row_still_surfaces_spawn_failed(monkeypatch, tmp_path: Path, capsys) -> None:
    _install_spawn_fakes(
        monkeypatch,
        tmp_path,
        response={
            "type": "spawn.error",
            "ok": False,
            "stream_id": "hostc:codex-child",
            "session": {
                "stream_id": "hostc:codex-child",
                "status": "closed",
                "closed_at": "2026-09-02T00:00:00Z",
            },
        },
    )

    assert cli.spawn(_spawn_args(tmp_path)) == 1
    assert _printed_json(capsys)["type"] == "spawn.error"


@pytest.mark.parametrize(
    ("state", "response_type", "expected_exit"),
    [
        ("queued", "spawn.error", 1),
        ("stale", "spawn.error", 1),
        ("unknown", "spawn.error", 1),
        ("delivered", "spawn.ok", 0),
    ],
)
def test_cli_prompt_receipt_state_matrix(
    monkeypatch,
    tmp_path: Path,
    capsys,
    state: str,
    response_type: str,
    expected_exit: int,
) -> None:
    receipt = {
        "request_id": f"matrix-{state}",
        "tell_id": f"initial-prompt-matrix-{state}",
        "ledger_row_id": None if state == "unknown" else 73,
        "to_stream_id": "hostc:codex-child",
        "delivery_status": state,
    }
    _install_spawn_fakes(
        monkeypatch,
        tmp_path,
        response={
            "type": response_type,
            "ok": expected_exit == 0,
            "session": {"stream_id": "hostc:codex-child"},
            "initial_prompt_delivery": receipt,
        },
    )
    args = _spawn_args(tmp_path)
    args.initial_prompt = "matrix brief"

    result = cli.spawn(args)
    printed = _printed_json(capsys)

    assert result == expected_exit
    assert printed["initial_prompt_delivery"] == receipt
    assert printed["type"] == response_type


@pytest.mark.parametrize(
    "failure_code",
    ["prompt_delivery_undeliverable", "prompt_stage_retry_exhausted", "prompt_delivery_expired"],
)
def test_cli_prompt_terminal_failure_is_spawn_error(
    monkeypatch, tmp_path: Path, capsys, failure_code: str
) -> None:
    receipt = {
        "request_id": "failed-owner",
        "tell_id": "initial-prompt-failed-owner",
        "ledger_row_id": 91,
        "to_stream_id": "hostc:codex-child",
        "delivery_status": "failed",
        "failure_code": failure_code,
        "failed_at": "2026-08-01T00:00:01Z",
    }
    _install_spawn_fakes(
        monkeypatch,
        tmp_path,
        response={
            "type": "spawn.error",
            "ok": False,
            "error": failure_code,
            "error_code": failure_code,
            "session": {"stream_id": "hostc:codex-child"},
            "initial_prompt_delivery": receipt,
        },
    )
    args = _spawn_args(tmp_path)
    args.initial_prompt = "failed brief"

    assert cli.spawn(args) == 1
    printed = _printed_json(capsys)
    assert printed["type"] == "spawn.error"
    assert printed["error_code"] == failure_code
    assert printed["initial_prompt_delivery"] == receipt


# -- hidden-seat self-close default (spec_example__hidden_seat_self_close) --

def _capture_spawn_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = dict(payload)
        return {"type": "spawn.ok", "session": {"stream_id": "hostc:codex-child"}}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    return captured


def test_hidden_spawn_defaults_self_close_when_flag_absent(monkeypatch, tmp_path: Path, capsys) -> None:
    captured = _capture_spawn_payload(monkeypatch, tmp_path)
    args = _spawn_args(tmp_path)
    args.visibility = "hidden"
    args.self_close_on_completion = None  # flag never passed on the CLI

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert captured["payload"]["self_close_on_completion"] is True


def test_visible_spawn_stays_default_off_without_flag(monkeypatch, tmp_path: Path, capsys) -> None:
    captured = _capture_spawn_payload(monkeypatch, tmp_path)
    args = _spawn_args(tmp_path)
    args.visibility = "default"
    args.self_close_on_completion = None

    assert cli.spawn(args) == 0
    _printed_json(capsys)
    assert "self_close_on_completion" not in captured["payload"]


def test_resolve_self_close_matrix() -> None:
    def _args(scoc):
        return SimpleNamespace(self_close_on_completion=scoc)

    resolve = cli._resolve_self_close_on_completion
    # Hidden + real parent defaults on; explicit opt-out is a wire false, while
    # non-hidden stays default-off/absent.
    assert resolve(_args(None), visibility="hidden", parent="p", handoff=False) is True
    assert resolve(_args(False), visibility="hidden", parent="p", handoff=False) is False
    assert resolve(_args(None), visibility="default", parent="p", handoff=False) is None
    assert resolve(_args(None), visibility="nested", parent="p", handoff=False) is None
    # Explicit opt-in honoured with lineage (parent or handoff), ignored without.
    assert resolve(_args(True), visibility="default", parent="p", handoff=False) is True
    assert resolve(_args(True), visibility="hidden", parent=None, handoff=True) is True
    assert resolve(_args(True), visibility="hidden", parent=None, handoff=False) is None
    # Hidden default needs a real parent, not a bare handoff (no parent lineage).
    assert resolve(_args(None), visibility="hidden", parent=None, handoff=True) is None
    # A blank/whitespace --parent is not lineage (the daemon strips it): neither
    # the hidden default nor an explicit opt-in may ride a leaderless payload.
    assert resolve(_args(None), visibility="hidden", parent="", handoff=False) is None
    assert resolve(_args(None), visibility="hidden", parent="   ", handoff=False) is None
    assert resolve(_args(True), visibility="hidden", parent="  ", handoff=False) is None
    assert resolve(_args(False), visibility="hidden", parent="  ", handoff=False) is None
