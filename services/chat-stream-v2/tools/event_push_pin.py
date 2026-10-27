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
from machines import configured_host_names, load_machines  # noqa: E402
from deploy.deploy import DeployError, _load_gate_evidence  # noqa: E402


SATELLITE_HOSTS = configured_host_names("PENTACLE_SATELLITE_HOSTS", remote_only=True)
OPTIONAL_SATELLITE_HOSTS = tuple(machine.name for machine in load_machines() if machine.smoke_optional)
SATELLITE_READBACK_INTERVAL_SECONDS = 2
SATELLITE_READBACK_DEADLINE_SECONDS = 420


def _require_gate_evidence(path: Path | None, candidate: str) -> None:
    if path is None:
        raise ValueError("--gate-evidence is required for a nonempty release target")
    _load_gate_evidence(path, candidate)


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


def _preflight_participants(path: Path | None, candidate: str) -> list[dict]:
    """Consume the FD's exact, fresh host/interpreter/import/compatibility receipt.

    This operation never provisions or SSH-mutates hosts. Dependencies and the
    installed consumer replay must be prepared before the single pin changes.
    """
    import re
    if path is None:
        raise ValueError('--satellite-preflight is required for a nonempty target')
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError('satellite preflight receipt unreadable') from exc
    stamp = payload.get('observed_at_epoch')
    rows = payload.get('participants')
    if (payload.get('candidate') != candidate or type(stamp) not in (int, float)
            or not 0 <= time.time() - stamp <= 900
            or not isinstance(rows, list) or not rows
            or len({row.get('host') for row in rows if isinstance(row, dict)}) != len(rows)):
        raise ValueError('satellite preflight candidate, freshness or participants invalid')
    if {row['host'] for row in rows} != set(SATELLITE_HOSTS):
        raise ValueError('every configured satellite must be explicitly required or optional')
    for row in rows:
        if (not isinstance(row, dict) or row.get('host') not in SATELLITE_HOSTS
                or type(row.get('required')) is not bool or type(row.get('online')) is not bool
                or (not row['required'] and row['host'] not in OPTIONAL_SATELLITE_HOSTS)):
            raise ValueError('explicit configured required/optional participants are required')
        if not row['online'] and not row['required']:
            continue  # retained as pending/partial, never a green offline exemption
        root = row.get('checkout')
        if (not row['online'] or not isinstance(root, str) or not Path(root).is_absolute()
                or row.get('script') != root + '/services/chat-stream-v2/satellite.py'
                or row.get('interpreter') != root + '/.venv/bin/python'
                or not isinstance(row.get('python_version'), str)
                or row['python_version'].split('.')[:2] not in (['3', '12'], ['3', '13'])
                or row.get('imports_passed') is not True or row.get('compatibility_passed') is not True
                or row.get('recovery_ready') is not True
                or not isinstance(row.get('previous_incarnation'), str) or not row['previous_incarnation']
                or not isinstance(row.get('installed_source_sha'), str)
                or re.fullmatch('[0-9a-f]{40}', row['installed_source_sha']) is None
                or any(not isinstance(row.get(key), str) or re.fullmatch('[0-9a-f]{64}', row[key]) is None
                       for key in ('configuration_sha256', 'installed_cli_sha256'))
                or not isinstance(row.get('dependency_digest'), str)
                or re.fullmatch('[0-9a-f]{64}', row['dependency_digest']) is None
                or not isinstance(row.get('installed_helpers_sha'), str)
                or re.fullmatch('[0-9a-f]{40}', row['installed_helpers_sha']) is None
                or not isinstance(row.get('consumer_payload_sha256'), str)
                or re.fullmatch('[0-9a-f]{64}', row['consumer_payload_sha256']) is None
                or not row.get('installed_cli_version')):
            raise ValueError('incomplete dedicated checkout/dependency/consumer preflight for ' + row['host'])
    if not any(row['required'] for row in rows):
        raise ValueError('at least one required participant is required')
    return rows


async def _observe(store, participants, target, since, deadline, *, useful):
    last = {}
    while True:
        for participant in participants:
            host = participant['host']
            raw = await store.get(f'event_push.runtime.{host}')
            try:
                row = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                row = {}
            if not isinstance(row, dict):
                row = {}
            row['process_state'] = 'connected' if row.get('observed_at') else 'not_observed'
            stamp = row.get('observed_at_epoch')
            ready = (participant['online'] and row.get('sha') == target
                and row.get('admitted') is True and bool(row.get('incarnation'))
                and row['incarnation'] != participant.get('previous_incarnation')
                and type(stamp) in (int, float) and since <= stamp <= time.time() + 5)
            if useful:
                event_stamp = row.get('useful_observed_at_epoch')
                ready = (ready and type(row.get('useful_events')) is int and row['useful_events'] > 0
                    and type(event_stamp) in (int, float) and event_stamp >= since
                    and row.get('recovery_pending') == 0 and row.get('recovery_gaps') == [])
            last[host] = {**row, 'ready': bool(ready), 'required': participant['required']}
        if all(last[row['host']]['ready'] for row in participants if row['required']):
            return last
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError('satellite runtime activation deadline: target=' + target
                               + ' last_observed=' + json.dumps(last, sort_keys=True))
        await asyncio.sleep(min(SATELLITE_READBACK_INTERVAL_SECONDS, remaining))


async def _run(
    db: str, *, stage: str | None, rollback: bool, gate_evidence: Path | None = None,
    satellite_preflight: Path | None = None, url: str = 'ws://127.0.0.1:7791',
) -> dict[str, object]:
    if not rollback and not SATELLITE_HOSTS:
        raise ValueError('no satellite hosts configured; refusing to change the target pin')
    store = Store(db)
    store.start()
    try:
        target = await store.get('event_push.target_sha.previous') if rollback else stage
        if target:
            _require_gate_evidence(gate_evidence, target)
            participants = _preflight_participants(satellite_preflight, target)
        elif not rollback:
            raise ValueError('a nonempty exact target is required')
        since = time.time()
        if rollback:
            restored = await store.rollback_event_push_target_sha()
            if not target:
                return {'action': 'rollback', 'target_sha': restored,
                        'readback': await store.get('event_push.target_sha')}
            previous = None
        else:
            previous = await store.stage_event_push_target_sha(target)
        deadline = asyncio.get_running_loop().time() + SATELLITE_READBACK_DEADLINE_SECONDS
        booted = await _observe(store, participants, target, since, deadline, useful=False)
        quota = []
        smoke_results = {}
        for participant in participants:
            host = participant['host']
            if not booted[host]['ready']:
                continue
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError('satellite deadline before affected Codex canary')
            # Existing single-host Codex canary only; never the default fleet matrix.
            smoke = subprocess.run(
                [sys.executable, str(SERVICE_DIR / 'tools' / 'spawn_fleet_smoke.py'), url, host],
                cwd=REPO_ROOT, text=True, capture_output=True, check=False,
            )
            notes = _smoke_quota_note(smoke)
            quota.extend(notes)
            smoke_results[host] = json.loads(smoke.stdout)
        # A quota-limited canary has no useful journey proof. Keep it partial,
        # without spending another deadline waiting for events it cannot produce.
        # The existing canary owns its protocol deadlines and finally cleanup;
        # never kill it at the tail of a satellite checkout deadline.
        deadline = asyncio.get_running_loop().time() + SATELLITE_READBACK_DEADLINE_SECONDS
        observed = booted if quota else await _observe(store, participants, target, since, deadline, useful=True)
        pending = [row['host'] for row in participants if not observed[row['host']]['ready']]
        partial = bool(quota or pending)
        if quota:
            print('pin staged; partial: Codex quota exhausted; restore quota before validation', file=sys.stderr)
        return {'action': 'rollback' if rollback else 'stage', 'previous': previous,
                'target_sha': target, 'readback': await store.get('event_push.target_sha'),
                'status': 'PARTIAL' if partial else 'PASS', 'satellites': observed,
                'pending': pending, 'quota_exhausted': quota, 'smoke': smoke_results}
    finally:
        store.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the deployed v2 sessions.db")
    parser.add_argument("--gate-evidence", type=Path,
                        help="canonical local merge evidence for the exact stage or rollback SHA")
    parser.add_argument('--satellite-preflight', type=Path,
                        help='fresh exact candidate host/dependency/consumer receipt')
    parser.add_argument('--url', default='ws://127.0.0.1:7791', help='affected Codex canary endpoint')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", metavar="SHA", help="exact deployed 40-character SHA")
    action.add_argument("--rollback", action="store_true", help="restore the captured preceding pin")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args.db, stage=args.stage, rollback=args.rollback,
                                  gate_evidence=args.gate_evidence,
                                  satellite_preflight=args.satellite_preflight, url=args.url))
    except (DeployError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("status") == "PARTIAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
