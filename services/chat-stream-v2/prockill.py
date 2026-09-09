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
import time
from typing import Any


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
                "start_id": " ".join(fields[2:7]), "command": fields[7]}
    except asyncio.CancelledError:
        await terminate_and_reap(proc)
        raise
    except (OSError, ValueError, asyncio.TimeoutError):
        await terminate_and_reap(proc)
        return None


async def process_records() -> dict[int, dict[str, Any]]:
    """Return identity-bearing local process records for close readback.

    ``lstart`` is stable across PID reuse and works on both macOS and Linux;
    the reconciler additionally binds the record to boot id and tmux pane proof.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-eo", "pid=,ppid=,uid=,pgid=,sid=,lstart=,command=",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except (asyncio.TimeoutError, OSError):
        return {}
    records: dict[int, dict[str, Any]] = {}
    for line in (out or b"").decode("utf-8", "replace").splitlines():
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
        start_id = " ".join(parts[5:10])
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
    captured = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "version": 2,
        "host": host,
        "uid": int(record["uid"]),
        "boot_id": boot,
        "pid": int(record["pid"]),
        "start_id": str(record["start_id"]),
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
