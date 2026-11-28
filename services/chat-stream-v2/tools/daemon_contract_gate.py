#!/usr/bin/env python3
"""Check daemon structure config."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = SERVICE_ROOT / "daemon_contract"
CONTRACT_PATH = CONTRACT_ROOT / "contract.json"
LOCAL_OWNER_CONFIG_PATH = Path.home() / ".config" / "pentacle" / "daemon-contract.json"
BASELINE_COMMIT = "f153ca8adce2a38e0c5092b2dc98ff5aec16e06e"
EXPECTED_LAYERS = [
    "F1_protected_chat_core", "F2_event_feed_and_projections",
    "F3_workflow_policy", "F4_migrations_and_safe_switch",
    "F5_observation", "F6_durable_delivery",
]
IDENTITY_RE = re.compile(r"^[a-z0-9_-]+:v2-[a-z0-9-]+$", re.I)


class GateError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, text=True,
                            capture_output=True, check=False, timeout=20)
    if result.returncode:
        raise GateError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def _local_structure_owner_configured() -> bool:
    """Read the optional installation owner without exposing its value."""
    try:
        raw = LOCAL_OWNER_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GateError("local daemon contract config is unreadable") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateError("local daemon contract config is invalid JSON") from exc
    owner = config.get("structure_owner") if isinstance(config, dict) else None
    if not isinstance(owner, str) or not IDENTITY_RE.fullmatch(owner):
        raise GateError("local daemon contract config must contain a valid structure_owner")
    return True


def _validate_contract() -> list[str]:
    errors: list[str] = []
    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"contract configuration is unreadable: {type(exc).__name__}") from exc
    if contract.get("contract_version") != 1 or contract.get("baseline_commit") != BASELINE_COMMIT:
        errors.append("contract version or frozen baseline commit changed")
    if contract.get("layers") != EXPECTED_LAYERS:
        errors.append("the six daemon layers must appear once in the frozen order")
    if contract.get("structure_owner") is not None or contract.get("structure_owner_status") != "pending":
        errors.append("public structure_owner must remain explicit pending/null")
    if not _local_structure_owner_configured():
        print("[daemon-contract] structure owner is pending local configuration")
    else:
        print("[daemon-contract] local structure owner is configured")

    return errors


def check(base: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise GateError("--base must be a full lowercase commit SHA")
    _git("cat-file", "-e", f"{base}^{{commit}}")
    return _validate_contract()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="trusted merge-base commit")
    args = parser.parse_args(argv)
    try:
        errors = check(args.base)
    except (GateError, OSError, UnicodeError, SyntaxError) as exc:
        print(f"[daemon-contract] FAIL: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"[daemon-contract] FAIL: {error}", file=sys.stderr)
        return 1
    print("[daemon-contract] PASS: six layers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
