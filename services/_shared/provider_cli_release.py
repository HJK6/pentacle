"""Exact reviewed provider releases. No filesystem, account or network mutation."""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import urlsplit

PLATFORMS = frozenset({"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"})
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("provider_cli_pin_duplicate_key")
        result[key] = value
    return result


def loads(raw):
    return validate(json.loads(raw, object_pairs_hook=_unique))


def version(provider, text):
    if not isinstance(text, str): raise ValueError("provider_cli_version_invalid")
    if provider == "codex" and text.startswith("codex-cli "): text = text[len("codex-cli "):]
    if provider == "claude" and text.endswith(" (Claude Code)"): text = text[:-len(" (Claude Code)")]
    if not SEMVER.fullmatch(text): raise ValueError("provider_cli_version_invalid")
    return text


def validate(value):
    try:
        if (not isinstance(value, dict) or set(value) != {"schema", "revision", "providers"}
                or type(value["schema"]) is not int or value["schema"] != 1
                or type(value["revision"]) is not int or value["revision"] < 1
                or not isinstance(value["providers"], dict) or set(value["providers"]) != {"claude", "codex"}):
            raise ValueError()
        platforms = None
        for provider, entry in value["providers"].items():
            if (not isinstance(entry, dict) or set(entry) != {"version", "artifacts"}
                    or not isinstance(entry["version"], str) or not SEMVER.fullmatch(entry["version"])
                    or not isinstance(entry["artifacts"], dict) or not entry["artifacts"]
                    or not set(entry["artifacts"]) <= PLATFORMS): raise ValueError()
            current = set(entry["artifacts"])
            if platforms is not None and platforms != current: raise ValueError()
            platforms = current
            for artifact in entry["artifacts"].values():
                if (not isinstance(artifact, dict) or set(artifact) != {"provenance", "native_sha256", "resource_sha256"}
                        or any(not isinstance(artifact[k], str) or not SHA256.fullmatch(artifact[k])
                               for k in ("native_sha256", "resource_sha256"))): raise ValueError()
                url = urlsplit(artifact["provenance"])
                if (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment
                        or any(ord(c) <= 32 or ord(c) >= 127 for c in artifact["provenance"])): raise ValueError()
        return json.loads(canonical(value))
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ValueError("provider_cli_pin_invalid") from None


def manifest(value):
    pin = validate(value)
    return {provider: {"required_version": entry["version"], "pin_revision": pin["revision"],
                       "pin_digest": digest(pin), "artifacts": entry["artifacts"]}
            for provider, entry in pin["providers"].items()}
