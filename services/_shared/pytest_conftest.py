from __future__ import annotations

import ast
import os
import re
import shutil
import socket
import subprocess
import warnings
from pathlib import Path

import pytest


LIVE_DAEMON_MARKERS = ("live_daemon", "live_codex")
LIVE_DAEMON_START_CALLS = ("spawned_daemon", "spawn_and_capture_events")
LIVE_DAEMON_START_FIXTURES = (
    "daemon_with_seed_machines",
    "enrolled_ws_client",
    "tmux_isolated_server",
)
_ALLOWED_HOSTNAMES = {"hosta", "hosta", "hosta.example.local"}
_ALLOWED_HOST_IDS = {"hosta", "hosta"}
_STREAM_ID_ENV_VARS = ("PENTACLE_STREAM_ID", "AGENT_ORCH_STREAM_ID")
_HOST_ID_ENV_VARS = ("AGENT_ORCH_HOST_ID", "PENTACLE_HOST_ID", "PENTACLE_MACHINE_ID")

# Public guard: live-daemon tests may run only on an explicitly allowed fixture host.
_LIVE_DAEMON_SAFE_SESSION_PREFIXES = (
    "pentacle-test-",
    "ptest-",
    "ptm-",
    "fixture-agent-canary",
    "fixture-agent-load-",
    "codex-test-",
)
_AGENT_CHAT_SESSION_RE = re.compile(r"^(claude|codex)-")
_LIVE_DAEMON_STRICT_GUARD_ENV = "PENTACLE_LIVE_DAEMON_STRICT_OPERATOR_GUARD"
_LIVE_DAEMON_ALLOW_OPERATOR_CHATS_ENV = "PENTACLE_LIVE_DAEMON_ALLOW_OPERATOR_CHATS"
_DEFAULT_TMUX_BIN_CANDIDATES = (
    "/opt/homebrew/bin/tmux",
    "/usr/local/bin/tmux",
    "/usr/bin/tmux",
)


def reject_unmarked_live_daemon_starters(items: list[pytest.Item]) -> None:
    """Collection lint for tests/fixtures that can start chat_streamd."""
    by_path: dict[Path, list[pytest.Item]] = {}
    for item in items:
        by_path.setdefault(Path(str(item.fspath)), []).append(item)

    offenders: list[str] = []
    for path, path_items in by_path.items():
        scan = _scan_live_daemon_starters(path)
        daemon_fixtures = set(LIVE_DAEMON_START_FIXTURES)
        changed = True
        while changed:
            changed = False
            for fixture_name, dependencies in scan.fixture_dependencies.items():
                if fixture_name not in daemon_fixtures and dependencies & daemon_fixtures:
                    daemon_fixtures.add(fixture_name)
                    changed = True
            daemon_fixtures.update(scan.fixture_direct_calls)

        for item in path_items:
            if _item_has_marker(item, "live_daemon"):
                continue
            test_name = str(getattr(item, "originalname", None) or getattr(item, "name", ""))
            test_name = test_name.split("[", 1)[0]
            direct_calls = scan.test_direct_calls.get(test_name, set())
            fixture_hits = set(getattr(item, "fixturenames", ())) & daemon_fixtures
            if direct_calls or fixture_hits:
                detail = sorted(direct_calls | fixture_hits)
                offenders.append(f"{item.nodeid} -> {', '.join(detail)}")

    if offenders:
        locations = "\n".join(f"- {offender}" for offender in offenders)
        raise pytest.UsageError(
            "live_daemon_marker_required: tests that start chat_streamd must carry "
            "@pytest.mark.live_daemon; @pytest.mark.live_smoke is additive only.\n"
            f"{locations}"
        )


class _LiveDaemonStarterScan:
    def __init__(self) -> None:
        self.test_direct_calls: dict[str, set[str]] = {}
        self.fixture_direct_calls: set[str] = set()
        self.fixture_dependencies: dict[str, set[str]] = {}


def _scan_live_daemon_starters(path: Path) -> _LiveDaemonStarterScan:
    scan = _LiveDaemonStarterScan()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return scan

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        call_names = {
            _call_name(call)
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        }
        direct_calls = {name for name in call_names if name in LIVE_DAEMON_START_CALLS}
        if _is_pytest_fixture(node):
            scan.fixture_dependencies[node.name] = {arg.arg for arg in node.args.args}
            if direct_calls:
                scan.fixture_direct_calls.add(node.name)
        elif node.name.startswith("test_") and direct_calls:
            scan.test_direct_calls[node.name] = direct_calls
    return scan


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_pytest_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
    return False


def deselect_live_daemon_on_unapproved_host(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect live-daemon tests unless this collection is running on an approved fixture host.

    Exact keep rules:
    - ``PENTACLE_FORCE_LIVE_DAEMON=1`` keeps all items.
    - ``socket.gethostname()`` lowercased, with any DNS suffix stripped, must
      match an approved synthetic host label..
    - A canonical host id exposed in env, such as ``AGENT_ORCH_HOST_ID=hosta``
      or a stream id prefix like ``PENTACLE_STREAM_ID=hosta:...``, also keeps
      all items. This avoids importing daemon, tmux, or fixture helpers during
      collection.
    """
    if _live_daemon_allowed_on_public_host():
        return
    keep: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if any(_item_has_marker(item, marker) for marker in LIVE_DAEMON_MARKERS):
            deselected.append(item)
        else:
            keep.append(item)
    if deselected:
        prior = int(getattr(config, "_pentacle_live_guard_deselected", 0))
        setattr(config, "_pentacle_live_guard_deselected", prior + len(deselected))
        config.hook.pytest_deselected(items=deselected)
        items[:] = keep


def normalize_no_tests_after_live_guard(session: pytest.Session, exitstatus: int) -> None:
    if exitstatus == 5 and int(getattr(session.config, "_pentacle_live_guard_deselected", 0)) > 0:
        session.exitstatus = 0


def default_tmux_socket_path() -> str:
    """Path of tmux's shared DEFAULT socket — where non_fixture sessions live.

    Computed independently of ``TMUX_TMPDIR`` on purpose: non_fixture sessions are
    launched without a pinned ``TMUX_TMPDIR`` so they bind the default socket
    ``/tmp/tmux-<uid>/default`` (on macOS ``/tmp`` resolves to ``/local/tmp``
    via symlink). The test session pins ``TMUX_TMPDIR`` to a local dir, so the
    default path must NOT be derived from the env here.
    """
    return f"/tmp/tmux-{os.getuid()}/default"


def _resolve_tmux_bin_for_guard() -> str | None:
    found = shutil.which("tmux")
    if found:
        return found
    for candidate in _DEFAULT_TMUX_BIN_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def session_is_non_fixture_chat(name: str) -> bool:
    """True if ``name`` is an agent chat that is NOT a known test/canary session."""
    if not _AGENT_CHAT_SESSION_RE.match(name):
        return False
    return not name.startswith(_LIVE_DAEMON_SAFE_SESSION_PREFIXES)


def non_fixture_chats_on_default_socket() -> list[str] | None:
    """Fixture session names live on tmux's default socket.

    Returns ``[]`` when none are present, a non-empty list when some are, and
    ``None`` when presence cannot be determined (the caller fails closed).
    """
    tmux_bin = _resolve_tmux_bin_for_guard()
    if tmux_bin is None:
        # No tmux binary => no sessions, and live_daemon tests skip anyway.
        return []
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    try:
        result = subprocess.run(
            [tmux_bin, "-S", default_tmux_socket_path(), "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except Exception:
        return None
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        # An absent server/socket is the common, safe case (no non_fixture sessions).
        if "no server" in stderr or "no such file" in stderr or "error connecting" in stderr:
            return []
        return None
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [name for name in names if session_is_non_fixture_chat(name)]


def self_tmux_default_socket() -> str | None:
    """Socket path from ``$TMUX`` when THIS process runs inside a tmux pane on
    the DEFAULT socket; ``None`` when not inside tmux or on a local socket.

    Feeds the non_fixture-chat guard's self-preservation rule: a live_daemon
    failure mode that escapes to the default socket kills every session on it,
    including the chat this pytest process is running in. An agent cannot
    "supervise" a gate that just killed it, so the allow override must not be
    honorable from inside an at-risk session (2026-06-12: the agent fixing the
    first host kill overrode the guard from its own default-socket chat and
    was killed mid-gate by the second).
    """
    raw = os.environ.get("TMUX") or ""
    socket_path = raw.split(",", 1)[0].strip()
    if not socket_path:
        return None
    try:
        if os.path.realpath(socket_path) != os.path.realpath(default_tmux_socket_path()):
            return None
    except OSError:
        return None
    return socket_path


def live_daemon_non_fixture_guard_message(non_fixture_chats: list[str] | None) -> str | None:
    """Build the human message when non_fixture sessions are live on the default tmux
    socket during a live_daemon run. Returns ``None`` when there are none.
    """
    if not non_fixture_chats:
        return None
    listed = ", ".join(sorted(non_fixture_chats))
    return (
        f"non_fixture session(s) live on the default tmux socket {default_tmux_socket_path()!r}: "
        f"{listed}. live_daemon runs on a generic test box have wiped live agents twice "
        "(2026-06-02 default-socket teardown race, "
        "public fixture regression; 2026-06-12 "
        "public store-safety regression) — the gate is NOT assumed "
        "self-safe while non_fixture sessions are present."
    )


def refuse_live_daemon_if_non_fixture_chat_on_default_socket(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Fixture-session guard for live_daemon runs — refuse by default.

    History: this guard shipped 2026-06-02 as a hard refuse, was relaxed to
    warn-by-default the same day ("gate is self-safe", ff430eb), and on
    2026-06-12 a live_daemon run on the generic test box wiped every live hosta
    agent (local store ``delete_missing`` mass-stamp + teardown). The relaxation
    rationale is disproven: a live_daemon failure mode that escapes isolation
    kills non_fixture sessions. Default is therefore REFUSE when non_fixture sessions are on
    the default socket.

    Override (``PENTACLE_LIVE_DAEMON_ALLOW_OPERATOR_CHATS=1``) proceeds with a
    warning — for deliberate, supervised runs after store + socket isolation has
    been verified. The override is NOT honored when this pytest process itself
    runs inside a tmux session on the default socket (``$TMUX`` points there):
    the runner would be wiped by the failure mode it is waiving, which is how
    the second 2026-06-12 host kill happened. ``PENTACLE_LIVE_DAEMON_STRICT_
    OPERATOR_GUARD=1`` forces the refuse even when the allow env is set, and
    additionally makes an undetermined tmux state fatal. Runs during collection
    (before any fixture/daemon spawn).
    """
    has_live_daemon_items = any(
        _item_has_marker(item, marker) for item in items for marker in LIVE_DAEMON_MARKERS
    )
    if not has_live_daemon_items:
        return
    # This hook runs before pytest applies -m filtering, so live_daemon items
    # are still in `items` even for runs that will deselect them (e.g.
    # -m 'not live_daemon'). Only guard runs whose markexpr would actually
    # SELECT a live_daemon-marked test; on evaluation failure, fail closed
    # (keep the guard active).
    try:
        expression = (config.getoption("markexpr") or "").strip()
    except Exception:
        expression = ""
    if expression:
        try:
            from _pytest.mark.expression import Expression

            expr = Expression.compile(expression)
            if not expr.evaluate(lambda name: name in LIVE_DAEMON_MARKERS):
                return
        except Exception:
            pass
    strict = os.environ.get(_LIVE_DAEMON_STRICT_GUARD_ENV) == "1"
    allow = os.environ.get(_LIVE_DAEMON_ALLOW_OPERATOR_CHATS_ENV) == "1" and not strict
    non_fixture_chats = non_fixture_chats_on_default_socket()
    if non_fixture_chats is None:
        # tmux presence could not be determined; only fatal in strict mode.
        if strict:
            pytest.exit(
                "REFUSING live_daemon gate (strict mode): could not determine whether non_fixture "
                f"chats are live on the default tmux socket {default_tmux_socket_path()!r} "
                f"(tmux query failed). Unset {_LIVE_DAEMON_STRICT_GUARD_ENV} to proceed.",
                returncode=2,
            )
        return
    message = live_daemon_non_fixture_guard_message(non_fixture_chats)
    if message is None:
        return
    if allow:
        self_socket = self_tmux_default_socket()
        if self_socket is not None:
            pytest.exit(
                f"REFUSING live_daemon gate: {message} "
                f"{_LIVE_DAEMON_ALLOW_OPERATOR_CHATS_ENV}=1 is set, but this pytest "
                f"process is itself running inside a tmux session on that default "
                f"socket ({self_socket}) — one of the chats the gate would wipe. "
                "The allow override is for supervised runs and cannot be "
                "self-authorized by an at-risk session: that is exactly how the "
                "2026-06-12 second host kill happened (the agent fixing the first "
                "kill overrode this guard from its own chat and died mid-gate). "
                "Run the gate from a shell outside the default tmux server (plain "
                "ssh, or a session on a local TMUX_TMPDIR socket).",
                returncode=2,
            )
        warnings.warn(
            UserWarning(
                f"live_daemon: {message} Proceeding because "
                f"{_LIVE_DAEMON_ALLOW_OPERATOR_CHATS_ENV}=1."
            )
        )
        return
    pytest.exit(
        f"REFUSING live_daemon gate: {message} Set "
        f"{_LIVE_DAEMON_ALLOW_OPERATOR_CHATS_ENV}=1 to proceed anyway (deliberate, "
        "supervised runs only).",
        returncode=2,
    )


def deselect_auto_off_markers(
    config: pytest.Config,
    items: list[pytest.Item],
    marker_names: tuple[str, ...],
) -> None:
    expression = (config.getoption("markexpr") or "").strip()
    tokens = set(marker_tokens(expression))
    auto_off = tuple(marker for marker in marker_names if marker not in tokens)
    if not auto_off:
        return
    keep: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        if any(_item_has_marker(item, marker) for marker in auto_off):
            deselected.append(item)
        else:
            keep.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = keep


def marker_tokens(expression: str) -> list[str]:
    """Return marker identifiers referenced in a pytest ``-m`` expression."""
    if not expression:
        return []
    cleaned = expression
    for sep in ("(", ")", "and", "or", "not"):
        cleaned = cleaned.replace(sep, " ")
    return [token for token in cleaned.split() if token]


def _live_daemon_allowed_on_public_host() -> bool:
    if os.environ.get("PENTACLE_FORCE_LIVE_DAEMON") == "1":
        return True

    override = os.environ.get("PYTEST_HOSTNAME_OVERRIDE")
    if override is not None:
        return _hostname_is_allowed_host(override)

    if _hostname_is_allowed_host(socket.gethostname()):
        return True

    return any(_host_id_is_allowed(host_id) for host_id in _canonical_host_id_candidates())


def _hostname_is_allowed_host(hostname: str) -> bool:
    lowered = hostname.strip().lower()
    short = lowered.split(".", 1)[0]
    return lowered in _ALLOWED_HOSTNAMES or short in _ALLOWED_HOSTNAMES


def _canonical_host_id_candidates() -> list[str]:
    candidates: list[str] = []
    for env_name in _HOST_ID_ENV_VARS:
        value = os.environ.get(env_name)
        if value:
            candidates.append(value)
    for env_name in _STREAM_ID_ENV_VARS:
        value = os.environ.get(env_name)
        if value and ":" in value:
            candidates.append(value.split(":", 1)[0])
    return candidates


def _host_id_is_allowed(host_id: str) -> bool:
    return host_id.strip().lower() in _ALLOWED_HOST_IDS


def _item_has_marker(item: pytest.Item, marker: str) -> bool:
    iter_markers = getattr(item, "iter_markers", None)
    if callable(iter_markers):
        return any(True for _ in iter_markers(name=marker))
    return marker in getattr(item, "keywords", {})
