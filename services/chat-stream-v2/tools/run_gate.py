#!/usr/bin/env python3
"""Run one v2 gate tier and write exact-SHA machine-readable evidence."""
from __future__ import annotations
import argparse
import json
import math
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
    from tools.gate_progress import summarize as summarize_progress
    from tools.source_integrity import status as source_integrity_status
except ModuleNotFoundError:  # direct execution from the tools directory
    from gate_owner_manifest import MANIFEST_ENV, ManifestError, initialize_manifest, mark_owner_stopping, reap_manifest  # type: ignore[no-redef]
    from gate_progress import summarize as summarize_progress  # type: ignore[no-redef]
    from source_integrity import status as source_integrity_status  # type: ignore[no-redef]


SERVICE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_DIR.parents[1]
PRE_FLIGHT = SERVICE_DIR / "tools" / "gate_preflight.sh"
DEFAULT_TIMEOUTS = {"unit": 900.0, "smoke": 900.0, "soak": 3900.0}
# Component bounds for the --deadline-seconds envelope. The reserve covers this
# runner's own terminate (2x5s), reader drain (READER_JOIN_S), reap and evidence writes.
PREFLIGHT_BOUND_S = 30.0
USAGE_BOUND_S = 30.0
DEFAULT_RESERVE_S = 60.0
READER_JOIN_S = 10.0
LOG_MAX_BYTES = 64 * 1024 * 1024
SOAK_PRESETS = {
    "short": {"SOAK_DURATION_S": "180", "SOAK_FLEET_CORE": "6", "SOAK_FLEET_CHURN": "2", "SOAK_OFFLINE": "3", "SOAK_DRIVERS": "6", "SOAK_CPU_IDLE_CEILING_PCT": "15.0", "SOAK_RPC_P95_S": "2.0", "SOAK_RESTART": "1"},
    "full": {"SOAK_DURATION_S": "1800", "SOAK_FLEET_CORE": "24", "SOAK_FLEET_CHURN": "6", "SOAK_OFFLINE": "6", "SOAK_DRIVERS": "10", "SOAK_CPU_IDLE_CEILING_PCT": "15.0", "SOAK_RPC_P95_S": "2.0", "SOAK_RESTART": "1"},
}
_ACTIVE_MANIFEST_PATH: Path | None = None
_ACTIVE_RUN_ID: str | None = None
_TERMINATION_REQUESTED = False
_TERMINATION_SIGNAL: str | None = None
# Facts about the most recent _run_process call; read by _run_tier.
_LAST_PROCESS: dict[str, Any] = {}


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
    return source_integrity_status(REPO_ROOT)
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
def _owner_signal_handler(signum: int, _frame: object) -> None:
    global _TERMINATION_REQUESTED, _TERMINATION_SIGNAL
    _TERMINATION_REQUESTED = True
    _TERMINATION_SIGNAL = signal.Signals(signum).name
    _mark_active()
    raise KeyboardInterrupt
def _copy_output(proc: subprocess.Popen[str], path: Path, facts: dict[str, Any]) -> None:
    """Drain the subject's output to its log, bounded; never stop draining on a write error."""
    assert proc.stdout is not None
    log = None
    try:
        log = path.open("w", encoding="utf-8", errors="replace")
    except OSError as exc:
        facts["log_error"] = type(exc).__name__
    for line in proc.stdout:
        size = len(line.encode("utf-8", "replace"))
        facts["bytes"] += size
        if facts["echo_error"] is None:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError) as exc:
                facts["echo_error"] = type(exc).__name__
        if log is None:
            facts["dropped_bytes"] += size
            continue
        if facts["logged_bytes"] + size > LOG_MAX_BYTES:
            facts["truncated"] = True
            facts["dropped_bytes"] += size
            continue
        try:
            log.write(line)
            log.flush()
            facts["logged_bytes"] += size
        except OSError as exc:
            facts["log_error"] = type(exc).__name__
            facts["dropped_bytes"] += size
            try:
                log.close()
            except OSError:
                pass
            log = None
    if log is not None:
        try:
            log.close()
        except OSError as exc:
            facts["log_error"] = type(exc).__name__
def _run_process(command: list[str], *, env: dict[str, str], log_path: Path, timeout: float) -> tuple[int, bool]:
    global _TERMINATION_REQUESTED
    output: dict[str, Any] = {"bytes": 0, "logged_bytes": 0, "dropped_bytes": 0, "truncated": False, "log_error": None, "echo_error": None, "reader_joined": False}
    _LAST_PROCESS.clear()
    _LAST_PROCESS.update(output=output, termination=None, subprocess_returncode=None)
    proc = subprocess.Popen(command, cwd=SERVICE_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, **_process_group_popen_kwargs())
    reader = threading.Thread(target=_copy_output, args=(proc, log_path, output), daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout)
    except KeyboardInterrupt:
        _mark_active()
        terminate_process_group(proc)
        _LAST_PROCESS["termination"] = {"kind": "external_signal", "signal": _TERMINATION_SIGNAL or "SIGINT"}
        code = 143
    except subprocess.TimeoutExpired:
        _TERMINATION_REQUESTED = True
        _mark_active()
        terminate_process_group(proc)
        _LAST_PROCESS["termination"] = {"kind": "internal_timeout", "signal": None, "bound_s": timeout}
        timed_out, code = True, 124
    _LAST_PROCESS["subprocess_returncode"] = proc.returncode
    reader.join(READER_JOIN_S)
    output["reader_joined"] = not reader.is_alive()
    return code, timed_out
def _preflight(env: dict[str, str], log: Path) -> tuple[int, bool]:
    print(f"[v2-gate] preflight: {PRE_FLIGHT}")
    return _run_process([str(PRE_FLIGHT)], env=env, log_path=log, timeout=PREFLIGHT_BOUND_S)
def _result(tier: str, *, code: int, passed: bool, reason: str | None = None, command: list[str] | None = None, timed_out: bool = False, preflight: int | None = None, junit: Path | None = None, log: Path | None = None, counts: dict[str, Any] | None = None, soak: dict[str, str] | None = None, **facts: Any) -> dict[str, Any]:
    # reason is the primary outcome; secondary_outcomes keeps reporting/cleanup facts it must not erase.
    return {"tier": tier, "command": command or [], "returncode": code, "timed_out": timed_out, "preflight_returncode": preflight, "junit": str(junit) if junit else None, "log": str(log) if log else None, "counts": counts, "soak_parameters": soak, "passed": passed, "reason": reason, "primary_outcome": reason, "secondary_outcomes": [], **facts}
def _classify(*, code: int, timed_out: bool, terminated: bool, counts: dict[str, Any] | None, tier: str, progress: dict[str, Any], output: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Primary outcome first by what happened to the subject, then reporting facts as secondaries."""
    reporting: list[str] = []
    if counts is None:
        reporting.append("missing_junit")
    elif "parse_error" in counts:
        reporting.append("invalid_junit")
    if not progress.get("present"):
        reporting.append("journal_missing")
    elif not progress.get("complete"):
        reporting.append("journal_incomplete")
    if output.get("truncated"):
        reporting.append("output_truncated")
    if output.get("log_error"):
        reporting.append("log_write_failed")
    if output and not output.get("reader_joined", True):
        reporting.append("output_reader_not_joined")
    primary: str | None = None
    if timed_out:
        primary = "timeout"
    elif terminated:
        primary = "terminated"
    elif counts is not None and "parse_error" not in counts:
        if counts["tests"] == 0:
            primary = "no_tests_collected"
        elif counts["failures"] or counts["errors"]:
            primary = "test_failures"
        elif tier == "soak" and counts["skipped"]:
            primary = "soak_skipped"
    if primary is None and reporting[:1] and reporting[0] in {"missing_junit", "invalid_junit"}:
        primary = reporting.pop(0)
    if primary is None and code != 0:
        primary = "pytest_failed"
    if primary is None and reporting:
        # The subject looked green but its evidence is partial: never a green verdict.
        primary = "evidence_incomplete"
    return primary, reporting
def _run_tier(tier: str, evidence: Path, timeout: float, *, basetemp: Path, manifest: Path) -> dict[str, Any]:
    if "TMUX" in os.environ:
        return _result(tier, code=2, passed=False, reason="ambient_tmux")
    junit, log = evidence / f"{tier}.junit.xml", evidence / f"{tier}.log"
    journal = evidence / f"{tier}.progress.jsonl"
    junit.unlink(missing_ok=True)
    journal.unlink(missing_ok=True)
    env, soak = os.environ.copy(), None
    env[MANIFEST_ENV] = str(manifest)
    # The fleet gate is strict: a capability a managed test requires must be present, never a green skip.
    # Only a sandbox that truly lacks tmux/ps/lsof may opt out by exporting PENTACLE_TEST_STRICT_CAPABILITIES=0;
    # never a fleet, merge or deploy gate, and never CI (see docs/TEST_CAPABILITIES.md).
    env.setdefault("PENTACLE_TEST_STRICT_CAPABILITIES", "1")
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
    command += ["-p", "tools.gate_progress", f"--gate-progress-journal={journal}", f"--gate-progress-run-id={_ACTIVE_RUN_ID or ''}:{tier}"]
    print(f"[v2-gate] {tier}: {' '.join(command)}")
    _LAST_PROCESS.clear()
    code, timed_out = _run_process(command, env=env, log_path=log, timeout=timeout)
    facts = dict(_LAST_PROCESS)
    raw = facts.get("subprocess_returncode")
    if facts.get("termination") is None and isinstance(raw, int) and raw < 0:
        # Killed by a signal this runner did not send (for example the OOM killer).
        facts["termination"] = {"kind": "subject_signal", "signal": signal.Signals(-raw).name if -raw in signal.Signals._value2member_map_ else str(-raw)}
    counts, progress = _junit_counts(junit), summarize_progress(journal)
    terminated = not timed_out and (_TERMINATION_REQUESTED or (facts.get("termination") or {}).get("kind") == "external_signal")
    reason, secondary = _classify(code=code, timed_out=timed_out, terminated=terminated, counts=counts, tier=tier, progress=progress, output=facts.get("output") or {})
    result = _result(tier, code=code, passed=reason is None, reason=reason, command=command, timed_out=timed_out, preflight=preflight if tier in {"smoke", "soak"} else None, junit=junit, log=log, counts=counts, soak=soak,
                     bound_s=timeout, subprocess_returncode=facts.get("subprocess_returncode"), termination=facts.get("termination"), output=facts.get("output"), progress=progress)
    result["secondary_outcomes"] = secondary
    return result


def _run_usage_manifest(manifest: Path, evidence: Path, deployment_contract: Path) -> dict[str, Any]:
    """Validate the candidate-bound usage receipt as part of a merge gate."""
    command = [
        sys.executable,
        str(SERVICE_DIR / "tools" / "usage_manifest.py"),
        "validate",
        "--manifest",
        str(manifest.resolve()),
        "--deployment-contract",
        str(deployment_contract.resolve()),
    ]
    log = evidence / "usage-manifest.log"
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        output = completed.stdout + completed.stderr
        log.write_text(output, encoding="utf-8")
        return _result(
            "usage_manifest",
            code=completed.returncode,
            passed=completed.returncode == 0,
            reason=None if completed.returncode == 0 else "usage_manifest_invalid",
            command=command,
            log=log,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        log.write_text(output, encoding="utf-8")
        return _result(
            "usage_manifest",
            code=124,
            passed=False,
            reason="usage_manifest_timeout",
            command=command,
            timed_out=True,
            log=log,
        )


def _write_evidence(gate: str, evidence: Path, output: Path | None, sha: str | None, before: str | None, after_sha: str | None, after: str | None, tiers: list[dict[str, Any]], *, finished: bool = True, deadline: dict[str, Any] | None = None) -> Path:
    passed = finished and bool(tiers) and all(bool(item["passed"]) for item in tiers)
    data = {"schema": "pentacle.v2.gate-evidence.v1", "gate": gate, "sha": sha, "source": {"sha_before": sha, "sha_after": after_sha, "clean_before": None if before is None else not before, "clean_after": None if after is None else not after, "status_before": before, "status_after": after}, "generated_at": datetime.now(timezone.utc).isoformat(), "repo_root": str(REPO_ROOT), "service_dir": str(SERVICE_DIR), "tiers": tiers, "finished": finished, "deadline": deadline, "passed": passed}
    path = output or evidence / f"v2-{gate}-evidence.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace: a crash mid-write leaves the previous (started, unfinished) record intact.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
def _positive_finite(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be finite and > 0: {value!r}")
    return number
def _envelope(remaining: list[tuple[str, float]], *, usage_pending: bool, reserve: float) -> dict[str, Any]:
    """Component bounds the remaining work may consume; no aggregate is invented beyond their sum."""
    tiers = {tier: limit for tier, limit in remaining}
    preflight = {tier: PREFLIGHT_BOUND_S for tier, _ in remaining if tier in {"smoke", "soak"}}
    usage = USAGE_BOUND_S if usage_pending else 0.0
    return {"tier_bounds_s": tiers, "preflight_bounds_s": preflight, "usage_bound_s": usage, "reserve_s": reserve,
            "required_s": sum(tiers.values()) + sum(preflight.values()) + usage + reserve}
def _deadline_check(deadline: dict[str, Any] | None, clock_start: float, stage: str, remaining: list[tuple[str, float]], *, usage_pending: bool) -> dict[str, Any] | None:
    """Return a refusal result when the remaining deadline cannot cover the remaining bounds."""
    if deadline is None:
        return None
    left = deadline["deadline_s"] - (time.monotonic() - clock_start)
    envelope = _envelope(remaining, usage_pending=usage_pending, reserve=deadline["reserve_s"])
    check = {"stage": stage, "remaining_s": round(left, 3), **envelope, "ok": left >= envelope["required_s"]}
    deadline["checks"].append(check)
    if check["ok"]:
        return None
    return _result(stage, code=2, passed=False, reason="deadline_envelope_exceeded", deadline_check=check)
def main() -> int:
    global _ACTIVE_MANIFEST_PATH, _ACTIVE_RUN_ID, _TERMINATION_REQUESTED, _TERMINATION_SIGNAL
    clock_start = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=("unit", "smoke", "merge", "soak"))
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--evidence-out", type=Path)
    parser.add_argument("--basetemp", type=Path, help="gate base temp directory and owner-manifest home")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--deadline-seconds", type=_positive_finite,
                        help="total time this runner may use; refuse before launch or a tier that cannot fit")
    parser.add_argument("--deadline-reserve-seconds", type=_positive_finite, default=DEFAULT_RESERVE_S,
                        help="frozen cleanup/evidence reserve held back from the deadline")
    parser.add_argument(
        "--usage-manifest",
        type=Path,
        help="candidate-bound usage manifest to validate as part of a merge gate",
    )
    parser.add_argument("--usage-deployment-contract", type=Path,
                        help="external expected owner and host inventory for the usage manifest")
    args = parser.parse_args()
    if bool(args.usage_manifest) != bool(args.usage_deployment_contract):
        parser.error("--usage-manifest and --usage-deployment-contract must be supplied together")
    if args.usage_manifest and args.gate != "merge":
        parser.error("usage manifest validation requires the merge gate")
    evidence = args.evidence_dir or Path(os.environ.get("V2_GATE_EVIDENCE_DIR") or tempfile.mkdtemp(prefix="pentacle-v2-gate-"))
    evidence.mkdir(parents=True, exist_ok=True)
    tiers = ("unit", "smoke") if args.gate == "merge" else ("soak",) if args.gate == "soak" else (args.gate,)
    deadline = None if args.deadline_seconds is None else {"deadline_s": args.deadline_seconds, "reserve_s": args.deadline_reserve_seconds, "checks": []}
    sha = before = None
    results: list[dict[str, Any]] = []
    _TERMINATION_REQUESTED, _TERMINATION_SIGNAL = False, None
    old_handlers, ready = {}, False
    try:
        # Setup faults below land in the runner_error record rather than a bare traceback.
        sha, before = _sha(), _git_status()
        _write_evidence(args.gate, evidence, args.evidence_out, sha, before, None, None, [], finished=False, deadline=deadline)
        basetemp = args.basetemp or evidence / f"pytest-{args.gate}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
        basetemp.mkdir(parents=True, exist_ok=True)
        manifest = basetemp / ".owned.json"
        limits = [(tier, args.timeout_seconds if args.timeout_seconds is not None else float(os.environ.get(f"V2_{tier.upper()}_TIMEOUT_S", DEFAULT_TIMEOUTS[tier]))) for tier in tiers]
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
            usage_pending = args.gate == "merge" and args.usage_manifest is not None
            refusal = _deadline_check(deadline, clock_start, "deadline", limits, usage_pending=usage_pending)
            if refusal:
                results.append(refusal)
            else:
                if usage_pending:
                    results.append(_run_usage_manifest(args.usage_manifest, evidence, args.usage_deployment_contract))
                for index, (tier, limit) in enumerate(limits):
                    if _TERMINATION_REQUESTED:
                        break
                    if index and (refusal := _deadline_check(deadline, clock_start, "deadline", limits[index:], usage_pending=False)):
                        results.append(refusal)
                        break
                    results.append(_run_tier(tier, evidence, limit, basetemp=basetemp, manifest=manifest))
    except KeyboardInterrupt:
        _TERMINATION_REQUESTED = True
        results.append(_result("owner", code=143, passed=False, reason="terminated", termination={"kind": "external_signal", "signal": _TERMINATION_SIGNAL or "SIGINT"}))
    except ManifestError as exc:
        results.append(_result("owner", code=125, passed=False, reason=f"manifest_error:{exc}"))
    except Exception as exc:  # keep a terminal record for any setup/logging fault
        results.append(_result("owner", code=125, passed=False, reason="runner_error", error=f"{type(exc).__name__}: {exc}"[:500]))
    finally:
        cleanup_errors: list[str] = []
        if ready:
            try:
                mark_owner_stopping(manifest)
                reap_manifest(manifest, _ACTIVE_RUN_ID)
            except Exception as exc:
                cleanup_errors.append(f"reap:{type(exc).__name__}: {exc}"[:500])
            # pytest never cleans an explicit --basetemp; evidence lives in the evidence dir.
            if not cleanup_errors and not manifest.exists():
                for tier in tiers:
                    shutil.rmtree(basetemp / tier, ignore_errors=True)
                    if (basetemp / tier).exists():
                        cleanup_errors.append(f"basetemp_not_removed:{tier}")
            elif manifest.exists() and not cleanup_errors:
                cleanup_errors.append("owner_manifest_unresolved")
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
        _ACTIVE_MANIFEST_PATH = _ACTIVE_RUN_ID = None
        if cleanup_errors:
            results.append(_result("cleanup", code=125, passed=False, reason="cleanup_failed", errors=cleanup_errors))
    try:
        after_sha, after = _sha(), _git_status()
    except Exception as exc:
        after_sha = after = None
        results.append(_result("source_integrity", code=125, passed=False, reason="source_readback_failed", error=f"{type(exc).__name__}: {exc}"[:500]))
    else:
        if sha is not None and (after_sha != sha or (not before and after)):
            results.append(_result("source_integrity", code=125, passed=False, reason="source_changed_during_gate", command=["git", "rev-parse", "HEAD"]))
    try:
        output = _write_evidence(args.gate, evidence, args.evidence_out, sha, before, after_sha, after, results, deadline=deadline)
    except Exception as exc:
        print(f"[v2-gate] evidence write FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for item in results:
        counts = item.get("counts") or {}
        print(f"[v2-gate] {item['tier']} {'PASS' if item['passed'] else 'FAIL'} rc={item['returncode']} reason={item['reason']} secondary={','.join(item.get('secondary_outcomes') or []) or '-'} tests={counts.get('tests', 'n/a')} failures={counts.get('failures', 'n/a')} errors={counts.get('errors', 'n/a')}")
        progress = item.get("progress") or {}
        if progress and not progress.get("complete"):
            print(f"[v2-gate] {item['tier']} progress incomplete: completed={progress.get('completed_nodes')} active={progress.get('active_nodes')} last_completed={progress.get('last_completed')}")
    print(f"[v2-gate] evidence={output}")
    return 0 if results and all(bool(item["passed"]) for item in results) else 1
if __name__ == "__main__":
    raise SystemExit(main())
