#!/usr/bin/env python3
"""Validate the candidate-bound Stage 1 usage manifest.

This intentionally stays dependency-free: the checked-in JSON fixture is the
human-readable schema, while this command is the fail-closed gate used by the
release receipt.  It validates structure, candidate/overlay bindings, receipt
digests and stream ownership without contacting a runtime or changing a pin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "pentacle.usage-accounting.stage1"
SELECTOR = [
    "services/chat-stream-v2/tests/test_usage_accounting.py",
    "services/chat-stream-v2/tests/test_event_push.py",
    "services/chat-stream-v2/tests/test_usage_telemetry.py",
]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")
HOST = re.compile(r"^[a-z][a-z0-9_-]*$")
STREAM = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9_.:-]+$")
RECEIPT_KEYS = {"path", "sha256"}
OVERLAY_KEYS = {"test_sha256", "fixture_sha256", "manifest_schema_sha256"}


class ManifestError(ValueError):
    pass


def _required(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{where} must be an object")
    missing = keys - set(value)
    extra = set(value) - keys
    if missing:
        raise ManifestError(f"{where} missing keys: {', '.join(sorted(missing))}")
    if extra:
        raise ManifestError(f"{where} has unexpected keys: {', '.join(sorted(extra))}")
    return value


def _string(value: Any, where: str, *, pattern: re.Pattern[str] | None = None, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ManifestError(f"{where} must be a string" + (" (non-empty)" if nonempty else ""))
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ManifestError(f"{where} has an invalid format")
    return value


def _positive_int(value: Any, where: str) -> int:
    if type(value) is not int or value <= 0:
        raise ManifestError(f"{where} must be a positive integer")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo_sha(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManifestError(f"cannot read candidate SHA: {exc}") from exc
    if result.returncode != 0:
        raise ManifestError("cannot read candidate SHA")
    return result.stdout.strip()


def _validate_receipt(value: Any, where: str, *, verify_files: bool) -> None:
    receipt = _required(value, RECEIPT_KEYS, where)
    path_text = _string(receipt["path"], f"{where}.path", nonempty=True)
    if not path_text.startswith("/"):
        raise ManifestError(f"{where}.path must be absolute")
    digest = _string(receipt["sha256"], f"{where}.sha256", pattern=SHA64)
    if verify_files:
        path = Path(path_text)
        if not path.is_file():
            raise ManifestError(f"{where}.path does not exist: {path_text}")
        if _digest(path) != digest:
            raise ManifestError(f"{where}.sha256 does not match {path_text}")


def _host_list(value: Any, where: str, *, nonempty: bool = False) -> set[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ManifestError(f"{where} must be an array" + (" (non-empty)" if nonempty else ""))
    for host in value:
        _string(host, where, pattern=HOST)
    if len(value) != len(set(value)):
        raise ManifestError(f"{where} must contain unique hosts")
    return set(value)


def validate(manifest_path: Path, *, repo: Path, deployment_contract: Path,
             verify_files: bool = True) -> dict[str, Any]:
    if deployment_contract.resolve().is_relative_to(repo.resolve()):
        raise ManifestError("deployment contract must be outside the source checkout")
    try:
        contract_bytes = deployment_contract.read_bytes()
        expected = _required(json.loads(contract_bytes), {
            "schema", "schema_version", "coordinator_host", "satellite_hosts", "pin_owner", "prechange_hosts",
        }, "deployment contract")
    except (OSError, ValueError) as exc:
        raise ManifestError("cannot read a valid deployment contract") from exc
    if (expected["schema"] != "pentacle.usage-deployment"
            or type(expected["schema_version"]) is not int or expected["schema_version"] != 1):
        raise ManifestError("deployment contract schema/version mismatch")
    expected_coordinator = _string(expected["coordinator_host"], "deployment.coordinator_host", pattern=HOST)
    expected_satellites = _host_list(expected["satellite_hosts"], "deployment.satellite_hosts")
    if expected_coordinator in expected_satellites:
        raise ManifestError("coordinator cannot also be a satellite")
    expected_prechange = _host_list(expected["prechange_hosts"], "deployment.prechange_hosts", nonempty=True)
    expected_owner = _string(expected["pin_owner"], "deployment.pin_owner", pattern=STREAM)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest: {exc}") from exc
    top = _required(payload, {
        "schema", "schema_version", "candidate_sha", "base_sha", "selector",
        "overlay", "runtime", "pin", "streams", "receipts",
    }, "manifest")
    if top["schema"] != SCHEMA or type(top["schema_version"]) is not int or top["schema_version"] != 2:
        raise ManifestError("manifest schema/version mismatch; migrate to v2 with an external deployment contract")
    candidate = _string(top["candidate_sha"], "candidate_sha", pattern=SHA40)
    _string(top["base_sha"], "base_sha", pattern=SHA40)
    selector = top["selector"]
    if selector != SELECTOR:
        raise ManifestError("selector mismatch")
    if verify_files and candidate != _repo_sha(repo):
        raise ManifestError("candidate_sha does not match repository HEAD")

    overlay = _required(top["overlay"], OVERLAY_KEYS, "overlay")
    for key in OVERLAY_KEYS:
        _string(overlay[key], f"overlay.{key}", pattern=SHA64)
    overlay_paths = {
        "test_sha256": repo / SELECTOR[2],
        "fixture_sha256": repo / "services/chat-stream-v2/tests/fixtures/usage_telemetry_cases.json",
        "manifest_schema_sha256": repo / "services/chat-stream-v2/tests/fixtures/usage_stage1_manifest_schema.json",
    }
    if verify_files:
        for key, path in overlay_paths.items():
            if not path.is_file() or _digest(path) != overlay[key]:
                raise ManifestError(f"{key} does not bind the current overlay")

    runtime = _required(top["runtime"], {"coordinator", "satellites"}, "runtime")
    coordinator = _required(runtime["coordinator"], {"host", "pid", "checkout", "sha"}, "runtime.coordinator")
    _string(coordinator["host"], "runtime.coordinator.host", pattern=HOST)
    if coordinator["host"] != expected_coordinator:
        raise ManifestError("runtime coordinator does not match deployment contract")
    _positive_int(coordinator["pid"], "runtime.coordinator.pid")
    _string(coordinator["checkout"], "runtime.coordinator.checkout", nonempty=True)
    if not coordinator["checkout"].startswith("/"):
        raise ManifestError("runtime.coordinator.checkout must be absolute")
    _string(coordinator["sha"], "runtime.coordinator.sha", pattern=SHA40)
    satellites = _required(runtime["satellites"], expected_satellites, "runtime.satellites")
    for host, value in satellites.items():
        sat = _required(value, {"checkout_sha", "pid", "event_push_runtime_sha", "observed_at"}, f"runtime.satellites.{host}")
        _string(sat["checkout_sha"], f"runtime.satellites.{host}.checkout_sha", pattern=SHA40)
        _positive_int(sat["pid"], f"runtime.satellites.{host}.pid")
        _string(sat["event_push_runtime_sha"], f"runtime.satellites.{host}.event_push_runtime_sha", pattern=SHA40)
        _string(sat["observed_at"], f"runtime.satellites.{host}.observed_at", nonempty=True)

    pin = _required(top["pin"], {"owner", "mutation_status", "target_sha", "previous_sha"}, "pin")
    if pin["owner"] != expected_owner:
        raise ManifestError("pin.owner does not match deployment contract")
    if pin["mutation_status"] not in {"deferred", "staged", "rolled_back"}:
        raise ManifestError("invalid pin mutation_status")
    for key in ("target_sha", "previous_sha"):
        if pin[key] is not None:
            _string(pin[key], f"pin.{key}", pattern=SHA40)

    streams = top["streams"]
    if not isinstance(streams, list) or not streams:
        raise ManifestError("streams must be a non-empty array")
    stream_keys = {
        "stream_id", "authenticated_source_host", "provider", "session_generation",
        "source_file_identity_digest", "snapshot_digest", "revision", "request_ids",
        "usage_recorded", "usage_replayed", "replay_zero_new_records",
    }
    for index, value in enumerate(streams):
        stream = _required(value, stream_keys, f"streams[{index}]")
        stream_id = _string(stream["stream_id"], f"streams[{index}].stream_id", nonempty=True)
        source_host = _string(stream["authenticated_source_host"], f"streams[{index}].authenticated_source_host", nonempty=True)
        if source_host not in {expected_coordinator, *expected_satellites}:
            raise ManifestError(f"streams[{index}] source host is outside the runtime inventory")
        if not stream_id.startswith(f"{source_host}:") or stream_id.removeprefix(f"{source_host}:") == "":
            raise ManifestError(f"streams[{index}] is not bound to its authenticated source host")
        if stream["provider"] not in {"codex", "claude"}:
            raise ManifestError(f"streams[{index}].provider is invalid")
        _string(stream["session_generation"], f"streams[{index}].session_generation", nonempty=True)
        _string(stream["source_file_identity_digest"], f"streams[{index}].source_file_identity_digest", pattern=SHA64)
        _string(stream["snapshot_digest"], f"streams[{index}].snapshot_digest", pattern=SHA64)
        _positive_int(stream["revision"], f"streams[{index}].revision")
        if not isinstance(stream["request_ids"], list) or not stream["request_ids"] or any(not isinstance(item, str) or not item for item in stream["request_ids"]):
            raise ManifestError(f"streams[{index}].request_ids must be non-empty strings")
        for key in ("usage_recorded", "usage_replayed"):
            if not isinstance(stream[key], list) or any(type(item) is not int or item < 0 for item in stream[key]):
                raise ManifestError(f"streams[{index}].{key} must contain non-negative integers")
        if type(stream["replay_zero_new_records"]) is not bool:
            raise ManifestError(f"streams[{index}].replay_zero_new_records must be boolean")
        if stream["replay_zero_new_records"] and any(item != 0 for item in stream["usage_recorded"]):
            raise ManifestError(f"streams[{index}] claims zero replay writes but records a write")

    receipts = _required(top["receipts"], {"red", "focused", "junit", "prechange"}, "receipts")
    for key in ("red", "focused", "junit"):
        value = receipts[key]
        _validate_receipt(value, f"receipts.{key}", verify_files=verify_files)
    prechange = _required(receipts["prechange"], expected_prechange, "receipts.prechange")
    for host, value in prechange.items():
        _validate_receipt(value, f"receipts.prechange.{host}", verify_files=verify_files)
    return {"passed": True, "candidate_sha": candidate,
            "deployment_contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
            "file_checks": verify_files}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--manifest", required=True, type=Path)
    validate_parser.add_argument("--deployment-contract", required=True, type=Path)
    validate_parser.add_argument("--repo", type=Path, default=None)
    validate_parser.add_argument("--no-file-check", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "validate":
        parser.error("unknown command")
    repo = args.repo.resolve() if args.repo else Path(__file__).resolve().parents[3]
    try:
        receipt = validate(args.manifest.resolve(), repo=repo, deployment_contract=args.deployment_contract.resolve(),
                           verify_files=not args.no_file_check)
    except ManifestError as exc:
        print(f"usage_manifest: INVALID: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
