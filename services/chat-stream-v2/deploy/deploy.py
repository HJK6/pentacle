#!/usr/bin/env python3
from __future__ import annotations

import argparse
from xml.sax.saxutils import escape
import hashlib
import json
import os
import plistlib
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from websockets.sync.client import connect


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RELEASE_CHECKOUT = Path.home() / "repos" / "pentacle-v2"
DEFAULT_STAMP_DIR = ".pentacle-deploy"
DEPLOY_STAMP_PATH_ENV = "PENTACLE_DEPLOY_STAMP_PATH"
DEFAULT_REF = "origin/main"
V2_DAEMON_LABEL = "com.pentacle.chat-streamd-v2"
V2_SERVICE_PATH = "services/chat-stream-v2"
USAGE_COLLECTOR_LABEL = "com.pentacle.usage-state-collector"
USAGE_COLLECTOR_TEMPLATE = Path("services/chat-stream-v2/deploy/com.pentacle.usage-state-collector.plist")
SPAWN_FLEET_SMOKE_TEMPLATE = Path("services/chat-stream-v2/deploy/com.pentacle.spawn-fleet-smoke.plist")
RELEASE_CHECKOUT_TOKEN = b"__PENTACLE_RELEASE_CHECKOUT__"


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    launchd_label: str
    requirements_path: str
    editable_installs: tuple[str, ...]
    log_guard_dir: str | None = None
    launchd_environment: tuple[tuple[str, str], ...] = ()
    runtime_roots: tuple[str, ...] = ()


SERVICES: dict[str, ServiceConfig] = {
    "chat-streamd-v2": ServiceConfig(
        name="chat-streamd-v2",
        launchd_label=V2_DAEMON_LABEL,
        requirements_path="services/chat-stream-v2/requirements.txt",
        editable_installs=("services/agent-orch",),
        log_guard_dir="/Volumes/data",
        launchd_environment=(("PENTACLE_SPAWN_RESERVATION_TTL_S", "600"),),
        runtime_roots=("services/chat-stream-v2", "services/_shared", "services/agent-orch/agent_orch"),
    ),
}


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]
LogOffsets = int | dict[str, int | None]


def _run(cmd: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(cmd), cwd=cwd, text=True, capture_output=True, check=False)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _checked(cmd: Sequence[str], cwd: Path, runner: Runner = _run) -> subprocess.CompletedProcess[str]:
    result = runner(cmd, cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DeployError(f"command failed rc={result.returncode}: {' '.join(cmd)} {detail}".strip())
    return result


def _git(repo: Path, *args: str, runner: Runner = _run) -> str:
    return _checked(("git", *args), repo, runner).stdout.strip()


def _dirty_entries(repo: Path, runner: Runner = _run) -> list[str]:
    result = _checked(("git", "status", "--porcelain"), repo, runner)
    allowed_prefixes = (
        "?? .pentacle-deploy/",
        "?? services/chat-stream-v2/.venv/",
    )
    return [line for line in result.stdout.splitlines() if line and not line.startswith(allowed_prefixes)]


def _attached_branch(repo: Path, runner: Runner = _run) -> str | None:
    result = runner(("git", "symbolic-ref", "--quiet", "--short", "HEAD"), repo)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise DeployError(f"cannot determine release checkout HEAD: {detail}")
    branch = result.stdout.strip()
    return branch or None


def verify_release_checkout(repo: Path, runner: Runner = _run) -> None:
    """Reject a development checkout before a deploy can mutate it."""
    dirty = _dirty_entries(repo, runner)
    if dirty:
        raise DeployError("release checkout is dirty")
    branch = _attached_branch(repo, runner)
    if branch:
        raise DeployError(f"release checkout is branch-attached ({branch})")


_RUNTIME_SUFFIXES = {".json", ".plist", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
_NON_RUNTIME_PARTS = {".venv", "__pycache__", "docs", "harness", "tests"}


def runtime_manifest(repo: Path, service: ServiceConfig, runner: Runner = _run) -> dict[str, object]:
    """Return the file-level bytes that can affect this service at runtime."""
    paths = service.runtime_roots or (V2_SERVICE_PATH,)
    result = _checked(("git", "ls-files", "-z", "--", *paths), repo, runner)
    files: dict[str, str] = {}
    for raw_path in result.stdout.split("\0"):
        if not raw_path:
            continue
        relative = Path(raw_path)
        if relative.suffix not in _RUNTIME_SUFFIXES or any(part in _NON_RUNTIME_PARTS for part in relative.parts):
            continue
        path = repo / relative
        if not path.is_file():
            raise DeployError(f"runtime manifest path is missing: {relative}")
        files[raw_path] = _sha256(path)
    if not files:
        raise DeployError("runtime manifest has no tracked runtime files")
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"schema": "PentacleRuntimeManifestV1", "service": service.name, "files": files, "digest": digest}


def _resolve_sha(repo: Path, ref: str, runner: Runner = _run) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", runner=runner)


def _origin_refs_containing(repo: Path, sha: str, runner: Runner = _run) -> list[str]:
    out = _git(
        repo,
        "for-each-ref",
        "refs/remotes/origin",
        "--contains",
        sha,
        "--format=%(refname:short)",
        runner=runner,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _verify_origin_reachable(repo: Path, sha: str, runner: Runner = _run) -> None:
    refs = _origin_refs_containing(repo, sha, runner)
    if not refs:
        raise DeployError(f"ref {sha} is not reachable from any origin ref")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp_path(repo: Path, service: ServiceConfig) -> Path:
    return repo / DEFAULT_STAMP_DIR / f"{service.name}.json"


def _read_stamp(repo: Path, service: ServiceConfig) -> dict[str, object]:
    path = _stamp_path(repo, service)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_stamp(repo: Path, service: ServiceConfig, stamp: dict[str, object]) -> None:
    path = _stamp_path(repo, service)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class _PreActivationTransaction:
    """Restore the release checkout and deploy stamp until activation is committed."""

    def __init__(self, repo: Path, prior_sha: str, stamp_path: Path, prior_stamp_bytes: bytes | None, runner: Runner) -> None:
        self.repo = repo
        self.prior_sha = prior_sha
        self.stamp_path = stamp_path
        self.prior_stamp_bytes = prior_stamp_bytes
        self.runner = runner
        self.committed = False
        self._rollback_actions: list[Callable[[], None]] = []

    def commit(self) -> None:
        self.committed = True

    def add_rollback(self, action: Callable[[], None]) -> None:
        self._rollback_actions.append(action)

    def _restore(self) -> list[str]:
        failures: list[str] = []
        try:
            _checked(("git", "checkout", "--detach", self.prior_sha), self.repo, self.runner)
            restored_sha = _git(self.repo, "rev-parse", "--verify", "HEAD", runner=self.runner)
            if restored_sha != self.prior_sha:
                raise DeployError(f"restored HEAD is {restored_sha}, expected {self.prior_sha}")
        except Exception as exc:
            failures.append(f"checkout restoration failed: {exc}")
        try:
            if self.prior_stamp_bytes is None:
                self.stamp_path.unlink(missing_ok=True)
                if self.stamp_path.exists():
                    raise DeployError(f"stamp remains at {self.stamp_path}")
            else:
                self.stamp_path.parent.mkdir(parents=True, exist_ok=True)
                self.stamp_path.write_bytes(self.prior_stamp_bytes)
                if self.stamp_path.read_bytes() != self.prior_stamp_bytes:
                    raise DeployError(f"stamp bytes differ at {self.stamp_path}")
        except Exception as exc:
            failures.append(f"stamp restoration failed: {exc}")
        for action in reversed(self._rollback_actions):
            try:
                action()
            except Exception as exc:
                failures.append(f"launchd restoration failed: {exc}")
        return failures

    def __enter__(self) -> "_PreActivationTransaction":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc is None or self.committed:
            return False
        failures = self._restore()
        if failures:
            raise DeployError(f"{exc}; pre-activation restoration failed: {'; '.join(failures)}") from exc
        return False


def _launchd_environment_for_release(repo: Path, service: ServiceConfig) -> tuple[tuple[str, str], ...]:
    environment = dict(service.launchd_environment)
    environment[DEPLOY_STAMP_PATH_ENV] = str(_stamp_path(repo, service))
    return tuple(sorted(environment.items()))


def _assert_deploy_stamp_path(release_stamp_path: Path, launchd_environment: Sequence[tuple[str, str]]) -> None:
    configured_path = dict(launchd_environment).get(DEPLOY_STAMP_PATH_ENV)
    if not configured_path:
        raise DeployError(f"launchd environment is missing {DEPLOY_STAMP_PATH_ENV}")
    release_path = release_stamp_path.resolve()
    if Path(configured_path).expanduser().resolve() != release_path:
        raise DeployError(f"{DEPLOY_STAMP_PATH_ENV} must equal the release stamp path")


def _new_restart_activation(
    *,
    requested_at: str,
    pre_restart_pid: int,
    target_sha: str,
    deployer_stream_id: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "activation_id": str(uuid.uuid4()),
        "requested_at": requested_at,
        "pre_restart_pid": pre_restart_pid,
        "target_sha": target_sha,
        "deployer_stream_id": deployer_stream_id,
    }


GATE_REQUIRED_TIER = "merge"
GATE_REQUIRED_EXECUTABLES = ("git", "tmux")
GATE_SYSTEM_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _effective_home() -> Path:
    """Return the account home from the OS database, never caller-controlled HOME."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _gate_path(repo: Path, service: ServiceConfig | None = None) -> str:
    paths = [str(_venv_python(repo, service).parent), *GATE_SYSTEM_PATHS]
    return os.pathsep.join(dict.fromkeys(paths))


def _gate_environment(repo: Path, service: ServiceConfig | None = None) -> dict[str, str]:
    python = _venv_python(repo, service)
    if not python.is_file() or not os.access(python, os.X_OK):
        raise DeployError(f"gate_environment_interpreter_missing: {python}")
    home = _effective_home()
    path = _gate_path(repo, service)
    missing = [name for name in GATE_REQUIRED_EXECUTABLES if shutil.which(name, path=path) is None]
    if missing:
        raise DeployError(f"gate_environment_executable_missing: {', '.join(missing)} on PATH={path}")
    return {
        "HOME": str(home),
        "PATH": path,
        "PYTHON": str(python),
        "PYTHONNOUSERSITE": "1",
    }


def _gate_command(
    repo: Path, *command: str, service: ServiceConfig | None = None
) -> tuple[str, ...]:
    environment = _gate_environment(repo, service)
    return ("/usr/bin/env", "-i", *(f"{key}={environment[key]}" for key in sorted(environment)), *command)


def _terminal_command_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip()).strip()
    return detail[-1200:]


def _smoke_quota_note(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """The fleet smoke's environmental `UNTESTED` cells, if any, parsed best-effort.

    A codex account over its usage limit is environmental, not a daemon fault:
    the smoke must not call it a pass, and the record names the affected host(s)
    so the deploy operator sees the actionable line. Any parse failure yields no note.
    """
    try:
        payload = json.loads(result.stdout or "{}")
        rows = payload.get("untested") or payload.get("quota_exhausted") or []
        return {"untested": rows} if rows else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def _smoke_payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    try:
        payload = json.loads(result.stdout or "{}")
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def run_gate_and_write_evidence(
    repo: Path,
    service: ServiceConfig,
    tiers: Sequence[str],
    out_path: Path,
    sha: str,
    runner: Runner = _run,
) -> dict[str, object]:
    """Run and validate the canonical v2 merge gate in the release checkout."""
    if tuple(tiers) != (GATE_REQUIRED_TIER,):
        raise DeployError(f"v2 deploy requires exactly the {GATE_REQUIRED_TIER} gate")
    cmd = _gate_command(
        repo,
        str(_venv_python(repo, service)),
        str(repo / V2_SERVICE_PATH / "tools" / "run_gate.py"),
        GATE_REQUIRED_TIER,
        "--evidence-out",
        str(out_path),
        service=service,
    )
    result = runner(tuple(str(part) for part in cmd), repo)
    if result.returncode != 0:
        raise DeployError(f"v2 merge gate failed rc={result.returncode}: {_terminal_command_detail(result)}")
    return _load_gate_evidence(out_path, sha)


def _load_gate_evidence(path: Path, sha: str) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeployError(f"invalid gate evidence: {exc}") from exc
    required = {"schema", "gate", "sha", "source", "tiers", "passed"}
    missing = sorted(required - set(raw))
    if missing:
        raise DeployError(f"gate evidence missing fields: {', '.join(missing)}")
    if raw.get("sha") != sha:
        raise DeployError("gate evidence sha does not match deploy sha")
    if raw.get("schema") != "pentacle.v2.gate-evidence.v1" or raw.get("gate") != GATE_REQUIRED_TIER:
        raise DeployError("gate evidence is not canonical v2 merge evidence")
    if raw.get("passed") is not True:
        raise DeployError("gate evidence is not passing")
    source = raw.get("source")
    if not isinstance(source, dict) or source.get("sha_before") != sha or source.get("sha_after") != sha:
        raise DeployError("gate evidence source SHA does not match deploy SHA")
    if source.get("clean_before") is not True or source.get("clean_after") is not True:
        raise DeployError("gate evidence source was not clean")
    tiers = raw.get("tiers")
    if not isinstance(tiers, list):
        raise DeployError("gate evidence tiers must be a list")
    required_tiers = {"unit", "smoke"}
    passed_tiers = {item.get("tier") for item in tiers if isinstance(item, dict) and item.get("passed") is True}
    if not required_tiers.issubset(passed_tiers):
        raise DeployError("gate evidence is missing a passing v2 unit or smoke tier")
    return raw


def _venv_python(repo: Path, service: ServiceConfig | None = None) -> Path:
    configured_service = service or SERVICES["chat-streamd-v2"]
    service_dir = Path(configured_service.requirements_path).parent
    return repo / service_dir / ".venv" / "bin" / "python"


def verify_venv(repo: Path, service: ServiceConfig, stamp: dict[str, object], runner: Runner = _run) -> str:
    requirements = repo / service.requirements_path
    requirements_hash = _sha256(requirements)
    python = _venv_python(repo, service)
    prior_hash = stamp.get("requirements_hash")
    if python.exists() and prior_hash == requirements_hash:
        return requirements_hash

    if not python.exists():
        _checked((sys.executable, "-m", "venv", str(python.parent.parent)), repo, runner)
    _checked((str(python), "-m", "pip", "install", "-r", str(requirements)), repo, runner)
    for editable in service.editable_installs:
        _checked((str(python), "-m", "pip", "install", "-e", editable), repo, runner)
    return requirements_hash


def _launchctl_target(label: str) -> str:
    return f"gui/{os.getuid()}/{label}"


def _requires_mount(path: Path) -> bool:
    parts = path.expanduser().resolve(strict=False).parts
    return len(parts) == 3 and parts[0] == "/" and parts[1] == "Volumes"


def path_guard_available(path: Path) -> bool:
    if _requires_mount(path):
        return path.is_mount()
    return path.is_dir()


def verify_log_guard(path: str | Path | None) -> None:
    if path is None:
        return
    guard = Path(path).expanduser()
    if not path_guard_available(guard):
        raise DeployError(f"log guard unavailable: {guard}")


def _kickstart(label: str, runner: Runner = _run) -> None:
    _checked(("launchctl", "kickstart", "-k", _launchctl_target(label)), Path.cwd(), runner)


def _launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_plist(label: str) -> dict[str, object]:
    try:
        payload = plistlib.loads(_launchd_plist_path(label).read_bytes())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _launchd_environment(label: str) -> dict[str, str]:
    payload = _launchd_plist(label)
    values = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
    return {str(key): str(value) for key, value in values.items()} if isinstance(values, dict) else {}


def _usage_collector_plist_path() -> Path:
    return _launchd_plist_path(USAGE_COLLECTOR_LABEL)


def _write_bytes(path: Path, contents: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(contents)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_plist(path: Path, payload: dict) -> None:
    _write_bytes(path, plistlib.dumps(payload))


def _v2_usage_probe_rollback(repo: Path, runner: Runner = _run) -> Callable[[], None]:
    daemon_path = _launchd_plist_path(V2_DAEMON_LABEL)
    collector_path = _usage_collector_plist_path()
    daemon_bytes = daemon_path.read_bytes()
    collector_bytes = collector_path.read_bytes() if collector_path.exists() else None

    def restore() -> None:
        _write_bytes(daemon_path, daemon_bytes)
        domain = f"gui/{os.getuid()}"
        runner(("launchctl", "bootout", f"{domain}/{USAGE_COLLECTOR_LABEL}"), repo)
        if collector_bytes is None:
            collector_path.unlink(missing_ok=True)
        else:
            _write_bytes(collector_path, collector_bytes)
            runner(("launchctl", "bootstrap", domain, str(collector_path)), repo)

    return restore


def _render_release_checkout_plist(repo: Path, template_path: Path, description: str) -> bytes:
    """Bind a helper job to the checkout being activated, not a legacy tree."""
    template = repo / template_path
    source = template.read_bytes()
    if RELEASE_CHECKOUT_TOKEN not in source:
        raise DeployError(f"{description} plist is missing release-checkout placeholders")
    return source.replace(RELEASE_CHECKOUT_TOKEN, escape(str(repo)).encode()).replace(
        b"__PENTACLE_USER_HOME__", escape(str(Path.home())).encode()
    )


def _render_v2_usage_collector_plist(repo: Path) -> bytes:
    return _render_release_checkout_plist(repo, USAGE_COLLECTOR_TEMPLATE, "usage collector")


def _render_v2_spawn_fleet_smoke_plist(repo: Path) -> bytes:
    return _render_release_checkout_plist(repo, SPAWN_FLEET_SMOKE_TEMPLATE, "fleet smoke")


def _validate_v2_launchd_program(repo: Path, arguments: Sequence[str]) -> Path:
    if not arguments:
        raise DeployError("v2 daemon plist has empty ProgramArguments")
    raw_program = Path(arguments[0]).expanduser()
    if not raw_program.is_absolute():
        raise DeployError(
            f"v2 daemon ProgramArguments[0] must be absolute and inside the release checkout: {arguments[0]}"
        )
    checkout = Path(os.path.abspath(repo))
    program = Path(os.path.abspath(raw_program))
    if not program.is_relative_to(checkout):
        raise DeployError(
            f"v2 daemon ProgramArguments[0] is outside the release checkout {checkout}: {program}"
        )
    return program


def _ensure_v2_usage_probe_launchd(repo: Path, runner: Runner = _run) -> bool:
    daemon_path = _launchd_plist_path(V2_DAEMON_LABEL)
    daemon = plistlib.loads(daemon_path.read_bytes())
    arguments = daemon.get("ProgramArguments")
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise DeployError("v2 daemon plist has invalid ProgramArguments")
    _validate_v2_launchd_program(repo, arguments)
    filtered: list[str] = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--claude-bin":
            if index + 1 == len(arguments):
                raise DeployError("v2 daemon plist has orphan --claude-bin")
            index += 2
            continue
        filtered.append(arguments[index])
        index += 1
    daemon_changed = filtered != arguments
    if daemon_changed:
        daemon["ProgramArguments"] = filtered
        _write_plist(daemon_path, daemon)

    destination = _usage_collector_plist_path()
    source_bytes = _render_v2_usage_collector_plist(repo)
    collector_changed = not destination.exists() or destination.read_bytes() != source_bytes
    if collector_changed:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(destination, source_bytes)
        domain = f"gui/{os.getuid()}"
        runner(("launchctl", "bootout", f"{domain}/{USAGE_COLLECTOR_LABEL}"), repo)
        runner(("launchctl", "bootstrap", domain, str(destination)), repo)
    return daemon_changed or collector_changed


def _ensure_launchd_environment(
    service: ServiceConfig,
    runner: Runner = _run,
    *,
    launchd_environment: Sequence[tuple[str, str]] | None = None,
) -> bool:
    current = _launchd_environment(service.launchd_label)
    plist_path = _launchd_plist_path(service.launchd_label)
    changed = False
    for key, value in launchd_environment or service.launchd_environment:
        if current.get(key) == value:
            continue
        operation = "-replace" if key in current else "-insert"
        _checked(
            ("plutil", operation, f"EnvironmentVariables.{key}", "-string", value, str(plist_path)),
            REPO_ROOT,
            runner,
        )
        current[key] = value
        changed = True
    return changed


def _reload_launchd(
    label: str,
    runner: Runner = _run,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    target = _launchctl_target(label)
    domain = target.rsplit("/", 1)[0]
    _checked(("launchctl", "bootout", target), Path.cwd(), runner)
    command = ("launchctl", "bootstrap", domain, str(_launchd_plist_path(label)))
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(3):
        result = runner(command, Path.cwd())
        if result.returncode == 0:
            return
        if result.returncode != 5 or attempt == 2:
            break
        sleep(0.25)
    detail = ((result.stderr or result.stdout) if result is not None else "") or ""
    raise DeployError(f"command failed rc={result.returncode if result is not None else '?'}: {' '.join(command)} {detail.strip()}".strip())


def _asset_store_identity(service: ServiceConfig) -> dict[str, object]:
    env = _launchd_environment(service.launchd_label)
    home = env.get("HOME") or str(Path.home())
    raw_path = env.get("PENTACLE_STREAM_ASSETS_DB") or str(Path(home) / ".local/share/pentacle-stream/assets.db")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise DeployError(f"asset store path must be absolute: {raw_path}")
    resolved = path.resolve(strict=False)
    identity: dict[str, object] = {"path": str(resolved), "home": home, "inode": None, "device": None, "schema_version": None}
    try:
        stat = resolved.stat()
        identity["inode"] = stat.st_ino
        identity["device"] = stat.st_dev
        with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as conn:
            identity["schema_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (OSError, sqlite3.Error):
        pass
    return identity


def _daemon_log_path(service: ServiceConfig) -> Path | None:
    raw = _launchd_environment(service.launchd_label).get("PENTACLE_LOG_PATH")
    if raw:
        return Path(raw).expanduser()
    standard_out = _launchd_plist(service.launchd_label).get("StandardOutPath")
    if standard_out is None:
        return None
    if not isinstance(standard_out, str) or not standard_out:
        raise DeployError("launchd StandardOutPath must be a non-empty absolute path")
    path = Path(standard_out).expanduser()
    if not path.is_absolute():
        raise DeployError(f"launchd StandardOutPath must be absolute: {standard_out}")
    return path


def _daemon_log_paths(service: ServiceConfig) -> tuple[Path, ...]:
    """Return the daemon stdout/stderr logs, preserving launchd's configured order."""
    paths: list[Path] = []
    stdout = _daemon_log_path(service)
    if stdout is not None:
        paths.append(stdout)
    standard_error = _launchd_plist(service.launchd_label).get("StandardErrorPath")
    if standard_error is not None:
        if not isinstance(standard_error, str) or not standard_error:
            raise DeployError("launchd StandardErrorPath must be a non-empty absolute path")
        stderr = Path(standard_error).expanduser()
        if not stderr.is_absolute():
            raise DeployError(f"launchd StandardErrorPath must be absolute: {standard_error}")
        if stderr not in paths:
            paths.append(stderr)
    return tuple(paths)


def _capture_log_offsets(paths: Sequence[Path]) -> LogOffsets | None:
    offsets: dict[str, int | None] = {}
    for path in paths:
        try:
            offsets[str(path)] = path.stat().st_size
        except OSError:
            offsets[str(path)] = None
    return offsets or None


BOOT_LINE = "chat-streamd listening on"
V2_BOOT_LINE = "chat_streamd_v2 listening on"
_TRACEBACK_MARKER = "Traceback (most recent call last):"
# Real busy-daemon boots have been measured at 11-18s (asset-store open, 4-host tail
# supervisors, inventory/ring rehydrate). The old 10s deadline sat inside that range and
# reported healthy deploys as failures; 90s is deliberate 5x headroom, and waiting longer
# costs nothing on the success path because the poll returns the moment the line lands.
DEFAULT_BOOT_WAIT_SECONDS = 90.0
RUNTIME_READBACK_INTERVAL_SECONDS = 2.0
RUNTIME_READBACK_DEADLINE_SECONDS = 90.0
_LOG_POLL_SECONDS = 0.2
_LAUNCHD_POLL_SECONDS = 1.0
_MAX_SCAN_BYTES = 4 * 1024 * 1024
_CARRY_CHARS = 256
# Consecutive launchd-answered polls that must agree the booted pid is gone before we call it
# a crash. One is too few: a single scheduling hiccup between kill and respawn would qualify.
_LAUNCHD_LOSS_POLLS = 2
# `launchctl print` status for a service launchd does not know about.
_LAUNCHCTL_NO_SUCH_SERVICE = 113

BOOT_OBSERVED = "observed"
BOOT_NOT_OBSERVED = "not_observed"
BOOT_FAILED = "boot_failed"

# Post-activation outcomes. Once the kickstart fires the bounce has been applied, so these are
# classified on the stamp and mapped to distinct non-refused exit codes in main() — never
# EXIT_REFUSED, which must keep meaning "nothing was kickstarted".
RUNTIME_SHA_OBSERVED = "observed"
RUNTIME_SHA_NOT_CONFIRMED = "sha_not_confirmed"
FLEET_SMOKE_PASSED = "passed"
FLEET_SMOKE_FAILED = "failed"
FLEET_SMOKE_UNTESTED = "untested"
SLOW_CONSUMER_PASSED = "passed"
SLOW_CONSUMER_FAILED = "failed"


_CLIENT_LOG_RE = re.compile(r"\bclient=([^\s,()]+)")


def classify_slow_consumer_log(
    log_text: str,
    *,
    boot_line: str | None = V2_BOOT_LINE,
) -> dict[str, object]:
    """Classify slow-consumer evidence after the current boot marker.

    Queue-depth warnings are telemetry only. Overflow drops and the two close-code
    forms are failures, with the client identity retained for deploy diagnostics.
    """
    boot_marker_seen = boot_line is None
    if boot_line is None:
        window = log_text
    else:
        marker_index = log_text.find(boot_line)
        boot_marker_seen = marker_index >= 0
        window = log_text[marker_index:] if marker_index >= 0 else ""

    queue_depth_warnings = 0
    failures: list[dict[str, str]] = []
    for raw_line in window.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if "slow_consumer queue depth=" in lowered:
            queue_depth_warnings += 1
        kind: str | None = None
        if "slow_consumer overflow" in lowered:
            kind = "overflow"
        elif "1011" in lowered and any(
            marker in lowered for marker in ("slow_consumer", "drop", "close", "closed")
        ):
            kind = "1011_close"
        elif "4000" in lowered and any(
            marker in lowered for marker in ("ws", "websocket", "close", "closed")
        ):
            kind = "4000_close"
        if kind is not None:
            client_match = _CLIENT_LOG_RE.search(line)
            failures.append({
                "kind": kind,
                "client": client_match.group(1) if client_match else "unknown",
                "line": line,
            })

    overflow_drops = sum(row["kind"] == "overflow" for row in failures)
    return {
        "class": "slow_consumer",
        "outcome": SLOW_CONSUMER_FAILED if failures else SLOW_CONSUMER_PASSED,
        "boot_marker_seen": boot_marker_seen,
        "queue_depth_warnings": queue_depth_warnings,
        "overflow_drops": overflow_drops,
        "failures": failures,
    }


@dataclass(frozen=True)
class LaunchdState:
    loaded: bool
    pid: int | None
    # False when `launchctl print` itself failed. That is ambiguous — the job may be gone, or
    # the tool may just have hiccuped — so an unanswered poll is never counted as evidence of
    # a crash mid-wait; only the deadline check acts on it, after a confirming re-sample.
    answered: bool = True


@dataclass(frozen=True)
class BootReadback:
    """Outcome of the post-restart boot-line readback.

    `not_observed` and `boot_failed` are deliberately distinct: the first means the deploy
    landed and the daemon is alive but the log line did not show up in time (retrying would
    double-restart a healthy fabric), the second means the daemon is actually down."""

    outcome: str
    detail: str
    waited_seconds: float
    pid: int | None
    prior_pid: int | None

    def as_stamp(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "waited_seconds": self.waited_seconds,
            "pid": self.pid,
            "prior_pid": self.prior_pid,
        }


_LAUNCHD_PID_RE = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)


def _previous_log_offset(previous_size: LogOffsets | None, path: Path) -> int | None:
    if isinstance(previous_size, dict):
        return previous_size.get(str(path))
    return previous_size


def _launchd_state(label: str, runner: Runner = _run) -> LaunchdState:
    """Ask launchd whether the job is loaded and which pid is running it."""
    result = runner(("launchctl", "print", _launchctl_target(label)), Path.cwd())
    if result.returncode != 0:
        # launchd answers "no such service" with EXIT_NO_SUCH_SERVICE / "Could not find
        # service" — a definite verdict that the job is gone. Any other non-zero status is
        # the tool failing, which says nothing about the daemon and must not be treated as
        # evidence against it.
        text = f"{result.stderr or ''}{result.stdout or ''}".lower()
        definite = result.returncode == _LAUNCHCTL_NO_SUCH_SERVICE or "could not find service" in text
        return LaunchdState(loaded=False, pid=None, answered=definite)
    match = _LAUNCHD_PID_RE.search(result.stdout or "")
    return LaunchdState(loaded=True, pid=int(match.group(1)) if match else None, answered=True)


class _AppendedLogScanner:
    """Stream the bytes appended after `previous_size`, bounded per read.

    Rescanning the whole appended range on every poll would re-read up to `_MAX_SCAN_BYTES`
    hundreds of times across a 90s wait, so each read advances a byte offset and carries a
    small character overlap forward to catch a marker straddling two reads."""

    def __init__(self, path: Path, previous_size: LogOffsets | None) -> None:
        self._path = path
        self._carry = ""
        previous_offset = _previous_log_offset(previous_size, path)
        if previous_offset is not None:
            self._offset = previous_offset
        else:
            try:
                self._offset = max(0, path.stat().st_size - _MAX_SCAN_BYTES)
            except OSError:
                self._offset = 0

    def read_new(self) -> str:
        try:
            size = self._path.stat().st_size
            if size < self._offset:  # rotated or truncated: restart from the top
                self._offset, self._carry = 0, ""
            if size <= self._offset:
                return ""
            with self._path.open("rb") as handle:
                handle.seek(self._offset)
                raw = handle.read(_MAX_SCAN_BYTES)
        except OSError:
            return ""
        self._offset += len(raw)
        text = self._carry + raw.decode("utf-8", errors="replace")
        self._carry = text[-_CARRY_CHARS:]
        return text


def _scan_slow_consumer_window(
    service: ServiceConfig,
    previous_size: LogOffsets | None,
) -> dict[str, object]:
    paths = _daemon_log_paths(service)
    appended: list[tuple[Path, str]] = []
    for path in paths:
        text = _AppendedLogScanner(path, previous_size).read_new()
        if text:
            appended.append((path, text))

    boot_marker_seen = any(V2_BOOT_LINE in text for _path, text in appended)
    scans: list[dict[str, object]] = []
    for _path, text in appended:
        # The daemon's boot line is stdout while logging warnings are stderr. The
        # pre-restart offset excludes old records; stderr has no boot line of its
        # own, so once stdout establishes the window, scan its appended text too.
        scan = classify_slow_consumer_log(
            text,
            boot_line=V2_BOOT_LINE if V2_BOOT_LINE in text else (None if boot_marker_seen else V2_BOOT_LINE),
        )
        scans.append(scan)

    failures = [row for scan in scans for row in scan["failures"]]
    queue_depth_warnings = sum(int(scan["queue_depth_warnings"]) for scan in scans)
    overflow_drops = sum(int(scan["overflow_drops"]) for scan in scans)
    return {
        "class": "slow_consumer",
        "outcome": SLOW_CONSUMER_FAILED if failures else SLOW_CONSUMER_PASSED,
        "boot_marker_seen": boot_marker_seen,
        "queue_depth_warnings": queue_depth_warnings,
        "overflow_drops": overflow_drops,
        "failures": failures,
        "log_paths": [str(path) for path in paths],
    }


def verify_fresh_daemon_log_line(
    service: ServiceConfig,
    *,
    previous_size: LogOffsets | None,
    prior_pid: int | None = None,
    wait_seconds: float = DEFAULT_BOOT_WAIT_SECONDS,
    runner: Runner = _run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BootReadback:
    """Prove that the structured daemon log, not launchd stdout, survived restart.

    Polls to a generous deadline and reports a `BootReadback` instead of collapsing every
    non-success into one error. A slow boot is never mistaken for a dead one: `boot_failed`
    is returned only on positive evidence — during the wait, the replacement process is seen
    to vanish or churn (a crash or a launchd respawn loop); at the deadline, launchd reports
    the job unloaded, with no pid, or still on the pre-kickstart pid. A startup traceback
    only corroborates (it sharpens the message and is confirmed by the process dying):
    healthy daemons log caught tracebacks in normal operation, so a traceback beside a live
    process is not a boot failure."""
    path = _daemon_log_path(service)
    if path is None:
        raise DeployError("daemon log path is not configured in launchd")

    scanner = _AppendedLogScanner(path, previous_size)
    boot_line = V2_BOOT_LINE if service.name == "chat-streamd-v2" else BOOT_LINE
    started = clock()
    deadline = started + wait_seconds
    next_launchd_poll = started
    state = LaunchdState(loaded=True, pid=prior_pid)
    booted_pid: int | None = None
    lost_polls = 0
    polled = False
    traceback_seen = False

    def _readback(outcome: str, detail: str) -> BootReadback:
        if traceback_seen and outcome != BOOT_OBSERVED:
            detail = f"{detail}; startup traceback present in {path}"
        return BootReadback(outcome, detail, round(clock() - started, 1), state.pid, prior_pid)

    while True:
        appended = scanner.read_new()
        if boot_line in appended:
            if not polled or state.pid is None or state.pid == prior_pid:
                # Re-sample whenever the pid we would stamp is stale or absent: on the first
                # pass no poll has run, and on a slow shutdown the last poll can still show
                # the pre-kickstart pid. Stamping that beside `observed` would contradict
                # itself, since an unchanged pid is elsewhere treated as a failed kickstart.
                state = _launchd_state(service.launchd_label, runner)
            return _readback(BOOT_OBSERVED, f"boot line observed in {path}")
        traceback_seen = traceback_seen or _TRACEBACK_MARKER in appended

        now = clock()
        if now >= next_launchd_poll:
            next_launchd_poll = now + _LAUNCHD_POLL_SECONDS
            state = _launchd_state(service.launchd_label, runner)
            polled = True
            if booted_pid is None:
                if state.pid is not None and state.pid != prior_pid:
                    booted_pid = state.pid
            elif not state.answered:
                pass  # tooling hiccup, not evidence either way: leave the counter alone
            elif state.pid == booted_pid:
                lost_polls = 0
            else:
                lost_polls += 1
                if lost_polls >= _LAUNCHD_LOSS_POLLS:
                    return _readback(
                        BOOT_FAILED,
                        f"daemon pid {booted_pid} did not survive boot (launchd now reports {state.pid})",
                    )

        if clock() >= deadline:
            break
        sleep(_LOG_POLL_SECONDS)

    # Only after the full generous wait is a missing or unchanged pid evidence of failure
    # rather than of a slow shutdown/restart still in progress.
    if not state.answered:
        # Never condemn the daemon on a single failed `launchctl print` at the deadline.
        state = _launchd_state(service.launchd_label, runner)
    if not state.answered:
        return _readback(
            BOOT_NOT_OBSERVED,
            f"could not query launchd for {service.launchd_label}, and {boot_line!r} did not "
            f"appear in {path} within {wait_seconds:.0f}s",
        )
    if not state.loaded:
        return _readback(BOOT_FAILED, f"launchd job {service.launchd_label} is not loaded")
    if state.pid is None:
        return _readback(BOOT_FAILED, f"launchd reports no running pid for {service.launchd_label}")
    if prior_pid is not None and state.pid == prior_pid:
        return _readback(BOOT_FAILED, f"kickstart did not replace daemon pid {prior_pid}")
    return _readback(
        BOOT_NOT_OBSERVED,
        f"daemon is running as pid {state.pid} but {boot_line!r} did not appear in {path} "
        f"within {wait_seconds:.0f}s",
    )


def _verify_runtime_sha(
    target_sha: str,
    booted_pid: int | None,
    *,
    deadline_seconds: float = RUNTIME_READBACK_DEADLINE_SECONDS,
    connect_fn: Callable[..., object] = connect,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, dict[str, object]]:
    """Poll the daemon welcome frame until it reports `target_sha` or the deadline expires.

    Returns `(confirmed, last_observed)`. `confirmed` is True only when a welcome frame
    reported `runtime_sha == target_sha` before the deadline. A deadline miss is a classified
    post-activation outcome, never an exception: the kickstart has already fired, so raising
    here would mislabel a live daemon as a refused deploy. Injectable clock/sleep/connect keep
    the loop deterministically testable without a real socket."""
    deadline = clock() + deadline_seconds
    last_observed: dict[str, object] = {"sha": None, "process_state": "not_observed", "pid": booted_pid}
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return False, last_observed
        try:
            with connect_fn(
                "ws://127.0.0.1:7791",
                open_timeout=min(2, remaining),
                close_timeout=0,
            ) as websocket:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise TimeoutError("runtime readback deadline")
                welcome = json.loads(websocket.recv(timeout=min(2, remaining)))
            last_observed = {
                "sha": welcome.get("runtime_sha") if isinstance(welcome, dict) else None,
                "process_state": "running" if booted_pid else "not_running",
                "pid": booted_pid,
            }
        except Exception:
            last_observed = {
                "sha": last_observed.get("sha"),
                "process_state": "running_unreachable" if booted_pid else "not_running",
                "pid": booted_pid,
            }
        remaining = deadline - clock()
        if remaining >= 0 and last_observed["sha"] == target_sha:
            return True, last_observed
        if remaining <= 0:
            return False, last_observed
        sleep(min(RUNTIME_READBACK_INTERVAL_SECONDS, remaining))


def _install_fleet_smoke_schedule(repo: Path, runner: Runner = _run) -> None:
    schedule = Path.home() / "Library/LaunchAgents/com.pentacle.spawn-fleet-smoke.plist"
    schedule.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(schedule, _render_v2_spawn_fleet_smoke_plist(repo))
    domain = f"gui/{os.getuid()}"
    runner(("launchctl", "bootout", f"{domain}/com.pentacle.spawn-fleet-smoke"), repo)
    _checked(("launchctl", "bootstrap", domain, str(schedule)), repo, runner)


def _apply_post_activation(
    service: ServiceConfig,
    repo: Path,
    sha: str,
    stamp: dict[str, object],
    *,
    prior_log_size: LogOffsets | None,
    prior_pid: int | None,
    reload_launchd: bool,
    boot_wait_seconds: float = DEFAULT_BOOT_WAIT_SECONDS,
    runner: Runner = _run,
    verify_boot: Callable[..., BootReadback] = verify_fresh_daemon_log_line,
    verify_runtime: Callable[..., tuple[bool, dict[str, object]]] = _verify_runtime_sha,
) -> None:
    """Restart the daemon and classify the result onto `stamp`. NEVER raises.

    Once the kickstart/reload below fires, the bounce has been applied: the checkout and stamp
    are already committed. Every subsequent failure — the restart itself, either readback, the
    fleet smoke, or the recurring-smoke schedule install — is recorded on the stamp and mapped
    by main() to a distinct do-NOT-retry exit code, never EXIT_REFUSED (which promises "nothing
    was kickstarted") and never a lost stamp. The caller writes the stamp unconditionally after
    this returns. Boot/runtime readbacks are injectable so the classification is unit-testable
    without a real daemon or socket."""
    try:
        if reload_launchd:
            _reload_launchd(service.launchd_label, runner)
        else:
            _kickstart(service.launchd_label, runner)
        readback = verify_boot(
            service,
            previous_size=prior_log_size,
            prior_pid=prior_pid,
            wait_seconds=boot_wait_seconds,
            runner=runner,
        )
        stamp["daemon_boot_readback"] = readback.as_stamp()
        stamp["daemon_log_post_restart_verified"] = readback.outcome == BOOT_OBSERVED
        if service.name == "chat-streamd-v2" and readback.outcome == BOOT_OBSERVED:
            runtime_confirmed, last_observed = verify_runtime(sha, readback.pid)
            stamp["daemon_runtime_readback"] = {
                "target_sha": sha,
                "outcome": RUNTIME_SHA_OBSERVED if runtime_confirmed else RUNTIME_SHA_NOT_CONFIRMED,
                **last_observed,
            }
            if runtime_confirmed:
                smoke = runner(
                    (
                        str(_venv_python(repo, service)),
                        str(repo / "services/chat-stream-v2/tools/spawn_fleet_smoke.py"),
                    ),
                    repo,
                )
                stamp["slow_consumer"] = _scan_slow_consumer_window(service, prior_log_size)
                smoke_payload = _smoke_payload(smoke)
                smoke_untested = smoke_payload.get("untested") or smoke_payload.get("quota_exhausted") or []
                smoke_failures = smoke_payload.get("failures") or []
                if smoke_untested and not smoke_failures:
                    stamp["fleet_smoke"] = {
                        "outcome": FLEET_SMOKE_UNTESTED,
                        "untested": smoke_untested,
                        "detail": _terminal_command_detail(smoke),
                    }
                elif smoke.returncode == 0:
                    stamp["fleet_smoke"] = {"outcome": FLEET_SMOKE_PASSED, **_smoke_quota_note(smoke)}
                    _install_fleet_smoke_schedule(repo, runner)
                else:
                    stamp["fleet_smoke"] = {
                        "outcome": FLEET_SMOKE_FAILED,
                        "detail": _terminal_command_detail(smoke),
                    }
        # Persist the classified record inside the same never-raising boundary. A stamp-write
        # failure after activation must not escape as a refusal either — record it and make one
        # best-effort retry so the error itself is captured; main() still prints the returned
        # stamp to stdout, so the record survives even if the disk write cannot.
        _write_stamp(repo, service, stamp)
    except Exception as exc:  # noqa: BLE001 - post-activation must never surface as a refusal
        stamp["post_activation_error"] = f"{type(exc).__name__}: {exc}"
        try:
            _write_stamp(repo, service, stamp)
        except Exception:  # noqa: BLE001 - already degraded; the returned stamp carries the record
            pass


def _coordination_resource(service: ServiceConfig) -> str:
    return f"daemon:{service.launchd_label}"


def _resolve_agent_orch(repo: Path, service: ServiceConfig | None = None) -> str:
    override = os.environ.get("AGENT_ORCH_BIN")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        raise DeployError(f"deploy_environment_agent_orch_override_invalid: {candidate}")
    pinned = shutil.which("agent-orch", path=_gate_path(repo, service))
    if pinned:
        return str(Path(pinned).resolve())
    home = _effective_home()
    fallbacks = (
        home / ".local" / "bin" / "agent-orch",
        home / "Library" / "Python" / "3.13" / "bin" / "agent-orch",
        Path("/opt/homebrew/bin/agent-orch"),
        Path("/usr/local/bin/agent-orch"),
    )
    for candidate in fallbacks:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise DeployError("deploy_environment_agent_orch_unresolved: checked pinned PATH and known install locations")


def verify_deploy_guard(
    service: ServiceConfig,
    deployer_stream_id: str,
    runner: Runner = _run,
    *,
    repo: Path = REPO_ROOT,
    sha: str,
    sessions_db: Path | None = None,
    legacy_schedules_db: Path | None = None,
) -> dict[str, object]:
    """Sole-deployer + schedule-quiescence guard for a daemon bounce.

    Replaces the retired coordination-window lease gate. A session row carries no
    service identity, so the guard cannot bind a deployer to one service; it
    instead serializes every deployer, refusing to proceed while any other open
    `role=deployer` session exists. It records the target checkout SHA and the
    pre-restart launchd pid as activation evidence, and refuses if any nonterminal
    scheduled spawn (v2 or legacy) could race the restart.
    """
    resource = _coordination_resource(service)
    if not deployer_stream_id:
        raise DeployError("restart activation requires a deployer stream id")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise DeployError("restart activation requires a full lowercase target SHA")
    agent_orch = _resolve_agent_orch(repo, service)
    listing = _checked((agent_orch, "list"), Path.cwd(), runner)
    try:
        rows = json.loads(listing.stdout or "[]")
    except ValueError as exc:
        raise DeployError("invalid agent-orch list response") from exc
    if not isinstance(rows, list):
        raise DeployError("invalid agent-orch list response")
    other_deployers = sorted(
        str(row.get("stream_id") or "")
        for row in rows
        if isinstance(row, dict)
        and row.get("role") == "deployer"
        and str(row.get("stream_id") or "") != deployer_stream_id
    )
    if other_deployers:
        raise DeployError(
            "daemon deploys serialize on a single deployer; other open deployer "
            f"session(s) present: {', '.join(other_deployers)}"
        )
    configured_db = sessions_db
    if (
        configured_db is None
        and service.launchd_label == V2_DAEMON_LABEL
        and os.environ.get("PENTACLE_STREAM_SESSIONS_DB")
    ):
        configured_db = Path(os.environ["PENTACLE_STREAM_SESSIONS_DB"])
    if configured_db is not None:
        try:
            with sqlite3.connect(f"file:{configured_db.resolve()}?mode=ro", uri=True) as conn:
                nonterminal = int(conn.execute(
                    "SELECT count(*) FROM v2_schedules WHERE state NOT IN "
                    "('fired','cancelled','failed','indeterminate','expired')"
                ).fetchone()[0])
        except (OSError, sqlite3.Error) as exc:
            raise DeployError("v2 schedule quiescence readback failed") from exc
        if nonterminal:
            raise DeployError(
                f"daemon deploy requires zero nonterminal v2 schedules; found {nonterminal}"
            )
    legacy_db = legacy_schedules_db
    if legacy_db is None:
        legacy_db = Path(os.environ.get(
            "PENTACLE_STREAM_SCHEDULES_DB",
            str(Path.home() / ".local/share/pentacle-stream/schedules.db"),
        ))
    try:
        if legacy_db.exists():
            with sqlite3.connect(f"file:{legacy_db.resolve()}?mode=ro", uri=True) as legacy_conn:
                legacy_count = int(legacy_conn.execute(
                    "SELECT count(*) FROM schedules "
                    "WHERE state IN ('pending','pending_retry','fired_in_progress')"
                ).fetchone()[0])
        else:
            legacy_count = 0
    except (OSError, sqlite3.Error) as exc:
        raise DeployError("legacy schedules.db readback failed") from exc
    if legacy_count:
        raise DeployError(
            f"daemon deploy requires zero nonterminal legacy schedules; found {legacy_count}"
        )
    pre_restart_pid = _launchd_state(service.launchd_label, runner).pid
    if type(pre_restart_pid) is not int or pre_restart_pid <= 0:
        raise DeployError("restart activation requires a positive launchd PID")
    return {
        "resource": resource,
        "deployer_stream_id": deployer_stream_id,
        "sole_deployer": True,
        "checkout_sha": sha,
        "pre_restart_pid": pre_restart_pid,
    }


def deploy(
    service: ServiceConfig,
    *,
    release_checkout: Path,
    ref: str,
    rollback: bool = False,
    gate_evidence: Path | None = None,
    deployer_stream_id: str | None = None,
    skip_kickstart: bool = False,
    log_guard_dir: str | Path | None = None,
    boot_wait_seconds: float = DEFAULT_BOOT_WAIT_SECONDS,
    runner: Runner = _run,
) -> dict[str, object]:
    repo = release_checkout.expanduser().resolve()
    if not (repo / ".git").exists():
        raise DeployError(f"release checkout is not a git checkout: {repo}")
    verify_release_checkout(repo, runner)
    _checked(("git", "fetch", "--prune", "origin"), repo, runner)
    prior_sha = _git(repo, "rev-parse", "--verify", "HEAD", runner=runner)
    prior_stamp = _read_stamp(repo, service)
    stamp_path = _stamp_path(repo, service)
    prior_stamp_bytes = stamp_path.read_bytes() if stamp_path.exists() else None
    deploy_ref = ref
    if rollback:
        prior = str(prior_stamp.get("prior_sha") or "")
        if not prior:
            raise DeployError("rollback requested but prior_sha is not stamped")
        deploy_ref = prior
    sha = _resolve_sha(repo, deploy_ref, runner)
    _verify_origin_reachable(repo, sha, runner)

    with _PreActivationTransaction(repo, prior_sha, stamp_path, prior_stamp_bytes, runner) as transaction:
        _checked(("git", "checkout", "--detach", sha), repo, runner)
        verify_log_guard(log_guard_dir if log_guard_dir is not None else service.log_guard_dir)
        requirements_hash = verify_venv(repo, service, prior_stamp, runner)
        if gate_evidence is not None:
            evidence = _load_gate_evidence(gate_evidence, sha)
            evidence_source = "file"
        else:
            evidence = run_gate_and_write_evidence(
                repo, service, (GATE_REQUIRED_TIER,), repo / DEFAULT_STAMP_DIR / f"{service.name}.gate.json", sha, runner
            )
            evidence = _load_gate_evidence(
                repo / DEFAULT_STAMP_DIR / f"{service.name}.gate.json", sha
            )
            evidence_source = "inline"
        gates = {
            "gate": evidence.get("gate"),
            "tiers": list(evidence.get("tiers") or []),
            "generated_at": evidence.get("generated_at"),
            "evidence_source": evidence_source,
        }

        log_paths = _daemon_log_paths(service)
        log_path = log_paths[0] if log_paths else None
        if log_path is None and not skip_kickstart:
            # Refuse now rather than from inside the readback: exit 2 promises "nothing was
            # kickstarted", and raising this after the restart would make that promise false.
            raise DeployError("daemon log path is not configured in launchd")
        prior_log_size = _capture_log_offsets(log_paths) if log_paths else None
        stamp = {
            "service": service.name,
            "ref": deploy_ref,
            "sha": sha,
            "prior_sha": prior_sha,
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "deployer_stream_id": deployer_stream_id or os.environ.get("AGENT_ORCH_STREAM_ID") or "",
            "requirements_hash": requirements_hash,
            "asset_store": _asset_store_identity(service),
            "gates": gates,
        }
        activation_runtime_manifest = runtime_manifest(repo, service, runner)
        stamp["activation_runtime_manifest"] = activation_runtime_manifest
        if not skip_kickstart:
            stamp["restart_guard"] = verify_deploy_guard(
                service, str(stamp["deployer_stream_id"]), runner, repo=repo, sha=sha
            )
        activation_launchd_environment = service.launchd_environment
        if not skip_kickstart:
            activation_launchd_environment = _launchd_environment_for_release(repo, service)
        launchd_environment_changed = False
        usage_probe_launchd_changed = False
        if not skip_kickstart:
            if service.name == "chat-streamd-v2":
                daemon_arguments = _launchd_plist(service.launchd_label).get("ProgramArguments")
                if not isinstance(daemon_arguments, list) or not all(
                    isinstance(value, str) for value in daemon_arguments
                ):
                    raise DeployError("v2 daemon plist has invalid ProgramArguments")
                _validate_v2_launchd_program(repo, daemon_arguments)
            base_environment_changed = _ensure_launchd_environment(service, runner)
            stamp_environment_changed = _ensure_launchd_environment(
                service,
                runner,
                launchd_environment=activation_launchd_environment,
            )
            launchd_environment_changed = base_environment_changed or stamp_environment_changed
            if service.name == "chat-streamd-v2":
                transaction.add_rollback(_v2_usage_probe_rollback(repo, runner))
                usage_probe_launchd_changed = _ensure_v2_usage_probe_launchd(repo, runner)
        stamp["launchd_environment"] = dict(activation_launchd_environment)
        stamp["launchd_environment_changed"] = launchd_environment_changed
        stamp["usage_probe_launchd_changed"] = usage_probe_launchd_changed
        if not skip_kickstart:
            _assert_deploy_stamp_path(stamp_path, activation_launchd_environment)
            restart_guard = stamp.get("restart_guard")
            if not isinstance(restart_guard, dict):
                raise DeployError("restart activation requires verified sole-deployer guard evidence")
            deployer_stream_id = stamp.get("deployer_stream_id")
            if not (isinstance(deployer_stream_id, str) and deployer_stream_id):
                raise DeployError("restart activation requires a non-empty deployer stream id")
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise DeployError("restart activation requires a full lowercase target SHA")
            prior_pid = _launchd_state(service.launchd_label, runner).pid
            if type(prior_pid) is not int or prior_pid <= 0:
                raise DeployError("restart activation requires a positive launchd PID")
            requested_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            stamp["restart_activation"] = _new_restart_activation(
                requested_at=requested_at,
                pre_restart_pid=prior_pid,
                target_sha=sha,
                deployer_stream_id=deployer_stream_id,
            )
        _write_stamp(repo, service, stamp)
        transaction.commit()
    if not skip_kickstart:
        # `_apply_post_activation` restarts the daemon, classifies the result onto the stamp, and
        # persists it — all inside one never-raising boundary. Once the kickstart fires the bounce
        # is applied, and EXIT_REFUSED (2) must keep meaning "nothing was kickstarted". NOTHING
        # here re-raises, so a post-activation failure (restart, readback, smoke, schedule, or the
        # stamp write itself) can never both mislabel a live daemon as refused AND lose the record
        # — the 2026-09-05 laneM/laneN bounces did exactly that on a workstation fleet-smoke miss whose
        # _checked() raised before the stamp write.
        _apply_post_activation(
            service,
            repo,
            sha,
            stamp,
            prior_log_size=prior_log_size,
            prior_pid=prior_pid,
            reload_launchd=launchd_environment_changed or usage_probe_launchd_changed,
            boot_wait_seconds=boot_wait_seconds,
            runner=runner,
        )
    return stamp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy a Pentacle service from an origin-backed release checkout.")
    parser.add_argument("service", choices=sorted(SERVICES))
    parser.add_argument("--release-checkout", type=Path, default=DEFAULT_RELEASE_CHECKOUT)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--gate-evidence", type=Path)
    parser.add_argument("--deployer-stream-id", default=None)
    parser.add_argument("--log-guard-dir", default=None, help="Override the service log guard directory checked before deploy.")
    parser.add_argument("--skip-kickstart", action="store_true", help="Test/dev only: do not kick launchd after stamping.")
    parser.add_argument(
        "--boot-wait-seconds",
        type=float,
        default=DEFAULT_BOOT_WAIT_SECONDS,
        help=f"How long to poll for the post-restart boot line (default {DEFAULT_BOOT_WAIT_SECONDS:.0f}s).",
    )
    return parser


# Exit codes. EXIT_REFUSED (2) means the deploy was refused BEFORE the kickstart — nothing was
# restarted, so a retry is safe. Every code >2 means the bounce was APPLIED and then a
# post-activation check did not confirm health: 3/4 for the boot-line readback ("not shown up
# yet" vs "daemon is down" — opposite operator responses, collapsing them into 2 invited a
# retry/force double-restart of a healthy fabric, 2026-07-26 windows 9127fa2, fe7e7e7), 5 for a
# runtime-SHA readback that never confirmed the target, 6 for a post-boot fleet-smoke miss (the
# 2026-09-05 laneM/laneN bounces, a workstation peer), 7 for any other post-activation error (the
# restart or schedule install itself raised). None of 3-7 is a refusal: do NOT retry/force.
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_BOOT_LINE_NOT_OBSERVED = 3
EXIT_DAEMON_BOOT_FAILED = 4
EXIT_RUNTIME_SHA_NOT_CONFIRMED = 5
EXIT_FLEET_SMOKE_FAILED = 6
EXIT_POST_ACTIVATION_ERROR = 7
EXIT_SLOW_CONSUMER_FAILED = 8
EXIT_FLEET_SMOKE_UNTESTED = 9


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        stamp = deploy(
            SERVICES[args.service],
            release_checkout=args.release_checkout,
            ref=args.ref,
            rollback=args.rollback,
            gate_evidence=args.gate_evidence,
            deployer_stream_id=args.deployer_stream_id,
            skip_kickstart=args.skip_kickstart,
            log_guard_dir=args.log_guard_dir,
            boot_wait_seconds=args.boot_wait_seconds,
        )
    except DeployError as exc:
        print(f"deploy refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    # Print the stamp on every post-kickstart outcome: the deploy record is what an operator
    # needs most in exactly the cases that are not a clean success.
    print(json.dumps(stamp, sort_keys=True))
    readback = stamp.get("daemon_boot_readback") or {}
    outcome = readback.get("outcome")
    detail = readback.get("detail") or ""
    if outcome == BOOT_FAILED:
        print(f"daemon failed to boot after deploy: {detail}", file=sys.stderr)
        print("The stamp landed but the service is NOT healthy: investigate, or redeploy with --rollback.", file=sys.stderr)
        return EXIT_DAEMON_BOOT_FAILED
    if outcome == BOOT_NOT_OBSERVED:
        print(f"deploy applied; boot-line readback inconclusive: {detail}", file=sys.stderr)
        print("This is NOT a failed deploy. Do NOT retry or force - that double-restarts a live daemon.", file=sys.stderr)
        print(f"Confirm with: launchctl print gui/$(id -u)/{V2_DAEMON_LABEL} | grep -E 'pid|state'", file=sys.stderr)
        return EXIT_BOOT_LINE_NOT_OBSERVED
    post_activation_error = stamp.get("post_activation_error")
    if post_activation_error:
        print(f"deploy applied; error after the restart was attempted: {post_activation_error}", file=sys.stderr)
        print("This is NOT a refused deploy - the checkout and stamp were committed and the daemon may have restarted.", file=sys.stderr)
        print(f"Verify health, roll back with --rollback if it is down: launchctl print gui/$(id -u)/{V2_DAEMON_LABEL} | grep -E 'pid|state'", file=sys.stderr)
        return EXIT_POST_ACTIVATION_ERROR
    runtime = stamp.get("daemon_runtime_readback") or {}
    if runtime.get("outcome") == RUNTIME_SHA_NOT_CONFIRMED:
        print(
            f"deploy applied; daemon runtime SHA not confirmed within "
            f"{RUNTIME_READBACK_DEADLINE_SECONDS:.0f}s: {json.dumps(runtime, sort_keys=True)}",
            file=sys.stderr,
        )
        print("This is NOT a refused deploy. Do NOT retry or force - the daemon was already restarted.", file=sys.stderr)
        print(f"Confirm with: launchctl print gui/$(id -u)/{V2_DAEMON_LABEL} | grep -E 'pid|state'", file=sys.stderr)
        return EXIT_RUNTIME_SHA_NOT_CONFIRMED
    slow_consumer = stamp.get("slow_consumer") or {}
    if slow_consumer.get("outcome") == SLOW_CONSUMER_FAILED:
        failures = slow_consumer.get("failures") or []
        clients = sorted({
            str(row.get("client") or "unknown")
            for row in failures
            if isinstance(row, dict)
        })
        client_detail = ", ".join(clients) if clients else "unknown"
        print(
            f"deploy applied; post-boot slow_consumer failure: client={client_detail}; "
            f"{json.dumps(failures, sort_keys=True)}",
            file=sys.stderr,
        )
        print("The daemon is on the target SHA; investigate the slow consumer and do NOT retry the deploy.", file=sys.stderr)
        return EXIT_SLOW_CONSUMER_FAILED
    if slow_consumer:
        print(
            "deploy OK; post-boot slow-consumer scan: "
            f"{slow_consumer.get('overflow_drops', 0)} overflow drops; "
            f"{slow_consumer.get('queue_depth_warnings', 0)} queue-depth warnings.",
            file=sys.stderr,
        )
    smoke = stamp.get("fleet_smoke") or {}
    if smoke.get("outcome") == FLEET_SMOKE_UNTESTED:
        print(
            "deploy applied; post-boot fleet smoke is UNTESTED: "
            f"{json.dumps(smoke.get('untested') or [], sort_keys=True)}; "
            "quota/unavailable cells are neither a pass nor a daemon regression.",
            file=sys.stderr,
        )
        return EXIT_FLEET_SMOKE_UNTESTED
    if smoke.get("outcome") == FLEET_SMOKE_FAILED:
        print(f"deploy applied; post-boot fleet smoke failed: {smoke.get('detail') or ''}", file=sys.stderr)
        print("This is NOT a refused deploy. The daemon is on the target SHA; investigate the failing host(s), do NOT retry the deploy.", file=sys.stderr)
        return EXIT_FLEET_SMOKE_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
