"""Run one frozen task with one model in a fresh headless session."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import protocol, scoring

HERE = os.path.dirname(os.path.abspath(__file__))

MODELS = {
    "luna": {"provider": "codex", "model": "gpt-6-luna", "effort": "max"},
    "haiku": {"provider": "claude", "model": "claude-haiku-5-5", "effort": "high"},
}
DEFAULT_TIMEOUT_S = 1800


def build_argv(key, worktree, last_message_path, run_dir):
    """Explicit model/effort/cwd binding for each CLI; the prompt goes on stdin.

    Both CLIs get the run directory (call log, shim) as an extra writable root and
    may run shell commands; Codex runs them without a login shell so the fake CLI
    stays first on PATH.
    """
    spec = MODELS[key]
    if key == "luna":
        argv = ["codex", "exec", "-m", spec["model"],
                "-c", f"model_reasoning_effort={spec['effort']}", "-c", "allow_login_shell=false",
                "-s", "workspace-write", "--add-dir", run_dir, "--skip-git-repo-check",
                "-C", worktree,
                "--json", "-o", last_message_path, "-"]
    else:
        argv = ["claude", "-p", "--model", spec["model"], "--effort", spec["effort"],
                "--permission-mode", "acceptEdits", "--allowedTools=Bash", "--add-dir", worktree, "--add-dir", run_dir,
                "--max-turns", "60", "--output-format", "stream-json", "--verbose"]
    return ["nice", "-n", "10"] + argv


def make_fake_bin(root, log_path):
    """A directory holding an `agent-orch` shim that runs fake_agent_orch.py."""
    bin_dir = os.path.join(root, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    shim = os.path.join(bin_dir, "agent-orch")
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write(f'#!/bin/sh\nexec "{sys.executable}" "{os.path.join(HERE, "fake_agent_orch.py")}" "$@"\n')
    os.chmod(shim, 0o755)
    open(log_path, "a").close()
    return bin_dir


def run_env(bin_dir, log_path, base=None):
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name.startswith("AGENT_ORCH") or name in ("TMUX", "TMUX_PANE"):
            del env[name]
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["EVAL_AGENT_ORCH_LOG"] = log_path
    env["AGENT_ORCH_STREAM_ID"] = "eval:run"
    return env


def parse_usage(key, stdout_text):
    """Extract (effective_model, tokens_in, tokens_out, usd_or_unavailable, raw_usage)."""
    effective, usage, usd = None, None, scoring.UNAVAILABLE
    for line in stdout_text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if key == "haiku" and event.get("type") == "result":
            usage = event.get("usage") or usage
            models = list((event.get("modelUsage") or {}).keys())
            effective = models[0] if models else effective
            if event.get("total_cost_usd") is not None:
                usd = event["total_cost_usd"]
        elif key == "luna" and event.get("type") == "turn.completed":
            usage = event.get("usage") or usage
    if not usage:
        return effective, None, None, usd, None
    if key == "haiku":
        t_in = sum(usage.get(k, 0) or 0 for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        t_out = usage.get("output_tokens", 0) or 0
    else:
        t_in, t_out = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return effective, t_in, t_out, usd, usage


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_session(key, brief, worktree, run_dir, timeout_s=DEFAULT_TIMEOUT_S, rates=None):
    """Run the model on `brief` in `worktree`; return the raw run record.

    The record stores the requested tuple, the effective tuple reported by the
    CLI, the actual cwd and the full argv. Scoring is a separate step.
    """
    os.makedirs(run_dir, exist_ok=True)
    log_path = os.path.join(run_dir, "agent_orch.jsonl")
    last_path = os.path.join(run_dir, "last_message.txt")
    bin_dir = make_fake_bin(run_dir, log_path)
    argv = build_argv(key, worktree, last_path, run_dir)
    out_path, err_path = (os.path.join(run_dir, n) for n in ("stdout.jsonl", "stderr.txt"))
    started, reason, code = time.time(), None, None
    with open(out_path, "w") as out, open(err_path, "w") as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=worktree,
                                    env=run_env(bin_dir, log_path), start_new_session=True, text=True)
        except OSError as exc:
            proc, reason = None, f"launch error: {exc}"
        if proc is not None:
            try:
                proc.communicate(brief, timeout=timeout_s)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.wait()
                reason = f"timeout after {timeout_s}s"
    wall_s = round(time.time() - started, 1)
    with open(out_path, encoding="utf-8", errors="replace") as fh:
        effective, t_in, t_out, usd, raw = parse_usage(key, fh.read())
    if key == "luna" and raw and rates:
        usd = scoring.luna_cost(raw, rates)
    spec = MODELS[key]
    if reason is None and code not in (0, None):
        reason = f"exit code {code}"
    return {
        "model": key,
        "requested": {"model": spec["model"], "effort": spec["effort"]},
        # Neither CLI reports the effective effort; it is recorded as unavailable, not assumed.
        "effective": {"model": effective or (spec["model"] if key == "luna" and raw else None),
                      "effort": scoring.UNAVAILABLE},
        "cwd": worktree, "argv": argv, "exit_code": code, "wall_s": wall_s,
        "tokens_in": t_in, "tokens_out": t_out, "usd_api_equiv": usd, "usage": raw,
        "failure_reason": reason, "log": log_path, "stdout": out_path, "stderr": err_path,
    }


def new_worktree(repo, sha, parent):
    """Fresh detached worktree at `sha` under `parent`; returns its path."""
    path = tempfile.mkdtemp(prefix="wt-", dir=parent)
    os.rmdir(path)
    subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", path, sha],
                   check=True, capture_output=True, text=True)
    return path


def remove_worktree(repo, path):
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", path],
                   capture_output=True, text=True)
    shutil.rmtree(path, ignore_errors=True)
