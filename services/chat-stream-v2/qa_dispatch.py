"""QA-specific admission and spec-issue verbs; never infer intent from prose."""

from __future__ import annotations

import hashlib
import json
import logging
import os

from sessions import VerbError
from store_qa import QaError

log = logging.getLogger("chat_streamd_v2.qa_dispatch")
BUG_REF = "spec_pentacle__qa_dispatch_reject_counter_2026_09"

FIELDS = ("qa_spec_id", "qa_surface", "qa_cycle")


def enforce():
    mode = os.environ.get("PENTACLE_QA_DISPATCH_MODE", "off")
    if mode not in {"off", "enforce"}:
        raise VerbError(
            "qa_dispatch_mode_invalid",
            "PENTACLE_QA_DISPATCH_MODE must be off or enforce",
        )
    return mode == "enforce"


def classified(msg, row):
    return any(msg.get(k) is not None for k in FIELDS) or any(
        str(row.get(k) or "").lower() == "qa" for k in ("role", "phase")
    )


async def actor(store, msg):
    auth = msg.get("_auth_context")
    if isinstance(auth, dict):
        owner = auth.get("stream_id") if auth.get("token_verified") else None
    else:
        token = msg.get("stream_token")
        state = (
            await store.stream_token_state(hashlib.sha256(token.encode()).hexdigest())
            if isinstance(token, str) and token
            else None
        )
        owner = (
            state.get("stream_id")
            if state
            and state.get("status") == "open"
            and state.get("token_hash_version") == "sha256:v1"
            else None
        )
    claims = [msg.get(k) for k in ("from_stream_id", "caller_stream_id") if msg.get(k)]
    if not owner or any(value != owner for value in claims):
        raise VerbError(
            "qa_unauthorized", "QA mutation requires a verified matching caller token"
        )
    host, name = owner.split(":", 1)
    row = await store.fetch_session(host, name)
    if not row or row.get("status") != "open":
        raise VerbError("qa_unauthorized", "QA caller is not open")
    expected = msg.get("_qa_owner_generation")
    if expected is not None and row["session_generation"] != expected:
        raise VerbError("qa_unauthorized", "scheduled QA owner generation changed")
    return owner, row["session_generation"]


async def admit(
    store,
    msg,
    *,
    row,
    reviewer,
    generation,
    msg_id,
    check_only=False,
    handoff=False,
    existing_reviewer=False,
):
    if handoff or not classified(msg, row):
        return None
    enabled = enforce()
    if not any(msg.get(k) is not None for k in FIELDS) and not enabled:
        return None
    if not all(msg.get(k) is not None for k in FIELDS):
        raise VerbError(
            "qa_scope_required",
            "QA commission requires qa_spec_id, qa_surface and qa_cycle",
        )
    owner, owner_generation = await actor(store, msg)
    # Semantic send retries exclude transport request identity; changed bodies
    # under the same msg id are a conflict, not a fresh commission.
    payload = {
        k: msg[k]
        for k in (
            "text",
            "message",
            "initial_prompt",
            "initial_prompt_blob_sha",
            "attachments",
            "inbox",
        )
        if k in msg
    }
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    try:
        return await store.qa_admit(
            scope={
                "spec_id": msg["qa_spec_id"],
                "surface": msg["qa_surface"],
                "cycle": msg["qa_cycle"],
            },
            actor=owner,
            actor_generation=owner_generation,
            reviewer=reviewer,
            generation=generation,
            msg_id=msg_id,
            payload_hash=payload_hash,
            target_specs=row.get("spec_ids") or [row.get("spec_id")],
            enforce=enabled,
            check_only=check_only,
            existing_reviewer=existing_reviewer,
        )
    except QaError as exc:
        log.info(
            "subsystem=qa_dispatch bug_ref=%s refusal=%s",
            BUG_REF,
            exc.code,
            extra={"subsystem": "qa_dispatch", "bug_ref": BUG_REF, "refusal": exc.code},
        )
        raise VerbError(exc.code, str(exc), **exc.extra) from exc


async def issue(store, msg):
    verb = str(msg["type"]).rsplit(".", 1)[-1]
    owner, generation = await actor(store, msg) if verb != "show" else ("", "")
    try:
        result = await store.qa_issue(
            verb, msg, actor=owner, actor_generation=generation
        )
    except QaError as exc:
        log.info(
            "subsystem=qa_dispatch bug_ref=%s refusal=%s",
            BUG_REF,
            exc.code,
            extra={"subsystem": "qa_dispatch", "bug_ref": BUG_REF, "refusal": exc.code},
        )
        raise VerbError(exc.code, str(exc), **exc.extra) from exc
    return {"type": f"coordination.spec_issue.{verb}.ok", **result}
