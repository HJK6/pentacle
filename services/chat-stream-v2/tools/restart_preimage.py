#!/usr/bin/env python3
"""Stationary SQLite main/whole-WAL capture; see ../docs/restart-preimage.md.

No service control, source SQLite connections, copy fallback, or recovery install.
The caller owns complete writer exclusion and all command acknowledgments.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
from typing import Any, Mapping
import uuid


MINIMUM_LABELS = frozenset({"sessions", "notifications", "assets", "archive", "schedules"})
LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
HEX256 = re.compile(r"[0-9a-f]{64}\Z")
STAT_KEYS = {"dev", "ino", "size", "mtime_ns", "ctime_ns"}


class PreimageError(ValueError):
    """Fail closed; existing files are diagnostic evidence, not an acknowledgment."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreimageError(message)


def _json_bytes(record: Mapping[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value: str) -> None:
    raise PreimageError(f"non-finite JSON number: {value}")


def _parse(data: bytes) -> dict[str, Any]:
    record = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    _require(type(record) is dict, "record must be a JSON object")
    return record


def _absolute(value: Any, *, missing_leaf: bool = False) -> Path:
    _require(isinstance(value, (str, os.PathLike)), "path must be absolute")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "path must be absolute without '..'")
    # lstat every component; resolve() alone would hide symlinks.
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing_leaf and part == path:
                continue
            raise
        _require(not stat.S_ISLNK(info.st_mode), f"symlink forbidden: {part}")
        if part != path:
            _require(stat.S_ISDIR(info.st_mode), f"parent is not a directory: {part}")
    return path


def _regular(path: Path) -> os.stat_result:
    _absolute(path)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode), f"not a regular file: {path}")
    return info


def _stat_tuple(info: os.stat_result) -> dict[str, int]:
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _identity(info: os.stat_result) -> dict[str, int]:
    return {"dev": info.st_dev, "ino": info.st_ino}


def _valid_stat(value: Any) -> None:
    _require(type(value) is dict and set(value) == STAT_KEYS, "incomplete stat tuple")
    _require(all(type(v) is int for v in value.values()), "stat values must be integers")
    _require(value["dev"] >= 0 and value["ino"] > 0 and value["size"] >= 0,
             "invalid stat tuple")


def _valid_identity(value: Any) -> None:
    _require(type(value) is dict and set(value) == {"dev", "ino"}, "invalid directory/source identity")
    _require(type(value["dev"]) is int and type(value["ino"]) is int
             and value["dev"] >= 0 and value["ino"] > 0, "invalid directory/source identity")


def _read_record(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _parse(_json_bytes(value))
    return _parse(_regular_read(_absolute(value)))


def _regular_read(path: Path) -> bytes:
    _regular(path)
    with path.open("rb") as stream:
        return stream.read()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_file(path: Path) -> None:
    _regular(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    _absolute(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def native_clone(source: Path, destination: Path) -> None:
    """Darwin clonefile(flags=0), never a byte-copy/hardlink fallback."""
    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "native Darwin clonefile is required")
    _regular(source)
    _absolute(destination, missing_leaf=True)
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, "clone destination exists", str(destination))
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        clonefile = libc.clonefile
    except (OSError, AttributeError) as error:
        raise OSError(errno.ENOTSUP, "Darwin clonefile is unavailable") from error
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    clonefile.restype = ctypes.c_int
    ctypes.set_errno(0)
    if clonefile(os.fsencode(source), os.fsencode(destination), 0) != 0:
        code = ctypes.get_errno() or errno.EIO
        raise OSError(code, os.strerror(code), str(destination))


def pid_absent(pid: int) -> bool:
    """Only ESRCH proves absence; permission denied/unknown is not absent."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _publish(path: Path, record: Mapping[str, Any]) -> str:
    """Visibility at replace is NOT adoption: the following dir fsync may fail."""
    _require(not os.path.lexists(path), f"receipt already exists: {path.name}")
    data = _json_bytes(record)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    # Preserve the temp file on error, and preserve a visible receipt after rename.
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(name, path)
    _sync_directory(path.parent)
    return _hash_bytes(data)


def _result(capture_id: str, receipt_sha256: str) -> dict[str, Any]:
    return {"eligible": True, "capture_id": capture_id, "receipt_sha256": receipt_sha256}


def _validate_inventory(record: dict[str, Any]) -> None:
    _require(record.get("schema") == "restart-preimage.inventory.v1", "unknown inventory schema")
    _require(record.get("compatible_restart") is True, "compatible_restart must be true")
    participants = record.get("participants")
    _require(type(participants) is dict and bool(participants), "participants must be nonempty")
    _require(all(type(k) is str and LABEL.fullmatch(k) for k in participants), "unsafe participant label")
    _require(MINIMUM_LABELS <= participants.keys(), "missing minimum participant")
    required = record.get("required_labels")
    _require(type(required) is list and bool(required), "required_labels must be nonempty")
    _require(all(type(k) is str and LABEL.fullmatch(k) for k in required), "unsafe required label")
    _require(len(set(required)) == len(required), "duplicate required label")
    _require(set(required) <= participants.keys(), "missing required participant")
    for value in participants.values():
        _require(type(value) is str and Path(value).is_absolute() and ".." not in Path(value).parts,
                 "participant path must be absolute without '..'")


def _probe_clone(source_directory: Path, destination_directory: Path) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".restart-preimage-probe-", dir=source_directory)
    source = Path(name)
    target = destination_directory / (".restart-preimage-probe-" + uuid.uuid4().hex)
    payload = b"restart-preimage native clone capability\n"
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        native_clone(source, target)
        _require(_regular_read(target) == payload, "clone probe bytes differ")
        _require(_identity(_regular(source)) != _identity(_regular(target)), "clone probe is an alias")
        _sync_file(target)
    finally:
        # Only uniquely named, owned probe entries are removed. Never remove DEST.
        source.unlink()
        if os.path.lexists(target):
            target.unlink()


def prepare(inventory: Mapping[str, Any] | str | Path, dest: str | Path) -> dict[str, Any]:
    """Validate while serving and publish PREPARED; never open a source database."""
    record = _read_record(inventory)
    _validate_inventory(record)
    destination = _absolute(dest, missing_leaf=True)
    _require(not os.path.lexists(destination), "destination already exists")
    parent_info = destination.parent.lstat()
    _require(stat.S_ISDIR(parent_info.st_mode), "destination parent is not a directory")
    # Compare filesystem identities, not spelling: // and case-insensitive
    # directory aliases can defeat lexical is_relative_to/realpath checks.
    ancestor_ids = [_identity(parent.lstat()) for parent in destination.parents]
    seen_paths: set[str] = set()
    seen_ids: set[tuple[int, int]] = set()
    source_ids: dict[str, dict[str, int]] = {}
    devices: dict[int, Path] = {}
    for label, name in sorted(record["participants"].items()):
        path = _absolute(name)
        info = _regular(path)
        _require(_identity(path.parent.lstat()) not in ancestor_ids,
                 "destination is inside a source directory")
        identity = (info.st_dev, info.st_ino)
        _require(str(path) not in seen_paths and identity not in seen_ids, "duplicate participant path/inode")
        _require(info.st_dev == parent_info.st_dev, "source and destination must be on the same device")
        seen_paths.add(str(path))
        seen_ids.add(identity)
        source_ids[label] = _identity(info)
        devices.setdefault(info.st_dev, path.parent)
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    for name in ("raw", "normalized"):
        (destination / name).mkdir(mode=0o700)
        os.chmod(destination / name, 0o700)
    # Establish the parent entry before acknowledging any preparation.
    _sync_directory(destination.parent)
    for source_directory in devices.values():
        _probe_clone(source_directory, destination)
    for name in ("raw", "normalized"):
        _probe_clone(destination, destination / name)
        _sync_directory(destination / name)
    prepared = {
        "schema": "restart-preimage.prepared.v1", "capture_id": str(uuid.uuid4()),
        "destination": str(destination), "inventory": record,
        "inventory_sha256": _hash_bytes(_json_bytes(record)), "sources": source_ids,
        "directories": {name: _identity((destination / name).lstat())
                        for name in (".", "raw", "normalized")},
    }
    digest = _publish(destination / "PREPARED.json", prepared)
    return _result(prepared["capture_id"], digest)


def _capture_id(value: Any) -> None:
    _require(type(value) is str, "missing capture_id")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise PreimageError("invalid capture_id") from error
    _require(str(parsed) == value, "invalid capture_id")


def _check_directories(prepared: dict[str, Any]) -> Path:
    destination = _absolute(prepared["destination"])
    directories = prepared.get("directories")
    _require(type(directories) is dict and set(directories) == {".", "raw", "normalized"},
             "incomplete prepared directory identities")
    for name, expected in directories.items():
        _valid_identity(expected)
        path = _absolute(destination / name)
        info = path.lstat()
        _require(stat.S_ISDIR(info.st_mode) and _identity(info) == expected, "prepared directory changed")
        _require(stat.S_IMODE(info.st_mode) == 0o700, "capture directories must remain mode 0700")
    return destination


def _load_prepared(value: Mapping[str, Any] | str | Path) -> tuple[dict[str, Any], bytes, Path]:
    prepared = _read_record(value)
    _require(prepared.get("schema") == "restart-preimage.prepared.v1", "unknown prepared schema")
    _capture_id(prepared.get("capture_id"))
    _require(type(prepared.get("destination")) is str, "missing prepared destination")
    inventory = prepared.get("inventory")
    _require(type(inventory) is dict, "missing prepared inventory")
    _validate_inventory(inventory)
    _require(prepared.get("inventory_sha256") == _hash_bytes(_json_bytes(inventory)), "inventory digest mismatch")
    sources = prepared.get("sources")
    _require(type(sources) is dict and set(sources) == set(inventory["participants"]), "incomplete prepared sources")
    for identity in sources.values():
        _valid_identity(identity)
    destination = _check_directories(prepared)
    data = _regular_read(destination / "PREPARED.json")
    _require(_parse(data) == prepared, "prepared record does not match its persisted receipt")
    if not isinstance(value, Mapping):
        _require(_absolute(value) == destination / "PREPARED.json", "wrong prepared receipt path")
    return prepared, data, destination


def _validate_exclusion(exclusion: dict[str, Any], capture_id: str) -> None:
    _require(exclusion.get("schema") == "restart-preimage.exclusion.v1", "unknown exclusion schema")
    _require(exclusion.get("capture_id") == capture_id, "exclusion capture_id mismatch")
    _require(type(exclusion.get("fence_ref")) is str and bool(exclusion["fence_ref"].strip()), "missing fence_ref")
    timestamp = exclusion.get("stopped_at")
    _require(type(timestamp) is str and bool(timestamp.strip()), "missing stopped_at timestamp")
    try:
        parsed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise PreimageError("invalid stopped_at timestamp") from error
    _require(parsed.tzinfo is not None, "stopped_at must include a timezone")
    pids = exclusion.get("writer_pids")
    _require(type(pids) is list and bool(pids), "writer_pids must be nonempty")
    _require(all(type(pid) is int and pid > 1 for pid in pids), "writer_pids must be integers >1")
    _require(len(set(pids)) == len(pids), "writer_pids must be unique")
    _require(exclusion.get("all_configured_writers_excluded") is True, "complete writer exclusion is required")


def _check_pids(exclusion: dict[str, Any]) -> None:
    for pid in exclusion["writer_pids"]:
        _require(pid_absent(pid) is True, f"writer PID is live or absence is unknown: {pid}")


def _source_inventory(prepared: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    identities: set[tuple[int, int]] = set()
    paths: set[str] = set()
    for label, name in sorted(prepared["inventory"]["participants"].items()):
        main = _absolute(name)
        entry: dict[str, Any] = {}
        # An empty or dangling-symlink journal is still present and is rejected.
        _require(not os.path.lexists(str(main) + "-journal"), "rollback journal sidecar exists")
        entry["journal"] = None
        for kind, suffix in (("main", ""), ("wal", "-wal"), ("shm", "-shm")):
            path = Path(str(main) + suffix)
            if kind != "main" and not os.path.lexists(path):
                entry[kind] = None
                continue
            info = _regular(path)
            identity = (info.st_dev, info.st_ino)
            _require(str(path) not in paths and identity not in identities, "duplicate source/sidecar identity")
            paths.add(str(path))
            identities.add(identity)
            if kind == "main":
                _require(_identity(info) == prepared["sources"][label], "source inode/device replaced after prepare")
            _require(info.st_dev == prepared["directories"]["."]["dev"], "source/sidecar device differs")
            entry[kind] = _stat_tuple(info)
        snapshot[label] = entry
    return snapshot


def _expected_files(prepared: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    participants = prepared["inventory"]["participants"]
    _require(type(snapshot) is dict and set(snapshot) == set(participants), "incomplete source inventory")
    result = []
    source_ids: set[tuple[int, int]] = set()
    source_paths: set[str] = set()
    for label, source in sorted(participants.items()):
        entry = snapshot[label]
        _require(type(entry) is dict and set(entry) == {"main", "wal", "shm", "journal"}, "incomplete sidecar states")
        _require(entry["journal"] is None, "captured rollback journal is forbidden")
        for kind in ("main", "wal", "shm"):
            value = entry[kind]
            if value is None:
                _require(kind != "main", "missing main source stat")
                continue
            _valid_stat(value)
            identity = (value["dev"], value["ino"])
            path = source + {"main": "", "wal": "-wal", "shm": "-shm"}[kind]
            _require(identity not in source_ids and path not in source_paths, "duplicate captured source/sidecar identity")
            source_ids.add(identity)
            source_paths.add(path)
            _require(value["dev"] == prepared["directories"]["."]["dev"], "captured source device mismatch")
            if kind == "main":
                _require({k: value[k] for k in ("dev", "ino")} == prepared["sources"][label], "captured main identity mismatch")
            if kind != "shm":
                raw = f"raw/{label}.db" + ("-wal" if kind == "wal" else "")
                result.append({"label": label, "kind": kind, "source": path,
                               "raw": raw, "source_stat": value})
    return result


def capture(prepared: Mapping[str, Any] | str | Path,
            exclusion: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Clone a complete stationary set; no database hash or SQLite call occurs."""
    start = time.monotonic_ns()
    record, prepared_bytes, destination = _load_prepared(prepared)
    _require(not os.path.lexists(destination / "CAPTURE.json"), "capture receipt already exists; prepare a fresh destination")
    _require(not any((destination / "raw").iterdir()), "partial raw capture is not resumable")
    _require(not any((destination / "normalized").iterdir())
             and not os.path.lexists(destination / "VERIFIED.json"), "destination was already used")
    fence = _read_record(exclusion)
    _validate_exclusion(fence, record["capture_id"])
    _check_pids(fence)
    before = _source_inventory(record)  # All participants checked before ANY clone.
    files = _expected_files(record, before)
    for entry in files:
        target = destination / entry["raw"]
        native_clone(Path(entry["source"]), target)
        _regular(target)
        os.chmod(target, 0o400)
        _sync_file(target)
    _sync_directory(destination / "raw")
    raw_ids: set[tuple[int, int]] = set()
    for entry in files:
        raw = _stat_tuple(_regular(destination / entry["raw"]))
        _require(raw["size"] == entry["source_stat"]["size"], "clone size differs from source")
        identity = (raw["dev"], raw["ino"])
        _require(identity not in raw_ids and identity != (entry["source_stat"]["dev"], entry["source_stat"]["ino"]), "raw alias detected")
        raw_ids.add(identity)
        entry["raw_stat"] = raw
    _require(_source_inventory(record) == before, "source stat/sidecar inventory changed during capture")
    _check_pids(fence)
    _check_directories(record)
    end = time.monotonic_ns()
    receipt = {
        "schema": "restart-preimage.capture.v1", "state": "captured_unverified",
        "capture_id": record["capture_id"], "destination": str(destination),
        "prepared_sha256": _hash_bytes(prepared_bytes), "exclusion": fence,
        "source_inventory": before, "files": files,
        "capture_timing": {"start_ns": start, "end_ns": end, "duration_ns": end - start},
    }
    digest = _publish(destination / "CAPTURE.json", receipt)
    return _result(record["capture_id"], digest)


def _validate_timing(value: Any) -> None:
    _require(type(value) is dict and set(value) == {"start_ns", "end_ns", "duration_ns"}, "invalid stage timing")
    _require(all(type(v) is int and v >= 0 for v in value.values()), "invalid stage timing values")
    _require(value["end_ns"] >= value["start_ns"]
             and value["duration_ns"] == value["end_ns"] - value["start_ns"], "inconsistent stage timing")


def _load_capture(value: Mapping[str, Any] | str | Path) -> tuple[dict[str, Any], bytes, dict[str, Any], Path]:
    receipt = _read_record(value)
    _require(receipt.get("schema") == "restart-preimage.capture.v1"
             and receipt.get("state") == "captured_unverified", "invalid capture schema/state")
    _require(type(receipt.get("destination")) is str, "missing capture destination")
    destination = _absolute(receipt["destination"])
    prepared, prepared_bytes, _ = _load_prepared(destination / "PREPARED.json")
    _require(receipt.get("capture_id") == prepared["capture_id"], "capture_id mismatch")
    _require(receipt.get("prepared_sha256") == _hash_bytes(prepared_bytes), "prepared receipt digest mismatch")
    fence = receipt.get("exclusion")
    _require(type(fence) is dict, "missing exclusion record")
    _validate_exclusion(fence, prepared["capture_id"])
    _validate_timing(receipt.get("capture_timing"))
    expected = _expected_files(prepared, receipt.get("source_inventory"))
    files = receipt.get("files")
    _require(type(files) is list and len(files) == len(expected), "incomplete capture file list")
    raw_ids: set[tuple[int, int]] = set()
    source_ids = {(item["source_stat"]["dev"], item["source_stat"]["ino"]) for item in expected}
    for actual, wanted in zip(files, expected):
        _require(type(actual) is dict and set(actual) == set(wanted) | {"raw_stat"}, "invalid capture file record")
        _require({k: actual[k] for k in wanted} == wanted, "capture mapping differs from complete inventory")
        raw = actual["raw_stat"]
        _valid_stat(raw)
        identity = (raw["dev"], raw["ino"])
        _require(raw["size"] == wanted["source_stat"]["size"]
                 and raw["dev"] == prepared["directories"]["raw"]["dev"], "invalid raw size/device")
        _require(identity not in raw_ids and identity not in source_ids, "raw baseline aliases another file")
        raw_ids.add(identity)
    data = _regular_read(destination / "CAPTURE.json")
    _require(_parse(data) == receipt, "capture record differs from persisted receipt")
    if not isinstance(value, Mapping):
        _require(_absolute(value) == destination / "CAPTURE.json", "wrong capture receipt path")
    return receipt, data, prepared, destination


def _check_raw(receipt: dict[str, Any], destination: Path) -> None:
    expected = {Path(entry["raw"]).name for entry in receipt["files"]}
    _require({path.name for path in (destination / "raw").iterdir()} == expected, "raw directory contents differ")
    # Check ALL files before the caller hashes the FIRST one.
    for entry in receipt["files"]:
        info = _regular(destination / entry["raw"])
        _require(_stat_tuple(info) == entry["raw_stat"], "raw metadata differs from capture baseline")
        _require(stat.S_IMODE(info.st_mode) == 0o400, "raw file must remain mode 0400")


def _raw_hashes(receipt: dict[str, Any], destination: Path) -> dict[str, str]:
    return {entry["raw"]: _sha256_file(destination / entry["raw"]) for entry in receipt["files"]}


def _integrity(connection: sqlite3.Connection) -> None:
    _require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "normalized integrity_check failed")


def _normalize(source: Path, target: Path) -> None:
    _require(not os.path.lexists(target), "normalized destination exists")
    # Both connections are owned outputs. Raw/source DBs are never opened here.
    source_connection = sqlite3.connect(str(source))
    try:
        target_connection = sqlite3.connect(str(target))
        try:
            source_connection.backup(target_connection)
            target_connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            _integrity(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()
    _require(not any(os.path.lexists(str(target) + suffix) for suffix in ("-wal", "-shm", "-journal")),
             "normalized database retains a sidecar")
    os.chmod(target, 0o400)
    _sync_file(target)


def _timing(start: int) -> dict[str, int]:
    end = time.monotonic_ns()
    return {"start_ns": start, "end_ns": end, "duration_ns": end - start}


def _validate_verified(verified: dict[str, Any], receipt: dict[str, Any],
                       receipt_bytes: bytes, prepared: dict[str, Any]) -> None:
    _require(verified.get("schema") == "restart-preimage.verified.v1" and verified.get("state") == "verified", "invalid verified schema/state")
    _require(verified.get("capture_id") == receipt["capture_id"]
             and verified.get("capture") == receipt
             and verified.get("capture_sha256") == _hash_bytes(receipt_bytes), "verified capture binding differs")
    hashes = verified.get("raw_sha256")
    _require(type(hashes) is dict and set(hashes) == {entry["raw"] for entry in receipt["files"]}, "incomplete verified raw hashes")
    _require(all(type(value) is str and HEX256.fullmatch(value) for value in hashes.values()), "invalid raw hash")
    normalized = verified.get("normalized")
    labels = sorted(prepared["inventory"]["participants"])
    _require(type(normalized) is list and len(normalized) == len(labels), "incomplete normalized file list")
    for entry, label in zip(normalized, labels):
        _require(type(entry) is dict and set(entry) == {"label", "path", "sha256", "size"}, "invalid normalized record")
        _require(entry["label"] == label and entry["path"] == f"normalized/{label}.db", "invalid normalized mapping")
        _require(type(entry["sha256"]) is str and HEX256.fullmatch(entry["sha256"]), "invalid normalized hash")
        _require(type(entry["size"]) is int and entry["size"] >= 0, "invalid normalized size")
    _require(type(verified.get("sqlite_version")) is str and bool(verified["sqlite_version"]), "missing SQLite version")
    timings = verified.get("timings")
    _require(type(timings) is dict and set(timings) == {"initial_raw_hash", "normalization", "normalized_hash", "final_raw_hash", "verification"}, "incomplete stage timings")
    for timing in timings.values():
        _validate_timing(timing)
    stages = [timings[key] for key in ("initial_raw_hash", "normalization", "normalized_hash", "final_raw_hash")]
    _require(all(a["end_ns"] <= b["start_ns"] for a, b in zip(stages, stages[1:])), "stage timings overlap")
    _require(timings["verification"]["start_ns"] <= stages[0]["start_ns"]
             and stages[-1]["end_ns"] <= timings["verification"]["end_ns"], "stage timings exceed verification")


def _check_normalized(verified: dict[str, Any], destination: Path) -> None:
    expected = {Path(entry["path"]).name for entry in verified["normalized"]}
    _require({path.name for path in (destination / "normalized").iterdir()} == expected, "normalized directory contents differ")
    for entry in verified["normalized"]:
        path = destination / entry["path"]
        before = _stat_tuple(_regular(path))
        _require(before["size"] == entry["size"], "normalized size differs")
        _require(_sha256_file(path) == entry["sha256"], "normalized hash differs")
        _require(_stat_tuple(_regular(path)) == before, "normalized metadata changed while hashing")


def _resync(verified: dict[str, Any], receipt: dict[str, Any], destination: Path) -> None:
    for entry in receipt["files"]:
        _sync_file(destination / entry["raw"])
    for entry in verified["normalized"]:
        _sync_file(destination / entry["path"])
    for name in ("PREPARED.json", "CAPTURE.json", "VERIFIED.json"):
        _sync_file(destination / name)
    for path in (destination / "raw", destination / "normalized", destination, destination.parent):
        _sync_directory(path)


def verify(capture: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Normalize after serving resumes. This cannot acknowledge an earlier capture."""
    start = time.monotonic_ns()
    receipt, receipt_bytes, prepared, destination = _load_capture(capture)
    _check_raw(receipt, destination)
    verified_path = destination / "VERIFIED.json"
    if os.path.lexists(verified_path):
        data = _regular_read(verified_path)
        verified = _parse(data)
        _validate_verified(verified, receipt, receipt_bytes, prepared)
        _require(_raw_hashes(receipt, destination) == verified["raw_sha256"], "raw hash differs")
        _check_raw(receipt, destination)
        _check_normalized(verified, destination)
        _require(_raw_hashes(receipt, destination) == verified["raw_sha256"], "raw hash changed during reentry")
        _check_raw(receipt, destination)
        _resync(verified, receipt, destination)
        _check_directories(prepared)
        _check_raw(receipt, destination)
        _require(_regular_read(verified_path) == data, "verified receipt changed during reentry")
        return _result(receipt["capture_id"], _hash_bytes(data))
    _require(not any((destination / "normalized").iterdir()), "partial normalization is not resumable")
    timings: dict[str, Any] = {}
    stage = time.monotonic_ns()
    hashes = _raw_hashes(receipt, destination)
    _check_raw(receipt, destination)
    timings["initial_raw_hash"] = _timing(stage)
    stage = time.monotonic_ns()
    scratch = Path(tempfile.mkdtemp(prefix="scratch-", dir=destination))
    os.chmod(scratch, 0o700)
    for entry in receipt["files"]:
        target = scratch / Path(entry["raw"]).name
        native_clone(destination / entry["raw"], target)
        _regular(target)
        os.chmod(target, 0o600)  # clonefile carries raw's 0400 mode.
    for label in sorted(prepared["inventory"]["participants"]):
        _normalize(scratch / f"{label}.db", destination / "normalized" / f"{label}.db")
    _sync_directory(destination / "normalized")
    timings["normalization"] = _timing(stage)
    stage = time.monotonic_ns()
    normalized = []
    for label in sorted(prepared["inventory"]["participants"]):
        path = destination / "normalized" / f"{label}.db"
        before = _stat_tuple(_regular(path))
        digest = _sha256_file(path)
        _require(_stat_tuple(_regular(path)) == before, "normalized metadata changed during hashing")
        normalized.append({"label": label, "path": f"normalized/{label}.db", "size": before["size"], "sha256": digest})
    timings["normalized_hash"] = _timing(stage)
    stage = time.monotonic_ns()
    _check_raw(receipt, destination)
    _require(_raw_hashes(receipt, destination) == hashes, "raw bytes changed during verification")
    _check_raw(receipt, destination)
    timings["final_raw_hash"] = _timing(stage)
    _check_directories(prepared)
    timings["verification"] = _timing(start)
    verified = {
        "schema": "restart-preimage.verified.v1", "state": "verified",
        "capture_id": receipt["capture_id"], "capture_sha256": _hash_bytes(receipt_bytes),
        "capture": receipt, "raw_sha256": hashes, "normalized": normalized,
        "sqlite_version": sqlite3.sqlite_version, "timings": timings,
    }
    digest = _publish(verified_path, verified)
    return _result(receipt["capture_id"], digest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("inventory")
    prep.add_argument("destination")
    cap = commands.add_parser("capture")
    cap.add_argument("prepared")
    cap.add_argument("exclusion")
    ver = commands.add_parser("verify")
    ver.add_argument("capture")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.inventory, args.destination)
        elif args.command == "capture":
            result = capture(args.prepared, args.exclusion)
        else:
            result = verify(args.capture)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"eligible": False, "error": f"{type(error).__name__}: {error}"}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
