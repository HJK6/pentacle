"""Root-only installation of the publisher's read-only CLI consumer."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid

ROOT = Path("/usr/local/libexec/pentacle-work-report")
PYTHON = "/usr/local/libexec/triforce-memory-python/bin/python"
PREFIXES = ("services/agent-orch/agent_orch", "services/_shared", "runtime-deps/websockets")
_ROOT_UID = 0


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _directory(path: Path) -> None:
    if not path.exists():
        _directory(path.parent)
        path.mkdir(mode=0o755)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != _ROOT_UID or info.st_mode & 0o022:
        raise ValueError("unsafe consumer directory")
    if path.parent != path:
        _directory(path.parent)


def _inventory(root: Path) -> dict[str, tuple[int, str]]:
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if info.st_uid != _ROOT_UID or info.st_mode & 0o222:
            raise ValueError("consumer release is writable or not root-owned")
        name = str(path.relative_to(root))
        if stat.S_ISDIR(info.st_mode):
            result[name] = (stat.S_IMODE(info.st_mode), "directory")
        elif stat.S_ISREG(info.st_mode):
            result[name] = (stat.S_IMODE(info.st_mode), _hash(path))
        else:
            raise ValueError("consumer release contains link or special file")
    return result


def _link(name: str, target: str) -> None:
    temporary = ROOT / ("." + name + "." + uuid.uuid4().hex)
    temporary.symlink_to(target)
    try:
        os.replace(temporary, ROOT / name)
    finally:
        temporary.unlink(missing_ok=True)


def _pointer(name: str) -> str | None:
    path = ROOT / name
    if not os.path.lexists(path):
        return None
    if not path.is_symlink() or path.lstat().st_uid != _ROOT_UID:
        raise ValueError("unsafe consumer " + name + " pointer")
    value = os.readlink(path)
    if not re.fullmatch(r"releases/[0-9a-f]{40}", value):
        raise ValueError("consumer " + name + " pointer outside release prefix")
    release = ROOT / value
    _directory(release)
    _inventory(release)
    return value


def _active() -> str | None:
    return _pointer("active")


def _restore_pointer(name: str, target: str | None) -> None:
    if target is None:
        (ROOT / name).unlink(missing_ok=True)
    else:
        _link(name, target)


def _extract(archive: Path, destination: Path) -> None:
    seen = set()
    with tarfile.open(archive) as bundle:
        for member in bundle:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("unsafe consumer archive path")
            if not any(member.name == p or member.name.startswith(p + "/") for p in PREFIXES):
                continue
            if member.name in seen or not (member.isdir() or member.isfile()):
                raise ValueError("consumer archive contains duplicate, link or special member")
            seen.add(member.name)
            path = destination / "app" / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            else:
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("consumer archive member missing")
                with source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
    for prefix in PREFIXES:
        if not (destination / "app" / prefix).is_dir():
            raise ValueError("consumer archive dependency missing")


def _probe(release: Path) -> None:
    # The copied archive and wrapper have been verified before this first import.
    publisher = pwd.getpwnam("_triforce_memory")
    code = ("import os,pathlib,runpy,sys; assert os.geteuid()==int(sys.argv[2]); p=pathlib.Path(sys.argv[1]); "
            "x=runpy.run_path(str(p/'agent-orch')); x['_load_reader'](); "
            "import agent_orch,websockets,_shared; "
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(p) "
            "for m in (agent_orch,websockets,_shared))")
    subprocess.run([PYTHON, "-I", "-c", code, str(release), str(publisher.pw_uid)], check=True,
                   user=publisher.pw_uid, group=publisher.pw_gid, extra_groups=[], cwd="/",
                   capture_output=True, timeout=20, env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})


def install(source: Path, commit: str, manifest_sha256: str) -> dict:
    if os.geteuid() != 0:
        raise ValueError("consumer installation requires root")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise ValueError("consumer requires exact commit and pinned manifest SHA-256")
    _directory(ROOT)
    _directory(ROOT / "releases")
    lock_path = ROOT / "install.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != _ROOT_UID or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("unsafe consumer installation lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = _active()
        old_previous = _pointer("previous")
        launcher = ROOT / "agent-orch"
        if os.path.lexists(launcher) and (not launcher.is_symlink()
                or launcher.lstat().st_uid != _ROOT_UID or os.readlink(launcher) != "active/agent-orch"):
            raise ValueError("unexpected consumer launcher")
        with tempfile.TemporaryDirectory(prefix=".consumer-", dir=ROOT) as temp:
            staging = Path(temp)
            # The source may belong to the daemon user. Never import it or trust
            # a check followed by a later copy from that mutable source.
            for name in ("pentacle.tar", "manifest.json"):
                shutil.copyfile(source / name, staging / name)
            if _hash(staging / "manifest.json") != manifest_sha256:
                raise ValueError("consumer manifest hash mismatch")
            manifest = json.loads((staging / "manifest.json").read_text())
            if (not isinstance(manifest, dict) or manifest.get("commit") != commit
                    or manifest.get("schema") != "PentacleReleaseManifestV2"
                    or _hash(staging / "pentacle.tar") != manifest.get("archive_sha256")):
                raise ValueError("consumer archive identity mismatch")
            candidate = staging / "release"
            candidate.mkdir()
            _extract(staging / "pentacle.tar", candidate)
            shutil.copyfile(staging / "manifest.json", candidate / "manifest.json")
            shutil.copyfile(Path(__file__).with_name("report_consumer_cli.py"), candidate / "agent-orch")
            for path in sorted(candidate.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() or path.name == "agent-orch" else 0o444)
            candidate.chmod(0o555)
            expected = _inventory(candidate)
            release = ROOT / "releases" / commit
            if os.path.lexists(release):
                _directory(release)
                if _inventory(release) != expected:
                    raise ValueError("existing consumer release differs")
            else:
                # Moving a directory changes its '..' entry on platforms that
                # require owner write permission even for this rename.
                candidate.chmod(0o755)
                candidate.rename(release)
                release.chmod(0o555)
            _probe(release)
            # A stable link through active switches wrapper and imports together.
            # Installing it first is safe: refresh requires the same link target.
            if not os.path.lexists(launcher):
                _link("agent-orch", "active/agent-orch")
            try:
                if old is not None:
                    _link("previous", old)
                _link("active", "releases/" + commit)
                if _active() != "releases/" + commit or launcher.resolve() != release / "agent-orch":
                    raise ValueError("consumer activation readback mismatch")
            except Exception:
                try:
                    _restore_pointer("active", old)
                finally:
                    _restore_pointer("previous", old_previous)
                raise
            return {"ok": True, "mode": "report_consumer", "commit": commit,
                    "manifest_sha256": manifest_sha256, "wrapper_sha256": _hash(release / "agent-orch"),
                    "active": "releases/" + commit, "previous": old, "consumer": str(launcher)}
