import pytest
from agent_orch import cli


@pytest.mark.parametrize("action", ["advise", "answer"])
def test_advice_commands_read_body_and_preserve_request_key(tmp_path, monkeypatch, action):
    body = tmp_path / "body.txt"
    body.write_text("GATE\nReview this scope.\n")
    captured = {}
    monkeypatch.setattr(cli, "_assistant_call", lambda args, payload: captured.update(payload) or 0)
    flags = ["--work-item", "none"] if action == "advise" else ["--ruling-request-id", "ruling-1"]
    args = cli.build_parser().parse_args(["assistant", action, "--body-file", str(body), "--request-id", "key-1", *flags])
    assert args.func(args) == 0
    assert captured["type"] == "assistant." + action
    assert captured["request_id"] == "key-1"
    assert captured["body"] == body.read_text()


def test_session_controls_are_one_authority_action(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "_assistant_call", lambda args, payload: captured.update(payload) or 0)
    args = cli.build_parser().parse_args(["assistant", "authority", "--session-kinds", "spawn,session_close", "--account", "test"])
    assert args.func(args) == 0
    assert captured["action"] == "session_kinds" and captured["account"] == "test"
    assert captured["session_kinds"] == "spawn,session_close"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["assistant", "authority", "--set", "sessions", "--session-kinds", "none"])


def test_model_cas_is_serialized_only_on_session_controls(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "_assistant_call", lambda args, payload: captured.update(payload) or 0)
    args = cli.build_parser().parse_args(["assistant", "authority", "--session-kinds", "spawn,session_close",
        "--model", "gpt-6.2-astra", "--expected-model", "gpt-6-astra"])
    assert args.func(args) == 0
    assert captured["action"] == "session_kinds"
    assert (captured["model"], captured["expected_model"]) == ("gpt-6.2-astra", "gpt-6-astra")
    assert "account" not in captured


@pytest.mark.parametrize("flags", [
    ["--model", "gpt-6.2-astra"], ["--expected-model", "gpt-6-astra"],
    ["--model", "gpt-6.2-astra", "--expected-model", "gpt-6-astra"],
    ["--session-kinds", "spawn", "--model", "" , "--expected-model", "gpt-6-astra"],
    ["--session-kinds", "spawn", "--model", "gpt-6.2-astra", "--expected-model", "gpt-6-astra", "--account", "other"],
])
def test_invalid_model_modifiers_never_reach_rpc(monkeypatch, flags):
    def forbidden(*args):
        raise AssertionError("invalid model modifiers reached RPC")
    monkeypatch.setattr(cli, "_assistant_call", forbidden)
    args = cli.build_parser().parse_args(["assistant", "authority", *flags])
    with pytest.raises(ValueError):
        args.func(args)
