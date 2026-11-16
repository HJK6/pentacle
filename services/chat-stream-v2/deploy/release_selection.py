"""Retained helper-manifest validators for the existing CLI pin protocol."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


class Invalid(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Invalid("release selection: " + reason)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read(path):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            require(name not in result, "duplicate JSON key " + name)
            result[name] = value
        return result
    try:
        return json.loads(Path(path).read_bytes(), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise Invalid(f"release selection: invalid input {path}: {exc}") from exc


def file_binding(binding):
    require(isinstance(binding, dict) and set(binding) == {"path", "sha256"}, "invalid file binding")
    path = Path(binding["path"])
    require(path.is_absolute(), "binding path must be absolute")
    try:
        actual = digest(path.read_bytes())
    except OSError as exc:
        raise Invalid(f"release selection: missing bound file {path}") from exc
    require(actual == binding["sha256"], f"changed bound file {path}")
    return binding


def helper_manifest(peer):
    """Configured helper disk/invocation identity; never loaded-runtime proof."""
    sha = peer.get("installed_helpers_sha256")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha), "missing helper manifest SHA256")
    binding = peer.get("installed_helpers_manifest")
    require(isinstance(binding, dict) and binding.get("sha256") == sha, "helper manifest digest mismatch")
    data = read(file_binding(binding)["path"])
    require(isinstance(data, dict) and set(data) == {"host", "configured_helpers", "files", "limits"}
            and data["host"] == peer.get("host") and isinstance(data["limits"], list) and data["limits"],
            "malformed helper identity manifest")
    identity, files = data["configured_helpers"], data["files"]
    require(isinstance(identity, dict) and identity.get("host") == data["host"]
            and isinstance(identity.get("invocations"), list) and identity["invocations"]
            and isinstance(identity.get("closures"), list) and identity["closures"]
            and isinstance(identity.get("interpreters"), dict) and identity["interpreters"]
            and isinstance(identity.get("loaded_module_provenance"), dict) and identity["loaded_module_provenance"]
            and isinstance(files, list) and files, "incomplete configured helper identity")
    for item in [binding, *files]:
        file_binding(item)
        path = Path(item["path"])
        require(not path.is_symlink() and not path.stat().st_mode & 0o222, "helper identity must be retained read-only")
    return [binding, *files]
