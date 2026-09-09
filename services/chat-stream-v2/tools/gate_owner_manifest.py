"""Small, conservative ownership manifest for one v2 gate invocation."""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


_REAL_POPEN = subprocess.Popen
MANIFEST_ENV = "V2_GATE_OWNER_MANIFEST"
MANIFEST_VERSION = 2
MANIFEST_SCHEMA = "pentacle.v2.gate-owner-manifest.v2"
ACTIVE, STOPPING = "active", "stopping"
_STATES = {ACTIVE, STOPPING}
_SOCKET_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class ManifestError(RuntimeError):
    """The manifest is invalid or cannot be used safely."""
class ManifestBusyError(ManifestError):
    """A different live owner holds the manifest."""


def manifest_path_from_env(env: dict[str, str] | None = None) -> Path | None:
    value = (env or os.environ).get(MANIFEST_ENV, "").strip()
    return Path(value) if value else None
def resolve_tmux_socket(socket: str | os.PathLike[str]) -> Path:
    value = str(socket)
    if os.path.isabs(value):
        return Path(value)
    return Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}" / value


def _pgid(value: int | str | None) -> int | None:
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"invalid process-group id: {value!r}") from exc
    if value <= 1:
        raise ManifestError(f"unsafe process-group id: {value}")
    return value
def _socket(value: str | os.PathLike[str] | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    if value == "default" or Path(value).name == "default":
        raise ManifestError("refusing the operator default tmux socket")
    if not Path(value).is_absolute() and not _SOCKET_NAME.fullmatch(value):
        raise ManifestError(f"invalid private tmux socket name: {value!r}")
    return value


def _entry(pgid: int | str | None, socket: str | os.PathLike[str] | None,
           leader_pid: int | str | None = None, run_id: str | None = None,
           leader_start_identity: str | None = None) -> dict[str, Any]:
    pgid, socket = _pgid(pgid), _socket(socket)
    if pgid is None and socket is None:
        raise ManifestError("an owned entry needs a process group or private socket")
    leader = _pgid(leader_pid) if leader_pid is not None else pgid
    identity = leader_start_identity if isinstance(leader_start_identity, str) and leader_start_identity else None
    return {"pgid": pgid, "socket": socket,
            "socket_path": str(resolve_tmux_socket(socket)) if socket else None,
            "leader_pid": leader, "leader_start_identity": identity, "run_id": run_id}
def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return pid > 0


def process_start_identity(pid: int) -> str | None:
    """Return a PID-reuse-resistant identity without test Popen patches."""
    if pid <= 0:
        return None
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19]
        except (OSError, IndexError):
            return None
    try:
        process = _REAL_POPEN(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, _ = process.communicate(timeout=2)
        return stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
def _owner(data: dict[str, Any]) -> tuple[int, str, str]:
    try:
        pid = int(data.get("owner_pid", 0))
    except (TypeError, ValueError):
        pid = 0
    identity = data.get("owner_start_identity")
    state = data.get("owner_state")
    return pid, identity if isinstance(identity, str) else "", state if isinstance(state, str) else ""
def _stale(data: dict[str, Any]) -> bool:
    pid, identity, _ = _owner(data)
    if not _exists(pid):
        return True
    observed = process_start_identity(pid)
    return observed is not None and bool(identity) and observed != identity
def _current(data: dict[str, Any]) -> bool:
    pid, identity, _ = _owner(data)
    return pid == os.getpid() and identity == process_start_identity(pid)
def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read owner manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION or data.get("schema") != MANIFEST_SCHEMA:
        raise ManifestError(f"unsupported owner manifest {path}")
    pid, identity, state = _owner(data)
    if pid <= 0 or not identity or state not in _STATES or not isinstance(data.get("entries"), list):
        raise ManifestError(f"invalid owner identity/state in {path}")
    entries, seen = [], set()
    for raw in data["entries"]:
        if not isinstance(raw, dict):
            raise ManifestError(f"invalid entry in {path}")
        item = _entry(raw.get("pgid"), raw.get("socket"), raw.get("leader_pid"),
                      raw.get("run_id"), raw.get("leader_start_identity"))
        key = (item["pgid"], item["socket"], item["leader_pid"], item["run_id"])
        if key not in seen:
            entries.append(item)
            seen.add(key)
    data["entries"] = entries
    return data
def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
    finally:
        Path(raw).unlink(missing_ok=True)
@contextlib.contextmanager
def _lock(path: Path) -> Iterator[None]:
    with path.with_name(f"{path.name}.lock").open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
def _reap_group(pgid: int) -> None:
    if pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and _group_exists(pgid):
        time.sleep(.05)
    if _group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
def leader_pgid(pid: int, timeout: float = 2.0) -> int:
    """Wait until *pid* leads its own group, then return that pgid.

    Closes the race between Popen(start_new_session=True) returning and the
    child's setsid() landing, during which os.getpgid(pid) still reports the
    parent's group.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, PermissionError) as exc:
            raise ManifestError(f"cannot read process group of {pid}: {exc}") from exc
        if pgid == pid:
            return pgid
        if time.monotonic() >= deadline:
            raise ManifestError(f"process {pid} never became its own group leader")
        time.sleep(.01)


def _entry_signalable(item: dict[str, Any]) -> bool:
    """True only when the recorded group still holds the recorded process.

    A bare pgid is never signalled: the number alone cannot distinguish the
    original group from an unrelated one that inherited it after pid reuse.
    """
    pgid, pid = item.get("pgid"), item.get("leader_pid")
    if not isinstance(pgid, int) or not isinstance(pid, int) or pid <= 0:
        return False
    if not _exists(pid):
        return False
    recorded = item.get("leader_start_identity")
    observed = process_start_identity(pid)
    if not isinstance(recorded, str) or not recorded or not observed or observed != recorded:
        return False
    try:
        return os.getpgid(pid) == pgid
    except (ProcessLookupError, PermissionError):
        return False


def _reap_entries(data: dict[str, Any], run_id: str | None = None) -> None:
    for item in data["entries"]:
        if run_id is not None and item.get("run_id") != run_id:
            continue
        if not _entry_signalable(item):
            continue
        _reap_group(item["pgid"])
        socket = item.get("socket")
        if isinstance(socket, str) and socket:
            try:
                process = _REAL_POPEN(
                    ["tmux", "-S" if socket.startswith("/") else "-L", socket, "kill-server"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                process.communicate(timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass
            Path(item.get("socket_path") or resolve_tmux_socket(socket)).unlink(missing_ok=True)
def initialize_manifest(path: str | os.PathLike[str], run_id: str | None = None) -> str:
    target = Path(path)
    with _lock(target):
        if target.exists():
            data = _read(target)
            if not _stale(data):
                raise ManifestBusyError(f"live gate owns {target}")
            _reap_entries(data)
            target.unlink(missing_ok=True)
        identity = process_start_identity(os.getpid())
        if identity is None:
            raise ManifestError("cannot establish gate owner process identity")
        run = run_id or uuid.uuid4().hex
        _write(target, {"schema": MANIFEST_SCHEMA, "version": MANIFEST_VERSION,
                        "owner_pid": os.getpid(), "owner_start_identity": identity,
                        "owner_state": ACTIVE, "run_id": run, "entries": []})
    return run
def record(path: str | os.PathLike[str], pgid: int | str | None,
           socket: str | os.PathLike[str] | None,
           leader_pid: int | str | None = None) -> bool:
    target = Path(path)
    checked = _pgid(pgid)
    if checked is not None and checked == os.getpgrp():
        raise ManifestError(
            f"refusing to record the recorder's own process group: {checked}")
    with _lock(target):
        if not target.exists():
            raise ManifestError(f"owner manifest is missing: {target}")
        data = _read(target)
        if _stale(data):
            _reap_entries(data)
            raise ManifestBusyError(f"owner manifest became stale: {target}")
        if _owner(data)[2] != ACTIVE:
            raise ManifestBusyError(f"gate owner is already stopping: {target}")
        leader = _pgid(leader_pid) if leader_pid is not None else checked
        identity = process_start_identity(leader) if leader is not None else None
        if checked is None or leader is None or not identity:
            raise ManifestError("an owned entry requires a group leader and start identity")
        item = _entry(checked, socket, leader, data.get("run_id"), identity)
        key = (item["pgid"], item["socket"], item["leader_pid"], item["run_id"])
        if key not in {(x["pgid"], x["socket"], x["leader_pid"], x["run_id"]) for x in data["entries"]}:
            data["entries"].append(item)
            _write(target, data)
    return True
def mark_owner_stopping(path: str | os.PathLike[str]) -> bool:
    target = Path(path)
    with _lock(target):
        if not target.exists():
            return False
        data = _read(target)
        if not _current(data):
            return False
        if data["owner_state"] == STOPPING:
            return True
        data["owner_state"] = STOPPING
        _write(target, data)
    return True
def reap_manifest(path: str | os.PathLike[str], run_id: str | None = None) -> bool:
    """Reap owned entries.

    With *run_id* the caller reaps only what its own run recorded and leaves
    every other run's entries — and the manifest file — in place.
    """
    target = Path(path)
    with _lock(target):
        if not target.exists():
            return False
        data = _read(target)
        current = _current(data) and _owner(data)[2] == STOPPING
        if not current and not _stale(data):
            return False
        _reap_entries(data, run_id)
        if run_id is None:
            target.unlink(missing_ok=True)
            return True
        survivors = [x for x in data["entries"] if x.get("run_id") != run_id]
        if survivors:
            data["entries"] = survivors
            _write(target, data)
        else:
            target.unlink(missing_ok=True)
    return True
