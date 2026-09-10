#!/usr/bin/env python3
"""Read labeled account-week limits from the user's authenticated Claude /usage UI."""

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
import uuid
from pathlib import Path


def parse_weekly_usage(screen: str) -> dict | None:
    result = {"week_all_pct": None, "week_all_resets": None,
              "week_fable_pct": None, "week_fable_resets": None}
    section = None
    for line in screen.splitlines():
        label = line.casefold()
        if re.search(r"current\s+(week|session)|weekly|all models|sonnet|opus|fable|extra usage", label):
            if ("week" in label or "current" in label) and "all model" in label:
                section = "week_all"
            elif "week" in label and "fable" in label:
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
    # Session usage/cost and unlabeled percentages are not account-week limits.
    return result if result["week_all_pct"] is not None else None


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
            if re.search(r"trust (?:the |these )?files|trust this (?:folder|workspace)|sign in to continue", screen, re.I):
                raise RuntimeError("Claude needs login or workspace trust; complete it interactively first")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="collector-compatible JSON output")
    parser.add_argument("--local-fallback", action="store_true", help="compatibility flag; this probe is always local")
    parser.add_argument("--claude", default=os.environ.get("PENTACLE_USAGE_CLAUDE_BIN", "claude"))
    parser.add_argument("--tmux", default=os.environ.get("PENTACLE_USAGE_TMUX_BIN", "tmux"))
    parser.add_argument("--cwd", default=os.environ.get("PENTACLE_USAGE_CWD", str(Path.cwd())))
    args = parser.parse_args()
    claude, tmux = shutil.which(args.claude), shutil.which(args.tmux)
    if not claude or not tmux:
        parser.exit(1, "Claude and tmux must be installed and available to this process\n")
    try:
        result = collect(claude=claude, tmux=tmux, cwd=args.cwd)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # Avoid echoing provider terminal content, paths or credentials into health UI.
        print(f"Claude usage unavailable ({type(exc).__name__}); check login, trusted cwd and /usage labels", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
