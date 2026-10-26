#!/usr/bin/env python3
"""FD-only private URL relay for one independently authorized Codex enrollment.

The caller arranges callback routing and operator readiness before invoking this
command. No URL, provider output or credential is written to a file or terminal.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from account_runtime import load_policy
from machines import load_machines, ssh_command
from provider_account_host import authorization_url
from provider_accounts import AccountError, _component, policy_manages


# SSH has one stdout channel: preserve it only as fd 3 in the host helper.
# Safe receipt output uses SSH stderr, which this runner consumes without echo.
REMOTE_RUNNER = ("import os,runpy,sys; "
    "os.dup2(1,3,inheritable=False); os.dup2(2,1); "
    "sys.argv=sys.argv[1:]; sys.path.insert(0,os.path.dirname(sys.argv[0])); "
    "runpy.run_path(sys.argv[0],run_name='__main__')")


def _command(arguments, *, run=subprocess.run):
    try:
        result = run(["agent-orch", *arguments], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=30, start_new_session=True)
        if result.returncode or len(result.stdout) > 65536: raise ValueError()
        value = json.loads(result.stdout)
        if not isinstance(value, dict): raise ValueError()
        return value
    except Exception: raise AccountError("operator_delivery_failed") from None


def verify_front_desk(*, command=_command):
    from agent_orch.config import load_config
    from agent_orch.send_boundary import stream_token_from_env
    from agent_orch.wsclient import assistant_once, _resolved_rpc_from_stream_id
    actor, token = _resolved_rpc_from_stream_id(), stream_token_from_env()
    if not actor or not token: raise AccountError("account_control_unauthorized")
    try:
        binding = asyncio.run(assistant_once(load_config(), {"type": "assistant.binding"}))
        current = command(["inspect", actor, "--json"])
        row = current.get("session") or current
        if (binding.get("type") != "assistant.binding.ok" or binding.get("stream_id") != actor
                or not binding.get("generation") or row.get("status") != "open"
                or row.get("session_generation") != binding["generation"]
                or row.get("token_hash") != hashlib.sha256(token.encode()).hexdigest()):
            raise ValueError()
        composite = binding.get("composite_stream_id")
        if not isinstance(composite, str) or not composite.endswith(":assistant") or not composite[:-10]:
            raise ValueError()
        return composite
    except Exception: raise AccountError("account_control_unauthorized") from None


def publish_url(url, operation_id, *, command=_command, verify=verify_front_desk):
    authorization_url(url)
    composite = verify()
    published = command(["assistant", "publish", "--proactive", "--request-id", operation_id + "-url",
        "--composite-stream-id", composite, "--publish-kind", "prose", "--message", url])
    if published.get("type") != "assistant.publish.ok" or not published.get("publication_key"):
        raise AccountError("operator_delivery_failed")
    prompt = command(["prompt", "ask", "--title", "Complete Codex authorization",
        "--body", "Open the private authorization link just posted in this chat and complete sign-in in your routed browser. Select Done after authorization; Not yet leaves this step pending. Native completion determines login success.",
        "--question-id", operation_id + "-login", "--option", "Done", "--option", "Not yet"])
    question_id = (prompt.get("question") or {}).get("question_id")
    delivery = prompt.get("notice_delivery") or {}
    notification = prompt.get("notification") or {}
    if (prompt.get("type") != "prompt.ask.ok" or not question_id
            or not (prompt.get("question") or {}).get("notification_id")
            or notification.get("surfaced_to_stream_id") != composite
            or delivery.get("delivery_status") in {"failed", "not_submitted", "not_delivered"}):
        raise AccountError("operator_prompt_delivery_failed")
    return dict(publication_id=published["publication_key"], question_id=question_id)


def relay(argv, operation_id, *, publish=publish_url, popen=subprocess.Popen, timeout=320):
    """Own the SSH child and bounded in-memory pipes until native completion."""
    process = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, close_fds=True)
    result, url_bytes, receipt_bytes = {}, bytearray(), bytearray()
    sent = False
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "url")
        selector.register(process.stderr, selectors.EVENT_READ, "receipt")
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise AccountError("provider_timeout")
            for key, _ in selector.select(min(remaining, 1)):
                data = os.read(key.fileobj.fileno(), 16384)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "receipt":
                    receipt_bytes.extend(data)
                    if len(receipt_bytes) > 65536: raise AccountError("account_host_response_invalid")
                    continue
                if sent: raise AccountError("account_browser_url_invalid")
                url_bytes.extend(data)
                if len(url_bytes) > 8193: raise AccountError("account_browser_url_invalid")
                if b"\n" in url_bytes:
                    if url_bytes.count(b"\n") != 1 or not url_bytes.endswith(b"\n"):
                        raise AccountError("account_browser_url_invalid")
                    try: url = authorization_url(url_bytes[:-1].decode("ascii"))
                    except (UnicodeError, ValueError): raise AccountError("account_browser_url_invalid") from None
                    result = publish(url, operation_id)
                    sent = True
                    url_bytes.clear()
        code = process.wait(timeout=max(.01, deadline - time.monotonic()))
        try: receipt = json.loads(receipt_bytes.decode())
        except (UnicodeError, ValueError): raise AccountError("account_host_response_invalid") from None
        if code or not sent or receipt.get("state") != "done" or receipt.get("outcome") != "ok":
            raise AccountError("account_enrollment_failed")
        return result | {"outcome": "ok"}
    finally:
        selector.close()
        # This group contains only the child SSH process, never a fleet seat.
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()


def enrollment_command(args, policy, machines):
    for value in (args.host, args.account, args.login_generation, args.new_generation): _component(value)
    if (args.provider != "codex" or args.login_generation == "active"
            or args.new_generation in {"active", args.login_generation}
            or not isinstance(args.operation_id, str) or not 1 <= len(args.operation_id) <= 160
            or any(ord(c) < 33 or ord(c) > 126 for c in args.operation_id)
            or not policy_manages(dict(policy, enabled=True), args.host, args.provider)):
        raise AccountError("account_upgrade_request_invalid")
    configured = policy.get("executors", {}).get(args.host)
    machine = next((m for m in machines if m.name == args.host), None)
    if not configured or not machine or not machine.ssh_target:
        raise AccountError("account_host_executor_unavailable")
    if any(not isinstance(configured.get(k), str) or not Path(configured[k]).is_absolute() for k in ("python", "script")):
        raise AccountError("account_host_executor_unavailable")
    remote = [configured["python"], "-c", REMOTE_RUNNER, configured["script"], "enroll",
        "--provider", args.provider, "--account", args.account, "--login-generation", args.login_generation,
        "--operation-id", hashlib.sha256(args.operation_id.encode()).hexdigest(),
        "--new-generation", args.new_generation, "--operator-browser-fd", "3"]
    return ssh_command(machine.ssh_target, "exec " + shlex.join(remote), multiplex=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("policy", "host", "provider", "account", "login-generation", "new-generation", "operation-id"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        command = enrollment_command(args, load_policy(args.policy), load_machines())
        verify_front_desk()
        result = relay(command, args.operation_id)
        print(json.dumps(result))
        return 0
    except AccountError as exc:
        print(json.dumps({"outcome": str(exc)})); return 1
    except Exception:
        print(json.dumps({"outcome": "operator_login_failed"})); return 1


if __name__ == "__main__": raise SystemExit(main())
