"""Installation identity and cached release discovery."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .discovery import read_json, run
from .releases import MANIFEST, OFFICIAL_REMOTE, SHA, ReleaseError, ancestor, parse


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def commit(root: Path):
    value = run(["git", "-C", root, "rev-parse", "HEAD"]).stdout.strip()
    return value if SHA.fullmatch(value) else None


def safe_sha(value):
    return value if isinstance(value, str) and SHA.fullmatch(value) else None


def release_manifest(root: Path, remote: str, *, write_cache=True, fetch=True) -> dict:
    source = "official" if remote == OFFICIAL_REMOTE else "custom"
    remote_digest = hashlib.sha256(remote.encode()).hexdigest()
    cache = root / ".pentacle-deploy/releases-cache.json"
    result = {"source": "none", "fetched_at": None, "data": None, "age_days": None, "release_remote": source}
    fetched = run(["git", "-C", root, "fetch", remote, "main"], timeout=30) if fetch else None
    if fetched is not None and fetched.returncode == 0:
        shown = run(["git", "-C", root, "show", f"FETCH_HEAD:{MANIFEST}"])
        try:
            data = parse(shown.stdout) if shown.returncode == 0 else None
            if data is not None and all(ancestor(root, entry["commit"], "FETCH_HEAD") for entry in data["releases"]):
                timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
                result.update(source="fetched", fetched_at=timestamp, data=data, age_days=0)
                if write_cache:
                    atomic_json(cache, {"schema_version": 1, "release_remote": source, "remote_sha256": remote_digest,
                                        "fetched_at": timestamp, "manifest": data})
                return result
        except (ReleaseError, OSError):
            pass
    cached = read_json(cache)
    try:
        if isinstance(cached, dict) and cached.get("remote_sha256") == remote_digest and cached.get("schema_version") == 1:
            data = parse(json.dumps(cached["manifest"]))
            stamp = dt.datetime.fromisoformat(cached["fetched_at"])
            if stamp.tzinfo is None:
                raise ValueError
            result.update(source="cache", fetched_at=stamp.isoformat(), data=data,
                          age_days=max(0, (dt.datetime.now(dt.timezone.utc) - stamp).total_seconds() / 86400))
    except (ReleaseError, ValueError, TypeError, KeyError):
        pass
    return result


def identity(root: Path, manifest: dict, daemon: dict | None, web: dict | None):
    head = commit(root)
    entries = (manifest.get("data") or {}).get("releases", [])
    installed = next((entry for entry in entries if entry["commit"] == head), None)
    behind = sum(entry["commit"] != head and ancestor(root, head, entry["commit"]) for entry in entries) if head else 0
    build_id = (web or {}).get("build_id")
    import re
    return {"schema_version": 1,
            "install": {"commit": head, "version": installed["version"] if installed else None,
                        "release_remote": manifest["release_remote"]},
            "daemon": {"commit": safe_sha((daemon or {}).get("runtime_sha"))},
            "web": {"build_commit": safe_sha((web or {}).get("build_commit")),
                    "build_id": build_id if isinstance(build_id, str) and re.fullmatch(r"[0-9a-f]{16}", build_id) else None},
            "latest": {"version": entries[-1]["version"] if entries else None,
                       "commit": entries[-1]["commit"] if entries else None},
            "manifest": {"source": manifest["source"], "fetched_at": manifest["fetched_at"]},
            "behind_releases": behind if installed or behind else None}
