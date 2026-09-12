#!/usr/bin/env python3
"""Reject anonymizer residue outside explicitly named synthetic test fixtures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

PATTERN = re.compile("|".join(["host" + suffix for suffix in "abc"] + ["ab" + "ra"]))

# CGNAT / Tailscale range 100.64/10 (second octet 64-127): a concrete
# tailnet address must never ship in the public tree. The residue check missed a
# real one (server/README.md, a daffodil deploy example) — this makes it a guard.
# Bounded by non-digit/non-dot on both sides so it never fires inside a larger
# number, and second octet 64-127 excludes 100.0-63 / 100.128-255.
CGNAT_PATTERN = re.compile(
    r"(?<![0-9.])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])"
    r"\.[0-9]{1,3}\.[0-9]{1,3}(?![0-9.])"
)


def _line_hits(line: str) -> bool:
    return bool(PATTERN.search(line) or CGNAT_PATTERN.search(line))


def check(root: Path, allowlist: Path) -> dict:
    manifest = json.loads(allowlist.read_text())
    if manifest.get("version") != 1 or not isinstance(manifest.get("fixtures"), dict):
        raise ValueError("expected version 1 and an exact-path fixtures object")
    fixtures = manifest["fixtures"]
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    names = set(tracked)
    for name, reason in fixtures.items():
        path = Path(name)
        if name not in names or not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"invalid fixture entry: {name}")
        if not ({"test", "tests", "fixtures"} & set(path.parts)) and name != ".github/ci/hermetic_machines.json":
            raise ValueError(f"allowlist cannot exempt shipped source: {name}")
    violations = []
    allowed_hits = {}
    for name in sorted(names - {""}):
        path = root / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lines = [number for number, line in enumerate(content.splitlines(), 1) if _line_hits(line)]
        if not lines:
            continue
        if name in fixtures:
            allowed_hits[name] = len(lines)
        else:
            violations.append({"path": name, "lines": lines})
    return {"passed": not violations, "violations": violations, "fixture_hits": allowed_hits}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--allowlist", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        result = check(root, args.allowlist or root / "configs/public_fixture_allowlist.json")
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
