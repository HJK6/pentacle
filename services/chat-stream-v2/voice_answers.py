"""voice_answers.v1 — bind one operator voice take to selected durable questions.

The client attaches ``meta.voice_answers`` to the voice turn it sends to the
assistant thread.  Ingest validates that binding against the question store
and stores it with the USER event in one transaction, writing
``meta.voice_answers_status`` onto that event so the transcript echo carries
it.  Ingest never answers or closes a question.

The front desk then answers bound items through ``voice_answer.answer``, a
binding-scoped entry point beneath the existing answer implementation.  The
answer row itself records ``by = voice_answer_by(recording_id)`` in the same
store transaction as the answer, so the per-question acknowledgement can be
re-derived from the answer after a lost reply; the binding's ack row is the
durable projection of that fact.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

VOICE_ANSWERS_VERSION = 1
VOICE_ANSWERS_MAX_ITEMS = 20
_ID_MAX = 200
_PROMPT_MAX = 2000
_ITEM_ID_FIELDS = ("key", "question_id", "notification_id", "producer_stream_id", "surface_stream_id")

VOICE_ANSWER_BINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS v2_voice_answer_bindings (
    recording_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    input_identity TEXT NOT NULL,
    actor_stream_id TEXT,
    state TEXT NOT NULL CHECK(state IN ('bound','dropped')),
    reason TEXT,
    stale_keys_json TEXT NOT NULL,
    binding_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

VOICE_ANSWER_ACKS_DDL = """
CREATE TABLE IF NOT EXISTS v2_voice_answer_acks (
    recording_id TEXT NOT NULL,
    question_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('answered')),
    ack_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (recording_id, question_id)
)
"""


class VoiceAnswersInvalid(ValueError):
    """The ``meta.voice_answers`` payload does not match voice_answers.v1."""


def voice_answer_by(recording_id: str) -> str:
    """The answer row's ``by``: front desk, on the operator's behalf, for one take."""
    return f"front_desk:operator:voice:{recording_id}"


def status(state: str, *, reason: str | None = None, stale_keys: list[str] | None = None) -> dict[str, Any]:
    """The ``meta.voice_answers_status`` wire shape."""
    result: dict[str, Any] = {"state": state}
    if reason:
        result["reason"] = reason
    result["stale_keys"] = list(stale_keys or [])
    return result


def _text(value: object, *, limit: int = _ID_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise VoiceAnswersInvalid("invalid_payload")
    return value.strip()


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise VoiceAnswersInvalid("invalid_payload")
    return round(float(value), 3)


def recording_id_of(raw: object) -> str | None:
    """The take's recording id when present and well formed (idempotency key)."""
    if not isinstance(raw, dict):
        return None
    try:
        return _text(raw.get("recording_id"))
    except VoiceAnswersInvalid:
        return None


def normalize_voice_answers(raw: object) -> dict[str, Any]:
    """Validate the v1 shape and return the closed, normalized binding."""
    if not isinstance(raw, dict) or raw.get("version") != VOICE_ANSWERS_VERSION:
        raise VoiceAnswersInvalid("invalid_payload")
    items = raw.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= VOICE_ANSWERS_MAX_ITEMS:
        raise VoiceAnswersInvalid("invalid_payload")
    normalized_items: list[dict[str, Any]] = []
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise VoiceAnswersInvalid("invalid_payload")
        fields = {name: _text(item.get(name)) for name in _ITEM_ID_FIELDS}
        if fields["key"] in keys:
            raise VoiceAnswersInvalid("invalid_payload")
        keys.add(fields["key"])
        segment = item.get("segment")
        if not isinstance(segment, dict):
            raise VoiceAnswersInvalid("invalid_payload")
        start, end = _seconds(segment.get("start_s")), _seconds(segment.get("end_s"))
        if end < start:
            raise VoiceAnswersInvalid("invalid_payload")
        prompt = item.get("prompt", "")
        if not isinstance(prompt, str) or len(prompt) > _PROMPT_MAX:
            raise VoiceAnswersInvalid("invalid_payload")
        normalized_items.append({**fields, "prompt": prompt, "segment": {"start_s": start, "end_s": end}})
    return {
        "version": VOICE_ANSWERS_VERSION,
        "recording_id": _text(raw.get("recording_id")),
        "blob_sha": _text(raw.get("blob_sha")),
        "duration_s": _seconds(raw.get("duration_s")),
        "items": normalized_items,
    }


def dispatch_block(binding: dict[str, Any] | None) -> str:
    """The binding as the front desk sees it beside the transcript, or ''."""
    if not isinstance(binding, dict):
        return ""
    payload = {
        "recording_id": binding.get("recording_id"),
        "status": status(str(binding.get("state") or ""), reason=binding.get("reason"),
                         stale_keys=binding.get("stale_keys")),
        "items": [
            {key: item.get(key) for key in (
                "key", "question_id", "producer_stream_id", "prompt", "segment", "stale")}
            for item in binding.get("items") or [] if isinstance(item, dict)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (
        "\nThis voice take is bound to the operator questions below, in order with segment times. "
        "Answer only bound, non-stale items whose answer the transcript states unambiguously, one "
        "item at a time, with the daemon verb voice_answer.answer {recording_id, question_id, "
        "selections?, text?}; leave ambiguous or absent answers unanswered and never guess an option. "
        "Name answered sessions only from answered acknowledgements.\n"
        "<voice-answers-binding-json>\n"
        f"{encoded}\n"
        "</voice-answers-binding-json>"
    )


# -- storage (shares the v2 store connection and its transactions) ------------


def insert_binding_conn(
    conn: sqlite3.Connection, *, stream_id: str, input_identity: str,
    actor_stream_id: str | None, prepared: dict[str, Any], created_at: str,
) -> None:
    """Store a prepared binding inside the caller's USER-admission transaction."""
    binding = prepared["binding"]
    state = prepared["status"]
    conn.execute(
        """INSERT INTO v2_voice_answer_bindings(
            recording_id,stream_id,input_identity,actor_stream_id,state,reason,
            stale_keys_json,binding_json,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (prepared["recording_id"], stream_id, input_identity, actor_stream_id, state["state"],
         state.get("reason"), json.dumps(state["stale_keys"]),
         json.dumps(binding, sort_keys=True, ensure_ascii=False), created_at),
    )


def binding_from_rows(row: sqlite3.Row | None, acks: list[sqlite3.Row]) -> dict[str, Any] | None:
    if row is None:
        return None
    binding = json.loads(row["binding_json"] or "{}")
    return {
        "recording_id": row["recording_id"],
        "stream_id": row["stream_id"],
        "input_identity": row["input_identity"],
        "actor_stream_id": row["actor_stream_id"],
        "state": row["state"],
        "reason": row["reason"],
        "stale_keys": json.loads(row["stale_keys_json"] or "[]"),
        "items": binding.get("items") if isinstance(binding.get("items"), list) else [],
        "created_at": row["created_at"],
        "acks": {ack["question_id"]: json.loads(ack["ack_json"]) for ack in acks},
    }


class VoiceAnswersStoreMixin:
    """Binding and acknowledgement rows on the v2 store."""

    async def get_voice_answer_binding(self, recording_id: str) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_voice_answer_bindings WHERE recording_id=?", (recording_id,),
            ).fetchone()
            acks = conn.execute(
                "SELECT question_id,ack_json FROM v2_voice_answer_acks WHERE recording_id=? ORDER BY created_at,question_id",
                (recording_id,),
            ).fetchall() if row is not None else []
            return binding_from_rows(row, acks)
        return await self.submit(_op)

    async def get_voice_answer_binding_for_input(
        self, *, stream_id: str, input_identity: str,
    ) -> dict[str, Any] | None:
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_voice_answer_bindings WHERE stream_id=? AND input_identity=?",
                (stream_id, input_identity),
            ).fetchone()
            return binding_from_rows(row, [])
        return await self.submit(_op)

    async def record_voice_answer_ack(
        self, *, recording_id: str, question_id: str, ack: dict[str, Any], created_at: str,
    ) -> dict[str, Any]:
        """Record the first acknowledgement for the pair; return the stored one."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO v2_voice_answer_acks(
                        recording_id,question_id,outcome,ack_json,created_at
                    ) VALUES (?,?,?,?,?)""",
                    (recording_id, question_id, "answered",
                     json.dumps(ack, sort_keys=True, ensure_ascii=False), created_at),
                )
                row = conn.execute(
                    "SELECT ack_json FROM v2_voice_answer_acks WHERE recording_id=? AND question_id=?",
                    (recording_id, question_id),
                ).fetchone()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return json.loads(row["ack_json"])
        return await self.submit(_op)
