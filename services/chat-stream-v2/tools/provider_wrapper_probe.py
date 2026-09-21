#!/usr/bin/env python3
"""Claude fleet cells plus independent tell/USER wrapper assertions.

Run only in the coordinator's runtime window. Reuses the fleet smoke's nonce
authentication, generation-bound ownership, measurements and verified cleanup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))
from tools import spawn_fleet_smoke as smoke  # noqa: E402
from tools.live_window import OwnedSessionRegistry  # noqa: E402


def assert_wrapper_receipt(
    receipt: dict, events: list[dict], *, stream_id: str, body: str, watermark: int,
) -> dict:
    """Independent oracle: assistant replies alone cannot satisfy this gate."""
    assert receipt.get("submission_confirmed") is True, "tell submission not confirmed"
    matches = [event for event in events if (
        event.get("stream_id") == stream_id and event.get("provider") == "claude"
        and event.get("kind") == "USER" and event.get("text") == body
        and int(event.get("daemon_seq", 0)) > watermark
    )]
    assert len(matches) == 1, "expected one post-watermark USER with exact display text"
    event = matches[0]
    wrapper = event.get("provider_wrapper") or {}
    assert wrapper.get("kind") == "claude_pasted_content", "missing wrapper kind"
    assert wrapper.get("provenance") == "grammar", "missing grammar provenance"
    identifier = wrapper.get("id")
    assert isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]+", identifier), "invalid wrapper ID"
    expected_raw = (
        f'\n\n<pasted_content id="{identifier}">\n{body}'
        f'\n</pasted_content id="{identifier}">\n'
    )
    assert (event.get("raw") or {}).get("provider_content") == expected_raw, "raw envelope not retained"
    return event


def runtime_binding(checkout: Path, sha: str, pid: int) -> dict:
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    assert actual == sha, "installed artifact SHA mismatch"
    assert str(checkout / "services/chat-stream-v2/main.py") in command, "PID is not the installed daemon"
    paths = ["provider_wrappers.py", "claude_jsonl_norm.py", "submission_events.py", "comms.py"]
    digests = {}
    for name in paths:
        rel = f"services/chat-stream-v2/{name}"
        installed = (checkout / rel).read_bytes()
        committed = subprocess.check_output(["git", "show", f"{sha}:{rel}"], cwd=checkout)
        assert installed == committed, f"installed bytes differ: {rel}"
        digests[rel] = hashlib.sha256(installed).hexdigest()
    return {"candidate_sha": sha, "daemon_pid": pid, "command": command, "source_sha256": digests}


def probe_cell(host: str, mode: str, args, output: Path) -> dict:
    registry_path = output / "owned.json"
    registry = OwnedSessionRegistry(registry_path)
    evidence: dict = {"host": host, "provider": "claude", "prompt_mode": mode, "rpc": []}
    closed: list[str] = []
    registered: list[str] = []
    try:
        with smoke._operator_connection(args.url, args.token_path, args.timeout, registry) as (
            rpc, wait_ready, wait_event, register_owned, close_owned,
        ):
            def capture_rpc(payload, prefix):
                response = rpc(payload, prefix)
                evidence["rpc"].append({"request": payload, "response": response})
                return response

            def close(stream):
                close_owned(stream)
                closed.append(stream)

            def register(payload, response):
                register_owned(payload, response)
                registered.append(response["stream_id"])

            def validate(stream, marker):
                metrics = close_owned.validate(stream, marker)
                # Preserve every existing fleet-smoke predicate before tell.
                assert all(value.get("passed", True) for value in metrics.values()
                           if isinstance(value, dict)), "fleet smoke predicate failed"
                before = rpc({"type": "request_stream_events", "stream_id": stream, "limit": 500}, "request_stream_events")
                watermark = max((int(e.get("daemon_seq", 0)) for e in before["events"]), default=0)
                tell_marker = f"PENTACLE_WRAPPER_{uuid.uuid4().hex}"
                body = f"Reply exactly {tell_marker} and do nothing else."
                evidence.update(watermark=watermark, tell_body=body)
                receipt = capture_rpc({"type": "tell", "stream_id": stream, "message": body,
                                       "tell_id": uuid.uuid4().hex}, "tell")
                replay = rpc({"type": "request_stream_events", "stream_id": stream, "limit": 500}, "request_stream_events")
                evidence["events"] = replay["events"]
                evidence["wrapper_event"] = assert_wrapper_receipt(
                    receipt, replay["events"], stream_id=stream, body=body, watermark=watermark,
                )
                inventory = rpc({"type": "list_sessions"}, "list_sessions")
                row = next(r for r in inventory["active"] if r["stream_id"] == stream)
                evidence["provider_binding"] = {key: row.get(key) for key in (
                    "host", "stream_id", "session_generation", "pane_pid", "observer_binding",
                )}
                assert row.get("pane_pid"), "provider live PID missing"
                wait_event(stream, tell_marker)
                metrics["provider_wrapper"] = {"passed": True}
                return metrics

            evidence["cell"] = smoke.run_cell(
                host, "claude", mode, rpc=capture_rpc, wait_ready=wait_ready, wait_event=wait_event,
                verify_teardown=close, register_owned=register, close_owned=close,
                prepare_owned_spawn=register_owned.prepare_owned_spawn, validate_session=validate,
                rescue_teardown=lambda stream: smoke._rescue_teardown(
                    args.url, args.token_path, args.timeout, stream, registry),
            )
            evidence["outcome"] = "PASS"
    except Exception as exc:
        # run_cell preserves the predicate exception as the cause.
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        evidence.update(outcome="FAIL", classification=(
            "CLEANUP_FAIL" if str(exc).startswith("teardown:") else
            "PRODUCT_FAIL" if isinstance(cause, AssertionError) else "HARNESS_ERROR"
        ), error=str(exc))
    finally:
        ownership = json.loads(registry_path.read_text()) if registry_path.exists() else {"owned": []}
        evidence["cleanup"] = {"closed_streams": closed, "remaining_owned": ownership["owned"],
                               "expected_count": len(registered),
                               "closed_count": len(registered) - len(ownership["owned"])}
        if ownership["owned"]:
            evidence.update(outcome="FAIL", classification="CLEANUP_FAIL")
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hosts", nargs="+", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--daemon-pid", type=int, required=True)
    parser.add_argument("--runtime-checkout", type=Path, required=True)
    parser.add_argument("--url", default=smoke.DEFAULT_URL)
    parser.add_argument("--token-path", type=Path, default=smoke.DEFAULT_TOKEN_PATH)
    parser.add_argument("--timeout", type=float, default=smoke.DEFAULT_TIMEOUT)
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    result = {"runtime": runtime_binding(args.runtime_checkout, args.candidate_sha, args.daemon_pid), "cells": []}
    for host in args.hosts:
        for mode in smoke.PROMPT_MODES:
            output = args.evidence_dir / f"{host}-{mode}"
            output.mkdir(exist_ok=True)
            cell = probe_cell(host, mode, args, output)
            result["cells"].append(cell)
            (args.evidence_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            if cell["outcome"] != "PASS":
                return 1  # preserve first failure; no retry or further live mutations
    result["runtime_after"] = runtime_binding(args.runtime_checkout, args.candidate_sha, args.daemon_pid)
    (args.evidence_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
