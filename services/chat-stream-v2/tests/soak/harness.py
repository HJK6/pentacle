"""Soak harness — real daemon, real tmux, synthetic fleet under sustained load.

This is the machinery `tests/soak/test_soak.py` drives. Nothing here fakes the
daemon: it is `main.py` in its own process on a free port, driving a fleet of
real tmux panes running `stub_cli.py`, exercised by concurrent WS clients that
measure every RPC. It is the same "real daemon / real tmux / stub CLI" contract
as the smoke tier (`../smoke/`), scaled up in time and fleet size and armed with
the five soak assertions from `v2_design.md` § Soak harness:

    port-bind < 5 s · RPC p95 < 2 s under load · daemon CPU ceiling ·
    zero false closes · zero uncommanded spawns · clean shutdown

The harness only measures and drives. It never loosens a threshold to make a run
pass: a breached assertion is a finding about the daemon, reported with its
measured number, not tuned away (Phase C rule 3).

Offline host: v2's `hosts.py` SSH probe pool is still a skeleton (spawn is
localhost-only; a remote target answers `unsupported_host`). Until it lands there
is no live probe loop to stall, so the "one offline host" is simulated at the
verb surface — seeded rows on an unreachable peer that the fleet keeps poking,
asserting the daemon answers each with a fast structured error and never stalls
the loop. The live SSH-stall chaos case is wired the day `hosts.py` grows a real
probe (see test_soak.py's offline-host note).
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from websockets.sync.client import connect

from tools.run_gate import (
    _process_group_popen_kwargs,
    _set_parent_death_signal,
    terminate_process_group,
)
from tools.gate_owner_manifest import leader_pgid, manifest_path_from_env, record, resolve_tmux_socket

SERVICE_DIR = Path(__file__).resolve().parents[2]

from store import Store  # noqa: E402  (after sys.path bootstrap)
from launch import local_machine, stream_token_file_for

BIND_DEADLINE_S = 5.0
SHUTDOWN_DEADLINE_S = 5.0
LOCAL_HOST = "soakhost"
STUB_CLI = Path(__file__).resolve().parents[1] / "smoke" / "stub_cli.py"
STUB_COMMAND = f"{sys.executable} {STUB_CLI}"
_CLK_TCK = os.sysconf("SC_CLK_TCK")
_MACOS_PS = "/bin/ps"
_MACOS_LSOF = "/usr/sbin/lsof"
_SAMPLER_COMMAND_TIMEOUT_S = 5.0


def _record_owned(pgid: int | None = None, socket: str | None = None,
                  leader_pid: int | None = None) -> None:
    path = manifest_path_from_env()
    if path is not None:
        record(path, pgid, socket, leader_pid=leader_pid)


class SamplerInfrastructureError(RuntimeError):
    """Named refusal when resource sampling cannot produce host-truthful data."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.code = f"sampler_{reason}"
        super().__init__(f"{self.code}: {detail}")


def _run_sampler_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=_SAMPLER_COMMAND_TIMEOUT_S,
    )


def _parse_proc_stat_ticks(contents: str) -> float | None:
    try:
        fields = contents.rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])
    except (IndexError, TypeError, ValueError):
        return None
    return ticks / _CLK_TCK if ticks >= 0 else None


def _parse_cpu_time_seconds(value: str) -> float | None:
    """Parse macOS ps cputime without relying on localized prose."""
    value = value.strip()
    if not re.fullmatch(r"(?:\d+-)?\d+(?::\d{2})?(?::\d{2}(?:\.\d+)?)?", value):
        return None

    days = 0
    if "-" in value:
        day_text, value = value.split("-", 1)
        days = int(day_text)
    parts = value.split(":")
    try:
        if len(parts) == 2:
            hours = 0
            minutes = int(parts[0])
            seconds = float(parts[1])
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
        else:
            return None
    except ValueError:
        return None
    if minutes >= 60 or seconds >= 60 or not math.isfinite(seconds):
        return None
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total if total >= 0 else None


def _parse_macos_cpu_output(output: str, pid: int) -> float | None:
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(pid):
            return _parse_cpu_time_seconds(fields[1])
    return None


def _parse_macos_fd_output(output: str, pid: int) -> int | None:
    selected = False
    count = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("p"):
            selected = line[1:] == str(pid)
        elif selected and line.startswith("f"):
            count += 1
    return count if selected else None


class TmuxNamespace:
    """One run-owned tmux server addressed by a short private socket name."""

    def __init__(self, tmp_path: Path) -> None:
        real_tmux = shutil.which("tmux")
        if not real_tmux:
            raise RuntimeError("tmux is required for the soak tier")
        self.real_tmux = real_tmux
        self.socket = f"v2s{uuid.uuid4().hex[:8]}"
        self.keepalive = f"__v2ns_{self.socket}"
        self.socket_path = resolve_tmux_socket(self.socket)
        self.machines_file = tmp_path / f"machines-{self.socket}.json"
        self.machines_file.write_text(
            json.dumps({"machines": [{"name": LOCAL_HOST}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        self._env = dict(os.environ)
        self._env.pop("TMUX", None)
        self.wrapper = tmp_path / f"tmux-{self.socket}"
        self.wrapper.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(self.real_tmux)} -L {shlex.quote(self.socket)} \"$@\"\n",
            encoding="utf-8",
        )
        self.wrapper.chmod(0o755)
        self.started = False

    def child_env(self, **overrides: str) -> dict[str, str]:
        env = dict(self._env)
        env.update(overrides)
        # The soak is local-only even when launched from a shell carrying a
        # fleet file, usage route, SSH control path, or agent socket.
        for name in (
            "PENTACLE_MACHINES_JSON",
            "PENTACLE_USAGE_PROBE_HOST",
            "PENTACLE_SSH_CONTROL_DIR",
            "PENTACLE_SSH_CONTROL_PERSIST",
            "SSH_AUTH_SOCK",
        ):
            env.pop(name, None)
        env["PENTACLE_MACHINES_FILE"] = str(self.machines_file)
        env.pop("TMUX", None)
        return env

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.real_tmux, "-L", self.socket, *args],
            env=self.child_env(),
            capture_output=True,
            text=True,
        )

    def session_names(self) -> list[str]:
        result = self.run("list-sessions", "-F", "#{session_name}")
        if result.returncode != 0:
            return []
        return result.stdout.splitlines()

    def start(self) -> None:
        if self.started:
            return
        result = self.run("new-session", "-d", "-s", self.keepalive, "sleep", "600")
        if result.returncode != 0:
            self.run("kill-server")
            self.socket_path.unlink(missing_ok=True)
            raise AssertionError(
                result.stderr or result.stdout or "private tmux server failed to start"
            )
        self.started = True

    def kill_server(self) -> None:
        """Kill only this run's server; safe after partial setup or prior cleanup."""
        if self.started:
            self.run("kill-server")
        self.socket_path.unlink(missing_ok=True)
        self.started = False


# --------------------------------------------------------------------------- #
# daemon process
# --------------------------------------------------------------------------- #


class Daemon:
    """A real `main.py` process on a free port, over a persistent on-disk DB so a
    restart re-adopts (B10)."""

    def __init__(self, db: str, tmux_namespace: TmuxNamespace,
                 extra_env: dict[str, str] | None = None) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self.port: int | None = None
        self.exec_started = 0.0
        self.bind_observed = 0.0
        self.db = db
        self.tmux_namespace = tmux_namespace
        self.extra_env = extra_env or {}
        root = Path(db).parent
        self.spawn_cwd = root / "seats"
        self.spawn_cwd.mkdir(exist_ok=True)
        self.notifications_db, self.assets_db, self.blob_root = (
            str(root / "notifications.db"), str(root / "assets.db"), str(root / "blobs"))
        self._has_bound = False
        # Daemon stdout+stderr go to a file (appended across restarts), not a
        # drained pipe: a drained pipe hides *why* a daemon died, and a stalled
        # drain thread could block the daemon on write. The file is the evidence
        # trail when an assertion trips.
        self.logpath = db + ".daemon.log"
        self._logf = None

    def start(self) -> float:
        """Launch and block until the bound port is announced. Returns seconds
        from exec to bind (the port-bind assertion's raw number)."""
        env = self.tmux_namespace.child_env()
        env.update(self.extra_env)
        for name in (
            "PENTACLE_MACHINES_JSON",
            "PENTACLE_USAGE_PROBE_HOST",
            "PENTACLE_SSH_CONTROL_DIR",
            "PENTACLE_SSH_CONTROL_PERSIST",
            "SSH_AUTH_SOCK",
        ):
            env.pop(name, None)
        env["PENTACLE_MACHINES_FILE"] = str(self.tmux_namespace.machines_file)
        env["PYTHONUNBUFFERED"] = "1"
        env.pop("TMUX", None)
        self.exec_started = time.monotonic()
        argv = [
            sys.executable, "main.py", "--port", "0", "--db", self.db,
            "--local-host", LOCAL_HOST, "--tmux-bin", str(self.tmux_namespace.wrapper),
            "--notifications-db", self.notifications_db, "--assets-db", self.assets_db,
            "--blob-root", self.blob_root,
            "--disable-hosts", "--disable-usage-state-publisher",
            "--claude-bin", str(STUB_CLI.resolve()),
            "--spawn-cwd", str(self.spawn_cwd),
        ]
        self._logf = open(self.logpath, "ab", buffering=0)
        self._logf.write(f"\n==== daemon start {time.time():.0f} ====\n".encode())
        start_off = self._logf.tell()
        # Optional stress knob: cap the daemon's fd soft limit so an fd leak hits
        # EMFILE in minutes instead of hours. Off unless SOAK_DAEMON_FD_LIMIT is
        # set; never changes a normal run.
        popen_kwargs = _process_group_popen_kwargs()
        fd_limit = os.environ.get("SOAK_DAEMON_FD_LIMIT")
        if fd_limit:
            import resource

            def preexec() -> None:  # runs in the child before exec
                _set_parent_death_signal()
                n = int(fd_limit)
                resource.setrlimit(resource.RLIMIT_NOFILE, (n, n))
            popen_kwargs["preexec_fn"] = preexec
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(SERVICE_DIR), env=env,
                stdout=self._logf, stderr=subprocess.STDOUT, text=True,
                **popen_kwargs,
            )
            _record_owned(leader_pgid(self.proc.pid), self.tmux_namespace.socket,
                          leader_pid=self.proc.pid)
            return self._wait_for_bind(start_off)
        except BaseException:
            self._cleanup_start_failure()
            raise

    def _wait_for_bind(self, start_off: int) -> float:
        while time.monotonic() - self.exec_started < BIND_DEADLINE_S:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"daemon exited before binding (exit={self.proc.returncode})\n"
                    f"{self.tail_log(40)}")
            with open(self.logpath, "r", errors="replace") as fh:
                fh.seek(start_off)
                for line in fh:
                    if "listening on" in line:
                        self.port = int(line.rsplit(":", 1)[1].strip())
                        self.bind_observed = time.monotonic()
                        self._has_bound = True
                        return self.bind_observed - self.exec_started
            time.sleep(0.02)
        raise AssertionError(
            f"daemon did not bind within {BIND_DEADLINE_S}s (exit={self.proc.poll()})\n"
            f"{self.tail_log(40)}")

    def _cleanup_start_failure(self) -> None:
        proc = self.proc
        try:
            if proc is not None:
                terminate_process_group(proc)
        finally:
            self.proc = None
            if not self._has_bound:
                self.tmux_namespace.kill_server()
            if self._logf is not None:
                self._logf.close()
                self._logf = None

    def tail_log(self, n: int = 30) -> str:
        try:
            with open(self.logpath, "r", errors="replace") as fh:
                return "".join(fh.readlines()[-n:])
        except OSError:
            return "(no daemon log)"

    def last_exit(self) -> int | None:
        return self.proc.poll() if self.proc is not None else None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def restart(self) -> float:
        """Same DB, new process — the B10 restart-survival path."""
        if self.proc is not None:
            terminate_process_group(self.proc)
        self.proc = None
        return self.start()

    def sigterm(self, timeout: float = SHUTDOWN_DEADLINE_S) -> int:
        assert self.proc is not None
        terminate_process_group(self.proc, timeout=timeout)
        assert self.proc.returncode is not None
        return self.proc.returncode

    def kill(self) -> None:
        if self.proc is not None:
            terminate_process_group(self.proc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


# --------------------------------------------------------------------------- #
# CPU sampler — the daemon's own event-loop process, in % of one core
# --------------------------------------------------------------------------- #


class CpuSampler:
    """Sample the daemon root PID's CPU and open FDs as % of one core."""

    def __init__(
        self,
        daemon: Daemon,
        interval_s: float = 1.0,
        *,
        platform_name: str | None = None,
        command_runner: Callable[
            [list[str]], subprocess.CompletedProcess[str]
        ] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._daemon = daemon
        self._interval = interval_s
        self._platform = platform_name or sys.platform
        self._command_runner = command_runner or _run_sampler_command
        self._clock = clock or time.monotonic
        self._samples: list[tuple[int, float]] = []
        self._fds: list[int] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _supported(self) -> bool:
        return self._platform.startswith("linux") or self._platform == "darwin"

    def _pid_is_dead(self) -> bool:
        proc = self._daemon.proc
        if proc is None:
            return True
        poll = getattr(proc, "poll", None)
        if not callable(poll):
            return False
        try:
            return poll() is not None
        except (OSError, ProcessLookupError):
            return True

    @staticmethod
    def _pid_error(reason: str, detail: str) -> SamplerInfrastructureError:
        return SamplerInfrastructureError(reason, detail)

    def _daemon_pid(self) -> int:
        proc = self._daemon.proc
        pid = getattr(proc, "pid", None) if proc is not None else None
        if not isinstance(pid, int) or pid <= 0:
            raise self._pid_error("unreadable_pid", "daemon PID is unavailable")
        if self._pid_is_dead():
            raise self._pid_error("dead_pid", f"daemon PID {pid} is not running")
        return pid

    def _command_output(
        self,
        command: list[str],
        *,
        strict: bool,
        failure_reason: str,
    ) -> str | None:
        try:
            result = self._command_runner(command)
        except FileNotFoundError as exc:
            if strict:
                raise self._pid_error("missing_facility", f"{command[0]}: {exc}") from exc
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            if strict:
                raise self._pid_error("command_failed", f"{command[0]}: {exc}") from exc
            return None
        if result.returncode != 0:
            if strict:
                reason = "dead_pid" if self._pid_is_dead() else failure_reason
                raise self._pid_error(reason, f"{command[0]} returned {result.returncode}")
            return None
        return result.stdout or ""

    def _fd_count(self, pid: int, *, strict: bool = False) -> int | None:
        if self._platform.startswith("linux"):
            try:
                return len(os.listdir(f"/proc/{pid}/fd"))
            except OSError as exc:
                if strict:
                    reason = "dead_pid" if self._pid_is_dead() else "unreadable_pid"
                    raise self._pid_error(reason, f"/proc/{pid}/fd: {exc}") from exc
                return None
        if self._platform != "darwin":
            return None
        output = self._command_output(
            [_MACOS_LSOF, "-n", "-P", "-p", str(pid), "-F", "f"],
            strict=strict,
            failure_reason="unreadable_pid",
        )
        count = _parse_macos_fd_output(output or "", pid)
        if count is None and strict:
            reason = "dead_pid" if self._pid_is_dead() else "unreadable_pid"
            raise self._pid_error(
                reason, f"{_MACOS_LSOF} returned no FD records for PID {pid}"
            )
        return count

    def _ticks(self, pid: int) -> float | None:
        try:
            with open(f"/proc/{pid}/stat", "r") as fh:
                return _parse_proc_stat_ticks(fh.read())
        except OSError:
            return None

    def _cpu_seconds(self, pid: int, *, strict: bool = False) -> float | None:
        if self._platform.startswith("linux"):
            value = self._ticks(pid)
        elif self._platform == "darwin":
            output = self._command_output(
                [_MACOS_PS, "-p", str(pid), "-o", "pid=,cputime="],
                strict=strict,
                failure_reason="dead_pid",
            )
            value = _parse_macos_cpu_output(output or "", pid)
        else:
            value = None
        if value is None and strict:
            reason = "dead_pid" if self._pid_is_dead() else "unreadable_pid"
            raise self._pid_error(reason, f"CPU time unavailable for PID {pid}")
        return value

    def preflight(self) -> None:
        """Resolve host support before the soak creates its core fleet."""
        if not self._supported():
            raise self._pid_error("unsupported_platform", f"platform {self._platform!r}")
        pid = self._daemon_pid()
        self._cpu_seconds(pid, strict=True)
        self._fd_count(pid, strict=True)

    def _run(self) -> None:
        proc = self._daemon.proc
        prev_pid = proc.pid if proc else None
        prev_cpu = self._cpu_seconds(prev_pid) if prev_pid else None
        prev_t = self._clock()
        while not self._stop.wait(self._interval):
            proc = self._daemon.proc
            pid = proc.pid if proc else None
            now = self._clock()
            cur = self._cpu_seconds(pid) if pid else None
            if pid != prev_pid or cur is None or prev_cpu is None:
                prev_pid, prev_cpu, prev_t = pid, cur, now
                continue
            if now > prev_t:
                pct = 100.0 * (cur - prev_cpu) / (now - prev_t)
                if pct >= 0:
                    self._samples.append((pid, pct))
            prev_cpu, prev_t = cur, now
            fds = self._fd_count(pid)
            if fds is not None:
                self._fds.append(fds)

    def start(self) -> None:
        self.preflight()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def mark(self) -> int:
        return len(self._samples)

    def fd_start(self) -> int:
        return self._fds[0] if self._fds else 0

    def fd_max(self) -> int:
        return max(self._fds) if self._fds else 0

    def fd_last(self) -> int:
        return self._fds[-1] if self._fds else 0

    @property
    def samples(self) -> list[float]:
        return [value for _pid, value in self._samples]

    @property
    def sample_pids(self) -> list[int]:
        return [pid for pid, _value in self._samples]

    def avg(self) -> float:
        values = self.samples
        return sum(values) / len(values) if values else 0.0

    def peak(self) -> float:
        return max(self.samples, default=0.0)

    def window_avg(self, start: int) -> float:
        values = self.samples[start:]
        return sum(values) / len(values) if values else 0.0

    def window_peak(self, start: int) -> float:
        return max(self.samples[start:], default=0.0)

    def window_n(self, start: int) -> int:
        return len(self._samples[start:])


# --------------------------------------------------------------------------- #
# latency bookkeeping
# --------------------------------------------------------------------------- #


@dataclass
class LatencyStats:
    """Every RPC's wall latency, thread-safe. p95 is the gated number."""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _samples: list[float] = field(default_factory=list)
    errors: int = 0
    total: int = 0

    def record(self, seconds: float) -> None:
        with self._lock:
            self._samples.append(seconds)
            self.total += 1

    def record_error(self) -> None:
        with self._lock:
            self.errors += 1
            self.total += 1

    def percentile(self, pct: float) -> float:
        with self._lock:
            if not self._samples:
                return 0.0
            ordered = sorted(self._samples)
        k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
        return ordered[k]

    def p50(self) -> float:
        return self.percentile(50)

    def p95(self) -> float:
        return self.percentile(95)

    def p99(self) -> float:
        return self.percentile(99)

    def max(self) -> float:
        with self._lock:
            return max(self._samples) if self._samples else 0.0


# --------------------------------------------------------------------------- #
# correlated RPC client — skips interleaved broadcasts (child_report_ready,
# coalesced snapshots) by matching request_id, and times each call
# --------------------------------------------------------------------------- #


_FIXTURE_TOKEN_LOCK = threading.Lock()


class RpcClient:
    """One persistent WS connection. `call` sends a uniquely-tagged frame and
    reads until the correlated reply arrives, discarding any broadcast frames in
    between. Latency is recorded on the shared stats object."""

    def __init__(self, url: str, stats: LatencyStats, tag: str, *, spawn_cwd: Path | None = None) -> None:
        # A strict full soak can legitimately produce a list_sessions reply
        # larger than websockets' 1 MiB client default after sustained churn.
        self._ws = connect(url, open_timeout=BIND_DEADLINE_S, max_size=None)
        self._url = url
        self._authenticated_sid: str | None = None
        self._stats = stats
        self._tag = tag
        self._seq = 0
        self._spawn_cwd = spawn_cwd

    def call(self, payload: dict, *, timeout: float = 5.0, record: bool = True) -> dict:
        if payload.get("type") == "close":
            if self._spawn_cwd is None:
                raise AssertionError("soak close requires isolated fixture credentials")
            sid = payload["stream_id"]
            path = _seat_token_path(self._spawn_cwd, sid)
            # Explicit-command fixtures have no launch token. Exercise the
            # real bootstrap-once API; share it across fixture connections.
            with _FIXTURE_TOKEN_LOCK:
                if not path.exists():
                    granted = self.call({"type": "grant_token", "stream_id": sid}, record=False)
                    if granted.get("type") != "grant_token.ok":
                        raise AssertionError(f"fixture token grant failed: {granted.get('error_code')}")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w") as secret:
                        secret.write(granted["stream_token"])
                token = path.read_text().strip()
            # Seat identity is connection-bound. A later fixture's self-close
            # must use a fresh socket, never impersonate it on the prior seat's.
            if self._authenticated_sid not in (None, sid):
                self._ws.close()
                self._ws = connect(self._url, open_timeout=BIND_DEADLINE_S, max_size=None)
            self._authenticated_sid = sid
            payload = {**payload, "from_stream_id": sid, "stream_token": token}
        self._seq += 1
        rid = f"{self._tag}-{self._seq}"
        payload = {**payload, "request_id": rid}
        start = time.monotonic()
        self._ws.send(json.dumps(payload))
        deadline = start + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no correlated reply for {rid} in {timeout}s")
                frame = json.loads(self._ws.recv(timeout=remaining))
                if frame.get("request_id") == rid:
                    if record:
                        self._stats.record(time.monotonic() - start)
                    return frame
                # else: a broadcast or a stale reply — skip it.
        except Exception:
            if record:
                self._stats.record_error()
            raise

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# fleet seeding
# --------------------------------------------------------------------------- #


def _seat_token_path(spawn_cwd: Path, stream_id: str) -> Path:
    host, name = stream_id.split(":", 1)
    return Path(stream_token_file_for(local_machine(host, cwd=str(spawn_cwd)), name))


def assert_quiescent_population(db: str, expected: set[str]) -> dict[str, int]:
    """Use durable rows, including hidden seats, rather than filtered UI lists."""
    import sqlite3

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        actual = {
            f"{host}:{name}" for host, name in conn.execute(
                "SELECT host, session_name FROM sessions WHERE status='open'"
            )
        }
    assert actual == expected, (
        f"soak population mismatch: open={len(actual)} expected={len(expected)}; "
        f"unexpected={sorted(actual - expected)[:10]} missing={sorted(expected - actual)[:10]}"
    )
    return {"open": len(actual), "expected": len(expected), "open_churn": 0}


def seed_offline_host(db: str, host: str, count: int) -> list[str]:
    """Write open rows for an unreachable peer BEFORE the daemon starts, so it
    adopts them at boot. Spawn is localhost-only, so these can only be reached by
    non-mutating verbs; close/tell must answer `unsupported_host` fast. Returns
    the stream_ids."""
    import asyncio

    ids = [f"{host}:offline-{i}" for i in range(count)]

    async def _go() -> None:
        store = Store(db)
        store.start()
        try:
            for i in range(count):
                await store.open_session(host, f"offline-{i}", visibility="visible", role="worker")
        finally:
            store.stop()

    asyncio.run(_go())
    return ids


def spawn_session(client: RpcClient, name: str, *, brief: str = "", handoff_from: str | None = None,
                  timeout: float = 45.0) -> dict:
    payload = {"objective": "Exercise the existing spawn contract",
        "type": "spawn", "host": LOCAL_HOST, "session_name": name,
        "command": STUB_COMMAND, "prompt": brief, "role": "worker", "visibility": "hidden",
    }
    if handoff_from:
        payload["handoff_from_stream_id"] = handoff_from
    return client.call(payload, timeout=timeout)


def kill_tracked_panes(tmux_namespace: TmuxNamespace, names: list[str]) -> None:
    """Clean up every tmux session the run created. B10 means the daemon leaves
    its panes alive on shutdown — the harness owns their teardown, so a pane
    surviving until here is expected, not a leak. The true leak signal is
    `residual_sessions` AFTER this cleanup."""
    for name in names:
        tmux_namespace.run("kill-session", "-t", f"={name}:")


def residual_sessions(tmux_namespace: TmuxNamespace, prefix: str) -> list[str]:
    """tmux sessions whose name starts with `prefix` still alive right now. After
    teardown this must be empty — anything left is a genuine leak (a pane the run
    created and lost track of, or one an uncommanded spawn made)."""
    return [n for n in tmux_namespace.session_names() if n.startswith(prefix)]
