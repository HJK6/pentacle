from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from machines import MachineConfig, ssh_command, ssh_control_path
from prockill import terminate_and_reap
from sessions import VerbError
from tmux_transport import Tmux, _exec
from v2_runtime import env_number

log = logging.getLogger("chat_streamd_v2.hosts")


async def _bounded_exec(*args: str, timeout: float) -> tuple[int, str, bool]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        return 127, "", False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await terminate_and_reap(proc)
        raise
    except asyncio.TimeoutError:
        await terminate_and_reap(proc)
        return 1, "", True
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace"), False


def _unlink_control_socket(path: Path) -> None:
    path.unlink(missing_ok=True)


@dataclass
class HostsConfig:
    """Loop-rule knobs for the probe pool. `from_env` keeps them discoverable
    the way `RetentionConfig` does, so a plist can tune cadence/timeouts without
    a code change."""

    #: Sweep cadence: how often `run_forever` re-examines which peers are due.
    tick_s: float = 5.0
    #: Steady-state re-probe interval for a healthy peer.
    interval_s: float = 20.0
    #: Hard bound on one SSH probe (wait_for). Must exceed connect_timeout_s.
    probe_timeout_s: float = 8.0
    #: SSH `ConnectTimeout` — bounds the TCP/auth handshake to a dead host.
    connect_timeout_s: float = 5.0
    #: Consecutive failures that trip the breaker into `circuit_open`.
    breaker_threshold: int = 3
    #: Backoff after a failure: base doubles per extra failure, capped.
    backoff_base_s: float = 5.0
    backoff_cap_s: float = 300.0
    #: Bounded worker pool = per-pass concurrency cap (loop rule).
    pool_size: int = 4
    #: Backoff after an unexpected sweep error (loop rule).
    error_backoff_s: float = 5.0

    @classmethod
    def from_env(cls) -> "HostsConfig":
        return cls(
            tick_s=env_number(os.environ, "PENTACLE_HOSTS_TICK_S", cls.tick_s, float),
            interval_s=env_number(os.environ, "PENTACLE_HOSTS_INTERVAL_S", cls.interval_s, float),
            probe_timeout_s=env_number(
                os.environ, "PENTACLE_HOSTS_PROBE_TIMEOUT_S", cls.probe_timeout_s, float,
            ),
            connect_timeout_s=env_number(
                os.environ, "PENTACLE_HOSTS_CONNECT_TIMEOUT_S", cls.connect_timeout_s, float,
            ),
            breaker_threshold=env_number(
                os.environ, "PENTACLE_HOSTS_BREAKER_THRESHOLD", cls.breaker_threshold, int,
            ),
            backoff_base_s=env_number(
                os.environ, "PENTACLE_HOSTS_BACKOFF_BASE_S", cls.backoff_base_s, float,
            ),
            backoff_cap_s=env_number(
                os.environ, "PENTACLE_HOSTS_BACKOFF_CAP_S", cls.backoff_cap_s, float,
            ),
            pool_size=env_number(os.environ, "PENTACLE_HOSTS_POOL_SIZE", cls.pool_size, int),
            error_backoff_s=env_number(
                os.environ, "PENTACLE_HOSTS_ERROR_BACKOFF_S", cls.error_backoff_s, float,
            ),
        )


@dataclass
class HostState:
    """One peer's reachability. `online is None` means never probed — reported
    as offline/`pending_probe` on the wire (binary; never "unknown")."""

    online: bool | None = None
    reason: str = "pending_probe"
    consecutive_failures: int = 0
    breaker_open: bool = False
    #: Monotonic gate: the loop skips this host until now >= next_probe_at.
    next_probe_at: float = 0.0
    #: Wall-clock of the last completed probe, for the wire `checked_at`.
    last_checked_at: float = 0.0
    last_latency_ms: int | None = None
    ever_online: bool = False


def _host_status_payload(host: str, *, online: bool, reason: str, checked_at: float,
                         latency_ms: int | None) -> dict[str, Any]:
    """v1's `host.status` shape (chat_streamd `_host_status_payload`), minus the
    "unknown" branch v2 never emits. Mobile/desktop enumerate these field names,
    so the shape is kept verbatim for the kept fields."""
    host_status = "online" if online else ("degraded" if reason.endswith("_after_online") else "offline")
    return {
        "host": host,
        "online": online,
        "host_status": host_status,
        "host_status_reason": "" if online else reason,
    }


class Hosts:
    """Peer reachability and the remote transport seam."""

    def __init__(
        self,
        local_host: str = "localhost",
        peers: dict[str, MachineConfig] | None = None,
        *,
        tmux_bin: str = "tmux",
        ssh_bin: str = "ssh",
        config: HostsConfig | None = None,
        on_status_change: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.local_host = local_host
        self.peers = dict(peers or {})
        self.tmux_bin = tmux_bin
        self.ssh_bin = ssh_bin
        self.cfg = config or HostsConfig()
        #: Optional broadcast: emit a `host.status` frame when a peer flips.
        self._on_status_change = on_status_change
        self._state: dict[str, HostState] = {h: HostState() for h in self.peers}
        self._sem = asyncio.Semaphore(self.cfg.pool_size)
        # Kept for the module docstring's "breakers" contract / introspection.
        self.breakers = self._state

    # -- classification ----------------------------------------------------

    def is_local(self, host: str) -> bool:
        return host == self.local_host

    def known(self, host: str) -> bool:
        return host == self.local_host or host in self.peers

    def known_offline(self, host: str) -> bool:
        """Return a cached positive-offline hint without probing."""
        st = self._state.get(host)
        if st is None:
            return False
        return st.breaker_open or st.online is False

    def is_online(self, host: str) -> bool:
        """Return true only for current positive reachability evidence."""
        if host == self.local_host:
            return True
        st = self._state.get(host)
        return bool(st and st.online is True and not st.breaker_open)

    async def ensure_reachable(self, host: str, what: str) -> None:
        """Fence a remote verb with cached state or one bounded probe."""
        if host == self.local_host:
            return
        if host not in self.peers:
            raise VerbError("unsupported_host", f"{host} is not a configured peer for v2 {what}")
        st = self._state[host]
        if st.breaker_open:
            code = "host_degraded" if st.ever_online else "host_offline"
            raise VerbError(code, f"{host} is {st.reason} for {what}")
        if st.online is True:
            return
        if not await self.probe_once(host):
            state = self._state[host]
            code = "host_degraded" if state.ever_online else "host_offline"
            raise VerbError(code, f"{host} is {state.reason} for {what}")

    # -- transport seam ----------------------------------------------------

    def tmux_for(self, host: str) -> Tmux:
        """Return a Tmux scoped to the requested host."""
        if host == self.local_host or host not in self.peers:
            return Tmux(self.tmux_bin)
        peer = self.peers[host]
        return Tmux(
            peer.tmux_bin,  # the peer's OWN tmux path, not the local one
            ssh_bin=self.ssh_bin,
            ssh_target=peer.ssh_target,
            connect_timeout=self.cfg.connect_timeout_s,
        )

    async def run_command(
        self, host: str, *args: str, timeout: float = 10.0, multiplex: bool = True,
    ) -> tuple[int, str]:
        if host == self.local_host:
            return await _exec(*args, timeout=timeout)
        peer = self.peers.get(host)
        if peer is None:
            return 127, "unknown host"
        remote = shlex.join(str(arg) for arg in args)
        return await _exec(
            *ssh_command(
                peer.ssh_target,
                remote,
                ssh_bin=self.ssh_bin,
                connect_timeout=self.cfg.connect_timeout_s,
                multiplex=multiplex,
            ),
            timeout=timeout,
        )

    # -- probe -------------------------------------------------------------

    async def probe_once(self, host: str) -> bool:
        # Reachability gets an independent SSH connection. A shared multiplexed
        # master can serialize a short ``true`` behind long remote tmux/proof
        # work; its 8s deadline would then measure queue starvation rather than
        # host health and trip the breaker against a reachable peer.
        argv = ssh_command(
            self.peers[host].ssh_target, "true",
            ssh_bin=self.ssh_bin, connect_timeout=self.cfg.connect_timeout_s,
            multiplex=False,
        )
        start = time.monotonic()
        rc, _ = await _exec(*argv, timeout=self.cfg.probe_timeout_s)
        latency_ms = int((time.monotonic() - start) * 1000)
        recorded = self._record(host, rc == 0, latency_ms)
        if rc != 0:
            return recorded
        path = ssh_control_path(self.peers[host].ssh_target)
        if path is None:
            return recorded

        target = self.peers[host].ssh_target or ""
        check_rc, check_out, check_timeout = (0, "", False)
        if path.exists():
            check_rc, check_out, check_timeout = await _bounded_exec(
                self.ssh_bin, "-S", str(path), "-O", "check", target,
                timeout=self.cfg.probe_timeout_s,
            )
        if check_rc != 0 or check_timeout:
            mux_rc, mux_timeout = check_rc, check_timeout
        else:
            mux_rc, _, mux_timeout = await _bounded_exec(
                *ssh_command(
                    target, "true", ssh_bin=self.ssh_bin,
                    connect_timeout=self.cfg.connect_timeout_s,
                    create_master=False,
                ),
                timeout=self.cfg.probe_timeout_s,
            )
        if mux_rc == 0:
            return recorded

        pid_match = re.search(r"Master running \(pid=(\d+)\)", check_out)
        exit_rc, _, exit_timeout = await _bounded_exec(
            self.ssh_bin, "-S", str(path), "-O", "exit", target,
            timeout=self.cfg.probe_timeout_s,
        )
        try:
            _unlink_control_socket(path)
            unlinked = True
        except OSError:
            unlinked = False
        log.warning(
            "SSH ControlMaster reset host=%s socket=%s mux_failure=%s "
            "control_check=%s master_pid=%s control_exit=%s unlinked=%s",
            host, path, "timeout" if mux_timeout else f"rc_{mux_rc}",
            "timeout" if check_timeout else f"rc_{check_rc}",
            pid_match.group(1) if pid_match else None,
            "timeout" if exit_timeout else f"rc_{exit_rc}", unlinked,
        )
        return recorded

    def _record(self, host: str, ok: bool, latency_ms: int) -> bool:
        st = self._state[host]
        was_online = st.online
        was_reason = st.reason
        was_breaker_open = st.breaker_open
        prior_failures = st.consecutive_failures
        st.last_checked_at = time.time()
        now = time.monotonic()
        if ok:
            st.online = True
            st.ever_online = True
            st.reason = ""
            st.consecutive_failures = 0
            st.breaker_open = False
            st.last_latency_ms = latency_ms
            st.next_probe_at = now + self.cfg.interval_s
            if was_breaker_open:
                log.info(
                    "host probe breaker closed host=%s consecutive_failures=%d "
                    "reason=successful_half_open_probe",
                    host, prior_failures,
                )
        else:
            st.online = False
            st.last_latency_ms = None
            st.consecutive_failures += 1
            if st.consecutive_failures >= self.cfg.breaker_threshold:
                opened = not st.breaker_open
                st.breaker_open = True
                st.reason = "circuit_open_after_online" if st.ever_online else "circuit_open"
                if opened:
                    log.warning(
                        "host probe breaker opened host=%s probe=ssh_true "
                        "consecutive_failures=%d prior_online=%s reason=%s",
                        host, st.consecutive_failures, st.ever_online, st.reason,
                    )
            else:
                st.reason = "unreachable_after_online" if st.ever_online else "unreachable"
            over = max(0, st.consecutive_failures - self.cfg.breaker_threshold)
            backoff = min(self.cfg.backoff_cap_s, self.cfg.backoff_base_s * (2 ** over))
            st.next_probe_at = now + backoff
        if (
            was_online is not st.online
            or was_reason != st.reason
            or was_breaker_open != st.breaker_open
        ):
            self._announce(host)
        return bool(st.online)

    def _announce(self, host: str) -> None:
        if self._on_status_change is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._on_status_change(self._payload(host)))
        except RuntimeError:  # pragma: no cover - no running loop (unit probe)
            pass

    # -- background loop ---------------------------------------------------

    async def run_forever(self) -> None:
        """Sweep on `tick_s`; probe only peers whose per-host backoff is due;
        bound concurrency to the pool. A hanging peer holds ONE worker slot for
        at most `probe_timeout_s` — never the loop, never the other peers."""
        while True:
            try:
                now = time.monotonic()
                due = [h for h in self.peers if self._state[h].next_probe_at <= now]
                if due:
                    await asyncio.gather(*(self._guarded_probe(h) for h in due))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sweep error backs off, never dies
                log.exception("hosts sweep failed")
                await asyncio.sleep(self.cfg.error_backoff_s)
                continue
            await asyncio.sleep(self.cfg.tick_s)

    async def _guarded_probe(self, host: str) -> None:
        async with self._sem:
            await self.probe_once(host)

    # -- wire --------------------------------------------------------------

    def _payload(self, host: str) -> dict[str, Any]:
        st = self._state[host]
        return _host_status_payload(
            host, online=bool(st.online), reason=st.reason or "pending_probe",
            checked_at=st.last_checked_at, latency_ms=st.last_latency_ms,
        )

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The `hosts` dict for `hello`/`snapshot`: the always-online local
        entry plus every peer's live probe state. Binary online/offline; an
        unprobed or breaker-open peer is offline-with-reason, never unknown."""
        hosts: dict[str, dict[str, Any]] = {
            self.local_host: _host_status_payload(
                self.local_host, online=True, reason="", checked_at=time.time(), latency_ms=0
            )
        }
        for host in self.peers:
            hosts[host] = self._payload(host)
        return hosts
