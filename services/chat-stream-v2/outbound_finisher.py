"""Server-owned completion of a committed outbound unit (``send_mandate``).

Once the bytes are on the daemon the client has no further job: this worker
transcribes (voice) and sends exactly once with the client's pre-committed
identity through the ordinary error-alert-wrapped ``send`` handler. State lives
in the existing ``voice_operations`` row (``data.server_mandate``/``data.state``);
there is no second queue. The per-operation lock is never held across an
awaited send, ASR call or wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from receipt_proof import verified_original_generation
from sessions import VerbError
from store_voice_operations import iso

log = logging.getLogger("chat_streamd_v2.outbound_finisher")

ACTIVE = ("pending", "running")
MANDATE_MAX_BYTES = 16 * 1024
RETRY_DELAYS = (5, 15, 30)
RETRY_EVERY = 60
#: ASR retry window, and how long an orphaned ``submitting`` row waits for a receipt.
WAIT_LIMIT = 600
#: A send refusal that proves the message already exists under this identity.
EXISTS_CODES = frozenset({
    "send_request_conflict", "assistant_receipt_request_conflict",
    "assistant_input_idempotency_conflict",
})
MILESTONES = (
    "upload_committed", "pending_blobs", "transcribe_started", "transcribe_result",
    "transcribe_failed", "transcribe_failed_code", "send_attempted", "send_committed",
    "send_proved", "server_finish_failed", "server_finish_failed_code", "cancelled_at",
    "send_request_id",
)
_EXPECTED_ASR = {"no_speech", "too_long", "unsupported"}


def _epoch(value):
    from error_alerts import epoch
    return epoch(value)


class OutboundFinisher:
    def __init__(self, server, alerts, *, send=None, transcriber=None,
                 sleep=asyncio.sleep, clock=time.time):
        self.server = server
        self.alerts = alerts
        self.store = server.store
        self._send = send or (lambda msg: server.handlers["send"](msg))
        self._transcriber = transcriber
        self._sleep = sleep
        self._clock = clock
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._workers: dict[tuple[str, str], asyncio.Task] = {}

    # -- contract ---------------------------------------------------------

    def validate(self, intent, mandate, *, upload_audio=None):
        """Semantic checks after the schema; any failure is invalid_request."""
        if len(json.dumps(mandate, separators=(",", ":")).encode()) > MANDATE_MAX_BYTES:
            raise ValueError("invalid_request")
        if mandate["client_build"] != intent["client_build"]:
            raise ValueError("invalid_request")
        origin = intent["origin_stream_id"]
        composite = self.server._composite_for(origin) is not None
        if bool(mandate.get("msg_id")) != composite or (
                composite and mandate["msg_id"] != mandate["optimistic_id"]):
            raise ValueError("invalid_request")
        if intent["operation_kind"] == "message":
            if any(k in mandate for k in ("text_prefix", "original_request", "audio")):
                raise ValueError("invalid_request")
            if not mandate.get("text") and not mandate.get("attachments"):
                raise ValueError("invalid_request")
            return mandate
        audio = mandate.get("audio") or upload_audio
        if "text" in mandate or not audio or (
                upload_audio and mandate.get("audio") and mandate["audio"] != upload_audio):
            raise ValueError("invalid_request")
        proof = mandate.get("original_request")
        if (not isinstance(proof, dict) or proof.get("logical_id") != intent["operation_id"]
                or proof.get("target") != origin
                or proof.get("generation") != intent["origin_generation"]
                or proof.get("sequence") != mandate["sequence"]
                or verified_original_generation(proof, origin, mandate["request_id"])
                != intent["origin_generation"]):
            raise ValueError("invalid_request")
        answers = mandate["meta"].get("voice_answers")
        if answers is not None and (
                not isinstance(answers, dict) or answers.get("recording_id") != intent["operation_id"]
                or answers.get("blob_sha") != audio["sha256"]):
            raise ValueError("invalid_request")
        return {**mandate, "audio": audio}

    async def register(self, principal, intent, mandate, *, upload_audio=None):
        stored = self.validate(intent, mandate, upload_audio=upload_audio)

        def mutate(row):
            data = row["data"]
            prior = data.get("server_mandate")
            if prior is not None:
                if prior != stored:
                    raise ValueError("event_conflict")
                return
            data.update(server_mandate=stored, server_mandate_at=iso(self._clock()), state="pending")

        await self.store.voice_mutate(principal, intent["operation_id"], mutate)
        self.kick(principal, intent["operation_id"])

    async def cancel(self, principal, operation_id):
        """Fences any future submission; never implies the message landed."""
        async with self._lock(principal, operation_id):
            row = await self.store.voice_get(principal, operation_id)
            if row is None:
                raise ValueError("operation_forbidden")
            data = row["data"]
            if not data.get("server_mandate"):
                return "no_mandate"
            state = data.get("state")
            if state == "submitting":
                return "in_flight"
            if state in ("committed", "proved"):
                return "committed"
            if state in ACTIVE:
                now = iso(self._clock())

                def mutate(r):
                    if r["data"].get("state") in ACTIVE:
                        r["data"].update(state="cancelled", cancelled_at=now)
                await self.store.voice_mutate(principal, operation_id, mutate)
            return "cancelled"

    async def get(self, principal, operation_id):
        row = await self.store.voice_get(principal, operation_id)
        if row is None or not row["data"].get("server_mandate"):
            raise ValueError("operation_forbidden")
        data = row["data"]
        state = data["state"]
        if state == "committed" and data.get("send_proved"):
            state = "proved"
        return {"operation_id": operation_id, "state": state,
                "milestones": {k: data.get(k) for k in MILESTONES}}

    # -- kicks --------------------------------------------------------------

    async def start(self):
        """A fresh process has no workers: running rows go back to pending."""
        for row in await self.store.outbound_rows():
            if row["data"].get("state") == "running":
                await self.store.voice_mutate(row["principal"], row["operation_id"], _reset_running)
        await self.kick_all()

    def kick(self, principal, operation_id):
        key = (principal, operation_id)
        task = self._workers.get(key)
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self._run(principal, operation_id))
        self._workers[key] = task
        task.add_done_callback(lambda t, key=key: self._finished(key, t))
        return task

    async def kick_all(self):
        for row in await self.store.outbound_rows():
            if row["data"].get("state") in ACTIVE:
                self.kick(row["principal"], row["operation_id"])

    async def kick_blob(self, principal, sha):
        for row in await self.store.outbound_rows():
            if row["principal"] == principal and row["data"].get("state") in ACTIVE and sha in _needed(row):
                self.kick(principal, row["operation_id"])

    async def reconcile_pass(self, now=None):
        """Each alert pass: re-kick active rows, resolve orphaned submissions."""
        now = self._clock() if now is None else now
        for row in await self.store.outbound_rows():
            state = row["data"].get("state")
            if state in ACTIVE:
                self.kick(row["principal"], row["operation_id"])
            elif state == "submitting" and not self._busy(row):
                await self._reconcile_submitting(row["principal"], row["operation_id"], now)

    # -- worker -------------------------------------------------------------

    def _lock(self, principal, operation_id):
        return self._locks.setdefault((principal, operation_id), asyncio.Lock())

    def _busy(self, row):
        task = self._workers.get((row["principal"], row["operation_id"]))
        return task is not None and not task.done()

    def _finished(self, key, task):
        if self._workers.get(key) is task:
            del self._workers[key]
        if not task.cancelled() and task.exception() is not None:
            # Row stays running; the next pass re-kicks it. Never log content.
            log.warning("subsystem=outbound_finisher action=worker_failed kind=%s",
                        type(task.exception()).__name__)

    async def _run(self, principal, operation_id):
        me = uuid.uuid4().hex
        async with self._lock(principal, operation_id):
            row = await self.store.voice_get(principal, operation_id)
            if row is None or not row["data"].get("server_mandate") or row["data"].get("state") not in ACTIVE:
                return
            started = iso(self._clock())

            def claim(r):
                if r["data"].get("state") in ACTIVE:
                    r["data"].update(state="running", worker={"id": me, "started_at": started})
            await self.store.voice_mutate(principal, operation_id, claim)
        await self._work(principal, operation_id, me)

    async def _mine(self, principal, operation_id, me, change=None):
        """Apply change(data) only while this worker still owns a running row."""
        def mutate(row):
            data = row["data"]
            if data.get("state") != "running" or (data.get("worker") or {}).get("id") != me:
                return False
            if change is not None:
                change(data)
            return True
        return await self.store.voice_mutate(principal, operation_id, mutate) is True

    async def _fail(self, principal, operation_id, me, code):
        now = iso(self._clock())
        await self._mine(principal, operation_id, me, lambda d: d.update(
            state="failed", server_finish_failed=now, server_finish_failed_code=code))

    async def _work(self, principal, operation_id, me):
        row = await self.store.voice_get(principal, operation_id)
        mandate = row["data"]["server_mandate"]
        missing = [sha for sha in _needed(row) if not self._blob_present(sha)]
        now = iso(self._clock())

        def readiness(data):
            data["pending_blobs"] = len(missing)
            if not missing:
                data.setdefault("ready_at", now)
        if not await self._mine(principal, operation_id, me, readiness) or missing:
            return  # the next blob commit kicks again
        if not await self._credential_ok(principal):
            return await self._fail(principal, operation_id, me, "credential_revoked")
        if not await self._origin_ok(row):
            return await self._fail(principal, operation_id, me, "origin_unavailable")
        transcript = None
        if row["operation_kind"] != "message":
            transcript = await self._transcribe(principal, row, me)
            if transcript is None:
                return
        await self._submit(principal, operation_id, me, mandate, transcript)

    async def _transcribe(self, principal, row, me):
        oid = row["operation_id"]
        audio = row["data"]["server_mandate"]["audio"]
        m = self.store.voice_milestone
        if not row["data"].get("upload_committed"):
            # Protocol-1 (web) audio: the committed blob is the upload evidence.
            await m(principal, oid, "upload_committed", blob_sha=audio["sha256"])
        transcriber = self._transcriber or self.server._get_transcriber()
        first = self._clock()
        delays = iter(RETRY_DELAYS)
        while True:
            if not await self._mine(principal, oid, me):
                return None  # cancelled while waiting
            # No transcribe request id: a later client-owned Retry binds its own.
            await m(principal, oid, "transcribe_started", blob_sha=audio["sha256"])
            try:
                result = await transcriber.transcribe_blob(
                    request_id=f"transcribe-{oid}", blob_sha=audio["sha256"], mime=audio["mime"])
                break
            except VerbError as exc:
                if exc.code != "backend_unavailable":
                    code = "unsupported" if exc.code == "mime_unsupported" else exc.code
                    await m(principal, oid, "transcribe_failed",
                            code=code if code in _EXPECTED_ASR else "transcribe_failed")
                    await self._fail(principal, oid, me, code)
                    return None
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass
            if self._clock() - first >= WAIT_LIMIT:
                await m(principal, oid, "transcribe_failed", code="transcribe_failed")
                await self._fail(principal, oid, me, "transcribe_unavailable")
                return None
            await self._sleep(next(delays, RETRY_EVERY))
        text = str(result.get("text") or "").strip()
        if not text:
            await m(principal, oid, "transcribe_failed", code="no_speech")
            await self._fail(principal, oid, me, "no_speech")
            return None
        await m(principal, oid, "transcribe_result")
        return text

    async def _submit(self, principal, operation_id, me, mandate, transcript):
        attempt = {"attempt_id": uuid.uuid4().hex, "request_id": mandate["request_id"]}
        async with self._lock(principal, operation_id):
            row = await self.store.voice_get(principal, operation_id)
            data = row["data"]
            if data.get("state") != "running" or (data.get("worker") or {}).get("id") != me:
                return  # cancelled (or superseded) before submission
            landed = await self._receipts(row)
            if landed not in (None, "not_landed"):
                return await self._record(principal, operation_id, landed, me=me)
            if not await self._origin_ok(row):
                return await self._fail(principal, operation_id, me, "origin_unavailable")
            now = iso(self._clock())
            attempt["started_at"] = now

            def submitting(d):
                d.update(state="submitting", attempt=attempt)
                d.setdefault("send_attempted", now)
            if not await self._mine(principal, operation_id, me, submitting):
                return
        # The lock is released before the send; a cancel now reads `in_flight`.
        frame = self._frame(row, mandate, transcript, principal)
        try:
            result = await self._send(frame)
        except asyncio.CancelledError:
            raise
        except VerbError as exc:
            if exc.code in EXISTS_CODES:
                outcome = "committed"
            elif exc.extra.get("phase") == "enter_failed":
                outcome = None
            else:
                outcome = ("failed", "origin_unavailable" if exc.code == "voice_origin_generation_conflict"
                           else exc.code)
        except ValueError as exc:  # unwrapped ErrorAlerts.send refusal
            outcome = ("failed", str(exc) or "invalid_request")
        except Exception:
            outcome = None
        else:
            committed = (result.get("action_committed") or result.get("submission_confirmed")
                         or result.get("delivery") in ("landed", "persisted", "accepted"))
            outcome = ("proved" if result.get("submission_confirmed") is True else "committed") if committed else None
        if outcome is None:
            return  # ambiguous: stays submitting for the receipt reconcile pass
        async with self._lock(principal, operation_id):
            await self._record(principal, operation_id, outcome, attempt_id=attempt["attempt_id"])

    async def _record(self, principal, operation_id, outcome, *, me=None, attempt_id=None):
        now = iso(self._clock())

        def mutate(row):
            data = row["data"]
            if me is not None and (data.get("state") != "running" or (data.get("worker") or {}).get("id") != me):
                return
            if attempt_id is not None and (data.get("state") != "submitting"
                                           or (data.get("attempt") or {}).get("attempt_id") != attempt_id):
                return  # a stale worker's result is dropped
            if isinstance(outcome, tuple):
                data.update(state="failed", server_finish_failed=now, server_finish_failed_code=outcome[1])
            else:
                data["state"] = outcome
        await self.store.voice_mutate(principal, operation_id, mutate)

    async def _reconcile_submitting(self, principal, operation_id, now):
        async with self._lock(principal, operation_id):
            row = await self.store.voice_get(principal, operation_id)
            data = row["data"] if row else {}
            if data.get("state") != "submitting":
                return
            attempt_id = (data.get("attempt") or {}).get("attempt_id")
            landed = await self._receipts(row)
            if landed == "not_landed":
                landed = ("failed", "not_landed")
            elif landed is None:
                started = _epoch((data.get("attempt") or {}).get("started_at"))
                if started is None or now < started + WAIT_LIMIT:
                    return
                landed = ("failed", "send_unconfirmed")
            await self._record(principal, operation_id, landed, attempt_id=attempt_id)

    async def _receipts(self, row):
        """Server-bound attempts only; client-reported ids are never consulted."""
        data = row["data"]
        ids = data.get("server_send_request_ids") or (
            [data["server_send_request_id"]] if data.get("server_send_request_id") else [])
        seen_not_landed = False
        for request_id in ids:
            receipt = await self.store.reconcile_send_receipt(
                row["origin_stream_id"], request_id, original_generation=row["origin_generation"])
            if not receipt:
                continue
            if receipt.get("state") == "not_landed":
                seen_not_landed = True
                continue
            m = self.store.voice_milestone
            await m(row["principal"], row["operation_id"], "send_committed", request_id=request_id)
            if receipt.get("submission_confirmed") is True:
                await m(row["principal"], row["operation_id"], "send_proved", request_id=request_id)
                return "proved"
            return "committed"
        return "not_landed" if seen_not_landed else None

    # -- helpers --------------------------------------------------------------

    def _blob_present(self, sha):
        blobs = getattr(self.server, "blobs", None)
        try:
            return blobs is not None and blobs._path_for(sha).is_file()
        except ValueError:
            return False

    async def _credential_ok(self, principal):
        try:
            return await self.alerts.principal({"_auth_context": _auth(principal)}) == principal
        except ValueError:
            return False

    async def _origin_ok(self, row):
        host, _, name = row["origin_stream_id"].partition(":")
        session = await self.store.fetch_session(host, name) if name else None
        return bool(session and session.get("status") == "open"
                    and session.get("session_generation") == row["origin_generation"])

    @staticmethod
    def _frame(row, mandate, transcript, principal):
        """The same frame the client would send (sendPayloadForSession)."""
        origin = row["origin_stream_id"]
        if transcript is None:
            text = mandate.get("text") or ""
        else:
            text = "\n".join(part for part in (mandate.get("text_prefix"), transcript) if part)
        frame = {"type": "send", "request_id": mandate["request_id"], "operation_id": row["operation_id"]}
        if mandate.get("msg_id"):
            frame.update(msg_id=mandate["msg_id"], stream_id=origin, message=text)
        else:
            host, _, name = origin.partition(":")
            frame.update(host=host, session_name=name, text=text, optimistic_id=mandate["optimistic_id"])
        for key in ("attachments", "meta", "reply_to_message_id", "reply_to_question_id"):
            if mandate.get(key):
                frame[key] = mandate[key]
        frame["_auth_context"] = _auth(principal)
        return frame


def _needed(row):
    mandate = row["data"]["server_mandate"]
    if row["operation_kind"] == "message":
        return [a["key"] for a in mandate.get("attachments") or ()]
    return [mandate["audio"]["sha256"]]


def _reset_running(row):
    if row["data"].get("state") == "running":
        row["data"]["state"] = "pending"
        row["data"].pop("worker", None)


def _auth(principal):
    return {
        "operator_authenticated": True, "transport": "v2",
        "credential_id": principal.split(":", 1)[1], "operator_principal": principal,
        "token_verified": False, "stream_id": "", "service_authenticated": False,
        "dot_principal": False, "outbound_finisher": True,
    }
