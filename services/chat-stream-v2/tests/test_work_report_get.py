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
            for verb in ("list_sessions", "session_close", "send", "notification.create", "report", "work.owed", "unknown.verb"):
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
            # A1.4: a named reader credential plus seat identity is ambiguous.
            assert code == 1 and response["error_code"] == "unavailable"
            # The installed publisher and reader launcher strip seat identity.
            env.pop("AGENT_ORCH_STREAM_ID")
            env.pop("AGENT_ORCH_STREAM_TOKEN")
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


def test_typed_qa_evidence_is_service_only_and_minimized(tmp_path, monkeypatch):
    """The installed reader gets immutable review evidence, never opaque report payload."""
    token = tmp_path / 'reader-token'
    token.write_text('synthetic-reader-secret')
    token.chmod(0o600)
    monkeypatch.setenv('PENTACLE_WORK_REPORT_READER_TOKEN_FILE', str(token))

    async def run():
        store = Store(':memory:'); store.start()
        server = None
        try:
            sessions = await setup(store)
            await store.update_session('host-a', 'reporter', spec_ids=['spec_demo__item_00'], spec_binding_provenance=[{
                'spec_id': 'spec_demo__item_00', 'provenance': 'spawn_explicit',
                'granting_principal': 'operator', 'granted_at': '2026-10-22T19:00:00Z'}])
            await sessions.refresh()
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot('host-a:reporter')
            review = {'candidate_identity': 'a' * 40, 'reviewed_scope': '{"exact":"manifest"}',
                      'gate_evidence_digest': 'b' * 64, 'commission_msg_id': 0,
                      'opaque_private_field': 'must-not-return'}
            saved = await store.put_report(payload('qa-evidence', qa_verdict='accept', target_sha='a' * 40,
                extras={'qa_review': review, 'private_extra': 'must-not-return'},
                details={'private_body': 'must-not-return'}), routing_snapshot=snapshot)
            await store.submit(lambda conn: conn.execute(
                "INSERT INTO v2_qa_commissions (reviewer,generation,msg_id,coordinator,coordinator_generation,"
                "spec_id,surface,cycle,payload_hash,created_at,phase) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (saved['from_stream_id'], saved['session_generation'], 0, 'host-a:parent', 'parent-generation',
                 'spec_demo__item_00', 'rule-release', 1, 'fixture-commission-hash', 1, 'code')))
            server = Server(store=store, sessions=sessions)
            peer = Peer('127.0.0.1')
            async def rpc(kind, **extra):
                return (await server._dispatch(json.dumps({'type': kind, 'request_id': 'lookup', **extra}),
                                                websocket=peer))[0]
            assert (await rpc('hello', from_stream_id='work-report-reader', stream_token='synthetic-reader-secret',
                             subscribe={'snapshot': False, 'mode': 'rpc'}))['type'] == 'ready'
            response = await rpc('work.report.get', report_id=saved['report_id'])
            expected_review = {k: review[k] for k in ('candidate_identity', 'reviewed_scope', 'gate_evidence_digest', 'commission_msg_id')}
            assert response.get('qa_review') == expected_review, {'response': response, 'saved': saved}
            assert response['qa_verdict'] == 'accept'
            assert response['target_sha'] == 'a' * 40
            assert response['session_generation'] == saved['session_generation']
            assert response['from_stream_id'] == saved['from_stream_id']
            assert set(response) == {'type', 'request_id', 'report_id', 'status', 'from_stream_id',
                'provenance_qualified_spec_ids', 'created_at', 'qa_verdict', 'target_sha',
                'session_generation', 'qa_review'}
            assert 'must-not-return' not in json.dumps(response)
            # Existing authenticated metadata readers keep their original response contract.
            legacy = await server._on_work_report_get({'type': 'work.report.get', 'report_id': saved['report_id'],
                                                       '_auth_context': {'operator_authenticated': True}})
            assert set(legacy) == {'type', 'report_id', 'status', 'from_stream_id',
                                  'provenance_qualified_spec_ids', 'created_at'}
            # Immutable generation, spec provenance and independent commission are required.
            for field, value in [('generation', 'other-generation'), ('spec_id', 'spec_demo__other'),
                                 ('coordinator', saved['from_stream_id']), ('phase', 'spec')]:
                prior = await store.submit(lambda conn: conn.execute(
                    'SELECT generation,spec_id,coordinator,phase FROM v2_qa_commissions').fetchone())
                await store.submit(lambda conn: conn.execute(f'UPDATE v2_qa_commissions SET {field}=?', (value,)))
                refused = await rpc('work.report.get', report_id=saved['report_id'])
                assert 'qa_review' not in refused
                await store.submit(lambda conn: conn.execute(f'UPDATE v2_qa_commissions SET {field}=?', (prior[field],)))
            await store.submit(lambda conn: conn.execute('DELETE FROM v2_qa_commissions'))
            assert 'qa_review' not in await rpc('work.report.get', report_id=saved['report_id'])
        finally:
            if server: await server.close()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize('change', [
    {'qa_review': None}, {'qa_review': {'reviewed_scope': 'legacy prose'}},
    {'qa_review': {'candidate_identity': 'c' * 40, 'reviewed_scope': 'scope', 'gate_evidence_digest': 'b' * 64}},
    {'qa_review': {'candidate_identity': 'a' * 40, 'reviewed_scope': '', 'gate_evidence_digest': 'b' * 64}},
    {'qa_review': {'candidate_identity': 'a' * 40, 'reviewed_scope': 'scope', 'gate_evidence_digest': 'bad'}},
    {'qa_review': {'candidate_identity': 'a' * 40, 'reviewed_scope': 'scope', 'gate_evidence_digest': 'b' * 64,
                   'commission_msg_id': True}},
])
def test_missing_or_malformed_qa_evidence_keeps_metadata_contract(change):
    async def run():
        store = Store(':memory:'); store.start()
        try:
            sessions = await setup(store)
            snapshot = await Ledger(store, sessions=sessions)._report_routing_snapshot('host-a:reporter')
            saved = await store.put_report(payload('legacy-qa', qa_verdict='accept', target_sha='a' * 40,
                extras=change), routing_snapshot=snapshot)
            server = Server(store=store, sessions=sessions)
            response = await server._on_work_report_get({'type': 'work.report.get', 'report_id': saved['report_id'],
                '_auth_context': {'service_authenticated': True, 'service_actor': 'work-report-reader'}})
            assert set(response) == {'type', 'report_id', 'status', 'from_stream_id',
                                    'provenance_qualified_spec_ids', 'created_at'}
        finally:
            store.stop()
    asyncio.run(run())
