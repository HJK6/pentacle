#!/usr/bin/env python3
"""Select and verify a release-runtime interpreter from ``requires-python``.

This file deliberately remains compatible with Python 3.9 because it is run by
the target host's ambient Python *before* the release runtime exists.  It must
therefore not rely on the package's own Python requirement to select the
interpreter that satisfies that requirement.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys


_CLAUSE = re.compile(r"(<=|>=|==|!=|~=|<|>)\s*(\d+(?:\.\d+){0,2}(?:\.\*)?)\Z")
_VERSION = re.compile(r"\d+(?:\.\d+){0,2}\Z")


def _release_version(value):
    """Return a three-part release tuple and its explicit component count."""
    wildcard = value.endswith(".*")
    if wildcard:
        value = value[:-2]
    if not _VERSION.fullmatch(value):
        raise ValueError("runtime_requires_python_invalid")
    values = [int(part) for part in value.split(".")]
    if len(values) > 3:
        raise ValueError("runtime_requires_python_invalid")
    return tuple(values + [0] * (3 - len(values))), len(values), wildcard


def version_satisfies(version, requirement):
    """Evaluate the supported PEP 440 release-version specifier subset.

    Unsupported syntax fails closed instead of allowing a runtime whose
    compatibility could not be established.  ``requires-python`` for this
    package is ``>=3.11`` and is within this subset.
    """
    if not isinstance(requirement, str) or not requirement.strip():
        raise ValueError("runtime_requires_python_invalid")
    if not isinstance(version, tuple) or len(version) != 3:
        raise ValueError("runtime_version_invalid")
    for clause in requirement.split(","):
        match = _CLAUSE.fullmatch(clause.strip())
        if match is None:
            raise ValueError("runtime_requires_python_invalid")
        operator, raw_target = match.groups()
        target, precision, wildcard = _release_version(raw_target)
        if wildcard and operator not in {"==", "!="}:
            raise ValueError("runtime_requires_python_invalid")
        if operator == ">=" and version < target:
            return False
        if operator == ">" and version <= target:
            return False
        if operator == "<=" and version > target:
            return False
        if operator == "<" and version >= target:
            return False
        if operator == "==":
            if wildcard:
                if version[:precision] != target[:precision]:
                    return False
            elif version != target:
                return False
        if operator == "!=":
            if wildcard:
                if version[:precision] == target[:precision]:
                    return False
            elif version == target:
                return False
        if operator == "~=":
            if wildcard or precision < 2:
                raise ValueError("runtime_requires_python_invalid")
            upper = list(target)
            upper[precision - 2] += 1
            for index in range(precision - 1, len(upper)):
                upper[index] = 0
            if version < target or version >= tuple(upper):
                return False
    return True


def _python_version(python):
    try:
        output = subprocess.check_output(
            [python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        version, _precision, wildcard = _release_version(output)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None
    if wildcard:
        return None
    return version


def _candidate_paths():
    override = os.environ.get("PENTACLE_RUNTIME_PYTHON")
    if override:
        return [override]
    return [
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/opt/homebrew/opt/python@3.13/bin/python3.13",
        "/opt/homebrew/opt/python@3.13/bin/python3",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3",
        "python3.13",
        "python3.12",
        "python3.11",
        sys.executable,
        "python3",
    ]


def select_python(requirement):
    seen = set()
    for candidate in _candidate_paths():
        resolved = candidate if os.path.sep in candidate else shutil.which(candidate)
        if not resolved:
            continue
        resolved = os.path.realpath(resolved)
        if resolved in seen or not os.access(resolved, os.X_OK):
            continue
        seen.add(resolved)
        version = _python_version(resolved)
        if version is not None and version_satisfies(version, requirement):
            return resolved
    raise RuntimeError("runtime_python_unsatisfied:" + requirement)


def main():
    parser = argparse.ArgumentParser()
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--select", metavar="REQUIRES_PYTHON")
    operation.add_argument("--check", metavar="REQUIRES_PYTHON")
    args = parser.parse_args()
    requirement = args.select or args.check
    try:
        if args.select:
            print(select_python(requirement))
        elif not version_satisfies(tuple(sys.version_info[:3]), requirement):
            raise RuntimeError("runtime_python_unsatisfied:" + requirement)
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
