#!/usr/bin/env python3
"""client_contract_probe.py — post-deploy read-back that a chat_streamd is
CLIENT-CONSUMABLE, not merely internally healthy.

Why this exists (canon: docs/config/pentacle_dev_guidelines.md principle 6):
a daemon-internal read-back can pass while every transcript renders empty. That
is exactly what shipped — the daemon served 500 correlated events per stream,
but stamped no `daemon_seq`, and the shared chat-core reducer
(`pentacle-chat-core/src/services/pentacleEventUtils.ts`,
`dedupeRecentEventsByStream`) DROPS every non-`client_origin` event whose
`daemon_seq` is not a finite number:

    const seq = Number(event?.daemon_seq);
    if (!Number.isFinite(seq) || seenSeq.has(seq)) continue;

So a "healthy" daemon rendered empty desktop AND mobile chats. This probe
connects like a real client, runs the desktop handshake (unsolicited `welcome`
-> `hello` with `subscribe.{include_subagents,events_mode}` -> `snapshot` ->
`request_stream_events`), and ASSERTS the reply is what a client can actually
render. It exits non-zero on any violation so it runs as a GATE, not prose.

Usage (post-deploy read-back against prod, from coordinator):
    python3 tools/client_contract_probe.py --stream coordinator:example-session
    # remote:  --url ws://example.local:7791
A stream KNOWN to have history is required (default --min-events 1) — an empty
stream is indistinguishable from the empty-chat bug, so pick one with turns.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from typing import Any, Iterable

from websockets.sync.client import connect

# Frames the daemon may push unsolicited between a request and its reply; a real
# client correlates replies by request_id and treats these as pushes. The
# `welcome` greeting (v1 parity) is the first frame on every connection.
_PUSH_TYPES = frozenset({
    "session.inventory", "working.state", "session.died", "child_report_ready",
    "host.status", "hosts.stats", "limits.update",
    "notification", "updates", "specs.changed", "asset.update",
    "schedule.inventory", "chat.event", "welcome",
})

# Locked by lane N's Slice 2 Astra gate. These are evidence constants, not
# command-line knobs; keep the smoke aligned with the supported envelope.
LINK_FLOOR_BYTES_PER_SECOND = 40 * 1024
HEARTBEAT_DEADLINE_MS = 1_000
HISTORY_HEARTBEAT_DEADLINE_MS = 6_000
CONTROL_PLANE_P95_LIMIT_MS = 1_000
MAX_QUEUED_BYTES = 64 * 1024
MAX_QUEUED_AGE_MS = 2_000
CONVERGENCE_QUIET_LIMIT_MS = 2_000
RECONNECT_SPIRAL_LIMIT = 0
RECONNECT_SPIRAL_WINDOW_SECONDS = 60
FULL_HELLO_BYTES = 287_809
# A full inspect_stream tail is a single JSON frame, unlike the bounded
# request_stream_events chunks.  Keep the read-only authority read below the
# websocket frame ceiling; oversized histories still get an independent final
# event check.
_INSPECT_AUTHORITY_PAYLOAD_BUDGET_BYTES = 512 * 1024

# Fields the shared reducer / normalizeEvent key on to render a NON-client_origin
# transcript event (pentacleEventUtils.dedupeRecentEventsByStream +
# pentacleStreamReducer.normalizeEvent: stream bucketing, correlatedDaemonSeq,
# and the `${seq}:${timestamp}:${kind}` dedup key).
_REQUIRED_FIELDS = ("stream_id", "kind", "timestamp")


def _recv_reply(
    ws, *, timeout: float, pushes: list[dict] | None = None,
    wire_sizes: list[int] | None = None,
) -> dict:
    """One correlated reply, skipping unsolicited pushes (incl. `welcome`)."""
    while True:
        raw = ws.recv(timeout=timeout)
        if wire_sizes is not None:
            wire_sizes.append(len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8")))
        frame = json.loads(raw)
        if frame.get("type") in _PUSH_TYPES:
            if pushes is not None:
                pushes.append(frame)
            continue
        return frame


def _event_key(event: dict, *, stream_id: str) -> tuple[str, int] | None:
    if event.get("stream_id") != stream_id:
        return None
    seq = event.get("daemon_seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        return None
    return stream_id, seq


def _event_keys(events: Iterable[dict], *, stream_id: str) -> list[tuple[str, int]]:
    return [key for event in events if (key := _event_key(event, stream_id=stream_id)) is not None]


def _event_content_key(event: dict) -> str:
    return json.dumps(
        {key: value for key, value in event.items() if key != "daemon_seq"},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


def audit_event_completeness(
    expected_events: list[dict], received_events: list[dict], *, stream_id: str,
    final_event: dict | None = None,
) -> dict[str, Any]:
    """Compare a subscription's received events with its authoritative set.

    ``daemon_seq`` is a global cursor and filtered subscriptions legitimately
    have gaps.  The set comparison therefore never invents a contiguous range;
    duplicate wire delivery is reported but accepted because the client reducer
    deduplicates by the stream/sequence key.
    """
    expected_keys = _event_keys(expected_events, stream_id=stream_id)
    received_keys = _event_keys(received_events, stream_id=stream_id)
    expected_set = set(expected_keys)
    received_set = set(received_keys)
    expected_content: dict[int, set[str]] = {}
    received_content: dict[int, set[str]] = {}
    for event in expected_events:
        key = _event_key(event, stream_id=stream_id)
        if key is not None:
            expected_content.setdefault(key[1], set()).add(_event_content_key(event))
    for event in received_events:
        key = _event_key(event, stream_id=stream_id)
        if key is not None:
            received_content.setdefault(key[1], set()).add(_event_content_key(event))
    final_key = _event_key(final_event, stream_id=stream_id) if final_event else (
        (stream_id, max(seq for _stream, seq in expected_keys)) if expected_keys else None
    )
    missing = sorted(seq for _stream, seq in expected_set - received_set)
    unexpected = sorted(seq for _stream, seq in received_set - expected_set)
    content_mismatch = sorted(
        seq for seq in expected_content.keys() & received_content.keys()
        if received_content[seq] != expected_content[seq]
    )
    counts = Counter(received_keys)
    duplicates = sorted(seq for (_stream, seq), count in counts.items() if count > 1)
    final_received = final_key is not None and final_key in received_set
    received_unique_seqs = {seq for _stream, seq in received_set}
    final_is_last = (
        final_key is not None
        and final_received
        and max(received_unique_seqs, default=final_key[1]) == final_key[1]
    )
    return {
        "passed": not missing and not unexpected and not content_mismatch and final_received and final_is_last,
        "expected_count": len(expected_set),
        "received_count": len(received_set),
        "missing": missing,
        "unexpected_seqs": unexpected,
        "content_mismatch_seqs": content_mismatch,
        "duplicate_seqs": duplicates,
        "final_seq": final_key[1] if final_key else None,
        "final_received": final_received,
        "final_is_last": final_is_last,
    }


def audit_reconnect_recovery(
    expected_events: list[dict], initial_events: list[dict], replay_events: list[dict], *,
    stream_id: str, final_event: dict | None = None,
) -> dict[str, Any]:
    """Audit actual replay recovery, allowing duplicate frames on the wire."""
    initial_keys = set(_event_keys(initial_events, stream_id=stream_id))
    replay_keys = set(_event_keys(replay_events, stream_id=stream_id))
    combined = audit_event_completeness(
        expected_events, initial_events + replay_events,
        stream_id=stream_id, final_event=final_event,
    )
    final_key = _event_key(final_event, stream_id=stream_id) if final_event else None
    recovered = sorted(seq for _stream, seq in replay_keys - initial_keys)
    return {
        **combined,
        "recovered_seqs": recovered,
        "final_occurrences": int(final_key in set(initial_keys | replay_keys)) if final_key else 0,
        "replay_wire_count": len(_event_keys(replay_events, stream_id=stream_id)),
    }


def audit_probe_reply(reply: dict, *, stream_id: str) -> dict[str, Any]:
    """Compare backfill wire delivery with the independent durable inspect set."""
    expected = reply.get("authoritative_events")
    received = reply.get("wire_events")
    if not isinstance(expected, list) or not isinstance(received, list):
        return {
            "passed": False,
            "reason": "probe did not retain both authoritative inspect events and received wire events",
            "expected_count": None,
            "received_count": None,
        }
    if not reply.get("authoritative_complete", True):
        # inspect_stream is intentionally bounded to the final event for a
        # large history: the request_stream_events path is chunked, while an
        # inspect tail is one websocket frame.  Compare only the final and any
        # later received sequence here; the full-tail path below compares the
        # complete authoritative per-stream set.
        final_key = _event_key(expected[-1], stream_id=stream_id) if expected else None
        if final_key is not None:
            received = [
                event for event in received
                if _event_key(event, stream_id=stream_id) is not None
                and _event_key(event, stream_id=stream_id)[1] >= final_key[1]
            ]
    final_event = expected[-1] if expected else None
    result = audit_event_completeness(
        expected, received, stream_id=stream_id, final_event=final_event,
    )
    if not reply.get("authoritative_complete", True):
        result["authority_scope"] = "final_event"
    return result


def audit_control_plane_latency(
    samples_ms: Iterable[float | None], *, p95_limit_ms: float,
) -> dict[str, Any]:
    raw_samples = list(samples_ms)
    values = sorted(float(value) for value in raw_samples if value is not None)
    timeouts = sum(value is None for value in raw_samples)
    if values:
        index = max(0, math.ceil(len(values) * 0.95) - 1)
        p95_ms = values[index]
    else:
        p95_ms = math.inf
    return {
        "passed": not timeouts and p95_ms <= p95_limit_ms,
        "samples": len(values),
        "timeouts": timeouts,
        "p95_ms": p95_ms,
        "limit_ms": p95_limit_ms,
    }


def audit_disconnects(
    disconnects: list[dict], *, reconnect_spiral_count: int,
    reconnect_spiral_limit: int = RECONNECT_SPIRAL_LIMIT,
    reconnect_attempts: list[dict] | None = None,
) -> dict[str, Any]:
    timestamps = [
        float(row["at_monotonic"])
        for row in disconnects
        if isinstance(row, dict) and isinstance(row.get("at_monotonic"), (int, float))
    ]
    latest = max(timestamps, default=None)
    in_window = (
        sum(latest - timestamp <= RECONNECT_SPIRAL_WINDOW_SECONDS for timestamp in timestamps)
        if latest is not None else len(disconnects)
    )
    attempt_timestamps = [
        float(row["at_monotonic"])
        for row in (reconnect_attempts or [])
        if isinstance(row, dict) and isinstance(row.get("at_monotonic"), (int, float))
    ]
    attempt_latest = max(attempt_timestamps, default=None)
    attempts_in_window = (
        sum(attempt_latest - timestamp <= RECONNECT_SPIRAL_WINDOW_SECONDS for timestamp in attempt_timestamps)
        if attempt_latest is not None else len(reconnect_attempts or [])
    )
    return {
        "passed": not disconnects and reconnect_spiral_count <= reconnect_spiral_limit,
        "disconnects": list(disconnects),
        "disconnects_in_window": in_window,
        "reconnect_attempts": list(reconnect_attempts or []),
        "reconnect_attempts_in_window": attempts_in_window,
        "reconnect_spiral_count": reconnect_spiral_count,
        "reconnect_spiral_limit": reconnect_spiral_limit,
        "reconnect_spiral_window_seconds": RECONNECT_SPIRAL_WINDOW_SECONDS,
    }


def audit_final_state_convergence(
    expected_rows: list[dict], received_rows: list[dict],
    *, quiet_ms: float | None = None,
) -> dict[str, Any]:
    """Compare the last rendered roster/status with the authoritative snapshot."""
    fields = ("stream_id", "bootstrap_state", "status", "working", "visibility")

    def project(rows: list[dict]) -> dict[str, dict[str, Any]]:
        return {
            str(row.get("stream_id")): {
                # Active summary inventory intentionally omits persistence
                # status (also on 8c). Membership means open; an explicit
                # closed/null status must still disagree with an open row.
                "status": row.get("status", "open"),
                **{field: row.get(field) for field in fields if field in row},
            }
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("stream_id"), str)
        }

    expected = project(expected_rows)
    received = project(received_rows)
    within_quiet_limit = quiet_ms is None or quiet_ms <= CONVERGENCE_QUIET_LIMIT_MS
    return {
        "passed": expected == received and within_quiet_limit,
        "expected": expected,
        "received": received,
        "quiet_ms": quiet_ms,
        "quiet_limit_ms": CONVERGENCE_QUIET_LIMIT_MS,
        "within_quiet_limit": within_quiet_limit,
        "missing_streams": sorted(set(expected) - set(received)),
        "unexpected_streams": sorted(set(received) - set(expected)),
    }


def desktop_handshake_fetch_reply(
    url: str, stream_id: str, *, limit: int, include_subagents: bool,
    events_mode: str, timeout: float, before_daemon_seq: int | None = None,
    chunk_limit: int | None = None,
) -> dict:
    reply, _frame_sizes, _chunk_count = desktop_handshake_fetch_reply_observed(
        url, stream_id, limit=limit, include_subagents=include_subagents,
        events_mode=events_mode, timeout=timeout, before_daemon_seq=before_daemon_seq,
        chunk_limit=chunk_limit,
    )
    return reply


def desktop_handshake_fetch_reply_observed(
    url: str, stream_id: str, *, limit: int, include_subagents: bool,
    events_mode: str, timeout: float, before_daemon_seq: int | None = None,
    chunk_limit: int | None = None,
) -> tuple[dict, list[int], int]:
    """Replicate the desktop connect + backfill fetch, returning the events the
    daemon serves for `stream_id` plus wire sizes and chunk count.

    Chunk frames are merged and sorted the way the desktop reducer consumes
    them, while the sizes remain available to the byte-budget gate.
    """
    pushes: list[dict] = []
    bootstrap_frame_sizes: list[int] = []
    bootstrap_started = time.monotonic()
    with connect(url, open_timeout=timeout) as ws:
        ws.send(json.dumps({
            "type": "hello", "request_id": "probe-hello",
            "client": "pentacle-contract-probe",
            "subscribe": {"include_subagents": include_subagents, "events_mode": events_mode},
        }))
        hello = _recv_reply(ws, timeout=timeout, pushes=pushes, wire_sizes=bootstrap_frame_sizes)
        if hello.get("type") != "hello":
            raise RuntimeError(f"expected hello reply, got {hello.get('type')!r}: {hello}")
        snapshot = _recv_reply(ws, timeout=timeout, pushes=pushes, wire_sizes=bootstrap_frame_sizes)
        if snapshot.get("type") != "snapshot":
            raise RuntimeError(f"expected snapshot, got {snapshot.get('type')!r}: {snapshot}")
        while True:
            raw_stats = ws.recv(timeout=timeout)
            stats_frame = json.loads(raw_stats)
            bootstrap_frame_sizes.append(
                len(raw_stats) if isinstance(raw_stats, bytes) else len(raw_stats.encode("utf-8"))
            )
            if stats_frame.get("type") == "hosts.stats":
                problems = assert_hosts_stats_frame(stats_frame)
                if problems:
                    raise RuntimeError("hosts.stats contract: " + "; ".join(problems))
                break
            if stats_frame.get("type") in _PUSH_TYPES:
                pushes.append(stats_frame)
                continue
            raise RuntimeError(f"expected hosts.stats, got {stats_frame.get('type')!r}: {stats_frame}")
        request = {
            "type": "request_stream_events", "request_id": "probe-events",
            "stream_id": stream_id, "limit": limit,
        }
        if before_daemon_seq is not None:
            request["before_daemon_seq"] = before_daemon_seq
        if chunk_limit is not None:
            request["chunk_limit"] = chunk_limit
        ws.send(json.dumps(request))
        chunk_events: list[dict] = []
        frame_sizes: list[int] = []
        chunk_count = 0
        while True:
            raw = ws.recv(timeout=timeout)
            frame = json.loads(raw)
            if frame.get("type") in _PUSH_TYPES:
                pushes.append(frame)
                continue
            wire_size = len(raw) if isinstance(raw, bytes) else len(raw.encode("utf-8"))
            if frame.get("type") == "request_stream_events.chunk":
                events = frame.get("events")
                if not isinstance(events, list):
                    raise RuntimeError(f"chunk.events is not a list: {frame}")
                chunk_events.extend(events)
                frame_sizes.append(wire_size)
                chunk_count += 1
                continue
            frame_sizes.append(wire_size)
            reply = frame
            break
        if reply.get("type") != "request_stream_events.ok":
            raise RuntimeError(f"request_stream_events failed: {reply}")
        terminal_events = reply.get("events")
        if not isinstance(terminal_events, list):
            raise RuntimeError(f"reply.events is not a list: {reply}")
        wire_events = [*chunk_events, *terminal_events]
        inspect_tail = len(wire_events)
        wire_payload_bytes = len(json.dumps(
            wire_events, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8"))
        authoritative_complete = before_daemon_seq is None and (
            wire_payload_bytes <= _INSPECT_AUTHORITY_PAYLOAD_BUDGET_BYTES
        )
        if not authoritative_complete:
            inspect_tail = 1 if wire_events else 0
        ws.send(json.dumps({
            "type": "inspect_stream", "request_id": "probe-authority",
            "stream_id": stream_id, "event_tail": inspect_tail,
        }))
        inspected = _recv_reply(ws, timeout=timeout, pushes=pushes)
        if inspected.get("type") != "inspect_stream.ok":
            raise RuntimeError(f"authoritative inspect failed: {inspected}")
        authoritative_events = inspected.get("recent_events")
        if not isinstance(authoritative_events, list):
            raise RuntimeError(f"inspect recent_events is not a list: {inspected}")
    if chunk_events:
        reply = dict(reply)
        combined = list(wire_events)
        try:
            combined = sorted(combined, key=lambda event: int(event["daemon_seq"]))
        except (KeyError, TypeError, ValueError):
            # Leave malformed data in wire order so the reducer-facing checks
            # report the actual contract violation rather than hiding it here.
            pass
        reply["events"] = combined
    reply = dict(reply)
    reply["wire_events"] = wire_events
    # This is intentionally obtained through the separate read-only
    # inspect_stream path.  Comparing request_stream_events to itself would
    # certify a missing final frame if that frame vanished from both fixtures.
    reply["authoritative_events"] = authoritative_events
    reply["authoritative_complete"] = authoritative_complete
    reply["bootstrap_frame_sizes"] = bootstrap_frame_sizes
    reply["bootstrap_total_bytes"] = sum(bootstrap_frame_sizes)
    reply["bootstrap_peak_bytes"] = max(bootstrap_frame_sizes, default=0)
    reply["bootstrap_elapsed_ms"] = (time.monotonic() - bootstrap_started) * 1000
    reply["bootstrap_rate_bytes_per_second"] = (
        reply["bootstrap_total_bytes"] / (reply["bootstrap_elapsed_ms"] / 1000)
        if reply["bootstrap_elapsed_ms"] > 0 else 0.0
    )
    reply["full_hello_reference_bytes"] = FULL_HELLO_BYTES
    reply["received_events"] = [
        frame.get("event") for frame in pushes
        if frame.get("type") == "chat.event" and isinstance(frame.get("event"), dict)
    ]
    return reply, frame_sizes, chunk_count


def assert_hosts_stats_frame(frame: dict) -> list[str]:
    """Validate the fleet frame that desktop and mobile project directly."""
    problems: list[str] = []
    if frame.get("type") != "hosts.stats":
        problems.append("type is not hosts.stats")
    hosts = frame.get("hosts")
    if not isinstance(hosts, dict):
        return [*problems, "hosts is not an object"]
    required = (
        "host", "cpu_load_1m", "memory_used_bytes", "memory_total_bytes",
        "disk_used_bytes", "disk_total_bytes", "uptime_seconds", "sampled_at",
    )
    for host, stats in hosts.items():
        if not isinstance(host, str) or not host:
            problems.append("host keys must be non-empty strings")
            continue
        if not isinstance(stats, dict):
            problems.append(f"{host}: sample is not an object")
            continue
        for field in required:
            if field not in stats:
                problems.append(f"{host}: missing {field}")
        for field in required[1:-1]:
            value = stats.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
                problems.append(f"{host}: {field} is not numeric")
        memory_used = stats.get("memory_used_bytes")
        memory_total = stats.get("memory_total_bytes")
        disk_used = stats.get("disk_used_bytes")
        disk_total = stats.get("disk_total_bytes")
        valid_number = lambda value: (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
        if not valid_number(memory_total) or not valid_number(disk_total) or memory_total <= 0 or disk_total <= 0:
            problems.append(f"{host}: totals must be positive")
        if valid_number(memory_used) and valid_number(memory_total) and memory_used > memory_total:
            problems.append(f"{host}: memory used exceeds total")
        if valid_number(disk_used) and valid_number(disk_total) and disk_used > disk_total:
            problems.append(f"{host}: disk used exceeds total")
        if not isinstance(stats.get("host"), str) or not stats["host"] or stats["host"] != host:
            problems.append(f"{host}: host key mismatch")
    return problems


def desktop_handshake_fetch(
    url: str, stream_id: str, *, limit: int, include_subagents: bool,
    events_mode: str, timeout: float,
) -> list[dict]:
    reply = desktop_handshake_fetch_reply(
        url, stream_id, limit=limit, include_subagents=include_subagents,
        events_mode=events_mode, timeout=timeout,
    )
    events = reply.get("events")
    if not isinstance(events, list):
        raise RuntimeError(f"reply.events is not a list: {reply}")
    return events


def _reducer_keeps(event: dict) -> bool:
    """The exact drop rule from dedupeRecentEventsByStream's non-client_origin
    branch: kept iff `Number(daemon_seq)` is finite. Client-origin echoes (which
    key on optimistic_id instead) are not what a backfill serves, so any such
    frame here is already a contract break and is reported by field checks."""
    if event.get("client_origin") is True:
        return bool(event.get("optimistic_id"))
    try:
        seq = float(event.get("daemon_seq"))  # JS Number(undefined|null) -> NaN
    except (TypeError, ValueError):
        return False
    return math.isfinite(seq)


def assert_client_consumable(events: list[dict], *, min_events: int) -> list[str]:
    """Return a list of violation strings (empty == the reply is renderable).

    A daemon whose reply passes this is one a real desktop/mobile client renders;
    a daemon that drops daemon_seq (the shipped bug) fails check 2 for every row.
    """
    problems: list[str] = []
    if len(events) < min_events:
        problems.append(
            f"non-empty history: got {len(events)} events, need >= {min_events} "
            f"(empty transcript is the symptom this probe guards)")
        return problems  # nothing else is meaningful on an empty reply

    dropped = [i for i, e in enumerate(events) if not _reducer_keeps(e)]
    if dropped:
        problems.append(
            f"reducer would DROP {len(dropped)}/{len(events)} events (no finite "
            f"daemon_seq) — empty chat; first dropped index {dropped[0]}")

    missing: dict[str, int] = {}
    non_int_seq = 0
    for e in events:
        for f in _REQUIRED_FIELDS:
            if not e.get(f):
                missing[f] = missing.get(f, 0) + 1
        s = e.get("daemon_seq")
        if not (isinstance(s, int) and not isinstance(s, bool)):
            non_int_seq += 1
    for f, n in missing.items():
        problems.append(f"field {f!r} missing/empty on {n}/{len(events)} events")
    if non_int_seq:
        problems.append(f"daemon_seq not an integer on {non_int_seq}/{len(events)} events")

    seqs = [e.get("daemon_seq") for e in events if isinstance(e.get("daemon_seq"), int)
            and not isinstance(e.get("daemon_seq"), bool)]
    if len(set(seqs)) != len(seqs):
        problems.append(f"daemon_seq not unique across the {len(seqs)} stamped events "
                        f"(reducer dedup collides)")
    if seqs != sorted(seqs):
        problems.append("daemon_seq not increasing oldest-first (backfill order wrong)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="ws://127.0.0.1:7791", help="daemon WS url (default: local prod)")
    ap.add_argument("--stream", required=True, help="stream_id with known history, e.g. coordinator:example-session")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--min-events", type=int, default=1)
    ap.add_argument("--events-mode", default="summary")
    ap.add_argument("--no-include-subagents", dest="include_subagents", action="store_false")
    ap.add_argument("--timeout", type=float, default=5.0)
    args = ap.parse_args(argv)

    try:
        reply = desktop_handshake_fetch_reply(
            args.url, args.stream, limit=args.limit,
            include_subagents=args.include_subagents, events_mode=args.events_mode,
            timeout=args.timeout,
        )
        events = reply.get("events")
        if not isinstance(events, list):
            raise RuntimeError(f"reply.events is not a list: {reply}")
        bootstrap_peak_bytes = reply.get("bootstrap_peak_bytes", 0)
        bootstrap_total_bytes = reply.get("bootstrap_total_bytes", 0)
        bootstrap_elapsed_ms = reply.get("bootstrap_elapsed_ms", 0.0)
        bootstrap_rate = reply.get("bootstrap_rate_bytes_per_second", 0.0)
        full_hello_reference_bytes = reply.get("full_hello_reference_bytes", FULL_HELLO_BYTES)
    except Exception as exc:  # transport / handshake / reply error is a failure
        print(f"FAIL {args.stream} @ {args.url}: handshake/fetch error: {exc}")
        return 2

    problems = assert_client_consumable(events, min_events=args.min_events)
    if problems:
        print(f"FAIL {args.stream} @ {args.url}: {len(events)} events, NOT client-consumable:")
        for p in problems:
            print(f"  - {p}")
        return 1
    event_gate = audit_probe_reply(reply, stream_id=args.stream)
    if not event_gate["passed"]:
        print(f"FAIL {args.stream} @ {args.url}: final rendered content gate: {json.dumps(event_gate, sort_keys=True)}")
        return 1
    print(f"PASS {args.stream} @ {args.url}: {len(events)} events, all client-consumable; "
          f"event_complete={event_gate['passed']} "
          f"bootstrap_peak_bytes={bootstrap_peak_bytes} "
          f"bootstrap_total_bytes={bootstrap_total_bytes} "
          f"bootstrap_elapsed_ms={bootstrap_elapsed_ms:.1f} "
          f"bootstrap_rate_bytes_per_second={bootstrap_rate:.1f} "
          f"full_hello_reference_bytes={full_hello_reference_bytes} "
          f"(finite unique increasing daemon_seq + required fields)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
