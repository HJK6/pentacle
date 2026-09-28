"""Manager close on a loopback daemon, scratch stores and owned real panes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json

import pytest
import websockets

from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from tmux_transport import Tmux

HOST = "fixture-manager"
MANAGER = HOST + ":manager"
COMMAND = "echo ready; exec sleep 600"
REASON = "Retire disposable acceptance target without a terminal report"


@asynccontextmanager
async def daemon(tmp_path):
    store = Store(str(tmp_path / "sessions.db"))
    store.start()
    tmux = Tmux()
    sessions = Sessions(store, tmux=tmux, local_host=HOST)
    server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, local_host=HOST)
    try:
        await tmux.new_session("manager", COMMAND)
        manager = await sessions.open(HOST, "manager", provider="codex", role="lead",
                                      pane_status="pane_alive", bootstrap_state="ready")
        # Seed only operator consent/grant in scratch storage. The close itself
        # crosses the actual socket and authenticates a generation-bound token.
        await store.lifecycle_authority_mutate(
            {"action": "designate", "target_stream_id": MANAGER,
             "target_generation": manager["session_generation"], "expected_revision": 0,
             "reason": "Isolated manager acceptance", "request_id": "fixture-grant"},
            {"operator_authenticated": True, "operator_principal": "operator:fixture",
             "_consent_id": "fixture-consent"}, sessions.assistant.role)
        token = "fixture-manager-token"
        await store.grant_stream_token(HOST, "manager", hashlib.sha256(token.encode()).hexdigest(),
                                       STREAM_TOKEN_HASH_VERSION)
        port = await server.bind()
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            assert json.loads(await socket.recv())["type"] == "welcome"
            await socket.send(json.dumps({"type": "hello", "client": "manager-acceptance",
                                          "stream_token": token, "from_stream_id": MANAGER}))
            frames = [json.loads(await socket.recv()) for _ in range(2)]
            assert any(frame["type"] == "snapshot" for frame in frames)

            async def close(name):
                request = {"type": "close", "host": HOST, "session_name": name,
                           "from_stream_id": MANAGER, "stream_token": token,
                           "request_id": "close-" + name, "reason": REASON}
                await socket.send(json.dumps(request))
                async with asyncio.timeout(10):
                    while True:
                        frame = json.loads(await socket.recv())
                        if frame.get("request_id") == request["request_id"]:
                            return frame

            yield store, sessions, tmux, close
    finally:
        await server.close()
        await tmux.run("kill-server")
        rc, _ = await tmux.run("list-sessions")
        assert rc != 0, "owned tmux socket survived cleanup"
        store.stop()


@pytest.mark.parametrize("orphan", [False, True], ids=["idle-unreported", "offline-orphan"])
def test_manager_closes_unreported_generation_with_waiver_audit(tmp_path, orphan):
    async def run():
        async with daemon(tmp_path) as (store, sessions, tmux, close):
            if not orphan:
                await tmux.new_session("target", COMMAND)
            target = await sessions.open(HOST, "target", provider="codex",
                                         offline_since_ts="2026-09-27T00:00:00Z" if orphan else None)
            reply = await close("target")
            assert reply["type"] == "close.ok", reply
            assert (await store.fetch_session(HOST, "target"))["status"] == "closed"
            assert not await tmux.has_session("target")
            if orphan:
                assert reply["reap_status"] == "unknown", reply
            audit = await store.lifecycle_authority_audit_rows(limit=20)
            waiver = next(row for row in audit if row["action"] == "manager_close_report_waived")
            assert waiver["reason"] == REASON and waiver["request_id"] == "close-target"
            assert waiver["actor_identity"] == MANAGER and waiver["old_revision"] == 1
            assert waiver["target_generation"] == target["session_generation"]
            assert await store.find_report(HOST + ":target", statuses={"done"},
                                           session_generation=target["session_generation"]) is None
    asyncio.run(run())


def test_manager_closes_reported_missing_pane_without_false_reap(tmp_path):
    async def run():
        async with daemon(tmp_path) as (store, sessions, tmux, close):
            target = await sessions.open(HOST, "target", provider="codex")

            def terminal(conn):
                conn.execute("INSERT INTO v2_reports (report_id,from_stream_id,session_generation,status,summary,"
                             "created_at,ingested_at) VALUES (?,?,?,'done','done',?,?)",
                             ("fixture-report", HOST + ":target", target["session_generation"],
                              "2026-09-28T00:00:00Z", "2026-09-28T00:00:00Z"))
                conn.commit()

            await store.submit(terminal)
            reply = await close("target")
            assert reply["type"] == "close.ok", reply
            assert reply["reap_status"] == "unknown", reply
            assert (await store.fetch_session(HOST, "target"))["status"] == "closed"
            assert not await tmux.has_session("target")
    asyncio.run(run())
