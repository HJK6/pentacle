#!/usr/bin/env python3
"""satellite.py — per-host transcript push agent with pinned auto-update.

Runs supervised on a remote fleet host (systemd-user on linux-workstation/WSL, launchd
on workstation). Tails that host's LOCAL provider transcripts, normalizes each new
turn with the provider-specific wire-stable normalizer, and pushes batches over
one outbound WebSocket to coordinator's `event.push` verb. coordinator owns the durable store
and exactly-once dedupe. The satellite atomically retains byte cursors and
original native-source bindings, never a payload queue or local event database.
Restart replays a fixed retained range through the existing transcript sink.

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
import hashlib
import hmac
import json
import logging
import os
import random
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import websockets
import chat_recovery

from claude_jsonl_norm import normalize_claude_jsonl_records_grouped
from codex_rollout_norm import codex_is_guardian_review, codex_session_identity, normalize_codex_rollout_records_grouped
from machine_stats import STATS_INTERVAL_S, WIRE_VERSION as STATS_WIRE_VERSION, sample_machine_stats
from mirror import _provider_from_session_name
from logging_config import configure_logging
from usage_accounting import FLAG_KIND as PROVENANCE_FLAG_KIND, native_provenance
from usage_admission import iso_ms
from usage_hold import DEFAULT_PATH as DEFAULT_HELD_SPAN_PATH, HeldSpans
from usage_provenance import (
    BACKFILL_BATCH, DEFAULT_CODEX_ROOT, MAX_ITEMS as PROVENANCE_MAX_ITEMS,
    REPROBE_S as PROVENANCE_REPROBE_S, BackfillCursor, codex_header_records,
    for_version as provenance_for_version, negotiated_version, probe_payload, run_backfill,
)

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
#: only on an incompatible change to the normalized event shape. 2 adds held
#: unfenced usage spans, loss records and clock samples (sent only while the
#: most recent ack came from a v2 daemon).
WIRE_VERSION = 2
#: A fenced item is sent for v2 classification only with a clock sample this
#: fresh (satellite monotonic); otherwise its records are held. The daemon
#: refuses a sample older than 600 s by its own clock.
FENCED_CLOCK_MAX_AGE_S = 540.0
HELD_CLOCK_MAX_AGE_S = 600.0
#: Item-level refusals that mean only "this fence is stale"; with a v2 daemon
#: the records are held and resolved from generation history instead.
STALE_FENCE_REASONS = frozenset({
    "session_not_open", "generation_mismatch", "pane_pid_mismatch", "provider_mismatch",
})

#: Root of the per-project `<uuid>.jsonl` transcript tree; overridable for
#: tests / a non-default HOME. Working-state observations do not require a
#: transcript, so Codex panes are surfaced from the same tmux discovery pass.
#: Codex rollout paths are discovered from the process that owns the open file,
#: so they need no separate root setting.
DEFAULT_CLAUDE_PROJECTS_ROOT = "~/.claude/projects"

DEFAULT_COORDINATOR_WS = "ws://127.0.0.1:7791"
DEFAULT_INTERVAL_S = 1.0
DEFAULT_MAX_EVENTS_PER_PASS = 500
DEFAULT_MAX_READ_BYTES = 4 * 1024 * 1024
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0
#: On first bind, replay at most this many trailing bytes of a transcript (see
#: SatelliteConfig.history_bytes). ~1 MiB is many recent turns without dragging a
#: multi-MB backlog ahead of current activity.
DEFAULT_CHAT_CHECKPOINT_PATH = "~/.local/state/pentacle/satellite_chat.json"
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
    coordinator_ws: str = DEFAULT_COORDINATOR_WS
    host: str = ""                      # this machine's fleet name; MUST match registry host
    push_secret: str = ""
    host_secret: str = ""               # per-host HMAC proof for usage/event.push
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
    held_span_path: str = DEFAULT_HELD_SPAN_PATH
    chat_checkpoint_path: str = ""
    update_origin: str = "https://github.com/HJK6/pentacle.git"

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
            coordinator_ws=e.get(ENV_PREFIX + "WS") or DEFAULT_COORDINATOR_WS,
            host=(e.get(ENV_PREFIX + "HOST") or socket.gethostname().split(".")[0]).strip().lower(),
            # The shared bearer secret is intentionally the same env name coordinator reads.
            push_secret=e.get("PENTACLE_EVENT_PUSH_SECRET") or "",
            host_secret=e.get("PENTACLE_EVENT_PUSH_HOST_SECRET") or "",
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
            held_span_path=e.get(ENV_PREFIX + "HELD_SPAN_PATH") or DEFAULT_HELD_SPAN_PATH,
            chat_checkpoint_path=e.get(ENV_PREFIX + "CHAT_CHECKPOINT_PATH") or "",
            update_origin=e.get(ENV_PREFIX + "UPDATE_ORIGIN") or "https://github.com/HJK6/pentacle.git",
        )


@dataclass
class _StreamTail:
    """Native-source tail state; checkpoints omit transient provenance context."""

    session_name: str
    path: str = ""
    offset: int = 0                     # byte offset of the last COMPLETE line acked
    provider_session_id: str = ""       # identity guard against a foreign transcript
    provider: str = ""                  # inferred from the bound transcript path
    source_pane_pid: str = ""           # ephemeral proof for a Codex push only
    primed: bool = False                # first-bind history-horizon seek applied?
    #: Cross-span provenance context (Codex identity, turn models); reset on rebind.
    provenance_state: dict = field(default_factory=dict)
    binding: dict = field(default_factory=dict)
    observed_size: int = 0
    observed_digest: str = ""
    recovery_range: dict | None = None
    recovery_range_id: str = ''
    recovery_parent: dict = field(default_factory=dict)
    recovery_gap: str = ""
    partial_tail_bytes: int = 0


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


def _is_codex_rollout_path(path: str, account: dict | None = None) -> bool:
    """Recognize a Codex rollout without making the configured HOME global."""
    normalized = str(path or "").replace("\\", "/")
    from provider_accounts import transcript_allowed
    return transcript_allowed(normalized, "codex", account)


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


def _codex_transcript_for_pid(pane_pid: int, account: dict | None = None) -> str:
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
        if line.startswith("n") and _is_codex_rollout_path(line[1:], account)
        and _codex_session_identity_from_bound_path(line[1:])
    }
    return next(iter(paths)) if len(paths) == 1 else ""


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
        if codex_is_guardian_review(record):
            return ""  # Internal thread, never a seat's conversation source.
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
        self._load_chat_checkpoint()
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
        self._usage_fences: dict[str, dict[str, str]] = {}
        self._pending_usage: dict[str, dict[str, object]] = {}
        self._pass_usage: dict[str, dict[str, object]] = {}
        self._usage_path_streams: dict[str, set[str]] = {}
        #: Live provenance awaiting a daemon ack, keyed so a re-collected span
        #: never duplicates an item; backfill remains the completeness authority.
        self._pass_provenance: dict[str, list[dict]] = {}
        self._pending_provenance: dict[tuple, dict] = {}
        self._inflight_provenance: dict[tuple, dict] = {}
        #: None until an ack shows whether the daemon understands provenance.
        self._provenance_supported: bool | None = None
        #: Payload version granted by the daemon's answer to the empty version-2
        #: probe (None: not yet probed in this process); a version-1 grant is
        #: re-probed after PROVENANCE_REPROBE_S.
        self._provenance_version: int | None = None
        self._provenance_reprobe_at = 0.0
        self._provenance_probe_inflight = False
        #: Held unfenced spans (usage_hold.py), loaded at the start of a session.
        self._held: HeldSpans | None = None
        self._pass_hold: dict[str, tuple[dict, list[dict]]] = {}
        #: Set only by an ack from a v2 daemon (wire_version >= 2 and a
        #: `usage_unfenced` block); cleared by any other ack or push error.
        self._unfenced_enabled = False
        #: Latest host.stats clock sample and its monotonic receipt time.
        self._clock: dict | None = None
        self._clock_mono = 0.0
        self._inflight_usage: dict[str, dict] = {}
        self._inflight_clock: dict | None = None
        self._held_tick_mono: float | None = None
        #: Distinguishes this process from an exec-restarted one with the same
        #: PID (auto-update), whose request ids restart at 1; the daemon orders
        #: loss frames per (host, pid, incarnation).
        self._incarnation = os.urandom(8).hex()

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
            fence = getattr(self, "_usage_fences", {}).get(f"{self.config.host}:{name}") or {}
            account = fence.get("account_binding")
            if account and (account.get("host") != self.config.host or account.get("provider") != provider):
                continue
            if provider == "claude":
                uuid = _claude_session_uuid_for_pid(pid, uuid, f"{self.config.host}:{name}")
            path = (self._transcript_for(uuid, account) if account else self._transcript_for(uuid)) if uuid else ""
            if not path and _is_codex_cmdline(cmdline):
                path = _codex_transcript_for_pid(pid, account) if account else _codex_transcript_for_pid(pid)
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

    def _transcript_for(self, uuid: str, account: dict | None = None) -> str:
        """Resolve `<uuid>.jsonl` under the claude projects root, cached. The
        cwd→dir encoding claude uses is not replicated — the uuid is unique, so a
        single rglob finds the exact file regardless of which project it is in."""
        root = Path(account["transcript_root"]) if account else Path(self.config.claude_projects_root).expanduser()
        cache_key = (str(root), uuid) if account else uuid
        cached = self._uuid_path.get(cache_key)
        if cached and os.path.exists(cached):
            return cached
        try:
            hits = list(root.glob(f"*/{uuid}.jsonl"))
            if account:
                from provider_accounts import transcript_allowed
                hits = [path for path in hits if transcript_allowed(str(path), "claude", account)]
            hit = hits[0] if len(hits) == 1 else None
        except OSError:
            hit = None
        path = str(hit) if hit else ""
        if path:
            self._uuid_path[cache_key] = path
        return path

    # -- tail pass ------------------------------------------------------------

    @staticmethod
    def _source_file_identity_digest(path: str) -> str:
        """Hash path plus stable file identity; never put the path on the wire."""
        try:
            stat = os.stat(path)
            identity = f"{os.path.abspath(path)}\0{stat.st_dev}\0{stat.st_ino}"
        except OSError:
            identity = os.path.abspath(path)
        return hashlib.sha256(identity.encode("utf-8", "surrogateescape")).hexdigest()

    @staticmethod
    def _sanitize_usage_records(provider: str, records: list[dict]) -> list[dict]:
        """Keep only provider-native identity and token-count fields, plus the
        record's own top-level transcript time as `transcript_ts` (wire v2;
        ignored by legacy admission)."""
        sanitized: list[dict] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            stamp = record.get("timestamp")
            transcript_ts = stamp if isinstance(stamp, str) and stamp else None
            start = len(sanitized)
            if provider == "codex":
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    identity = payload.get("id") if isinstance(payload, dict) else None
                    if isinstance(identity, str) and identity:
                        sanitized.append({"type": "session_meta", "payload": {"id": identity}})
                elif record.get("type") == "event_msg":
                    payload = record.get("payload")
                    info = payload.get("info") if isinstance(payload, dict) else None
                    usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    if (
                        isinstance(payload, dict) and payload.get("type") == "token_count"
                        and isinstance(usage, dict)
                    ):
                        allowed = {
                            key: usage[key] for key in (
                                "input_tokens", "cached_input_tokens",
                                "output_tokens", "reasoning_output_tokens",
                            ) if type(usage.get(key)) is int and usage[key] >= 0
                        }
                        if len(allowed) == 4:
                            sanitized.append({
                                "type": "event_msg",
                                "payload": {"type": "token_count", "info": {"total_token_usage": allowed}},
                            })
            elif provider == "claude" and record.get("type") == "assistant":
                message = record.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                message_id = message.get("id") if isinstance(message, dict) else None
                session_id = record.get("sessionId") or record.get("session_id")
                if isinstance(usage, dict) and isinstance(message_id, str) and message_id and isinstance(session_id, str) and session_id:
                    allowed = {
                        key: usage[key] for key in (
                            "input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens", "output_tokens",
                        ) if type(usage.get(key)) is int and usage[key] >= 0
                    }
                    if len(allowed) == 4:
                        sanitized.append({
                            "type": "assistant", "sessionId": session_id,
                            "message": {"id": message_id, "usage": allowed},
                        })
            for item in sanitized[start:]:
                item["transcript_ts"] = transcript_ts
        return sanitized

    def _usage_candidate(
        self, st: _StreamTail, provider: str, records: list[dict],
        record_end_offsets: list[int], consumed_to: int,
    ) -> dict[str, object] | None:
        """The fenced usage item for this span, or None.

        A span with usage but no matching fence (none issued yet, or the fence
        row closed) is staged in `_pass_hold` instead of being discarded; the
        caller writes it to the held-span file before this pass's push.
        """
        stream_id = f"{self.config.host}:{st.session_name}"
        fences = getattr(self, "_usage_fences", {}).get(stream_id)
        source_pid = str(st.source_pane_pid or "").removeprefix("!")
        if not st.provider_session_id or not source_pid:
            return None
        included = [
            record for record, end_offset in zip(records, record_end_offsets)
            if end_offset <= consumed_to
        ]
        wire_records = self._sanitize_usage_records(provider, included)
        if not wire_records:
            return None
        identity = {
            "stream_id": stream_id,
            "provider": provider,
            "source_pane_pid": source_pid,
            "native_session_id": st.provider_session_id,
            "source_file_identity_digest": self._source_file_identity_digest(st.path),
        }
        if (
            not isinstance(fences, dict)
            or fences.get("provider") != provider
            or fences.get("source_pane_pid") != source_pid
            or not fences.get("session_generation")
        ):
            held = [record for record in wire_records if record.get("type") != "session_meta"]
            if held:
                if not hasattr(self, "_pass_hold"):
                    self._pass_hold = {}
                self._pass_hold[stream_id] = (identity, held)
            return None
        return {
            "stream_id": stream_id,
            "provider": provider,
            "session_generation": str(fences["session_generation"]),
            "source_pane_pid": source_pid,
            "native_session_id": st.provider_session_id,
            "source_file_identity_digest": identity["source_file_identity_digest"],
            "records": wire_records,
        }

    # -- held unfenced spans (wire v2) -------------------------------------------

    def _held_spans(self) -> HeldSpans:
        if getattr(self, "_held", None) is None:
            self._held = HeldSpans(getattr(self.config, "held_span_path", DEFAULT_HELD_SPAN_PATH))
        return self._held

    def _clock_age(self) -> float | None:
        if getattr(self, "_clock", None) is None:
            return None
        return time.monotonic() - self._clock_mono

    def _clock_within(self, max_age_s: float) -> dict | None:
        age = self._clock_age()
        return self._clock if age is not None and age <= max_age_s else None

    def _note_clock(self, ack: dict, t_send_wall: float, t_send_mono: float) -> None:
        """Keep the latest {offset_s, rtt_s, server_now} from a v2 stats ack."""
        from usage_admission import epoch

        server_now = epoch(ack.get("server_now"))
        if server_now is None:
            return
        rtt = max(0.0, time.monotonic() - t_send_mono)
        self._clock = {
            "offset_s": round(server_now - (t_send_wall + rtt / 2), 6),
            "rtt_s": round(rtt, 6),
            "server_now": ack["server_now"],
        }
        self._clock_mono = time.monotonic()
        held = getattr(self, "_held", None)
        if held is not None:
            held.bind_clock(self._clock)

    def _hold_pass(self, stream_id: str) -> None:
        staged = getattr(self, "_pass_hold", {}).get(stream_id)
        if staged is None:
            return
        identity, records = staged
        try:
            self._held_spans().hold(identity, records, self._clock_within(HELD_CLOCK_MAX_AGE_S))
        except OSError as exc:
            # A broken disk must not stop chat ingest; this span's usage is
            # then lost exactly as before the held-span path existed.
            log.error("held-span write failed stream=%s records=%d: %s", stream_id, len(records), exc)

    @staticmethod
    def _prefix_digest(path, length):
        digest = hashlib.sha256()
        with open(path, 'rb') as f:
            remaining = length
            while remaining:
                part = f.read(min(remaining, 1024 * 1024))
                if not part:
                    raise ValueError('transcript truncated')
                remaining -= len(part)
                digest.update(part)
        return digest.hexdigest()

    def _load_chat_checkpoint(self):
        path = Path(os.path.expanduser(self.config.chat_checkpoint_path or DEFAULT_CHAT_CHECKPOINT_PATH))
        self._chat_checkpoint_path = path
        self._recovering = {}
        self._chat_gap = ''
        self._pass_recovery = []
        self._pass_sources = []
        self._pass_chat_gaps = []
        try:
            if path.stat().st_size > WS_MAX_SIZE:
                raise ValueError('oversized metadata checkpoint')
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or data.get('version') != 1 or data.get('host') != self.config.host:
                raise ValueError('checkpoint version/host mismatch')
            if not isinstance(data.get('sources'), list) or len(data['sources']) > chat_recovery.MAX_SOURCES:
                raise ValueError('invalid source checkpoint list')
            for item in data['sources']:
                tail = _StreamTail(**item)
                if (type(tail.offset) is not int or tail.offset < 0
                        or type(tail.observed_size) is not int or tail.observed_size < tail.offset
                        or not isinstance(tail.binding, dict)
                        or not isinstance(tail.path, str) or not isinstance(tail.session_name, str)):
                    raise ValueError('malformed source checkpoint')
                if tail.binding and (not chat_recovery.source_valid(tail.binding, self.config.host)
                        or not isinstance(tail.binding.get('checkpoint_id'), str)):
                    raise ValueError('malformed original binding')
                fixed = tail.recovery_range
                if (not isinstance(tail.recovery_parent, dict)
                        or (tail.recovery_parent and (set(tail.recovery_parent) != {'range_id', 'end', 'digest'}
                            or not isinstance(tail.recovery_parent['range_id'], str)
                            or chat_recovery.HEX32.fullmatch(tail.recovery_parent['range_id']) is None
                            or not chat_recovery.offset(tail.recovery_parent['end'])
                            or tail.recovery_parent['end'] > tail.offset
                            or not isinstance(tail.recovery_parent['digest'], str)
                            or chat_recovery.HEX64.fullmatch(tail.recovery_parent['digest']) is None))
                        or (fixed is None and tail.recovery_range_id)
                        or (fixed is not None and (not isinstance(tail.recovery_range_id, str)
                            or chat_recovery.HEX32.fullmatch(tail.recovery_range_id) is None))):
                    raise ValueError('malformed recovery range identity')
                if fixed is not None and (not isinstance(fixed, dict) or set(fixed) != {'start', 'end', 'digest'}
                        or not chat_recovery.offset(fixed.get('start')) or not chat_recovery.offset(fixed.get('end'))
                        or not fixed['start'] <= tail.offset <= fixed['end']
                        or not isinstance(fixed.get('digest'), str)
                        or chat_recovery.HEX64.fullmatch(fixed['digest']) is None):
                    raise ValueError('malformed fixed range')
                key = (tail.binding or {}).get('checkpoint_id') or tail.session_name
                self._recovering[key] = tail
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Preserve the unreadable preimage; never cold-seek past its data.
            self._chat_gap = 'checkpoint_corrupt'
            log.error('chat recovery gap: %s (%s)', self._chat_gap, type(exc).__name__)

    def _save_chat_checkpoint(self):
        if getattr(self, '_chat_gap', ''):
            return
        from dataclasses import asdict
        sources = []
        for tail in [*self._recovering.values(), *self._tails.values()]:
            if not tail.path:
                continue
            item = asdict(tail)
            item['provenance_state'] = {}
            sources.append(item)
        data = {'version': 1, 'host': self.config.host, 'sources': sources}
        path = self._chat_checkpoint_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        with temporary.open('w') as f:
            os.chmod(temporary, 0o600)
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _capture_chat_source(self, tail):
        """Keep an integrity anchor for the retained bytes before sending."""
        if tail.binding and self._source_file_identity_digest(tail.path) != tail.binding['file_digest']:
            tail.recovery_gap = 'source_file_replaced'
            return
        size = os.path.getsize(tail.path)
        if tail.observed_size and (size < tail.observed_size or
                self._prefix_digest(tail.path, tail.observed_size) != tail.observed_digest):
            tail.recovery_gap = 'source_truncated_or_changed'
            return
        if size != tail.observed_size or not tail.observed_digest:
            tail.observed_size = size
            tail.observed_digest = self._prefix_digest(tail.path, size)

    def _collect_chat_recovery(self, budget, events, high_water):
        for tail in self._recovering.values():
            if tail.recovery_gap:
                continue
            if not tail.binding:
                tail.recovery_gap = 'original_identity_unproven'
                continue
            try:
                self._capture_chat_source(tail)
                if tail.recovery_gap:
                    continue
                if tail.recovery_range is None:
                    if tail.recovery_parent:
                        # Live ACKs may have moved farther ahead; replay that
                        # bridge through the shared dedupe sink for exact range continuity.
                        tail.offset = tail.recovery_parent['end']
                        if self._prefix_digest(tail.path, tail.offset) != tail.recovery_parent['digest']:
                            tail.recovery_gap = 'recovery_prefix_changed'
                            continue
                    # Capture complete-record EOF once. Incomplete trailing bytes
                    # stay explicit/non-green and are never silently acknowledged.
                    size = os.path.getsize(tail.path)
                    with open(tail.path, 'rb') as f:
                        f.seek(max(0, size - self.config.max_read_bytes))
                        suffix = f.read()
                    end = size - len(suffix) + suffix.rfind(b'\n') + 1
                    if end < tail.offset:
                        tail.recovery_gap = 'incomplete_source_record'
                        continue
                    tail.partial_tail_bytes = size - end
                    if end == tail.offset and tail.recovery_parent:
                        continue  # retained identity at EOF; no empty range loop
                    tail.recovery_range_id = os.urandom(16).hex()
                    tail.recovery_range = {'start': tail.offset, 'end': end,
                        'digest': self._prefix_digest(tail.path, end)}
                    # Fixed range and original binding reach disk before wire.
                    self._save_chat_checkpoint()
                fixed = tail.recovery_range
                if self._prefix_digest(tail.path, fixed['end']) != fixed['digest']:
                    tail.recovery_gap = 'recovery_range_changed'
                    continue
                if tail.offset >= fixed['end']:
                    # Empty ranges and lost final ACKs still need their durable
                    # handoff response. Never discard proof before that receipt.
                    self._pass_recovery.append({**tail.binding, 'range_id': tail.recovery_range_id,
                        'predecessor': tail.recovery_parent, 'range': fixed,
                        'from_offset': tail.offset, 'next_offset': tail.offset})
                    continue
                before = len(events)
                pending, _ = self._collect_stream(tail, budget, events)
                budget -= len(events) - before
                self._pass_recovery.append({**tail.binding, 'range_id': tail.recovery_range_id,
                        'predecessor': tail.recovery_parent, 'range': fixed,
                    'from_offset': tail.offset, 'next_offset': pending})
                if pending != tail.offset:
                    high_water[tail.path] = pending
            except (OSError, ValueError) as exc:
                tail.recovery_gap = 'source_unavailable'
                log.error('chat recovery gap stream=%s: %s', tail.session_name, exc)
            if budget <= 0:
                break
        return budget

    def _chat_source(self, tail, next_offset):
        fence = self._usage_fences.get(f'{self.config.host}:{tail.session_name}') or {}
        if (not tail.provider_session_id or tail.recovery_gap
                or str(fence.get('source_pane_pid') or '') != tail.source_pane_pid
                or fence.get('provider') != tail.provider
                or not fence.get('session_created_at') or not fence.get('session_generation')):
            return None
        return {'stream_id': f'{self.config.host}:{tail.session_name}', 'provider': tail.provider,
                'native_session_id': tail.provider_session_id,
                'file_digest': self._source_file_identity_digest(tail.path),
                'pane_pid': tail.source_pane_pid, 'generation': fence['session_generation'],
                'session_created_at': fence['session_created_at'],
                'start_offset': tail.offset, 'next_offset': next_offset}

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
        if not hasattr(self, "_pending_usage"):
            self._pending_usage = {}
        if not hasattr(self, "_usage_fences"):
            self._usage_fences = {}
        self._pass_usage = {}
        self._pass_hold = {}
        self._usage_path_streams = {}
        self._pass_provenance = {}
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
        for key, tail in list(self._recovering.items()):
            if tail.recovery_range is not None:
                continue
            pane = discovered.get(tail.session_name)
            fence = self._usage_fences.get(f'{self.config.host}:{tail.session_name}') or {}
            if (pane is not None and pane.transcript_path == tail.path
                    and tail.recovery_parent
                    and not tail.partial_tail_bytes
                    and fence.get('session_generation') == tail.binding.get('generation')
                    and str(pane.pane_pid or '') == tail.source_pane_pid):
                self._tails[tail.session_name] = tail
                self._recovering.pop(key)
        # Retain only already-discovered native sources. A closed name never
        # creates a new binding; the daemon-issued checkpoint is the authority.
        for name, tail in list(self._tails.items()):
            if name not in discovered:
                if tail.binding:
                    self._recovering[tail.binding['checkpoint_id']] = tail
                self._tails.pop(name, None)

        budget = cfg.max_events_per_pass
        events: list[dict] = []
        high_water: dict[str, int] = {}
        capped = False
        self._pass_recovery = []
        self._pass_sources = []
        self._pass_chat_gaps = []
        budget = self._collect_chat_recovery(budget, events, high_water)
        recovering_names = {
            tail.session_name for tail in self._recovering.values()
            if (discovered.get(tail.session_name) is None or
                (discovered[tail.session_name].transcript_path == tail.path and
                 (self._usage_fences.get(f'{self.config.host}:{tail.session_name}') or {}).get(
                     'session_generation', tail.binding.get('generation')) == tail.binding.get('generation')))
        }
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
            if name in recovering_names:
                continue
            path = pane.transcript_path
            if not path:
                continue
            account = (getattr(self, "_usage_fences", {}).get(f"{self.config.host}:{name}") or {}).get("account_binding")
            if account:
                from provider_accounts import transcript_allowed
                if not transcript_allowed(path, pane.provider, account): continue
            st = self._tails.setdefault(name, _StreamTail(session_name=name))
            fence = self._usage_fences.get(f'{self.config.host}:{name}') or {}
            if st.binding and (st.path != path or st.source_pane_pid != str(pane.pane_pid or '')
                    or (fence.get('session_generation') and
                        fence['session_generation'] != st.binding['generation'])):
                self._recovering[st.binding['checkpoint_id']] = st
                if st.path == path:
                    st = self._tails[name] = _StreamTail(session_name=name, path=path,
                        offset=st.offset, provider_session_id=st.provider_session_id,
                        provider=st.provider, source_pane_pid=str(pane.pane_pid or ''), primed=True)
                else:
                    st = self._tails[name] = _StreamTail(session_name=name)
            if self._chat_gap:
                st.primed = True  # preserve corrupt preimage; no unproved cold-tail skip
            provider = "codex" if pane.provider == "codex" or _is_codex_rollout_path(path) else pane.provider
            if st.path != path:
                # A Claude fork handoff replays its new transcript from head;
                # initial binds retain the horizon and the store dedupes replays.
                fork_rebind = bool(st.path) and st.provider == provider == "claude"
                st.path, st.offset, st.provider_session_id, st.provider, st.source_pane_pid, st.primed = (
                    path, 0, "", "", "", fork_rebind
                )
                st.provenance_state = {}
                st.binding = {}
                st.observed_size, st.observed_digest = 0, ""
                st.recovery_gap, st.recovery_range = "", None
                st.recovery_range_id, st.recovery_parent = "", {}
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
                self._capture_chat_source(st)
                if st.recovery_gap:
                    continue
                pending, stream_capped = self._collect_stream(st, budget, events)
            except Exception as exc:  # noqa: BLE001 - one sick stream never kills the pass
                del events[n_before:]  # drop any partial contribution
                log.warning("stream %s collect failed: %s; skipping this pass", name, exc)
                continue
            source = self._chat_source(st, pending)
            if source:
                self._pass_sources.append(source)
            elif self.config.host_secret and st.path:
                self._pass_chat_gaps.append({'stream_id': f'{self.config.host}:{name}',
                                             'reason': 'live_checkpoint_unproven'})
            budget -= (len(events) - n_before)
            if stream_capped:
                capped = True          # this stream hit the per-pass cap
            if pending != st.offset:
                # Advance to the prefix pushed this pass (a capped stream advances
                # PAST what it pushed rather than re-collecting it forever).
                high_water[path] = pending
                usage = getattr(self, "_pass_usage", {}).get(f"{self.config.host}:{name}")
                if usage is not None:
                    self._usage_path_streams.setdefault(path, set()).add(f"{self.config.host}:{name}")
                    self._pending_usage.setdefault(f"{self.config.host}:{name}", usage)
                    self._pending_usage[f"{self.config.host}:{name}"] = usage
                # Durable (fsynced) before this pass's events push, so the
                # offset may advance exactly as before without losing usage.
                self._hold_pass(f"{self.config.host}:{name}")
                self._queue_provenance(self._pass_provenance.get(f"{self.config.host}:{name}") or ())
        self._save_chat_checkpoint()
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
        if st.recovery_range is not None:
            size = min(size, st.recovery_range['end'])
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
                    # Never discard retained proof or acknowledge a foreign native source.
                    if st.binding or st.recovery_range is not None:
                        st.recovery_gap = 'native_source_changed'
                        return st.offset, False
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
            for payload in group:
                payload['transcript_offset'] = end_offset
                if st.recovery_range is not None:
                    payload['recovery_id'] = st.binding['checkpoint_id']
            out.extend(group)
            consumed_to = end_offset
        if not capped:
            # Whole span consumed, including any trailing non-record bytes.
            consumed_to = new_offset
        if st.recovery_range is not None:
            return consumed_to, capped
        candidate = self._usage_candidate(
            st, provider, records, record_end_offsets, consumed_to,
        )
        if candidate is not None:
            if not hasattr(self, "_pass_usage"):
                self._pass_usage = {}
            self._pass_usage[f"{self.config.host}:{st.session_name}"] = candidate
        provenance = self._provenance_items(st, provider, records, record_end_offsets, consumed_to)
        if provenance:
            if not hasattr(self, "_pass_provenance"):
                self._pass_provenance = {}
            self._pass_provenance[f"{self.config.host}:{st.session_name}"] = provenance
        return consumed_to, capped

    def _provenance_items(
        self, st: _StreamTail, provider: str, records: list[dict],
        record_end_offsets: list[int], consumed_to: int,
    ) -> list[dict]:
        """Metadata-only provenance for the consumed prefix of this span."""
        included = [
            record for record, end_offset in zip(records, record_end_offsets)
            if end_offset <= consumed_to
        ]
        if not included:
            return []
        try:
            bound = (st.path, st.provider_session_id)
            if st.provenance_state.get("_bound") != bound:
                st.provenance_state.clear()
                st.provenance_state["_bound"] = bound
            if provider == "codex" and not st.provenance_state.get("meta_seen"):
                # The history horizon may begin after session_meta; seed the
                # session identity from the immutable head once per bind.
                native_provenance(
                    "codex", codex_header_records(st.path),
                    native_session_id=st.provider_session_id or None,
                    state=st.provenance_state,
                )
            return native_provenance(
                provider, included,
                native_session_id=(st.provider_session_id or None) if provider == "codex" else None,
                source_file_identity_digest=self._source_file_identity_digest(st.path),
                state=st.provenance_state, proof=True,
            )
        except Exception as exc:  # noqa: BLE001 - provenance never blocks chat/usage
            log.warning("stream %s provenance failed: %s", st.session_name, exc)
            return []

    def _attach_usage_v2(self, frame: dict) -> None:
        """Choose v2 or legacy admission for this frame's fenced items.

        The frame carries `clock` only when every fenced item in it can be
        classified: a sample <= 540 s old, or (v2 daemon) any sample, in which
        case items without a fresh sample are held instead of sent. Without
        a v2 daemon and a fresh sample the items keep legacy admission.
        """
        enabled = getattr(self, "_unfenced_enabled", False)
        fresh = self._clock_within(FENCED_CLOCK_MAX_AGE_S)
        pending = getattr(self, "_pending_usage", {})
        if enabled and pending and fresh is None:
            for stream_id in list(pending):
                item = pending[stream_id]
                try:
                    self._held_spans().move_fenced(
                        item, list(range(len(item["records"]))), self._clock_within(HELD_CLOCK_MAX_AGE_S),
                    )
                except OSError as exc:
                    log.error("held-span write failed stream=%s: %s; sending legacy", stream_id, exc)
                    continue
                pending.pop(stream_id)
        sample = (getattr(self, "_clock", None) if enabled else None) or fresh
        if sample is not None and (enabled or fresh is not None):
            frame["clock"] = sample
            frame["satellite_now"] = iso_ms(time.time())
        self._inflight_clock = frame.get("clock")

    def _attach_unfenced(self, frame: dict) -> None:
        """Held entries and loss records, only while the daemon is v2."""
        self._frame_had_unfenced = False
        if not getattr(self, "_unfenced_enabled", False):
            return
        held = self._held_spans()
        budget = int(WS_MAX_SIZE * 0.5) - len(json.dumps(frame))
        entries = held.frame_entries(budget) if frame.get("clock") is not None else []
        losses = held.frame_losses()
        if entries:
            frame["usage_unfenced"] = entries
        if losses:
            frame["usage_unfenced_losses"] = losses
        self._frame_had_unfenced = bool(entries or losses)

    async def _push_pass(self, ws, events: list[dict], high_water: dict[str, int],
                         inventory: list[str] | None = None) -> dict | None:
        """Push one pass; if a frame carrying the v2 usage fields is rejected
        (e.g. by a rolled-back v1 daemon), retry the same pass once without
        them. Offsets and held data are untouched by the rejected attempt."""
        ack = await self._push(ws, events, high_water, inventory=inventory)
        if (
            isinstance(ack, dict) and ack.get("type") == "event.push.error"
            and getattr(self, "_frame_had_unfenced", False)
        ):
            log.warning("push with held usage rejected (%s); retrying this pass without it", ack.get("error"))
            self._apply_unfenced_ack(ack)  # clears the enable flag
            ack = await self._push(ws, events, high_water, inventory=inventory)
        return ack

    def _apply_unfenced_ack(self, ack: dict) -> None:
        """Enable flag, held-entry/loss removal, and deferred fenced records."""
        self._rehoused_streams = set()
        if ack.get("type") != "event.push.ok":
            self._unfenced_enabled = False
            self._inflight_usage = {}
            return
        block = ack.get("usage_unfenced")
        version = ack.get("wire_version")
        self._unfenced_enabled = isinstance(block, dict) and type(version) is int and version >= 2
        try:
            if self._unfenced_enabled:
                self._held_spans().apply_ack(block, ack.get("losses_recorded"), ack.get("losses_conflict"))
                # An item refused only because its fence went stale (the row
                # closed or reopened) is held for history resolution instead
                # of being re-sent under that fence forever.
                pending = getattr(self, "_pending_usage", {})
                for item in ack.get("usage_rejected") or ():
                    if not isinstance(item, dict) or "index" in item or item.get("reason") not in STALE_FENCE_REASONS:
                        continue
                    stream_id = str(item.get("stream_id") or "")
                    sent = getattr(self, "_inflight_usage", {}).get(stream_id)
                    if sent is None:
                        continue
                    self._held_spans().move_fenced(
                        sent, list(range(len(sent["records"]))),
                        getattr(self, "_inflight_clock", None) or self._clock_within(HELD_CLOCK_MAX_AGE_S),
                    )
                    if pending.get(stream_id) is sent:
                        pending.pop(stream_id)
                    self._rehoused_streams.add(stream_id)
            deferred: dict[str, list[int]] = {}
            for item in ack.get("usage_rejected") or ():
                if isinstance(item, dict) and item.get("transient") and type(item.get("index")) is int:
                    deferred.setdefault(str(item.get("stream_id") or ""), []).append(item["index"])
            for stream_id, indices in deferred.items():
                sent = getattr(self, "_inflight_usage", {}).get(stream_id)
                if sent is not None:
                    self._held_spans().move_fenced(sent, indices, getattr(self, "_inflight_clock", None))
        except OSError as exc:
            log.error("held-span ack write failed: %s", exc)
        self._inflight_usage = {}

    def _attach_provenance(self, frame: dict) -> None:
        """Attach pending provenance within a byte budget so it never pushes a
        frame past the WebSocket limit (events always take precedence)."""
        self._inflight_provenance = {}
        self._provenance_probe_inflight = False
        pending = getattr(self, "_pending_provenance", {})
        if not pending or getattr(self, "_provenance_supported", True) is False:
            return
        version = getattr(self, "_provenance_version", None)
        if version == 1 and time.monotonic() >= self._provenance_reprobe_at:
            version = self._provenance_version = None
        if version is None:
            # Capability probe before data: the next push carries the batch.
            frame["usage_provenance"] = probe_payload()
            self._provenance_probe_inflight = True
            return
        budget = int(WS_MAX_SIZE * 0.75) - len(json.dumps(frame)) - 128  # block envelope
        items = []
        for key, item in list(pending.items()):
            shaped = provenance_for_version([item], version)
            if not shaped:  # a version-1 daemon cannot store flags; the backfill re-sends them
                pending.pop(key, None)
                continue
            size = len(json.dumps(shaped[0])) + 2
            if len(items) >= PROVENANCE_MAX_ITEMS or size > budget:
                break
            budget -= size
            items.append(shaped[0])
            self._inflight_provenance[key] = item
        if items:
            frame["usage_provenance"] = {"version": version, "items": items}

    @staticmethod
    def _provenance_key(item: dict) -> tuple:
        data = item.get("data") or {}
        if item.get("kind") == "rate_limit":
            return ("rate_limit", data.get("account_id"), data.get("window_kind"),
                    data.get("window_minutes"), str(data.get("resets_at")), data.get("pct"))
        if item.get("kind") == PROVENANCE_FLAG_KIND:
            return (PROVENANCE_FLAG_KIND, item.get("native_session_id"), data.get("response_id"), data.get("flag"))
        return (item.get("kind"), item.get("native_session_id"),
                data.get("record_key") or data.get("response_id"))

    def _queue_provenance(self, items) -> None:
        pending = self._pending_provenance
        for item in items:
            pending.pop(self._provenance_key(item), None)
            pending[self._provenance_key(item)] = item
        overflow = len(pending) - 4 * PROVENANCE_MAX_ITEMS
        if overflow > 0:
            # An unacknowledging daemon must not grow memory without bound;
            # dropped items are recovered by `--backfill`.
            for key in list(pending)[:overflow]:
                pending.pop(key, None)
            log.warning("provenance queue overflow: dropped %d oldest items", overflow)

    # -- push / ack -----------------------------------------------------------

    def _source_host_proof(self, satellite_pid: int) -> str | None:
        secret = str(getattr(self.config, "host_secret", "") or "")
        if not secret or not self.config.host or not self.sha or not satellite_pid:
            return None
        message = f"event.push.v1\0{self.config.host}\0{self.sha}\0{satellite_pid}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    async def _send_host_stats(self, ws) -> dict | None:
        self._req += 1
        rid = self._req
        try:
            stats = await asyncio.to_thread(sample_machine_stats, self.config.host)
        except Exception as exc:  # noqa: BLE001 - retry on the next sample interval
            log.warning("machine stats sample failed: %s", exc)
            return None
        frame = {
            "type": "host.stats",
            "request_id": rid,
            "push_secret": self.config.push_secret,
            "satellite_sha": self.sha,
            "satellite_pid": os.getpid(),
            "wire_version": STATS_WIRE_VERSION,
            "host": self.config.host,
            "stats": stats,
        }
        t_send_wall, t_send_mono = time.time(), time.monotonic()
        await ws.send(json.dumps(frame))
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
                if msg.get("type") == "host.stats.ok":
                    self._note_clock(msg, t_send_wall, t_send_mono)
                fences = msg.get("usage_fences")
                if isinstance(fences, list):
                    self._usage_fences = {
                        str(item.get("stream_id")): dict(item)
                        for item in fences
                        if isinstance(item, dict) and item.get("stream_id")
                    }
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
            "satellite_incarnation": getattr(self, "_incarnation", ""),
        }
        if self.config.host_secret:
            frame['chat_sources'] = getattr(self, '_pass_sources', [])
            frame['chat_recovery'] = getattr(self, '_pass_recovery', [])
            frame['chat_recovery_pending'] = sum(
                (tail.offset < tail.observed_size if tail.recovery_range is None
                 else tail.offset < tail.recovery_range['end'])
                for tail in self._recovering.values()
            )
            frame['chat_recovery_gaps'] = [
                {'stream_id': f'{self.config.host}:{tail.session_name}',
                 'reason': tail.recovery_gap or 'incomplete_source_record'}
                for tail in [*self._tails.values(), *self._recovering.values()]
                if tail.recovery_gap or tail.partial_tail_bytes
            ]
            frame['chat_recovery_gaps'].extend(getattr(self, '_pass_chat_gaps', []))
            if self._chat_gap:
                frame['chat_recovery_gaps'].append({'reason': self._chat_gap})
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
        self._attach_usage_v2(frame)
        pending_usage = getattr(self, "_pending_usage", {})
        if pending_usage:
            frame["usage"] = [pending_usage[sid] for sid in sorted(pending_usage)]
        self._inflight_usage = dict(pending_usage)
        proof = self._source_host_proof(os.getpid())
        if proof is not None:
            frame["source_host_proof"] = proof
        self._attach_unfenced(frame)
        self._attach_provenance(frame)
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
        self._apply_unfenced_ack(ack)
        if kind == "event.push.error":
            log.warning("push rejected: %s", ack.get("error"))
            version = ack.get("version") if isinstance(ack.get("version"), dict) else {}
            if version.get("status") == "update_required":
                return str(version.get("target_sha") or "") or None
            return None
        if kind != "event.push.ok":
            return None
        usage_was_acknowledged = "usage_recorded" in ack or "usage_replayed" in ack or "usage_rejected" in ack
        provenance_ack = ack.get("usage_provenance")
        if getattr(self, "_provenance_probe_inflight", False):
            self._provenance_probe_inflight = False
            if not isinstance(provenance_ack, dict):
                self._provenance_supported = False  # predates provenance entirely
            else:
                granted = negotiated_version(provenance_ack)
                if granted is not None:
                    self._provenance_version = granted
                    self._provenance_reprobe_at = time.monotonic() + PROVENANCE_REPROBE_S
        elif isinstance(provenance_ack, dict) and provenance_ack.get("error") == "unsupported_version":
            # A rolled-back daemon: resend the kept batch as version 1, re-probe later.
            self._provenance_version = 1
            self._provenance_reprobe_at = time.monotonic() + PROVENANCE_REPROBE_S
        if getattr(self, "_inflight_provenance", None):
            # A daemon that ignores the block (older release) stops further
            # attachment until reconnect; backfill recovers what it missed.
            self._provenance_supported = isinstance(provenance_ack, dict)
        if isinstance(provenance_ack, dict) and not provenance_ack.get("error"):
            # Per-item rejections are final (backfill is the authority); only a
            # whole-block error keeps the batch for the next push.
            pending_provenance = getattr(self, "_pending_provenance", {})
            for key, sent in getattr(self, "_inflight_provenance", {}).items():
                if pending_provenance.get(key) is sent:  # never drop a newer replacement
                    pending_provenance.pop(key, None)
        self._inflight_provenance = {}
        # Record-level v2 refusals (`index`) are final or already held; only
        # an item-level rejection keeps the stream's offset and pending item.
        usage_rejected_streams = {
            str(item.get("stream_id") or "")
            for item in (ack.get("usage_rejected") or ())
            if isinstance(item, dict) and item.get("stream_id") and "index" not in item
        } - getattr(self, "_rehoused_streams", set())
        if usage_was_acknowledged:
            pending_usage = getattr(self, "_pending_usage", {})
            for stream_id in list(pending_usage):
                if stream_id not in usage_rejected_streams:
                    pending_usage.pop(stream_id, None)
        dropped_streams: set[str] = set()
        refused_recovery_tokens: set[str] = set()
        refused_recovery_streams: set[str] = set()
        for item in ack.get("dropped") or ():
            if isinstance(item, dict):
                if item.get('checkpoint_id') is not None:
                    refused_recovery_tokens.add(str(item['checkpoint_id']))
                    refused_recovery_streams.add(str(item.get('stream_id') or ''))
                    continue  # historical refusal never freezes a later live generation
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
            stream_id = f"{self.config.host}:{name}"
            if (
                stream_id not in dropped_streams
                and stream_id not in usage_rejected_streams
                and st.path in high_water
            ):
                st.offset = high_water[st.path]
        for binding in ack.get('chat_bindings') or ():
            if not isinstance(binding, dict):
                continue
            if binding.get('status') != 'bound':
                log.error('chat checkpoint refused stream=%s reason=%s',
                          binding.get('stream_id'), binding.get('status'))
                continue
            name = binding['stream_id'].removeprefix(f'{self.config.host}:')
            tail = self._tails.get(name)
            if tail is not None and binding.get('native_session_id') == tail.provider_session_id:
                tail.binding = {key: value for key, value in binding.items() if key != 'status'}
        for result in ack.get('chat_recovery') or ():
            if (result.get('checkpoint_id') in refused_recovery_tokens
                    or result.get('stream_id') in refused_recovery_streams):
                continue
            tail = self._recovering.get(result.get('checkpoint_id'))
            if (tail is not None and tail.recovery_range is not None
                    and result.get('range_id') == tail.recovery_range_id
                    and result.get('status') in {'pending', 'drained'}):
                through = result.get('through')
                if type(through) is int and tail.offset <= through <= tail.recovery_range['end']:
                    tail.offset = through
                    if result.get('status') == 'drained' and through == tail.recovery_range['end']:
                        tail.recovery_parent = {'range_id': tail.recovery_range_id,
                            'end': through, 'digest': tail.recovery_range['digest']}
                        tail.recovery_range, tail.recovery_range_id = None, ''
                        # Keep the original live-issued identity even when the
                        # source is closed. Later bytes get a new immutable range.
        self._save_chat_checkpoint()
        version = ack.get("version") if isinstance(ack.get("version"), dict) else {}
        if version.get("status") == "update_required":
            return str(version.get("target_sha") or "") or None
        return None

    # -- auto-update ----------------------------------------------------------

    def _update_environment(self):
        root = Path(self.config.checkout).resolve()
        if root != Path(_repo_root()).resolve() or root.stat().st_uid != os.getuid():
            raise RuntimeError('executed source and owned checkout must agree')
        if not (root / '.git').is_dir():
            raise RuntimeError('a dedicated clone is required')
        if (root / '.venv').is_symlink() or Path(sys.prefix).resolve() != (root / '.venv').resolve():
            raise RuntimeError('the dedicated checkout service venv is required')
        checks = [
            (('rev-parse', '--show-toplevel'), str(root)),
            (('remote', 'get-url', 'origin'), self.config.update_origin),
            (('status', '--porcelain'), ''),
            (('rev-parse', 'HEAD'), self.sha),
        ]
        for args, expected in checks:
            result = _git(str(root), *args, timeout=10)
            if result.returncode or result.stdout.strip() != expected:
                raise RuntimeError('checkout preflight failed: ' + args[0])
        if _git(str(root), 'symbolic-ref', '-q', 'HEAD', timeout=10).returncode != 1:
            raise RuntimeError('the satellite checkout must be detached')
        return root

    def _preflight_candidate(self, root):
        # Imports run in a disposable process with the actual service interpreter.
        # No Satellite instance, connection, discovery or dependency installation.
        program = (
            'import sys,runpy,importlib.metadata,re; from pathlib import Path; '
            'root=Path(sys.argv[1]); source=root/"services/chat-stream-v2"; '
            'sys.path.insert(0,str(source)); '
            'req=(source/"requirements.txt").read_text(); '
            'pin=re.search(r"(?m)^websockets==([^\\s]+)$",req); '
            'assert pin and importlib.metadata.version("websockets")==pin[1], "websockets dependency pin"; '
            'runpy.run_path(str(source/"satellite.py"),run_name="satellite_preflight")'
        )
        result = subprocess.run([sys.executable, '-I', '-B', '-c', program, str(root)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('candidate dependency/import preflight failed')

    def _maybe_update(self, target_sha: str) -> None:
        """Bounded exact-pin update of the dedicated clone; restore on pre-exec failure."""
        if not isinstance(target_sha, str) or re.fullmatch(r'[0-9a-f]{40}', target_sha) is None:
            return
        if target_sha == self.sha or self.config.no_autoupdate:
            return
        now = time.monotonic()
        floor = max(0.0, self.config.update_min_interval_s)
        backoff = min(max(floor, self.config.backoff_max_s),
                      floor * 2 ** min(self._update_failures, 10))
        if now - self._last_update_attempt < backoff:
            return
        self._last_update_attempt = now
        previous = self.sha
        checkout_started = False
        try:
            root = self._update_environment()
            if self._chat_gap or any(t.recovery_gap for t in [*self._tails.values(), *self._recovering.values()]):
                raise RuntimeError('unresolved transcript recovery gap')
            self._save_chat_checkpoint()
            preimage = self._chat_checkpoint_path.with_name(f'update-{previous}-{target_sha}.json')
            if not preimage.exists():
                # Immutable per-transition receipt; retry cannot replace rollback evidence.
                with preimage.open('x') as f:
                    json.dump({'previous': previous, 'target': target_sha,
                               'checkout': str(root), 'interpreter': sys.executable,
                               'python': sys.version, 'created_at': time.time()}, f, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
            fetch = _git(str(root), 'fetch', '--no-tags', 'origin', target_sha, timeout=120)
            if fetch.returncode:
                raise RuntimeError('target fetch failed')
            checkout_started = True
            co = _git(str(root), 'checkout', '--detach', target_sha, timeout=60)
            if co.returncode or self._read_sha() != target_sha:
                raise RuntimeError('exact target checkout failed')
            self._preflight_candidate(root)
            self._update_outcome = 'exec_pending'
            self._exec_restart()
        except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
            self._update_failures += 1
            self._update_outcome = 'preflight_failed'
            if checkout_started:
                try:
                    restored = _git(self.config.checkout, 'checkout', '--detach', previous, timeout=60)
                    restored_ok = restored.returncode == 0 and self._read_sha() == previous
                except (OSError, subprocess.SubprocessError):
                    restored_ok = False
                if not restored_ok:
                    self._update_outcome = 'source_restore_failed'
                    log.error('satellite source restoration failed; supervisor/SSH recovery required')
                else:
                    self._update_outcome = 'source_restored'
            log.error('satellite update refused outcome=%s reason=%s', self._update_outcome, exc)

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
        self._provenance_supported = None  # re-probe the (possibly redeployed) daemon
        self._unfenced_enabled = False  # re-learned from this connection's first ack
        try:
            self._held_spans()
        except OSError as exc:
            log.error("held-span file unavailable: %s", exc)
        self._held_tick_mono = time.monotonic()
        stats_acknowledged = False
        if not cfg.disabled:
            ack = await self._send_host_stats(ws)
            stats_acknowledged = isinstance(ack, dict) and ack.get("type") == "host.stats.ok"
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
                stats_acknowledged = isinstance(ack, dict) and ack.get("type") == "host.stats.ok"
                version = ack.get("version") if isinstance(ack, dict) else None
                target = version.get("target_sha") if isinstance(version, dict) else None
                if target:
                    await asyncio.to_thread(self._maybe_update, str(target))
                self._last_stats_at = time.monotonic()
            self._tick_held()
            events, high_water, _capped = await asyncio.to_thread(self._collect)
            inventory = self._last_discovery_inventory
            ack = await self._push_pass(ws, events, high_water, inventory=inventory)
            if ack is None:
                continue           # no ack: offsets unmoved, re-read next pass
            target = self._apply_ack(ack, high_water)
            unfenced = ack.get("usage_unfenced")
            provenance = ack.get("usage_provenance")
            # Delivery state is applied above, unchanged. A partial service ACK
            # must not erase the outage, nor may the initial capability probe
            # stand in for delivery of already-held records or losses.
            if (
                stats_acknowledged and ack.get("type") == "event.push.ok"
                and not ack.get("dropped") and not ack.get("usage_rejected")
                and not ack.get("losses_conflict")
                and not (isinstance(unfenced, dict) and unfenced.get("rejected"))
                and not (isinstance(provenance, dict)
                         and (provenance.get("error") or provenance.get("rejected")))
                and not self._held_spans().pending_count()
                and not self._held_spans().frame_losses()
            ):
                self._session_acknowledged = True
            if target:
                # Blocking git work (and a possible exec-restart) off the loop.
                await asyncio.to_thread(self._maybe_update, target)

    def _tick_held(self) -> None:
        now = time.monotonic()
        last = getattr(self, "_held_tick_mono", None)
        self._held_tick_mono = now
        held = getattr(self, "_held", None)
        if held is None or last is None:
            return
        try:
            held.tick(enabled=self._unfenced_enabled, elapsed_s=now - last)
        except OSError as exc:
            log.error("held-span tick failed: %s", exc)

    async def run_forever(self) -> None:
        cfg = self.config
        failures = 0
        base = min(cfg.backoff_base_s, cfg.backoff_max_s)
        backoff = base
        while True:
            self._session_acknowledged = False
            try:
                async with websockets.connect(
                    cfg.coordinator_ws, max_size=WS_MAX_SIZE,
                    ping_interval=20, ping_timeout=20, close_timeout=10,
                ) as ws:
                    log.info("connected to %s as host=%s sha=%s",
                             cfg.coordinator_ws, cfg.host, (self.sha or "?")[:12])
                    await self._session(ws)
                reason = "session ended"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport fault
                reason = str(exc)
            if self._session_acknowledged:
                failures = 0
                backoff = base
            failures += 1
            back = random.uniform(backoff / 2, backoff)
            log.warning("connection lost (%d): %s; reconnect in %.2fs", failures, reason, back)
            await asyncio.sleep(back)
            # Saturate before the next attempt; no exponent grows with outage age.
            backoff = min(cfg.backoff_max_s, backoff * 2)


async def _backfill(sat: Satellite, args) -> dict:
    """One-shot provenance backfill over this host's transcripts (spec AC7)."""
    roots = {}
    if args.provider in ("all", "claude"):
        roots["claude"] = args.claude_root or sat.config.claude_projects_root
    if args.provider in ("all", "codex"):
        roots["codex"] = args.codex_root or DEFAULT_CODEX_ROOT
    cursor = BackfillCursor(None if args.no_cursor else args.cursor)
    async with websockets.connect(
        sat.config.coordinator_ws, max_size=WS_MAX_SIZE,
        ping_interval=20, ping_timeout=20, close_timeout=10,
    ) as ws:
        granted = {"version": 1}

        async def send(payload: dict) -> dict:
            sat._req += 1
            rid = sat._req
            frame = {
                "type": "event.push", "request_id": rid,
                "push_secret": sat.config.push_secret, "satellite_sha": sat.sha,
                "satellite_pid": os.getpid(), "wire_version": WIRE_VERSION,
                "host": sat.config.host, "events": [], "high_water": {},
                "usage_provenance": payload,
            }
            proof = sat._source_host_proof(os.getpid())
            if proof is not None:
                frame["source_host_proof"] = proof
            await ws.send(json.dumps(frame))
            deadline = asyncio.get_running_loop().time() + max(sat.config.ack_timeout_s, 120.0)
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {"error": "ack_timeout"}
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if not isinstance(msg, dict) or msg.get("request_id") != rid:
                    continue
                if msg.get("type") != "event.push.ok":
                    return {"error": str(msg.get("error") or msg.get("type") or "push_failed")}
                return msg.get("usage_provenance") or {"error": "provenance_unacknowledged"}

        async def probe() -> int | None:
            # The empty version-2 payload precedes data, so a cold producer's first
            # batch already carries ordinal 1; unsupported_version downgrades to 1.
            version = negotiated_version(await send(probe_payload()))
            if version is not None:
                granted["version"] = version
            return version

        async def push(items: list[dict], dry_run: bool) -> dict:
            return await send({"version": granted["version"], "items": items, "dry_run": dry_run})

        summary = await run_backfill(push, roots=roots, cursor=cursor,
                                     dry_run=args.dry_run, batch=args.batch, probe=probe)
    summary["host"] = sat.config.host
    summary["satellite_sha"] = sat.sha
    return summary


def _parse_args(argv: list[str] | None):
    import argparse

    parser = argparse.ArgumentParser(description="Pentacle per-host ingest satellite")
    parser.add_argument("--backfill", action="store_true",
                        help="push usage provenance for every local transcript once, then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --backfill: report native sessions found vs known, write nothing")
    parser.add_argument("--provider", choices=("all", "claude", "codex"), default="all")
    parser.add_argument("--cursor", default="~/.local/state/pentacle-satellite/provenance_backfill.json")
    parser.add_argument("--no-cursor", action="store_true", help="ignore and do not write the resume cursor")
    parser.add_argument("--claude-root", default="")
    parser.add_argument("--codex-root", default="")
    parser.add_argument("--batch", type=int, default=BACKFILL_BATCH)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging(os.environ.get("PENTACLE_SATELLITE_LOG_LEVEL", "INFO"))
    args = _parse_args(argv)
    cfg = SatelliteConfig.from_env()
    if not cfg.host:
        log.error("no host configured (set PENTACLE_SATELLITE_HOST)")
        return 2
    sat = Satellite(cfg)
    if args.backfill:
        if not 1 <= args.batch <= PROVENANCE_MAX_ITEMS:
            log.error("--batch must be 1..%d", PROVENANCE_MAX_ITEMS)
            return 2
        summary = asyncio.run(_backfill(sat, args))
        print(json.dumps(summary, sort_keys=True))
        failed = sum(int(p.get("batches_failed", 0)) for p in summary["providers"].values())
        return 1 if failed else 0
    try:
        asyncio.run(sat.run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
