"""Small, conservative ownership manifest for one v2 gate invocation."""
from __future__ import annotations
import contextlib
import errno
import fcntl
import json
import os
import re
import select
import stat
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
_SOCKET_FIELDS = {"nonce", "server_pid", "server_start_identity", "root_dev", "root_ino", "socket_dev", "socket_ino"}
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
           leader_start_identity: str | None = None,
           socket_owner: dict[str, Any] | None = None,
           socket_path: str | None = None) -> dict[str, Any]:
    pgid, socket = _pgid(pgid), _socket(socket)
    if pgid is None and socket is None:
        raise ManifestError("an owned entry needs a process group or private socket")
    leader = _pgid(leader_pid) if leader_pid is not None else pgid
    identity = leader_start_identity if isinstance(leader_start_identity, str) and leader_start_identity else None
    item = {"pgid": pgid, "socket": socket,
            "socket_path": str(resolve_tmux_socket(socket)) if socket else None,
            "leader_pid": leader, "leader_start_identity": identity, "run_id": run_id}
    if socket_owner is not None:
        if socket_path != socket or not socket or not Path(socket).is_absolute():
            raise ManifestError("bound socket must retain its absolute address")
        item["socket_owner"] = _socket_binding(socket, socket_owner)
    return item
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
    if pid <= 0 or not identity or state not in _STATES or not isinstance(data.get("entries"), list) or not data.get("run_id") or not isinstance(data["run_id"], str):
        raise ManifestError(f"invalid owner identity/state in {path}")
    entries, seen = [], set()
    for raw in data["entries"]:
        if not isinstance(raw, dict):
            raise ManifestError(f"invalid entry in {path}")
        item = _entry(raw.get("pgid"), raw.get("socket"), raw.get("leader_pid"),
                      raw.get("run_id"), raw.get("leader_start_identity"),
                      raw.get("socket_owner"), raw.get("socket_path"))
        if not isinstance(item["run_id"], str) or not item["run_id"]:
            raise ManifestError("entry has no recorded run identity")
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
class _ExitEvent:
    """One finite kernel exit event, registered before any signal/shutdown."""
    def __init__(self, pid: int) -> None:
        self.pid, self.fd, self.kq, self.terminal = pid, None, None, False
        try:
            if sys.platform.startswith("linux"):
                self.fd = os.pidfd_open(pid, 0)
            elif sys.platform == "darwin":
                self.kq = select.kqueue()
                self.terminal = self._events(self.kq.control([select.kevent(
                    pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                    fflags=select.KQ_NOTE_EXIT)], 1, 0))
            else:
                raise ManifestError("kernel exit events unavailable")
        except ProcessLookupError:
            # ESRCH proves the recorded instance has already terminated.
            self.terminal = True
        except (OSError, AttributeError, ManifestError) as exc:
            self.close()
            raise ManifestError(f"cannot register exit event for {pid}: {exc}") from exc

    def _events(self, events: list[Any]) -> bool:
        for event in events:
            matching = event.ident == self.pid and getattr(event, "filter", None) == select.KQ_FILTER_PROC
            if event.flags & select.KQ_EV_ERROR:
                if matching and event.data == errno.ESRCH:
                    return True
                raise ManifestError(f"exit-event registration failed: {event.data}")
            if matching and event.fflags & select.KQ_NOTE_EXIT:
                return True
        return False

    def wait(self, deadline: float) -> None:
        try:
            remaining = max(0, deadline - time.monotonic())
            if not self.terminal:
                if self.fd is not None:
                    self.terminal = bool(select.select([self.fd], [], [], remaining)[0])
                elif self.kq is not None:
                    self.terminal = self._events(self.kq.control(None, 1, remaining))
            if not self.terminal:
                raise ManifestError(f"exit event not observed for {self.pid}")
        except OSError as exc:
            raise ManifestError(f"exit-event wait failed for {self.pid}: {exc}") from exc

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.kq is not None:
            self.kq.close()
            self.kq = None


def _matching_leader(pgid: int, pid: int, identity: str) -> bool:
    if not _exists(pid):
        return False
    if not identity or process_start_identity(pid) != identity:
        raise ManifestError("live leader identity is unreadable or recycled")
    try:
        if os.getpgid(pid) != pgid:
            raise ManifestError("recorded leader is in a different process group")
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise ManifestError("cannot read recorded leader process group") from exc
    return True


def _reap_group(pgid: int, *, leader_pid: int, identity: str, deadline: float) -> None:
    if pgid == os.getpgrp():
        raise ManifestError("refusing to reap the caller's own process group")
    event = _ExitEvent(leader_pid)
    try:
        # The entry reaper has already checked ownership. A terminal witness
        # permits only the final absence check, never another group signal.
        if not event.terminal and _matching_leader(pgid, leader_pid, identity):
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise ManifestError("group TERM denied") from exc
            # Preserve the existing TERM grace; no additional group polling.
            grace = min(deadline, time.monotonic() + 1)
            while time.monotonic() < grace and _group_exists(pgid):
                time.sleep(.05)
            if _group_exists(pgid):
                if not _matching_leader(pgid, leader_pid, identity):
                    raise ManifestError("leader disappeared before group escalation")
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise ManifestError("group KILL denied") from exc
        event.wait(deadline)
        # A leader exit is not evidence that its process group is absent.
        if _group_exists(pgid):
            raise ManifestError("original process group is still live or unobservable")
    finally:
        event.close()


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


def _socket_binding(socket: str, binding: dict[str, Any]) -> dict[str, Any]:
    path = Path(socket)
    if (not isinstance(binding, dict) or set(binding) != _SOCKET_FIELDS
            or not path.is_absolute() or path.name != "s"
            or path.parent.parent != Path("/tmp") or not path.parent.name.startswith("v2s-")
            or str(path) != socket):
        raise ManifestError("invalid private socket ownership binding")
    if not isinstance(binding["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", binding["nonce"]):
        raise ManifestError("invalid socket nonce")
    if not isinstance(binding["server_start_identity"], str) or not binding["server_start_identity"]:
        raise ManifestError("missing server start identity")
    for key in _SOCKET_FIELDS - {"nonce", "server_start_identity"}:
        value = binding[key]
        if type(value) is not int or value < (2 if key == "server_pid" else 1 if key.endswith("_ino") else 0):
            raise ManifestError(f"invalid socket identity: {key}")
    return dict(binding)


def _socket_objects(socket: str, binding: dict[str, Any]) -> tuple[int, bool]:
    root = Path(socket).parent
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        rs = os.fstat(fd)
        if (rs.st_dev, rs.st_ino, rs.st_uid, stat.S_IMODE(rs.st_mode)) != (
                binding["root_dev"], binding["root_ino"], os.getuid(), 0o700):
            raise ManifestError("private socket root identity changed")
        try:
            ss = os.stat("s", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return fd, False
        if not stat.S_ISSOCK(ss.st_mode) or (ss.st_dev, ss.st_ino) != (
                binding["socket_dev"], binding["socket_ino"]):
            raise ManifestError("private socket object identity changed")
        return fd, True
    except BaseException:
        if "fd" in locals():
            os.close(fd)
        raise


def _reap_socket(socket: str, binding: dict[str, Any], deadline: float,
                 tmux: str = "tmux") -> None:
    binding = _socket_binding(socket, binding)
    pid, identity = binding["server_pid"], binding["server_start_identity"]
    live = _exists(pid)
    if live and process_start_identity(pid) != identity:
        raise ManifestError("tmux server identity is unreadable or recycled")
    try:
        fd, present = _socket_objects(socket, binding)
    except FileNotFoundError:
        if not live:
            return  # ESRCH plus absent exclusive root: no original resource remains.
        if sys.platform == "darwin":
            # An exited, uncollected process returns EV_ERROR/ESRCH at registration.
            # Interpret it only here, after the bound exclusive root is absent.
            kernel = None
            try:
                kernel = select.kqueue()
                events = kernel.control([select.kevent(
                    pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
                    fflags=select.KQ_NOTE_EXIT)], 1, 0)
                if _exists(pid) and process_start_identity(pid) != identity:
                    raise ManifestError("tmux identity changed after event registration")
                if len(events) != 1:
                    raise ManifestError("no bound terminal registration event")
                event = events[0]
                matching = event.ident == pid and event.filter == select.KQ_FILTER_PROC
                terminal = (event.data == errno.ESRCH if event.flags & select.KQ_EV_ERROR
                            else bool(event.fflags & select.KQ_NOTE_EXIT))
                if not matching or not terminal:
                    raise ManifestError("no bound terminal registration event")
            except (OSError, AttributeError, ManifestError) as exc:
                raise ManifestError("private socket root vanished while its server is live") from exc
            finally:
                if kernel is not None:
                    kernel.close()
            return
        # Linux may retain an exited server PID until its parent collects it.
        # An absent root is terminal only with the same bound instance's exit.
        exited = _ExitEvent(pid)
        try:
            if _exists(pid) and process_start_identity(pid) != identity:
                raise ManifestError("tmux identity changed after event registration")
            exited.wait(time.monotonic())
        except ManifestError as exc:
            raise ManifestError("private socket root vanished while its server is live") from exc
        finally:
            exited.close()
        return
    except OSError as exc:
        raise ManifestError(f"cannot verify private socket objects: {exc}") from exc
    event = None
    try:
        if live:
            if not present:
                raise ManifestError("private socket vanished while its server is live")
            event = _ExitEvent(pid)
            # Revalidate the registered process and objects immediately before the
            # single connection, which pins the server evaluating this guard.
            if _exists(pid) and process_start_identity(pid) != identity:
                raise ManifestError("tmux identity changed after event registration")
            check, _ = _socket_objects(socket, binding)
            os.close(check)
            if not event.terminal:
                guard = "#{&&:#{==:#{pid}," + str(pid) + "},#{==:#{@v2_gate_nonce}," + binding["nonce"] + "}}"
                process = _REAL_POPEN([tmux, "-N", "-S", socket, "if-shell", "-F", guard,
                                       "kill-server", "run-shell 'exit 1'"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = process.communicate(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise ManifestError(f"tmux shutdown transport timed out: {stderr!r}") from exc
                if process.returncode:
                    raise ManifestError(f"tmux shutdown refused ({process.returncode}): {stdout!r} {stderr!r}")
                event.wait(deadline)
        # Never infer terminal completion from the tmux client's return code.
        # When already absent, the initial ESRCH itself is the terminal proof.
        checked, present = _socket_objects(socket, binding)
        try:
            if present:
                os.unlink("s", dir_fd=checked)
        finally:
            os.close(checked)
        root = Path(socket).parent
        # Verify the pathname still identifies the opened root before rmdir.
        rs = root.lstat()
        if (rs.st_dev, rs.st_ino) != (binding["root_dev"], binding["root_ino"]) or stat.S_ISLNK(rs.st_mode):
            raise ManifestError("private root changed before removal")
        root.rmdir()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"private tmux cleanup failed: {exc}") from exc
    finally:
        os.close(fd)
        if event is not None:
            event.close()


def _reap_entries(data: dict[str, Any], run_id: str | None = None) -> None:
    survivors, errors = [], []
    for item in data["entries"]:
        if run_id is not None and item.get("run_id") != run_id:
            survivors.append(item)
            continue
        try:
            deadline = time.monotonic() + 5
            pgid, pid = item.get("pgid"), item.get("leader_pid")
            group_error = None
            if pgid is not None:
                identity = item.get("leader_start_identity")
                if not isinstance(pid, int) or not isinstance(identity, str) or not identity:
                    raise ManifestError("missing recorded leader identity")
                # A mismatched live leader permits neither group nor socket action.
                live = _matching_leader(pgid, pid, identity)
                try:
                    if live:
                        _reap_group(pgid, leader_pid=pid, identity=identity, deadline=deadline)
                    elif _group_exists(pgid):
                        raise ManifestError("dead leader's process group remains unresolved")
                except ManifestError as exc:
                    group_error = exc
            socket = item.get("socket")
            if socket:
                binding = item.get("socket_owner")
                if not binding:
                    raise ManifestError("socket has no recorded ownership binding")
                _reap_socket(socket, binding, deadline)
            if group_error is not None:
                raise group_error
        except ManifestError as exc:
            survivors.append(item)
            errors.append(str(exc))
    data["entries"] = survivors
    if errors:
        raise ManifestError("; ".join(errors))


def initialize_manifest(path: str | os.PathLike[str], run_id: str | None = None) -> str:
    target = Path(path)
    with _lock(target):
        if target.exists():
            data = _read(target)
            if not _stale(data):
                raise ManifestBusyError(f"live gate owns {target}")
            try:
                _reap_entries(data, data["run_id"])
            except ManifestError:
                _write(target, data)
                raise
            if data["entries"]:
                _write(target, data)
                raise ManifestError("foreign entries prevent manifest replacement")
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
           leader_pid: int | str | None = None,
           socket_owner: dict[str, Any] | None = None) -> bool:
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
            try:
                _reap_entries(data, data["run_id"])
            finally:
                _write(target, data)
            raise ManifestBusyError(f"owner manifest became stale: {target}")
        if _owner(data)[2] != ACTIVE:
            raise ManifestBusyError(f"gate owner is already stopping: {target}")
        leader = _pgid(leader_pid) if leader_pid is not None else checked
        identity = process_start_identity(leader) if leader is not None else None
        if checked is None or leader is None or not identity:
            raise ManifestError("an owned entry requires a group leader and start identity")
        if socket and socket_owner is None:
            raise ManifestError("new socket records require an ownership binding")
        item = _entry(checked, socket, leader, data.get("run_id"), identity,
                      socket_owner, str(socket) if socket_owner else None)
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
        try:
            _reap_entries(data, run_id if run_id is not None else data["run_id"])
        except ManifestError:
            _write(target, data)
            raise
        if data["entries"]:
            _write(target, data)
            if run_id is None:
                raise ManifestError("foreign entries prevent manifest deletion")
        else:
            target.unlink(missing_ok=True)
    return True
