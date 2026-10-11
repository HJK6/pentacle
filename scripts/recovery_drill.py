"""Isolated cold-recovery drill: open a captured daemon state copy in-process and answer one message.

Test-style, not a service. Starts no provider, tmux or host roster; refuses to run when any
producer/push key is configured. Usage:
    python scripts/recovery_drill.py <state-dir> --composite-stream <host:session> --binding-name <name>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "services" / "chat-stream-v2"), str(ROOT / "services")]

PRODUCER_KEYS = (
    "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN", "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE",
    "PENTACLE_SYSTEM_PRODUCER_STREAM_ID", "PENTACLE_BOT_MESSAGING_STREAM_TOKEN_FILE",
    "PENTACLE_WMI_BACKUP_STREAM_TOKEN_FILE", "PENTACLE_REPORT_PRODUCER_CONFIG",
    "PENTACLE_DOT_TLS_PORT", "PENTACLE_DOT_TLS_CERT", "PENTACLE_DOT_TLS_KEY", "PENTACLE_DOT_TLS_BINDS",
)
DRILL_HOST, DRILL_SESSION = "drill-fd", "assistant"
DRILL_STREAM = f"{DRILL_HOST}:{DRILL_SESSION}"


class DrillRefused(RuntimeError):
    pass


def preflight(env, roster) -> None:
    configured = [key for key in PRODUCER_KEYS if env.get(key)]
    if configured:
        raise DrillRefused(f"producer_or_push_key_present: {configured}")
    if roster:
        raise DrillRefused(f"real_host_roster_present: {sorted(roster)}")


def _direct(db: Path, sql: str, args=()):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


async def run_drill(state_dir: Path, *, composite_stream: str, binding_name: str, env=None, roster=(),
                    remove_state_dir: bool = True) -> dict:
    env = os.environ if env is None else env
    preflight(env, roster)
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    from server import Server
    from sessions import Sessions
    from store import STREAM_TOKEN_HASH_VERSION, Store
    import websockets

    started = time.monotonic()
    db = state_dir / "sessions.db"
    result: dict = {"composite_stream": composite_stream}
    result["direct_reports"] = _direct(db, "SELECT COUNT(*) FROM v2_reports")[0]
    result["direct_lane_events"] = _direct(db, "SELECT COUNT(*) FROM v2_work_lane_events")[0]
    bound = _direct(db, "SELECT stream_id, generation FROM v2_assistant_direct_binding WHERE name=?", (binding_name,))
    holder = bound[0] if bound else DRILL_STREAM
    token = hashlib.sha256(os.urandom(16)).hexdigest()
    if bound:
        # The captured holder's real token is never read. In this isolated copy only, its stored hash is
        # replaced with a fresh fixture hash so the drill can speak as the holder (recorded as a gap).
        conn = sqlite3.connect(db)
        try:
            host, session = holder.split(":", 1)
            changed = conn.execute("UPDATE sessions SET token_hash=?, token_hash_version=? WHERE host=? "
                                   "AND session_name=? AND status='open'",
                                   (hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION,
                                    host, session)).rowcount
            conn.commit()
        finally:
            conn.close()
        result["holder_token_replaced_in_copy"] = changed == 1
    store = Store(str(db))
    store.start()
    server = composite = None
    port = None
    try:
        drill = await store.open_session(DRILL_HOST, DRILL_SESSION, provider="codex", role="assistant",
                                         effective_model="gpt-6-sol", effective_effort="high",
                                         pane_status="pane_alive")
        # assistant.rebind is authorized for the current holder, so the drill speaks as the captured
        # holder with a fresh fixture token in this isolated copy (spec deviation, recorded as a gap).
        if not bound:
            assert await store.grant_stream_token(*holder.split(":", 1), hashlib.sha256(token.encode()).hexdigest(),
                                                  STREAM_TOKEN_HASH_VERSION) == "ok"
        result["rebind_caller"] = holder
        drill_token = hashlib.sha256(os.urandom(16)).hexdigest()
        assert await store.grant_stream_token(DRILL_HOST, DRILL_SESSION,
                                              hashlib.sha256(drill_token.encode()).hexdigest(),
                                              STREAM_TOKEN_HASH_VERSION) == "ok"
        settings = {"PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                    "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": composite_stream}
        if bound:
            settings |= {"PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": bound[0],
                         "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": bound[1]}
        dispatched = []

        async def dispatch(route):
            dispatched.append(route)
            return {"delivery": "landed"}

        composite = AssistantComposite(store, config=AssistantCompositeConfig.from_env(settings), dispatch=dispatch)
        await composite.load_binding()
        await composite.ensure_projection()
        local = composite_stream.split(":", 1)[0]
        sessions = Sessions(store, local_host=local)
        await sessions.refresh()
        server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, local_host=local)
        server.assistant_composite = composite
        port = await server.bind()

        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            assert json.loads(await socket.recv())["type"] == "welcome"
            await socket.send(json.dumps({"type": "hello", "client": "recovery-drill",
                                          "stream_token": token, "from_stream_id": holder}))

            async def rpc(payload):
                await socket.send(json.dumps(payload))
                async with asyncio.timeout(5):
                    while True:
                        frame = json.loads(await socket.recv())
                        if frame.get("request_id") == payload["request_id"]:
                            return frame

            # (1) history: the daemon's view of the newest event equals the direct read.
            result["direct_newest_seq"] = _direct(
                db, "SELECT MAX(event_id) FROM session_event_tail WHERE stream_id=?", (composite_stream,))[0]
            tail = await store.fetch_session_event_tail(composite_stream, limit=1)
            result["daemon_newest_seq"] = tail[-1].get("daemon_seq") if tail else None
            assert result["daemon_newest_seq"] == result["direct_newest_seq"], result
            # (2) lanes and reports readable; the CLI run requires them non-empty.
            result["lanes_reports_nonempty"] = result["direct_reports"] > 0 and result["direct_lane_events"] > 0
            # (3) binding, then rebind to the drill holder.
            before = await rpc({"type": "assistant.binding", "request_id": "drill-binding"})
            result["binding_before"] = {k: before.get(k) for k in ("source", "stream_id", "generation", "revision")}
            moved = await rpc({"type": "assistant.rebind", "request_id": "drill-rebind",
                               "expected_revision": before.get("revision") or 0,
                               "target_stream_id": DRILL_STREAM,
                               "target_generation": drill["session_generation"]})
            assert moved["type"] == "assistant.rebind.ok", moved
            assert moved["new_binding"]["stream_id"] == DRILL_STREAM
            result["binding_after"] = moved["new_binding"]
            # (4) one message in, one correlated reply out.
            await server._on_send({"host": local, "session_name": composite_stream.split(":", 1)[1],
                                   "text": "recovery drill ping", "request_id": "drill-send",
                                   "optimistic_id": "drill-input",
                                   "_auth_context": {"operator_authenticated": True,
                                                     "operator_principal": "operator:drill"}})
            route = None
            for _ in range(300):
                route = await store.get_assistant_composite_route(stream_id=composite_stream,
                                                                  input_identity="drill-input")
                if route and route["routing_state"] == "resolved":
                    break
                await asyncio.sleep(0.01)
            assert route and route["route_target"] == DRILL_STREAM, route
            # The reply is published by the new holder over its own authenticated connection.
            async with websockets.connect(f"ws://127.0.0.1:{port}") as holder_socket:
                assert json.loads(await holder_socket.recv())["type"] == "welcome"
                await holder_socket.send(json.dumps({"type": "hello", "client": "recovery-drill-holder",
                                                     "stream_token": drill_token, "from_stream_id": DRILL_STREAM}))
                request = {"type": "assistant.publish", "request_id": "publish:" + route["dispatch_id"],
                           "composite_stream_id": composite_stream, "dispatch_id": route["dispatch_id"],
                           "reply_to_message_id": "drill-input", "publish_kind": "prose",
                           "response_state": "final", "message": "recovery drill reply",
                           "attachment_ids": [], "evidence_refs": []}
                await holder_socket.send(json.dumps(request))
                async with asyncio.timeout(5):
                    while True:
                        published = json.loads(await holder_socket.recv())
                        if published.get("request_id") == request["request_id"]:
                            break
            assert published["type"] == "assistant.publish.ok", published
            events = await store.fetch_session_event_tail(composite_stream, limit=10)
            assert any(event.get("text") == "recovery drill reply" for event in events)
            result["time_to_first_reply_s"] = round(time.monotonic() - started, 3)
    finally:
        if composite is not None:
            await composite.stop()
        if server is not None:
            await server.close()
        store.close() if hasattr(store, "close") else None
        if port is not None:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                result["port_closed"] = False
            except OSError:
                result["port_closed"] = True
        if remove_state_dir:
            shutil.rmtree(state_dir, ignore_errors=True)
            result["state_dir_removed"] = not state_dir.exists()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--composite-stream", required=True)
    parser.add_argument("--binding-name", required=True, help="assistant direct-binding name in the captured store")
    parser.add_argument("--keep", action="store_true", help="do not remove the state dir")
    args = parser.parse_args()
    result = asyncio.run(run_drill(args.state_dir, composite_stream=args.composite_stream, binding_name=args.binding_name,
                                   remove_state_dir=not args.keep))
    print(json.dumps(result, indent=1, default=str))
    return 0 if result.get("lanes_reports_nonempty") and result.get("port_closed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
