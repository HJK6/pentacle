"""Bind lane-owned opening facts to the shared digest-only alert family."""
from __future__ import annotations

from alerts import Alerts
from error_adapters import ErrorFact
from store_work_lane_episodes import LaneEpisodeFact


class WorkLaneAlertSink:
    def __init__(self, alerts: Alerts) -> None:
        # Keep the existing Alerts object, not its initially unavailable sink.
        self.alerts = alerts

    async def emit(self, fact: LaneEpisodeFact) -> str | None:
        # M1's serialized principal stays immutable. Every opening, including
        # a pending M1 fact, uses this one stable producer at the real core seam.
        return await self.alerts.error(
            ErrorFact(family="work_lane.v1", code=fact.code,
                      episode_id=fact.episode_id, condition="active"),
            principal="system:work-lanes",
        )
