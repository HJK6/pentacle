from agent_orch import cli
from test_cli_spawn_resume import RESUME_ID, _install_capture, _resume_args


def test_parent_can_request_native_codex_resume(monkeypatch, tmp_path):
    captured = _install_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda config: "fixture:review-parent")
    args = _resume_args(tmp_path, provider="codex", top_level=True)
    args.account = "primary"

    assert cli.spawn(args) == 0
    payload = captured["payload"]
    assert payload["provider"] == "codex"
    assert payload["account"] == "primary"
    assert payload["resume_session_id"] == RESUME_ID
    assert "parent_stream_id" not in payload

    # Only retries of the same resume should address the same receipt.
    key = payload["idempotency_key"]
    assert cli.spawn(args) == 0
    assert captured["payload"]["idempotency_key"] == key
    args.resume = "11111111-2222-3333-4444-555555555556"
    assert cli.spawn(args) == 0
    assert captured["payload"]["idempotency_key"] != key
