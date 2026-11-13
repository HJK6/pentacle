"""Versioned intake continuation and off-thread proof of the bytes consumed.

The hash is deliberately not a snapshot: a rewrite behind the reader is seen
at the next restart, just as it is for an uninterrupted tailer.
"""
import hashlib
from copy import deepcopy
import json
import os
from pathlib import Path
from time import monotonic

FORMAT_VERSION = 1
MAX_RECORD_BYTES = 256 * 1024
HASH_BLOCK_BYTES = 1024 * 1024
POSITION_FIELDS = frozenset(("version", "stream_id", "generation", "lifecycle", "provider",
                             "session_id", "path", "file_id", "offset", "digest"))
PROVENANCE_FIELDS = frozenset(("_bound", "native_session_id", "turn_models", "accounts",
                               "meta_seen", "cli_version", "last_model"))


def snapshot_provenance(state):
    """Pure pass-start snapshot; separately measurable on large turn maps."""
    return deepcopy(state)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def decode(record):
    """Reject unknown shapes and malformed types, rather than partially restore."""
    if not record:
        raise ValueError("missing")
    if "invalid" in record:
        raise ValueError(record["invalid"])
    if sum(len(record[key].encode("utf-8")) for key in ("position_json", "provenance_json", "routing_json")) > MAX_RECORD_BYTES:
        raise ValueError("state_too_large")
    try:
        position = json.loads(record["position_json"])
        provenance = json.loads(record["provenance_json"])
        routing = json.loads(record["routing_json"])
    except json.JSONDecodeError as exc:
        raise ValueError("unreadable") from exc
    if not isinstance(position, dict) or set(position) != POSITION_FIELDS:
        raise ValueError("position_shape")
    if type(position["version"]) is not int or position["version"] != FORMAT_VERSION:
        raise ValueError("format_version")
    if any(not isinstance(position[key], str) or not position[key]
           for key in POSITION_FIELDS - {"version", "offset", "file_id"}):
        raise ValueError("position_type")
    if type(position["offset"]) is not int or position["offset"] < 0:
        raise ValueError("offset")
    identity = position["file_id"]
    if not isinstance(identity, list) or len(identity) != 3 or any(type(i) is not int or i < 0 for i in identity):
        raise ValueError("file_identity")
    if len(position["digest"]) != 64 or any(c not in "0123456789abcdef" for c in position["digest"]):
        raise ValueError("digest")
    if not isinstance(provenance, dict) or not set(provenance) <= PROVENANCE_FIELDS:
        raise ValueError("provenance_shape")
    for key in ("native_session_id", "cli_version", "last_model"):
        if key in provenance and provenance[key] is not None and not isinstance(provenance[key], str):
            raise ValueError("provenance_type")
    if provenance.get("native_session_id", position["session_id"]) != position["session_id"]:
        raise ValueError("provenance_binding")
    if "meta_seen" in provenance and type(provenance["meta_seen"]) is not bool:
        raise ValueError("provenance_type")
    if "accounts" in provenance and (not isinstance(provenance["accounts"], list)
                                     or any(not isinstance(v, str) for v in provenance["accounts"])):
        raise ValueError("provenance_type")
    models = provenance.get("turn_models", {})
    if not isinstance(models, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in models.items()):
        raise ValueError("provenance_type")
    if "_bound" in provenance:
        if provenance["_bound"] != [position["path"], position["session_id"]]:
            raise ValueError("provenance_binding")
        provenance["_bound"] = tuple(provenance["_bound"])
    if routing is not None:
        if (not isinstance(routing, list) or len(routing) != 2
                or routing[0] != position["lifecycle"] or not isinstance(routing[1], list)
                or len(routing[1]) != 2 or any(not isinstance(v, str) for v in routing[1])):
            raise ValueError("routing_shape")
        routing = (routing[0], tuple(routing[1]))
    return position, provenance, routing


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_nlink)


def verify_prefix(path, fd, position):
    """Run with to_thread; return the *same* hash object used for proof."""
    started = monotonic()
    path = Path(path).expanduser()
    expected = tuple(position["file_id"])
    end = position["offset"]
    digest = hashlib.sha256()
    for info in (os.fstat(fd), os.stat(path)):
        if file_identity(info) != expected:
            raise ValueError("file_identity")
        if info.st_size < end:
            raise ValueError("truncated")
    offset = 0
    last = b""
    while offset < end:
        chunk = os.pread(fd, min(HASH_BLOCK_BYTES, end - offset), offset)
        if not chunk:
            raise ValueError("truncated")
        digest.update(chunk)
        offset += len(chunk)
        last = chunk[-1:]
    for info in (os.fstat(fd), os.stat(path)):
        if file_identity(info) != expected:
            raise ValueError("file_identity")
        if info.st_size < end:
            raise ValueError("truncated")
    if end and last != b"\n":
        raise ValueError("line_boundary")
    if digest.hexdigest() != position["digest"]:
        raise ValueError("prefix_hash")
    return digest, monotonic() - started
