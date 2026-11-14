"""The one daemon projection of first-class work lanes for mobile and web.

Pure functions build the ``work_lanes.inventory`` frame from stored lane rows
(``Store.work_lane_rows``) and the in-memory session presence
(``sessions.list_open()``).  ``WorkLanesInventory`` is the signature-deduped
emitter beside ``InventoryEmitter``.  Wire contract:
``docs/web_work_lanes.md`` and ``pentacle-chat-core/tests/fixtures/work-lanes-inventory.json``.
"""
from __future__ import annotations

import asyncio
import math
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from work_lane_progress import lane_state, lane_progress, seat_live, seat_working, working_spec_ids
from store_work_lane_episodes import LaneEpisodeSink
from _shared.lane_icons import resolve_icon
from _shared.lane_hues import resolve_hue
from _shared.summary_prose import field_line
from work_lane_worked import WorkedCache
from _shared.work_goals import project_goals
from _shared.checkpoint_policy import detail_field_keys, policy_revision


def detail_member_keys():
    return detail_field_keys() | {"blocked_line", "checkpoint_state", "checkpoint_deficiencies", "checkpoint_revision",
        "checkpoint_policy_revision", "checkpoint_changed_at", "checkpoint_first_observed_at",
        "goal_text", "goal_text_truncated", "goal_source",
        "progress_at", "summary_line", "estimate_first", "unblocks", "recent"}

def _line(member, field_id, raw_key):
    """Whole valid label or null; a duplicate label is never valid, though raw text keeps the first."""
    if any(d.get("code") == field_id + "_duplicate" for d in member.get("checkpoint_deficiencies") or []):
        return None
    return field_line(field_id, member.get(raw_key))


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
        if isinstance(item, dict) and item.get("status") in {"active", "in_progress"}:
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
    if live.get("session_generation") and live["session_generation"] != lane.get("bound_generation"):
        live = {}
    qualifies = bool(lane.get("_qualifies"))
    card = _status_card(live.get("status_card") if presence is not None else row.get("status_card"))
    eta = card.get("eta_at")
    online = bool(live.get("online")) if presence is not None else False
    return {
        "stream_id": lane["bound_stream_id"], "generation": lane.get("bound_generation"),
        "qualifies": qualifies, "status": row.get("status") or "unknown",
        "visibility": row.get("visibility") or "default",
        # Existing client team-busy field; identity/online/card remain the lead's.
        "presence": {"online": online,
                     "working": bool(lane.get("_members_working") or seat_working(live)),
                     "capture_liveness": live.get("capture_liveness") or ("idle" if online else "transport_unknown"),
                     "last_activity": live.get("last_activity")},
        "status_card": card,
        "eta_stale": bool(eta) and (not qualifies or str(eta) < now_iso),
    }


SEAT_CAP = 8
_ROLE_ORDER = {"lead": 0, "nexus": 1, "planner": 2, "worker": 3, "qa": 4}


def _activity(row: dict[str, Any]) -> str:
    """An observation of the seat, never progress, intent or a pause."""
    if not seat_live(row):
        return "offline"
    if seat_working(row):
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
        lead = next((seat for seat in seats if seat["role"] in {"lead", "nexus"}), None)
        lead_seat = None
        if lead:
            row = sessions_by_stream[lead["stream_id"]]
            card = _status_card(row.get("status_card"))
            step = card["active_step"]
            if isinstance(step, str) and len(step) > 120:
                step = step[:119] + "…"
            lead_seat = {key: lead[key] for key in ("stream_id", "host", "activity")}
            lead_seat.update(provider=row.get("provider"), eta_at=card["eta_at"], active_step=step)
        out.append({**member, "seats": seats[:SEAT_CAP], "seats_total": len(seats),
                    "lead_seat": lead_seat,
                    "open_questions": {"count": int(question.get("count") or 0),
                                       "first_question_id": question.get("first_question_id")}})
    return out


def project_lane(lane: dict[str, Any], presence: dict[str, Any] | None, now_iso: str,
                 *, index_available: bool | None = None, all_members: bool = False,
                 sessions_by_stream: dict[str, dict[str, Any]] | None = None,
                 questions_by_spec: dict[str, dict[str, Any]] | None = None,
                 worked_by_spec: dict[str, dict[str, Any] | None] | None = None) -> dict[str, Any]:
    lane = dict(lane)
    if index_available is not None:
        lane["_work_index_available"] = index_available
    if presence is not None:
        lane["_qualifies"] = bool(lane.get("_qualifies") and seat_live(presence)
                                   and (not presence.get("session_generation")
                                        or presence["session_generation"] == lane.get("bound_generation")))
    # Presence supplies activity only; it never grants ownership or activation.
    lane["_working_specs"] = working_spec_ids(lane, list((sessions_by_stream or {}).values()))
    lane["_members_working"] = bool(lane["_working_specs"])
    state, reason = lane_state(lane)
    progress = lane_progress(lane, now_iso=now_iso, index_available=index_available, all_members=all_members)
    detail_keys = detail_member_keys()
    if sessions_by_stream is not None:
        # Preserve the installed client's needs-you gate even when every
        # question-bearing member falls beyond its eight-row inventory preview.
        if not all_members and not any((questions_by_spec or {}).get(m["spec_id"], {}).get("count")
                                       for m in progress["members"]):
            question_member = next((m for m in lane.get("_members") or []
                                    if (questions_by_spec or {}).get(m["spec_id"], {}).get("count")), None)
            if question_member is not None and progress["members"]:
                progress["members"] = [*progress["members"][:-1], question_member]
        progress["members"] = attribute_members(progress["members"], sessions_by_stream, questions_by_spec)
    progress["members"] = [{**{k: v for k, v in member.items()
                               if not k.startswith("_") and (all_members or k not in detail_keys | {"criteria", "transitions"})},
                             "worked": (worked_by_spec or {}).get(member["spec_id"]),
                             "lead_seat": member.get("lead_seat"),
                             "status_line": _line(member, "status", "status_text"),
                             "next_line": _line(member, "next_action", "next_action_text"),
                             **({"blocked_line": _line(member, "blockers", "blockers_text"),
                                 "progress_at": member.get("_progress_at"),
                                 "criteria": member.get("criteria", []),
                                 "transitions": member.get("transitions", [])} if all_members else {})}
                            for member in progress["members"]]
    if all_members:
        for member in progress["members"]:
            for key in detail_keys:
                member.setdefault(key, [] if key in {"checkpoint_deficiencies", "unblocks", "recent"} else None)
            if not lane.get("_work_index_available") or member.get("observation", {}).get("quality") != "fresh":
                member["checkpoint_state"] = "unverifiable"
            member["checkpoint_policy_revision"] = member.get("checkpoint_policy_revision") or policy_revision()
    return {
        "lane_id": lane["lane_id"], "title": lane.get("title"), "summary": lane.get("summary") or "",
        "icon": resolve_icon(lane.get("icon"), lane.get("title")),
        "hue": resolve_hue(lane.get("hue")),
        "state": state, "state_reason": reason, "blocker": lane.get("blocker"),
        **({"parked": {"at": lane["parked_at"][:19] + "Z", "reason": lane["parked_reason"]}}
           if state == "paused" and lane.get("parked_at") else {}),
        "owner_kind": lane.get("owner_kind"), "version": int(lane["version"]),
        "updated_at": lane.get("updated_at"), "first_admitted_at": lane.get("first_admitted_at"),
        "done_at": lane.get("done_at"),
        "lead": _lead(lane, presence, now_iso),
        "visible_chat": {"stream_id": lane.get("visible_chat_stream_id"),
                         "generation": lane.get("visible_chat_generation"),
                         "kind": lane.get("_chat_kind") or "session",
                         "available": lane.get("_chat_available") or "unavailable"},
        "last_update": lane.get("_last_update"),
        "progress_at": max((m["_progress_at"] for m in lane.get("_members") or [] if m.get("_progress_at")), default=None),
        "state_at": lane.get("_state_at"),
        **progress, **project_goals(lane, all_goals=all_members),
        **({"visibility_counts": visibility_counts(lane.get("_members") or [], sessions_by_stream, questions_by_spec,
                                                  source_available=lane.get("_work_index_available", False))}
           if all_members else {}),
    }


def visibility_counts(members, sessions, questions, *, source_available):
    """Distinct item counts over the complete snapshot; unknowns are explicit."""
    from store_specs import effective_assignment
    ids = {member["spec_id"] for member in members}
    complete = bool(source_available and all(member.get("observation", {}).get("quality") == "fresh"
                                            for member in members))
    working = {effective_assignment(row) for row in (sessions or {}).values() if seat_working(row, descendants=False)} & ids
    return {"unfinished": sum(m.get("terminal") not in {"completed", "deprecated"} for m in members) if complete else None,
            "working": len(working) if sessions is not None else None,
            "blocked": sum(m.get("status") == "blocked" for m in members) if complete else None,
            "needs_answer": sum(bool((questions or {}).get(identity, {}).get("count")) for identity in ids) if questions is not None else None,
            "checkpoint_deficient": sum(m.get("checkpoint_state", "unverifiable") != "valid" for m in members) if complete else None,
            "complete": {"source": complete, "presence": sessions is not None, "questions": questions is not None}}


def page_lane_detail(shown: dict, projected: dict, *, members_limit=32,
                     members_after=None, goal_filter=None, sessions_by_stream=None, questions_by_spec=None) -> dict:
    """A live cursor page; aggregate/goals/revision inputs were computed uncapped."""
    from _shared.specs_parser import WORK_SPEC_ID_RE
    if type(members_limit) is not int or not 1 <= members_limit <= 32:
        raise ValueError("work_lane_members_limit_invalid")
    if members_after is not None and (not isinstance(members_after, str)
                                     or not WORK_SPEC_ID_RE.fullmatch(members_after)):
        raise ValueError("work_lane_members_cursor_invalid")
    if goal_filter is not None and (not isinstance(goal_filter, str)
                                   or goal_filter not in {goal["goal_id"] for goal in projected.get("goals", [])}):
        raise ValueError("work_lane_goal_filter_invalid")
    selected = sorted((member for member in projected["members"]
                       if goal_filter is None or member.get("goal_id") == goal_filter),
                      key=lambda member: member["spec_id"])
    total = len(selected)
    remaining = [member for member in selected if members_after is None or member["spec_id"] > members_after]
    page = remaining[:members_limit]
    cursor = page[-1]["spec_id"] if page and len(remaining) > len(page) else None
    from work_lane_progress import progress_trail
    trail_events, unblocks = shown.get("_trail_events") or [], shown.get("_unblocks") or {}
    page = [{**member, "unblocks": unblocks.get(member["spec_id"], [])[:4],
             "recent": progress_trail(trail_events, {member["spec_id"]})} for member in page]
    # Keep the legacy projection page, but serialize new source prose/metadata
    # only in the canonical top-level detail members, never twice per response.
    legacy_page = [{key: value for key, value in member.items() if key not in detail_member_keys()} for member in page]
    projection = {**{key: value for key, value in projected.items() if not key.startswith("_")}, "members": legacy_page, "members_total": total,
                  "members_next": cursor,
                  "recent": progress_trail(trail_events, {member["spec_id"] for member in projected["members"]})}
    if goal_filter is not None:
        # Reuse the full membership census, not this cursor page. Goal prose is
        # included once for the selected goal, never in goal previews.
        members = [member for member in projected["members"] if member.get("goal_id") == goal_filter]
        # Full activity is captured before seat caps in project_lane.
        goal_counts = visibility_counts(members, sessions_by_stream, questions_by_spec,
                                       source_available=shown.get("_work_index", {}).get("available", False))
        selected = next(goal for goal in projected["goals"] if goal["goal_id"] == goal_filter)
        from _shared.work_goals import goal_id
        record = next((r for identity, r in (shown.get("_projection_row", {}).get("_goals") or {}).items()
                       if goal_id(identity) == goal_filter), {})
        detail = record.get("facts", {}).get("_detail") or {}
        projection["selected_goal"] = {**selected, "visibility_counts": goal_counts, **detail,
                                       "summary_line": detail.get("summary_line"),
                                       "recent": progress_trail(trail_events, {member["spec_id"] for member in members}),
                                       "source": {"epic_id": record.get("facts", {}).get("epic_id"),
                                                  "revision": record.get("source_hash")}}
    lane = {key: value for key, value in shown["lane"].items()
            if not key.startswith("_") and key not in {"members", "members_json", "hue_open_order"}}
    lane["members"] = [member["spec_id"] for member in page]
    response = {"type": "work_lanes.show.ok", "lane": lane, "projection": projection,
                "events": shown["events"], "updates": shown["updates"], "members": page,
                "members_total": total, "members_next": cursor,
                "detail_revision": projected.get("detail_revision"),
                "work_index": shown["_work_index"]}
    # Use the actual normal response encoding's conservative ASCII size too:
    # either encoding must fit the transport ceiling, never truncate goals.
    if len(json.dumps(response).encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("work_lane_show_too_large")
    return response


def server_order(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """blocked, active, paused (then done); within a group updated_at desc, then lane_id."""
    out = sorted(lanes, key=lambda lane: lane["lane_id"])
    out.sort(key=lambda lane: str(lane.get("updated_at") or ""), reverse=True)
    out.sort(key=lambda lane: _STATE_ORDER.get(lane["state"], 9))
    return out


def project_lanes(rows: list[dict[str, Any]], presence_by_stream: dict[str, dict[str, Any]],
                  now_iso: str | None = None, *, index_available: bool | None = None,
                  questions_by_spec: dict[str, dict[str, Any]] | None = None,
                  worked_by_spec: dict[str, dict[str, Any] | None] | None = None) -> list[dict[str, Any]]:
    now_iso = now_iso or _iso_now()
    lanes = [project_lane(row, presence_by_stream.get(str(row.get("bound_stream_id") or "")), now_iso,
                          index_available=index_available, sessions_by_stream=presence_by_stream,
                          questions_by_spec=questions_by_spec, worked_by_spec=worked_by_spec)
             for row in rows]
    return server_order(lanes)


def build_frame(rows: list[dict[str, Any]], presence_by_stream: dict[str, dict[str, Any]],
                *, now_iso: str | None = None, cap: int = LANE_FRAME_CAP,
                work_index: dict[str, Any] | None = None,
                questions_by_spec: dict[str, dict[str, Any]] | None = None,
                worked_by_spec: dict[str, dict[str, Any] | None] | None = None) -> dict[str, Any]:
    lanes = [lane for lane in project_lanes(rows, presence_by_stream, now_iso,
             index_available=bool(work_index["available"]) if work_index is not None else None,
             questions_by_spec=questions_by_spec, worked_by_spec=worked_by_spec)
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
        path = getattr(store, "path", None)
        root = getattr(specs, "memory_root", None)
        self.worked = WorkedCache(Path(path).parent if path and path != ":memory:" else None,
                                  Path(root) / "work" if root else None, changed=self.refresh)

    async def refresh_for_close(self):
        """One caller waits on the existing coalesced drain, bounded by close."""
        self._index_dirty = True
        self.refresh()
        if self._task is not None:
            await asyncio.shield(self._task)
        # The ordinary drain isolates errors from inventory consumers. A close
        # caller needs positive refresh evidence before using a cached status.
        if self.specs is not None and self._index_dirty:
            raise RuntimeError("work_index_refresh_pending")

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
                if self.worked.needs_refresh():
                    rows = await self.store.work_lane_rows(include_done=True)
                    self.worked.refresh({member["spec_id"] for row in rows for member in row.get("_members", [])})
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
        await self.worked.stop()
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

    async def _source_snapshot(self, *, include_done=False, select_rows=None):
        snapshot = getattr(self.store, "work_lane_snapshot", None)
        if snapshot is not None:
            return await snapshot(include_done=include_done, select_rows=select_rows)
        # Compatibility for consumers implementing the older Store protocol.
        return await self.store.work_lane_rows(include_done=include_done), await self.store.work_index_status()

    async def current(self) -> dict[str, Any]:
        def select_rows(rows):
            # Match build_frame's state/order/cap before any lazy hue write.
            ordered = server_order([{**row, "state": lane_state(row)[0]} for row in rows])
            return [row for row in ordered if row["state"] != "done"][:LANE_FRAME_CAP]
        rows, work_index = await self._source_snapshot(select_rows=select_rows)
        return build_frame(rows, presence_index(self.sessions.list_open()),
                           work_index=work_index,
                           worked_by_spec=self.worked.values,
                           questions_by_spec=await self.questions_by_spec())

    async def list(self, *, include_done: bool = False, limit: int = 200,
                   before_updated_at: str | None = None, before_lane_id: str | None = None,
                   members: bool = False) -> dict[str, Any]:
        """Page by the compound key (updated_at, lane_id) descending, so ties never drop lanes."""
        key = lambda lane: (str(lane.get("updated_at") or ""), lane["lane_id"])  # noqa: E731
        def select_rows(rows):
            ordered = sorted(rows, key=key, reverse=True)
            if before_updated_at:
                cursor = (before_updated_at, before_lane_id or "\uffff")
                ordered = [row for row in ordered if key(row) < cursor]
            return ordered[:limit]
        rows, work_index = await self._source_snapshot(include_done=include_done, select_rows=select_rows)
        lanes = project_lanes(rows, presence_index(self.sessions.list_open()),
                              worked_by_spec=self.worked.values,
                              questions_by_spec=await self.questions_by_spec())
        page = select_rows(lanes)
        # One lookahead determines continuation without allocating its hue.
        more = sorted(lanes, key=key, reverse=True)
        if before_updated_at:
            cursor = (before_updated_at, before_lane_id or "\uffff")
            more = [lane for lane in more if key(lane) < cursor]
        last = page[-1] if page and len(more) > limit else None
        return {"type": "work_lanes.list.ok", "include_done": include_done, "lanes": server_order(page),
                "next_before_updated_at": str(last.get("updated_at") or "") if last else None,
                "next_before_lane_id": last["lane_id"] if last else None,
                "work_index": work_index}

    async def emit_if_changed(self) -> bool:
        async with self._lock:
            frame = await self.current()
            signature = frame_signature(json.loads(json.dumps(frame, default=str)))
            if signature == self._last_signature:
                return False
            self._last_signature = signature
            await self.broadcast(frame)
            return True
