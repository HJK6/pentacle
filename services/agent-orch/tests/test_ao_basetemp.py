"""Tests for the session-basetemp lifecycle helpers (``_ao_basetemp``).

Covers the spec's locked validation plan:
1. unit tests for ``_prune_stale_ao_basetemps``;
2. behavioral inner-pytest run (self-clean at sessionfinish; ``AO_PYTEST_KEEP_TMP``
   preserves the basetemp and suppresses the prune);
3. an explicit ``--basetemp`` run leaves stale siblings and the target alone.

The behavioral / explicit-basetemp cases drive an inner pytest session in a
**fresh interpreter** via ``subprocess`` (equivalent to
``pytester.runpytest_subprocess``: clean module state, no ``_TMPDIR_OVERRIDDEN``
carryover, isolated rootdir). The suite does not enable the ``pytester`` plugin,
and enabling it would require a rootdir-level ``pytest_plugins`` that loads into
every gate session — out of scope for this fix — so a direct subprocess is used.
The inner runs are pointed at an isolated tmp root (``AO_TEST_ROOT``) so they
never create, prune, or clean anything under the real ``/tmp``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import _ao_basetemp


def _mkdir(root: Path, name: str, age_seconds: float = 0.0) -> Path:
    d = root / name
    d.mkdir()
    if age_seconds:
        t = time.time() - age_seconds
        os.utime(d, (t, t))
    return d


# --------------------------------------------------------------------------- #
# 1. Unit: _prune_stale_ao_basetemps
# --------------------------------------------------------------------------- #

def test_prune_keeps_newest_and_removes_older(tmp_path):
    old = 10_000
    d0 = _mkdir(tmp_path, "ao-pytest-0", age_seconds=old + 3)
    d1 = _mkdir(tmp_path, "ao-pytest-1", age_seconds=old + 2)
    d2 = _mkdir(tmp_path, "ao-pytest-2", age_seconds=old + 1)
    d3 = _mkdir(tmp_path, "ao-pytest-3", age_seconds=old + 0)  # newest

    removed = _ao_basetemp._prune_stale_ao_basetemps(
        root=str(tmp_path), prefix="ao-pytest-", keep=1, min_age_seconds=1,
    )

    assert d3.is_dir()  # newest kept
    assert not d0.exists() and not d1.exists() and not d2.exists()
    assert set(removed) == {str(d0), str(d1), str(d2)}


def test_prune_honors_min_age(tmp_path):
    recent = _mkdir(tmp_path, "ao-pytest-recent", age_seconds=0)  # ~now
    stale = _mkdir(tmp_path, "ao-pytest-stale", age_seconds=10_000)

    removed = _ao_basetemp._prune_stale_ao_basetemps(
        root=str(tmp_path), prefix="ao-pytest-", keep=0, min_age_seconds=3600,
    )

    assert recent.is_dir()  # too recent to reap
    assert not stale.exists()
    assert removed == [str(stale)]


def test_prune_never_deletes_current_basetemp(tmp_path):
    current = _mkdir(tmp_path, "ao-pytest-current", age_seconds=10_000)
    other = _mkdir(tmp_path, "ao-pytest-other", age_seconds=10_000)

    removed = _ao_basetemp._prune_stale_ao_basetemps(
        root=str(tmp_path), prefix="ao-pytest-", keep=0, min_age_seconds=0,
        current_basetemp=str(current),
    )

    assert current.is_dir()  # always skipped even at keep=0
    assert not other.exists()
    assert removed == [str(other)]


def test_prune_ignores_non_matching_names(tmp_path):
    keep_me = _mkdir(tmp_path, "unrelated-dir", age_seconds=10_000)
    stale = _mkdir(tmp_path, "ao-pytest-x", age_seconds=10_000)

    removed = _ao_basetemp._prune_stale_ao_basetemps(
        root=str(tmp_path), prefix="ao-pytest-", keep=0, min_age_seconds=0,
    )

    assert keep_me.is_dir()
    assert not stale.exists()
    assert removed == [str(stale)]


def test_prune_noops_on_missing_root(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert _ao_basetemp._prune_stale_ao_basetemps(root=str(missing)) == []


def test_prune_never_raises_on_non_dir_root(tmp_path):
    a_file = tmp_path / "a-file"
    a_file.write_text("x")
    # A file where a dir is expected must not raise.
    assert _ao_basetemp._prune_stale_ao_basetemps(root=str(a_file)) == []


def test_keep_tmp_env_parsing(monkeypatch):
    monkeypatch.delenv(_ao_basetemp.KEEP_ENV, raising=False)
    assert _ao_basetemp.keep_tmp() is False
    for falsy in ("", "0", "false", "False", "no", "off"):
        monkeypatch.setenv(_ao_basetemp.KEEP_ENV, falsy)
        assert _ao_basetemp.keep_tmp() is False, falsy
    for truthy in ("1", "true", "yes", "anything"):
        monkeypatch.setenv(_ao_basetemp.KEEP_ENV, truthy)
        assert _ao_basetemp.keep_tmp() is True, truthy


def test_create_and_cleanup_roundtrip(tmp_path):
    base = _ao_basetemp.create_basetemp(root=str(tmp_path), prefix="ao-pytest-")
    assert Path(base).is_dir()
    assert Path(base).name.startswith("ao-pytest-")
    _ao_basetemp.cleanup_basetemp(base)
    assert not Path(base).exists()
    _ao_basetemp.cleanup_basetemp(None)  # no-op, no raise


# --------------------------------------------------------------------------- #
# Inner-pytest harness (fresh-interpreter subprocess) for cases 2 & 3
# --------------------------------------------------------------------------- #

_REAL_TESTS_DIR = str(Path(__file__).resolve().parent)

# Inner conftest mirrors the real conftest's thin wiring: create + record +
# prune at configure (controller-only, create-branch-only), rmtree at
# sessionfinish, both honoring AO_PYTEST_KEEP_TMP. Rooted at AO_TEST_ROOT so it
# never touches the host's real /tmp.
_INNER_CONFTEST = '''
import os, sys
from pathlib import Path
sys.path.insert(0, {real_tests_dir!r})
import _ao_basetemp

_ROOT = os.environ["AO_TEST_ROOT"]
_MARKER = os.environ["AO_MARKER"]
_CREATED = None


def pytest_configure(config):
    global _CREATED
    is_controller = getattr(config, "workerinput", None) is None
    if is_controller and not getattr(config.option, "basetemp", None):
        base = _ao_basetemp.create_basetemp(root=_ROOT)
        config.option.basetemp = base
        _CREATED = base
        Path(_MARKER).write_text(base)
        if not _ao_basetemp.keep_tmp():
            _ao_basetemp._prune_stale_ao_basetemps(
                root=_ROOT, min_age_seconds=0, current_basetemp=base,
            )


def pytest_sessionfinish(session, exitstatus):
    global _CREATED
    if _CREATED and not _ao_basetemp.keep_tmp():
        _ao_basetemp.cleanup_basetemp(_CREATED)
        _CREATED = None
'''

_INNER_TEST = '''
import os


def test_basetemp_is_live(tmp_path, pytestconfig):
    # Requesting tmp_path materializes the basetemp (pytest creates it lazily).
    base = pytestconfig.option.basetemp
    assert base and os.path.isdir(str(base))
    assert str(tmp_path).startswith(str(base))
'''


def _make_inner(tmp_path: Path):
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "conftest.py").write_text(
        _INNER_CONFTEST.format(real_tests_dir=_REAL_TESTS_DIR)
    )
    (inner / "test_inner.py").write_text(_INNER_TEST)
    root = tmp_path / "aoroot"
    root.mkdir()
    marker = tmp_path / "created_basetemp.txt"
    return inner, root, marker


def _run_inner(inner: Path, root: Path, marker: Path, *, keep=False, basetemp=None):
    env = dict(os.environ)
    env["AO_TEST_ROOT"] = str(root)
    env["AO_MARKER"] = str(marker)
    env.pop("AO_PYTEST_KEEP_TMP", None)
    if keep:
        env["AO_PYTEST_KEEP_TMP"] = "1"
    cmd = [sys.executable, "-m", "pytest", str(inner), "-q", "-p", "no:cacheprovider"]
    if basetemp is not None:
        cmd += ["--basetemp", str(basetemp)]
    proc = subprocess.run(
        cmd, cwd=str(inner), env=env, capture_output=True, text=True, timeout=120,
    )
    return proc


# --------------------------------------------------------------------------- #
# 2. Behavioral: self-clean at sessionfinish; KEEP_TMP preserves + suppresses prune
# --------------------------------------------------------------------------- #

def test_behavioral_self_clean_at_sessionfinish(tmp_path):
    inner, root, marker = _make_inner(tmp_path)
    proc = _run_inner(inner, root, marker)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    created = marker.read_text().strip()
    assert created  # inner test asserted the basetemp existed mid-run
    assert not Path(created).exists()  # removed at sessionfinish


def test_behavioral_keep_tmp_preserves_and_suppresses_prune(tmp_path):
    inner, root, marker = _make_inner(tmp_path)
    # A comfortably-old sibling: the prune WOULD reap it (min_age 0 in inner),
    # so its survival proves KEEP_TMP suppressed the prune.
    stale = _mkdir(root, "ao-pytest-stale", age_seconds=10_000)

    proc = _run_inner(inner, root, marker, keep=True)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    created = marker.read_text().strip()
    assert Path(created).is_dir()  # KEEP_TMP: basetemp preserved
    assert stale.is_dir()          # KEEP_TMP: prune suppressed


# --------------------------------------------------------------------------- #
# 3. Explicit --basetemp: stale sibling and target left alone; no new dir created
# --------------------------------------------------------------------------- #

def test_explicit_basetemp_is_respected(tmp_path):
    inner, root, marker = _make_inner(tmp_path)
    stale = _mkdir(root, "ao-pytest-stale", age_seconds=10_000)
    explicit = tmp_path / "explicit-bt"  # does not pre-exist; pytest creates it

    proc = _run_inner(inner, root, marker, basetemp=explicit)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # (a) stale sibling not removed
    assert stale.is_dir()
    # (b) no new ao-pytest-* created in the root (only the pre-existing stale)
    assert sorted(p.name for p in root.iterdir()) == ["ao-pytest-stale"]
    # (c) the explicit target is used and left intact (our code never cleaned it)
    assert explicit.is_dir()
    assert not marker.exists()  # create branch skipped → no marker written
