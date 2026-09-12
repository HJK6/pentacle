#!/usr/bin/env python3
"""Unit test for check_public_residue's detectors (run under Public checks).

Standalone (no pytest dependency). Sample addresses are assembled from parts so
this test file itself carries no literal CGNAT address or anonymizer token that
the checker would (correctly) flag — the same trick check_public_residue.py uses
for its own patterns. Run: `python3 scripts/test_check_public_residue.py`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_mod_path = Path(__file__).resolve().with_name("check_public_residue.py")
_spec = importlib.util.spec_from_file_location("check_public_residue", _mod_path)
cpr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cpr)

_P = "100."  # not a CGNAT address on its own (not four octets)


def _cgnat(second: int, rest: str) -> str:
    return _P + f"{second}.{rest}"


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

    print("ok - check_public_residue detectors: CGNAT + anonymizer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
