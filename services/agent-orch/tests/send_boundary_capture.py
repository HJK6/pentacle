"""Run the same isolated wire journeys on the pinned base and the candidate.

Only synthetic identities, credentials and sockets are used. Run with -I to
capture a fixture; pytest imports run_cell for candidate comparisons.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import site
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent)]
# Homebrew's provisioned dependencies are in its user site; -I excludes the
# directory, so admit that installed dependency location explicitly.
sys.path.append(site.getusersitepackages())
from agent_orch import cli, config as config_module, work_report_cli, wsclient as w
from agent_orch.config import Config
from _shared import operator_auth

BASE_SHA = "889ca9dba944b725328ab38143f5d7043131e961"
IDS = {"CD": "altum-bot-cd", "BK": "amaterasu:wmi-pg-dailybackup",
       "RD": "work-report-reader", "SEAT": "fixture:seat", "OP": None}
CALLERS = ("none", "stream_token", "identity_token", "both")
PATHS = (
    "persistent", "reconnect", "persistent-request", "replay", "snapshot",
    "rpc", "retry", "rpc-identity", "notification", "resolve-dedup", "asset",
    "notification-await", "inbox", "receipt", "rename", "close", "reparent",
    "lifecycle", "consent", "grant", "upload", "upload-prompt", "fetch", "thread",
    "persistent-spawn", "persistent-schedule", "persistent-send", "persistent-tell",
    "persistent-close", "persistent-report", "report-once", "assistant", "screen",
    "external-work", "asset-list", "send-once", "spawn-once", "schedule-once",
    "spec", "status", "prompt",
)
NONCE = "ERERERERERERERERERERERERERERERERERERERERERE"
ENVELOPE = operator_auth.encode_envelope("00000000-0000-4000-8000-000000000001", "pentacle", b"\x11" * 32)


class Socket:
    def __init__(self, *, envelope=False, fail=False):
        self.raw = []
        self.queue = ([{"type": "welcome", "auth": {"operator": {
            "protocol_version": 2, "scheme": "hmac-sha256-v2", "nonce": NONCE}}}]
                      if envelope else [])
        self.closed = False
        self.fail = fail

    async def send(self, raw):
        assert not self.closed, "send on closed fixture socket"
        self.raw.append(raw)
        frame = json.loads(raw)
        kind = frame["type"]
        request_id = frame.get("request_id")
        if kind == "hello":
            self.queue += [{"type": "ping"}, {"type": "ready"} if "auth_v2" in frame
                           else {"type": "snapshot", "hosts": ["fixture"], "sessions": []}]
        elif kind == "upload_blob_init":
            self.queue.append({"type": "upload_blob.init.ok", "request_id": request_id})
        elif kind == "upload_prompt_blob_init":
            self.queue.append({"type": "upload_prompt_blob.init.ok", "request_id": request_id})
        elif kind in {"upload_blob_chunk", "upload_prompt_blob_chunk"}:
            self.queue.append({"type": kind.removesuffix("_chunk") + ".ok", "request_id": request_id})
        elif kind == "fetch_blob":
            self.queue.append({"type": "fetch_blob.ok", "request_id": request_id, "final": True,
                               "content_b64": "eA=="})
        elif kind != "pong":
            self.queue.append({"type": ("thread.read.ok" if kind == "thread.read" else kind + ".ok"),
                               "request_id": request_id})

    async def recv(self):
        if not self.queue:
            raise AssertionError("fixture response exhausted")
        frame = self.queue.pop(0)
        if self.fail and frame["type"] not in {"snapshot", "ready", "ping"}:
            self.fail = False
            raise TimeoutError("fixture retry")
        return json.dumps(frame)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.queue:
            return await self.recv()
        raise StopAsyncIteration


class Connection:
    def __init__(self, socket):
        self.socket = socket

    def __await__(self):
        async def connected():
            return self.socket
        return connected().__await__()

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_):
        await self.socket.close()


def cell_key(path, identity, state, caller):
    return "/".join((path, identity, state, caller))


def run_cell(path, identity, state="F1", caller="none", *, form="bearer", seat_env=False):
    sockets = []
    error = None
    result = None
    output = []
    with tempfile.TemporaryDirectory(prefix="send-boundary-") as tmp, ExitStack() as stack:
        token_path = Path(tmp) / "stream-token"
        env = {"AGENT_ORCH_HOST_ID": "fixture", "AGENT_ORCH_TOKEN": "OPERATOR-sentinel",
               "AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS": "2"}
        stream_id = IDS[identity]
        if stream_id:
            env["AGENT_ORCH_STREAM_ID"] = stream_id
        if state.startswith("F") and state not in {"F6", "F7"}:
            env["AGENT_ORCH_STREAM_TOKEN_FILE"] = str(token_path)
            if state == "F5":
                token_path.mkdir()
            elif state != "F2":
                token_path.write_text("" if state == "F4" else "FILE-sentinel")
                token_path.chmod(0o644 if state == "F3" else 0o600)
        if state == "F6":
            env["AGENT_ORCH_STREAM_TOKEN"] = "LEGACY-sentinel"
        if state == "EMPTY":
            env["AGENT_ORCH_STREAM_TOKEN"] = ""
        if state in {"R1", "PADDED", "R2", "SPACE"}:
            env.pop("AGENT_ORCH_STREAM_ID", None)
            env["PENTACLE_WORK_REPORT_READER_TOKEN"] = {
                "R1": "READER-sentinel", "PADDED": " READER-sentinel ", "R2": "", "SPACE": "  "}[state]
        if seat_env:
            env["AGENT_ORCH_STREAM_ID"] = "fixture:seat"
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        original_read = Path.read_text
        if state == "F8":
            def unreadable(self, *args, **kwargs):
                if self == token_path:
                    raise PermissionError("fixture unreadable")
                return original_read(self, *args, **kwargs)
            stack.enter_context(patch.object(Path, "read_text", unreadable))
        stack.enter_context(patch.object(w.uuid, "uuid4", lambda: "00000000-0000-4000-8000-000000000002"))
        stack.enter_context(patch.object(config_module, "_read_json_config", lambda: {}))
        stack.enter_context(patch.object(w, "_retry_next_delay", lambda *a: (0, None) if a[-1] < 2 else (None, "retry_exhausted")))

        def connect(*_, **__):
            sock = Socket(envelope=(path == "envelope" and form == "envelope"),
                          fail=(path == "retry" and not sockets))
            sockets.append(sock)
            return Connection(sock)

        stack.enter_context(patch.object(w.websockets, "connect", connect))
        cfg = Config(ws_url="ws://fixture.invalid", token=ENVELOPE if form == "envelope" else "OPERATOR-sentinel",
                     host_id="fixture", runtime_dir=Path(tmp))
        payload = {"type": "fixture", "request_id": "fixture-request"}
        if stream_id and path not in {"reader", "envelope"}:
            payload["from_stream_id"] = stream_id
        if caller in {"stream_token", "both"}:
            payload["stream_token"] = "CALLER-stream-sentinel"
        if caller in {"identity_token", "both"}:
            payload["identity_token"] = "CALLER-identity-sentinel"

        async def journey():
            nonlocal result
            client = w.WebsocketClient(cfg)
            if path in {"persistent", "reconnect"}:
                await client._connect_and_read()
                if path == "reconnect":
                    await client._connect_and_read()
            elif path in {"persistent-request", "replay"} or path.startswith("persistent-"):
                sock = connect().socket
                client._ws = sock
                if path.startswith("persistent-") and path not in {"persistent-request"}:
                    captured = []
                    client._rpc = lambda prefix, body: captured.append(body)
                    if path == "persistent-spawn":
                        client.spawn(host="fixture", provider="codex", parent_stream_id=stream_id, role="worker", phase=None, visibility="hidden")
                    elif path == "persistent-schedule":
                        client.schedule_insert(fires_at_utc="2030-01-01T00:00:00Z", provider="codex", target_host="fixture", visibility="hidden", created_by_stream_id=stream_id)
                    elif path == "persistent-send":
                        client.send_rpc("fixture", "target", "text", from_stream_id=stream_id)
                    elif path == "persistent-tell":
                        client.tell("fixture-tell", stream_id, "fixture:target", "text")
                    elif path == "persistent-close":
                        client.close_rpc("fixture", "target", "done", from_stream_id=stream_id)
                    elif path == "persistent-report":
                        client.report("fixture-report", stream_id, 1, "done")
                    body = captured[0]
                    body.setdefault("request_id", "fixture-request")
                else:
                    body = payload
                pending = w.PendingRpc("fixture", threading.Event(), body, retry_eligible=True,
                                       retry_policy=w._rpc_retry_policy_from_env(1),
                                       retry_deadline=time.monotonic() + 10, attempts=1, send_owner="fixture")
                client._pending[body["request_id"]] = pending
                if path == "replay":
                    pending.send_owner = None
                    pending.reconnect_waiting = True
                    await client._resume_pending_after_reconnect()
                else:
                    await client._send_pending_payload(body["request_id"], pending, body, owner="fixture")
            elif path == "snapshot":
                await w._fetch_snapshot_async(cfg, 1)
            elif path in {"rpc", "retry"}:
                payload["type"] = "thread.read" if path == "retry" else "fixture"
                await w._one_shot_rpc(cfg, payload, prefix="thread" if path == "retry" else "fixture", timeout=1, from_stream_id=stream_id)
            elif path == "rpc-identity":
                ws = await w._connect_rpc_ready(cfg, from_stream_id=stream_id,
                    identity_token="CALLER-identity-sentinel" if caller in {"identity_token", "both"} else None)
                await ws.close()
            elif path == "reader":
                result = await work_report_cli.get_report(cfg, "fixture-report", timeout=1)
            elif path == "envelope":
                body = {"type": "dashboard.owner.transfer", "request_id": "fixture-request"}
                if stream_id:
                    body["from_stream_id"] = stream_id
                await w.dashboard_once(cfg, body, timeout=1)
            elif path == "notification":
                payload["type"] = "notification.create"
                await w.notification_create_once(cfg, payload, timeout=1)
            elif path == "resolve-dedup":
                payload["type"] = "notification.resolve_by_dedup"
                await w.notification_resolve_by_dedup_once(cfg, payload, timeout=1)
            elif path == "asset":
                payload["type"] = "asset.publish"
                await w.asset_publish_once(cfg, payload, timeout=1)
            elif path == "notification-await":
                await w.notification_await_once(cfg, "fixture-note", timeout=1)
            elif path == "inbox":
                payload.update(type="inbox", stream_id=stream_id or "fixture:target")
                await w._inbox_once_attempt(cfg, payload, timeout=1)
            elif path == "receipt":
                await w.send_receipt_once(cfg, "fixture:target", "fixture-request", timeout=1)
            elif path == "rename":
                await w.rename_once(cfg, "fixture", "target", "name", timeout=1)
            elif path == "close":
                await w.close_once(cfg, "fixture:target", from_stream_id=stream_id, timeout=1)
            elif path == "reparent":
                await w.reparent_once(cfg, "fixture:target", "fixture:parent", from_stream_id=stream_id, timeout=1)
            elif path == "lifecycle":
                await w.assistant_lifecycle_once(cfg, payload, from_stream_id=stream_id, timeout=1)
            elif path == "consent":
                await w.consent_once(cfg, "consent.status", payload, from_stream_id=stream_id, timeout=1)
            elif path == "grant":
                await w.grant_token_once(cfg, "fixture:target", from_stream_id=stream_id, timeout=1)
            elif path == "upload":
                await w.upload_blob_once(cfg, b"x", timeout=1)
            elif path == "upload-prompt":
                await w.upload_prompt_blob_once(cfg, b"x", timeout=1)
            elif path == "fetch":
                await w._fetch_blob_once_attempt(cfg, "a" * 64, request_id="fixture-request", timeout=1)
            elif path == "thread":
                # Invoke the real preflight and command-local error route.
                result = cli.main(["thread", "fixture:target", "--timeout", "1"])
            elif path == "spec":
                await w.spec_update_once(cfg, "fixture:target", "add", "spec_fixture", from_stream_id=stream_id, timeout=1)
            elif path == "status":
                await w.status_card_once(cfg, stream_id or "fixture:target", {}, timeout=1)
            else:
                funcs = {"report-once": (w.report_once, "report"), "assistant": (w.assistant_once, "assistant.transfer"),
                         "screen": (w.screen_lease_once, "screen.lease.acquire"), "external-work": (w.external_work_once, "external_work.show"),
                         "asset-list": (w.asset_list_once, "asset.list"), "send-once": (w.send_once, "send"),
                         "spawn-once": (w.spawn_once, "spawn"), "schedule-once": (w.schedule_once, "schedule.insert"),
                         "spec": (w.spec_update_once, "spec.update"), "status": (w.status_card_once, "status_card"),
                         "prompt": (w.prompt_ask_once, "prompt.ask")}
                fn, kind = funcs[path]
                payload["type"] = kind
                if path == "schedule-once":
                    payload["created_by_stream_id"] = stream_id
                await fn(cfg, payload, timeout=1)

        # thread owns asyncio.run itself; execute it outside the journey loop.
        import contextlib
        import io
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if path == "thread":
                    result = cli.main(["thread", "fixture:target", "--timeout", "1"])
                else:
                    asyncio.run(journey())
        except Exception as exc:
            error = type(exc).__name__
        finally:
            # Every socket, including injected failure and pending-send sockets,
            # is harness-owned and torn down even when admission fails.
            for sock in sockets:
                asyncio.run(sock.close())
        output = [stdout.getvalue(), stderr.getvalue()]
    return {"frames": [sock.raw for sock in sockets], "error": error,
            "result": result, "output": output, "closed": all(sock.closed for sock in sockets)}


def capture():
    cells = {}
    for path in PATHS:
        for identity in ("SEAT", "OP"):
            for state in [f"F{i}" for i in range(1, 9)]:
                for caller in CALLERS:
                    cells[cell_key(path, identity, state, caller)] = run_cell(path, identity, state, caller)
    for identity in ("SEAT", "OP"):
        for state in ("F1", "F7"):
            for form in ("bearer", "envelope"):
                cells[cell_key("envelope", identity, state, form)] = run_cell("envelope", identity, state, form=form)
    for state in ("R1", "PADDED", "R2", "SPACE", "R3"):
        cells[cell_key("reader", "RD", state, "none")] = run_cell("reader", "OP", state)
    for identity in ("SEAT", "OP"):
        cells[cell_key("reader", identity, "F7", "none")] = run_cell("reader", identity, "F7")
        for state in ("F1", "F2", "F4", "F6", "F7", "EMPTY"):
            cells[cell_key("thread", identity, state, "none")] = run_cell("thread", identity, state)
    return {"base_sha": BASE_SHA, "cells": cells}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fixture = capture()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
    errors = {}
    for cell in fixture["cells"].values():
        if cell["error"]:
            errors[cell["error"]] = errors.get(cell["error"], 0) + 1
    print(json.dumps({"base_sha": BASE_SHA, "cells": len(fixture["cells"]), "errors": errors}))
