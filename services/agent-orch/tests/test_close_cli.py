"""Unit coverage for the direct-connect ``agent-orch close`` path.

``close()`` connects straight to chat_streamd via ``close_once`` (mirroring
``report()``). These tests pin that contract: the resolved
stream_id / operator_confirm / from_stream_id / caller_stream_id reach
``close_once``, the exit code maps off the daemon ``close.*`` response, and the
old wrapper socket path is gone.

This suite covers direct local connection behavior.
"""

from __future__ import annotations

from argparse import Namespace
import json

import pytest

from agent_orch import cli
from agent_orch.config import Config


def _close_args(**overrides):
    data = {
        "stream_id": "hostb:codex-x",
        "reason": "manual",
        "operator_confirm": False,
        "force": False,
        "from_stream_id": None,
        "caller_stream_id": None,
        "progeny": None,
        "disposition_waived_reason": None,
        "timeout": 5.0,
    }
    data.update(overrides)
    return Namespace(**data)


@pytest.fixture
def patched_close(monkeypatch, tmp_path):
    """Patch load_config + leader discovery and capture close_once calls.

    Returns a list the test can inspect; the close_once stub's response is
    swappable via the returned ``set_response`` hook.
    """
    calls: list[dict] = []
    state = {"response": {"type": "close.ok", "request_id": "close-1"}, "raises": None}

    async def fake_close_once(
        config,
        stream_id,
        *,
        reason="report_terminate",
        timeout=30.0,
        operator_confirm=False,
        force=False,
        defer_if_working=False,
        from_stream_id=None,
        caller_stream_id=None,
        progeny_stream_id=None,
        disposition_waived_reason=None,
    ):
        calls.append(
            {
                "stream_id": stream_id,
                "reason": reason,
                "timeout": timeout,
                "operator_confirm": operator_confirm,
                "force": force,
                "defer_if_working": defer_if_working,
                "from_stream_id": from_stream_id,
                "caller_stream_id": caller_stream_id,
                "progeny_stream_id": progeny_stream_id,
                "disposition_waived_reason": disposition_waived_reason,
            }
        )
        if state["raises"] is not None:
            raise state["raises"]
        return state["response"]

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-x")
    monkeypatch.setattr(cli, "close_once", fake_close_once)

    class Handle:
        def __init__(self):
            self.calls = calls

        def set_response(self, response):
            state["response"] = response

        def set_raises(self, exc):
            state["raises"] = exc

    return Handle()


def test_self_close_drives_close_once_with_operator_confirm(patched_close, capsys):
    # `agent-orch close --operator-confirm $AGENT_ORCH_STREAM_ID` from a
    # wrapper-less worker: discovery resolves the caller to its own stream, so
    # caller == target == from_stream_id and the daemon authorizes self-close.
    rc = cli.close(_close_args(operator_confirm=True))
    assert rc == 0
    assert len(patched_close.calls) == 1
    call = patched_close.calls[0]
    assert call["stream_id"] == "hostb:codex-x"
    assert call["operator_confirm"] is True
    assert call["force"] is False
    assert call["from_stream_id"] == "hostb:codex-x"
    assert call["caller_stream_id"] == "hostb:codex-x"
    assert call["progeny_stream_id"] is None
    assert call["disposition_waived_reason"] is None
    assert call["reason"] == "manual"
    assert call["timeout"] == 5.0
    assert json.loads(capsys.readouterr().out)["type"] == "close.ok"


def test_close_passes_disposition_waiver_reason(patched_close):
    rc = cli.close(_close_args(disposition_waived_reason="tracked elsewhere"))
    assert rc == 0
    assert patched_close.calls[0]["disposition_waived_reason"] == "tracked elsewhere"


def test_close_already_closed_is_success(patched_close, capsys):
    patched_close.set_response(
        {"type": "close.already_closed", "request_id": "close-1", "already_closed": True}
    )
    rc = cli.close(_close_args())
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["type"] == "close.already_closed"


def test_leader_driven_close_passes_explicit_caller(patched_close):
    # AC-4: a leader closes a child. Explicit --caller-stream-id is the parent;
    # from_stream_id defaults to it so the daemon's parent-close auth passes.
    rc = cli.close(
        _close_args(
            stream_id="hostb:codex-child",
            operator_confirm=True,
            caller_stream_id="hostb:leader",
        )
    )
    assert rc == 0
    call = patched_close.calls[0]
    assert call["stream_id"] == "hostb:codex-child"
    assert call["caller_stream_id"] == "hostb:leader"
    assert call["from_stream_id"] == "hostb:leader"


def test_force_flag_reaches_close_once(patched_close):
    rc = cli.close(
        _close_args(
            stream_id="hostb:codex-child",
            operator_confirm=True,
            caller_stream_id="hostb:leader",
            force=True,
        )
    )

    assert rc == 0
    assert patched_close.calls[0]["force"] is True


def test_defer_if_working_flag_reaches_close_once(patched_close):
    rc = cli.close(
        _close_args(
            stream_id="hostb:codex-child",
            operator_confirm=True,
            caller_stream_id="hostb:leader",
            defer_if_working=True,
        )
    )

    assert rc == 0
    assert patched_close.calls[0]["defer_if_working"] is True


def test_explicit_from_stream_id_takes_precedence(patched_close):
    rc = cli.close(
        _close_args(
            operator_confirm=True,
            from_stream_id="hostb:override",
            caller_stream_id="hostb:caller",
        )
    )
    assert rc == 0
    call = patched_close.calls[0]
    assert call["caller_stream_id"] == "hostb:caller"
    assert call["from_stream_id"] == "hostb:override"


def test_progeny_flag_reaches_close_once(patched_close):
    rc = cli.close(_close_args(operator_confirm=True, progeny="hostb:successor"))
    assert rc == 0
    assert patched_close.calls[0]["progeny_stream_id"] == "hostb:successor"


def test_close_error_response_exits_nonzero(patched_close, capsys):
    # AC-5: daemon refusal (operator_session_refused) surfaces as nonzero.
    patched_close.set_response(
        {"type": "close.error", "request_id": "close-1", "error": "operator_session_refused"}
    )
    rc = cli.close(_close_args(operator_confirm=True))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["type"] == "close.error"
    assert out["error"] == "operator_session_refused"


def test_close_timeout_exit_code(patched_close, capsys):
    patched_close.set_raises(TimeoutError("close_timeout"))
    rc = cli.close(_close_args())
    assert rc == 67
    assert "timeout" in capsys.readouterr().err


def test_close_auth_failure_exit_code(patched_close, capsys):
    patched_close.set_raises(PermissionError("auth.error"))
    rc = cli.close(_close_args())
    assert rc == 66
    assert "auth failed" in capsys.readouterr().err


def test_close_unreachable_exit_code(patched_close, capsys):
    patched_close.set_raises(ConnectionRefusedError("connection refused"))
    rc = cli.close(_close_args())
    assert rc == 64
    assert "unreachable" in capsys.readouterr().err


def test_close_invalid_stream_id_exits_2(monkeypatch, tmp_path, capsys):
    # Exercise the real close_once validation (no colon → invalid_stream_id)
    # so we cover the CLI's ValueError mapping end-to-end.
    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: None)
    rc = cli.close(_close_args(stream_id="no-colon-here"))
    assert rc == 2
    assert "validation failed" in capsys.readouterr().err
