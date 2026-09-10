#!/usr/bin/env python3
"""Perform exactly one Nexus-gated v2 event.push pin-window action.

This is deliberately not a deployer: it neither checks out code nor restarts a
daemon or satellite.  Invoke it only inside an already-authorized, serialized
deploy/rollback window, after recording the target SHA and database path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from store import Store  # noqa: E402
from machines import configured_host_names  # noqa: E402


SATELLITE_HOSTS = configured_host_names("PENTACLE_SATELLITE_HOSTS", remote_only=True)
SATELLITE_READBACK_INTERVAL_SECONDS = 2
SATELLITE_READBACK_DEADLINE_SECONDS = 90


def _require_gate_passed_sha(candidate: str) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "merge_gate.py"), "verify-tag", "--candidate", candidate],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "candidate has no verified v2-gate tag")


def _smoke_quota_note(smoke: subprocess.CompletedProcess[str]) -> list[dict]:
    """Accept a real PASS or a quota-only UNTESTED result; reject other outcomes."""
    try:
        payload = json.loads(smoke.stdout)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        failures = payload.get("failures")
        untested = payload.get("untested")
        passed = (
            smoke.returncode == 0 and payload.get("status") == "PASS"
            and payload.get("ok") is True and failures == [] and untested == []
        )
        quota_only = (
            smoke.returncode == 2 and payload.get("status") == "UNTESTED"
            and payload.get("ok") is False and failures == []
            and isinstance(untested, list) and bool(untested)
            and all(isinstance(row, dict) and row.get("class") == "untested"
                    and row.get("reason") == "quota_exhausted" for row in untested)
        )
        if passed or quota_only:
            return untested if quota_only else []
    detail = smoke.stderr.strip() or smoke.stdout.strip() or "missing smoke result"
    raise RuntimeError(f"post-deploy spawn smoke failed: {detail}")


async def _run(db: str, *, stage: str | None, rollback: bool) -> dict[str, object]:
    if not rollback and not SATELLITE_HOSTS:
        raise ValueError("no satellite hosts configured; refusing to change the target pin")
    store = Store(db)
    store.start()
    try:
        if rollback:
            previous = await store.get("event_push.target_sha.previous")
            if previous is not None:
                _require_gate_passed_sha(previous)
            restored = await store.rollback_event_push_target_sha()
            return {
                "action": "rollback",
                "target_sha": restored,
                "readback": await store.get("event_push.target_sha"),
            }
        assert stage is not None
        readback_started_at = time.time()
        previous = await store.stage_event_push_target_sha(stage)
        deadline = asyncio.get_running_loop().time() + SATELLITE_READBACK_DEADLINE_SECONDS
        last_observed: dict[str, object] = {}
        while True:
            for host in SATELLITE_HOSTS:
                raw = await store.get(f"event_push.runtime.{host}")
                try:
                    observed = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    observed = {}
                last_observed[host] = {
                    "sha": observed.get("sha"),
                    "process_state": "connected" if observed.get("observed_at") else "not_observed",
                    "pid": observed.get("pid"),
                    "observed_at": observed.get("observed_at"),
                    "observed_at_epoch": observed.get("observed_at_epoch"),
                }
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining >= 0 and all(
                last_observed[host]["sha"] == stage
                and isinstance(last_observed[host]["observed_at_epoch"], (int, float))
                and last_observed[host]["observed_at_epoch"] >= readback_started_at
                for host in SATELLITE_HOSTS
            ):
                break
            if remaining <= 0:
                raise RuntimeError(
                    f"satellite runtime SHA readback deadline: target={stage} last_observed="
                    + json.dumps(last_observed, sort_keys=True)
                )
            await asyncio.sleep(min(SATELLITE_READBACK_INTERVAL_SECONDS, remaining))
        smoke = subprocess.run(
            [sys.executable, str(SERVICE_DIR / "tools" / "spawn_fleet_smoke.py")],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        quota = _smoke_quota_note(smoke)
        if quota:
            print(f"pin OK; fleet smoke non-code note: codex quota exhausted {json.dumps(quota, sort_keys=True)}; restore quota or wait for reset", file=sys.stderr)
        return {
            "action": "stage",
            "previous": previous,
            "target_sha": stage,
            "readback": await store.get("event_push.target_sha"),
            "satellites": last_observed,
            "quota_exhausted": quota,
        }
    finally:
        store.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the deployed v2 sessions.db")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", metavar="SHA", help="exact deployed 40-character SHA")
    action.add_argument("--rollback", action="store_true", help="restore the captured preceding pin")
    args = parser.parse_args(argv)
    if args.stage is not None:
        try:
            _require_gate_passed_sha(args.stage)
        except RuntimeError as exc:
            parser.error(str(exc))
    print(json.dumps(asyncio.run(_run(args.db, stage=args.stage, rollback=args.rollback)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
