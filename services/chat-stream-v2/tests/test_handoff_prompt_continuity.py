"""Question identity, ownership and deterministic handoff interleavings."""
import asyncio
import json

import pytest

from notification_answer_fixture import fixture
from notify import Notify
from _shared.notifications_store import InvalidNotification
from test_lifecycle_continuity import environment, HOST, SOURCE
from test_question_contract_d3 import _ask


def test_transfer_preserves_identity_deadline_exclusions_and_restart(tmp_path):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            source = await store.fetch_session(HOST, "source")
            successor = await opened("successor")
            notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
            await notify.start()
            try:
                before = {}
                for kind in ("ordinary", "legacy", "wrong_generation", "terminal", "proxy", "approval"):
                    msg = _ask("q-" + kind, producer=SOURCE)
                    envelope = msg["envelope"]
                    envelope.update(ttl_seconds=3600, spec_id="spec_fixture_continuity",
                                    producer_session_generation=source["session_generation"])
                    if kind == "legacy":
                        envelope.pop("producer_session_generation")
                    elif kind == "wrong_generation":
                        envelope["producer_session_generation"] = "prior-generation"
                    elif kind == "proxy":
                        envelope["_assistant_composite_question_proxy"] = True
                    elif kind == "approval":
                        envelope["context"] = json.dumps({"schema": "HandoffModelChangeApprovalV1"})
                    q = await notify._db.call("create_agent_question", envelope=envelope, actions=msg["actions"])
                    if kind == "terminal":
                        await notify._db.call("resolve_notification", q["notification_id"],
                                              action_kind="resolved", by="fixture")
                    before[kind] = (await notify._db.call("get_agent_question", q["question_id"]),
                                    await notify._db.call("get_notification", q["notification_id"]))
                assert await notify.transfer_questions_for_handoff(source, successor) == 2
                assert await notify.transfer_questions_for_handoff(source, successor) == 0
                for kind in ("wrong_generation", "terminal", "proxy", "approval"):
                    q, card = before[kind]
                    assert await notify._db.call("get_agent_question", q["question_id"]) == q
                    assert await notify._db.call("get_notification", q["notification_id"]) == card
                await notify.stop()
                notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
                await notify.start()
                for kind, (old_q, old_card) in before.items():
                    q = await notify._db.call("get_agent_question", old_q["question_id"])
                    card = await notify._db.call("get_notification", old_q["notification_id"])
                    if kind in {"ordinary", "legacy"}:
                        assert q["producer_stream_id"] == successor["stream_id"]
                        assert q["producer_session_generation"] == successor["session_generation"]
                        assert q["envelope"]["_handoff_origin"]["producer_stream_id"] == SOURCE
                        for key, value in old_q.items():
                            if key not in {"producer_stream_id", "producer_session_generation", "envelope"}:
                                assert q[key] == value, key
                        for key, value in old_q["envelope"].items():
                            if key not in {"producer_stream_id", "producer_session_generation"}:
                                assert q["envelope"][key] == value, key
                        assert card == {**old_card, "answer_to_stream_id": successor["stream_id"]}
                    elif kind in {"wrong_generation", "approval"}:
                        # Existing startup expiry retains generation/approval policy.
                        assert q["state"] == "expired"
                        assert q["producer_stream_id"] == SOURCE
                        assert q["producer_session_generation"] == old_q["producer_session_generation"]
                    else:
                        assert q == old_q and card == old_card
            finally:
                await notify.stop()
    asyncio.run(run())


def test_pair_failure_rolls_back_all_prompt_moves(tmp_path):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            source = await store.fetch_session(HOST, "source")
            successor = await opened("successor")
            notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
            await notify.start()
            try:
                for qid in ("q-good", "q-mismatch"):
                    assert (await notify.prompt(_ask(qid, producer=SOURCE)))["type"] == "prompt.ask.ok"
                # Corrupt only the scratch pair, after the first row in the transaction.
                def mismatch():
                    db = notify._db._store
                    with db._lock, db._conn:
                        db._conn.execute("UPDATE notifications SET answer_to_stream_id='fixture:wrong' "
                                         "WHERE notification_id=(SELECT notification_id FROM agent_questions WHERE question_id='q-mismatch')")
                await asyncio.to_thread(mismatch)
                with pytest.raises(InvalidNotification):
                    await notify.transfer_questions_for_handoff(source, successor)
                for qid in ("q-good", "q-mismatch"):
                    assert (await notify._db.call("get_agent_question", qid))["producer_stream_id"] == SOURCE
            finally:
                await notify.stop()
    asyncio.run(run())


@pytest.mark.parametrize("ask_first", [True, False])
def test_ask_and_handoff_share_source_lifecycle_fence(tmp_path, monkeypatch, ask_first):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            await opened("successor")
            notify = Notify(str(tmp_path / "notifications.db"), sessions=sessions)
            await notify.start()
            ctl.consent_notify = notify
            entered, release = asyncio.Event(), asyncio.Event()
            try:
                target = "_prompt_ask" if ask_first else "transfer_questions_for_handoff"
                original = getattr(notify, target)
                async def pause(*args):
                    entered.set()
                    await release.wait()
                    return await original(*args)
                monkeypatch.setattr(notify, target, pause)
                async def handoff():
                    return await ctl._finish_handoff({"handoff_from_stream_id": SOURCE}, HOST + ":successor")
                async def ask():
                    return await notify.prompt(_ask("q-race", producer=SOURCE))
                first = asyncio.create_task(ask() if ask_first else handoff())
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(handoff() if ask_first else ask())
                await asyncio.sleep(0)
                assert not second.done()
                release.set()
                one, two = await asyncio.wait_for(asyncio.gather(first, second), 2)
                ask_result = one if ask_first else two
                assert (two if ask_first else one)["state"] == "complete"
                q = await notify._db.call("get_agent_question", "q-race")
                if ask_first:
                    assert ask_result["type"] == "prompt.ask.ok"
                    assert q["state"] == "open" and q["producer_stream_id"] == HOST + ":successor"
                else:
                    assert ask_result["error_code"] == "producer_not_open" and q is None
            finally:
                release.set()
                await notify.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["answer", "cancel"])
@pytest.mark.parametrize("transfer_first", [True, False])
def test_mutation_racing_transfer_uses_current_owner(tmp_path, monkeypatch, mutation, transfer_first):
    async def run():
        async with fixture(tmp_path) as (notify, queue, comms, provider, sessions, store):
            source = await sessions.open("hosta", "source", provider="codex", visibility="visible")
            successor = sessions.get("hosta:v2-test")
            assert (await notify.prompt(_ask("q-mutation", producer=source["stream_id"])))["type"] == "prompt.ask.ok"
            request = {"type": "prompt." + mutation, "question_id": "q-mutation", "selections": ["yes"],
                       "_auth_context": {"token_verified": True, "stream_id": source["stream_id"]},
                       "from_stream_id": source["stream_id"]}
            entered, release = asyncio.Event(), asyncio.Event()
            original = notify._db.call
            async def pause(method, *args, **kw):
                if method == "resolve_notification":
                    entered.set()
                    await release.wait()
                return await original(method, *args, **kw)
            if transfer_first:
                monkeypatch.setattr(notify._db, "call", pause)
                task = asyncio.create_task(notify.prompt(request))
                await asyncio.wait_for(entered.wait(), 2)
                assert await notify.transfer_questions_for_handoff(source, successor) == 1
                release.set()
                reply = await asyncio.wait_for(task, 2)
                assert reply["type"] == "prompt.error"
                q = await original("get_agent_question", "q-mutation")
                assert q["state"] == "open" and q["producer_stream_id"] == successor["stream_id"]
                monkeypatch.setattr(notify._db, "call", original)
                forbidden = await notify.prompt({**request, "type": "prompt.cancel"})
                assert forbidden["error_code"] == "question_unauthorized"
                listed = await notify.prompt({"type": "prompt.list", "open": True,
                                             "producer_stream_id": successor["stream_id"]})
                assert listed["questions"][0]["question_id"] == "q-mutation"
                reply = await notify.prompt({**request, "from_stream_id": successor["stream_id"],
                    "_auth_context": {"token_verified": True, "stream_id": successor["stream_id"]}})
            else:
                reply = await notify.prompt(request)
                assert await notify.transfer_questions_for_handoff(source, successor) == 0
            assert reply["type"] == "prompt." + mutation + ".ok"
            q = await original("get_agent_question", "q-mutation")
            if mutation == "answer":
                card = await original("get_notification", q["notification_id"])
                owner = card["resolution"]["v2_answer_delivery"]
                assert owner["producer_stream_id"] == (successor["stream_id"] if transfer_first else source["stream_id"])
                if transfer_first:
                    assert await queue.drain_once(force=True) == 1
                    assert len(provider.pastes) == 1
                    assert (await original("get_notification", q["notification_id"]))["resolution"]["delivery_status"] == "delivered"
            else:
                assert q["state"] == "dismissed" and provider.pastes == []
    asyncio.run(run())
