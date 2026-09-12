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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_jsonl_norm import normalize_claude_jsonl_records
from codex_rollout_norm import codex_session_identity, normalize_codex_rollout_record
from context_adapters import parse_codex_context_reading
from prockill import process_tree, process_record
from store import ENTRY_DROPPED
from tmux_transport import TRANSCRIPT_DIRS, _exec
from v2_runtime import env_number

log = logging.getLogger("chat_streamd_v2.ingest")

DEFAULT_INTERVAL_S = 1.5
#: Per-pass work cap (loop rule 2): at most this many newly-appended events per
#: pass, so a stream that just replayed a huge transcript cannot monopolize the
#: pump; the remainder is picked up next pass.
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
class _StreamIngest:
    """Per-stream supervised tail state — the unit B13 isolates so one sick
    stream never kills the pump. Never persisted (rebuilt on daemon restart; the
    durable identity dedups the replay-from-start)."""

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


def _jsonl_event_identity(payload: dict) -> tuple:
    """Source-independent durable dedup identity (LIFTED VERBATIM from v1
    chat_streamd `_jsonl_event_identity`). A record's `jsonl_record_uuid` +
    event index is the stable key; text-hash fallback when a record has none."""
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    stream_id = str(payload.get("stream_id") or "")
    provider = str(payload.get("provider") or "")
    kind = str(payload.get("kind") or "")
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


async def append_ingested_event(
    store: Any,
    broadcast: Broadcast,
    payload: dict[str, Any],
    *,
    recent_limit: int,
    routing_integrity: Any = None,
    lifecycle: dict[str, Any] | None = None,
) -> int | None:
    """Append one event under its raw identity, stamping durable receipt metadata."""
    stream_id = str(payload.get("stream_id") or "")
    raw_identity = _identity_key(payload)
    corrected = await store.stamp_event_with_send_receipt(payload)
    if lifecycle is None:
        seq = await store.append_session_event(
            stream_id, corrected, identity=raw_identity, limit=recent_limit,
        )
    else:
        sequences = await store.append_session_events_lifecycle_cas([
            {"stream_id": stream_id, "event": corrected, "identity": raw_identity, "lifecycle": lifecycle},
        ], limit=recent_limit)
        if sequences is None or sequences[0] is ENTRY_DROPPED:
            return None
        seq = sequences[0]
    if routing_integrity is not None:
        try:
            await routing_integrity.observe_claude_event(corrected)
        except Exception as exc:  # noqa: BLE001 - ingest must keep accepting
            log.warning("routing-integrity Claude observation failed sid=%s: %s", stream_id, exc)
    if seq is not None:
        await broadcast({"type": "chat.event", "event": {**corrected, "daemon_seq": seq}})
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
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.tmux = tmux
        self.broadcast = broadcast
        self.local_host = local_host
        self.recent_limit = recent_limit
        self.config = config or IngestConfig()
        self.routing_integrity = routing_integrity
        self.inventory_emitter = inventory_emitter
        self._streams: dict[str, _StreamIngest] = {}

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
                delay = cfg.interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad pass never kills the pump
                failures += 1
                delay = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (failures - 1)))
                log.warning("ingest pass failed (%d): %s; retrying in %.0fs", failures, exc, delay)

    async def run_pass(self) -> int:
        """One bounded ingest pass. Returns the number of events newly appended.
        Also the test-only forced trigger (called directly)."""
        cfg = self.config
        rows = [r for r in self.sessions.list_open()
                if str(r.get("host") or "") == self.local_host]
        open_ids = {r["stream_id"] for r in rows}
        # Forget tail state for streams no longer open (membership follows the
        # O(open) registry, never a filesystem scan).
        for sid in list(self._streams):
            if sid not in open_ids:
                stale = self._streams.pop(sid, None)
                if stale is not None:
                    _close_stream(stale)

        now = asyncio.get_running_loop().time()
        budget = cfg.max_events_per_pass
        appended = 0
        for row in rows:
            if budget <= 0:
                break
            sid = row["stream_id"]
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
                _close_stream(st)
                return 0
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
        if provider not in ("claude", "codex"):
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
        if isinstance(binding, dict) and not row.get("jsonl_path"):
            native = await asyncio.to_thread(_native_session_identity_from_fd, st.fd, provider)
            if not native:
                return 0
            transcript = {"path": st.path, "provider": provider, "session_id": native,
                          "file_id": list(file_id[:2])}
            prior = binding.get("transcript")
            if prior is not None and prior != transcript:
                _close_stream(st)
                return 0
            if prior is None:
                if not await self.store.bind_observer_transcript(
                    sid, generation=generation, pane_pid=str(row.get("pane_pid") or ""),
                    expected=binding, transcript=transcript,
                ):
                    return 0
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
        if size < st.offset:
            # Rotation/truncation in place: replay from the start; dedup keeps
            # it exact.
            st.offset = 0
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
        # Consume only up to the last complete line; a partial trailing line is
        # left for the next pass (advance offset by the consumed byte count).
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            return 0  # no complete line yet
        consumed = chunk[: last_nl + 1]
        new_offset = st.offset + len(consumed)

        records: list[dict] = []
        malformed_usage_span = False
        # File offset just past each kept record's line, so the Codex per-pass cap
        # can advance the offset to exactly the last record it processed. Byte
        # lengths come off the RAW bytes (`splitlines(keepends=True)`); decoding
        # with "replace" is not byte-reversible, so it cannot be measured after.
        record_end_offsets: list[int] = []
        cursor = st.offset
        for raw_line in consumed.splitlines(keepends=True):
            cursor += len(raw_line)
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
            usage = await self.store.record_usage(
                row, records, native_session_id=st.session_id,
                collection_host=self.local_host, malformed=malformed_usage_span,
            )
            if usage is None:
                return 0
            self.sessions.apply_durable(sid, usage=usage)

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
                if payloads and len(entries) + len(payloads) > budget:
                    # Leave this record and the rest of the span for next pass.
                    capped = True
                    break
                for payload in payloads:
                    admission_payload = {**payload, "source_pane_pid": source_pid}
                    if validate_event_payload(admission_payload, host) is not None:
                        return 0
                    corrected = await self.store.stamp_event_with_send_receipt(payload)
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
                        log.warning("routing-integrity local observation failed sid=%s: %s", sid, exc)
            try:
                for entry, seq in zip(entries, sequences):
                    if seq is not None:
                        self.sessions.apply_genuine_activity_event(sid, entry["event"])
                        await self.broadcast({
                            "type": "chat.event",
                            "event": {**entry["event"], "daemon_seq": seq},
                        })
                if any(seq is not None for seq in sequences) and self.inventory_emitter is not None:
                    await self.inventory_emitter.emit_if_changed()
            except Exception as exc:  # noqa: BLE001 - never claim the failed batch
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
            )
            if seq is not None:
                self.sessions.apply_genuine_activity_event(sid, payload)
                appended += 1
        if not capped:
            st.offset = new_offset
        if appended and self.inventory_emitter is not None:
            await self.inventory_emitter.emit_if_changed()
        return appended

    async def _provider_root(self, row: dict[str, Any], name: str) -> tuple[str, str] | None:
        binding = row.get("observer_binding")
        if not isinstance(binding, dict) or binding.get("generation") != row.get("session_generation"):
            return None
        pid = codex_source_pane_pid(row.get("pane_pid"))
        if not pid or binding.get("pane_pid") != pid or not binding.get("pane_started_at"):
            return None
        if self.tmux is None or str(await self.tmux.pane_pid(name)) != pid:
            return None
        record = await process_record(pid)
        if (not record or record["uid"] != os.getuid()
                or record["start_id"] != " ".join(str(binding["pane_started_at"]).split())
                or not _provider_command_matches(record["command"], str(binding.get("executable") or ""))):
            return None
        return pid, record["start_id"]

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
            return path if path and await asyncio.to_thread(_exists, path) else ""
        recorded = str(row.get("jsonl_path") or "")
        if recorded and await asyncio.to_thread(_exists, recorded):
            return recorded
        root = await self._provider_root(row, name)
        if root is None:
            return ""
        candidates = await _open_writable_transcripts(root[0])
        fragment = "/.codex/sessions/" if row.get("provider") == "codex" else "/.claude/"
        candidates = [candidate for candidate in candidates if fragment in candidate[0]]
        if len(candidates) != 1:
            return ""
        path, identity = candidates[0]
        if state is not None:
            state.selected_file_id = identity
        return path


def _provider_command_matches(command: str, expected: str) -> bool:
    if not expected:
        return False
    try:
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        tokens = [next(lexer, ""), next(lexer, "")]
    except ValueError:
        return False
    def matches(token: str) -> bool:
        return os.path.isabs(token) and os.path.realpath(token) == os.path.realpath(expected)
    if tokens and matches(tokens[0]):
        return True
    return bool(len(tokens) >= 2 and os.path.basename(tokens[0]).lower() in
                {"python", "python3", "node", "bash", "sh", "zsh"} and matches(tokens[1]))


def _native_session_identity_from_fd(fd: int, provider: str) -> str:
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


async def _open_writable_transcripts(root_pid: str) -> list[tuple[str, tuple[int, int]]]:
    rc, out = await _exec("lsof", "-p", root_pid, "-FpfatDin")
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
        elif (field == "n" and pid == root_pid and descriptor.isdecimal()
              and access in {"u", "w"} and kind == "REG"
              and value.endswith(".jsonl") and any(fragment in value for fragment in TRANSCRIPT_DIRS)):
            try:
                identity = (int(device, 0), int(inode))
                info = os.stat(value)
            except (OSError, ValueError):
                return []
            if not stat_module.S_ISREG(info.st_mode) or identity != (info.st_dev, info.st_ino):
                return []
            files.setdefault(identity, value)
    return sorted((path, identity) for identity, path in files.items())
