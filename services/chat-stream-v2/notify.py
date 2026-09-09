"""notify.py - questions (`prompt.*`) + notifications/updates, LIFTED from v1.

Per `v1_code_reuse_map.md` these subsystems are healthy in prod (operator
testimony; B11 already fixed in v1), so this is a migration, not a rewrite:

  - The store is the shared `notifications_store.NotificationStore` under
    `services/_shared/`. It owns `notifications.db` + the `agent_questions` table, the dedup unique
    index, TTL/expiry, the state machine, and the client-record shapes. A prompt
    question is one notification row (`producer='agent_question.v1'`) plus a
    linked durable question row - the "Updates card" mobile renders.
  - The handler orchestration + serialization (`_serialize_notification_for_client`,
    `_notification_answer_payload`, the prompt/notification verb dispatch, the
    push gate) are lifted from `chat_streamd.py` and adapted at ONE seam only:
    **I/O placement** (`v1_code_reuse_map.md` cross-cutting rule). The synchronous
    store runs behind a single worker thread (never on the loop), and the
    answer-back that v1 pushed through its peer-queue subsystem is delivered
    through v2's one injection path (`comms.tell`) instead.

Wire parity is therefore free and verified against the consumers: mobile renders
the `notification` push (`producer`/`severity`/`title`/`body`/`actions[kind,
action_id,label]`/`resolution`) and answers with `notification.resolve`
(`pentacle-mobile` `pentacleStream.ts`); agents ask with `prompt.ask` and await
the answer with `notification.await` (`agent-orch prompt`).

Expiry loop rules (spec constraint / loop rule 2): cadence
(`--notification-expiry-interval-s`), per-pass cap on rows post-processed,
backoff on failure, kill switch `--disable-notification-expiry`.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Awaitable, Callable

SERVICES_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from _shared.notifications_store import (
    InvalidNotification,
    NotificationNotFound,
    NotificationResolutionConflict,
    NotificationResolutionInProgress,
    NotificationStore,
    NotificationStoreError,
    NotificationTerminalState,
    TERMINAL_STATES,
)
from v2_runtime import env_number

log = logging.getLogger("chat_streamd_v2.notify")

DEFAULT_NOTIFICATIONS_DB = str(Path.home() / ".local/share/pentacle-stream/notifications.db")
HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT = 100
#: Summary-mode (mobile) clients get a small hello/reconnect notification page and
#: backfill the rest via `notification.list`. A full 100-row page is ~157 KB and,
#: re-sent on every reconnect, head-of-line-blocks the focused-liveness pong on a
#: slow link past the client's 1 s probe window -> 4000 focused_heartbeat_timeout
#: churn (spec_example_2026_01). Per-subscription:
#: full-mode (desktop) keeps 100.
SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT = 20
DEFAULT_NOTIFICATION_RECOVERY_STALE_AFTER_S = 300.0
#: Default page cap when `notification.list` omits `limit`. v1 (and this lift)
#: applied NO default: a bare `notification.list` returned every row, which on
#: prod is 1.6k rows / >1MiB (the payload-bloat this closes). Both real
#: consumers already send `limit:100` — mobile `useNotifications.ts` and desktop
#: `notifications.js` — so bounding a *bare* call to the same 100 changes only
#: unbounded callers (CLI, harness, stale builds), never mobile/desktop wire
#: behaviour. `states` is deliberately NOT defaulted: mobile omits it on purpose
#: to fetch all states (terminal cards in the Updates feed), so a default state
#: filter WOULD change mobile. An explicit `limit` (any size) still lists fully.
DEFAULT_NOTIFICATION_LIST_LIMIT = 100
ANSWER_BACK_MAX_CHARS = 800
ANSWERED_QUESTION_RECOVERY_LIMIT = 100
ANSWER_TELL_ID_PREFIX = "notification-answer-"
_AGENT_QUESTION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
#: D3 (daemon_updates_2026_09): ack is deleted from accepted modes. A question
#: is a choice (single/multi) or pure free text; every mode admits free text via
#: canonical ``allow_custom=true``.
_AGENT_QUESTION_RESPONSE_MODES = frozenset({"single_choice", "multi_choice", "free_text"})
_CHOICE_RESPONSE_MODES = frozenset({"single_choice", "multi_choice"})

#: Admission format caps (locked by the banked read-only replay boundaries;
#: `_artifacts/question_format_boundaries_20260908.json`). Measured in Unicode
#: code points, enforced here at daemon admission and mirrored in the CLI.
QUESTION_TITLE_MAX = 90
QUESTION_BODY_MAX = 1200
QUESTION_BLOCK_MAX = 300
QUESTION_BLOCK_MAX_LINES = 3
QUESTION_OPTION_MIN = 1
QUESTION_OPTION_MAX = 5
QUESTION_OPTION_LABEL_MAX = 40
QUESTION_OPTION_DESC_MAX = 100


def _nullable_text(value: object) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    return text


class QuestionFormatError(ValueError):
    """Admission-format rejection carrying the field, rule, limit/actual and a
    correction hint so the caller can fix the exact violation."""

    def __init__(self, *, field: str, rule: str, message: str,
                 limit: object = None, actual: object = None) -> None:
        parts = [f"{field}: {message} (rule={rule}"]
        if limit is not None:
            parts.append(f", limit={limit}")
        if actual is not None:
            parts.append(f", actual={actual}")
        parts.append(")")
        super().__init__("".join(parts))
        self.field = field
        self.rule = rule


_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def _normalize_and_validate_question_body(raw: object) -> str:
    """Normalize + enforce the body contract (D3 admission format).

    CRLF -> LF, reject a bare CR, trim outer whitespace; nonempty; <=1200 code
    points total; blocks (separated by >=1 blank line) each <=300 code points and
    <=3 raw LF-separated lines; no code fences or markdown tables.
    """
    if not isinstance(raw, str):
        raise QuestionFormatError(field="body", rule="body_type",
                                  message="body must be a string")
    if "\r\n" in raw:
        raw = raw.replace("\r\n", "\n")
    if "\r" in raw:
        raise QuestionFormatError(field="body", rule="body_bare_cr",
                                  message="bare carriage returns are not allowed; use LF newlines")
    body = raw.strip()
    if not body:
        raise QuestionFormatError(field="body", rule="body_required",
                                  message="body is required")
    total = len(body)
    if total > QUESTION_BODY_MAX:
        raise QuestionFormatError(field="body", rule="body_total_cap",
                                  message="body is too long; shorten it",
                                  limit=QUESTION_BODY_MAX, actual=total)
    if "```" in body:
        raise QuestionFormatError(field="body", rule="body_no_fences",
                                  message="code fences are not allowed in body")
    lines = body.split("\n")
    for prev, cur in zip(lines, lines[1:]):
        if "|" in prev and _TABLE_SEP_RE.match(cur):
            raise QuestionFormatError(field="body", rule="body_no_tables",
                                      message="markdown tables are not allowed in body")
    # Blocks are separated by one or more blank (whitespace-only) lines.
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    for block in blocks:
        if len(block) > QUESTION_BLOCK_MAX_LINES:
            raise QuestionFormatError(
                field="body", rule="body_block_lines",
                message="a block has too many lines; split with a blank line",
                limit=QUESTION_BLOCK_MAX_LINES, actual=len(block))
        block_len = len("\n".join(block))
        if block_len > QUESTION_BLOCK_MAX:
            raise QuestionFormatError(
                field="body", rule="body_block_cap",
                message="a block is too long; shorten or split it",
                limit=QUESTION_BLOCK_MAX, actual=block_len)
    return body


def _actor_provenance(msg: dict[str, Any], *, system: bool = False) -> dict[str, Any]:
    """Derive resolution provenance from the server-injected auth context."""
    context = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
    stream_id = _nullable_text(context.get("stream_id")) or None
    actor_client = _nullable_text(context.get("connection_client")) or None
    claimed_by = _nullable_text(msg.get("by")) or None

    if context.get("token_verified") is True and stream_id:
        actor_class = "verified_agent_relay"
        actor_verified = True
        derived_by = f"agent_relay:{stream_id}"
        actor_stream_id = stream_id
    elif context.get("operator_authenticated") is True:
        actor_class = "direct_operator"
        actor_verified = True
        derived_by = "operator"
        actor_stream_id = None
    elif system:
        actor_class = "system"
        actor_verified = False
        derived_by = "system"
        actor_stream_id = None
    else:
        actor_class = "unverified_direct_client"
        actor_verified = False
        derived_by = f"client:{actor_client or 'unknown'}"
        actor_stream_id = None

    provenance: dict[str, Any] = {
        "actor_class": actor_class,
        "actor_stream_id": actor_stream_id,
        "actor_client": actor_client,
        "actor_verified": actor_verified,
        "by": derived_by,
    }
    if claimed_by is not None:
        provenance["claimed_by"] = claimed_by
    return provenance


def _question_mutation_authorized(
    msg: dict[str, Any],
    question: dict[str, Any],
    *,
    allow_verified_agent_relay: bool = False,
) -> bool:
    """Authorize the operator or the permitted verified question actor."""
    context = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
    if context.get("operator_authenticated") is True:
        return True
    owner = _nullable_text(context.get("stream_id"))
    if allow_verified_agent_relay and context.get("token_verified") is True and owner:
        return True
    producer = _nullable_text(question.get("producer_stream_id"))
    return bool(context.get("token_verified") is True and owner and owner == producer)


class _StoreThread:
    """v1's `NotificationStore` behind a single worker thread. The store is
    synchronous (its own `threading.Lock`, `check_same_thread=False`); routing
    every call through one thread keeps SQLite off the event loop (the sole
    adaptation the reuse map allows) and preserves single-writer ordering."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notif-store")
        self._store: NotificationStore | None = None

    async def start(self) -> None:
        # open() runs PRAGMA integrity_check + WAL — off the loop.
        self._store = await self._run(NotificationStore, self._path)

    async def stop(self) -> None:
        if self._store is not None:
            try:
                await self._run(self._store.close)
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._pool.shutdown(wait=False)

    async def call(self, method: str, /, *args: Any, **kwargs: Any) -> Any:
        store = self._store
        if store is None:
            raise NotificationStoreError("notification store not started")
        return await self._run(functools.partial(getattr(store, method), *args, **kwargs))

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, functools.partial(fn, *args))


class Notify:
    """Questions + notifications, lifted. Injected with v2's `broadcast` (server
    fan-out) and `comms` (the one injection path for answer-back)."""

    def __init__(
        self,
        db_path: str = DEFAULT_NOTIFICATIONS_DB,
        *,
        comms: Any = None,
        broadcast: Any = None,
        recovery_stale_after_s: float | None = None,
        sessions: Any = None,
        notice_store: Any = None,
    ) -> None:
        self._db = _StoreThread(db_path)
        self._comms = comms
        self._broadcast = broadcast
        self._sessions = sessions
        self._notice_store = notice_store
        configured_recovery_age = (
            DEFAULT_NOTIFICATION_RECOVERY_STALE_AFTER_S
            if recovery_stale_after_s is None
            else float(recovery_stale_after_s)
        )
        self._recovery_stale_after_s = max(
            0.0,
            env_number(
                os.environ, "PENTACLE_NOTIFICATION_RECOVERY_STALE_AFTER_S",
                configured_recovery_age, float,
            ),
        )
        #: notification.await futures parked by notification_id (B11 await).
        self._await_waiters: dict[str, set[asyncio.Future]] = {}
        #: Set once the store is open. A tier-0 `prompt.*`/`notification.*` verb
        #: arriving in the sub-second boot window waits on this rather than
        #: erroring `notification store not started` (questions are a cutover
        #: blocker; a brief park is correct, a spurious error is not).
        self._ready = asyncio.Event()

    async def start(self) -> None:
        await self._db.start()
        recovered = await self._db.call(
            "recover_claimed_external_resolutions",
            stale_after_s=self._recovery_stale_after_s,
        )
        if recovered:
            log.info(
                "startup: marked %s stale notification resolution claim(s) indeterminate",
                recovered,
            )
        reconciled = await self._reconcile_answered_question_deliveries()
        if reconciled:
            log.info("startup: consumed %s previously delivered agent-question answer(s)", reconciled)
        self._ready.set()
        try:
            stale = await self.reconcile_open_questions_against_sessions()
            if stale:
                log.info("startup: expired %s open question(s) whose asker is gone", stale)
        except Exception:  # noqa: BLE001 - reconciliation is best-effort at boot
            log.exception("startup: open-question asker reconciliation failed")

    async def _await_ready(self) -> None:
        if not self._ready.is_set():
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=10.0)
            except asyncio.TimeoutError:  # pragma: no cover - store genuinely down
                raise NotificationStoreError("notification store not started")

    async def stop(self) -> None:
        await self._db.stop()

    async def recover_once(self) -> int:
        """Recover only claims older than the configured lease age."""
        await self._await_ready()
        recovered = await self._db.call(
            "recover_claimed_external_resolutions",
            stale_after_s=self._recovery_stale_after_s,
        )
        if recovered:
            log.info("recovered %s stale notification resolution claim(s)", recovered)
        return recovered

    async def _reconcile_answered_question_deliveries(self) -> int:
        """Consume only answers proven delivered by v2's local tell ledger.

        This starts from ``v2_tell_deliveries``, not from the shared questions
        table.  A matching deterministic tell ID is a positive proof that v2
        performed the answer delivery.  Absent/partial/foreign evidence is
        intentionally unselectable, so startup cannot retry or consume any
        ambiguous answered row.
        """
        if self._comms is None:
            return 0
        tell_ids = await self._comms.store.list_delivered_tell_ids(
            prefix=ANSWER_TELL_ID_PREFIX,
            limit=ANSWERED_QUESTION_RECOVERY_LIMIT,
        )
        reconciled = 0
        for tell_id in tell_ids:
            notification_id = str(tell_id).removeprefix(ANSWER_TELL_ID_PREFIX)
            if not notification_id:
                continue
            question = await self._db.call(
                "get_agent_question_for_notification", notification_id
            )
            if question is None or question.get("state") != "answered":
                continue
            consumed = await self._db.call(
                "consume_answered_agent_question_for_notification", notification_id
            )
            if consumed is None or consumed.get("state") != "consumed":
                continue
            reconciled += 1
            record = await self._db.call("get_notification", notification_id)
            if record is not None:
                client_record = await self._serialize(record)
                if self._broadcast is not None:
                    await self._broadcast({"type": "notification", "notification": client_record})
        return reconciled

    def wire_handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[Any]]]:
        prompt = {f"prompt.{v}": self.prompt for v in ("ask", "status", "answer", "cancel", "list")}
        notif = {f"notification.{v}": self.notification
                 for v in ("create", "resolve_by_dedup", "await", "list", "resolve")}
        return {**prompt, **notif}

    # -- hello snapshot ----------------------------------------------------

    async def snapshot_notifications(self, *, summary: bool = False) -> list[dict[str, Any]]:
        # hello is on the hot connect path and can arrive in the sub-second
        # window before `start()` opens the store. An empty list then is
        # correct-enough (the client backfills via `notification.list`); never
        # fail a hello over it.
        if self._db._store is None:
            return []
        limit = SUMMARY_SNAPSHOT_NOTIFICATIONS_LIMIT if summary else HELLO_SNAPSHOT_NOTIFICATIONS_LIMIT
        records = await self._db.call("list_notifications", states=["open"], limit=limit)
        serialized: list[dict[str, Any]] = []
        for record in records:
            # Do not expose an open question whose asker is gone/replaced; the
            # close hook and background sweep expire it durably out of band.
            if self._sessions is not None and record.get("producer") == "agent_question.v1":
                question = await self._agent_question_for_notification(record)
                if (question is not None and question.get("state") == "open"
                        and not self._producer_is_current(question)):
                    continue
            serialized.append(await self._serialize(record))
        return serialized

    # -- client serialization (lifted) ------------------------------------

    async def _agent_question_for_notification(self, record: dict) -> dict | None:
        if record.get("producer") != "agent_question.v1":
            return None
        notification_id = str(record.get("notification_id") or "")
        if not notification_id:
            return None
        return await self._db.call("get_agent_question_for_notification", notification_id)

    @staticmethod
    def _question_client_payload(question: dict) -> dict:
        envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
        options = envelope.get("options") if isinstance(envelope.get("options"), list) else []
        payload = {
            "question_id": question.get("question_id"),
            "producer_stream_id": question.get("producer_stream_id"),
            "response_mode": envelope.get("response_mode"),
            "options": options,
            "state": question.get("state"),
            "answer": question.get("answer"),
        }
        if envelope.get("allow_custom") is True:
            payload["allow_custom"] = True
        return payload

    async def _serialize(self, record: dict | None) -> dict | None:
        if record is None:
            return None
        client_record = dict(record)
        client_record.pop("_resolution_replayed", None)
        if record.get("producer") != "agent_question.v1":
            client_record.pop("question", None)
            return client_record
        question = await self._agent_question_for_notification(record)
        if question is not None:
            client_record["question"] = self._question_client_payload(question)
        return client_record

    # -- answer payload (lifted verbatim) ---------------------------------

    @staticmethod
    def _action_for_resolution(record: dict, resolution: dict) -> dict | None:
        actions = record.get("actions") or []
        action_id = resolution.get("action_id")
        if isinstance(action_id, str) and action_id:
            for action in actions:
                if isinstance(action, dict) and action.get("action_id") == action_id:
                    return action
        action_kind = str(resolution.get("action_kind") or "")
        for action in actions:
            if isinstance(action, dict) and action.get("kind") == action_kind:
                return action
        return None

    def _selection_values(self, record: dict, resolution: dict) -> list[str]:
        selections = resolution.get("selections")
        if isinstance(selections, list) and all(isinstance(i, str) for i in selections):
            return selections[:]
        raw_value = resolution.get("value")
        if isinstance(raw_value, dict) and isinstance(raw_value.get("answer"), str):
            return [raw_value["answer"]]
        action = self._action_for_resolution(record, resolution)
        if isinstance(action, dict):
            action_value = action.get("value")
            if isinstance(action_value, dict) and isinstance(action_value.get("answer"), str):
                return [action_value["answer"]]
            if isinstance(action_value, str):
                return [action_value]
        if isinstance(raw_value, str):
            return [raw_value]
        return []

    def _answer_payload(self, record: dict) -> dict | None:
        if record.get("state") not in TERMINAL_STATES:
            return None
        resolution = record.get("resolution")
        if not isinstance(resolution, dict):
            return None
        action_kind = str(resolution.get("action_kind") or "")
        if not action_kind:
            return None
        if (record.get("producer") == "agent_question.v1" and action_kind == "resolved"
                and not isinstance(resolution.get("text"), str)
                and not isinstance(resolution.get("custom_text"), str)
                and not resolution.get("selections")):
            return None
        action = self._action_for_resolution(record, resolution)
        action_id = str(resolution.get("action_id")
                        or ((action or {}).get("action_id") if isinstance(action, dict) else "") or "")
        label = str(resolution.get("label")
                    or ((action or {}).get("label") if isinstance(action, dict) else "") or action_kind)
        payload: dict[str, Any] = {
            "notification_id": record.get("notification_id"),
            "action_id": action_id,
            "action_kind": action_kind,
            "label": label,
            "by": resolution.get("by") or resolution.get("claimed_by") or "system",
            "at": resolution.get("at") or resolution.get("claimed_at") or record.get("resolved_at"),
            "selections": self._selection_values(record, resolution),
            "note": resolution["note"] if isinstance(resolution.get("note"), str) else None,
        }
        if isinstance(resolution.get("text"), str):
            payload["text"] = resolution["text"]
        if isinstance(resolution.get("custom_text"), str):
            payload["custom_text"] = resolution["custom_text"]
        if action_kind == "yes_no" and isinstance(resolution.get("choice"), bool):
            payload["choice"] = bool(resolution["choice"])
        if "value" in resolution:
            payload["value"] = resolution["value"]
        elif isinstance(action, dict) and "value" in action:
            payload["value"] = action["value"]
        for key in ("actor_class", "actor_stream_id", "actor_client", "actor_verified", "claimed_by"):
            if key in resolution:
                payload[key] = resolution[key]
        return payload

    # -- post-resolution fan-out ------------------------------------------

    async def _after_resolution(
        self, record: dict, *, v2_delivery_transaction: bool = False
    ) -> dict | None:
        """Persist the answer onto the question row, wake await waiters, deliver
        the answer back to the asking agent, and broadcast the updated card.

        ``v2_delivery_transaction`` is set only by v2 resolution handlers.
        Its successful tell is therefore a v2-owned delivery act, the only live
        path allowed to consume a shared question row.  A caller observing a
        foreign already-answered row still serializes/broadcasts it but cannot
        initiate an answer tell or mutate that row.
        """
        answer = self._answer_payload(record)
        notification_id = str(record.get("notification_id") or "")
        if answer is not None and notification_id:
            await self._db.call("answer_agent_question_for_notification", notification_id, answer)
            self._wake_await_waiters(notification_id, answer)
            if v2_delivery_transaction and await self._deliver_answer_back(record, answer):
                await self._db.call(
                    "consume_answered_agent_question_for_notification", notification_id
                )
        client_record = await self._serialize(record)
        if self._broadcast is not None:
            await self._broadcast({"type": "notification", "notification": client_record})
        return client_record

    def _wake_await_waiters(self, notification_id: str, answer: dict) -> None:
        for future in list(self._await_waiters.get(notification_id, set())):
            if not future.done():
                future.set_result(answer)

    async def _deliver_answer_back(self, record: dict, answer: dict) -> bool:
        """v1 delivered the answer to the asking agent through its peer-queue
        subsystem; v2 has one injection path, so the answer lands in the agent's
        pane as a readable tell (comms supplies handoff-lineage routing +
        idempotency). Best-effort: a closed/remote asker is never an error."""
        answer_to = _nullable_text(record.get("answer_to_stream_id"))
        if not answer_to or self._comms is None:
            return False
        try:
            result = await self._comms.tell({
                "tell_id": f"{ANSWER_TELL_ID_PREFIX}{answer['notification_id']}",
                "stream_id": answer_to,
                "message": _answer_back_text(answer),
            })
            return isinstance(result, dict) and result.get("delivery_status") == "delivered"
        except Exception as exc:  # noqa: BLE001 - answer already durable + awaited
            log.info("answer-back tell failed nid=%s: %s", answer.get("notification_id"), exc)
            return False

    async def create_internal_notification(
        self,
        *,
        producer: str,
        title: str,
        body: str,
        severity: str = "warning",
        dedup_key: str | None = None,
    ) -> dict:
        """Create and surface a daemon-owned notification without a wire round trip."""
        await self._await_ready()
        record = await self._db.call(
            "create_notification",
            producer=producer,
            title=title,
            body=body,
            severity=severity,
            dedup_key=dedup_key,
            actions=None,
            ttl_seconds=None,
            answer_to_stream_id=None,
        )
        client_record = await self._serialize(record)
        if self._broadcast is not None:
            await self._broadcast({"type": "notification", "notification": client_record})
        return client_record or {}

    # -- prompt.* dispatch (lifted) ---------------------------------------

    async def prompt(self, msg: dict[str, Any]) -> dict[str, Any]:
        request_id = str(msg.get("request_id") or "")
        verb = str(msg.get("type") or "")
        try:
            await self._await_ready()
            if verb == "prompt.ask":
                return await self._prompt_ask(msg, request_id)
            if verb == "prompt.status":
                qid = str(msg.get("question_id") or "")
                question = await self._db.call("get_agent_question", qid)
                if question is None:
                    return self._prompt_error(request_id, "question_not_found", question_id=qid)
                question = await self._expire_if_stale(question)
                return {"type": "prompt.status.ok", "request_id": request_id, "ok": True, "question": question}
            if verb == "prompt.answer":
                return await self._prompt_answer(msg, request_id)
            if verb == "prompt.cancel":
                return await self._prompt_cancel(msg, request_id)
            if verb == "prompt.list":
                return await self._prompt_list(msg, request_id)
            return self._prompt_error(request_id, "prompt_unknown_command", command=verb)
        except QuestionFormatError as exc:
            error_code = "ack_removed" if exc.rule == "ack_removed" else "prompt_invalid"
            return self._prompt_error(request_id, error_code, message=str(exc),
                                      field=exc.field, rule=exc.rule)
        except InvalidNotification as exc:
            return self._prompt_error(request_id, "prompt_invalid", message=str(exc))
        except NotificationTerminalState as exc:
            return self._prompt_error(request_id, "question_terminal_state",
                                      question_id=str(msg.get("question_id") or ""),
                                      state=str(exc.args[0] if exc.args else ""))
        except NotificationNotFound:
            return self._prompt_error(request_id, "question_not_found",
                                      question_id=str(msg.get("question_id") or ""))
        except ValueError as exc:
            return self._prompt_error(request_id, "prompt_invalid", message=str(exc))
        except NotificationStoreError as exc:
            return self._prompt_error(request_id, "notification_store_error", message=str(exc))

    async def _prompt_ask(self, msg: dict, request_id: str) -> dict:
        envelope = msg.get("envelope")
        if not isinstance(envelope, dict):
            return self._prompt_error(request_id, "prompt_invalid", message="envelope must be an object")
        envelope, actions = self._validate_prompt_envelope_and_actions(envelope, msg.get("actions"))
        producer = str(envelope.get("producer_stream_id") or "").strip()
        question_id = str(envelope.get("question_id") or "")
        if not producer:
            return self._prompt_error(request_id, "producer_unverified", question_id=question_id,
                                      message="producer_stream_id is required")
        # Verified producer identity: the ask must carry a verified stream token
        # whose owner is the producer. This rejects a forged payload identity and
        # an unverified/service-only producer rather than storing a NULL producer.
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if not (
            auth.get("token_verified") is True
            and str(auth.get("stream_id") or "") == producer
            and str(msg.get("from_stream_id") or "") == producer
        ):
            return self._prompt_error(request_id, "stream_ownership_unverified",
                                      question_id=question_id,
                                      message="prompt.ask requires a verified producer stream token")
        # Eligibility: only an OPEN, operator-visible seat may ask the operator.
        # Hidden/subagent seats are told to ask their parent (by a normal tell).
        # Rechecked here under the current session inventory before creation.
        generation: str | None = None
        if self._sessions is not None:
            row = self._sessions.get(producer)
            if not isinstance(row, dict) and self._notice_store is not None:
                host, name = self._sessions.split(producer)
                row = await self._notice_store.fetch_session(host, name)
            if not isinstance(row, dict) or str(row.get("status") or "open") != "open":
                return self._prompt_error(request_id, "producer_not_open",
                                          question_id=question_id,
                                          message="producer session is not open")
            generation = _nullable_text(row.get("session_generation")) or None
            # Whitelist the operator-visible classes; every other visibility
            # (hidden, subagent, nested, or any future/unknown value) must ask
            # its parent rather than the operator.
            visibility = str(row.get("visibility") or "")
            if visibility not in ("default", "visible"):
                parent = _nullable_text(row.get("parent_stream_id")) or None
                message = (
                    "only an operator-visible seat may ask; ask your parent by a normal tell"
                    if parent else
                    "only an operator-visible seat may ask, and no parent is registered"
                )
                return self._prompt_error(request_id, "ask_parent", question_id=question_id,
                                          parent_stream_id=parent, message=message)
        if generation is not None:
            envelope = {**envelope, "producer_session_generation": generation}

        existing = await self._db.call("get_agent_question", question_id)
        if isinstance(existing, dict):
            existing_envelope = (
                existing.get("envelope")
                if isinstance(existing.get("envelope"), dict) else {}
            )
            existing_generation = (
                _nullable_text(existing_envelope.get("producer_session_generation")) or None
            )
            if (generation is not None and existing_generation is not None
                    and existing_generation != generation):
                return self._prompt_error(
                    request_id, "prompt_question_generation_conflict",
                    question_id=question_id,
                    message="question_id belongs to a prior producer generation; use a fresh id")
            existing_notification = await self._db.call(
                "get_notification", str(existing.get("notification_id") or "")
            )
            existing_actions = (
                existing_notification.get("actions")
                if isinstance(existing_notification, dict)
                and isinstance(existing_notification.get("actions"), list)
                else []
            )
            if self._prompt_notice_material(
                existing_envelope, existing_actions
            ) != self._prompt_notice_material(envelope, actions):
                return self._prompt_error(request_id, "prompt_question_replay_conflict",
                                          question_id=question_id)
            client_notification = await self._serialize(existing_notification)
            return {
                "type": "prompt.ask.ok", "request_id": request_id, "ok": True,
                "question": existing, "notification": client_notification,
            }

        question = await self._db.call(
            "create_agent_question", envelope=envelope, actions=actions,
            severity=str(msg.get("severity") or "info"),
        )
        notification = await self._db.call("get_notification", str(question.get("notification_id") or ""))
        client_notification = await self._serialize(notification)
        if notification is not None and question.get("state") == "open":
            if self._broadcast is not None:
                await self._broadcast({"type": "notification", "notification": client_notification})
        return {
            "type": "prompt.ask.ok", "request_id": request_id, "ok": True,
            "question": question, "notification": client_notification,
        }

    @staticmethod
    def _prompt_notice_material(envelope: dict, actions: object) -> dict[str, Any]:
        return {
            "envelope": {
                key: envelope.get(key)
                for key in (
                    "schema_version", "question_id", "title", "body", "context",
                    "dedup_key", "producer_stream_id", "producer_provider", "spec_id",
                    "response_mode", "options", "allow_custom", "ttl_seconds",
                    "default_action",
                )
            },
            "actions": list(actions) if isinstance(actions, list) else [],
        }

    async def _prompt_answer(self, msg: dict, request_id: str) -> dict:
        qid = str(msg.get("question_id") or "")
        # Canonical answer payload (D3): {question_id, selections?: [str], text?: str}.
        # Legacy aliases are rejected rather than guessed at.
        for legacy in ("custom_text", "value", "note"):
            if msg.get(legacy) is not None:
                return self._prompt_error(request_id, "prompt_invalid", question_id=qid,
                                          message=f"{legacy} is not a valid answer field; "
                                                  "send selections and/or text")
        if msg.get("text") is not None and not isinstance(msg.get("text"), str):
            return self._prompt_error(request_id, "prompt_invalid", question_id=qid,
                                      message="text must be a string")
        if msg.get("selections") is not None and not isinstance(msg.get("selections"), list):
            return self._prompt_error(request_id, "prompt_invalid", question_id=qid,
                                      message="selections must be a list")
        question = await self._db.call("get_agent_question", qid)
        if question is None:
            return self._prompt_error(request_id, "question_not_found", question_id=qid)
        if not _question_mutation_authorized(msg, question, allow_verified_agent_relay=True):
            return self._prompt_error(request_id, "question_unauthorized", question_id=qid)
        # Recheck producer liveness/generation before answering so recovery cannot
        # expose an actionable stale question and an answer cannot be delivered to
        # a replacement generation that reused the producer stream id.
        if question.get("state") == "open" and not self._producer_is_current(question):
            expired = await self._expire_stale_question(question)
            return self._prompt_error(request_id, "question_producer_gone", question_id=qid,
                                      question=expired,
                                      message="the asking session is gone; the question expired")
        envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        selections = list(msg["selections"]) if isinstance(msg.get("selections"), list) else None
        provenance = _actor_provenance(msg)
        if str(envelope.get("response_mode") or "") != "free_text":
            # A choice accepts selected values, nonblank text alone, or both.
            # The free-text portion maps to the store's existing custom_text path.
            choice_text = text if (text is not None and text.strip()) else None
            question, client, already = await self._answer_choice(
                question, value=None, selections=selections,
                custom_text=choice_text, note=None, actor_provenance=provenance)
        else:
            if selections:
                return self._prompt_error(request_id, "prompt_invalid", question_id=qid,
                                          message="free_text questions take text only, not selections")
            if not isinstance(text, str) or not text.strip():
                return self._prompt_error(request_id, "prompt_invalid", question_id=qid,
                                          message="text must be a non-empty string")
            question, client, already = await self._answer_free_text(
                question, text=text, note=None, actor_provenance=provenance
            )
        response = {"type": "prompt.answer.ok", "request_id": request_id, "ok": True, "question": question}
        if already:
            response["already_answered"] = True
        else:
            response["notification"] = client
        return response

    async def _prompt_cancel(self, msg: dict, request_id: str) -> dict:
        qid = str(msg.get("question_id") or "")
        if msg.get("note") is not None and not isinstance(msg.get("note"), str):
            return self._prompt_error(request_id, "prompt_invalid", question_id=qid,
                                      message="note must be a string")
        question = await self._db.call("get_agent_question", qid)
        if question is None:
            return self._prompt_error(request_id, "question_not_found", question_id=qid)
        if not _question_mutation_authorized(msg, question):
            return self._prompt_error(request_id, "question_unauthorized", question_id=qid)
        state = str(question.get("state") or "")
        if state != "open":
            if state == "dismissed":
                return {"type": "prompt.cancel.ok", "request_id": request_id, "ok": True,
                        "question": question, "already_cancelled": True}
            return self._prompt_error(request_id, "question_terminal_state", question_id=qid, state=state)
        provenance = _actor_provenance(msg)
        question, client, already = await self._cancel_question(
            question, note=msg.get("note") if isinstance(msg.get("note"), str) else None,
            actor_provenance=provenance)
        response = {"type": "prompt.cancel.ok", "request_id": request_id, "ok": True, "question": question}
        if already:
            response["already_cancelled"] = True
        else:
            response["notification"] = client
        return response

    async def _prompt_list(self, msg: dict, request_id: str) -> dict:
        raw_open = msg.get("open")
        if raw_open is not None and not isinstance(raw_open, bool):
            return self._prompt_error(request_id, "prompt_invalid", message="open must be a boolean")
        raw_limit = msg.get("limit")
        if raw_limit is not None and (isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 0):
            return self._prompt_error(request_id, "prompt_invalid", message="limit must be a nonnegative integer")
        producer_stream_id = _nullable_text(msg.get("producer_stream_id")) or None
        # v1 parity: `--from <viewer>` is a VISIBILITY scope, not a literal
        # producer match. A lead must see the questions its hidden workers asked,
        # so the viewer expands to itself + the descendants it owns (nearest
        # operator-visible ancestor == viewer). Without a sessions registry
        # (unit tests build Notify with comms=None) fall back to the literal
        # filter — the only behaviour that path could offer.
        scope = await self._question_scope(producer_stream_id) if producer_stream_id else None
        questions = await self._db.call(
            "list_agent_questions",
            producer_stream_id=producer_stream_id if scope is None else None,
            producer_stream_ids=scope,
            spec_id=_nullable_text(msg.get("spec_id")) or None,
            open_only=bool(raw_open),
            limit=raw_limit if isinstance(raw_limit, int) else None,
        )
        questions = await self._sweep_stale_open_questions(questions)
        response = {"type": "prompt.list.ok", "request_id": request_id, "ok": True, "questions": questions}
        if scope is not None:
            response["producer_stream_ids"] = scope
            response["surfaced_to_stream_id"] = producer_stream_id
        return response

    async def _question_scope(self, viewer_stream_id: str) -> list[str] | None:
        sessions = getattr(self._comms, "sessions", None) if self._comms is not None else None
        resolver = getattr(sessions, "visible_question_scope_stream_ids", None)
        if resolver is None:
            return None
        return await resolver(viewer_stream_id)

    # -- prompt answer/cancel helpers (lifted) ----------------------------

    async def _answer_free_text(self, question, *, text, note, actor_provenance):
        qid = str(question.get("question_id") or "")
        nid = str(question.get("notification_id") or "")
        try:
            record = await self._db.call("resolve_notification", nid, action_kind="resolved",
                                         by=actor_provenance["by"], actor_provenance=actor_provenance,
                                         selections=[], text=text, note=note)
        except NotificationTerminalState:
            return await self._db.call("get_agent_question", qid), None, True
        replayed = bool(record.get("_resolution_replayed"))
        client = await self._after_resolution(record, v2_delivery_transaction=not replayed)
        return await self._db.call("get_agent_question", qid), client, replayed

    async def _answer_choice(self, question, *, value, selections, custom_text, note, actor_provenance):
        if value is not None and selections is not None:
            raise InvalidNotification("value cannot be combined with selections")
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise InvalidNotification("value must be a non-empty string")
            selections = [value]
        if selections is None and custom_text is None:
            raise InvalidNotification("selections are required")
        if selections is not None and not selections:
            raise InvalidNotification("selections must contain at least one value")
        if selections is not None and any(not isinstance(i, str) or not i for i in selections):
            raise InvalidNotification("selections must contain non-empty strings")
        if selections is not None and len(set(selections)) != len(selections):
            raise InvalidNotification("selections must be unique")
        envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
        response_mode = str(envelope.get("response_mode") or "")
        if response_mode not in _CHOICE_RESPONSE_MODES:
            raise InvalidNotification("selections are only valid for choice questions")
        if selections is not None and response_mode == "single_choice" and len(selections) != 1:
            raise InvalidNotification("single_choice questions require exactly one selection")
        option_values = {str(o["value"]) for o in envelope.get("options") or []
                         if isinstance(o, dict) and isinstance(o.get("value"), str)}
        if selections is not None and any(i not in option_values for i in selections):
            raise InvalidNotification("selections must be option values for the question")
        qid = str(question.get("question_id") or "")
        nid = str(question.get("notification_id") or "")
        notification = await self._db.call("get_notification", nid)
        if notification is None:
            raise NotificationNotFound(nid)
        action = self._action_for_selection(notification, selections[0]) if selections else None
        if selections and action is None:
            raise InvalidNotification("notification action for first selection not found")
        action_kind = str(action.get("kind") or "") if action is not None else "resolved"
        choice = (bool(action["choice"]) if action is not None and action_kind == "yes_no"
                  and isinstance(action.get("choice"), bool) else None)
        try:
            record = await self._db.call(
                "resolve_notification", nid, action_kind=action_kind, by=actor_provenance["by"],
                actor_provenance=actor_provenance, choice=choice,
                selections=selections, custom_text=custom_text, note=note,
                action_id=str(action.get("action_id") or "") if action and action.get("action_id") else None,
                label=str(action.get("label") or "") if action and action.get("label") else None,
                value=action.get("value") if action is not None and "value" in action else None)
        except NotificationTerminalState:
            return await self._db.call("get_agent_question", qid), None, True
        replayed = bool(record.get("_resolution_replayed"))
        client = await self._after_resolution(record, v2_delivery_transaction=not replayed)
        return await self._db.call("get_agent_question", qid), client, replayed

    @staticmethod
    def _action_for_selection(record: dict, selection: str) -> dict | None:
        for action in record.get("actions") or []:
            if not isinstance(action, dict):
                continue
            value = action.get("value")
            if isinstance(value, dict) and value.get("answer") == selection:
                return action
            if value == selection:
                return action
        return None

    async def _cancel_question(self, question, *, note, actor_provenance):
        qid = str(question.get("question_id") or "")
        nid = str(question.get("notification_id") or "")
        try:
            record = await self._db.call(
                "resolve_notification", nid, action_kind="resolved", by=actor_provenance["by"],
                actor_provenance=actor_provenance, note=note
            )
        except NotificationTerminalState:
            q = await self._db.call("get_agent_question", qid)
            if q is not None and q.get("state") == "dismissed":
                return q, None, True
            raise
        client = await self._after_resolution(record, v2_delivery_transaction=True)
        return await self._db.call("get_agent_question", qid), client, False

    # -- notification.* dispatch (lifted) ---------------------------------

    async def notification(self, msg: dict[str, Any]) -> dict[str, Any]:
        verb = str(msg.get("type") or "")
        reply = await self._dispatch_notification(msg)
        # Cutover diagnostic (forward-moving, no behavior change): a rejected
        # resolve from a real consumer is otherwise invisible. When a
        # `notification.*` verb is refused as invalid or unknown, log ONE bounded
        # WARNING describing the SHAPE of what arrived — the payload's keys and
        # the resolve-relevant fields — so a stale installed mobile build sending
        # the wrong payload (e.g. a resolve missing `choice`) is diagnosable from
        # the daemon log alone. Never logs bodies/titles or the choice VALUE.
        if reply.get("error_code") in ("notification_invalid", "notification_unknown_command"):
            self._log_notification_rejection(verb, reply, msg)
        return reply

    async def _dispatch_notification(self, msg: dict[str, Any]) -> dict[str, Any]:
        request_id = str(msg.get("request_id") or "")
        verb = str(msg.get("type") or "")
        try:
            await self._await_ready()
            if verb == "notification.create":
                return await self._notif_create(msg, request_id)
            if verb == "notification.resolve_by_dedup":
                return await self._notif_resolve_by_dedup(msg, request_id)
            if verb == "notification.await":
                return await self._notif_await(msg, request_id)
            if verb == "notification.list":
                return await self._notif_list(msg, request_id)
            if verb == "notification.resolve":
                return await self._notif_resolve(msg, request_id)
            return self._notif_error(request_id, "notification_unknown_command", command=verb)
        except NotificationResolutionConflict:
            return self._notif_error(request_id, "notification_resolution_conflict",
                                     notification_id=str(msg.get("notification_id") or ""))
        except NotificationResolutionInProgress:
            return self._notif_error(request_id, "notification_resolution_in_progress",
                                     notification_id=str(msg.get("notification_id") or ""))
        except InvalidNotification as exc:
            return self._notif_error(request_id, "notification_invalid", message=str(exc))
        except NotificationNotFound:
            return self._notif_error(request_id, "notification_not_found",
                                     notification_id=str(msg.get("notification_id") or ""))
        except NotificationTerminalState as exc:
            return self._notif_error(request_id, "notification_terminal_state",
                                     notification_id=str(msg.get("notification_id") or ""),
                                     state=str(exc.args[0] if exc.args else ""))
        except ValueError as exc:
            return self._notif_error(request_id, "notification_invalid", message=str(exc))
        except NotificationStoreError as exc:
            return self._notif_error(request_id, "notification_store_error", message=str(exc))

    async def _notif_create(self, msg: dict, request_id: str) -> dict:
        # v2 removes investigation routing (spec constraint 3): a warning/critical
        # notification is stored + surfaced, never diverted to a spawner.
        record = await self._db.call(
            "create_notification",
            producer=str(msg.get("producer") or ""),
            title=str(msg.get("title") or ""),
            body=_nullable_text(msg.get("body")) or None,
            severity=str(msg.get("severity") or "info"),
            dedup_key=_nullable_text(msg.get("dedup_key")) or None,
            actions=msg.get("actions") if isinstance(msg.get("actions"), list) else None,
            ttl_seconds=msg.get("ttl_seconds"),
            answer_to_stream_id=_nullable_text(msg.get("answer_to_stream_id")) or None,
        )
        client_record = await self._serialize(record)
        if self._broadcast is not None:
            await self._broadcast({"type": "notification", "notification": client_record})
        return {"type": "notification.create.ok", "request_id": request_id, "notification": client_record}

    async def _notif_resolve_by_dedup(self, msg: dict, request_id: str) -> dict:
        producer = str(msg.get("producer") or "")
        dedup_key = _nullable_text(msg.get("dedup_key")) or ""
        existing = await self._db.call(
            "get_latest_by_dedup", producer=producer, dedup_key=dedup_key
        )
        if existing is None:
            return {"type": "notification.resolve_by_dedup.ok", "request_id": request_id,
                    "resolved": False, "notification": None}
        question = await self._db.call(
            "get_agent_question_for_notification", existing["notification_id"]
        )
        if question is not None and not _question_mutation_authorized(msg, question):
            return self._notif_error(
                request_id, "question_unauthorized",
                notification_id=str(existing["notification_id"]),
            )
        provenance = _actor_provenance(msg, system=True)
        record = await self._db.call(
            "resolve_open_dedup", producer=producer, dedup_key=dedup_key,
            by=provenance["by"], actor_provenance=provenance,
            expected_notification_id=str(existing["notification_id"]))
        client_record = await self._serialize(record) if record else None
        if client_record is not None:
            answer = self._answer_payload(record)
            if answer is not None:
                self._wake_await_waiters(str(record.get("notification_id") or ""), answer)
            if self._broadcast is not None:
                await self._broadcast({"type": "notification", "notification": client_record})
        return {"type": "notification.resolve_by_dedup.ok", "request_id": request_id,
                "resolved": client_record is not None, "notification": client_record}

    async def _notif_list(self, msg: dict, request_id: str) -> dict:
        if 'notification_ids' in msg:
            ids = msg['notification_ids']
            if (not isinstance(ids, list) or len(ids) > DEFAULT_NOTIFICATION_LIST_LIMIT
                    or any(not isinstance(nid, str) or not nid.strip() for nid in ids)):
                return self._notif_error(request_id, 'notification_invalid',
                                         message='notification_ids must be a bounded list of nonempty strings')
            records = []
            for nid in dict.fromkeys(ids):
                record = await self._db.call('get_notification', nid)
                if record is not None:
                    records.append(await self._serialize(record))
            return {'type': 'notification.list.ok', 'request_id': request_id, 'notifications': records}
        states = msg.get("states") if isinstance(msg.get("states"), list) else None
        raw_limit = msg.get("limit")
        # Omitted (or non-int) limit -> the default page cap, not unbounded, so a
        # bare `notification.list` cannot pull the full 1.6k-row / >1MiB backlog.
        # An explicit int (incl. a large one for a full listing, or 0 for empty)
        # is honoured verbatim; a negative int falls through to the store's
        # ValueError -> notification_invalid, as before.
        limit = (int(raw_limit) if isinstance(raw_limit, int) and not isinstance(raw_limit, bool)
                 else DEFAULT_NOTIFICATION_LIST_LIMIT)
        records = [await self._serialize(r)
                   for r in await self._db.call("list_notifications", states=states, limit=limit)]
        return {"type": "notification.list.ok", "request_id": request_id, "notifications": records}

    async def _notif_resolve(self, msg: dict, request_id: str) -> dict:
        nid = str(msg.get("notification_id") or "")
        question = await self._db.call("get_agent_question_for_notification", nid)
        # This is the mobile/desktop question-answer route. prompt.cancel and
        # notification.resolve_by_dedup retain the producer/operator-only rule.
        if question is not None and not _question_mutation_authorized(
            msg, question, allow_verified_agent_relay=True
        ):
            return self._notif_error(
                request_id, "question_unauthorized", notification_id=nid
            )
        # Recheck producer liveness/generation before answering an agent_question
        # through this route too, so a stale asker's card cannot be answered and
        # an answer cannot be delivered to a replacement generation.
        if (question is not None and question.get("state") == "open"
                and not self._producer_is_current(question)):
            await self._expire_stale_question(question)
            return self._notif_error(request_id, "question_producer_gone",
                                     notification_id=nid,
                                     message="the asking session is gone; the question expired")
        action_kind = str(msg.get("action_kind") or "")
        # v2 supports the question-answering resolution kinds mobile sends (ack /
        # yes_no) plus generic `resolved`. spawn_worker/run_command resolutions
        # drive spawning + host command execution - REDESIGN-out subsystems (spec
        # constraint 3); they answer structurally rather than acting.
        if action_kind in {"spawn_worker", "run_command"}:
            return self._notif_error(request_id, "notification_invalid",
                                     notification_id=nid,
                                     message=f"{action_kind} resolutions are not supported in v2")
        raw_action_id = msg.get("action_id")
        action_id = None
        if raw_action_id is not None:
            if not isinstance(raw_action_id, str) or not raw_action_id.strip():
                return self._notif_error(request_id, "notification_invalid", notification_id=nid,
                                         message="action_id must be a non-empty string")
            action_id = raw_action_id.strip()
        choice = msg.get("choice") if isinstance(msg.get("choice"), bool) else None
        selections = msg.get("selections") if isinstance(msg.get("selections"), list) else None
        text = msg.get("text") if isinstance(msg.get("text"), str) else None
        custom_text = msg.get("custom_text") if isinstance(msg.get("custom_text"), str) else None
        note = msg.get("note") if isinstance(msg.get("note"), str) else None

        # The desktop and mobile question cards submit the selected option
        # value(s), while the stored ``yes_no`` action owns the corresponding
        # boolean choice. V1 resolves that action before calling the store; v2
        # previously forwarded the selection with ``choice=None``, so the
        # store rejected every normal single-choice card as
        # ``notification_invalid``. Keep the client payload contract stable and
        # derive the complete resolution from the durable action here.
        if text is None and selections is not None:
            if not selections:
                raise InvalidNotification("selections must contain at least one value")
            notification = await self._db.call("get_notification", nid)
            if notification is None:
                raise NotificationNotFound(nid)
            action = self._action_for_selection(notification, selections[0])
            if action is None:
                raise InvalidNotification("notification action for first selection not found")
            stored_action_id = _nullable_text(action.get("action_id"))
            if action_id is not None and action_id != stored_action_id:
                raise InvalidNotification("action_id must match the first selection")
            action_id = action_id or stored_action_id or None
            action_kind = str(action.get("kind") or action_kind or "")
            if (
                action_kind == "yes_no"
                and choice is None
                and isinstance(action.get("choice"), bool)
            ):
                choice = bool(action["choice"])
        elif custom_text is not None:
            # A custom-only choice answer has no option from which to select an
            # action. V1 records it as a generic resolved answer, which still
            # updates the linked durable question row.
            if action_id is not None:
                raise InvalidNotification("action_id requires a selection")
            action_kind = "resolved"
        elif action_id is not None:
            notification = await self._db.call("get_notification", nid)
            if notification is None:
                raise NotificationNotFound(nid)
            if not any(
                isinstance(action, dict) and action.get("action_id") == action_id
                for action in notification.get("actions") or []
            ):
                raise InvalidNotification("notification action_id not found")

        provenance = _actor_provenance(msg)
        record = await self._db.call(
            "resolve_notification", nid, action_kind=action_kind or "resolved",
            by=provenance["by"], actor_provenance=provenance, choice=choice, selections=selections,
            note=note, text=text, custom_text=custom_text, action_id=action_id)
        replayed = bool(record.get("_resolution_replayed"))
        client_record = await self._after_resolution(
            record, v2_delivery_transaction=not replayed
        )
        response = {"type": "notification.resolve.ok", "request_id": request_id,
                    "notification": client_record, "replayed": replayed}
        return response

    async def _notif_await(self, msg: dict, request_id: str) -> dict:
        nid = str(msg.get("notification_id") or "")
        if not nid:
            return self._notif_error(request_id, "notification_invalid", message="notification_id is required")
        try:
            timeout = float(msg.get("timeout", 30.0))
        except (TypeError, ValueError):
            return self._notif_error(request_id, "notification_invalid", notification_id=nid,
                                     message="timeout must be a non-negative number")
        if timeout < 0:
            return self._notif_error(request_id, "notification_invalid", notification_id=nid,
                                     message="timeout must be a non-negative number")
        # Already terminal-with-answer? answer immediately from the durable row.
        record = await self._db.call("get_notification", nid)
        if record is None:
            return self._notif_error(request_id, "notification_not_found", notification_id=nid)
        answer = self._answer_payload(record)
        if answer is not None:
            return {"type": "notification.await.ok", "request_id": request_id, "ok": True,
                    "notification_id": nid, "answer": answer}
        # Park until resolved or timeout. One task per request (server model), so
        # awaiting here parks only this request, never the socket.
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._await_waiters.setdefault(nid, set()).add(fut)
        try:
            answer = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"type": "notification.await.timeout", "request_id": request_id, "ok": False,
                    "notification_id": nid, "error": "await_timeout", "reason": "await_timeout",
                    "message": f"notification.await timed out after {timeout:.2f}s"}
        finally:
            waiters = self._await_waiters.get(nid)
            if waiters is not None:
                waiters.discard(fut)
                if not waiters:
                    self._await_waiters.pop(nid, None)
        return {"type": "notification.await.ok", "request_id": request_id, "ok": True,
                "notification_id": nid, "answer": answer}

    # -- expiry loop (loop rules) -----------------------------------------

    async def expire_once(self, *, cap: int = 200) -> int:
        """One expiry pass: expire due rows, then post-process up to `cap` of
        them (broadcast the expired card so UIs drop it). Returns the count."""
        await self.recover_once()
        expired_ids = await self._db.call("expire_due")
        for nid in list(expired_ids)[:cap]:
            record = await self._db.call("get_notification", str(nid))
            if record is None:
                continue
            client_record = await self._serialize(record)
            if self._broadcast is not None:
                await self._broadcast({"type": "notification", "notification": client_record})
        return len(expired_ids)

    # -- asker lifetime (D3) ----------------------------------------------

    async def _broadcast_notification_by_id(self, nid: str) -> None:
        record = await self._db.call("get_notification", str(nid))
        if record is not None and self._broadcast is not None:
            await self._broadcast({
                "type": "notification", "notification": await self._serialize(record)
            })

    def _stored_generation(self, question: dict) -> str | None:
        gen = _nullable_text(question.get("producer_session_generation"))
        if gen:
            return gen
        envelope = question.get("envelope") if isinstance(question.get("envelope"), dict) else {}
        return _nullable_text(envelope.get("producer_session_generation")) or None

    def _producer_is_current(self, question: dict) -> bool:
        """Whether the question's asker is still open at its stored generation.

        With no sessions registry (unit tests) the answer is 'current' — the
        daemon always has a registry. A missing/NULL producer is left to the
        bounce-A clear, not treated as stale here.
        """
        if self._sessions is None:
            return True
        producer = _nullable_text(question.get("producer_stream_id"))
        if not producer:
            return True
        row = self._sessions.get(producer)
        if not isinstance(row, dict) or str(row.get("status") or "open") != "open":
            return False
        stored = self._stored_generation(question)
        if not stored:
            return True
        return _nullable_text(row.get("session_generation")) == stored

    async def _expire_stale_question(self, question: dict) -> dict | None:
        producer = _nullable_text(question.get("producer_stream_id"))
        if producer:
            superseding = None
            if self._sessions is not None:
                row = self._sessions.get(producer)
                if isinstance(row, dict) and str(row.get("status") or "open") == "open":
                    superseding = _nullable_text(row.get("session_generation")) or None
            nids = await self._db.call(
                "expire_open_questions_for_producer", producer,
                superseding_generation=superseding, reason="asker_gone",
            )
            for nid in nids:
                await self._broadcast_notification_by_id(nid)
        return await self._db.call("get_agent_question", str(question.get("question_id") or ""))

    async def expire_questions_for_closed_producer(
        self, producer_stream_id: str, *, generation: str | None = None,
    ) -> list[str]:
        """Expire every open question a now-closed producer asked, terminalizing
        BOTH the notification and agent_question through the shared path. Wired to
        the session close/replacement hook so a departing asker cannot leave an
        actionable question behind. Broadcasts the terminal cards."""
        await self._await_ready()
        # Scope to the closed generation (+ generationless rows) so a handoff
        # successor already open at a newer generation keeps its own questions.
        nids = await self._db.call(
            "expire_open_questions_for_producer", producer_stream_id,
            only_generation=_nullable_text(generation) or None, reason="asker_closed",
        )
        for nid in nids:
            await self._broadcast_notification_by_id(nid)
        return nids

    async def _expire_if_stale(self, question: dict | None) -> dict | None:
        """If an open question's asker is gone/replaced, expire the pair and
        return the refreshed terminal row; otherwise return it unchanged. Used on
        the read path so status/list never serve an actionable stale question."""
        if (question is None or question.get("state") != "open"
                or self._producer_is_current(question)):
            return question
        return await self._expire_stale_question(question)

    async def _sweep_stale_open_questions(self, questions: list[dict]) -> list[dict]:
        if self._sessions is None or not questions:
            return questions
        stale_producers: set[str] = set()
        for question in questions:
            if question.get("state") == "open" and not self._producer_is_current(question):
                producer = _nullable_text(question.get("producer_stream_id"))
                if producer and producer not in stale_producers:
                    stale_producers.add(producer)
                    await self._expire_stale_question(question)
        if not stale_producers:
            return questions
        refreshed: list[dict] = []
        for question in questions:
            if _nullable_text(question.get("producer_stream_id")) in stale_producers:
                current = await self._db.call(
                    "get_agent_question", str(question.get("question_id") or "")
                )
                refreshed.append(current if current is not None else question)
            else:
                refreshed.append(question)
        return refreshed

    async def reconcile_open_questions_against_sessions(self) -> int:
        """Startup reconciliation across the two databases: a crash after a
        session closed (sessions.db) but before its questions expired
        (notifications.db) leaves an open question with no live asker. Expire
        those pairs so recovery never serves a stale, answerable question."""
        if self._sessions is None:
            return 0
        open_questions = await self._db.call("list_agent_questions", open_only=True, limit=None)
        expired = 0
        seen: set[str] = set()
        for question in open_questions or []:
            producer = _nullable_text(question.get("producer_stream_id"))
            if not producer or producer in seen:
                continue
            if self._producer_is_current(question):
                continue
            seen.add(producer)
            nids = await self._db.call(
                "expire_open_questions_for_producer", producer,
                superseding_generation=None, reason="startup_asker_gone",
            )
            for nid in nids:
                await self._broadcast_notification_by_id(nid)
            expired += len(nids)
        return expired

    # -- error frames (lifted) --------------------------------------------

    @staticmethod
    def _prompt_error(request_id: str, error_code: str, **extra: Any) -> dict:
        return {"type": "prompt.error", "request_id": request_id, "ok": False,
                "error_code": error_code, "error": error_code, **extra}

    @staticmethod
    def _notif_error(request_id: str, error_code: str, **extra: Any) -> dict:
        return {"type": "notification.error", "request_id": request_id,
                "error_code": error_code, "error": error_code, **extra}

    @staticmethod
    def _log_notification_rejection(verb: str, reply: dict, msg: dict) -> None:
        """One bounded WARNING per rejected `notification.*` verb (see caller).
        Privacy: emits the payload's KEY names and the resolve-relevant field
        SHAPES only — `action_kind` is a short enum discriminator (safe to log),
        `action_id`/`choice` as TYPE names — never any body, title, or the choice
        value itself."""
        log.warning(
            "notification %s rejected code=%s error=%s payload_keys=%s "
            "action_kind=%r action_id=%s choice=%s",
            verb or "<none>",
            reply.get("error_code"),
            reply.get("message") or reply.get("error"),
            sorted(msg.keys()),
            str(msg.get("action_kind") or ""),
            type(msg.get("action_id")).__name__,
            type(msg.get("choice")).__name__,
        )

    # -- prompt envelope validation (lifted verbatim) ---------------------

    def _validate_prompt_envelope_and_actions(self, envelope: dict, raw_actions: Any) -> tuple[dict, list[dict]]:
        schema_version = envelope.get("schema_version")
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValueError("schema_version must be 1")
        question_id = envelope.get("question_id")
        if not isinstance(question_id, str) or not _AGENT_QUESTION_ID_RE.fullmatch(question_id):
            raise ValueError("question_id must be 1-120 chars: letters, digits, underscore, dash, dot, colon")
        raw_response_mode = str(envelope.get("response_mode") or "")
        if raw_response_mode == "ack":
            raise QuestionFormatError(
                field="response_mode", rule="ack_removed",
                message="ack mode is removed; use a Done/Not yet choice question for an "
                        "action request, or status/notify for an FYI")
        title = _nullable_text(envelope.get("title"))
        dedup_key = _nullable_text(envelope.get("dedup_key"))
        if not title:
            raise QuestionFormatError(field="title", rule="title_required",
                                      message="title is required")
        if "\n" in title or "\r" in title:
            raise QuestionFormatError(field="title", rule="title_one_line",
                                      message="title must be a single raw line")
        if len(title) > QUESTION_TITLE_MAX:
            raise QuestionFormatError(field="title", rule="title_cap",
                                      message="title is too long; shorten it",
                                      limit=QUESTION_TITLE_MAX, actual=len(title))
        body = _normalize_and_validate_question_body(envelope.get("body"))
        # `context` is not a second prose channel: authors put context in `body`.
        raw_context = envelope.get("context")
        if isinstance(raw_context, str) and raw_context.strip():
            raise QuestionFormatError(
                field="context", rule="context_cap_bypass",
                message="a separate context is not allowed; put all readable context in "
                        "body (which enforces the format caps)")
        if not dedup_key:
            raise QuestionFormatError(field="dedup_key", rule="dedup_key_required",
                                      message="dedup_key is required")
        response_mode = raw_response_mode
        if response_mode not in _AGENT_QUESTION_RESPONSE_MODES:
            raise QuestionFormatError(
                field="response_mode", rule="response_mode",
                message="response_mode must be single_choice, multi_choice, or free_text")
        ttl_seconds = envelope.get("ttl_seconds")
        if ttl_seconds is not None and (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
                                        or ttl_seconds <= 0):
            raise ValueError("ttl_seconds must be a positive int or null")
        options = envelope.get("options")
        if not isinstance(options, list):
            raise ValueError("options must be a list")
        normalized_options: list[dict[str, str]] = []
        seen_values: set[str] = set()
        for option in options:
            if not isinstance(option, dict):
                raise ValueError("options must contain objects")
            label = _nullable_text(option.get("label"))
            value = _nullable_text(option.get("value"))
            description = option.get("description")
            if not label:
                raise ValueError("option labels must be non-empty")
            if len(label) > QUESTION_OPTION_LABEL_MAX:
                raise QuestionFormatError(field="option.label", rule="option_label_cap",
                                          message="option label is too long",
                                          limit=QUESTION_OPTION_LABEL_MAX, actual=len(label))
            if "\n" in label or "\r" in label:
                raise QuestionFormatError(field="option.label", rule="option_label_one_line",
                                          message="option label must be a single line")
            if not value:
                raise ValueError("option values must be non-empty")
            if value in seen_values:
                raise ValueError("option values must be unique")
            seen_values.add(value)
            normalized_option = {"label": label, "value": value}
            if description is not None:
                if not isinstance(description, str):
                    raise ValueError("option description must be a string")
                clean = description.strip()
                if "\n" in clean or "\r" in clean:
                    raise QuestionFormatError(field="option.description",
                                              rule="option_desc_one_line",
                                              message="option description must be a single line")
                if len(clean) > QUESTION_OPTION_DESC_MAX:
                    raise QuestionFormatError(field="option.description", rule="option_desc_cap",
                                              message="option description is too long",
                                              limit=QUESTION_OPTION_DESC_MAX, actual=len(clean))
                if clean:
                    normalized_option["description"] = clean
            normalized_options.append(normalized_option)
        allow_custom = envelope.get("allow_custom")
        if allow_custom is not None and not isinstance(allow_custom, bool):
            raise ValueError("allow_custom must be a boolean when present")
        if response_mode == "free_text":
            if normalized_options:
                raise ValueError("free_text prompts cannot define options")
        elif not normalized_options:
            raise QuestionFormatError(field="options", rule="option_min",
                                      message=f"{response_mode} prompts require at least one option",
                                      limit=QUESTION_OPTION_MIN, actual=0)
        elif len(normalized_options) > QUESTION_OPTION_MAX:
            raise QuestionFormatError(field="options", rule="option_max",
                                      message="too many options",
                                      limit=QUESTION_OPTION_MAX, actual=len(normalized_options))
        if not isinstance(raw_actions, list):
            raise ValueError("actions must be a list")
        if len(raw_actions) != len(normalized_options):
            raise ValueError("actions must match options length")
        normalized_actions: list[dict] = []
        for index, raw_action in enumerate(raw_actions):
            if not isinstance(raw_action, dict):
                raise ValueError("actions must contain objects")
            action = dict(raw_action)
            expected_kind = "yes_no"
            if action.get("kind") != expected_kind:
                raise ValueError(f"action {index} kind must be {expected_kind}")
            action_id = _nullable_text(action.get("action_id"))
            if not action_id:
                raise ValueError("action_id must be a non-empty string")
            value = action.get("value")
            if not isinstance(value, dict):
                raise ValueError("action value must be an object")
            if value.get("schema_version") != 1:
                raise ValueError("action value schema_version must be 1")
            if value.get("question_id") != question_id:
                raise ValueError("action value question_id must match envelope")
            if value.get("answer") != normalized_options[index]["value"]:
                raise ValueError("action value answer must match option value")
            if expected_kind == "yes_no" and not isinstance(action.get("choice"), bool):
                raise ValueError("yes_no prompt actions require choice bool")
            normalized_actions.append(action)
        normalized_envelope = dict(envelope)
        normalized_envelope.update({
            "schema_version": 1, "question_id": question_id, "title": title, "body": body,
            "dedup_key": dedup_key, "response_mode": response_mode,
            "options": normalized_options, "ttl_seconds": ttl_seconds,
            # allow_custom is canonical true on EVERY question (free text is
            # always admissible), and context is never a second prose channel.
            "allow_custom": True,
            "context": None,
        })
        return normalized_envelope, normalized_actions


def _answer_back_text(answer: dict) -> str:
    """A readable pane message carrying the operator's answer to the asking
    agent (v2's one-injection-path replacement for v1's peer-queue
    `notification.answer` payload)."""
    parts = ["[notification.answer]", f"notification_id={answer.get('notification_id')}"]
    if answer.get("selections"):
        parts.append("answer=" + ", ".join(str(s) for s in answer["selections"]))
    if isinstance(answer.get("text"), str):
        parts.append("text=" + answer["text"])
    if isinstance(answer.get("custom_text"), str):
        parts.append("custom_text=" + answer["custom_text"])
    if "choice" in answer:
        parts.append(f"choice={answer['choice']}")
    parts.append(f"by={answer.get('by')}")
    text = "\n".join(parts)
    if len(text) > ANSWER_BACK_MAX_CHARS:
        text = text[: ANSWER_BACK_MAX_CHARS - 3] + "..."
    return text


class NotificationExpiry:
    """Background expiry sweep under the four loop rules: cadence, per-pass cap,
    backoff on failure, kill switch (`--disable-notification-expiry`)."""

    def __init__(self, notify: Notify, *, interval_s: float = 60.0, cap: int = 200,
                 backoff_max_s: float = 300.0, first_delay_s: float | None = None) -> None:
        self._notify = notify
        self._interval_s = interval_s
        self._cap = cap
        self._backoff_max_s = backoff_max_s
        self._first_delay_s = interval_s if first_delay_s is None else first_delay_s

    async def run_forever(self) -> None:
        await asyncio.sleep(self._first_delay_s)
        backoff = 1.0
        while True:
            try:
                await self._notify.expire_once(cap=self._cap)
                backoff = 1.0
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
                log.exception("notification expiry pass failed")
                await asyncio.sleep(min(backoff, self._backoff_max_s))
                backoff = min(backoff * 2, self._backoff_max_s)
