"""Durable state, idempotent records and notice issue for the external-work check.

Every operation is one transaction on the store worker: a fault leaves neither
partial state nor a receipt. Mixed into ``Store``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable

from external_work import (
    OBSERVATION_MAX_AGE_S, REMINDER_INTERVAL_S, STATE_KEYS, apply_observation, canonical, derive_reasons,
    due_at, new_state, notice_id, record_digest,
)
from message_envelopes import build_external_work_due_body
from outbound_notices import NOTICE_KIND_EXTERNAL_WORK_DUE
from store_assistant_binding import _binding_conn
from store_routing import _assistant_actor_conn, _insert_outbound_notice_conn, _routing_iso_now

EXTERNAL_WORK_DDL = (
    """
CREATE TABLE IF NOT EXISTS v2_external_work_state (
    watch_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    data TEXT NOT NULL
)
""",
    """
CREATE TABLE IF NOT EXISTS v2_external_work_records (
    watch_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    actor_stream TEXT NOT NULL,
    actor_generation TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (watch_id, request_id)
)
""",
)


def _load_conn(conn, watch_id, now=None):
    """Return ``(version, state)``; with ``now`` an absent watch is created at version 0."""
    row = conn.execute("SELECT version,data FROM v2_external_work_state WHERE watch_id=?", (watch_id,)).fetchone()
    if row is None:
        if now is None:
            return None
        state = new_state(now)
        conn.execute("INSERT INTO v2_external_work_state (watch_id,version,data) VALUES (?,0,?)",
                     (watch_id, canonical(state)))
        return 0, state
    state = json.loads(row["data"])
    if not isinstance(state, dict) or set(state) != STATE_KEYS or state["v"] != 1:
        raise ValueError("external_work_state_corrupt")
    return int(row["version"]), state


def _save_conn(conn, watch_id, version, state):
    conn.execute("UPDATE v2_external_work_state SET version=?,data=? WHERE watch_id=?",
                 (version, canonical(state), watch_id))


def _recipient_conn(conn, config, env_binding):
    try:
        binding = _binding_conn(conn, env_binding or {}, name=config.assistant_name, include_target=False)
    except ValueError:
        return None
    if binding["source"] == "unconfigured":
        return None
    return {"stream_id": binding["stream_id"], "generation": binding["generation"]}


def _authorize_conn(conn, config, env_binding, actor, generation):
    """Only the configured assistant's current front desk, at its current open generation."""
    if not actor or not generation or _recipient_conn(conn, config, env_binding) != {
            "stream_id": actor, "generation": generation}:
        raise ValueError("fd_not_current")
    try:
        _assistant_actor_conn(conn, actor, generation)
    except ValueError:
        raise ValueError("fd_not_current") from None
    if not config.enabled:
        raise ValueError(config.status)


# Terminal reasons after which the pending episode is sent again at once: the
# reminder was refused for want of a recipient, not delivered or exhausted.
_UNSENT_REASONS = ("external_work_recipient_rebound", "external_work_disabled")


def _retire_conn(conn, watch_id, episode, reason):
    """Retire the episode's undelivered reminders, except one a delivery holds.

    A leased row has passed its guard and may be mid-paste; retiring it would
    mislabel a send that still lands. Its own next attempt meets the guard."""
    conn.execute(
        "UPDATE v2_outbound_notices SET terminal_at=?,terminal_reason=? "
        "WHERE kind=? AND episode_id=? AND delivered_at IS NULL AND terminal_at IS NULL "
        "AND (lease_until IS NULL OR lease_until <= ?)",
        (_routing_iso_now(), reason, NOTICE_KIND_EXTERNAL_WORK_DUE, f"{watch_id}:{episode}", time.time()))


def _refresh_conn(conn, watch_id, state, now):
    """Advance the clock, re-derive reasons and open or close the episode. Returns effective now."""
    effective = max(now, state["clock_high_water"])
    state["clock_high_water"] = effective
    reasons, prior = derive_reasons(state, effective), state["active_reasons"]
    if reasons and not prior:
        state["episode"] += 1
        state["last_notice_at"] = state["active_notice_id"] = state["active_recipient"] = None
    elif prior and not reasons:
        _retire_conn(conn, watch_id, state["episode"], "external_work_resolved")
        state["active_notice_id"] = state["active_recipient"] = None
    state["active_reasons"] = reasons
    return effective


def _issue_conn(conn, config, state, recipient, effective):
    """At most one nonterminal reminder per episode and recipient; reminders are 2 h apart."""
    prior, active = state["active_recipient"], state["active_notice_id"]
    if prior is not None and prior != recipient:
        # Routing replacement: the new front desk gets the pending episode at once.
        _retire_conn(conn, config.watch_id, state["episode"], "external_work_recipient_replaced")
    elif active is not None:
        row = conn.execute(
            "SELECT delivered_at,terminal_at,terminal_reason,lease_until FROM v2_outbound_notices "
            "WHERE notice_id=?", (active,)).fetchone()
        unsent = row is not None and row["terminal_reason"] in _UNSENT_REASONS
        if not unsent and effective < state["last_notice_at"] + REMINDER_INTERVAL_S:
            return None
        if row is not None and not (row["delivered_at"] or row["terminal_at"]):
            if row["lease_until"] is not None and row["lease_until"] > time.time():
                return None  # in flight: never queue a second reminder beside it
            # Two hours without a delivery outcome: replace it, so a reminder
            # the outbox can never settle cannot silence the check.
            _retire_conn(conn, config.watch_id, state["episode"], "external_work_superseded")
    state["notice_sequence"] += 1
    nid = notice_id(config.watch_id, state["episode"], state["notice_sequence"], recipient)
    age = lambda since: None if since is None else int(effective - since)  # noqa: E731
    body = build_external_work_due_body(
        nid, label=config.label, queue_ref=config.queue_ref, episode=state["episode"],
        reasons=state["active_reasons"], last_verified_age_s=age(state["last_verified_at"]),
        blocked_age_s=age(state["blocked_since"]))
    _insert_outbound_notice_conn(
        conn, notice_id=nid, tell_id=nid, kind=NOTICE_KIND_EXTERNAL_WORK_DUE, dedupe_key=nid,
        recipient_stream_id=recipient["stream_id"], body=body, episode_id=f"{config.watch_id}:{state['episode']}",
        metadata={"watch_id": config.watch_id, "episode": state["episode"],
                  "recipient_generation": recipient["generation"]})
    state["last_notice_at"], state["active_notice_id"], state["active_recipient"] = effective, nid, recipient
    return nid


def _view_conn(conn, config, version, state, effective):
    notice = None
    if state["active_notice_id"]:
        row = conn.execute(
            "SELECT notice_id,attempts,delivered_at,terminal_at,terminal_reason FROM v2_outbound_notices "
            "WHERE notice_id=?", (state["active_notice_id"],)).fetchone()
        notice = dict(row) if row else None
    return {"watch_id": config.watch_id, "version": version, "state": state,
            "health": {"enabled": True, "due_at": due_at(state, effective),
                       "reasons": state["active_reasons"], "last_notice": notice}}


class _ExternalWorkStoreMixin:
    # Test hook: raised after the state update and before the receipt insert.
    _external_work_fault: Callable[[], None] | None = None

    async def _external_work_txn(self, body):
        def _op(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = body(conn)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise
        return await self.submit(_op)

    async def external_work_tick(self, *, config, env_binding, now) -> dict[str, Any]:
        def body(conn):
            version, state = _load_conn(conn, config.watch_id, now)
            effective = _refresh_conn(conn, config.watch_id, state, now)
            issued = None
            if state["active_reasons"]:
                recipient = _recipient_conn(conn, config, env_binding)
                # No current binding: the episode stays pending and the first bound tick recovers it.
                if recipient is not None:
                    issued = _issue_conn(conn, config, state, recipient, effective)
            _save_conn(conn, config.watch_id, version, state)
            return {"reasons": state["active_reasons"], "episode": state["episode"], "issued": issued}
        return await self._external_work_txn(body)

    async def external_work_show(self, *, config, env_binding, actor, generation, now) -> dict[str, Any]:
        def body(conn):
            _authorize_conn(conn, config, env_binding, actor, generation)
            version, state = _load_conn(conn, config.watch_id, now)
            effective = _refresh_conn(conn, config.watch_id, state, now)
            _save_conn(conn, config.watch_id, version, state)
            return _view_conn(conn, config, version, state, effective)
        return await self._external_work_txn(body)

    async def external_work_record(self, *, config, env_binding, actor, generation, now, record) -> dict[str, Any]:
        digest, fault = record_digest(record), self._external_work_fault

        def body(conn):
            _authorize_conn(conn, config, env_binding, actor, generation)
            if record["watch_id"] != config.watch_id:
                raise ValueError("invalid_request")
            prior = conn.execute(
                "SELECT payload_digest,result FROM v2_external_work_records WHERE watch_id=? AND request_id=?",
                (config.watch_id, record["request_id"])).fetchone()
            if prior is not None:
                if prior["payload_digest"] != digest:
                    raise ValueError("idempotency_conflict")
                return {**json.loads(prior["result"]), "duplicate": True}
            version, state = _load_conn(conn, config.watch_id, now)
            if record["expected_version"] != version:
                raise ValueError("version_conflict")
            observed_at, latest = record["observed_at"], state["observation"]
            if observed_at > now:
                raise ValueError("invalid_request")
            if now - observed_at > OBSERVATION_MAX_AGE_S or (latest and observed_at < latest["observed_at"]):
                raise ValueError("observation_stale")
            apply_observation(state, record)
            effective = _refresh_conn(conn, config.watch_id, state, now)
            version += 1
            _save_conn(conn, config.watch_id, version, state)
            result = {**_view_conn(conn, config, version, state, effective), "request_id": record["request_id"]}
            if fault is not None:
                fault()
            conn.execute(
                "INSERT INTO v2_external_work_records (watch_id,request_id,payload_digest,actor_stream,"
                "actor_generation,recorded_at,result) VALUES (?,?,?,?,?,?,?)",
                (config.watch_id, record["request_id"], digest, actor, generation, now, canonical(result)))
            return {**result, "duplicate": False}
        return await self._external_work_txn(body)

    async def external_work_notice_verdict(self, notice_id, *, config, env_binding) -> dict[str, Any]:
        """Whether a queued reminder may still be delivered to its recipient."""
        def _op(conn):
            row = conn.execute("SELECT recipient_stream_id,metadata FROM v2_outbound_notices WHERE notice_id=?",
                               (notice_id,)).fetchone()
            metadata = json.loads(row["metadata"] or "{}") if row else {}
            generation = metadata.get("recipient_generation")
            if not config.enabled:
                code = "external_work_disabled"
            elif not row or metadata.get("watch_id") != config.watch_id:
                code = "external_work_watch_changed"
            else:
                loaded = _load_conn(conn, config.watch_id)
                state = loaded[1] if loaded else None
                if (not state or not state["active_reasons"] or state["active_notice_id"] != notice_id
                        or state["episode"] != metadata.get("episode")):
                    code = "external_work_resolved"
                elif _recipient_conn(conn, config, env_binding) != {
                        "stream_id": row["recipient_stream_id"], "generation": generation}:
                    code = "external_work_recipient_rebound"
                else:
                    code = "ok"
            return {"code": code, "generation": generation}
        return await self.submit(_op)
