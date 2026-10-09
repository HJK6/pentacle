"""Real daemon/notification/CLI confirmation journey on disposable stores."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from assistant_composite import AssistantComposite
from notification_answer_fixture import fixture
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from test_assistant_prose_mirror import ASSISTANT, _config_for


@pytest.mark.parametrize("transport", ["prompt", "notification"])
@pytest.mark.parametrize("delivered,comment", [(False, ""), (True, ""), (False, "A recorded comment"),
                                                (True, "A recorded comment")])
def test_confirm_survives_notice_and_comment_once(tmp_path, delivered, comment, transport):
    asyncio.run(_journey(tmp_path, delivered=delivered, comment=comment, transport=transport))


@pytest.mark.parametrize("refusal", ["decline", "unanswered", "comment_only", "wrong_lane", "wrong_action", "wrong_version"])
def test_confirmation_refusals(tmp_path, refusal):
    asyncio.run(_journey(tmp_path, delivered=True, comment="A recorded comment", refusal=refusal))


async def _journey(tmp_path, *, delivered, comment, refusal=None, transport="prompt"):
    async with fixture(tmp_path, host="fixture") as (notify, queue, comms, provider, sessions, store):
        producer = "fixture:v2-test"
        row = await store.fetch_session("fixture", "v2-test")
        composite = AssistantComposite(store, config=_config_for(producer, row["session_generation"]))
        await composite.ensure_projection()
        composite.work_lane_confirmation_reader = notify.work_lane_confirmation
        token = "confirmation-fixture-token"
        assert await store.grant_stream_token("fixture", "v2-test", hashlib.sha256(token.encode()).hexdigest(),
                                               STREAM_TOKEN_HASH_VERSION) == "ok"
        server = Server(port=0, store=store, sessions=sessions, comms=comms, local_host="fixture")
        server.assistant_composite = composite
        server.handlers.update(notify.wire_handlers())

        async def cli(*args):
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                   "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "agent-orch"),
                   "AGENT_ORCH_WS_URL": f"ws://127.0.0.1:{port}", "AGENT_ORCH_TOKEN": "",
                   "AGENT_ORCH_HOST_ID": "fixture", "AGENT_ORCH_STREAM_ID": producer,
                   "AGENT_ORCH_STREAM_TOKEN": token, "AGENT_ORCH_RUNTIME_DIR": str(tmp_path / "cli"),
                   "AGENT_ORCH_MEMORY_REPO": str(tmp_path / "memory")}
            proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "work-lane", *args,
                                                       env=env, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
            except BaseException:
                proc.kill()
                await proc.wait()
                raise
            assert stdout, stderr.decode()
            return proc.returncode, json.loads(stdout)

        async def adopt(key):
            result = await composite.operation({"type": "assistant.operation", "request_id": "adopt:" + key,
                "composite_stream_id": ASSISTANT, "dispatch_id": "none", "operation": "work_lane.adopt",
                "payload": {"adoption_key": key, "title": "Confirmation lane", "summary": "Synthetic work",
                            "owner_kind": "operator", "work_state": "paused", "visible_chat": {"stream_id": ASSISTANT},
                            "no_spec_reason": "Disposable confirmation fixture."},
                "_auth_context": {"token_verified": True, "stream_id": producer,
                                  "session_generation": row["session_generation"]}}, actor_stream_id=producer)
            return result["lane"]

        try:
            port = await server.bind()
            lane = await adopt("stream:confirmation")
            # The old wire has no version: retained pre-fix questions must still work.
            rc, asked = await cli("request-confirmation", lane["lane_id"], "--action", "set_state:done",
                                  "--title", "Complete lane?", "--body", "Mark this work done.",
                                  "--question-id", "q-confirm")
            assert rc == 0, asked
            qid = "q-confirm"
            question = await notify._db.call("get_agent_question", qid)
            assert question["envelope"]["context"] == {"schema": "WorkLaneConfirmationV1",
                                                       "lane_id": lane["lane_id"], "action": "set_state:done"}
            if refusal != "unanswered":
                auth = {"operator_authenticated": True}
                if transport == "notification":
                    reply = await notify.notification({"type": "notification.resolve",
                        "notification_id": question["notification_id"], "action_kind": "yes_no",
                        "selections": ["Not yet" if refusal == "decline" else "Confirm"],
                        "custom_text": comment or None,
                        "_auth_context": auth})
                    assert reply["type"] == "notification.resolve.ok", reply
                else:
                    selection = {} if refusal == "comment_only" else {
                        "selections": ["Not yet" if refusal == "decline" else "Confirm"]}
                    reply = await notify.prompt({"type": "prompt.answer", "question_id": qid,
                        **selection, "text": "Confirm" if refusal == "comment_only" else comment,
                        "_auth_context": auth})
                    assert reply["type"] == "prompt.answer.ok", reply
                if delivered:
                    assert await queue.drain_once(force=True) == 1
                    assert len(provider.pastes) == 1
                question = await notify._db.call("get_agent_question", qid)
                assert question["state"] == ("consumed" if delivered else "answered")
                assert question["answer"].get("custom_text") == ("Confirm" if refusal == "comment_only" else comment or None)
            target = await adopt("stream:other") if refusal == "wrong_lane" else lane
            args = ["set-owner", target["lane_id"], "--to", "fd"] if refusal == "wrong_action" else [
                "set-state", target["lane_id"], "--to", "done", "--outcome", "Completed"]
            version = target["version"] + (1 if refusal == "wrong_version" else 0)
            args += ["--expected-version", str(version), "--request-id", "use-confirmation",
                     "--confirmation-question-id", qid, "--composite-stream-id", ASSISTANT]
            rc, result = await cli(*args)
            if refusal:
                assert rc != 0, result
                assert result["error"] == ("assistant_lane_version_conflict" if refusal == "wrong_version"
                                            else "work_lane_operator_confirmation_mismatch"), result
                assert (await store.get_work_lane(target["lane_id"]))["lane"]["work_state"] == "paused"
            else:
                assert rc == 0, result
                assert result["lane"]["work_state"] == "done"
                events = (await store.get_work_lane(lane["lane_id"]))["events"]
                assert sum(e.get("consumed_question_id") == qid for e in events) == 1
                # A genuine second mutation, with fresh CAS, must refuse the same id.
                rc, reused = await cli("set-owner", lane["lane_id"], "--to", "fd", "--expected-version",
                                      str(result["lane"]["version"]), "--request-id", "reuse-confirmation",
                                      "--confirmation-question-id", qid, "--composite-stream-id", ASSISTANT)
                # Wrong action is checked before single-use. Reopen, then repeat the same action.
                assert rc != 0 and reused["error"] == "work_lane_operator_confirmation_mismatch", reused
                rc, reopened = await cli("set-state", lane["lane_id"], "--to", "paused", "--expected-version",
                                         str(result["lane"]["version"]), "--request-id", "reopen",
                                         "--composite-stream-id", ASSISTANT)
                assert rc == 0, reopened
                args[args.index("--expected-version") + 1] = str(reopened["lane"]["version"])
                args[args.index("--request-id") + 1] = "reuse-same-action"
                rc, reused = await cli(*args)
                assert rc != 0 and reused["error"] == "work_lane_operator_confirmation_consumed", reused
        finally:
            await server.close()
            await composite.stop()
            assert server._ws_server is None
