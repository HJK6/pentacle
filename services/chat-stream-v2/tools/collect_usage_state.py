#!/usr/bin/env python3
"""Run the external usage CLIs once and persist their combined state."""

from __future__ import annotations

import argparse
from contextlib import closing
import fcntl
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usage_collector import UsageStateCollector, bounded_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--shared-scripts",
        type=Path,
        # Resolve relative to this file, not the process CWD. Under launchd the
        # collector runs with CWD="/", which turned a CWD-relative default into
        # "/scripts/check_claude_usage.py" and broke every usage probe.
        default=Path(__file__).resolve().parents[3] / "scripts",
    )
    parser.add_argument("--skip-codex", action="store_true", help="collect only Claude; preserve prior Codex state without probing")
    parser.add_argument("--skip-claude", action="store_true", help="collect only Codex; preserve prior Claude/Fable state without probing")
    parser.add_argument("--account-ledger", type=Path,
                        help="central collector: resolve the healthy host Codex default from this ledger each tick")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json",
                        help="Claude Code config whose cached utilization feeds usage_history.jsonl")
    args = parser.parse_args(argv)
    # One invocation at a time: a probe still running from the previous tick makes this
    # tick skip (exit 0) instead of stacking a second CLI session on the account.
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with open(args.state.with_name(args.state.name + ".lock"), "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("usage collector tick skipped: previous run still in progress", file=sys.stderr)
            return 0
        _collect(args)
    return 0


def _codex_default_home(ledger: Path) -> str:
    # Import only inside the Codex probe boundary: an unavailable account reader
    # must not prevent the independent Claude observation or state write.
    from account_store import AccountStore

    host = os.environ.get("PENTACLE_HOST_ID")
    if not host:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent-orch"))
        from agent_orch.config import local_host_id
        host = local_host_id()
    with closing(sqlite3.connect(ledger.absolute().as_uri() + "?mode=ro", uri=True, timeout=1.0)) as conn:
        conn.execute("PRAGMA query_only=1")
        home = AccountStore(conn, {}, initialize=False).codex_default_home(host=host)
    directory = Path(home)
    if not directory.is_absolute() or not directory.is_dir() or not os.access(directory, os.R_OK | os.X_OK):
        raise ValueError("codex_default_home_unavailable")
    return home


def _default_home_run(ledger: Path, codex_command: tuple[str, ...]):
    def run(command, **kwargs):
        if command == codex_command:
            try:
                home = _codex_default_home(ledger)
            except Exception:
                # SQL/import/path errors can contain private data. The collector's
                # existing provider failure path retains LKG without freshening it.
                raise RuntimeError("codex_default_home_unavailable") from None
            kwargs["env"] = dict(kwargs.get("env") or {}, CODEX_HOME=home)
        return bounded_run(command, **kwargs)
    return run


def _collect(args: argparse.Namespace) -> None:
    codex_command = ((sys.executable, "-c", 'print(\'{"status":"no_update"}\')') if args.skip_codex
                     else (sys.executable, str(args.shared_scripts / "check_codex_usage.py"), "--json"))
    options = {}
    if args.account_ledger is not None and not args.skip_codex:
        options["run"] = _default_home_run(args.account_ledger, codex_command)
    UsageStateCollector(
        state_path=args.state,
        claude_command=((sys.executable, "-c", 'print(\'{"status":"no_update"}\')') if args.skip_claude
                        else (sys.executable, str(args.shared_scripts / "check_claude_usage.py"), "--local-fallback", "--json")),
        codex_command=codex_command,
        claude_config_path=args.claude_config,
        **options,
    ).run_once()


if __name__ == "__main__":
    raise SystemExit(main())
