"""The one daemon projection of first-class work lanes for mobile and web.

Pure functions build the ``work_lanes.inventory`` frame from stored lane rows
(``Store.work_lane_rows``) and the in-memory session presence
(``sessions.list_open()``).  ``WorkLanesInventory`` is the signature-deduped
emitter beside ``InventoryEmitter``.  Wire contract:
``docs/work-lanes.md`` and ``pentacle-chat-core/tests/fixtures/work-lanes-inventory.json``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

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
        "presence": {"online": online, "working": bool(live.get("working")),
                     "capture_liveness": live.get("capture_liveness") or ("idle" if online else "transport_unknown"),
                     "last_activity": live.get("last_activity")},
        "status_card": card,
        "eta_stale": bool(eta) and (not qualifies or str(eta) < now_iso),
    }


def project_lane(lane: dict[str, Any], presence: dict[str, Any] | None, now_iso: str) -> dict[str, Any]:
    stored = lane["work_state"]
    state, reason = stored, lane.get("work_state_reason")
    if stored == "active" and not lane.get("_qualifies"):
        state, reason = "paused", "lead_lost_unreconciled"
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
    }


def server_order(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """blocked, active, paused (then done); within a group updated_at desc, then lane_id."""
    out = sorted(lanes, key=lambda lane: lane["lane_id"])
    out.sort(key=lambda lane: str(lane.get("updated_at") or ""), reverse=True)
    out.sort(key=lambda lane: _STATE_ORDER.get(lane["state"], 9))
    return out


def project_lanes(rows: list[dict[str, Any]], presence_by_stream: dict[str, dict[str, Any]],
                  now_iso: str | None = None) -> list[dict[str, Any]]:
    now_iso = now_iso or _iso_now()
    lanes = [project_lane(row, presence_by_stream.get(str(row.get("bound_stream_id") or "")), now_iso)
             for row in rows]
    return server_order(lanes)


def build_frame(rows: list[dict[str, Any]], presence_by_stream: dict[str, dict[str, Any]],
                *, now_iso: str | None = None, cap: int = LANE_FRAME_CAP) -> dict[str, Any]:
    lanes = [lane for lane in project_lanes(rows, presence_by_stream, now_iso) if lane["state"] != "done"]
    counts = {"open": len(lanes), "active": 0, "paused": 0, "blocked": 0}
    for lane in lanes:
        counts[lane["state"]] += 1
    return {"type": "work_lanes.inventory", "lanes": lanes[:cap], "counts": counts,
            "truncated": len(lanes) > cap, "generated_at": now_iso or _iso_now()}


def presence_index(open_sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stream_id") or ""): row for row in open_sessions if isinstance(row, dict)}


def frame_signature(frame: dict[str, Any]) -> str:
    body = {k: v for k, v in frame.items() if k != "generated_at"}
    for lane in body.get("lanes") or []:
        presence = (lane.get("lead") or {}).get("presence")
        if isinstance(presence, dict):
            presence.pop("last_activity", None)
    return json.dumps(body, sort_keys=True, default=str)


class WorkLanesInventory:
    """Signature-deduped ``work_lanes.inventory`` emitter (sibling of ``InventoryEmitter``)."""

    def __init__(self, store: Any, sessions: Any,
                 broadcast: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.store = store
        self.sessions = sessions
        self.broadcast = broadcast
        self._last_signature: str | None = None
        self._lock = asyncio.Lock()
        self._dirty = False
        self._task: asyncio.Task[None] | None = None
        self._periodic: asyncio.Task[None] | None = None

    def refresh(self) -> None:
        """Coalesced, non-blocking: reconcile lead loss then emit if changed.

        Called on every session-inventory recompute and lane write; one task
        drains repeated calls so the session hot path never waits on it.
        """
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        while self._dirty:
            self._dirty = False
            try:
                await self.store.reconcile_work_lanes()
                await self.emit_if_changed()
            except Exception:  # noqa: BLE001 - a lane refresh must not break session paths
                log.exception("work lanes refresh failed")

    def start(self, interval_s: float = 60.0) -> None:
        async def loop() -> None:
            while True:
                await asyncio.sleep(interval_s)
                self.refresh()
        if self._periodic is None:
            self._periodic = asyncio.create_task(loop())

    async def stop(self) -> None:
        for task in (self._periodic, self._task):
            if task is not None and not task.done():
                task.cancel()

    async def current(self) -> dict[str, Any]:
        rows = await self.store.work_lane_rows()
        return build_frame(rows, presence_index(self.sessions.list_open()))

    async def list(self, *, include_done: bool = False, limit: int = 200,
                   before_updated_at: str | None = None, before_lane_id: str | None = None) -> dict[str, Any]:
        """Page by the compound key (updated_at, lane_id) descending, so ties never drop lanes."""
        rows = await self.store.work_lane_rows(include_done=include_done)
        lanes = project_lanes(rows, presence_index(self.sessions.list_open()))
        key = lambda lane: (str(lane.get("updated_at") or ""), lane["lane_id"])  # noqa: E731
        lanes.sort(key=key, reverse=True)
        if before_updated_at:
            cursor = (before_updated_at, before_lane_id or "\uffff")
            lanes = [lane for lane in lanes if key(lane) < cursor]
        page = lanes[:limit]
        last = page[-1] if page and len(lanes) > limit else None
        return {"type": "work_lanes.list.ok", "include_done": include_done, "lanes": server_order(page),
                "next_before_updated_at": str(last.get("updated_at") or "") if last else None,
                "next_before_lane_id": last["lane_id"] if last else None}

    async def emit_if_changed(self) -> bool:
        async with self._lock:
            frame = await self.current()
            signature = frame_signature(json.loads(json.dumps(frame, default=str)))
            if signature == self._last_signature:
                return False
            self._last_signature = signature
            await self.broadcast(frame)
            return True
