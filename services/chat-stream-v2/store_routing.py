"""Routing-integrity persistence owned by the v2 Store façade.

This module contains the routing capability, lifecycle guard, and routing
table operations. It never opens SQLite itself; callers reach the same
Store.submit worker boundary through the composed façade.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from typing import Any

from store_specs import _row, _session_row

# v2-only. One current routing episode per stream. The sessions row carries the
# latest observation (`routing_integrity`, reason, and timestamp); this table is
# the durable drift episode and notice-dedupe state. Keeping it separate
# preserves the shared v1 sessions shape while making evidence survive restart.
ROUTING_INTEGRITY_DDL = """
CREATE TABLE IF NOT EXISTS v2_routing_integrity (
    stream_id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    session_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    requested_model TEXT,
    requested_effort TEXT,
    effective_model TEXT,
    effective_effort TEXT,
    reason TEXT,
    first_observed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    parent_stream_id TEXT,
    notified_at TEXT
)
"""
ROUTING_INTEGRITY_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS v2_routing_integrity_audit (
    audit_id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
)
"""
OUTBOUND_NOTICE_DDL = """
CREATE TABLE IF NOT EXISTS v2_outbound_notices (
    notice_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    recipient_stream_id TEXT NOT NULL,
    tell_id TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    source_stream_id TEXT,
    episode_id TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    last_error TEXT,
    next_action TEXT,
    delivered_at TEXT,
    terminal_at TEXT,
    terminal_reason TEXT
)
"""
OUTBOUND_NOTICE_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_v2_outbound_notices_due "
    "ON v2_outbound_notices (terminal_at, delivered_at, lease_until, next_attempt_at)"
)
ROUTING_INTEGRITY_NOTICE_DDL = OUTBOUND_NOTICE_DDL

def _insert_outbound_notice_conn(
    conn: sqlite3.Connection, *, notice_id: str, kind: str, dedupe_key: str,
    recipient_stream_id: str, tell_id: str, body: str,
    source_stream_id: str | None = None, episode_id: str | None = None,
    metadata: dict[str, Any] | None = None, created_at: str | None = None,
) -> dict[str, Any]:
    """Transaction-local insertion; caller owns commit/rollback."""
    stamp = created_at or _routing_iso_now()
    metadata_json = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
    digest = _outbound_notice_digest(
        kind,
        dedupe_key,
        recipient_stream_id,
        tell_id,
        body,
        metadata_json,
        source_stream_id or "",
        episode_id or "",
    )
    existing = conn.execute(
        """SELECT * FROM v2_outbound_notices
           WHERE notice_id=? OR dedupe_key=? OR tell_id=?
           LIMIT 1""",
        (notice_id, dedupe_key, tell_id),
    ).fetchone()
    if existing is not None:
        prior = dict(existing)
        if (
            prior.get("payload_digest") != digest
            or prior.get("kind") != kind
            or prior.get("dedupe_key") != dedupe_key
            or prior.get("recipient_stream_id") != recipient_stream_id
            or prior.get("tell_id") != tell_id
        ):
            raise ValueError(
                "outbound_notice_conflict: identity reused with a different payload "
                f"(notice_id={notice_id}, dedupe_key={dedupe_key})"
            )
        prior["created"] = False
        return prior
    conn.execute(
        """INSERT INTO v2_outbound_notices (
            notice_id, kind, dedupe_key, recipient_stream_id, tell_id, body,
            payload_digest, source_stream_id, episode_id, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            notice_id, kind, dedupe_key, recipient_stream_id, tell_id, body,
            digest, source_stream_id, episode_id, metadata_json, stamp,
        ),
    )
    row = conn.execute(
        "SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice_id,)
    ).fetchone()
    out = dict(row)
    out["created"] = True
    return out


class _RoutingIntegrityLifecycleEntry:
    """Reference-counted per-stream lock entry.

    A plain lock cannot be evicted safely: a task may have fetched it and be
    waiting while a close path removes the dictionary entry. New work could
    then acquire a different lock for the same stream. References cover both
    owners and waiters, so retirement happens only after the last guard exits.
    """

    __slots__ = ("store", "stream_id", "lock", "references", "retire_requested")

    def __init__(self, store: Any, stream_id: str) -> None:
        self.store = store
        self.stream_id = stream_id
        self.lock = asyncio.Lock()
        self.references = 0
        self.retire_requested = False

class _RoutingIntegrityLifecycleGuard:
    """Async context manager retaining a lifecycle entry through acquisition."""

    __slots__ = ("entry", "_released", "_acquired")

    def __init__(self, entry: _RoutingIntegrityLifecycleEntry) -> None:
        self.entry = entry
        entry.references += 1
        # A new lifecycle use means the stream is active again (for example a
        # close racing with a name reopen), so do not retire this entry.
        entry.retire_requested = False
        self._released = False
        self._acquired = False

    async def __aenter__(self) -> "_RoutingIntegrityLifecycleGuard":
        try:
            await self.entry.lock.acquire()
        except BaseException:
            self._release_reference()
            raise
        self._acquired = True
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._acquired:
            self.entry.lock.release()
        self._release_reference()

    def _release_reference(self) -> None:
        if self._released:
            return
        self._released = True
        self.entry.references -= 1
        self.entry.store._maybe_retire_routing_integrity_lifecycle_lock(self.entry)

class _RoutingStoreMixin:
    """Route and outbound-notice persistence mixed into Store."""

    def _init_routing_integrity_state(self) -> None:
        self._routing_lifecycle_locks: dict[str, _RoutingIntegrityLifecycleEntry] = {}

    def routing_integrity_lifecycle_lock(self, stream_id: str) -> _RoutingIntegrityLifecycleGuard:
        """Serialize close/reopen with routing notification delivery.

        The returned guard retains the dictionary entry before it can suspend
        on acquisition, which makes closed-stream cache eviction race-safe.
        """
        entry = self._routing_lifecycle_locks.get(stream_id)
        if entry is None:
            entry = _RoutingIntegrityLifecycleEntry(self, stream_id)
            self._routing_lifecycle_locks[stream_id] = entry
        return _RoutingIntegrityLifecycleGuard(entry)

    def _retire_routing_integrity_lifecycle_lock(self, stream_id: str) -> None:
        entry = self._routing_lifecycle_locks.get(stream_id)
        if entry is None:
            return
        entry.retire_requested = True
        self._maybe_retire_routing_integrity_lifecycle_lock(entry)

    def _maybe_retire_routing_integrity_lifecycle_lock(
        self, entry: _RoutingIntegrityLifecycleEntry
    ) -> None:
        if (
            entry.retire_requested
            and entry.references == 0
            and not entry.lock.locked()
            and self._routing_lifecycle_locks.get(entry.stream_id) is entry
        ):
            self._routing_lifecycle_locks.pop(entry.stream_id, None)

    async def apply_routing_integrity(
        self,
        host: str,
        session_name: str,
        *,
        provider: str,
        requested_model: str,
        requested_effort: str,
        effective_model: str | None,
        effective_effort: str | None,
        integrity: str,
        reason: str | None,
        observed_at: str | None = None,
        expected_generation: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically stamp an observation and maintain its current episode."""
        observed = observed_at or _routing_iso_now()
        stream_id = f"{host}:{session_name}"

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()
                if session_row is None:
                    conn.commit()
                    return None
                session = dict(session_row)
                session_generation = str(session.get("created_at") or "")
                if expected_generation is not None and session_generation != expected_generation:
                    conn.commit()
                    return None
                episode: dict[str, Any] | None = None
                if integrity == "mismatch":
                    current_row = conn.execute(
                        "SELECT * FROM v2_routing_integrity WHERE stream_id=?",
                        (stream_id,),
                    ).fetchone()
                    current = dict(current_row) if current_row is not None else None
                    incoming_tuple = (
                        provider,
                        requested_model,
                        requested_effort,
                        str(effective_model or ""),
                        str(effective_effort or ""),
                    )
                    current_tuple = None if current is None else (
                        str(current.get("provider") or ""),
                        str(current.get("requested_model") or ""),
                        str(current.get("requested_effort") or ""),
                        str(current.get("effective_model") or ""),
                        str(current.get("effective_effort") or ""),
                    )
                    if current is None:
                        episode_id = _routing_episode_id(
                            conn,
                            stream_id=stream_id,
                            session_generation=session_generation,
                            provider=provider,
                            requested_model=requested_model,
                            requested_effort=requested_effort,
                            effective_model=effective_model,
                            effective_effort=effective_effort,
                            first_observed=observed,
                        )
                        first_observed = observed
                        conn.execute(
                            """INSERT INTO v2_routing_integrity (
                                stream_id, host, session_name, provider, episode_id,
                                requested_model, requested_effort, effective_model,
                                effective_effort, reason, first_observed_at, updated_at,
                                parent_stream_id, notified_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                stream_id,
                                host,
                                session_name,
                                provider,
                                episode_id,
                                requested_model,
                                requested_effort,
                                effective_model,
                                effective_effort,
                                reason,
                                first_observed,
                                observed,
                                session.get("parent_stream_id"),
                                None,
                            ),
                        )
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-drift-" + episode_id,
                                stream_id,
                                episode_id,
                                "routing_drift",
                                "daemon:routing_integrity",
                                reason,
                                json.dumps(
                                    {
                                        "provider": provider,
                                        "requested_model": requested_model,
                                        "requested_effort": requested_effort,
                                        "effective_model": effective_model,
                                        "effective_effort": effective_effort,
                                    },
                                    separators=(",", ":"),
                                ),
                                observed,
                            ),
                        )
                    elif current_tuple != incoming_tuple:
                        old_episode_id = str(current["episode_id"])
                        supersede_material = "\x1f".join(
                            (old_episode_id, *incoming_tuple, observed)
                        )
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-superseded-"
                                + hashlib.sha256(supersede_material.encode("utf-8")).hexdigest()[:24],
                                stream_id,
                                old_episode_id,
                                "routing_drift_superseded",
                                "daemon:routing_integrity",
                                reason,
                                json.dumps(
                                    {
                                        "old_episode_id": old_episode_id,
                                        "new_tuple": incoming_tuple,
                                    },
                                    separators=(",", ":"),
                                ),
                                observed,
                            ),
                        )
                        conn.execute(
                            "DELETE FROM v2_routing_integrity WHERE stream_id=?",
                            (stream_id,),
                        )
                        episode_id = _routing_episode_id(
                            conn,
                            stream_id=stream_id,
                            session_generation=session_generation,
                            provider=provider,
                            requested_model=requested_model,
                            requested_effort=requested_effort,
                            effective_model=effective_model,
                            effective_effort=effective_effort,
                            first_observed=observed,
                        )
                        conn.execute(
                            """INSERT INTO v2_routing_integrity (
                                stream_id, host, session_name, provider, episode_id,
                                requested_model, requested_effort, effective_model,
                                effective_effort, reason, first_observed_at, updated_at,
                                parent_stream_id, notified_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                stream_id,
                                host,
                                session_name,
                                provider,
                                episode_id,
                                requested_model,
                                requested_effort,
                                effective_model,
                                effective_effort,
                                reason,
                                observed,
                                observed,
                                session.get("parent_stream_id"),
                                None,
                            ),
                        )
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-drift-" + episode_id,
                                stream_id,
                                episode_id,
                                "routing_drift",
                                "daemon:routing_integrity",
                                reason,
                                json.dumps(
                                    {
                                        "provider": provider,
                                        "requested_model": requested_model,
                                        "requested_effort": requested_effort,
                                        "effective_model": effective_model,
                                        "effective_effort": effective_effort,
                                    },
                                    separators=(",", ":"),
                                ),
                                observed,
                            ),
                        )
                    else:
                        episode_id = str(current["episode_id"])
                        conn.execute(
                            """UPDATE v2_routing_integrity
                               SET provider=?, requested_model=?, requested_effort=?,
                                   effective_model=?, effective_effort=?, reason=?,
                                   updated_at=?, parent_stream_id=?
                             WHERE stream_id=?""",
                            (
                                provider,
                                requested_model,
                                requested_effort,
                                effective_model,
                                effective_effort,
                                reason,
                                observed,
                                session.get("parent_stream_id"),
                                stream_id,
                            ),
                        )
                    episode_row = conn.execute(
                        "SELECT * FROM v2_routing_integrity WHERE stream_id=?",
                        (stream_id,),
                    ).fetchone()
                    episode = dict(episode_row) if episode_row is not None else None
                    if episode is not None:
                        episode["session_generation"] = session_generation
                    event_id = episode.get("episode_id") if episode is not None else None
                else:
                    prior = conn.execute(
                        "SELECT episode_id FROM v2_routing_integrity WHERE stream_id=?",
                        (stream_id,),
                    ).fetchone()
                    if prior is not None:
                        conn.execute(
                            """INSERT OR IGNORE INTO v2_routing_integrity_audit (
                                audit_id, stream_id, episode_id, event_type, actor,
                                reason, metadata, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                "routing-resolved-" + str(prior["episode_id"]),
                                stream_id,
                                str(prior["episode_id"]),
                                "routing_drift_resolved",
                                "daemon:routing_integrity",
                                reason,
                                "{}",
                                observed,
                            ),
                        )
                        conn.execute("DELETE FROM v2_routing_integrity WHERE stream_id=?", (stream_id,))
                    event_id = None
                conn.execute(
                    """UPDATE sessions
                       SET effective_model=?, effective_effort=?, routing_integrity=NULL,
                           routing_integrity_reason=NULL, routing_integrity_updated_at=NULL
                     WHERE host=? AND session_name=?""",
                    (
                        effective_model,
                        effective_effort,
                        host,
                        session_name,
                    ),
                )
                conn.commit()
                session = _session_row(conn, conn.execute(
                    "SELECT * FROM sessions WHERE host=? AND session_name=?",
                    (host, session_name),
                ).fetchone()) or session
                return {
                    "session": session,
                    "episode": episode,
                    "should_notify": bool(episode and not episode.get("notified_at")),
                }
            except Exception:
                conn.rollback()
                raise

        return await self.submit(_op)

    async def routing_integrity_episode(self, stream_id: str) -> dict[str, Any] | None:
        """Return the current durable routing-drift episode, if any."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM v2_routing_integrity WHERE stream_id=?",
                (stream_id,),
            ).fetchone()
            return None if row is None else dict(row)

        return await self.submit(_op)

    async def mark_routing_integrity_notified(
        self,
        stream_id: str,
        episode_id: str,
        *,
        expected_generation: str | None = None,
    ) -> bool:
        """Durably close the one-notice window for an episode."""
        stamp = _routing_iso_now()

        def _op(conn: sqlite3.Connection) -> bool:
            if expected_generation is not None:
                generation_host, separator, generation_name = stream_id.partition(":")
                if not separator:
                    conn.commit()
                    return False
                session = conn.execute(
                    "SELECT created_at FROM sessions WHERE host=? AND session_name=?",
                    (generation_host, generation_name),
                ).fetchone()
                if session is None or str(session[0] or "") != expected_generation:
                    conn.commit()
                    return False
            cur = conn.execute(
                """UPDATE v2_routing_integrity SET notified_at=?
                   WHERE stream_id=? AND episode_id=? AND notified_at IS NULL""",
                (stamp, stream_id, episode_id),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def enqueue_outbound_notice(
        self,
        *,
        notice_id: str,
        kind: str,
        dedupe_key: str,
        recipient_stream_id: str,
        tell_id: str,
        body: str,
        source_stream_id: str | None = None,
        episode_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
        watch_fact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert the notice and satisfy subscriptions in the same transaction."""
        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            from store_watch_wake import coalesce_notice_conn
            with conn:
                notice = dict(notice_id=notice_id, kind=kind, dedupe_key=dedupe_key,
                    recipient_stream_id=recipient_stream_id, tell_id=tell_id, body=body,
                    source_stream_id=source_stream_id, episode_id=episode_id,
                    metadata=metadata, created_at=created_at)
                return coalesce_notice_conn(conn, notice, watch_fact)
        return await self.submit(_op)

    async def list_outbound_notice_ids(
        self,
        *,
        limit: int = 32,
        now: float | None = None,
        kinds: set[str] | None = None,
        force: bool = False,
    ) -> list[str]:
        stamp = time.time() if now is None else float(now)

        def _op(conn: sqlite3.Connection) -> list[str]:
            clauses = [
                "delivered_at IS NULL",
                "terminal_at IS NULL",
                "(lease_until IS NULL OR lease_until <= ?)",
            ]
            params: list[Any] = [stamp]
            if not force:
                clauses.append("next_attempt_at <= ?")
                params.append(stamp)
            if kinds:
                marks = ",".join("?" for _ in kinds)
                clauses.append(f"kind IN ({marks})")
                params.extend(sorted(kinds))
            params.append(max(1, int(limit)))
            rows = conn.execute(
                "SELECT notice_id FROM v2_outbound_notices WHERE "
                + " AND ".join(clauses)
                + " ORDER BY next_attempt_at ASC, created_at ASC LIMIT ?",
                params,
            ).fetchall()
            return [str(row[0]) for row in rows]

        return await self.submit(_op)

    async def claim_outbound_notice(
        self,
        notice_id: str,
        *,
        owner: str,
        lease_s: float,
        force: bool = False,
    ) -> dict[str, Any] | None:
        now = time.time()
        lease_until = now + max(0.1, float(lease_s))

        def _op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            due_clause = "" if force else " AND next_attempt_at <= ?"
            params: list[Any] = [owner, lease_until, notice_id, now]
            if not force:
                params.append(now)
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET lease_owner=?, lease_until=?, attempts=attempts+1
                   WHERE notice_id=? AND delivered_at IS NULL AND terminal_at IS NULL
                     AND (lease_until IS NULL OR lease_until <= ?)""" + due_clause,
                params,
            )
            if cur.rowcount != 1:
                conn.commit()
                return None
            conn.commit()
            row = conn.execute(
                "SELECT * FROM v2_outbound_notices WHERE notice_id=?", (notice_id,)
            ).fetchone()
            return None if row is None else dict(row)

        return await self.submit(_op)

    async def fail_outbound_notice(
        self,
        notice_id: str,
        *,
        owner: str,
        error: str,
        next_attempt_at: float,
        next_action: str,
    ) -> bool:
        def _op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET last_error=?, next_attempt_at=?, next_action=?,
                       lease_owner=NULL, lease_until=NULL
                   WHERE notice_id=? AND lease_owner=?
                     AND delivered_at IS NULL AND terminal_at IS NULL""",
                (error[:400], float(next_attempt_at), next_action[:400], notice_id, owner),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def complete_outbound_notice(self, notice_id: str, *, owner: str) -> bool:
        stamp = _routing_iso_now()

        def _op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET delivered_at=?, last_error=NULL, next_action=NULL,
                       lease_owner=NULL, lease_until=NULL
                   WHERE notice_id=? AND lease_owner=?
                     AND delivered_at IS NULL AND terminal_at IS NULL""",
                (stamp, notice_id, owner),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

    async def terminal_outbound_notice(
        self,
        notice_id: str,
        *,
        owner: str,
        reason: str,
        next_action: str,
    ) -> bool:
        stamp = _routing_iso_now()

        def _op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                """UPDATE v2_outbound_notices
                   SET terminal_at=?, terminal_reason=?, last_error=?, next_action=?,
                       lease_owner=NULL, lease_until=NULL
                   WHERE notice_id=? AND lease_owner=?
                     AND delivered_at IS NULL AND terminal_at IS NULL""",
                (stamp, reason[:400], reason[:400], next_action[:400], notice_id, owner),
            )
            conn.commit()
            return cur.rowcount == 1

        return await self.submit(_op)

def _routing_iso_now() -> str:
    """UTC stamp with sub-second precision for recurring observations."""
    now = time.time()
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{whole}.{int(now * 1_000_000) % 1_000_000:06d}Z"

def _outbound_notice_digest(
    kind: str,
    dedupe_key: str,
    recipient_stream_id: str,
    tell_id: str,
    body: str,
    metadata_json: str,
    source_stream_id: str,
    episode_id: str,
) -> str:
    payload = "\x00".join(
        (
            kind,
            dedupe_key,
            recipient_stream_id,
            tell_id,
            body,
            metadata_json,
            source_stream_id,
            episode_id,
        )
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _routing_episode_id(
    conn: sqlite3.Connection,
    *,
    stream_id: str,
    session_generation: str,
    provider: str,
    requested_model: str,
    requested_effort: str,
    effective_model: str | None,
    effective_effort: str | None,
    first_observed: str,
) -> str:
    """Derive a replay-stable episode id, suffixing only a real collision."""
    material = "\x1f".join(
        (
            stream_id,
            session_generation,
            provider,
            requested_model,
            requested_effort,
            str(effective_model or ""),
            str(effective_effort or ""),
            first_observed,
        )
    )
    base = "routing-drift-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    candidate = base
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM v2_routing_integrity_audit WHERE episode_id=? LIMIT 1",
        (candidate,),
    ).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
