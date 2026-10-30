"""Private relay boundary tests with actual disposable pipe-owning children."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest
from provider_accounts import AccountError
from tools import provider_account_operator_login as tool

URL = "https://auth.openai.com/oauth/authorize?state=fixture-secret&code=fixture-code"


def child_script(url=URL, receipt=None, code=0, delay=0):
    receipt = receipt or {"state": "done", "outcome": "ok"}
    return [sys.executable, "-c", "import os,time,sys; os.write(1," + repr((url + "\n").encode()) +
        "); time.sleep(" + repr(delay) + "); os.write(2," + repr(json.dumps(receipt).encode()) + "); sys.exit(" + str(code) + ")"]


def test_private_relay_delivers_once_and_prints_only_safe_ids(capsys):
    seen = []
    def publish(url, op):
        seen.append((url, op)); return {"publication_id": "pub", "question_id": "prompt"}
    result = tool.relay(child_script(), "op", publish=publish)
    assert seen == [(URL, "op")]
    assert result == {"publication_id": "pub", "question_id": "prompt", "outcome": "ok"}
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("url", ["http://auth.openai.com/", "https://auth.openai.com.evil.test/",
    "https://user@auth.openai.com/", "https://auth.openai.com:443/", "https://evil.test/",
    "https://auth.openai.com/\nsecond", "https://auth.openai.com/#secret", "not-a-url", "x"*8193])
def test_malicious_url_never_publishes_or_leaks(url, capsys):
    seen = []
    with pytest.raises(AccountError): tool.relay(child_script(url), "op", publish=lambda *a: seen.append(a))
    assert not seen and capsys.readouterr() == ("", "")


@pytest.mark.parametrize("case", ["missing-prompt", "failed-publication", "child-failure", "native-failure", "timeout", "no-url"])
def test_failure_masks_child_output_and_reaps_owned_child(case, capsys):
    processes = []
    def popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs); processes.append(process); return process
    def publish(*args):
        if case in {"missing-prompt", "failed-publication"}: raise AccountError("operator_delivery_failed")
        return {"publication_id": "pub", "question_id": "prompt"}
    argv = child_script(code=1 if case == "child-failure" else 0,
        receipt={"state": "done", "outcome": "provider_timeout", "ignored": URL} if case == "native-failure" else None,
        delay=10 if case == "timeout" else 0)
    if case == "no-url": argv = [sys.executable, "-c", "import os;os.write(2,b'{}')"]
    with pytest.raises(AccountError):
        tool.relay(argv, "op", publish=publish, popen=popen, timeout=.1 if case == "timeout" else 10)
    assert len(processes) == 1 and processes[0].poll() is not None
    assert capsys.readouterr() == ("", "")


def test_publish_precedes_action_prompt_and_url_is_absent_from_prompt():
    seen = []
    def command(argv):
        seen.append(argv)
        if argv[:2] == ["assistant", "publish"]:
            return {"type": "assistant.publish.ok", "publication_key": "pub"}
        return {"type": "prompt.ask.ok", "question": {"question_id": "q", "notification_id": "notice"}, "notification":{"surfaced_to_stream_id":"fixture:assistant"}}
    assert tool.publish_url(URL, "op", command=command, verify=lambda: "fixture:assistant") == {"publication_id": "pub", "question_id": "q"}
    assert URL in seen[0] and URL not in " ".join(seen[1])
    assert seen[1][-4:] == ["--option", "Done", "--option", "Not yet"]


@pytest.mark.parametrize("reply", [{}, {"type": "prompt.ask.ok"},
    {"type": "prompt.ask.ok", "question": {"question_id": "q"}, "notice_delivery": {"delivery_status": "failed"}}])
def test_missing_prompt_delivery_refuses(reply):
    def command(argv):
        if argv[0] == "assistant": return {"type": "assistant.publish.ok", "publication_key": "pub"}
        return reply
    with pytest.raises(AccountError): tool.publish_url(URL, "op", command=command, verify=lambda: "fixture:assistant")


def test_remote_contract_uses_policy_paths_hashed_operation_and_private_fd():
    from machines import MachineConfig
    args = SimpleNamespace(host="host-target", provider="codex", account="ours", login_generation="old",
        new_generation="new", operation_id="op")
    policy = {"enabled": False, "executors": {"host-target": {"python": "/usr/bin/python3", "script": "/reviewed/host.py"}}}
    from provider_cli_fixtures import release_pair
    clearance={"outcome":"enrollment_prepared","clearance":"a"*64,"pin":release_pair(),"arguments":vars(args).copy()}
    with pytest.raises(AccountError,match="account_mutation_clearance_required"):
        tool.enrollment_command(args,policy,[MachineConfig(name="host-target",ssh_target="fixture-target")])
    argv = tool.enrollment_command(args, policy, [MachineConfig(name="host-target", ssh_target="fixture-target")],clearance=clearance)
    import shlex
    remote = shlex.split(argv[-1])
    assert remote[:4] == ["exec", "/usr/bin/python3", "-c", tool.REMOTE_RUNNER]
    assert remote[4] == "/reviewed/host.py" and remote[-2:] == ["--operator-browser-fd", "3"]
    assert remote[remote.index("--operation-id")+1] == hashlib.sha256(b"op").hexdigest()
    args.host = "unconfigured"
    with pytest.raises(AccountError): tool.enrollment_command(args, policy, [])


def test_remote_fd_adapter_preserves_only_url_on_stdout(tmp_path):
    # The exact runpy bridge used through SSH, with a harmless counterpart.
    script = tmp_path / "host.py"
    (tmp_path / "counterpart.py").write_text("VALUE = 1")
    script.write_text("import counterpart,os,json;os.write(3,b'" + URL + "\\n');os.close(3);print(json.dumps({'state':'done','outcome':'ok'}))")
    seen = []
    argv = [sys.executable, "-c", tool.REMOTE_RUNNER, str(script)]
    assert tool.relay(argv, "op", publish=lambda url, op: seen.append(url) or {"publication_id":"p", "question_id":"q"})["outcome"] == "ok"
    assert seen == [URL]


@pytest.mark.parametrize("case", ["worker", "stale-generation", "wrong-token", "closed", "missing-token"])
def test_fd_verification_uses_authenticated_binding_and_generation(monkeypatch, case):
    from agent_orch import config, send_boundary, wsclient
    token = "fixture-only-token"
    monkeypatch.setattr(config, "load_config", lambda: object())
    monkeypatch.setattr(wsclient, "_resolved_rpc_from_stream_id", lambda: "fixture:worker" if case == "worker" else "fixture:fd")
    monkeypatch.setattr(send_boundary, "stream_token_from_env", lambda: None if case == "missing-token" else token)
    async def binding(*args, **kwargs): return {"type":"assistant.binding.ok", "stream_id":"fixture:fd", "generation":"g", "composite_stream_id":"fixture:assistant"}
    monkeypatch.setattr(wsclient, "assistant_once", binding)
    row = {"session_generation": "old" if case == "stale-generation" else "g", "status":"closed" if case == "closed" else "open",
        "token_hash": "wrong" if case == "wrong-token" else hashlib.sha256(token.encode()).hexdigest()}
    with pytest.raises(AccountError, match="account_control_unauthorized"):
        tool.verify_front_desk(command=lambda args: {"session":row})


def test_main_masks_untrusted_exception_and_never_starts_for_non_fd(monkeypatch, capsys):
    monkeypatch.setattr(tool, "load_policy", lambda _: {})
    monkeypatch.setattr(tool, "load_machines", lambda: [])
    monkeypatch.setattr(tool, "enrollment_command", lambda *args: [])
    monkeypatch.setattr(tool, "verify_front_desk", lambda: (_ for _ in ()).throw(RuntimeError(URL)))
    monkeypatch.setattr(tool, "relay", lambda *args: pytest.fail("unauthenticated remote mutation"))
    assert tool.main(["--policy", "/fixture", "--host", "host-target", "--provider", "codex", "--account", "ours",
        "--login-generation", "old", "--new-generation", "new", "--operation-id", "op"]) == 1
    output = capsys.readouterr()
    assert output.err == "" and json.loads(output.out) == {"outcome":"operator_login_failed"}


@pytest.mark.parametrize("signal_error", [PermissionError, ProcessLookupError])
def test_exited_child_cleanup_preserves_original_refusal(monkeypatch, capsys, signal_error):
    processes, published = [], []
    refusal = AccountError("operator_delivery_failed")

    def popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        return process

    def publish(*args):
        published.append(args)
        raise refusal

    def exited_group(pid, signum):
        assert len(processes) == 1 and pid == processes[0].pid
        assert signum == signal.SIGKILL
        processes[0].wait(timeout=5)  # Schedule actual exit before the failed group signal.
        raise signal_error()

    monkeypatch.setattr(tool.os, "killpg", exited_group)
    with pytest.raises(AccountError) as caught:
        tool.relay(child_script(), "op", publish=publish, popen=popen)
    assert caught.value is refusal
    assert len(published) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
    assert capsys.readouterr() == ("", "")


def test_denied_live_child_cleanup_keeps_original_and_visible_failure(monkeypatch, capsys):
    processes, original_waits = [], {}
    refusal = AccountError("operator_delivery_failed")

    def popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        real_wait = process.wait
        original_waits[process.pid] = real_wait

        def bounded_wait(timeout=None):
            if process.poll() is None:
                raise subprocess.TimeoutExpired("owned relay child", timeout)
            return real_wait(timeout=timeout)

        monkeypatch.setattr(process, "wait", bounded_wait)
        return process

    def denied_group(pid, signum):
        assert pid == processes[0].pid and processes[0].poll() is None
        raise PermissionError()

    def publish(*args):
        raise refusal

    monkeypatch.setattr(tool.os, "killpg", denied_group)
    try:
        with pytest.raises(AccountError) as caught:
            tool.relay(child_script(delay=30), "op", publish=publish, popen=popen)
        assert caught.value is refusal
        assert any("TimeoutExpired" in note for note in caught.value.__notes__)
        assert processes[0].poll() is None  # Never pretend the denied cleanup reaped it.
        assert processes[0].stdout.closed and processes[0].stderr.closed
        assert capsys.readouterr() == ("", "")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            original_waits[process.pid](timeout=5)


def test_cleanup_failure_without_original_exception_is_not_success(monkeypatch):
    processes = []

    def popen(*args, **kwargs):
        process = subprocess.Popen(*args, **kwargs)
        processes.append(process)
        real_wait = process.wait
        waits = 0

        def wait(timeout=None):
            nonlocal waits
            waits += 1
            if waits == 2:
                raise subprocess.TimeoutExpired("owned relay cleanup", timeout)
            return real_wait(timeout=timeout)

        monkeypatch.setattr(process, "wait", wait)
        return process

    with pytest.raises(subprocess.TimeoutExpired, match="owned relay cleanup"):
        tool.relay(child_script(), "op", publish=lambda *args: {"publication_id": "pub"}, popen=popen)
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed
