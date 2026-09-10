"""Local resource accounting helpers for hermetic tests.

The public test suite tracks only resources created by the current process.
Host labels and session names are synthetic, and no network, deployment, or
production-store operation is performed by this module.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import signal
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping, Sequence


LOCAL_HOST_ALIASES = {"local", "localhost", "example.local"}
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = ("*:public-protected-*",)
DEFAULT_BACKGROUND_DAEMON_PATTERNS: tuple[str, ...] = ("*:public-background-*",)
PROTECT_FILE = Path.home() / ".config" / "public-test-protect.txt"
AGENT_ORCH_ROOT = Path(os.environ.get("AGENT_ORCH_ROOT", "~/.agent-orch")).expanduser()
PROD_SESSION_DB: Path | None = None
TEST_PREFIX_BASE = "pentacle-test-"
OBSERVABLE_TEST_PREFIX_BASE = "ptest-"
TMUX_TIMEOUT_S = 5.0
KILL_TIMEOUT_S = 5.0
REMOTE_TEST_CWD_PREFIXES = ("/tmp/public-test",)
REMOTE_TEST_CWD_PREFIX = REMOTE_TEST_CWD_PREFIXES[0]
_CODEX_VERSION_UNSET = "__codex_dismissed_version_unset__"


@dataclass(frozen=True)
class ResourceState:
    local_tmux_sessions: frozenset[str] = frozenset()
    agent_orch_workspaces: frozenset[Path] = frozenset()
    remote_tmux_sessions: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    process_pids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class ResourceDiff:
    added_local_tmux_sessions: frozenset[str] = frozenset()
    removed_local_tmux_sessions: frozenset[str] = frozenset()
    added_agent_orch_workspaces: frozenset[Path] = frozenset()
    removed_agent_orch_workspaces: frozenset[Path] = frozenset()
    added_remote_tmux_sessions: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    removed_remote_tmux_sessions: Mapping[str, frozenset[str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    added_process_pids: frozenset[int] = frozenset()
    removed_process_pids: frozenset[int] = frozenset()

    def is_empty(self) -> bool:
        return not any(
            (
                self.added_local_tmux_sessions,
                self.removed_local_tmux_sessions,
                self.added_agent_orch_workspaces,
                self.removed_agent_orch_workspaces,
                any(self.added_remote_tmux_sessions.values()),
                any(self.removed_remote_tmux_sessions.values()),
                self.added_process_pids,
                self.removed_process_pids,
            )
        )


@dataclass
class ResourceLedger:
    test_id: str
    test_prefix: str = field(default_factory=lambda: f"{TEST_PREFIX_BASE}{uuid.uuid4().hex}-")
    tmux_tmpdir: Path | None = None
    local_tmux_sessions: set[str] = field(default_factory=set)
    isolated_tmux_servers: dict[Path, int | None] = field(default_factory=dict)
    agent_orch_workspaces: set[Path] = field(default_factory=set)
    remote_tmux_sessions: dict[str, set[str]] = field(default_factory=dict)
    remote_trusted_cwds: dict[str, set[str]] = field(default_factory=dict)
    remote_cwd_dirs: dict[str, set[str]] = field(default_factory=dict)
    remote_codex_trusted_cwds: dict[str, set[str]] = field(default_factory=dict)
    remote_codex_version_restore: dict[str, object] = field(default_factory=dict)
    ssh_subprocess_pids: set[int] = field(default_factory=set)

    def record_local_session(self, name: str) -> None:
        _validate_test_owned_session("local", name, self)
        self.local_tmux_sessions.add(name)

    def record_isolated_server(self, tmpdir: Path, pid: int | None = None) -> None:
        resolved = Path(tmpdir).resolve()
        self.tmux_tmpdir = resolved
        self.isolated_tmux_servers[resolved] = pid

    def record_workspace(self, path: Path) -> None:
        self.agent_orch_workspaces.add(_safe_agent_orch_workspace(path))

    def record_remote_session(self, host: str, name: str) -> None:
        canonical = _canonical_host(host)
        _validate_test_owned_session(canonical, name, self)
        self.remote_tmux_sessions.setdefault(canonical, set()).add(name)

    def record_remote_trusted_cwd(self, host: str, cwd: str) -> None:
        canonical = _canonical_host(host)
        _validate_remote_test_cwd(canonical, cwd)
        self.remote_trusted_cwds.setdefault(canonical, set()).add(cwd)

    def record_remote_cwd_dir(self, host: str, cwd: str) -> None:
        canonical = _canonical_host(host)
        _validate_remote_test_cwd(canonical, cwd)
        self.remote_cwd_dirs.setdefault(canonical, set()).add(cwd)

    def record_remote_codex_trusted_cwd(self, host: str, realpath_cwd: str) -> None:
        canonical = _canonical_host(host)
        _validate_remote_test_cwd(canonical, realpath_cwd)
        self.remote_codex_trusted_cwds.setdefault(canonical, set()).add(realpath_cwd)

    def record_remote_codex_version_restore(self, host: str, prior_dismissed_version: object) -> None:
        self.remote_codex_version_restore.setdefault(_canonical_host(host), prior_dismissed_version)

    def record_ssh_pid(self, pid: int) -> None:
        if pid <= 0:
            raise CleanupViolation(f"invalid process id recorded: {pid!r}")
        self.ssh_subprocess_pids.add(pid)


class CleanupViolation(AssertionError):
    """The test mutated a resource outside its ledger or hit a safety guard."""


class LeakedResource(AssertionError):
    """The test left a ledger-owned resource alive after normal teardown."""


def make_test_prefix() -> str:
    return f"{TEST_PREFIX_BASE}{uuid.uuid4().hex}-"


def make_observable_test_prefix() -> str:
    return f"{OBSERVABLE_TEST_PREFIX_BASE}{uuid.uuid4().hex}-"


def _parse_patterns(patterns: list[str]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for pattern in patterns:
        host, sep, session_pattern = pattern.partition(":")
        parsed.append(("*", host) if not sep else (_canonical_host(host), session_pattern))
    return tuple(parsed)


def effective_protected_patterns() -> tuple[tuple[str, str], ...]:
    patterns = list(DEFAULT_PROTECTED_PATTERNS)
    if PROTECT_FILE.is_file():
        patterns.extend(
            line.strip()
            for line in PROTECT_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
    return _parse_patterns(patterns)


def effective_background_daemon_patterns() -> tuple[tuple[str, str], ...]:
    return _parse_patterns(list(DEFAULT_BACKGROUND_DAEMON_PATTERNS))


def _matches_any(host: str, session_name: str, patterns: tuple[tuple[str, str], ...]) -> bool:
    canonical = _canonical_host(host)
    return any(
        pattern_host in {"*", canonical} and fnmatch.fnmatchcase(session_name, pattern)
        for pattern_host, pattern in patterns
    )


def is_protected_session(host: str, session_name: str) -> bool:
    return _matches_any(host, session_name, effective_protected_patterns())


def is_background_daemon_session(host: str, session_name: str) -> bool:
    return _matches_any(host, session_name, effective_background_daemon_patterns())


def capture_state(
    remote_hosts: Sequence[str] | None = None,
    *,
    process_pids: Sequence[int] | None = None,
) -> ResourceState:
    """Capture local state; host arguments are accepted for API compatibility only."""
    del remote_hosts
    live_pids = frozenset(pid for pid in process_pids or () if _pid_is_live(pid))
    return ResourceState(
        local_tmux_sessions=frozenset(_list_local_tmux_sessions()),
        agent_orch_workspaces=frozenset(_list_agent_orch_workspaces()),
        remote_tmux_sessions=MappingProxyType({}),
        process_pids=live_pids,
    )


def state_diff(pre: ResourceState, post: ResourceState) -> ResourceDiff:
    hosts = set(pre.remote_tmux_sessions) | set(post.remote_tmux_sessions)
    added_remote = {
        host: post.remote_tmux_sessions.get(host, frozenset()) - pre.remote_tmux_sessions.get(host, frozenset())
        for host in hosts
    }
    removed_remote = {
        host: pre.remote_tmux_sessions.get(host, frozenset()) - post.remote_tmux_sessions.get(host, frozenset())
        for host in hosts
    }
    return ResourceDiff(
        added_local_tmux_sessions=post.local_tmux_sessions - pre.local_tmux_sessions,
        removed_local_tmux_sessions=pre.local_tmux_sessions - post.local_tmux_sessions,
        added_agent_orch_workspaces=post.agent_orch_workspaces - pre.agent_orch_workspaces,
        removed_agent_orch_workspaces=pre.agent_orch_workspaces - post.agent_orch_workspaces,
        added_remote_tmux_sessions=MappingProxyType(added_remote),
        removed_remote_tmux_sessions=MappingProxyType(removed_remote),
        added_process_pids=post.process_pids - pre.process_pids,
        removed_process_pids=pre.process_pids - post.process_pids,
    )


@contextmanager
def resource_cleanup_audit(
    ledger: ResourceLedger,
    *,
    remote_hosts: Sequence[str] | None = None,
) -> Iterator[ResourceLedger]:
    pre = capture_state(remote_hosts, process_pids=ledger.ssh_subprocess_pids)
    try:
        yield ledger
    finally:
        post = capture_state(remote_hosts, process_pids=ledger.ssh_subprocess_pids)
        diff = state_diff(pre, post)
        violations = diff_outside_ledger(diff, ledger)
        leaks = leftover_in_ledger(diff, ledger)
        cleanup_errors = teardown_ledger(ledger)
        if violations:
            raise CleanupViolation("; ".join(violations))
        if leaks:
            raise LeakedResource("; ".join(leaks))
        if cleanup_errors:
            raise CleanupViolation("; ".join(cleanup_errors))


def diff_outside_ledger(diff: ResourceDiff, ledger: ResourceLedger) -> list[str]:
    problems: list[str] = []
    added_local = {
        s for s in diff.added_local_tmux_sessions - ledger.local_tmux_sessions
        if not (is_protected_session("local", s) or is_background_daemon_session("local", s))
    }
    removed_local = {
        s for s in diff.removed_local_tmux_sessions - ledger.local_tmux_sessions
        if not is_background_daemon_session("local", s)
    }
    if added_local:
        problems.append(f"unrecorded local tmux sessions appeared: {sorted(added_local)!r}")
    if removed_local:
        problems.append(f"unrecorded local tmux sessions disappeared: {sorted(removed_local)!r}")
    added_workspaces = diff.added_agent_orch_workspaces - ledger.agent_orch_workspaces
    removed_workspaces = diff.removed_agent_orch_workspaces - ledger.agent_orch_workspaces
    if added_workspaces:
        problems.append(f"unrecorded workspaces appeared: {sorted(map(str, added_workspaces))!r}")
    if removed_workspaces:
        problems.append(f"unrecorded workspaces disappeared: {sorted(map(str, removed_workspaces))!r}")
    for host, sessions in diff.added_remote_tmux_sessions.items():
        unrecorded = sessions - ledger.remote_tmux_sessions.get(host, set())
        if unrecorded and not all(is_background_daemon_session(host, s) for s in unrecorded):
            problems.append(f"unrecorded synthetic sessions appeared on {host}: {sorted(unrecorded)!r}")
    for host, sessions in diff.removed_remote_tmux_sessions.items():
        unrecorded = sessions - ledger.remote_tmux_sessions.get(host, set())
        if unrecorded and not all(is_background_daemon_session(host, s) for s in unrecorded):
            problems.append(f"unrecorded synthetic sessions disappeared on {host}: {sorted(unrecorded)!r}")
    return problems


def leftover_in_ledger(diff: ResourceDiff, ledger: ResourceLedger) -> list[str]:
    leaks: list[str] = []
    local_left = diff.added_local_tmux_sessions & ledger.local_tmux_sessions
    if local_left:
        leaks.append(f"ledger local tmux sessions leaked: {sorted(local_left)!r}")
    workspace_left = diff.added_agent_orch_workspaces & ledger.agent_orch_workspaces
    if workspace_left:
        leaks.append(f"ledger workspaces leaked: {sorted(map(str, workspace_left))!r}")
    for host, sessions in diff.added_remote_tmux_sessions.items():
        remote_left = sessions & ledger.remote_tmux_sessions.get(host, set())
        if remote_left:
            leaks.append(f"ledger synthetic sessions leaked on {host}: {sorted(remote_left)!r}")
    if diff.added_process_pids:
        leaks.append(f"ledger processes still live: {sorted(diff.added_process_pids)!r}")
    return leaks


def teardown_ledger(ledger: ResourceLedger) -> list[str]:
    errors: list[str] = []
    tmux_bin = shutil.which("tmux")
    for session in sorted(ledger.local_tmux_sessions):
        try:
            _validate_test_owned_session("local", session, ledger)
            if tmux_bin:
                _run([tmux_bin, "kill-session", "-t", session], timeout_s=KILL_TIMEOUT_S)
        except Exception as exc:
            errors.append(f"local tmux cleanup refused/failed for {session!r}: {exc}")
    for path in sorted(ledger.agent_orch_workspaces):
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"workspace cleanup failed for {path}: {exc}")
    for pid in sorted(ledger.ssh_subprocess_pids):
        if _pid_is_live(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError as exc:
                errors.append(f"process cleanup failed for {pid}: {exc}")
    for tmpdir in sorted(ledger.isolated_tmux_servers):
        if tmpdir.exists():
            try:
                shutil.rmtree(tmpdir)
            except OSError as exc:
                errors.append(f"temporary server cleanup failed for {tmpdir}: {exc}")
    return errors


def sweep_prod_session_store_for_test_sessions(
    ledger: ResourceLedger,
    *,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Optionally clean an explicitly supplied local test database."""
    if db_path is None or not Path(db_path).exists():
        return {"sessions": 0, "lifecycle_audit": 0, "agent_ids": 0, "trash": 0}
    counts = {"sessions": 0, "lifecycle_audit": 0, "agent_ids": 0, "trash": 0}
    with sqlite3.connect(Path(db_path)) as conn:
        for host, sessions in sorted(ledger.remote_tmux_sessions.items()):
            for session in sorted(sessions):
                params = (_canonical_host(host), session)
                for table in counts:
                    if not _sqlite_table_exists(conn, table):
                        continue
                    cursor = conn.execute(
                        f"DELETE FROM {table} WHERE host = ? AND session_name = ?", params
                    )
                    counts[table] += max(int(cursor.rowcount), 0)
    return counts


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _validate_remote_test_cwd(host: str, cwd: str) -> None:
    if _canonical_host(host) in LOCAL_HOST_ALIASES:
        raise CleanupViolation(f"synthetic host must not be local: {host!r}")
    if not isinstance(cwd, str) or not cwd.startswith(REMOTE_TEST_CWD_PREFIXES):
        raise CleanupViolation(f"refusing cwd outside the public test prefix: {cwd!r}")
    if "\n" in cwd or "\x00" in cwd:
        raise CleanupViolation(f"refusing cwd with control characters: {cwd!r}")


def _validate_test_owned_session(host: str, session_name: str, ledger: ResourceLedger) -> None:
    canonical = _canonical_host(host)
    if is_protected_session(canonical, session_name):
        raise CleanupViolation(f"refusing protected session {canonical}:{session_name}")
    if not session_name.startswith(ledger.test_prefix):
        raise CleanupViolation(
            f"session {canonical}:{session_name} lacks test prefix {ledger.test_prefix!r}"
        )


def _list_local_tmux_sessions() -> set[str]:
    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        return set()
    result = _run([tmux_bin, "list-sessions", "-F", "#{session_name}"], timeout_s=TMUX_TIMEOUT_S)
    return {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}


def _list_remote_tmux_sessions(host: str) -> set[str]:
    del host
    return set()


def _list_agent_orch_workspaces() -> set[Path]:
    if not AGENT_ORCH_ROOT.is_dir():
        return set()
    root = AGENT_ORCH_ROOT.resolve()
    return {
        child.resolve()
        for child in AGENT_ORCH_ROOT.iterdir()
        if child.is_dir() and not child.is_symlink() and root in child.resolve().parents
    }


def _safe_agent_orch_workspace(path: Path) -> Path:
    resolved = Path(path).resolve()
    if Path(path).is_symlink() or resolved.is_symlink():
        raise CleanupViolation(f"refusing symlink workspace: {resolved}")
    return resolved


def _canonical_host(host: str) -> str:
    lowered = str(host).strip().lower()
    configured = os.environ.get("PENTACLE_TEST_LOCAL_HOST", "").strip().lower()
    aliases = LOCAL_HOST_ALIASES | ({configured} if configured else set())
    return "local" if lowered in aliases else lowered


def _ssh_target(host: str) -> str:
    """Return a synthetic host label for compatibility with older test helpers."""
    return _canonical_host(host)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _run(
    cmd: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd), env=dict(env) if env is not None else None,
            capture_output=True, text=True, check=False, timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(list(cmd), 1, "", "")


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return isinstance(pid, int) and pid > 0 and False
    return True
