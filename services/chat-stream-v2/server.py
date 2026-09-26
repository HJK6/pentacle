"""server.py — WS accept, hello/snapshot, uniform dispatch, request correlation.

Contract (v2_design.md module table):
  owns: WS accept, hello/snapshot, uniform dispatch, request correlation.
  notes: no business logic.

Protocol (v2_design.md § Protocol):
  - One uniform request/response envelope. Requests carry `type` + `request_id`;
    replies are `<verb>.ok` / `<verb>.error` correlated by `request_id`.
  - Reply type strings are preserved exactly from v1 for every kept verb.
  - Any v1 verb not implemented returns structured `unsupported_in_v2` with the
    verb echoed — cutover telemetry doubles as requirements discovery
    (spec constraint 4).

Architecture constraint 1 (spec): the WS port binds before any reconciliation or
scanning. `main.py` owns that ordering; this module exposes `bind()` separately
from `serve_forever()` so the ordering is enforceable, not aspirational.
"""

from __future__ import annotations

import qa_dispatch

import asyncio
import errno
import functools
import hmac
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from pathlib import Path

try:
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    from websockets.server import serve  # type: ignore[no-redef]
    from websockets.exceptions import ConnectionClosed  # type: ignore[no-redef]

from _shared import operator_auth
from comms import ATTACHMENT_MAX_BYTES, AttachmentValidationError, validate_send_attachments
import store_lifecycle_authority as lifecycle_authority
import store_consent as consent
import local_admin
from ledger import TERMINAL_REPORT_STATUSES, StatusCardError, _claim_wire_fields
from seat_token_telemetry import (
    SeatTokenTelemetry,
    TOKEN_REASON_ABSENT,
    TOKEN_REASON_EXPIRED,
    TOKEN_REASON_INTERNAL_ERROR,
    TOKEN_REASON_MALFORMED,
    TOKEN_REASON_VERIFIED,
    TOKEN_REASON_WRONG_SEAT,
)
from sessions import VerbError, _finish_despite_cancel, with_bootstrap_state
from store import STREAM_TOKEN_HASH_VERSION, role_source_for
from machine_stats import validate_machine_stats

log = logging.getLogger("chat_streamd_v2.server")

_monotonic = time.monotonic
UNSUPPORTED_LOG_INTERVAL_SECONDS = 60.0
MAX_UNSUPPORTED_LABEL_LENGTH = 128
MAX_UNSUPPORTED_VERBS = 256
MAX_UNSUPPORTED_CALLERS_PER_VERB = 32
UNSUPPORTED_OVERFLOW_VERB = "<overflow>"
UNSUPPORTED_OTHER_CALLER = "<other>"
UNSUPPORTED_FALLBACK_LABELS = frozenset({"<unknown>", "<missing>"})
_TELEMETRY_LOG_SAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/<>-"
)

SYSTEM_PRODUCER_STREAM_TOKEN_ENV = "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN"
SYSTEM_PRODUCER_STREAM_TOKEN_FILE_ENV = "PENTACLE_SYSTEM_PRODUCER_STREAM_TOKEN_FILE"
SYSTEM_PRODUCER_STREAM_ID_ENV = "PENTACLE_SYSTEM_PRODUCER_STREAM_ID"
FIXED_SYSTEM_PRODUCER_STREAM_ID = "altum-bot-cd"
SYSTEM_NOTIFICATION_CREATE_FIELDS = frozenset({
    "type", "request_id", "from_stream_id", "stream_token", "producer",
    "title", "body", "severity", "dedup_key", "actions",
})
SYSTEM_NOTIFICATION_DEDUP_RE = re.compile(
    r"^(?:pipeline\|[A-Za-z0-9][A-Za-z0-9_.-]{0,119}"
    r"|census\|[A-Za-z0-9][A-Za-z0-9_.-]{0,119}"
    r"\|[A-Za-z0-9][A-Za-z0-9_.-]{0,119}"
    r"|infra-census\|[A-Za-z][-A-Za-z0-9]{0,127})"
    r"\|([0-9]{4}-[0-9]{2}-[0-9]{2})$"
)

#: WS frame ceiling. Blob transport sends a 1 MiB *decoded* chunk, which is
#: ~1.37 MiB once base64-wrapped in a JSON envelope — over the websockets 1 MiB
#: default, which would drop the connection with a 1009 instead of letting the
#: app-level chunk-size check answer `upload_blob_chunk_oversize`. 4 MiB clears
#: one legal chunk with margin while still rejecting absurd frames at the wire.
WS_MAX_SIZE = 4 * 1024 * 1024

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# Each client has a bounded broadcast queue drained by one writer task. Overflow
# coalesces full state; append-only chat events on a stuck client disconnect it.
CLIENT_SEND_QUEUE_MAX = int(os.environ.get("PENTACLE_CLIENT_SEND_QUEUE_MAX", "256"))
CLIENT_SEND_QUEUE_WARN_RATIO = 0.80
COALESCIBLE_BROADCAST_FRAME_TYPES = frozenset({
    "host.status", "session.inventory", "working.state",
    "schedule.inventory",
    "hosts.stats",
    # A complete replacement: a slow client only needs the newest limits frame.
    "limits.update",
})
#: Bounded backfill clamp (lifted): a client can never pull more than this many
#: recent events in one `request_stream_events`. Env-overridable like v1.
RECENT_LIMIT = int(os.environ.get("PENTACLE_RECENT_LIMIT", "500"))
# A resumable request reads at most one fixed-size page before yielding a frame.
# This is deliberately not environment-overridable: it is an implementation
# bound, not an operational knob.
STREAM_EVENTS_PAGE_MAX = 8
# Mobile and desktop clients accept websocket messages below 1 MiB. Each
# resumable frame is assembled to this fixed wire budget; unlike the retired
# whole-reply cap, no oversized history is fetched and then trimmed.
STREAM_EVENTS_FRAME_BUDGET_BYTES = 900 * 1024
INSPECT_DEFAULT_EVENT_TAIL = 50
INSPECT_TERMINAL_STATUSES = tuple(sorted(TERMINAL_REPORT_STATUSES))
# v2's send path deliberately has no durable send-frame source. Returning a
# typed marker keeps that capability gap distinct from a genuine empty list;
# adding a second send-frame cache here would violate the v2 subtraction rule.
SEND_FRAMES_NOT_IMPLEMENTED = {
    "status": "not_implemented",
    "source": "v2_comms.send",
    "reason": "v2 does not persist send outcomes for inspect",
}


@dataclass(frozen=True)
class _EncodedFrame:
    """A text frame whose JSON was produced away from the asyncio loop."""

    frame_type: str
    payload: str


def _encode_frame(payload: Any) -> str:
    if isinstance(payload, dict) and payload.get("type") in {"snapshot", "session.inventory"}:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(payload)


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _event_frame_parts(
    *,
    frame_type: str,
    request_id: Any,
    stream_id: str,
    complete: bool,
) -> tuple[bytes, bytes]:
    """Return the JSON prefix/suffix around the events array.

    The event objects are encoded separately and appended once. Keeping the
    envelope separate makes the byte budget exact without repeatedly dumping a
    growing candidate payload.
    """
    envelope: dict[str, Any] = {
        "type": frame_type,
        "stream_id": stream_id,
        "events": [],
        "complete": complete,
    }
    if request_id is not None:
        envelope["request_id"] = request_id
    encoded = _json_bytes(envelope)
    marker = b'"events":[]'
    marker_start = encoded.find(marker)
    if marker_start < 0:  # pragma: no cover - fixed local envelope
        raise RuntimeError("request_stream_events envelope lost its events marker")
    prefix_end = marker_start + len(b'"events":[')
    suffix_start = marker_start + len(marker)
    return encoded[:prefix_end], b"]" + encoded[suffix_start:]


def _assemble_event_frames(
    events: list[dict[str, Any]],
    *,
    request_id: Any,
    stream_id: str,
    force_chunks: bool,
) -> list[_EncodedFrame]:
    """Encode one bounded page in one pass and return wire-ready frames.

    Events are encoded once each. When the page is chunked, the newest chunk is
    emitted first and each chunk itself remains oldest-first. The client can
    therefore persist the minimum `daemon_seq` from any delivered chunk and
    resume with `before_daemon_seq` after a disconnect.
    """
    event_json = [_json_bytes(event) for event in events]
    direct_type = "request_stream_events.ok"
    direct_prefix, direct_suffix = _event_frame_parts(
        frame_type=direct_type,
        request_id=request_id,
        stream_id=stream_id,
        complete=True,
    )
    direct_size = len(direct_prefix) + len(direct_suffix)
    direct_size += sum(len(item) for item in event_json) + max(0, len(event_json) - 1)
    if not force_chunks and direct_size <= STREAM_EVENTS_FRAME_BUDGET_BYTES:
        return [_EncodedFrame(
            direct_type,
            (direct_prefix + b",".join(event_json) + direct_suffix).decode("utf-8"),
        )]

    chunk_type = "request_stream_events.chunk"
    chunk_prefix, chunk_suffix = _event_frame_parts(
        frame_type=chunk_type,
        request_id=request_id,
        stream_id=stream_id,
        complete=False,
    )
    base_size = len(chunk_prefix) + len(chunk_suffix)
    frames: list[_EncodedFrame] = []
    pending_newest_first: list[bytes] = []
    pending_size = base_size

    def emit_pending() -> None:
        if not pending_newest_first:
            return
        ordered = list(reversed(pending_newest_first))
        payload = chunk_prefix + b",".join(ordered) + chunk_suffix
        if len(payload) > STREAM_EVENTS_FRAME_BUDGET_BYTES:  # pragma: no cover
            raise RuntimeError("request_stream_events chunk exceeded its byte budget")
        frames.append(_EncodedFrame(chunk_type, payload.decode("utf-8")))

    for encoded_event in reversed(event_json):
        candidate_size = pending_size + len(encoded_event) + (1 if pending_newest_first else 0)
        if pending_newest_first and candidate_size > STREAM_EVENTS_FRAME_BUDGET_BYTES:
            emit_pending()
            pending_newest_first = []
            pending_size = base_size
            candidate_size = pending_size + len(encoded_event)
        if candidate_size > STREAM_EVENTS_FRAME_BUDGET_BYTES:
            raise ValueError("request_stream_events event exceeds its frame byte budget")
        pending_newest_first.append(encoded_event)
        pending_size = candidate_size
    emit_pending()
    return frames


def _encoded_stream_events_error(request_id: Any, error: str) -> _EncodedFrame:
    payload: dict[str, Any] = {
        "type": "request_stream_events.error",
        "error_code": "internal_error",
        "error": error,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    return _EncodedFrame("request_stream_events.error", _json_bytes(payload).decode("utf-8"))

# v1 handshake parity: the daemon SPEAKS FIRST with an unsolicited `welcome`
# frame the instant a client connects (v1 chat_streamd.py:19120-19139). The
# desktop client parks in `await_welcome` and will NOT send its `hello` until it
# receives this frame — any other first frame fails its handshake and drops it
# into degraded/local-tmux mode (raw window names, unfiltered hidden workers,
# peer-probe phantoms). v2 had dropped the welcome, so every real client hung on
# connect. The `auth.operator` challenge mirrors v1's shape so a desktop built
# for operator-auth-v2 can authenticate its UI principal. The nonce, proof
# transcript, client kinds, and expiry are the shared v1 contract verbatim.
def _welcome_frame(*, nonce: str, expires_at: float, runtime_sha: str = "") -> dict[str, Any]:
    """The unsolicited first frame every client requires before sending hello."""
    return {
        "type": "welcome",
        "runtime_sha": runtime_sha,
        "auth_required": False,
        "auth": {
            "operator": {
                "protocol_version": operator_auth.AUTH_PROTOCOL_VERSION,
                "scheme": operator_auth.AUTH_SCHEME,
                "nonce": nonce,
                "expires_at": expires_at,
            }
        },
    }


def _is_send_frame(raw: Any) -> bool:
    """Cheap pre-dispatch peek for send and answer mutations. Used to
    let accepted durable work outlive the submitting connection
    (spec_example_2026_01). Any
    parse failure falls through to the normal (connection-scoped) path."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(msg, dict) and str(msg.get("type") or "") in {
        "send", "notification.resolve", "notification.resolve_by_dedup", "prompt.answer",
    }


class Server:
    """The accept loop and the single generic dispatch table."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7791,
        store: Any = None,
        sessions: Any = None,
        spawnctl: Any = None,
        comms: Any = None,
        ledger: Any = None,
        local_host: str = "localhost",
        binds: list[str] | None = None,
        hosts: Any = None,
        reconciler: Any = None,
        token_telemetry: SeatTokenTelemetry | None = None,
    ) -> None:
        #: Interfaces to bind (v1 parity: Tailscale IP + 127.0.0.1). `--host`
        #: stays a single-bind alias; `--bind` (repeatable) lists all interfaces.
        #: All binds share one port (port-bind-first order, spec constraint 1),
        #: so a multi-bind server needs an explicit `--port`.
        self.binds = [b for b in (binds or []) if b] or [host]
        self.host = self.binds[0]
        self.port = port
        self.store = store
        self.sessions = sessions
        self.spawnctl = spawnctl
        self.token_telemetry = (
            token_telemetry
            or getattr(spawnctl, "token_telemetry", None)
            or SeatTokenTelemetry()
        )
        self.comms = comms
        # Attached by main after the durable store is available.  Keeping it
        # optional makes old deployments and focused unit servers unchanged.
        self.assistant_composite: Any = None
        #: The peer probe pool. When present, `hello`/`snapshot` serves its live
        #: hosts dict (local entry + every peer's binary reachability); absent,
        #: the snapshot carries the local-only entry (unit tests).
        self.hosts = hosts
        #: Durable row↔presence reconciler, attached by main after construction
        #: just like the ledger/notify subsystems.
        self.reconciler = reconciler
        #: Assigned by `main.py` after construction — the ledger needs
        #: `broadcast`, which only exists once the server does.
        self.ledger = ledger
        #: Assigned by `main.py` after construction (same reason as `ledger`).
        #: When present, the hello snapshot carries the open Updates cards and
        #: its verb handlers are merged into the dispatch table.
        self.notify: Any = None
        #: Assigned by `main.py` after construction. The blob store owns partial
        #: upload state; the server tears down a closed connection's uploads
        #: through it (spec: the connection is an upload's one owning path).
        self.blobs: Any = None
        #: Assigned by `main.py` after construction. Its fixed-order cache is
        #: projected into subscribed hello snapshots as `limits`.
        self.limits: Any = None
        #: Live trackers are attached by `main.py` after the local mirror and
        #: remote presence observers are constructed. Unit-only Server
        #: instances still expose the stable empty v1-parity map.
        self.working_state_trackers: list[Any] = []
        #: Assigned by `main.py` after construction. Lifecycle audit is
        #: read-only and intentionally absent from unit-only Server instances.
        self.lifecycle: Any = None
        self._clients: set[Any] = set()
        #: Broadcasts stay bounded behind one writer per client. RPC replies
        #: write from their request task and use the shared send lock below.
        self._client_send_queues: dict[Any, asyncio.Queue] = {}
        self._client_writer_tasks: dict[Any, asyncio.Task] = {}
        self._client_send_locks: dict[Any, asyncio.Lock] = {}
        #: spec_example_2026_01:
        #: an accepted `send`'s injection must survive the SUBMITTING connection
        #: closing. Per-connection request tasks are cancelled on disconnect
        #: (see `_handle_client`), so send-injection tasks are tracked HERE at
        #: daemon level instead — never cancelled by a disconnect, drained on
        #: shutdown. Physical delivery (and its per-pane FIFO order via
        #: `comms._pane_input_lock`) is preserved; the result frame is delivered
        #: best-effort iff the submitter is still attached (direct send no-ops a
        #: gone client). Durable receipt queryability is supplied separately by
        #: the append-only receipt log, never by this task registry.
        self._detached_send_tasks: set[asyncio.Task] = set()
        self._client_queue_warned: set[Any] = set()
        self._client_identities: dict[Any, str] = {}
        # Per-client hello.subscribe state drives projection of broadcasts and
        # snapshot/list/history reads. Defaults are top-level-only.
        self._client_include_subagents: dict[Any, bool] = {}
        self._client_opened_by_host_ids: dict[Any, frozenset[str] | None] = {}
        self._client_exclude_event_types: dict[Any, frozenset[str]] = {}
        self._client_events_mode: dict[Any, str] = {}
        self._client_assistant_composite_v1: dict[Any, bool] = {}
        #: Per-client last-sent digest, keyed by coalescing key, of the most
        #: recent COALESCIBLE broadcast frame handed to that client. A frame
        #: byte-identical to the last one is suppressed (zero new visible state):
        #: a busy fleet re-broadcasts session.inventory on every mutation, and a
        #: full-mode client sees ~40 KB frames that are mostly identical -> queue
        #: fills -> 1011. Reset on (re)connect (the hello snapshot reseeds).
        self._client_last_sent_digest: dict[Any, dict[tuple, int]] = {}
        #: The one frame currently owned by each client's writer, if it is a
        #: coalescible broadcast.  This is send-idle state, not a second
        #: delivery/dedupe ledger: broadcast needs it only to avoid treating a
        #: last-delivered A as redundant while a different B for the same key
        #: is blocked in websocket.send().
        self._client_inflight_coalescible: dict[Any, tuple[str, str]] = {}
        # A host-stats subscriber is admitted only beside its hello replay,
        # under _host_stats_order_lock. Ordinary broadcasts retain their
        # pre-hello registration semantics.
        self._host_stats_clients: set[Any] = set()
        # Stream identity proven by a per-request token. It is bound to the
        # websocket for the lifetime of the connection so a caller cannot use
        # one authenticated request to pivot the same socket to another stream.
        self._client_authenticated_streams: dict[Any, str] = {}
        self._client_token_hashes: dict[Any, str] = {}
        # The sole non-seat service principal is connection-bound after its
        # exact hello proof. It never enters the seat-token maps above.
        self._client_system_producers: dict[Any, str] = {}
        #: Wire-provided client names are claims only. A UI principal enters this
        #: connection-local map only after an auth-v2 proof checks against the
        #: existing operator credential registry.
        self.operator_credential_registry = operator_auth.OperatorCredentialRegistry()
        self._operator_challenges: dict[Any, tuple[str, float]] = {}
        self._connection_trust: dict[Any, operator_auth.ConnectionTrust] = {}
        self._unsupported_records: dict[str, dict[str, Any]] = {}
        self._unsupported_last_logged_at: dict[str, float] = {}
        self.window_schedule: Any = None
        self._ws_server: Any = None
        #: Spawn — and ONLY spawn — waits on this during the boot window: a
        #: spawn arriving before boot reconciliation settles could have its
        #: reservation adopted or released underneath it (QA #18). Default SET
        #: (ungated), so a Server not driven by `main.py`'s boot sequence (unit
        #: tests) is unaffected; `main.py` clears it before bind and sets it once
        #: `reconcile_spawn_intents()` returns. Port-bind-first (spec constraint
        #: 1) is untouched — every other verb serves the instant the port binds.
        self.spawn_ready = asyncio.Event()
        self.spawn_ready.set()
        #: `list_sessions` waits on this during the boot window. The port binds
        #: (spec constraint 1) and serves ping/hello immediately, but the
        #: in-memory inventory is empty until the first `sessions.refresh()`
        #: re-adopts the persisted open rows
        #: AFTER bind — so a restart could serve an empty `list_sessions` and make
        #: every live session vanish from the UI for the adoption window (B10).
        #: Gating just this verb closes that race deterministically instead of
        #: relying on adoption winning it. Default SET (ungated) so a Server not
        #: driven by `main.py`'s boot sequence (unit tests) is unaffected;
        #: `main.py` clears it before bind and sets it once `refresh()` returns.
        self.inventory_ready = asyncio.Event()
        self.inventory_ready.set()
        # ONE generic dispatch table. Unknown types fall through to
        # `unsupported_in_v2` — never a per-verb error branch. Keys and reply
        # types are v1's vocabulary verbatim.
        self.handlers: dict[str, Handler] = {
            "ping": self._on_ping,
            "hello": self._on_hello,
            "list_sessions": self._on_list_sessions,
            "inspect_stream": self._on_inspect_stream,
            "thread.read": self._on_thread_read,
            "rename": self._on_rename,
            "set_visibility": self._on_set_visibility,
            "status_card": self._on_status_card,
            "spawn": self._on_spawn,
            "assistant.lifecycle": self._on_assistant_lifecycle,
            **{f"consent.{verb}": self._on_consent for verb in ("request", "approve", "deny", "cancel", "status")},
            **{f"consent_key.{verb}": self._on_consent for verb in ("enroll_code", "prepare", "enroll", "confirm", "revoke", "list")},
            **{f"coordination.spec_issue.{verb}": self._on_qa_issue for verb in ("adjudicate", "diagnose", "show")},
            "await_spawn": self._on_await_spawn,
            "spawn_cancel": self._on_spawn_cancel,
            "spawn_status": self._on_spawn_status,
            "spawn_freeze": self._on_spawn_freeze,
            "spawn_unfreeze": self._on_spawn_unfreeze,
            "reparent": self._on_reparent,
            "close": self._on_close,
            "tell": self._on_tell,
            "send": self._on_send,
            "transcribe_blob": self._on_transcribe_blob,
            "assistant.publish": self._on_assistant_publish,
            "assistant.operation": self._on_assistant_operation,
            "send_image": self._on_send_image,
            "send.receipt.get": self._on_send_receipt_get,
            "ledger_get": self._on_ledger_get,
            "inbound_audit": self._on_inbound_audit,
            "report": self._on_report,
            "await_report": self._on_await_report,
            "role.set": self._on_role_set,
            "role.get": self._on_role_get,
            "request_stream_events": self._on_request_stream_events,
            "daemon.stats": self._on_daemon_stats,
            "reconcile.status": self._on_reconcile_status,
        }
        self.local_host = local_host
        from watch_wake import WatchWake
        self.watch_wake = WatchWake(getattr(self.sessions, "store", None), self.sessions)
        self.handlers.update(self.watch_wake.wire_handlers())
        self.runtime_sha = ""
        self._host_stats: dict[str, dict[str, Any]] = {}
        self._host_stats_order_lock = asyncio.Lock()

    def host_stats_snapshot(self) -> dict[str, dict[str, Any]]:
        return {host: dict(stats) for host, stats in self._host_stats.items()}

    def hosts_stats_frame(self) -> dict[str, Any]:
        return {"type": "hosts.stats", "hosts": self.host_stats_snapshot()}

    async def merge_host_stats(self, host: str, stats: dict[str, Any]) -> None:
        async with self._host_stats_order_lock:
            normalized = validate_machine_stats(stats, host)
            if normalized is None:
                raise ValueError("bad_stats")
            normalized["sampled_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self._host_stats[host] = normalized
            await self.broadcast(self.hosts_stats_frame())

    # -- lifecycle ---------------------------------------------------------

    async def bind(self) -> int:
        """Bind every interface in `self.binds` on one port and start accepting.
        Returns the bound port. Call FIRST (spec constraint 1). A list host binds
        all given interfaces on the same port (v1 dual-bind parity: Tailscale IP +
        127.0.0.1); a single bind keeps the plain-string form unchanged."""
        target: Any = self.binds if len(self.binds) > 1 else self.binds[0]
        try:
            self._ws_server = await serve(
                self._handle_client, target, self.port, max_size=WS_MAX_SIZE,
            )
        except OSError as exc:
            bind_errno = exc.errno or errno.EADDRNOTAVAIL
            log.error("v2 bind failed host=%s errno=%s", ",".join(self.binds), bind_errno)
            await self.close()
            if exc.errno is None:
                raise OSError(bind_errno, str(exc)) from exc
            raise

        bound_addresses = {
            (sock.family, sock.getsockname()[0])
            for sock in (getattr(self._ws_server, "sockets", None) or ())
        }
        failed_binds: list[tuple[str, int | None]] = []
        for bind in self.binds:
            try:
                requested_addresses = {
                    (family, sockaddr[0])
                    for family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                        bind,
                        self.port,
                        type=socket.SOCK_STREAM,
                        flags=socket.AI_PASSIVE,
                    )
                }
            except OSError as exc:
                failed_binds.append((bind, exc.errno))
                continue
            if requested_addresses.isdisjoint(bound_addresses):
                failed_binds.append((bind, errno.EADDRNOTAVAIL))

        if failed_binds:
            for bind, bind_errno in failed_binds:
                log.error("v2 bind failed host=%s errno=%s", bind, bind_errno)
            await self.close()
            bind, bind_errno = failed_binds[0]
            raise OSError(
                bind_errno or errno.EADDRNOTAVAIL,
                f"requested bind unavailable: {bind}",
            )

        for sock in getattr(self._ws_server, "sockets", None) or []:
            self.port = sock.getsockname()[1]
            break
        log.info("v2 bound host=%s port=%s", ",".join(self.binds), self.port)
        return self.port

    async def serve_forever(self) -> None:
        assert self._ws_server is not None, "bind() must be called before serve_forever()"
        await self._ws_server.serve_forever()

    async def close(self) -> None:
        # spec_example_2026_01:
        # let accepted sends finish injecting before teardown, then cancel any
        # stragglers so shutdown stays bounded.
        if self._detached_send_tasks:
            pending = tuple(self._detached_send_tasks)
            _, still_running = await asyncio.wait(pending, timeout=5.0)
            for task in still_running:
                task.cancel()
            if still_running:
                await asyncio.gather(*still_running, return_exceptions=True)
        for task in tuple(self._client_writer_tasks.values()):
            task.cancel()
        self._client_writer_tasks.clear()
        if self._ws_server is None:
            return
        self._ws_server.close()
        try:
            await asyncio.wait_for(self._ws_server.wait_closed(), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):  # pragma: no cover
            pass
        self._ws_server = None

    # -- connection --------------------------------------------------------

    async def _handle_client(self, websocket: Any) -> None:
        self._register_client(websocket)
        # v1 parity: speak first. It uses the same send lock as RPC results, so
        # a client that sends hello without waiting still receives welcome first.
        nonce, expires_at = operator_auth.new_nonce()
        self._operator_challenges[websocket] = (nonce, expires_at)
        if not await self._send_direct(websocket, json.dumps(_welcome_frame(
            nonce=nonce, expires_at=expires_at, runtime_sha=self.runtime_sha,
        ))):
            return
        inflight: set[asyncio.Task] = set()
        hello_barrier: asyncio.Task | None = None
        try:
            async for raw in websocket:
                # One task per request: a verb that parks (`await_report`) must
                # not hold up the requests behind it. Replies correlate by
                # `request_id`, so out-of-order completion is expected.
                try:
                    is_hello = json.loads(raw).get("type") == "hello"
                except (ValueError, TypeError, AttributeError):
                    is_hello = False
                task = asyncio.create_task(self._serve_after_hello(websocket, raw, hello_barrier))
                if is_hello:
                    hello_barrier = task
                # spec_example_2026_01:
                # an accepted send or answer must NOT be aborted when this
                # submitter disconnects, so track send tasks at daemon level
                # instead of in `inflight` (which the finally below cancels).
                if _is_send_frame(raw):
                    self._detached_send_tasks.add(task)
                    task.add_done_callback(self._detached_send_tasks.discard)
                else:
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
        except ConnectionClosed:
            pass
        finally:
            for task in inflight:
                task.cancel()
            # Drain the cancelled request tasks before tearing blob uploads down:
            # a cancelled `upload_blob_chunk` task may still be mid-`_append`/
            # `_finish` in the blob executor, and `abort_connection` must not
            # race that disk work (close/unlink an fd or temp file it is using).
            # Awaiting here lets each task unwind (its executor slice completes,
            # its per-upload lock releases) so teardown is race-free.
            if inflight:
                await asyncio.gather(*inflight, return_exceptions=True)
            self._unregister_client(websocket)
            # The connection owns its in-flight blob uploads; drop their partial
            # state (fd, temp file) now that it is gone. A later chunk for the
            # same request_id then meets `upload_blob_unknown_request_id`.
            if self.blobs is not None:
                await self.blobs.abort_connection(websocket)

    async def _serve_after_hello(self, websocket: Any, raw: Any, barrier: asyncio.Task | None) -> None:
        # CLI callers may send the RPC immediately after hello. Authenticate
        # that hello first, without serializing long-running ordinary RPCs.
        if barrier is not None:
            await asyncio.shield(barrier)
        await self._serve(websocket, raw)

    async def _serve(self, websocket: Any, raw: Any) -> None:
        # Reply frames write straight to their originating socket. A handler may
        # return an async iterator to STREAM frames (blob fetch: 1 MiB slices
        # read + encoded off-loop, one at a time, so the loop never holds the
        # whole blob); the per-client send lock keeps blob slices contiguous.
        # Correlated history chunks instead yield the socket between frames.
        try:
            request = json.loads(raw)
        except (TypeError, ValueError):
            request = None
        if isinstance(request, dict) and request.get("type") == "hello":
            # Hello's fleet replay must share an ordering domain with the
            # broadcast that can update it. The host-stats subscription is
            # admitted only in this critical section, so no hosts.stats frame
            # can queue before the replay; merge_host_stats waits until the
            # direct replay lands.
            async with self._host_stats_order_lock:
                result = await self._dispatch(raw, websocket=websocket)
                if not any(
                    isinstance(frame, dict) and frame.get("type") == "hello.error"
                    for frame in result
                ):
                    if (websocket not in self._client_system_producers and (
                            self._is_loopback_client(websocket)
                            or self._operator_authenticated(websocket)
                            or websocket in self._client_authenticated_streams)):
                        self._activate_client(websocket)
                    result = [
                        self.hosts_stats_frame()
                        if isinstance(frame, dict) and frame.get("type") == "hosts.stats"
                        else frame
                        for frame in result
                    ]
                await self._send_direct(websocket, result)
            return
        result = await self._dispatch(raw, websocket=websocket)
        await asyncio.sleep(0)
        sent = await self._send_direct(
            websocket, result,
            interleave_history=isinstance(request, dict) and request.get("type") == "request_stream_events",
        )
        if isinstance(request, dict) and request.get("type") in {
            "notification.resolve", "notification.resolve_by_dedup", "prompt.answer",
        }:
            for frame in result:
                if isinstance(frame, dict):
                    notification = frame.get("notification") or {}
                    question = frame.get("question") or {}
                    log.info("answer response send request_id=%s nid=%s type=%s socket_send=%s",
                             frame.get("request_id"), notification.get("notification_id")
                             or question.get("notification_id") or request.get("notification_id"),
                             frame.get("type"), "completed" if sent else "failed",
                             extra={"subsystem": "server", "bug_ref": "notification_answer_disconnect_delivery_2026_09"})

    async def broadcast(self, frame: dict[str, Any]) -> None:
        """Project a top-level frame for each connected client before enqueue."""
        frame_type = str(frame.get("type") or "")
        recipients = self._host_stats_clients if frame_type == "hosts.stats" else self._clients
        if not recipients:
            return
        # All connected clients with the same subscription receive the same
        # projected frame. Render it once per subscription group instead of
        # rebuilding and JSON-encoding a fleet inventory for every socket.
        groups: dict[
            tuple[bool, frozenset[str] | None, frozenset[str], bool, str, bool], list[Any],
        ] = {}
        for websocket in tuple(recipients):
            if websocket in self._client_system_producers:
                continue
            if not self._is_loopback_client(websocket) and not self._operator_authenticated(websocket):
                auth = await self._auth_context(websocket, {})
                if not auth.get("token_verified"):
                    continue
            key = (
                bool(self._client_include_subagents.get(websocket, False)),
                self._client_opened_by_host_ids.get(websocket),
                self._client_exclude_event_types.get(websocket, frozenset()),
                self._operator_authenticated(websocket),
                # events_mode partitions the group: `_frame_for_client` reduces
                # session.inventory for summary clients, so a summary and a full
                # client must not share one rendered projection.
                self._client_events_mode.get(websocket, "full"),
                # Composite inventory and events differ by negotiated capability.
                # Never reuse an unsupported client's projection for a capable one.
                bool(self._client_assistant_composite_v1.get(websocket, False)),
            )
            groups.setdefault(key, []).append(websocket)
        for clients in groups.values():
            projected = self._frame_for_client(clients[0], frame_type, frame)
            if projected is None:
                continue
            encoded = _encode_frame(projected)
            # Per-client byte-identical suppression for coalescible (latest-wins)
            # frames: a frame equal to the one this client was last sent for the
            # same key carries no new visible state. Append-only frames
            # (chat.event) are never deduped.
            dedupe = frame_type in COALESCIBLE_BROADCAST_FRAME_TYPES
            key = self._coalesce_frame_key(frame_type, encoded) if dedupe else None
            digest = hash(encoded) if dedupe else 0
            for websocket in clients:
                # Suppress only a frame byte-identical to the one last DELIVERED
                # to this client for the same key — the digest is recorded by the
                # writer loop AFTER a successful send, never at enqueue, so a
                # frame coalesced-away or dropped before delivery is re-sent (it
                # was never delivered) and the client always converges.
                if (dedupe
                        and self._client_last_sent_digest.get(websocket, {}).get(key) == digest
                        and not self._has_different_pending_coalescible(
                            websocket, key, encoded)):
                    continue
                self._enqueue(websocket, frame_type, encoded)

        # A committed ingest batch can call broadcast repeatedly without any
        # awaited operation suspending. Give the existing socket writers a turn
        # before the next frame; otherwise a healthy reader's bounded queue can
        # overflow solely because its writer never ran. A blocked writer still
        # accumulates pressure and follows the unchanged overflow policy.
        await asyncio.sleep(0)

    def _register_client(self, websocket: Any) -> None:
        self._clients.add(websocket)
        if websocket not in self._client_send_queues:
            self._client_send_queues[websocket] = asyncio.Queue(maxsize=CLIENT_SEND_QUEUE_MAX)
        self._client_send_locks.setdefault(websocket, asyncio.Lock())
        task = self._client_writer_tasks.get(websocket)
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._client_writer_loop(websocket))
        self._client_writer_tasks[websocket] = task

    def _activate_client(self, websocket: Any) -> None:
        self._clients.add(websocket)
        self._host_stats_clients.add(websocket)

    def _unregister_client(self, websocket: Any) -> None:
        self._clients.discard(websocket)
        self._client_queue_warned.discard(websocket)
        self._client_identities.pop(websocket, None)
        self._client_include_subagents.pop(websocket, None)
        self._client_opened_by_host_ids.pop(websocket, None)
        self._client_exclude_event_types.pop(websocket, None)
        self._client_events_mode.pop(websocket, None)
        self._client_assistant_composite_v1.pop(websocket, None)
        self._client_last_sent_digest.pop(websocket, None)
        self._client_inflight_coalescible.pop(websocket, None)
        self._host_stats_clients.discard(websocket)
        self._client_authenticated_streams.pop(websocket, None)
        self._client_token_hashes.pop(websocket, None)
        self._client_system_producers.pop(websocket, None)
        self._operator_challenges.pop(websocket, None)
        self._connection_trust.pop(websocket, None)
        self._client_send_queues.pop(websocket, None)
        self._client_send_locks.pop(websocket, None)
        task = self._client_writer_tasks.pop(websocket, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _client_writer_loop(self, websocket: Any) -> None:
        try:
            while True:
                queue = self._client_send_queues.get(websocket)
                if queue is None:
                    return
                _frame_type, frame = await queue.get()
                lock = self._client_send_locks.get(websocket)
                if lock is None:
                    return
                coalescible = _frame_type in COALESCIBLE_BROADCAST_FRAME_TYPES
                if coalescible:
                    self._client_inflight_coalescible[websocket] = (_frame_type, frame)
                try:
                    async with lock:
                        await websocket.send(frame)
                    # Record the DELIVERED digest (not at enqueue): byte-identical
                    # coalescible frames are suppressed in broadcast() only once this
                    # exact state has actually reached the client, so a frame
                    # coalesced-away or dropped before delivery is never suppressed.
                    if coalescible:
                        self._client_last_sent_digest.setdefault(websocket, {})[
                            self._coalesce_frame_key(_frame_type, frame)
                        ] = hash(frame)
                finally:
                    if coalescible and self._client_inflight_coalescible.get(websocket) == (_frame_type, frame):
                        self._client_inflight_coalescible.pop(websocket, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a broken socket kills its writer, not the daemon
            self._unregister_client(websocket)

    @staticmethod
    def _coalesce_frame_key(frame_type: str, frame: str) -> tuple:
        """Coalescing identity for a queued full-state frame."""
        if frame_type not in ("host.status", "working.state"):
            return (frame_type,)
        try:
            payload = json.loads(frame)
        except (TypeError, ValueError):
            return (frame_type, object())
        if frame_type == "host.status":
            return (frame_type, str(payload.get("host") or ""))
        return (frame_type, str(payload.get("stream_id") or ""))

    def _has_different_pending_coalescible(self, websocket: Any, key: tuple, frame: str) -> bool:
        """Whether a differing latest-wins frame for ``key`` is unsent.

        A delivered digest alone cannot prove an arriving byte-identical A is
        redundant: delivered A -> pending B -> arriving A must deliver that
        final A.  ``asyncio.Queue`` has no non-destructive public inspection
        API; its backing deque is inspected only while this event-loop turn is
        running (no await here), just like the queue compaction path below.
        """
        inflight = self._client_inflight_coalescible.get(websocket)
        if (inflight is not None
                and self._coalesce_frame_key(inflight[0], inflight[1]) == key
                and inflight[1] != frame):
            return True
        queue = self._client_send_queues.get(websocket)
        if queue is None:
            return False
        for pending_type, pending_frame in tuple(queue._queue):
            if (pending_type in COALESCIBLE_BROADCAST_FRAME_TYPES
                    and self._coalesce_frame_key(pending_type, pending_frame) == key
                    and pending_frame != frame):
                return True
        return False

    def _evict_superseded_queue_frames(self, queue: asyncio.Queue) -> bool:
        """Coalesce replaceable state without dropping events."""
        items = [queue.get_nowait() for _ in range(queue.qsize())]
        for _ in items:
            queue.task_done()
        kept_reversed: list[tuple[str, str]] = []
        seen_keys: set = set()
        for item in reversed(items):
            if item[0] in COALESCIBLE_BROADCAST_FRAME_TYPES:
                key = self._coalesce_frame_key(item[0], item[1])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
            kept_reversed.append(item)
        for item in reversed(kept_reversed):
            queue.put_nowait(item)
        return len(kept_reversed) < len(items)

    def _enqueue(self, websocket: Any, frame_type: str, frame: str) -> bool:
        if websocket not in self._clients:
            return False
        queue = self._client_send_queues.get(websocket)
        if queue is None:
            return False
        try:
            queue.put_nowait((frame_type, frame))
        except asyncio.QueueFull:
            enqueued = False
            if self._evict_superseded_queue_frames(queue):
                try:
                    queue.put_nowait((frame_type, frame))
                    enqueued = True
                except asyncio.QueueFull:
                    pass
            if not enqueued:
                # Coalescing freed nothing (the backlog is incompressible, e.g.
                # a chat.event burst). Drop the CONNECTION (1011): it SIGNALS the
                # loss so the client reconnects and refetches — a bounded,
                # recoverable overload policy. Steady-state desktop pressure is
                # removed upstream by the byte-identical dedupe (below), so this
                # path is a rare true-overload backstop, never the normal case.
                log.warning("slow_consumer overflow: dropping client=%s peer=%s (queue_max=%d)",
                            self._client_identities.get(websocket, "unknown"),
                            getattr(websocket, "remote_address", None), CLIENT_SEND_QUEUE_MAX)
                self._drop_slow_consumer(websocket)
                return False
        depth = queue.qsize()
        warn_at = max(1, int(CLIENT_SEND_QUEUE_MAX * CLIENT_SEND_QUEUE_WARN_RATIO))
        if depth >= warn_at:
            # A client this far behind is on a link that cannot drain the fleet's
            # full-state churn. Collapse its superseded frames NOW (lossless,
            # latest-wins) instead of letting the backlog climb to a hard
            # QueueFull: it cuts the volume a slow mobile must carry and parse and
            # prevents the 1011 overflow drop below — the daemon-owned half of the
            # mobile 4000 focused_heartbeat_timeout reconnect churn (the pong reply
            # path itself is healthy; see the spec). Only fire when the arriving
            # frame is itself coalescible, so an incompressible chat.event flood
            # falls to the QueueFull drop rather than paying an O(queue) scan per
            # frame.
            if frame_type in COALESCIBLE_BROADCAST_FRAME_TYPES:
                self._evict_superseded_queue_frames(queue)
            if websocket not in self._client_queue_warned:
                self._client_queue_warned.add(websocket)
                log.warning("slow_consumer queue depth=%d/%d client=%s peer=%s", depth,
                            CLIENT_SEND_QUEUE_MAX, self._client_identities.get(websocket, "unknown"),
                            getattr(websocket, "remote_address", None))
        return True

    def _drop_slow_consumer(self, websocket: Any) -> None:
        self._unregister_client(websocket)
        asyncio.create_task(self._close_ws(websocket, 1011, "slow_consumer"))

    @staticmethod
    async def _close_ws(websocket: Any, code: int, reason: str) -> None:
        close = getattr(websocket, "close", None)
        if callable(close):
            try:
                await close(code=code, reason=reason)
            except Exception:  # noqa: BLE001
                pass

    async def _send_direct(self, websocket: Any, frames: Any, *, interleave_history: bool = False) -> bool:
        """Write greeting or results without entering broadcast fanout."""
        lock = self._client_send_locks.get(websocket)
        if websocket not in self._clients or lock is None:
            return False
        try:
            if interleave_history and hasattr(frames, "__aiter__"):
                try:
                    # Fetch/encode the next page outside the socket lock: a
                    # paused backfill must not block readiness or pongs.
                    async for frame in frames:
                        payload = frame.payload if isinstance(frame, _EncodedFrame) else _encode_frame(frame)
                        async with lock:
                            if websocket not in self._clients:
                                return False
                            await websocket.send(payload)
                        # send() and a ready iterator need not suspend. Let the
                        # broadcast writer join the fair lock queue each frame.
                        await asyncio.sleep(0)
                finally:
                    close = getattr(frames, "aclose", None)
                    if callable(close):
                        await close()
                return True
            async with lock:
                if hasattr(frames, "__aiter__"):
                    async for frame in frames:
                        await websocket.send(
                            frame.payload if isinstance(frame, _EncodedFrame) else _encode_frame(frame)
                        )
                else:
                    for frame in ((frames,) if isinstance(frames, str) else frames):
                        await websocket.send(frame if isinstance(frame, str) else _encode_frame(frame))
        except ConnectionClosed:
            self._unregister_client(websocket)
            return False
        return True


    # -- dispatch ----------------------------------------------------------

    async def _dispatch(self, raw: Any, *, websocket: Any = None) -> list[dict[str, Any]]:
        """Parse, route through the one table, correlate. Returns frames to send."""
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return [{"type": "protocol.error", "error_code": "bad_json"}]
        if not isinstance(msg, dict):
            return [{"type": "protocol.error", "error_code": "bad_envelope"}]

        verb = str(msg.get("type") or "")
        request_id = msg.get("request_id")
        caller = self._client_identities.get(websocket) if websocket is not None else None
        if verb == "hello" and websocket is not None:
            client = str(msg.get("client") or "").strip()
            if client:
                caller = client
                self._client_identities[websocket] = client
        handler = self.handlers.get(verb)
        if handler is None:
            return [self._unsupported(verb, request_id, caller=caller)]
        # Never let a wire client provide internal authorization fields. The
        # routing-integrity handler receives a server-derived context carrying
        # the owner of a validated stream token; a bare from_stream_id remains
        # only a claim and cannot authorize an acknowledgement.
        dispatch_msg = {
            key: value for key, value in msg.items() if not str(key).startswith("_")
        }
        if websocket is not None:
            dispatch_msg["_auth_context"] = await self._auth_context(websocket, msg)
            service_auth = dispatch_msg["_auth_context"]
            if service_auth.get("service_attempted"):
                if not service_auth.get("service_authenticated"):
                    return [self._auth_error_frame(
                        verb, request_id, "system_producer_auth_required"
                    )]
                if verb == "hello":
                    if not self._valid_system_producer_hello(msg):
                        return [self._auth_error_frame(
                            verb, request_id, "system_producer_auth_required"
                        )]
                elif verb != "notification.create":
                    return [self._auth_error_frame(
                        verb, request_id, "system_producer_forbidden"
                    )]
                elif not self._valid_system_notification_create(msg):
                    return [self._auth_error_frame(
                        verb, request_id, "system_producer_payload_invalid"
                    )]
            if not self._is_loopback_client(websocket) and verb not in {
                "ping", "hello", "enroll", "event.push", "host.stats",
            }:
                auth = dispatch_msg["_auth_context"]
                code = None
                if not any(auth.get(key) for key in (
                    "operator_authenticated", "token_verified", "service_authenticated",
                )):
                    code = "authentication_required"
                elif verb in {"grant_token", "spawn_freeze", "spawn_unfreeze"} and not (
                    auth.get("operator_authenticated") or auth.get("service_authenticated")
                ):
                    code = "operator_auth_required"
                if code:
                    denied = {"type": f"{verb}.error", "error_code": code}
                    if request_id is not None:
                        denied["request_id"] = request_id
                    return [denied]
            if verb in {
                "hello", "list_sessions", "inspect_stream", "request_stream_events", "send.receipt.get",
                # Upload verbs carry the owning connection so an interrupted
                # upload can be torn down when that connection closes.
                "upload_blob_init", "upload_blob_chunk",
                "upload_prompt_blob_init", "upload_prompt_blob_chunk",
            }:
                dispatch_msg["_client_websocket"] = websocket
        try:
            reply = await handler(dispatch_msg)
        except (VerbError, StatusCardError) as exc:  # expected business failures
            reply = {"type": f"{verb}.error", "error_code": exc.code, "error": str(exc),
                     **getattr(exc, "extra", {})}
        except Exception as exc:  # noqa: BLE001 - one generic error boundary
            log.exception("handler failed verb=%s", verb)
            reply = {"type": f"{verb}.error", "error_code": "internal_error", "error": str(exc)}
        # A streaming handler returns an async iterator; it sets `request_id` on
        # every frame itself (v1 echoes it on each blob frame), so it is passed
        # through untouched rather than list-wrapped and correlated here.
        if hasattr(reply, "__aiter__"):
            return reply
        frames = list(reply) if isinstance(reply, list) else [reply]
        if any(frame.get("error_code") == "unsupported_in_v2" for frame in frames):
            # Some lifted handlers reject a supported wire verb with the same
            # capability-gap code instead of falling through the dispatch table.
            # Keep those paths in the same aggregate as unknown verbs.
            self._record_unsupported(verb, caller=caller)
        if request_id is not None and frames:
            frames[0].setdefault("request_id", request_id)
        return frames

    @staticmethod
    def _token_auth_requested(msg: dict[str, Any]) -> bool:
        """Identify requests that actually carry or require seat identity."""
        return bool(
            "stream_token" in msg
            or "from_stream_id" in msg
            or "actor_stream_id" in msg
        )

    @staticmethod
    def _token_input_reason(token: Any) -> str | None:
        if token is None or token == "":
            return TOKEN_REASON_ABSENT
        if not isinstance(token, str):
            return TOKEN_REASON_MALFORMED
        if token != token.strip() or any(
            ord(char) < 0x21 or ord(char) > 0x7E for char in token
        ):
            return TOKEN_REASON_MALFORMED
        return None

    async def _auth_context(self, websocket: Any, msg: dict[str, Any]) -> dict[str, Any]:
        """Return connection-bound stream auth and record a safe reason code."""
        trust = self._connection_trust.get(websocket)
        operator_authenticated = self._operator_authenticated(websocket)
        context: dict[str, Any] = {
            "stream_id": "",
            "token_verified": False,
            "operator_authenticated": operator_authenticated,
            "operator_principal": f"operator:{trust.credential_id}" if operator_authenticated else "",
            "connection_client": self._client_identities.get(websocket),
            "transport": trust.transport if trust is not None else "legacy",
            "operator_trusted": bool(trust and trust.operator_trusted),
            "service_authenticated": False,
            "service_actor": "",
            "service_attempted": False,
            "peer_loopback": self._is_loopback_client(websocket),
            "local_admin_verified": (isinstance(msg.get("local_admin_token"), str)
                                     and self._is_loopback_client(websocket)
                                     and await asyncio.to_thread(local_admin.verify, msg.get("local_admin_token"))),
        }
        bound_system_actor = self._client_system_producers.get(websocket)
        if bound_system_actor is not None:
            claim = str(msg.get("from_stream_id") or "").strip()
            token_was_supplied = "stream_token" in msg
            credentials_changed = bool(
                (claim and claim != bound_system_actor)
                or (token_was_supplied and not self._verify_system_producer_token(msg.get("stream_token")))
            )
            context.update({
                "service_attempted": True,
                "service_authenticated": not credentials_changed,
                "service_actor": bound_system_actor if not credentials_changed else "",
                "reason_code": TOKEN_REASON_WRONG_SEAT if credentials_changed else TOKEN_REASON_VERIFIED,
            })
            return context

        cached_hash = self._client_token_hashes.get(websocket)
        claim = str(msg.get("from_stream_id") or "").strip()
        producer = str(msg.get("producer") or "").strip()
        token_matches = self._verify_system_producer_token(msg.get("stream_token"))
        service_attempted = bool(
            claim == FIXED_SYSTEM_PRODUCER_STREAM_ID
            or producer == FIXED_SYSTEM_PRODUCER_STREAM_ID
            or token_matches
        )
        if service_attempted:
            configured_id = str(os.environ.get(SYSTEM_PRODUCER_STREAM_ID_ENV) or "").strip()
            authenticated = bool(
                configured_id == FIXED_SYSTEM_PRODUCER_STREAM_ID
                and claim == configured_id
                and token_matches
                and msg.get("type") == "hello"
                and self._valid_system_producer_hello(msg)
            )
            if authenticated:
                # Bind only after the entire restricted hello is accepted.
                self._client_system_producers[websocket] = configured_id
            context.update({
                "service_attempted": True,
                "service_authenticated": authenticated,
                "service_actor": configured_id if authenticated else "",
                "reason_code": TOKEN_REASON_VERIFIED if authenticated else TOKEN_REASON_WRONG_SEAT,
            })
            return context

        if not self._token_auth_requested(msg) and not cached_hash:
            return context

        token = msg.get("stream_token")
        claim = str(msg.get("from_stream_id") or msg.get("actor_stream_id") or "").strip()
        # Keep wire-controlled values out of telemetry labels. The operation
        # label is intentionally a fixed vocabulary, even for malformed input.
        operation = "identity"
        # A tokenless RPC can follow an authenticated hello on this socket.
        # Keep only its hash and revalidate against current durable state.
        # An explicitly supplied bad token never falls back to the binding.
        use_binding = "stream_token" not in msg and cached_hash is not None
        reason_code = None if use_binding else self._token_input_reason(token)
        owner: str | None = None
        verified_generation: str | None = None
        retired_owner: dict[str, Any] | None = None

        if reason_code is None:
            if self.store is None:
                reason_code = TOKEN_REASON_INTERNAL_ERROR
            else:
                token_hash = cached_hash if use_binding else hashlib.sha256(token.encode("utf-8")).hexdigest()
                try:
                    state = await self.store.stream_token_state(token_hash)
                    if state is None:
                        reason_code = TOKEN_REASON_EXPIRED
                    elif state.get("token_hash_version") != STREAM_TOKEN_HASH_VERSION:
                        reason_code = TOKEN_REASON_INTERNAL_ERROR
                    elif state.get("status") != "open":
                        reason_code = TOKEN_REASON_EXPIRED
                        # Narrow read-back identity for a retired protected
                        # assistant's own handoff receipt. Never token_verified;
                        # SpawnCtl serves only the stored receipt with it.
                        closed_owner = str(state.get("stream_id") or "")
                        if (msg.get("type") == "spawn" and msg.get("handoff") is True
                                and closed_owner
                                and msg.get("handoff_from_stream_id") == closed_owner
                                and (not claim or claim == closed_owner)):
                            retired_owner = {
                                "stream_id": closed_owner,
                                "session_generation": state.get("session_generation"),
                                "token_hash": token_hash,
                            }
                    else:
                        owner = str(state.get("stream_id") or "").strip() or None
                        verified_generation = state.get("session_generation")
                        reason_code = (
                            TOKEN_REASON_VERIFIED
                            if owner
                            else TOKEN_REASON_INTERNAL_ERROR
                        )
                except Exception:  # noqa: BLE001 - auth is fail-closed
                    reason_code = TOKEN_REASON_INTERNAL_ERROR

        if reason_code == TOKEN_REASON_VERIFIED:
            prior = self._client_authenticated_streams.get(websocket)
            if (prior is not None and str(owner) != prior) or (
                claim and claim != owner
            ):
                reason_code = TOKEN_REASON_WRONG_SEAT
                owner = None
            else:
                owner = str(owner)
                self._client_authenticated_streams[websocket] = owner
                self._client_token_hashes[websocket] = token_hash

        if reason_code == TOKEN_REASON_VERIFIED:
            self.token_telemetry.record_verification(
                reason_code=TOKEN_REASON_VERIFIED,
                stream_id=owner or claim,
                operation=operation,
            )
        else:
            self._client_authenticated_streams.pop(websocket, None)
            self._client_token_hashes.pop(websocket, None)
            telemetry_stream_id = claim if ":" in claim else ""
            self.token_telemetry.record_verification(
                reason_code=reason_code or TOKEN_REASON_INTERNAL_ERROR,
                stream_id=telemetry_stream_id,
                operation=operation,
            )
        context.update(
            {
                "stream_id": owner or "",
                "session_generation": verified_generation if reason_code == TOKEN_REASON_VERIFIED else None,
                "token_verified": reason_code == TOKEN_REASON_VERIFIED,
                "reason_code": reason_code or TOKEN_REASON_INTERNAL_ERROR,
            }
        )
        if retired_owner is not None and reason_code == TOKEN_REASON_EXPIRED:
            context["retired_handoff_owner"] = retired_owner
        return context

    @staticmethod
    def _verify_system_producer_token(token: Any) -> bool:
        # The fixed producer uses the configured file only; never fall back
        # to an inline daemon token when that file is absent or unreadable.
        expected = None
        token_file = os.environ.get(SYSTEM_PRODUCER_STREAM_TOKEN_FILE_ENV)
        if token_file:
            try:
                expected = Path(token_file).expanduser().read_text(encoding="utf-8").strip()
            except OSError:
                expected = None
        return bool(expected and isinstance(token, str) and hmac.compare_digest(expected, token))

    @staticmethod
    def _auth_error_frame(verb: str, request_id: Any, code: str) -> dict[str, Any]:
        frame = {"type": f"{verb}.error", "error_code": code}
        if request_id is not None:
            frame["request_id"] = request_id
        return frame

    def _valid_system_producer_hello(self, msg: dict[str, Any]) -> bool:
        subscribe = msg.get("subscribe")
        return bool(
            msg.get("from_stream_id") == FIXED_SYSTEM_PRODUCER_STREAM_ID
            and self._verify_system_producer_token(msg.get("stream_token"))
            and isinstance(subscribe, dict)
            and subscribe.get("snapshot") is False
            and subscribe.get("mode") == "rpc"
        )

    def _valid_system_notification_create(self, msg: dict[str, Any]) -> bool:
        if not set(msg).issubset(SYSTEM_NOTIFICATION_CREATE_FIELDS):
            return False
        if (
            msg.get("from_stream_id") != FIXED_SYSTEM_PRODUCER_STREAM_ID
            or not self._verify_system_producer_token(msg.get("stream_token"))
            or msg.get("producer") != FIXED_SYSTEM_PRODUCER_STREAM_ID
            or not isinstance(msg.get("request_id"), str)
            or not msg["request_id"]
            or not isinstance(msg.get("title"), str)
            or not msg["title"].strip()
            or ("body" in msg and not isinstance(msg.get("body"), str))
            or msg.get("severity") not in {"info", "warning", "critical"}
            or ("actions" in msg and msg.get("actions") != [])
            or not isinstance(msg.get("dedup_key"), str)
        ):
            return False
        match = SYSTEM_NOTIFICATION_DEDUP_RE.fullmatch(msg["dedup_key"])
        if match is None:
            return False
        try:
            datetime.strptime(match.group(1), "%Y-%m-%d")
        except ValueError:
            return False
        return True

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _safe_telemetry_label(value: str, *, fallback: str) -> str:
        raw = value or fallback
        suffix = "...<truncated>"
        safe: list[str] = []
        length = 0
        for char in raw:
            rendered = char if char in _TELEMETRY_LOG_SAFE_CHARS else f"%{ord(char):02X}"
            if length + len(rendered) > MAX_UNSUPPORTED_LABEL_LENGTH:
                return "".join(safe)[: max(0, MAX_UNSUPPORTED_LABEL_LENGTH - len(suffix))] + suffix
            safe.append(rendered)
            length += len(rendered)
        label = "".join(safe)
        if value and label in (
            UNSUPPORTED_OVERFLOW_VERB,
            UNSUPPORTED_OTHER_CALLER,
            *UNSUPPORTED_FALLBACK_LABELS,
        ):
            # Keep raw untrusted labels distinct from the aggregate sentinels.
            label = "".join(
                f"%{ord(char):02X}" if char in "<>" else char
                for char in label
            )
        return label

    def _record_unsupported(self, verb: str, *, caller: str | None) -> None:
        telemetry_verb = self._safe_telemetry_label(verb, fallback="<missing>")
        caller_name = self._safe_telemetry_label(caller or "", fallback="<unknown>")
        if telemetry_verb not in self._unsupported_records:
            # Reserve one record slot for the overflow bucket until it exists;
            # otherwise the first overflow hit would create MAX+1 records.
            named_capacity = MAX_UNSUPPORTED_VERBS - (
                0 if UNSUPPORTED_OVERFLOW_VERB in self._unsupported_records else 1
            )
            if named_capacity <= 0 or len(self._unsupported_records) >= named_capacity:
                telemetry_verb = UNSUPPORTED_OVERFLOW_VERB
        timestamp = self._utc_now()
        record = self._unsupported_records.setdefault(
            telemetry_verb,
            {
                "count": 0,
                "callers": {},
                "first_seen": timestamp,
                "last_seen": timestamp,
                "last_caller": caller_name,
            },
        )
        record["count"] += 1
        callers = record["callers"]
        if caller_name not in callers:
            # Apply the same reservation to the bounded caller map so the
            # first overflow identity cannot create MAX+1 caller keys.
            caller_capacity = MAX_UNSUPPORTED_CALLERS_PER_VERB - (
                0 if UNSUPPORTED_OTHER_CALLER in callers else 1
            )
            if caller_capacity <= 0 or len(callers) >= caller_capacity:
                caller_name = UNSUPPORTED_OTHER_CALLER
        callers[caller_name] = callers.get(caller_name, 0) + 1
        record["last_seen"] = timestamp
        record["last_caller"] = caller_name

        now = _monotonic()
        last_logged_at = self._unsupported_last_logged_at.get(telemetry_verb)
        if last_logged_at is None or now - last_logged_at >= UNSUPPORTED_LOG_INTERVAL_SECONDS:
            self._unsupported_last_logged_at[telemetry_verb] = now
            log.warning(
                "unsupported_in_v2 verb=%s caller=%s count=%d timestamp=%s",
                telemetry_verb,
                caller_name,
                record["count"],
                timestamp,
            )

    def _unsupported_stats_snapshot(self) -> dict[str, Any]:
        verbs: dict[str, dict[str, Any]] = {}
        total = 0
        for verb in sorted(self._unsupported_records):
            record = self._unsupported_records[verb]
            count = int(record["count"])
            total += count
            verbs[verb] = {
                "verb": verb,
                "count": count,
                "caller": record["last_caller"],
                "callers": dict(sorted(record["callers"].items())),
                "first_seen": record["first_seen"],
                "last_seen": record["last_seen"],
            }
        recent = sorted(
            verbs.values(),
            key=lambda record: (record["last_seen"], record["verb"]),
            reverse=True,
        )
        return {"total": total, "verbs": verbs, "recent": recent}

    def _unsupported(self, verb: str, request_id: Any, *, caller: str | None = None) -> dict[str, Any]:
        self._record_unsupported(verb, caller=caller)
        reply = {
            "type": f"{verb}.error",
            "error_code": "unsupported_in_v2",
            "verb": verb,
        }
        if request_id is not None:
            reply["request_id"] = request_id
        return reply

    # -- handlers (no business logic lives here) ---------------------------

    async def _on_ping(self, _msg: dict[str, Any]) -> dict[str, Any]:
        return {"type": "pong"}

    @staticmethod
    def _optional_string_list(value: Any, field_name: str) -> frozenset[str] | None:
        if value is None:
            return None
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"subscribe.{field_name} must be list[str]")
        return frozenset(item for item in value if item)

    def _subscription_for_message(
        self, msg: dict[str, Any],
    ) -> tuple[bool, frozenset[str] | None, frozenset[str], str]:
        websocket = msg.get("_client_websocket")
        if websocket is None:
            return True, None, frozenset(), "full"
        if websocket not in self._client_include_subagents:
            return False, None, frozenset(), "full"
        return (
            bool(self._client_include_subagents.get(websocket, False)),
            self._client_opened_by_host_ids.get(websocket),
            self._client_exclude_event_types.get(websocket, frozenset()),
            self._client_events_mode.get(websocket, "full"),
        )

    def _session_is_visible_to_client(
        self,
        session: dict[str, Any] | None,
        include_subagents: bool,
        opened_by_host_ids: frozenset[str] | None,
        include_assistant_composite: bool = False,
    ) -> bool:
        if session is None:
            return True
        if (
            str(session.get("provider") or "") == "composite"
            and getattr(self.assistant_composite, "is_stream", lambda _value: False)(session.get("stream_id"))
            and not include_assistant_composite
        ):
            return False
        visibility = str(session.get("visibility") or "default").lower()
        if not include_subagents and visibility in {"hidden", "nested", "subagent"}:
            return False
        if opened_by_host_ids is None:
            return True
        return str(session.get("opened_by_host_id") or "") in opened_by_host_ids

    def _stream_is_visible_to_client(
        self,
        stream_id: str,
        include_subagents: bool,
        opened_by_host_ids: frozenset[str] | None,
        include_assistant_composite: bool = False,
    ) -> bool:
        session = self.sessions.get(stream_id) if self.sessions is not None and stream_id else None
        return self._session_is_visible_to_client(
            session, include_subagents, opened_by_host_ids, include_assistant_composite,
        )

    def _filter_sessions_for_client(
        self,
        sessions: list[dict[str, Any]],
        include_subagents: bool,
        opened_by_host_ids: frozenset[str] | None,
        include_assistant_composite: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            self._project_assistant_composite_session(session)
            for session in sessions
            if isinstance(session, dict)
            and self._session_is_visible_to_client(
                session, include_subagents, opened_by_host_ids, include_assistant_composite,
            )
        ]

    def _project_assistant_composite_session(self, session: dict[str, Any]) -> dict[str, Any]:
        composite = self.assistant_composite
        if composite is not None and composite.is_stream(session.get("stream_id")):
            return composite.project_session(session)
        return dict(session)

    @staticmethod
    def _client_wants_assistant_composite(msg: dict[str, Any]) -> bool:
        capabilities = msg.get("capabilities") if isinstance(msg.get("capabilities"), dict) else {}
        subscribe = msg.get("subscribe") if isinstance(msg.get("subscribe"), dict) else {}
        subscribed = subscribe.get("capabilities") if isinstance(subscribe.get("capabilities"), dict) else {}
        return bool(capabilities.get("assistant_composite_v1") or subscribed.get("assistant_composite_v1"))

    def _assistant_composite_capable_for_message(self, msg: dict[str, Any]) -> bool:
        websocket = msg.get("_client_websocket")
        return bool(websocket is not None and self._client_assistant_composite_v1.get(websocket, False))

    @staticmethod
    def _summary_snapshot_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return v1-compatible compact session rows for summary-mode hello.

        The full inventory stays authoritative for normal subscribers.  Summary
        subscribers receive the sidebar fields, but never persistence-only
        columns such as token hashes, transcript paths, or reopen bookkeeping.
        """
        fields = (
            "stream_id", "host", "session_name", "provider", "display_name",
            "last_event_at", "last_text", "last_kind", "draft", "question",
            "pending_peer_messages", "role", "role_source", "phase", "spec_id", "spec_ids",
            "qualified_spec_ids", "spec_binding_provenance", "spec_resolution",
            "visibility", "self_close_on_completion", "online", "pending",
            "working", "working_label", "parent_stream_id", "handoff_from_stream_id",
            "opened_by_host_id", "status_card", "usage", "context_tokens",
            "model_context_window", "context_updated_at", "context_level", "model",
            "effort", "requested_model", "requested_effort", "effective_model",
            "effective_effort", "routing_integrity", "routing_integrity_reason",
            "routing_integrity_event_id", "routing_integrity_updated_at", "agent_id", "preview", "attached",
            "created", "last_activity", "pane_pid", "pane_status", "capture_liveness", "close_pending",
            "close_intent_id", "close_requested_at", "session_generation", "turn_state",
            "session_kind", "capabilities", "assistant_activity",
            "turn_state_since", "turn_state_sources", "host_status",
            "host_status_reason", "host_status_since",
            "bootstrap_state", "state", "agents", "objective", "objective_source",
        )
        legacy = frozenset({
            "stream_id", "host", "session_name", "provider", "display_name", "role",
            "phase", "visibility", "online", "pending", "working", "working_label",
            "parent_stream_id", "handoff_from_stream_id", "opened_by_host_id",
            "host_status", "host_status_reason", "host_status_since",
        })
        return [
            {
                field: session[field]
                for field in fields
                if field in session
                and (field in legacy or session[field] not in (None, "", False, 0, [], {}))
            }
            for session in sessions
            if isinstance(session, dict)
        ]

    def _frame_for_client(
        self, websocket: Any, frame_type: str, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not (self._is_loopback_client(websocket)
                or self._operator_authenticated(websocket)
                or websocket in self._client_authenticated_streams):
            return None
        # The server speaks first, but a connection has the restrictive default
        # immediately: broadcasts may race the client's hello and must never
        # leak hidden/nested work during that window.
        include_subagents = bool(self._client_include_subagents.get(websocket, False))
        opened_by_host_ids = self._client_opened_by_host_ids.get(websocket)
        exclude_event_types = self._client_exclude_event_types.get(websocket, frozenset())
        if frame_type in exclude_event_types:
            return None
        # Schedules expose prompt previews and lineage. Registration in the
        # broadcast set happens before hello, so recipient eligibility must be
        # connection-bound and fail closed here rather than inferred from the
        # ordinary inventory subscription.
        if frame_type.startswith("schedule.") and not self._operator_authenticated(websocket):
            return None
        if frame_type == "session.inventory":
            sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
            projected = self._filter_sessions_for_client(
                sessions, include_subagents, opened_by_host_ids,
                bool(self._client_assistant_composite_v1.get(websocket, False)),
            )
            # A summary-mode (mobile) client gets the SAME compact rows on the
            # broadcast path as it already gets in its hello snapshot — the
            # existing reduction was applied only to the snapshot, so summary
            # clients were receiving full ~4.5 KB/session rows on every
            # broadcast. Full-size inventory frames head-of-line-block the
            # focused-liveness pong on a slow link (focused_heartbeat churn).
            if self._client_events_mode.get(websocket) == "summary":
                projected = self._summary_snapshot_sessions(projected)
            return {**payload, "sessions": projected}
        if frame_type == "chat.event":
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            if not self._stream_is_visible_to_client(
                str(event.get("stream_id") or ""), include_subagents, opened_by_host_ids,
                bool(self._client_assistant_composite_v1.get(websocket, False)),
            ):
                return None
        if frame_type in {"working.state", "completion.report", "session.died"}:
            stream_id = str(payload.get("stream_id") or payload.get("from_stream_id") or "")
            if stream_id and not self._stream_is_visible_to_client(
                stream_id, include_subagents, opened_by_host_ids,
                bool(self._client_assistant_composite_v1.get(websocket, False)),
            ):
                return None
        return dict(payload)

    def _operator_authenticated(self, websocket: Any) -> bool:
        """The one connection-bound operator predicate used by auth and fanout."""
        trust = self._connection_trust.get(websocket)
        return bool(
            trust
            and trust.transport == "v2"
            and trust.operator_trusted
            and trust.client_kind in operator_auth.CLIENT_KINDS
        )

    @staticmethod
    def _is_loopback_client(websocket: Any) -> bool:
        """Local bootstrap trusts the actual transport peer, never wire claims."""
        peer = getattr(websocket, "remote_address", None)
        if not isinstance(peer, (tuple, list)) or not peer:
            return False
        try:
            address = ipaddress.ip_address(peer[0])
        except (ValueError, TypeError):
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.is_loopback

    def _authenticate_operator_hello(self, websocket: Any, msg: dict[str, Any]) -> str | None:
        """Bind a v2 UI principal to this socket, or fail the claimed proof closed."""
        challenge = self._operator_challenges.pop(websocket, None)
        auth_v2 = msg.get("auth_v2")
        if auth_v2 is None:
            return None
        try:
            if challenge is None or time.time() > challenge[1]:
                raise operator_auth.OperatorAuthError("operator auth challenge unavailable")
            if not isinstance(auth_v2, dict) or set(auth_v2) != {"scheme", "credential_id", "proof"}:
                raise operator_auth.OperatorAuthError("invalid operator auth frame")
            if auth_v2.get("scheme") != operator_auth.AUTH_SCHEME:
                raise operator_auth.OperatorAuthError("unsupported operator auth scheme")
            client_kind = operator_auth.canonical_client_kind(msg.get("client"))
            credential_id = operator_auth.canonical_uuid(auth_v2.get("credential_id"))
            proof = operator_auth.decode_b64url(
                auth_v2.get("proof"), expected_bytes=operator_auth.AUTH_PROOF_BYTES,
            )
            trust = self.operator_credential_registry.verify(
                credential_id, client_kind, challenge[0], operator_auth.encode_b64url(proof),
            )
        except operator_auth.OperatorAuthError:
            return "operator_auth_invalid"
        self._connection_trust[websocket] = trust
        return None

    async def _on_hello(self, msg: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply hello.subscribe before constructing the connection's frames."""
        subscribe = msg.get("subscribe") if isinstance(msg.get("subscribe"), dict) else {}
        try:
            opened_by_host_ids = self._optional_string_list(
                subscribe.get("opened_by_host_ids"), "opened_by_host_ids",
            )
            exclude_event_types = self._optional_string_list(
                subscribe.get("exclude_event_types"), "exclude_event_types",
            ) or frozenset()
        except ValueError as exc:
            return [{"type": "hello.error", "error": str(exc)}]
        include_subagents = bool(subscribe.get("include_subagents"))
        events_mode = str(subscribe.get("events_mode") or "full").lower()
        if events_mode not in {"full", "summary"}:
            events_mode = "full"
        snapshot_requested = subscribe.get("snapshot") is not False and str(
            subscribe.get("mode") or "",
        ).lower() != "rpc"
        websocket = msg.get("_client_websocket")
        if websocket is not None:
            error_code = self._authenticate_operator_hello(websocket, msg)
            if error_code:
                return [{"type": "hello.error", "error_code": error_code}]
            auth = msg.get("_auth_context") or {}
            if not self._is_loopback_client(websocket) and not (
                self._operator_authenticated(websocket) or auth.get("token_verified")
                or (auth.get("service_authenticated") and not snapshot_requested
                    and str(subscribe.get("mode") or "").lower() == "rpc")
            ):
                return [{"type": "hello.error", "error_code": "authentication_required"}]
            self._client_include_subagents[websocket] = include_subagents
            self._client_opened_by_host_ids[websocket] = opened_by_host_ids
            self._client_exclude_event_types[websocket] = exclude_event_types
            self._client_events_mode[websocket] = events_mode
            self._client_assistant_composite_v1[websocket] = self._client_wants_assistant_composite(msg)
        if not snapshot_requested:
            frames = [{
                "type": "ready", "snapshot": False, "events_mode": events_mode,
            }]
            if getattr(self.assistant_composite, "enabled", False):
                frames[0]["capabilities"] = {"assistant_composite_v1": True}
            if str(subscribe.get("mode") or "").lower() != "rpc" and "hosts.stats" not in exclude_event_types:
                frames.append(self.hosts_stats_frame())
            return frames

        # Bootstrap frames: `hello`, `snapshot`, then the daemon-owned fleet
        # projection. The stats frame is a complete replacement, not a delta.
        if self.hosts is not None:
            hosts = self.hosts.snapshot()
        else:
            hosts = {self.local_host: {"host": self.local_host, "online": True, "host_status": "online"}}
        sessions = self.sessions.list_open() if self.sessions else []
        working_states = self._working_states_snapshot(int(time.time() * 1000))
        snapshot_sessions = self._filter_sessions_for_client(
            sessions, include_subagents, opened_by_host_ids,
            self._assistant_composite_capable_for_message(msg),
        )
        # `agent-orch list` consumes this snapshot (summary mode), so the role
        # provenance projection must ride the snapshot rows, not only the
        # list_sessions RPC. Enrich before the summary reduction so the compact
        # projection can carry it.
        if self.store is not None and snapshot_sessions:
            sources = await self.store.all_role_sources()
            snapshot_sessions = [
                {
                    **row,
                    "role_source": role_source_for(
                        row,
                        sources.get(
                            (str(row.get("host") or ""), str(row.get("session_name") or "")), {},
                        ).get("role_source"),
                    ),
                }
                for row in snapshot_sessions
            ]
        if events_mode == "summary":
            snapshot_sessions = self._summary_snapshot_sessions(snapshot_sessions)
        snapshot: dict[str, Any] = {
            "type": "snapshot",
            "events_mode": events_mode,
            # A client must not infer this from a version stamp: it needs an
            # explicit wire guarantee before it can safely close a same-name
            # target after a reconnect. Old daemons simply omit this field.
            "capabilities": {
                "close_expected_generation": True,
                **({"assistant_composite_v1": True} if getattr(self.assistant_composite, "enabled", False) else {}),
            },
            "sessions": snapshot_sessions,
            "notifications": (await self.notify.snapshot_notifications(
                summary=(events_mode == "summary"),
            ) if self.notify else []) + (
                await self.store.consent_notifications() if self.store else []
            ),
            "updates": [],
            "hosts": hosts,
            "working_states": {
                stream_id: state
                for stream_id, state in working_states.items()
                if "working.state" not in exclude_event_types
                and self._stream_is_visible_to_client(
                    stream_id, include_subagents, opened_by_host_ids,
                    self._assistant_composite_capable_for_message(msg),
                )
            },
        }
        # `_authenticate_operator_hello` above must bind this exact socket
        # before schedules enter its initial snapshot. Authless, token-auth,
        # satellite, and pre-hello connections receive no schedule surface.
        if (
            websocket is not None
            and self._operator_authenticated(websocket)
            and self.window_schedule is not None
        ):
            snapshot["schedules"] = await self.window_schedule.schedule_inventory()
        if "limits.update" not in exclude_event_types:
            snapshot["limits"] = self.limits.snapshot() if self.limits else [
                {"id": "claude", "label": "Claude", "pct": None, "resets_at_iso": None, "resets_text": None, "upstream_reported_at": None, "probed_at": None},
                {"id": "fable", "label": "Fable", "pct": None, "resets_at_iso": None, "resets_text": None, "upstream_reported_at": None, "probed_at": None},
                {"id": "codex", "label": "Codex", "pct": None, "resets_at_iso": None, "resets_text": None, "upstream_reported_at": None, "probed_at": None},
            ]
            snapshot["limits_health"] = (
                self.limits.health_snapshot()
                if self.limits and hasattr(self.limits, "health_snapshot")
                else None
            )
        hello: dict[str, Any] = {"type": "hello"}
        if getattr(self.assistant_composite, "enabled", False):
            hello["capabilities"] = {"assistant_composite_v1": True}
        frames: list[dict[str, Any]] = [hello, snapshot]
        if "hosts.stats" not in exclude_event_types:
            frames.append(self.hosts_stats_frame())
        return frames

    def _working_states_snapshot(self, daemon_now_ms: int | None = None) -> dict[str, dict[str, Any]]:
        """Merge the local and remote tracker maps for the hello snapshot."""
        if daemon_now_ms is None:
            daemon_now_ms = int(time.time() * 1000)
        merged: dict[str, dict[str, Any]] = {}
        for tracker in self.working_state_trackers:
            snapshot = getattr(tracker, "snapshot", None)
            if not callable(snapshot):
                continue
            try:
                values = snapshot(daemon_now_ms)
            except TypeError:
                # Keep lightweight test doubles and older optional trackers
                # readable while the built-in tracker uses daemon-now renders.
                values = snapshot()
            if isinstance(values, dict):
                merged.update({
                    str(stream_id): dict(payload)
                    for stream_id, payload in values.items()
                    if isinstance(payload, dict)
                })
        composite = self.assistant_composite
        if composite is not None and composite.enabled:
            merged[composite.config.stream_id] = composite.working_payload()
        return merged

    # Session registry verbs. Every one is a thin adapter: parse the envelope,
    # call `sessions.py`, name the reply. No business rules live in this file.

    async def _on_list_sessions(self, msg: dict[str, Any]) -> dict[str, Any]:
        # Wait out the boot adoption window so a restart never serves an empty
        # inventory (B10); a no-op the instant boot completes and forever after.
        await self.inventory_ready.wait()
        # Served from the in-memory inventory — O(open), never a store round
        # trip (design: this is UI polling, the hottest verb at 100k/12d).
        include_subagents, opened_by_host_ids, _excluded, _events_mode = self._subscription_for_message(msg)
        active = self._filter_sessions_for_client(
            self.sessions.list_open(), include_subagents, opened_by_host_ids,
            self._assistant_composite_capable_for_message(msg),
        )
        sources = await self.store.all_role_sources() if self.store is not None else {}
        active = [
            {
                **row,
                "role_source": role_source_for(
                    row, sources.get((str(row.get("host") or ""), str(row.get("session_name") or "")), {}).get("role_source"),
                ),
            }
            for row in active
        ]
        return {"type": "list_sessions.ok", "active": active}

    async def _on_thread_read(self, msg: dict[str, Any]) -> dict[str, Any]:
        auth = dict(msg.get("_auth_context") or {})
        if auth.get("token_verified"):
            auth["thread_token_hash"] = hashlib.sha256(str(msg.get("stream_token") or "").encode()).hexdigest()
        return await self.store.read_child_thread(msg, auth)

    async def _on_inspect_stream(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Return bounded, read-only data from the v2 durable sources.

        Reports follow v1 inspect precedence: an exact terminal row for the
        requested ``(stream_id, msg_id)`` wins; a missing exact row falls back
        to the newest terminal row for the stream.  v2 ``send`` replies are
        transient and are therefore exposed as an explicit marker rather than
        a falsy placeholder or a new parallel cache.
        """
        host, name = await self.sessions.resolve(msg)
        stream_id = f"{host}:{name}"
        # An explicit inspect is an administrative, named lookup.  The hello
        # subscription narrows presentation streams (snapshot/list/events), but
        # must not rewrite a known live id to `unknown_session` here.
        msg_id = msg.get("msg_id")
        if msg_id is not None and (not isinstance(msg_id, int) or isinstance(msg_id, bool) or msg_id < 0):
            raise VerbError("invalid_request", "msg_id must be a non-negative integer")
        report_id = msg.get("report_id")
        if report_id is not None and (not isinstance(report_id, str) or not report_id.strip()):
            raise VerbError("invalid_request", "report_id must be a non-empty string")
        raw_tail = msg.get("event_tail", INSPECT_DEFAULT_EVENT_TAIL)
        if raw_tail is None:
            raw_tail = INSPECT_DEFAULT_EVENT_TAIL
        if not isinstance(raw_tail, int) or isinstance(raw_tail, bool):
            raise VerbError("invalid_request", "event_tail must be a non-negative integer")
        event_tail = max(0, min(int(raw_tail), RECENT_LIMIT))
        session = self.sessions.get(stream_id) or await self.store.fetch_session(host, name)
        if session is None:
            raise VerbError("unknown_session", "Unknown session")
        recent_events = (
            await self.store.fetch_session_event_tail(stream_id, limit=event_tail)
            if event_tail else []
        )
        if self.assistant_composite is not None and self.assistant_composite.is_stream(stream_id):
            recent_events = await self.assistant_composite.enrich_events(recent_events)
        bootstrap_event = bool(recent_events) or bool(session.get("_bootstrap_event_seen"))
        if not bootstrap_event:
            bootstrap_event = bool(
                await self.store.fetch_session_event_tail(stream_id, limit=1)
            )
        session = with_bootstrap_state(session, event_seen=bootstrap_event)
        if report_id is not None:
            existing_report = await self.store.get_report(report_id)
            if existing_report is not None and existing_report.get("from_stream_id") != stream_id:
                existing_report = None
        else:
            existing_report = await self.store.find_report(
                stream_id, msg_id, statuses=INSPECT_TERMINAL_STATUSES,
            )
            if msg_id is not None and existing_report is None:
                existing_report = await self.store.find_report(
                    stream_id, statuses=INSPECT_TERMINAL_STATUSES,
                )
        if isinstance(session, dict):
            stored = await self.store.fetch_role_source(host, name)
            session = {
                **session,
                "role_source": role_source_for(session, (stored or {}).get("role_source")),
            }
        close_audit = await self.store.latest_close_audit(stream_id)
        return {
            "type": "inspect_stream.ok",
            "stream_id": stream_id,
            "session": session,
            "recent_events": recent_events,
            "send_frames": dict(SEND_FRAMES_NOT_IMPLEMENTED),
            "existing_report": existing_report,
            "close_audit": close_audit,
            "deferred_reap": await self.store.get_deferred_reap(stream_id),
        }

    async def _on_rename(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        session = await self.sessions.rename(
            host, name,
            str(msg.get("display_name") or ""),
            str(msg.get("source") or "agent").strip() or "agent",
        )
        return {"type": "rename.ok", "session": session}

    async def _on_set_visibility(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        requested = str(msg.get("visibility") or "").strip()
        session = await self.sessions.set_visibility(host, name, requested)
        return {"type": "set_visibility.ok", "session": session}

    async def _on_status_card(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        fields = {k: msg[k] for k in ("goal", "plan", "step_done", "update", "handoff_planned") if k in msg}
        session = await self.sessions.set_status_card(host, name, fields)
        return {"type": "status_card.ok", "session": session}

    async def _on_qa_issue(self, msg):
        return await qa_dispatch.issue(self.store, msg)

    async def _on_spawn(self, msg: dict[str, Any]) -> dict[str, Any]:
        # Hold a spawn until boot reconciliation has settled the reservation
        # table (QA #18). Only spawn waits; the gate is open outside the boot
        # window, so this is a no-op for every steady-state spawn.
        await self.spawn_ready.wait()
        return await self.spawnctl.spawn(msg, self.local_host)

    async def _on_await_spawn(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.spawnctl.await_spawn(msg)

    async def _on_spawn_cancel(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.spawnctl.spawn_cancel(msg, self.local_host)

    async def _on_spawn_status(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.spawnctl.spawn_status(msg, self.local_host)

    async def _on_spawn_freeze(self, msg: dict[str, Any]) -> dict[str, Any]:
        host = str(msg.get("host") or self.local_host).strip()
        return await self.spawnctl.set_spawn_freeze(
            host, reason=str(msg.get("reason") or ""),
            ttl_s=float(msg.get("ttl_s") or 900.0),
        )

    async def _on_spawn_unfreeze(self, msg: dict[str, Any]) -> dict[str, Any]:
        host = str(msg.get("host") or self.local_host).strip()
        return await self.spawnctl.clear_spawn_freeze(host)

    async def _on_reparent(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        new_parent = str(msg.get("new_parent_stream_id") or "").strip()
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        try:
            return await self.sessions.reparent(host, name, new_parent, auth_context=auth)
        except VerbError as exc:
            if exc.code != "reparent_unauthorized":
                raise
            if not await self.sessions.assistant.manager_holds(auth):
                row = await self.store.fetch_session(host, name)
                await self._manager_audit(
                    "reparent", msg, auth, f"{host}:{name}",
                    str((row or {}).get("session_generation") or "") or None,
                    result="refused", refusal_code=exc.code, actor_kind="seat")
                raise
        sid = f"{host}:{name}"
        policy = self.sessions.assistant
        # Authority (outermost), then the worker's lifecycle lock: the grant is
        # re-verified and cannot change until the effect is written. Once both
        # are held, admission, effect and outcome audit finish together; a
        # cancelled caller sees its cancellation only after the outcome row.
        async with policy.authority_lock, self.sessions._lifecycle_lock(host, name):
            return await _finish_despite_cancel(
                self._manager_reparent_locked(msg, auth, host, name, new_parent))

    async def _manager_reparent_locked(self, msg: dict[str, Any], auth: dict[str, Any],
                                       host: str, name: str, new_parent: str) -> dict[str, Any]:
        sid = f"{host}:{name}"
        policy = self.sessions.assistant
        target = await self.store.fetch_session(host, name)
        generation = str((target or {}).get("session_generation") or "") or None
        code = policy.manager_request_code(msg)
        if code is None and not await policy.manager_holds(auth):
            code = "authority_holder_required"
        if code is not None:
            await self._manager_audit("reparent", msg, auth, sid, generation, result="refused", refusal_code=code)
            raise VerbError(code, f"manager reparent refused: {code}")
        await self._manager_audit("reparent", msg, auth, sid, generation, result="admitted")
        try:
            reply = await self.sessions.reparent(
                host, name, new_parent, auth_context=auth, manager_authorized=True)
        except VerbError as exc:
            await self._manager_audit("reparent", msg, auth, sid, generation, result="refused", refusal_code=exc.code)
            raise
        await self._manager_audit("reparent", msg, auth, sid, generation, result="applied")
        return reply

    async def _on_role_set(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Set a role on a live seat, then deliver its baseline into the pane
        through the existing tell path. The nexus authority gate lives in
        `sessions.set_role`; delivery is best-effort post-set and reports
        `role_baseline: null` when no baseline content was supplied."""
        host, name = await self.sessions.resolve(msg)
        role = str(msg.get("role") or "").strip()
        session = await self.sessions.set_role(
            host, name, role, auth_context=msg.get("_auth_context"),
        )
        stored = await self.store.fetch_role_source(host, name)
        session = {
            **session,
            "role_source": role_source_for(session, (stored or {}).get("role_source")),
        }
        baseline = await self._deliver_role_baseline(host, name, role, msg)
        return {"type": "role.set.ok", "session": session, "role_baseline": baseline}

    async def _on_role_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        stream_id = f"{host}:{name}"
        session = (
            self.sessions.get(stream_id) if self.sessions is not None else None
        ) or await self.store.fetch_session(host, name)
        if session is None:
            raise VerbError("unknown_session", f"{stream_id} is not a known session")
        stored = await self.store.fetch_role_source(host, name)
        return {
            "type": "role.get.ok",
            "stream_id": stream_id,
            "role": session.get("role"),
            "role_source": role_source_for(session, (stored or {}).get("role_source")),
            "role_actor": (stored or {}).get("actor"),
            "role_changed_at": (stored or {}).get("changed_at"),
        }

    async def _deliver_role_baseline(
        self, host: str, name: str, role: str, msg: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Paste the frontmatter-stripped baseline the caller supplied into the
        seat's pane, stamped `[role baseline: <role>]`, via `comms.tell`. Absent
        content (missing baseline file → the CLI passes null) delivers nothing."""
        content = msg.get("baseline_content")
        if content is None or not str(content).strip() or self.comms is None:
            return None
        stamp = f"[role baseline: {role}]"
        tell_reply = await self.comms.tell({
            "to_stream_id": f"{host}:{name}",
            "text": f"{stamp}\n{content}",
            "from_stream_id": str(msg.get("from_stream_id") or "").strip() or "daemon:role",
            "sanitize": True,
        })
        return {
            "stamp": stamp,
            "delivery_status": tell_reply.get("delivery_status"),
            "submission_confirmed": tell_reply.get("submission_confirmed"),
            "tell_id": tell_reply.get("tell_id"),
        }

    async def _on_tell(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        target_stream_id = f"{host}:{name}"
        if getattr(self.assistant_composite, "is_stream", lambda _value: False)(target_stream_id):
            raise VerbError("assistant_composite_no_pane", "assistant composite has no provider pane")
        composite = self.assistant_composite
        if composite is not None:
            suppressed = await composite.suppress_routine_backend_ingress(
                target_stream_id=target_stream_id, body=str(msg.get("text") or msg.get("message") or ""),
                msg=msg, verb="tell",
            )
            if suppressed is not None:
                return suppressed
        return await self.comms.tell(msg)

    async def _on_send(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        stream_id = f"{host}:{name}"
        composite = self.assistant_composite
        if composite is not None and composite.is_stream(stream_id):
            auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
            if auth.get("operator_authenticated") is not True:
                raise VerbError("assistant_send_unauthorized", "assistant composite send requires authenticated caller")
            try:
                attachments = await self._validate_assistant_input_attachments(msg.get("attachments"))
                accepted = await composite.accept_input(
                    {**msg, "attachments": attachments},
                    operator_principal=str(auth.get("operator_principal") or "") or None,
                )
            except ValueError as exc:
                raise VerbError(str(exc), str(exc)) from exc
            return {
                "type": "send.result",
                "host": host,
                "session_name": name,
                "to_stream_id": stream_id,
                "delivery": "accepted",
                "submission_confirmed": True,
                "action_committed": True,
                "assistant_composite": accepted,
            }
        # Wire callers cannot suppress ordinary QA/progress admission with the
        # private daemon-only backend marker.  Only main's composite dispatcher
        # reaches ``Comms.send_assistant_backend`` directly.
        direct_msg = dict(msg)
        direct_msg.pop("_assistant_composite_backend_dispatch", None)
        if composite is not None:
            suppressed = await composite.suppress_routine_backend_ingress(
                target_stream_id=stream_id, body=str(direct_msg.get("text") or direct_msg.get("message") or ""),
                msg=direct_msg, verb="send",
            )
            if suppressed is not None:
                return {
                    "type": "send.result", "host": host, "session_name": name,
                    "to_stream_id": stream_id, "delivery": "persisted",
                    "submission_confirmed": False, "action_committed": True,
                    "assistant_backend_ingress": "persisted_suppressed",
                }
        return await self.comms.send(direct_msg)

    async def accept_assistant_operator_input(
        self, msg: dict[str, Any], *, operator_principal: str,
    ) -> dict[str, Any]:
        """Server-only daemon intake; it cannot be reached by a seat token."""
        composite = self.assistant_composite
        if composite is None:
            raise ValueError("assistant_composite_unavailable")
        principal = str(operator_principal or "").strip()
        if not principal:
            raise ValueError("assistant_operator_principal_required")
        attachments = await self._validate_assistant_input_attachments(msg.get("attachments"))
        return await composite.accept_input(
            {**msg, "attachments": attachments}, operator_principal=principal,
        )

    async def _validate_assistant_input_attachments(self, raw: object) -> list[dict[str, Any]]:
        if raw is None or raw == []:
            return []
        try:
            attachments = validate_send_attachments(raw)
        except AttachmentValidationError as exc:
            raise VerbError("attachment_invalid", str(exc)) from exc
        blob_store = getattr(self.comms, "blob_store", None)
        if blob_store is None or not callable(getattr(blob_store, "read_verified", None)):
            raise VerbError("attachment_fetch_failed", "blob store unavailable")
        for attachment in attachments:
            try:
                await blob_store.read_verified(str(attachment["key"]), max_bytes=ATTACHMENT_MAX_BYTES)
            except KeyError as exc:
                raise VerbError("attachment_missing", "attachment was not uploaded") from exc
            except ValueError as exc:
                raise VerbError("attachment_invalid", str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - stable public surface
                raise VerbError("attachment_fetch_failed", str(exc)) from exc
        return attachments

    async def _assistant_publication_attachments(self, attachment_ids, route):
        """Use the existing authenticated, content-addressed blob boundary."""
        blob_store = getattr(self.comms, "blob_store", None)
        if blob_store is None or not callable(getattr(blob_store, "read_verified", None)):
            raise ValueError("assistant_publish_attachment_validation_unavailable")
        originals = {item["key"]: item for item in json.loads(route.get("attachments_json") or "[]")}
        attachments = []
        for key in attachment_ids:
            try:
                data = await blob_store.read_verified(key, max_bytes=ATTACHMENT_MAX_BYTES)
            except (ValueError, KeyError) as exc:
                raise ValueError("assistant_publish_attachment_unverified") from exc
            # Input references retain their validated envelope. A generated blob
            # without declared metadata is downloadable, never mislabeled as an image.
            attachments.append(originals.get(key) or {"key": key, "mime": "application/octet-stream", "size": len(data)})
        return attachments

    def _get_transcriber(self) -> Any:
        """Lazily build the transcription proxy bound to the blob store.

        Kept on the server so the 10-minute content-addressed cache survives
        across calls. Authentication is enforced by the generic dispatch gate
        (a new verb requires operator/token/service auth for non-loopback
        clients, exactly like ``send``)."""
        transcriber = getattr(self, "_transcriber", None)
        if transcriber is None:
            from transcribe import Transcriber
            blob_store = getattr(self.comms, "blob_store", None)
            transcriber = Transcriber(blob_store)
            self._transcriber = transcriber
        return transcriber

    async def _on_transcribe_blob(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Transcribe an uploaded audio blob on the managed mic backend.

        Reads the content-addressed blob and forwards it to the loopback mic
        route with ``prompt_profile=fleet``; the daemon never loads a model.
        See ``transcribe.py`` for the contract and error codes."""
        result = await self._get_transcriber().transcribe_blob(
            request_id=str(msg.get("request_id") or ""),
            blob_sha=str(msg.get("blob_sha") or ""),
            mime=str(msg.get("mime") or ""),
        )
        return {"type": "transcribe_blob.ok", **result}

    async def _on_assistant_publish(self, msg: dict[str, Any]) -> dict[str, Any]:
        composite = self.assistant_composite
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if composite is None or not auth.get("token_verified"):
            raise VerbError("assistant_publish_unauthorized", "assistant.publish requires a verified backend stream token")
        try:
            return await composite.publish(msg, actor_stream_id=str(auth.get("stream_id") or "") or None)
        except ValueError as exc:
            raise VerbError(str(exc), str(exc)) from exc

    async def _assistant_question_operation(self, operation: str, msg: dict[str, Any]) -> dict[str, Any]:
        if self.notify is None:
            raise ValueError("assistant_question_adapter_unavailable")
        payload = dict(msg.get("payload") or {}) if isinstance(msg.get("payload"), dict) else {}
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        actor = str(auth.get("stream_id") or "").strip()
        if not actor or auth.get("token_verified") is not True:
            raise ValueError("assistant_question_actor_unverified")
        if operation == "question.open":
            envelope = payload.get("envelope")
            if not isinstance(envelope, dict):
                raise ValueError("assistant_question_envelope_required")
            # The established question store retains issuer provenance.  This
            # server-only marker permits a hidden composite lead to ask while
            # keeping response delivery on the current lane binding.
            payload["envelope"] = {
                **envelope,
                "producer_stream_id": actor,
                "_assistant_composite_question_proxy": True,
            }
        prompt_type = "prompt.ask" if operation == "question.open" else "prompt.cancel"
        return await self.notify.prompt({
            **payload,
            "type": prompt_type,
            "request_id": str(msg.get("request_id") or ""),
            "from_stream_id": actor,
            "_auth_context": {**auth, "assistant_composite_question_proxy": True},
        })

    async def _assistant_question_answer(
        self, question_id: str, body: str, source_msg: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve an existing question through a composite chat reply.

        The same accepted USER route is subsequently dispatched to the lane's
        current backend.  No answer tell is ever directed to the historical
        question producer.
        """
        if self.notify is None:
            return {"ok": False, "error_code": "assistant_question_adapter_unavailable"}
        auth = source_msg.get("_auth_context") if isinstance(source_msg.get("_auth_context"), dict) else {}
        if auth.get("operator_authenticated") is not True:
            return {"ok": False, "error_code": "assistant_question_answer_unauthorized"}
        request_id = str(source_msg.get("request_id") or source_msg.get("optimistic_id") or question_id)
        reply = await self.notify.prompt({
            "type": "prompt.answer",
            "request_id": "assistant-question-answer:" + request_id,
            "question_id": question_id,
            "text": body,
            "_auth_context": {**auth, "assistant_composite_question_answer_proxy": True},
        })
        return {"ok": bool(str(reply.get("type") or "").endswith(".ok")), "reply": reply}

    async def _on_assistant_operation(self, msg: dict[str, Any]) -> dict[str, Any]:
        composite = self.assistant_composite
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        if composite is None or not auth.get("token_verified"):
            raise VerbError("assistant_operation_unauthorized", "assistant.operation requires a verified backend stream token")
        try:
            return await composite.operation(msg, actor_stream_id=str(auth.get("stream_id") or "") or None)
        except ValueError as exc:
            raise VerbError(str(exc), str(exc)) from exc

    async def _on_send_image(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Attach an uploaded image to the caller's OWN conversation.

        The destination is the seat bound to the verified stream token, never a
        wire-supplied target, so a seat can only ever post to its own stream. An
        operator connection (no seat token) and any unverified caller are refused
        with a genuine ``stream_ownership_unverified`` receipt; the operator's own
        image path is the existing ``send`` attachment flow, not this verb.
        """
        auth = msg.get("_auth_context") or {}
        stream_id = str(auth.get("stream_id") or "").strip()
        if not (auth.get("token_verified") and stream_id):
            raise VerbError(
                "stream_ownership_unverified",
                "send_image requires a verified seat token bound to your own stream",
            )
        request_id = str(msg.get("request_id") or "").strip()
        if not request_id:
            raise VerbError("bad_request", "request_id is required")
        return await self.comms.post_self_image(
            stream_id=stream_id,
            raw_attachments=msg.get("attachments"),
            caption=str(msg.get("caption") or ""),
            request_id=request_id,
            broadcast=self.broadcast,
            recent_limit=RECENT_LIMIT,
        )

    async def _on_send_receipt_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Read the one highest-rowid durable receipt visible to this client."""
        target = str(msg.get("to_stream_id") or "").strip()
        request_id = str(msg.get("request_id") or "").strip()
        if not target or not request_id:
            raise VerbError("bad_request", "to_stream_id and request_id are required")
        include_subagents, opened_by_host_ids, _excluded, _events_mode = self._subscription_for_message(msg)
        if not self._stream_is_visible_to_client(
            target, include_subagents, opened_by_host_ids,
            self._assistant_composite_capable_for_message(msg),
        ):
            raise VerbError("unknown_session", "Unknown or inactive session")
        receipt = await self.store.get_send_receipt(target, request_id)
        return {
            "type": "send.receipt.get.ok",
            "found": receipt is not None,
            "receipts": [receipt] if receipt is not None else [],
        }

    async def _on_ledger_get(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.ledger.ledger_get(msg)

    async def _on_inbound_audit(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.ledger.inbound_audit(msg)

    async def _on_report(self, msg: dict[str, Any]) -> dict[str, Any]:
        # Snapshot the authenticated generation before persistence can yield to
        # a close/reopen. The current session is not a replacement credential.
        auth = dict(msg.get("_auth_context") or {})
        composite = self.assistant_composite
        extras = msg.get("extras")
        correlation = extras.get("assistant_composite") if isinstance(extras, dict) else None
        if (composite is None or not composite.enabled or correlation is None
                or msg.get("status") not in {"done", "error", "aborted"}):
            return await self.ledger.report(msg)
        if (not auth.get("token_verified") or not isinstance(correlation, dict)
                or set(correlation) != {"stream_id", "lane_id", "dispatch_id"}
                or correlation.get("stream_id") != composite.config.stream_id
                or any(not isinstance(value, str) or not value.strip() for value in correlation.values())):
            raise ValueError("assistant_terminal_report_correlation_invalid")
        correlation = dict(correlation)
        await composite._authenticated_generation(msg, str(auth.get("stream_id") or ""))
        async def complete_before_close(reply):
            completed = await composite.terminal_report({
                **msg, **reply, **correlation, "actor_stream_id": str(auth.get("stream_id") or ""),
            }, actor_generation=str(auth.get("session_generation") or ""))
            if completed is None:
                raise ValueError("assistant_terminal_report_scope_unverified")
        return await self.ledger.report(msg, before_close=complete_before_close)

    async def _on_await_report(self, msg: dict[str, Any]) -> dict[str, Any]:
        return await self.ledger.await_report(msg)

    async def _on_request_stream_events(self, msg: dict[str, Any]) -> Any:
        """Return a cursor-resumable, byte-bounded event stream."""
        host, name = await self.sessions.resolve(msg)
        stream_id = f"{host}:{name}"
        include_subagents, opened_by_host_ids, _excluded, _events_mode = self._subscription_for_message(msg)
        if not self._stream_is_visible_to_client(
            stream_id, include_subagents, opened_by_host_ids,
            self._assistant_composite_capable_for_message(msg),
        ):
            raise VerbError("unknown_session", "Unknown or inactive session")
        raw_limit = msg.get("limit")
        try:
            limit = RECENT_LIMIT if raw_limit is None else int(raw_limit)
        except (TypeError, ValueError):
            limit = RECENT_LIMIT
        effective_limit = max(0, min(limit, RECENT_LIMIT))

        raw_before = msg.get("before_daemon_seq")
        try:
            before_daemon_seq = None if raw_before is None else int(raw_before)
        except (TypeError, ValueError):
            before_daemon_seq = None
        if before_daemon_seq is not None and before_daemon_seq < 0:
            before_daemon_seq = None

        raw_chunk_limit = msg.get("chunk_limit")
        try:
            requested_chunk_limit = 0 if raw_chunk_limit is None else int(raw_chunk_limit)
        except (TypeError, ValueError):
            requested_chunk_limit = 0
        requested_chunk_limit = max(0, requested_chunk_limit)
        page_size = min(effective_limit, STREAM_EVENTS_PAGE_MAX)
        if requested_chunk_limit:
            page_size = min(page_size, requested_chunk_limit)

        return self._stream_request_stream_events(
            stream_id=stream_id,
            request_id=msg.get("request_id"),
            limit=effective_limit,
            before_daemon_seq=before_daemon_seq,
            page_size=page_size,
        )

    async def _stream_request_stream_events(
        self,
        *,
        stream_id: str,
        request_id: Any,
        limit: int,
        before_daemon_seq: int | None,
        page_size: int,
    ) -> Any:
        """Walk a bounded cursor page at a time, yielding wire-ready frames."""
        remaining = limit
        cursor = before_daemon_seq
        sent_frame = False
        sent_chunks = False
        try:
            while remaining > 0 and page_size > 0:
                query_limit = page_size + (1 if remaining > page_size else 0)
                rows = await self.store.fetch_session_event_page(
                    stream_id,
                    before_daemon_seq=cursor,
                    limit=query_limit,
                )
                has_more = remaining > page_size and len(rows) > page_size
                page = rows[-page_size:] if has_more else rows[:page_size]
                if not page:
                    break

                if self.assistant_composite is not None and self.assistant_composite.is_stream(stream_id):
                    page = await self.assistant_composite.enrich_events(page)
                frames = await asyncio.to_thread(
                    _assemble_event_frames,
                    page,
                    request_id=request_id,
                    stream_id=stream_id,
                    force_chunks=sent_chunks or has_more,
                )
                for frame in frames:
                    sent_frame = True
                    if frame.frame_type == "request_stream_events.chunk":
                        sent_chunks = True
                    yield frame

                remaining -= len(page)
                if not has_more:
                    break
                next_cursor = page[0].get("daemon_seq")
                if (
                    not isinstance(next_cursor, int)
                    or isinstance(next_cursor, bool)
                    or (cursor is not None and next_cursor >= cursor)
                ):
                    raise RuntimeError("request_stream_events cursor did not advance")
                cursor = next_cursor

            if sent_chunks or not sent_frame:
                terminal = await asyncio.to_thread(
                    _assemble_event_frames,
                    [],
                    request_id=request_id,
                    stream_id=stream_id,
                    force_chunks=False,
                )
                yield terminal[0]
        except Exception as exc:
            log.exception("request_stream_events failed stream_id=%s", stream_id)
            yield await asyncio.to_thread(_encoded_stream_events_error, request_id, str(exc))

    async def _on_daemon_stats(self, _msg: dict[str, Any]) -> dict[str, Any]:
        lifecycle = None
        snapshot = getattr(self.lifecycle, "snapshot", None)
        if callable(snapshot):
            lifecycle = await snapshot()
        stats = {
            "unsupported_in_v2": self._unsupported_stats_snapshot(),
            "seat_tokens": self.token_telemetry.snapshot(),
        }
        boot_queue_depths = getattr(self.spawnctl, "boot_queue_depths", None)
        if callable(boot_queue_depths):
            stats["boot_queue_depth"] = boot_queue_depths()
        if lifecycle is not None:
            stats["lifecycle"] = lifecycle
        return {
            "type": "daemon.stats.ok",
            "stats": stats,
        }

    async def _on_reconcile_status(self, msg: dict[str, Any]) -> dict[str, Any]:
        if self.reconciler is None:
            raise VerbError("unsupported_in_v2", "reconciler is not configured")
        requested = msg.get("host")
        host = str(requested).strip() if isinstance(requested, str) and requested.strip() else None
        payload = await self.reconciler.status(host=host)
        return {"type": "reconcile.status.ok", **payload}

    async def _on_close(self, msg: dict[str, Any]) -> dict[str, Any]:
        host, name = await self.sessions.resolve(msg)
        target_stream_id = f"{host}:{name}"
        raw_expected_generation = msg.get("expected_generation")
        if raw_expected_generation is None:
            expected_generation: str | None = None
        elif isinstance(raw_expected_generation, str) and raw_expected_generation.strip():
            expected_generation = raw_expected_generation.strip()
        else:
            raise VerbError(
                "expected_generation_invalid",
                "expected_generation must be a non-empty string when supplied",
            )
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        caller_stream_id = str(auth.get("stream_id") or "").strip()
        claimed_stream_id = str(msg.get("from_stream_id") or msg.get("actor_stream_id") or "").strip()
        operator_authenticated = bool(auth.get("operator_authenticated"))
        expired_self_close = False
        if auth.get("reason_code") == TOKEN_REASON_EXPIRED and self.store is not None:
            token = msg.get("stream_token")
            if isinstance(token, str) and token:
                try:
                    state = await self.store.stream_token_state(
                        hashlib.sha256(token.encode("utf-8")).hexdigest()
                    )
                except Exception:  # noqa: BLE001 - authorization stays fail-closed
                    state = None
                expired_self_close = (
                    isinstance(state, dict)
                    and str(state.get("stream_id") or "") == target_stream_id
                    and str(state.get("status") or "") != "open"
                    and not (claimed_stream_id and claimed_stream_id != target_stream_id)
                )
        if not bool(auth.get("token_verified")) and not expired_self_close and not operator_authenticated:
            self._log_close_unauthorized(auth)
            raise VerbError("close_unauthorized", "close requires a verified self or direct-parent caller")
        if expired_self_close:
            caller_stream_id = target_stream_id
        close_kind = "session_close"
        coordinator_authorized = False
        manager_generation: str | None = None
        if caller_stream_id != target_stream_id:
            target = await self.store.fetch_session(host, name) if self.store is not None else None
            if not (
                caller_stream_id
                and isinstance(target, dict)
                and str(target.get("parent_stream_id") or "").strip() == caller_stream_id
            ):
                if operator_authenticated:
                    close_kind = "operator_close"
                elif await self.sessions.assistant.manager_holds(auth):
                    # Fenced to the generation read now; every lifecycle fence
                    # is re-checked under the lifecycle lock before any kill.
                    manager_generation = expected_generation or self._manager_target_generation(target)
                    close_kind = "manager_close"
                else:
                    # A seat token proves only that seat's identity. Visibility
                    # and a caller-supplied confirmation bit do not promote it
                    # into a connection-authenticated operator principal.
                    self._log_close_unauthorized(auth)
                    await self._manager_audit(
                        "close", msg, auth, target_stream_id,
                        str((target or {}).get("session_generation") or "") or None,
                        result="refused", refusal_code="close_unauthorized", actor_kind="seat")
                    raise VerbError(
                        "close_unauthorized",
                        "close requires a verified self, direct-parent, or authenticated operator",
                    )
            else:
                coordinator_authorized = True
        claim_verified: str | None = None
        claim_mismatch: dict[str, Any] | None = None
        if isinstance(msg.get("ac_claim"), dict) and self.ledger is not None:
            claim_verified, claim_mismatch = await self.ledger.verify_ac_claim(msg["ac_claim"])
            self.ledger._alert_claim_result(
                claim_verified, claim_mismatch,
                report_id=str(msg.get("report_id") or f"close:{target_stream_id}"),
                from_stream_id=target_stream_id,
            )
        defer_if_working = bool(msg.get("defer_if_working"))
        if expired_self_close:
            actor_kind, auth_kind = "expired_self", "expired"
        elif close_kind == "operator_close":
            actor_kind, auth_kind = "operator", "operator_authenticated"
        elif close_kind == "manager_close":
            actor_kind, auth_kind = "manager", "token_verified"
        elif coordinator_authorized:
            actor_kind, auth_kind = "parent", "token_verified"
        else:
            actor_kind, auth_kind = "self", "token_verified"
        # `closed_by` names the caller; an operator connection carries no seat
        # stream id, so fall back to its authenticated principal.
        closed_by = caller_stream_id or str(auth.get("operator_principal") or "")
        attribution = {
            "actor_kind": actor_kind,
            "closed_by": closed_by,
            "auth_kind": auth_kind,
            "peer": f"{auth.get('connection_client') or 'unknown'}/"
                    f"{auth.get('transport') or 'unknown'}",
            "request_id": str(msg.get("request_id") or ""),
            "defer_if_working": defer_if_working,
        }
        if manager_generation is not None:
            # A manager close is fenced to the reported generation and never
            # kills a busy or unobservable seat, nor leaves a deferred intent.
            expected_generation = manager_generation
            defer_if_working = False
            attribution["defer_if_working"] = False
        close = functools.partial(
            self.sessions.close,
            host,
            name,
            str(msg.get("reason") or ""),
            expected_generation=expected_generation,
            close_kind=close_kind,
            requires_idle=bool(msg.get("requires_idle") or msg.get("reap")) or manager_generation is not None,
            operator_override=bool(msg.get("operator_override"))
            and (operator_authenticated or coordinator_authorized)
            and manager_generation is None,
            operator_confirm=msg.get("operator_confirm") is True and manager_generation is None,
            defer_if_working=defer_if_working,
            attribution=attribution,
            admission_guard=(
                None if manager_generation is None
                else lambda row: self._manager_close_admission(msg, host, name, row, manager_generation, auth)
            ),
        )
        if manager_generation is None:
            result = await close()
        else:
            # The grant cannot change between the manager's admission, the kill
            # and its audit; a concurrent revoke/transfer waits for this close.
            # Once the lock is held the close and its outcome audit finish
            # together; a cancelled caller sees it only after the outcome row.
            async with self.sessions.assistant.authority_lock:
                result = await _finish_despite_cancel(self._manager_close_locked(
                    close, msg, auth, target_stream_id, manager_generation))
        # A working pane refused via `defer_if_working` is an explicit,
        # non-error deferral (not a kill, not a generic failure) — intercept it
        # before the `already_closed` read below.
        if result.get("deferred"):
            return {
                "type": "close.deferred", "ok": False, "host": host,
                "session_name": name, "deferred": True,
                "reason": result.get("reason"), "session": result.get("session"),
                **_claim_wire_fields(claim_verified, claim_mismatch),
            }
        # The close ladder replaced `close.degraded` (2026-08-05 ruling): a kill
        # that could not be verified is now either recovered to `close.ok`
        # (SIGKILL/carcass) or an honest `close.failed` with the row left open —
        # local (no deliverable signal) and remote (`ssh_unreachable`) alike.
        if result.get("failed"):
            return {
                "type": "close.failed", "ok": False, "host": host, "session_name": name,
                "error_code": "close_failed", "error": result.get("reason"),
                "reason": result.get("reason"), "session": result.get("session"),
            }
        # `already_closed` is still a non-error reply: a repeated close succeeds.
        kind = "close.already_closed" if result["already_closed"] else "close.ok"
        if result.get("reap_status") == "deferred_host_offline":
            deferred = await self.store.get_deferred_reap(target_stream_id)
            if deferred:
                await self.sessions.surface_offline_close(self.notify, deferred)
        return {
            "type": kind, "ok": True, "host": host, "session_name": name,
            "already_closed": result["already_closed"], "session": result.get("session"),
            "reap_status": result.get("reap_status", "unknown"),
            **_claim_wire_fields(claim_verified, claim_mismatch),
        }

    async def _manager_close_locked(
        self, close: Callable[[], Awaitable[dict[str, Any]]], msg: dict[str, Any], auth: dict[str, Any],
        target_stream_id: str, generation: str,
    ) -> dict[str, Any]:
        result = await close()
        closed_now = not (result.get("failed") or result.get("deferred")
                          or result.get("already_closed") or result.get("stale_generation"))
        await self._manager_audit(
            "close", msg, auth, target_stream_id, generation,
            result="applied" if closed_now else "refused",
            refusal_code=None if closed_now else str(result.get("reason") or "close_not_applied")[:200],
        )
        return result

    async def _manager_audit(self, action: str, msg: dict[str, Any], auth: dict[str, Any],
                             target: str, generation: str | None, *, result: str,
                             refusal_code: str | None = None, actor_kind: str = "manager") -> None:
        """Audit a lifecycle action under the grant revision current at write time.

        Only the server-verified seat identity is recorded, never wire claims.
        """
        revision = (await self.store.lifecycle_authority_current())["revision"]
        await self.store.lifecycle_authority_audit(
            action=f"manager_{action}", actor_kind=actor_kind,
            actor_identity=str(auth.get("stream_id") or "") or None,
            actor_generation=auth.get("session_generation"), target_stream_id=target,
            target_generation=generation, old_revision=revision, new_revision=revision,
            reason=msg.get("reason"), request_id=msg.get("request_id"),
            result=result, refusal_code=refusal_code,
        )

    @staticmethod
    def _manager_target_generation(target: dict[str, Any] | None) -> str:
        return str((target or {}).get("session_generation") or "") or "unknown"

    async def _manager_lifecycle_fences(
        self, action: str, msg: dict[str, Any], host: str, name: str,
        target: dict[str, Any] | None, auth: dict[str, Any], expected_generation: str,
    ) -> None:
        """Refuse (auditing the verified manager) unless every fence passes.

        Requires the caller to still hold the grant, an explicit reason and
        request id, the target's terminal report for this exact generation, no
        live children or pending spawns, and a non-protected target.
        """
        sid = f"{host}:{name}"
        generation = str((target or {}).get("session_generation") or "") or None
        code = None
        if not await self.sessions.assistant.manager_holds(auth):
            code = "authority_holder_required"
        elif target is None:
            code = "lifecycle_target_unavailable"
        elif generation != expected_generation:
            code = "lifecycle_generation_mismatch"
        if code is None:
            children = await self.sessions._live_children(sid)
            pending = []
            for reservation in await self.store.reservations(include_expired=True):
                raw = reservation.get("payload")
                intent = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
                if str((intent.get("open_fields") or {}).get("parent_stream_id") or "") == sid:
                    pending.append(reservation)
            code = await self.sessions.assistant.manager_fences(
                target, generation, msg, children=children, pending_spawns=pending)
        if code is None and await self.store.find_report(
                sid, statuses=INSPECT_TERMINAL_STATUSES, session_generation=generation) is None:
            code = "lifecycle_report_required"
        if code is not None:
            await self._manager_audit(action, msg, auth, sid, generation, result="refused", refusal_code=code)
            raise VerbError(code, f"manager {action} refused: {code}")

    async def _manager_close_admission(
        self, msg: dict[str, Any], host: str, name: str, row: dict[str, Any] | None,
        expected_generation: str, auth: dict[str, Any],
    ) -> None:
        await self._manager_lifecycle_fences("close", msg, host, name, row, auth, expected_generation)
        # Durable attribution precedes the effect: an audit failure refuses the close.
        await self._manager_audit("close", msg, auth, f"{host}:{name}", expected_generation, result="admitted")

    async def _on_assistant_lifecycle(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Operator designate/revoke and holder transfer of lifecycle authority."""
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        action = str(msg.get("action") or "")
        if action == "inspect":
            if not (auth.get("operator_authenticated") or auth.get("token_verified") or (auth.get("peer_loopback") and auth.get("local_admin_verified"))):
                raise VerbError("authentication_required", "lifecycle inspect requires an authenticated caller")
            reply = {"type": "assistant.lifecycle.ok", "action": "inspect",
                     "grant": await self.store.lifecycle_authority_current(),
                     "audit": await self.store.lifecycle_authority_audit_rows(limit=20)}
            target = str(msg.get("target_stream_id") or "").strip()
            if ":" in target:
                reply["target"] = await self.store.lifecycle_authority_target(
                    target, self.sessions.assistant.role)
            return reply
        if action not in lifecycle_authority.ACTIONS:
            raise VerbError("invalid_request", "action must be designate, transfer, revoke or inspect")
        if action == "transfer":
            raise VerbError("authority_transfer_disabled", "Use a phone-approved designation to change holders")
        if msg.get("emergency"):
            if action != "revoke" or not auth.get("peer_loopback") or not auth.get("local_admin_verified"):
                raise VerbError("emergency_local_only", "Emergency revoke requires the local admin token on loopback")
            try:
                async with self.sessions.assistant.authority_lock:
                    receipt = await _finish_despite_cancel(self.store.lifecycle_authority_mutate(
                        msg, {**auth, "operator_authenticated": True,
                              "operator_principal": "operator:local-admin", "token_verified": False,
                              "_emergency_verified": True}, self.sessions.assistant.role))
            except lifecycle_authority.AuthorityError as exc:
                raise VerbError(exc.code, str(exc)) from exc
            return {"type": "assistant.lifecycle.ok", "action": action, "receipt": receipt}
        # Cached socket trust never authorizes a lifecycle effect.
        result = await self._on_consent({**msg, "type": "consent.request", "action": f"lifecycle.{action}"})
        return {**result, "type": "assistant.lifecycle.ok", "action": action}

    async def _on_consent(self, msg: dict[str, Any]) -> dict[str, Any]:
        auth = msg.get("_auth_context") if isinstance(msg.get("_auth_context"), dict) else {}
        verb = str(msg.get("type") or "")
        async def finish() -> dict[str, Any]:
            if verb == "consent.approve":
                result = await self.store.consent_approve_and_mutate(
                    msg, auth, self.operator_credential_registry, self.sessions.assistant.role)
            else:
                result = await self.store.consent_operation(
                    verb, msg, auth, self.operator_credential_registry, self.sessions.assistant.role)
            if result.get("challenge"):
                await self.broadcast({"type": "notification", "notification": consent.notification(result["challenge"])})
            return {"type": f"{verb}.ok", **result}
        try:
            async with self.sessions.assistant.authority_lock:
                # Store submission is a completion boundary. Cancellation must
                # not release authority_lock while its worker can still commit.
                return await _finish_despite_cancel(finish())
        except (consent.ConsentError, lifecycle_authority.AuthorityError) as exc:
            raise VerbError(exc.code, str(exc)) from exc

    async def consent_expiry_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                async with self.sessions.assistant.authority_lock:
                    expired = await _finish_despite_cancel(self.store.consent_expire(self.operator_credential_registry))
                for record in expired:
                    await self.broadcast({"type": "notification", "notification": record})
            except Exception:
                log.exception("consent expiry failed", extra={"subsystem": "consent", "bug_ref": "mobile_faceid_privileged_consent_2026_09"})

    @staticmethod
    def _log_close_unauthorized(auth: dict[str, Any]) -> None:
        client_kind = str(auth.get("connection_client") or "unknown")
        if client_kind not in operator_auth.CLIENT_KINDS:
            client_kind = "unknown"
        transport = str(auth.get("transport") or "unknown")
        if transport not in {"legacy", "v2"}:
            transport = "unknown"
        log.warning(
            "close_unauthorized client_kind=%s transport=%s operator_trusted=%s",
            client_kind,
            transport,
            bool(auth.get("operator_trusted")),
        )
