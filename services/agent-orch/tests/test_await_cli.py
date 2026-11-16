"""Unit coverage for the close-aware stream ``agent-orch await``.

Pins the CLI surface of the close-aware await:
- ``--msg-id`` is OPTIONAL on the ``await`` subparser; omitting it selects
  "stream mode" (resolve on the stream's terminal report for any msg_id, or on
  close) and ``msg_id`` parses to ``None``.
- the ``--timeout`` default is mode-aware: 30s with ``--msg-id`` (back-compat),
  large (``AWAIT_STREAM_MODE_DEFAULT_TIMEOUT``) in stream mode; an explicit
  ``--timeout`` always wins.
- ``await_command`` propagates the optional msg_id (incl. ``None``) to
  ``await_report_once`` and maps the response (report / closed_without_report).

The tests exercise the public completion contract and its timeout modes.
"""

from __future__ import annotations

import json

import pytest

from agent_orch import cli
from agent_orch.config import Config


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_await_flag_form_without_msg_id_is_stream_mode_with_large_default_timeout():
    parsed = cli.build_parser().parse_args(["await", "--from", "hostb:codex-x"])
    assert parsed.command == "await"
    assert parsed.stream_id == "hostb:codex-x"
    assert parsed.msg_id is None
    assert parsed.timeout == cli.AWAIT_STREAM_MODE_DEFAULT_TIMEOUT
    assert parsed.timeout > cli.AWAIT_MSG_ID_MODE_DEFAULT_TIMEOUT


def test_await_flag_form_with_msg_id_keeps_30s_default_timeout():
    parsed = cli.build_parser().parse_args(["await", "--from", "hostb:codex-x", "--msg-id", "7"])
    assert parsed.stream_id == "hostb:codex-x"
    assert parsed.msg_id == 7
    assert parsed.timeout == cli.AWAIT_MSG_ID_MODE_DEFAULT_TIMEOUT


def test_await_explicit_timeout_wins_in_stream_mode():
    parsed = cli.build_parser().parse_args(["await", "--from", "hostb:codex-x", "--timeout", "120"])
    assert parsed.msg_id is None
    assert parsed.timeout == 120.0


def test_await_explicit_timeout_wins_in_msg_id_mode():
    parsed = cli.build_parser().parse_args(
        ["await", "--from", "hostb:codex-x", "--msg-id", "7", "--timeout", "5"]
    )
    assert parsed.msg_id == 7
    assert parsed.timeout == 5.0


def test_await_legacy_positional_form_still_requires_msg_id():
    parsed = cli.build_parser().parse_args(["await", "hostb:codex-x", "7"])
    assert parsed.stream_id == "hostb:codex-x"
    assert parsed.msg_id == 7
    assert parsed.timeout == cli.AWAIT_MSG_ID_MODE_DEFAULT_TIMEOUT


def test_await_msg_id_without_from_is_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["await", "--msg-id", "7"])


def test_await_legacy_and_flag_forms_cannot_mix():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["await", "hostb:codex-x", "7", "--from", "hostb:other"])


def test_await_bare_invocation_is_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["await"])


# ---------------------------------------------------------------------------
# await_command → await_report_once propagation + response mapping
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_await(monkeypatch, tmp_path):
    calls: list[dict] = []
    state = {"response": {"type": "await_report.ok", "ok": True, "report_id": "r1"}}

    async def fake_await_report_once(
        config,
        stream_id,
        msg_id,
        *,
        timeout=30.0,
        include_details=False,
        include_extras=False,
        **_kw,
    ):
        calls.append(
            {
                "stream_id": stream_id,
                "msg_id": msg_id,
                "timeout": timeout,
                "include_details": include_details,
                "include_extras": include_extras,
            }
        )
        return state["response"]

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "await_report_once", fake_await_report_once)

    class Handle:
        def __init__(self):
            self.calls = calls

        def set_response(self, response):
            state["response"] = response

    return Handle()


def test_await_command_stream_mode_propagates_none_msg_id_and_large_timeout(patched_await, capsys):
    args = cli.build_parser().parse_args(["await", "--from", "hostb:codex-x"])
    rc = cli.await_command(args)
    assert rc == 0
    assert len(patched_await.calls) == 1
    call = patched_await.calls[0]
    assert call["stream_id"] == "hostb:codex-x"
    assert call["msg_id"] is None
    assert call["timeout"] == cli.AWAIT_STREAM_MODE_DEFAULT_TIMEOUT
    assert json.loads(capsys.readouterr().out)["report_id"] == "r1"


def test_await_command_msg_id_mode_propagates_int_msg_id(patched_await):
    args = cli.build_parser().parse_args(["await", "--from", "hostb:codex-x", "--msg-id", "9"])
    rc = cli.await_command(args)
    assert rc == 0
    call = patched_await.calls[0]
    assert call["msg_id"] == 9
    assert call["timeout"] == cli.AWAIT_MSG_ID_MODE_DEFAULT_TIMEOUT


def test_await_command_prints_closed_without_report_and_exits_69(patched_await, capsys):
    patched_await.set_response(
        {
            "type": "await_report.closed_without_report",
            "ok": False,
            "stream_id": "hostb:codex-x",
            "error": "closed_without_report",
            "reason": "closed_without_report",
            "existing_report": {
                "type": "completion.report",
                "report_id": "synth-cwr-hostb:codex-x-0",
                "from_stream_id": "hostb:codex-x",
                "msg_id": 0,
                "status": "aborted",
                "synthesis_kind": "closed_without_report",
                "lower_trust": True,
            },
        }
    )
    args = cli.build_parser().parse_args(["await", "--from", "hostb:codex-x"])
    rc = cli.await_command(args)
    assert rc == 69
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["type"] == "await_report.closed_without_report"
    assert payload["existing_report"]["synthesis_kind"] == "closed_without_report"
    assert payload["existing_report"]["lower_trust"] is True
    assert "closed without a terminal report" in captured.err
