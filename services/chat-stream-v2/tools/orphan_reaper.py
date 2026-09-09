#!/usr/bin/env python3
"""External periodic residue reaper for a configured host.

Nothing reaps the harness residue that accumulates on the production host:
coordinator gate/deploy shells on the default tmux server, empty harness tmux
server sockets, and scratch daemons left alive. Such residue can exhaust
pseudo-terminals. This tool reads truth it does not own (`tmux
list-sessions/list-panes` per socket, `ps`/`pgrep`/`lsof`), classifies the
obviously-dead residue, and acts ONLY through OS-level operations (tmux
kill-session/kill-server, SIGTERM). Zero daemon LOC and zero daemon verbs -- the
daemon keeps owning lifecycle policy (rule 2 of epic_daemon_v2_debloat).

Finished-but-open *hidden seats* are NOT in scope: closing them requires the
daemon's own authority (verified-self / direct-parent / authenticated-operator;
`server.py:~1229/1686`), which a bare CLI/launchd job cannot present. That
backlog is the daemon self-close defect and is owned by
`spec_example_2026_01`, not by this reaper.

Two phases, reproducing v1's trash-then-purge shape outside the daemon: a tmux
session / harness server socket / scratch daemon matched by a rule is RECORDED in
the reaper ledger with `trashed_at` on first sight, then killed in a later cycle
only after >= PURGE_WINDOW in the ledger AND only if still present and not live.
Any recorded target that came back to life (or vanished on its own) is dropped
from the ledger, never killed.

Dry-run is the default. `--act` is required to kill anything. Every action and
every skip -- with its reason -- is posted to a single Updates card per cycle
(`agent-orch notify`, deduped on the decision set so an unchanged cycle does not
re-post).

Invariants (never touched, not configurable): a `v2-*` tmux session
(daemon-owned); an attached tmux session; a harness socket holding a
`v2-*`/attached session or whose enumeration failed; a live pane; the default
tmux server; the real production daemon (real `--port`); a scratch daemon with
clients. Everything fails closed on unreadable evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

# --------------------------------------------------------------------------- #
# Ruled thresholds (operator, 2026-09-05; see spec § Operator rulings/Design).
# --------------------------------------------------------------------------- #
HOST = os.environ.get("PENTACLE_HOST_ID", "hosta")
SHELL_IDLE_SECONDS = 6 * 3600               # idle default-server shell session > 6 h
DAEMON_NO_CLIENT_SECONDS = 2 * 3600         # scratch daemon with no clients > 2 h
PURGE_WINDOW_SECONDS = 24 * 3600            # phase-2 recovery window before a kill

DEFAULT_LEDGER_PATH = Path.home() / ".local" / "share" / "pentacle-stream" / "orphan_reaper.json"

# Harness tmux server sockets that are reap-eligible as a whole server when empty
# or holding no live pane. The default server is NEVER matched here.
HARNESS_SOCKET_RE = re.compile(r"^(v2cap-|v2s|v2loop-|ptr-|authctx|.*-gate$)")

# Processes that count as "just a shell" -- a pane holding only these is dead.
_SHELL_COMMANDS = frozenset({"zsh", "-zsh", "bash", "-bash", "sh", "-sh", "login", "tmux"})

LEDGER_VERSION = 1
LEDGER_PRODUCER = "orphan_reaper"


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (with Z or offset) or epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError:
            return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _age(now: datetime, then: Optional[datetime]) -> Optional[float]:
    if then is None:
        return None
    return (now - then).total_seconds()


def _fmt_dur(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


# --------------------------------------------------------------------------- #
# Inventory model (pure data; the gather layer builds it, the classifier reads it)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TmuxSession:
    server: str                 # "default" for the default server
    socket_path: Optional[str]  # None for the default server
    name: str
    attached: bool
    created_at: Optional[datetime]
    has_live_child: bool

    def age_seconds(self, now: datetime) -> Optional[float]:
        return _age(now, self.created_at)


@dataclass(frozen=True)
class TmuxServer:
    socket: str                 # socket file name (e.g. "v2cap-abc")
    socket_path: str
    session_count: int
    any_live_pane: bool
    has_protected_session: bool = False   # any v2-* / attached session on the socket


@dataclass(frozen=True)
class ScratchDaemon:
    pid: int
    cmdline: str
    client_count: int
    started_at: Optional[datetime]

    def age_seconds(self, now: datetime) -> Optional[float]:
        return _age(now, self.started_at)


@dataclass(frozen=True)
class Inventory:
    tmux_sessions: tuple[TmuxSession, ...] = ()   # default-server sessions only
    tmux_servers: tuple[TmuxServer, ...] = ()     # harness server sockets
    scratch_daemons: tuple[ScratchDaemon, ...] = ()


# --------------------------------------------------------------------------- #
# Decision model
# --------------------------------------------------------------------------- #
ACTION_KILL_SESSION = "kill_session"
ACTION_KILL_SERVER = "kill_server"
ACTION_SIGTERM = "sigterm"


@dataclass(frozen=True)
class Candidate:
    kind: str            # "tmux_session" | "tmux_server" | "scratch_daemon"
    target_id: str       # stable ledger key
    label: str           # human-readable
    action: str
    rule: str
    # The act layer's handle, kept opaque to the classifier:
    exec_ref: str = ""   # session name / socket path / pid


@dataclass(frozen=True)
class Skip:
    target_desc: str
    reason: str
    category: str        # "invariant" | "active" | "threshold" | "protected"


@dataclass
class Plan:
    now: datetime
    purges: list[Candidate] = field(default_factory=list)     # phase-2 kills (window elapsed)
    recorded: list[Candidate] = field(default_factory=list)   # newly trashed (record only)
    pending: list[tuple[Candidate, float]] = field(default_factory=list)  # in-window; hrs left
    recovered: list[dict] = field(default_factory=list)       # ledger entries dropped
    skips: list[Skip] = field(default_factory=list)
    next_ledger: dict = field(default_factory=dict)
    refused: list[dict] = field(default_factory=list)         # filled by the act layer
    # Act-layer OUTCOMES (so counters/card report what actually happened, never
    # what was merely planned).
    acted: bool = False
    purged_ok: list[Candidate] = field(default_factory=list)  # targets actually killed
    live_skips: list[Skip] = field(default_factory=list)      # refused by pre-act recheck (revived)

    def counters(self) -> dict:
        # Dry-run reports the PLAN (would-purge); act reports OUTCOMES.
        purged = len(self.purged_ok) if self.acted else len(self.purges)
        return {
            "purged": purged,
            "recorded": len(self.recorded),
            "pending": len(self.pending),
            "recovered": len(self.recovered),
            "skipped_live": len(self.live_skips),
            "skipped_active": sum(1 for s in self.skips if s.category in ("active", "invariant", "protected")),
            "skipped_threshold": sum(1 for s in self.skips if s.category == "threshold"),
            "refused": len(self.refused),
        }

    def has_actions(self) -> bool:
        if self.acted:
            return bool(self.purged_ok or self.refused or self.live_skips)
        return bool(self.purges)

    def dedup_key(self, mode: str) -> str:
        parts = []
        for c in (*self.purges, *self.recorded):
            parts.append(f"A:{c.target_id}:{c.action}")
        for c, _ in self.pending:
            parts.append(f"P:{c.target_id}")
        for s in self.skips:
            parts.append(f"S:{s.target_desc}:{s.reason}")
        # Audit outcomes must change the key too, or a cycle that differs only in
        # what recovered/was refused would be deduped and its card suppressed.
        for r in self.recovered:
            parts.append(f"R:{r.get('target_id')}")
        for r in self.refused:
            parts.append(f"F:{r.get('label')}:{r.get('detail')}")
        for s in self.live_skips:
            parts.append(f"L:{s.target_desc}")
        digest = hashlib.sha1("\n".join(sorted(parts)).encode()).hexdigest()[:16]
        return f"orphan-reaper-{mode}-{digest}"


# --------------------------------------------------------------------------- #
# Stable target ids. A deferred (phase-2) target's id embeds a generation stamp
# (creation time) so a reused session name or a recycled pid gets a FRESH ledger
# entry and 24 h clock instead of inheriting a dead predecessor's `trashed_at`.
# --------------------------------------------------------------------------- #
def _gen(dt: Optional[datetime]) -> str:
    return str(int(dt.timestamp())) if dt else "na"


def _session_tid(s: "TmuxSession") -> str:
    return f"tmux_session:{s.server}:{s.name}:{_gen(s.created_at)}"


def _server_tid(srv: "TmuxServer") -> str:
    return f"tmux_server:{srv.socket}"


def _daemon_tid(d: "ScratchDaemon") -> str:
    return f"scratch_daemon:{d.pid}:{_gen(d.started_at)}"


# --------------------------------------------------------------------------- #
# Classifier + two-phase resolution (PURE: inventory + ledger + now -> Plan)
# --------------------------------------------------------------------------- #
def _classify_session(sess: TmuxSession, now: datetime) -> tuple[Optional[Candidate], Optional[Skip]]:
    desc = f"tmux session {sess.name} (default server)"
    tid = _session_tid(sess)
    if sess.name.startswith("v2-"):
        return None, Skip(desc, "v2-* session is daemon-owned (invariant)", "invariant")
    if sess.attached:
        return None, Skip(desc, "session is attached (operator/interactive)", "active")
    if sess.has_live_child:
        return None, Skip(desc, "pane has a running non-shell child (active)", "active")
    age = sess.age_seconds(now)
    if age is None:
        return None, Skip(desc, "unknown created_at; cannot clear 6h (fail closed)", "invariant")
    if age <= SHELL_IDLE_SECONDS:      # strict: must be OLDER than 6h
        return None, Skip(desc, f"not older than 6h ({_fmt_dur(age)})", "threshold")
    return Candidate("tmux_session", tid, desc, ACTION_KILL_SESSION,
                     f"idle shell session, age {_fmt_dur(age)} > 6h", sess.name), None


def _classify_server(srv: TmuxServer) -> tuple[Optional[Candidate], Optional[Skip]]:
    desc = f"tmux server socket {srv.socket} ({srv.session_count} sessions)"
    tid = _server_tid(srv)
    # A kill-server must never bypass the v2-*/attached protections that guard an
    # individual session: enumerate them at gather time and refuse the whole
    # server if any protected session lives on the socket (QA blocker #5).
    if srv.has_protected_session:
        return None, Skip(desc, "socket holds a v2-*/attached session (invariant)", "invariant")
    if srv.any_live_pane:
        return None, Skip(desc, "server has a live pane process (active)", "active")
    return Candidate("tmux_server", tid, desc, ACTION_KILL_SERVER,
                     ("empty harness server" if srv.session_count == 0
                      else "harness server with no live pane"), srv.socket_path), None


def _classify_daemon(dae: ScratchDaemon, now: datetime) -> tuple[Optional[Candidate], Optional[Skip]]:
    desc = f"scratch daemon pid {dae.pid} ({dae.cmdline[:60]})"
    tid = _daemon_tid(dae)
    if dae.client_count != 0:
        # Non-zero (>0 active connections) or -1 (client probe failed) both fail
        # closed: an unreadable daemon is never assumed idle.
        detail = (f"{dae.client_count} client connection(s) (active)" if dae.client_count > 0
                  else "client probe failed; cannot prove idle (fail closed)")
        return None, Skip(desc, detail, "active")
    age = dae.age_seconds(now)
    if age is None:
        return None, Skip(desc, "unknown start time; cannot clear 2h (fail closed)", "invariant")
    if age <= DAEMON_NO_CLIENT_SECONDS:   # strict: must be OLDER than 2h
        return None, Skip(desc, f"no clients but not older than 2h ({_fmt_dur(age)})", "threshold")
    return Candidate("scratch_daemon", tid, desc, ACTION_SIGTERM,
                     f"no clients for {_fmt_dur(age)} > 2h", str(dae.pid)), None


def _target_is_live(target_id: str, inv: Inventory, now: datetime) -> bool:
    """Would this recorded deferred target still classify as a candidate now?

    Returns True when the target has come back to life OR vanished -- either way
    it must be dropped from the ledger, never killed. Matching is by the exact
    generation-stamped id, so a same-named/-pid successor is treated as vanished
    (its predecessor is gone), never inheriting the old clock.
    """
    if target_id.startswith("tmux_session:"):
        for s in inv.tmux_sessions:
            if _session_tid(s) == target_id:
                cand, _skip = _classify_session(s, now)
                return cand is None      # reclassified as skip -> came back to life
        return True                       # gone
    if target_id.startswith("tmux_server:"):
        for srv in inv.tmux_servers:
            if _server_tid(srv) == target_id:
                cand, _skip = _classify_server(srv)
                return cand is None
        return True
    if target_id.startswith("scratch_daemon:"):
        for d in inv.scratch_daemons:
            if _daemon_tid(d) == target_id:
                cand, _skip = _classify_daemon(d, now)
                return cand is None
        return True
    return True


def plan_cycle(inv: Inventory, ledger: dict, now: datetime, protected: Iterable[str] = ()) -> Plan:
    """The whole decision, as a pure function of (inventory, ledger, now).

    `protected` is accepted for interface stability (there are no seat targets to
    protect any more; residue targets are OS objects, not streams)."""
    plan = Plan(now=now)
    old_targets: dict = dict((ledger or {}).get("targets", {}))
    new_targets: dict = {}

    candidates: list[Candidate] = []
    for sess in inv.tmux_sessions:
        cand, skip = _classify_session(sess, now)
        (candidates.append(cand) if cand else plan.skips.append(skip))
    for srv in inv.tmux_servers:
        cand, skip = _classify_server(srv)
        (candidates.append(cand) if cand else plan.skips.append(skip))
    for dae in inv.scratch_daemons:
        cand, skip = _classify_daemon(dae, now)
        (candidates.append(cand) if cand else plan.skips.append(skip))

    candidate_ids = {c.target_id for c in candidates}

    for cand in candidates:
        # Every residue target is deferred: honour the 24 h recovery window.
        prior = old_targets.get(cand.target_id)
        if prior is None:
            plan.recorded.append(cand)
            new_targets[cand.target_id] = {
                "kind": cand.kind, "label": cand.label, "rule": cand.rule,
                "action": cand.action, "exec_ref": cand.exec_ref, "trashed_at": _iso(now),
            }
            continue
        trashed_at = _parse_ts(prior.get("trashed_at")) or now
        elapsed = (now - trashed_at).total_seconds()
        new_targets[cand.target_id] = {**prior, "kind": cand.kind, "label": cand.label,
                                       "rule": cand.rule, "action": cand.action,
                                       "exec_ref": cand.exec_ref}
        if elapsed >= PURGE_WINDOW_SECONDS:
            plan.purges.append(cand)
        else:
            hours_left = (PURGE_WINDOW_SECONDS - elapsed) / 3600.0
            plan.pending.append((cand, hours_left))

    # Drop ledger entries that are no longer candidates -- came back to life or gone.
    for tid, entry in old_targets.items():
        if tid in new_targets:
            continue
        if tid in candidate_ids:
            continue  # still a candidate this cycle (already carried into new_targets above)
        if _target_is_live(tid, inv, now):
            plan.recovered.append({"target_id": tid, "label": entry.get("label", tid),
                                   "reason": "came back to life or vanished"})

    ledger_out = {"version": LEDGER_VERSION, "producer": LEDGER_PRODUCER,
                  "updated_at": _iso(now), "targets": new_targets}
    plan.next_ledger = ledger_out
    return plan


# --------------------------------------------------------------------------- #
# Card rendering
# --------------------------------------------------------------------------- #
def render_card(plan: Plan, mode: str) -> str:
    c = plan.counters()
    lines = [
        f"Residue reaper — {mode} — {HOST} @ {_iso(plan.now)}",
        (f"purged={c['purged']} recorded={c['recorded']} "
         f"pending={c['pending']} recovered={c['recovered']} skipped_live={c['skipped_live']} "
         f"skipped_active={c['skipped_active']} skipped_threshold={c['skipped_threshold']} "
         f"refused={c['refused']}"),
    ]
    # Act reports what actually happened; dry-run reports what it would do.
    purge_list = plan.purged_ok if plan.acted else plan.purges
    purge_hdr = "PURGED (phase 2):" if plan.acted else "PURGE (phase 2, window elapsed):"
    if purge_list:
        lines.append("\n" + purge_hdr)
        lines += [f"  • {x.label} — {x.rule}" for x in purge_list]
    if plan.live_skips:
        lines.append("\nSKIPPED (revived at act time — not acted, clock reset):")
        lines += [f"  • {s.target_desc} — {s.reason}" for s in plan.live_skips]
    if plan.recorded:
        lines.append("\nRECORDED (newly trashed; kill after 24h):")
        lines += [f"  • {x.label} — {x.rule}" for x in plan.recorded]
    if plan.pending:
        lines.append("\nPENDING (in 24h window):")
        lines += [f"  • {x.label} — {hrs:.1f}h left" for x, hrs in plan.pending]
    if plan.recovered:
        lines.append("\nRECOVERED (dropped from ledger):")
        lines += [f"  • {r['label']} — {r['reason']}" for r in plan.recovered]
    if plan.refused:
        lines.append("\nREFUSED (verb declined):")
        lines += [f"  • {r['label']} — {r['detail']}" for r in plan.refused]
    if plan.skips:
        lines.append("\nSKIPPED:")
        lines += [f"  • {s.target_desc} — {s.reason} [{s.category}]" for s in plan.skips]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Ledger persistence
# --------------------------------------------------------------------------- #
def load_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": LEDGER_VERSION, "producer": LEDGER_PRODUCER, "targets": {}}


def save_ledger(path: Path, ledger: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True))
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Gather layer (impure): builds an Inventory from live sources.
# --------------------------------------------------------------------------- #
def _run(cmd: Sequence[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run a command, never raising: a timeout or OS error returns a non-zero
    result with a marked stderr so callers degrade (fail closed) instead of
    crashing the cycle."""
    try:
        return subprocess.run(list(cmd), text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "orphan_reaper: timeout")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"orphan_reaper: {exc}")


def _process_command(pid: str) -> Optional[str]:
    """The command for a pid, "" if the pid is gone (ps clean-exit no rows), or
    None if the probe itself failed (fail closed: caller treats None as live)."""
    res = _run(["ps", "-o", "command=", "-p", str(pid)])
    if res.returncode == 0:
        return res.stdout.strip()
    if res.stderr.strip():        # ps error, not "no such pid"
        return None
    return ""                      # ps exits 1 with no output when the pid is gone


def _cmd_base(cmd: str) -> str:
    return cmd.split()[0].rsplit("/", 1)[-1] if cmd.strip() else ""


def _pid_is_work(pid: str, _depth: int = 0) -> bool:
    """A pid represents real work if its OWN command is a non-shell process, or
    any descendant is. Checking the pid itself (not only its children) is what
    keeps a live soak/gate pane -- whose pane process IS the worker with no
    shell wrapper -- from reading as dead (liveness false-negative).

    Fails closed: if a process/child probe errors (not a clean "no such pid"),
    the pid is treated as live so a transient ps/pgrep failure never lets a live
    pane read as dead and get purged."""
    if _depth > 60:            # cycle/recursion guard -> fail closed (assume live)
        return True
    cmd = _process_command(pid)
    if cmd is None:            # probe failed -> fail closed (assume live)
        return True
    base = _cmd_base(cmd)
    if base and base not in _SHELL_COMMANDS:
        return True
    res = _run(["pgrep", "-P", str(pid)])
    if res.returncode != 0:
        # rc=1 with no stderr is the normal "no children"; a real error carries
        # stderr and fails closed.
        return bool(res.stderr.strip())
    return any(_pid_is_work(child, _depth + 1) for child in res.stdout.split())


def _pane_is_live(pane_pid: str) -> bool:
    return _pid_is_work(pane_pid)


def _default_session_live(session_name: str) -> bool:
    """True if the session has any live pane across ALL its windows, OR its pane
    enumeration failed (fail closed). `-s` scopes to the whole session; without
    it list-panes returns only the current window and would miss a live worker in
    another window (which would then be wrongly recorded and later killed)."""
    res = _run(["tmux", "list-panes", "-s", "-t", session_name, "-F", "#{pane_pid}"])
    if res.returncode != 0:
        return True
    return any(_pane_is_live(pid) for pid in res.stdout.split())


def _list_default_sessions() -> tuple[TmuxSession, ...]:
    fmt = "#{session_name}\t#{session_attached}\t#{session_created}"
    res = _run(["tmux", "list-sessions", "-F", fmt])
    if res.returncode != 0:
        return ()          # cannot enumerate -> reap nothing (fail closed)
    return _parse_default_sessions(res.stdout, _default_session_live)


def _parse_default_sessions(stdout: str, session_live_fn) -> tuple[TmuxSession, ...]:
    out: list[TmuxSession] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0]
        attached = len(parts) > 1 and parts[1] not in ("", "0")
        created = _parse_ts(parts[2]) if len(parts) > 2 else None
        out.append(TmuxSession("default", None, name, attached, created, session_live_fn(name)))
    return tuple(out)


def _list_harness_servers(tmux_dir: Path) -> tuple[TmuxServer, ...]:
    servers: list[TmuxServer] = []
    if not tmux_dir.is_dir():
        return ()
    for sock in sorted(tmux_dir.iterdir()):
        name = sock.name
        if name == "default" or not HARNESS_SOCKET_RE.match(name):
            continue
        # A v2-* or attached session ANYWHERE on the socket protects the whole
        # server from kill-server (the per-session invariants must not be
        # bypassed). If EITHER enumeration fails, fail closed: mark the server
        # protected so it is skipped, never purged on unreadable evidence.
        sess = _run(["tmux", "-S", str(sock), "list-sessions",
                     "-F", "#{session_name}\t#{session_attached}"])
        panes = _run(["tmux", "-S", str(sock), "list-panes", "-a", "-F", "#{pane_pid}"])
        if sess.returncode != 0 or panes.returncode != 0:
            servers.append(TmuxServer(name, str(sock), -1, True, True))
            continue
        session_lines = [x for x in sess.stdout.splitlines() if x.strip()]
        session_count = len(session_lines)
        protected = False
        for line in session_lines:
            fields = line.split("\t")
            sname = fields[0]
            attached = len(fields) > 1 and fields[1] not in ("", "0")
            if sname.startswith("v2-") or attached:
                protected = True
                break
        any_live = any(_pane_is_live(pid) for pid in panes.stdout.split())
        servers.append(TmuxServer(name, str(sock), session_count, any_live, protected))
    return tuple(servers)


# Scratch daemons are v2 daemon processes started with `--port 0` (ephemeral
# port) against a temp/test db -- the smoke/soak harness leaves these behind. The
# real production daemon binds a real `--port <n>` and the shared sessions.db, so
# the `--port 0` filter never matches it. `client_count` is the number of
# ESTABLISHED inbound TCP connections; zero for > 2 h (deferred, 24 h window) is
# the reap signal, so a live gate's daemon (which lives minutes) is never purged.
# `--port 0` must be main.py's OWN server port (the first arg after main.py) --
# the production daemon binds a real `--port <n>` (with `--bind` first), so this
# never matches it even if a later arg contains "--port 0". Belt-and-suspenders:
# never match a cmdline that references the production sessions db.
_SCRATCH_CMD_RE = re.compile(r"\bmain\.py\s+--port\s+0(?:\s|$)")
_PROD_DB_MARKER = ".local/share/pentacle-stream/sessions.db"
# The db MUST live in a temp/test location -- the harness always points a scratch
# daemon at one. Requiring this positive identity (not just "not prod") keeps an
# unrelated `main.py --port 0` against some other db from ever being SIGTERM-ed.
_SCRATCH_TMPDB_RE = re.compile(
    r"--db\s+\S*(?:/T/|/tmp/|/private/var/folders/|/\.pentacle/test-logs/|/pytest-|[-/](?:smoke|soak)|/v2_[a-z]+\.db)")


def _is_scratch_daemon_cmd(cmdline: str) -> bool:
    return (bool(_SCRATCH_CMD_RE.search(cmdline))
            and _PROD_DB_MARKER not in cmdline
            and bool(_SCRATCH_TMPDB_RE.search(cmdline)))


def _scratch_client_count(pid: int) -> int:
    """ESTABLISHED inbound TCP connection count, or -1 if the probe failed.

    lsof exits non-zero both when there are no matching connections and when it
    could not read the process. Distinguish them: a clean exit with no rows is a
    real zero; a failure with stderr is -1 (unknown), which classify fails closed.
    """
    res = _run(["lsof", "-nP", "-p", str(pid), "-a", "-iTCP", "-sTCP:ESTABLISHED"])
    rows = [line for line in res.stdout.splitlines()[1:] if line.strip()]
    if rows:
        return len(rows)
    if res.returncode != 0 and res.stderr.strip():
        return -1
    return 0


def _parse_ps_lstart(lstart: str) -> Optional[datetime]:
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y"):
        try:
            # ps lstart is local time with no tz; assume the host's local tz.
            return datetime.strptime(lstart.strip(), fmt).astimezone().astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _list_scratch_daemons(client_count_fn=None) -> tuple[ScratchDaemon, ...]:
    client_count_fn = client_count_fn or _scratch_client_count
    # `pid lstart command`: pid, then lstart's fixed 5 tokens (Wkd Mon DD
    # HH:MM:SS YYYY), then the command line.
    res = _run(["ps", "-eo", "pid=,lstart=,command="])
    if res.returncode != 0:
        return ()
    return _parse_scratch_daemons(res.stdout, client_count_fn)


def _parse_scratch_daemons(stdout: str, client_count_fn) -> tuple[ScratchDaemon, ...]:
    out: list[ScratchDaemon] = []
    for line in stdout.splitlines():
        toks = line.split()
        if len(toks) < 7 or not toks[0].isdigit():
            continue
        cmdline = " ".join(toks[6:])
        if not _is_scratch_daemon_cmd(cmdline):
            continue
        pid = int(toks[0])
        started = _parse_ps_lstart(" ".join(toks[1:6]))
        out.append(ScratchDaemon(pid=pid, cmdline=cmdline,
                                 client_count=client_count_fn(pid), started_at=started))
    return tuple(out)


def gather_inventory(tmux_dir: Optional[Path] = None) -> Inventory:
    if tmux_dir is None:
        tmux_dir = Path(f"/private/tmp/tmux-{os.getuid()}")
    return Inventory(
        tmux_sessions=_list_default_sessions(),
        tmux_servers=_list_harness_servers(tmux_dir),
        scratch_daemons=_list_scratch_daemons(),
    )


# --------------------------------------------------------------------------- #
# Act layer (impure): execute a Plan's phase-2 purges (all residue is deferred).
# --------------------------------------------------------------------------- #
def _kill_session(name: str) -> tuple[bool, str]:
    res = _run(["tmux", "kill-session", "-t", name])
    return res.returncode == 0, (res.stderr or "").strip()[:200]


def _kill_server(socket_path: str) -> tuple[bool, str]:
    res = _run(["tmux", "-S", socket_path, "kill-server"])
    return res.returncode == 0, (res.stderr or "").strip()[:200]


def _sigterm(pid_str: str) -> tuple[bool, str]:
    try:
        os.kill(int(pid_str), signal.SIGTERM)
        return True, ""
    except (ProcessLookupError, ValueError, PermissionError) as exc:
        return False, str(exc)


def execute_plan(plan: Plan, purge_recheck=None) -> None:
    """Run the phase-2 purges; record OUTCOMES on the Plan.

    `purge_recheck (cand) -> (safe, reason)` is the final guard before every
    irreversible kill, evaluated against a probe re-taken after the plan gather so
    nothing that revived in the plan-gather -> act window is killed. A target that
    fails its recheck is dropped from the ledger (restarts its 24 h clock next
    cycle) and recorded in `live_skips`, NOT counted as an action. Only
    actually-completed kills land in `purged_ok`; failures land in `refused`.
    """
    plan.acted = True
    ledger_targets = plan.next_ledger.setdefault("targets", {})

    for cand in plan.purges:
        if purge_recheck is not None:
            safe, reason = purge_recheck(cand)
            if not safe:
                plan.live_skips.append(Skip(cand.label, f"pre-kill recheck: {reason}", "live"))
                ledger_targets.pop(cand.target_id, None)     # reset the clock
                continue
        if cand.action == ACTION_KILL_SESSION:
            ok, detail = _kill_session(cand.exec_ref)
        elif cand.action == ACTION_KILL_SERVER:
            ok, detail = _kill_server(cand.exec_ref)
        elif cand.action == ACTION_SIGTERM:
            ok, detail = _sigterm(cand.exec_ref)
        else:
            ok, detail = False, f"unknown action {cand.action}"
        if ok:
            plan.purged_ok.append(cand)
            ledger_targets.pop(cand.target_id, None)         # purged: gone
        else:
            plan.refused.append({"label": cand.label, "detail": detail})


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #
def post_card(plan: Plan, mode: str, dry: bool = False) -> bool:
    """Post the per-cycle Updates card. Returns True on success. `dry` (print
    only) is honoured for dry-run mode; in act mode the card is a MANDATORY
    durable audit and is always posted regardless of --no-notify."""
    body = render_card(plan, mode)
    key = plan.dedup_key(mode)
    severity = "warning" if plan.has_actions() and mode == "act" else "info"
    if dry and mode != "act":
        print("[notify skipped: --no-notify]\n" + body)
        return True
    cmd = ["agent-orch", "notify", "--title", f"Orphan reaper ({mode}) — {HOST}",
           "--message", body, "--producer", LEDGER_PRODUCER,
           "--severity", severity, "--dedup-key", key]
    res = _run(cmd)
    if res.returncode != 0:
        print(f"notify failed rc={res.returncode}: {(res.stderr or res.stdout)[:200]}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------- #
# Orchestration + CLI
# --------------------------------------------------------------------------- #
def run_once(*, act: bool, ledger_path: Path,
             tmux_dir: Optional[Path], notify: bool) -> Plan:
    inv = gather_inventory(tmux_dir)
    ledger = load_ledger(ledger_path)
    plan = plan_cycle(inv, ledger, _now())
    mode = "act" if act else "dry-run"
    if act:
        def _recheck_against_fresh(cand: Candidate) -> tuple[bool, str]:
            fresh = gather_inventory(tmux_dir)
            if _target_is_live(cand.target_id, fresh, _now()):
                return False, "became live or vanished since gather"
            return True, ""
        # Re-probe for each kill so a target that revived after the plan gather is
        # never acted on.
        execute_plan(plan, purge_recheck=_recheck_against_fresh)
        save_ledger(ledger_path, plan.next_ledger)
    ok = post_card(plan, mode, dry=not notify)
    if act and not ok:
        # The card is the durable audit for an act cycle; a failed post is a hard
        # error so the launchd run is visibly non-zero and re-driven.
        raise RuntimeError("orphan_reaper act cycle acted but failed to post the Updates card")
    return plan


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="External periodic residue reaper (dry-run by default).")
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", action="store_true", default=True,
                     help="Classify and report only; take no action (default).")
    grp.add_argument("--act", action="store_true", help="Perform phase-2 purges of due residue.")
    p.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER_PATH)
    p.add_argument("--tmux-dir", type=Path, default=None)
    p.add_argument("--no-notify", action="store_true", help="Do not post the Updates card (print it instead).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    plan = run_once(act=bool(args.act), ledger_path=args.ledger,
                    tmux_dir=args.tmux_dir, notify=not args.no_notify)
    c = plan.counters()
    print(json.dumps({"mode": "act" if args.act else "dry-run", **c}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
