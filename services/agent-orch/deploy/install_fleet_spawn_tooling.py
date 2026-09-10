#!/usr/bin/env python3
"""Install and atomically activate a content-verified immutable release.

This command intentionally has no "best effort" path: releases are staged on
every target and hash-verified before any active pointer moves.  The old active
pointer is retained as ``previous`` and each switch, rollback, and resume is
read back from the target before the command reports success.
"""
from __future__ import annotations

import argparse
import ast
import errno
import fcntl
import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


HOSTS = {
    "local": ("localhost", str(Path.home() / ".local/share/pentacle/releases")),
}
PINNED = {"claude": "2.1.207", "codex": "0.144.1"}
SHA = re.compile(r"[0-9a-f]{40}\Z")
STAMP = re.compile(r"[0-9a-z][0-9a-z._-]{7,127}\Z")
DEFAULT_SFTP_TIMEOUT_SECONDS = 120
RUNTIME_SELECTOR_PATH = "services/agent-orch/deploy/runtime_python.py"
_PROGRESS_STREAM = sys.stderr
_SHA256_FILE_SCRIPT = 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())'
_VERIFY_MANIFEST_IDENTITY_SCRIPT = 'import json,sys; x=json.load(open(sys.argv[1])); raise SystemExit(0 if x["commit"] == sys.argv[2] and x["archive_sha256"] == sys.argv[3] else 1)'
_VERIFY_MANIFEST_SCRIPT = 'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1])) == json.loads(sys.argv[2]) else 1)'
_VERIFY_MANIFEST_CATALOG_SCRIPT = 'import json,sys; x=json.load(open(sys.argv[1])); raise SystemExit(0 if x["catalog_version"] == sys.argv[2] and x["provider_packages"] else 1)'
_VERIFY_RUNTIME_MANIFEST_SCRIPT = 'import json,sys; x=json.load(open(sys.argv[1])); raise SystemExit(0 if x.get("runtime_package") == json.loads(sys.argv[2]) else 1)'


def _progress(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=_PROGRESS_STREAM, flush=True)


class RolloutLock:
    """Serialize release mutations and retain the active stamp in the lock file."""

    def __init__(self, path: Path, stamp: str) -> None:
        self.path = Path(path)
        self.stamp = stamp
        self._handle = None

    def acquire(self) -> "RolloutLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno != errno.EAGAIN:
                handle.close()
                raise
            handle.seek(0)
            running_stamp = handle.read().strip() or "unknown"
            handle.close()
            raise RuntimeError(f"rollout_in_progress:{running_stamp}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(self.stamp + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "RolloutLock":
        return self.acquire()

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.release()


@dataclass(frozen=True)
class Target:
    name: str
    ssh: str
    root: str
    local: bool = False
    loopback: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _archive_tree_sha256(archive: Path, prefix: str) -> str:
    """Hash the release-owned runtime source tree independently of its tar metadata."""
    digest = hashlib.sha256()
    with tarfile.open(archive) as bundle:
        members = sorted(
            (
                member
                for member in bundle.getmembers()
                if member.isfile()
                and member.name.startswith(prefix + "/")
                and "__pycache__" not in Path(member.name).parts
            ),
            key=lambda member: member.name,
        )
        if not members:
            raise ValueError("runtime_package_missing")
        for member in members:
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError("runtime_package_missing")
            relative = member.name.removeprefix(prefix + "/")
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(source.read())
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted((path for path in root.rglob("*") if path.is_file() and "__pycache__" not in path.parts), key=lambda path: path.relative_to(root).as_posix())
    if not files:
        raise ValueError("runtime_dependency_missing")
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _runtime_dependency_bundle(destination: Path) -> tuple[Path, dict[str, str]]:
    try:
        distribution = importlib.metadata.distribution("websockets")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ValueError("runtime_dependency_missing") from exc
    source_root = Path(distribution.locate_file("")).resolve()
    package = source_root / "websockets"
    dist_info = sorted(source_root.glob("websockets-*.dist-info"))
    if not package.is_dir() or len(dist_info) != 1:
        raise ValueError("runtime_dependency_missing")
    bundle = destination / "runtime-deps" / "websockets"
    shutil.copytree(package, bundle / "websockets")
    shutil.copytree(dist_info[0], bundle / dist_info[0].name)
    return bundle, {"version": distribution.version, "sha256": _tree_sha256(bundle)}


def _archive_member_sha256(archive: Path, path: str) -> str:
    with tarfile.open(archive) as bundle:
        member = bundle.getmember(path)
        source = bundle.extractfile(member)
        if source is None:
            raise ValueError("runtime_support_module_missing")
        return _sha256_bytes(source.read())


def _agent_orch_requires_python(repo: Path, commit: str) -> str:
    path = "services/agent-orch/pyproject.toml"
    try:
        project = tomllib.loads(
            subprocess.check_output(["git", "-C", str(repo), "show", f"{commit}:{path}"], text=True)
        )
    except (subprocess.CalledProcessError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("runtime_requires_python_missing") from exc
    requirement = project.get("project", {}).get("requires-python")
    if not isinstance(requirement, str) or not requirement.strip():
        raise ValueError("runtime_requires_python_missing")
    return requirement


def _release_contract(repo: Path, commit: str) -> tuple[bytes, str]:
    path = "services/_shared/spawn_profiles.py"
    value = subprocess.check_output(["git", "-C", str(repo), "show", f"{commit}:{path}"])
    tree = ast.parse(value, filename=path)
    catalog = None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "CATALOG_VERSION":
            catalog = ast.literal_eval(node.value)
            break
    if not isinstance(catalog, str) or not catalog.startswith("spawn-catalog-"):
        raise ValueError("spawn_profile_catalog_missing")
    return value, catalog


def _archive(repo: Path, commit: str, destination: Path, stamp: str | None = None) -> dict[str, object]:
    canonical = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", f"{commit}^{{commit}}"], text=True
    ).strip()
    if canonical != commit:
        raise ValueError("commit_not_canonical")
    archive = destination / "pentacle.tar"
    with archive.open("wb") as handle:
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], check=True, stdout=handle)
    with tarfile.open(archive) as bundle:
        if not bundle.getmembers():
            raise ValueError("archive_empty")
    contract, catalog = _release_contract(repo, commit)
    runtime_package = "services/agent-orch/agent_orch"
    runtime_support_module = "services/_shared/asset_schema.py"
    stamp = stamp or f"{commit[:12]}-{_sha256_bytes(contract)[:12]}"
    manifest: dict[str, object] = {
        "schema": "PentacleReleaseManifestV2",
        "commit": commit,
        "stamp": stamp,
        "archive_sha256": "",
        "contract": {"path": "services/_shared/spawn_profiles.py", "sha256": _sha256_bytes(contract)},
        "catalog_version": catalog,
        "platforms": ["darwin", "linux"],
        "provider_packages": {name: {"minimum_version": version, "binary": name} for name, version in PINNED.items()},
    }
    try:
        runtime_tree = _archive_tree_sha256(archive, runtime_package)
        support_sha256 = _archive_member_sha256(archive, runtime_support_module)
        selector_sha256 = _archive_member_sha256(archive, RUNTIME_SELECTOR_PATH)
        requires_python = _agent_orch_requires_python(repo, commit)
        dependency_bundle, dependency = _runtime_dependency_bundle(destination)
        with tarfile.open(archive, "a") as bundle:
            bundle.add(dependency_bundle.parent, arcname="runtime-deps")
        manifest["runtime_package"] = {
            "path": runtime_package,
            "sha256": runtime_tree,
            "requires_python": requires_python,
            "interpreter_selector": {"path": RUNTIME_SELECTOR_PATH, "sha256": selector_sha256},
            "asset_schema": {"path": runtime_support_module, "sha256": support_sha256},
            "dependencies": {"websockets": dependency},
        }
    except ValueError:
        pass
    manifest["archive_sha256"] = _sha256(archive)
    (destination / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def _remote_argv(target: Target, command: str) -> list[str]:
    if target.local and not target.loopback:
        return ["sh", "-lc", command]
    return ["ssh", "-o", "BatchMode=yes", target.ssh, f"sh -lc {shlex.quote(command)}"]


def _remote(target: Target, command: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> str:
    argv = _remote_argv(target, command)
    if dry_run:
        return ""
    result = runner(argv, text=True, capture_output=True, check=False)
    if getattr(result, "returncode", 1) != 0:
        raise subprocess.CalledProcessError(getattr(result, "returncode", 1), argv, output=getattr(result, "stdout", ""), stderr=getattr(result, "stderr", ""))
    return str(getattr(result, "stdout", "")).strip()


def _remote_quote(value: str) -> str:
    return shlex.quote(value)


def _scp_argv(target: Target, bundle: Path, staging: str) -> list[str]:
    return [
        "scp",
        str(bundle / "pentacle.tar"),
        str(bundle / "manifest.json"),
        f"{target.ssh}:{staging}/",
    ]


def _release(target: Target, commit: str) -> str:
    return f"{target.root}/{commit}"


def _stable_launcher(target: Target) -> str:
    return str(Path(target.root).parents[2] / "bin" / "agent-orch")


def _path_launchers(target: Target) -> list[str]:
    launchers = [_stable_launcher(target)]
    if target.root.startswith("/Users/"):
        launchers.append("/opt/homebrew/bin/agent-orch")
    return launchers


def _runtime_launcher(release: str) -> str:
    return release + "/runtime/bin/agent-orch"


def _read_pointer(target: Target, pointer: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> str | None:
    value = _remote(target, f"readlink {_remote_quote(target.root + '/' + pointer)} 2>/dev/null || true", dry_run=dry_run, runner=runner)
    return value or None


def _remote_realpath(target: Target, path: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> str:
    script = "import os,sys; print(os.path.realpath(sys.argv[1]))"
    return _remote(target, f"python3 -c {_remote_quote(script)} {_remote_quote(path)}", dry_run=dry_run, runner=runner)


def _replace_pointer_command(target: Target, pointer: str, value: str) -> str:
    return _replace_link_command(target.root + "/" + pointer, value)


def _replace_link_command(path: str, value: str) -> str:
    staged = path + ".new"
    return " && ".join((
        f"ln -sfn {_remote_quote(value)} {_remote_quote(staged)}",
        f"case \"$(uname -s)\" in Darwin) mv -f -h {_remote_quote(staged)} {_remote_quote(path)} ;; *) mv -f -T {_remote_quote(staged)} {_remote_quote(path)} ;; esac",
    ))


def _rollout_state_paths(target: Target, stamp: str) -> tuple[str, str, str]:
    if not STAMP.fullmatch(stamp):
        raise ValueError("release_stamp_invalid")
    rollouts = target.root + "/rollouts"
    legacy_root = str(Path(target.root).parents[1] / "agent-orch-releases")
    return (
        rollouts + "/" + stamp + ".launcher-state.json",
        legacy_root,
        rollouts + "/" + stamp + ".agent-orch-releases",
    )


def _launcher_state_command(target: Target, stamp: str, *, migrate: bool) -> str:
    state_path, legacy_root, legacy_backup = _rollout_state_paths(target, stamp)
    script = """import hashlib, json, os, pathlib, shutil, site, stat, sys
root = pathlib.Path(sys.argv[1])
launcher_paths = [pathlib.Path(value) for value in json.loads(sys.argv[2])]
state_path, legacy_root, legacy_backup = [pathlib.Path(value) for value in sys.argv[3:6]]
migrate = sys.argv[6] == '1'
state_path.parent.mkdir(parents=True, exist_ok=True)
def lexists(path):
    return os.path.lexists(path)
def write_state(value):
    staged = state_path.with_name(state_path.name + '.new')
    staged.write_text(json.dumps(value, sort_keys=True), encoding='utf-8')
    os.replace(staged, state_path)
def residue_pth_files():
    pth_files = set()
    for directory in site.getsitepackages() + [site.getusersitepackages()]:
        path = pathlib.Path(directory)
        if path.is_dir():
            pth_files.update(path.glob('*.pth'))
    user_base = os.environ.get('PYTHONUSERBASE')
    if user_base:
        base = pathlib.Path(user_base)
        if base.is_dir():
            pth_files.update(base.rglob('*.pth'))
    home = root.parents[3]
    for directory in list((home / 'Library' / 'Python').glob('*/lib/python/site-packages')) + list((home / '.local' / 'lib').glob('python*/site-packages')):
        if directory.is_dir():
            pth_files.update(directory.glob('*.pth'))
    for path in sorted(pth_files):
        name = path.name.lower()
        text = path.read_text(encoding='utf-8', errors='replace')
        named_residue = any(marker in name for marker in ('agent_orch', 'agent-orch', 'pentacle'))
        path_residue = any(marker in text.replace('\\\\', '/') for marker in ('agent-orch-releases', '/pentacle/services/agent-orch', '/deploy/pentacle'))
        if named_residue or path_residue:
            yield path
def pth_backup(path):
    digest = hashlib.sha256(os.fsencode(str(path))).hexdigest()
    return state_path.with_name(state_path.name + '.pth.' + digest)
if lexists(state_path):
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SystemExit('launcher_state_invalid') from exc
    if not isinstance(state, dict) or state.get('schema') != 'PentacleLauncherStateV2':
        raise SystemExit('launcher_state_invalid')
else:
    launchers = []
    for launcher in launcher_paths:
        if os.path.islink(launcher):
            launcher_state = {'kind': 'symlink', 'target': os.readlink(launcher)}
        elif lexists(launcher):
            if not launcher.is_file():
                raise SystemExit('launcher_state_unsupported')
            digest = hashlib.sha256(os.fsencode(str(launcher))).hexdigest()
            backup = state_path.with_name(state_path.name + '.launcher.' + digest)
            if lexists(backup):
                raise SystemExit('launcher_state_backup_exists')
            shutil.copyfile(launcher, backup)
            shutil.copystat(launcher, backup)
            launcher_state = {'kind': 'file', 'backup': str(backup), 'mode': stat.S_IMODE(launcher.stat().st_mode)}
        else:
            launcher_state = {'kind': 'absent'}
        launchers.append({'path': str(launcher), 'state': launcher_state})
    pth_files = []
    for path in residue_pth_files():
        backup = pth_backup(path)
        if lexists(backup):
            raise SystemExit('launcher_state_pth_backup_exists')
        shutil.copy2(path, backup)
        pth_files.append({'path': str(path), 'backup': str(backup)})
    state = {
        'schema': 'PentacleLauncherStateV2',
        'launchers': launchers,
        'legacy_root': {'original': str(legacy_root), 'backup': str(legacy_backup), 'moved': False},
        'pth_files': pth_files,
        'migrated_pth': [],
    }
    write_state(state)
if not migrate:
    raise SystemExit(0)
legacy = state.get('legacy_root')
if not isinstance(legacy, dict) or legacy.get('original') != str(legacy_root) or legacy.get('backup') != str(legacy_backup):
    raise SystemExit('launcher_state_invalid')
pth_files = state.get('pth_files', [])
if not isinstance(pth_files, list):
    raise SystemExit('launcher_state_invalid')
launchers = state.get('launchers', [])
if not isinstance(launchers, list):
    raise SystemExit('launcher_state_invalid')
for entry in launchers:
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not isinstance(entry.get('state'), dict):
        raise SystemExit('launcher_state_invalid')
for entry in pth_files:
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not isinstance(entry.get('backup'), str):
        raise SystemExit('launcher_state_invalid')
if lexists(legacy_root):
    if lexists(legacy_backup):
        raise SystemExit('legacy_release_backup_conflict')
    os.replace(legacy_root, legacy_backup)
    legacy['moved'] = True
    write_state(state)
for entry in pth_files:
    path = pathlib.Path(entry['path'])
    if lexists(path):
        path.unlink()
        state['migrated_pth'].append(str(path))
state['migrated_pth'] = sorted(set(state.get('migrated_pth', [])))
state['migrated'] = True
write_state(state)
"""
    return f"python3 -c {_remote_quote(script)} {_remote_quote(target.root)} {_remote_quote(json.dumps(_path_launchers(target)))} {_remote_quote(state_path)} {_remote_quote(legacy_root)} {_remote_quote(legacy_backup)} {_remote_quote('1' if migrate else '0')}"


def _migrate_legacy_residue(target: Target, stamp: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> None:
    _remote(target, _launcher_state_command(target, stamp, migrate=True), dry_run=dry_run, runner=runner)


def _snapshot_legacy_residue(target: Target, stamp: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> None:
    _remote(target, _launcher_state_command(target, stamp, migrate=False), dry_run=dry_run, runner=runner)


def _restore_legacy_residue(target: Target, stamp: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> None:
    _remote(target, _restore_launcher_state_command(target, stamp), dry_run=dry_run, runner=runner)


def _restore_launcher_state_command(target: Target, stamp: str) -> str:
    state_path, legacy_root, legacy_backup = _rollout_state_paths(target, stamp)
    script = """import json, os, pathlib, shutil, stat, sys
launcher, state_path, legacy_root, legacy_backup = map(pathlib.Path, sys.argv[1:5])
def lexists(path):
    return os.path.lexists(path)
try:
    state = json.loads(state_path.read_text(encoding='utf-8'))
except (OSError, ValueError) as exc:
    raise SystemExit('launcher_state_invalid') from exc
if not isinstance(state, dict):
    raise SystemExit('launcher_state_invalid')
if state.get('schema') == 'PentacleLauncherStateV1':
    launchers = [{'path': str(launcher), 'state': state.get('launcher')}]
elif state.get('schema') == 'PentacleLauncherStateV2':
    launchers = state.get('launchers')
else:
    raise SystemExit('launcher_state_invalid')
legacy = state.get('legacy_root')
if not isinstance(launchers, list) or not isinstance(legacy, dict):
    raise SystemExit('launcher_state_invalid')
if legacy.get('original') != str(legacy_root) or legacy.get('backup') != str(legacy_backup):
    raise SystemExit('launcher_state_invalid')
pth_files = state.get('pth_files', [])
if not isinstance(pth_files, list):
    raise SystemExit('launcher_state_invalid')
for entry in pth_files:
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not isinstance(entry.get('backup'), str):
        raise SystemExit('launcher_state_invalid')
    if not lexists(pathlib.Path(entry['backup'])):
        raise SystemExit('launcher_state_invalid')
for entry in launchers:
    if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not isinstance(entry.get('state'), dict):
        raise SystemExit('launcher_state_invalid')
    launcher_state = entry['state']
    kind = launcher_state.get('kind')
    if kind == 'symlink' and not isinstance(launcher_state.get('target'), str):
        raise SystemExit('launcher_state_invalid')
    if kind == 'file' and (not isinstance(launcher_state.get('backup'), str) or not lexists(pathlib.Path(launcher_state['backup']))):
        raise SystemExit('launcher_state_invalid')
    if kind not in {'absent', 'symlink', 'file'}:
        raise SystemExit('launcher_state_invalid')
if lexists(legacy_backup):
    if lexists(legacy_root):
        raise SystemExit('legacy_release_restore_unavailable')
    os.replace(legacy_backup, legacy_root)
elif legacy.get('moved') and not lexists(legacy_root):
    raise SystemExit('legacy_release_restore_unavailable')
for entry in launchers:
    launcher = pathlib.Path(entry['path'])
    launcher_state = entry['state']
    kind = launcher_state['kind']
    launcher.parent.mkdir(parents=True, exist_ok=True)
    if kind == 'absent':
        if lexists(launcher):
            launcher.unlink()
    elif kind == 'symlink':
        staged = launcher.with_name(launcher.name + '.rollback.new')
        if lexists(staged):
            raise SystemExit('launcher_restore_staging_exists')
        os.symlink(launcher_state['target'], staged)
        os.replace(staged, launcher)
    else:
        staged = launcher.with_name(launcher.name + '.rollback.new')
        if lexists(staged):
            raise SystemExit('launcher_restore_staging_exists')
        shutil.copyfile(launcher_state['backup'], staged)
        staged.chmod(int(launcher_state['mode']))
        os.replace(staged, launcher)
for entry in pth_files:
    path = pathlib.Path(entry['path'])
    staged = path.with_name(path.name + '.rollback.new')
    if lexists(staged):
        raise SystemExit('launcher_restore_staging_exists')
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(entry['backup'], staged)
    os.replace(staged, path)
"""
    return f"python3 -c {_remote_quote(script)} {_remote_quote(_stable_launcher(target))} {_remote_quote(state_path)} {_remote_quote(legacy_root)} {_remote_quote(legacy_backup)}"


def _runtime_prepare_command(release: str, requires_python: str) -> str:
    script = """import pathlib, shutil, subprocess, sys
release = pathlib.Path(sys.argv[1]).resolve()
requires_python = sys.argv[2]
runtime = release / 'runtime'
package = release / 'app' / 'services' / 'agent-orch'
support_module = release / 'app' / 'services' / '_shared' / 'asset_schema.py'
dependency_bundle = release / 'app' / 'runtime-deps' / 'websockets'
selector = release / 'app' / 'services' / 'agent-orch' / 'deploy' / 'runtime_python.py'
if not (package / 'agent_orch').is_dir():
    raise SystemExit('runtime_package_missing')
if not support_module.is_file():
    raise SystemExit('runtime_support_module_missing')
if not dependency_bundle.is_dir():
    raise SystemExit('runtime_dependency_missing')
if not selector.is_file():
    raise SystemExit('runtime_interpreter_selector_missing')
selected_python = subprocess.check_output([sys.executable, str(selector), '--select', requires_python], text=True).strip()
if not pathlib.Path(selected_python).is_file():
    raise SystemExit('runtime_interpreter_selector_invalid')
subprocess.run([selected_python, '-m', 'venv', '--copies', str(runtime)], check=True)
python = runtime / 'bin' / 'python'
subprocess.run([str(python), '-I', str(selector), '--check', requires_python], check=True)
site = subprocess.check_output([str(python), '-c', 'import site; print(site.getsitepackages()[0])'], text=True).strip()
site = pathlib.Path(site)
shutil.copytree(package / 'agent_orch', site / 'agent_orch')
(pathlib.Path(site) / 'pentacle_release.pth').write_text(str(release / 'app' / 'services') + '\\n', encoding='utf-8')
shutil.copy2(support_module, pathlib.Path(site) / 'asset_schema.py')
for path in dependency_bundle.iterdir():
    target = site / path.name
    if path.is_dir():
        shutil.copytree(path, target)
    else:
        shutil.copy2(path, target)
launcher = runtime / 'bin' / 'agent-orch'
launcher.write_text('#!' + str(python) + ' -I\\nfrom agent_orch.cli import main\\nraise SystemExit(main())\\n', encoding='utf-8')
launcher.chmod(0o755)
"""
    return f"python3 -c {_remote_quote(script)} {_remote_quote(release)} {_remote_quote(requires_python)}"


def _runtime_verify_command(release: str, manifest: dict[str, object]) -> str:
    runtime = release + "/runtime"
    package = dict(manifest["runtime_package"])
    requires_python = package.get("requires_python")
    selector = dict(package.get("interpreter_selector") or {})
    selector_path = selector.get("path")
    selector_sha256 = selector.get("sha256")
    if not isinstance(requires_python, str) or not isinstance(selector_path, str) or not isinstance(selector_sha256, str):
        raise ValueError("runtime_requires_python_missing")
    if selector_path != RUNTIME_SELECTOR_PATH:
        raise ValueError("runtime_interpreter_selector_invalid")
    source_selector = release + "/app/" + selector_path
    hash_file = _remote_quote(_SHA256_FILE_SCRIPT)
    script = """import hashlib, importlib, importlib.metadata, json, pathlib, sys
release = pathlib.Path(sys.argv[1]).resolve()
expected_tree = sys.argv[2]
expected_catalog = sys.argv[3]
runtime = release / 'runtime'
if pathlib.Path(sys.executable).resolve() != (runtime / 'bin' / 'python').resolve():
    raise SystemExit('runtime_interpreter_mismatch')
launcher_path = runtime / 'bin' / 'agent-orch'
expected_launcher = '#!' + str(runtime / 'bin' / 'python') + ' -I\\nfrom agent_orch.cli import main\\nraise SystemExit(main())\\n'
if launcher_path.read_text(encoding='utf-8') != expected_launcher:
    raise SystemExit('runtime_launcher_tampered')
agent = importlib.import_module('agent_orch')
shared = importlib.import_module('_shared.spawn_profiles')
dependency = importlib.import_module('websockets')
support = importlib.import_module('asset_schema')
agent_root = pathlib.Path(agent.__file__).resolve().parent
shared_path = pathlib.Path(shared.__file__).resolve()
dependency_path = pathlib.Path(dependency.__file__).resolve()
site_packages = pathlib.Path(importlib.import_module('site').getsitepackages()[0]).resolve()
support_path = site_packages / 'asset_schema.py'
source_root = release / 'app' / 'services' / 'agent-orch' / 'agent_orch'
source_support = release / 'app' / 'services' / '_shared' / 'asset_schema.py'
for name, path in {'agent_orch':agent_root, 'shared_contract':shared_path, 'source_package':source_root, 'dependency':dependency_path, 'support_module':support_path, 'source_support':source_support}.items():
    if not path.is_relative_to(release):
        raise SystemExit('runtime_path_outside_release:' + name + ':' + str(path))
if not dependency_path.is_relative_to(runtime):
    raise SystemExit('runtime_dependency_outside_runtime')
def tree_hash(root):
    digest = hashlib.sha256()
    files = sorted((path for path in root.rglob('*') if path.is_file() and '__pycache__' not in path.parts), key=lambda path: path.relative_to(root).as_posix())
    if not files:
        raise SystemExit('runtime_package_missing')
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()
if tree_hash(source_root) != expected_tree or tree_hash(agent_root) != expected_tree:
    raise SystemExit('runtime_package_hash_mismatch')
support_manifest = json.loads(sys.argv[4])
if support_manifest:
    expected_support = support_manifest.get('sha256')
    if not isinstance(expected_support, str) or hashlib.sha256(source_support.read_bytes()).hexdigest() != expected_support or hashlib.sha256(support_path.read_bytes()).hexdigest() != expected_support:
        raise SystemExit('runtime_support_module_hash_mismatch')
dependency_manifest = json.loads(sys.argv[5])
if not dependency_manifest:
    raise SystemExit('runtime_dependency_manifest_missing')
expected_dependency = dependency_manifest.get('sha256')
expected_version = dependency_manifest.get('version')
dependency_source = release / 'app' / 'runtime-deps' / 'websockets'
def dependency_hash(root):
    digest = hashlib.sha256()
    files = []
    for path in (root / 'websockets', *root.glob('websockets-*.dist-info')):
        if path.is_dir():
            files.extend(item for item in path.rglob('*') if item.is_file() and '__pycache__' not in item.parts)
    if not files:
        raise SystemExit('runtime_dependency_missing')
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode('utf-8') + b'\\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()
dependency_site = dependency_path.parent.parent
if not isinstance(expected_dependency, str) or not isinstance(expected_version, str) or dependency_hash(dependency_source) != expected_dependency or dependency_hash(dependency_site) != expected_dependency or importlib.metadata.version('websockets') != expected_version:
    raise SystemExit('runtime_dependency_hash_mismatch')
if getattr(shared, 'CATALOG_VERSION', None) != expected_catalog:
    raise SystemExit('runtime_catalog_mismatch')
"""
    return " && ".join((
        f"test -x {_remote_quote(runtime + '/bin/python')}",
        f"test -x {_remote_quote(runtime + '/bin/python3')}",
        f"test -x {_remote_quote(_runtime_launcher(release))}",
        f"test -f {_remote_quote(source_selector)}",
        f"test \"$(python3 -c {hash_file} {_remote_quote(source_selector)})\" = {_remote_quote(selector_sha256)}",
        f"{_remote_quote(runtime + '/bin/python')} -I {_remote_quote(source_selector)} --check {_remote_quote(requires_python)}",
        f"{_remote_quote(runtime + '/bin/python')} -I -c {_remote_quote(script)} {_remote_quote(release)} {_remote_quote(str(package['sha256']))} {_remote_quote(str(manifest['catalog_version']))} {_remote_quote(json.dumps(dict(package.get('asset_schema') or {}), sort_keys=True))} {_remote_quote(json.dumps(dict(package.get('dependencies') or {}).get('websockets') or {}, sort_keys=True))}",
    ))


def _verify_release(target: Target, commit: str, manifest: dict[str, object], *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> None:
    release = _release(target, commit)
    expected = str(manifest["archive_sha256"])
    hash_file = _remote_quote(_SHA256_FILE_SCRIPT)
    manifest_identity = _remote_quote(_VERIFY_MANIFEST_IDENTITY_SCRIPT)
    manifest_exact = _remote_quote(_VERIFY_MANIFEST_SCRIPT)
    manifest_catalog = _remote_quote(_VERIFY_MANIFEST_CATALOG_SCRIPT)
    command = " && ".join((
        f"test -d {_remote_quote(release)}",
        f"test -s {_remote_quote(release + '/pentacle.tar')}",
        f"test -s {_remote_quote(release + '/manifest.json')}",
        f"test -f {_remote_quote(release + '/app/services/_shared/spawn_profiles.py')}",
        f"test \"$(python3 -c {hash_file} {_remote_quote(release + '/pentacle.tar')})\" = {_remote_quote(expected)}",
        f"python3 -c {manifest_identity} {_remote_quote(release + '/manifest.json')} {_remote_quote(commit)} {_remote_quote(expected)}",
        f"python3 -c {manifest_exact} {_remote_quote(release + '/manifest.json')} {_remote_quote(json.dumps(manifest, sort_keys=True))}",
        f"test \"$(python3 -c {hash_file} {_remote_quote(release + '/app/services/_shared/spawn_profiles.py')})\" = {_remote_quote(str(dict(manifest['contract'])['sha256']))}",
        f"python3 -c {manifest_catalog} {_remote_quote(release + '/manifest.json')} {_remote_quote(str(manifest['catalog_version']))}",
    ))
    if manifest.get("runtime_package"):
        runtime_manifest = _remote_quote(json.dumps(manifest["runtime_package"], sort_keys=True))
        command += " && " + " && ".join((
            f"test -d {_remote_quote(release + '/app/services/agent-orch/agent_orch')}",
            f"python3 -c {_remote_quote(_VERIFY_RUNTIME_MANIFEST_SCRIPT)} {_remote_quote(release + '/manifest.json')} {runtime_manifest}",
            _runtime_verify_command(release, manifest),
        ))
    _remote(target, command, dry_run=dry_run, runner=runner)


def stage(
    target: Target,
    bundle: Path,
    commit: str,
    manifest: dict[str, object],
    *,
    dry_run: bool,
    runner: Callable[..., object] = subprocess.run,
    sftp_timeout: int = DEFAULT_SFTP_TIMEOUT_SECONDS,
) -> str:
    """Stage once or resume an already verified immutable release."""
    release = _release(target, commit)
    archive = bundle / "pentacle.tar"
    archive_bytes = archive.stat().st_size if archive.is_file() else None
    _progress("stage_start", host=target.name, commit=commit, bytes=archive_bytes)
    try:
        _verify_release(target, commit, manifest, dry_run=dry_run, runner=runner)
        result = "resumed_verified"
    except subprocess.CalledProcessError:
        active = _read_pointer(target, "active", dry_run=dry_run, runner=runner)
        if active == release:
            raise RuntimeError(f"active_release_failed_verification:{target.name}")
        staging = release + ".staging"
        _remote(target, f"mkdir -p {_remote_quote(target.root)} && rm -rf {_remote_quote(staging)} {_remote_quote(release)} && mkdir -m 700 {_remote_quote(staging)}", dry_run=dry_run, runner=runner)
        if not dry_run:
            if target.local and not target.loopback:
                staging_path = Path(staging)
                shutil.copy2(bundle / "pentacle.tar", staging_path / "pentacle.tar")
                shutil.copy2(bundle / "manifest.json", staging_path / "manifest.json")
            else:
                try:
                    result = runner(
                        _scp_argv(target, bundle, staging),
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=sftp_timeout,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(f"sftp_timeout:{target.name}:{sftp_timeout}s") from exc
                if getattr(result, "returncode", 1) != 0:
                    raise subprocess.CalledProcessError(
                        getattr(result, "returncode", 1), ["scp", target.name],
                        output=getattr(result, "stdout", ""), stderr=getattr(result, "stderr", ""),
                    )
            _progress("stage_transfer", host=target.name, commit=commit, bytes=archive_bytes)
        _remote(target, " && ".join((
            f"mkdir {_remote_quote(staging + '/app')}",
            f"tar -xf {_remote_quote(staging + '/pentacle.tar')} -C {_remote_quote(staging + '/app')}",
            f"test ! -e {_remote_quote(release)}",
            f"mv {_remote_quote(staging)} {_remote_quote(release)}",
        )), dry_run=dry_run, runner=runner)
        _remote(target, _runtime_prepare_command(release, str(dict(manifest["runtime_package"])["requires_python"])), dry_run=dry_run, runner=runner)
        try:
            _verify_release(target, commit, manifest, dry_run=dry_run, runner=runner)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"release_hash_verify_failed:{target.name}") from exc
        result = "staged_verified"
    stamp = manifest.get("stamp")
    if not isinstance(stamp, str):
        raise ValueError("release_stamp_invalid")
    _snapshot_legacy_residue(target, stamp, dry_run=dry_run, runner=runner)
    _progress(
        "hash_verify",
        host=target.name,
        commit=commit,
        archive_sha256=manifest.get("archive_sha256"),
    )
    _progress("stage_end", host=target.name, commit=commit, result=result, bytes=archive_bytes)
    return result


def activate(target: Target, commit: str, manifest: dict[str, object], previous: str | None, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> dict[str, str | None]:
    stamp = manifest.get("stamp")
    if not isinstance(stamp, str):
        raise ValueError("release_stamp_invalid")
    release = _release(target, commit)
    launchers = _path_launchers(target)
    _progress("activate_start", host=target.name, commit=commit)
    _verify_release(target, commit, manifest, dry_run=dry_run, runner=runner)
    _progress("hash_verify", host=target.name, commit=commit, archive_sha256=manifest.get("archive_sha256"))
    _remote(target, _launcher_state_command(target, stamp, migrate=False), dry_run=dry_run, runner=runner)
    old = previous
    command = " && ".join((
        f"mkdir -p {_remote_quote(target.root)}",
        f"mkdir -p {_remote_quote(target.root + '/rollouts')}",
        *(f"mkdir -p {_remote_quote(str(Path(launcher).parent))}" for launcher in launchers),
        f"if test -n {_remote_quote(old or '')}; then {_replace_pointer_command(target, 'previous', old or '')} && {_replace_pointer_command(target, 'rollouts/' + str(manifest['stamp']) + '.previous', old or '')}; else rm -f {_remote_quote(target.root + '/previous')} {_remote_quote(target.root + '/rollouts/' + str(manifest['stamp']) + '.previous')}; fi",
        _replace_pointer_command(target, "active", release),
        _replace_pointer_command(target, "rollouts/" + str(manifest["stamp"]) + ".release", release),
        *(_replace_link_command(launcher, target.root + '/active/runtime/bin/agent-orch') for launcher in launchers),
    ))
    _remote(target, command, dry_run=dry_run, runner=runner)
    actual = _read_pointer(target, "active", dry_run=dry_run, runner=runner)
    observed_previous = _read_pointer(target, "previous", dry_run=dry_run, runner=runner)
    if not dry_run and actual != release:
        raise RuntimeError(f"activation_readback_mismatch:{target.name}")
    if not dry_run and observed_previous != old:
        raise RuntimeError(f"previous_pointer_readback_mismatch:{target.name}")
    for launcher in launchers:
        observed = _remote(target, f"readlink {_remote_quote(launcher)}", dry_run=dry_run, runner=runner)
        if not dry_run and observed != target.root + "/active/runtime/bin/agent-orch":
            raise RuntimeError(f"launcher_readback_mismatch:{target.name}")
    _migrate_legacy_residue(target, stamp, dry_run=dry_run, runner=runner)
    _progress("activate_end", host=target.name, commit=commit, active=actual, previous=observed_previous)
    return {"active": actual, "previous": observed_previous}


def _release_manifest(target: Target, release: str, *, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> dict[str, object]:
    raw = _remote(target, f"cat {_remote_quote(release + '/manifest.json')}", dry_run=dry_run, runner=runner)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("rollback_manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("rollback_manifest_invalid")
    commit = manifest.get("commit")
    catalog = manifest.get("catalog_version")
    if not isinstance(commit, str) or not SHA.fullmatch(commit) or Path(release).name != commit or not isinstance(catalog, str):
        raise ValueError("rollback_manifest_identity_invalid")
    return manifest


def rollback(target: Target, previous: str | None, *, stamp: str | None = None, dry_run: bool, runner: Callable[..., object] = subprocess.run) -> dict[str, str | None]:
    if not previous:
        raise RuntimeError(f"rollback_previous_missing:{target.name}")
    manifest = _release_manifest(target, previous, dry_run=dry_run, runner=runner)
    commit = str(manifest["commit"])
    _verify_release(target, commit, manifest, dry_run=dry_run, runner=runner)
    if stamp is not None:
        _remote(target, _restore_launcher_state_command(target, stamp), dry_run=dry_run, runner=runner)
    _remote(target, f"test -e {_remote_quote(previous)} && {_replace_pointer_command(target, 'active', previous)}", dry_run=dry_run, runner=runner)
    actual = _read_pointer(target, "active", dry_run=dry_run, runner=runner)
    if not dry_run and actual != previous:
        raise RuntimeError(f"rollback_readback_mismatch:{target.name}")
    if stamp is None:
        launcher = _remote_realpath(target, _stable_launcher(target), dry_run=dry_run, runner=runner)
        expected = _remote_realpath(target, previous + "/runtime/bin/agent-orch", dry_run=dry_run, runner=runner)
        if not dry_run and launcher != expected:
            raise RuntimeError(f"rollback_launcher_readback_mismatch:{target.name}")
    return {
        "active": actual,
        "previous": _read_pointer(target, "previous", dry_run=dry_run, runner=runner),
        "commit": commit,
        "catalog_version": str(manifest["catalog_version"]),
    }


def _rollout_stamp(repo: Path, commit: str, *, resume: str | None, rollback_stamp: str | None) -> str:
    if rollback_stamp:
        return rollback_stamp
    if resume:
        return resume
    contract, _catalog = _release_contract(repo, commit)
    return f"{commit[:12]}-{_sha256_bytes(contract)[:12]}"


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return seconds


def _load_hosts(path: str | None) -> dict[str, tuple[str, str]]:
    if path is None:
        return dict(HOSTS)
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("--host-config must contain a nonempty host mapping")
    hosts = {}
    for name, config in raw.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or not isinstance(config, dict):
            raise ValueError("invalid host name or host-config entry")
        ssh, root = config.get("ssh"), config.get("release_root")
        if not isinstance(ssh, str) or not ssh or ssh.startswith("-") or any(char.isspace() for char in ssh):
            raise ValueError(f"invalid SSH target for {name}")
        if not isinstance(root, str) or not root.startswith("/") or any(char in root for char in "\n\r\0"):
            raise ValueError(f"release_root must be an absolute path for {name}")
        hosts[name] = (ssh, root)
    return hosts


def _parse_ssh_target_overrides(values: list[str], hosts: dict[str, tuple[str, str]] | None = None) -> dict[str, str]:
    hosts = HOSTS if hosts is None else hosts
    overrides: dict[str, str] = {}
    for value in values:
        name, separator, alias = value.partition("=")
        if not separator or name not in hosts or not alias or alias.startswith("-") or any(char.isspace() for char in alias):
            raise ValueError("--ssh-target must use NAME=ALIAS for a known host")
        if name in overrides:
            raise ValueError(f"--ssh-target specified more than once for {name}")
        overrides[name] = alias
    return overrides


def _targets(names: list[str], run_host: str, ssh_overrides: dict[str, str], hosts: dict[str, tuple[str, str]] | None = None) -> list[Target]:
    hosts = HOSTS if hosts is None else hosts
    targets: list[Target] = []
    for name in names:
        default_ssh, root = hosts[name]
        ssh = ssh_overrides.get(name, default_ssh)
        local = name == run_host
        targets.append(Target(name, ssh, root, local=local, loopback=local and ssh in {"localhost", "127.0.0.1", "::1"}))
    return targets


def _rollout_lock_path() -> Path:
    configured = os.environ.get("PENTACLE_RELEASE_ROLLOUT_LOCK") or os.environ.get("PENTACLE_RELEASE_ROLLOUT_LOCK_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "pentacle" / "agent-orch-release-rollout.lock"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument(
        "--hosts",
        required=True,
        help="comma-separated names from --host-config (default map: local only)",
    )
    parser.add_argument("--host-config", metavar="PATH", help="JSON mapping names to ssh and absolute release_root")
    parser.add_argument("--run-host", default="local", metavar="NAME", help="configured name of this machine")
    parser.add_argument("--ssh-target", action="append", default=[], metavar="NAME=ALIAS")
    parser.add_argument("--sftp-timeout", type=_positive_seconds, default=DEFAULT_SFTP_TIMEOUT_SECONDS, metavar="SECONDS")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", metavar="STAMP", help="resume the exact staged immutable rollout stamp")
    parser.add_argument("--rollback", metavar="STAMP", help="restore the previous pointer recorded for rollout stamp")
    args = parser.parse_args()
    if not SHA.fullmatch(args.commit):
        parser.error("--commit must be a full immutable 40-character SHA")
    try:
        hosts = _load_hosts(args.host_config)
        names = [name.strip() for name in args.hosts.split(",")]
        if not names or len(set(names)) != len(names) or any(name not in hosts for name in names):
            raise ValueError("--hosts must name configured hosts at most once")
        if args.run_host not in hosts:
            raise ValueError("--run-host must name a configured host")
        ssh_overrides = _parse_ssh_target_overrides(args.ssh_target, hosts)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if args.resume and args.rollback:
        parser.error("--resume and --rollback are mutually exclusive")
    if (args.resume and not STAMP.fullmatch(args.resume)) or (args.rollback and not STAMP.fullmatch(args.rollback)):
        parser.error("rollout stamp is invalid")
    targets = _targets(names, args.run_host, ssh_overrides, hosts)
    records: dict[str, object] = {"schema": "PentacleFleetInstallV1", "commit": args.commit, "hosts": {}}
    try:
        repo = Path(args.repo)
        lock_stamp = _rollout_stamp(repo, args.commit, resume=args.resume, rollback_stamp=args.rollback)
        with RolloutLock(_rollout_lock_path(), lock_stamp):
            _progress("rollout_start", stamp=lock_stamp, hosts=names, dry_run=args.dry_run)
            if args.rollback:
                for target in targets:
                    old = _read_pointer(target, f"rollouts/{args.rollback}.previous", dry_run=args.dry_run)
                    records["hosts"][target.name] = {"rollback": rollback(target, old, stamp=args.rollback, dry_run=args.dry_run)}
            else:
                with tempfile.TemporaryDirectory(prefix="pentacle-release-") as temp:
                    manifest = _archive(repo, args.commit, Path(temp), args.resume)
                    records["manifest"] = manifest
                    for target in targets:
                        records["hosts"][target.name] = {
                            "stage": stage(
                                target,
                                Path(temp),
                                args.commit,
                                manifest,
                                dry_run=args.dry_run,
                                sftp_timeout=args.sftp_timeout,
                            )
                        }
                    activated: list[tuple[Target, str | None]] = []
                    try:
                        for target in targets:
                            previous = _read_pointer(target, "active", dry_run=args.dry_run)
                            # Append before switching: an activation whose readback
                            # transport fails may already have moved active.
                            activated.append((target, previous))
                            state = activate(target, args.commit, manifest, previous, dry_run=args.dry_run)
                            records["hosts"][target.name]["activation"] = state
                    except Exception:
                        for target, previous in reversed(activated):
                            records["hosts"][target.name]["rollback"] = rollback(target, previous, stamp=str(manifest["stamp"]), dry_run=args.dry_run)
                        raise
        records["ok"] = True
        print(json.dumps(records, sort_keys=True), flush=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        records.update({"ok": False, "error": str(exc) or type(exc).__name__})
        print(json.dumps(records, sort_keys=True), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
