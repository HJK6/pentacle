"""ingest.py — B13 supervised per-stream transcript ingest tailer.

Turns each open LOCAL session's provider transcript (`.jsonl`) into durable
`session_event_tail` rows + `chat.event` pushes, so mobile/desktop chat views
render live turns. Without this the read path (`request_stream_events`) only ever
serves pre-cutover history — the top user-facing gap after v2 went live on prod.

LIFTED (operator lift-first directive 2026-08-05, `v1_code_reuse_map.md` lane 8):
  - the record→payload parsing/shapes, verbatim, in `claude_jsonl_norm.py`;
  - the durable dedup identity (`_jsonl_event_identity`), from v1 chat_streamd;
  - the append→broadcast-iff-inserted ordering (v1 `accept_event`): the store's
    INSERT OR IGNORE is the single writer, and `chat.event` fires only on a real
    insert, so a duplicate never re-broadcasts.

REDESIGNED I/O PLACEMENT (the diseased part the ledger's B13 names):
  - NO per-file `tail -F` thread with parent-held stdin (v1's remote-tail wedge,
    where a waiter exited but the outer `cat` blocked forever). Instead a single
    bounded-cadence pump reads new bytes off the event loop (`asyncio.to_thread`)
    and writes through `store.py`'s thread — no blocking syscall on the loop.
  - SUPERVISED PER-STREAM: each stream carries its own bind/offset/backoff, and
    one stream's failure is caught and backed off individually — it never fails
    the pass or starves the others (B13's core requirement). A dead/rebound
    transcript is unbound and re-discovered; replay-from-start is exactly-once
    because the durable identity dedups (ledger B13 "replay-from-start with
    durable dedupe").
  - FOUR LOOP RULES (event-loop rule 2): cadence, per-pass event cap, exponential
    backoff, kill switch `--disable-ingest`.

Remote sessions are OUT OF SCOPE: their events are ingested when that host runs
its own daemon. This pump only tails LOCAL panes; it never SSH-tails.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable

from claude_jsonl_norm import normalize_claude_jsonl_records
from codex_rollout_norm import codex_is_guardian_review, codex_session_identity, normalize_codex_rollout_record
from context_adapters import parse_codex_context_reading
from message_envelopes import annotate_message_envelope
from prockill import canonical_start, process_tree, process_record, pid_alive
from error_adapters import ErrorFact
from store import ENTRY_DROPPED
from tmux_transport import TRANSCRIPT_DIRS, _exec
from provider_accounts import transcript_allowed
from usage_accounting import native_provenance
from usage_provenance import MAX_ITEMS as PROVENANCE_MAX_ITEMS, PAYLOAD_VERSION as PROVENANCE_VERSION
from v2_runtime import env_number
import ingest_checkpoint as checkpoint

log = logging.getLogger("chat_streamd_v2.ingest")

DEFAULT_INTERVAL_S = 1.5
#: Per-pass work cap (loop rule 2): at most this many newly-appended events per
#: pass. Rotating the starting stream prevents a replay from monopolizing
#: successive passes; the remainder is picked up on its next rotation.
DEFAULT_MAX_EVENTS_PER_PASS = 500
#: Cap the bytes read from one transcript per pass so a multi-GB log never lands
#: on the loop thread in one gulp; the tail advances over successive passes.
DEFAULT_MAX_READ_BYTES = 4 * 1024 * 1024
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0

ENV_PREFIX = "PENTACLE_INGEST_"


@dataclass
class IngestConfig:
    """Loop-rule knobs (event-loop rule 2), mirroring MirrorConfig.

    cadence      `interval_s`, default 1.5s, env `PENTACLE_INGEST_INTERVAL_S`
    per-pass cap `max_events_per_pass` newly-appended events across all streams
    backoff      exponential PER STREAM from `backoff_base_s`, capped at max
    kill switch  `--disable-ingest` (main.py never constructs the job)
    """

    interval_s: float = DEFAULT_INTERVAL_S
    #: None => wait a full interval before the first pass. Tests set a fraction of
    #: a second; it is the "forced pass" trigger for a real daemon process, so no
    #: test-only wire verb has to exist (same convention as MirrorConfig).
    first_delay_s: float | None = None
    max_events_per_pass: int = DEFAULT_MAX_EVENTS_PER_PASS
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S

    @classmethod
    def from_env(cls, env: dict | None = None) -> "IngestConfig":
        e = os.environ if env is None else env
        return cls(
            interval_s=env_number(e, "INTERVAL_S", DEFAULT_INTERVAL_S, float, prefix=ENV_PREFIX),
            first_delay_s=env_number(e, "FIRST_DELAY_S", None, float, prefix=ENV_PREFIX),
            max_events_per_pass=env_number(
                e, "MAX_EVENTS", DEFAULT_MAX_EVENTS_PER_PASS, int, prefix=ENV_PREFIX,
            ),
            max_read_bytes=env_number(
                e, "MAX_READ_BYTES", DEFAULT_MAX_READ_BYTES, int, prefix=ENV_PREFIX,
            ),
        )


@dataclass
class _IdentityGrace:
    key: tuple[str, str, str, str]
    since: float
    attempts: int = 0


@dataclass
class _StreamIngest:
    """Per-stream supervised tail state — the unit B13 isolates so one sick
    stream never kills the pump. Only the proven complete frontier and necessary
    continuation are persisted; supervision and stored projections stay fresh."""

    generation: str = ""
    selected_file_id: tuple[int, int] | None = None
    path: str = ""            # bound transcript path, "" until discovered
    offset: int = 0           # byte offset of the last COMPLETE line consumed
    session_id: str = ""      # bound provider session id (identity guard)
    #: (device, inode, link count) of the bound file. A size shrink is not the only way a
    #: transcript is replaced — swapping in a LARGER file leaves the offset
    #: mid-stream, so the span carries no session header and the identity guard
    #: never fires. Comparing file identity catches the replacement regardless
    #: of which direction the size moved.
    file_id: tuple[int, int, int] | None = None
    fd: int | None = None
    activity_restored: bool = False
    failures: int = 0         # consecutive failures → this stream's backoff
    next_attempt_monotonic: float = 0.0  # earliest retry time (per-stream backoff)
    #: Cross-span provenance context (Codex identity, turn models); reset on rebind.
    provenance_state: dict = field(default_factory=dict)
    cursor_norm_state: dict = field(default_factory=dict)
    cursor_line: int = 0
    identity_incident_id: str | None = None
    identity_grace: _IdentityGrace | None = None
    # Coalesce entry/late refusals; eligibility advances only at attempt completion.
    identity_refusal: tuple[dict, dict] | None = None
    replay_boundary: int | None = None
    replay_complete: bool = False
    replay_pending: bool = False
    resume_key: tuple | None = None
    resume_reason: str = "unbound"
    checkpoint_hash: Any = field(default_factory=hashlib.sha256)
    checkpoint_offset: int = 0
    checkpoint_span: tuple[int, bytes] | None = None
    checkpoint_capped: bool = False
    checkpoint_hold: str = ""
    checkpoint_last: dict = field(default_factory=dict)
    provenance_pass: tuple | None = None


def _jsonl_event_identity(payload: dict) -> tuple:
    """Source-independent durable dedup identity. A record's `jsonl_record_uuid` +
    event index is the stable key; text-hash fallback when a record has none."""
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    stream_id = str(payload.get("stream_id") or "")
    provider = str(payload.get("provider") or "")
    kind = str(payload.get("kind") or "")
    from claude_jsonl_norm import is_reserved_seat_injection
    # These native user-role injections used USER in existing immutable history.
    # Preserve that identity component on replay; only newly inserted rows use
    # SYSTEM presentation. No old row is rewritten or re-broadcast.
    if (kind == "SYSTEM" and provider in {"claude", "codex"}
            and raw.get("subtype") in {"synthetic-user", "queued-command"}
            and is_reserved_seat_injection(str(payload.get("text") or ""))):
        kind = "USER"
    record_uuid = raw.get("jsonl_record_uuid")
    if record_uuid is not None:
        return (stream_id, provider, str(record_uuid), raw.get("jsonl_event_index"), kind)
    fallback = (
        raw.get("message_id")
        or raw.get("request_id")
        or raw.get("tool_use_id")
        or hashlib.sha256(str(payload.get("text") or "").encode("utf-8")).hexdigest()
    )
    return (stream_id, provider, kind, str(payload.get("timestamp") or ""),
            raw.get("jsonl_event_index"), str(fallback))


def _identity_key(payload: dict) -> str:
    """Stable string form of the identity, for the durable unique index."""
    return json.dumps(list(_jsonl_event_identity(payload)), separators=(",", ":"))


def validate_event_payload(payload: Any, frame_host: str) -> str | None:
    """Reject malformed or unproven Codex source claims before any append.

    Both active Codex ingress routes use this same admission check.  The
    source-pane proof remains in-flight only; lifecycle authority is acquired
    separately from the existing store boundary and is never added to an event.
    """
    if not isinstance(payload, dict):
        return "bad_event"
    stream_id = payload.get("stream_id")
    provider = payload.get("provider")
    if not isinstance(stream_id, str) or not stream_id or provider not in {"claude", "codex"}:
        return "bad_event"
    host, separator, session_name = stream_id.partition(":")
    if not separator or not host or not session_name:
        return "bad_event"
    if provider != "codex":
        return None
    if host != frame_host:
        return "codex_identity_unproven"
    payload_host = payload.get("host")
    if payload_host is not None and payload_host != host:
        return "codex_identity_unproven"
    payload_name = payload.get("session_name")
    if payload_name is not None and payload_name != session_name:
        return "codex_identity_unproven"
    session_id = str(payload.get("session_id") or "").strip()
    raw = payload.get("raw")
    raw_session_id = (
        str(raw.get("source_session_identity") or "").strip()
        if isinstance(raw, dict) else ""
    )
    if not session_id or session_id != raw_session_id:
        return "codex_identity_unproven"
    return None


def codex_source_pane_pid(value: Any) -> str | None:
    """Normalize the ephemeral source-pane proof shared by both ingress routes."""
    if isinstance(value, bool):
        return None
    text = str(value or "").strip()
    if not text.isdecimal() or int(text) <= 0:
        return None
    return str(int(text))


Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


async def broadcast_assistant_mirror(
    store: Any, broadcast: Broadcast, source_event_id: int, *, sessions: Any = None,
) -> None:
    """Fan out the already-committed canonical projection of one source row.

    The mirror row lands on the composite stream without touching the session
    registry, so it advances that stream's preview here.
    """
    event = await store.assistant_mirror_event_for_source(source_event_id)
    if event is not None:
        if sessions is not None:
            sessions.apply_genuine_activity_event(str(event.get("stream_id") or ""), event)
        await broadcast({"type": "chat.event", "event": event})


async def append_ingested_event(
    store: Any,
    broadcast: Broadcast,
    payload: dict[str, Any],
    *,
    recent_limit: int,
    routing_integrity: Any = None,
    lifecycle: dict[str, Any] | None = None,
    sessions: Any = None,
    _segments_prepared: bool = False,
    on_incomplete: Callable[[str], None] | None = None,
) -> int | None:
    """Append one event under its raw identity, stamping durable receipt metadata."""
    if not _segments_prepared and hasattr(store, "prepare_native_input_events"):
        segments = await store.prepare_native_input_events([payload])
        last = None
        for segment in segments:
            seq = await append_ingested_event(store, broadcast, segment, recent_limit=recent_limit,
                routing_integrity=routing_integrity, lifecycle=lifecycle, sessions=sessions,
                _segments_prepared=True, on_incomplete=on_incomplete)
            if seq is not None:
                last = seq
        return last
    stream_id = str(payload.get("stream_id") or "")
    raw_identity = _identity_key(payload)
    corrected = annotate_message_envelope(
        await store.stamp_event_with_send_receipt(payload),
    )
    if lifecycle is None:
        seq = await store.append_session_event(
            stream_id, corrected, identity=raw_identity, limit=recent_limit,
        )
    else:
        sequences = await store.append_session_events_lifecycle_cas([
            {"stream_id": stream_id, "event": corrected, "identity": raw_identity, "lifecycle": lifecycle},
        ], limit=recent_limit)
        if sequences is None or sequences[0] is ENTRY_DROPPED:
            if on_incomplete is not None:
                on_incomplete("lifecycle_refused")
            return None
        seq = sequences[0]
    if routing_integrity is not None:
        try:
            observation = await routing_integrity.observe_claude_event(corrected)
            if isinstance(observation, dict) and observation.get("incomplete_effect") and on_incomplete is not None:
                on_incomplete(observation["incomplete_effect"])
        except Exception as exc:  # noqa: BLE001 - ingest must keep accepting
            if on_incomplete is not None:
                on_incomplete("routing_observation")
            log.warning("routing-integrity Claude observation failed sid=%s: %s", stream_id, exc)
    if seq is not None:
        projected = await store.project_session_events([
            {**corrected, "daemon_seq": seq},
        ])
        if sessions is not None:
            sessions.apply_genuine_activity_event(
                stream_id, projected[0],
                expected_generation=lifecycle["generation"] if lifecycle else None,
            )
        await broadcast({"type": "chat.event", "event": projected[0]})
        await broadcast_assistant_mirror(store, broadcast, seq, sessions=sessions)
    return seq


class Ingest:
    """Bounded-cadence, supervised-per-stream ingest of open LOCAL transcripts."""

    def __init__(
        self,
        store: Any,
        sessions: Any,
        tmux: Any,
        broadcast: Broadcast,
        *,
        local_host: str,
        recent_limit: int,
        config: IngestConfig | None = None,
        routing_integrity: Any = None,
        inventory_emitter: Any = None,
        provenance: Any = None,
        alerts: Any = None,
        started_monotonic: float | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.tmux = tmux
        #: `usage_provenance.ProvenanceSink` shared with event.push (metadata only).
        self.provenance = provenance
        self.alerts = alerts
        self.broadcast = broadcast
        self.local_host = local_host
        self.recent_limit = recent_limit
        self.config = config or IngestConfig()
        self.routing_integrity = routing_integrity
        self.inventory_emitter = inventory_emitter
        self._streams: dict[str, _StreamIngest] = {}
        self._last_visited_sid: str | None = None
        self._previous_inventory: tuple[str, ...] = ()
        self._started_monotonic = monotonic() if started_monotonic is None else started_monotonic
        self.force_full_replay = os.environ.get(ENV_PREFIX + "FORCE_FULL_REPLAY") == "1"

    def _hold_position(self, sid, st, reason):
        if not st.checkpoint_hold:
            st.checkpoint_hold = reason
            log.warning("ingest_position_held sid=%s offset=%d reason=%s", sid, st.checkpoint_offset, reason)

    async def _prepare_position(self, row, st, provider, size):
        key = (st.path, st.file_id, st.session_id, st.generation)
        if st.resume_key == key and size >= st.checkpoint_offset and st.offset >= st.checkpoint_offset:
            return
        prior_key = st.resume_key
        st.resume_key = key
        st.checkpoint_hash = hashlib.sha256()
        st.checkpoint_offset = 0
        st.checkpoint_last = {}
        st.checkpoint_hold = ""
        if prior_key is not None:
            st.offset = 0
            st.provenance_state.clear()
            if self.routing_integrity is not None:
                getattr(self.routing_integrity, "_claude_tuples", {}).pop(row["stream_id"], None)
        reason = "missing"
        try:
            if self.force_full_replay:
                raise ValueError("forced_full_replay")
            if provider not in {"claude", "codex"}:
                raise ValueError("unsupported_provider")
            loader = getattr(self.store, "load_ingest_position", None)
            if loader is None:
                raise ValueError("store_unavailable")
            saved = await loader(row["stream_id"])
            position, provenance, routing = checkpoint.decode(saved)
            expected = {"stream_id": row["stream_id"], "generation": st.generation,
                        "lifecycle": str(row.get("created_at") or ""), "provider": provider,
                        "session_id": st.session_id, "path": st.path, "file_id": list(st.file_id)}
            for field, value in expected.items():
                if position[field] != value:
                    raise ValueError(field)
            digest, elapsed = await asyncio.to_thread(checkpoint.verify_prefix, st.path, st.fd, position)
            tuples = getattr(self.routing_integrity, "_claude_tuples", None)
            if routing is not None and not isinstance(tuples, dict):
                raise ValueError("routing_state_unavailable")
            if isinstance(tuples, dict):
                if routing is None:
                    tuples.pop(row["stream_id"], None)
                else:
                    tuples[row["stream_id"]] = routing
            st.offset = st.checkpoint_offset = position["offset"]
            st.checkpoint_hash = digest
            st.provenance_state = provenance
            st.checkpoint_last = saved
            reason = "resumed"
            log.info("ingest_prefix_verified sid=%s bytes=%d elapsed_s=%.6f mb_s=%.3f",
                     row["stream_id"], st.offset, elapsed, st.offset / max(elapsed, 1e-9) / 1e6)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "unreadable"
        except Exception:  # A checkpoint is optional; ordinary intake stays available.
            reason = "unreadable"
        st.resume_reason = reason
        log.info("ingest_position sid=%s reason=%s offset=%d", row["stream_id"], reason, st.offset)

    async def _finish_position(self, row, st):
        span = st.checkpoint_span
        if span is None or self.force_full_replay or row.get("provider") not in {"claude", "codex"}:
            return
        start, consumed = span
        if st.offset <= start:
            return
        if st.checkpoint_offset != start or st.offset > start + len(consumed):
            self._hold_position(row["stream_id"], st, "hash_frontier")
            return
        # Never re-read for saving: these are the exact bytes normalized above.
        st.checkpoint_hash.update(consumed[:st.offset - start])
        st.checkpoint_offset = st.offset
        if st.checkpoint_capped or st.checkpoint_hold:
            return
        position = {"version": checkpoint.FORMAT_VERSION, "stream_id": row["stream_id"],
                    "generation": st.generation, "lifecycle": str(row.get("created_at") or ""),
                    "provider": row["provider"], "session_id": st.session_id,
                    "path": st.path, "file_id": list(st.file_id), "offset": st.offset,
                    "digest": st.checkpoint_hash.hexdigest()}
        routing = getattr(self.routing_integrity, "_claude_tuples", {}).get(row["stream_id"])
        try:
            saved = {"position_json": checkpoint.encode(position),
                     "provenance_json": checkpoint.encode(st.provenance_state),
                     "routing_json": checkpoint.encode(routing)}
            checkpoint.decode(saved)  # Same shape/size contract on both sides.
            changes = {key: value for key, value in saved.items() if st.checkpoint_last.get(key) != value}
            saver = getattr(self.store, "save_ingest_position", None)
            if saver is None:
                return
            started = monotonic()
            if not await saver(row, changes):
                self._hold_position(row["stream_id"], st, "save_lifecycle_refused")
                return
            st.checkpoint_last = saved
            log.debug("ingest_position_saved sid=%s offset=%d bytes=%d elapsed_s=%.6f",
                      row["stream_id"], st.offset, sum(len(v.encode("utf-8")) for v in changes.values()),
                      monotonic() - started)
        except Exception as exc:
            # No cache advancement on rollback. The earlier checkpoint remains
            # a complete frontier, even after many further successful passes.
            if isinstance(exc, ValueError) and str(exc) == "state_too_large":
                await self.store.invalidate_ingest_position(row["stream_id"], st.generation)
                st.checkpoint_last = {}
                self._hold_position(row["stream_id"], st, "state_too_large")
            else:
                self._hold_position(row["stream_id"], st, "save_failed:" + type(exc).__name__)

    def _replay_backlog(self) -> bool:
        now = monotonic()
        return any(st.replay_pending and not st.replay_complete and st.replay_boundary is not None
                   and st.offset < st.replay_boundary and now >= st.next_attempt_monotonic
                   for st in self._streams.values())

    def _complete_replay(self, sid, st, *, disposition="consumed") -> None:
        if st.replay_complete or st.replay_boundary is None:
            return
        st.replay_complete = True
        log.info("ingest_replay_complete %s", json.dumps({
            "stream_id": sid, "generation": st.generation,
            "elapsed_s": round(monotonic() - self._started_monotonic, 6),
            "boundary": st.replay_boundary, "offset": st.offset,
            "disposition": disposition,
            "resume_reason": st.resume_reason,
        }, separators=(",", ":")))

    async def _record_provenance(self, sid: str, provider: str, records: list[dict], st: _StreamIngest) -> None:
        """Thoth-local provenance through the same sink as satellites; best effort."""
        if self.provenance is None or not records:
            return
        try:
            bound = (st.path, st.session_id)
            if st.provenance_state.get("_bound") != bound:
                # Rebind (new file or native session) within one generation.
                st.provenance_state.clear()
                st.provenance_state["_bound"] = bound
            items = native_provenance(
                provider, records, native_session_id=st.session_id or None,
                state=st.provenance_state, proof=True,
            )
            for start in range(0, len(items), PROVENANCE_MAX_ITEMS):
                result = await self.provenance.admit(self.local_host, {
                    "version": PROVENANCE_VERSION,
                    "items": items[start:start + PROVENANCE_MAX_ITEMS],
                })
                if isinstance(result, dict) and (result.get("error") or result.get("rejected")
                        or (result.get("counts") or {}).get("history_unavailable")):
                    self._hold_position(sid, st, "provenance_rejected")
        except Exception as exc:  # noqa: BLE001 - backfill is the authority; ingest continues
            self._hold_position(sid, st, "provenance_observation")
            log.warning("usage provenance failed sid=%s: %s", sid, exc)

    def _align_provenance(self, st):
        """Whole-span effects may precede the cap; carried state may not.

        Recompute only the continuation, using already parsed consumed records.
        This adds no Store work and leaves whole-span passes unchanged.
        """
        span = st.provenance_pass
        st.provenance_pass = None
        if span is None:
            return
        before, provider, records, ends, start, end = span
        if st.offset == end:
            return
        st.provenance_state = before
        if st.offset <= start:
            return
        consumed = [record for record, offset in zip(records, ends) if offset <= st.offset]
        if not consumed:
            return
        bound = (st.path, st.session_id)
        if before.get("_bound") != bound:
            before.clear()
            before["_bound"] = bound
        native_provenance(provider, consumed, native_session_id=st.session_id or None,
                          state=before, proof=True)

    # -- loop --------------------------------------------------------------

    async def run_forever(self) -> None:
        """Cadence + pump-level backoff. Cancelled at shutdown; never swallows
        CancelledError. Per-stream failures are handled INSIDE run_pass and do
        not reach here — this backoff is only for a whole-pass fault (e.g. the
        registry read itself failing)."""
        cfg = self.config
        delay = cfg.interval_s if cfg.first_delay_s is None else cfg.first_delay_s
        failures = 0
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self.run_pass()
                failures = 0
                # sleep(0) still yields between bounded round-robin passes.
                delay = 0 if self._replay_backlog() else cfg.interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad pass never kills the pump
                failures += 1
                delay = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (failures - 1)))
                log.warning("ingest pass failed (%d): %s; retrying in %.0fs", failures, exc, delay)

    async def run_pass(self) -> int:
        """One bounded ingest pass. Returns the number of events newly appended.
        Also the test-only forced trigger (called directly).

        For a fixed finite inventory of N continuously eligible streams, each
        gets an attempt within N completed passes while this actor runs. Churn,
        restart and whole-pass backoff do not inherit that bound; neither does
        the time to reach a prompt buried in a stream's own replay backlog.
        """
        cfg = self.config
        rows = [
            r for r in self.sessions.list_open()
            if str(r.get("host") or "") == self.local_host
            and str(r.get("provider") or "") != "composite"
        ]
        open_ids = {r["stream_id"] for r in rows}
        # Forget tail state for streams no longer open (membership follows the
        # O(open) registry, never a filesystem scan).
        for sid in list(self._streams):
            if sid not in open_ids:
                stale = self._streams.pop(sid, None)
                if stale is not None:
                    _close_stream(stale)

        # Scheduling state uses identities, not indices into a mutable registry.
        # If the cursor disappeared, follow its previous cyclic successors to
        # the first survivor, rather than restarting at the inventory head.
        ids = tuple(r["stream_id"] for r in rows)
        start_sid = None
        if self._last_visited_sid in open_ids:
            start_sid = ids[(ids.index(self._last_visited_sid) + 1) % len(ids)]
        elif self._last_visited_sid in self._previous_inventory:
            prior = self._previous_inventory
            after = prior.index(self._last_visited_sid) + 1
            start_sid = next(
                (sid for sid in prior[after:] + prior[:after] if sid in open_ids),
                None,
            )
        self._previous_inventory = ids
        if start_sid is not None:
            start = ids.index(start_sid)
            rows = rows[start:] + rows[:start]

        now = asyncio.get_running_loop().time()
        budget = cfg.max_events_per_pass
        appended = 0
        for row in rows:
            if budget <= 0:
                break
            sid = row["stream_id"]
            # A backoff skip is a visit, not an ingest attempt. It must still
            # advance the frontier, as must zero inserts and handled failures.
            self._last_visited_sid = sid
            st = self._streams.setdefault(sid, _StreamIngest())
            if now < st.next_attempt_monotonic:
                continue  # this stream is in its own backoff window
            try:
                n = await self._ingest_stream(row, st, budget)
            except Exception as exc:  # noqa: BLE001 - one sick stream never kills the pump
                st.failures += 1
                back = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (st.failures - 1)))
                st.next_attempt_monotonic = now + back
                log.warning("ingest stream failed sid=%s (%d): %s; backing off %.0fs",
                            sid, st.failures, exc, back)
            else:
                st.failures = 0
                budget -= n
                appended += n
        return appended

    # -- per-stream ingest -------------------------------------------------

    async def _ingest_stream(self, row: dict[str, Any], st: _StreamIngest, budget: int) -> int:
        st.identity_refusal = None
        st.checkpoint_span = None
        st.checkpoint_capped = False
        st.provenance_pass = None
        previous_offset = st.offset
        st.replay_pending = False
        try:
            appended = await self._ingest_stream_events(row, st, budget)
        except BaseException:
            if st.checkpoint_span is not None:
                self._hold_position(row["stream_id"], st, "pass_incomplete")
            st.checkpoint_span = None
            raise
        finally:
            self._align_provenance(st)
        st.replay_pending = st.offset > previous_offset
        # A strict discovery/pre-append check can fail after entry was proven.
        # Surface that refusal at this pass's end, without consuming any bytes
        # after a late recovery; ordinary replay resumes on the next pass.
        if (appended == 0 and st.identity_incident_id is None
                and isinstance(row.get("observer_binding"), dict)):
            current = self.sessions.get(row["stream_id"]) or row
            if (current.get("status") == "open"
                    and current.get("session_generation") == row.get("session_generation")
                    and isinstance(current.get("observer_binding"), dict)):
                _, name = self.sessions.split(row["stream_id"])
                if await self._provider_root(current, name) is None:
                    observation = {"start": "", "reason": "proof_unavailable"}
                    try:
                        current = await self._maybe_rebind_observer_pane(current, name, observation=observation)
                    except Exception:  # noqa: BLE001 - missing proof stays a refusal
                        observation["reason"] = "proof_unavailable"
                    if await self._provider_root(current, name, observation=observation) is None:
                        _close_stream(st)
                        await self._record_identity_blocked(current, st, observation)
                    else:
                        st.identity_refusal = None  # proven late recovery
                else:
                    st.identity_refusal = None
        refusal = st.identity_refusal
        st.identity_refusal = None
        if refusal is not None:
            failed_row, observation = refusal
            await self._record_identity_blocked(failed_row, st, observation, completed_attempt=True)
        else:
            st.identity_grace = None
        if refusal is None and st.fd is not None:
            await self._finish_position(row, st)
        st.checkpoint_span = None
        if st.replay_boundary is not None and st.offset >= st.replay_boundary:
            self._complete_replay(row["stream_id"], st)
        return appended

    async def _ingest_stream_events(self, row: dict[str, Any], st: _StreamIngest, budget: int) -> int:
        """Tail one LOCAL stream's transcript for new events. Returns the count
        newly appended (0 when nothing changed). Off-loop file reads; store-thread
        writes; broadcast iff the durable append inserted (v1 accept_event order)."""
        sid = row["stream_id"]
        host, name = self.sessions.split(sid)
        generation = str(row.get("session_generation") or row.get("created_at") or "")
        if st.generation != generation:
            _close_stream(st)
            st.__dict__.update(_StreamIngest(generation=generation).__dict__)
        binding = row.get("observer_binding")
        if isinstance(binding, dict):
            if await self._provider_root(row, name) is None:
                # An in-place provider replacement leaves the binding PID stale
                # within the same generation; repair it against an independently
                # proven same-rollout pane before giving up, then re-run the proof.
                observation = {"start": "", "reason": "proof_unavailable"}
                try:
                    row = await self._maybe_rebind_observer_pane(row, name, observation=observation)
                except Exception:  # noqa: BLE001 - unavailable proof never authorizes a binding
                    observation["reason"] = "proof_unavailable"
                binding = row.get("observer_binding")
                if await self._provider_root(row, name, observation=observation) is None:
                    _close_stream(st)
                    await self._record_identity_blocked(row, st, observation)
                    return 0
            if st.identity_incident_id:
                log.info("ingest strict root restored incident=%s", st.identity_incident_id,
                         extra={"subsystem": "ingest", "bug_ref": "issue_201"})
                st.identity_incident_id = None
        if not st.activity_restored:
            restore = getattr(self.sessions, "restore_genuine_activity", None)
            if callable(restore):
                events = await self.store.fetch_session_event_tail(sid, limit=64)
                restore(sid, events)
            st.activity_restored = True
        provider = str(row.get("provider") or _provider_from_session_name(name) or "claude")
        # Codex rollouts carry a different record schema, so they get their own
        # normalizer (`codex_rollout_norm`) — the claude normalizer is never
        # pointed at them. Any third provider still short-circuits rather than
        # being parsed by a normalizer that was not written for it.
        if provider not in ("claude", "codex", "gemini", "cursor"):
            return 0
        if provider == "cursor" and st.cursor_norm_state.get("halted"):
            return 0

        if not st.path:
            path = await self._discover_path(row, host, name, state=st)
            if not path:
                return 0
            st.path = path
            st.offset = 0  # replay-from-start; durable identity makes it exactly-once

        if st.fd is None:
            st.fd = await asyncio.to_thread(_open_descriptor, st.path)
            if st.fd is None:
                st.path = ""
                st.offset = 0
                st.file_id = None
                return 0

        try:
            stat = await asyncio.to_thread(_file_stat, st.path)
        except OSError:
            _close_stream(st)
            # Bound went stale (file gone / permissions) — unbind and re-discover
            # next pass rather than wedging on a dead path (B13 dead-task unbind).
            st.path = ""
            st.offset = 0
            st.file_id = None
            return 0
        if stat is None:
            _close_stream(st)
            st.path = ""
            st.offset = 0
            st.file_id = None
            return 0
        size, file_id = stat
        if st.selected_file_id is not None:
            opened = await asyncio.to_thread(os.fstat, st.fd)
            if file_id[:2] != st.selected_file_id or (opened.st_dev, opened.st_ino) != st.selected_file_id:
                _close_stream(st)
                st.path = ""
                st.offset = 0
                st.file_id = None
                st.selected_file_id = None
                return 0
        if st.file_id is None:
            st.file_id = file_id
        elif st.file_id != file_id:
            self._complete_replay(sid, st, disposition="rotated")
            # The bound path now names a DIFFERENT file. A size check alone
            # misses this whenever the replacement is larger: the stale offset
            # would land mid-file, the span would carry no session header, and
            # the identity guard would never see a record to reject — so another
            # session's turns would be appended under this stream_id. Unbind and
            # re-discover; `session_id` is deliberately KEPT so the re-bound
            # file's own header must still match it.
            _close_stream(st)
            st.path = ""
            st.offset = 0
            st.file_id = None
            return 0
        # Fallback authority survives a writer gap/restart in the lifecycle row.
        # Legacy launch-recorded paths keep their existing native identity guard.
        # A launch-recorded Claude row is also anchored once, best effort, so a
        # start-presentation shift can be repaired (issue 201; approach proposed
        # by JOI, @Mchighlight): only for the recorded path, read through a
        # descriptor that is still that path's file, carrying the recorded id.
        claude_anchor = ""
        if (isinstance(binding, dict) and provider == "claude" and row.get("jsonl_path")
                and binding.get("transcript") is None and row.get("claude_session_id")
                and st.path == row.get("jsonl_path")):
            opened = await asyncio.to_thread(os.fstat, st.fd)
            if (opened.st_dev, opened.st_ino) == file_id[:2]:
                native = await asyncio.to_thread(_native_session_identity_from_fd, st.fd, provider)
                if native == row.get("claude_session_id"):
                    claude_anchor = native
        if isinstance(binding, dict) and (not row.get("jsonl_path") or claude_anchor):
            native = claude_anchor or await asyncio.to_thread(_native_session_identity_from_fd, st.fd, provider)
            if not native:
                return 0
            transcript = {"path": st.path, "provider": provider, "session_id": native,
                          "file_id": list(file_id[:2])}
            prior = binding.get("transcript")
            if prior is not None and prior != transcript:
                _close_stream(st)
                return 0
            bound = (prior is None and await self._provider_root(row, name) is not None
                     and await self.store.bind_observer_transcript(
                         sid, generation=generation, pane_pid=str(row.get("pane_pid") or ""),
                         expected=binding, transcript=transcript,
                     ))
            if prior is None and not bound and not claude_anchor:
                return 0  # A first-bind probe cannot seed recovery during a mismatch.
            if bound:  # A refused Claude anchor stays optional: keep the legacy path.
                binding = {**binding, "transcript": transcript}
                self.sessions.apply_durable(sid, observer_binding=binding)
                log.info("observer transcript bound sid=%s generation=%s pid=%s",
                         sid, generation, row.get("pane_pid"), extra={
                             "subsystem": "ingest",
                             "bug_ref": "session_attribution_title_parity_2026_09",
                         })
                row = {**row, "observer_binding": binding}
            st.session_id = native
        elif not st.session_id and provider == "claude":
            st.session_id = str(row.get("claude_session_id") or "")
        if provider == "cursor":
            launch_binding = binding.get("cursor") if isinstance(binding, dict) else None
            if not transcript_allowed(st.path, "cursor", launch_binding):
                _close_stream(st)
                return 0
            header = await asyncio.to_thread(_cursor_header, st.fd)
            if (not launch_binding or not header or header.get("session_id") != launch_binding.get("session_id")
                    or header.get("chat_id") != launch_binding.get("chat_id")):
                _close_stream(st)
                log.warning("Cursor transcript identity refused", extra={"subsystem":"ingest", "bug_ref":"cursor_provider_identity"})
                return 0
            st.session_id = launch_binding["session_id"]
        if provider == "gemini":
            native = await asyncio.to_thread(_native_session_identity_from_fd, st.fd, provider)
            expected = str(row.get("claude_session_id") or st.session_id or "")
            if not native or (expected and native != expected):
                _close_stream(st)
                st.path, st.offset, st.file_id = "", 0, None
                log.warning("Gemini recording identity refused", extra={
                    "subsystem": "ingest", "bug_ref": "gemini_provider_identity",
                })
                return 0
            st.session_id = native
        if provider == "codex" and not st.session_id:
            native = await asyncio.to_thread(_codex_session_identity_from_fd, st.fd)
            if not native:
                return 0
            st.session_id = native
        await self._prepare_position(row, st, provider, size)
        if size < st.offset:
            # Rotation/truncation in place: replay from the start; dedup keeps
            # it exact.
            st.offset = 0
            self._complete_replay(sid, st, disposition="rotated")
        if st.replay_boundary is None:
            st.replay_boundary = await asyncio.to_thread(_complete_record_boundary, st.fd, size)
        if size == st.offset:
            if provider == "codex" and st.session_id:
                current_session_id = await asyncio.to_thread(
                    _codex_session_identity_from_fd, st.fd
                )
                if current_session_id != st.session_id:
                    _close_stream(st)
                    st.path = ""
                    st.offset = 0
                    st.file_id = None
                    return 0
            return 0

        read_to = min(size, st.offset + self.config.max_read_bytes)
        read = await asyncio.to_thread(
            _read_bound_span, st.path, st.fd, st.offset, read_to
        )
        if read is None:
            _close_stream(st)
            st.path = ""
            st.offset = 0
            st.file_id = None
            return 0
        chunk, consumed_id, path_id = read
        if consumed_id != st.file_id or path_id != st.file_id:
            _close_stream(st)
            st.path = ""
            st.offset = 0
            st.file_id = None
            return 0
        if provider == "codex":
            current_session_id = await asyncio.to_thread(
                _codex_session_identity_from_fd, st.fd
            )
            if not current_session_id or (st.session_id and current_session_id != st.session_id):
                _close_stream(st)
                st.path = ""
                st.offset = 0
                st.file_id = None
                return 0
            st.session_id = current_session_id
        # Consume only up to the last complete line; a partial trailing line is
        # left for the next pass (advance offset by the consumed byte count).
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return 0  # no complete line yet
        consumed = chunk[: last_nl + 1]
        st.checkpoint_span = (st.offset, consumed)
        new_offset = st.offset + len(consumed)

        records: list[dict] = []
        malformed_usage_span = False
        # File offset just past each kept record's line, so the Codex per-pass cap
        # can advance the offset to exactly the last record it processed. Byte
        # lengths come off the RAW bytes (`splitlines(keepends=True)`); decoding
        # with "replace" is not byte-reversible, so it cannot be measured after.
        record_end_offsets: list[int] = []
        cursor = st.offset
        cursor_line = st.cursor_line if st.offset else 0
        cursor_records = []
        for raw_line in consumed.splitlines(keepends=True):
            cursor += len(raw_line)
            cursor_line += 1
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed_usage_span = True
                continue
            if not isinstance(record, dict):
                malformed_usage_span = True
                continue
            # never ingest another session's transcript into this stream. Codex
            # states its identity ONCE, in `session_meta`, instead of stamping
            # every record — reading `sessionId` off a Codex line would leave the
            # guard silently disarmed on the majority of the fleet.
            if provider == "codex":
                rec_sid = codex_session_identity(record)
            elif provider == "cursor":
                rec_sid = ""  # Native chat UUID differs from the daemon session UUID.
                cursor_records.append((cursor_line, record))
            else:
                rec_sid = str(record.get("sessionId") or record.get("session_id") or "")
            if rec_sid:
                if not st.session_id:
                    st.session_id = rec_sid
                elif rec_sid != st.session_id:
                    _close_stream(st)
                    # A foreign record means the bound path is wrong — unbind.
                    st.path = ""
                    st.offset = 0
                    st.session_id = ""
                    st.file_id = None
                    return 0
            records.append(record)
            record_end_offsets.append(cursor)

        # Legacy rows can lack an authoritative provider while the existing
        # chat reader infers one from the session name. Keep that chat path;
        # accounting stays unknown until the durable provider is classified.
        if row.get("provider") in {"codex", "claude"}:
            if self.provenance is not None and records:
                st.provenance_pass = (checkpoint.snapshot_provenance(st.provenance_state),
                                      provider, records, record_end_offsets, st.offset, new_offset)
            usage = await self.store.record_usage(
                row, records, native_session_id=st.session_id,
                collection_host=self.local_host, malformed=malformed_usage_span,
            )
            if usage is None:
                return 0
            self.sessions.apply_durable(sid, usage=usage)
            await self._record_provenance(sid, str(row.get("provider")), records, st)

        if not records:
            st.offset = new_offset
            return 0

        if provider == "codex" and st.session_id and self.routing_integrity is not None:
            # Preserve every crossing in this bounded, identity-checked span.
            # Keeping only the final reading hides a compaction and subsequent
            # rise when both arrive before the next ingest/nudge pass.
            for record in records:
                reading = parse_codex_context_reading(record)
                if reading is None:
                    continue
                try:
                    await self.routing_integrity.observe_context(
                        host,
                        name,
                        provider="codex",
                        reading=reading,
                        observed_at=str(record.get("timestamp") or "") or None,
                        expected_generation=str(row.get("created_at") or "") or None,
                    )
                except Exception as exc:  # noqa: BLE001 - event ingest remains available
                    self._hold_position(sid, st, "context_observation")
                    log.warning("routing-integrity Codex context observation failed sid=%s: %s", sid, exc)

        if provider == "codex":
            # Local discovery supplies the same ephemeral pane proof that a
            # satellite supplies on event.push.  Refuse an unproven or stale
            # source before it can first-bind a transcript to this lifecycle.
            source_pid = codex_source_pane_pid(row.get("pane_pid"))
            if source_pid is None:
                return 0
            # Per-pass cap, mirroring the Claude path below: build at most
            # `budget` entries, then advance the offset PAST the records that
            # produced them. Counting built entries (not genuine inserts) is safe
            # only because the offset advances over the processed prefix — a
            # restart replays from offset 0, and advancing past each processed
            # record keeps the drain moving instead of re-reading the same capped
            # prefix forever (the freeze this lane fixes). The durable identity
            # makes the replay exactly-once; `max_read_bytes` still bounds bytes.
            entries: list[dict[str, Any]] = []
            capped = False
            consumed_to = st.offset
            for record, end_offset in zip(records, record_end_offsets):
                payloads = normalize_codex_rollout_record(
                    record, host=host, session_name=name, session_id=st.session_id,
                )
                payloads = await self.store.prepare_native_input_events(payloads)
                if payloads and len(entries) + len(payloads) > budget:
                    # Leave this record and the rest of the span for next pass.
                    capped = True
                    break
                for payload in payloads:
                    admission_payload = {**payload, "source_pane_pid": source_pid}
                    if validate_event_payload(admission_payload, host) is not None:
                        return 0
                    corrected = annotate_message_envelope(
                        await self.store.stamp_event_with_send_receipt(payload),
                    )
                    entries.append({
                        "stream_id": sid,
                        "event": corrected,
                        "identity": _identity_key(payload),
                        "lifecycle": None,
                    })
                consumed_to = end_offset
            if not capped:
                # No cap: the whole span is consumed, including any trailing
                # non-record bytes past the last event.
                consumed_to = new_offset
            st.checkpoint_capped = capped
            if not entries:
                st.offset = consumed_to
                return 0
            lifecycle = await self.store.fetch_open_session_lifecycle(sid, pane_pid=source_pid)
            if lifecycle is None or (row.get("session_generation") and lifecycle["generation"] != row["session_generation"]):
                return 0
            if isinstance(binding, dict) and await self._provider_root(row, name) is None:
                return 0
            for entry in entries:
                entry["lifecycle"] = lifecycle
            try:
                sequences = await self.store.append_session_events_lifecycle_cas(
                    entries, limit=self.recent_limit,
                )
            except Exception as exc:  # noqa: BLE001 - fail closed before broadcast
                self._hold_position(sid, st, "append_binding_promotion")
                log.warning("local Codex durable batch failed sid=%s: %s", sid, exc)
                return 0
            if sequences is None:
                return 0
            if any(seq is ENTRY_DROPPED for seq in sequences):
                # This local batch is one stream, so a per-entry predicate miss
                # the lines are re-read once a live pane rebinds the row.
                return 0
            for entry in entries:
                if self.routing_integrity is not None:
                    try:
                        await self.routing_integrity.observe_claude_event(entry["event"])
                    except Exception as exc:  # noqa: BLE001 - existing best-effort observer
                        self._hold_position(sid, st, "routing_observation")
                        log.warning("routing-integrity local observation failed sid=%s: %s", sid, exc)
            try:
                inserted = [
                    {**entry["event"], "daemon_seq": seq}
                    for entry, seq in zip(entries, sequences)
                    if seq is not None
                ]
                projected = await self.store.project_session_events(inserted)
                from store_capacity_retry import refresh_projection
                await refresh_projection(self.store, self.sessions, sid)
                projected_index = 0
                for entry, seq in zip(entries, sequences):
                    if seq is not None:
                        self.sessions.apply_genuine_activity_event(
                            sid, projected[projected_index], expected_generation=lifecycle["generation"],
                        )
                        await self.broadcast({
                            "type": "chat.event",
                            "event": projected[projected_index],
                        })
                        await broadcast_assistant_mirror(self.store, self.broadcast, seq, sessions=self.sessions)
                        projected_index += 1
                if any(seq is not None for seq in sequences) and self.inventory_emitter is not None:
                    await self.inventory_emitter.emit_if_changed()
            except Exception as exc:  # noqa: BLE001 - never claim the failed batch
                self._hold_position(sid, st, "projection_publication")
                log.warning("local Codex broadcast failed sid=%s: %s", sid, exc)
                return 0
            st.offset = consumed_to
            return sum(seq is not None for seq in sequences)
        source_pid = codex_source_pane_pid(row.get("pane_pid"))
        lifecycle = await self.store.fetch_open_session_lifecycle(sid, pane_pid=source_pid) if source_pid else None
        if lifecycle is None or (row.get("session_generation") and lifecycle["generation"] != row["session_generation"]):
            return 0
        if isinstance(binding, dict) and await self._provider_root(row, name) is None:
            return 0
        cursor_state = dict(st.cursor_norm_state) if st.offset else {}
        if provider == "cursor":
            from cursor_stream_norm import normalize_cursor_stream_records
            events = normalize_cursor_stream_records(cursor_records, host=host, session_name=name,
                session_id=st.session_id, state=cursor_state)
        elif provider == "gemini":
            from gemini_jsonl_norm import normalize_gemini_jsonl_records
            events = normalize_gemini_jsonl_records(
                records, host=host, session_name=name,
                default_session_id=st.session_id,
            )
        else:
            events = normalize_claude_jsonl_records(records, host=host, session_name=name)
        appended = 0
        capped = False
        for payload in events:
            if appended >= budget:
                # Per-pass cap hit mid-batch. Do NOT advance the offset: the whole
                # chunk is re-read next pass, and the durable identity dedup makes
                # the already-inserted prefix a no-op (no re-broadcast) so the tail
                # resumes exactly where the budget ran out — the cap never drops an
                # event (loop rule 2 without data loss).
                capped = True
                break
            seq = await append_ingested_event(
                self.store,
                self.broadcast,
                payload,
                recent_limit=self.recent_limit,
                routing_integrity=self.routing_integrity,
                lifecycle=lifecycle,
                sessions=self.sessions,
                on_incomplete=lambda reason: self._hold_position(sid, st, reason),
            )
            if seq is not None:
                appended += 1
        if not capped:
            st.offset = new_offset
            if provider == "cursor":
                st.cursor_norm_state = cursor_state
                st.cursor_line = cursor_line
        if appended and self.inventory_emitter is not None:
            await self.inventory_emitter.emit_if_changed()
        return appended

    async def _provider_root(self, row: dict[str, Any], name: str, *, observation: dict | None = None) -> tuple[str, str] | None:
        binding = row.get("observer_binding")
        account = row.get("account_binding")
        if account is not None:
            if (not isinstance(account, dict) or account.get("host") != row.get("host")
                    or account.get("provider") != row.get("provider") or not account.get("generation")):
                return None
            transcript = binding.get("transcript") if isinstance(binding, dict) else None
            path = (transcript or {}).get("path") or row.get("jsonl_path")
            if path and not transcript_allowed(path, row.get("provider"), account): return None
        if (not isinstance(binding, dict) or not row.get("session_generation")
                or binding.get("generation") != row.get("session_generation")
                or row.get("provider") not in {"codex", "claude", "gemini", "cursor"}):
            return None
        if row.get("provider") == "cursor" and not isinstance(binding.get("cursor"), dict):
            return None
        pid = codex_source_pane_pid(row.get("pane_pid"))
        started = canonical_start(binding.get("pane_started_at"))
        if not pid or binding.get("pane_pid") != pid or not started:
            return None
        try:
            live_pid = str(await self.tmux.pane_pid(name)) if self.tmux is not None else ""
        except Exception:  # noqa: BLE001 - a missing pane probe is no identity proof
            return None
        if live_pid != pid:
            return None
        record = await process_record(pid)
        if observation is not None:
            observation["start"] = canonical_start(record.get("start_id")) if record else ""
        if (not record or record["uid"] != os.getuid()
                or canonical_start(record["start_id"]) != started
                or not _provider_command_matches(record["command"], str(binding.get("executable") or ""), binding.get("cursor") if row.get("provider") == "cursor" else None)):
            return None
        return pid, canonical_start(record["start_id"])

    async def _maybe_rebind_observer_pane(
        self, row: dict[str, Any], name: str, *, observation: dict | None = None,
    ) -> dict[str, Any]:
        """Repair a stale observer binding after an in-place provider replacement
        (`tmux respawn-pane -k` → `codex resume`): same tmux session, SAME
        generation, SAME rollout, but a different pane PID or printed start.
        The reconciler refreshes
        `sessions.pane_pid`, yet `observer_binding.pane_pid` (minted at spawn, only
        rewritten on a generation change) stays stale, so the strict
        `_provider_root` proof rejects the still-open rollout every pass and server
        history freezes silently.

        Rebind ONLY when the replacement is independently proven — same generation,
        the live tmux pane IS the new pid, a legitimate process (uid + executable),
        and exactly one writable provider transcript that is the SAME rollout the
        binding was already proven against (device+inode AND native session id).
        Then CAS the binding's pane PID/start (nothing else). Returns the row with
        the rebound binding on success, otherwise the row unchanged. This never
        widens `_provider_root`; the caller re-runs that proof against the result."""
        binding = row.get("observer_binding")
        observation = observation if observation is not None else {}
        observation.update(start="", reason="proof_unavailable")
        generation = row.get("session_generation")
        if (row.get("status") != "open" or not generation or not isinstance(binding, dict)
                or binding.get("generation") != generation):
            return row
        transcript = binding.get("transcript")
        pid = codex_source_pane_pid(row.get("pane_pid"))
        if not pid:
            return row
        if self.tmux is None or str(await self.tmux.pane_pid(name)) != pid:
            return row  # the live pane must already BE the replacement process
        record = await process_record(pid)
        started = canonical_start(record.get("start_id")) if record else ""
        observation["start"] = started
        if (not record or record["uid"] != os.getuid()
                or not _provider_command_matches(record["command"], str(binding.get("executable") or ""), binding.get("cursor") if row.get("provider") == "cursor" else None)):
            if record:
                observation["reason"] = "foreign_identity"
            return row
        prior_start = canonical_start(binding.get("pane_started_at"))
        if not started or not prior_start:
            return row
        if binding.get("pane_pid") == pid and started == prior_start:
            return row  # presentation equality cannot repair another failed predicate
        if not isinstance(transcript, dict):
            observation["reason"] = "start_mismatch" if started != prior_start else "proof_unavailable"
            return row  # recorded/first-bind paths cannot seed recovery authority
        provider = row.get("provider")
        if provider not in {"codex", "claude", "gemini", "cursor"} or transcript.get("provider") != provider:
            observation["reason"] = "foreign_identity"
            return row
        if provider == "claude" and row.get("jsonl_path"):
            # Claude Code need not keep its transcript open, so there is no
            # writer to census. Only the SAME process may resume its anchored,
            # launch-recorded transcript (a shifted start presentation).
            if binding.get("pane_pid") != pid:
                return row
            if (transcript.get("path") != row.get("jsonl_path")
                    or transcript.get("session_id") != row.get("claude_session_id")):
                observation["reason"] = "foreign_identity"
                return row
            path = str(transcript["path"])
            try:
                info = await asyncio.to_thread(os.stat, path)
            except OSError:
                return row
            identity = (info.st_dev, info.st_ino)
        else:
            account = row.get("account_binding")
            candidates = await (_open_writable_transcripts(pid, account) if account else _open_writable_transcripts(pid))
            candidates = [c for c in candidates if transcript_allowed(c[0], provider, account)]
            if len(candidates) != 1:
                return row  # ambiguous (or no) writable transcript on the new pane
            path, identity = candidates[0]
        expected_id = transcript.get("file_id")
        if not isinstance(expected_id, (list, tuple)) or list(identity) != list(expected_id):
            observation["reason"] = "foreign_identity"
            return row  # a different rollout — never cross-bind another session
        fd = await asyncio.to_thread(_open_descriptor, path)
        if fd is None:
            return row
        try:
            opened = await asyncio.to_thread(os.fstat, fd)
            if (opened.st_dev, opened.st_ino) != identity:
                observation["reason"] = "foreign_identity"
                return row  # replaced between lsof proof and open (TOCTOU)
            native = await asyncio.to_thread(_native_session_identity_from_fd, fd, provider)
        finally:
            await asyncio.to_thread(os.close, fd)
        if not native or native != transcript.get("session_id"):
            observation["reason"] = "foreign_identity" if native else "proof_unavailable"
            return row  # same path/inode but a different session identity — refuse
        # Observation fence: all OS/filesystem probes precede the Store CAS.
        if str(await self.tmux.pane_pid(name)) != pid:
            return row
        current = await process_record(pid)
        if (not current or current.get("pid") != record.get("pid")
                or current.get("pid") != int(pid) or current.get("uid") != record.get("uid")
                or canonical_start(current.get("start_id")) != started
                or not _provider_command_matches(current.get("command", ""), str(binding.get("executable") or ""))):
            return row
        rebound = await self.store.rebind_observer_pane(
            row["stream_id"], generation=str(generation or ""), pane_pid=pid,
            pane_started_at=started, expected=binding,
        )
        if not rebound:
            observation["reason"] = "cas_changed"
            return row  # CAS lost to a concurrent binding change — retry next pass
        self.sessions.apply_durable(row["stream_id"], observer_binding=rebound)
        log.info("observer pane rebound sid=%s generation=%s pid=%s<-%s",
                 row["stream_id"], generation, pid, binding.get("pane_pid"), extra={
                     "subsystem": "ingest",
                     "bug_ref": "issue_201",
                 })
        return {**row, "observer_binding": rebound}

    async def _record_identity_blocked(
        self, row: dict, st: _StreamIngest, observation: dict, *, completed_attempt: bool = False,
    ) -> None:
        """Historical refusal at normal ingest cadence; never an activity claim."""
        binding = row.get("observer_binding")
        generation = row.get("session_generation")
        if not generation or not isinstance(binding, dict) or binding.get("generation") != generation:
            st.identity_grace = None
            st.identity_refusal = None
            return
        try:
            current = await self.store.fetch_session(row["host"], row["session_name"])
            if (not current or current.get("status") != "open"
                    or current.get("session_generation") != generation
                    or current.get("observer_binding") != binding
                    or current.get("pane_pid") != row.get("pane_pid")):
                st.identity_grace = None
                st.identity_refusal = None
                return  # stale diagnostic; next normal pass observes the new tuple
            digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            started = canonical_start(observation.get("start")) or "unavailable"
            reason = observation.get("reason", "proof_unavailable")
            unavailable = (reason == "proof_unavailable" and started == "unavailable"
                           and binding.get("pane_pid") == row.get("pane_pid"))
            if unavailable:
                # Liveness is only an alert filter. It grants no transcript or
                # rebind authority; strict root proof has already refused both.
                if not await asyncio.to_thread(pid_alive, str(binding.get("pane_pid") or "")):
                    st.identity_grace = None
                    st.identity_refusal = None
                    log.debug("ingest identity deferred stream=%s action=process_gone", row["stream_id"])
                    return
                if not completed_attempt:
                    st.identity_refusal = (row, dict(observation))
                    return
                key = (row["stream_id"], generation, digest, str(row.get("pane_pid") or ""))
                now = monotonic()
                if st.identity_grace is None or st.identity_grace.key != key:
                    st.identity_grace = _IdentityGrace(key, now)
                st.identity_grace.attempts += 1
                if st.identity_grace.attempts < 3 or now - st.identity_grace.since < 10:
                    log.debug("ingest identity deferred stream=%s attempts=%s elapsed_s=%.3f",
                              row["stream_id"], st.identity_grace.attempts, now - st.identity_grace.since)
                    return
            else:
                st.identity_grace = None
                st.identity_refusal = None
            episode = "ingest_identity_blocked:" + hashlib.sha256(json.dumps(
                [row["stream_id"], generation, digest, started], separators=(",", ":"),
            ).encode()).hexdigest()
            if not unavailable:
                st.identity_incident_id = episode  # preserve immediate-refusal semantics
            log.warning("ingest identity blocked incident=%s stream=%s generation=%s binding_digest=%s stored_pid=%s current_pid=%s stored_start=%s current_start=%s reason=%s",
                        episode, row["stream_id"], generation, digest, binding.get("pane_pid"),
                        row.get("pane_pid"), canonical_start(binding.get("pane_started_at")), started, reason,
                        extra={"subsystem": "ingest", "bug_ref": "issue_201", "incident_id": episode,
                               "stream_id": row["stream_id"], "generation": generation,
                               "expected_binding_digest": digest, "stored_pid": binding.get("pane_pid"),
                               "observed_pid": row.get("pane_pid"), "stored_start": canonical_start(binding.get("pane_started_at")),
                               "observed_start": started, "reason": reason})
            fact = ErrorFact("integrity", "ingest_identity_blocked", episode, stage=reason)
            ref = await self.alerts.error(fact) if self.alerts is not None else None
            if ref:
                st.identity_incident_id = episode
            else:
                log.warning("ingest identity incident=%s action=sink_unavailable", episode,
                            extra={"subsystem": "ingest", "bug_ref": "issue_201"})
        except Exception:  # noqa: BLE001 - retry at normal cadence, never fail the pump
            log.warning("ingest identity action=sink_failed", extra={"subsystem": "ingest", "bug_ref": "issue_201"})

    async def _discover_path(self, row: dict[str, Any], host: str, name: str, *, state: _StreamIngest | None = None) -> str:
        """Launch-recorded path, or a unique writable file of the proven root."""
        binding = row.get("observer_binding")
        if isinstance(binding, dict) and await self._provider_root(row, name) is None:
            return ""
        prior = binding.get("transcript") if isinstance(binding, dict) else None
        if isinstance(prior, dict):
            if await self._provider_root(row, name) is None:
                return ""
            path = str(prior.get("path") or "")
            if row.get("provider") == "cursor" and not transcript_allowed(path, "cursor", binding.get("cursor")):
                return ""
            return path if path and await asyncio.to_thread(_exists, path) else ""
        recorded = str(row.get("jsonl_path") or "")
        if row.get("provider") == "cursor":
            launch_binding = binding.get("cursor") if isinstance(binding, dict) else None
            return recorded if recorded and transcript_allowed(recorded, "cursor", launch_binding) else ""
        if recorded and await asyncio.to_thread(_exists, recorded):
            return recorded
        root = await self._provider_root(row, name)
        if root is None:
            return ""
        account = row.get("account_binding")
        candidates = await (_open_writable_transcripts(root[0], account) if account else _open_writable_transcripts(root[0]))
        candidates = [candidate for candidate in candidates if transcript_allowed(candidate[0], row.get("provider"), account)]
        if len(candidates) != 1:
            return ""
        path, identity = candidates[0]
        if state is not None:
            state.selected_file_id = identity
        return path


def _provider_command_matches(command: str, expected: str, cursor: dict | None = None) -> bool:
    if not expected:
        return False
    try:
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        tokens = [next(lexer, ""), next(lexer, "")]
    except ValueError:
        return False
    if cursor is not None:
        return (isinstance(cursor, dict) and len(tokens) == 2
                and os.path.isabs(tokens[0]) and os.path.isabs(tokens[1])
                and os.path.realpath(tokens[0]) == cursor.get("python") == os.path.realpath(expected)
                and os.path.realpath(tokens[1]) == cursor.get("repl"))
    def matches(token: str) -> bool:
        return os.path.isabs(token) and os.path.realpath(token) == os.path.realpath(expected)
    if tokens and matches(tokens[0]):
        return True
    return bool(len(tokens) >= 2 and os.path.basename(tokens[0]).lower() in
                {"python", "python3", "node", "bash", "sh", "zsh"} and matches(tokens[1]))


def _complete_record_boundary(fd: int, size: int) -> int:
    """Capture the last newline at first bind without consuming a partial line."""
    end = size
    while end > 0:
        start = max(0, end - 65536)
        chunk = os.pread(fd, end - start, start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return start + newline + 1
        end = start
    return 0


def _native_session_identity_from_fd(fd: int, provider: str) -> str:
    if provider == "cursor":
        header = _cursor_header(fd)
        return str(header.get("session_id") or "") if header else ""
    if provider == "codex":
        return _codex_session_identity_from_fd(fd) or ""
    for line in os.pread(fd, 262144, 0).splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and (record.get("sessionId") or record.get("session_id")):
            return str(record.get("sessionId") or record.get("session_id"))
    return ""


def _provider_from_session_name(name: str) -> str | None:
    lowered = name.lower()
    if "codex" in lowered:
        return "codex"
    if "claude" in lowered:
        return "claude"
    if "cursor" in lowered:
        return "cursor"
    if "gemini" in lowered:
        return "gemini"
    return None


def _exists(path: str) -> bool:
    return Path(path).expanduser().exists()


def _close_stream(st: _StreamIngest) -> None:
    if st.fd is None:
        return
    try:
        os.close(st.fd)
    except OSError:
        pass
    st.fd = None


def _file_id(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, info.st_nlink)


def _open_descriptor(path: str) -> int | None:
    try:
        return os.open(os.fspath(Path(path).expanduser()), os.O_RDONLY)
    except OSError:
        return None


def _read_bound_span(
    path: str,
    fd: int,
    start: int,
    end: int,
) -> tuple[bytes, tuple[int, int, int], tuple[int, int, int]] | None:
    try:
        chunk = os.pread(fd, max(0, end - start), start)
        consumed_id = _file_id(os.fstat(fd))
        path_id = _file_id(Path(path).expanduser().stat())
        return chunk, consumed_id, path_id
    except OSError:
        return None


def _codex_session_identity_from_fd(fd: int) -> str | None:
    try:
        prefix = os.pread(fd, 64 * 1024, 0)
    except OSError:
        return None
    for line in prefix.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "session_meta":
            if codex_is_guardian_review(record):
                return None
            session_id = codex_session_identity(record)
            return session_id or None
    return None


def _file_stat(path: str) -> tuple[int, tuple[int, int, int]] | None:
    """`(size, (device, inode, link_count))` for the bound transcript, or None if it is gone.

    The identity half is what distinguishes "the same file grew" from "a
    different file now answers to this path" — a distinction size alone cannot
    make when the replacement is larger.
    """
    try:
        info = Path(path).expanduser().stat()
        return info.st_size, _file_id(info)
    except FileNotFoundError:
        return None


async def _open_transcripts(pids: list[str]) -> list[str]:
    """Legacy path probe retained for callers outside first-bind admission."""
    if not pids:
        return []
    _rc, out = await _exec("lsof", "-p", ",".join(pids), "-Fn")
    return sorted({line[1:] for line in out.splitlines()
                   if line.startswith("n") and line.endswith(".jsonl")
                   and any(fragment in line for fragment in TRANSCRIPT_DIRS)})


async def _open_writable_transcripts(root_pid: str, account: dict | None = None) -> list[tuple[str, tuple[int, int]]]:
    # The npm Codex launcher is a Node wrapper; current releases keep the
    # rollout descriptor open in their native Codex child. The pane-root proof
    # is still checked separately by ``_provider_root``. Restrict descriptor
    # admission to that proven process tree, matching the satellite observer.
    pids = await process_tree(root_pid)
    if not pids:
        return []
    allowed_pids = set(pids)
    rc, out = await _exec("lsof", "-p", ",".join(pids), "-FpfatDin")
    if rc != 0:
        return []  # a partial listing cannot establish uniqueness
    pid = descriptor = access = kind = device = inode = ""
    files: dict[tuple[int, int], str] = {}
    for line in out.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == "p":
            pid = value
            descriptor = access = kind = device = inode = ""
        elif field == "f":
            descriptor, access, kind, device, inode = value, "", "", "", ""
        elif field == "a": access = value
        elif field == "t": kind = value
        elif field == "D": device = value
        elif field == "i": inode = value
        elif (field == "n" and pid in allowed_pids and descriptor.isdecimal()
              and access in {"u", "w"} and kind == "REG"
              and value.endswith(".jsonl") and (
                  transcript_allowed(value, account.get('provider'), account)
                  if account else any(fragment in value for fragment in TRANSCRIPT_DIRS))):
            try:
                identity = (int(device, 0), int(inode))
                info = os.stat(value)
            except (OSError, ValueError):
                return []
            if not stat_module.S_ISREG(info.st_mode) or identity != (info.st_dev, info.st_ino):
                return []
            if "/.codex/sessions/" in value or (account and account.get("provider") == "codex"):
                fd = _open_descriptor(value)
                if fd is None:
                    return []
                try:
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != identity:
                        return []
                    if not _codex_session_identity_from_fd(fd):
                        continue
                finally:
                    os.close(fd)
            files.setdefault(identity, value)
    return sorted((path, identity) for identity, path in files.items())


def _cursor_header(fd: int) -> dict | None:
    try:
        header = json.loads(os.pread(fd, 16384, 0).split(b"\n", 1)[0])
        return header if isinstance(header, dict) and header.get("type") == "pentacle_repl" and header.get("version") == 1 else None
    except (OSError, ValueError):
        return None
