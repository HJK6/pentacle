"""Real socket/store proof; only the provider/tmux counterpart is synthetic."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from comms import Comms
from ledger import Ledger
from notify import Notify
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
from _shared.operator_auth import OperatorCredentialRegistry
from tools import daily_retro as retro
from tools.live_window import authenticated_operator_connection

CHAT = "fixture-chat:assistant"
A, B = "fixture:a", "fixture:b"


@pytest.fixture
def daily_retro_surface(tmp_path, monkeypatch):
    @asynccontextmanager
    async def surface():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        notify = server = composite = None
        delivered, killed = [], []
        try:
            rows = {}
            for name in ("a", "b", "worker"):
                rows[name] = await store.open_session("fixture", name, provider="codex", role="assistant" if name != "worker" else "worker",
                                                     visibility="visible" if name != "worker" else "hidden", pane_status="pane_alive",
                                                     effective_model="gpt-6-sol", effective_effort="medium")
                await store.grant_stream_token("fixture", name, hashlib.sha256(f"token-{name}".encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
            sessions = Sessions(store, local_host="fixture")
            await sessions.refresh()

            async def terminate(name):
                killed.append(name)
                return "ok", "", {"reap_status": "reaped", "survivors": []}

            monkeypatch.setattr(sessions, "_terminate_pane", terminate)
            comms = Comms(store, sessions, None, attachment_root=tmp_path / "attachments")

            async def provider(plan):
                delivered.append(plan.display_text)
                return True, 1, False, "codex", None

            monkeypatch.setattr(comms, "_attempt_send_delivery", provider)
            ledger = Ledger(store, sessions)
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, comms=comms, ledger=ledger, local_host="fixture")
            registry = OperatorCredentialRegistry(tmp_path / "auth/credentials.json")
            registry.initialize()
            _, envelope = registry.issue("pentacle", label="owned isolated fixture")
            token_path = tmp_path / "operator-token"
            token_path.write_text(envelope)
            server.operator_credential_registry = registry
            notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
            await notify.start()
            server.notify = notify
            server.handlers.update(notify.wire_handlers())

            async def closed(stream_id, *, session_generation=None, reason="fixture_close"):
                await ledger.resolve_awaiters_on_close(stream_id, session_generation=session_generation, reason=reason)
                await notify.expire_questions_for_closed_producer(stream_id, generation=session_generation)

            sessions.set_awaiter_resolver(closed)
            composite_config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1", "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": A,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": rows["a"]["session_generation"],
            })
            composite = AssistantComposite(store, config=composite_config)
            await composite.load_binding()
            await composite.ensure_projection()
            server.assistant_composite = composite
            port = await server.bind()
            assert port != 7791 and CHAT.startswith("fixture-")
            url = f"ws://127.0.0.1:{port}"
            monkeypatch.setenv("AGENT_ORCH_WS_URL", url)
            monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
            monkeypatch.setenv("PENTACLE_STREAM_ID", A)
            monkeypatch.setenv("AGENT_ORCH_STREAM_ID", A)
            monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "token-a")
            memory = tmp_path / "memory"
            for folder in ("completed", "deprecated"):
                (memory / "work" / folder).mkdir(parents=True)
            work = memory / "work/in_progress/fixture/spec.md"
            work.parent.mkdir(parents=True)
            work.write_text("---\nid: spec_fixture\nstatus: in_progress\ntype: spec\n---\n\n# Fixture work\n")
            settings = retro.Settings(memory, tmp_path / "state", url, token_path, "fixture", isolated=True)

            async def operator(payload):
                def call():
                    with authenticated_operator_connection(url, token_path, 5) as connection:
                        return connection.rpc(payload)
                return await asyncio.to_thread(call)

            async def bind_b(close_a=False):
                changed = await composite.rebind({"request_id": "fixture-rebind-a-b", "expected_revision": 0, "target_stream_id": B,
                                                  "target_generation": rows["b"]["session_generation"]}, actor_stream_id=A)
                assert changed["new_binding"]["stream_id"] == B
                if close_a:
                    result = await operator({"type": "close", "host": "fixture", "session_name": "a",
                                             "expected_generation": rows["a"]["session_generation"], "reason": "fixture_restart"})
                    assert result["type"] == "close.ok", result
                monkeypatch.setenv("PENTACLE_STREAM_ID", B)
                monkeypatch.setenv("AGENT_ORCH_STREAM_ID", B)
                monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "token-b")

            yield SimpleNamespace(settings=settings, store=store, sessions=sessions, notify=notify, server=server,
                                  composite=composite, rows=rows, operator=operator, bind_b=bind_b,
                                  delivered=delivered, killed=killed, work=work, comms=comms)
        finally:
            if composite:
                await composite.stop()
            if server:
                await server.close()
            if notify:
                await notify.stop()
            store.stop()
    return surface


def fixture_proposal():
    return {"id": "fixture-repair", "scope": "Write the expected fixture output", "citations": ["spec_fixture_source"],
            "title": "Approve fixture repair?", "body": "The isolated fixture output is missing. Write its expected content.",
            "options": [{"label": "Approve", "value": "approve"}, {"label": "Reject", "value": "reject"}],
            "owner": B, "checkpoint": "2026-09-29", "success_measure": "expected fixture bytes exist"}


def test_restart_pending_then_answer_once(daily_retro_surface):
    async def run():
        async with daily_retro_surface() as s:
            pipeline = retro.Pipeline(s.settings)
            first = await pipeline.decision("spec_fixture", fixture_proposal())
            old = first["attempts"][0]["question_id"]
            await s.bind_b(close_a=True)
            recovered = await pipeline.decision("spec_fixture", fixture_proposal())
            new = recovered["attempts"][-1]["question_id"]
            assert old != new
            assert (await retro.wsclient.prompt_status_once(pipeline.config, old))["question"]["state"] == "expired"
            assert (await retro.wsclient.prompt_status_once(pipeline.config, new))["question"]["state"] == "open"
            answered = await s.operator({"type": "prompt.answer", "question_id": new, "selections": ["approve"]})
            assert answered["type"] == "prompt.answer.ok", answered
            result = await pipeline.decision("spec_fixture", fixture_proposal())
            assert result["state"] == "answered" and result["answer"]["selections"] == ["approve"]
            assert len(result["attempts"]) == 2
            # Observe a real bounded fixture outcome, not just an approval row.
            outcome = s.settings.state_root / "approved-output.txt"
            outcome.parent.mkdir(parents=True, exist_ok=True)
            outcome.write_text("expected fixture result\n")
            records = retro.proposals(s.work.read_text())
            records[result["id"]].update(outcome={"path": str(outcome), "sha256": hashlib.sha256(outcome.read_bytes()).hexdigest()}, state="shipped")
            retro.save_proposals(s.work, s.work.read_bytes(), records)
            assert outcome.read_text() == "expected fixture result\n"
            assert not await s.store.fetch_session_event_tail(CHAT, limit=20)
    asyncio.run(run())


def test_answer_before_close_and_material_scope_change(daily_retro_surface):
    async def run():
        async with daily_retro_surface() as s:
            pipeline = retro.Pipeline(s.settings)
            first = await pipeline.decision("spec_fixture", fixture_proposal())
            qid = first["attempts"][0]["question_id"]
            assert (await s.operator({"type": "prompt.answer", "question_id": qid, "selections": ["reject"]}))["type"] == "prompt.answer.ok"
            await s.bind_b(close_a=True)
            recovered = await pipeline.decision("spec_fixture", fixture_proposal())
            assert recovered["answer"]["selections"] == ["reject"] and len(recovered["attempts"]) == 1
            changed = fixture_proposal(); changed["scope"] = "A larger fixture output change"
            pending = await pipeline.decision("spec_fixture", changed)
            assert pending["state"] == "pending" and "answer" not in pending
            assert pending["attempts"][0]["stale_answer_refused"]
    asyncio.run(run())


def test_current_delivery_receipt_and_quiet_review(daily_retro_surface):
    async def run():
        async with daily_retro_surface() as s:
            pipeline = retro.Pipeline(s.settings)
            manifest = retro.collect(s.settings, retro.datetime.fromisoformat("2026-09-28T05:00:00-05:00"))
            packet = {"run_id": manifest["run_id"], "dispositions": [], "candidates": []}
            final = {"packet": packet, "packet_hash": retro.digest(packet), "report": {"report_id": "fixture-report"}}
            root = s.settings.state_root / "runs" / manifest["run_id"]
            retro.atomic(root / "astra.json", final)
            first = await pipeline.deliver(manifest, final)
            await pipeline.deliver(manifest, final)
            assert len(s.delivered) == 1 and first["attempts"][0]["target"] == A
            await s.bind_b()
            await pipeline.deliver(manifest, final)
            assert len(s.delivered) == 2
            reviewed = await pipeline.record_review(manifest["run_id"], {"packet_hash": final["packet_hash"], "dispositions": []})
            assert reviewed["actor"]["stream_id"] == B
            await pipeline.deliver(manifest, final)
            assert len(s.delivered) == 2
            assert not await s.store.fetch_session_event_tail(CHAT, limit=20)
            assert not await s.notify._db.call("list_agent_questions")
    asyncio.run(run())


def test_unattended_auth_allowlist_and_generation_cleanup(daily_retro_surface):
    async def run():
        async with daily_retro_surface() as s:
            transport = retro.ProducerTransport(s.settings)
            assert transport.ALLOWED == {"spawn", "await_spawn", "await_report", "close", "assistant.binding", "send.receipt.get", "send"}
            for verb in ("prompt.ask", "assistant.publish", "assistant.rebind", "authority.request", "send_image"):
                with pytest.raises(ValueError, match="allowlist"):
                    await transport.call({"type": verb})
            for stream, gen in [(B, s.rows["b"]["session_generation"]), ("fixture:worker", "stale")]:
                with pytest.raises(ValueError, match="ownership"):
                    await transport.close_once(None, stream, expected_generation=gen)
            path = s.settings.state_root / "runs/2026-09-28/sol.json"
            retro.atomic(path, {"stream_id": "fixture:worker", "generation": "old-generation"})
            stale = await transport.close_once(None, "fixture:worker", expected_generation="old-generation")
            assert stale["type"] == "close.already_closed" and stale["session"]["session_generation"] != "old-generation" and not s.killed
            assert (await s.store.fetch_session("fixture", "worker"))["status"] == "open"
            retro.atomic(path, {"stream_id": "fixture:worker", "generation": s.rows["worker"]["session_generation"]})
            for generation in (None, "", "stale"):
                payload = {"type": "close", "host": "fixture", "session_name": "worker", "reason": "report_terminate"}
                if generation is not None:
                    payload["expected_generation"] = generation
                with pytest.raises(ValueError, match="generation ownership"):
                    await transport.call(payload)
            assert not s.killed and (await s.store.fetch_session("fixture", "worker"))["status"] == "open"
            closed = await transport.close_once(None, "fixture:worker", expected_generation=s.rows["worker"]["session_generation"])
            assert closed["type"] == "close.ok" and closed["session"]["status"] == "closed"
            with pytest.raises(ValueError, match="retained REPORT"):
                await transport.send_once(None, {"text": "REPORT daily-retro malicious", "host": "fixture", "session_name": "a"})
            producer = retro.Pipeline(s.settings, transport)
            binding = await producer.binding()
            assert binding["stream_id"] == A
            manifest = retro.collect(s.settings, retro.datetime.fromisoformat("2026-09-28T05:00:00-05:00"))
            packet = {"run_id": manifest["run_id"], "dispositions": [], "candidates": []}
            final = {"packet": packet, "packet_hash": retro.digest(packet), "report": {"report_id": "fixture-report"}}
            await producer.deliver(manifest, final)
            await producer.deliver(manifest, final)
            assert len(s.delivered) == 1 and not await s.store.fetch_session_event_tail(CHAT, limit=20)
    asyncio.run(run())


def test_hidden_top_level_self_closes_on_durable_report(daily_retro_surface, monkeypatch):
    async def run():
        async with daily_retro_surface() as s:
            # Provider counterpart admits the exact typed spawn flag; the real
            # Ledger owns completion/close, not a fixture close shortcut.
            row = await s.store.open_session("fixture", "astra", provider="codex", role="worker", visibility="hidden",
                                             self_close_on_completion=True, effective_model="gpt-6-astra", effective_effort="high")
            await s.store.grant_stream_token("fixture", "astra", hashlib.sha256(b"token-astra").hexdigest(), STREAM_TOKEN_HASH_VERSION)
            await s.sessions.refresh()
            monkeypatch.setenv("PENTACLE_STREAM_ID", "fixture:astra")
            monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "fixture:astra")
            monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "token-astra")
            result = await retro.wsclient.report_once(s.settings.rpc(), {
                "type": "report", "from_stream_id": "fixture:astra", "msg_id": 0, "status": "done",
                "report_id": "fixture-astra-terminal", "summary": "Fixture final packet.",
                "findings": [], "next_action": "Producer retains packet.", "extras": {"daily_retro": {"run_id": "fixture"}},
            })
            assert result["type"] == "report.ok" and result.get("closed"), result
            closed = await s.store.fetch_session("fixture", "astra")
            assert closed["status"] == "closed" and closed["session_generation"] == row["session_generation"]
            transport = retro.ProducerTransport(s.settings)
            retro.atomic(s.settings.state_root / "runs/2026-09-28/astra.json", {
                "stream_id": "fixture:astra", "generation": row["session_generation"], "packet": {}, "failed": True})
            response = await transport.close_once(None, "fixture:astra", expected_generation=row["session_generation"])
            assert response["type"] == "close.already_closed" and response["session"]["status"] == "closed"
    asyncio.run(run())


@pytest.mark.timeout(1300)
@pytest.mark.skipif(not os.environ.get("DAILY_RETRO_WORKERS_CONFIG"), reason="explicit runtime-window worker config required")
def test_real_worker_rehearsal(daily_retro_surface, monkeypatch, tmp_path):
    """Opt-in only: never post synthetic questions to the real worker endpoint."""
    async def run():
        async with daily_retro_surface() as s:
            fixtures_path = Path(__file__).parents[1] / "fixtures/daily_retro_sources.json"
            fixtures = json.loads(fixtures_path.read_text())
            day = (retro.datetime.now(retro.timezone.utc).astimezone(retro.ZONE).date() - retro.timedelta(days=1)).isoformat()
            for name, body in fixtures["retros"].items():
                path = s.settings.memory_root / "work/completed" / name / "spec.md"
                path.parent.mkdir(parents=True)
                path.write_text(f"---\nid: spec_fixture_{name}\ntype: spec\nstatus: completed\ncompleted_at: '{day}'\n---\n\n## Retro\n{body}\n")
            for name, text in fixtures["files"].items():
                path = s.settings.memory_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            owner = s.settings.memory_root / "work/in_progress/existing-owner/spec.md"
            owner.parent.mkdir(parents=True)
            owner.write_text("---\nid: spec_fixture_existing_owner\ntype: spec\nstatus: in_progress\nowner: fixture:existing\n---\n\n# Active timer fix\nAccepting owner: fixture:existing. Next checkpoint: tomorrow. Success measure: both DST calendar days pass. Existing authorization covers this work.\n")
            test_config = tmp_path / "test-config.json"
            test_config.write_text(json.dumps({"timezone": retro.ZONE.key, "memory_root": str(s.settings.memory_root),
                                               "state_root": str(s.settings.state_root), "ws_url": s.settings.ws_url,
                                               "token_path": str(s.settings.token_path), "host": "fixture", "isolated": True}))

            async def assistant_counterpart(plan):
                assert plan.display_text.startswith("REPORT daily-retro ready"), plan.display_text
                roots = list((s.settings.state_root / "runs").glob("*/astra.json"))
                assert len(roots) == 1
                final = retro.read(roots[0])
                result = {"packet_hash": final["packet_hash"], "dispositions": [
                    {"id": c["id"], "disposition": "no_change", "reason": "Fictional fixture reviewed; real decision path proven separately on isolated surface."}
                    for c in final["packet"]["candidates"]]}
                await retro.Pipeline(s.settings).record_review(roots[0].parent.name, result)
                s.delivered.append(plan.display_text)
                return True, 1, False, "codex", None

            monkeypatch.setattr(s.comms, "_attempt_send_delivery", assistant_counterpart)
            tool = Path(os.environ.get("DAILY_RETRO_TOOL", retro.__file__)).resolve()
            expected = os.environ.get("DAILY_RETRO_TOOL_SHA256")
            if expected:
                assert hashlib.sha256(tool.read_bytes()).hexdigest() == expected
            evidence = Path(os.environ.get("DAILY_RETRO_EVIDENCE", str(tmp_path / "evidence"))).resolve()
            worker_config = Path(os.environ["DAILY_RETRO_WORKERS_CONFIG"]).resolve()
            process = await asyncio.create_subprocess_exec(sys.executable, str(tool), "rehearse", "--config", str(test_config),
                                                           "--workers-config", str(worker_config), "--evidence-dir", str(evidence),
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=1200)
            assert process.returncode == 0, stderr.decode()[-3000:]
            receipt = json.loads(stdout)
            assert receipt["review"] and receipt["review"]["collection_to_decision_ready_seconds"] >= 0
            assert len(s.delivered) == 1 and not await s.store.fetch_session_event_tail(CHAT, limit=20)
            assert not await s.notify._db.call("list_agent_questions")
            stages = [retro.read(p) for p in (s.settings.state_root / "runs").glob("*/*.json") if p.name in {"sol.json", "astra.json"}]
            assert len(stages) == 2 and all(stage["closed"] for stage in stages)
            evidence.mkdir(parents=True, exist_ok=True)
            # Preserve exact sources/reports/state outside shared memory, not just
            # ephemeral pytest paths mentioned by the compact receipt.
            import shutil
            shutil.copytree(s.settings.state_root / "runs", evidence / "runs")
            shutil.copytree(s.settings.memory_root, evidence / "fixture-memory")
            retro.atomic(evidence / "fixture-receipt.json", {"fixtures_sha256": hashlib.sha256(fixtures_path.read_bytes()).hexdigest(),
                                                            "tool_sha256": hashlib.sha256(tool.read_bytes()).hexdigest(),
                                                            "sources": len(fixtures["retros"]), "workers": len(stages), "cleanup_closed": 2,
                                                            "synthetic_production_questions": 0, "composite_messages": 0})
    asyncio.run(run())
