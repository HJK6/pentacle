#!/usr/bin/env python3
"""Read authenticated Claude account-period limits for Pentacle."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_KEYCHAIN_SERVICE = "Claude Code-credentials"
#: Opt-in (default OFF) for the authenticated OAuth usage path. It reads the
#: user's own Claude OAuth token (Keychain on macOS, or CLAUDE_CODE_OAUTH_TOKEN)
#: and makes a network call to the Anthropic usage endpoint. Until a deployment
#: sets this, the probe does NEITHER: no Keychain lookup and no network call, so
#: the default path has no side effect and uses the local CLI /usage screen.
OAUTH_ENABLED_ENV = "PENTACLE_USAGE_CLAUDE_OAUTH"


def oauth_enabled() -> bool:
    return os.environ.get(OAUTH_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_weekly_usage(screen: str) -> dict | None:
    result = {"week_all_pct": None, "week_all_resets": None,
              "week_fable_pct": None, "week_fable_resets": None}
    section = None
    for line in screen.splitlines():
        label = line.casefold()
        if re.search(r"current\s+(week|month|session)|weekly|monthly|all models|sonnet|opus|fable|extra usage", label):
            if ("week" in label or "month" in label) and "all model" in label:
                section = "week_all"
            elif ("week" in label or "month" in label) and "fable" in label:
                section = "week_fable"
            else:
                section = None
        if section is None:
            continue
        match = re.search(r"\b(\d{1,3})%\s+used\b", line, re.I)
        if match:
            value = int(match.group(1))
            if value > 100:
                return None
            result[section + "_pct"] = value
        reset = re.search(r"\bResets\s+(.+)", line, re.I)
        if reset and result[section + "_pct"] is not None:
            result[section + "_resets"] = reset.group(1).strip()
    # Session usage/cost and unlabeled percentages are not account-period limits.
    return result if result["week_all_pct"] is not None else None


def parse_oauth_usage(payload: dict) -> dict | None:
    """Map Enterprise monthly spend onto the stable account-period wire."""
    spend = payload.get("spend")
    if not isinstance(spend, dict) or spend.get("enabled") is False:
        return None
    value = spend.get("percent")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    value = int(value)
    if not 0 <= value <= 100:
        return None
    return {
        "week_all_pct": value,
        "week_all_resets": None,
        "week_fable_pct": None,
        "week_fable_resets": None,
    }


def oauth_token(*, run=subprocess.run) -> str | None:
    """Read Claude OAuth from a protected env or Claude Code's macOS Keychain."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token
    if sys.platform != "darwin":
        return None
    security = shutil.which("security")
    service = os.environ.get(
        "PENTACLE_USAGE_CLAUDE_KEYCHAIN_SERVICE", DEFAULT_KEYCHAIN_SERVICE
    ).strip()
    if not security or not service:
        return None
    try:
        result = run(
            [security, "find-generic-password", "-w", "-s", service],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        credentials = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(credentials, dict):
        return None
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None
    value = oauth.get("accessToken")
    return value.strip() if isinstance(value, str) and value.strip() else None


def collect_oauth(token: str, *, timeout: float = 10,
                  urlopen=urllib.request.urlopen) -> dict | None:
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "pentacle-usage-probe",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return parse_oauth_usage(payload) if isinstance(payload, dict) else None


def collect(*, claude: str, tmux: str, cwd: str, timeout: float = 60,
            run=subprocess.run, sleep=time.sleep, monotonic=time.monotonic) -> dict:
    socket = "pentacle-usage-" + uuid.uuid4().hex
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"

    def call(*args: str, check: bool = True):
        return run([tmux, "-L", socket, *args], env=env, capture_output=True,
                   text=True, check=check, timeout=5)

    deadline = monotonic() + timeout
    try:
        command = shlex.join([claude, "--disallowed-tools", "AskUserQuestion"])
        call("new-session", "-d", "-s", "probe", "-x", "140", "-y", "60", "-c", cwd, command)
        sent = False
        while monotonic() < deadline:
            screen = call("capture-pane", "-p", "-t", "probe", "-S", "-150").stdout
            if re.search(r"sign in to continue|log ?in to continue", screen, re.I):
                raise RuntimeError("Claude CLI is not logged in; run it once interactively and sign in")
            if re.search(r"trust (?:the |these )?files|trust this (?:folder|workspace)", screen, re.I):
                # Never auto-accept the trust dialog (deliberate security contract).
                # ``cwd`` is an operator-configured path (PENTACLE_USAGE_CWD or the
                # default home), never provider terminal capture, so naming it in
                # the error is safe and makes the health banner actionable.
                raise RuntimeError(
                    f"cwd {cwd!r} is not a trusted Claude workspace; trust it once "
                    f"(open the Claude CLI there and accept), or point PENTACLE_USAGE_CWD "
                    f"at an already-trusted folder"
                )
            if not sent and re.search(r"(?m)^\s*[❯>]", screen):
                call("send-keys", "-t", "probe", "-l", "/usage")
                call("send-keys", "-t", "probe", "Enter")
                sent = True
            elif sent:
                result = parse_weekly_usage(screen)
                if result is not None:
                    return result
            sleep(1)
        raise RuntimeError("Claude /usage did not provide labeled weekly account limits within the timeout")
    finally:
        # The random dedicated socket contains only this probe, never user sessions.
        call("kill-server", check=False)


def default_cwd() -> str:
    """Directory the probe launches the Claude CLI in.

    Never inherit the process cwd: under launchd that is ``/``, which is not a
    trusted Claude workspace, so the probe would hit the trust dialog and fail.
    Prefer the explicit ``PENTACLE_USAGE_CWD``; otherwise fall back to the user's
    home as a sane per-user default. The chosen directory must be trusted once
    for the account (the probe never auto-accepts the trust dialog).
    """
    configured = os.environ.get("PENTACLE_USAGE_CWD")
    return configured if configured else str(Path.home())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="collector-compatible JSON output")
    parser.add_argument("--local-fallback", action="store_true", help="compatibility flag; this probe is always local")
    parser.add_argument("--claude", default=os.environ.get("PENTACLE_USAGE_CLAUDE_BIN", "claude"))
    parser.add_argument("--tmux", default=os.environ.get("PENTACLE_USAGE_TMUX_BIN", "tmux"))
    parser.add_argument("--cwd", default=default_cwd())
    args = parser.parse_args()
    # The OAuth path is opt-in. When disabled (the default), do not read the
    # Keychain and do not make the usage network call — fall straight through to
    # the local CLI /usage screen below. The opt-in check runs BEFORE any token
    # lookup or network, so the disabled path has neither side effect.
    if oauth_enabled():
        token = oauth_token()
        if token:
            try:
                result = collect_oauth(token)
                if result is not None:
                    print(json.dumps(result))
                    return 0
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
                # Older account types and restricted networks still use the
                # authenticated CLI screen below. Never surface private HTTP data.
                pass
    claude, tmux = shutil.which(args.claude), shutil.which(args.tmux)
    if not claude or not tmux:
        parser.exit(1, "Claude and tmux must be installed and available to this process\n")
    try:
        result = collect(claude=claude, tmux=tmux, cwd=args.cwd)
    except RuntimeError as exc:
        # collect() raises only our own sanitized strings (never provider terminal
        # capture); surfacing them — including the operator-configured cwd — makes
        # the health banner actionable instead of an opaque "(RuntimeError)".
        print(f"Claude usage unavailable: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        # These can carry system paths/errno detail; keep the generic sanitized line.
        print(f"Claude usage unavailable ({type(exc).__name__}); check login, trusted cwd and /usage labels", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
