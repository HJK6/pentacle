#!/usr/bin/env python3
"""Unit test for check_public_residue's detectors (run under Public checks).

Standalone (no pytest dependency). Sample addresses are assembled from parts so
this test file itself carries no literal CGNAT address or anonymizer token that
the checker would (correctly) flag — the same trick check_public_residue.py uses
for its own patterns. Run: `python3 scripts/test_check_public_residue.py`.
"""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager
import os
import json
import subprocess
import tempfile
from pathlib import Path

_mod_path = Path(__file__).resolve().with_name("check_public_residue.py")
_spec = importlib.util.spec_from_file_location("check_public_residue", _mod_path)
cpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpr)

_P = "100."  # not a CGNAT address on its own (not four octets)


def _cgnat(second: int, rest: str) -> str:
    return _P + f"{second}.{rest}"


@contextmanager
def isolated_git_environment():
    # Hooks export repository-selection variables. Fixture Git commands must
    # select their owned temporary repository, then restore the caller context.
    saved = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
    for key in saved:
        os.environ.pop(key)
    try:
        yield
    finally:
        os.environ.update(saved)


def main() -> int:
    # In-range (second octet 64-127) must be flagged, anywhere on the line.
    hits = [
        _cgnat(64, "0.0"), _cgnat(64, "0.1"), _cgnat(80, "28.24"),
        _cgnat(70, "128.35"), _cgnat(127, "255.255"),
        "  --bind " + _cgnat(96, "10.10") + " --port 7796",
        "host: '" + _cgnat(111, "2.3") + "'",
    ]
    for s in hits:
        assert cpr.CGNAT_PATTERN.search(s), f"expected CGNAT hit: {s!r}"
        assert cpr._line_hits(s), f"expected _line_hits: {s!r}"

    # Outside the range, loopback, private, and RFC5737 doc ranges: no flag.
    misses = [
        _cgnat(63, "255.255"),        # just below
        _cgnat(128, "0.0"),           # just above
        _cgnat(200, "1.1"),           # second octet > 127
        "10.0.0.1", "192.168.1.5", "127.0.0.1", "0.0.0.0",
        "198.51.100.24", "203.0.113.7",
        "1" + _cgnat(64, "0.1"),      # leading digit -> not a boundary
        _cgnat(64, "0.1") + ".5",     # trailing dotted digit
    ]
    for s in misses:
        assert not cpr.CGNAT_PATTERN.search(s), f"unexpected CGNAT hit: {s!r}"

    # The anonymizer-residue pattern still fires and _line_hits unions both.
    anon = "host" + "a"
    assert cpr.PATTERN.search(anon), "anonymizer pattern regressed"
    assert cpr._line_hits("something " + "host" + "b here")
    assert not cpr._line_hits("a perfectly clean line 10.0.0.1")

    for word in ["host" + suffix for suffix in ("a", "b", "c", "config", "admission", "busy")]:
        assert cpr._line_hits(word), "default web detector changed"
        assert not cpr._line_hits(word, True), "fixed mobile vocabulary rejected"
        assert not cpr._line_hits("optimistic_" + word + "_one", True), "underscore boundary rejected"
    assert cpr._line_hits("host" + "alpha", True), "non-approved residue exempted"
    assert cpr._line_hits("ab" + "ra", True), "other anonymizer exempted"
    for address in hits:
        assert cpr._line_hits("host" + "a " + address, True), "mobile profile masked address"

    with isolated_git_environment(), tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        source = root / "source.txt"
        source.write_text("id_" + "host" + "b_fixture " + "host" + "config")
        subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
        allowlist = root / "allowlist.json"
        allowlist.write_text(json.dumps({"version": 1, "profile": "mobile-synthetic", "fixtures": {}}))
        assert cpr.check(root, allowlist)["passed"]
        source.write_text(source.read_text() + " " + hits[0])
        assert not cpr.check(root, allowlist)["passed"], "profile hid original address"
        allowlist.write_text(json.dumps({"version": 1, "profile": "arbitrary", "fixtures": {}}))
        try:
            cpr.check(root, allowlist)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown profile accepted")

    print("ok - check_public_residue detectors: default web + bounded mobile profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
