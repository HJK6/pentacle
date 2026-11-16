"""§5.5 — agent-orch ssh subcommand: true ssh(1) passthrough with
SendEnv=PENTACLE_STREAM_ID AGENT_ORCH_STREAM_ID injected.

All tests drive the actual CLI entry point (``cli.main``) and assert
``os.execvp`` was called with the expected argv. The pre-argparse
``sys.argv`` intercept in ``main()`` is what's under test -- exercising the
public CLI surface guarantees that the intercept (not just the helper) does
the right thing.

The implementation MUST use ``argparse.REMAINDER``'s alternative (a pre-
argparse argv slice) because Python's ``argparse.REMAINDER`` stops capturing
on leading flag-like tokens (``-i``, ``-v``, ``-h``); these tests pin the
contract.
"""

from __future__ import annotations

import pytest

from agent_orch import cli


EXPECTED_SEND_ENV = "SendEnv=PENTACLE_STREAM_ID AGENT_ORCH_STREAM_ID AGENT_ORCH_STREAM_TOKEN_FILE AGENT_ORCH_STREAM_TOKEN"


@pytest.fixture
def captured_execvp(monkeypatch):
    """Monkey-patch os.execvp at the cli module's import site so the real
    binary is never exec'd. Returns the captured (file, args) tuple after
    main() is invoked.
    """
    captured: dict = {}

    def fake_execvp(file, args):
        captured["file"] = file
        captured["args"] = list(args)
        # In production execvp does not return; the helper's `return 1` after
        # it is unreachable. We return None here so main() falls through to
        # ssh_passthrough's `return 1`, matching the production unreachable
        # path's documented fallback behavior.
        return None

    monkeypatch.setattr(cli.os, "execvp", fake_execvp)
    return captured


def test_ssh_passthrough_invokes_real_ssh_with_sendenv(captured_execvp) -> None:
    cli.main(["ssh", "hosta", "echo", "ok"])

    assert captured_execvp["file"] == "ssh"
    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "hosta",
        "echo",
        "ok",
    ]


def test_ssh_passthrough_preserves_leading_dash_i_flag(captured_execvp) -> None:
    # The case Python's argparse.REMAINDER fails on: a leading short flag
    # with an argument. The pre-argparse intercept must capture it verbatim.
    cli.main(["ssh", "-i", "/path/key", "host"])

    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "-i",
        "/path/key",
        "host",
    ]


def test_ssh_passthrough_preserves_leading_dash_v_flag(captured_execvp) -> None:
    cli.main(["ssh", "-v", "host"])

    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "-v",
        "host",
    ]


def test_ssh_passthrough_does_not_intercept_dash_h(
    captured_execvp, capsys
) -> None:
    # `agent-orch ssh -h host` must pass `-h host` through to ssh, NOT trigger
    # agent-orch argparse help. The pre-argparse intercept catches `ssh`
    # before any flag-parsing runs; the stub subparser uses add_help=False as
    # a belt-and-suspenders measure.
    cli.main(["ssh", "-h", "host"])

    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "-h",
        "host",
    ]
    # argparse help would print "usage:" to stdout and SystemExit; neither
    # should happen here.
    captured_out = capsys.readouterr()
    assert "usage:" not in captured_out.out
    assert "usage:" not in captured_out.err


def test_ssh_passthrough_preserves_double_dash_delimiter(captured_execvp) -> None:
    # The literal `--` token must survive intact; argparse normally strips it.
    cli.main(["ssh", "--", "host", "cmd"])

    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "--",
        "host",
        "cmd",
    ]


def test_ssh_passthrough_preserves_multiple_flags_in_order(captured_execvp) -> None:
    cli.main(["ssh", "-p", "2222", "-J", "jump.example.com", "host"])

    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "-p",
        "2222",
        "-J",
        "jump.example.com",
        "host",
    ]


def test_ssh_passthrough_arg_order_with_command_after_host(captured_execvp) -> None:
    cli.main(["ssh", "-i", "/key", "host", "sudo", "systemctl", "status", "sshd"])

    assert captured_execvp["args"] == [
        "ssh",
        "-o",
        EXPECTED_SEND_ENV,
        "-i",
        "/key",
        "host",
        "sudo",
        "systemctl",
        "status",
        "sshd",
    ]
