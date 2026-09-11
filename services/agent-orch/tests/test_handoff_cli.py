from __future__ import annotations

from argparse import Namespace

import pytest

from agent_orch import cli
from agent_orch.config import Config

_REAL_HANDOFF_SOURCE_ROW = cli._handoff_source_row


def _args(**overrides):
    data = {"objective": "Exercise the existing spawn contract",
        "workspace": None,
        "provider": "codex",
        "model": None,
        "effort": None,
        "host": "hostb",
        "role": None,
        "phase": "stage-c",
        "visibility": None,
        "parent": None,
        "handoff": True,
        "confirm_model_change": False,
        "initial_prompt": "short prompt",
        "initial_prompt_file": None,
        "timeout": 1.0,
    }
    data.update(overrides)
    return Namespace(**data)


def _delivered_spawn(payload, *, stream_id="hostb:codex-new"):
    request_id = payload["request_id"]
    return {
        "type": "spawn.ok",
        "request_id": request_id,
        "session": {"stream_id": stream_id},
        "initial_prompt_delivery": {
            "request_id": request_id,
            "tell_id": f"handoff-{request_id}",
            "ledger_row_id": 17,
            "to_stream_id": stream_id,
            "delivery_status": "delivered",
            "delivery_ack_at": "2026-08-01T00:00:01Z",
        },
    }


@pytest.fixture(autouse=True)
def _source_row(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ORCH_MEMORY_REPO", str(tmp_path))
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "stream_id": "hostb:codex-old",
            "provider": "codex",
            "effective_model": "gpt-5.6-sol",
            "effective_effort": "high",
            "role": "qa",
        },
    )


def test_handoff_short_initial_prompt_is_inlined(monkeypatch, capsys, tmp_path):
    captured = {}

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = payload
        captured["timeout"] = timeout
        return _delivered_spawn(payload)

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-old")
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)

    assert cli.spawn(_args()) == 0

    payload = captured["payload"]
    assert payload["type"] == "spawn"
    assert payload["handoff"] is True
    assert payload["handoff_from_stream_id"] == "hostb:codex-old"
    assert payload["role"] == "qa"
    assert payload["visibility"] == "default"
    assert "parent_stream_id" not in payload
    assert payload["initial_prompt"] == "short prompt"
    assert "initial_prompt_blob_sha" not in payload
    assert '"stream_id":"hostb:codex-new"' in capsys.readouterr().out


def test_handoff_without_objective_succeeds(monkeypatch, capsys, tmp_path):
    # A handoff successor inherits its predecessor's goal; no --objective is required.
    captured = {}

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = payload
        return _delivered_spawn(payload)

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-old")
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)

    assert cli.spawn(_args(objective=None)) == 0

    payload = captured["payload"]
    assert payload["handoff"] is True
    assert payload["objective"] is None
    assert payload["objective_supported"] is True
    assert "parent_stream_id" not in payload


def test_handoff_long_initial_prompt_file_uploads_prompt_blob(monkeypatch, capsys, tmp_path):
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("x" * (cli.INITIAL_PROMPT_INLINE_CAP_BYTES + 1), encoding="utf-8")
    captured = {}

    async def fake_upload_prompt_blob_once(config, data, timeout):
        captured["upload"] = {"config": config, "data": data, "timeout": timeout}
        return {"type": "upload_prompt_blob.ok", "prompt_blob_sha": "sha-prompt", "size_bytes": len(data)}

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = payload
        return _delivered_spawn(payload)

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-old")
    monkeypatch.setattr(cli, "upload_prompt_blob_once", fake_upload_prompt_blob_once)
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)

    assert cli.spawn(_args(initial_prompt=None, initial_prompt_file=str(prompt_file))) == 0

    assert captured["upload"]["data"] == prompt_file.read_bytes()
    assert captured["payload"]["initial_prompt_blob_sha"] == "sha-prompt"
    assert "initial_prompt" not in captured["payload"]
    assert "role_baseline missing for role 'qa'" in capsys.readouterr().err


def test_handoff_rejects_initial_prompt_mutual_exclusion():
    parser = cli.build_parser()

    try:
        parser.parse_args(
            [
                "spawn", "--objective", "Exercise the existing spawn contract",
                "--provider",
                "codex",
                "--handoff",
                "--initial-prompt",
                "inline",
                "--initial-prompt-file",
                "/tmp/prompt.txt",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted both initial prompt sources")


def test_handoff_rejects_non_utf8_initial_prompt_file(monkeypatch, tmp_path, capsys):
    prompt_file = tmp_path / "bad.txt"
    prompt_file.write_bytes(b"\xff\xfe")

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-old")

    assert cli.spawn(_args(initial_prompt=None, initial_prompt_file=str(prompt_file))) == 2
    assert "prompt_blob_invalid_utf8" in capsys.readouterr().err


def _install_spawn(monkeypatch, tmp_path, captured):
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-old")

    async def _spawn(_config, payload, timeout):
        captured["payload"] = payload
        return _delivered_spawn(payload, stream_id="hostb:successor")

    monkeypatch.setattr(cli, "spawn_once", _spawn)


def test_handoff_omitted_tuple_inherits_cross_provider_source(monkeypatch, tmp_path):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "claude",
            "effective_model": "claude-fable-5",
            "effective_effort": "xhigh",
            "role": "qa",
        },
    )

    assert cli.spawn(_args(provider=None)) == 0
    assert {field: captured["payload"][field] for field in ("provider", "model", "effort")} == {
        "provider": "claude",
        "model": "claude-fable-5-1",
        "effort": "xhigh",
    }
    assert captured["payload"]["resolution_source"] == "handoff_inherited"


def test_handoff_same_tuple_alias_needs_no_confirmation(monkeypatch, tmp_path):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "claude",
            "effective_model": "claude-fable-5",
            "effective_effort": "high",
            "role": "qa",
        },
    )

    assert cli.spawn(_args(provider="claude", model="fable", effort="high")) == 0
    assert captured["payload"]["model"] == "claude-fable-5-1"
    assert captured["payload"]["resolution_source"] == "handoff_inherited"


@pytest.mark.parametrize(
    "overrides",
    [
        {"effort": "xhigh"},
        {"model": "terra"},
        {"provider": "claude", "model": "fable", "effort": "high"},
    ],
)
def test_handoff_changed_explicit_tuple_warns_without_confirmation(monkeypatch, tmp_path, capsys, overrides):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)

    assert cli.spawn(_args(**overrides)) == 0
    stderr = capsys.readouterr().err
    assert "WARNING" in stderr
    assert "changed tuple fields" in stderr
    assert captured["payload"]["handoff"] is True
    assert "confirm_model_change" not in captured["payload"]
    assert "handoff_model_change_override" not in captured["payload"]


def test_handoff_changed_tuple_warning_names_changed_fields(
    monkeypatch, tmp_path, capsys
):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)

    assert cli.spawn(_args(effort="xhigh")) == 0
    stderr = capsys.readouterr().err
    assert "WARNING" in stderr
    assert "changed tuple fields: effort" in stderr
    assert captured["payload"]["effort"] == "xhigh"


def test_handoff_role_only_warns_without_confirmation(monkeypatch, tmp_path, capsys):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)

    assert cli.spawn(_args(role="nexus")) == 0
    stderr = capsys.readouterr().err
    assert "changed tuple fields: role" in stderr
    assert captured["payload"]["role"] == "nexus"
    assert "confirm_model_change" not in captured["payload"]


def test_handoff_confirm_flag_suppresses_only_the_compatibility_warning(
    monkeypatch, tmp_path, capsys
):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)

    assert cli.spawn(_args(effort="xhigh", confirm_model_change=True)) == 0
    payload = captured["payload"]
    assert payload["confirm_model_change"] is True
    assert "handoff_model_change_override" not in payload
    assert "role_baseline missing for role 'qa'" in capsys.readouterr().err


def test_handoff_confirm_flag_suppresses_role_change_warning(
    monkeypatch, tmp_path, capsys
):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)

    assert cli.spawn(_args(role="nexus", confirm_model_change=True)) == 0
    payload = captured["payload"]
    assert payload["role"] == "nexus"
    assert payload["confirm_model_change"] is True
    assert "handoff_model_change_override" not in payload
    assert "role_baseline missing for role 'nexus'" in capsys.readouterr().err


def test_handoff_unchanged_tuple_ignores_confirm_override(monkeypatch, tmp_path):
    captured = {}
    _install_spawn(monkeypatch, tmp_path, captured)

    assert cli.spawn(_args(confirm_model_change=True)) == 0
    assert captured["payload"]["resolution_source"] == "handoff_inherited"
    assert "handoff_model_change_override" not in captured["payload"]


def test_handoff_legacy_question_option_does_not_gate(monkeypatch, tmp_path, capsys):
    _install_spawn(monkeypatch, tmp_path, {})
    assert cli.spawn(_args(effort="xhigh", model_change_approved="q-approved")) == 0
    stderr = capsys.readouterr().err
    assert "WARNING" in stderr


def test_handoff_missing_effective_tuple_fails_closed(monkeypatch, tmp_path, capsys):
    _install_spawn(monkeypatch, tmp_path, {})
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {"provider": "codex", "effective_model": None, "effective_effort": "high"},
    )

    assert cli.spawn(_args(provider=None)) == 2
    assert "handoff_effective_tuple_missing" in capsys.readouterr().err


def test_non_handoff_still_requires_provider(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    assert cli.spawn(_args(handoff=False, provider=None, top_level=True)) == 2
    assert "--provider is required unless --handoff" in capsys.readouterr().err


def test_handoff_source_lookup_requires_exact_stream_row(monkeypatch):
    monkeypatch.setattr(
        cli,
        "fetch_snapshot",
        lambda _config, timeout: {
            "sessions": [
                {"stream_id": "hostb:other", "provider": "claude"},
                {"stream_id": "hostb:codex-old", "provider": "codex"},
            ]
        },
    )
    assert _REAL_HANDOFF_SOURCE_ROW(object(), "hostb:codex-old", timeout=1)["provider"] == "codex"
    with pytest.raises(cli.SpawnProfileError, match="retiring stream row not found") as exc:
        _REAL_HANDOFF_SOURCE_ROW(object(), "hostb:missing", timeout=1)
    assert exc.value.code == "handoff_source_not_found"

