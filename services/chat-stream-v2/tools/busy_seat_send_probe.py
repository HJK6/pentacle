#!/usr/bin/env python3
"""Reproduce a send to a busy provider seat at the daemon level, cheaply.

Spawns one low-cost hidden seat on THIS host, holds it in a working turn for
about a minute with a single foreground shell command, sends it one probe
message through the ordinary daemon ``send`` path, and compares the daemon's
send result with the pane's native-queue ground truth. No UI is involved.

    python3 services/chat-stream-v2/tools/busy_seat_send_probe.py --provider claude
    python3 services/chat-stream-v2/tools/busy_seat_send_probe.py --provider codex

Verdicts (JSON on stdout, exit code):
  queued_reported (0)  pane shows the probe in the native queue and the send
                       result carries ``provider_queued: true``.
  queued_unreported (1) pane shows it queued but the result does not say so
                       (the spec_pentacle__chat_queued_message_state_2026_10 bug).
  not_queued (2)       the probe never appeared in the native queue (the seat
                       was not busy, or the TUI submitted it at once).
  harness_error (3)    spawn, busy-state or send failed; no product verdict.

The seat is closed in a ``finally`` path unless ``--keep`` is given. Pane
capture uses local tmux; for a peer ``--host`` pass ``--ssh USER@HOST`` to capture there.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid

MODELS = {"claude": ("claude-sonnet-5-5", "low"), "codex": ("gpt-6-luna", "low")}
# Native-queue chrome as rendered by the provider TUIs (Claude Code 2.1.x,
# codex-cli 0.15x). Deliberately independent of the daemon's own predicates so
# the probe is ground truth for them, not a restatement.
QUEUE_MARKERS = {
    "claude": ("ctrl+x ctrl+s to send now", "Press up to edit queued messages"),
    "codex": ("messages to be submitted after next tool call",),
}


def run(cmd: list[str], timeout: float = 120) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


def last_json(text: str) -> dict:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


SSH_TARGET: str | None = None


def capture(session_name: str) -> str:
    cmd = ["tmux", "capture-pane", "-p", "-t", f"={session_name}:"]
    if SSH_TARGET:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_TARGET,
               "PATH=/opt/homebrew/bin:/usr/local/bin:$PATH " + " ".join(f"'{c}'" for c in cmd)]
    rc, out = run(cmd, timeout=20)
    return out if rc == 0 else ""


def pane_queued(pane: str, provider: str, token: str) -> bool:
    lowered = pane.lower()
    return token in pane and any(marker.lower() in lowered for marker in QUEUE_MARKERS[provider])


def wait_until(predicate, timeout_s: float, poll_s: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=sorted(MODELS), required=True)
    parser.add_argument("--host", default=None, help="spawn host (default: daemon's choice for this caller)")
    parser.add_argument("--busy-seconds", type=int, default=75)
    parser.add_argument("--spec-id", action="append", default=[])
    parser.add_argument("--keep", action="store_true", help="leave the seat open for inspection")
    parser.add_argument("--ssh", default=None, metavar="USER@HOST",
                        help="capture the pane over ssh when --host is a peer (e.g. user@peer-host)")
    args = parser.parse_args()
    global SSH_TARGET
    SSH_TARGET = args.ssh

    model, effort = MODELS[args.provider]
    token = f"busyprobe-{uuid.uuid4().hex[:10]}"
    busy_cmd = f"python3 -c 'import time; time.sleep({args.busy_seconds})'"
    prompt = (
        "You are a disposable test target. Do not read instruction files or memory. "
        f"Run exactly this foreground shell command and wait for it to finish: {busy_cmd} "
        "Then reply PROBE_IDLE. For any later message reply only: ack."
    )
    report: dict = {"provider": args.provider, "model": model, "effort": effort, "token": token}
    code = 3
    try:
        code = _probe(args, report, token, model, effort, prompt)
    finally:
        stream_id = str(report.get("stream_id") or "")
        if stream_id and not args.keep:
            rc, _ = run(["agent-orch", "close", "--operator-confirm", stream_id], timeout=60)
            report["cleanup"] = "closed" if rc == 0 else f"close_failed rc={rc}"
            if rc != 0:
                # A probe that leaves its hidden seat open is never a green run.
                report.update(verdict="harness_error", stage="cleanup")
                code = 3
        report["exit_code"] = code
        print(json.dumps(report, indent=2))
    return code


def _probe(args: argparse.Namespace, report: dict, token: str, model: str, effort: str, prompt: str) -> int:
    spawn_key = f"busy-seat-probe-{token}"
    report["spawn_key"] = spawn_key
    spawn = ["agent-orch", "spawn", "--provider", args.provider, "--model", model, "--effort", effort,
             "--idempotency-key", spawn_key,
             "--visibility", "hidden", "--no-self-close-on-completion",
             "--objective", "busy-seat send probe target (disposable)", "--initial-prompt", prompt]
    if args.host:
        spawn += ["--host", args.host]
    for spec_id in args.spec_id:
        spawn += ["--spec-id", spec_id]
    try:
        rc, out = run(spawn, timeout=300)
    except subprocess.TimeoutExpired:
        # Spawn admission can stall; record the daemon's view and recover
        # any seat it created so cleanup still runs.
        _, status_out = run(["agent-orch", "spawn-status", spawn_key], timeout=60)
        report["spawn_status"] = status_out[-800:]
        out = status_out
    spawned = last_json(out)
    stream_id = str(spawned.get("stream_id") or "")
    if not stream_id:
        report.update(verdict="harness_error", stage="spawn", detail=out[-500:])
        return 3
    session_name = stream_id.split(":", 1)[1]
    report["stream_id"] = stream_id

    # Busy = the foreground command is visibly running (the command text is
    # on screen and the provider shows its interrupt affordance).
    def busy() -> bool:
        pane = capture(session_name)
        return "time.sleep(" in pane and ("esc to interrupt" in pane.lower() or "Running" in pane)
    if not wait_until(busy, 120):
        report.update(verdict="harness_error", stage="busy", pane_tail=capture(session_name)[-800:])
        return 3
    time.sleep(3)

    sent_at = time.time()
    rc, out = run(["agent-orch", "send", stream_id, "1", f"probe {token} while busy"], timeout=120)
    result = last_json(out)
    report["send_result"] = {k: result.get(k) for k in (
        "delivery", "submission_confirmed", "provider_queued", "reason", "receipt_id", "request_id", "ok")}
    report["send_latency_s"] = round(time.time() - sent_at, 2)
    if not result:
        report.update(verdict="harness_error", stage="send", detail=out[-500:])
        return 3

    queued_seen = wait_until(lambda: pane_queued(capture(session_name), args.provider, token), 10, 0.5)
    report["pane_queued"] = queued_seen
    if queued_seen:
        # The queue must drain into a real turn once the busy command ends.
        drained = wait_until(lambda: not pane_queued(capture(session_name), args.provider, token),
                             args.busy_seconds + 90, 2)
        report["queue_drained"] = drained
    if not queued_seen:
        report["verdict"] = "not_queued"
        return 2
    if result.get("provider_queued") is True:
        report["verdict"] = "queued_reported"
        return 0
    report["verdict"] = "queued_unreported"
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.TimeoutExpired as exc:
        print(json.dumps({"verdict": "harness_error", "detail": str(exc)}))
        sys.exit(3)
