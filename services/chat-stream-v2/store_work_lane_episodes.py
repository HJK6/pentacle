"""Lane-owned R6 facts; durable openings handed to an idempotent async sink."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from typing import Literal, Protocol

from work_lane_progress import lane_progress

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS v2_work_lane_episodes (
    episode_id TEXT PRIMARY KEY,
    lane_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('completed','stale')),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    opened_at TEXT NOT NULL,
    cleared_at TEXT,
    emitted_ref TEXT,
    fact_json TEXT NOT NULL,
    UNIQUE(lane_id,kind,sequence)
)
"""


def ensure_work_lane_episodes_schema(conn) -> None:
    conn.execute(DDL)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_v2_work_lane_episodes_open "
                 "ON v2_work_lane_episodes(lane_id,kind) WHERE cleared_at IS NULL")


@dataclass(frozen=True)
class LaneEpisodeFact:
    lane_id: str
    kind: Literal["completed", "stale"]
    episode_id: str
    opened_at: str
    family: str = "work_lane.v1"
    principal: str = "daemon:work-lanes"
    condition: str = "active"

    @property
    def code(self) -> str:
        return "lane_" + self.kind


class LaneEpisodeSink(Protocol):
    async def emit(self, fact: LaneEpisodeFact) -> str | None:
        """Commit idempotently by family/principal/episode_id; None means unavailable.

        The payload and producer identity survive clear, restart and FD handoff.
        This method runs outside every lane Store transaction, so the sink may
        itself use the same Store. An uncertain outcome must remain replayable.
        """
        ...


def lead_reported_done_conn(conn, lane: dict) -> bool | None:
    """Only persisted, resolved routing evidence can establish a report fact."""
    # Same predicate as store_routing._assistant_route_lane_conn (route_json
    # names the lane, or a lane.admit receipt binds its dispatch), evaluated in
    # one query instead of decoding every resolved route of the stream per lane.
    route_dispatch_ids = {r["dispatch_id"] for r in conn.execute(
        "SELECT r.dispatch_id FROM v2_assistant_composite_routes AS r "
        "WHERE r.stream_id=? AND r.routing_state='resolved' AND ("
        "json_extract(COALESCE(NULLIF(r.route_json,''),'{}'),'$.lane_id')=? "
        "OR EXISTS (SELECT 1 FROM v2_assistant_composite_operations AS o "
        "WHERE o.stream_id=r.stream_id AND o.dispatch_id=r.dispatch_id "
        "AND o.lane_id=? AND o.operation='lane.admit'))",
        (lane["stream_id"], lane["lane_id"], lane["lane_id"]))}
    if not route_dispatch_ids:
        return None
    prior = lane.get("lead_reported_done")
    prior = None if prior is None else bool(prior)
    pointer = lane.get("completion_report_id")
    if not pointer:
        return False
    report = conn.execute("SELECT * FROM v2_assistant_composite_terminal_reports "
                          "WHERE report_id=? AND stream_id=? AND lane_id=?",
                          (pointer, lane["stream_id"], lane["lane_id"])).fetchone()
    lead = lane.get("_lead_row")
    if (report is not None and report["dispatch_id"] in route_dispatch_ids
            and (report["actor_stream_id"], report["actor_generation"]) ==
                (lane.get("bound_stream_id"), lane.get("bound_generation"))
            and lead is not None and lead.get("session_generation") == lane.get("bound_generation")):
        return True
    return prior


class WorkLaneEpisodesStoreMixin:
    async def reconcile_work_lane_episodes(self, sink: LaneEpisodeSink | None,
                                           *, now_iso: str | None = None) -> None:
        from store_work_lanes import _now, work_lane_rows_conn
        stamp = now_iso or _now()

        def evaluate(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Re-read membership, observations, lead and routing in this writer
                # transaction; never apply a projection captured before set_members.
                for lane in work_lane_rows_conn(conn, include_done=True):
                    conn.execute("UPDATE v2_assistant_composite_lanes SET lead_reported_done=? WHERE lane_id=?",
                                 (lane["_lead_reported_done"], lane["lane_id"]))
                    facts = lane_progress(lane, now_iso=stamp)
                    for kind, condition in (("completed", facts["completion_pending"]), ("stale", facts["stale"])):
                        current = conn.execute("SELECT * FROM v2_work_lane_episodes "
                                               "WHERE lane_id=? AND kind=? AND cleared_at IS NULL",
                                               (lane["lane_id"], kind)).fetchone()
                        if condition is False and current is not None:
                            conn.execute("UPDATE v2_work_lane_episodes SET cleared_at=? WHERE episode_id=?",
                                         (stamp, current["episode_id"]))
                        elif condition is True and current is None:
                            sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM v2_work_lane_episodes "
                                                    "WHERE lane_id=? AND kind=?", (lane["lane_id"], kind)).fetchone()[0]
                            episode_id = f"{lane['lane_id']}:{kind}:{sequence}"
                            fact = LaneEpisodeFact(lane["lane_id"], kind, episode_id, stamp)
                            conn.execute("INSERT INTO v2_work_lane_episodes "
                                         "(episode_id,lane_id,kind,sequence,opened_at,emitted_ref,fact_json) "
                                         "VALUES (?,?,?,?,?,NULL,?)",
                                         (episode_id, lane["lane_id"], kind, sequence, stamp,
                                          json.dumps(asdict(fact), sort_keys=True, separators=(",", ":"))))
                # A clear is silent but does not discard an unhanded opening.
                pending = [dict(r) for r in conn.execute("SELECT episode_id,fact_json FROM v2_work_lane_episodes "
                           "WHERE emitted_ref IS NULL ORDER BY lane_id,kind,sequence")]
                conn.commit()
                return pending
            except BaseException:
                conn.rollback()
                raise

        pending = await self.submit(evaluate)
        if sink is None:
            return
        for row in pending:
            try:
                ref = await sink.emit(LaneEpisodeFact(**json.loads(row["fact_json"])))
                if ref is None:
                    continue
                if not isinstance(ref, str) or not ref:
                    raise ValueError("work_lane_episode_ref_invalid")

                def latch(conn):
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        conn.execute("UPDATE v2_work_lane_episodes SET emitted_ref=? "
                                     "WHERE episode_id=? AND fact_json=? AND emitted_ref IS NULL",
                                     (ref, row["episode_id"], row["fact_json"]))
                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise
                await self.submit(latch)
            except Exception:
                # The existing drain is the only retry opportunity. A sink may
                # have committed before failing: keep the identical fact pending.
                log.exception("work lane episode handoff incomplete: %s", row["episode_id"])
