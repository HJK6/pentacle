"""Typed voice facts and operator projection over the existing notice outbox.

One coordinator runs inside the existing five-second outbox pass. No content,
second delivery queue, independent retry loop, or client-controlled destination.
"""

from __future__ import annotations
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from jsonschema import Draft202012Validator, FormatChecker
from _shared.notification_errors import FAMILIES
from outbound_notices import NoticeDecision
from store_routing import _insert_outbound_notice_conn
from store_voice_operations import iso

log = logging.getLogger("chat_streamd_v2.error_alerts")
SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs/contracts/error-alerts-v1.schema.json"
    ).read_text()
)
VALIDATORS = {
    verb: Draft202012Validator(
        {**shape, "$defs": SCHEMA["$defs"]}, format_checker=FormatChecker()
    )
    for verb, shape in SCHEMA["requests"].items()
}
INTENT_VALIDATOR = Draft202012Validator(
    SCHEMA["$defs"]["voice_operation"], format_checker=FormatChecker()
)
LIMITS = dict(
    zip(
        SCHEMA["$defs"]["limits"]["properties"],
        [60, 150, 60, 60, 1, 3, 200, 30, 100, 16384, "Current front desk binding"],
    )
)
EXPECTED_CODES = {
    "no_speech",
    "too_long",
    "unsupported",
    "permission_declined",
    "operation_cancelled",
}
INCOMPLETE = [
    "old_clients_uninstrumented",
    "log_only_events_unavailable",
    "offline_unreported_events_unavailable",
    "retained_history_only",
]


def metadata(row):
    value = row.get("metadata") or "{}"
    return json.loads(value) if isinstance(value, str) else value


def epoch(value):
    return (
        datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        if value
        else None
    )


def fingerprint(ctx):
    return hashlib.sha256(
        "\0".join((ctx["principal"], ctx["family"], ctx["stage"], ctx["code"])).encode()
    ).hexdigest()


def evaluate(operation, now):
    """Project evidence, never turn a missing milestone into definite failure."""
    d = operation["data"]
    e = d.get("client_event") or {}
    condition = "unknown"
    code = None
    stage = "upload"
    if d.get("send_proved"):
        return "recovered", "recovered", "send"
    if d.get("send_committed"):
        return (
            ("unknown", "send_unconfirmed", "send")
            if now >= epoch(d["send_committed"]) + 60
            else ("unknown", None, "send")
        )
    if e.get("outcome") == "cancelled" or e.get("code") in EXPECTED_CODES:
        return (
            "cancelled",
            e.get("code", "operation_cancelled"),
            e.get("stage", "upload"),
        )
    if e.get("outcome") == "recovered":
        return "recovered", "recovered", e.get("stage", "upload")
    if e.get("code") == "report_gap":
        return "unknown", "report_gap", e.get("stage", "upload")
    if d.get("report_gap_count") and not e and not d.get("upload_committed"):
        return "unknown", "report_gap", "upload"
    if e.get("outcome") in ("failed", "unconfirmed"):
        if now < epoch(e["at"]) + 60:
            return "unknown", None, e["stage"]
        return (
            (
                "active"
                if e["outcome"] == "failed" and e.get("retry_state") != "pending"
                else "unknown"
            ),
            e["code"],
            e["stage"],
        )
    if d.get("transcribe_result") and now >= epoch(d["transcribe_result"]) + 60:
        return "unknown", "send_unconfirmed", "send"
    if d.get("transcribe_failed_code") in EXPECTED_CODES:
        return "cancelled", d["transcribe_failed_code"], "transcribe"
    if d.get("transcribe_failed") and now >= epoch(d["transcribe_failed"]) + 60:
        return "active", "transcribe_failed", "transcribe"
    if (
        d.get("transcribe_started")
        and not d.get("transcribe_result")
        and not d.get("transcribe_failed")
        and now >= epoch(d["transcribe_started"]) + 150
    ):
        return "unknown", "transcribe_milestone_missing", "transcribe"
    if (
        d.get("upload_committed")
        and not d.get("transcribe_started")
        and now >= epoch(d["upload_committed"]) + 60
    ):
        return "unknown", "upload_milestone_missing", "upload"
    return condition, code, stage


class ErrorAlerts:
    def __init__(self, server, queue):
        self.server = server
        self.store = server.store
        self.notify = server.notify
        self.queue = queue
        self._facts_lock = asyncio.Lock()
        self.cutover_at = None
        queue.error_alerts = self
        queue.register_kind(
            "error_alert", guard=self.guard, lock_factory=self.delivery_lock
        )

    def mode(self):
        value = os.environ.get("PENTACLE_ERROR_ALERTS_MODE", "off").lower()
        return value if value in ("off", "record-only", "on") else "off"

    @property
    def composite(self):
        return self.server.assistant_composite

    async def start(self):
        self.cutover_at = await self.notify._db.call("error_cutover")

    async def principal(self, msg):
        a = msg.get("_auth_context") or {}
        cid = a.get("credential_id")
        if (
            a.get("operator_authenticated") is not True
            or a.get("transport") != "v2"
            or not cid
            or a.get("operator_authority_source") == "stream_token"
            or a.get("token_verified")
            or any(
                a.get(k)
                for k in ("scoped_principal", "dot_principal", "service_authenticated")
            )
        ):
            raise ValueError("operator_required")
        try:
            r = (
                await asyncio.to_thread(self.server.operator_credential_registry.load)
            ).credentials.get(cid)
        except Exception:
            raise ValueError("credential_revoked") from None
        if not r or r.get("revoked_at") or r.get("scope"):
            raise ValueError("credential_revoked")
        return "operator:" + cid

    async def request(self, msg):
        verb = msg.get("type")
        rid = msg.get("request_id", "")
        wire = {k: v for k, v in msg.items() if not k.startswith("_")}
        try:
            principal = await self.principal(msg)
            if list(VALIDATORS[verb].iter_errors(wire)):
                raise ValueError("invalid_request")
            if verb == "error.report":
                if len(json.dumps(wire, separators=(",", ":")).encode()) > 4096:
                    raise ValueError("invalid_request")
                if msg.get("dropped_count") and msg["code"] != "report_gap":
                    raise ValueError("invalid_request")
                code = msg["code"]
                outcome = msg["outcome"]
                stage = msg["stage"]
                if code in ("upload_milestone_missing", "transcribe_milestone_missing"):
                    raise ValueError("invalid_request")
                if code.startswith(
                    ("upload_", "transcribe_", "send_")
                ) and not code.startswith(stage + "_"):
                    raise ValueError("invalid_request")
                if code == "recovered" and outcome != "recovered":
                    raise ValueError("invalid_request")
                if code in EXPECTED_CODES and outcome != "cancelled":
                    raise ValueError("invalid_request")
                if code == "report_gap" and outcome != "unconfirmed":
                    raise ValueError("invalid_request")
                async with self._facts_lock:
                    result = await self.store.voice_report(principal, msg)
            elif verb == "error.list":
                result = await self.list(msg)
            elif verb == "error.get":
                result = {"detail": await self.detail(msg["id"], msg)}
            elif verb == "error.mark":
                if not msg["id"].startswith("notification:"):
                    raise ValueError("read_only")
                await self.notify._db.call(
                    "error_patch",
                    msg["id"].split(":", 1)[1],
                    {msg["action"] + "_at": iso()},
                )
                result = {"alert": (await self.detail(msg["id"], msg))["alert"]}
            elif verb == "error.settings.get":
                result = {
                    "settings": await self.notify._db.call("error_settings"),
                    "limits": LIMITS,
                    "coverage": await self.coverage(),
                }
            elif verb == "error.settings.set":
                async with self._facts_lock:
                    result = {
                        "setting": await self.notify._db.call(
                            "error_settings_set",
                            actor=principal,
                            expected_revision=msg["expected_revision"],
                            delivery_mode=msg.get("delivery_mode"),
                            mute_for_s=msg.get("mute_for_s"),
                            restore_defaults=msg.get("restore_defaults", False),
                        )
                    }
            else:
                cursor = msg.get("cursor")
                before = None
                if cursor:
                    try:
                        before = int(cursor)
                    except (ValueError, TypeError):
                        raise ValueError("invalid_cursor") from None
                    if before < 1:
                        raise ValueError("invalid_cursor")
                items, next_cursor = await self.notify._db.call(
                    "error_settings_audit", before=before, limit=msg.get("limit", 20)
                )
                result = {"items": items, "next_cursor": next_cursor}
            return {"type": verb + ".ok", "request_id": rid, **result}
        except ValueError as exc:
            code = str(exc)
            if code not in SCHEMA["$defs"]["error"]["properties"]["error_code"]["enum"]:
                code = "invalid_request"
            result = {"type": verb + ".error", "request_id": rid, "error_code": code}
            if code == "rate_limited":
                result["retry_after"] = 60
            if code == "revision_conflict":
                result["current_revision"] = (
                    await self.notify._db.call("error_settings")
                )[0]["revision"]
            return result
        except Exception:
            # Never echo exception strings or caller fields through this surface.
            log.warning(
                "subsystem=error_alerts bug_ref=error_alerts_v1 action=persistence_failed"
            )
            return {
                "type": verb + ".error",
                "request_id": rid,
                "error_code": "persistence_failed",
            }

    async def register_upload(self, msg):
        principal = await self.principal(msg)
        intent = msg["voice_operation"]
        if list(INTENT_VALIDATOR.iter_errors(intent)):
            raise ValueError("invalid_request")
        await self.store.voice_register(
            principal, intent, upload_request_id=msg["request_id"]
        )
        return principal, intent["operation_id"]

    async def transcribe(self, msg, handler):
        if "voice_operation" not in msg:
            return await handler(msg)
        principal = await self.principal(msg)
        intent = msg["voice_operation"]
        if list(INTENT_VALIDATOR.iter_errors(intent)):
            raise ValueError("invalid_request")
        op = await self.store.voice_get(principal, intent["operation_id"])
        if op is None:
            raise ValueError("operation_forbidden")
        for key in ("origin_stream_id", "origin_generation", "operation_kind"):
            if op[key] != intent[key]:
                raise ValueError("origin_mismatch")
        await self.store.voice_milestone(
            principal,
            intent["operation_id"],
            "transcribe_started",
            blob_sha=msg.get("blob_sha"),
            request_id=msg["request_id"],
        )
        try:
            result = await handler(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "transcribe_failed")
            if code == "mime_unsupported":
                code = "unsupported"
            code = code if code in EXPECTED_CODES else "transcribe_failed"
            await self.store.voice_milestone(
                principal, intent["operation_id"], "transcribe_failed", code=code
            )
            raise
        await self.store.voice_milestone(
            principal, intent["operation_id"], "transcribe_result"
        )
        return result

    async def send(self, msg, handler):
        oid = msg.get("operation_id")
        if not oid:
            return await handler(msg)
        principal = await self.principal(msg)
        op = await self.store.voice_get(principal, oid)
        target = str(msg.get("stream_id") or msg.get("to_stream_id") or "") or str(
            msg.get("host") or self.server.local_host
        ) + ":" + str(msg.get("session_name") or "")
        if not op or target != op["origin_stream_id"]:
            raise ValueError("operation_forbidden")
        voice_answers = (msg.get("meta") or {}).get("voice_answers")
        if voice_answers and voice_answers.get("recording_id") != oid:
            raise ValueError("operation_forbidden")
        await self.store.voice_bind_send(principal, oid, msg["request_id"])
        result = await handler(
            {**msg, "_voice_operation_generation": op["origin_generation"]}
        )
        if (
            result.get("action_committed")
            or result.get("submission_confirmed")
            or result.get("delivery") in ("landed", "persisted", "accepted")
        ):
            await self.store.voice_milestone(
                principal, oid, "send_committed", request_id=msg["request_id"]
            )
        if result.get("submission_confirmed") is True:
            await self.store.voice_milestone(
                principal, oid, "send_proved", request_id=msg["request_id"]
            )
        return result

    async def notice_rows(self):
        return await self.store.submit(
            lambda conn: [
                dict(r)
                for r in conn.execute(
                    "SELECT n.*,t.reply AS proof_envelope FROM v2_outbound_notices n LEFT JOIN v2_tell_deliveries t ON t.tell_id=n.tell_id"
                )
            ]
        )

    async def coverage(self):
        operations = await self.store.voice_list()
        latest = max((r["updated_at"] for r in operations), default=None)
        return {
            "cutover_at": self.cutover_at,
            "families": [
                {
                    "family": f,
                    "last_accepted_at": latest if f == FAMILIES[0] else None,
                    "status": "available" if f == FAMILIES[0] else "source_managed",
                }
                for f in FAMILIES
            ],
            "incomplete_reasons": INCOMPLETE,
            "report_gap_count": sum(
                r["data"].get("report_gap_count", 0) for r in operations
            ),
        }

    async def reconcile(self, now=None):
        now = time.time() if now is None else now
        # No new records are backfilled from old native outbox/legacy rows.
        for operation in await self.store.voice_list():
            operation = await self._reconcile_send(operation)
            condition, code, stage = evaluate(operation, now)
            key = operation["principal"] + ":" + operation["operation_id"]
            nid = str(
                __import__("uuid").uuid5(
                    __import__("uuid").NAMESPACE_URL, "error-alert:" + key
                )
            )
            prior = await self.notify._db.call("error_get", nid)
            if code is None or (not prior and condition in ("recovered", "cancelled")):
                continue
            context = {
                "version": 1,
                "family": "voice_operation.v1",
                "principal": operation["principal"],
                "operation_id": operation["operation_id"],
                "origin_stream_id": operation["origin_stream_id"],
                "origin_generation": operation["origin_generation"],
                "operation_revision": operation["revision"],
                "condition": condition,
                "code": code,
                "stage": stage,
                "cutover_at": self.cutover_at,
            }
            # Timer projection can change without a new producer revision. Keep
            # condition/code authoritative but never fabricate operation progress.
            if (
                prior
                and prior["error_context"]["operation_revision"]
                == operation["revision"]
            ):
                row = prior
            else:
                row = await self.notify._db.call(
                    "error_upsert",
                    error_key=key,
                    context=context,
                    title="Voice operation needs review",
                    now=iso(now),
                )
            await self.store.voice_projected(
                operation["principal"], operation["operation_id"], operation["revision"]
            )
            await self._reconcile_fact(row, now)
        await self._rebind_pending(now)
        await self._prune(now)

    async def _reconcile_send(self, operation):
        data = operation["data"]
        if data.get("send_proved"):
            return operation
        request_ids = data.get("server_send_request_ids") or (
            [data["server_send_request_id"]]
            if data.get("server_send_request_id")
            else []
        )
        for request_id in request_ids:
            receipt = await self.store.reconcile_send_receipt(
                operation["origin_stream_id"],
                request_id,
                original_generation=operation["origin_generation"],
            )
            if receipt and receipt.get("state") != "not_landed":
                await self.store.voice_milestone(
                    operation["principal"],
                    operation["operation_id"],
                    "send_committed",
                    request_id=request_id,
                    now=receipt.get("created_at"),
                )
                if receipt.get("submission_confirmed") is True:
                    await self.store.voice_milestone(
                        operation["principal"],
                        operation["operation_id"],
                        "send_proved",
                        request_id=request_id,
                        now=receipt.get("created_at"),
                    )
                    break
        return (
            await self.store.voice_get(
                operation["principal"], operation["operation_id"]
            )
            if request_ids
            else operation
        )

    async def _reconcile_fact(self, row, now):
        ctx = row["error_context"]
        nid = row["notification_id"]
        policy = (await self.notify._db.call("error_settings", now=iso(now)))[0]
        notices = {r["notice_id"]: r for r in await self.notice_rows()}
        # A crash can commit enqueue before the cross-store link/watermark.
        # Recover those immutable rows before applying cancellation or policy.
        ids = list(
            dict.fromkeys(
                [
                    *ctx.get("notice_ids", []),
                    *[
                        r["notice_id"]
                        for r in notices.values()
                        if metadata(r).get("notification_id") == nid
                    ],
                ]
            )
        )
        if ids != ctx.get("notice_ids", []):
            await self.notify._db.call(
                "error_patch",
                nid,
                {"notice_ids": ids},
                expected_operation_revision=ctx["operation_revision"],
            )
            ctx = {**ctx, "notice_ids": ids}
        linked = [notices[n] for n in ids if n in notices]
        changed_policy = [
            r
            for r in linked
            if r["kind"] == "front_desk_held"
            and not r["terminal_at"]
            and metadata(r).get("intent_kind") == "initial"
            and not metadata(r).get("awareness")
            and metadata(r).get("policy_mode") != policy["effective_delivery_mode"]
        ]
        if changed_policy:
            await self._suppress_unattempted(changed_policy, "suppressed_policy", now)
            for r in changed_policy:
                r.update(terminal_at=iso(now), terminal_reason="suppressed_policy")
        recovering = ctx["condition"] in ("recovered", "cancelled")
        transport_rows = [
            notices.get(metadata(r).get("folded_into_notice_id"), r) for r in linked
        ]
        # Claims and guards consume queue attempts without reaching input.
        # Only the existing pre-input tell record makes submission possible.
        attempted = any(
            r.get("proof_envelope") or r["delivered_at"] for r in transport_rows
        )
        if recovering and not attempted:
            await self._suppress_unattempted(
                linked, "suppressed_" + ctx["condition"], now
            )
            await self.notify._db.call(
                "error_patch",
                nid,
                {
                    "disposition": "suppressed_" + ctx["condition"],
                    "enqueued_notice_revision": ctx["desired_notice_revision"],
                },
            )
            return
        if ctx["code"] == "report_gap":
            return
        if self.mode() != "on" or policy["effective_delivery_mode"] == "muted":
            if ctx["desired_notice_revision"] == ctx["enqueued_notice_revision"]:
                await self.notify._db.call("error_patch", nid, {"disposition": "muted"})
            return
        intent = ctx.get("notice_intent")
        desired = ctx["desired_notice_revision"]
        enqueued = ctx["enqueued_notice_revision"]
        latest = linked[-1] if linked else None
        latest_transport = transport_rows[-1] if transport_rows else None
        need_initial = not desired or (
            latest_transport
            and latest_transport["terminal_reason"]
            in ("suppressed_policy", "suppressed_members_changed")
        )
        need_recovery = (
            recovering
            and attempted
            and (not intent or intent.get("kind") != "recovery")
        )
        if desired == enqueued and (need_initial or need_recovery):
            desired += 1
            intent = {
                "kind": "recovery" if need_recovery else "initial",
                "condition": ctx["condition"],
                "code": ctx["code"],
                "stage": ctx["stage"],
                "first_at": row["first_fired_at"],
                "latest_at": row["last_fired_at"],
                "count": row["firing_count"],
                "mode": (
                    "digest" if need_recovery else policy["effective_delivery_mode"]
                ),
                "fingerprint": fingerprint(ctx),
            }
            updated = await self.notify._db.call(
                "error_patch",
                nid,
                {
                    "desired_notice_revision": desired,
                    "notice_intent": intent,
                    "disposition": None,
                },
                expected_operation_revision=ctx["operation_revision"],
            )
            if not updated:
                return
            ctx = {**ctx, "desired_notice_revision": desired, "notice_intent": intent}
        if desired > enqueued:
            composite = self.composite
            if not composite or not composite.enabled:
                return
            async with composite._binding_lock:
                binding = await composite.binding()
                if not binding.get("stream_id") or not binding.get("generation"):
                    return
                async with self._facts_lock:
                    current = await self.store.voice_get(
                        ctx["principal"], ctx["operation_id"]
                    )
                    condition, code, _ = evaluate(current, now)
                    if (
                        condition in ("recovered", "cancelled")
                        and intent["kind"] != "recovery"
                    ) or code is None:
                        return
                    notice = await self._enqueue(row, ctx, binding, now)
            ids = list(dict.fromkeys([*ctx.get("notice_ids", []), notice["notice_id"]]))
            await self.notify._db.call(
                "error_patch",
                nid,
                {"enqueued_notice_revision": desired, "notice_ids": ids},
                expected_operation_revision=ctx["operation_revision"],
            )

    async def _suppress_unattempted(self, rows, reason, now):
        def op(conn):
            with conn:
                for row in rows:
                    conn.execute(
                        "UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason=? WHERE notice_id=? AND delivered_at IS NULL AND terminal_at IS NULL AND NOT EXISTS (SELECT 1 FROM v2_tell_deliveries t WHERE t.tell_id=v2_outbound_notices.tell_id)",
                        (iso(now), reason, row["notice_id"]),
                    )

        await self.store.submit(op)

    def _notice_spec(
        self, row, ctx, binding, now, *, predecessor=None, awareness=False
    ):
        intent = ctx["notice_intent"]
        revision = ctx["desired_notice_revision"]
        nid = row["notification_id"]
        ident = (
            "error-alert:"
            + hashlib.sha256(
                "\0".join(
                    (
                        nid,
                        str(revision),
                        binding["stream_id"],
                        binding["generation"],
                        "awareness" if awareness else "",
                    )
                ).encode()
            ).hexdigest()
        )
        prefix = (
            "BLOCKER"
            if intent["kind"] == "initial" and not awareness
            else "Error alert update"
        )
        text = (
            f"{prefix} voice_operation.v1 {intent['code']} alert={nid} operation={ctx['operation_id']} "
            f"condition={intent['condition']} milestone={intent['stage']} first={intent['first_at']} latest={intent['latest_at']} count={intent['count']} "
            + ("prior_submission=unconfirmed " if awareness else "")
            + "review=#error-alerts Awareness only; no voice replay or repair requested."
        )
        meta = {
            "error_alert_v1": True,
            "notification_id": nid,
            "principal": ctx["principal"],
            "operation_id": ctx["operation_id"],
            "revision": revision,
            "root_generation": binding["generation"],
            "fingerprint": intent["fingerprint"],
            "intent_kind": intent["kind"],
            "policy_mode": intent["mode"],
            "predecessor_notice_id": predecessor,
            "awareness": awareness,
        }
        return dict(
            notice_id=ident,
            tell_id=ident,
            kind="error_alert",
            dedupe_key=ident,
            recipient_stream_id=binding["stream_id"],
            body=text,
            metadata=meta,
            created_at=iso(now),
        )

    async def _enqueue(
        self, row, ctx, binding, now, *, predecessor=None, awareness=False
    ):
        spec = self._notice_spec(
            row, ctx, binding, now, predecessor=predecessor, awareness=awareness
        )

        def op(conn):
            with conn:
                existing = conn.execute(
                    "SELECT * FROM v2_outbound_notices WHERE notice_id=?",
                    (spec["notice_id"],),
                ).fetchone()
                if existing:
                    return dict(existing)  # immutable bytes win after recovery/crash
                meta = spec["metadata"]
                since = iso(now - 900)
                recent = [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT metadata FROM v2_outbound_notices WHERE kind='error_alert' AND created_at>? AND json_extract(metadata,'$.error_alert_v1')=1 AND json_extract(metadata,'$.intent_kind')='initial' AND json_extract(metadata,'$.typed_digest') IS NULL",
                        (since,),
                    )
                ]
                if (
                    ctx["notice_intent"]["mode"] == "digest"
                    or awareness
                    or len(recent) >= 3
                    or any(r.get("fingerprint") == meta["fingerprint"] for r in recent)
                ):
                    spec["kind"] = "front_desk_held"
                result = _insert_outbound_notice_conn(conn, **spec)
                if predecessor:
                    old = conn.execute(
                        "SELECT * FROM v2_outbound_notices WHERE notice_id=?",
                        (predecessor,),
                    ).fetchone()
                    if (
                        old
                        and not conn.execute(
                            "SELECT 1 FROM v2_tell_deliveries WHERE tell_id=?",
                            (old["tell_id"],),
                        ).fetchone()
                    ):
                        conn.execute(
                            "UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason='superseded_binding' WHERE notice_id=? AND delivered_at IS NULL AND terminal_at IS NULL",
                            (iso(now), predecessor),
                        )
                return result

        return await self.store.submit(op)

    @asynccontextmanager
    async def delivery_lock(self, row):
        composite = self.composite
        if not composite:
            yield
            return
        async with composite._binding_lock:
            host, name = self.server.sessions.split(row["recipient_stream_id"])
            async with self.server.sessions._lifecycle_lock(host, name):
                async with self._facts_lock:
                    yield

    async def guard(self, row):
        m = metadata(row)
        composite = self.composite
        if self.mode() != "on":
            return NoticeDecision.defer("delivery_disabled")
        if not composite or not composite.enabled:
            return NoticeDecision.defer("waiting_for_fd")
        binding = await composite.binding()
        if not binding.get("stream_id"):
            return NoticeDecision.defer("waiting_for_fd")
        if (binding["stream_id"], binding["generation"]) != (
            row["recipient_stream_id"],
            m["root_generation"],
        ):
            return NoticeDecision.defer("binding_changed")
        session = await self.store.fetch_session(
            *self.server.sessions.split(row["recipient_stream_id"])
        )
        if (
            not session
            or session["status"] != "open"
            or session["session_generation"] != m["root_generation"]
        ):
            return NoticeDecision.defer("waiting_for_fd")
        # Retry of possible input is always evidence-only, never suppressed into
        # a claim that the old input did not occur.
        if await self.store.get_tell_delivery(row["tell_id"]):
            return None
        policy = (await self.notify._db.call("error_settings"))[0]
        if policy["effective_delivery_mode"] == "muted":
            return NoticeDecision.terminal("suppressed_policy")
        if (
            not m.get("awareness")
            and m.get("intent_kind") == "initial"
            and m.get("policy_mode") != policy["effective_delivery_mode"]
        ):
            return NoticeDecision.terminal("suppressed_policy")
        members = m.get("members") or [m["notification_id"]]
        active = 0
        cancelled = 0
        for nid in members:
            fact = await self.notify._db.call("error_get", nid)
            if not fact:
                continue
            ctx = fact["error_context"]
            operation = await self.store.voice_get(
                ctx["principal"], ctx["operation_id"]
            )
            condition, code, _ = evaluate(operation, time.time())
            if code is None:
                return NoticeDecision.defer("recovery_grace")
            if (
                condition not in ("recovered", "cancelled")
                or m.get("intent_kind") == "recovery"
            ):
                active += 1
            else:
                cancelled += 1
        if active and cancelled:
            return NoticeDecision.terminal("suppressed_members_changed")
        return None if active else NoticeDecision.terminal("suppressed_recovered")

    async def _rebind_pending(self, now):
        composite = self.composite
        if not composite or not composite.enabled:
            return
        async with composite._binding_lock:
            binding = await composite.binding()
            if not binding.get("stream_id") or not binding.get("generation"):
                return
            rows = await self.notice_rows()
            for old in rows:
                m = metadata(old)
                if (
                    not m.get("error_alert_v1")
                    or old["delivered_at"]
                    or old["terminal_at"]
                ):
                    continue
                if (old["recipient_stream_id"], m["root_generation"]) == (
                    binding["stream_id"],
                    binding["generation"],
                ):
                    continue
                prior = await self.store.get_tell_delivery(old["tell_id"])
                if prior:
                    async with self.server.sessions._lifecycle_lock(
                        *self.server.sessions.split(old["recipient_stream_id"])
                    ):
                        prior = await self.server.comms.reconcile_notification_answer(
                            old["tell_id"],
                            old["recipient_stream_id"],
                            m["root_generation"],
                        )
                    if prior and prior["reply"].get("delivery_status") == "delivered":
                        await self.store.submit(
                            lambda conn: self._mark_proved(conn, old["notice_id"], now)
                        )
                        continue
                if m.get("typed_digest"):
                    await self._rebind_digest(old, binding, prior, rows, now)
                    continue
                fact = await self.notify._db.call("error_get", m["notification_id"])
                if not fact:
                    continue
                ctx = fact["error_context"]
                if ctx["condition"] in ("recovered", "cancelled") and not prior:
                    continue
                if prior and any(
                    metadata(r).get("awareness")
                    and metadata(r).get("notification_id") == m["notification_id"]
                    for r in rows
                ):
                    continue
                new = await self._enqueue(
                    fact,
                    ctx,
                    binding,
                    now,
                    predecessor=old["notice_id"],
                    awareness=bool(prior),
                )
                await self.notify._db.call(
                    "error_patch",
                    fact["notification_id"],
                    {
                        "notice_ids": list(
                            dict.fromkeys([*ctx["notice_ids"], new["notice_id"]])
                        )
                    },
                )

    async def _rebind_digest(self, old, binding, prior, rows, now):
        meta = metadata(old)
        if prior and any(
            metadata(r).get("awareness")
            and metadata(r).get("predecessor_notice_id") == old["notice_id"]
            for r in rows
        ):
            return
        nid = (
            "error-digest:"
            + hashlib.sha256(
                (
                    old["notice_id"]
                    + "\0"
                    + binding["stream_id"]
                    + "\0"
                    + binding["generation"]
                ).encode()
            ).hexdigest()
        )
        new_meta = {
            **meta,
            "root_generation": binding["generation"],
            "predecessor_notice_id": old["notice_id"],
            "awareness": bool(prior),
        }
        body = (
            "Prior submission unconfirmed. Awareness summary only.\n" if prior else ""
        ) + old["body"]

        def op(conn):
            with conn:
                existing = conn.execute(
                    "SELECT notice_id FROM v2_outbound_notices WHERE notice_id=?",
                    (nid,),
                ).fetchone()
                if not existing:
                    _insert_outbound_notice_conn(
                        conn,
                        notice_id=nid,
                        tell_id=nid,
                        kind="error_alert",
                        dedupe_key=nid,
                        recipient_stream_id=binding["stream_id"],
                        body=body,
                        metadata=new_meta,
                        created_at=iso(now),
                    )
                if not prior:
                    conn.execute(
                        "UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason='superseded_binding' WHERE notice_id=?",
                        (iso(now), old["notice_id"]),
                    )
                    for member_id in meta["member_notice_ids"]:
                        row = conn.execute(
                            "SELECT metadata FROM v2_outbound_notices WHERE notice_id=?",
                            (member_id,),
                        ).fetchone()
                        if row:
                            m = json.loads(row[0])
                            m["folded_into_notice_id"] = nid
                            conn.execute(
                                "UPDATE v2_outbound_notices SET metadata=? WHERE notice_id=?",
                                (json.dumps(m, sort_keys=True), member_id),
                            )

        await self.store.submit(op)

    @staticmethod
    def _mark_proved(conn, nid, now):
        with conn:
            conn.execute(
                "UPDATE v2_outbound_notices SET delivered_at=?,lease_owner=NULL,lease_until=NULL WHERE notice_id=?",
                (iso(now), nid),
            )

    async def _prune(self, now):
        notices = {r["notice_id"]: r for r in await self.notice_rows()}
        settled = set()
        for fact in await self.notify._db.call("error_rows"):
            ctx = fact.get("error_context")
            if not ctx or ctx["condition"] not in ("recovered", "cancelled"):
                continue
            if ctx["desired_notice_revision"] > ctx["enqueued_notice_revision"]:
                continue
            if all(
                self.delivery(notices.get(n), notices)["state"]
                in (
                    "delivered",
                    "failed",
                    "suppressed_recovered",
                    "suppressed_cancelled",
                )
                for n in ctx["notice_ids"]
            ):
                settled.add(fact["notification_id"])
        await self.notify._db.call(
            "prune_resolved", now=iso(now), settled_error_ids=settled
        )
        retained = {
            (r["error_context"]["principal"], r["error_context"]["operation_id"])
            for r in await self.notify._db.call("error_rows")
            if r.get("error_context")
        }
        await self.store.voice_prune(retained, now)

    @staticmethod
    def proof_at(row):
        if not row or not row.get("delivered_at"):
            return None
        try:
            envelope = json.loads(row.get("proof_envelope") or "{}")
        except (ValueError, TypeError):
            return None
        proof = envelope.get("delivery") or {}
        reply = envelope.get("reply") or {}
        m = metadata(row)
        generation = proof.get("notification_answer_generation")
        if (
            reply.get("delivery_status") == "delivered"
            and proof.get("proof_state") == "proven"
            and proof.get("proof_event_id")
            and generation
            and proof.get("to_stream_id") == row["recipient_stream_id"]
            and (not m.get("root_generation") or generation == m["root_generation"])
        ):
            return proof.get("proof_event_at") or row["delivered_at"]
        return None

    def delivery(self, row, notices, ctx=None):
        result = dict.fromkeys(SCHEMA["$defs"]["delivery"]["properties"])
        result["state"] = "legacy_unknown"

        def terminal_state(reason):
            if reason in ("unconfirmed_after_bound", "submission_proof_pending"):
                return "unconfirmed"
            if reason == "suppressed_policy":
                return "muted"
            if reason in (
                "suppressed_cancelled",
                "suppressed_recovered",
                "suppressed_members_changed",
            ):
                return (
                    "suppressed_cancelled"
                    if reason == "suppressed_cancelled"
                    or (ctx or {}).get("condition") == "cancelled"
                    else "suppressed_recovered"
                )
            return "failed"

        if row:
            m = metadata(row)
            result.update(
                notice_id=row["notice_id"],
                tell_id=row["tell_id"],
                recipient_stream_id=row["recipient_stream_id"],
                recipient_generation=m.get("root_generation"),
                predecessor_notice_id=m.get("predecessor_notice_id"),
                folded_into_notice_id=m.get("folded_into_notice_id"),
            )
            if row["delivered_at"]:
                proved = self.proof_at(row)
                result.update(
                    state=(
                        "delivered"
                        if proved and not m.get("awareness")
                        else (
                            "unconfirmed"
                            if m.get("error_alert_v1")
                            else "legacy_unknown"
                        )
                    ),
                    proof_at=proved if not m.get("awareness") else None,
                )
            elif m.get("folded_into_notice_id"):
                folded = notices.get(m["folded_into_notice_id"])
                if folded:
                    result.update(
                        recipient_stream_id=folded["recipient_stream_id"],
                        recipient_generation=metadata(folded).get("root_generation"),
                    )
                if folded and folded["delivered_at"]:
                    proved = self.proof_at(folded)
                    result.update(
                        state="delivered" if proved else "unconfirmed", proof_at=proved
                    )
                elif folded and folded["terminal_at"]:
                    result["state"] = terminal_state(folded.get("terminal_reason"))
                else:
                    result["state"] = "folded"
            elif row["terminal_at"]:
                reason = row.get("terminal_reason") or ""
                result["state"] = terminal_state(reason)
            elif row.get("last_error") in ("waiting_for_fd", "binding_changed"):
                result["state"] = "waiting_for_fd"
            elif row["kind"] == "front_desk_held":
                result["state"] = "held_for_digest"
            elif row.get("last_error") in (
                "submission_proof_pending",
                "submission_unconfirmed",
            ):
                result["state"] = "unconfirmed"
            elif row["attempts"]:
                result["state"] = "retrying"
            else:
                result["state"] = "queued"
            if result["state"] == "failed":
                result["next_action"] = (
                    "Owner disposition required; automatic voice replay is disabled"
                )
        elif ctx:
            result["state"] = ctx.get("disposition") or (
                "waiting_for_fd"
                if ctx["desired_notice_revision"] > ctx["enqueued_notice_revision"]
                else "muted"
            )
        return result

    async def _retained_primary_bindings(self, binding):
        """Follow only the unambiguous recorded ancestry of the current primary.

        The older audit has no assistant-name column. Never collect unrelated
        assistants' historical targets, or infer a link across a missing audit.
        This read-only association is not a producer or a completeness claim.
        """

        def identity(value):
            return (
                value.get("stream_id"),
                value.get("generation"),
                value.get("revision"),
            )

        cursor = identity(binding)
        pairs = {(cursor[0], cursor[1])} if cursor[0] and cursor[1] else set()
        audits = await self.store.submit(
            lambda conn: [
                dict(row)
                for row in conn.execute(
                    "SELECT old_binding_json,new_binding_json FROM v2_assistant_rebind_audit WHERE outcome='ok'"
                )
            ]
        )
        edges = []
        for audit in audits:
            try:
                old, new = json.loads(audit["old_binding_json"]), json.loads(
                    audit["new_binding_json"]
                )
                if isinstance(old, dict) and isinstance(new, dict):
                    edges.append((identity(old), identity(new)))
            except (TypeError, ValueError):
                continue
        while type(cursor[2]) is int and cursor[2] > 0:
            predecessors = {
                old for old, new in edges if new == cursor and old[2] == cursor[2] - 1
            }
            if len(predecessors) != 1:
                break
            cursor = predecessors.pop()
            if cursor[0] and cursor[1]:
                pairs.add((cursor[0], cursor[1]))
        return pairs

    async def _items(self, msg):
        errors = []
        rows = []
        notices = {}
        try:
            rows = await self.notify._db.call("error_rows")
        except Exception:
            errors.append("notifications_unavailable")
        try:
            notices = {r["notice_id"]: r for r in await self.notice_rows()}
        except Exception:
            errors.append("delivery_unavailable")
        if len(errors) == 2:
            raise ValueError("unavailable")
        setting = (await self.notify._db.call("error_settings"))[0]
        binding = await self.composite.binding() if self.composite else {}
        known_bindings = await self._retained_primary_bindings(binding)
        known_streams = {stream for stream, _ in known_bindings}
        native = [
            r
            for r in notices.values()
            if r["recipient_stream_id"] in known_streams
            and (
                not metadata(r).get("owner_generation")
                or (r["recipient_stream_id"], metadata(r)["owner_generation"])
                in known_bindings
            )
            and (
                r["kind"] == "wake_missed"
                or (
                    r["kind"] == "reconciler"
                    and metadata(r).get("class")
                    in (
                        "row_open_session_dead",
                        "row_open_host_unreachable",
                        "row_closed_tree_alive",
                    )
                )
            )
        ]
        associated_native = set()
        items = []
        for row in rows:
            ctx = row.get("error_context")
            source = {"type": "notification", "id": row["notification_id"]}
            family = "legacy_notification"
            if ctx:
                family = "voice_operation.v1"
            elif row["producer"] == "session_reconciler":
                family = "session_lifecycle"
            elif "integrity" in row["producer"]:
                family = "integrity"
            elif row["producer"] in ("altum-bot-cd", "wmi-backup"):
                family = "system_notification"
            linked = [
                notices[n] for n in (ctx or {}).get("notice_ids", []) if n in notices
            ]
            if not ctx:
                linked = [
                    r
                    for r in native
                    if metadata(r).get("notification_id") == row["notification_id"]
                    or (
                        row.get("dedup_key") and r.get("episode_id") == row["dedup_key"]
                    )
                ]
                associated_native.update(r["notice_id"] for r in linked)
            # Prefer the first proved receipt; subsequent awareness is not a
            # replay that overwrites the original recipient/proof.
            primary = [r for r in linked if not metadata(r).get("awareness")]
            selected = next(
                (
                    r
                    for r in primary
                    if self.delivery(r, notices)["state"] == "delivered"
                ),
                primary[-1] if primary else None,
            )
            policy = (
                {
                    k: setting[k]
                    for k in ("delivery_mode", "effective_delivery_mode", "mute_until")
                }
                if ctx
                else {
                    "delivery_mode": "source_managed",
                    "effective_delivery_mode": "source_managed",
                    "mute_until": None,
                }
            )
            policy["source_managed"] = not bool(ctx)
            items.append(
                {
                    "id": "notification:" + row["notification_id"],
                    "source": source,
                    "family": family,
                    "code": ctx["code"] if ctx else "retained_warning",
                    "title": row["title"],
                    "first_at": row.get("first_fired_at") or row["created_at"],
                    "latest_at": row.get("last_fired_at") or row["updated_at"],
                    "count": row.get("firing_count") or 1,
                    "condition": ctx["condition"] if ctx else "unknown",
                    "attention": {
                        "seen_at": (ctx or {}).get("seen_at"),
                        "ack_at": (ctx or {}).get("ack_at"),
                        "legacy_state": row["state"],
                    },
                    "delivery": self.delivery(selected, notices, ctx),
                    "policy": policy,
                    "correlation": {
                        k: (ctx or {}).get(k)
                        for k in (
                            "operation_id",
                            "origin_stream_id",
                            "origin_generation",
                        )
                    },
                    "read_only": not bool(ctx),
                    "overdue": bool(
                        ctx
                        and ctx["condition"] not in ("recovered", "cancelled")
                        and epoch(row["created_at"]) < time.time() - 30 * 86400
                    ),
                }
            )
        # Existing notices are source projections only. No error_context backfill.
        for row in native:
            m = metadata(row)
            if row["notice_id"] in associated_native:
                continue
            items.append(
                {
                    "id": "outbox:" + row["notice_id"],
                    "source": {"type": "outbox", "id": row["notice_id"]},
                    "family": "native_notice",
                    "code": row["kind"],
                    "title": "Retained " + row["kind"].replace("_", " "),
                    "first_at": row["created_at"],
                    "latest_at": row["created_at"],
                    "count": 1,
                    "condition": "unknown",
                    "attention": {
                        "seen_at": None,
                        "ack_at": None,
                        "legacy_state": None,
                    },
                    "delivery": self.delivery(row, notices),
                    "policy": {
                        "delivery_mode": "source_managed",
                        "effective_delivery_mode": "source_managed",
                        "mute_until": None,
                        "source_managed": True,
                    },
                    "correlation": dict.fromkeys(
                        ("operation_id", "origin_stream_id", "origin_generation")
                    ),
                    "read_only": True,
                    "overdue": False,
                }
            )
        try:
            consent = await self.server._consent_notifications_for_msg(msg)
            for row in consent:
                if row.get("severity") not in ("warning", "critical"):
                    continue
                nid = row.get("notification_id") or row.get("id")
                created = row.get("created_at") or self.cutover_at
                items.append(
                    {
                        "id": "consent:" + nid,
                        "source": {"type": "consent", "id": nid},
                        "family": "consent_security",
                        "code": "retained_security",
                        "title": row.get("title", "Security review"),
                        "first_at": created,
                        "latest_at": row.get("updated_at") or created,
                        "count": 1,
                        "condition": "unknown",
                        "attention": {
                            "seen_at": None,
                            "ack_at": None,
                            "legacy_state": row.get("state"),
                        },
                        "delivery": self.delivery(None, notices),
                        "policy": {
                            "delivery_mode": "source_managed",
                            "effective_delivery_mode": "source_managed",
                            "mute_until": None,
                            "source_managed": True,
                        },
                        "correlation": dict.fromkeys(
                            ("operation_id", "origin_stream_id", "origin_generation")
                        ),
                        "read_only": True,
                        "overdue": False,
                    }
                )
        except Exception:
            errors.append("consent_unavailable")
        return items, notices, errors

    async def list(self, msg):
        items, _, errors = await self._items(msg)
        f = msg.get("filter", "all")
        if f == "received":
            items = [i for i in items if i["delivery"]["state"] == "delivered"]
        elif f == "failed":
            items = [i for i in items if i["delivery"]["state"] == "failed"]
        elif f == "muted":
            items = [
                i for i in items if i["policy"]["effective_delivery_mode"] == "muted"
            ]
        elif f == "pending":
            items = [
                i
                for i in items
                if i["delivery"]["state"]
                in (
                    "waiting_for_fd",
                    "queued",
                    "held_for_digest",
                    "folded",
                    "retrying",
                    "unconfirmed",
                )
            ]
        for key in ("family", "condition"):
            if msg.get(key):
                items = [i for i in items if i[key] == msg[key]]
        if msg.get("id"):
            items = [
                i
                for i in items
                if msg["id"]
                in (i["id"], i["source"]["id"], i["correlation"]["operation_id"])
            ]
        items.sort(key=lambda i: (i["latest_at"], i["id"]), reverse=True)
        filters = {k: msg.get(k) for k in ("filter", "family", "condition", "id")}
        signature = hashlib.sha256(
            json.dumps(filters, sort_keys=True).encode()
        ).hexdigest()[:16]
        if msg.get("cursor"):
            try:
                cursor = json.loads(base64.urlsafe_b64decode(msg["cursor"]))
                if (
                    set(cursor) != {"after", "filter"}
                    or cursor["filter"] != signature
                    or not isinstance(cursor["after"], list)
                    or len(cursor["after"]) != 2
                ):
                    raise ValueError()
                items = [
                    i
                    for i in items
                    if (i["latest_at"], i["id"]) < tuple(cursor["after"])
                ]
            except Exception:
                raise ValueError("invalid_cursor") from None
        limit = msg.get("limit", 50)
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "after": [page[-1]["latest_at"], page[-1]["id"]],
                        "filter": signature,
                    }
                ).encode()
            ).decode()
        return {
            "items": page,
            "next_cursor": next_cursor,
            "coverage": await self.coverage(),
            "partial": bool(errors),
            "errors": errors,
        }

    async def detail(self, identity, msg):
        items, notices, errors = await self._items(msg)
        item = next((i for i in items if i["id"] == identity), None)
        if item is None:
            raise ValueError("unavailable" if errors else "not_found")
        milestones = []
        attempts = []
        if item["family"] == "voice_operation.v1":
            row = await self.notify._db.call("error_get", item["source"]["id"])
            ctx = row["error_context"]
            operation = await self.store.voice_get(
                ctx["principal"], ctx["operation_id"]
            )
            for stage in (
                "upload_committed",
                "transcribe_started",
                "transcribe_result",
                "transcribe_failed",
                "send_committed",
                "send_proved",
            ):
                if operation["data"].get(stage):
                    milestones.append(
                        {
                            "stage": stage,
                            "at": operation["data"][stage],
                            "evidence": "server",
                            "code": operation["data"].get(stage + "_code"),
                        }
                    )
            e = operation["data"].get("client_event")
            if e:
                milestones.append(
                    {
                        "stage": e["stage"],
                        "at": e["at"],
                        "evidence": "client",
                        "code": e["code"],
                    }
                )
            ids = list(ctx["notice_ids"])
            for nid in list(ids):
                linked = (
                    metadata(notices[nid]).get("folded_into_notice_id")
                    if nid in notices
                    else None
                )
                if linked and linked not in ids:
                    ids.append(linked)
            for r in notices.values():
                if metadata(r).get("notification_id") == row["notification_id"] or row[
                    "notification_id"
                ] in metadata(r).get("members", []):
                    if r["notice_id"] not in ids:
                        ids.append(r["notice_id"])
            for nid in ids:
                r = notices.get(nid)
                if not r:
                    continue
                m = metadata(r)
                attempts.append(
                    {
                        "notice_id": nid,
                        "tell_id": r["tell_id"],
                        "recipient_stream_id": r["recipient_stream_id"],
                        "recipient_generation": m.get("root_generation"),
                        "attempts": r["attempts"],
                        "created_at": r["created_at"],
                        "last_attempt_at": None,
                        "proof_at": self.proof_at(r),
                        "terminal_at": r["terminal_at"],
                        "reason_code": (
                            "transport_failed"
                            if r.get("terminal_reason")
                            else "proof_pending" if r.get("last_error") else None
                        ),
                        "folded_into_notice_id": m.get("folded_into_notice_id"),
                        "predecessor_notice_id": m.get("predecessor_notice_id"),
                    }
                )
        return {
            "alert": item,
            "milestones": milestones,
            "attempts": attempts,
            "recovery_evidence": [
                m
                for m in milestones
                if m["stage"] == "send_proved"
                or m["code"] in ("recovered", "operation_cancelled")
            ],
        }
