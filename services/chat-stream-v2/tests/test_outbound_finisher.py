"""Server-owned completion of committed outbound units (send mandates).

Real websocket/Store/BlobStore/ErrorAlerts/send path (ErrorAlertsHarness); only
the ASR backend poster and the fixture pane provider are synthetic. A client that
disconnects after its final ACK models "the phone is gone": this is server
contract evidence, not installed-phone lock proof.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid

import pytest

from error_alerts import evaluate
from outbound_finisher import MILESTONES, WAIT_LIMIT
from sessions import VerbError
from test_voice_upload_resume import voice_harness, metadata, init, send_chunk  # noqa: F401

AUDIO = b"synthetic voice bytes for the finisher"
TRANSCRIPT = "cosmo-e2e stub transcript"


async def rpc(client, verb, **fields):
    fields.setdefault("request_id", "rpc-" + uuid.uuid4().hex)
    return await client.rpc(verb, **fields)


def principal(h):
    return "operator:" + h.credential_id


def voice_request_id(oid, target, generation, sequence=1):
    packed = json.dumps([oid, target, generation, sequence], ensure_ascii=False, separators=(",", ":"))
    return "voice-send-" + hashlib.sha256(packed.encode()).hexdigest()[:40]


def voice_mandate(intent, sequence=1, **overrides):
    oid, target, gen = intent["operation_id"], intent["origin_stream_id"], intent["origin_generation"]
    optimistic = "opt-" + oid[:8]
    mandate = {
        "version": 1, "request_id": voice_request_id(oid, target, gen, sequence),
        "optimistic_id": optimistic, "sequence": sequence,
        "original_request": {"protocol": "voice-v1", "logical_id": oid, "target": target,
                             "generation": gen, "sequence": sequence},
        "text_prefix": "", "meta": {"voice": {"duration_s": 2}},
        "client_build": intent["client_build"],
    }
    if target.endswith(":assistant"):
        mandate["msg_id"] = optimistic
    mandate.update(overrides)
    return mandate


def voice_meta(h, data=AUDIO, target="fixture:v2-test", generation=None):
    m = metadata(h, data)
    m["voice_operation"].update(origin_stream_id=target,
                                origin_generation=generation or h.origin["session_generation"])
    return m


async def composite_generation(h):
    await h.composite.ensure_projection()
    return (await h.store.fetch_session("fixture", "assistant"))["session_generation"]


async def upload_voice(h, m, mandate=None, *, chunks=True):
    """Init (with mandate) and upload; the client disconnects after the final ACK."""
    async with h.client() as c:
        fields = {**m, **({"send_mandate": mandate} if mandate is not None else {})}
        ack = await init(c, fields)
        if chunks and "error_code" not in ack:
            done = await send_chunk(c, m, m["_data"] if "_data" in m else AUDIO, final=True)
            assert done["complete"] is True
        return ack


async def upload_blob(h, data):
    rid = "p1-" + uuid.uuid4().hex
    async with h.client() as c:
        assert (await rpc(c, "upload_blob_init", request_id=rid))["type"] == "upload_blob.init.ok"
        done = await rpc(c, "upload_blob_chunk", request_id=rid, final=True,
                           data_b64=base64.b64encode(data).decode())
        assert done["type"] == "upload_blob.ok", done
        return done["blob_sha"]


async def row(h, oid):
    return await h.store.voice_get(principal(h), oid)


async def settle(h, oid, states=("committed", "proved", "failed", "cancelled"), timeout=10):
    finisher = h.server.error_alerts.finisher
    async with asyncio.timeout(timeout):
        while True:
            current = await row(h, oid)
            task = finisher._workers.get((principal(h), oid))
            if current and current["data"].get("state") in states and (task is None or task.done()):
                return current
            await asyncio.sleep(0.02)


async def quiesce(h):
    for task in list(h.server.error_alerts.finisher._workers.values()):
        await asyncio.gather(task, return_exceptions=True)


async def user_rows(h, stream_id):
    events = await h.store.fetch_session_event_tail(stream_id, limit=50)
    return [e for e in events if e["kind"] == "USER"]


class Gate:
    """Transcriber or send double held by the test, then released."""

    def __init__(self, inner=None):
        self.inner, self.entered, self.release, self.calls = inner, asyncio.Event(), asyncio.Event(), 0

    async def transcribe_blob(self, **kwargs):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return {"text": TRANSCRIPT}

    async def __call__(self, frame):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return await self.inner(frame)


# -- U1 mandate validation ----------------------------------------------------

@pytest.mark.asyncio
async def test_u2_voice_mandate_finishes_server_side_after_sender_disconnects(voice_harness):
    h = voice_harness
    m = voice_meta(h)
    mandate = voice_mandate(m["voice_operation"])
    ack = await upload_voice(h, m, mandate)
    assert ack["server_send"] is True
    oid = m["voice_operation"]["operation_id"]
    done = await settle(h, oid)
    assert done["data"]["state"] in ("committed", "proved")
    assert h.provider.pastes == [TRANSCRIPT]
    data = done["data"]
    for key in ("upload_committed", "transcribe_started", "transcribe_result", "send_attempted", "send_committed"):
        assert data.get(key), key
    assert data["server_send_request_ids"] == [mandate["request_id"]]
    receipt = await h.store.get_send_receipt("fixture:v2-test", mandate["request_id"])
    assert receipt and receipt["state"] != "not_landed"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["request_id", "logical_id", "oversize", "attachments", "text", "client_build"])
async def test_u1_invalid_mandate_is_refused_and_nothing_is_stored(voice_harness, bad):
    h = voice_harness
    m = voice_meta(h)
    intent = m["voice_operation"]
    mandate = voice_mandate(intent)
    if bad == "request_id":
        mandate["request_id"] = "voice-send-" + "0" * 40
    elif bad == "logical_id":
        mandate["original_request"]["logical_id"] = str(uuid.uuid4())
    elif bad == "oversize":
        mandate["text_prefix"] = "x" * (17 * 1024)
    elif bad == "attachments":
        mandate["attachments"] = [{"key": "a" * 64, "mime": "image/jpeg"}] * 6
    elif bad == "text":
        mandate["text"] = "client text on a voice unit"
    else:
        mandate["client_build"] = "other-build"
    ack = await upload_voice(h, m, mandate, chunks=False)
    assert ack["error_code"] == "invalid_request"
    stored = await row(h, intent["operation_id"])
    assert stored is None or "server_mandate" not in stored["data"]


@pytest.mark.asyncio
async def test_u1_identical_mandate_is_idempotent_and_differing_conflicts(voice_harness):
    h = voice_harness
    m = voice_meta(h)
    mandate = voice_mandate(m["voice_operation"])
    async with h.client() as c:
        assert (await init(c, {**m, "send_mandate": mandate}))["server_send"] is True
        assert (await init(c, {**m, "send_mandate": mandate}))["server_send"] is True
        changed = {**mandate, "text_prefix": "different"}
        assert (await init(c, {**m, "send_mandate": changed}))["error_code"] == "upload_conflict"
    stored = await row(h, m["voice_operation"]["operation_id"])
    assert stored["data"]["server_mandate"]["text_prefix"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["outbound.commit", "outbound.cancel", "outbound.get"])
@pytest.mark.parametrize("auth", ["scoped", "dot", "stream_token", "service"])
async def test_u1_non_operator_callers_are_refused(voice_harness, verb, auth):
    h = voice_harness
    context = {"operator_authenticated": True, "transport": "v2", "credential_id": h.credential_id}
    context.update({"scoped": {"scoped_principal": True}, "dot": {"dot_principal": True},
                    "stream_token": {"token_verified": True},
                    "service": {"service_authenticated": True}}[auth])
    m = voice_meta(h)
    body = ({"voice_operation": m["voice_operation"], "send_mandate": voice_mandate(m["voice_operation"])}
            if verb == "outbound.commit" else {"operation_id": m["voice_operation"]["operation_id"]})
    reply = await h.server.error_alerts.request({"type": verb, "request_id": "r", **body,
                                                 "_auth_context": context})
    assert reply == {"type": verb + ".error", "request_id": "r", "error_code": "operator_required"}
    assert await row(h, m["voice_operation"]["operation_id"]) is None


# -- U3 idempotency / crash -----------------------------------------------------

@pytest.mark.asyncio
async def test_u3_restart_after_send_never_sends_again(voice_harness):
    h = voice_harness
    m = voice_meta(h)
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    oid = m["voice_operation"]["operation_id"]
    await settle(h, oid)
    await h.restart()  # the fixture provider (pastes) restarts; USER rows persist
    await h.server.error_alerts.finisher.reconcile_pass()
    await quiesce(h)
    assert [e["text"] for e in await user_rows(h, "fixture:v2-test")] == [TRANSCRIPT]


@pytest.mark.asyncio
async def test_u3_restart_while_running_before_submission_sends_once(voice_harness):
    h = voice_harness
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]), chunks=False)
    waiting = await settle(h, oid, states=("running",))
    assert waiting["data"]["pending_blobs"] == 1
    await h.restart()
    assert (await row(h, oid))["data"]["state"] in ("pending", "running")
    async with h.client() as c:
        assert (await init(c, m))["next_offset"] == 0
        assert (await send_chunk(c, m, AUDIO, final=True))["complete"]
    await settle(h, oid)
    assert h.provider.pastes == [TRANSCRIPT]


@pytest.mark.asyncio
async def test_u3_ambiguous_submission_is_reconciled_never_resubmitted(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher
    real = finisher._send

    async def lost_ack(frame):
        await real(frame)
        raise ConnectionError("ack lost after the frame left")
    finisher._send = lost_ack
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    stuck = await settle(h, oid, states=("submitting",))
    assert stuck["data"]["send_attempted"]
    await h.restart()  # a restart never resubmits `submitting`
    await h.server.error_alerts.finisher.reconcile_pass()
    done = await settle(h, oid)
    assert done["data"]["state"] in ("committed", "proved")
    assert [e["text"] for e in await user_rows(h, "fixture:v2-test")] == [TRANSCRIPT]


@pytest.mark.asyncio
async def test_u3_concurrent_kicks_send_once(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher
    gate = Gate()
    finisher._transcriber = gate
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    await gate.entered.wait()
    for _ in range(3):
        finisher.kick(principal(h), oid)
    await finisher.kick_all()
    await finisher.reconcile_pass()
    gate.release.set()
    await settle(h, oid)
    assert gate.calls == 1 and h.provider.pastes == [TRANSCRIPT]


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("target", ["plain", "composite"])
async def test_u3_client_send_with_same_identity_coalesces(voice_harness, when, target):
    h = voice_harness
    stream = "fixture:v2-test" if target == "plain" else "fixture:assistant"
    m = voice_meta(h, target=stream,
                   generation=await composite_generation(h) if target == "composite" else None)
    intent = m["voice_operation"]
    mandate = voice_mandate(intent)
    oid = intent["operation_id"]
    frame = {"operation_id": oid, "request_id": mandate["request_id"], "meta": mandate["meta"]}
    if target == "plain":
        frame.update(host="fixture", session_name="v2-test", text=TRANSCRIPT, optimistic_id=mandate["optimistic_id"])
    else:
        frame.update(stream_id=stream, msg_id=mandate["msg_id"], message=TRANSCRIPT)
    async with h.client() as c:
        await init(c, {**m, "send_mandate": mandate})
        if when == "before":
            assert (await rpc(c, "send", **frame))["type"] == "send.result"
        assert (await send_chunk(c, m, AUDIO, final=True))["complete"]
        await settle(h, oid)
        if when == "after":
            assert (await rpc(c, "send", **frame))["type"] == "send.result"
    done = await row(h, oid)
    assert done["data"]["state"] in ("committed", "proved")
    if target == "plain":
        assert len(h.provider.pastes) == 1
    else:
        assert len(await user_rows(h, stream)) == 1


@pytest.mark.asyncio
async def test_u3b_terminal_mandate_never_submits_after_restart(voice_harness):
    h = voice_harness
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]), chunks=False)
    await settle(h, oid, states=("running",))

    def not_landed(r):
        r["data"].update(state="failed", server_finish_failed_code="not_landed")
    await h.store.voice_mutate(principal(h), oid, not_landed)
    await h.restart()
    async with h.client() as c:
        await init(c, m)
        assert (await send_chunk(c, m, AUDIO, final=True))["complete"]
    await h.server.error_alerts.finisher.reconcile_pass(now=10 ** 10)
    await quiesce(h)
    assert await user_rows(h, "fixture:v2-test") == []
    assert (await row(h, oid))["data"]["state"] == "failed"


@pytest.mark.asyncio
async def test_u3c_client_reported_send_id_is_never_a_receipt(voice_harness):
    h = voice_harness
    async with h.client() as c:
        landed = await rpc(c, "send", host="fixture", session_name="v2-test", text="unrelated",
                             optimistic_id="unrelated-opt")
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]), chunks=False)
    await h.store.voice_mutate(principal(h), oid, lambda r: r["data"].update(
        client_send_request_id=landed["request_id"]))
    async with h.client() as c:
        await init(c, m)
        assert (await send_chunk(c, m, AUDIO, final=True))["complete"]
    await settle(h, oid)
    assert h.provider.pastes == ["unrelated", TRANSCRIPT]


# -- U4 cancel ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_u4_cancel_during_asr_fences_the_send(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher
    gate = Gate()
    finisher._transcriber = gate
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    await gate.entered.wait()
    async with h.client() as c:
        reply = await rpc(c, "outbound.cancel", operation_id=oid)
        assert reply["state"] == "cancelled"
        gate.release.set()
        await settle(h, oid)
        got = await rpc(c, "outbound.get", operation_id=oid)
    assert got["state"] == "cancelled" and got["milestones"]["cancelled_at"]
    assert h.provider.pastes == []


@pytest.mark.asyncio
async def test_u4_cancel_while_send_is_held_reports_in_flight(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher
    gate = Gate(finisher._send)
    finisher._send = gate
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    await gate.entered.wait()
    async with h.client() as c:
        reply = await rpc(c, "outbound.cancel", operation_id=oid)
        assert reply["state"] == "in_flight"
        assert (await row(h, oid))["data"]["state"] == "submitting"
        gate.release.set()
        done = await settle(h, oid)
        assert done["data"]["state"] in ("committed", "proved")
        assert (await rpc(c, "outbound.cancel", operation_id=oid))["state"] == "committed"
    assert len(h.provider.pastes) == 1


@pytest.mark.asyncio
async def test_u4_concurrent_cancel_and_submission_never_both(voice_harness):
    h = voice_harness
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]), chunks=False)
    await settle(h, oid, states=("running",))
    async with h.client() as c:
        await init(c, m)
        upload = asyncio.create_task(send_chunk(c, m, AUDIO, final=True))
        cancel = await h.server.error_alerts.finisher.cancel(principal(h), oid)
        await upload
    await settle(h, oid)
    assert (cancel, len(h.provider.pastes)) in (("cancelled", 0), ("in_flight", 1), ("committed", 1))


@pytest.mark.asyncio
async def test_u4_cancel_failed_and_no_mandate(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]), chunks=False)
    await settle(h, oid, states=("running",))
    await h.store.voice_mutate(principal(h), oid, lambda r: r["data"].update(state="failed"))
    assert await finisher.cancel(principal(h), oid) == "cancelled"
    assert await finisher.cancel(principal(h), oid) == "cancelled"
    plain = voice_meta(h)
    await upload_voice(h, plain)
    assert await finisher.cancel(principal(h), plain["voice_operation"]["operation_id"]) == "no_mandate"


# -- U5 failures --------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now, self.sleeps = 1_800_000_000.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Down:
    async def transcribe_blob(self, **kwargs):
        raise VerbError("backend_unavailable", "mic backend unreachable")


@pytest.mark.asyncio
async def test_u5_asr_unavailable_backs_off_then_fails(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher
    clock = Clock()
    finisher._clock, finisher._sleep, finisher._transcriber = clock, clock.sleep, Down()
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    done = await settle(h, oid)
    assert done["data"]["server_finish_failed_code"] == "transcribe_unavailable"
    assert clock.sleeps[:4] == [5, 15, 30, 60] and sum(clock.sleeps) >= WAIT_LIMIT - 60
    assert h.provider.pastes == []
    assert evaluate(done, clock.now)[:2] == ("active", "server_finish_failed")


@pytest.mark.asyncio
async def test_u5_empty_transcript_is_no_speech(voice_harness):
    h = voice_harness
    class Silent:
        async def transcribe_blob(self, **kwargs):
            return {"text": "  "}
    h.server.error_alerts.finisher._transcriber = Silent()
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    done = await settle(h, oid)
    assert done["data"]["server_finish_failed_code"] == "no_speech" and h.provider.pastes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["revoked", "closed", "regenerated"])
async def test_u5_revoked_credential_or_changed_origin_never_sends(voice_harness, change):
    h = voice_harness
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]), chunks=False)
    await settle(h, oid, states=("running",))
    async with h.client() as c:  # bytes arrive before the change; the change wins
        await init(c, m)
        if change == "revoked":
            h.server.operator_credential_registry.revoke(h.credential_id)
        elif change == "closed":
            await h.store.submit(lambda conn: (conn.execute(
                "UPDATE sessions SET status='closed' WHERE host='fixture' AND session_name='v2-test'"), conn.commit()))
        else:
            await h.store.submit(lambda conn: (conn.execute(
                "UPDATE v2_session_generations SET generation=? WHERE host='fixture' AND session_name='v2-test'",
                (str(uuid.uuid4()),)), conn.commit()))
        if change != "revoked":
            assert (await send_chunk(c, m, AUDIO, final=True))["complete"]
    if change == "revoked":
        # The upload itself is refused now; the commit path is exercised by the finisher directly.
        await h.store.voice_milestone(principal(h), oid, "upload_committed", blob_sha=m["sha256"])
        h.blobs._path_for(m["sha256"]).parent.mkdir(parents=True, exist_ok=True)
        h.blobs._path_for(m["sha256"]).write_bytes(AUDIO)
        await h.server.error_alerts.finisher.kick_all()
    done = await settle(h, oid)
    assert done["data"]["server_finish_failed_code"] == (
        "credential_revoked" if change == "revoked" else "origin_unavailable")
    assert h.provider.pastes == []


@pytest.mark.asyncio
async def test_u5_definite_refusal_is_terminal_and_not_rekicked(voice_harness):
    h = voice_harness
    finisher = h.server.error_alerts.finisher

    async def refuse(frame):
        raise VerbError("attachment_missing", "attachment was not uploaded")
    finisher._send = refuse
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    done = await settle(h, oid)
    assert done["data"]["server_finish_failed_code"] == "attachment_missing"
    await h.restart()
    await h.server.error_alerts.finisher.reconcile_pass()
    await quiesce(h)
    assert await user_rows(h, "fixture:v2-test") == []
    assert (await row(h, oid))["data"]["state"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, "not_landed"])
async def test_u5_unresolved_submission_fails_after_window(voice_harness, monkeypatch, receipt):
    h = voice_harness
    finisher = h.server.error_alerts.finisher

    async def dropped(frame):
        # The wrapped send binds the attempt before submission; then transport drops.
        await h.store.voice_bind_send(principal(h), frame["operation_id"], frame["request_id"])
        raise ConnectionError("transport dropped before any result")
    finisher._send = dropped
    if receipt:
        async def not_landed(*args, **kwargs):
            return {"state": "not_landed"}
        monkeypatch.setattr(h.store, "reconcile_send_receipt", not_landed)
    m = voice_meta(h)
    oid = m["voice_operation"]["operation_id"]
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    stuck = await settle(h, oid, states=("submitting",))
    assert evaluate(stuck, 10 ** 10)[1] == "send_unconfirmed"
    from error_alerts import epoch
    started = epoch(stuck["data"]["attempt"]["started_at"])
    await finisher.reconcile_pass(now=started + 1)
    assert (await row(h, oid))["data"]["state"] == ("failed" if receipt else "submitting")
    await finisher.reconcile_pass(now=started + WAIT_LIMIT + 1)
    done = await row(h, oid)
    assert done["data"]["server_finish_failed_code"] == ("not_landed" if receipt else "send_unconfirmed")
    assert h.provider.pastes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["plain", "composite"])
async def test_u5b_origin_regenerated_during_asr_never_retargets(voice_harness, target):
    h = voice_harness
    stream = "fixture:v2-test" if target == "plain" else "fixture:assistant"
    m = voice_meta(h, target=stream,
                   generation=await composite_generation(h) if target == "composite" else None)
    gate = Gate()
    h.server.error_alerts.finisher._transcriber = gate
    oid = m["voice_operation"]["operation_id"]
    before = len(await user_rows(h, stream))
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    await gate.entered.wait()
    name = stream.split(":", 1)[1]
    await h.store.submit(lambda conn: (conn.execute(
        "UPDATE v2_session_generations SET generation=? WHERE host='fixture' AND session_name=?",
        (str(uuid.uuid4()), name)), conn.commit()))
    gate.release.set()
    done = await settle(h, oid)
    assert done["data"]["server_finish_failed_code"] == "origin_unavailable"
    assert h.provider.pastes == [] and len(await user_rows(h, stream)) == before


@pytest.mark.asyncio
async def test_u5c_composite_regenerated_inside_admission_is_refused(voice_harness, monkeypatch):
    h = voice_harness
    stream = "fixture:assistant"
    m = voice_meta(h, target=stream, generation=await composite_generation(h))
    oid = m["voice_operation"]["operation_id"]
    real = h.store.commit_attachment_input

    async def regenerate_first(attachments, op):
        # After the worker precheck and accept_input start; before the admission txn.
        def wrapped(conn):
            conn.execute("UPDATE v2_session_generations SET generation=? WHERE host='fixture' "
                         "AND session_name='assistant'", (str(uuid.uuid4()),))
            conn.commit()
            return op(conn)
        return await real(attachments, wrapped)
    monkeypatch.setattr(h.store, "commit_attachment_input", regenerate_first)
    before = len(await user_rows(h, stream))
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    done = await settle(h, oid)
    assert done["data"]["server_finish_failed_code"] == "origin_unavailable"
    assert len(await user_rows(h, stream)) == before


def test_u5_evaluate_projects_failed_and_stalled():
    base = {"principal": "p", "operation_id": "o", "operation_kind": "message", "revision": 1}
    at = "2026-10-31T00:00:00Z"
    from error_alerts import epoch
    t = epoch(at)
    def ev(data, now):
        return evaluate({**base, "data": {"server_mandate": {"version": 1}, "server_mandate_at": at, **data}}, now)
    assert ev({"state": "failed", "server_finish_failed_code": "origin_unavailable"}, t)[:2] == (
        "active", "server_finish_failed")
    assert ev({"state": "running"}, t + 599)[1] is None
    assert ev({"state": "running"}, t + 601)[1] == "server_finish_stalled"
    assert ev({"state": "running", "ready_at": at}, t + 61)[1] == "server_finish_stalled"
    assert ev({"state": "cancelled"}, t)[0] == "cancelled"
    # Rows without a mandate keep the offline-gap suppression unchanged.
    legacy = {**base, "operation_kind": "voice_chat",
              "data": {"upload_protocol": 2, "upload_committed": at}}
    assert evaluate(legacy, t + 10_000) == ("unknown", None, "upload")


# -- U6 compat ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_u6_init_without_mandate_is_unchanged_and_get_is_content_free(voice_harness):
    h = voice_harness
    plain = voice_meta(h)
    ack = await upload_voice(h, plain)
    assert "server_send" not in ack
    await asyncio.sleep(0.1)
    assert h.provider.pastes == []
    assert "server_mandate" not in (await row(h, plain["voice_operation"]["operation_id"]))["data"]
    m = voice_meta(h, data=AUDIO + b"!")
    m["_data"] = AUDIO + b"!"
    mandate = voice_mandate(m["voice_operation"], text_prefix="secret prefix")
    async with h.client() as c:
        await init(c, {**{k: v for k, v in m.items() if k != "_data"}, "send_mandate": mandate})
        await send_chunk(c, m, m["_data"], final=True)
        await settle(h, m["voice_operation"]["operation_id"])
        got = await rpc(c, "outbound.get", operation_id=m["voice_operation"]["operation_id"])
    assert set(got) == {"type", "request_id", "operation_id", "state", "milestones"}
    assert set(got["milestones"]) == set(MILESTONES)
    assert "secret" not in json.dumps(got) and TRANSCRIPT not in json.dumps(got)
    from error_alerts_security_scenario import peer
    async with peer(h, h.envelope) as (_, _, frames):
        snapshot = next(f for f in frames if f["type"] == "snapshot")
    assert snapshot["capabilities"].get("outbound_commit_v1") is True
    async with peer(h) as (_, _, frames):  # unauthenticated: not advertised
        assert not any(f.get("capabilities", {}).get("outbound_commit_v1") for f in frames)


# -- U10/U11 committed photo messages and web voice ------------------------------

def message_mandate(oid, text, keys, *, composite=True):
    optimistic = "opt-" + oid[:8]
    return {"version": 1, "request_id": "photo-" + oid, "optimistic_id": optimistic,
            **({"msg_id": optimistic} if composite else {}), "sequence": 1, "text": text,
            "attachments": [{"key": k, "mime": "image/jpeg"} for k in keys], "meta": {},
            "client_build": "photo-fixture"}


async def commit(h, c, intent, mandate):
    return await rpc(c, "outbound.commit", voice_operation=intent, send_mandate=mandate)


async def message_intent(h, stream="fixture:assistant"):
    return {"version": 1, "operation_id": str(uuid.uuid4()), "origin_stream_id": stream,
            "origin_generation": await composite_generation(h), "operation_kind": "message",
            "client_build": "photo-fixture"}


@pytest.mark.asyncio
async def test_u10_message_waits_for_every_digest_then_sends_once(voice_harness):
    h = voice_harness
    images = [b"\xff\xd8 first synthetic jpeg", b"\xff\xd8 second synthetic jpeg"]
    keys = [hashlib.sha256(i).hexdigest() for i in images]
    intent = await message_intent(h)
    oid = intent["operation_id"]
    async with h.client() as c:
        reply = await commit(h, c, intent, message_mandate(oid, "two photos", keys))
    assert reply["type"] == "outbound.commit.ok" and reply["server_send"] is True
    before = len(await user_rows(h, "fixture:assistant"))
    await upload_blob(h, images[0])
    await upload_blob(h, b"unrelated blob")
    waiting = await settle(h, oid, states=("running",))
    assert waiting["data"]["pending_blobs"] == 1
    assert len(await user_rows(h, "fixture:assistant")) == before
    await upload_blob(h, images[1])
    done = await settle(h, oid)
    assert done["data"]["state"] in ("committed", "proved")
    rows = await user_rows(h, "fixture:assistant")
    assert len(rows) == before + 1
    assert rows[-1]["text"] == "two photos"
    assert [a["key"] for a in rows[-1]["attachments"]] == keys


@pytest.mark.asyncio
async def test_u10_missing_image_stays_running_and_stalls_without_sending(voice_harness):
    h = voice_harness
    intent = await message_intent(h)
    oid = intent["operation_id"]
    async with h.client() as c:
        await commit(h, c, intent, message_mandate(oid, "never arrives", ["c" * 64]))
    waiting = await settle(h, oid, states=("running",))
    from error_alerts import epoch
    assert evaluate(waiting, epoch(waiting["data"]["server_mandate_at"]) + 601)[1] == "server_finish_stalled"
    assert waiting["data"]["pending_blobs"] == 1


@pytest.mark.asyncio
async def test_u3d_finished_mandate_rows_leave_the_reconcile_scan(voice_harness):
    """QA F4: committed-never-proved rows stay for alerts but are not rescanned."""
    h = voice_harness
    m = voice_meta(h)
    await upload_voice(h, m, voice_mandate(m["voice_operation"]))
    oid = m["voice_operation"]["operation_id"]
    done = await settle(h, oid)
    assert done["data"]["state"] in ("committed", "proved")
    assert oid not in [r["operation_id"] for r in await h.server.store.outbound_rows()]


@pytest.mark.asyncio
async def test_u10_web_voice_commit_then_protocol1_audio_sends_once(voice_harness):
    h = voice_harness
    origin = h.origin["session_generation"]
    intent = {"version": 1, "operation_id": str(uuid.uuid4()), "origin_stream_id": "fixture:v2-test",
              "origin_generation": origin, "operation_kind": "voice_chat", "client_build": "web-fixture"}
    audio = b"web voice recording bytes"
    mandate = voice_mandate(intent, audio={"sha256": hashlib.sha256(audio).hexdigest(),
                                           "mime": "audio/mp4", "bytes": len(audio)})
    async with h.client() as c:
        assert (await commit(h, c, intent, mandate))["server_send"] is True
    await upload_blob(h, audio)
    done = await settle(h, intent["operation_id"])
    assert done["data"]["state"] in ("committed", "proved")
    assert h.provider.pastes == [TRANSCRIPT]


@pytest.mark.asyncio
async def test_u11_cancel_before_last_blob_and_client_send_coalesce(voice_harness):
    h = voice_harness
    image = b"\xff\xd8 cancelled synthetic jpeg"
    intent = await message_intent(h)
    oid = intent["operation_id"]
    before = len(await user_rows(h, "fixture:assistant"))
    async with h.client() as c:
        await commit(h, c, intent, message_mandate(oid, "cancel me", [hashlib.sha256(image).hexdigest()]))
        assert (await rpc(c, "outbound.cancel", operation_id=oid))["state"] == "cancelled"
    await upload_blob(h, image)
    await quiesce(h)
    assert len(await user_rows(h, "fixture:assistant")) == before
    # Client send with the unit's identity after a server send: one USER row.
    image2 = b"\xff\xd8 coalesced synthetic jpeg"
    key = hashlib.sha256(image2).hexdigest()
    intent2 = await message_intent(h)
    mandate = message_mandate(intent2["operation_id"], "once", [key])
    async with h.client() as c:
        await commit(h, c, intent2, mandate)
        await upload_blob(h, image2)
        await settle(h, intent2["operation_id"])
        again = await rpc(c, "send", operation_id=intent2["operation_id"], request_id=mandate["request_id"],
                            stream_id="fixture:assistant", msg_id=mandate["msg_id"], message="once",
                            attachments=mandate["attachments"])
    assert again["type"] == "send.result"
    assert len(await user_rows(h, "fixture:assistant")) == before + 1


@pytest.mark.asyncio
async def test_u11_voice_with_images_sends_attachments_once(voice_harness):
    h = voice_harness
    image = b"\xff\xd8 voice image jpeg"
    key = await upload_blob(h, image)
    stream = "fixture:assistant"
    m = voice_meta(h, target=stream, generation=await composite_generation(h))
    mandate = voice_mandate(m["voice_operation"], text_prefix="look",
                            attachments=[{"key": key, "mime": "image/jpeg"}])
    before = len(await user_rows(h, stream))
    await upload_voice(h, m, mandate)
    done = await settle(h, m["voice_operation"]["operation_id"])
    assert done["data"]["state"] in ("committed", "proved")
    rows = await user_rows(h, stream)
    assert len(rows) == before + 1
    assert rows[-1]["text"] == "look\n" + TRANSCRIPT
    assert [a["key"] for a in rows[-1]["attachments"]] == [key]
