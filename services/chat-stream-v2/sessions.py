from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import signal
import threading
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime
from typing import Any

import prockill
from mirror import _extract_live_state, _provider_from_session_name
from v2_runtime import iso_now

VISIBILITIES = ("default", "visible", "hidden", "subagent")
#: Close escalation ladder windows (spec 2026-08-05 `close.degraded` DELETION
#: ruling). Graceful tmux kill is confirmed within the first window; a pane that
#: survives it is SIGKILLed by pid and confirmed within the second. Both bounded
#: so `close` can never wedge.
CLOSE_GRACEFUL_CONFIRM_S = 3.0
CLOSE_KILL_CONFIRM_S = 2.0
CLOSE_POLL_INTERVAL_S = 0.1
REMOTE_CLOSE_RETRY_ATTEMPTS = 3
REMOTE_CLOSE_RETRY_S = 0.1
CAPTURE_LIVENESS_STATES = frozenset({"idle", "wedged_unknown", "transport_unknown"})
DECISION_CAPTURE_TIMEOUT_S = 8.0

#: v1-parity "New Chat" display placeholders (chat_streamd.py ~2313-2340):
#: untitled rows render "New Chat - <Host>" (numbered per host) instead of the
#: raw session name. Display-time only — stamped on the copies `list_open`
#: returns, never on the inventory or the store, so manual rename logic never
#: sees a placeholder as a real title.
_GENERIC_WINDOW_NAMES = {"bash", "zsh", "sh", "fish", "claude", "codex", "node", "python"}
_NUMERIC_WINDOW_RE = re.compile(r"^\d+(?:\.\d+)*$")
_NEW_CHAT_PLACEHOLDER_RE = re.compile(r"^new chat(?:\s+-\s+.*)?$", re.IGNORECASE)

#: `title_source` value stamped onto the display-time placeholder copies. Any
#: consumer that must treat a row as still-untitled (e.g. the title nudge) keys
#: off this rather than re-parsing the placeholder string.
TITLE_SOURCE_PLACEHOLDER = "placeholder"

# Non-durable observations are copied back onto a store row when an inventory
# reconcile adopts/refreses it. Keep this as the one source of truth for both
# the live-write seam and the generation-safe merge below. ``capture_liveness``
# and the observer metadata are included because the close/presence paths also
# use ``apply_live`` for their short-lived safety/transport observations.
LIVE_OVERLAY_FIELDS = frozenset({
    "preview", "working", "working_label", "online", "last_activity",
    "pane_status", "pane_pid", "local_mirror", "mirror_local",
    "mirror_generation", "local_mirror_generation", "capture_liveness",
    "observer_source", "host_status", "host_status_reason",
    "genuine_activity_at", "genuine_activity_generation", "watch_working_at",
    "operator_activity_at", "operator_activity_generation", "capture_generation",
})

log = logging.getLogger("chat_streamd_v2.sessions")

_GENUINE_ACTIVITY_KINDS = frozenset({"USER", "TOOL_USE", "ASSIST_TEXT"})
BOOTSTRAP_EVENT_WINDOW_S = 15.0
_DAEMON_NUDGE_USER_PREFIXES = (
    "Please update your status card:",
    "Please run: agent-orch title",
)


def _event_timestamp_epoch(value: object) -> float | None:
    stamp = str(value or "").strip()
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(
            stamp[:-1] + "+00:00" if stamp.endswith(("Z", "z")) else stamp
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        epoch = parsed.timestamp()
    except (TypeError, ValueError):
        return None
    return epoch if math.isfinite(epoch) else None


def genuine_activity_epoch(event: object) -> float | None:
    """Return the event's turn timestamp, never an observation timestamp."""
    if not isinstance(event, dict):
        return None
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if bool(raw.get("is_sidechain")):
        return None
    kind = str(event.get("kind") or "").upper()
    if kind not in _GENUINE_ACTIVITY_KINDS:
        return None
    if kind == "USER":
        source = str(
            raw.get("from_stream_id")
            or raw.get("source_stream_id")
            or event.get("from_stream_id")
            or ""
        )
        if source == "daemon:status-sweep":
            return None
        text = str(event.get("text") or "").lstrip()
        if text.startswith(_DAEMON_NUDGE_USER_PREFIXES):
            return None
    return _event_timestamp_epoch(event.get("timestamp"))


def operator_user_epoch(event: object, created_at: object = None) -> float | None:
    """A normalized human USER turn, excluding provider and reminder feedback."""
    from claude_jsonl_norm import _classify_user_string, _parse_peer_tell
    if not isinstance(event, dict) or event.get("kind") != "USER":
        return None
    raw = event.get("raw") if isinstance(event.get("raw"), dict) else {}
    if raw.get("is_sidechain") or raw.get("isSidechain"):
        return None
    if any(raw.get(key) or event.get(key) for key in ("from_stream_id", "source_stream_id", "sender", "tell_id")):
        return None
    text = str(event.get("text") or "").strip()
    if not text or text.startswith(_DAEMON_NUDGE_USER_PREFIXES):
        return None
    if (event.get("provider") == "codex" and not event.get("receipt_id")
            and text.startswith("# AGENTS.md instructions for ")
            and "<environment_context>" in text):
        return None
    if _classify_user_string(text) != "user" or _parse_peer_tell(text) is not None:
        return None
    epoch = genuine_activity_epoch(event)
    floor = _event_timestamp_epoch(created_at) if created_at is not None else None
    if created_at is not None and (floor is None or epoch is None or epoch < floor):
        return None
    return epoch


def _title_is_untitled(value: object) -> bool:
    title = str(value or "").strip()
    if not title:
        return True
    lowered = title.lower()
    if lowered in _GENERIC_WINDOW_NAMES or lowered.startswith("gpt-"):
        return True
    if _NUMERIC_WINDOW_RE.fullmatch(title):
        return True
    return bool(_NEW_CHAT_PLACEHOLDER_RE.fullmatch(title))


def _new_chat_placeholder(host: object, index: int) -> str:
    value = str(host or "").strip()
    label = (value[:1].upper() + value[1:]) if value else "Unknown"
    base = f"New Chat - {label}"
    return base if index == 0 else f"{base} {index + 1}"


def _with_display_placeholders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill untitled rows' `display_name`/`title` with per-host numbered
    placeholders. Numbering is stable across refreshes: untitled rows are
    ordered by (created_at, session_name) within each host."""
    untitled_by_host: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if _title_is_untitled(row.get("title") or row.get("display_name")):
            untitled_by_host.setdefault(str(row.get("host") or ""), []).append(row)
    for host, group in untitled_by_host.items():
        group.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("session_name") or "")))
        for idx, row in enumerate(group):
            placeholder = _new_chat_placeholder(host, idx)
            row["display_name"] = placeholder
            row["title"] = placeholder
            row["title_source"] = TITLE_SOURCE_PLACEHOLDER
    return rows


def with_bootstrap_state(
    row: dict[str, Any], *, event_seen: bool | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Project whether a newly-opened seat produced any normalized event."""
    result = dict(row)
    internal_seen = bool(result.pop("_bootstrap_event_seen", False))
    seen = internal_seen if event_seen is None else event_seen
    if str(result.get("status") or "open") != "open":
        return result
    if result.get("bootstrap_state") in {"queued", "starting", "ready", "failed"}:
        result["state"] = result["bootstrap_state"]
        return result
    # A Codex reset interstitial is not a transient boot state. Preserve the
    # typed block until an input path explicitly observes a usable composer and
    # clears it durably; event activity alone is not current-pane evidence.
    if result.get("bootstrap_state") == "reset_blocked":
        return result
    if seen:
        result["bootstrap_state"] = "started"
        return result
    if result.get("bootstrap_state") == "unproven":
        return result
    created_at = str(result.get("created_at") or "").strip()
    try:
        created = datetime.fromisoformat(
            created_at[:-1] + "+00:00" if created_at.endswith(("Z", "z")) else created_at
        ).timestamp()
    except (TypeError, ValueError):
        result["bootstrap_state"] = "pending"
        return result
    age = (time.time() if now is None else now) - created
    result["bootstrap_state"] = (
        "unsubmitted" if age >= BOOTSTRAP_EVENT_WINDOW_S else "pending"
    )
    return result


class VerbError(Exception):
    """Business-rule failure. `server.py` renders it as `<verb>.error`."""

    def __init__(self, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        #: Extra keys merged into the `<verb>.error` frame. v1's report errors
        #: carry `error_message`/`schema_violations`/`report_id`; the vocabulary
        #: is preserved here without a per-verb error branch in `server.py`.
        self.extra: dict[str, Any] = extra


class Sessions:
    """Registry + lifecycle. All persistence goes through `store.py`.

    Holds an in-memory inventory keyed by stream_id so `list_sessions` — 100k
    dispatches / 12 days, the hottest verb — is served O(open) without touching
    the store thread (design § feature dispositions).
    """

    def __init__(self, store: Any, tmux: Any = None, local_host: str = "localhost",
                 alerts: Any = None, hosts: Any = None,
                 capture_timeout_s: float = DECISION_CAPTURE_TIMEOUT_S) -> None:
        self.store = store
        self.tmux = tmux
        # Local tmux can only ever speak for THIS host. Verbs that touch panes
        # must refuse a row belonging to another host rather than read local
        # tmux's silence as evidence about a remote pane (ledger req 5).
        self.local_host = local_host
        #: Where the close ladder's carcass/failed rungs raise an operator alert.
        #: Optional so unit tests can construct a registry without one.
        self.alerts = alerts
        #: The probe pool + transport seam. When present, `close` of a peer row
        #: runs the kill ladder over that peer's ssh-scoped tmux; None keeps
        #: close localhost-only (a foreign host is refused by `assert_local`).
        self.hosts = hosts
        self.capture_timeout_s = max(0.01, float(capture_timeout_s))
        self._inv: dict[str, dict[str, Any]] = {}
        # All registry swaps and synchronous projections use this re-entrant
        # lock. Store reads never hold it; reconcile only holds it while it
        # compares, merges, and publishes the complete replacement map.
        self._inventory_lock = threading.RLock()
        self._inv_epoch = 0
        self._popped_at_epoch: dict[str, int] = {}
        self._inventory_emitter: Any = None
        # Close and reopen of the same host/session name must be serialized in
        # this daemon.  The durable generation CAS below is still required for
        # callers that bypass this registry (or race a restart), but this lock
        # prevents two in-process close requests from both driving tmux.
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}
        #: Ledger callback for confirmed-dead close resolution. The callback is
        #: optional so the registry remains usable in focused lifecycle tests.
        self._awaiter_resolver: Any = None

    def _alert(self, kind: str, **fields: Any) -> None:
        if self.alerts is not None:
            self.alerts.emit(kind, **fields)

    async def _live_children(self, parent_stream_id: str) -> list[dict[str, Any]]:
        """Read the direct open-child set from the central durable registry."""
        children = await self.store.children_of(parent_stream_id)
        result: list[dict[str, Any]] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            host = str(child.get("host") or "")
            name = str(child.get("session_name") or "")
            stream_id = str(child.get("stream_id") or (f"{host}:{name}" if host and name else ""))
            if not stream_id:
                continue
            result.append({
                "stream_id": stream_id,
                "host": host,
                "status": str(child.get("status") or "open"),
                "parent_stream_id": str(child.get("parent_stream_id") or parent_stream_id),
            })
        return sorted(result, key=lambda item: item["stream_id"])

    def _capture_liveness(self, stream_id: str, row: dict[str, Any] | None) -> str | None:
        """Prefer the live observer overlay over the durable session copy."""
        for candidate in (self._inv.get(stream_id), row):
            value = candidate.get("capture_liveness") if isinstance(candidate, dict) else None
            if value in CAPTURE_LIVENESS_STATES:
                return str(value)
        return None

    def _capture_is_idle(self, stream_id: str, row: dict[str, Any] | None) -> bool:
        if self._capture_liveness(stream_id, row) != "idle":
            return False
        for candidate in (self._inv.get(stream_id), row):
            if isinstance(candidate, dict) and candidate.get("working") is True:
                return False
        return True

    async def _probe_capture_liveness(
        self, host: str, session_name: str, row: dict[str, Any] | None,
    ) -> str:
        """Refresh capture truth immediately before an idle-dependent close.

        The mirror/presence overlays are intentionally cached for observation;
        they are not a lease authorizing a destructive operation.  This probe
        is the close-time lease check shared by local and peer close paths:
        capture is bounded, a successful blank gets one retry, and every result
        replaces the stale liveness/working overlay before the gate reads it.
        """
        stream_id = f"{host}:{session_name}"

        def stamp(liveness: str, *, working: bool = False, label: str = "") -> str:
            overlay = {
                "capture_liveness": liveness,
                "working": working,
                "working_label": label,
            }
            # Keep the row supplied by the close operation current even when a
            # test/minimal registry has no in-memory inventory entry.
            if isinstance(row, dict):
                row.update(overlay)
            self.apply_live(stream_id, **overlay)
            return liveness

        try:
            if host == self.local_host:
                tmux = self.tmux
            elif self.hosts is not None and self.hosts.known(host):
                tmux = self.hosts.tmux_for(host)
            else:
                tmux = None
            capture_checked = getattr(tmux, "capture_checked", None)
            capture = getattr(tmux, "capture", None)
            if not callable(capture_checked) and not callable(capture):
                return stamp("transport_unknown")

            async def attempt() -> tuple[bool, str]:
                if callable(capture_checked):
                    checked = await asyncio.wait_for(
                        capture_checked(session_name, timeout=self.capture_timeout_s),
                        timeout=self.capture_timeout_s,
                    )
                    if isinstance(checked, tuple):
                        return bool(checked[0]), str(checked[1] or "")
                    return True, str(checked or "")
                captured = await asyncio.wait_for(
                    capture(session_name), timeout=self.capture_timeout_s,
                )
                return True, str(captured or "")

            ok, pane = await attempt()
            if not ok:
                return stamp("transport_unknown")
            if not pane.strip():
                # A successful blank is ambiguous. One bounded retry is enough
                # to distinguish a transient empty frame from a wedged pane.
                ok, pane = await attempt()
                if not ok:
                    return stamp("transport_unknown")
                if not pane.strip():
                    return stamp("wedged_unknown")

            provider = str(
                (row or {}).get("provider")
                or _provider_from_session_name(session_name)
                or "claude"
            )
            live = _extract_live_state(pane, provider)
            return stamp(
                "idle",
                working=bool(live.get("working")),
                label=str(live.get("working_label") or ""),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - close must fence ambiguous capture
            return stamp("transport_unknown")

    def _reap_fenced(
        self, host: str, session_name: str, row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        stream_id = f"{host}:{session_name}"
        liveness = self._capture_liveness(stream_id, row) or "unknown"
        self._alert(
            "reap_fenced",
            host=host,
            session_name=session_name,
            stream_id=stream_id,
            capture_liveness=liveness,
            reason="capture_liveness_not_idle",
        )
        return {
            "already_closed": False,
            "failed": True,
            "fenced": True,
            "reason": f"reap_fenced: capture_liveness={liveness}",
            "session": row,
        }

    async def _close_deferred(
        self, host: str, session_name: str, row: dict[str, Any] | None, *,
        close_kind: str = "session_close", attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Refuse a close of a working pane (the client's `defer_if_working`
        guard): the pane is NOT killed and the row stays open. Distinct from
        `_reap_fenced` so clients/agent-orch can tell an intentional deferral
        apart from a reap refusal. The deferred intent's retry stays owned by the
        client's own pending-close queue — no daemon-side queue is created."""
        stream_id = f"{host}:{session_name}"
        liveness = self._capture_liveness(stream_id, row) or "unknown"
        working = bool(
            (self._inv.get(stream_id) or {}).get("working")
            or (row or {}).get("working")
        )
        reason = f"defer_if_working: capture_liveness={liveness} working={working}"
        self._alert(
            "close_deferred", host=host, session_name=session_name,
            stream_id=stream_id, capture_liveness=liveness, reason=reason,
        )
        await self.store.record_close_audit(
            host=host, session_name=session_name, close_kind=close_kind,
            disposition="deferred", attribution=attribution, reason=reason,
        )
        return {
            "already_closed": False,
            "failed": False,
            "deferred": True,
            "reason": reason,
            "session": row,
        }

    def set_awaiter_resolver(self, resolver: Any) -> None:
        """Attach the durable await resolver after both subsystems are built."""
        self._awaiter_resolver = resolver

    async def _resolve_confirmed_dead_awaiters(
        self, row: dict[str, Any], *, reason: str,
    ) -> None:
        resolver = self._awaiter_resolver
        if not callable(resolver):
            return
        try:
            await resolver(
                str(row.get("stream_id") or ""),
                session_generation=str(row.get("session_generation") or ""),
                reason=reason or "confirmed_dead_close",
            )
        except Exception:  # noqa: BLE001 - close truth is already durable
            log.exception(
                "confirmed-dead await resolution failed stream=%s",
                row.get("stream_id"),
            )

    def assert_local(self, host: str, what: str) -> None:
        if host != self.local_host:
            raise VerbError(
                "unsupported_host",
                f"v2 {what} is localhost-only in this increment; {host} is not {self.local_host}",
            )

    # -- inventory ---------------------------------------------------------

    def _inventory_context(self):
        """Return the registry lock, with a no-op fallback for tiny test stubs."""
        return getattr(self, "_inventory_lock", None) or nullcontext()

    @staticmethod
    def _inventory_generation(row: dict[str, Any]) -> str:
        return str(row.get("session_generation") or f"created_at:{row.get('created_at') or ''}")

    def _pop_inventory_locked(self, stream_id: str) -> dict[str, Any] | None:
        if stream_id not in self._inv:
            return None
        self._inv_epoch += 1
        self._popped_at_epoch[stream_id] = self._inv_epoch
        return self._inv.pop(stream_id, None)

    def _set_inventory_row_locked(self, stream_id: str, row: dict[str, Any]) -> None:
        row.setdefault("working", False)
        previous = self._inv.get(stream_id)
        if (
            previous is None
            or self._inventory_generation(previous) != self._inventory_generation(row)
            or str(previous.get("stream_id") or "") != str(row.get("stream_id") or "")
        ):
            self._inv_epoch += 1
        self._inv[stream_id] = row

    def _replace_inventory_locked(self, rebuilt: dict[str, dict[str, Any]]) -> None:
        """Publish a complete boot replacement while recording structural work."""
        for stream_id in tuple(self._inv):
            if stream_id not in rebuilt:
                self._pop_inventory_locked(stream_id)
        for stream_id, row in rebuilt.items():
            current = self._inv.get(stream_id)
            if current is None or self._inventory_generation(current) != self._inventory_generation(row):
                self._inv_epoch += 1
        self._inv = {stream_id: {"working": False, **row} for stream_id, row in rebuilt.items()}

    def set_inventory_emitter(self, emitter: Any) -> None:
        """Attach the shared deduplicating inventory broadcaster."""
        self._inventory_emitter = emitter

    async def _rebuild_from_store(self) -> dict[str, dict[str, Any]]:
        """Read and fully build store truth without touching the live registry."""
        rows = await self.store.list_open_sessions_with_event_summary()
        rebuilt: dict[str, dict[str, Any]] = {}
        for source_row in rows:
            row = dict(source_row)
            title = row.get("title")
            if title is not None:
                row["display_name"] = title
            sid = row["stream_id"]
            episode = await self.store.routing_integrity_episode(sid)
            if isinstance(episode, dict):
                row.update({
                    "requested_model": episode.get("requested_model"),
                    "requested_effort": episode.get("requested_effort"),
                    "effective_model": episode.get("effective_model"),
                    "effective_effort": episode.get("effective_effort"),
                    "routing_integrity": "mismatch",
                    "routing_integrity_reason": episode.get("reason"),
                    "routing_integrity_event_id": episode.get("episode_id"),
                    "routing_integrity_updated_at": episode.get("updated_at"),
                })
            rebuilt[sid] = row
        return rebuilt

    async def refresh(self) -> int:
        """Rebuild the inventory from the store at daemon startup only."""
        rebuilt = await self._rebuild_from_store()
        with self._inventory_context():
            self._replace_inventory_locked(rebuilt)
        with self._inventory_context():
            return len(self._inv)

    def list_open(self) -> list[dict[str, Any]]:
        from agents_roster import project
        with self._inventory_context():
            rows = _with_display_placeholders([with_bootstrap_state(dict(row)) for row in self._inv.values()])
            if not hasattr(self, "_agent_transitions"):
                self._agent_transitions = {}
            return project(rows, self._agent_transitions)

    def get(self, stream_id: str) -> dict[str, Any] | None:
        with self._inventory_context():
            row = self._inv.get(stream_id)
            if not row:
                return None
            result = dict(row)
            if any(child.get("parent_stream_id") == stream_id for child in self._inv.values()):
                result["agents"] = next(r["agents"] for r in self.list_open() if r["stream_id"] == stream_id)
            return result

    async def apply_report_state(self, report):
        with self._inventory_context():
            row = self._inv.get(report["from_stream_id"])
            if row and row.get("session_generation") == report.get("session_generation"):
                row["_agent_report_status"] = report["status"]
                row["_agent_report_ts"] = report["ingested_at"]
        if emit := getattr(self._inventory_emitter, "emit_if_changed", None):
            await emit(immediate=True)

    def apply_live(self, stream_id: str, **overlay: Any) -> dict[str, Any] | None:
        """Merge mirror-observed live fields (preview/working/working_label/
        online/last_activity/pane_*) onto the in-memory summary so every read
        path — `list_sessions`, the hello snapshot, and the pushed
        `session.inventory` — serves ONE consistent view.

        These fields are deliberately not persisted (v1 parity: they are
        tmux-derived and refreshed every pump tick); the durable pane columns are
        written by the mirror through `store.update_session`. Returns None when
        the session is no longer in the inventory (closed under the pump), so the
        mirror never resurrects a row the registry already dropped — the A8 fix
        cuts both ways."""
        with self._inventory_context():
            row = self._inv.get(stream_id)
            if row is None:
                return None
            if overlay.get("working") is True:
                row["watch_working_at"] = time.time()
            row.update({
                field: value for field, value in overlay.items()
                if field in LIVE_OVERLAY_FIELDS
            })
            return dict(row)

    def restore_genuine_activity(
        self, stream_id: str, events: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Replace activity with current-generation durable turn evidence."""
        with self._inventory_context():
            row = self._inv.get(stream_id)
            if row is None:
                return None
            generation = str(row.get("session_generation") or "").strip()
            floor = _event_timestamp_epoch(row.get("created_at"))
            current = [event for event in events if floor is not None
                       and (_event_timestamp_epoch(event.get("timestamp")) or 0) >= floor]
            epochs = [genuine_activity_epoch(event) for event in current]
            valid = [epoch for epoch in epochs if epoch is not None]
            user_epochs = [operator_user_epoch(event, row.get("created_at")) for event in current]
            users = [epoch for epoch in user_epochs if epoch is not None]
            row.update({
                "_bootstrap_event_seen": bool(current),
                "operator_activity_at": max(users) if generation and users else None,
                "operator_activity_generation": generation if generation and users else None,
                "genuine_activity_at": max(valid) if generation and valid else None,
                "genuine_activity_generation": generation if generation and valid else None,
            })
            return dict(row)

    def apply_genuine_activity_event(
        self, stream_id: str, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Advance event time and genuine-turn state after a durable insert."""
        with self._inventory_context():
            row = self._inv.get(stream_id)
            if row is None:
                return None
            floor = _event_timestamp_epoch(row.get("created_at"))
            timestamp = _event_timestamp_epoch(event.get("timestamp"))
            if floor is None or timestamp is None or timestamp < floor:
                return dict(row)
            row["_bootstrap_event_seen"] = True
            user_epoch = operator_user_epoch(event, row.get("created_at"))
            generation = str(row.get("session_generation") or "")
            if user_epoch is not None and generation:
                prior_user = row.get("operator_activity_at")
                if row.get("operator_activity_generation") != generation or not isinstance(prior_user, (int, float)) or user_epoch > prior_user:
                    row.update(operator_activity_at=user_epoch, operator_activity_generation=generation)
            event_timestamp = str(event.get("timestamp") or "").strip()
            event_epoch = _event_timestamp_epoch(event_timestamp)
            prior_epoch = _event_timestamp_epoch(row.get("last_event_at"))
            if event_epoch is not None and (prior_epoch is None or event_epoch >= prior_epoch):
                row["last_event_at"] = event_timestamp
            generation = str(row.get("session_generation") or "").strip()
            epoch = genuine_activity_epoch(event)
            if not generation or epoch is None:
                return dict(row)
            prior_generation = str(row.get("genuine_activity_generation") or "")
            prior = row.get("genuine_activity_at")
            prior_epoch = (
                float(prior)
                if prior_generation == generation
                and isinstance(prior, (int, float))
                and not isinstance(prior, bool)
                and math.isfinite(float(prior))
                else None
            )
            if prior_epoch is None or epoch > prior_epoch:
                row.update({
                    "genuine_activity_at": epoch,
                    "genuine_activity_generation": generation,
                })
            return dict(row)

    def apply_durable(self, stream_id: str, **fields: Any) -> dict[str, Any] | None:
        """Refresh durable fields that were written by an off-loop observer.

        The routing observer persists through ``Store`` and then uses this
        narrow registry seam so list/hello reads do not lag the SQLite row.
        """
        with self._inventory_context():
            row = self._inv.get(stream_id)
            if row is None:
                return None
            row.update(fields)
            return dict(row)

    @staticmethod
    def split(stream_id: str) -> tuple[str, str]:
        host, _, name = stream_id.partition(":")
        return host, name

    def _cache(self, row: dict[str, Any] | None, **overlay: Any) -> dict[str, Any]:
        if row is None:
            raise VerbError("unknown_session", "Unknown or inactive session")
        sid = row["stream_id"]
        with self._inventory_context():
            merged = {**self._inv.get(sid, {}), **row, **overlay}
            if str(merged.get("status") or "open") == "open":
                self._set_inventory_row_locked(sid, merged)
            else:
                self._pop_inventory_locked(sid)
            return dict(merged)

    def _lifecycle_lock(self, host: str, session_name: str) -> asyncio.Lock:
        sid = f"{host}:{session_name}"
        lock = self._lifecycle_locks.get(sid)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_locks[sid] = lock
        return lock

    @staticmethod
    def _row_generation(row: dict[str, Any]) -> str:
        return str(row.get("session_generation") or f"created_at:{row.get('created_at') or ''}")

    _RECONCILED_RESTORE_FIELDS = (
        "visibility", "parent_stream_id", "role", "phase", "handoff_from_stream_id",
        "spec_id", "spec_resolution", "offline_since_ts", "self_close_on_completion", "no_watch",
        "token_hash", "token_hash_version", "harness_run_id", "opened_by_host_id",
        "observer_binding", "jsonl_path", "claude_session_id", "claude_session_lineage", "spec_ids", "status_card", "context_tokens",
        "model_context_window", "context_updated_at", "context_level", "requested_model",
        "requested_effort", "effective_model", "effective_effort", "provider",
        "qualified_spec_ids", "spec_binding_provenance", "bootstrap_state", "title",
        "objective", "objective_source",
    )

    # -- lifecycle ---------------------------------------------------------

    async def open(self, host: str, session_name: str, *, fence: str | None = None, **fields: Any) -> dict[str, Any]:
        """`fence` is the reserving request_id (ledger req 4): a reserved id can
        never be adopted by an unrelated live session."""
        async with self._lifecycle_lock(host, session_name):
            held = [r for r in await self.store.reservations(include_expired=True)
                    if r["host"] == host and r["session_name"] == session_name]
            if held and (fence is None or held[0].get("request_id") != fence):
                raise VerbError("stream_id_reserved", f"{host}:{session_name} is reserved by another spawn")
            title = fields.pop("title", None) or fields.pop("display_name", None)
            if title:
                fields["title"] = title
            row = await self.store.open_session(host, session_name, spawn_request_id=fence, **fields)
            if row is None:
                raise VerbError("spawn_fence_lost", f"{host}:{session_name} spawn no longer owns admission")
            overlay = {"title": title, "display_name": title, "title_source": "agent"} if title else {}
            return self._cache(row, **overlay)

    async def resolve(self, msg: dict[str, Any]) -> tuple[str, str]:
        """Resolve the target the daemon was addressed with, accepting every
        wire form a real consumer sends: `stream_id`, `host`+`session_name`, or
        `to_stream_id` — the field the agent-orch CLI puts a tell/send target in
        (the CLI is the consumer contract, so the daemon meets its field names)."""
        stream_id = str(msg.get("stream_id") or msg.get("to_stream_id") or "").strip()
        if stream_id:
            host, name = self.split(stream_id)
        else:
            host = str(msg.get("host") or "").strip()
            name = str(msg.get("session_name") or "").strip()
        if not host or not name:
            raise VerbError("bad_request", "stream_id / to_stream_id (or host + session_name) is required")
        return host, name

    async def rename(self, host: str, session_name: str, display_name: str, source: str = "agent") -> dict[str, Any]:
        display_name = display_name.strip()
        if not display_name:
            raise VerbError("bad_request", "display_name is required")
        if len(display_name) > 80:
            raise VerbError("bad_request", "display_name must be 80 characters or fewer")
        sid = f"{host}:{session_name}"
        current = self._inv.get(sid)
        if current is None:
            raise VerbError("unknown_session", "Unknown or inactive session")
        # v1 rule: an agent rename never overwrites a manual title.
        if source == "agent" and current.get("title_source") == "manual":
            return dict(current)
        row = await self.store.update_session(host, session_name, title=display_name)
        if row is None or str(row.get("status") or "") != "open":
            raise VerbError("unknown_session", "Unknown or inactive session")
        renamed = self._cache(
            {**current, **row, "stream_id": sid},
            title=display_name, display_name=display_name, title_source=source,
        )
        if emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None):
            await emit_if_changed(immediate=True)
        return renamed

    # -- question visibility scope (v1 parity) -----------------------------

    async def _lineage_records(self) -> dict[str, dict[str, Any]]:
        """Every session (open OR closed) keyed by stream_id with just the two
        lineage fields the scope walk needs. Closed rows are included on purpose:
        a question outlives the worker that asked it, so `prompt list --from
        <lead>` must still resolve a since-closed hidden child up to its lead
        (v1 read `list_sessions(status=None)` for the same reason)."""
        records: dict[str, dict[str, Any]] = {}
        for row in await self.store.list_sessions(status=None):
            sid = str(row.get("stream_id") or "").strip()
            if not sid:
                continue
            records[sid] = {
                "parent_stream_id": str(row.get("parent_stream_id") or "").strip() or None,
                "visibility": str(row.get("visibility") or "default"),
            }
        return records

    @staticmethod
    def _nearest_default_ancestor(stream_id: str, records: dict[str, dict[str, Any]]) -> str | None:
        """Climb parent links until a session whose effective visibility is
        `default` — the operator-facing scope owner (v1
        `_nearest_visible_ancestor_for_lineage`). A hidden/subagent/visible node
        is transparent and keeps climbing; a missing record reads as `default`
        (an unknown/phantom parent owns its subtree), matching v1."""
        seen: set[str] = set()
        current: str | None = stream_id
        depth = 0
        while current and depth < 64 and current not in seen:
            seen.add(current)
            record = records.get(current)
            if str((record or {}).get("visibility") or "default") == "default":
                return current
            parent = (record or {}).get("parent_stream_id")
            if not parent:
                return None
            current = parent
            depth += 1
        return None

    async def visible_question_scope_stream_ids(self, viewer_stream_id: str) -> list[str]:
        """The producer streams whose questions surface to `viewer_stream_id`
        (v1 `_visible_question_scope_stream_ids`): the viewer itself plus every
        descendant whose nearest operator-visible (`default`) ancestor is the
        viewer. This is what lets a lead's `prompt list --from <lead>` see the
        questions its hidden workers asked, while NOT leaking the workers of a
        visible sub-lead nested beneath it."""
        viewer = str(viewer_stream_id or "").strip()
        if not viewer:
            return []
        records = await self._lineage_records()
        children_by_parent: dict[str, list[str]] = {}
        for sid, record in records.items():
            parent = record.get("parent_stream_id")
            if parent:
                children_by_parent.setdefault(parent, []).append(sid)
        scoped: list[str] = [viewer]
        seen: set[str] = {viewer}
        queue: deque[tuple[str, int]] = deque((c, 1) for c in children_by_parent.get(viewer, []))
        while queue:
            sid, depth = queue.popleft()
            if sid in seen or depth > 64:
                continue
            seen.add(sid)
            if self._nearest_default_ancestor(sid, records) == viewer:
                scoped.append(sid)
            for child in children_by_parent.get(sid, []):
                if child not in seen:
                    queue.append((child, depth + 1))
        return scoped

    async def reparent(
        self,
        host: str,
        session_name: str,
        new_parent_stream_id: str,
        *,
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Move one open worker to an open parent in the central registry.

        Parentage is daemon-owned metadata, not a pane-local operation.  That
        distinction is what makes a successor on one host able to adopt a
        child on another host without pretending that local tmux can observe
        the peer.  The caller check mirrors the useful v1 authorization rows:
        the new parent, the current parent, or a live handoff successor may
        perform the move. The caller must be the server-derived owner of a
        verified stream token; a wire identity claim alone cannot authorize a
        lifecycle mutation.
        """
        worker_stream_id = f"{host}:{session_name}"
        new_parent_stream_id = str(new_parent_stream_id or "").strip()
        if not host or not session_name or ":" not in new_parent_stream_id:
            raise VerbError("invalid_request", "worker and new parent stream ids are required")
        new_parent_host, new_parent_name = self.split(new_parent_stream_id)
        if not new_parent_host or not new_parent_name or new_parent_stream_id == worker_stream_id:
            raise VerbError("invalid_request", "new parent must be a different stream")

        worker = self._inv.get(worker_stream_id) or await self.store.fetch_session(host, session_name)
        if worker is None or str(worker.get("status") or "open") != "open":
            raise VerbError("reparent_worker_not_found", f"{worker_stream_id} is not an open worker")
        old_parent_stream_id = str(worker.get("parent_stream_id") or "").strip() or None

        parent = self._inv.get(new_parent_stream_id) or await self.store.fetch_session(
            new_parent_host, new_parent_name
        )
        if parent is None or str(parent.get("status") or "open") != "open":
            raise VerbError("reparent_target_closed", f"{new_parent_stream_id} is not an open parent")

        auth = auth_context if isinstance(auth_context, dict) else {}
        if not bool(auth.get("token_verified")):
            raise VerbError("stream_ownership_unverified", "reparent requires a verified stream-token owner")
        caller = str(auth.get("stream_id") or "").strip()
        if not caller:
            raise VerbError("stream_ownership_unverified", "reparent requires a verified stream-token owner")
        allowed = caller in {new_parent_stream_id, old_parent_stream_id}
        if not allowed:
            caller_row = None
            if ":" in caller:
                caller_host, caller_name = self.split(caller)
                caller_row = self._inv.get(caller) or await self.store.fetch_session(
                    caller_host, caller_name
                )
            allowed = (
                isinstance(caller_row, dict)
                and str(caller_row.get("status") or "open") == "open"
                and str(caller_row.get("handoff_from_stream_id") or "").strip()
                == (old_parent_stream_id or "")
            )
        if not allowed:
            raise VerbError("reparent_unauthorized", f"{caller} cannot reparent {worker_stream_id}")

        updated = await self.store.update_session(
            host, session_name, parent_stream_id=new_parent_stream_id
        )
        if updated is None or str(updated.get("status") or "open") != "open":
            raise VerbError("reparent_worker_not_found", f"{worker_stream_id} is no longer open")
        self._cache(updated)
        if emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None):
            await emit_if_changed(immediate=True)
        return {
            "type": "reparent.ok",
            "ok": True,
            "worker_stream_id": worker_stream_id,
            "old_parent_stream_id": old_parent_stream_id,
            "new_parent_stream_id": new_parent_stream_id,
        }

    async def set_role(
        self,
        host: str,
        session_name: str,
        role: str,
        *,
        auth_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set a role on one LIVE seat (`role set`).

        `role` is daemon-owned metadata on the `sessions` row; the authority a
        role grants is read from `sessions.role` by `window_schedule`, so a
        post-hoc set changes scheduler authority. The nexus gate is the safety
        check: only the operator-facing seat (operator/service auth, or a caller
        whose own seat is already `nexus`) or the session's parent may grant
        `nexus`; any other role is open to any authenticated caller. The verb is
        authenticated exactly like `reparent`: a wire identity claim alone
        cannot mutate a seat.
        """
        role = str(role or "").strip()
        if not role:
            raise VerbError("bad_request", "role is required")
        if len(role) > 40 or not re.fullmatch(r"[a-z0-9_-]+", role):
            raise VerbError("bad_request", "role must be a short slug ([a-z0-9_-])")

        stream_id = f"{host}:{session_name}"
        target = self._inv.get(stream_id) or await self.store.fetch_session(host, session_name)
        if target is None or str(target.get("status") or "open") != "open":
            raise VerbError("unknown_session", f"{stream_id} is not an open session")

        auth = auth_context if isinstance(auth_context, dict) else {}
        operator = bool(auth.get("operator_authenticated"))
        service = bool(auth.get("service_authenticated"))
        token_verified = bool(auth.get("token_verified"))
        if not (operator or service or token_verified):
            raise VerbError(
                "stream_ownership_unverified",
                "role set requires operator or a verified stream-token owner",
            )
        caller = str(
            auth.get("stream_id")
            or auth.get("service_actor")
            or auth.get("operator_principal")
            or ""
        ).strip()

        if role == "nexus":
            operator_facing = operator or service
            if not operator_facing and caller and ":" in caller:
                caller_host, caller_name = self.split(caller)
                caller_row = self._inv.get(caller) or await self.store.fetch_session(
                    caller_host, caller_name
                )
                operator_facing = (
                    isinstance(caller_row, dict)
                    and str(caller_row.get("role") or "") == "nexus"
                    and str(caller_row.get("status") or "open") == "open"
                )
            is_parent = bool(caller) and caller == str(
                target.get("parent_stream_id") or ""
            ).strip()
            if not (operator_facing or is_parent):
                raise VerbError(
                    "role_authority_denied",
                    "setting role=nexus requires the operator-facing seat or the session's parent",
                )

        updated = await self.store.update_session(host, session_name, role=role)
        if updated is None or str(updated.get("status") or "open") != "open":
            raise VerbError("unknown_session", f"{stream_id} is no longer open")
        await self.store.set_session_role_source(
            host, session_name, role_source="role_set",
            actor=caller or None, changed_at=iso_now(),
        )
        cached = self._cache(updated)
        if emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None):
            await emit_if_changed(immediate=True)
        return cached

    async def reparent_children(self, old_parent_stream_id: str, new_parent_stream_id: str) -> int:
        """Move the retiring leader's direct children to the successor on
        `spawn --handoff` (v1 `_handoff_reparent_children`, Pin 7: enumerate by
        `parent_stream_id == old` plainly). Parentage is central daemon metadata,
        so the move is a pure `parent_stream_id` reassignment — no per-pane RPC
        and no same-host restriction. Returns the count moved, including rows
        owned by a configured SSH peer."""
        moved = 0
        for child in await self.store.children_of(old_parent_stream_id):
            if child.get("stream_id") == new_parent_stream_id:
                continue
            row = await self.store.update_session(
                child["host"], child["session_name"], parent_stream_id=new_parent_stream_id
            )
            if row is None:
                continue
            self._cache(row)
            moved += 1
        if moved and (emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None)):
            await emit_if_changed(immediate=True)
        return moved

    async def set_visibility(self, host: str, session_name: str, visibility: str) -> dict[str, Any]:
        if visibility not in VISIBILITIES:
            raise VerbError("bad_request", f"visibility must be one of {', '.join(VISIBILITIES)}")
        row = await self.store.update_session(host, session_name, visibility=visibility)
        updated = self._cache(row)
        if emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None):
            await emit_if_changed(immediate=True)
        return updated

    async def set_status_card(self, host: str, session_name: str, fields: dict[str, Any]) -> dict[str, Any]:
        from ledger import apply_status_card_update

        sid = f"{host}:{session_name}"
        current = (self._inv.get(sid) or {}).get("status_card")
        card = apply_status_card_update(current if isinstance(current, dict) else None, fields, now_iso=iso_now())
        row = await self.store.update_session(host, session_name, status_card=card)
        if row is None:
            # The card contract promises restart survival; never ack a write
            # that did not reach the sessions row (v1 parity).
            raise VerbError("unknown_session", "session has no persisted row")
        return self._cache(row)

    async def mark_closed(
        self,
        host: str,
        session_name: str,
        reason: str = "",
        *,
        reap_status: str = "reaped",
        survivors: list[dict[str, Any]] | None = None,
        expected_generation: str | None = None,
        close_kind: str = "session_close",
    ) -> dict[str, Any] | None:
        """Mark the row closed. Callers MUST have confirmed process death first
        (ledger req 5) — this method never kills anything.

        `close_kind` (rollback-truth spec, AC5) types the close origin; a
        spawn-admission rollback passes ``spawn_rollback`` so a failed-boot
        close is queryably distinct from a live-session close (default
        ``session_close``)."""
        async with self._lifecycle_lock(host, session_name):
            current = self._inv.get(f"{host}:{session_name}") or await self.store.fetch_session(host, session_name)
            generation = expected_generation or (self._row_generation(current) if current else None)
            return await self._mark_closed_locked(
                host, session_name, reason,
                reap_status=reap_status, survivors=survivors,
                expected_generation=generation, close_kind=close_kind,
            )

    async def _mark_closed_locked(
        self,
        host: str,
        session_name: str,
        reason: str = "",
        *,
        reap_status: str = "reaped",
        survivors: list[dict[str, Any]] | None = None,
        expected_generation: str | None = None,
        close_kind: str = "session_close",
        attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = await self.store.mark_closed(
            host, session_name,
            closed_at=iso_now(), pane_status="pane_dead",
            expected_generation=expected_generation,
            close_kind=close_kind, reason=reason,
            attribution=attribution,
        )
        # Every close funnels through here — make the close path LOG-audible so a
        # future vanish is nameable from the append-only daemon log even if the DB
        # row is later overwritten by a same-name reopen (the VANISH class was
        # log-silent: zero close lines for the lost stream). One line, generation
        # + reason + whether it actually closed.
        log.info(
            "session close mark_closed stream=%s generation=%s reason=%s closed=%s",
            f"{host}:{session_name}", expected_generation or "(current)",
            reason or "(none)", row is not None,
        )
        if row is not None:
            with self._inventory_context():
                self._pop_inventory_locked(f"{host}:{session_name}")
            if emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None):
                await emit_if_changed(immediate=True)
            await self.store.upsert_session_reap(
                f"{host}:{session_name}",
                reap_status=reap_status,
                survivors=survivors or [],
                updated_at=str(row.get("closed_at") or iso_now()),
            )
            await self._resolve_confirmed_dead_awaiters(
                row, reason=reason or "confirmed_dead_close",
            )
        return row

    async def mark_reconciled_dead(
        self,
        host: str,
        session_name: str,
        *,
        presumed_dead_at: str,
        closed_at: str,
        expected_generation: str,
    ) -> dict[str, Any] | None:
        """Persist affirmative pane death and remove the row from open views.

        This is deliberately separate from ``close``: no pane command is issued
        after the presence reconciler has confirmed that the target pane is
        already gone.  The durable marker retains the evidence episode for
        audit/readback even though the UI's open inventory drops the ghost.
        """
        async with self._lifecycle_lock(host, session_name):
            row = await self.store.mark_reconciled_dead(
                host,
                session_name,
                expected_generation=expected_generation,
                presumed_dead_at=presumed_dead_at,
                closed_at=closed_at,
            )
            log.info(
                "session close reconciled_dead stream=%s generation=%s closed=%s",
                f"{host}:{session_name}", expected_generation or "(current)",
                row is not None,
            )
            if row is not None:
                with self._inventory_context():
                    self._pop_inventory_locked(f"{host}:{session_name}")
                if emit_if_changed := getattr(self._inventory_emitter, "emit_if_changed", None):
                    await emit_if_changed(immediate=True)
                await self._resolve_confirmed_dead_awaiters(
                    row, reason="reconciler_confirmed_dead",
                )
            return row

    async def restore_reconciled(self, host: str, session_name: str) -> dict[str, Any]:
        """Reopen a reconciler-closed row after proving its pane is alive.

        This path is deliberately narrower than ``open``: operator/self closes
        have no reconciler evidence and cannot be resurrected accidentally.
        The old lifecycle markers are cleared by ``Store.open_session`` while
        its new generation prevents late observations from the old lifecycle
        from closing the restored row.
        """
        async with self._lifecycle_lock(host, session_name):
            current = self._inv.get(f"{host}:{session_name}") or await self.store.fetch_session(host, session_name)
            if current is None:
                raise VerbError("unknown_session", "Unknown or inactive session")
            if str(current.get("status") or "") == "open":
                return {"restored": False, "already_open": True, "liveness": "already_open", "session": dict(current)}
            if not (
                str(current.get("presumed_dead_at") or "").strip()
                or str(current.get("dead_open_closed_at") or "").strip()
            ):
                raise VerbError(
                    "restore_not_reconciler_closed",
                    "only a reconciler-marked close may be restored",
                )

            if host == self.local_host:
                tmux = self.tmux
            elif self.hosts is not None:
                tmux = self.hosts.tmux_for(host)
            else:
                raise VerbError("unsupported_host", f"no tmux transport for {host}")
            probe = getattr(tmux, "session_state", None)
            if not callable(probe):
                raise VerbError("restore_liveness_unproven", "tmux liveness probe is unavailable")
            try:
                state = probe(session_name)
                if hasattr(state, "__await__"):
                    state = await state
            except Exception as exc:  # noqa: BLE001 - no DB mutation without proof
                raise VerbError("restore_liveness_unproven", str(exc)) from exc
            if str(state or "").strip().lower() != "alive":
                raise VerbError(
                    "restore_liveness_unproven",
                    f"tmux session is not proven alive: {state or 'unknown'}",
                )

            fields = {
                field: current.get(field)
                for field in self._RECONCILED_RESTORE_FIELDS
                if field in current
            }
            fields["pane_status"] = "pane_alive"
            identity_probe = getattr(tmux, "pane_identity", None)
            if callable(identity_probe):
                try:
                    identity = identity_probe(session_name)
                    if hasattr(identity, "__await__"):
                        identity = await identity
                    if isinstance(identity, dict) and identity.get("pane_pid"):
                        fields["pane_pid"] = str(identity["pane_pid"])
                except Exception:  # noqa: BLE001 - session_state is sufficient proof
                    pass
            row = await self.store.open_session(host, session_name, **fields)
            restored = self._cache(row)
            return {"restored": True, "already_open": False, "liveness": "alive", "session": restored}

    async def close(
        self,
        host: str,
        session_name: str,
        reason: str = "",
        *,
        expected_generation: str | None = None,
        close_kind: str = "session_close",
        requires_idle: bool = False,
        requires_hidden: bool = False,
        operator_override: bool = False,
        operator_confirm: bool = False,
        defer_if_working: bool = False,
        attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Serialize one lifecycle operation for this host/session name.

        A caller that validated a specific generation (e.g. the reconciler
        self-close sweep, which authorizes a close from a generation-scoped
        report) passes `expected_generation` so the close is fenced to exactly
        that generation: `_close_locked` re-reads the authoritative row and
        refuses (never kills) when a close/reopen produced a different
        generation. When omitted, the current inventory generation is used.

        `requires_hidden` fences a hidden-only close (the sweep): the
        authoritative row's visibility is re-read INSIDE the lifecycle lock, so
        a `set_visibility` flip after the caller's own pre-lock recheck cannot
        be outrun — a now-visible seat is refused, never killed."""
        sid = f"{host}:{session_name}"
        if expected_generation is None:
            hint = self._inv.get(sid)
            expected_generation = (
                self._row_generation(hint)
                if hint is not None and str(hint.get("status") or "open") == "open"
                else None
            )
        async with self._lifecycle_lock(host, session_name):
            return await self._close_locked(
                host, session_name, reason,
                expected_generation=expected_generation, close_kind=close_kind,
                requires_idle=requires_idle, requires_hidden=requires_hidden,
                operator_override=operator_override,
                operator_confirm=operator_confirm,
                defer_if_working=defer_if_working, attribution=attribution,
            )

    async def reap_idle(
        self,
        host: str,
        session_name: str,
        reason: str = "idle_reap",
        *,
        operator_override: bool = False,
    ) -> dict[str, Any]:
        """Run a destructive close only when capture proved the pane idle."""
        return await self.close(
            host,
            session_name,
            reason,
            close_kind="idle_reap",
            requires_idle=True,
            operator_override=operator_override,
        )

    async def _close_locked(
        self,
        host: str,
        session_name: str,
        reason: str = "",
        *,
        expected_generation: str | None = None,
        close_kind: str = "session_close",
        requires_idle: bool = False,
        requires_hidden: bool = False,
        operator_override: bool = False,
        operator_confirm: bool = False,
        defer_if_working: bool = False,
        attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bounded, idempotent, keyed on CONFIRMED process death (ledger req 5).

        The order is always terminate -> confirm -> mark closed, never
        mark-then-kill, except explicit operator-confirmed offline intent
        (separate deferred-reap ledger, never recorded as verified death).
        A closed row with a live tree is the wedge that killed
        live work. A row is NEVER marked closed while a userspace-runnable
        process still exists, so the v1 `row_closed_tree_alive` cascade stays
        impossible. `_terminate_pane` runs the escalation ladder and returns one
        of `ok` / `carcass` / `failed`:

          ok      the pane is gone (graceful kill, or a by-pid SIGKILL) -> mark closed
          carcass SIGKILL landed but the process is a kernel D-state husk that can
                  never execute userspace again -> mark closed as fact + alert
          failed  no signal was deliverable (no pane pid, tmux unresponsive) ->
                  the row stays OPEN, `close.failed`, alert; SessionReconciler
                  re-checks

        Local tmux is evidence about the LOCAL host only. Closing a remote row
        through it would read "no local pane" as confirmed death and false-close
        a live remote session, so without a transport a foreign host is refused
        outright; with a `hosts` pool the close runs over the peer's own tmux.
        """
        if self.hosts is not None and not self.hosts.is_local(host):
            return await self._close_remote(
                host, session_name, reason,
                expected_generation=expected_generation, close_kind=close_kind,
                requires_idle=requires_idle, requires_hidden=requires_hidden,
                operator_override=operator_override,
                operator_confirm=operator_confirm,
                defer_if_working=defer_if_working, attribution=attribution,
            )
        self.assert_local(host, "close")
        sid = f"{host}:{session_name}"
        # The durable row is authoritative here. The in-memory mirror may still
        # carry the predecessor while a close/reopen happened in another task.
        row = await self.store.fetch_session(host, session_name) or self._inv.get(sid)
        if row is not None and str(row.get("status") or "open") != "open":
            reap = await self.store.get_session_reap(sid)
            return {
                "already_closed": True,
                "failed": False,
                "session": row,
                "reap_status": (reap or {}).get("reap_status", "unknown"),
                "survivors": (reap or {}).get("survivors", []),
                "live_children": await self._live_children(sid),
            }
        if row is not None:
            current_generation = self._row_generation(row)
            if expected_generation is not None and current_generation != expected_generation:
                return await self._stale_close_result(sid, row)
            expected_generation = current_generation
        # Hidden-only fence, under the lifecycle lock: fail closed if the
        # authoritative row is no longer hidden (a set_visibility flip after the
        # sweep's pre-lock recheck). Refuse, never kill.
        if requires_hidden and str((row or {}).get("visibility") or "default") != "hidden":
            return self._visibility_fenced_result(row)
        pane_live = bool(self.tmux) and await self.tmux.has_session(session_name)
        if row is None and not pane_live:
            raise VerbError("unknown_session", "Unknown session")
        if (requires_idle or defer_if_working) and not operator_override:
            await self._probe_capture_liveness(host, session_name, row)
            if not self._capture_is_idle(sid, row):
                # The client's `defer_if_working` guard means "do not kill a busy
                # seat" — refuse with a distinct deferred outcome. A `reap`/
                # `requires_idle` close keeps the existing reap_fenced refusal.
                if defer_if_working:
                    return await self._close_deferred(
                        host, session_name, row, close_kind=close_kind,
                        attribution=attribution,
                    )
                await self.store.record_close_audit(
                    host=host, session_name=session_name, close_kind=close_kind,
                    disposition="fenced", attribution=attribution,
                    reason=f"reap_fenced: capture_liveness="
                           f"{self._capture_liveness(sid, row) or 'unknown'}",
                )
                return self._reap_fenced(host, session_name, row)

        # No pane at entry is not a process-tree readback. Keep the close
        # unknown so descendants cannot be silently lost behind `reaped, []`.
        reap: dict[str, Any] = {"reap_status": "unknown", "survivors": []}
        if pane_live:
            state, pane_pid, reap = await self._terminate_pane(session_name)
            if state == "failed":
                # No deliverable signal: the process may still run userspace, so
                # the row must NOT be recorded closed. Honest `close.failed`.
                self._alert("close_failed", host=host, session_name=session_name,
                            pane_pid=pane_pid, reason="no_deliverable_signal")
                return {"failed": True, "already_closed": False,
                        "reason": "pane_process_unkillable: no deliverable signal "
                                  "(no pane pid and tmux unresponsive)",
                        "session": row}
            if state == "carcass":
                # SIGKILLed but still visible = uninterruptible sleep. It can
                # never run userspace again, so closing the row is fact, not a
                # guess — but it is worth an operator's eyes (carcass pid).
                self._alert("close_carcass", host=host, session_name=session_name,
                            pane_pid=pane_pid)

        already = row is not None and str(row.get("status") or "") != "open"
        closed = await self._mark_closed_locked(
            host,
            session_name,
            reason,
            reap_status=str(reap.get("reap_status") or "unknown"),
            survivors=reap.get("survivors") if isinstance(reap.get("survivors"), list) else [],
            expected_generation=expected_generation,
            close_kind=close_kind,
            attribution=attribution,
        )
        if closed is None and expected_generation is not None:
            current = await self.store.fetch_session(host, session_name)
            return await self._stale_close_result(sid, current or row)
        # Close can race spawn: the pane exists but its row is not written yet,
        # so `row` is None and `mark_closed`'s UPDATE touches nothing. The kill
        # still happened, so the reply is `close.ok` — but never with a null
        # session (QA #12). Synthesize a minimal closed descriptor the caller
        # can key on when there was no persisted row to return.
        session = closed or row or {
            "stream_id": sid, "host": host, "session_name": session_name,
            "status": "closed", "closed_at": iso_now(),
        }
        live_children = await self._live_children(sid)
        reap = await self.store.get_session_reap(sid)
        return {
            "already_closed": already,
            "closed": True,
            "failed": False,
            "session": session,
            "reap_status": (reap or {}).get("reap_status", "reaped"),
            "survivors": (reap or {}).get("survivors", []),
            "live_children": live_children,
        }

    @staticmethod
    def _visibility_fenced_result(row: dict[str, Any] | None) -> dict[str, Any]:
        """Refuse (never kill) a hidden-only close whose current row is no longer
        hidden. No `closed` key: the caller (sweep) counts only real closes."""
        return {
            "already_closed": False,
            "failed": False,
            "visibility_changed": True,
            "session": row,
        }

    async def _stale_close_result(self, sid: str, row: dict[str, Any] | None) -> dict[str, Any]:
        reap = await self.store.get_session_reap(sid)
        return {
            "already_closed": True,
            "stale_generation": True,
            "failed": False,
            "session": row,
            "reap_status": (reap or {}).get("reap_status", "unknown"),
            "survivors": (reap or {}).get("survivors", []),
            "live_children": await self._live_children(sid),
        }

    async def _capture_tree_identity(
        self, session_name: str, pane_pid: str
    ) -> tuple[list[int], dict[int, dict[str, Any]], dict[int, dict[str, Any]], bool]:
        """Capture the target tree and complete process-instance proofs."""
        try:
            pane = await self.tmux.pane_identity(session_name)
        except (AttributeError, VerbError):
            pane = None
        try:
            records = await prockill.process_records()
        except Exception:  # noqa: BLE001 - close remains bounded
            records = {}
        try:
            root = int(pane_pid)
        except (TypeError, ValueError):
            return [], records, {}, False
        if root <= 0 or root not in records:
            # A missing root means the detailed process inventory itself was
            # incomplete. Do not let a root-only fallback be recorded as a
            # clean reap; descendants may have survived pane teardown.
            return [root] if root > 0 else [], records, {}, False
        children: dict[int, list[int]] = {}
        for pid, record in records.items():
            children.setdefault(int(record["ppid"]), []).append(pid)
        tree: list[int] = []
        queue = [root]
        while queue:
            pid = queue.pop(0)
            if pid in tree:
                continue
            tree.append(pid)
            queue.extend(sorted(children.get(pid, [])))
        if pane is None:
            return tree, records, {}, True
        try:
            boot = await prockill.boot_id()
        except Exception:  # noqa: BLE001 - missing boot identity only loses proof
            boot = ""
        proofs: dict[int, dict[str, Any]] = {}
        for pid in tree:
            proof = prockill.identity_proof(
                host=self.local_host,
                boot=boot,
                record=records[pid],
                tmux_session=str(pane.get("session_name") or session_name),
                tmux_pane=str(pane.get("pane_id") or ""),
                tty=str(pane.get("tty") or ""),
                tmux_socket=str(pane.get("tmux_socket") or ""),
            )
            if proof is not None:
                proofs[pid] = proof
        return tree, records, proofs, True

    @staticmethod
    def _reap_readback(
        pids: list[int], records: dict[int, dict[str, Any]], proofs: dict[int, dict[str, Any]],
        *, inventory_complete: bool = True,
    ) -> dict[str, Any]:
        if not inventory_complete:
            root = pids[0] if pids else -1
            if root <= 0 or not prockill.pid_alive(str(root)):
                return {"reap_status": "unknown", "survivors": []}
            return {
                "reap_status": "unknown",
                "survivors": [{
                    "pid": root,
                    "command": str(records.get(root, {}).get("command") or ""),
                    "first_seen": iso_now(),
                    "reap_reason": "process_observed_alive",
                    "inventory_complete": False,
                }],
            }
        survivors: list[dict[str, Any]] = []
        for pid in pids:
            if not prockill.pid_alive(str(pid)):
                continue
            item: dict[str, Any] = {
                "pid": pid,
                "command": str(records.get(pid, {}).get("command") or ""),
                "first_seen": iso_now(),
            }
            if pid in proofs:
                item["ownership_proof_v2"] = proofs[pid]
            else:
                item["reap_reason"] = "ownership_proof_missing"
            survivors.append(item)
        if not survivors:
            return {"reap_status": "reaped", "survivors": []}
        status = "survivors" if all("ownership_proof_v2" in item for item in survivors) else "unknown"
        return {"reap_status": status, "survivors": survivors}

    async def _terminate_pane(self, session_name: str) -> tuple[str, str, dict[str, Any]]:
        """Run the close escalation ladder against a pane confirmed live.

        Returns `(state, pane_pid)` where state is `ok` / `carcass` / `failed`.
        The pane pid is read up front, while the pane is still alive, so a
        by-pid SIGKILL survives tmux going unresponsive under us.
        """
        pane_pid = await self.tmux.pane_pid(session_name)
        tracked, records, proofs, inventory_complete = await self._capture_tree_identity(
            session_name, pane_pid
        )

        # (1) Graceful: ask tmux to kill the session, confirm it is gone.
        await self.tmux.kill_session(session_name)
        if await self._confirm_pane_gone(session_name, CLOSE_GRACEFUL_CONFIRM_S):
            return "ok", pane_pid, self._reap_readback(
                tracked, records, proofs, inventory_complete=inventory_complete
            )

        # (4) Graceful did not confirm death and there is no pid to signal: tmux
        # is unresponsive and we have nothing to escalate to. Fail honestly.
        if not pane_pid:
            return "failed", pane_pid, {"reap_status": "unknown", "survivors": []}

        # (2) SIGKILL the whole pane process tree DIRECTLY by pid — no tmux
        # dependency, since tmux may be the wedged component — and confirm the
        # root pid is gone by asking the kernel.
        await prockill.signal_tree(pane_pid, signal.SIGKILL)
        if await self._confirm_pid_gone(pane_pid, CLOSE_KILL_CONFIRM_S):
            await self.tmux.kill_session(session_name)  # drop the now-defunct pane
            return "ok", pane_pid, self._reap_readback(
                tracked, records, proofs, inventory_complete=inventory_complete
            )

        # (3) SIGKILL delivered, process still visible = kernel D-state carcass.
        await self.tmux.kill_session(session_name)  # remove the tmux artifacts
        return "carcass", pane_pid, self._reap_readback(
            tracked, records, proofs, inventory_complete=inventory_complete
        )

    async def _confirm_pane_gone(self, session_name: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while await self.tmux.has_session(session_name):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(CLOSE_POLL_INTERVAL_S)
        return True

    async def _confirm_pid_gone(self, pane_pid: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while prockill.pid_alive(pane_pid):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(CLOSE_POLL_INTERVAL_S)
        return True

    async def _close_offline_locked(
        self, row: dict[str, Any], reason: str, generation: str | None,
        attribution: dict[str, Any] | None,
    ) -> dict[str, Any]:
        host, name = row["host"], row["session_name"]
        sid = f"{host}:{name}"
        closed = await self.store.mark_closed(
            host, name, closed_at=iso_now(), pane_status="unknown",
            expected_generation=generation, close_kind="operator_offline_close",
            attribution=attribution, reason=reason or "operator_confirmed_host_offline",
        )
        if closed is None:
            return await self._stale_close_result(sid, await self.store.fetch_session(host, name) or row)
        with self._inventory_context():
            self._pop_inventory_locked(sid)
        if emit := getattr(self._inventory_emitter, "emit_if_changed", None):
            await emit(immediate=True)
        # No confirmed-dead awaiter resolution or session_reap='reaped' write:
        # this transition records intent, not process death.
        self._alert(
            "operator_offline_close", subsystem="sessions",
            bug_ref="offline_host_operator_close_2026_09", stream_id=sid,
            host=host, requested_at=closed["closed_at"],
            closed_by=(attribution or {}).get("closed_by"),
        )
        log.info("operator_offline_close stream=%s generation=%s actor=%s at=%s",
                 sid, generation, (attribution or {}).get("closed_by"), closed["closed_at"])
        return {"already_closed": False, "closed": True, "failed": False,
                "session": closed, "reap_status": "deferred_host_offline", "survivors": []}

    async def surface_offline_close(self, notify: Any, deferred: dict[str, Any]) -> None:
        if notify is None:
            return
        sid = deferred["stream_id"]
        audit = await self.store.latest_close_audit(sid) or {}
        try:
            await notify.create_internal_notification(
                producer="session_close", title="Offline session closed",
                body=f"{audit.get('closed_by') or 'Authorized caller'} closed {sid} on "
                     f"{deferred['host']} at {deferred['requested_at']}. Pane cleanup is deferred "
                     "until the host returns; process death has not been verified.",
                dedup_key=f"operator_offline_close:{sid}:{deferred['generation']}",
            )
        except Exception:  # notification failure cannot undo committed intent
            log.exception("offline close notification failed stream=%s", sid)

    async def reap_deferred(self, deferred: dict[str, Any]) -> None:
        """One bounded attempt on a reachable peer, serialized with open/close."""
        host, name, sid = deferred["host"], deferred["session_name"], deferred["stream_id"]
        async with self._lifecycle_lock(host, name):
            current = await self.store.get_deferred_reap(sid)
            if not current or current["done_at"] or current["exhausted_at"]:
                return
            generation = current["generation"]
            row = await self.store.fetch_session(host, name)
            error = None
            done = False
            try:
                if not row or row["status"] != "closed" or self._row_generation(row) != generation:
                    raise VerbError("generation_changed", "deferred close no longer owns row")
                tmux = self.hosts.tmux_for(host)
                state = await tmux.session_state(name)
                if state == "alive":
                    identity = await tmux.pane_identity(name)
                    if not identity:
                        raise VerbError("pane_identity_unavailable", "cannot lease deferred pane")
                    if current["pane_identity"]:
                        if identity != json.loads(current["pane_identity"]):
                            raise VerbError("pane_identity_changed", "deferred pane lease changed")
                    else:
                        if row.get("pane_pid") and str(identity.get("pane_pid")) != str(row["pane_pid"]):
                            raise VerbError("pane_identity_changed", "pane differs from closed row")
                        await self.store.update_deferred_reap(sid, generation, pane_identity=identity)
                    await tmux.kill_session(name)
                    deadline = time.monotonic() + CLOSE_GRACEFUL_CONFIRM_S
                    while True:
                        state = await tmux.session_state(name)
                        if state != "alive" or time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(CLOSE_POLL_INTERVAL_S)
                done = state == "gone"
                if not done:
                    error = "ssh_unreachable" if state == "unreachable" else "pane_still_alive_after_kill"
            except Exception as exc:  # retry transport/identity failures within the durable cap
                error = exc.code if isinstance(exc, VerbError) else type(exc).__name__
            await self.store.update_deferred_reap(sid, generation, error=error, done=done)
            updated = await self.store.get_deferred_reap(sid)
            log.info("deferred_reap stream=%s generation=%s done=%s attempts=%s error=%s exhausted=%s",
                     sid, generation, done, updated["attempts"], error, updated["exhausted_at"])
            if updated["exhausted_at"]:
                self._alert("deferred_reap_exhausted", subsystem="sessions",
                            bug_ref="offline_host_operator_close_2026_09",
                            stream_id=sid, host=host, attempts=updated["attempts"], last_error=error)

    async def _close_remote(
        self,
        host: str,
        session_name: str,
        reason: str,
        *,
        expected_generation: str | None = None,
        close_kind: str = "session_close",
        requires_idle: bool = False,
        requires_hidden: bool = False,
        operator_override: bool = False,
        operator_confirm: bool = False,
        defer_if_working: bool = False,
        attribution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Close a PEER row over SSH, degrading honestly.

        The rule that made close safe locally — key on CONFIRMED death, never DB
        status — is the exact rule an SSH transport can violate: a dropped
        connection makes a plain `has-session` exit non-zero, which reads as
        "pane gone". So liveness here uses the TRI-STATE `session_state`, which
        tells "gone" (tmux exit 1) apart from "unreachable" (ssh failed), and
        an unreachable normally settles `close.failed` (reason `ssh_unreachable`,
        row LEFT OPEN). Explicit operator confirmation at the initial failed
        host probe instead records closed intent and a durable deferred reap,
        never a confirmed death:

          host unreachable at any step          -> close.failed / ssh_unreachable
          pane confirmed gone (tmux said so)     -> close.ok (mark closed)
          pane still alive after kill window     -> close.failed (row open; the
                  remote arm has no by-pid SIGKILL escalation yet — follow-up)
        """
        sid = f"{host}:{session_name}"
        if not self.hosts.known(host):
            raise VerbError("unsupported_host", f"v2 close does not know host {host}")
        row = await self.store.fetch_session(host, session_name) or self._inv.get(sid)
        if row is not None and str(row.get("status") or "open") != "open":
            deferred = await self.store.get_deferred_reap(sid)
            reap = await self.store.get_session_reap(sid)
            return {
                "already_closed": True,
                "failed": False,
                "session": row,
                "reap_status": "deferred_host_offline" if deferred and not deferred["done_at"] else (reap or {}).get("reap_status", "unknown"),
                "survivors": (reap or {}).get("survivors", []),
                "live_children": await self._live_children(sid),
            }
        if row is not None:
            current_generation = self._row_generation(row)
            if expected_generation is not None and current_generation != expected_generation:
                return await self._stale_close_result(sid, row)
            expected_generation = current_generation
        # Hidden-only fence (peer arm), under the lifecycle lock: fail closed if
        # the authoritative row is no longer hidden. Refuse, never kill.
        if requires_hidden and str((row or {}).get("visibility") or "default") != "hidden":
            return self._visibility_fenced_result(row)
        if (requires_idle or defer_if_working) and not operator_override:
            await self._probe_capture_liveness(host, session_name, row)
            if not self._capture_is_idle(sid, row):
                # Same guard as the local arm: defer a busy seat, fence a reap.
                if defer_if_working:
                    return await self._close_deferred(
                        host, session_name, row, close_kind=close_kind,
                        attribution=attribution,
                    )
                await self.store.record_close_audit(
                    host=host, session_name=session_name, close_kind=close_kind,
                    disposition="fenced", attribution=attribution,
                    reason=f"reap_fenced: capture_liveness="
                           f"{self._capture_liveness(sid, row) or 'unknown'}",
                )
                return self._reap_fenced(host, session_name, row)
        # Fast path: one bounded probe fails an offline host in ~probe_timeout
        # rather than waiting out the per-call tmux timeout. `session_state`
        # below is still the correctness guard for a host that drops mid-close.
        if not await self.hosts.probe_once(host):
            if operator_confirm and row is not None:
                return await self._close_offline_locked(row, reason, expected_generation, attribution)
            return self._close_failed(row, "ssh_unreachable")
        tmux = self.hosts.tmux_for(host)
        identity_reader = getattr(tmux, "pane_identity", None)
        pane_lease: dict[str, Any] | None = None
        confirmed_gone = False
        for attempt in range(REMOTE_CLOSE_RETRY_ATTEMPTS):
            try:
                state = await tmux.session_state(session_name)
                if state == "unreachable":
                    # Treat a mid-close transport flap like a timeout and give
                    # the already-probed peer a small bounded recovery window.
                    raise VerbError("tmux_timeout", "remote tmux became unreachable")
                if state == "gone":
                    confirmed_gone = True
                    break
                if state == "alive":
                    # A retry must not turn a same-name replacement pane into
                    # the target of the stale close. Production Tmux exposes a
                    # pane identity; small test transports without that seam
                    # retain the older tri-state behavior.
                    if callable(identity_reader):
                        try:
                            current_identity = await identity_reader(session_name)
                        except Exception:  # noqa: BLE001 - fail closed on lease read
                            return self._close_failed(row, "pane_identity_unavailable")
                        if not isinstance(current_identity, dict):
                            return self._close_failed(row, "pane_identity_unavailable")
                        if pane_lease is None:
                            pane_lease = dict(current_identity)
                        elif current_identity != pane_lease:
                            return self._close_failed(row, "pane_identity_changed")
                    await tmux.kill_session(session_name)
                    deadline = time.monotonic() + CLOSE_GRACEFUL_CONFIRM_S
                    while True:
                        state = await tmux.session_state(session_name)
                        if state == "gone":  # tmux itself confirmed it — a real death
                            confirmed_gone = True
                            break
                        if state == "unreachable":
                            raise VerbError("tmux_timeout", "remote tmux became unreachable")
                        if time.monotonic() >= deadline:
                            # `close.degraded` left the vocabulary (2026-08-05 ruling):
                            # a kill-failure we cannot yet escalate remotely is an
                            # honest failure with the row LEFT OPEN, never a shrug —
                            # and never a false `close.ok`.
                            self._alert("close_failed", host=host, session_name=session_name,
                                        reason="pane_still_alive_after_kill")
                            return self._close_failed(row, "pane_still_alive_after_kill")
                        await asyncio.sleep(CLOSE_POLL_INTERVAL_S)
                    break
            except VerbError as exc:
                if exc.code not in ("tmux_timeout", "paste_failed"):
                    raise
                if attempt + 1 >= REMOTE_CLOSE_RETRY_ATTEMPTS:
                    return self._close_failed(row, "ssh_unreachable")
                await asyncio.sleep(REMOTE_CLOSE_RETRY_S)
        if not confirmed_gone:
            return self._close_failed(row, "ssh_unreachable")
        if row is None:  # host reachable, pane confirmed gone, never had a row
            raise VerbError("unknown_session", "Unknown session")
        already = str(row.get("status") or "") != "open"
        closed = await self._mark_closed_locked(
            host,
            session_name,
            reason,
            reap_status="unknown",
            survivors=[],
            expected_generation=expected_generation,
            close_kind=close_kind,
            attribution=attribution,
        )
        if closed is None and expected_generation is not None:
            current = await self.store.fetch_session(host, session_name)
            return await self._stale_close_result(sid, current or row)
        session = closed or row or {
            "stream_id": sid, "host": host, "session_name": session_name,
            "status": "closed", "closed_at": iso_now(),
        }
        live_children = await self._live_children(sid)
        reap = await self.store.get_session_reap(sid)
        return {
            "already_closed": already,
            "closed": True,
            "degraded": False,
            "failed": False,
            "session": session,
            "reap_status": (reap or {}).get("reap_status", "reaped"),
            "survivors": (reap or {}).get("survivors", []),
            "live_children": live_children,
        }

    @staticmethod
    def _close_failed(row: dict[str, Any] | None, reason: str) -> dict[str, Any]:
        """A remote close that could not confirm death: the row is left OPEN and
        the reply is `close.failed`, so no unverified death is ever recorded."""
        return {"already_closed": False, "degraded": False, "failed": True,
                "reason": reason, "session": row}
