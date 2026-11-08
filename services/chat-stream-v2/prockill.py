"""prockill.py - direct process-tree termination for the close ladder.

The close escalation (spec close ruling, steps 2-3) must be able to end a pane's
processes WITHOUT tmux: tmux itself may be the wedged component. Given a pane
pid this enumerates the process tree, SIGKILLs it, and reports liveness by
asking the kernel directly (`kill(pid, 0)`) rather than asking tmux.

Everything here is either one `ps` exec off the event loop or a non-blocking
syscall. No `/proc` dependency, so it stays portable to the macOS peers. This
module owns the process-tree walk for the whole daemon; `spawnctl.py`'s
transcript probe imports `process_tree` from here rather than keeping its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
import time
from typing import Any


# Darwin ps has no numeric sid column (sess is not an equivalent). This
# standalone probe also runs on peers without importing the daemon package.
# Keep the ordinary Linux ps path; use this only after that query fails.
SESSION_ID_PROBE = """\
import os
import subprocess

table = subprocess.run(
    ['ps', '-ww', '-eo', 'pid=,ppid=,uid=,pgid=,lstart=,command='],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, check=True, timeout=5,
).stdout
rows = []
for line in table.splitlines():
    fields = line.strip().split(None, 9)
    if len(fields) < 9:
        continue
    try:
        sid = os.getsid(int(fields[0]))
    except ProcessLookupError:
        continue
    rows.append(' '.join(fields[:4] + [str(sid)] + fields[4:]))
print('\\n'.join(rows))
"""


async def terminate_and_reap(proc: object) -> None:
    """Kill a bounded subprocess and wait for its process handle to settle.

    Every async subprocess owner uses this on timeout and cancellation. The
    cleanup path catches ``BaseException`` because a cancelled ``communicate``
    may itself raise ``CancelledError``; the caller then re-raises its original
    cancellation or timeout after the child has been reaped.
    """
    if proc is None:
        return
    try:
        if getattr(proc, "returncode", None) is None:
            proc.kill()  # type: ignore[attr-defined]
    except (OSError, ProcessLookupError):
        pass
    # A descendant can retain inherited pipes after the owned root is killed.
    # Close our handles so wait() does not wait for that descendant's EOF.
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except BaseException:  # noqa: BLE001 - still reap and preserve caller error
            pass
    try:
        # Waiting directly avoids re-entering a ``communicate`` coroutine that
        # ``asyncio.wait_for`` may already have cancelled on timeout.
        await proc.wait()  # type: ignore[attr-defined]
        return
    except BaseException:  # noqa: BLE001 - cleanup must not mask the original error
        try:
            communicate = getattr(proc, "communicate", None)
            if callable(communicate):
                await communicate()
        except BaseException:  # noqa: BLE001 - best effort after kill
            pass


async def _ps() -> str:
    """One `ps` exec, off the loop. Empty string on any failure — a caller that
    cannot enumerate the tree falls back to the root pid alone."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-eo", "pid=,ppid=",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.CancelledError:
        await terminate_and_reap(proc)
        raise
    except (asyncio.TimeoutError, OSError):
        await terminate_and_reap(proc)
        return ""
    return (out or b"").decode("utf-8", "replace")


def canonical_start(value: object) -> str:
    """Canonical ps lstart presentation; an unavailable value stays invalid."""
    return " ".join(value.split()) if isinstance(value, str) else ""


async def process_record(pid: str) -> dict[str, Any] | None:
    """Read one provider root with fields supported by Darwin and Linux."""
    if not str(pid).isdecimal() or int(pid) <= 0:
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-ww", "-p", str(pid), "-o", "pid=,uid=,lstart=,command=",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode != 0:
            return None
        fields = out.decode("utf-8", "replace").strip().split(None, 7)
        if len(fields) != 8 or int(fields[0]) != int(pid):
            return None
        return {"pid": int(fields[0]), "uid": int(fields[1]),
                "start_id": canonical_start(" ".join(fields[2:7])), "command": fields[7]}
    except asyncio.CancelledError:
        await terminate_and_reap(proc)
        raise
    except (OSError, ValueError, asyncio.TimeoutError):
        await terminate_and_reap(proc)
        return None


async def process_records() -> dict[int, dict[str, Any]]:
    """Return identity-bearing local process records for close readback.

    ``lstart`` is a printed start value, not a portable process-birth token;
    the reconciler additionally binds the record to boot id and tmux pane proof.
    The SID fallback reads getsid after ps; the existing birth comparison on
    the fresh pre-signal inventory remains mandatory for PID-reuse fencing.
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-eo", "pid=,ppid=,uid=,pgid=,sid=,lstart=,command=",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode != 0:
            # Darwin still prints a partial table on failure. Never parse it.
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", SESSION_ID_PROBE,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode != 0:
                return {}
    except asyncio.CancelledError:
        await terminate_and_reap(proc)
        raise
    except (asyncio.TimeoutError, OSError):
        await terminate_and_reap(proc)
        return {}
    return parse_process_records((out or b"").decode("utf-8", "replace"))


def parse_process_records(output: str) -> dict[int, dict[str, Any]]:
    """Shared identity parser for async lifecycle and synchronous host helpers."""
    records: dict[int, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 10)
        if len(parts) < 10:
            continue
        try:
            pid, ppid, uid, pgid, sid = (int(parts[index]) for index in range(5))
        except (TypeError, ValueError):
            continue
        # lstart is five whitespace-separated fields after sid.  Some ps
        # implementations omit command for a kernel process; retain an empty
        # command but never invent identity fields.
        start_id = canonical_start(" ".join(parts[5:10]))
        command = parts[10] if len(parts) > 10 else ""
        if pid > 0 and ppid >= 0 and uid >= 0 and pgid >= 0 and sid >= 0 and start_id:
            records[pid] = {
                "pid": pid, "ppid": ppid, "uid": uid, "pgid": pgid, "sid": sid,
                "start_id": start_id, "command": command,
            }
    return records


async def boot_id() -> str:
    """Read a host boot identity without blocking the event loop."""
    commands = (
        ("cat", "/proc/sys/kernel/random/boot_id"),
        ("sysctl", "-n", "kern.boottime"),
    )
    for argv in commands:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (asyncio.TimeoutError, OSError):
            continue
        if proc.returncode == 0 and (value := (out or b"").decode("utf-8", "replace").strip()):
            return value
    return ""


def command_fingerprint(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()


def identity_proof(
    *, host: str, boot: str, record: dict[str, Any],
    tmux_session: str, tmux_pane: str, tty: str, tmux_socket: str,
) -> dict[str, Any] | None:
    """Build the complete process-instance proof stored in session_reap."""
    required = ("pid", "ppid", "uid", "pgid", "sid", "start_id", "command")
    if not boot or any(key not in record for key in required):
        return None
    started = canonical_start(record["start_id"])
    if not started:
        return None
    captured = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "version": 2,
        "host": host,
        "uid": int(record["uid"]),
        "boot_id": boot,
        "pid": int(record["pid"]),
        "start_id": started,
        "ppid": int(record["ppid"]),
        "pgid": int(record["pgid"]),
        "sid": int(record["sid"]),
        "tmux_socket": str(tmux_socket),
        "tmux_session": str(tmux_session),
        "tmux_pane": str(tmux_pane),
        "tty": str(tty),
        "captured_at": captured,
        "command_fingerprint": command_fingerprint(str(record["command"])),
    }


async def process_tree(root_pid: str) -> list[str]:
    """`root_pid` plus every descendant pid. The agent CLI (and the transcript
    it holds open) is a descendant of the pane's shell, not the pane pid."""
    if not root_pid:
        return []
    out = await _ps()
    if not out:
        return [root_pid]
    children: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            children.setdefault(parts[1], []).append(parts[0])
    seen: list[str] = []
    seen_set: set[str] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen_set:
            continue
        seen_set.add(pid)
        seen.append(pid)
        stack.extend(children.get(pid, ()))
    return seen


async def signal_tree(root_pid: str, sig: int = signal.SIGKILL) -> list[str]:
    """Send `sig` to `root_pid` and every descendant. Returns the pids signalled.
    A pid that vanished between enumeration and delivery is fine — it is already
    gone, which is the goal."""
    pids = await process_tree(root_pid)
    for pid in pids:
        try:
            os.kill(int(pid), sig)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    return pids


def pid_alive(pid: str) -> bool:
    """True while `pid` still exists. `kill(pid, 0)` asks the kernel directly, so
    a wedged tmux cannot make a dead process look alive or a live one look dead.
    A pid we may not signal (PermissionError) is treated as alive — never claim a
    process we cannot see the death of is gone."""
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def comparison_environment(*, host_local: bool = False) -> dict[str, str]:
    env = {**os.environ, "LC_ALL": "C"}
    if host_local:
        env.pop("TZ", None)
    else:
        env["TZ"] = "UTC0"
    return env


def classify_start_observation(rc: int, stdout: str, stderr: str) -> tuple[str, str]:
    """record / absent / unknown; a failed observation never proves absence."""
    from datetime import datetime
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return "unknown", ""
    if rc == 1 and stdout == "" and stderr == "":
        return "absent", ""
    if rc != 0 or stderr or len(stdout.splitlines()) != 1:
        return "unknown", ""
    started = canonical_start(stdout)
    try:
        datetime.strptime(started, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return "unknown", ""
    return "record", started


def birth_matches(recorded: object, *observed: object) -> bool:
    """Positive comparable rendering only; false never establishes death."""
    stored = canonical_start(recorded)
    return (classify_start_observation(0, stored, "")[0] == "record"
            and any(stored == canonical_start(value) for value in observed))


def darwin_boot(value: object) -> tuple[int, int] | None:
    import re
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*\{\s*sec\s*=\s*([0-9]+),\s*usec\s*=\s*([0-9]+)\s*\}(?:\s+[^\n]*)?\s*", value)
    if match is None:
        return None
    sec, usec = map(int, match.groups())
    return (sec, usec) if usec < 1_000_000 else None


def boot_matches(recorded: object, observed: object) -> bool:
    if not isinstance(recorded, str) or not isinstance(observed, str) or not recorded or not observed:
        return False
    # Darwin's trailing ctime presentation changes with TZ. Malformed Darwin
    # observations do not acquire equality authority, even if the bytes match.
    if recorded.lstrip().startswith("{") or observed.lstrip().startswith("{"):
        key = darwin_boot(recorded)
        return key is not None and key == darwin_boot(observed)
    # Linux UUID equality supplies no clock bound. Invalid tokens stay unknown.
    import re
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", recorded)
                and recorded == observed)


def _comparison_inventory(sid_probe):
    """Standalone producing-host comparison; no imports from an installed daemon."""
    import os
    import subprocess
    import sys

    def run(argv, env):
        return subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=3, check=False)

    def environment(local=False):
        env = {**os.environ, "LC_ALL": "C"}
        if local:
            env.pop("TZ", None)
        else:
            env["TZ"] = "UTC0"
        return env

    def boot():
        for argv in (("sysctl", "-n", "kern.boottime"), ("cat", "/proc/sys/kernel/random/boot_id")):
            try:
                result = run(argv, environment())
            except OSError:
                continue
            if result.returncode == 0 and not result.stderr and result.stdout.strip():
                return result.stdout.strip()
        raise RuntimeError("boot observation unavailable")

    def inventory(local=False):
        env = environment(local)
        result = run(("ps", "-eo", "pid=,ppid=,uid=,pgid=,sid=,lstart=,command="), env)
        if result.returncode != 0:
            result = run((sys.executable, "-c", sid_probe), env)
        if result.returncode != 0 or result.stderr or not result.stdout.strip():
            raise RuntimeError("process observation unavailable")
        return result.stdout

    before = boot()
    utc = inventory()
    local = inventory(True)
    utc_after = inventory()
    after = boot()
    return {"utc": utc, "local": local, "utc_after": utc_after,
            "boot_before": before, "boot_after": after}


def comparison_inventory_probe() -> str:
    import inspect
    return ("import json\n" + inspect.getsource(_comparison_inventory)
            + "\nprint(json.dumps(_comparison_inventory(" + repr(SESSION_ID_PROBE) + ")))\n")


PSResult = tuple[int, str, str]


async def start_observations(pid: str, *, ps_bin: str = "ps", timeout_s: float = 5.0) -> tuple[PSResult, ...]:
    """Observe the two supported birth renderings on the process's host.

    Empty TZ is not host-local on Darwin. Explicitly remove it for that read;
    keep the persisted producer and the daemon's own environment untouched.
    """
    results = []
    for zone in ("UTC0", None):
        env = comparison_environment(host_local=zone is None)
        proc = await asyncio.create_subprocess_exec(
            ps_bin, "-o", "lstart=", "-p", pid, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except BaseException:
            await terminate_and_reap(proc)
            raise
        value = (int(proc.returncode or 0), out.decode(errors="replace"), err.decode(errors="replace"))
        if value == (1, "", ""):
            # A hidden/permission-limited ps row is not kernel absence. EPERM
            # (or mere PID existence) still supplies no comparable birth.
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                pass
            except (OSError, ValueError):
                value = (2, "", "process_absence_unproven")
            else:
                value = (2, "", "process_absence_unproven")
        results.append(value)
    return tuple(results)
