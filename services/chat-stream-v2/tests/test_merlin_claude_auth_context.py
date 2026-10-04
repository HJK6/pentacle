"""Synthetic-config tests for the generic Claude auth-context shim.

The shim (``tools/merlin_claude_auth_context.sh``) is the lane-5 emitter of the
pinned auth-context marker. These tests run it as a subprocess with stubbed
``aws``/``security``/``claude`` on PATH — no real AWS, keychain, or provider —
so they are CI-safe. They assert: host-specific values come from configuration
(no hard-coded personal paths), the failure contract is stable, and no secret is
emitted.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "tools" / "merlin_claude_auth_context.sh"
MARKER_ROOT = Path("/tmp/pentacle-auth-context")  # contract-pinned marker dir
CONTRACT_BYTES = b"provider_auth_context_unavailable\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _marker_path(stream_id: str) -> Path:
    digest = hashlib.sha256(stream_id.encode()).hexdigest()
    return MARKER_ROOT / f"{digest}.code"


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # A log so tests can assert whether aws/security were invoked.
    log = tmp_path / "calls.log"
    _stub(bindir / "aws", f'echo aws >> "{log}"\nprintf "%s" "$FAKE_CRED"\n')
    _stub(bindir / "security", f'echo security >> "{log}"\nexit "${{FAKE_SECURITY_RC:-0}}"\n')
    sentinel = tmp_path / "claude_ran"
    claude = tmp_path / "claude"
    _stub(claude, f'echo "$@" > "{sentinel}"\nexit 0\n')
    home = tmp_path / "home"
    home.mkdir()
    base = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "AGENT_ORCH_STREAM_ID": f"test:{tmp_path.name}",
        "PENTACLE_AUTH_CLAUDE_BIN": str(claude),
        "PENTACLE_AUTH_LOGIN_KEYCHAIN": str(tmp_path / "login.keychain-db"),
    }
    marker = _marker_path(base["AGENT_ORCH_STREAM_ID"])
    yield {"base": base, "log": log, "sentinel": sentinel, "claude": claude, "marker": marker}
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def _run(env, extra, arg="hi"):
    e = dict(env["base"]); e.update(extra)
    return subprocess.run([str(SHIM), arg], env=e, capture_output=True, text=True, timeout=30)


def test_pinned_literal_and_printf_shape_preserved():
    text = SHIM.read_text()
    assert 'AUTH_CONTEXT_CODE="provider_auth_context_unavailable"' in text
    assert "printf '%s\\n' \"$AUTH_CONTEXT_CODE\"" in text


def test_no_hardcoded_personal_paths():
    text = SHIM.read_text()
    # Host/personal values must be configured, not defaulted into source.
    assert "/bartimaeus/" not in text
    assert "PENTACLE_AUTH_SSM_PARAMETER" in text  # SSM param is configuration


def test_unconfigured_ssm_fails_closed_without_calling_aws(env):
    r = _run(env, {})  # PENTACLE_AUTH_SSM_PARAMETER unset
    assert r.returncode == 78
    assert "provider_auth_context_unavailable: ssm_parameter_unconfigured" in r.stderr
    assert env["marker"].read_bytes() == CONTRACT_BYTES
    assert not env["log"].exists() or "aws" not in env["log"].read_text()
    assert not env["sentinel"].exists()


def test_keychain_unlock_failure_marker(env):
    r = _run(env, {"PENTACLE_AUTH_SSM_PARAMETER": "/synthetic/param",
                   "FAKE_CRED": "s3cr3t", "FAKE_SECURITY_RC": "1"})
    assert r.returncode == 78
    assert "provider_auth_context_unavailable: keychain_unlock_failed" in r.stderr
    assert env["marker"].read_bytes() == CONTRACT_BYTES
    assert "s3cr3t" not in r.stdout and "s3cr3t" not in r.stderr  # no secret emitted
    assert not env["sentinel"].exists()


def test_success_execs_configured_claude_binary(env):
    r = _run(env, {"PENTACLE_AUTH_SSM_PARAMETER": "/synthetic/param", "FAKE_CRED": "s3cr3t"})
    assert r.returncode == 0, r.stderr
    assert env["sentinel"].read_text().strip() == "hi"  # configured claude bin exec'd
    assert "s3cr3t" not in r.stdout and "s3cr3t" not in r.stderr
    assert not env["marker"].exists()  # no failure marker on success
