"""The one daemon projection of first-class work lanes for mobile and web.

Pure functions build the ``work_lanes.inventory`` frame from stored lane rows
(``Store.work_lane_rows``) and the in-memory session presence
(``sessions.list_open()``).  ``WorkLanesInventory`` is the signature-deduped
emitter beside ``InventoryEmitter``.  Wire contract:
``docs/work-lanes.md`` and ``pentacle-chat-core/tests/fixtures/work-lanes-inventory.json``.
"""
from __future__ import annotations

import asyncio
import math
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from work_lane_progress import lane_lead_lost, lane_progress
from store_work_lane_episodes import LaneEpisodeSink

LANE_FRAME_CAP = 64
log = logging.getLogger(__name__)
_STATE_ORDER = {"blocked": 0, "active": 1, "paused": 2, "done": 3}


def _iso_now() -> str:
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000):03d}Z"


def _card(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    return raw if isinstance(raw, dict) else {}


def _status_card(raw: Any) -> dict[str, Any]:
    card = _card(raw)
    step = None
    for item in card.get("plan") or []:
        if isinstance(item, dict) and item.get("status") == "in_progress":
            step = item.get("text")
            break
    return {"goal": card.get("goal"), "active_step": step, "update": card.get("update"),
            "eta_at": card.get("eta_at"), "eta_set_at": card.get("eta_set_at"),
            "updated_at": card.get("updated_at")}


def _lead(lane: dict[str, Any], presence: dict[str, Any] | None, now_iso: str) -> dict[str, Any] | None:
    if not lane.get("bound_stream_id"):
        return None
    row = lane.get("_lead_row") or {}
    live = presence or {}
    qualifies = bool(lane.get("_qualifies"))
    card = _status_card(live.get("status_card") if presence is not None else row.get("status_card"))
    eta = card.get("eta_at")
    online = bool(live.get("online")) if presence is not None else False
    return {
        "stream_id": lane["bound_stream_id"], "generation": lane.get("bound_generation"),
        "qualifies": qualifies, "status": row.get("status") or "unknown",
        "visibility": row.get("visibility") or "default",
        # Lane presence is display-only: the lead reads working while it or its
        # descendants are busy (generating or provider-reported background).
        "presence": {"online": online,
                     "working": bool(live.get("busy") if "busy" in live else live.get("working")),
                     "capture_liveness": live.get("capture_liveness") or ("idle" if online else "transport_unknown"),
                     "last_activity": live.get("last_activity")},
        "status_card": card,
        "eta_stale": bool(eta) and (not qualifies or str(eta) < now_iso),
    }


SEAT_CAP = 8
_ROLE_ORDER = {"lead": 0, "nexus": 1, "planner": 2, "worker": 3, "qa": 4}


def _activity(row: dict[str, Any]) -> str:
    """An observation of the seat, never progress, intent or a pause."""
    if row.get("online") is False or row.get("pane_status") == "pane_dead":
        return "offline"
    if row.get("busy") or row.get("working"):
        return "working"
    return "idle" if row.get("online") else "unknown"


def attribute_members(members: list[dict[str, Any]], sessions_by_stream: dict[str, dict[str, Any]],
                      questions_by_spec: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    """D1: each member row gains the open seats assigned to it and its open questions.

    Only the effective work assignment attributes a seat; inherited authority
    tags never do."""
    from store_specs import effective_assignment
    by_spec: dict[str, list[dict[str, Any]]] = {}
    for stream_id, row in sessions_by_stream.items():
        assigned = effective_assignment(row)
        if assigned and str(row.get("status") or "open") == "open":
            by_spec.setdefault(assigned, []).append({
                "stream_id": stream_id, "role": row.get("role"), "host": row.get("host"),
                "visibility": row.get("visibility"), "parent_stream_id": row.get("parent_stream_id"),
                "activity": _activity(row), "last_activity": row.get("last_activity")})
    out = []
    for member in members:
        seats = sorted(by_spec.get(member["spec_id"], []),
                       key=lambda seat: (_ROLE_ORDER.get(str(seat["role"] or ""), 9), seat["stream_id"]))
        question = (questions_by_spec or {}).get(member["spec_id"]) or {}
        out.append({**member, "seats": seats[:SEAT_CAP], "seats_total": len(seats),
                    "open_questions": {"count": int(question.get("count") or 0),
                                       "first_question_id": question.get("first_question_id")}})
    return out


def project_lane(lane: dict[str, Any], presence: dict[str, Any] | None, now_iso: str,
                 *, index_available: bool | None = None, all_members: bool = False,
                 sessions_by_stream: dict[str, dict[str, Any]] | None = None,
                 questions_by_spec: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    stored = lane["work_state"]
    state, reason = stored, lane.get("work_state_reason")
    progress = lane_progress(lane, now_iso=now_iso, index_available=index_available, all_members=all_members)
    if sessions_by_stream is not None:
        progress["members"] = attribute_members(progress["members"], sessions_by_stream, questions_by_spec)
    if lane_lead_lost(lane) and not lane.get("_qualifies"):
        # An unexpected owner loss is a fault the FD repairs, never an intentional pause.
        state, reason = "active", "lead_lost"
    return {
        "lane_id": lane["lane_id"], "title": lane.get("title"), "summary": lane.get("summary") or "",
        "state": state, "state_reason": reason, "blocker": lane.get("blocker"),
        "owner_kind": lane.get("owner_kind"), "version": int(lane["version"]),
        "updated_at": lane.get("updated_at"), "first_admitted_at": lane.get("first_admitted_at"),
        "done_at": lane.get("done_at"),
        "lead": _lead(lane, presence, now_iso),
        "visible_chat": {"stream_id": lane.get("visible_chat_stream_id"),
                         "generation": lane.get("visible_chat_generation"),
                         "kind": lane.get("_chat_kind") or "session",
                         "available": lane.get("_chat_available") or "unavailable"},
        "last_update": lane.get("_last_update"),
        **progress,
    }


def server_order(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """blocked, active, paused (then done); within a group updated_at desc, then lane_id."""
    out = sorted(lanes, key=lambda lane: lane["lane_id"])
    out.sort(key=lambda lane: str(lane.get("updated_at") or ""), reverse=True)
    out.sort(key=lambda lane: _STATE_ORDER.get(lane["state"], 9))
    return out


def project_lanes(rows: list[dict[str, Any]], presence_by_stream: dict[str, dict[str, Any]],
                  now_iso: str | None = None, *, index_available: bool | None = None,
                  questions_by_spec: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    now_iso = now_iso or _iso_now()
    lanes = [project_lane(row, presence_by_stream.get(str(row.get("bound_stream_id") or "")), now_iso,
                          index_available=index_available, sessions_by_stream=presence_by_stream,
                          questions_by_spec=questions_by_spec)
             for row in rows]
    return server_order(lanes)


def build_frame(rows: list[dict[str, Any]], presence_by_stream: dict[str, dict[str, Any]],
                *, now_iso: str | None = None, cap: int = LANE_FRAME_CAP,
                work_index: dict[str, Any] | None = None,
                questions_by_spec: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    lanes = [lane for lane in project_lanes(rows, presence_by_stream, now_iso,
             index_available=bool(work_index["available"]) if work_index is not None else None,
             questions_by_spec=questions_by_spec)
             if lane["state"] != "done"]
    counts = {"open": len(lanes), "active": 0, "paused": 0, "blocked": 0}
    for lane in lanes:
        counts[lane["state"]] += 1
    return {"type": "work_lanes.inventory", "lanes": lanes[:cap], "counts": counts,
            "truncated": len(lanes) > cap, "generated_at": now_iso or _iso_now(),
            "work_index": work_index or {"available": False, "root_configured": False,
                                         "snapshot_at": None, "last_sweep_at": None, "error": "work_index_unavailable"}}


def presence_index(open_sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stream_id") or ""): row for row in open_sessions if isinstance(row, dict)}


def frame_signature(frame: dict[str, Any]) -> str:
    body = {k: v for k, v in frame.items() if k != "generated_at"}
    for lane in body.get("lanes") or []:
        presence = (lane.get("lead") or {}).get("presence")
        if isinstance(presence, dict):
            presence.pop("last_activity", None)
        for member in lane.get("members") or []:
            for seat in member.get("seats") or []:
                seat.pop("last_activity", None)
    return json.dumps(body, sort_keys=True, default=str)


class WorkLanesInventory:
    """Signature-deduped ``work_lanes.inventory`` emitter (sibling of ``InventoryEmitter``)."""

    def __init__(self, store: Any, sessions: Any,
                 broadcast: Callable[[dict[str, Any]], Awaitable[None]], *, specs: Any = None,
                 sweep_interval_s: float | None = None, settle_s: float | None = None,
                 refresh_min_s: float | None = None,
                 episode_sink: LaneEpisodeSink | None = None) -> None:
        self.store = store
        self.sessions = sessions
        self.broadcast = broadcast
        self.specs = specs
        self.episode_sink = episode_sink
        self.sweep_interval_s = float(sweep_interval_s if sweep_interval_s is not None else
                                      os.environ.get("WORK_INDEX_SWEEP_S", "60"))
        self.settle_s = float(settle_s if settle_s is not None else os.environ.get("WORK_INDEX_SETTLE_S", "300"))
        if (not math.isfinite(self.sweep_interval_s) or not math.isfinite(self.settle_s)
                or self.sweep_interval_s <= 0 or self.settle_s < 0):
            raise ValueError("work_index_interval_invalid")
        # Minimum spacing between drain passes. Each pass rereads current state,
        # so waiting coalesces a burst into one pass that sees the latest state.
        self.refresh_min_s = float(refresh_min_s if refresh_min_s is not None else
                                   os.environ.get("WORK_LANES_REFRESH_MIN_S", "2"))
        if not math.isfinite(self.refresh_min_s) or self.refresh_min_s < 0:
            raise ValueError("work_lanes_refresh_interval_invalid")
        # Optional async reader of open operator questions keyed by spec (main wires notify).
        self.open_questions: Callable[[], Awaitable[dict[str, dict[str, Any]]]] | None = None
        self._last_pass = float("-inf")
        self._last_sweep = float("-inf")
        self._index_dirty = specs is not None
        self._last_signature: str | None = None
        self._lock = asyncio.Lock()
        self._dirty = False
        self._task: asyncio.Task[None] | None = None
        self._periodic: asyncio.Task[None] | None = None
        # Standalone emitters without a file index keep their existing refresh
        # behavior. A file-index emitter waits for main's core-ready start.
        self._running = specs is None
        self._epoch = 0
        self._index_lock = asyncio.Lock()
        self._watcher_start: asyncio.Task[None] | None = None

    async def refresh_for_close(self):
        """One caller waits on the existing coalesced drain, bounded by close."""
        self._index_dirty = True
        self.refresh()
        if self._task is not None:
            await asyncio.shield(self._task)

    def refresh(self) -> None:
        """Coalesced, non-blocking: reconcile lead loss then emit if changed.

        Called on every session-inventory recompute and lane write; one task
        drains repeated calls so the session hot path never waits on it.
        """
        self._dirty = True
        if (self._running or self.specs is None) and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while (self._running or self.specs is None) and self._dirty:
            wait = self._last_pass + self.refresh_min_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            self._dirty = False
            self._last_pass = time.monotonic()
            try:
                await self.reconcile_index()
                await self.store.reconcile_work_lane_episodes(self.episode_sink)
                await self.emit_if_changed()
            except Exception:  # noqa: BLE001 - a lane refresh must not break session paths
                log.exception("work lanes refresh failed")

    async def reconcile_index(self, *, force_sweep: bool = False) -> None:
        if self.specs is None:
            return
        async with self._index_lock:
            sweep = force_sweep or time.monotonic() - self._last_sweep >= self.sweep_interval_s
            if not sweep and not self._index_dirty:
                return
            store, specs, epoch = self.store, self.specs, self._epoch
            worker = getattr(store, "_thread", None)
            self._index_dirty = False
            started = time.monotonic()
            try:
                scan = await asyncio.to_thread(specs.scan_work_observations)
                log.info("work index phase=worktree_sweep duration_s=%.6f", time.monotonic() - started)
                # A worker cannot be cancelled once scanning. Its late result
                # belongs only to the Store/lifecycle that requested it.
                if (store is not self.store or specs is not self.specs or epoch != self._epoch
                        or worker is not getattr(store, "_thread", None)):
                    return
                started = time.monotonic()
                await store.reconcile_work_observations(scan, settle_s=self.settle_s, sweep=sweep)
                log.info("work index phase=work_observation_apply duration_s=%.6f", time.monotonic() - started)
            except BaseException:
                self._index_dirty = True
                raise
            if sweep:
                self._last_sweep = time.monotonic()

    def start(self, interval_s: float = 60.0) -> None:
        if self._periodic is not None:
            return
        loop = asyncio.get_running_loop()
        self._running = True
        if self.specs is not None:
            def changed(_ids):
                def mark():
                    if self._running:
                        self._index_dirty = True
                        self.refresh()
                loop.call_soon_threadsafe(mark)
            self.specs.changed_callback = changed
            specs = self.specs
            epoch = self._epoch
            def attach_watcher() -> None:
                try:
                    specs.start()
                finally:
                    # Cleanup stays in the attaching worker: cancelling its
                    # asyncio await cannot abandon a late native watcher.
                    if not self._running or epoch != self._epoch or specs is not self.specs:
                        specs.stop()
            self._watcher_start = asyncio.create_task(asyncio.to_thread(attach_watcher),
                                                      name="work-index-watcher-start")
        async def periodic() -> None:
            while True:
                # The due timestamp is recorded after the scan. Align the
                # interval with that drain so the next tick cannot skip it.
                if self._task is not None:
                    await asyncio.shield(self._task)
                await asyncio.sleep(min(interval_s, self.sweep_interval_s))
                self.refresh()
        if self._periodic is None:
            self._periodic = asyncio.create_task(periodic())
            self.refresh()

    async def stop(self) -> None:
        self._running = False
        self._epoch += 1
        tasks = [task for task in (self._periodic, self._task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.specs is not None:
            await asyncio.to_thread(self.specs.stop)
        # Attachment owns its final stop if it overlapped shutdown; do not
        # cancel its to_thread await and abandon a newly attached watcher.
        if self._watcher_start is not None:
            await asyncio.shield(self._watcher_start)
        self._periodic = self._task = self._watcher_start = None

    async def questions_by_spec(self) -> dict[str, dict[str, Any]] | None:
        if self.open_questions is None:
            return None
        try:
            return await self.open_questions()
        except Exception:  # noqa: BLE001 - question attribution is display-only
            log.exception("work lanes question attribution unavailable")
            return None

    async def current(self) -> dict[str, Any]:
        rows = await self.store.work_lane_rows()
        return build_frame(rows, presence_index(self.sessions.list_open()),
                           work_index=await self.store.work_index_status(),
                           questions_by_spec=await self.questions_by_spec())

    async def list(self, *, include_done: bool = False, limit: int = 200,
                   before_updated_at: str | None = None, before_lane_id: str | None = None,
                   members: bool = False) -> dict[str, Any]:
        """Page by the compound key (updated_at, lane_id) descending, so ties never drop lanes."""
        rows = await self.store.work_lane_rows(include_done=include_done)
        lanes = project_lanes(rows, presence_index(self.sessions.list_open()),
                              questions_by_spec=await self.questions_by_spec())
        key = lambda lane: (str(lane.get("updated_at") or ""), lane["lane_id"])  # noqa: E731
        lanes.sort(key=key, reverse=True)
        if before_updated_at:
            cursor = (before_updated_at, before_lane_id or "\uffff")
            lanes = [lane for lane in lanes if key(lane) < cursor]
        page = lanes[:limit]
        last = page[-1] if page and len(lanes) > limit else None
        return {"type": "work_lanes.list.ok", "include_done": include_done, "lanes": server_order(page),
                "next_before_updated_at": str(last.get("updated_at") or "") if last else None,
                "next_before_lane_id": last["lane_id"] if last else None,
                "work_index": await self.store.work_index_status()}

    async def emit_if_changed(self) -> bool:
        async with self._lock:
            frame = await self.current()
            signature = frame_signature(json.loads(json.dumps(frame, default=str)))
            if signature == self._last_signature:
                return False
            self._last_signature = signature
            await self.broadcast(frame)
            return True
