from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from inventory import InventoryEmitter
from mirror import _extract_live_state, _extract_preview_line, _provider_from_session_name
from session_names import is_agent_session_name, is_surfaceable_session
from working_state import WorkingStateTracker
from v2_runtime import env_number

log = logging.getLogger("chat_streamd_v2.presence")


@dataclass
class PresenceConfig:
    """Observation and preview bounds; `from_env` keeps them plist-tunable."""

    max_rows_per_pass: int = 200
    list_timeout_s: float = 8.0
    preview_max_rows_per_pass: int = 50
    preview_cache_ttl_s: float = 30.0
    preview_cache_max_entries: int = 256
    preview_concurrency: int = 4
    #: Fast working-state refresh: capture just the "hot" streams (a recent
    #: admitted event, or currently working) on a tight cadence so the single
    #: capture owner reflects a turn within seconds instead of one 60 s
    #: reconcile pass. Idle streams stay on the reconcile sweep. Defaults keep
    #: both AC1 bounds: working within ≤ interval+capture (~≤3 s) of a tool
    #: event, idle within ≤ grace+interval (2.1 s) of the reply. Grace > interval
    #: so a single missed spinner frame mid-turn is bridged, not flapped while a
    #: late final spinner capture still clears inside the locked 2 s convergence
    #: window.
    working_refresh_interval_s: float = 1.0
    working_active_ttl_s: float = 30.0
    #: Grace after the last observed spinner before a missed capture flips the
    #: indicator off; smooths a single mid-turn miss without stranding it.
    working_clear_grace_ms: int = 1100

    @classmethod
    def from_env(cls) -> "PresenceConfig":
        return cls(
            max_rows_per_pass=env_number(
                os.environ, "PENTACLE_PRESENCE_MAX_ROWS_PER_PASS", cls.max_rows_per_pass, int,
            ),
            list_timeout_s=env_number(
                os.environ, "PENTACLE_PRESENCE_LIST_TIMEOUT_S", cls.list_timeout_s, float,
            ),
            preview_max_rows_per_pass=env_number(
                os.environ, "PENTACLE_PRESENCE_PREVIEW_MAX_ROWS_PER_PASS",
                cls.preview_max_rows_per_pass, int,
            ),
            preview_cache_ttl_s=env_number(
                os.environ, "PENTACLE_PRESENCE_PREVIEW_CACHE_TTL_S", cls.preview_cache_ttl_s, float,
            ),
            preview_cache_max_entries=env_number(
                os.environ, "PENTACLE_PRESENCE_PREVIEW_CACHE_MAX_ENTRIES",
                cls.preview_cache_max_entries, int,
            ),
            preview_concurrency=env_number(
                os.environ, "PENTACLE_PRESENCE_PREVIEW_CONCURRENCY", cls.preview_concurrency, int,
            ),
            working_refresh_interval_s=env_number(
                os.environ, "PENTACLE_WORKING_REFRESH_INTERVAL_S",
                cls.working_refresh_interval_s, float,
            ),
            working_active_ttl_s=env_number(
                os.environ, "PENTACLE_WORKING_ACTIVE_TTL_S", cls.working_active_ttl_s, float,
            ),
            working_clear_grace_ms=env_number(
                os.environ, "PENTACLE_WORKING_CLEAR_GRACE_MS", cls.working_clear_grace_ms, int,
            ),
        )


@dataclass
class PresenceObservation:
    """One bounded, host-scoped liveness observation."""

    host: str
    state: str
    known: tuple[str, ...] = ()
    alive: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    raw_sessions: tuple[str, ...] = ()

    @property
    def confirms_absence(self) -> bool:
        return self.state == "host_online_tmux_absent"

    @property
    def transport_failed(self) -> bool:
        return self.state == "ssh_unreachable"


@dataclass
class _RemoteCaptureState:
    """Last successfully parsed capture for one open session generation."""

    preview: str = ""
    content_hash: int = 0
    working: bool = False
    working_label: str = ""
    last_working_ms: int = 0
    last_activity: str = ""
    observer_source: str = "reconciler-fallback"
    capture_liveness: str = ""


async def _noop_broadcast(_frame: dict[str, Any]) -> None:
    return None


class RemotePresence:
    def __init__(
        self,
        sessions: Any,
        hosts: Any,
        *,
        config: PresenceConfig | None = None,
        broadcast: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        tracker: WorkingStateTracker | None = None,
        inventory_emitter: InventoryEmitter | None = None,
    ) -> None:
        self.sessions = sessions
        self.hosts = hosts
        self.cfg = config or PresenceConfig()
        self.broadcast = broadcast or _noop_broadcast
        self.inventory_emitter = inventory_emitter or InventoryEmitter(sessions, self.broadcast)
        self.tracker = tracker or WorkingStateTracker()
        #: Rotating start index so the per-pass cap never starves the tail rows.
        self._offset = 0
        #: The last host observations are consumed by the durable reconciler.
        self.last_observations: dict[str, PresenceObservation] = {}
        #: Rows included in the last bounded pass, grouped by host.
        self.last_rows: dict[str, list[dict[str, Any]]] = {}
        #: Successful capture results, keyed by stream id + open generation.
        #: Failed captures do not replace the last good value, preserving an
        #: honest cached preview.
        self._preview_cache: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        self._preview_offset = 0
        #: Parsed live state travels with the same bounded capture lifecycle as
        #: preview; it is never acquired by a second capture subsystem.
        self._capture_state: dict[tuple[str, str], _RemoteCaptureState] = {}
        self._capture_epochs: dict[tuple[str, str], int] = {}
        self._tracker_generation: dict[str, tuple[str, str]] = {}
        self._known_live: set[tuple[str, str]] = set()
        self._last_seen: dict[tuple[str, str], tuple[float, str]] = {}
        self._death_emitted: set[tuple[str, str]] = set()
        self._pending_deaths: list[dict[str, Any]] = []
        self._inventory_dirty = False
        #: Rotating start index for the fast refresh's per-pass cap on hot rows.
        self._refresh_offset = 0
        #: Serializes only the apply phase of the reconcile sweep and the fast
        #: refresh (never held across the SSH/tmux capture), so the two passes
        #: never interleave a mutation of the shared capture caches.
        self._apply_lock = asyncio.Lock()

    def record_admitted_activity(
        self,
        stream_id: str,
        *,
        generation: str,
        now_ms: int | None = None,
    ) -> bool:
        """Advance the live activity cache after a lifecycle-CAS append.

        ``event.push`` is authoritative only after it has inserted an event
        under the matching durable lifecycle predicate.  Presence owns the
        corresponding in-memory overlay, so update its generation-keyed cache
        before the next satellite heartbeat can reapply a stale seed.
        """
        row = self.sessions.get(stream_id) or {}
        key = (stream_id, str(generation or ""))
        if not key[1] or self._preview_key(row) != key:
            return False
        recorded_ms = self._now_ms() if now_ms is None else int(now_ms)
        state = self._capture_state.setdefault(key, _RemoteCaptureState())
        state.last_activity = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(recorded_ms / 1000),
        )
        before = self.sessions.get(stream_id) or row
        if self.sessions.apply_live(stream_id, last_activity=state.last_activity) is None:
            return False
        if before.get("last_activity") != state.last_activity:
            self._inventory_dirty = True
            return True
        return False

    def _host_overlay(
        self,
        host: str,
        *,
        pane_alive: bool,
        status_row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project session visibility from the authoritative host breaker."""
        if status_row is None:
            snapshot = getattr(self.hosts, "snapshot", None)
            status_row = snapshot().get(host, {}) if callable(snapshot) else {}
            if "online" in status_row:
                online = bool(status_row.get("online"))
            else:
                is_online = getattr(self.hosts, "is_online", None)
                # Lightweight test/adapter doubles predate the Hosts seam. Keep
                # their historical pane-evidence behavior; production always
                # has the authoritative callable.
                online = bool(is_online(host)) if callable(is_online) else True
        else:
            online = bool(status_row.get("online"))
        reason = "" if online else str(
            status_row.get("host_status_reason") or "host_offline"
        )
        status = "online" if online else str(
            status_row.get("host_status")
            or ("degraded" if reason.endswith("_after_online") else "offline")
        )
        return {
            "online": bool(online and pane_alive),
            "host_status": status,
            "host_status_reason": reason,
                **({"capture_generation": None} if status != "online" else {}),
        }

    async def host_status_changed(self, payload: dict[str, Any]) -> None:
        """Apply a breaker flip to UI inventory from the same host source."""
        host = str(payload.get("host") or "")
        if not host:
            return
        # Callbacks are scheduled tasks: a stale open notification may run
        # after a successful half-open probe. Re-read the current breaker state
        # rather than letting delayed delivery overwrite newer truth.
        for row in self.sessions.list_open():
            if str(row.get("host") or "") != host:
                continue
            self._apply_overlay(
                str(row.get("stream_id") or ""),
                self._host_overlay(
                    host,
                    pane_alive=row.get("pane_status") == "pane_alive",
                ),
            )
        if self._inventory_dirty:
            self._inventory_dirty = False
            await self.inventory_emitter.emit_if_changed()

    # -- one pass ----------------------------------------------------------

    def select_rows(self) -> list[dict[str, Any]]:
        rows = [
            row for row in self.sessions.list_open()
            if row.get("host") == self.hosts.local_host or row.get("host") in self.hosts.peers
        ]
        return self._bounded(rows)

    def apply_observation(
        self, rows: list[dict[str, Any]], observation: PresenceObservation
    ) -> int:
        """Apply one already-collected live overlay to its exact row batch."""
        current_host = self._host_overlay(observation.host, pane_alive=True)
        if current_host["host_status"] != "online":
            return self._apply_host_status(
                rows,
                str(current_host["host_status"]),
                str(current_host["host_status_reason"]),
            )
        if observation.transport_failed:
            reason = observation.reason or "host_offline"
            if reason not in {"host_offline", "pending_probe", "unreachable_after_online", "circuit_open", "circuit_open_after_online"}:
                return self._invalidate_capture_rows(rows)
            status = "degraded" if reason.endswith("_after_online") else "offline"
            return self._apply_host_status(rows, status, reason)
        if observation.confirms_absence:
            return (self._apply_host_status(rows, "online", "")
                    + self._invalidate_capture_rows(rows))
        if observation.state != "host_online_tmux_present":
            return self._invalidate_capture_rows(rows)
        return self._apply(rows, observation.alive)

    def _apply_host_status(self, rows: list[dict[str, Any]], status: str, reason: str) -> int:
        stamped = 0
        for row in rows:
            sid = str(row.get("stream_id") or "")
            _applied, changed = self._apply_overlay(sid, {
                "online": status == "online", "host_status": status,
                "host_status_reason": reason,
                **({"capture_generation": None} if status != "online" else {}),
            })
            stamped += changed
        return stamped

    def _invalidate_capture_rows(self, rows: list[dict[str, Any]]) -> int:
        changed = 0
        for row in rows:
            sid = str(row.get("stream_id") or "")
            current = self.sessions.get(sid)
            if current is not None and self._preview_key(current) == self._preview_key(row):
                key = self._preview_key(current)
                if (not current.get("capture_generation")
                        and not current.get("local_mirror") and not current.get("mirror_local")
                        and key not in self._capture_state and key not in self._preview_cache):
                    # Revoke in-flight reads without inventing an observation
                    # for a row that has never had capture authority.
                    self._capture_epochs[key] = self._capture_epochs.get(key, 0) + 1
                    continue
                _applied, updated = self._apply_overlay(sid, {
                    "capture_generation": None, "capture_liveness": "transport_unknown",
                })
                changed += updated
        return changed

    def _apply_overlay(self, sid: str, overlay: dict[str, Any]) -> tuple[bool, bool]:
        before = self.sessions.get(sid) or {}
        revoked = (overlay.get("online") is False
                   or overlay.get("pane_status") == "pane_dead"
                   or (overlay.get("online") is True and before.get("online") is False)
                   or (overlay.get("pane_status") == "pane_alive"
                       and before.get("pane_status") == "pane_dead")
                   or ("capture_liveness" in overlay and overlay["capture_liveness"] != "idle")
                   or (overlay.get("pane_pid") and before.get("pane_pid")
                       and str(overlay["pane_pid"]) != str(before["pane_pid"])))
        if before and revoked:
            key = self._preview_key(before)
            self._capture_epochs[key] = self._capture_epochs.get(key, 0) + 1
            self._preview_cache.pop(key, None)
            state = self._capture_state.get(key)
            if state is not None:
                state.capture_liveness = "transport_unknown"
            overlay = {**overlay, "capture_generation": None,
                       "capture_liveness": "transport_unknown"}
        after = self.sessions.apply_live(sid, **overlay)
        if after is None:
            return False, False
        changed = any(before.get(key) != after.get(key) for key in overlay)
        self._inventory_dirty |= changed
        return True, changed

    async def observe_once(
        self, *, by_host: dict[str, list[dict[str, Any]]] | None = None
    ) -> dict[str, PresenceObservation]:
        """Collect one bounded observation per peer."""
        if by_host is None:
            rows = self.select_rows()
            by_host = {}
            for row in rows:
                by_host.setdefault(str(row["host"]), []).append(row)
        observations: dict[str, PresenceObservation] = {}
        for host, hrows in by_host.items():
            known = tuple(str(r.get("session_name") or "") for r in hrows)
            observations[host] = await self._observe_host(host, set(known), known_order=known)
        self.last_rows = by_host
        self.last_observations = observations
        return observations

    def _bounded(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cap = self.cfg.max_rows_per_pass
        if cap <= 0 or len(rows) <= cap:
            return rows
        start = self._offset % len(rows)
        window = (rows + rows)[start:start + cap]
        self._offset = start + cap
        return window

    async def _observe_host(
        self,
        host: str,
        known: set[str],
        *,
        known_order: tuple[str, ...] | None = None,
    ) -> PresenceObservation:
        if not self.hosts.is_online(host):
            snapshot = getattr(self.hosts, "snapshot", None)
            reason = "host_offline" if not callable(snapshot) else str(
                snapshot().get(host, {}).get("host_status_reason") or "host_offline"
            )
            return PresenceObservation(
                host, "ssh_unreachable", known=known_order or tuple(sorted(known)),
                reason=reason,
            )
        tmux = self.hosts.tmux_for(host)
        try:
            rc, out = await tmux.run(
                "list-panes", "-a", "-F", "#{session_name}|#{pane_pid}",
                timeout=self.cfg.list_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - transport = fail closed
            return PresenceObservation(
                host, "ssh_unreachable", known=known_order or tuple(sorted(known)),
                reason=type(exc).__name__,
            )
        if rc == 1:
            # tmux normally uses rc=1 for an absent server, but an SSH wrapper,
            # permission failure, or a broken tmux binary can also relay rc=1.
            # Only tmux's explicit no-server diagnostic is affirmative death
            # evidence; every other rc=1 remains fail-closed.
            lowered = out.lower()
            if any(signature in lowered for signature in (
                "no server running",
                "failed to connect to server",
            )):
                return PresenceObservation(
                    host, "host_online_tmux_absent", known=known_order or tuple(sorted(known)),
                    reason="tmux_server_absent",
                )
            return PresenceObservation(
                host, "ambiguous", known=known_order or tuple(sorted(known)),
                reason="tmux_rc_1_unclassified",
            )
        if rc != 0:
            # ssh's 255 and all other command failures are unconfirmable. Never
            # turn them into a dead-pane decision.
            return PresenceObservation(
                host, "ssh_unreachable", known=known_order or tuple(sorted(known)),
                reason=f"tmux_rc_{rc}",
            )
        raw_lines = [line for line in out.splitlines() if line.strip()]
        alive: dict[str, str] = {}
        raw_sessions: list[str] = []
        for line in raw_lines:
            fields = line.split('|')
            if (len(fields) != 2 or not fields[0].strip()
                    or not fields[1].isascii() or not fields[1].isdigit() or int(fields[1]) <= 0):
                return PresenceObservation(
                    host, "ambiguous", known=known_order or tuple(sorted(known)),
                    reason="invalid_tmux_inventory_record",
                )
            name, pid = fields
            name = name.strip()
            if name and is_agent_session_name(name) and name not in raw_sessions:
                raw_sessions.append(name)
            if name and name not in alive and is_surfaceable_session(name, known):
                alive[name] = pid.strip()
        if not raw_lines or not alive:
            # rc==0 with no usable agent panes is an incomplete/ambiguous
            # inventory. It is intentionally not a death signal.
            return PresenceObservation(
                host, "ambiguous", known=known_order or tuple(sorted(known)),
                reason="empty_tmux_inventory", raw_sessions=tuple(raw_sessions),
            )
        return PresenceObservation(
            host, "host_online_tmux_present", known=known_order or tuple(sorted(known)),
            alive=alive, raw_sessions=tuple(raw_sessions),
        )

    def _apply(self, hrows: list[dict[str, Any]], alive: dict[str, str]) -> int:
        n = 0
        for r in hrows:
            sid = str(r["stream_id"])
            sname = str(r.get("session_name") or "")
            key = self._preview_key(r)
            if sname in alive:
                overlay: dict[str, Any] = {
                    "online": True, "pane_status": "pane_alive",
                    "host_status": "online", "host_status_reason": "",
                }
                if alive[sname]:
                    overlay["pane_pid"] = alive[sname]
                applied, _changed = self._apply_overlay(sid, overlay)
                n += applied
                self._known_live.add(key)
                self._death_emitted.discard(key)
                self._last_seen[key] = (time.time(), alive[sname])
            elif alive:
                # Host reachable AND tmux enumerated real sessions, yet this pane
                # is absent -> observed dead. Observation only: stamp the field,
                # never close the row or kill anything. Guarded by `alive` being
                # non-empty so an empty result can't false-death a whole host.
                prior_live = (
                    key in self._known_live
                    or r.get("pane_status") == "pane_alive"
                    or r.get("online") is True
                )
                applied, _changed = self._apply_overlay(sid, {
                    "online": False, "working": False, "working_label": "",
                    "pane_status": "pane_dead", "host_status": "online",
                    "host_status_reason": "",
                })
                n += applied
                if self._tracker_generation.get(sid) == key:
                    self.tracker.drop_stream(sid)
                    self._tracker_generation.pop(sid, None)
                self._capture_state.pop(key, None)
                if prior_live and key not in self._death_emitted:
                    last_seen_ts, last_seen_pid = self._last_seen.get(
                        key, (time.time(), str(r.get("pane_pid") or ""))
                    )
                    death: dict[str, Any] = {
                        "type": "session.died",
                        "host": str(r.get("host") or ""),
                        "session_name": sname,
                        "stream_id": sid,
                        "reason": "pane_pid_gone",
                        "last_seen_ts": float(last_seen_ts),
                        "offline_since_ts": int(time.time()),
                    }
                    try:
                        if last_seen_pid:
                            death["last_seen_pane_pid"] = int(last_seen_pid)
                    except (TypeError, ValueError):
                        pass
                    self._pending_deaths.append(death)
                    self._death_emitted.add(key)
        return n

    async def capture_previews(self) -> int:
        """Capture one bounded, TTL-cached live overlay for remote rows.

        The one existing L12 capture result feeds preview, the inherited
        ``_extract_live_state`` parser, and ``WorkingStateTracker``. A capture
        failure leaves all last-good overlay fields in place and never changes
        pane truth. Injected test transports may only implement ``run``; those
        simply have no capture capability and retain liveness behavior.

        The apply phases are serialized with the fast working refresh
        (``refresh_active_working``) through ``_apply_lock`` so the two passes
        never interleave a mutation of the shared capture caches. The SSH/tmux
        capture itself is NOT under the lock, so a slow sweep never starves a
        short-turn refresh. Both drive the same single owner
        (``_apply_capture_state``); the refresh only makes it run sooner for
        streams with an in-flight turn.
        """
        # Phase 1 (apply lock): prune, evict, select candidates, serve cache,
        # and snapshot the rows that still need a fresh capture.
        async with self._apply_lock:
            to_capture, applied = await self._prepare_sweep()
        if not to_capture:
            return applied
        # Phase 2 (no lock): the SSH/tmux capture.
        semaphore = asyncio.Semaphore(max(1, int(self.cfg.preview_concurrency)))
        results = await asyncio.gather(
            *(self._capture_pane(host, row, semaphore) for host, row in to_capture)
        )
        captured_at = asyncio.get_running_loop().time()
        # Phase 3 (apply lock): generation/liveness-checked apply of fresh reads.
        async with self._apply_lock:
            applied += await self._apply_results(results, captured_at=captured_at, cache=True)
        return applied

    async def _prepare_sweep(self) -> tuple[list[tuple[str, dict[str, Any]]], int]:
        """Under the apply lock: prune, evict, serve cached previews, and return
        the rows that still need a fresh capture plus the cached-serve count."""
        now = asyncio.get_running_loop().time()
        active_keys = {
            str(row.get("stream_id") or ""): self._preview_key(row)
            for row in self.sessions.list_open()
            if row.get("stream_id")
        }
        self._prune_generation_state(active_keys)
        for key, cached in list(self._preview_cache.items()):
            active_key = active_keys.get(key[0])
            if active_key is None or active_key != key:
                self._preview_cache.pop(key, None)
            elif now - cached[1] >= self.cfg.preview_cache_ttl_s:
                self._preview_cache.pop(key, None)

        candidates: list[tuple[str, dict[str, Any]]] = []
        for host, rows in self.last_rows.items():
            observation = self.last_observations.get(host)
            if observation is None or observation.state != "host_online_tmux_present":
                continue
            for row in rows:
                name = str(row.get("session_name") or "")
                if name in observation.alive:
                    candidates.append((host, row))
        cap = self.cfg.preview_max_rows_per_pass
        if not candidates or cap <= 0:
            await self._flush_broadcasts()
            return [], 0
        if len(candidates) > cap:
            start = self._preview_offset % len(candidates)
            ordered = (candidates + candidates)[start:start + len(candidates)]
            self._preview_offset = start + cap
        else:
            ordered = candidates

        to_capture: list[tuple[str, dict[str, Any]]] = []
        applied = 0
        for host, row in ordered:
            sid = str(row.get("stream_id") or "")
            key = self._preview_key(row)
            cached = self._preview_cache.get(key)
            if cached is not None and now - cached[1] < self.cfg.preview_cache_ttl_s:
                self._preview_cache.move_to_end(key)
                state = self._capture_state.get(key)
                if state is not None:
                    applied += await self._apply_capture_state(row, key, state)
                else:
                    before = self.sessions.get(sid) or {}
                    if self.sessions.apply_live(sid, preview=cached[0]) is not None:
                        applied += 1
                        after = self.sessions.get(sid) or {}
                        if before.get("preview") != after.get("preview"):
                            self._inventory_dirty = True
                continue
            if len(to_capture) < cap:
                to_capture.append((host, row))

        if not to_capture:
            await self._emit_tracker_heartbeats()
            await self._flush_broadcasts()
        return to_capture, applied

    async def _apply_results(
        self,
        results: list[tuple[tuple[str, str], str, str, dict[str, object], str] | None],
        *,
        captured_at: float,
        cache: bool,
    ) -> int:
        """Under the apply lock: cache (sweep only) and apply fresh captures,
        then emit heartbeats and flush. ``active_after`` is recomputed here so a
        close/reopen during the capture cannot land on the successor row."""
        active_after = {
            str(row.get("stream_id") or ""): self._preview_key(row)
            for row in self.sessions.list_open()
            if row.get("stream_id")
        }
        applied = 0
        for result in results:
            if cache:
                applied += self._cache_capture_result(result, active_after, captured_at)
            if result is not None:
                applied += await self._apply_capture_result(result, active_after)
        await self._emit_tracker_heartbeats()
        await self._flush_broadcasts()
        return applied

    async def _capture_pane(
        self, host: str, row: dict[str, Any], semaphore: asyncio.Semaphore,
    ) -> tuple[tuple[str, str], str, str, dict[str, object], str]:
        key = self._preview_key(row)
        epoch = self._capture_epochs.get(key, 0)
        result = await self._capture_pane_read(host, row, semaphore)
        if result is None:
            result = (key, "", "", {}, "transport_unknown")
        result_key, pane, preview, live, liveness = result
        return result_key, pane, preview, {**live, "_capture_epoch": epoch}, liveness

    async def _capture_pane_read(
        self, host: str, row: dict[str, Any], semaphore: asyncio.Semaphore,
    ) -> tuple[tuple[str, str], str, str, dict[str, object], str] | None:
        """Capture one pane and parse its live state — the single working owner's
        one capture implementation, shared by the reconcile sweep and the fast
        refresh. Returns ``None`` when the transport has no capture capability."""
        name = str(row.get("session_name") or "")
        try:
            tmux = self.hosts.tmux_for(host)
            capture_checked = getattr(tmux, "capture_checked", None)
            capture = getattr(tmux, "capture", None)
            if not callable(capture_checked) and not callable(capture):
                return None
            async with semaphore:
                if callable(capture_checked):
                    checked = await asyncio.wait_for(
                        capture_checked(name, timeout=self.cfg.list_timeout_s),
                        timeout=self.cfg.list_timeout_s,
                    )
                    if isinstance(checked, tuple):
                        ok, pane = checked
                    else:
                        ok, pane = True, checked
                else:
                    ok = True
                    pane = await asyncio.wait_for(
                        capture(name), timeout=self.cfg.list_timeout_s
                    )
            if not ok:
                return self._preview_key(row), "", "", {}, "transport_unknown"
            pane_text = str(pane or "")
            if not pane_text.strip():
                # A successful blank is ambiguous. Retry the blank probe
                # once, while a transport failure remains transport truth.
                async with semaphore:
                    if callable(capture_checked):
                        checked = await asyncio.wait_for(
                            capture_checked(name, timeout=self.cfg.list_timeout_s),
                            timeout=self.cfg.list_timeout_s,
                        )
                        if isinstance(checked, tuple):
                            retry_ok, retry_pane = checked
                        else:
                            retry_ok, retry_pane = True, checked
                    else:
                        retry_ok = True
                        retry_pane = await asyncio.wait_for(
                            capture(name), timeout=self.cfg.list_timeout_s
                        )
                if not retry_ok:
                    return self._preview_key(row), "", "", {}, "transport_unknown"
                pane_text = str(retry_pane or "")
                if not pane_text.strip():
                    return self._preview_key(row), "", "", {}, "wedged_unknown"
            preview = _extract_preview_line(pane_text)
            provider = str(
                row.get("provider")
                or _provider_from_session_name(name)
                or "claude"
            )
            return self._preview_key(row), pane_text, preview, _extract_live_state(pane_text, provider), "idle"
        except Exception:  # noqa: BLE001 - preview is cosmetic and cached
            return self._preview_key(row), "", "", {}, "transport_unknown"

    def _cache_capture_result(
        self,
        result: tuple[tuple[str, str], str, str, dict[str, object], str] | None,
        active_after: dict[str, tuple[str, str]],
        captured_at: float,
    ) -> int:
        """TTL-cache a fresh idle capture's preview; evict stale generations.

        Returns 0; callers add the apply count separately. Splitting the cache
        write from the apply keeps ``_apply_capture_result`` reusable by the
        fast refresh, which never caches (it wants every read fresh)."""
        if result is None:
            return 0
        key, _pane, preview, _live, liveness = result
        sid = key[0]
        if (active_after.get(sid) != key
                or _live.get("_capture_epoch", 0) != self._capture_epochs.get(key, 0)):
            self._preview_cache.pop(key, None)
            return 0
        for old_key in list(self._preview_cache):
            if old_key[0] == sid and old_key != key:
                self._preview_cache.pop(old_key, None)
        if liveness == "idle" and self.cfg.preview_cache_max_entries > 0:
            self._preview_cache[key] = (preview, captured_at)
            self._preview_cache.move_to_end(key)
            while len(self._preview_cache) > self.cfg.preview_cache_max_entries:
                self._preview_cache.popitem(last=False)
        return 0

    async def _apply_capture_result(
        self,
        result: tuple[tuple[str, str], str, str, dict[str, object], str],
        active_after: dict[str, tuple[str, str]],
    ) -> int:
        """Apply one fresh capture through the single working owner."""
        key, pane, preview, live, liveness = result
        sid = key[0]
        # The capture awaited SSH/tmux. A close/reopen can replace this stream
        # id while it was in flight; never let the predecessor's pane text land
        # on the successor row.
        if (active_after.get(sid) != key
                or live.get("_capture_epoch", 0) != self._capture_epochs.get(key, 0)):
            return 0
        # A same-generation pane death or host-offline observation can land
        # between this capture's read and its apply (the generation check above
        # only catches a close/reopen). Never let a stale in-flight capture
        # resurrect working on a dead or offline pane; drop the state so the row
        # is not kept hot.
        row_now = self.sessions.get(sid) or {}
        if row_now.get("pane_status") == "pane_dead" or row_now.get("online") is False:
            self._invalidate_capture_rows([row_now])
            existing = self._capture_state.get(key)
            if existing is not None:
                existing.working = False
                existing.working_label = ""
                existing.last_working_ms = 0
            return 0
        state = self._capture_state.setdefault(key, _RemoteCaptureState())
        state.capture_liveness = liveness
        if liveness == "idle":
            return await self._apply_capture_state(
                self.sessions.get(sid) or {}, key, state, pane=pane, preview=preview, live=live,
            )
        state.working = False
        state.working_label = ""
        state.last_working_ms = 0
        return await self._apply_capture_state(self.sessions.get(sid) or {}, key, state)

    def _hot_working_targets(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        """Rows with an in-flight turn: genuine activity within the active window
        (the universal ``genuine_activity_at`` signal, advanced by BOTH local
        ingest and remote event-push for claude and codex), or a capture that
        last read working (so a turn's end transition is not missed). Offline
        hosts and dead panes are skipped so the fast cadence stays cheap."""
        ttl = self.cfg.working_active_ttl_s
        targets: list[tuple[str, dict[str, Any]]] = []
        for row in self.sessions.list_open():
            sid = str(row.get("stream_id") or "")
            if not sid:
                continue
            host = str(row.get("host") or "")
            if not host or not self.hosts.is_online(host):
                continue
            if row.get("pane_status") == "pane_dead" or row.get("online") is False:
                continue
            key = self._preview_key(row)
            state = self._capture_state.get(key)
            recent = False
            ga = row.get("genuine_activity_at")
            if isinstance(ga, (int, float)) and not isinstance(ga, bool):
                recent = (now - float(ga)) <= ttl
            if recent or (state is not None and state.working):
                targets.append((host, row))
        return targets

    async def refresh_active_working(self) -> int:
        """Fast, targeted capture for streams with an in-flight turn.

        This is not a second working derivation: it drives the same single owner
        (``_capture_pane`` -> ``_apply_capture_result`` -> ``_apply_capture_state``)
        the reconcile sweep uses, only sooner and only for the few hot streams,
        bypassing the preview TTL cache so each read is fresh. It lets one turn
        surface within seconds instead of waiting up to one 60 s reconcile pass.

        The SSH/tmux capture runs OUTSIDE ``_apply_lock`` (only target selection
        and the generation/liveness-checked apply are serialized), so a slow
        reconcile sweep cannot starve a short turn's refresh.
        """
        now = time.time()
        # Phase 1 (apply lock): choose the hot targets, bounded and rotated so a
        # burst of concurrent turns cannot starve the tail.
        async with self._apply_lock:
            targets = self._hot_working_targets(now)
        if not targets:
            return 0
        cap = self.cfg.preview_max_rows_per_pass
        if cap > 0 and len(targets) > cap:
            start = self._refresh_offset % len(targets)
            targets = (targets + targets)[start:start + cap]
            self._refresh_offset = start + cap
        # Phase 2 (no lock): the SSH/tmux capture.
        semaphore = asyncio.Semaphore(max(1, int(self.cfg.preview_concurrency)))
        results = await asyncio.gather(*(
            self._capture_pane(host, row, semaphore) for host, row in targets
        ))
        # Phase 3 (apply lock): generation/liveness-checked apply. The fast path
        # never writes the preview TTL cache (it wants every read fresh).
        async with self._apply_lock:
            return await self._apply_results(results, captured_at=now, cache=False)

    async def working_refresh_loop(self) -> None:
        """Run the fast working refresh forever at the configured cadence."""
        interval = max(0.5, float(self.cfg.working_refresh_interval_s))
        while True:
            try:
                await self.refresh_active_working()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a refresh miss must not kill the loop
                log.debug("working refresh failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _apply_capture_state(
        self,
        row: dict[str, Any],
        key: tuple[str, str],
        state: _RemoteCaptureState,
        *,
        pane: str | None = None,
        preview: str | None = None,
        live: dict[str, object] | None = None,
    ) -> int:
        sid = key[0]
        now_ms = self._now_ms()
        if pane is not None and live is not None and preview is not None:
            working = bool(live.get("working"))
            working_label = str(live.get("working_label") or "")
            if working:
                state.last_working_ms = now_ms
            elif state.working and now_ms - state.last_working_ms < self.cfg.working_clear_grace_ms:
                # Hold the last spinner briefly so a single missed frame mid-turn
                # does not flap the indicator off; a real turn end clears once the
                # grace since the last observed spinner elapses. Time-based (not a
                # pass count) so the reconcile sweep cannot strand it for minutes.
                working = True
                working_label = state.working_label
            content_hash = hash(pane)
            if not state.last_activity or state.content_hash != content_hash:
                state.last_activity = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            state.content_hash = content_hash
            state.preview = preview
            state.working = working
            state.working_label = working_label
            state.observer_source = "reconciler-fallback"

        prior_generation = self._tracker_generation.get(sid)
        if prior_generation is not None and prior_generation != key:
            self.tracker.drop_stream(sid)
        self._tracker_generation[sid] = key
        host, _, name = sid.partition(":")
        payload = self.tracker.observe(
            {
                "stream_id": sid,
                "host": host,
                "provider": str(row.get("provider") or _provider_from_session_name(name) or "claude"),
                "session_name": name,
                "kind": "WORKING",
                "raw": {
                    "working": state.working,
                    "working_label": state.working_label,
                    "state": "working" if state.working else "idle",
                },
            },
            now_ms,
        )
        overlay = {
            "capture_generation": key[1] if state.capture_liveness == "idle" else None,
            "preview": state.preview,
            "working": state.working,
            "working_label": state.working_label,
            "last_activity": state.last_activity,
            "observer_source": state.observer_source,
        }
        if state.capture_liveness:
            overlay["capture_liveness"] = state.capture_liveness
        before = self.sessions.get(sid) or row
        if self.sessions.apply_live(sid, **overlay) is None:
            return 0
        after = self.sessions.get(sid) or {}
        if any(before.get(field) != after.get(field) for field in overlay):
            self._inventory_dirty = True
            applied = 1
        else:
            applied = 0
        # Apply the generation-checked capture before this await. A concurrent
        # close/reopen can therefore never make an in-flight broadcast the
        # point at which predecessor text lands on the replacement row.
        if payload is not None:
            await self._emit_working_state(payload, observer_source=state.observer_source)
        return applied

    async def _emit_tracker_heartbeats(self) -> None:
        for payload in self.tracker.heartbeat(self._now_ms()):
            sid = str(payload.get("stream_id") or "")
            source = self._observer_source(sid)
            await self._emit_working_state(payload, observer_source=source)

    async def _emit_working_state(
        self,
        payload: dict[str, Any],
        *,
        observer_source: str | None = None,
    ) -> None:
        frame = {"type": "working.state", **payload}
        if observer_source:
            frame["observer_source"] = observer_source
        await self._safe_broadcast(frame)

    async def _flush_broadcasts(self) -> None:
        pending = self._pending_deaths
        self._pending_deaths = []
        for frame in pending:
            await self._safe_broadcast(frame)
        if self._inventory_dirty:
            self._inventory_dirty = False
            await self.inventory_emitter.emit_if_changed()

    async def _safe_broadcast(self, frame: dict[str, Any]) -> None:
        try:
            result = self.broadcast(frame)
            if hasattr(result, "__await__"):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a disconnected client cannot kill observation
            log.debug("remote presence broadcast failed type=%s", frame.get("type"), exc_info=True)

    def _prune_generation_state(self, active_keys: dict[str, tuple[str, str]]) -> None:
        """Drop state for closed rows and reused stream generations.

        an active generation may keep its last-good parsed overlay while a
        capture is due, but no predecessor generation may survive a reopen.
        """
        keys = (
            set(self._preview_cache)
            | set(self._capture_state)
            | set(self._capture_epochs)
            | self._known_live
            | set(self._last_seen)
            | self._death_emitted
        )
        for key in keys:
            active_key = active_keys.get(key[0])
            if active_key == key:
                continue
            self._preview_cache.pop(key, None)
            self._capture_state.pop(key, None)
            self._capture_epochs.pop(key, None)
            self._known_live.discard(key)
            self._last_seen.pop(key, None)
            self._death_emitted.discard(key)
            if active_key is not None and active_key != key:
                # Do not let a pane preview from a closed generation leak into
                # a reused stream id while the new capture is pending.
                self.sessions.apply_live(key[0], preview="")
            if self._tracker_generation.get(key[0]) == key:
                self.tracker.drop_stream(key[0])
                self._tracker_generation.pop(key[0], None)

    def _observer_source(self, stream_id: str) -> str:
        key = self._tracker_generation.get(stream_id)
        if key is not None:
            state = self._capture_state.get(key)
            if state is not None:
                return state.observer_source
        return "reconciler-fallback"

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _preview_key(row: dict[str, Any]) -> tuple[str, str]:
        sid = str(row.get("stream_id") or "")
        generation = str(row.get("session_generation") or row.get("created_at") or "")
        return sid, generation
