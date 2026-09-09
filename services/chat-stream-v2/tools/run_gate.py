#!/usr/bin/env python3
"""Run one v2 gate tier and write exact-SHA machine-readable evidence."""
from __future__ import annotations
import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


try:
    from tools.gate_owner_manifest import MANIFEST_ENV, ManifestError, initialize_manifest, mark_owner_stopping, reap_manifest
except ModuleNotFoundError:  # direct execution from the tools directory
    from gate_owner_manifest import MANIFEST_ENV, ManifestError, initialize_manifest, mark_owner_stopping, reap_manifest  # type: ignore[no-redef]


SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
PRE_FLIGHT = SERVICE_DIR / "tools" / "gate_preflight.sh"
DEFAULT_TIMEOUTS = {"unit": 900.0, "smoke": 900.0, "soak": 3900.0}
SOAK_PRESETS = {
    "short": {"SOAK_DURATION_S": "180", "SOAK_FLEET_CORE": "6", "SOAK_FLEET_CHURN": "2", "SOAK_OFFLINE": "3", "SOAK_DRIVERS": "6", "SOAK_CPU_IDLE_CEILING_PCT": "15.0", "SOAK_RPC_P95_S": "2.0", "SOAK_RESTART": "1"},
    "full": {"SOAK_DURATION_S": "1800", "SOAK_FLEET_CORE": "24", "SOAK_FLEET_CHURN": "6", "SOAK_OFFLINE": "6", "SOAK_DRIVERS": "10", "SOAK_CPU_IDLE_CEILING_PCT": "15.0", "SOAK_RPC_P95_S": "2.0", "SOAK_RESTART": "1"},
}
_ACTIVE_MANIFEST_PATH: Path | None = None
_ACTIVE_RUN_ID: str | None = None
_TERMINATION_REQUESTED = False


def _int_attr(node: ET.Element, name: str) -> int:
    try:
        return int(node.attrib.get(name, "0"))
    except (TypeError, ValueError):
        return 0
def _junit_counts(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return {"parse_error": str(exc)}
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    result: dict[str, Any] = {name: sum(_int_attr(node, name) for node in suites) for name in ("tests", "failures", "errors", "skipped")}
    try:
        result["duration_s"] = sum(float(node.attrib.get("time", "0")) for node in suites)
    except (TypeError, ValueError):
        result["duration_s"] = None
    return result


def _sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
def _git_status() -> str:
    return subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=REPO_ROOT, text=True)
def _python_bin() -> str:
    return os.environ.get("V2_PYTHON_BIN", sys.executable)


def _effective_soak_parameters(env: dict[str, str]) -> dict[str, str]:
    tier = env.get("SOAK_TIER", "full")
    if tier not in SOAK_PRESETS:
        raise ValueError(f"unsupported SOAK_TIER={tier!r}; expected short or full")
    values = dict(SOAK_PRESETS[tier])
    values.update({name: env[name] for name in values if name in env})
    return {"SOAK_TIER": tier, **values}
def _apply_strict_full_soak(env: dict[str, str]) -> None:
    if env.get("V2_SOAK_STRICT_FULL") == "1":
        env.update({"SOAK_TIER": "full", **SOAK_PRESETS["full"]})
def _tier_command(tier: str, junit: Path, basetemp: Path) -> list[str]:
    args = {
        "unit": ["tests", "--ignore=tests/smoke", "--ignore=tests/soak", "-m", "not soak", "-q", "-rs"],
        "smoke": ["tests/smoke", "-q", "-rs", "--maxfail=1"],
        "soak": ["tests/soak", "-m", "soak", "-q", "-s", "-rs"],
    }.get(tier)
    if args is None:
        raise ValueError(f"unknown tier: {tier}")
    return [_python_bin(), "-m", "pytest", *args, f"--junitxml={junit}", "--basetemp", str(basetemp / tier)]
def _set_parent_death_signal() -> None:
    if sys.platform != "linux":
        return
    parent = os.getppid()
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)
def _process_group_popen_kwargs() -> dict[str, object]:
    return {"start_new_session": os.name == "posix", "preexec_fn": _set_parent_death_signal if sys.platform == "linux" else None}
def _process_group_id(proc: subprocess.Popen[str]) -> int | None:
    if os.name != "posix":
        return None
    try:
        return os.getpgid(proc.pid)
    except (PermissionError, ProcessLookupError):
        return None
def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals, *, pgid: int | None = None) -> None:
    if os.name == "posix":
        group = pgid if pgid is not None else _process_group_id(proc)
        if group is not None:
            try:
                os.killpg(group, sig)
                return
            except (PermissionError, ProcessLookupError):
                pass
    if proc.poll() is None:
        (proc.terminate() if sig == signal.SIGTERM else proc.kill())
def _process_group_exists(pgid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
def terminate_process_group(proc: subprocess.Popen[str], *, timeout: float = 5.0) -> None:
    pgid = _process_group_id(proc)
    _signal_process_group(proc, signal.SIGTERM, pgid=pgid)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    if pgid is not None and _process_group_exists(pgid):
        _signal_process_group(proc, signal.SIGKILL, pgid=pgid)
    if proc.poll() is None:
        proc.wait(timeout=timeout)
def _mark_active() -> bool:
    return bool(_ACTIVE_MANIFEST_PATH and mark_owner_stopping(_ACTIVE_MANIFEST_PATH))
def _owner_signal_handler(_signum: int, _frame: object) -> None:
    global _TERMINATION_REQUESTED
    _TERMINATION_REQUESTED = True
    _mark_active()
    raise KeyboardInterrupt
def _copy_output(proc: subprocess.Popen[str], path: Path) -> None:
    assert proc.stdout is not None
    with path.open("w", encoding="utf-8") as log:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
def _run_process(command: list[str], *, env: dict[str, str], log_path: Path, timeout: float) -> tuple[int, bool]:
    global _TERMINATION_REQUESTED
    proc = subprocess.Popen(command, cwd=SERVICE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **_process_group_popen_kwargs())
    threading.Thread(target=_copy_output, args=(proc, log_path), daemon=True).start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except KeyboardInterrupt:
        _mark_active()
        terminate_process_group(proc)
        code = 143
    except subprocess.TimeoutExpired:
        _TERMINATION_REQUESTED = True
        _mark_active()
        terminate_process_group(proc)
        timed_out, code = True, 124
    return code, timed_out
def _preflight(env: dict[str, str], log: Path) -> tuple[int, bool]:
    print(f"[v2-gate] preflight: {PRE_FLIGHT}")
    return _run_process([str(PRE_FLIGHT)], env=env, log_path=log, timeout=30)
def _result(tier: str, *, code: int, passed: bool, reason: str | None = None, command: list[str] | None = None, timed_out: bool = False, preflight: int | None = None, junit: Path | None = None, log: Path | None = None, counts: dict[str, Any] | None = None, soak: dict[str, str] | None = None) -> dict[str, Any]:
    return {"tier": tier, "command": command or [], "returncode": code, "timed_out": timed_out, "preflight_returncode": preflight, "junit": str(junit) if junit else None, "log": str(log) if log else None, "counts": counts, "soak_parameters": soak, "passed": passed, "reason": reason}
def _run_tier(tier: str, evidence: Path, timeout: float, *, basetemp: Path, manifest: Path) -> dict[str, Any]:
    junit, log = evidence / f"{tier}.junit.xml", evidence / f"{tier}.log"
    junit.unlink(missing_ok=True)
    env, soak = os.environ.copy(), None
    env[MANIFEST_ENV] = str(manifest)
    if tier == "soak":
        env["SOAK_TIER"] = os.environ.get("SOAK_TIER", "full")
        _apply_strict_full_soak(env)
        try:
            soak = _effective_soak_parameters(env)
        except ValueError as exc:
            return _result(tier, code=2, passed=False, reason="invalid_soak_tier", junit=junit, log=log, soak={"error": str(exc)})
    if tier in {"smoke", "soak"}:
        if shutil.which("tmux") is None:
            return _result(tier, code=127, passed=False, reason="tmux_missing", preflight=127, junit=junit, log=log, soak=soak)
        preflight, preflight_timeout = _preflight(env, evidence / f"{tier}.preflight.log")
        if preflight != 0:
            return _result(tier, code=preflight, passed=False, reason="preflight_failed", timed_out=preflight_timeout, preflight=preflight, command=[str(PRE_FLIGHT)], junit=junit, log=log, soak=soak)
    command = _tier_command(tier, junit, basetemp)
    print(f"[v2-gate] {tier}: {' '.join(command)}")
    code, timed_out = _run_process(command, env=env, log_path=log, timeout=timeout)
    counts, reason = _junit_counts(junit), None
    passed = code == 0 and not timed_out and counts is not None
    if counts is None:
        passed, reason = False, "missing_junit"
    elif "parse_error" in counts:
        passed, reason = False, "invalid_junit"
    elif counts["tests"] == 0:
        passed, reason = False, "no_tests_collected"
    elif counts["failures"] or counts["errors"]:
        passed, reason = False, "test_failures"
    elif tier == "soak" and counts["skipped"]:
        passed, reason = False, "soak_skipped"
    elif timed_out:
        passed, reason = False, "timeout"
    elif _TERMINATION_REQUESTED:
        passed, reason = False, "terminated"
    elif code != 0:
        passed, reason = False, "pytest_failed"
    return _result(tier, code=code, passed=passed, reason=reason, command=command, timed_out=timed_out, preflight=preflight if tier in {"smoke", "soak"} else None, junit=junit, log=log, counts=counts, soak=soak)
def _write_evidence(gate: str, evidence: Path, output: Path | None, sha: str, before: str, after_sha: str, after: str, tiers: list[dict[str, Any]]) -> Path:
    data = {"schema": "pentacle.v2.gate-evidence.v1", "gate": gate, "sha": sha, "source": {"sha_before": sha, "sha_after": after_sha, "clean_before": not before, "clean_after": not after, "status_before": before, "status_after": after}, "generated_at": datetime.now(timezone.utc).isoformat(), "repo_root": str(REPO_ROOT), "service_dir": str(SERVICE_DIR), "tiers": tiers, "passed": all(bool(item["passed"]) for item in tiers)}
    path = output or evidence / f"v2-{gate}-evidence.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
def main() -> int:
    global _ACTIVE_MANIFEST_PATH, _ACTIVE_RUN_ID, _TERMINATION_REQUESTED
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=("unit", "smoke", "merge", "soak"))
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--evidence-out", type=Path)
    parser.add_argument("--basetemp", type=Path, help="gate base temp directory and owner-manifest home")
    parser.add_argument("--timeout-seconds", type=float)
    args = parser.parse_args()
    evidence = args.evidence_dir or Path(os.environ.get("V2_GATE_EVIDENCE_DIR") or tempfile.mkdtemp(prefix="pentacle-v2-gate-"))
    evidence.mkdir(parents=True, exist_ok=True)
    basetemp = args.basetemp or evidence / f"pytest-{args.gate}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
    basetemp.mkdir(parents=True, exist_ok=True)
    manifest = basetemp / ".owned.json"
    tiers = ("unit", "smoke") if args.gate == "merge" else ("soak",) if args.gate == "soak" else (args.gate,)
    sha, before, results = _sha(), _git_status(), []
    _TERMINATION_REQUESTED = False
    old_handlers, ready = {}, False
    try:
        reap_manifest(manifest)
        _ACTIVE_RUN_ID = initialize_manifest(manifest)
        _ACTIVE_MANIFEST_PATH, ready = manifest, True
        if os.name == "posix":
            for signum in (signal.SIGTERM, signal.SIGHUP):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _owner_signal_handler)
        if before:
            results.append(_result("source_integrity", code=125, passed=False, reason="dirty_worktree", command=["git", "status", "--porcelain", "--untracked-files=all"]))
        else:
            for tier in tiers:
                if _TERMINATION_REQUESTED:
                    break
                limit = args.timeout_seconds if args.timeout_seconds is not None else float(os.environ.get(f"V2_{tier.upper()}_TIMEOUT_S", DEFAULT_TIMEOUTS[tier]))
                results.append(_run_tier(tier, evidence, limit, basetemp=basetemp, manifest=manifest))
    except KeyboardInterrupt:
        _TERMINATION_REQUESTED = True
        results.append(_result("owner", code=143, passed=False, reason="terminated"))
    except ManifestError as exc:
        results.append(_result("owner", code=125, passed=False, reason=f"manifest_error:{exc}"))
    finally:
        if ready:
            mark_owner_stopping(manifest)
            reap_manifest(manifest, _ACTIVE_RUN_ID)
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
        _ACTIVE_MANIFEST_PATH = _ACTIVE_RUN_ID = None
    after_sha, after = _sha(), _git_status()
    if after_sha != sha or (not before and after):
        results.append(_result("source_integrity", code=125, passed=False, reason="source_changed_during_gate", command=["git", "rev-parse", "HEAD"]))
    output = _write_evidence(args.gate, evidence, args.evidence_out, sha, before, after_sha, after, results)
    for item in results:
        counts = item.get("counts") or {}
        print(f"[v2-gate] {item['tier']} {'PASS' if item['passed'] else 'FAIL'} rc={item['returncode']} tests={counts.get('tests', 'n/a')} failures={counts.get('failures', 'n/a')} errors={counts.get('errors', 'n/a')}")
    print(f"[v2-gate] evidence={output}")
    return 0 if all(bool(item["passed"]) for item in results) else 1
if __name__ == "__main__":
    raise SystemExit(main())
