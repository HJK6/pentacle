#!/usr/bin/env python3
"""satellite.py — stateless per-host chat-ingest push agent with auto-update.

Runs supervised on a remote fleet host (systemd-user on linux-workstation/WSL, launchd
on workstation). Tails that host's LOCAL provider transcripts, normalizes each new
turn with the provider-specific wire-stable normalizer, and pushes batches over
one outbound WebSocket to coordinator's `event.push` verb. coordinator owns the durable store
and exactly-once dedupe; the satellite holds NO local DB and only in-memory byte
offsets — a restart rescans from offset 0 and the store dedupes the replay.

Why this exists (`spec_example_2026_01`): remote-host
session chat had no live path to coordinator's `session_event_tail` (mobile only saw
pre-cutover history). This replaces both v1's per-session SSH tail pipes (the
recurring remote-tail wedge) and v2's never-built "remote host runs its own
daemon" deferral, with the minimum moving part: a push-only ingest agent.

DESIGN INVARIANTS (mirrors ingest.py, the local counterpart):
  - Session membership from LOCAL tmux, not coordinator's registry (the satellite has
    no registry). A pentacle session's tmux name IS its `session_name`, so
    `stream_id = f"{host}:{session_name}"` matches the row coordinator's registry holds
    (this is what makes an linux-workstation:v2-* push land on the right session).
  - NO per-file tail thread. One bounded-cadence pass reads new bytes off no
    event loop the daemon owns; a partial trailing line is left for next pass.
  - Four loop rules (event-loop rule 2): cadence, per-pass event cap, exponential
    backoff, kill switch (`PENTACLE_SATELLITE_DISABLE=1`). Reconnect is its own
    backoff loop.
  - At-least-once on the wire, exactly-once at rest: an offset advances ONLY
    after coordinator acks the batch (high-water echo). A dropped ack re-reads and the
    store dedupes — never a loss, never a duplicate broadcast.
  - CURRENT-FIRST ordering: sessions are tailed newest-transcript-mtime first,
    and on first bind the tail starts within a bounded history horizon
    (`history_bytes`) rather than at offset 0 — so a long-lived session's live
    turns reach mobile immediately instead of queuing behind its whole backlog.

VERSION GATE + AUTO-UPDATE (the crux): every push carries this checkout's git SHA
and the wire schema version. When coordinator's ack says `update_required` naming a
target SHA, the satellite runs `git fetch origin && git checkout <sha>` in its
own checkout and exec-restarts (the service manager also respawns). It only ever
acts on a SHA coordinator NAMES; code only ever comes from git origin. A failed update
leaves offsets unmoved and pushes rejected until the running process reports
the target checkout. The
recovery-spec lesson is honored: the update path has a hard rate limit, a
no-op guard when already on target, and a full kill switch — no runaway loop.

CODEX: Codex panes hold their rollout JSONL open in a child process rather than
stamping Claude's `--session-id` argument. Discovery therefore finds the file
through the pane's bounded process tree, and the existing Codex normalizer maps
only its renderable response items to the same wire event contract.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import websockets

from claude_jsonl_norm import normalize_claude_jsonl_records_grouped
from codex_rollout_norm import codex_session_identity, normalize_codex_rollout_records_grouped
from machine_stats import STATS_INTERVAL_S, WIRE_VERSION as STATS_WIRE_VERSION, sample_machine_stats
from mirror import _provider_from_session_name

log = logging.getLogger("chat_streamd_v2.satellite")

_TMUX_SOCKET_ABSENT_ERROR = re.compile(
    r"error connecting to (?P<socket>\S+) \(No such file or directory\)",
    re.IGNORECASE,
)
_TMUX_SOCKET_NO_SERVER = (
    "error connecting to /tmp/tmux-1000/default (No such file or directory)"
)


def _tmux_reports_authoritative_empty(returncode: int, stderr: str, stdout: str) -> bool:
    """Recognize only tmux's affirmative no-server outcomes.

    A missing socket path is deterministic evidence when the error exposes it.
    The observed 3.6a Linux diagnostic and classic wording remain strict
    compatibility fallbacks; unrelated tmux failures remain inconclusive.
    """
    if returncode != 1:
        return False
    detail = f"{stderr}\n{stdout}"
    match = _TMUX_SOCKET_ABSENT_ERROR.search(detail)
    if match is not None and not os.path.exists(match.group("socket")):
        return True
    lowered = detail.lower()
    return (
        _TMUX_SOCKET_NO_SERVER.lower() in lowered
        or "no server running on " in lowered
        or "failed to connect to server" in lowered
    )

#: Wire schema version of the payloads this agent sends. MUST equal
#: `event_push.WIRE_VERSION` on coordinator (a unit test pins the two together); bumped
#: only on an incompatible change to the normalized event shape.
WIRE_VERSION = 1

#: Root of the per-project `<uuid>.jsonl` transcript tree; overridable for
#: tests / a non-default HOME. Working-state observations do not require a
#: transcript, so Codex panes are surfaced from the same tmux discovery pass.
#: Codex rollout paths are discovered from the process that owns the open file,
#: so they need no separate root setting.
DEFAULT_CLAUDE_PROJECTS_ROOT = "~/.claude/projects"

DEFAULT_BART_WS = "ws://127.0.0.1:7791"
DEFAULT_INTERVAL_S = 1.0
DEFAULT_MAX_EVENTS_PER_PASS = 500
DEFAULT_MAX_READ_BYTES = 4 * 1024 * 1024
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0
#: On first bind, replay at most this many trailing bytes of a transcript (see
#: SatelliteConfig.history_bytes). ~1 MiB is many recent turns without dragging a
#: multi-MB backlog ahead of current activity.
DEFAULT_HISTORY_BYTES = 1024 * 1024
#: WS frame ceiling — matches the server's WS_MAX_SIZE (4 MiB) so a large batch
#: is bounded the same both ways.
WS_MAX_SIZE = 4 * 1024 * 1024
DEFAULT_PANE_SCROLLBACK = 80
MAX_PANE_CAPTURE_BYTES = 128 * 1024
#: Floor on seconds between self-update ATTEMPTS, so a checkout that keeps
#: failing (or a flapping pin) can never spin git in a tight loop.
DEFAULT_UPDATE_MIN_INTERVAL_S = 30.0
#: While tmux discovery keeps failing (e.g. an unresolvable `tmux` — the remote-host
#: PATH outage), re-log at most this often. A satellite that cannot discover
#: says so every cycle-band without spamming the ~1.5s loop; silence is the bug
#: (spec_example_2026_01).
DISCOVER_FAIL_LOG_INTERVAL_S = 60.0

ENV_PREFIX = "PENTACLE_SATELLITE_"


def _repo_root() -> str:
    """The git checkout this file lives in: <repo>/services/chat-stream-v2/
    satellite.py → two parents up is <repo>. This is the checkout auto-update
    operates on."""
    return str(Path(__file__).resolve().parents[2])


@dataclass
class SatelliteConfig:
    bart_ws: str = DEFAULT_BART_WS
    host: str = ""                      # this machine's fleet name; MUST match registry host
    push_secret: str = ""
    checkout: str = ""                  # git checkout root for auto-update
    session_glob: str = "v2-*"          # tmux session-name filter (pentacle sessions)
    claude_projects_root: str = DEFAULT_CLAUDE_PROJECTS_ROOT
    #: History horizon: on FIRST bind, seek to the last `history_bytes` of the
    #: transcript rather than replaying it whole, so a long-lived session's
    #: CURRENT turns reach mobile immediately instead of being starved behind its
    #: entire backlog. <0 replays the whole file. Ongoing appends are unaffected.
    history_bytes: int = DEFAULT_HISTORY_BYTES
    interval_s: float = DEFAULT_INTERVAL_S
    first_delay_s: float | None = None
    max_events_per_pass: int = DEFAULT_MAX_EVENTS_PER_PASS
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S
    disabled: bool = False              # kill switch: connect but push nothing
    no_autoupdate: bool = False         # never git-checkout/exec-restart
    update_min_interval_s: float = DEFAULT_UPDATE_MIN_INTERVAL_S
    ack_timeout_s: float = 30.0

    @classmethod
    def from_env(cls, env: dict | None = None) -> "SatelliteConfig":
        e = os.environ if env is None else env

        def num(name, default, cast):
            raw = e.get(ENV_PREFIX + name)
            if raw is None or raw == "":
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                log.warning("ignoring bad %s%s=%r", ENV_PREFIX, name, raw)
                return default

        def flag(name):
            return str(e.get(ENV_PREFIX + name) or "").strip().lower() in ("1", "true", "yes", "on")

        return cls(
            bart_ws=e.get(ENV_PREFIX + "WS") or e.get(ENV_PREFIX + "BART_WS") or DEFAULT_BART_WS,
            host=(e.get(ENV_PREFIX + "HOST") or socket.gethostname().split(".")[0]).strip().lower(),
            # The shared bearer secret is intentionally the same env name coordinator reads.
            push_secret=e.get("PENTACLE_EVENT_PUSH_SECRET") or "",
            checkout=e.get(ENV_PREFIX + "CHECKOUT") or _repo_root(),
            session_glob=e.get(ENV_PREFIX + "SESSION_GLOB") or "v2-*",
            claude_projects_root=e.get(ENV_PREFIX + "CLAUDE_PROJECTS_ROOT") or DEFAULT_CLAUDE_PROJECTS_ROOT,
            history_bytes=num("HISTORY_BYTES", DEFAULT_HISTORY_BYTES, int),
            interval_s=num("INTERVAL_S", DEFAULT_INTERVAL_S, float),
            first_delay_s=num("FIRST_DELAY_S", None, float),
            max_events_per_pass=num("MAX_EVENTS", DEFAULT_MAX_EVENTS_PER_PASS, int),
            max_read_bytes=num("MAX_READ_BYTES", DEFAULT_MAX_READ_BYTES, int),
            disabled=flag("DISABLE"),
            no_autoupdate=flag("NO_AUTOUPDATE"),
            update_min_interval_s=num("UPDATE_MIN_INTERVAL_S", DEFAULT_UPDATE_MIN_INTERVAL_S, float),
        )


@dataclass
class _StreamTail:
    """Per-session tail state — never persisted; rebuilt on restart, the store's
    durable identity makes the replay-from-0 exactly-once."""

    session_name: str
    path: str = ""
    offset: int = 0                     # byte offset of the last COMPLETE line acked
    provider_session_id: str = ""       # identity guard against a foreign transcript
    provider: str = ""                  # inferred from the bound transcript path
    source_pane_pid: str = ""           # ephemeral proof for a Codex push only
    primed: bool = False                # first-bind history-horizon seek applied?


@dataclass(frozen=True)
class _DiscoveredPane:
    session_name: str
    provider: str
    transcript_path: str = ""
    pane_text: str = ""
    pane_pid: int = 0


#: The claude `--session-id <uuid>` / `--resume <uuid>` the pentacle launch
#: stamps on every spawned pane (spawnctl → launch.build_launch). The transcript
#: is `<projects>/<encoded-cwd>/<uuid>.jsonl`; we resolve it by the unique uuid,
#: NOT by lsof — claude on Linux appends-and-closes the file (holds no fd), so
#: the lsof probe local ingest uses on macOS finds nothing on WSL.
_SESSION_UUID = re.compile(r"--(?:session-id|resume)[= ]([0-9a-fA-F-]{36})")


def _pane_cmdlines(pids: list[int]) -> dict[int, str]:
    """{pid: full argv string} for the given pane pids, in one `ps` call.
    `-ww` defeats argv truncation; works on both Linux and macOS/BSD."""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "pid=,args=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    cmds: dict[int, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        try:
            cmds[int(head)] = rest
        except ValueError:
            continue
    return cmds


def _session_uuid(cmdline: str) -> str:
    m = _SESSION_UUID.search(cmdline)
    return m.group(1).lower() if m else ""


def _infer_provider_from_pane(session_name: str, pane_text: str) -> str | None:
    named_provider = _provider_from_session_name(session_name)
    if named_provider:
        return named_provider
    lowered = pane_text.lower()
    if "openai codex" in lowered or "codex" in lowered and "claude code" not in lowered:
        return "codex"
    if "claude code" in lowered or "bypass permissions" in lowered:
        return "claude"
    return None


def _infer_provider_from_cmdline(cmdline: str) -> str | None:
    lowered = str(cmdline or "").lower()
    if "codex" in lowered:
        return "codex"
    if "claude" in lowered:
        return "claude"
    return None


def _is_codex_cmdline(cmdline: str) -> bool:
    """Return true when a pane command line contains the Codex executable."""
    return bool(re.search(r"(?:^|[\s/])codex(?:$|[\s/])", cmdline, re.IGNORECASE))


def _is_codex_rollout_path(path: str) -> bool:
    """Recognize a Codex rollout without making the configured HOME global."""
    normalized = str(path or "").replace("\\", "/")
    return "/.codex/sessions/" in normalized and normalized.endswith(".jsonl")


def _descendant_pids(root_pid: int) -> list[int]:
    """Return a bounded process tree rooted at one tmux pane pid."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [root_pid]
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, ppid = (int(value) for value in fields)
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    found: list[int] = []
    pending = [root_pid]
    seen = {root_pid}
    while pending:
        pid = pending.pop(0)
        found.append(pid)
        for child in children.get(pid, []):
            if child not in seen:
                seen.add(child)
                pending.append(child)
    return found


def _codex_transcript_for_pid(pane_pid: int) -> str:
    """Find the active Codex rollout held by a pane or one of its children."""
    pids = _descendant_pids(pane_pid)
    try:
        result = subprocess.run(
            ["lsof", "-n", "-p", ",".join(str(pid) for pid in pids), "-Fn"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    paths = {
        line[1:]
        for line in result.stdout.splitlines()
        if line.startswith("n") and _is_codex_rollout_path(line[1:])
    }
    return max(paths, key=_mtime, default="")


def _claude_session_uuid_for_pid(pane_pid: int, direct_uuid: str, stream_id: str = "") -> str:
    """Prefer the deepest fork-session uuid below a Claude pane; if none is a
    descendant, match a detached fork-session host by stream id."""
    pids = _descendant_pids(pane_pid)
    cmds = _pane_cmdlines(pids)
    for pid in reversed(pids):
        cmd = cmds.get(pid, "")
        uuid = _session_uuid(cmd)
        if uuid and "--fork-session" in cmd:
            return uuid
    return _detached_fork_session_uuid(stream_id) or direct_uuid


_PROC_ROOT = Path("/proc")


def _detached_fork_session_uuid(stream_id: str, proc_root: Path = _PROC_ROOT) -> str:
    """Claude Code >= 2.1.239 hosts a forked session under `claude bg-pty-host`
    reparented to pid 1 — outside the tmux pane tree, so the descendant walk
    never sees it. Those processes inherit PENTACLE_STREAM_ID from the pane, so
    without /proc this is a no-op and the direct binding stands."""
    if not stream_id or not proc_root.is_dir():
        return ""
    needle = f"PENTACLE_STREAM_ID={stream_id}".encode()
    best_uuid, best_start = "", -1
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes()
            if b"--fork-session" not in cmd:
                continue
            if needle not in (entry / "environ").read_bytes().split(b"\0"):
                continue
            uuid = _session_uuid(cmd.replace(b"\0", b" ").decode("utf-8", "replace"))
            if not uuid:
                continue
            stat_fields = (entry / "stat").read_text().rsplit(")", 1)[-1].split()
            start = int(stat_fields[19]) if len(stat_fields) > 19 else 0
        except (OSError, ValueError):
            continue
        if start >= best_start:
            best_uuid, best_start = uuid, start
    return best_uuid


def _codex_session_identity_from_bound_path(path: str) -> str:
    """Read Codex's head-only session identity before the history horizon.

    Discovery obtained ``path`` from an FD held by a descendant of the observed
    tmux pane.  Re-open the same path read-only here solely to inspect its
    immutable prefix before any tail seek can skip ``session_meta``.  The value
    remains in-memory source evidence; no provider-to-session mapping is stored.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return ""
    try:
        prefix = os.pread(fd, 64 * 1024, 0)
    except OSError:
        return ""
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    for line in prefix.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        found = codex_session_identity(record) if isinstance(record, dict) else ""
        if found:
            return found
    return ""


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _git(checkout: str, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", checkout, *args],
        capture_output=True, text=True, timeout=timeout,
    )


class Satellite:
    def __init__(self, config: SatelliteConfig | None = None) -> None:
        self.config = config or SatelliteConfig.from_env()
        self.sha = self._read_sha()
        self._tails: dict[str, _StreamTail] = {}
        self._req = 0
        self._last_update_attempt = 0.0
        self._update_failures = 0
        self._last_stats_at = 0.0
        #: uuid -> resolved transcript path, so the projects tree is rglob'd once
        #: per session, not every discovery pass.
        self._uuid_path: dict[str, str] = {}
        #: Pane working state is intentionally in-memory and generation-free;
        #: the open session identity comes from this pass's tmux enumeration.
        #: None is failed/inconclusive; a list is this pass's authoritative tmux membership.
        self._last_discovery_inventory: list[str] | None = None
        #: Discovery-failure rate-limit state (0.0 = discovery is healthy). A
        #: broken `tmux` lookup must be logged, never swallowed into a silent
        #: empty pass.
        self._discover_fail_since = 0.0
        self._discover_fail_logged_at = 0.0

    def _read_sha(self) -> str:
        try:
            cp = _git(self.config.checkout, "rev-parse", "HEAD")
            return cp.stdout.strip() if cp.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    # -- discovery ------------------------------------------------------------

    def _discover(self) -> dict[str, _DiscoveredPane]:
        """Discover surfaceable panes and their optional transcripts.

        Membership follows tmux (O(open)); the transcript is resolved from the
        pane's `--session-id <uuid>` (the pentacle launch stamps it), not lsof —
        Claude holds no fd on the file on Linux. Codex rollouts resolve from an
        open file in the pane's bounded process tree. Working state is captured
        from every Claude/Codex pane in this same pass, including panes with no
        transcript yet.
        """
        cmd = ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"]
        self._last_discovery_inventory = None
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            # tmux unresolvable/unrunnable (the remote-host PATH outage) — ingest nothing
            # this pass, but LOUDLY, and keep the loop alive to recover.
            self._note_discover_failure(cmd, exc)
            return {}
        out = str(getattr(result, "stdout", "") or "")
        returncode = int(getattr(result, "returncode", 0) or 0)
        if returncode != 0:
            stderr = str(getattr(result, "stderr", "") or "")
            detail = f"{stderr}\n{out}".lower()
            if _tmux_reports_authoritative_empty(returncode, stderr, out):
                # This is tmux's affirmative no-server result, not a failed
                # transport.  It is the authoritative empty-inventory case.
                self._last_discovery_inventory = []
                self._note_discover_ok()
                return {}
            self._note_discover_failure(cmd, RuntimeError(
                f"tmux list-panes rc={returncode}: {detail.strip()[:200]}"
            ))
            return {}
        self._note_discover_ok()
        panes: dict[str, int] = {}
        for line in out.splitlines():
            if "\t" not in line:
                continue
            name, pid_s = line.split("\t", 1)
            if (
                not (
                    fnmatch.fnmatch(name, self.config.session_glob)
                    or re.fullmatch(r"fleet-smoke-(?:codex|claude)-[0-9a-f]{8}", name)
                )
                or name in panes
            ):
                continue
            try:
                panes[name] = int(pid_s)
            except ValueError:
                continue
        # Inventory membership is taken from tmux before provider/transcript
        # classification.  A live pane with an unrecognized provider remains
        # present; only absence from this authenticated listing is death proof.
        self._last_discovery_inventory = sorted(panes)
        if not panes:
            return {}
        cmds = _pane_cmdlines(list(panes.values()))
        mapping: dict[str, _DiscoveredPane] = {}
        for name, pid in panes.items():
            cmdline = cmds.get(pid, "")
            pane = self._capture_pane(name)
            # The pane's own command line outranks pane text: a Claude chat
            # that *talks about* Codex (and never prints "claude code") was
            # classified codex and bound to the wrong transcript (R84).
            provider = (
                _provider_from_session_name(name)
                or _infer_provider_from_cmdline(cmdline)
                or _infer_provider_from_pane(name, pane)
            )
            uuid = _session_uuid(cmdline)
            if provider == "claude":
                uuid = _claude_session_uuid_for_pid(pid, uuid, f"{self.config.host}:{name}")
            path = self._transcript_for(uuid) if uuid else ""
            if not path and _is_codex_cmdline(cmdline):
                path = _codex_transcript_for_pid(pid)
            # A named Claude session with a transcript is the existing ingest
            # fallback when the pane is between rendered provider markers.
            if provider is None and path:
                provider = "claude"
            if provider in {"claude", "codex"}:
                mapping[name] = _DiscoveredPane(
                    session_name=name,
                    provider=provider,
                    transcript_path=path,
                    pane_text=pane,
                    pane_pid=pid,
                )
        return mapping

    def _capture_pane(self, session_name: str) -> str:
        """Capture a bounded pane tail; called only from the worker thread."""
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-J", "-p", "-t", session_name,
                 "-S", f"-{DEFAULT_PANE_SCROLLBACK}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if getattr(result, "returncode", 1) != 0:
            return ""
        text = str(getattr(result, "stdout", "") or "")
        return text[-MAX_PANE_CAPTURE_BYTES:]

    def _note_discover_failure(self, cmd: list[str], exc: BaseException) -> None:
        """Log a discovery failure with the failing command + errno. First
        failure of a streak logs immediately; while it persists it re-logs at
        most every DISCOVER_FAIL_LOG_INTERVAL_S — rate-limited, never silent."""
        now = time.monotonic()
        first = self._discover_fail_since == 0.0
        if first:
            self._discover_fail_since = now
        if first or now - self._discover_fail_logged_at >= DISCOVER_FAIL_LOG_INTERVAL_S:
            self._discover_fail_logged_at = now
            log.error("session discovery failed: %s: %s (errno=%s); ingesting "
                      "nothing until it recovers", " ".join(cmd), exc,
                      getattr(exc, "errno", None))

    def _note_discover_ok(self) -> None:
        """Clear the failure streak, logging one recovery line if we had been
        failing — so the log shows exactly when ingest resumed."""
        if self._discover_fail_since:
            log.warning("session discovery recovered after %.0fs",
                        time.monotonic() - self._discover_fail_since)
            self._discover_fail_since = 0.0
            self._discover_fail_logged_at = 0.0

    def _transcript_for(self, uuid: str) -> str:
        """Resolve `<uuid>.jsonl` under the claude projects root, cached. The
        cwd→dir encoding claude uses is not replicated — the uuid is unique, so a
        single rglob finds the exact file regardless of which project it is in."""
        cached = self._uuid_path.get(uuid)
        if cached and os.path.exists(cached):
            return cached
        root = Path(self.config.claude_projects_root).expanduser()
        try:
            hit = next(root.rglob(f"{uuid}.jsonl"), None)
        except OSError:
            hit = None
        path = str(hit) if hit else ""
        if path:
            self._uuid_path[uuid] = path
        return path

    # -- tail pass ------------------------------------------------------------

    def _collect(
        self,
        discovered: dict[str, _DiscoveredPane | str] | None = None,
    ) -> tuple[list[dict], dict[str, int], bool]:
        """One bounded tail pass across all discovered sessions. Returns
        (events, {path: pending_offset}, any_capped). pending_offset is the byte
        offset each file may advance to ONCE coordinator acks — a capped stream exposes a
        PARTIAL offset just past the record prefix it pushed this pass, so it
        drains over successive passes instead of re-collecting the same prefix.
        `discovered` is injected in tests; production passes None to use local tmux."""
        cfg = self.config
        if discovered is None:
            discovered = self._discover()
        else:
            # Test/injected discovery is already the caller's explicit tmux
            # snapshot, including `{}` for an empty server.
            self._last_discovery_inventory = sorted(str(name) for name in discovered)
        discovered = {
            name: pane if isinstance(pane, _DiscoveredPane) else _DiscoveredPane(
                session_name=name,
                provider="claude",
                transcript_path=str(pane or ""),
            )
            for name, pane in discovered.items()
        }
        # Forget tails whose session closed (no filesystem scan; O(open)).
        for name in list(self._tails):
            if name not in discovered:
                self._tails.pop(name, None)

        budget = cfg.max_events_per_pass
        events: list[dict] = []
        high_water: dict[str, int] = {}
        capped = False
        # Newest-active session first: its current turns reach mobile ahead of a
        # quieter session's, and ahead of any session's older backlog.
        for name, pane in sorted(
            discovered.items(),
            key=lambda kv: _mtime(kv[1].transcript_path),
            reverse=True,
        ):
            if budget <= 0:
                capped = True
                break
            path = pane.transcript_path
            if not path:
                continue
            st = self._tails.setdefault(name, _StreamTail(session_name=name))
            provider = "codex" if pane.provider == "codex" or _is_codex_rollout_path(path) else pane.provider
            if st.path != path:
                # A Claude fork handoff replays its new transcript from head;
                # initial binds retain the horizon and the store dedupes replays.
                fork_rebind = bool(st.path) and st.provider == provider == "claude"
                st.path, st.offset, st.provider_session_id, st.provider, st.source_pane_pid, st.primed = (
                    path, 0, "", "", "", fork_rebind
                )
            if st.source_pane_pid == f"!{pane.pane_pid}":
                continue
            st.provider = provider
            st.source_pane_pid = str(pane.pane_pid or "")
            if provider == "codex":
                # Bind identity while the session_meta header is still readable;
                # a first-bind history seek may begin after it.  The proof is
                # only usable with the observed tmux pane PID carried below.
                source_identity = _codex_session_identity_from_bound_path(path)
                if not source_identity or not st.source_pane_pid:
                    st.path, st.offset, st.provider_session_id, st.provider, st.source_pane_pid = "", 0, "", "", ""
                    continue
                if st.provider_session_id and st.provider_session_id != source_identity:
                    st.path, st.offset, st.provider_session_id, st.provider, st.source_pane_pid = "", 0, "", "", ""
                    continue
                st.provider_session_id = source_identity
            n_before = len(events)
            try:
                pending, stream_capped = self._collect_stream(st, budget, events)
            except Exception as exc:  # noqa: BLE001 - one sick stream never kills the pass
                del events[n_before:]  # drop any partial contribution
                log.warning("stream %s collect failed: %s; skipping this pass", name, exc)
                continue
            budget -= (len(events) - n_before)
            if stream_capped:
                capped = True          # this stream hit the per-pass cap
            if pending != st.offset:
                # Advance to the prefix pushed this pass (a capped stream advances
                # PAST what it pushed rather than re-collecting it forever).
                high_water[path] = pending
        return events, high_water, capped

    def _collect_stream(self, st: _StreamTail, budget: int, out: list[dict]) -> tuple[int, bool]:
        """Read st's transcript from st.offset; append up to `budget` new events
        to `out`. Returns (advance_offset, capped): the byte offset the file may
        advance to on ack, and whether the per-pass cap was hit. Even when capped
        the offset advances byte-accurately PAST the records whose events were
        pushed, so a stream whose span exceeds one pass's budget drains over
        successive passes instead of re-collecting the same prefix forever."""
        try:
            size = os.path.getsize(st.path)
        except OSError:
            st.path = ""           # gone/rotated — rediscover next pass
            return st.offset, False
        if not st.primed:
            # History horizon: on first read of this bind, start near EOF so a
            # long backlog never precedes current turns. A partial leading line
            # (we may land mid-record) simply fails to parse and is skipped.
            hb = self.config.history_bytes
            if hb >= 0 and size > hb:
                st.offset = size - hb
            st.primed = True
        if size < st.offset:
            st.offset = 0          # truncation/rotation: replay, dedupe keeps it exact
        if size == st.offset:
            return st.offset, False
        read_to = min(size, st.offset + self.config.max_read_bytes)
        try:
            with open(st.path, "rb") as fh:
                fh.seek(st.offset)
                chunk = fh.read(max(0, read_to - st.offset))
        except OSError:
            st.path = ""
            return st.offset, False
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return st.offset, False   # no complete line yet
        consumed = chunk[: last_nl + 1]
        new_offset = st.offset + len(consumed)

        records: list[dict] = []
        # File offset just past each kept record's raw line, so a capped pass can
        # advance the offset to exactly the last record it pushed (the remote twin
        # of B2's byte-accurate ingest cap). Lengths come off RAW bytes; decoding
        # with "replace" is not byte-reversible, so it cannot be measured after.
        record_end_offsets: list[int] = []
        cursor = st.offset
        provider = st.provider or ("codex" if _is_codex_rollout_path(st.path) else "claude")
        st.provider = provider
        for raw_line in consumed.splitlines(keepends=True):
            cursor += len(raw_line)
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if provider == "codex":
                rec_sid = codex_session_identity(record)
            else:
                rec_sid = str(record.get("sessionId") or record.get("session_id") or "")
            if rec_sid:
                if not st.provider_session_id:
                    st.provider_session_id = rec_sid
                elif rec_sid != st.provider_session_id:
                    # Foreign record → bound path is wrong; unbind and rediscover.
                    st.path, st.offset, st.provider_session_id, st.provider = "", 0, "", ""
                    return 0, False
            records.append(record)
            record_end_offsets.append(cursor)

        if not records:
            return new_offset, False
        host, name = self.config.host, st.session_name
        if provider == "codex":
            # coordinator consumes this one-shot source proof during admission and
            # removes it before durable storage.  It is not a provider identity
            # registry, event field, or compatibility path.
            if not st.source_pane_pid or not st.provider_session_id:
                return 0, False
            groups = normalize_codex_rollout_records_grouped(
                records, host=host, session_name=name, session_id=st.provider_session_id,
            )
            for group in groups:
                for payload in group:
                    payload["source_pane_pid"] = st.source_pane_pid
        else:
            groups = normalize_claude_jsonl_records_grouped(records, host=host, session_name=name)
        # Per-pass cap with a byte-accurate advance. Append a whole record's events
        # only while they fit THIS stream's remaining `budget` (count what this
        # stream adds, len(out)-start, not cumulative len(out) — else a 2nd stream
        # is starved while budget remains, QA D1), then advance the offset PAST the
        # records pushed. Unlike coordinator-local ingest the satellite has no genuine-
        # insert signal (dedup is remote), so it advances on the raw record
        # boundary; a capped stream still drains instead of re-collecting the same
        # prefix forever (this lane's fix). A capped stream that can fit no record
        # (a single record's events exceed budget) leaves the offset unmoved — the
        # same benign edge B2 carries; budget=500 vs per-record event counts.
        start = len(out)
        consumed_to = st.offset
        capped = False
        for group, end_offset in zip(groups, record_end_offsets):
            if group and (len(out) - start) + len(group) > budget:
                capped = True
                break
            out.extend(group)
            consumed_to = end_offset
        if not capped:
            # Whole span consumed, including any trailing non-record bytes.
            consumed_to = new_offset
        return consumed_to, capped

    # -- push / ack -----------------------------------------------------------

    async def _send_host_stats(self, ws) -> dict | None:
        self._req += 1
        rid = self._req
        try:
            stats = await asyncio.to_thread(sample_machine_stats, self.config.host)
        except Exception as exc:  # noqa: BLE001 - retry on the next sample interval
            log.warning("machine stats sample failed: %s", exc)
            return None
        await ws.send(json.dumps({
            "type": "host.stats",
            "request_id": rid,
            "push_secret": self.config.push_secret,
            "satellite_sha": self.sha,
            "satellite_pid": os.getpid(),
            "wire_version": STATS_WIRE_VERSION,
            "host": self.config.host,
            "stats": stats,
        }))
        deadline = asyncio.get_running_loop().time() + self.config.ack_timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                log.warning("host stats ack timeout (rid=%s)", rid)
                return None
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(msg, dict) and msg.get("request_id") == rid:
                return msg

    async def _push(
        self,
        ws,
        events: list[dict],
        high_water: dict[str, int],
        inventory: list[str] | None = None,
    ) -> dict | None:
        self._req += 1
        rid = self._req
        frame = {
            "type": "event.push",
            "request_id": rid,
            "push_secret": self.config.push_secret,
            "satellite_sha": self.sha,
            "satellite_pid": os.getpid(),
            "wire_version": WIRE_VERSION,
            "host": self.config.host,
            "events": events,
            "high_water": high_water,
        }
        if inventory is not None:
            frame["inventory"] = inventory
        frozen_streams = sorted(
            f"{self.config.host}:{name}"
            for name, tail in self._tails.items()
            if tail.source_pane_pid.startswith("!")
        )
        # Recovery acks are at-least-once: retain this proof until the daemon
        # observes a later request after `_apply_ack` has released the tail.
        # Always send the field, including the empty acknowledgement, so the
        # daemon can distinguish an updated satellite from a legacy caller.
        frame["frozen_streams"] = frozen_streams
        await ws.send(json.dumps(frame))
        # Read frames until our reply arrives; the daemon fans broadcasts (incl.
        # our own chat.event echoes) to every client — those are ignored here.
        deadline = asyncio.get_running_loop().time() + self.config.ack_timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                log.warning("push ack timeout (rid=%s)", rid)
                return None
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict) or msg.get("request_id") != rid:
                continue
            return msg

    def _apply_ack(self, ack: dict, high_water: dict[str, int]) -> str | None:
        """Advance acked offsets and read the version verdict. Returns a target
        SHA to update to, or None."""
        kind = str(ack.get("type") or "")
        if kind == "event.push.error":
            log.warning("push rejected: %s", ack.get("error"))
            version = ack.get("version") if isinstance(ack.get("version"), dict) else {}
            if version.get("status") == "update_required":
                return str(version.get("target_sha") or "") or None
            return None
        if kind != "event.push.ok":
            return None
        dropped_streams: set[str] = set()
        for item in ack.get("dropped") or ():
            if isinstance(item, dict):
                stream_id = str(item.get("stream_id") or "")
                if stream_id.startswith(f"{self.config.host}:"):
                    tail = self._tails.get(stream_id.removeprefix(f"{self.config.host}:"))
                    if tail is not None:
                        tail.source_pane_pid = f"!{tail.source_pane_pid.removeprefix('!')}"
                        dropped_streams.add(stream_id)
        # A frozen tail has no events to prove its own recovery.  The daemon
        # therefore names only streams whose previously closed row has opened
        # again; leave a stream named in this same ack's drop list frozen.
        for stream_id in ack.get("reopened") or ():
            if not isinstance(stream_id, str) or stream_id in dropped_streams:
                continue
            if not stream_id.startswith(f"{self.config.host}:"):
                continue
            tail = self._tails.get(stream_id.removeprefix(f"{self.config.host}:"))
            if tail is not None and tail.source_pane_pid.startswith("!"):
                tail.source_pane_pid = tail.source_pane_pid.removeprefix("!")
                log.info("tail resumed after daemon-confirmed row reopen stream=%s", stream_id)
        # Durable accept confirmed → advance each acked file's offset.
        for name, st in self._tails.items():
            if f"{self.config.host}:{name}" not in dropped_streams and st.path in high_water:
                st.offset = high_water[st.path]
        version = ack.get("version") if isinstance(ack.get("version"), dict) else {}
        if version.get("status") == "update_required":
            return str(version.get("target_sha") or "") or None
        return None

    # -- auto-update ----------------------------------------------------------

    def _maybe_update(self, target_sha: str) -> None:
        """git fetch/checkout the target and exec-restart. No-op guards + a hard
        rate limit + a kill switch keep this from ever spinning. On failure: back
        off and return — the caller keeps pushing flagged-stale."""
        # `startswith` also absorbs a truncated (short-SHA) pin, so a manual short
        # pin can't wedge us in perpetual failed-update retries (post-checkout
        # HEAD is the full 40-char SHA and would never == a short target).
        if not target_sha or self.sha.startswith(target_sha):
            return
        if self.config.no_autoupdate:
            log.error("update_required -> %s but autoupdate disabled; staying stale", target_sha[:12])
            return
        now = time.monotonic()
        backoff = min(self.config.backoff_max_s,
                      self.config.update_min_interval_s * (2 ** self._update_failures))
        if now - self._last_update_attempt < backoff:
            return             # inside the (possibly backed-off) update window
        self._last_update_attempt = now
        log.warning("update_required: %s -> %s; fetch+checkout", (self.sha or "?")[:12], target_sha[:12])
        try:
            fetch = _git(self.config.checkout, "fetch", "origin", timeout=120.0)
            if fetch.returncode != 0:
                raise RuntimeError(f"git fetch failed: {fetch.stderr.strip()[:200]}")
            co = _git(self.config.checkout, "checkout", "--detach", target_sha, timeout=60.0)
            if co.returncode != 0:
                raise RuntimeError(f"git checkout failed: {co.stderr.strip()[:200]}")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self._update_failures += 1
            log.error("self-update to %s failed (%d): %s; keep pushing flagged-stale",
                      target_sha[:12], self._update_failures, exc)
            return
        head = self._read_sha()
        if head != target_sha:
            self._update_failures += 1
            log.error("post-checkout HEAD %s != target %s; staying", head[:12], target_sha[:12])
            return
        log.warning("self-update OK -> %s; exec-restart", target_sha[:12])
        self._exec_restart()

    def _exec_restart(self) -> None:  # pragma: no cover - replaces the process
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])

    # -- loops ----------------------------------------------------------------

    async def _session(self, ws) -> None:
        """Cadence loop for one live connection. Runs until the socket drops
        (raises ConnectionClosed) — the outer loop reconnects."""
        cfg = self.config
        delay = cfg.interval_s if cfg.first_delay_s is None else cfg.first_delay_s
        if not cfg.disabled:
            ack = await self._send_host_stats(ws)
            version = ack.get("version") if isinstance(ack, dict) else None
            target = version.get("target_sha") if isinstance(version, dict) else None
            if target:
                await asyncio.to_thread(self._maybe_update, str(target))
            self._last_stats_at = time.monotonic()
        while True:
            await asyncio.sleep(delay)
            delay = cfg.interval_s
            if cfg.disabled:
                continue           # kill switch: stay connected, push nothing
            if time.monotonic() - self._last_stats_at >= STATS_INTERVAL_S:
                ack = await self._send_host_stats(ws)
                version = ack.get("version") if isinstance(ack, dict) else None
                target = version.get("target_sha") if isinstance(version, dict) else None
                if target:
                    await asyncio.to_thread(self._maybe_update, str(target))
                self._last_stats_at = time.monotonic()
            events, high_water, _capped = await asyncio.to_thread(self._collect)
            inventory = self._last_discovery_inventory
            ack = await self._push(ws, events, high_water, inventory=inventory)
            if ack is None:
                continue           # no ack: offsets unmoved, re-read next pass
            target = self._apply_ack(ack, high_water)
            if target:
                # Blocking git work (and a possible exec-restart) off the loop.
                await asyncio.to_thread(self._maybe_update, target)

    async def run_forever(self) -> None:
        cfg = self.config
        failures = 0
        while True:
            try:
                async with websockets.connect(
                    cfg.bart_ws, max_size=WS_MAX_SIZE,
                    ping_interval=20, ping_timeout=20, close_timeout=10,
                ) as ws:
                    log.info("connected to %s as host=%s sha=%s",
                             cfg.bart_ws, cfg.host, (self.sha or "?")[:12])
                    failures = 0
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport fault
                failures += 1
                back = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (failures - 1)))
                log.warning("connection lost (%d): %s; reconnect in %.0fs", failures, exc, back)
                await asyncio.sleep(back)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("PENTACLE_SATELLITE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = SatelliteConfig.from_env()
    if not cfg.host:
        log.error("no host configured (set PENTACLE_SATELLITE_HOST)")
        return 2
    sat = Satellite(cfg)
    try:
        asyncio.run(sat.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
