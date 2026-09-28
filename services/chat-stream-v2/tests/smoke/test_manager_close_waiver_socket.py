"""Manager close on a loopback daemon, scratch stores and owned real panes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json

import pytest
import websockets

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from tmux_transport import Tmux

HOST = "fixture-manager"
MANAGER = HOST + ":manager"
COMMAND = "echo ready; exec sleep 600"
REASON = "Retire disposable acceptance target without a terminal report"


@asynccontextmanager
async def daemon(tmp_path, *, lane_owned=False):
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
        if lane_owned:
            await sessions.open(HOST, "advisor", provider="codex", pane_status="pane_alive")
            server.assistant_composite = AssistantComposite(store, config=AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": HOST + ":assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": MANAGER,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": manager["session_generation"],
                "PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID": HOST + ":advisor",
            }))
        port = await server.bind()
        async with websockets.connect(f"ws://127.0.0.1:{port}") as socket:
            assert json.loads(await socket.recv())["type"] == "welcome"
            await socket.send(json.dumps({"type": "hello", "client": "manager-acceptance",
                                          "stream_token": token, "from_stream_id": MANAGER}))
            frames = [json.loads(await socket.recv()) for _ in range(2)]
            assert any(frame["type"] == "snapshot" for frame in frames)

            async def close(name, **extra):
                request = {"type": "close", "host": HOST, "session_name": name,
                           "from_stream_id": MANAGER, "stream_token": token,
                           "request_id": "close-" + name, "reason": REASON, **extra}
                await socket.send(json.dumps(request))
                async with asyncio.timeout(10):
                    while True:
                        frame = json.loads(await socket.recv())
                        if frame.get("request_id") == request["request_id"]:
                            return frame

            close.lane_rulings = server.lane_rulings
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


def test_unreported_lane_close_keeps_external_ruling(tmp_path):
    async def run():
        async with daemon(tmp_path, lane_owned=True) as (store, sessions, tmux, close):
            await tmux.new_session("target", COMMAND)
            target = await sessions.open(HOST, "target", provider="codex")
            await sessions.refresh()
            # Ownership is fixture state; close still crosses the authenticated
            # daemon socket and the actual external-ruling/report boundary.
            rulings = close.lane_rulings
            await rulings._record_ownership({
                "requester_stream_id": MANAGER,
                "requester_generation": (await store.fetch_session(HOST, "manager"))["session_generation"],
                "ruling_request_id": "fixture-lane-admission",
            }, HOST + ":target", target["session_generation"])
            reply = await close("target")
            await rulings.stop()
            assert reply["type"] == "close.pending_ruling", reply
            assert await tmux.has_session("target")
            assert (await store.fetch_session(HOST, "target"))["status"] == "open"
            ruling = await rulings._fetch(reply["ruling_request_id"])
            assert ruling["state"] == "pending"
            assert ruling["authority_stream_id"] == HOST + ":advisor"
            assert not any(row["action"] == "manager_close" and row["result"] == "applied"
                           for row in await store.lifecycle_authority_audit_rows(limit=20))
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


def test_lane_stale_generation_refuses_before_ruling_or_kill(tmp_path):
    async def run():
        async with daemon(tmp_path, lane_owned=True) as (store, sessions, tmux, close):
            await tmux.new_session("target", COMMAND)
            target = await sessions.open(HOST, "target", provider="codex")
            rulings = close.lane_rulings
            await rulings._record_ownership({
                "requester_stream_id": MANAGER,
                "requester_generation": (await store.fetch_session(HOST, "manager"))["session_generation"],
                "ruling_request_id": "fixture-admission",
            }, HOST + ":target", target["session_generation"])
            reply = await close("target", expected_generation="prior-generation")
            await rulings.stop()
            assert reply["type"] == "close.error" and reply["error_code"] == "lifecycle_generation_mismatch", reply
            assert await tmux.has_session("target")
            assert (await store.fetch_session(HOST, "target"))["status"] == "open"
            assert await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_assistant_lane_rulings").fetchone()[0]) == 0
    asyncio.run(run())
