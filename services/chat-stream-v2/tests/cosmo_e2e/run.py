"""Reproducible start / seed / run / teardown for the Cosmo E2E Foundation
harness, emitting the infrastructure proof receipt (A).

Usage (from services/chat-stream-v2, under .venv):
    env -u TMUX ./.venv/bin/python -m tests.cosmo_e2e.run \
        [--out <receipt.json>] [--gate-evidence-dir <dir with {unit,smoke}.junit.xml>]

Steps:
  start    - build the real Foundation server on an allocated loopback port with
             disposable DB/blob/config/creds roots and a disposable scoped cred
  seed     - one committed turn over the authorized WIRE paths (scoped send ->
             accept_input -> authorized assistant.publish by the backend seat)
  run      - assert the text oracle (user msg + reply + receipt + one stub push)
  teardown - close the server (ALWAYS), drop the disposable root, read back port
             release and root removal

The full RED/GREEN proof is the pytest suite via:
  env -u TMUX ./.venv/bin/python tools/run_gate.py unit
  env -u TMUX ./.venv/bin/python tools/run_gate.py smoke
Cosmo rows need COSMO_SERVER_DIR (else they importorskip).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket as _socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tests.cosmo_e2e import harness as H

SERVICE_DIR = Path(__file__).resolve().parents[2]
FOUNDATION_BASE = "71a764d59e736ba313226413affa2641f1f3e916"
COSMO_BASE = "09ffbf6b29e90aaf1242284577b06cf581f473cf"


def _git_sha(cwd: str) -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
    except Exception:
        return "unknown"


def _is_ancestor(base: str, head_cwd: str) -> bool:
    try:
        subprocess.check_call(["git", "merge-base", "--is-ancestor", base, "HEAD"],
                              cwd=head_cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _harness_sha() -> str:
    h = hashlib.sha256()
    for rel in ("tests/cosmo_e2e/harness.py", "tests/cosmo_e2e/cases.json",
                "tests/smoke/test_cosmo_e2e_walk.py", "tests/test_cosmo_e2e_cosmo_rows.py",
                "tests/test_cosmo_e2e_isolation.py"):
        h.update((SERVICE_DIR / rel).read_bytes())
    return h.hexdigest()


def _gate_evidence_digests(gate_dir: str | None) -> dict:
    out: dict[str, str] = {}
    if not gate_dir:
        return out
    base = Path(gate_dir)
    for tier in ("unit", "smoke"):
        junit = base / f"{tier}.junit.xml"
        if junit.exists():
            out[f"{tier}.junit.xml"] = hashlib.sha256(junit.read_bytes()).hexdigest()
    return out


async def _start_seed_run_teardown() -> dict:
    # The outer cleanup scope opens BEFORE the temp root is allocated, so there is
    # no unprotected window: any exception after mkdtemp (incl. between allocation
    # and harness construction) is caught by the outer finally, which removes the
    # root when it exists.  If mkdtemp itself raises, tmp stays None (nothing to
    # remove).
    tmp = None
    port = pid = None
    port_released = False
    try:
        tmp = Path(tempfile.mkdtemp(prefix="cosmo-e2e-"))
        hz = H.FoundationHarness(tmp)
        try:
            await hz.start(bind=True)
            port, pid = hz.port, os.getpid()
            # seed one committed turn over the authorized WIRE paths.
            async with hz.mobile_client() as mob, hz.backend_seat() as seat:
                await mob.rpc("send", request_id="r-seed", to_stream_id=H.DAFF_CHAT,
                              text="receipt seed", msg_id="m-seed")
                did = await hz.resolved_dispatch_id("m-seed")
                r1 = await seat.publish(dispatch_id=did, reply_to_message_id="m-seed", message="seed reply")
                tail = await hz.store.fetch_session_event_tail(H.DAFF_CHAT, limit=20)
                users = [e for e in tail if "input_identity" in e["raw"]]
                pubs = [e for e in tail if "publish_kind" in e["raw"]]
                receipt_ok = (await hz.store.get_send_receipt(H.DAFF_CHAT, "r-seed")) is not None
                oracle = {
                    "duplicate_false": r1["duplicate"] is False,
                    "one_user_message": len(users) == 1,
                    "one_published_reply": len(pubs) == 1,
                    "receipt_retrievable": receipt_ok,
                    "one_stub_push": len(hz.push_sent) == 1,
                }
        finally:
            # destroy is NESTED so it runs even if stop() raises.
            try:
                await hz.stop()      # ALWAYS close the server, on success or failure
            finally:
                hz.destroy()         # ALWAYS remove the harness's disposable root
    finally:
        # Backstop: guarantee the temp root is gone on EVERY path, including a
        # failure between allocation and harness construction.  Guarded for the
        # case where mkdtemp itself never ran (tmp is None).
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    # teardown read-back: port released, disposable root removed.
    if port is not None:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
        port_released = True
    return {
        "port": port, "pid": pid,
        "disposable_root_pattern": "tempfile.mkdtemp(prefix='cosmo-e2e-')",
        "disposable_root_removed": not tmp.exists(), "port_released_after_close": port_released,
        "oracle": oracle, "oracle_all_green": all(oracle.values()),
    }


def build_receipt(gate_evidence_dir: str | None = None) -> dict:
    run = asyncio.run(_start_seed_run_teardown())
    cosmo_dir = H.cosmo_server_dir()
    candidate_sha = _git_sha(str(SERVICE_DIR))
    return {
        "receipt": "cosmo-e2e-foundation-harness/infrastructure-proof/A",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec": "spec_pentacle__cosmo_e2e_foundation_harness_2026_10",
        # Identity: the base is asserted to be an ancestor of the candidate; the
        # candidate SHA is reported separately (never relabeled as the base).
        "foundation_commit": FOUNDATION_BASE,
        "foundation_base_is_ancestor_of_candidate": _is_ancestor(FOUNDATION_BASE, str(SERVICE_DIR)),
        "candidate_sha": candidate_sha,
        "cosmo_server_sha": COSMO_BASE,
        "cosmo_server_sha_present": bool(cosmo_dir and _is_ancestor(COSMO_BASE, cosmo_dir)
                                         if cosmo_dir else False),
        "harness_sha256": _harness_sha(),
        "gate_evidence_digests": _gate_evidence_digests(gate_evidence_dir),
        "scenario_seed": "cases.json (10 rows); fixtures photo.png/voice.m4a (sha256 in manifest)",
        "stub_boundaries": [
            "transcriber backend poster (STUB:transcriber-backend-poster) — real BlobStore/Transcriber used",
            "cosmo push Expo transport (STUB:expo-push-transport) + CosmoPushTokens table (STUB:cosmo-push-table)",
            "backend delivery dispatch callback (fail-once injection seam)",
        ],
        "real_boundaries": [
            "server.Server accept loop + handlers (loopback bind, auth_v2 + stream-token hello)",
            "store.Store", "blobs.BlobStore (fixture bytes)",
            "assistant_composite.AssistantComposite.publish (authorized assistant.publish path)",
            "operator_auth scoped credential", "cosmo_server create_app (calendar/list, undo window)",
        ],
        "app_build": "pending (Cosmo lead)",
        "app_profile": "pending (Cosmo lead)",
        "maestro_input": "pending (Cosmo lead)",
        "run": run,
        "credentials": "redacted (disposable scoped credential issued under tmp root; never logged)",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None, help="path to write the receipt JSON")
    ap.add_argument("--gate-evidence-dir", default=None,
                    help="dir holding {unit,smoke}.junit.xml to bind gate-evidence digests")
    args = ap.parse_args()
    receipt = build_receipt(args.gate_evidence_dir)
    text = json.dumps(receipt, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n")
        print(f"receipt written: {args.out}")
    else:
        print(text)
    return 0 if receipt["run"]["oracle_all_green"] else 1


if __name__ == "__main__":
    sys.exit(main())
