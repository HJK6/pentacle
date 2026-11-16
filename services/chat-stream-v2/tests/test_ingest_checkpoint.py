"""Trust decisions and byte-as-read interleavings, without a database."""
import hashlib
import json
import os

import pytest

import ingest_checkpoint as checkpoint
from usage_accounting import native_provenance


def position(path):
    return {"version": checkpoint.FORMAT_VERSION, "stream_id": "test:seat",
            "generation": "generation", "lifecycle": "2026-11-13T00:00:00Z",
            "provider": "codex", "session_id": "native", "path": str(path),
            "file_id": list(checkpoint.file_identity(path.stat())),
            "offset": path.stat().st_size, "digest": hashlib.sha256(path.read_bytes()).hexdigest()}


def record(pos, provenance=None, routing=None):
    return {"position_json": checkpoint.encode(pos),
            "provenance_json": checkpoint.encode(provenance or {}),
            "routing_json": checkpoint.encode(routing)}


def test_serialized_shape_requires_explicit_version_change(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b'{}\n')
    state = {"_bound": (str(path), "native")}
    native_provenance("codex", [
        {"type": "session_meta", "payload": {"id": "native", "cli_version": "1", "creator_account_id": "account"}},
        {"type": "turn_context", "payload": {"turn_id": "turn", "model": "model"}},
    ], native_session_id="native", state=state, proof=True)
    # This fixture is intentionally independent of the implementation constants.
    # A shape change must select a new entry, not edit the old version's entry.
    versions = {1: (
        ["digest", "file_id", "generation", "lifecycle", "offset", "path", "provider", "session_id", "stream_id", "version"],
        ["_bound", "accounts", "cli_version", "last_model", "meta_seen", "native_session_id", "turn_models"],
    )}
    assert (sorted(position(path)), sorted(state)) == versions[checkpoint.FORMAT_VERSION]
    pos, restored, routing = checkpoint.decode(record(position(path), state, [position(path)["lifecycle"], ["model", "high"]]))
    assert restored == state
    assert routing == (pos["lifecycle"], ("model", "high"))


@pytest.mark.parametrize("field,value,reason", [
    ("version", 999, "format_version"), ("version", True, "format_version"),
    ("offset", -1, "offset"), ("offset", True, "offset"), ("offset", "5", "offset"),
    ("file_id", [1, 2], "file_identity"), ("file_id", [1, True, 2], "file_identity"),
    ("digest", "x" * 64, "digest"), ("generation", None, "position_type"),
])
def test_malformed_position_rejected(tmp_path, field, value, reason):
    path = tmp_path / "source"
    path.write_bytes(b'{}\n')
    pos = position(path)
    pos[field] = value
    with pytest.raises(ValueError, match=reason):
        checkpoint.decode(record(pos))


@pytest.mark.parametrize("state", [{"new_field": 1}, {"turn_models": []}, {"accounts": [1]},
                                   {"meta_seen": 1}, {"_bound": ["foreign", "native"]}])
def test_unknown_or_corrupt_continuation_is_not_partially_restored(tmp_path, state):
    path = tmp_path / "source"
    path.write_bytes(b'{}\n')
    with pytest.raises(ValueError):
        checkpoint.decode(record(position(path), state))


def test_bounded_record_is_rejected_without_trimming(tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b'{}\n')
    with pytest.raises(ValueError, match="state_too_large"):
        checkpoint.decode(record(position(path), {"turn_models": {"turn": "x" * checkpoint.MAX_RECORD_BYTES}}))


@pytest.mark.parametrize("change", ["rewrite", "truncate", "replace", "partial"])
def test_prefix_proof_refuses_invalid_source(tmp_path, change):
    path = tmp_path / "source"
    path.write_bytes(b"prefix\n" * 100)
    pos = position(path)
    fd = os.open(path, os.O_RDONLY)
    try:
        if change == "rewrite":
            with path.open("r+b") as target:
                target.seek(350)
                target.write(b"X")
        elif change == "truncate":
            path.write_bytes(b"prefix\n")
        elif change == "replace":
            other = path.with_suffix(".new")
            other.write_bytes(path.read_bytes())
            other.replace(path)
        else:
            pos["offset"] -= 1
            pos["digest"] = hashlib.sha256(path.read_bytes()[:-1]).hexdigest()
        with pytest.raises(ValueError):
            checkpoint.verify_prefix(path, fd, pos)
    finally:
        os.close(fd)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("append", [False, True])
def test_concurrent_rewrite_follows_bytes_as_read_and_next_restart_detects_it(tmp_path, monkeypatch, when, append):
    path = tmp_path / "source"
    path.write_bytes(b"a" * 31 + b"\n" + b"b" * 31 + b"\n")
    pos = position(path)
    fd = os.open(path, os.O_RDONLY)
    real_read = os.pread
    changed = False

    def rewrite():
        with path.open("r+b") as target:
            target.seek(0)
            target.write(b"z")
            if append:
                target.seek(0, 2)
                target.write(b"tail\n")

    def read(descriptor, count, offset):
        nonlocal changed
        if descriptor == fd and offset == 0 and not changed:
            changed = True
            if when == "before":
                rewrite()
            data = real_read(descriptor, count, offset)
            if when == "after":
                rewrite()
            return data
        return real_read(descriptor, count, offset)

    monkeypatch.setattr(checkpoint, "HASH_BLOCK_BYTES", 32)
    monkeypatch.setattr(checkpoint.os, "pread", read)
    try:
        if when == "before":
            with pytest.raises(ValueError, match="prefix_hash"):
                checkpoint.verify_prefix(path, fd, pos)
        else:
            consumed, _ = checkpoint.verify_prefix(path, fd, pos)
            if append:
                consumed.update(real_read(fd, 5, pos["offset"]))
                pos["offset"] += 5
            pos["digest"] = consumed.hexdigest()
            with pytest.raises(ValueError, match="prefix_hash"):
                checkpoint.verify_prefix(path, fd, pos)
        assert changed
    finally:
        os.close(fd)


def test_append_during_hash_keeps_position_eligible(tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_bytes(b"a\n" * 100)
    pos = position(path)
    fd = os.open(path, os.O_RDONLY)
    real_read = os.pread

    def read(descriptor, count, offset):
        data = real_read(descriptor, count, offset)
        if descriptor == fd and offset == 0:
            with path.open("ab") as target:
                target.write(b"suffix\n")
        return data

    monkeypatch.setattr(checkpoint.os, "pread", read)
    try:
        digest, _ = checkpoint.verify_prefix(path, fd, pos)
        assert digest.hexdigest() == pos["digest"]
    finally:
        os.close(fd)
