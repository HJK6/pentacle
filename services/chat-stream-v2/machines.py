"""machines.py - fleet host config + remote SSH command construction.

LIFTED from the retired v1 machine configuration plus the SSH-dispatch half of
`session.py::_run`), per the v1 code-reuse map (lane 5): "host config
(machines.json), remote tmux/SSH command construction — config + mechanics
fine; only the probe model is diseased." The config parser and the multiplexed
SSH option set are healthy v1 code and are migrated here, not reinvented. What
is redesigned (the probe pool + circuit breaker) lives in `hosts.py`.

Adaptation (cross-cutting reuse rule): v1 logic ports, v1 I/O placement does
not. v1's `_run` shelled out synchronously (`subprocess.run`, shell=True for
local); v2 execs argv off the event loop. So the LOCAL-shell branch of `_run` is
dropped (v2's `Tmux` execs tmux argv directly, never via a shell), and only the
remote-argv construction is lifted — `ssh_command` returns the argv v2 execs
asynchronously with its own `wait_for` bound (the async form of v1's
`timeout=20` -> synthetic-nonzero "never freeze the loop" guard).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MachineConfig:
    """One fleet machine. `ssh_target is None` marks the local machine — the
    only field the hosts lane consumes beyond `name` and `tmux_bin`; the rest
    are carried through verbatim so other lanes (spawn launch/env, provider
    paths) can reuse the same parsed config."""

    name: str
    ssh_target: str | None
    tmux_bin: str = "tmux"
    claude_bin: str = ""
    codex_bin: str = "codex"
    projects_root: str = ""
    cwd: str = ""
    agent_orch_bin_dir: str | None = None
    label: str | None = None

    @property
    def is_local(self) -> bool:
        return self.ssh_target is None


DEFAULT_MACHINES_FILE = "~/.config/pentacle-stream/machines.json"


def _expand(value: str) -> str:
    return str(Path(value).expanduser()) if value.startswith("~") else value


def _default_local_machine() -> MachineConfig:
    home = Path.home()
    return MachineConfig(
        name="local",
        ssh_target=None,
        tmux_bin="tmux",
        claude_bin=str(home / ".local/bin/claude"),
        codex_bin="codex",
        projects_root=str(home / ".claude/projects"),
        cwd=str(home / "agent-workspace"),
        label="Local",
    )


def _machine_from_dict(raw: dict) -> MachineConfig:
    if not isinstance(raw, dict):
        raise TypeError("machine entry must be an object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("machine entry missing required field: name")
    defaults = _default_local_machine()
    raw_target = raw.get("ssh_target")
    if raw_target is None:
        ssh_target = None
    else:
        ssh_target = str(raw_target).strip()
        if not ssh_target:
            raise ValueError(f"machine entry {name} has a blank ssh_target")
    is_remote = ssh_target is not None
    if is_remote:
        for field in (
            "claude_bin", "codex_bin", "cwd", "projects_root",
            "agent_orch_bin_dir",
        ):
            value = raw.get(field)
            if value is not None and str(value).strip().startswith("~"):
                raise ValueError(f"remote machine entry {name} requires absolute {field}")
    # A remote entry must never inherit the daemon host's home-directory
    # provider paths. Keep omitted remote fields empty so the active-provider
    # validator can reject an unusable tuple instead of silently launching a
    # peer session with local paths.
    claude_default = "" if is_remote else defaults.claude_bin
    codex_default = "" if is_remote else defaults.codex_bin
    projects_default = "" if is_remote else defaults.projects_root
    cwd_default = "" if is_remote else defaults.cwd
    return MachineConfig(
        name=name,
        ssh_target=ssh_target,
        tmux_bin=_expand(str(raw.get("tmux_bin") or defaults.tmux_bin)),
        claude_bin=_expand(str(raw.get("claude_bin") or claude_default)),
        codex_bin=_expand(str(raw.get("codex_bin") or codex_default)),
        projects_root=_expand(str(raw.get("projects_root") or projects_default)),
        cwd=_expand(str(raw.get("cwd") or cwd_default)),
        agent_orch_bin_dir=(
            _expand(str(raw["agent_orch_bin_dir"]))
            if raw.get("agent_orch_bin_dir")
            else None
        ),
        label=str(raw["label"]).strip() if raw.get("label") else None,
    )


def _machines_from_payload(payload: object) -> tuple[MachineConfig, ...]:
    entries = payload.get("machines") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("machine config must be a list or an object with a machines list")
    machines = tuple(_machine_from_dict(entry) for entry in entries)
    if not machines:
        raise ValueError("machine config must define at least one machine")
    names = [m.name for m in machines]
    if len(names) != len(set(names)):
        raise ValueError("machine config contains duplicate machine names")
    return machines


def load_machines(env: dict[str, str] | None = None) -> tuple[MachineConfig, ...]:
    """v1's resolution order, verbatim: inline `PENTACLE_MACHINES_JSON`, then
    `PENTACLE_MACHINES_FILE`, then `~/.config/pentacle-stream/machines.json`,
    else a single local machine. No peers configured => v2 is localhost-only."""
    env = env or os.environ
    inline = env.get("PENTACLE_MACHINES_JSON")
    if inline:
        return _machines_from_payload(json.loads(inline))
    file_name = env.get("PENTACLE_MACHINES_FILE")
    if file_name:
        config_path = Path(file_name).expanduser()
    else:
        home = Path(env.get("HOME") or str(Path.home())).expanduser()
        config_path = home / ".config/pentacle-stream/machines.json"
    if config_path.exists():
        return _machines_from_payload(json.loads(config_path.read_text(encoding="utf-8")))
    return (_default_local_machine(),)


def get_local_machine_name(machines: tuple[MachineConfig, ...]) -> str | None:
    for machine in machines:
        if machine.is_local:
            return machine.name
    return None


def configured_local_host() -> str:
    """Resolve tool identity using explicit identity or the configured local machine."""
    for key in ("PENTACLE_HOST_ID", "AGENT_ORCH_HOST_ID"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    host = get_local_machine_name(load_machines())
    if not host:
        raise ValueError("no local machine configured; set PENTACLE_HOST_ID")
    return host


def configured_host_names(env_key: str, *, remote_only: bool = False) -> tuple[str, ...]:
    """Resolve live-tool targets from an explicit list or the machine allowlist."""
    if env_key in os.environ:
        hosts = tuple(host.strip() for host in os.environ[env_key].split(",") if host.strip())
        if not hosts or len(hosts) != len(set(hosts)):
            raise ValueError(f"{env_key} must name a nonempty, unique host list")
        return hosts
    return tuple(machine.name for machine in load_machines() if not remote_only or not machine.is_local)


# -- remote SSH command construction (lifted from v1 session._run) ------------

SSH_CONTROL_DIR_ENV = "PENTACLE_SSH_CONTROL_DIR"
SSH_CONTROL_PERSIST_ENV = "PENTACLE_SSH_CONTROL_PERSIST"


def _ssh_control_dir() -> Path:
    override = os.environ.get(SSH_CONTROL_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ssh" / "cm"


#: A unix-domain socket path is capped at ~108 bytes. If the final path would
#: exceed this, ssh aborts with "ControlPath too
#: long" and EVERY remote call returns non-zero — which the probe pool would
#: read as the peer being offline. So multiplexing is used only when the socket
#: path is safely short; otherwise it is dropped (each call reconnects, slower,
#: but correct). Guards against a long HOME / config dir silently blacking out
#: the whole fleet.
_CONTROL_PATH_LIMIT = 100


def ssh_control_path(ssh_target: str) -> Path | None:
    """Return the deterministic, target-derived daemon control socket."""
    control_dir = _ssh_control_dir()
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", ssh_target).strip("._-") or "target"
    digest = hashlib.sha256(ssh_target.encode()).hexdigest()[:16]
    path = control_dir / f"cm-{prefix[:24]}-{digest}.sock"
    if len(os.fsencode(path)) > _CONTROL_PATH_LIMIT:
        return None
    control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    control_dir.chmod(0o700)
    return path


def ssh_command(
    ssh_target: str, remote_command: str, *, ssh_bin: str = "ssh", connect_timeout: float = 5.0,
    multiplex: bool = True, create_master: bool = True,
) -> list[str]:
    """v1's `_run` remote option set: BatchMode (no prompts), ControlMaster/Path/
    Persist connection multiplexing (so the receipt-poll's repeated captures
    reuse ONE connection instead of a handshake each), ConnectTimeout, and
    ServerAlive so a post-connect hang is detected. The multiplexing is what
    makes remote receipt polling viable; the ServerAlive + an async `wait_for` on
    the exec are the "never freeze the loop" guard. Multiplexing is skipped when
    the control socket path would overflow the kernel limit (see above), or
    explicitly disabled for transport-independent reachability probes.

    `create_master=False` is the mux HEALTH probe: attach to an existing master
    only (`ControlMaster=no` on the named path) and never create one. If a master
    is wedged the attached session hangs (the caller's bound catches it); if none
    exists ssh makes a direct connection instead of forking a throwaway master
    that the wedge-reset then can't address (`-O exit` on nothing → rc 255)."""
    opts = [
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=1",
    ]
    path = ssh_control_path(ssh_target) if multiplex else None
    if not multiplex:
        # Reachability probes must not queue behind a long-running channel on
        # the shared master. Explicitly defeat ssh_config multiplexing too.
        opts += ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    elif path is not None and not create_master:
        # Attach-only health probe: use the master if present, else connect
        # directly. No ControlPersist because this leg never becomes a master.
        opts += ["-o", "ControlMaster=no", "-o", f"ControlPath={path}"]
    elif path is not None:
        persist = os.environ.get(SSH_CONTROL_PERSIST_ENV, "300")
        opts += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={path}",
            "-o", f"ControlPersist={persist}",
        ]
    return [ssh_bin, *opts, ssh_target, remote_command]


def ssh_tmux_command(
    ssh_target: str, tmux_bin: str, tmux_args: tuple[str, ...], *,
    ssh_bin: str = "ssh", connect_timeout: float = 5.0,
) -> list[str]:
    """The argv to run `tmux <args>` on a peer: the tmux invocation is
    shell-quoted into the single command string SSH hands the peer's login
    shell, exactly as v1 builds its remote tmux strings (`shlex.quote` per part).
    Session names / commands with spaces survive the extra remote-shell hop that
    the local direct-exec path does not have."""
    return ssh_command(
        ssh_target, " ".join(_quote_remote_tmux_word(value) for value in (tmux_bin, *tmux_args)),
        ssh_bin=ssh_bin, connect_timeout=connect_timeout,
    )


def _quote_remote_tmux_word(value: str) -> str:
    """Quote one tmux argv word for the peer's login shell.

    ``shlex.join`` deliberately leaves ``=name:`` unquoted because it is safe
    for POSIX ``sh``. macOS peers commonly use zsh, where an unquoted word
    beginning with ``=`` undergoes command-path expansion (``=name`` means the
    path of command ``name``). Exact tmux targets intentionally use that prefix,
    so force shell quotes for them while retaining normal ``shlex.quote`` for
    every other argument.
    """
    if value.startswith("="):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return shlex.quote(value)
