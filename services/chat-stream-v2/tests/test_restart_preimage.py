"""Synthetic restart-preimage proof; injected copies are not native/speed proof."""

import importlib

import pytest


@pytest.mark.parametrize("name", ["prepare", "capture", "verify"])
def test_public_api_feature_absence_red(name):
    """Pinned-base RED is the missing feature, not corruption or a timing claim."""
    helper = importlib.import_module("restart_preimage")
    assert callable(getattr(helper, name))

import copy
import ctypes
import datetime as dt
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace


LABELS = ("sessions", "notifications", "assets", "archive", "schedules", "analytics")


def copy_clone(source, destination):
    """Tiny portable stand-in only; deliberately inherits native clone's mode."""
    source, destination = Path(source), Path(destination)
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst)
    os.chmod(destination, stat.S_IMODE(source.stat().st_mode))
    os.utime(destination, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))


@pytest.fixture
def helper(monkeypatch):
    module = importlib.import_module("restart_preimage")
    monkeypatch.setattr(module, "native_clone", copy_clone)
    monkeypatch.setattr(module, "pid_absent", lambda pid: True)
    return module


def document(path):
    return json.loads(Path(path).read_text())


def write_document(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True) + "\n")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_fingerprint(paths):
    result = {}
    for source in paths:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(source) + suffix)
            if os.path.lexists(path):
                info = path.lstat()
                result[str(path)] = (info.st_dev, info.st_ino, info.st_size,
                                     info.st_mtime_ns, info.st_ctime_ns,
                                     stat.S_IMODE(info.st_mode), digest(path))
            else:
                result[str(path)] = None
    return result


def canonical(path):
    """Full schema, every ordered row, and user_version; no count-only oracle."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name,tbl_name,sql"
        ).fetchall()
        rows = {}
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        ):
            quoted = '"' + name.replace('"', '""') + '"'
            values = connection.execute(f"SELECT * FROM {quoted}").fetchall()
            rows[name] = sorted(values, key=repr)
        return {"schema": schema, "rows": rows,
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0]}
    finally:
        connection.close()


@pytest.fixture
def fixture_set(tmp_path, helper):
    source = tmp_path / "source"
    source.mkdir()
    paths = {label: source / f"{label}.db" for label in LABELS}
    connections = {}
    for label, path in paths.items():
        db = sqlite3.connect(path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("PRAGMA user_version=37")
        db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, logical_id TEXT UNIQUE, payload TEXT, metadata TEXT)")
        db.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("CREATE INDEX record_payload ON records(payload)")
        db.execute("INSERT INTO records VALUES(?,?,?,?)", (1, f"{label}-accepted", "before prepare", '{"retained":true}'))
        db.execute("INSERT INTO metadata VALUES('archive_path',?)", (str(paths["archive"]),))
        db.execute("INSERT INTO metadata VALUES('pair_id','synthetic-hot-archive-001')")
        if label in {"sessions", "archive"}:
            db.execute("CREATE TABLE receipt_routes(receipt_id TEXT PRIMARY KEY, route_id TEXT, retained_id INTEGER)")
            db.execute("INSERT INTO receipt_routes VALUES('accepted-request-001','retained-route-001',901)")
        db.commit()
        if label == "assets":
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.close()  # Genuine checkpointed main with absent WAL.
        else:
            connections[label] = db
    inventory = {"schema": "restart-preimage.inventory.v1",
                 "participants": {label: str(path) for label, path in paths.items()},
                 "required_labels": list(LABELS), "compatible_restart": True}
    state = SimpleNamespace(source=source, paths=paths, connections=connections,
                            inventory=inventory, dest=tmp_path / "capture", root=tmp_path)
    yield state
    for db in connections.values():
        db.close()


def prepared_set(helper, state):
    result = helper.prepare(state.inventory, state.dest)
    state.prepared = state.dest / "PREPARED.json"
    state.exclusion = {"schema": "restart-preimage.exclusion.v1",
                       "capture_id": result["capture_id"],
                       "fence_ref": "synthetic-fence-001", "stopped_at": "2026-10-11T06:00:00Z",
                       "writer_pids": [40001, 40002], "all_configured_writers_excluded": True}
    return result


def captured_set(helper, state):
    prepared_set(helper, state)
    result = helper.capture(state.prepared, state.exclusion)
    state.capture = state.dest / "CAPTURE.json"
    return result


def invoke(helper, capsys, args):
    status = helper.main([str(item) for item in args])
    stdout = capsys.readouterr().out
    return status, json.loads(stdout)


def assert_result(result, receipt):
    assert result == {"eligible": True, "capture_id": document(receipt)["capture_id"],
                      "receipt_sha256": digest(receipt)}


def test_prepare_probes_only_owned_tiny_files(helper, fixture_set, monkeypatch):
    state = fixture_set
    original = source_fingerprint(state.paths.values())
    clones = []
    def clone(source, destination):
        assert ".restart-preimage-probe-" in source.name
        assert source.stat().st_size < 128
        clones.append((source, destination))
        copy_clone(source, destination)
    monkeypatch.setattr(helper, "native_clone", clone)
    monkeypatch.setattr(helper.sqlite3, "connect", lambda *a, **k: pytest.fail("prepare opened SQLite"))
    monkeypatch.setattr(helper, "_sha256_file", lambda *a: pytest.fail("prepare hashed database"))
    result = prepared_set(helper, state)
    assert len(clones) == 3  # One source device, plus raw/ and normalized/.
    assert_result(result, state.prepared)
    assert source_fingerprint(state.paths.values()) == original
    assert not list(state.source.glob(".restart-preimage-probe-*"))
    assert not list(state.dest.rglob(".restart-preimage-probe-*"))
    assert stat.S_IMODE(state.dest.stat().st_mode) == 0o700
    assert stat.S_IMODE(state.prepared.stat().st_mode) == 0o600
    assert not (state.dest / "CAPTURE.json").exists()
    prepared = document(state.prepared)
    assert prepared["inventory_sha256"] == hashlib.sha256(helper._json_bytes(state.inventory)).hexdigest()
    assert set(prepared["sources"]) == set(LABELS)


def test_frozen_oracle_all_rows_metadata_and_post_capture_isolation(helper, fixture_set):
    state = fixture_set
    prepared_set(helper, state)
    for label, path in state.paths.items():
        db = state.connections.get(label) or sqlite3.connect(path)
        for i in (2, 3, 4):
            db.execute("INSERT INTO records VALUES(?,?,?,?)", (i, f"{label}-after-{i}", "after prepare", '{"cut":"frozen"}'))
            db.commit()
        if label not in state.connections:
            db.close()
    oracle = state.root / "oracle"
    oracle.mkdir()
    for label, path in state.paths.items():
        src, dst = sqlite3.connect(path), sqlite3.connect(oracle / f"{label}.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    # No writes occur between the independent backup oracle and capture.
    frozen = source_fingerprint(state.paths.values())
    result = helper.capture(state.prepared, state.exclusion)
    state.capture = state.dest / "CAPTURE.json"
    assert_result(result, state.capture)
    assert source_fingerprint(state.paths.values()) == frozen
    receipt = document(state.capture)
    assert receipt["source_inventory"]["assets"]["wal"] is None
    assert len(receipt["files"]) == 11  # Six mains, five complete WALs.
    raw_before = {e["raw"]: digest(state.dest / e["raw"]) for e in receipt["files"]}
    for label, path in state.paths.items():
        db = state.connections.get(label) or sqlite3.connect(path)
        db.execute("INSERT INTO records VALUES(99,?,?,?)", (f"{label}-later", "not in capture", "{}"))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if label not in state.connections:
            db.close()
    later_source = source_fingerprint(state.paths.values())
    verified_result = helper.verify(state.capture)
    assert_result(verified_result, state.dest / "VERIFIED.json")
    assert source_fingerprint(state.paths.values()) == later_source
    assert raw_before == {e["raw"]: digest(state.dest / e["raw"]) for e in receipt["files"]}
    for label in LABELS:
        actual = canonical(state.dest / "normalized" / f"{label}.db")
        assert actual == canonical(oracle / f"{label}.db")
        assert len(actual["rows"]["records"]) == 4
        assert actual["user_version"] == 37
        assert ("archive_path", str(state.paths["archive"])) in actual["rows"]["metadata"]
    hot, archive = (canonical(state.dest / "normalized" / f"{label}.db") for label in ("sessions", "archive"))
    assert hot["rows"]["receipt_routes"] == archive["rows"]["receipt_routes"] == [("accepted-request-001", "retained-route-001", 901)]
    assert document(state.dest / "VERIFIED.json")["capture"] == receipt


def wal_frames(path):
    data = Path(path).read_bytes()
    assert len(data) >= 32
    page_size = struct.unpack(">I", data[8:12])[0]
    frame_size = page_size + 24
    assert (len(data) - 32) % frame_size == 0
    return [(offset, struct.unpack(">II", data[offset:offset + 8]))
            for offset in range(32, len(data), frame_size)]


def test_real_committed_wal_with_observed_uncommitted_spill(helper, fixture_set):
    state = fixture_set
    prepared_set(helper, state)
    db = state.connections["sessions"]
    db.execute("PRAGMA cache_size=1")
    db.execute("PRAGMA cache_spill=ON")
    db.execute("BEGIN IMMEDIATE")
    for i in range(100, 180):
        db.execute("INSERT INTO records VALUES(?,?,?,?)", (i, f"uncommitted-{i}", "x" * 4096, "{}"))
    frames = wal_frames(str(state.paths["sessions"]) + "-wal")
    committed = [index for index, (_, (_, commit_size)) in enumerate(frames) if commit_size]
    assert committed and committed[-1] < len(frames) - 1
    assert all(commit_size == 0 for _, (_, commit_size) in frames[committed[-1] + 1:])
    # The backup oracle uses a separate reader, not the active write transaction.
    oracle = state.root / "uncommitted-oracle.db"
    src, dst = sqlite3.connect(state.paths["sessions"]), sqlite3.connect(oracle)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    helper.capture(state.prepared, state.exclusion)
    helper.verify(state.dest / "CAPTURE.json")
    restored = canonical(state.dest / "normalized" / "sessions.db")
    assert restored == canonical(oracle)
    assert len(restored["rows"]["records"]) == 1
    db.rollback()


def test_capture_has_no_expensive_database_calls_and_uses_fresh_stats(helper, fixture_set, monkeypatch):
    state = fixture_set
    prepared_set(helper, state)
    db = state.connections["sessions"]
    db.execute("INSERT INTO records VALUES(2,'fresh','after prepare','{}')")
    db.commit()
    expected = source_fingerprint(state.paths.values())
    for name in ("_sha256_file", "_normalize", "_integrity"):
        monkeypatch.setattr(helper, name, lambda *a, _name=name, **k: pytest.fail(f"capture called {_name}"))
    monkeypatch.setattr(helper.sqlite3, "connect", lambda *a, **k: pytest.fail("capture opened SQLite"))
    result = helper.capture(state.prepared, state.exclusion)
    assert result["eligible"] is True
    assert source_fingerprint(state.paths.values()) == expected
    receipt = document(state.dest / "CAPTURE.json")
    for entry in receipt["files"]:
        assert entry["source_stat"] == helper._stat_tuple(Path(entry["source"]).stat())
        assert entry["raw_stat"] == helper._stat_tuple((state.dest / entry["raw"]).stat())
        assert entry["raw_stat"]["ino"] != entry["source_stat"]["ino"]
        assert stat.S_IMODE((state.dest / entry["raw"]).stat().st_mode) == 0o400
    timing = receipt["capture_timing"]
    assert timing["end_ns"] >= timing["start_ns"]
    assert timing["duration_ns"] == timing["end_ns"] - timing["start_ns"]


@pytest.mark.parametrize("defect", ["missing_minimum", "missing_required", "empty_required", "duplicate_required",
                                     "empty_participants", "unsafe_label", "unknown_schema", "false_compatible",
                                     "integer_compatible", "relative", "parent_traversal", "alias_path", "alias_inode",
                                     "symlink", "symlink_parent", "nonregular", "absent", "existing_dest", "inside_source"])
def test_prepare_rejects_inventory_and_path_defects(helper, fixture_set, defect):
    state = fixture_set
    inventory = copy.deepcopy(state.inventory)
    dest = state.dest
    if defect == "missing_minimum":
        inventory["participants"].pop("sessions")
    elif defect == "missing_required":
        inventory["required_labels"].append("missing")
    elif defect == "empty_required":
        inventory["required_labels"] = []
    elif defect == "duplicate_required":
        inventory["required_labels"].append("sessions")
    elif defect == "empty_participants":
        inventory["participants"] = {}
    elif defect == "unsafe_label":
        inventory["participants"]["../escape"] = inventory["participants"].pop("analytics")
    elif defect == "unknown_schema":
        inventory["schema"] = "unknown"
    elif defect in {"false_compatible", "integer_compatible"}:
        inventory["compatible_restart"] = False if defect == "false_compatible" else 1
    elif defect == "relative":
        inventory["participants"]["sessions"] = "relative.db"
    elif defect == "parent_traversal":
        inventory["participants"]["sessions"] = str(state.source / ".." / "source" / "sessions.db")
    elif defect == "alias_path":
        inventory["participants"]["analytics"] = inventory["participants"]["sessions"]
    elif defect == "alias_inode":
        path = state.source / "alias.db"
        os.link(state.paths["sessions"], path)
        inventory["participants"]["analytics"] = str(path)
    elif defect == "symlink":
        path = state.source / "alias.db"
        path.symlink_to(state.paths["sessions"])
        inventory["participants"]["analytics"] = str(path)
    elif defect == "symlink_parent":
        path = state.root / "linked-source"
        path.symlink_to(state.source, target_is_directory=True)
        inventory["participants"]["sessions"] = str(path / "sessions.db")
    elif defect == "nonregular":
        inventory["participants"]["analytics"] = str(state.source)
    elif defect == "absent":
        inventory["participants"]["analytics"] = str(state.source / "absent.db")
    elif defect == "existing_dest":
        dest.mkdir()
    elif defect == "inside_source":
        dest = state.source / "capture"
    with pytest.raises((helper.PreimageError, OSError)):
        helper.prepare(inventory, dest)
    assert not (dest / "PREPARED.json").exists()


def test_prepare_rejects_different_device(helper, fixture_set, monkeypatch):
    real_regular = helper._regular
    def regular(path):
        value = real_regular(path)
        if path == fixture_set.paths["sessions"]:
            return SimpleNamespace(st_dev=value.st_dev + 1, st_ino=value.st_ino)
        return value
    monkeypatch.setattr(helper, "_regular", regular)
    with pytest.raises(helper.PreimageError, match="same device"):
        helper.prepare(fixture_set.inventory, fixture_set.dest)
    assert not fixture_set.dest.exists()


@pytest.mark.parametrize("contents", ['{"schema":"x","schema":"y"}', '{"participants":{"a":1,"a":2}}', '{"x":NaN}', '{"x":Infinity}'])
def test_json_duplicate_keys_and_nonfinite_refused(helper, tmp_path, contents):
    path = tmp_path / "input.json"
    path.write_text(contents)
    with pytest.raises(helper.PreimageError):
        helper.prepare(path, tmp_path / "dest")


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.EXDEV, errno.EACCES, errno.EEXIST])
def test_prepare_clone_failure_no_fallback_or_receipt(helper, fixture_set, monkeypatch, code):
    def fail(*args):
        raise OSError(code, "injected native failure")
    monkeypatch.setattr(helper, "native_clone", fail)
    with pytest.raises(OSError) as caught:
        helper.prepare(fixture_set.inventory, fixture_set.dest)
    assert caught.value.errno == code
    assert not (fixture_set.dest / "PREPARED.json").exists()
    assert not list(fixture_set.source.glob(".restart-preimage-probe-*"))
    assert not list(fixture_set.dest.rglob(".restart-preimage-probe-*"))
    assert list((fixture_set.dest / "raw").iterdir()) == []


def test_native_adapter_requires_darwin_without_copy_fallback(tmp_path, monkeypatch):
    module = importlib.import_module("restart_preimage")
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"tiny")
    monkeypatch.setattr(module.sys, "platform", "linux")
    with pytest.raises(OSError) as caught:
        module.native_clone(source, target)
    assert caught.value.errno == errno.ENOTSUP
    assert not target.exists()


@pytest.mark.parametrize("code", [0, errno.EXDEV, errno.ENOTSUP, errno.EACCES, errno.EEXIST])
def test_native_darwin_adapter_flags_zero_and_errno(tmp_path, monkeypatch, code):
    module = importlib.import_module("restart_preimage")
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"tiny")
    calls = []
    class Clone:
        def __call__(self, src, dst, flags):
            calls.append((src, dst, flags))
            ctypes.set_errno(code)
            return -1 if code else 0
    clone = Clone()
    def library(name, *, use_errno):
        assert name == "/usr/lib/libSystem.B.dylib" and use_errno is True
        return SimpleNamespace(clonefile=clone)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.ctypes, "CDLL", library)
    if code:
        with pytest.raises(OSError) as caught:
            module.native_clone(source, target)
        assert caught.value.errno == code
    else:
        module.native_clone(source, target)
    assert calls == [(os.fsencode(source), os.fsencode(target), 0)]
    assert clone.argtypes == [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    assert clone.restype is ctypes.c_int
    assert not target.exists()  # Mock call only, never copied by production wrapper.


def test_native_adapter_existing_destination_and_unavailable_symbol(tmp_path, monkeypatch):
    module = importlib.import_module("restart_preimage")
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"tiny")
    target.write_bytes(b"keep")
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *a, **k: pytest.fail("should reject before libc"))
    with pytest.raises(FileExistsError):
        module.native_clone(source, target)
    assert target.read_bytes() == b"keep"
    target.unlink()
    monkeypatch.setattr(module.ctypes, "CDLL", lambda *a, **k: SimpleNamespace())
    with pytest.raises(OSError) as caught:
        module.native_clone(source, target)
    assert caught.value.errno == errno.ENOTSUP


@pytest.mark.parametrize("error,expected", [(None, False), (ProcessLookupError(), True),
                                            (PermissionError(), False), (OSError(errno.EIO, "unknown"), False)])
def test_pid_probe_only_process_absence_is_accepted(monkeypatch, error, expected):
    module = importlib.import_module("restart_preimage")
    calls = []
    def kill(pid, signal):
        calls.append((pid, signal))
        if error is not None:
            raise error
    monkeypatch.setattr(module.os, "kill", kill)
    assert module.pid_absent(40001) is expected
    assert calls == [(40001, 0)]


@pytest.mark.parametrize("defect", ["schema", "capture_id", "fence", "timestamp", "timezone", "empty_pids",
                                     "duplicate_pids", "boolean_pid", "small_pid", "string_pid", "incomplete", "integer_flag"])
def test_capture_exclusion_validation_before_any_clone(helper, fixture_set, monkeypatch, defect):
    state = fixture_set
    prepared_set(helper, state)
    fence = copy.deepcopy(state.exclusion)
    mutations = {"schema": ("schema", "wrong"), "capture_id": ("capture_id", "wrong"),
                 "fence": ("fence_ref", " "), "timestamp": ("stopped_at", "not-a-date"),
                 "timezone": ("stopped_at", "2026-10-11T06:00:00"), "empty_pids": ("writer_pids", []),
                 "duplicate_pids": ("writer_pids", [40001, 40001]), "boolean_pid": ("writer_pids", [True]),
                 "small_pid": ("writer_pids", [1]), "string_pid": ("writer_pids", ["40001"]),
                 "incomplete": ("all_configured_writers_excluded", False),
                 "integer_flag": ("all_configured_writers_excluded", 1)}
    key, value = mutations[defect]
    fence[key] = value
    monkeypatch.setattr(helper, "native_clone", lambda *a: pytest.fail("cloned invalid exclusion"))
    with pytest.raises(helper.PreimageError):
        helper.capture(state.prepared, fence)
    assert not (state.dest / "CAPTURE.json").exists()


@pytest.mark.parametrize("point", ["begin", "end"])
@pytest.mark.parametrize("outcome", [False, None, "unknown", 1])
def test_live_or_unknown_pid_begin_and_end_refused(helper, fixture_set, monkeypatch, point, outcome):
    state = fixture_set
    prepared_set(helper, state)
    calls = []
    def absent(pid):
        calls.append(pid)
        return outcome if point == "begin" or len(calls) > 2 else True
    monkeypatch.setattr(helper, "pid_absent", absent)
    with pytest.raises(helper.PreimageError, match="live or absence is unknown"):
        helper.capture(state.prepared, state.exclusion)
    assert not (state.dest / "CAPTURE.json").exists()
    assert bool(list((state.dest / "raw").iterdir())) is (point == "end")


@pytest.mark.parametrize("defect", ["empty_journal", "journal_symlink", "wal_symlink", "wal_directory", "shm_symlink",
                                     "sidecar_alias", "main_is_other_wal", "replaced_main", "missing_main"])
def test_capture_inventories_every_participant_before_any_clone(helper, fixture_set, monkeypatch, defect):
    state = fixture_set
    prepared_set(helper, state)
    source = state.paths["assets"]  # Checkpointed, last-ish label does not matter.
    if defect == "empty_journal":
        Path(str(source) + "-journal").touch()
    elif defect == "journal_symlink":
        Path(str(source) + "-journal").symlink_to(state.root / "absent")
    elif defect == "wal_symlink":
        Path(str(source) + "-wal").symlink_to(state.root / "absent")
    elif defect == "wal_directory":
        Path(str(source) + "-wal").mkdir()
    elif defect == "shm_symlink":
        Path(str(source) + "-shm").symlink_to(state.root / "absent")
    elif defect == "sidecar_alias":
        os.link(state.paths["sessions"], str(source) + "-wal")
    elif defect == "main_is_other_wal":
        # The alias appears after prepare; the original main inode remains stable.
        os.link(source, str(state.paths["schedules"]) + "-journal")
        # Journal presence alone is not the requested oracle: remove it and use WAL.
        Path(str(state.paths["schedules"]) + "-journal").unlink()
        Path(str(state.paths["schedules"]) + "-wal").unlink()
        os.link(source, str(state.paths["schedules"]) + "-wal")
    elif defect == "replaced_main":
        replacement = state.root / "replacement.db"
        copy_clone(source, replacement)
        os.replace(replacement, source)
    elif defect == "missing_main":
        source.unlink()
    monkeypatch.setattr(helper, "native_clone", lambda *a: pytest.fail("clone occurred before full inventory"))
    with pytest.raises((helper.PreimageError, OSError)):
        helper.capture(state.prepared, state.exclusion)
    assert not (state.dest / "CAPTURE.json").exists()
    assert list((state.dest / "raw").iterdir()) == []


@pytest.mark.parametrize("mutation", ["introduced_wal", "disappearing_wal", "mtime", "ctime", "introduced_journal", "shm_change"])
def test_capture_rechecks_full_source_inventory(helper, fixture_set, monkeypatch, mutation):
    state = fixture_set
    prepared_set(helper, state)
    calls = 0
    def clone(source, target):
        nonlocal calls
        copy_clone(source, target)
        calls += 1
        if calls != 11:
            return
        if mutation == "introduced_wal":
            Path(str(state.paths["assets"]) + "-wal").write_bytes(b"new wal")
        elif mutation == "disappearing_wal":
            Path(str(state.paths["sessions"]) + "-wal").unlink()
        elif mutation == "mtime":
            path = state.paths["sessions"]
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
        elif mutation == "ctime":
            path = state.paths["sessions"]
            os.chmod(path, 0o600)
        elif mutation == "introduced_journal":
            Path(str(state.paths["assets"]) + "-journal").touch()
        elif mutation == "shm_change":
            path = Path(str(state.paths["sessions"]) + "-shm")
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    monkeypatch.setattr(helper, "native_clone", clone)
    with pytest.raises(helper.PreimageError):
        helper.capture(state.prepared, state.exclusion)
    assert calls == 11
    assert not (state.dest / "CAPTURE.json").exists()
    assert len(list((state.dest / "raw").iterdir())) == 11


@pytest.mark.parametrize("after_file", range(1, 12))
def test_interrupt_after_each_capture_file_preserves_incomplete_evidence(helper, fixture_set, monkeypatch, after_file):
    state = fixture_set
    prepared_set(helper, state)
    frozen = source_fingerprint(state.paths.values())
    count = 0
    def clone(source, target):
        nonlocal count
        copy_clone(source, target)
        count += 1
        if count == after_file:
            raise KeyboardInterrupt("synthetic interruption")
    monkeypatch.setattr(helper, "native_clone", clone)
    with pytest.raises(KeyboardInterrupt):
        helper.capture(state.prepared, state.exclusion)
    assert count == after_file
    assert len(list((state.dest / "raw").iterdir())) == after_file
    assert not (state.dest / "CAPTURE.json").exists()
    assert source_fingerprint(state.paths.values()) == frozen
    monkeypatch.setattr(helper, "native_clone", lambda *a: pytest.fail("partial capture resumed"))
    with pytest.raises(helper.PreimageError, match="not resumable"):
        helper.capture(state.prepared, state.exclusion)


@pytest.mark.parametrize("stage,point,visible", [
    ("capture", "raw_file", False), ("capture", "raw_directory", False),
    ("capture", "temp_fsync", False), ("capture", "before_rename", False),
    ("capture", "after_rename", True), ("capture", "destination_directory", True),
    ("verify", "normalized_file", False), ("verify", "normalized_directory", False),
    ("verify", "temp_fsync", False), ("verify", "before_rename", False),
    ("verify", "after_rename", True), ("verify", "destination_directory", True),
])
def test_publication_fault_visibility_and_cli_nonadoption(helper, fixture_set, monkeypatch, capsys, stage, point, visible):
    state = fixture_set
    prepared_set(helper, state)
    fence_path = state.root / "exclusion.json"
    write_document(fence_path, state.exclusion)
    if stage == "verify":
        helper.capture(state.prepared, state.exclusion)
    receipt_path = state.dest / ("CAPTURE.json" if stage == "capture" else "VERIFIED.json")
    original_source = source_fingerprint(state.paths.values())
    real_sync_file, real_sync_directory = helper._sync_file, helper._sync_directory
    real_replace, real_fsync = helper.os.replace, helper.os.fsync
    if point in {"raw_file", "normalized_file"}:
        selected = "raw" if stage == "capture" else "normalized"
        def sync_file(path):
            if path.parent.name == selected:
                raise OSError(errno.EIO, "synthetic data fsync failure")
            real_sync_file(path)
        monkeypatch.setattr(helper, "_sync_file", sync_file)
    elif point in {"raw_directory", "normalized_directory", "destination_directory"}:
        target = state.dest if point == "destination_directory" else state.dest / point.removesuffix("_directory")
        def sync_directory(path):
            if path == target:
                raise OSError(errno.EIO, "synthetic directory fsync failure")
            real_sync_directory(path)
        monkeypatch.setattr(helper, "_sync_directory", sync_directory)
    elif point in {"before_rename", "after_rename"}:
        def replace(source, target):
            if Path(target) == receipt_path:
                if point == "after_rename":
                    real_replace(source, target)
                raise KeyboardInterrupt("synthetic receipt rename interruption")
            real_replace(source, target)
        monkeypatch.setattr(helper.os, "replace", replace)
    elif point == "temp_fsync":
        # A regular writable fd is the receipt temp file; data fsyncs use O_RDONLY.
        import fcntl
        def fsync(fd):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                raise OSError(errno.EIO, "synthetic receipt temp fsync failure")
            real_fsync(fd)
        monkeypatch.setattr(helper.os, "fsync", fsync)
    args = ["capture", state.prepared, fence_path] if stage == "capture" else ["verify", state.dest / "CAPTURE.json"]
    code, output = invoke(helper, capsys, args)
    assert code != 0 and output["eligible"] is False
    assert receipt_path.exists() is visible
    if visible:
        assert document(receipt_path)["state"] == ("captured_unverified" if stage == "capture" else "verified")
    assert source_fingerprint(state.paths.values()) == original_source
    assert list((state.dest / "raw").iterdir())
    if stage == "verify":
        assert list(state.dest.glob("scratch-*"))  # Diagnostic scratch is retained.


@pytest.mark.parametrize("stage", ["capture", "verify"])
def test_lost_stdout_is_nonadoptable_despite_visible_receipt(helper, fixture_set, monkeypatch, capsys, stage):
    state = fixture_set
    captured = captured_set(helper, state) if stage == "verify" else prepared_set(helper, state)
    assert captured["eligible"] is True
    fence_path = state.root / "exclusion.json"
    write_document(fence_path, state.exclusion)
    args = ["verify", state.dest / "CAPTURE.json"] if stage == "verify" else ["capture", state.prepared, fence_path]
    def broken_print(*args, **kwargs):
        raise BrokenPipeError("stdout lost")
    monkeypatch.setattr("builtins.print", broken_print)
    with pytest.raises(BrokenPipeError):
        helper.main([str(arg) for arg in args])
    assert capsys.readouterr().out == ""
    assert (state.dest / ("VERIFIED.json" if stage == "verify" else "CAPTURE.json")).exists()


def test_existing_capture_always_refuses_reentry(helper, fixture_set, monkeypatch):
    state = fixture_set
    captured_set(helper, state)
    original = (state.dest / "CAPTURE.json").read_bytes()
    monkeypatch.setattr(helper, "native_clone", lambda *a: pytest.fail("existing capture resumed"))
    with pytest.raises(helper.PreimageError, match="already exists"):
        helper.capture(state.prepared, state.exclusion)
    assert (state.dest / "CAPTURE.json").read_bytes() == original


def mutate_committed_wal(path):
    """Change a same-size row payload in a committed frame and rebuild checksums.

    This is logically changed data which remains SQLite-valid, not a malformed-WAL
    fixture that SQLite silently ignores. No production helper uses this function.
    """
    path = Path(path)
    data = bytearray(path.read_bytes())
    frames = wal_frames(path)
    committed_end = max(index for index, (_, (_, commit_size)) in enumerate(frames) if commit_size)
    old, new = b"before prepare", b"before corrupt"
    assert len(old) == len(new)
    changed = False
    page_size = struct.unpack(">I", data[8:12])[0]
    # Change every committed copy, including the payload index, so relational
    # structure remains valid. Integrity=ok is asserted below before the guard.
    for offset, _ in frames[:committed_end + 1]:
        start = offset + 24
        end = start + page_size
        page = bytes(data[start:end])
        if old in page:
            data[start:end] = page.replace(old, new)
            changed = True
    assert changed, "must mutate an actual committed row payload"
    magic = struct.unpack(">I", data[:4])[0]
    assert magic in (0x377F0682, 0x377F0683)
    byte_order = "<" if magic == 0x377F0682 else ">"
    def checksum(chunk, seed=(0, 0)):
        words = struct.unpack(byte_order + "I" * (len(chunk) // 4), chunk)
        first, second = seed
        for index in range(0, len(words), 2):
            first = (first + words[index] + second) & 0xFFFFFFFF
            second = (second + words[index + 1] + first) & 0xFFFFFFFF
        return first, second
    seed = checksum(data[:24])
    assert seed == struct.unpack(">II", data[24:32])
    for offset, _ in frames:
        seed = checksum(data[offset:offset + 8] + data[offset + 24:offset + 24 + page_size], seed)
        data[offset + 16:offset + 24] = struct.pack(">II", *seed)
    return bytes(data)


@pytest.mark.parametrize("restore_mtime", [False, True])
def test_same_size_valid_committed_wal_mutation_rejected_before_first_hash(helper, fixture_set, monkeypatch, restore_mtime):
    state = fixture_set
    captured_set(helper, state)
    raw_main = state.dest / "raw" / "sessions.db"
    raw_wal = Path(str(raw_main) + "-wal")
    original = raw_wal.stat()
    changed = mutate_committed_wal(raw_wal)
    assert len(changed) == original.st_size
    # Demonstrate SQLite would accept the changed committed payload with integrity=ok.
    disposable = state.root / "changed-but-valid"
    disposable.mkdir()
    copy_clone(raw_main, disposable / "sessions.db")
    os.chmod(disposable / "sessions.db", 0o600)
    (disposable / "sessions.db-wal").write_bytes(changed)
    db = sqlite3.connect(disposable / "sessions.db")
    try:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT payload FROM records WHERE id=1").fetchone() == ("before corrupt",)
    finally:
        db.close()
    os.chmod(raw_wal, 0o600)
    raw_wal.write_bytes(changed)
    if restore_mtime:
        os.utime(raw_wal, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.chmod(raw_wal, 0o400)
    assert raw_wal.stat().st_size == original.st_size
    assert raw_wal.stat().st_ctime_ns != original.st_ctime_ns
    if restore_mtime:
        assert raw_wal.stat().st_mtime_ns == original.st_mtime_ns
    monkeypatch.setattr(helper, "_sha256_file", lambda *a: pytest.fail("hashed before rejecting captured baseline change"))
    with pytest.raises(helper.PreimageError, match="raw metadata"):
        helper.verify(state.capture)
    assert not (state.dest / "VERIFIED.json").exists()


@pytest.mark.parametrize("mutation", ["inode", "missing", "symlink", "directory", "last_file_metadata", "extra_file"])
def test_every_raw_baseline_checked_before_first_hash(helper, fixture_set, monkeypatch, mutation):
    state = fixture_set
    captured_set(helper, state)
    files = document(state.capture)["files"]
    path = state.dest / files[-1]["raw"]  # Last entry proves this is a full pre-hash pass.
    if mutation == "inode":
        replacement = state.root / "replacement"
        copy_clone(path, replacement)
        assert replacement.stat().st_ino != path.stat().st_ino
        os.replace(replacement, path)
    elif mutation == "missing":
        path.unlink()
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(state.paths["sessions"])
    elif mutation == "directory":
        path.unlink()
        path.mkdir()
    elif mutation == "last_file_metadata":
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    elif mutation == "extra_file":
        (state.dest / "raw" / "unlisted.db").write_bytes(b"extra")
    monkeypatch.setattr(helper, "_sha256_file", lambda *a: pytest.fail("first hash reached before complete baseline check"))
    with pytest.raises((helper.PreimageError, OSError)):
        helper.verify(state.capture)
    assert not (state.dest / "VERIFIED.json").exists()


@pytest.mark.parametrize("stage", ["initial_hash", "normalization", "final_hash"])
def test_raw_mutation_during_verify_is_never_adoptable(helper, fixture_set, monkeypatch, stage):
    state = fixture_set
    captured_set(helper, state)
    raw = state.dest / document(state.capture)["files"][-1]["raw"]
    real_hash, real_normalize = helper._sha256_file, helper._normalize
    hash_calls = 0
    changed = False
    def change():
        nonlocal changed
        if changed:
            return
        changed = True
        info = raw.stat()
        os.utime(raw, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    def hash_file(path):
        nonlocal hash_calls
        value = real_hash(path)
        if path.parent.name == "raw":
            hash_calls += 1
            if stage == "initial_hash" or (stage == "final_hash" and hash_calls > 11):
                change()
        return value
    def normalize(source, target):
        real_normalize(source, target)
        if stage == "normalization":
            change()
    monkeypatch.setattr(helper, "_sha256_file", hash_file)
    monkeypatch.setattr(helper, "_normalize", normalize)
    with pytest.raises(helper.PreimageError, match="raw metadata"):
        helper.verify(state.capture)
    assert changed
    assert not (state.dest / "VERIFIED.json").exists()


def test_corrupt_captured_database_fails_real_sqlite_without_source_mutation(helper, fixture_set):
    state = fixture_set
    prepared_set(helper, state)
    # Corruption predates capture, therefore raw baseline is valid but SQLite is not.
    state.paths["assets"].write_bytes(b"broken database contents" * 100)
    frozen = source_fingerprint(state.paths.values())
    helper.capture(state.prepared, state.exclusion)
    with pytest.raises(sqlite3.DatabaseError):
        helper.verify(state.dest / "CAPTURE.json")
    assert not (state.dest / "VERIFIED.json").exists()
    assert source_fingerprint(state.paths.values()) == frozen
    assert list(state.dest.glob("scratch-*"))


def test_verify_opens_only_writable_scratch_and_normalized_paths(helper, fixture_set, monkeypatch):
    state = fixture_set
    captured_set(helper, state)
    original = source_fingerprint(state.paths.values())
    raw_before = source_fingerprint([state.dest / entry["raw"] for entry in document(state.capture)["files"]])
    real_connect = helper.sqlite3.connect
    opened = []
    def connect(path, *args, **kwargs):
        path = Path(path)
        assert path.is_relative_to(state.dest)
        assert path.parent.name.startswith("scratch-") or path.parent.name == "normalized"
        assert path.parent.name != "raw"
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) & 0o200
        assert not kwargs.get("uri", False)  # Ordinary SQLite RW scratch recovery.
        opened.append(path)
        return real_connect(path, *args, **kwargs)
    monkeypatch.setattr(helper.sqlite3, "connect", connect)
    helper.verify(state.capture)
    assert len(opened) == 12
    assert source_fingerprint(state.paths.values()) == original
    assert source_fingerprint([state.dest / entry["raw"] for entry in document(state.capture)["files"]]) == raw_before


@pytest.mark.parametrize("defect", ["missing_file", "duplicate_file", "unsafe_raw", "source_mapping", "raw_stat_missing",
                                     "raw_alias", "source_inventory_missing", "sidecar_state_missing", "prepared_digest",
                                     "capture_id", "state", "timing"])
def test_incomplete_capture_receipts_rejected_before_hash(helper, fixture_set, monkeypatch, defect):
    state = fixture_set
    captured_set(helper, state)
    receipt = document(state.capture)
    if defect == "missing_file":
        receipt["files"].pop()
    elif defect == "duplicate_file":
        receipt["files"][-1] = receipt["files"][0]
    elif defect == "unsafe_raw":
        receipt["files"][0]["raw"] = "../outside.db"
    elif defect == "source_mapping":
        receipt["files"][0]["source"] = str(state.root / "outside.db")
    elif defect == "raw_stat_missing":
        receipt["files"][0]["raw_stat"].pop("ctime_ns")
    elif defect == "raw_alias":
        receipt["files"][0]["raw_stat"]["ino"] = receipt["files"][0]["source_stat"]["ino"]
    elif defect == "source_inventory_missing":
        receipt["source_inventory"].pop("analytics")
    elif defect == "sidecar_state_missing":
        receipt["source_inventory"]["analytics"].pop("wal")
    elif defect == "prepared_digest":
        receipt["prepared_sha256"] = "0" * 64
    elif defect == "capture_id":
        receipt["capture_id"] = "wrong"
    elif defect == "state":
        receipt["state"] = "verified"
    elif defect == "timing":
        receipt["capture_timing"]["duration_ns"] += 1
    write_document(state.capture, receipt)
    monkeypatch.setattr(helper, "_sha256_file", lambda *a: pytest.fail("hash reached incomplete receipt"))
    with pytest.raises(helper.PreimageError):
        helper.verify(state.capture)


def test_prepared_digest_and_persisted_mapping_validation(helper, fixture_set):
    state = fixture_set
    prepared_set(helper, state)
    parsed = document(state.prepared)
    parsed["inventory"]["required_labels"].append("nonexistent")
    with pytest.raises(helper.PreimageError):
        helper.capture(parsed, state.exclusion)
    parsed = document(state.prepared)
    parsed["inventory_sha256"] = "0" * 64
    with pytest.raises(helper.PreimageError, match="digest"):
        helper.capture(parsed, state.exclusion)
    parsed = document(state.prepared)
    parsed["capture_id"] = "00000000-0000-4000-8000-000000000000"
    with pytest.raises(helper.PreimageError, match="persisted"):
        helper.capture(parsed, state.exclusion)


def test_parsed_record_api_matches_path_api(helper, fixture_set):
    state = fixture_set
    prepared_set(helper, state)
    result = helper.capture(document(state.prepared), state.exclusion)
    assert_result(result, state.dest / "CAPTURE.json")
    result = helper.verify(document(state.dest / "CAPTURE.json"))
    assert_result(result, state.dest / "VERIFIED.json")


def test_verified_reentry_resyncs_without_rewriting_bytes_or_receipt(helper, fixture_set, monkeypatch):
    state = fixture_set
    captured_set(helper, state)
    result = helper.verify(state.capture)
    receipt_path = state.dest / "VERIFIED.json"
    before = source_fingerprint(list((state.dest / "raw").iterdir()) +
                                list((state.dest / "normalized").iterdir()) +
                                [state.prepared, state.capture, receipt_path])
    syncs = []
    real_file, real_directory = helper._sync_file, helper._sync_directory
    def sync_file(path):
        syncs.append(("file", path))
        real_file(path)
    def sync_directory(path):
        syncs.append(("directory", path))
        real_directory(path)
    monkeypatch.setattr(helper, "_sync_file", sync_file)
    monkeypatch.setattr(helper, "_sync_directory", sync_directory)
    monkeypatch.setattr(helper, "native_clone", lambda *a: pytest.fail("reentry cloned"))
    monkeypatch.setattr(helper, "_normalize", lambda *a: pytest.fail("reentry normalized"))
    monkeypatch.setattr(helper, "_publish", lambda *a: pytest.fail("reentry rewrote receipt"))
    assert helper.verify(state.capture) == result
    assert source_fingerprint(list((state.dest / "raw").iterdir()) +
                              list((state.dest / "normalized").iterdir()) +
                              [state.prepared, state.capture, receipt_path]) == before
    required_files = {state.prepared, state.capture, receipt_path} | set((state.dest / "raw").iterdir()) | set((state.dest / "normalized").iterdir())
    assert {path for kind, path in syncs if kind == "file"} == required_files
    assert syncs[-4:] == [("directory", state.dest / "raw"), ("directory", state.dest / "normalized"),
                           ("directory", state.dest), ("directory", state.dest.parent)]


@pytest.mark.parametrize("defect", ["raw_hash", "normalized_hash", "normalized_missing", "raw_metadata",
                                     "missing_raw_hash", "missing_normalized_record", "capture_binding", "timing"])
def test_verified_reentry_refuses_incomplete_or_changed_content(helper, fixture_set, defect):
    state = fixture_set
    captured_set(helper, state)
    helper.verify(state.capture)
    verified_path = state.dest / "VERIFIED.json"
    verified = document(verified_path)
    if defect == "raw_hash":
        verified["raw_sha256"][next(iter(verified["raw_sha256"]))] = "0" * 64
    elif defect == "normalized_hash":
        path = state.dest / verified["normalized"][0]["path"]
        os.chmod(path, 0o600)
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        os.chmod(path, 0o400)
    elif defect == "normalized_missing":
        (state.dest / verified["normalized"][0]["path"]).unlink()
    elif defect == "raw_metadata":
        path = state.dest / next(iter(verified["raw_sha256"]))
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    elif defect == "missing_raw_hash":
        verified["raw_sha256"].pop(next(iter(verified["raw_sha256"])))
    elif defect == "missing_normalized_record":
        verified["normalized"].pop()
    elif defect == "capture_binding":
        verified["capture_sha256"] = "0" * 64
    elif defect == "timing":
        verified["timings"].pop("normalization")
    if defect not in {"normalized_hash", "normalized_missing", "raw_metadata"}:
        write_document(verified_path, verified)
    with pytest.raises((helper.PreimageError, OSError)):
        helper.verify(state.capture)


@pytest.mark.parametrize("target_name", ["normalized", ".", ".."])
def test_verified_reentry_fsync_failure_nonadoptable_and_retry_reestablishes_stage(helper, fixture_set, monkeypatch, capsys, target_name):
    state = fixture_set
    captured_set(helper, state)
    result = helper.verify(state.capture)
    path = state.dest / "VERIFIED.json"
    before = path.read_bytes()
    real_sync = helper._sync_directory
    target = state.dest.parent if target_name == ".." else state.dest / target_name
    def sync(directory):
        if directory == target:
            raise OSError(errno.EIO, "reentry durability failure")
        real_sync(directory)
    monkeypatch.setattr(helper, "_sync_directory", sync)
    status, output = invoke(helper, capsys, ["verify", state.capture])
    assert status != 0 and output["eligible"] is False
    assert path.read_bytes() == before
    monkeypatch.setattr(helper, "_sync_directory", real_sync)
    assert helper.verify(state.capture) == result
    assert path.read_bytes() == before


def test_after_rename_verified_can_explicitly_resync_without_acknowledging_capture(helper, fixture_set, monkeypatch, capsys):
    state = fixture_set
    captured_set(helper, state)
    real_sync = helper._sync_directory
    def sync(path):
        if path == state.dest:
            raise OSError(errno.EIO, "post-publication failure")
        real_sync(path)
    monkeypatch.setattr(helper, "_sync_directory", sync)
    code, output = invoke(helper, capsys, ["verify", state.capture])
    assert code == 1 and output["eligible"] is False
    before = (state.dest / "VERIFIED.json").read_bytes()
    monkeypatch.setattr(helper, "_sync_directory", real_sync)
    code, output = invoke(helper, capsys, ["verify", state.capture])
    assert code == 0 and output["eligible"] is True
    assert (state.dest / "VERIFIED.json").read_bytes() == before
    # The return has no field purporting to reconstruct capture acknowledgment.
    assert set(output) == {"eligible", "capture_id", "receipt_sha256"}


def test_partial_normalization_refuses_retry_preserving_diagnostics(helper, fixture_set, monkeypatch):
    state = fixture_set
    captured_set(helper, state)
    real_normalize = helper._normalize
    count = 0
    def normalize(source, target):
        nonlocal count
        real_normalize(source, target)
        count += 1
        if count == 2:
            raise OSError(errno.ENOSPC, "synthetic disk full")
    monkeypatch.setattr(helper, "_normalize", normalize)
    with pytest.raises(OSError):
        helper.verify(state.capture)
    scratch = list(state.dest.glob("scratch-*"))
    normalized = list((state.dest / "normalized").iterdir())
    assert scratch and len(normalized) == 2
    monkeypatch.setattr(helper, "_normalize", lambda *a: pytest.fail("partial normalization resumed"))
    with pytest.raises(helper.PreimageError, match="partial normalization"):
        helper.verify(state.capture)
    assert list(state.dest.glob("scratch-*")) == scratch
    assert list((state.dest / "normalized").iterdir()) == normalized


def test_fsync_publication_order_and_truthful_stage_timestamps(helper, fixture_set, monkeypatch):
    state = fixture_set
    events = []
    real_sync_file, real_sync_directory, real_replace = helper._sync_file, helper._sync_directory, helper.os.replace
    def sync_file(path):
        real_sync_file(path)
        events.append(("file", path))
    def sync_directory(path):
        real_sync_directory(path)
        events.append(("directory", path))
    def replace(source, target):
        real_replace(source, target)
        events.append(("replace", Path(target)))
    monkeypatch.setattr(helper, "_sync_file", sync_file)
    monkeypatch.setattr(helper, "_sync_directory", sync_directory)
    monkeypatch.setattr(helper.os, "replace", replace)
    prepared_set(helper, state)
    prep_publish = events.index(("replace", state.prepared))
    assert events.index(("directory", state.dest.parent)) < prep_publish
    for directory in (state.dest / "raw", state.dest / "normalized"):
        assert events.index(("directory", directory)) < prep_publish
    assert events[prep_publish + 1] == ("directory", state.dest)
    events.clear()
    helper.capture(state.prepared, state.exclusion)
    state.capture = state.dest / "CAPTURE.json"
    cap_publish = events.index(("replace", state.capture))
    raw_sync = events.index(("directory", state.dest / "raw"))
    assert raw_sync < cap_publish
    assert all(index < raw_sync for index, (kind, path) in enumerate(events) if kind == "file")
    assert events[cap_publish + 1] == ("directory", state.dest)
    events.clear()
    helper.verify(state.capture)
    verified_path = state.dest / "VERIFIED.json"
    ver_publish = events.index(("replace", verified_path))
    normalized_sync = events.index(("directory", state.dest / "normalized"))
    assert all(index < normalized_sync for index, (kind, path) in enumerate(events) if kind == "file")
    assert normalized_sync < ver_publish
    assert events[ver_publish + 1] == ("directory", state.dest)
    receipt = document(verified_path)
    helper._validate_verified(receipt, document(state.capture), state.capture.read_bytes(), document(state.prepared))
    assert all(timing["duration_ns"] >= 0 for timing in receipt["timings"].values())
    assert receipt["capture"]["capture_timing"]["end_ns"] <= receipt["timings"]["verification"]["start_ns"]


def test_actual_cli_unsupported_platform_returns_nonzero_eligible_false(fixture_set):
    state = fixture_set
    inventory = state.root / "inventory.json"
    write_document(inventory, state.inventory)
    helper_path = Path(__file__).parents[1] / "tools" / "restart_preimage.py"
    child = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import restart_preimage; "
             "restart_preimage.sys.platform='unsupported-test-platform'; "
             "raise SystemExit(restart_preimage.main(sys.argv[1:]))")
    completed = subprocess.run([sys.executable, "-c", child, str(helper_path.parent), "prepare", str(inventory), str(state.dest)],
                               text=True, capture_output=True, timeout=10, env={k: v for k, v in os.environ.items() if k != "TMUX"})
    assert completed.returncode != 0
    assert json.loads(completed.stdout)["eligible"] is False
    assert not (state.dest / "PREPARED.json").exists()


@pytest.mark.parametrize("stage", ["capture", "verify"])
@pytest.mark.parametrize("code", [errno.EXDEV, errno.ENOTSUP, errno.EACCES, errno.EEXIST])
def test_clone_failures_during_capture_and_verify_have_no_fallback(helper, fixture_set, monkeypatch, stage, code):
    state = fixture_set
    if stage == "capture":
        prepared_set(helper, state)
    else:
        captured_set(helper, state)
    before = source_fingerprint(state.paths.values())
    calls = []
    def fail(source, destination):
        calls.append((source, destination))
        raise OSError(code, "injected clone error")
    monkeypatch.setattr(helper, "native_clone", fail)
    with pytest.raises(OSError) as caught:
        if stage == "capture":
            helper.capture(state.prepared, state.exclusion)
        else:
            helper.verify(state.capture)
    assert caught.value.errno == code and len(calls) == 1
    assert not (state.dest / ("CAPTURE.json" if stage == "capture" else "VERIFIED.json")).exists()
    assert source_fingerprint(state.paths.values()) == before
    if stage == "capture":
        assert list((state.dest / "raw").iterdir()) == []
    else:
        assert list((state.dest / "normalized").iterdir()) == []
        assert list(state.dest.glob("scratch-*"))


@pytest.mark.parametrize("rows", [[], [("ok",), ("broken",)], [("OK",)], [("wrong",)], [("ok",), ("ok",)]])
def test_integrity_result_must_be_only_one_ok(helper, rows):
    class Connection:
        def execute(self, sql):
            assert sql == "PRAGMA integrity_check"
            return SimpleNamespace(fetchall=lambda: rows)
    with pytest.raises(helper.PreimageError, match="integrity_check"):
        helper._integrity(Connection())


@pytest.mark.parametrize("target_name", ["parent", "raw", "normalized", "destination"])
def test_prepare_directory_sync_failure_never_returns_eligible(helper, fixture_set, monkeypatch, capsys, target_name):
    state = fixture_set
    inventory_path = state.root / "inventory.json"
    write_document(inventory_path, state.inventory)
    target = {"parent": state.dest.parent, "raw": state.dest / "raw", "normalized": state.dest / "normalized",
              "destination": state.dest}[target_name]
    real_sync = helper._sync_directory
    def sync(path):
        if path == target:
            raise OSError(errno.EIO, "prepare durability failure")
        real_sync(path)
    monkeypatch.setattr(helper, "_sync_directory", sync)
    code, output = invoke(helper, capsys, ["prepare", inventory_path, state.dest])
    assert code == 1 and output["eligible"] is False
    assert (state.dest / "PREPARED.json").exists() is (target_name == "destination")
    assert not (state.dest / "CAPTURE.json").exists()


@pytest.mark.parametrize("name", ["raw", "normalized"])
def test_output_directory_replacement_or_symlink_refused(helper, fixture_set, monkeypatch, name):
    state = fixture_set
    prepared_set(helper, state)
    path = state.dest / name
    path.rmdir()
    other = state.root / "other"
    other.mkdir()
    path.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(helper, "native_clone", lambda *a: pytest.fail("cloned into changed directory"))
    with pytest.raises(helper.PreimageError, match="symlink"):
        helper.capture(state.prepared, state.exclusion)
    assert not (state.dest / "CAPTURE.json").exists()


def test_raw_baseline_mode_and_ctime_are_measured_after_chmod_fsync(helper, fixture_set, monkeypatch):
    state = fixture_set
    prepared_set(helper, state)
    real_sync = helper._sync_file
    observations = {}
    def sync(path):
        if path.parent == state.dest / "raw":
            assert stat.S_IMODE(path.stat().st_mode) == 0o400
            real_sync(path)
            observations[str(path)] = helper._stat_tuple(path.lstat())
        else:
            real_sync(path)
    monkeypatch.setattr(helper, "_sync_file", sync)
    helper.capture(state.prepared, state.exclusion)
    receipt = document(state.dest / "CAPTURE.json")
    assert len(observations) == len(receipt["files"]) == 11
    for entry in receipt["files"]:
        assert entry["raw_stat"] == observations[str(state.dest / entry["raw"])]


@pytest.mark.parametrize("alias_side", ["destination", "source"])
def test_prepare_rejects_containment_through_double_slash_alias(helper, fixture_set, alias_side):
    state = fixture_set
    inventory = copy.deepcopy(state.inventory)
    dest = state.source / "forbidden-capture"
    if alias_side == "destination":
        dest = Path("/" + str(dest))
    else:
        inventory["participants"] = {label: "/" + path for label, path in inventory["participants"].items()}
    source_parent = Path(inventory["participants"]["sessions"]).parent
    assert not dest.is_relative_to(source_parent)  # Reproduces the lexical trap.
    assert dest.resolve().is_relative_to(source_parent.resolve())
    with pytest.raises(helper.PreimageError, match="inside a source directory"):
        helper.prepare(inventory, dest)
    assert not dest.exists()


def test_prepare_containment_uses_directory_identity_not_casefolding(helper, fixture_set, monkeypatch):
    state = fixture_set
    # Simulate an alternate directory spelling resolving to the source parent,
    # as can happen on case-insensitive volumes. This is not native Darwin proof.
    real_lstat = Path.lstat
    source_parent_info = real_lstat(state.source)
    destination_parent = state.dest.parent
    def lstat(path, *args, **kwargs):
        if path == destination_parent:
            return source_parent_info
        return real_lstat(path, *args, **kwargs)
    assert not state.dest.is_relative_to(state.source)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(helper.PreimageError, match="inside a source directory"):
        helper.prepare(state.inventory, state.dest)
    assert not state.dest.exists()
