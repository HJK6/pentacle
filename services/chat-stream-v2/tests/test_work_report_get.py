"""Accepted report lookup and the publisher's single-verb credential."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from ledger import Ledger
from server import Server
from store import Store
from test_report_disposition_context import setup, payload
from test_system_notification_producer import Peer


def test_report_reader_lookup_and_scope(tmp_path, monkeypatch):
    token = tmp_path / "reader-token"
    token.write_text("synthetic-reader-secret")
    token.chmod(0o600)
    monkeypatch.setenv("PENTACLE_WORK_REPORT_READER_TOKEN_FILE", str(token))

    async def run():
        store = Store(":memory:")
        store.start()
        server = Server(store=store)
        peer = Peer("127.0.0.1")

        async def rpc(kind, **extra):
            return (await server._dispatch(json.dumps({"type": kind, "request_id": "lookup", **extra}),
                                           websocket=peer))[0]

        try:
            hello = await rpc("hello", from_stream_id="work-report-reader", stream_token="synthetic-reader-secret",
                              subscribe={"snapshot": False, "mode": "rpc"})
            assert hello["type"] == "ready", hello
            assert server._client_system_producers[peer] == "work-report-reader"
            assert not server._operator_authenticated(peer)
            missing = await rpc("work.report.get", report_id="missing")
            assert missing["error_code"] == "not_found", missing
            for verb in ("list_sessions", "session_close", "send", "notification.create", "report", "unknown.verb"):
                assert (await rpc(verb))["error_code"] == "system_producer_forbidden"
            token.unlink()
            assert (await rpc("work.report.get", report_id="missing"))["error_code"] == "system_producer_auth_required"
        finally:
            store.stop()

    asyncio.run(run())


def test_real_socket_cli_accepted_and_rejected_reports(tmp_path, monkeypatch):
    token = tmp_path / "reader-token"
    token.write_text("synthetic-reader-secret")
    token.chmod(0o600)
    monkeypatch.setenv("PENTACLE_WORK_REPORT_READER_TOKEN_FILE", str(token))

    async def run():
        store = Store(":memory:")
        store.start()
        server = None
        try:
            sessions = await setup(store)
            await store.update_session("host-a", "reporter", spec_ids=["spec_demo__item_00"], spec_binding_provenance=[{
                "spec_id": "spec_demo__item_00", "provenance": "spawn_explicit",
                "granting_principal": "operator", "granted_at": "2026-10-22T19:00:00Z"}])
            await sessions.refresh()
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot("host-a:reporter")
            reporter = await store.fetch_session("host-a", "reporter")
            await store.record_report_rejection("host-a:reporter", session_generation=reporter["session_generation"],
                                                status="done", reason="synthetic rejected attempt")
            saved = await store.put_report(payload("accepted-after-rejection"), routing_snapshot=snapshot)
            await sessions.close("host-a", "reporter", close_kind="operator_close")
            server = Server(store=store, sessions=sessions, port=0)
            port = await server.bind()
            env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
                   "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_HOST_ID": "fixture",
                   "AGENT_ORCH_TOKEN": "synthetic-operator-must-not-leak",
                   "AGENT_ORCH_STREAM_ID": "arbitrary:seat", "AGENT_ORCH_STREAM_TOKEN": "wrong-seat-secret",
                   "PENTACLE_WORK_REPORT_READER_TOKEN": "synthetic-reader-secret",
                   "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli")}

            async def cli(report_id):
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "report", "get",
                    report_id, "--json", "--timeout", "3", env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(proc.communicate(), 8)
                assert not err, err.decode()
                return proc.returncode, json.loads(out)

            code, response = await cli(saved["report_id"])
            assert code == 0, response
            expected = {key: saved[key] for key in (
                "report_id", "status", "from_stream_id", "provenance_qualified_spec_ids", "created_at")}
            assert {k: v for k, v in response.items() if k not in {"type", "request_id"}} == expected
            assert response["provenance_qualified_spec_ids"] == ["spec_demo__item_00"]
            code, response = await cli("rejected-only-not-ingested")
            assert code == 1 and response["error_code"] == "not_found"
            env["PENTACLE_WORK_REPORT_READER_TOKEN"] = "bad-reader-secret"
            code, response = await cli(saved["report_id"])
            assert code == 1 and response["error_code"] == "unavailable"
        finally:
            if server:
                await server.close()
            store.stop()

    asyncio.run(run())


@pytest.mark.parametrize("bad_token", [False, True])
def test_reader_refuses_snapshot_spoof_and_unsafe_token_permissions(tmp_path, monkeypatch, bad_token):
    token = tmp_path / "reader-token"
    token.write_text("synthetic-reader-secret")
    token.chmod(0o644 if bad_token else 0o600)
    monkeypatch.setenv("PENTACLE_WORK_REPORT_READER_TOKEN_FILE", str(token))

    async def run():
        server = Server()
        for claim, subscribe in (("work-report-reader", {"snapshot": True}),
                                 ("arbitrary:seat", {"snapshot": False, "mode": "rpc"}),
                                 ("work-report-reader", {"snapshot": False, "mode": "rpc"})):
            if bad_token and claim != "work-report-reader":
                continue  # An unreadable file cannot identify another token claim.
            peer = Peer("127.0.0.1")
            frames = await server._dispatch(json.dumps({"type": "hello", "from_stream_id": claim,
                "stream_token": "synthetic-reader-secret", "subscribe": subscribe}), websocket=peer)
            if bad_token or claim != "work-report-reader" or subscribe.get("snapshot"):
                assert frames[0]["error_code"] == "system_producer_auth_required"
            else:
                assert [f["type"] for f in frames] == ["ready"]
    asyncio.run(run())


def test_unavailable_lookup_is_typed_and_bounded(tmp_path, monkeypatch):
    from agent_orch.config import Config
    from agent_orch.work_report_cli import get_report

    monkeypatch.setenv("PENTACLE_WORK_REPORT_READER_TOKEN", "synthetic-reader-secret")
    config = Config("ws://127.0.0.1:1", "", "fixture", tmp_path)
    response = asyncio.run(get_report(config, "anything", timeout=0.05))
    assert response["error_code"] == "unavailable"
