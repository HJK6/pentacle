"""Late proof is receipt reconciliation, never another native submission."""
import asyncio
import json

import pytest

import comms as comms_module
from comms import Comms
from sessions import Sessions, VerbError
from spawnctl import SpawnCtl
from store import Store
from test_tell_immediate_delivery import StoreTailProofCodex


@pytest.fixture(autouse=True)
def short_initial_wait(monkeypatch):
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_TIMEOUT_S", 0.01)
    monkeypatch.setattr(comms_module, "SUBMISSION_EVIDENCE_POLL_S", 0.001)


async def setup(path=":memory:"):
    store = Store(str(path)); store.start()
    target = "localhost:v2-late-proof"
    tmux = StoreTailProofCodex(store, target, append_on_paste=False)
    sessions = Sessions(store, tmux=tmux, local_host="localhost")
    await sessions.open("localhost", "v2-late-proof", provider="codex")
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
    return store, sessions, comms, tmux, {"stream_id": target, "tell_id": "late-proof-one", "message": "same input"}


async def append(store, target, text, *, identity="late-input", **overrides):
    return await store.append_session_event(target, {
        "stream_id": target, "provider": "codex", "kind": "USER", "text": text,
        "timestamp": "2026-10-30T10:00:00Z", **overrides,
    }, identity=identity, limit=500)


@pytest.mark.parametrize("case", ["pending", "wrong-text", "wrong-stream", "wrong-digest",
    "assistant-event", "pre-watermark", "missing-anchor", "unreachable-anchor", "reopened"])
def test_unproven_or_other_generation_input_never_promotes(case):
    async def run():
        store, sessions, comms, tmux, request = await setup()
        try:
            if case == "pre-watermark":
                await append(store, request["stream_id"], request["message"])
            first = await comms.tell(request)
            assert first["delivery_status"] == "committed_pending_proof"
            if case in {"missing-anchor", "unreachable-anchor"}:
                def change(conn):
                    row = conn.execute("SELECT reply FROM v2_tell_deliveries WHERE tell_id=?", (request["tell_id"],)).fetchone()
                    payload = json.loads(row[0])
                    if case == "missing-anchor": payload["delivery"].pop("proof_generation")
                    else: payload["delivery"]["proof_watermark_state"] = "unreachable"
                    conn.execute("UPDATE v2_tell_deliveries SET reply=? WHERE tell_id=?",
                                 (json.dumps(payload), request["tell_id"]))
                    conn.commit()
                await store.submit(change)
            if case == "reopened":
                await store.mark_closed("localhost", "v2-late-proof", closed_at="2026-10-30T10:00:01Z",
                                        pane_status="pane_dead")
                await sessions.open("localhost", "v2-late-proof", provider="codex")
            if case not in {"pending", "pre-watermark"}:
                overrides = {"stream_id": "localhost:other"} if case == "wrong-stream" else {}
                if case == "wrong-digest": overrides["provider_text_digest"] = "0" * 64
                if case == "assistant-event": overrides["kind"] = "ASSISTANT"
                await append(store, request["stream_id"],
                             "other input" if case == "wrong-text" else tmux.pastes[0], **overrides)
            replay = await comms.tell(request)
            assert replay["duplicate"] and not replay["submission_confirmed"]
            assert replay["delivery_status"] == "committed_pending_proof"
            assert replay["do_not_resubmit"]
            assert tmux.pastes == [request["message"]] and tmux.enter_only == 0
        finally: store.stop()
    asyncio.run(run())


def test_late_proof_survives_store_and_comms_restart_and_live_tail_churn(tmp_path):
    async def run():
        path = tmp_path / "events.db"
        store, sessions, comms, tmux, request = await setup(path)
        try:
            await comms.tell(request)
            await append(store, request["stream_id"], tmux.pastes[0])
            for i in range(505):
                await append(store, request["stream_id"], "other input " + str(i), identity="churn-" + str(i))
            assert not any(e.get("text") == tmux.pastes[0] for e in
                           await store.fetch_session_event_tail(request["stream_id"], limit=500))
        finally: store.stop()
        store = Store(str(path)); store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            await sessions.refresh()
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            replay = await comms.tell(request)
            assert replay["duplicate"] and replay["submission_confirmed"]
            assert replay["delivery_status"] == "delivered"
            assert replay["confirmation_pending"] is False and replay["confirmation_status"] == "confirmed"
            assert len(tmux.pastes) == 1 and tmux.enter_only == 0
            assert (await comms.tell(request))["proof_event_id"] == replay["proof_event_id"]
            with pytest.raises(VerbError) as conflict:
                await comms.tell(dict(request, message="different payload"))
            assert conflict.value.code == "tell_id_conflict"
        finally: store.stop()
    asyncio.run(run())


def test_overlapping_identical_bare_inputs_cannot_share_one_user_proof():
    async def run():
        store, sessions, comms, tmux, request = await setup()
        try:
            second = dict(request, tell_id="late-proof-two")
            await comms.tell(request); await comms.tell(second)
            await append(store, request["stream_id"], tmux.pastes[0])
            for msg in (request, second):
                result = await comms.tell(msg)
                assert result["delivery_status"] == "committed_pending_proof"
                assert not result["submission_confirmed"]
            assert len(tmux.pastes) == 2 and tmux.enter_only == 0
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["persist-before-paste", "proof-after-paste"])
def test_unknown_input_after_restart_never_pastes_again(tmp_path, failure):
    class InterruptedDaemon(BaseException):
        pass
    async def run():
        path = tmp_path / "interrupted.db"
        store, sessions, comms, tmux, request = await setup(path)
        try:
            if failure == "persist-before-paste":
                put = store.put_tell_delivery
                async def interrupted(*args, **kwargs):
                    await put(*args, **kwargs)
                    raise InterruptedDaemon()
                store.put_tell_delivery = interrupted
                error = InterruptedDaemon
            else:
                async def broken_reader(*args, **kwargs):
                    raise RuntimeError("proof reader unavailable")
                comms._submission_evidence = broken_reader
                error = RuntimeError
            with pytest.raises(error): await comms.tell(request)
            saved = await store.get_tell_delivery(request["tell_id"])
            assert saved["reply"]["delivery_status"] == "proof_unavailable"
            assert saved["delivery"]["proof_generation"]
            assert type(saved["delivery"]["proof_watermark"]) is int
            assert len(tmux.pastes) == (0 if failure == "persist-before-paste" else 1)
        finally: store.stop()
        store = Store(str(path)); store.start()
        try:
            sessions = Sessions(store, tmux=tmux, local_host="localhost")
            await sessions.refresh()
            comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=tmux))
            for _ in range(2):
                result = await comms.tell(request)
                assert result["duplicate"] and result["do_not_resubmit"]
                assert result["delivery_status"] == "proof_unavailable"
            assert len(tmux.pastes) == (0 if failure == "persist-before-paste" else 1)
            assert tmux.enter_only == 0
            if failure == "proof-after-paste":
                await append(store, request["stream_id"], tmux.pastes[0])
                result = await comms.tell(request)
                assert result["delivery_status"] == "delivered" and result["submission_attempts"] == 1
                assert len(tmux.pastes) == 1
        finally: store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["payload_digest", "proof_generation", "proof_session_created_at",
                                  "proof_watermark", "text", "to_stream_id"])
def test_prepared_completion_cannot_change_original_payload_or_anchor(field):
    async def run():
        store, sessions, comms, tmux, request = await setup()
        put = store.put_tell_delivery
        async def before_paste(*args, **kwargs):
            await put(*args, **kwargs)
            raise RuntimeError("stop after durable preparation")
        store.put_tell_delivery = before_paste
        try:
            with pytest.raises(RuntimeError): await comms.tell(request)
            original = await store.get_tell_delivery(request["tell_id"])
            completed = json.loads(json.dumps(original))
            if field == "payload_digest": completed[field] = "other-payload"
            else:
                value = completed["delivery"][field]
                completed["delivery"][field] = value + 1 if type(value) is int else str(value) + "-changed"
            completed["reply"].update(delivery_status="delivered", submission_confirmed=True, submission_attempts=1)
            completed["delivery"].update(delivery_status="delivered", submission_confirmed=True, submission_attempts=1)
            assert await store.promote_tell_delivery(request["tell_id"], original["payload_digest"],
                                                       prepared_result=completed) is None
            assert await store.get_tell_delivery(request["tell_id"]) == original
            assert tmux.pastes == []
        finally: store.stop()
    asyncio.run(run())
