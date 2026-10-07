#!/usr/bin/env python3
"""Gate a scheduled test job at fire time on a per-occurrence FD approval.

`run` resolves the exact candidate, sends the FD one GATE, waits at most 30 minutes for
`approve RUN_ID --reservations TEXT`, re-checks the candidate, then runs the wrapped command.
With no approval in time it prints and records SKIPPED_UNAPPROVED and exits 77: the occurrence is
never reported as passing, and a late approval cannot revive it. State lives under --state-dir
(default ~/.pentacle/gate-at-fire); this is a plain wrapper, not a daemon.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import threading
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

SKIPPED_RC = 77
REFUSED_RC = 78
MAX_WAIT_S = 1800
DEFAULT_DIR = Path.home() / ".pentacle" / "gate-at-fire"
# The same daemon endpoint and operator token the fleet smoke and the daily retro already use.
DEFAULT_URL = "ws://127.0.0.1:7791"
DEFAULT_TOKEN_PATH = Path.home() / ".config/pentacle-stream/token"
SERVICE_DIR = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class Refused(Exception):
    """A GATE must not be sent: the occurrence cannot be described exactly."""


def _snapshot() -> str:
    load = os.getloadavg()[0] / (os.cpu_count() or 1)
    mem = "unknown"
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
            page = int(re.search(r"page size of (\d+)", out).group(1))
            pages = sum(int(m) for m in re.findall(r"Pages (?:free|inactive):\s+(\d+)", out))
            mem = f"{pages * page / 2**30:.1f}"
        else:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    mem = f"{int(line.split()[1]) / 1048576:.1f}"
    except (AttributeError, OSError, ValueError, subprocess.SubprocessError):
        pass
    return f"load/cpu={load:.2f} mem_avail_gb={mem}"


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=15, check=False).stdout.strip()


def candidate(repo: str) -> str:
    """Full 40-hex HEAD of a CLEAN candidate checkout; a dirty or unresolvable one is refused.

    Release checkouts are pinned and clean, so HEAD alone identifies the code that will run; any
    uncommitted or untracked change makes the occurrence REFUSED rather than approvable.
    """
    try:
        sha = _git(repo, "rev-parse", "HEAD")
        dirty = [ln for ln in _git(repo, "status", "--porcelain").splitlines() if ln]
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"candidate unresolved: {type(exc).__name__}") from exc
    if not SHA40.match(sha):
        raise Refused(f"candidate unresolved: {repo!r} has no full HEAD sha")
    if dirty:
        raise Refused(f"candidate checkout is dirty ({len(dirty)} path(s)); only a clean pinned checkout is gated")
    return sha


def targets(raw: str) -> tuple[str, bytes | None]:
    """Resolve the GATE's target hosts once; `@machines` also captures the machines file bytes.

    Returns (names, captured_bytes). The captured bytes are what the approved run will use, so a
    later edit of PENTACLE_MACHINES_FILE cannot change who the FD approved.
    """
    if raw != "@machines":
        return raw, None
    if os.environ.get("PENTACLE_MACHINES_JSON"):
        raise Refused("target hosts ambiguous: PENTACLE_MACHINES_JSON overrides the machines file")
    try:
        blob = Path(os.environ["PENTACLE_MACHINES_FILE"]).expanduser().read_bytes()
        names = ",".join(str(m["name"]) for m in json.loads(blob)["machines"])
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise Refused(f"target hosts unresolved: {type(exc).__name__}") from exc
    if not names:
        raise Refused("target hosts unresolved: empty machines list")
    return names, blob


def _prior_duration(state: Path, job: str) -> str:
    try:
        last = json.loads((state / f"last-{job}.json").read_text())
        return f" prior_run_s={round(float(last['duration_s']))}" if "duration_s" in last else ""
    except (OSError, ValueError, KeyError):
        return ""


def gate_text(run_id: str, a: argparse.Namespace, host: str, sha: str, cmd: list[str], target_hosts: str,
              machines_sha: str = "") -> str:
    tgt = f" target_hosts={target_hosts}" if target_hosts else ""
    tgt += f" machines_sha256={machines_sha[:16]}" if machines_sha else ""
    return (f"GATE scheduled-test run={run_id} candidate={sha} candidate_clean=true runtime={' '.join(cmd)} "
            f"proposed_host={host}{tgt} expected_duration={a.duration}{_prior_duration(Path(a.state_dir), a.job)} "
            f"{_snapshot()} known_reservations=NOT KNOWN TO THE JOB: check portfolio and quiet windows, then "
            f"approve with: gate_at_fire.py approve {run_id} --reservations '<current reservations or none>' "
            f"--state-dir {a.state_dir}; no approval within {a.wait_s:.0f}s = SKIPPED_UNAPPROVED, "
            f"late approval cannot revive it")


def _left(deadline: float) -> float:
    """Seconds left on the occurrence's one monotonic deadline; raises when none remain."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("occurrence deadline exhausted before the next notification phase")
    return left


def ws_notify(text: str, run_id: str, url: str, token_path: Path, deadline: float | None = None) -> None:
    """Deliver the GATE to the current assistant binding, as the daily retro delivers its REPORT.

    Only two verbs are used: `assistant.binding` (read) and one `send` whose text starts
    with `GATE scheduled-test`. Every blocking phase (connect, each RPC) is bounded by the time
    left on the one absolute `deadline`, recomputed before the phase, with no floor. Raises on any
    refusal or an exhausted deadline so the caller can log it.
    """
    if not text.startswith("GATE scheduled-test "):
        raise ValueError("only GATE scheduled-test messages may be sent")
    if deadline is None:  # direct callers outside run() get a 20 s absolute budget
        deadline = time.monotonic() + 20.0
    if str(SERVICE_DIR) not in sys.path:
        sys.path.insert(0, str(SERVICE_DIR))
    from tools.live_window import authenticated_operator_connection

    with authenticated_operator_connection(url, token_path, min(20.0, _left(deadline))) as connection:
        connection.timeout = _left(deadline)
        binding = connection.rpc({"type": "assistant.binding"})
        if binding.get("type") != "assistant.binding.ok":
            raise RuntimeError(f"assistant.binding refused: {binding.get('type')}")
        host, session = str(binding["stream_id"]).split(":", 1)
        key = "gate-at-fire-" + run_id
        connection.timeout = _left(deadline)
        sent = connection.rpc({"type": "send", "host": host, "session_name": session, "text": text,
                               "request_id": key, "optimistic_id": key})
        if sent.get("type") != "send.result" or sent.get("delivery") != "landed":  # only `landed` proves delivery
            raise RuntimeError(f"send refused: {sent.get('type')} delivery={sent.get('delivery')}")


def _notify_within(deadline: float, send) -> None:
    """Run the blocking notification so it cannot outlive the occurrence deadline (watchdog thread)."""
    outcome: list[BaseException] = []

    def target() -> None:
        try:
            send()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller below
            outcome.append(exc)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(_left(deadline))
    if worker.is_alive():
        raise TimeoutError("notification still running at the occurrence deadline")
    if outcome:
        raise outcome[0]


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    tmp.replace(path)


@contextmanager
def _lock(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _expire(state: Path, run_id: str) -> bool:
    """Atomically expire an unapproved occurrence; False if an approval landed first."""
    with _lock(state):
        if (state / "approved" / run_id).exists():
            return False
        _write(state / "expired" / run_id, {"run_id": run_id, "expired_at": time.time()})
        (state / "pending" / f"{run_id}.json").unlink(missing_ok=True)
        return True


def _skip(state: Path, a: argparse.Namespace, run_id: str, outcome: str, why: str) -> int:
    _write(state / f"last-{a.job}.json", {"run_id": run_id, "outcome": outcome, "at": time.time()})
    print(f"{outcome} job={a.job} run={run_id} ({why}; not a pass)", flush=True)
    return SKIPPED_RC


def run(a: argparse.Namespace, cmd: list[str]) -> int:
    if not 0 < a.wait_s <= MAX_WAIT_S or a.poll_s <= 0:
        print(f"refused: --wait-s must be in (0, {MAX_WAIT_S}] and --poll-s positive", file=sys.stderr)
        return REFUSED_RC
    deadline = time.monotonic() + a.wait_s  # the one deadline: notification, polling and sleeps all live inside it
    state = Path(a.state_dir)
    host = socket.gethostname()
    try:
        sha = candidate(a.candidate_repo)
        target_hosts, machines_blob = targets(a.targets)
    except Refused as exc:
        print(f"REFUSED job={a.job}: {exc}; no GATE sent, job not run (not a pass)", flush=True)
        _write(state / f"last-{a.job}.json", {"outcome": "REFUSED", "why": str(exc), "at": time.time()})
        return REFUSED_RC
    run_id = f"{a.job}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
    started = time.time()
    machines_sha = hashlib.sha256(machines_blob).hexdigest() if machines_blob is not None else ""
    snapshot_path = None
    if machines_blob is not None:  # the approved run reads exactly these bytes, never a fresh reload
        snapshot_path = state / "machines" / f"{run_id}.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_bytes(machines_blob)
        snapshot_path.chmod(0o400)
    _write(state / "pending" / f"{run_id}.json", {"run_id": run_id, "job": a.job, "host": host, "candidate": sha,
           "command": cmd, "machines_sha256": machines_sha, "requested_at": started, "deadline_at": started + a.wait_s})
    text = gate_text(run_id, a, host, sha, cmd, target_hosts, machines_sha)
    print(text, flush=True)
    notify_cmd = a.notify_cmd
    if not notify_cmd and (state / "notify-cmd").exists():
        notify_cmd = (state / "notify-cmd").read_text().strip()  # test/override transport
    if deadline - time.monotonic() <= 0:
        print("gate notify skipped: occurrence deadline already exhausted", flush=True)
    else:
        try:
            if notify_cmd:
                sent = subprocess.run([*shlex.split(notify_cmd), text], capture_output=True, text=True, check=False,
                                      timeout=_left(deadline))
                if sent.returncode != 0:
                    raise RuntimeError(f"rc={sent.returncode}: {sent.stderr.strip()[:200]}")
            else:
                _notify_within(deadline, lambda: ws_notify(text, run_id, a.ws_url, Path(a.token_path), deadline))
        except Exception as exc:  # noqa: BLE001 - an undelivered GATE still waits, then skips visibly
            print(f"gate notify failed {type(exc).__name__}: {str(exc)[:200]}", flush=True)
    approved = state / "approved" / run_id
    grant = None
    while True:
        # Validate and consume the single-use grant under the same lock approve and expiry use.
        with _lock(state):
            if approved.exists():
                candidate_grant = json.loads(approved.read_text())
                approved.unlink(missing_ok=True)
                (state / "pending" / f"{run_id}.json").unlink(missing_ok=True)
                if candidate_grant.get("run_id") == run_id and candidate_grant.get("host") == host:
                    grant = candidate_grant
                    break
        left = deadline - time.monotonic()
        if left <= 0:
            if _expire(state, run_id):
                return _skip(state, a, run_id, "SKIPPED_UNAPPROVED", f"no FD approval within {a.wait_s:.0f}s")
            continue  # an approval landed first: consume it on the next pass
        time.sleep(min(a.poll_s, left))
    # Approved: re-check that exactly what the FD approved is what will run.
    try:
        now = candidate(a.candidate_repo)
    except Refused:
        now = ""
    changed = now != sha
    if machines_blob is not None:
        try:
            changed = changed or Path(os.environ["PENTACLE_MACHINES_FILE"]).expanduser().read_bytes() != machines_blob
        except (KeyError, OSError):
            changed = True
    if changed:
        return _skip(state, a, run_id, "SKIPPED_CANDIDATE_CHANGED",
                     "candidate or target list changed after approval; a fresh GATE is required")
    env = {**os.environ, "GATE_AT_FIRE_RUN_ID": run_id, "GATE_AT_FIRE_STATE_DIR": str(state)}
    if snapshot_path is not None:
        env["PENTACLE_MACHINES_FILE"] = str(snapshot_path)  # the captured, approved list
        env.pop("PENTACLE_MACHINES_JSON", None)
    _write(state / "running" / run_id, {"run_id": run_id, "pid": os.getpid(), "host": host})
    _write(state / f"last-{a.job}.json", {"run_id": run_id, "outcome": "APPROVED_RUNNING",
                                          "reservations": grant.get("reservations"), "at": time.time()})
    try:
        rc = subprocess.run(cmd, check=False, env=env).returncode
    finally:
        (state / "running" / run_id).unlink(missing_ok=True)
    _write(state / f"last-{a.job}.json", {"run_id": run_id, "outcome": "RAN", "rc": rc, "at": time.time(),
                                          "duration_s": time.time() - started})
    return rc


def approve(a: argparse.Namespace) -> int:
    state = Path(a.state_dir)
    if not a.reservations.strip():
        print("refused: --reservations is required (the current reservations/planned work, or 'none')", file=sys.stderr)
        return 1
    with _lock(state):
        pending = state / "pending" / f"{a.run_id}.json"
        if (state / "expired" / a.run_id).exists() or not pending.exists():
            print(f"refused: {a.run_id} is expired or unknown; a late approval cannot revive it", file=sys.stderr)
            return 1
        request = json.loads(pending.read_text())
        if time.time() > float(request["deadline_at"]):
            print(f"refused: {a.run_id} passed its deadline; a late approval cannot revive it", file=sys.stderr)
            return 1
        _write(state / "approved" / a.run_id, {"run_id": a.run_id, "host": request["host"],
                                               "candidate": request["candidate"], "reservations": a.reservations,
                                               "approved_at": time.time()})
    print(f"approved {a.run_id} on {request['host']} candidate={request['candidate']}")
    return 0


def verify(a: argparse.Namespace) -> int:
    """0 only inside a wrapper-run occurrence: a live `running` marker for RUN_ID with its pid alive."""
    marker = Path(a.state_dir) / "running" / a.run_id
    try:
        pid = int(json.loads(marker.read_text())["pid"])
        os.kill(pid, 0)
    except (OSError, ValueError, KeyError, TypeError):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("run")
    r.add_argument("--job", required=True)
    r.add_argument("--duration", required=True)
    r.add_argument("--targets", default="")
    r.add_argument("--candidate-repo", default=".", help="checkout whose full HEAD is the candidate")
    r.add_argument("--wait-s", type=float, default=float(os.environ.get("GATE_AT_FIRE_WAIT_S", MAX_WAIT_S)))
    r.add_argument("--poll-s", type=float, default=10)
    r.add_argument("--notify-cmd", default=os.environ.get("GATE_AT_FIRE_NOTIFY_CMD", ""),
                   help="test/override transport: command taking the GATE text as its last argument; default is the daemon websocket")
    r.add_argument("--ws-url", default=DEFAULT_URL)
    r.add_argument("--token-path", default=str(DEFAULT_TOKEN_PATH))
    r.add_argument("--state-dir", default=str(DEFAULT_DIR))
    ap = sub.add_parser("approve")
    ap.add_argument("run_id")
    ap.add_argument("--reservations", default="")
    ap.add_argument("--state-dir", default=str(DEFAULT_DIR))
    vp = sub.add_parser("verify")
    vp.add_argument("run_id")
    vp.add_argument("--state-dir", default=str(DEFAULT_DIR))
    a = p.parse_args(argv)
    if a.mode == "approve":
        return approve(a)
    if a.mode == "verify":
        return verify(a)
    if not cmd:
        p.error("run needs a command after --")
    return run(a, cmd)


if __name__ == "__main__":
    sys.exit(main())
