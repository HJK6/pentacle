"""Installed-client voice detection and the typed producer seam.

Real file stores, Notify, outbox, digest and Comms from the existing fixture;
only the provider counterpart is scripted.
"""

import asyncio
import time
import uuid

import pytest

import error_adapters
from alerts import Alerts
from error_adapters import ErrorFact
from error_alerts import ErrorAlerts
from test_error_alerts import subject  # noqa: F401  (shared fixture)


class AsrError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def transcribe_msg(s, request_id="transcribe-rec-1-0", mime="audio/mp4", auth=None):
    return {
        "type": "transcribe_blob",
        "request_id": request_id,
        "blob_sha": "b" * 64,
        "mime": mime,
        "_auth_context": s.auth if auth is None else auth,
    }


def send_msg(s, request_id="send-1", msg_id="optimistic-1"):
    return {
        "type": "send",
        "request_id": request_id,
        "stream_id": "fixture:v2-test",
        "msg_id": msg_id,
        "message": "x",
        "meta": {"voice": {"duration_s": 3}},
        "_auth_context": s.auth,
    }


async def failing(msg):
    raise AsrError("asr_unavailable")


async def ok(msg):
    return {"type": "transcribe_blob.ok", "request_id": msg["request_id"]}


async def facts(s):
    return await s.notify._db.call("error_rows")


@pytest.mark.asyncio
async def test_installed_audio_transcribe_failure_alerts_after_grace(subject):
    s = subject
    with pytest.raises(AsrError):
        await s.service.transcribe(transcribe_msg(s), failing)
    await s.service.reconcile(now=time.time() + 30)
    assert await facts(s) == []  # inside the client's retry grace
    await s.service.reconcile(now=time.time() + 61)
    rows = await facts(s)
    assert len(rows) == 1
    ctx = rows[0]["error_context"]
    assert (ctx["family"], ctx["code"], ctx["condition"]) == (
        "voice_operation.v1", "transcribe_failed", "active")
    notices = await s.service.notice_rows()
    assert len(notices) == 1 and "b" * 64 not in notices[0]["body"]


@pytest.mark.asyncio
async def test_installed_transcribe_retry_same_request_recovers_quietly(subject):
    s = subject
    with pytest.raises(AsrError):
        await s.service.transcribe(transcribe_msg(s), failing)
    assert (await s.service.transcribe(transcribe_msg(s), ok))["type"] == "transcribe_blob.ok"
    await s.service.reconcile(now=time.time() + 400)
    assert await facts(s) == []
    assert await s.service.notice_rows() == []


@pytest.mark.asyncio
async def test_installed_transcribe_hang_is_milestone_missing(subject):
    s = subject

    async def hang(msg):
        # Daemon dies mid-transcription: started is persisted, no terminal.
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await s.service.transcribe(transcribe_msg(s), hang)
    await s.service.reconcile(now=time.time() + 151)
    (row,) = await facts(s)
    assert row["error_context"]["code"] == "transcribe_milestone_missing"


@pytest.mark.asyncio
async def test_expected_and_non_voice_transcribes_stay_quiet(subject):
    s = subject

    async def no_speech(msg):
        raise AsrError("no_speech")

    with pytest.raises(AsrError):
        await s.service.transcribe(transcribe_msg(s, "t-quiet"), no_speech)
    # Non-audio MIME and a non-operator caller are never registered as voice.
    with pytest.raises(AsrError):
        await s.service.transcribe(transcribe_msg(s, "t-image", mime="image/jpeg"), failing)
    with pytest.raises(AsrError):
        await s.service.transcribe(
            transcribe_msg(s, "t-agent", auth={"token_verified": True}), failing)
    await s.service.reconcile(now=time.time() + 400)
    assert await facts(s) == []
    assert len(await s.store.voice_list()) == 1  # only the operator audio request


@pytest.mark.asyncio
async def test_installed_voice_send_failure_alerts_and_retry_recovers(subject):
    s = subject

    async def refused(msg):
        raise AsrError("send_failed")

    with pytest.raises(AsrError):
        await s.service.send(send_msg(s), refused)
    await s.service.reconcile(now=time.time() + 61)
    (row,) = await facts(s)
    assert row["error_context"]["code"] == "send_transport_failed"

    # A second message whose send is proved never alerts.
    async def proved(msg):
        return {"action_committed": True, "submission_confirmed": True}

    await s.service.send(send_msg(s, "send-2", "optimistic-2"), proved)
    await s.service.reconcile(now=time.time() + 61)
    assert len(await facts(s)) == 1


@pytest.mark.asyncio
async def test_installed_voice_send_does_not_change_send_message(subject):
    s = subject
    seen = []

    async def handler(msg):
        seen.append(msg)
        return {"action_committed": True, "submission_confirmed": True}

    msg = send_msg(s)
    await s.service.send(msg, handler)
    assert seen == [msg]  # no private generation fence added for untagged sends


@pytest.fixture
def probe_family(monkeypatch):
    monkeypatch.setitem(error_adapters.FAMILY_CODES, "probe.v1", frozenset({"probe_failed"}))
    monkeypatch.setitem(
        error_adapters.ADAPTERS, "probe_kind",
        lambda f: ErrorFact("probe.v1", "probe_failed", str(f["episode"]), stage="probe"))


@pytest.mark.asyncio
async def test_typed_fact_is_durable_at_return_and_delivered_once(subject, probe_family):
    s = subject
    alerts = Alerts()
    assert await alerts.record("probe_kind", episode="e1") is None  # no sink: not recorded
    assert await facts(s) == []
    alerts.sink = s.service
    nid = await alerts.record("probe_kind", episode="e1")
    (row,) = await facts(s)
    assert row["notification_id"] == nid and row["producer"] == "probe.v1"
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
    body = (await s.service.notice_rows())[0]["body"]
    assert body.startswith("BLOCKER probe.v1 probe_failed") and "episode=e1" in body
    # Same episode updates the one record; no second notice.
    assert await alerts.record("probe_kind", episode="e1") == nid
    await s.queue.drain_once()
    assert len(await facts(s)) == 1 and len(s.provider.pastes) == 1


@pytest.mark.asyncio
async def test_typed_fact_survives_coordinator_restart_without_duplicate(subject, probe_family):
    s = subject
    s.server.alerts = None
    nid = await s.service.emit(ErrorFact("probe.v1", "probe_failed", "e2"))
    await s.service.reconcile()
    (before,) = await s.service.notice_rows()
    # A fresh coordinator over the same file stores replays, never re-sends.
    s.queue._guards.pop("error_alert", None)
    restarted = ErrorAlerts(s.server, s.queue)
    await restarted.start()
    await restarted.reconcile()
    await s.queue.drain_once()
    await restarted.reconcile()
    await s.queue.drain_once()
    rows = await restarted.notice_rows()
    assert [r["notice_id"] for r in rows] == [before["notice_id"]]
    assert len(s.provider.pastes) == 1
    assert (await facts(s))[0]["notification_id"] == nid


@pytest.mark.asyncio
async def test_invalid_facts_and_principals_refused(subject, probe_family):
    s = subject
    with pytest.raises(ValueError):
        ErrorFact("probe.v1", "unknown_code", "e3")
    with pytest.raises(ValueError):
        ErrorFact("probe.v1", "probe_failed", "bad episode/with space")
    with pytest.raises(ValueError):
        await s.service.emit(ErrorFact("probe.v1", "probe_failed", "e3"), principal="operator:x")
    with pytest.raises(KeyError):
        await Alerts().record("probe_kind")  # a mapping error is a caller bug and raises


@pytest.mark.asyncio
async def test_configure_attaches_sink_before_producers(subject):
    s = subject
    alerts = Alerts()
    s.server.blobs = type("B", (), {})()
    s.server.handlers = {"transcribe_blob": ok, "send": ok}
    s.queue._guards.pop("error_alert", None)
    await s.server.configure_error_alerts(s.queue, alerts)
    assert alerts.sink is s.server.error_alerts
    assert set(h for h in s.server.handlers if h.startswith("error.")) == {"error.report"}


@pytest.mark.asyncio
async def test_installed_retry_after_grace_recovers_and_discarded_transcript_quiet(subject):
    s = subject
    with pytest.raises(AsrError):
        await s.service.transcribe(transcribe_msg(s), failing)
    await s.service.reconcile(now=time.time() + 61)
    (row,) = await facts(s)
    assert row["error_context"]["condition"] == "active"
    await s.service.transcribe(transcribe_msg(s), ok)
    await s.service.reconcile(now=time.time() + 400)
    (row,) = await facts(s)
    assert row["error_context"]["condition"] == "recovered"
    # A successful transcript that is never sent has no daemon correlation.
    await s.service.transcribe(transcribe_msg(s, "transcribe-rec-2-0"), ok)
    await s.service.reconcile(now=time.time() + 400)
    assert len(await facts(s)) == 1


@pytest.mark.asyncio
async def test_record_logs_only_kind_and_typed_fact(subject, probe_family, caplog):
    alerts = Alerts()
    alerts.sink = subject.service
    await alerts.record("probe_kind", episode="e9", body="private-body-text")
    assert "private-body-text" not in caplog.text
    assert caplog.text.count("ALERT probe_kind") == 1


def test_unhashable_fact_fields_raise_value_error():
    for fields in (([], "probe_failed", "e"), ("voice_operation.v1", {}, "e")):
        with pytest.raises(ValueError):
            ErrorFact(*fields)


@pytest.mark.asyncio
async def test_bookkeeping_failure_never_blocks_ordinary_send(subject, monkeypatch):
    s = subject
    seen = []

    async def handler(msg):
        seen.append(msg)
        return {"action_committed": True, "submission_confirmed": True}

    async def broken(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(s.store, "fetch_session", broken)
    assert (await s.service.send(send_msg(s), handler))["submission_confirmed"] is True
    assert len(seen) == 1 and await s.store.voice_list() == []


async def upload_op(s, ago=61):
    from store_voice_operations import iso as iso_at
    intent = {**s.intent, "operation_id": str(uuid.uuid4())}
    await s.store.voice_register(s.principal, intent, upload_request_id="u-" + intent["operation_id"])
    await s.store.voice_milestone(s.principal, intent["operation_id"], "upload_committed",
                                  blob_sha=uuid.uuid4().hex * 2, now=iso_at(time.time() - ago))
    return intent["operation_id"]


async def rebind_to(s, name):
    new = await s.server.sessions.open("fixture", name, provider="codex", visibility="visible")
    revision = (await s.server.assistant_composite.binding()).get("revision") or 0

    def bind(conn):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES(?,?,?,?,'2026-10-09T00:00:00Z')",
                (s.server.assistant_composite.config.name, "fixture:" + name, new["session_generation"], revision + 1),
            )

    await s.store.submit(bind)
    await s.server.assistant_composite.load_binding()
    return new


def live(rows):
    return [r for r in rows if not r["terminal_at"] and not r["delivered_at"]]


@pytest.mark.asyncio
async def test_unattempted_rebind_successor_keeps_immediate_admission(subject):
    s = subject
    await upload_op(s)
    await s.service.reconcile()
    (old,) = await s.service.notice_rows()
    assert old["kind"] == "error_alert"
    await rebind_to(s, "v2-next")
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    (successor,) = [r for r in rows if r["recipient_stream_id"] == "fixture:v2-next"]
    assert successor["kind"] == "error_alert"
    assert next(r for r in rows if r["notice_id"] == old["notice_id"])["terminal_reason"] == "superseded_binding"


@pytest.mark.asyncio
async def test_attempted_same_fingerprint_still_consumes_budget(subject):
    s = subject
    await upload_op(s)
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
    await upload_op(s)
    await s.service.reconcile()
    kinds = sorted(r["kind"] for r in await s.service.notice_rows())
    assert kinds == ["error_alert", "front_desk_held"]  # second same-fingerprint alert folds


@pytest.mark.asyncio
async def test_unrelated_fingerprints_share_the_total_budget(subject, monkeypatch):
    s = subject
    monkeypatch.setitem(error_adapters.FAMILY_CODES, "probe.v1", frozenset({"c1", "c2", "c3", "c4"}))
    for code in ("c1", "c2", "c3", "c4"):
        await s.service.emit(ErrorFact("probe.v1", code, "e-" + code))
        await s.service.reconcile()
    kinds = sorted(r["kind"] for r in await s.service.notice_rows())
    assert kinds == ["error_alert"] * 3 + ["front_desk_held"]


@pytest.mark.asyncio
async def test_repeated_rebind_before_attempt_never_duplicates(subject):
    s = subject
    await upload_op(s)
    await s.service.reconcile()
    await rebind_to(s, "v2-next")
    await s.service.reconcile()
    await rebind_to(s, "v2-third")
    await s.service.reconcile()
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    pending = live(rows)
    assert len(pending) == 1 and pending[0]["recipient_stream_id"] == "fixture:v2-third"
    assert pending[0]["kind"] == "error_alert"
    assert sum(r["terminal_reason"] == "superseded_binding" for r in rows) == 2
    await s.queue.drain_once()
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
