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
    due_at, new_state, notice_id, record_digest, PacketError, OPEN_PACKET_STATES, validate_tag,
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
    """
CREATE TABLE IF NOT EXISTS v2_external_work_packets (
    watch_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    spec_id TEXT,
    state TEXT NOT NULL,
    opened_at REAL NOT NULL,
    state_at REAL NOT NULL,
    evidence_at REAL NOT NULL,
    mail_id TEXT,
    PRIMARY KEY (watch_id, tag)
)
""",
)


def _packet_rows(conn, watch_id):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM v2_external_work_packets WHERE watch_id=? ORDER BY tag", (watch_id,))]


def current_packets_conn(conn, *, config, snapshot=None, observations_read=()):
    """Read current packets, optionally sharing a lane detail transaction's observations."""
    if config is None or not config.enabled:
        return {"label": "", "packets": []}
    rows = [row for row in _packet_rows(conn, config.watch_id) if row["state"] in OPEN_PACKET_STATES]
    from store_work_index import effective_membership_conn
    if snapshot is None:
        snapshot = effective_membership_conn(conn, spec_ids=[row["spec_id"] for row in rows])
        members = snapshot["members"]
    else:
        # The lane read can have looked up IDs that have no observation row.
        # Only IDs it did not look up need another observation query.
        packet_only = {row["spec_id"] for row in rows if row["spec_id"] not in observations_read}
        members = snapshot["members"]
        if packet_only:
            extra = effective_membership_conn(conn, spec_ids=packet_only, snapshot=snapshot)
            members = members | extra["members"]
    if snapshot["state"]["available"]:
        rows = [row for row in rows if members.get(row["spec_id"], {}).get("terminal")
                not in {"completed", "deprecated"}]
    return {"label": config.label, "packets": rows}


def _close_finished_conn(conn, config, now):
    from store_work_index import effective_membership_conn
    rows = [row for row in _packet_rows(conn, config.watch_id) if row["state"] in OPEN_PACKET_STATES]
    snapshot = effective_membership_conn(conn, spec_ids=[row["spec_id"] for row in rows])
    if snapshot["state"]["available"]:
        for row in rows:
            if snapshot["members"].get(row["spec_id"], {}).get("terminal") in {"completed", "deprecated"}:
                conn.execute("UPDATE v2_external_work_packets SET state='closed',state_at=?,evidence_at=? "
                             "WHERE watch_id=? AND tag=?", (now, max(now, row["evidence_at"]), config.watch_id, row["tag"]))


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
            result = _view_conn(conn, config, version, state, effective)
            result["packets"] = [{key: row[key] for key in ("tag", "spec_id", "state", "state_at", "evidence_at")}
                                 for row in _packet_rows(conn, config.watch_id) if row["state"] != "closed"]
            return result
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
            _close_finished_conn(conn, config, now)
            packets = _packet_rows(conn, config.watch_id)
            if record["state"] == "working" and not any(row["state"] == "working" for row in packets):
                raise PacketError("packets_required")
            unlinked = [row["tag"] for row in packets if row["state"] == "unlinked"]
            if record["state"] != "unknown" and unlinked:
                raise PacketError("packets_unlinked", tags=unlinked[:32])
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

    async def external_work_current_packets(self, *, config=None):
        config = config if config is not None else getattr(self, "external_work_config", None)
        return await self.submit(lambda conn: current_packets_conn(conn, config=config))

    async def external_work_packet(self, *, config, env_binding, actor, generation, now,
                                   op, tag, spec_id=None, state=None):
        tag = validate_tag(tag)
        def body(conn):
            _authorize_conn(conn, config, env_binding, actor, generation)
            row = conn.execute("SELECT * FROM v2_external_work_packets WHERE watch_id=? AND tag=?",
                               (config.watch_id, tag)).fetchone()
            old = dict(row) if row else None
            opened = old is not None and old["state"] in OPEN_PACKET_STATES
            if op == "open":
                if opened and old["spec_id"] != spec_id:
                    raise PacketError("packet_conflict")
                from store_work_index import effective_membership_conn
                snapshot = effective_membership_conn(conn, spec_ids=[spec_id])
                if snapshot["state"]["available"] and not any(spec_id in ids for ids in snapshot["membership"].values()):
                    raise PacketError("packet_spec_unplaced")
                if not opened and sum(r["state"] in OPEN_PACKET_STATES for r in _packet_rows(conn, config.watch_id)) >= 32:
                    raise PacketError("packet_limit")
                target, identity = state or "working", spec_id
            elif op == "set":
                if not opened:
                    raise PacketError("packet_unknown")
                target, identity = state, old["spec_id"]
            elif op == "close":
                if old is None or old["state"] == "ignored":
                    raise PacketError("packet_unknown")
                target, identity = "closed", old["spec_id"]
            elif op == "ignore":
                if opened:
                    raise PacketError("packet_conflict")
                target, identity = "ignored", None
            else:
                raise PacketError("invalid_request")
            if op == "open" and target not in {"working", "queued"} or op == "set" and target not in OPEN_PACKET_STATES:
                raise PacketError("invalid_request")
            if old is None:
                conn.execute("INSERT INTO v2_external_work_packets VALUES (?,?,?,?,?,?,?,NULL)",
                             (config.watch_id, tag, identity, target, now, now, now))
            elif old["state"] != target or old["spec_id"] != identity:
                conn.execute("UPDATE v2_external_work_packets SET spec_id=?,state=?,state_at=?,evidence_at=? "
                             "WHERE watch_id=? AND tag=?", (identity, target, now, max(now, old["evidence_at"]), config.watch_id, tag))
            _close_finished_conn(conn, config, now)
            return {"packet": dict(conn.execute("SELECT * FROM v2_external_work_packets WHERE watch_id=? AND tag=?",
                                                (config.watch_id, tag)).fetchone())}
        return await self._external_work_txn(body)

    async def external_work_mail(self, *, config, tag, mail_id, at, now):
        def body(conn):
            if not config.enabled:
                raise PacketError(config.status)
            row = conn.execute("SELECT * FROM v2_external_work_packets WHERE watch_id=? AND tag=?",
                               (config.watch_id, tag)).fetchone()
            if row is not None:
                if at >= row["evidence_at"]:
                    conn.execute("UPDATE v2_external_work_packets SET evidence_at=?,mail_id=? WHERE watch_id=? AND tag=?",
                                 (at, mail_id, config.watch_id, tag))
                return {"linked": row["state"] in OPEN_PACKET_STATES, "stored": True}
            count = conn.execute("SELECT COUNT(*) FROM v2_external_work_packets WHERE watch_id=? AND state='unlinked'",
                                 (config.watch_id,)).fetchone()[0]
            if count >= 32:
                return {"linked": False, "stored": False}
            conn.execute("INSERT INTO v2_external_work_packets VALUES (?,?,NULL,'unlinked',?,?,?,?)",
                         (config.watch_id, tag, at, at, at, mail_id))
            return {"linked": False, "stored": True}
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
