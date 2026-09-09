"""Session-basetemp lifecycle helpers for the agent-orch pytest suite.

The conftest overrides pytest's basetemp to ``/tmp/ao-pytest-<rand>/`` so unix
socket paths stay under macOS's 104-char AF_UNIX limit (see ``conftest.py``).
pytest never cleans an *explicitly-set* basetemp, so each session used to leak
one ~300M dir permanently. This module owns create / record / cleanup / prune
so the conftest hooks stay thin and the logic is unit-testable without pytester
rootdir / sys.path trouble.

Deliberately dependency-light: **no ``_shared`` imports** so both the real
conftest and the behavioral pytester test can import it in isolation.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

# The basetemp lives directly under /tmp with this prefix. A random suffix per
# session prevents a second concurrent ``pytest`` invocation from colliding on
# (and rmtree-ing) a live session's dir.
TMP_ROOT = "/tmp"
PREFIX = "ao-pytest-"

# Prune tuning. Reaping abandoned dirs (from hard-killed sessions) is not
# time-critical, so keep the newest one and only remove siblings that are
# comfortably old. A generous min-age cheaply closes the concurrent-session
# race: mtime is an imperfect liveness signal, and a long-running session that
# creates no new child tmp dirs can legitimately leave a stale-mtime basetemp.
KEEP = 1
MIN_AGE_SECONDS = 6 * 60 * 60  # 6 hours

KEEP_ENV = "AO_PYTEST_KEEP_TMP"


def keep_tmp() -> bool:
    """True when ``AO_PYTEST_KEEP_TMP`` opts out of cleanup + prune."""
    val = os.environ.get(KEEP_ENV)
    if not val:
        return False
    return val.strip().lower() not in ("0", "false", "no", "off")


def create_basetemp(root: str = TMP_ROOT, prefix: str = PREFIX) -> str:
    """Create and return a fresh session basetemp under ``root``."""
    return tempfile.mkdtemp(prefix=prefix, dir=root)


def cleanup_basetemp(base: str | None) -> None:
    """Remove a basetemp this conftest created. No-op on ``None``; never raises."""
    if not base:
        return
    shutil.rmtree(base, ignore_errors=True)


def _prune_stale_ao_basetemps(
    root: str = TMP_ROOT,
    prefix: str = PREFIX,
    keep: int = KEEP,
    min_age_seconds: float = MIN_AGE_SECONDS,
    current_basetemp: str | None = None,
) -> list[str]:
    """Remove stale sibling basetemps left by hard-killed sessions.

    Keeps the newest ``keep`` matching dirs, removes the rest only when their
    mtime is older than ``min_age_seconds``, always skips ``current_basetemp``,
    ignores non-matching names, no-ops on a missing root, and never raises.
    Returns the list of removed paths (for tests / logging).
    """
    try:
        root_path = Path(root)
        if not root_path.is_dir():
            return []

        current = None
        if current_basetemp:
            try:
                current = Path(current_basetemp).resolve()
            except OSError:
                current = Path(current_basetemp)

        candidates: list[tuple[float, Path]] = []
        for entry in root_path.iterdir():
            if not entry.name.startswith(prefix):
                continue
            if not entry.is_dir():
                continue
            try:
                resolved = entry.resolve()
            except OSError:
                resolved = entry
            if current is not None and resolved == current:
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            candidates.append((mtime, entry))

        # Newest first: the newest `keep` are protected regardless of age.
        candidates.sort(key=lambda item: item[0], reverse=True)

        now = time.time()
        removed: list[str] = []
        for idx, (mtime, entry) in enumerate(candidates):
            if idx < keep:
                continue
            if (now - mtime) < min_age_seconds:
                continue  # too recent — could be a live concurrent session
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(str(entry))
        return removed
    except Exception:
        # Prune is best-effort disk hygiene; it must never fail a test session.
        return []
