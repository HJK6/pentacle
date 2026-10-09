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
                "type": "error.list",
                "request_id": "r",
                "_auth_context": {**s.auth, **flags},
            }
        )
        assert r["error_code"] == "operator_required"
    # Revocation is checked from registry on every call, not cached hello trust.
    s.server.operator_credential_registry.revoke(s.auth["credential_id"])
    r = await s.service.request(
        {"type": "error.list", "request_id": "r", "_auth_context": s.auth}
    )
    assert r["error_code"] == "credential_revoked"


@pytest.mark.asyncio
async def test_settings_cas_audit_and_mute_overlay(subject):
    s = subject
    request = {
        "type": "error.settings.set",
        "request_id": "r",
        "family": "voice_operation.v1",
        "expected_revision": 0,
        "delivery_mode": "digest",
        "mute_for_s": 900,
        "_auth_context": s.auth,
    }
    result = await s.service.request(request)
    assert result["setting"]["delivery_mode"] == "digest"
    assert result["setting"]["effective_delivery_mode"] == "muted"
    stale = await s.service.request(request)
    assert stale["error_code"] == "revision_conflict"
    settings = await s.notify._db.call("error_settings", now="2099-01-01T00:00:00Z")
    assert settings[0]["effective_delivery_mode"] == "digest"
    items, _ = await s.notify._db.call("error_settings_audit")
    assert len(items) == 1


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
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
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
async def test_legacy_cursor_and_question_exclusion(subject):
    s = subject
    for i in range(251):
        await s.notify._db.call(
            "create_notification",
            producer="legacy",
            title="Retained warning",
            severity="warning",
            notification_id=f"n-{i:03}",
            now="2026-01-01T00:00:00Z",
        )
    await s.notify._db.call(
        "create_notification", producer="legacy", title="Information", severity="info"
    )
    ids = []
    msg = {"type": "error.list", "request_id": "r", "_auth_context": s.auth}
    while True:
        result = await s.service.request(msg)
        assert result["type"] == "error.list.ok", result
        ids.extend(i["id"] for i in result["items"])
        if not result["next_cursor"]:
            break
        msg["cursor"] = result["next_cursor"]
    assert len(ids) == len(set(ids)) == 251


@pytest.mark.asyncio
async def test_cancel_before_attempt_suppresses_held_member(subject):
    s = subject
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
    await upload(s)
    await s.service.reconcile()
    await report(s, outcome="cancelled", code="operation_cancelled")
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    assert rows[0]["terminal_reason"] == "suppressed_cancelled"


@pytest.mark.asyncio
async def test_unmute_does_not_replay_recovered_but_releases_active(subject):
    s = subject
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="muted",
    )
    await upload(s)
    await s.service.reconcile()
    assert not await s.service.notice_rows()
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=1,
        delivery_mode="immediate",
    )
    await s.service.reconcile()
    assert len(await s.service.notice_rows()) == 1
    await report(s, outcome="recovered", code="recovered")
    await s.service.reconcile()
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=2,
        delivery_mode="muted",
    )
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=3,
        delivery_mode="immediate",
    )
    await s.service.reconcile()
    assert len(await s.service.notice_rows()) == 1


@pytest.mark.asyncio
async def test_rebind_held_and_folded_before_attempt(subject, monkeypatch):
    s = subject
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
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
    detail = await s.service.detail(
        "notification:" + fact["notification_id"], {"_auth_context": s.auth}
    )
    assert detail["alert"]["delivery"]["recipient_stream_id"] == "fixture:v2-next"


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
    projected = await s.service.list({"filter": "received", "_auth_context": s.auth})
    assert len(projected["items"]) == 1
    assert (
        projected["items"][0]["delivery"]["recipient_generation"]
        == s.intent["origin_generation"]
    )
    assert projected["items"][0]["delivery"]["proof_at"]
    await s.queue.drain_once()
    assert len(s.provider.pastes) == 1


@pytest.mark.asyncio
async def test_legacy_delivered_timestamp_without_user_generation_proof_is_unknown(
    subject,
):
    s = subject
    await s.store.enqueue_outbound_notice(
        notice_id="native-fixture",
        kind="reconciler",
        dedupe_key="native-fixture",
        recipient_stream_id="fixture:v2-test",
        tell_id="native-fixture",
        body="Safe fixed template",
        metadata={"class": "row_open_session_dead"},
    )

    def mark(conn):
        with conn:
            conn.execute(
                "UPDATE v2_outbound_notices SET delivered_at='2026-10-09T00:00:00Z' WHERE notice_id='native-fixture'"
            )

    await s.store.submit(mark)
    result = await s.service.list({"_auth_context": s.auth})
    assert result["items"][0]["delivery"]["state"] == "legacy_unknown"
    assert not (await s.service.list({"filter": "received", "_auth_context": s.auth}))[
        "items"
    ]


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
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
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
async def test_attempt_history_does_not_call_unproved_timestamp_proof(subject):
    s = subject
    await upload(s)
    await s.service.reconcile()

    def mark(conn):
        with conn:
            conn.execute(
                "UPDATE v2_outbound_notices SET delivered_at='2026-10-09T00:00:00Z'"
            )

    await s.store.submit(mark)
    fact = (await s.notify._db.call("error_rows"))[0]
    detail = await s.service.detail(
        "notification:" + fact["notification_id"], {"_auth_context": s.auth}
    )
    assert detail["attempts"][0]["proof_at"] is None


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
    assert (
        len(
            (await s.service.list({"filter": "received", "_auth_context": s.auth}))[
                "items"
            ]
        )
        == 1
    )


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
async def test_policy_change_before_attempt_requeues_under_saved_policy(subject):
    s = subject
    await upload(s)
    await s.service.reconcile()
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
    await s.queue.drain_once()
    assert not s.provider.pastes
    await s.service.reconcile()
    rows = await s.service.notice_rows()
    assert any(r["terminal_reason"] == "suppressed_policy" for r in rows)
    assert any(r["kind"] == "front_desk_held" and not r["terminal_at"] for r in rows)


@pytest.mark.asyncio
async def test_mixed_digest_rebuild_submits_only_still_active_member(
    subject, monkeypatch
):
    s = subject
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
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
    await s.notify._db.call(
        "error_settings_set",
        actor=s.principal,
        expected_revision=0,
        delivery_mode="digest",
    )
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
async def test_product_responses_match_frozen_panel_schema(subject):
    from error_alerts import SCHEMA
    from jsonschema import Draft202012Validator, FormatChecker

    s = subject
    await upload(s)
    await s.queue.drain_once()
    fact = (await s.notify._db.call("error_rows"))[0]
    identity = "notification:" + fact["notification_id"]
    requests = [
        ("error.list", {}),
        ("error.get", {"id": identity}),
        ("error.mark", {"id": identity, "action": "seen"}),
        ("error.settings.get", {}),
        (
            "error.settings.set",
            {
                "family": "voice_operation.v1",
                "expected_revision": 0,
                "delivery_mode": "digest",
            },
        ),
        ("error.settings.audit", {}),
    ]
    for verb, fields in requests:
        response = await s.service.request(
            {"type": verb, "request_id": verb, "_auth_context": s.auth, **fields}
        )
        Draft202012Validator(
            {**SCHEMA["responses"][verb], "$defs": SCHEMA["$defs"]},
            format_checker=FormatChecker(),
        ).validate(response)


@pytest.mark.asyncio
async def test_existing_notification_native_pair_is_one_read_only_projection(subject):
    s = subject
    await s.notify._db.call(
        "create_notification",
        producer="session_reconciler",
        notification_id="legacy-pair",
        dedup_key="episode-one",
        severity="warning",
        title="Retained warning",
    )
    await s.store.enqueue_outbound_notice(
        notice_id="native-pair",
        kind="reconciler",
        dedupe_key="native-pair",
        tell_id="native-pair",
        recipient_stream_id="fixture:v2-test",
        body="Fixed safe fixture",
        episode_id="episode-one",
        metadata={"class": "row_open_session_dead"},
    )
    result = await s.service.list({"_auth_context": s.auth})
    assert len(result["items"]) == 1
    assert result["items"][0]["source"] == {"type": "notification", "id": "legacy-pair"}
    assert result["items"][0]["delivery"]["notice_id"] == "native-pair"
    assert result["items"][0]["read_only"] is True
    assert (await s.notify._db.call("error_get", "legacy-pair"))[
        "error_context"
    ] is None


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


@pytest.mark.asyncio
async def test_retained_native_notice_survives_recorded_primary_rebind(subject):
    s = subject
    await s.store.update_session("fixture", "v2-test", pane_status="pane_alive")
    old = await s.server.assistant_composite.binding()
    for notice_id, target, generation in (
        ("old-primary-wake", old["stream_id"], old["generation"]),
        ("unrelated-wake", "fixture:other", "unrelated-generation"),
    ):
        await s.store.enqueue_outbound_notice(
            notice_id=notice_id,
            kind="wake_missed",
            dedupe_key=notice_id,
            tell_id=notice_id,
            recipient_stream_id=target,
            body="Retained fixture wake",
            metadata={"owner_generation": generation},
        )
    new = await s.server.sessions.open(
        "fixture",
        "v2-next",
        provider="codex",
        visibility="visible",
        effective_model="gpt-6-astra",
        effective_effort="high",
        pane_status="pane_alive",
    )
    await s.store.rebind_assistant(
        env_binding={"stream_id": old["stream_id"], "generation": old["generation"]},
        actor_stream_id=old["stream_id"],
        actor_generation=old["generation"],
        target_stream_id="fixture:v2-next",
        target_generation=new["session_generation"],
        request_id="retained-native-rebind",
        expected_revision=old["revision"],
    )
    await s.server.assistant_composite.load_binding()
    result = await s.service.list({"_auth_context": s.auth})
    assert [r["id"] for r in result["items"]] == ["outbox:old-primary-wake"]
    assert result["items"][0]["read_only"] is True
    assert await s.notify._db.call("error_rows") == []
