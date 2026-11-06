"""Exact runner wrapper + installed CLI protocol against an isolated real daemon.

Only credential storage and the FD pane sink are counterparts. No prompts/providers.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from server import Server
from sessions import Sessions
from store import Store, STREAM_TOKEN_HASH_VERSION
import household_due_reminders as tool


class DeskSink:
    def __init__(self):
        self.receipts = {}
        self.calls = []
        self.assistant_ingress_policy = None

    async def tell(self, msg):
        key = msg["tell_id"]
        if key not in self.receipts:
            self.calls.append(dict(msg))
            self.receipts[key] = {"type": "tell.ok", "tell_id": key, "delivery_status": "delivered",
                                  "action_committed": True, "ledger_row_id": len(self.calls)}
        return self.receipts[key]


def test_exact_cli_auth_publication_replay_rebind_and_blocker(tmp_path, monkeypatch):
    original_run = tool.subprocess.run
    def checked_run(*args, **kwargs):
        result = original_run(*args, **kwargs)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return result
    monkeypatch.setattr(tool.subprocess, "run", checked_run)
    async def journey():
        store = Store(str(tmp_path / "sessions.db")); store.start()
        sink = DeskSink(); server = None; composite = None
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
            sessions.assistant.front_desk = composite
            await sessions.refresh()
            server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, comms=sink, local_host=host)
            server.assistant_composite = composite
            port = await server.bind()
            # Redirect only the CLI's credential-store location to fixture storage.
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
