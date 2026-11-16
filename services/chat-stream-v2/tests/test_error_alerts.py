"""Focused file-store and policy tests. No bound daemon, tmux, or model."""

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import uuid
import pytest
import pytest_asyncio
from _shared.notifications_store import NotificationStore
from _shared import operator_auth
from assistant_composite import AssistantComposite, AssistantCompositeConfig
import error_alerts
from error_alerts import ErrorAlerts, evaluate
from server import Server
from store_voice_operations import iso
from notification_answer_fixture import fixture


@pytest_asyncio.fixture
async def subject(tmp_path, monkeypatch):
    monkeypatch.setenv("PENTACLE_ERROR_ALERTS_MODE", "on")
    async with fixture(tmp_path, host="fixture") as (
        notify,
        queue,
        comms,
        provider,
        sessions,
        store,
    ):
        server = Server(
            store=store, sessions=sessions, comms=comms, local_host="fixture"
        )
        server.notify = notify
        origin = await store.fetch_session("fixture", "v2-test")
        cfg = AssistantCompositeConfig.from_env(
            {
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": "fixture:assistant",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": "fixture:v2-test",
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": origin[
                    "session_generation"
                ],
            }
        )
        server.assistant_composite = AssistantComposite(store, config=cfg)
        comms.assistant_ingress_policy = (
            server.assistant_composite.suppress_routine_backend_ingress
        )
        queue.front_desk_digest = server.assistant_composite.front_desk_digest
        server.operator_credential_registry = operator_auth.OperatorCredentialRegistry(
            tmp_path / "creds.json"
        )
        cid, _ = server.operator_credential_registry.issue(
            "pentacle-mobile", label="unit"
        )
        service = ErrorAlerts(server, queue)
        await service.start()
        auth = {"operator_authenticated": True, "transport": "v2", "credential_id": cid}
        intent = {
            "version": 1,
            "operation_id": str(uuid.uuid4()),
            "origin_stream_id": "fixture:v2-test",
            "origin_generation": origin["session_generation"],
            "operation_kind": "voice_chat",
            "client_build": "unit-v1",
        }
        yield SimpleNamespace(
            service=service,
            store=store,
            notify=notify,
            queue=queue,
            auth=auth,
            intent=intent,
            principal="operator:" + cid,
            server=server,
            provider=provider,
        )


async def received(s):
    """The FD read-only query: typed facts whose linked notice has bound proof."""
    notices = {r["notice_id"]: r for r in await s.service.notice_rows()}
    out = []
    for fact in await s.notify._db.call("error_rows"):
        ctx = fact["error_context"]
        for nid in ctx["notice_ids"]:
            d = s.service.delivery(notices.get(nid), notices, ctx)
            if d["state"] == "delivered":
                out.append(d)
    return out


async def upload(s, ago=61):
    import time

    await s.store.voice_register(s.principal, s.intent, upload_request_id="unit-upload")
    await s.store.voice_milestone(
        s.principal,
        s.intent["operation_id"],
        "upload_committed",
        blob_sha="a" * 64,
        now=iso(time.time() - ago),
    )


async def report(
    s, sequence=1, outcome="failed", code="upload_transport_failed", **extra
):
    return await s.service.request(
        {
            "type": "error.report",
            "request_id": "r1",
            "version": 1,
            "event_id": str(uuid.uuid4()),
            "operation_id": s.intent["operation_id"],
            "sequence": sequence,
            "stage": "upload",
            "outcome": outcome,
            "code": code,
            "retry_state": "exhausted",
            "client_build": "unit-v1",
            "_auth_context": s.auth,
            **extra,
        }
    )


@pytest.mark.asyncio
async def test_interruption_creates_one_fact_and_notice(subject):
    s = subject
    await upload(s)
    await s.service.reconcile()
    rows = await s.notify._db.call("error_rows")
    assert len(rows) == 1
    ctx = rows[0]["error_context"]
    assert ctx["condition"] == "unknown"
    assert ctx["enqueued_notice_revision"] == ctx["desired_notice_revision"] == 1
    notices = await s.service.notice_rows()
    assert len(notices) == 1 and notices[0]["kind"] == "error_alert"
    assert "a" * 64 not in notices[0]["body"]
    await s.service.reconcile()
    assert len(await s.service.notice_rows()) == 1


@pytest.mark.asyncio
async def test_report_idempotency_conflict_sequence_and_recovery(subject):
    s = subject
    await upload(s)
    event = str(uuid.uuid4())
    r = await report(s, event_id=event)
    assert r["type"] == "error.report.ok"
    again = await report(s, event_id=event)
    assert again["duplicate"]
    conflict = await report(s, event_id=event, outcome="cancelled")
    assert conflict["error_code"] == "event_conflict"
    stale = await report(s)
    assert stale["error_code"] == "stale_sequence"
    await report(s, sequence=2, outcome="cancelled", code="operation_cancelled")
    await report(s, sequence=3)
    op = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert evaluate(op, 10**10)[0] == "cancelled"


@pytest.mark.asyncio
async def test_auth_denies_seat_elevation_scopes_and_revocation(subject):
    s = subject
    for flags in (
        {"operator_authority_source": "stream_token"},
        {"token_verified": True},
        {"scoped_principal": True},
        {"dot_principal": True},
        {"service_authenticated": True},
        {"credential_id": ""},
    ):
        r = await s.service.request(
            {
                "type": "error.report",
                "request_id": "r",
                "_auth_context": {**s.auth, **flags},
            }
        )
        assert r["error_code"] == "operator_required"
    # Revocation is checked from registry on every call, not cached hello trust.
    s.server.operator_credential_registry.revoke(s.auth["credential_id"])
    r = await s.service.request(
        {"type": "error.report", "request_id": "r", "_auth_context": s.auth}
    )
    assert r["error_code"] == "credential_revoked"


@pytest.mark.asyncio
async def test_cross_principal_origin_and_blob_ownership(subject):
    s = subject
    await upload(s)
    with pytest.raises(ValueError, match="operation_forbidden"):
        await s.store.voice_milestone(
            "operator:other",
            s.intent["operation_id"],
            "transcribe_started",
            blob_sha="a" * 64,
        )
    with pytest.raises(ValueError, match="blob_forbidden"):
        await s.store.voice_milestone(
            s.principal,
            s.intent["operation_id"],
            "transcribe_started",
            blob_sha="b" * 64,
        )
    with pytest.raises(ValueError, match="origin_mismatch"):
        await s.store.voice_register(
            s.principal, {**s.intent, "origin_generation": "wrong"}
        )


@pytest.mark.asyncio
async def test_digest_fold_is_not_receipt_and_retention_protects(subject, monkeypatch):
    s = subject
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    await upload(s)
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    assert rows[0]["kind"] == "front_desk_held"
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    rows = await s.service.notice_rows()
    member = next(r for r in rows if r["kind"] == "front_desk_held")
    digest = next(r for r in rows if r["kind"] == "error_alert")
    assert (
        json.loads(member["metadata"])["folded_into_notice_id"] == digest["notice_id"]
    )
    assert (
        s.service.delivery(member, {r["notice_id"]: r for r in rows})["state"]
        == "folded"
    )
    await report(s, outcome="recovered", code="recovered")
    await s.service.reconcile()
    fact = (await s.notify._db.call("error_rows"))[0]
    assert fact["state"] == "resolved"
    await s.notify._db.call("prune_resolved", now="2099-01-01T00:00:00Z")
    assert await s.notify._db.call("error_get", fact["notification_id"])


@pytest.mark.asyncio
async def test_cancel_before_attempt_suppresses_held_member(subject, monkeypatch):
    s = subject
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    await upload(s)
    await s.service.reconcile()
    await report(s, outcome="cancelled", code="operation_cancelled")
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    assert rows[0]["terminal_reason"] == "suppressed_cancelled"


@pytest.mark.asyncio
async def test_rebind_held_and_folded_before_attempt(subject, monkeypatch):
    s = subject
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    await upload(s)
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    old_rows = await s.service.notice_rows()
    old_digest = next(r for r in old_rows if r["kind"] == "error_alert")
    new = await s.server.sessions.open(
        "fixture", "v2-next", provider="codex", visibility="visible"
    )

    # Unit exercises authoritative binding readback, with no transport/runtime.
    def bind(conn):
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) VALUES(?,?,?,1,'2026-10-09T00:00:00Z')",
                (
                    s.server.assistant_composite.config.name,
                    "fixture:v2-next",
                    new["session_generation"],
                ),
            )

    await s.store.submit(bind)
    await s.server.assistant_composite.load_binding()
    await s.service.reconcile()
    await s.queue.front_desk_digest.tick()
    rows = await s.service.notice_rows()
    new_notices = [
        r
        for r in rows
        if r["kind"] == "error_alert" and r["recipient_stream_id"] == "fixture:v2-next"
    ]
    assert len(new_notices) == 1
    assert (
        next(r for r in rows if r["notice_id"] == old_digest["notice_id"])[
            "terminal_reason"
        ]
        == "superseded_binding"
    )
    fact = (await s.notify._db.call("error_rows"))[0]
    notices = {r["notice_id"]: r for r in rows}
    ctx = fact["error_context"]
    deliveries = [s.service.delivery(notices.get(n), notices, ctx) for n in ctx["notice_ids"]]
    assert deliveries[-1]["recipient_stream_id"] == "fixture:v2-next"


@pytest.mark.asyncio
async def test_guard_grace_and_bounded_ingress(subject):
    s = subject
    await upload(s)
    await s.service.reconcile()
    await report(s, retry_state="pending")
    row = (await s.service.notice_rows())[0]
    decision = await s.service.guard(row)
    assert decision.action == "defer"
    for sequence in range(2, 61):
        assert (await report(s, sequence=sequence))["type"] == "error.report.ok"
    assert (await report(s, sequence=61))["error_code"] == "rate_limited"


@pytest.mark.asyncio
async def test_real_comms_generation_proof_is_required_for_received(subject):
    s = subject
    await upload(s)
    await s.queue.drain_once()
    rows = await s.service.notice_rows()
    assert len(rows) == 1
    assert len(s.provider.pastes) == 1
    (projected,) = await received(s)
    assert projected["recipient_generation"] == s.intent["origin_generation"]
    assert projected["proof_at"]
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1


@pytest.mark.asyncio
async def test_report_gap_does_not_replace_failure_evidence(subject):
    s = subject
    await upload(s)
    await report(s)
    await report(
        s, sequence=2, outcome="unconfirmed", code="report_gap", dropped_count=4
    )
    op = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert evaluate(op, 10**10) == ("active", "upload_transport_failed", "upload")
    assert op["data"]["report_gap_count"] == 4


@pytest.mark.asyncio
async def test_explicit_send_retry_keeps_both_server_receipt_identities(subject):
    s = subject
    await upload(s)
    await s.store.voice_bind_send(s.principal, s.intent["operation_id"], "first-send")
    await s.store.voice_bind_send(
        s.principal, s.intent["operation_id"], "explicit-user-retry"
    )
    await s.store.voice_milestone(
        s.principal, s.intent["operation_id"], "send_committed", request_id="first-send"
    )
    await s.store.voice_milestone(
        s.principal,
        s.intent["operation_id"],
        "send_proved",
        request_id="explicit-user-retry",
    )
    op = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert op["data"]["server_send_request_ids"] == [
        "first-send",
        "explicit-user-retry",
    ]
    assert evaluate(op, 10**10)[0] == "recovered"


@pytest.mark.asyncio
async def test_cancelled_member_cannot_escape_inside_mixed_digest(subject, monkeypatch):
    s = subject
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    await upload(s)
    first = s.intent
    s.intent = {**first, "operation_id": str(uuid.uuid4())}
    await s.store.voice_register(
        s.principal, s.intent, upload_request_id="second-upload"
    )
    import time

    await s.store.voice_milestone(
        s.principal,
        s.intent["operation_id"],
        "upload_committed",
        blob_sha="b" * 64,
        now=iso(time.time() - 61),
    )
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    digest = next(
        r for r in await s.service.notice_rows() if r["kind"] == "error_alert"
    )
    s.intent = first
    await report(s, outcome="cancelled", code="operation_cancelled")
    decision = await s.service.guard(digest)
    assert decision is not None and decision.action == "terminal"


@pytest.mark.asyncio
async def test_retained_alert_keeps_proof_beyond_generic_tell_limit(
    subject, monkeypatch
):
    s = subject
    await upload(s)
    await s.queue.drain_once()
    notice = (await s.service.notice_rows())[0]
    monkeypatch.setattr("store.TELL_RETENTION", 1)
    for i in range(3):
        await s.store.put_tell_delivery(
            "unrelated-" + str(i), {"reply": {}, "delivery": {}}
        )
    assert await s.store.get_tell_delivery(notice["tell_id"]) is not None
    assert len(await received(s)) == 1


@pytest.mark.asyncio
async def test_cancel_does_not_undo_committed_pending_send(subject):
    s = subject
    await upload(s)
    await s.store.voice_bind_send(
        s.principal, s.intent["operation_id"], "send-committed"
    )
    await s.store.voice_milestone(
        s.principal,
        s.intent["operation_id"],
        "send_committed",
        request_id="send-committed",
    )
    await report(s, outcome="cancelled", code="operation_cancelled")
    op = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert evaluate(op, 10**10) == ("unknown", "send_unconfirmed", "send")


@pytest.mark.asyncio
async def test_recovery_after_enqueue_before_watermark_repairs_link(subject):
    s = subject
    await upload(s)
    await s.service.reconcile()
    fact = (await s.notify._db.call("error_rows"))[0]
    await s.notify._db.call(
        "error_patch",
        fact["notification_id"],
        {"notice_ids": [], "enqueued_notice_revision": 0},
    )
    await report(s, outcome="cancelled", code="operation_cancelled")
    await s.service.reconcile()
    fact = await s.notify._db.call("error_get", fact["notification_id"])
    rows = await s.service.notice_rows()
    assert fact["error_context"]["notice_ids"] == [rows[0]["notice_id"]]
    assert rows[0]["terminal_reason"] == "suppressed_cancelled"


@pytest.mark.asyncio
async def test_mixed_digest_rebuild_submits_only_still_active_member(
    subject, monkeypatch
):
    s = subject
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    await upload(s)
    first = s.intent
    s.intent = {**first, "operation_id": str(uuid.uuid4())}
    await s.store.voice_register(
        s.principal, s.intent, upload_request_id="second-upload"
    )
    import time

    await s.store.voice_milestone(
        s.principal,
        s.intent["operation_id"],
        "upload_committed",
        blob_sha="b" * 64,
        now=iso(time.time() - 61),
    )
    active_id = s.intent["operation_id"]
    await s.service.reconcile()
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    await s.queue.front_desk_digest.tick()
    s.intent = first
    await report(s, outcome="cancelled", code="operation_cancelled")
    await s.queue.drain_once()
    await s.queue.drain_once()
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
    assert active_id in s.provider.pastes[0]
    assert first["operation_id"] not in s.provider.pastes[0]


@pytest.mark.asyncio
async def test_recovery_summary_survives_batch_with_active_alert(subject, monkeypatch):
    s = subject
    await upload(s)
    first = s.intent
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1
    await report(s, outcome="recovered", code="recovered")
    monkeypatch.setitem(error_alerts.POLICY, "effective_delivery_mode", "digest")
    s.intent = {**first, "operation_id": str(uuid.uuid4())}
    await s.store.voice_register(
        s.principal, s.intent, upload_request_id="second-upload"
    )
    import time

    await s.store.voice_milestone(
        s.principal,
        s.intent["operation_id"],
        "upload_committed",
        blob_sha="b" * 64,
        now=iso(time.time() - 61),
    )
    monkeypatch.setenv("PENTACLE_FRONT_DESK_DIGEST_ENABLED", "0")
    for _ in range(3):
        await s.queue.drain_once()
    updates = "\n".join(s.provider.pastes[1:])
    assert "condition=recovered" in updates
    assert s.intent["operation_id"] in updates
    assert "BLOCKER voice_operation.v1 recovered" not in updates


@pytest.mark.asyncio
async def test_voice_send_cannot_paste_into_replacement_origin(subject, monkeypatch):
    s = subject
    await upload(s)
    comms = s.server.comms
    original = comms._materialize_send_plan
    reached = 0

    async def replace_origin_after_admission(plan):
        nonlocal reached
        reached += 1
        materialized = await original(plan)
        await s.store.submit(
            lambda conn: conn.execute(
                "UPDATE v2_session_generations SET generation='replacement' "
                "WHERE host='fixture' AND session_name='v2-test'"
            )
        )
        return materialized

    monkeypatch.setattr(comms, "_materialize_send_plan", replace_origin_after_admission)
    result = await s.service.send(
        {
            "type": "send",
            "request_id": "voice-stale-generation",
            "stream_id": s.intent["origin_stream_id"],
            "text": "disposable voice",
            "operation_id": s.intent["operation_id"],
            "_auth_context": s.auth,
        },
        comms.send,
    )
    assert reached == 1
    assert s.provider.pastes == [], "old voice operation reached replacement generation"
    assert result["delivery"] == "not_landed"
    op = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert "send_committed" not in op["data"]


@pytest.mark.asyncio
async def test_existing_report_cannot_register_different_carrier_operation(subject):
    s = subject
    await upload(s)
    unrelated = {**s.intent, "operation_id": str(uuid.uuid4())}
    result = await report(s, voice_operation=unrelated)
    assert result.get("error_code") == "operation_forbidden"
    assert await s.store.voice_get(s.principal, unrelated["operation_id"]) is None
    original = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert original["highest_sequence"] == 0


@pytest.mark.parametrize("method", ["_dot_hello_frames", "_scoped_hello_frames"])
@pytest.mark.asyncio
async def test_restricted_hello_stays_available_without_alert_capability(
    subject, method
):
    subject.server.error_alerts = subject.service
    frames = getattr(subject.server, method)("full")
    snapshot = next(frame for frame in frames if frame["type"] == "snapshot")
    assert not snapshot["capabilities"].get("error_alerts_v1")
    assert "error_reporting_credential_id" not in snapshot
    assert snapshot["notifications"] == []



@pytest.mark.parametrize("protocol", [None, 2])
@pytest.mark.parametrize("stage", ["upload", "transcribe"])
def test_v2_only_suppresses_offline_absent_next_stage(protocol, stage):
    data = {"upload_committed": iso(100)}
    if protocol is not None:
        data["upload_protocol"] = protocol
    if stage == "transcribe":
        data.update(transcribe_started=iso(101), transcribe_result=iso(102))
    operation = {"operation_kind": "voice_chat", "data": data}
    actual = evaluate(operation, 1000000)
    expected_code = None if protocol == 2 else (
        "upload_milestone_missing" if stage == "upload" else "send_unconfirmed")
    assert actual[1] == expected_code


@pytest.mark.parametrize("evidence,code", [
    ({"transcribe_started": iso(101)}, "transcribe_milestone_missing"),
    ({"transcribe_failed": iso(101), "transcribe_failed_code": "transcribe_failed"}, "transcribe_failed"),
    ({"send_committed": iso(101)}, "send_unconfirmed"),
    ({"transcribe_result": iso(101), "server_send_request_ids": ["attempt-1"]}, "send_unconfirmed"),
    ({"transcribe_result": iso(101), "server_send_request_id": "attempt-legacy"}, "send_unconfirmed"),
    ({"client_event": {"outcome": "failed", "code": "upload_transport_failed", "stage": "upload",
                       "retry_state": "exhausted", "at": iso(102)}}, "upload_transport_failed"),
    ({"client_event": {"outcome": "cancelled", "code": "operation_cancelled", "stage": "upload"}}, "operation_cancelled"),
    ({"client_event": {"outcome": "recovered", "code": "recovered", "stage": "send"}}, "recovered"),
])
def test_v2_offline_suppression_preserves_real_failure_and_explicit_precedence(evidence, code):
    op = {"operation_kind": "voice_chat", "data": {
        "upload_protocol": 2, "upload_committed": iso(100), **evidence}}
    assert evaluate(op, 1000000)[1] == code


@pytest.mark.asyncio
async def test_v2_operation_binds_stable_upload_identity_and_durable_server_flag(subject):
    s = subject
    upload_id = str(uuid.uuid4())
    first = {"protocol": 2, "request_id": "rpc-first", "upload_id": upload_id,
             "voice_operation": s.intent, "_auth_context": s.auth}
    await s.service.register_upload(first)
    await s.service.register_upload({**first, "request_id": "rpc-resume"})
    row = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert row["data"]["upload_request_id"] == upload_id
    assert row["data"]["upload_protocol"] == 2
    revision = row["revision"]
    await s.service.register_upload({**first, "request_id": "rpc-again"})
    assert (await s.store.voice_get(s.principal, s.intent["operation_id"]))["revision"] == revision
    with pytest.raises(ValueError, match="event_conflict"):
        await s.service.register_upload({**first, "upload_id": str(uuid.uuid4())})
    with pytest.raises(ValueError, match="event_conflict"):
        await s.service.register_upload({**first, "protocol": 1, "request_id": upload_id})
    with pytest.raises(ValueError, match="origin_mismatch"):
        await s.service.register_upload({**first, "voice_operation": {**s.intent, "client_build": "changed"}})


@pytest.mark.asyncio
async def test_v2_upload_identity_is_principal_scoped(subject):
    s = subject
    upload_id = str(uuid.uuid4())
    cid, _ = s.server.operator_credential_registry.issue("pentacle-mobile", label="second-synthetic")
    second_auth = {**s.auth, "credential_id": cid}
    for auth in (s.auth, second_auth):
        await s.service.register_upload({"protocol": 2, "request_id": "rpc-" + auth["credential_id"],
            "upload_id": upload_id, "voice_operation": s.intent, "_auth_context": auth})
    assert len(await s.store.voice_list()) == 2


@pytest.mark.asyncio
async def test_v1_registration_cannot_set_upload_protocol_from_wire(subject):
    s = subject
    await s.service.register_upload({"request_id": "legacy", "voice_operation": s.intent,
        "upload_protocol": 2, "_auth_context": s.auth})
    row = await s.store.voice_get(s.principal, s.intent["operation_id"])
    assert "upload_protocol" not in row["data"]
    assert row["data"]["upload_request_id"] == "legacy"
    response = await report(s, upload_protocol=2)
    assert response["error_code"] == "invalid_request"


@pytest.mark.asyncio
async def test_offline_capture_before_init_has_no_server_operation(subject):
    assert await subject.store.voice_list() == []
    await subject.service.reconcile(now=1000000)
    assert await subject.notify._db.call("error_rows") == []


@pytest.mark.asyncio
async def test_v2_transcribe_preserves_immutable_client_build(subject):
    s = subject
    await s.service.register_upload({"protocol": 2, "request_id": "first-rpc",
        "upload_id": str(uuid.uuid4()), "voice_operation": s.intent, "_auth_context": s.auth})
    await s.store.voice_milestone(s.principal, s.intent["operation_id"],
                                 "upload_committed", blob_sha="a" * 64)
    called = []
    async def asr(msg):
        called.append(msg)
        return {"text": "synthetic"}
    with pytest.raises(ValueError, match="origin_mismatch"):
        await s.service.transcribe({"voice_operation": {**s.intent, "client_build": "other"},
            "request_id": "transcribe-" + s.intent["operation_id"], "blob_sha": "a" * 64,
            "mime": "audio/mp4", "_auth_context": s.auth}, asr)
    assert called == []


def test_v2_success_after_retry_preserves_result_precedence_over_old_failure():
    op = {"operation_kind": "voice_chat", "data": {
        "upload_protocol": 2, "upload_committed": iso(100), "transcribe_started": iso(101),
        "transcribe_failed": iso(102), "transcribe_failed_code": "transcribe_failed",
        "transcribe_result": iso(103)}}
    assert evaluate(op, 1000000) == ("unknown", None, "send")
