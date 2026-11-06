"""Exact runner wrapper + installed CLI protocol against an isolated real daemon.

Only credential storage and the FD pane sink are counterparts. No prompts/providers.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from comms import Comms
from ledger import Ledger
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
import household_due_reminders as tool


class DeskSink(Comms):
    """Keep real route/digest/ledger behavior; capture only the pane delivery."""
    def __init__(self, store, sessions):
        super().__init__(store, sessions, None)
        self.calls = []

    async def _deliver_tell(self, msg, tell_id, route, wire, digest):
        self.calls.append(dict(msg))
        reply = {"type": "tell.ok", "tell_id": tell_id, "delivery_status": "delivered",
                 "action_committed": True, "submission_confirmed": True}
        record = {"payload_digest": digest, "reply": reply, "delivery": {
            "tell_id": tell_id, "from_stream_id": msg["from_stream_id"],
            "to_stream_id": route["final_target"], "original_to_stream_id": route["original_target"],
            "text": wire, "delivery_status": "delivered", "submission_confirmed": True,
            **self._logical_receipt(msg.get("_logical_front_desk"))}}
        reply["ledger_row_id"] = await self.store.put_tell_delivery(tell_id, record)
        return reply


def test_exact_cli_auth_publication_replay_rebind_and_blocker(tmp_path, monkeypatch):
    original_run = tool.subprocess.run
    children = []
    def recorded_run(*args, **kwargs):
        children.append((args[0], kwargs["env"]))
        return original_run(*args, **kwargs)
    monkeypatch.setattr(tool.subprocess, "run", recorded_run)
    async def journey():
        store = Store(str(tmp_path / "sessions.db")); store.start()
        sink = None; server = None; composite = None
        try:
            host = "fixture-host"; first = "fixture-host:desk-a"; second = "fixture-host:desk-b"
            seats = {}
            for stream in (first, second):
                _, name = stream.split(":")
                seats[stream] = await store.open_session(host, name, provider="codex", role="assistant",
                    visibility="default", pane_status="pane_alive", bootstrap_state="ready",
                    effective_model="gpt-6.1-sol", effective_effort="medium")
                token = "synthetic-token-" + name
                await store.grant_stream_token(host, name, hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
                path = tmp_path / ".pentacle-stream-tokens" / (tool.digest(stream)[:24] + ".token")
                path.parent.mkdir(exist_ok=True); path.write_text(token); path.chmod(0o600)
            chat = "fixture-chat:assistant"
            composite = AssistantComposite(store, config=AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1", "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": chat,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": first,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": seats[first]["session_generation"],
            }))
            await composite.load_binding(); await composite.ensure_projection()
            # Seed the existing named binding as production already does after handoff.
            def seed_binding(db):
                db.execute("INSERT INTO v2_assistant_direct_binding VALUES(?,?,?,?,?)",
                    ("assistant", first, seats[first]["session_generation"], 0, "fixture"))
                db.commit()
            await store.submit(seed_binding)
            pushed = []
            async def push(**payload):
                pushed.append(payload)
            composite.reply_push = push
            sessions = Sessions(store, tmux=None, local_host=host)
            sink = DeskSink(store, sessions)
            sessions.assistant.front_desk = composite
            await sessions.refresh()
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, comms=sink, ledger=Ledger(store, sessions, sink), local_host=host)
            server.assistant_composite = composite
            port = await server.bind()
            # The real CLI authenticates only through these fixture seat token files.
            # Production credential bytes and HOME are never read or changed here.
            cli = tmp_path / "fixture_cli.py"
            cli.write_text("from agent_orch.cli import main\nraise SystemExit(main())\n")
            config = {"agent_orch": [sys.executable, str(cli)], "ws_url": f"ws://127.0.0.1:{port}",
                      "host_id": host, "seat_cwd": str(tmp_path), "composite_stream_id": chat,
                      "sessions_db": str(tmp_path / "sessions.db"), "binding_name": "assistant"}
            wrapper = tool.Publisher(config)
            wrapper.base_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "agent-orch") + ":" + str(Path(__file__).resolve().parents[2])
            key = "household-due:2026-11-12"; text = "Due today · Thu, Nov 12\n• Synthetic filter"
            first_reply = await asyncio.to_thread(wrapper.publish, key, text)
            assert first_reply["type"] == "assistant.publish.ok" and not first_reply["duplicate"]
            replay = await asyncio.to_thread(wrapper.publish, key, text)
            assert replay["duplicate"] and replay["event_id"] == first_reply["event_id"]
            # Commit a real BLOCKER but simulate losing its local receipt before handoff.
            await asyncio.to_thread(wrapper.tell, "household-due:blocker:fixture", "BLOCKER Synthetic Cosmo failure")
            assert len(sink.calls) == 1
            # Rebind the real binding store without a model or credential provisioning.
            binding = await composite.binding()
            await store.rebind_assistant(env_binding=composite._env_binding(), actor_stream_id=first,
                actor_generation=seats[first]["session_generation"], target_stream_id=second,
                target_generation=seats[second]["session_generation"], request_id="fixture-handoff",
                expected_revision=binding["revision"], authorized_spec_ids=frozenset())
            await store.update_session(host, "desk-a", status="closed", pane_status="pane_dead")
            await sessions.refresh()
            rebind_replay = await asyncio.to_thread(wrapper.publish, key, text)
            assert rebind_replay["duplicate"] and rebind_replay["event_id"] == first_reply["event_id"]
            events = [event for event in await store.fetch_session_event_tail(chat, limit=50) if event["kind"] == "ASSIST_TEXT"]
            assert len(events) == 1 and events[0]["text"] == text
            assert len(pushed) == 1
            receipt = await asyncio.to_thread(wrapper.tell, "household-due:blocker:fixture", "BLOCKER Synthetic Cosmo failure")
            assert receipt["type"] == "tell.ok" and len(sink.calls) == 1
            assert receipt["reconciled"] is True
            assert receipt["original_receipt"]["tell"]["from_stream_id"] == first
            ledger_calls = [(args, env) for args, env in children if args[2:4] == ["ledger", "get"]]
            assert len(ledger_calls) == 1
            assert ledger_calls[0][1]["AGENT_ORCH_STREAM_ID"] == second
            assert ledger_calls[0][1]["AGENT_ORCH_TOKEN"] == ""
            await asyncio.to_thread(wrapper.tell, "household-due:blocker:fixture", "BLOCKER Synthetic Cosmo failure")
            assert len(sink.calls) == 1
            # Missing current-seat credentials refuse outbound publication and tell.
            (tmp_path / ".pentacle-stream-tokens" / (tool.digest(second)[:24] + ".token")).unlink()
            import pytest
            with pytest.raises(tool.Failure, match="missing_private_file"):
                await asyncio.to_thread(wrapper.publish, "household-due:2026-11-13", text)
        finally:
            if server is not None:
                await server.close()
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(journey())
