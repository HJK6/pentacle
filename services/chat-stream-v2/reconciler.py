"""Durable session/pane reconciliation for daemon v2.

Presence supplies evidence; this loop owns bounded lifecycle episodes and status.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from presence import PresenceObservation, RemotePresence
from ledger import TERMINAL_REPORT_STATUSES
from outbound_notices import NOTICE_KIND_RECONCILER, OutboundNoticeQueue
import prockill
from session_names import is_agent_session_name
from v2_runtime import env_number, iso_now

log = logging.getLogger("chat_streamd_v2.reconciler")


@dataclass
class ReconcileConfig:
    interval_s: float = 60.0
    max_rows_per_pass: int = 200
    threshold_checks: int = 2
    lookback_h: int = 48
    error_backoff_s: float = 5.0
    reap_max_rows_per_pass: int = 50
    reap_timeout_s: float = 10.0
    reap_max_attempts: int = 5
    local_list_timeout_s: float = 8.0

    @classmethod
    def from_env(cls) -> "ReconcileConfig":
        return cls(
            interval_s=env_number(
                os.environ, "PENTACLE_RECONCILER_INTERVAL_S", cls.interval_s, float,
            ),
            max_rows_per_pass=env_number(
                os.environ, "PENTACLE_RECONCILER_MAX_ROWS_PER_PASS", cls.max_rows_per_pass, int,
            ),
            threshold_checks=max(
                1,
                env_number(
                    os.environ,
                    "PENTACLE_RECONCILER_VISIBLE_DEATH_THRESHOLD_CHECKS",
                    env_number(
                        os.environ, "PENTACLE_RECONCILER_THRESHOLD_CHECKS", cls.threshold_checks, int,
                    ),
                    int,
                ),
            ),
            lookback_h=env_number(
                os.environ, "PENTACLE_RECONCILER_LOOKBACK_H", cls.lookback_h, int,
            ),
            error_backoff_s=env_number(
                os.environ, "PENTACLE_RECONCILER_ERROR_BACKOFF_S", cls.error_backoff_s, float,
            ),
            reap_max_rows_per_pass=env_number(
                os.environ, "PENTACLE_RECONCILER_REAP_MAX_ROWS_PER_PASS",
                cls.reap_max_rows_per_pass, int,
            ),
            reap_timeout_s=env_number(
                os.environ, "PENTACLE_RECONCILER_REAP_TIMEOUT_S", cls.reap_timeout_s, float,
            ),
            reap_max_attempts=max(
                1,
                env_number(
                    os.environ, "PENTACLE_RECONCILER_REAP_MAX_ATTEMPTS", cls.reap_max_attempts, int,
                ),
            ),
            local_list_timeout_s=env_number(
                os.environ, "PENTACLE_RECONCILER_LOCAL_LIST_TIMEOUT_S",
                cls.local_list_timeout_s, float,
            ),
        )


class SessionReconciler:
    """Bounded, episode-based row↔remote-presence reconciler."""

    def __init__(
        self,
        sessions: Any,
        hosts: Any,
        *,
        presence: RemotePresence | None = None,
        alerts: Any = None,
        notify: Any = None,
        comms: Any = None,
        config: ReconcileConfig | None = None,
        outbound: OutboundNoticeQueue | None = None,
        spawnctl: Any = None,
        on_reconcile_tick: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.store = sessions.store
        self.hosts = hosts
        self.presence = presence
        self.alerts = alerts
        self.notify = notify
        self.comms = comms
        self.spawnctl = spawnctl
        self.outbound = outbound or OutboundNoticeQueue(self.store, comms)
        self.outbound.comms = comms
        self.on_reconcile_tick = on_reconcile_tick
        self.cfg = config or ReconcileConfig()
        self._dead_episodes: dict[str, dict[str, Any]] = {}
        self._offline_episodes: dict[str, dict[str, Any]] = {}
        self._last_observations: dict[str, PresenceObservation] = {}
        self._local_observations: dict[str, tuple[str, str]] = {}

    @staticmethod
    def _row_generation(row: dict[str, Any]) -> str:
        """Return the durable open-generation identity with old-row fallback."""
        return str(row.get("session_generation") or f"created_at:{row.get('created_at') or ''}")

    @staticmethod
    def _dead_episode_id(row: dict[str, Any], generation: str) -> str:
        """Keep one visible-death dedup identity for one row generation."""
        return f"reconciler:c1:{row.get('host')}:{row.get('session_name')}:{generation}"

    @staticmethod
    def _offline_episode_id(host: str) -> str:
        return f"reconciler:offline:{host}"

    async def reconcile_once(self) -> dict[str, Any]:
        if self.spawnctl is not None:
            await self.spawnctl.reconcile_spawn_intents(limit=1, recurring=True)
        counters: dict[str, Any] = {
            "checked": 0,
            "closed": 0,
            "presumed_dead": 0,
            "host_online_tmux_absent": 0,
            "remote_reprobe_alive": 0,
            "remote_reprobe_gone": 0,
            "remote_reprobe_unavailable": 0,
            "ambiguous": 0,
            "ssh_unreachable": 0,
            "row_closed_tree_alive": 0,
            "session_reap_checked": 0,
            "session_reap_reaped": 0,
            "session_reap_survivors": 0,
            "session_reap_inventory_unavailable": 0,
            "session_reap_exhausted": 0,
            "self_close_swept": 0,
        }
        all_open_rows = await self.store.list_sessions("open")
        local_rows = [
            row for row in all_open_rows
            if str(row.get("host") or "") == str(self.hosts.local_host)
        ]
        self._local_observations = await self._observe_local(local_rows)

        if self.presence is not None:
            selected = self.presence.select_rows()
            selected_ids = {str(row.get("stream_id") or "") for row in selected}
            selected_by_host = self._group_presence_rows(selected)
            # Probe every configured peer, including hosts with no open rows,
            # so status can report unmanaged agent panes after restart. Only the
            # selected row batch receives a live overlay below.
            for peer in self.presence.hosts.peers:
                selected_by_host.setdefault(str(peer), [])
            self._last_observations = await self.presence.observe_once(by_host=selected_by_host)
            for observed_host, observed_rows in self._group_presence_rows(selected).items():
                self.presence.apply_observation(observed_rows, self._last_observations[observed_host])
            await self.presence.capture_previews()
        else:
            selected_ids = set()
            self._last_observations = {}

        open_by_sid = {
            f"{row.get('host')}:{row.get('session_name')}": row
            for row in all_open_rows
            if row.get("host") and row.get("session_name")
        }
        open_hosts = {str(row.get("host") or "") for row in all_open_rows}
        selected_hosts = {
            str(row.get("host") or "") for row in all_open_rows
            if str(row.get("stream_id") or "") in selected_ids
        }
        rows = all_open_rows
        if self.presence is not None:
            # Consume exactly the bounded presence batch. A separately sliced
            # store list would apply a rotating host observation to the wrong
            # custom-named rows and could close live sessions.
            selected_ids.update(
                str(row.get("stream_id") or "") for row in local_rows
            )
            rows = [row for row in rows if str(row.get("stream_id") or "") in selected_ids]
        else:
            cap = self.cfg.max_rows_per_pass
            if cap > 0:
                rows = rows[:cap]
        current_offline: set[str] = set()
        for row in rows:
            host = str(row.get("host") or "")
            name = str(row.get("session_name") or "")
            sid = f"{host}:{name}"
            if not host or not name:
                continue
            counters["checked"] += 1
            state, reason = self._row_truth(row)
            if state == "dead":
                counters["host_online_tmux_absent"] += reason == "tmux_server_absent"
                episode = self._dead_episodes.setdefault(
                    sid,
                    {"class": "row_open_session_dead", "host": host, "observations": 0,
                     "session_name": name, "episode_start_ts": iso_now(),
                     "generation": self._row_generation(row),
                     "episode_id": self._dead_episode_id(row, self._row_generation(row))},
                )
                if episode.get("generation") != self._row_generation(row):
                    episode = {
                        "class": "row_open_session_dead", "host": host, "observations": 1,
                        "session_name": name, "episode_start_ts": iso_now(),
                        "generation": self._row_generation(row),
                        "episode_id": self._dead_episode_id(row, self._row_generation(row)),
                    }
                    self._dead_episodes[sid] = episode
                else:
                    episode["observations"] = int(episode.get("observations") or 0) + 1
                if int(episode["observations"]) >= self.cfg.threshold_checks:
                    if host != self.hosts.local_host:
                        reprobe = await self._remote_liveness_reprobe(row)
                        if reprobe == "alive":
                            counters["remote_reprobe_alive"] += 1
                            self.sessions.apply_live(
                                sid, online=True, pane_status="pane_alive"
                            )
                        elif reprobe == "gone":
                            counters["remote_reprobe_gone"] += 1
                        else:
                            counters["remote_reprobe_unavailable"] += 1
                        if reprobe != "gone":
                            # A stale/failed presence observation is not a
                            # close decision. Require a fresh death episode
                            # after any ambiguous re-probe.
                            self._dead_episodes.pop(sid, None)
                            continue
                    closed = await self._close_dead(row, episode)
                    if closed:
                        counters["closed"] += 1
                        counters["presumed_dead"] += 1
                        await self._surface(row, episode, reason)
                        self._dead_episodes.pop(sid, None)
            elif state == "offline":
                current_offline.add(host)
                counters["ssh_unreachable"] += 1
                episode = self._offline_episodes.setdefault(
                    host, {"class": "row_open_host_unreachable", "host": host,
                           "observations": 0, "episode_start_ts": iso_now(),
                           "episode_id": self._offline_episode_id(host)},
                )
                episode["observations"] = int(episode.get("observations") or 0) + 1
            elif state == "ambiguous":
                counters["ambiguous"] += 1
                if sid in self._dead_episodes:
                    self._dead_episodes.pop(sid, None)
            else:
                if sid in self._dead_episodes:
                    self._dead_episodes.pop(sid, None)

        for sid, episode in list(self._dead_episodes.items()):
            open_row = open_by_sid.get(sid)
            if open_row is None or episode.get("generation") != self._row_generation(open_row):
                self._dead_episodes.pop(sid, None)
        for host in list(self._offline_episodes):
            if host not in open_hosts or (host in selected_hosts and host not in current_offline):
                self._offline_episodes.pop(host, None)

        await self._sweep_self_close_backlog(all_open_rows, counters)
        await self._reconcile_closed_survivors(counters)
        counters["row_closed_tree_alive"] = await self._closed_tree_alive_count()
        return counters

    @staticmethod
    def _group_presence_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            host = str(row.get("host") or "")
            if host:
                grouped.setdefault(host, []).append(row)
        return grouped

    async def _observe_local(self, rows: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
        """Take one bounded local pane inventory and classify known rows.

        An empty or failed inventory is ambiguous: it may be a tmux restart or
        a transient local read failure, so it cannot close anything. A non-empty
        inventory containing at least one known/agent pane is affirmative enough
        to treat absent known rows as dead; the consecutive episode threshold
        still owns the durable close.
        """
        if not rows:
            return {}
        tmux = getattr(self.sessions, "tmux", None)
        runner = getattr(tmux, "run", None)
        ambiguous = {
            str(row.get("stream_id") or ""): ("ambiguous", "local_tmux_inventory_unavailable")
            for row in rows
        }
        if not callable(runner):
            return ambiguous
        known_names = {str(row.get("session_name") or "") for row in rows}
        try:
            rc, output = await runner(
                "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}",
                timeout=self.cfg.local_list_timeout_s,
            )
        except Exception:  # noqa: BLE001 - local observation is fail-closed
            return ambiguous
        if rc != 0:
            return {
                str(row.get("stream_id") or ""): ("ambiguous", f"local_tmux_rc_{rc}")
                for row in rows
            }
        raw_lines = [line for line in str(output or "").splitlines() if line.strip()]
        alive: dict[str, str] = {}
        for line in raw_lines:
            name, _, pid = line.partition("\t")
            name = name.strip()
            if name and (name in known_names or is_agent_session_name(name)):
                alive.setdefault(name, pid.strip())
        if not raw_lines or not alive:
            return {
                str(row.get("stream_id") or ""): ("ambiguous", "local_empty_tmux_inventory")
                for row in rows
            }
        observations: dict[str, tuple[str, str]] = {}
        for row in rows:
            sid = str(row.get("stream_id") or "")
            name = str(row.get("session_name") or "")
            if name in alive:
                overlay: dict[str, Any] = {
                    "online": True, "pane_status": "pane_alive",
                }
                if alive[name]:
                    overlay["pane_pid"] = alive[name]
                self.sessions.apply_live(sid, **overlay)
                observations[sid] = ("live", "pane_alive")
            else:
                self.sessions.apply_live(sid, online=False, pane_status="pane_dead")
                observations[sid] = ("dead", "pane_absent")
        return observations

    async def _remote_liveness_reprobe(self, row: dict[str, Any]) -> str:
        """Ask the owning peer whether this exact tmux session still exists.

        Presence is intentionally broad and restart-sensitive. This second
        probe is narrow and tri-state: only an explicit tmux ``has-session``
        miss is proof of death; SSH errors remain unknown.
        """
        host = str(row.get("host") or "")
        name = str(row.get("session_name") or "")
        try:
            tmux = self.hosts.tmux_for(host)
            probe = getattr(tmux, "session_state", None)
            if not callable(probe):
                return "unavailable"
            result = probe(name)
            if hasattr(result, "__await__"):
                result = await result
            state = str(result or "").strip().lower()
            return state if state in {"alive", "gone"} else "unavailable"
        except Exception:  # noqa: BLE001 - liveness must fail closed
            log.warning("remote liveness re-probe failed host=%s session=%s", host, name, exc_info=True)
            return "unavailable"

    def _row_truth(self, row: dict[str, Any]) -> tuple[str, str]:
        host = str(row.get("host") or "")
        name = str(row.get("session_name") or "")
        if host == self.hosts.local_host:
            # A persisted pane_status is a last observation, not fresh proof.
            # A fresh inventory pass is safe to consume; without one the local
            # Mirror remains the owner and we fail closed after a restart.
            return self._local_observations.get(
                f"{host}:{name}", ("ambiguous", "local_observation_owned_by_mirror")
            )
        observation = self._last_observations.get(host)
        if observation is None:
            return "offline", "no_observation"
        if observation.transport_failed:
            return "offline", observation.reason
        if observation.confirms_absence:
            return "dead", observation.reason
        if observation.state == "host_online_tmux_present":
            return ("live", "pane_alive") if name in observation.alive else ("dead", "pane_absent")
        return "ambiguous", observation.reason

    async def _close_dead(self, row: dict[str, Any], episode: dict[str, Any]) -> bool:
        host = str(row["host"])
        name = str(row["session_name"])
        sid = f"{host}:{name}"
        now = iso_now()
        marked = await self.sessions.mark_reconciled_dead(
            host,
            name,
            presumed_dead_at=str(episode.get("episode_start_ts") or now),
            closed_at=now,
            expected_generation=str(episode.get("generation") or ""),
        )
        if marked is None:
            return False
        # Presence has proven only that the remote pane is absent.  It has not
        # captured the close-time process/boot inventory needed to prove an
        # empty survivor set, so closed-survivor reconciliation must own this
        # readback and keep it fail-closed until it can prove absence.
        await self.store.upsert_session_reap(
            sid, reap_status="unknown", survivors=[], attempts=0, updated_at=now
        )
        return True

    async def _has_authorized_terminal_report(self, row: dict[str, Any]) -> bool:
        if not (row.get("parent_stream_id") and row.get("self_close_on_completion") is True):
            return False
        report = await self.store.find_report(
            f"{row.get('host')}:{row.get('session_name')}",
            statuses=("done",), session_generation=self._row_generation(row),
        )
        return isinstance(report, dict)

    async def _parent_closed_or_absent(self, row: dict[str, Any]) -> bool:
        """True when the seat's parent is closed, or its row is gone entirely.

        Fail closed: a missing lineage or an unreadable parent state does NOT
        authorize a sweep close (returns False), so predicate B fires only on a
        parent we can positively confirm is closed or absent."""
        parent = str(row.get("parent_stream_id") or "").strip()
        if not parent:
            return False
        try:
            phost, pname = self.sessions.split(parent)
            psession = self.sessions.get(parent) or await self.store.fetch_session(phost, pname)
        except Exception:  # noqa: BLE001 - unknown parent state must not authorize a close
            return False
        if not isinstance(psession, dict):
            return True
        return str(psession.get("status") or "") == "closed"

    async def _sweep_self_close_backlog(
        self, rows: list[dict[str, Any]], counters: dict[str, Any]
    ) -> None:
        """Close finished hidden seats the report-time self-close never reaped.

        Rides the reconcile pass (boot included) — no new timer. A close needs
        BOTH a valid terminal report of the row's CURRENT generation (proof it
        finished; a resumed seat is never closed on a stale report) AND the pane
        proven idle by the busy-pane gate (`defer_if_working`) — a still-working
        pane is deferred, never reaped (the reaped-mid-review incident,
        2026-09-07). Under one of two predicates:
          A. flag set — the daemon owns `self_close_on_completion`; this is the
             retry for a report-time close (`ledger._terminate_after_report`)
             that failed or raced.
          B. parent closed/absent — nobody can ever send the seat more work, so
             the flag is moot and the finished seat is freed.
        A candidate that satisfies a predicate but lacks a valid report — e.g.
        only a schema-REJECTED report (a `v2_report_rejections` row) — is NOT a
        completion: it is UNKNOWN, skipped and logged, retried next pass. It
        still terminalizes when it files a valid report or its pane dies (the
        dead-pane path), so no seat is immortal.

        `rows` is the pass-start snapshot; every close decision is re-validated
        against the AUTHORITATIVE current row (fetched from the store) immediately
        before closing, so a concurrent close/reopen (new generation) or a parent
        reopen between the snapshot and here cannot let a stale report authorize a
        close. A candidate whose state changed is skipped and retried next pass.
        """
        for snapshot in rows:
            if str(snapshot.get("visibility") or "default") != "hidden":
                continue
            host = str(snapshot.get("host") or "")
            name = str(snapshot.get("session_name") or "")
            if not host or not name:
                continue
            # Cheap snapshot prefilter: only a flagged or parent-gone row is ever
            # a candidate, so non-candidates cost no extra store reads.
            if snapshot.get("self_close_on_completion") is not True and not await (
                self._parent_closed_or_absent(snapshot)
            ):
                continue
            row = await self.store.fetch_session(host, name)
            if not isinstance(row, dict) or str(row.get("status") or "") != "open":
                continue
            # Re-check visibility on the CURRENT row: a hidden→visible transition
            # (set_visibility) between the snapshot and here must not let the
            # hidden-seat sweep close a now-visible seat.
            if str(row.get("visibility") or "default") != "hidden":
                continue
            flagged = row.get("self_close_on_completion") is True
            if not flagged and not await self._parent_closed_or_absent(row):
                continue
            generation = self._row_generation(row)
            report = await self.store.find_report(
                f"{host}:{name}",
                statuses=TERMINAL_REPORT_STATUSES,
                session_generation=generation,
            )
            # A sweep close needs the seat's OWN durable terminal report of THIS
            # generation — proof it finished. Anything else is UNKNOWN: skip and
            # log, retried next pass. In particular a schema-REJECTED report (a
            # durable rejection row but NO valid report) is NOT a completion
            # signal — the seat never finished; reaping it on the rejection loses
            # the work and its chance to re-file (the reaped-mid-review incident,
            # 2026-09-07). A skipped seat still terminalizes when it files a valid
            # report or its pane dies, so no seat is immortal.
            if not isinstance(report, dict):
                log.info(
                    "self-close sweep skipped stream=%s:%s flagged=%s reason=no_terminal_report",
                    host, name, flagged,
                )
                continue
            authorized_by = "report"
            try:
                # Fence the close to the exact generation whose report authorized
                # it: `_close_locked` re-reads the authoritative row and refuses
                # (never kills) if a close/reopen produced a different generation,
                # so a stale report can never close a live successor.
                # `defer_if_working` adds the busy-pane gate the report-time close
                # relies on: a seat whose pane is still working is deferred, never
                # reaped, so the sweep cannot kill a seat mid-work. Attribution
                # stamps the discriminating fields (auth_kind, closed_by) onto the
                # v2_close_audit row so a sweep close is distinguishable from a
                # legitimate one.
                result = await self.sessions.close(
                    host, name, "self_close_backlog_sweep",
                    expected_generation=generation, requires_hidden=True,
                    defer_if_working=True,
                    attribution={
                        "closed_by": "self_close_sweep",
                        "auth_kind": authorized_by,
                        "defer_if_working": True,
                    },
                )
            except Exception:  # noqa: BLE001 - a failed close is retried next pass
                log.exception("self-close sweep close failed stream=%s:%s", host, name)
                continue
            if result.get("deferred"):
                # Busy pane: the working gate spared it (audited as `deferred`);
                # retried next pass once the pane goes idle.
                log.info(
                    "self-close sweep deferred stream=%s:%s flagged=%s reason=working",
                    host, name, flagged,
                )
                continue
            # Count only a real close: a stale-generation / already-closed /
            # visibility-fenced result carries no `closed` flag and is retried
            # (or spared) next pass, so the counters and log never overstate.
            if result.get("closed"):
                counters["self_close_swept"] += 1
                counters["closed"] += 1
                log.info(
                    "self-close sweep closed stream=%s:%s flagged=%s authorized_by=%s",
                    host, name, flagged, authorized_by,
                )

    async def _surface(self, row: dict[str, Any], episode: dict[str, Any], reason: str) -> None:
        episode_id = str(
            episode.get("episode_id")
            or f"reconciler:c1:{row['host']}:{row['session_name']}:{episode.get('episode_start_ts')}"
        )
        payload = {
            "episode_id": episode_id,
            "class": "row_open_session_dead",
            "stream_id": f"{row['host']}:{row['session_name']}",
            "host": row.get("host"),
            "visibility": row.get("visibility"),
            "suspected_cause": reason or "dead_pane",
            "survivors": None,
            "next_action": "inspect the peer and resume or respawn the session",
        }
        surface_allowed = (
            str(row.get("visibility") or "default") == "default"
            and not row.get("parent_stream_id")
        )
        if self.alerts is not None and surface_allowed:
            self.alerts.emit("reconciler_session_dead", **payload)
        if self.notify is not None and surface_allowed:
            try:
                await self.notify.notification({
                    "type": "notification.create",
                    "producer": "session_reconciler",
                    "dedup_key": payload["episode_id"],
                    "severity": "warning",
                    "title": "Session pane is gone",
                    "body": (
                        f"{payload['stream_id']} on {payload['host']} was confirmed dead after "
                        f"{self.cfg.threshold_checks} consecutive observations."
                    ),
                    "actions": [],
                })
            except Exception:  # noqa: BLE001 - surfacing never blocks cleanup
                log.exception("reconciler notification failed stream=%s", payload["stream_id"])
        if not surface_allowed and row.get("parent_stream_id"):
            if await self._has_authorized_terminal_report(row):
                log.info("reconciler child terminal report owns close stream=%s", payload["stream_id"])
                return
            notice_id = str(payload["episode_id"])
            try:
                await self.outbound.enqueue(
                    kind=NOTICE_KIND_RECONCILER,
                    dedupe_key=f"reconciler:{notice_id}",
                    recipient_stream_id=str(row["parent_stream_id"]),
                    tell_id=notice_id,
                    body=(
                        f"Child session {payload['stream_id']} was confirmed dead by the reconciler; "
                        "inspect or respawn it."
                    ),
                    source_stream_id=str(payload["stream_id"]),
                    episode_id=notice_id,
                    metadata={"class": payload["class"], "reason": reason},
                )
                await self.outbound.deliver_now(notice_id)
            except Exception:  # noqa: BLE001 - the durable row is the retry boundary
                log.exception("reconciler parent notice queue failed stream=%s", payload["stream_id"])

    # -- closed survivor reconciliation -----------------------------------

    @staticmethod
    def _parse_process_table(text: str) -> dict[int, dict[str, Any]]:
        processes: dict[int, dict[str, Any]] = {}
        for line in text.splitlines():
            parts = line.strip().split(None, 10)
            if len(parts) < 10:
                continue
            try:
                pid, ppid, uid, pgid, sid = (int(parts[index]) for index in range(5))
            except (TypeError, ValueError):
                continue
            start_id = " ".join(parts[5:10])
            command = parts[10] if len(parts) > 10 else ""
            if pid > 0 and ppid >= 0 and uid >= 0 and pgid >= 0 and sid >= 0 and start_id:
                processes[pid] = {
                    "pid": pid, "ppid": ppid, "uid": uid, "pgid": pgid,
                    "sid": sid, "start_id": start_id, "command": command,
                }
        return processes

    @staticmethod
    def _descendants(root_pid: int, processes: dict[int, dict[str, Any]]) -> set[int]:
        if root_pid not in processes:
            return set()
        children: dict[int, list[int]] = {}
        for pid, item in processes.items():
            try:
                children.setdefault(int(item["ppid"]), []).append(pid)
            except (KeyError, TypeError, ValueError):
                continue
        found: set[int] = set()
        queue = [root_pid]
        while queue:
            pid = queue.pop(0)
            if pid in found:
                continue
            found.add(pid)
            queue.extend(sorted(children.get(pid, [])))
        return found

    async def _host_command(self, host: str, *args: str) -> tuple[int, str] | None:
        runner = getattr(self.hosts, "run_command", None)
        if not callable(runner):
            return None
        try:
            return await runner(host, *args, timeout=self.cfg.reap_timeout_s)
        except TypeError:
            # Small test seams and older injected host adapters may not accept
            # the keyword; the bounded production Hosts implementation does.
            try:
                return await runner(host, *args)
            except Exception:  # noqa: BLE001 - unavailable inventory is safe
                return None
        except Exception:  # noqa: BLE001 - unavailable inventory is safe
            return None

    async def _reap_inventory(
        self, host: str
    ) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], str] | None:
        """Read tmux panes and ps as one authoritative, fail-closed snapshot."""
        try:
            tmux = self.hosts.tmux_for(host)
            rc, output = await tmux.run(
                "list-panes", "-a", "-F",
                "#{session_name}|#{pane_pid}|#{pane_id}|#{pane_tty}|#{socket_path}",
                timeout=self.cfg.reap_timeout_s,
            )
        except Exception:  # noqa: BLE001
            return None
        if rc != 0:
            return None
        panes: list[dict[str, Any]] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            fields = line.split("|")
            if (len(fields) != 5 or not all(field.strip() for field in fields)
                    or not fields[1].isascii() or not fields[1].isdigit()):
                return None
            try:
                pid = int(fields[1].strip())
            except (TypeError, ValueError):
                return None
            if pid <= 0:
                return None
            panes.append({
                "session_name": fields[0].strip(), "pane_pid": pid,
                "pane_id": fields[2].strip(), "tty": fields[3].strip(),
                "tmux_socket": fields[4].strip(),
            })
        ps_result = await self._host_command(
            host, "ps", "-eo", "pid=,ppid=,uid=,pgid=,sid=,lstart=,command="
        )
        boot_result = await self._host_command(host, "sysctl", "-n", "kern.boottime")
        if boot_result is None or boot_result[0] != 0 or not boot_result[1].strip():
            boot_result = await self._host_command(host, "cat", "/proc/sys/kernel/random/boot_id")
        if (
            ps_result is None or ps_result[0] != 0
            or boot_result is None or boot_result[0] != 0 or not boot_result[1].strip()
        ):
            return None
        raw_processes = str(ps_result[1] or "")
        # A successful command with no bytes, or bytes that contain no valid
        # records, is not a complete process inventory. This matters most when
        # tmux also reports no panes: treating an empty snapshot as proof that
        # recorded survivors died would turn an observability gap into reaped.
        if not raw_processes.strip():
            return None
        processes = self._parse_process_table(raw_processes)
        if not processes:
            return None
        boot = boot_result[1].strip()
        # A pane root missing from ps means the process inventory is incomplete;
        # treating that as an empty tree would falsely prove foreign exclusion.
        if any(int(pane["pane_pid"]) not in processes for pane in panes):
            return None
        return panes, processes, boot

    @staticmethod
    def _survivor_records(survivors: object) -> list[dict[str, Any]]:
        return [item for item in survivors if isinstance(item, dict)] if isinstance(survivors, list) else []

    def _identity_safe_records(
        self,
        host: str,
        session_name: str,
        survivors: list[dict[str, Any]],
        panes: list[dict[str, Any]],
        processes: dict[int, dict[str, Any]],
        boot: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split survivors using a complete, current process-instance proof."""
        safe: list[dict[str, Any]] = []
        deferred: list[dict[str, Any]] = []
        expected_fields = {
            "version", "host", "uid", "boot_id", "pid", "start_id", "ppid",
            "pgid", "sid", "tmux_socket", "tmux_session", "tmux_pane", "tty",
            "captured_at", "command_fingerprint",
        }
        for item in survivors:
            if item.get("reap_reason") == "inventory_incomplete":
                deferred.append({**item, "reap_reason": "inventory_incomplete"})
                continue
            try:
                pid = int(item.get("pid"))
            except (TypeError, ValueError):
                deferred.append({**item, "reap_reason": "ownership_proof_malformed"})
                continue
            if pid <= 0:
                deferred.append({**item, "reap_reason": "ownership_proof_malformed"})
                continue
            current = processes.get(pid)
            if current is None:
                continue
            proof = item.get("ownership_proof_v2")
            if not isinstance(proof, dict) or set(proof) != expected_fields:
                deferred.append({**item, "reap_reason": "ownership_proof_missing"})
                continue
            try:
                proof_pid = int(proof.get("pid"))
                proof_uid = int(proof.get("uid"))
                proof_ppid = int(proof.get("ppid"))
                proof_pgid = int(proof.get("pgid"))
                proof_sid = int(proof.get("sid"))
                current_uid = int(current.get("uid"))
                current_ppid = int(current.get("ppid"))
                current_pgid = int(current.get("pgid"))
                current_sid = int(current.get("sid"))
            except (TypeError, ValueError):
                deferred.append({**item, "reap_reason": "ownership_proof_malformed"})
                continue
            if (
                proof.get("version") != 2
                or proof.get("host") != host
                or proof.get("tmux_session") != session_name
                or proof_pid != pid
                or proof_uid != current_uid
                or proof_ppid != current_ppid
                or proof_pgid != current_pgid
                or proof_sid != current_sid
                or proof.get("boot_id") != boot
                or proof.get("start_id") != current.get("start_id")
                or proof.get("command_fingerprint") != prockill.command_fingerprint(str(current.get("command") or ""))
                or not all(str(proof.get(field) or "") for field in ("tmux_socket", "tmux_session", "tmux_pane", "tty"))
            ):
                deferred.append({**item, "reap_reason": "identity_drift"})
                continue
            target_panes = [
                pane for pane in panes
                if str(pane.get("session_name") or "") == session_name
            ]
            matching = [
                pane for pane in target_panes
                if int(pane.get("pane_pid") or -1) == pid
                and str(pane.get("pane_id") or "") == str(proof.get("tmux_pane") or "")
                and str(pane.get("tty") or "") == str(proof.get("tty") or "")
                and str(pane.get("tmux_socket") or "") == str(proof.get("tmux_socket") or "")
            ]
            # The target pane may have been removed by close. If a replacement
            # pane with the same session name exists, however, absence of an
            # exact proof match is PID/session drift and is never signalable.
            if target_panes and not matching:
                deferred.append({**item, "reap_reason": "identity_drift"})
                continue
            safe.append({**item, "pid": pid, "ownership_proof_v2": proof})
        return safe, deferred

    def _reap_set(
        self,
        session_name: str,
        roots: list[int],
        processes: dict[int, dict[str, Any]],
        panes: list[dict[str, Any]],
    ) -> set[int]:
        recorded: set[int] = set()
        for root in roots:
            recorded.update(self._descendants(root, processes))
        foreign: set[int] = set()
        for pane in panes:
            if str(pane.get("session_name") or "") == session_name:
                continue
            try:
                foreign.update(self._descendants(int(pane["pane_pid"]), processes))
            except (KeyError, TypeError, ValueError):
                continue
        return recorded - foreign

    async def _signal_pids(self, host: str, pids: set[int], sig: signal.Signals) -> bool:
        if not pids:
            return False
        hook = getattr(self.hosts, "signal_pids", None)
        if callable(hook):
            try:
                result = hook(host, sorted(pids), sig)
                if hasattr(result, "__await__"):
                    result = await result
                return bool(result is not False)
            except Exception:  # noqa: BLE001
                return False
        if host == self.hosts.local_host:
            def _send() -> bool:
                sent = False
                for pid in sorted(pids):
                    if pid in {1, os.getpid()}:
                        continue
                    try:
                        os.kill(pid, sig)
                        sent = True
                    except (ProcessLookupError, PermissionError, ValueError):
                        pass
                return sent
            return await asyncio.to_thread(_send)
        command = "kill" if sig == signal.SIGKILL else "kill"
        sign = "-KILL" if sig == signal.SIGKILL else "-TERM"
        result = await self._host_command(host, command, sign, *(str(pid) for pid in sorted(pids)))
        return bool(result is not None and result[0] == 0)

    @staticmethod
    def _next_reap_time(attempts: int) -> str:
        backoff_s = (10, 30, 120, 600, 600)
        index = max(0, min(attempts - 1, len(backoff_s) - 1))
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + backoff_s[index]))

    async def _reconcile_closed_survivors(self, counters: dict[str, Any]) -> None:
        rows = await self.store.list_session_reap_unreaped(lookback_h=self.cfg.lookback_h, now=iso_now())
        if self.cfg.reap_max_rows_per_pass > 0:
            rows = rows[:self.cfg.reap_max_rows_per_pass]
        for reap in rows:
            counters["session_reap_checked"] += 1
            sid = str(reap.get("stream_id") or "")
            host, sep, session_name = sid.partition(":")
            if not sep or not host or not session_name:
                continue
            inventory = await self._reap_inventory(host)
            if inventory is None:
                counters["session_reap_inventory_unavailable"] += 1
                continue
            panes, processes, boot = inventory
            stored = self._survivor_records(reap.get("survivors"))
            safe, deferred = self._identity_safe_records(
                host, session_name, stored, panes, processes, boot
            )
            if reap.get("exhausted_at"):
                # An unknown close without a currently valid identity is not
                # proof of death. In particular, an empty remote-close
                # readback must remain visible as unknown across restarts.
                if reap.get("reap_status") == "unknown" and not safe and not deferred:
                    await self.store.upsert_session_reap(
                        sid, reap_status="unknown", survivors=stored,
                        attempts=int(reap.get("attempts") or 0),
                        exhausted_at=str(reap.get("exhausted_at") or iso_now()),
                        updated_at=iso_now(),
                    )
                    counters["session_reap_exhausted"] += 1
                    continue
                if safe or deferred:
                    counters["session_reap_exhausted"] += 1
                    await self._surface_survivors(sid, safe + deferred, exhausted=True)
                    continue
                await self.store.upsert_session_reap(
                    sid, reap_status="reaped", survivors=[], attempts=int(reap.get("attempts") or 0),
                    updated_at=iso_now(),
                )
                counters["session_reap_reaped"] += 1
                continue
            if not safe:
                # Identity drift/malformed survivors are retained for audit but
                # never turn into a blind PID kill. They are unknown, not dead.
                incomplete = any(
                    item.get("reap_reason") == "inventory_incomplete" for item in deferred
                )
                preserve_unknown = reap.get("reap_status") == "unknown" and not stored
                status = "unknown" if incomplete or preserve_unknown else (
                    "survivors" if deferred else "reaped"
                )
                retained = deferred if deferred else (stored if preserve_unknown else [])
                await self.store.upsert_session_reap(
                    sid, reap_status=status, survivors=retained,
                    attempts=int(reap.get("attempts") or 0), updated_at=iso_now(),
                )
                if status == "reaped":
                    counters["session_reap_reaped"] += 1
                else:
                    counters["session_reap_survivors"] += 1
                continue
            # Re-read immediately before signal delivery. A proof captured on
            # an earlier pass is not enough to authorize a reused PID.
            pre_signal = await self._reap_inventory(host)
            if pre_signal is None:
                counters["session_reap_inventory_unavailable"] += 1
                continue
            panes, processes, boot = pre_signal
            safe, deferred = self._identity_safe_records(
                host, session_name, safe, panes, processes, boot
            )
            if not safe:
                incomplete = any(
                    item.get("reap_reason") == "inventory_incomplete" for item in deferred
                )
                await self.store.upsert_session_reap(
                    sid,
                    reap_status="unknown" if incomplete else ("survivors" if deferred else "reaped"),
                    survivors=deferred, attempts=int(reap.get("attempts") or 0),
                    updated_at=iso_now(),
                )
                continue
            roots = [int(item["pid"]) for item in safe]
            candidates = self._reap_set(session_name, roots, processes, panes)
            # Never signal pid 1 or this daemon, even if a malformed ledger row
            # contains them. Foreign tmux trees were removed above.
            candidates -= {1, os.getpid()}
            attempts = int(reap.get("attempts") or 0) + 1
            sig = signal.SIGTERM if attempts <= 3 else signal.SIGKILL
            await self._signal_pids(host, candidates, sig)
            after_inventory = await self._reap_inventory(host)
            if after_inventory is None:
                remaining = safe + deferred
                status = "unknown"
            else:
                after_panes, after_processes, after_boot = after_inventory
                still_safe, still_deferred = self._identity_safe_records(
                    host, session_name, safe, after_panes, after_processes, after_boot
                )
                remaining = still_safe + still_deferred + deferred
                status = "survivors" if remaining else "reaped"
            exhausted = attempts >= self.cfg.reap_max_attempts and status != "reaped"
            await self.store.upsert_session_reap(
                sid,
                reap_status="survivors" if exhausted and remaining else status,
                survivors=remaining,
                attempts=attempts,
                next_attempt_at=None if exhausted or status == "reaped" else self._next_reap_time(attempts),
                exhausted_at=iso_now() if exhausted else None,
                updated_at=iso_now(),
            )
            if status == "reaped" or not remaining:
                counters["session_reap_reaped"] += 1
            elif exhausted:
                counters["session_reap_exhausted"] += 1
                await self._surface_survivors(sid, remaining, exhausted=True)
            else:
                counters["session_reap_survivors"] += 1

    async def _surface_survivors(self, sid: str, survivors: list[dict[str, Any]], *, exhausted: bool) -> None:
        row = await self.store.fetch_session(*sid.split(":", 1)) if ":" in sid else None
        if not row:
            return
        if row.get("parent_stream_id"):
            notice_id = f"reconciler:c2:{sid}"
            try:
                await self.outbound.enqueue(
                    kind=NOTICE_KIND_RECONCILER,
                    dedupe_key=f"reconciler:{notice_id}",
                    recipient_stream_id=str(row["parent_stream_id"]),
                    tell_id=notice_id,
                    body=f"Child session {sid} still has survivor processes after close.",
                    source_stream_id=sid,
                    episode_id=notice_id,
                    metadata={"class": "row_closed_tree_alive", "exhausted": exhausted},
                )
                await self.outbound.deliver_now(notice_id)
            except Exception:  # noqa: BLE001 - the durable row is the retry boundary
                log.exception("reconciler parent survivor notice queue failed stream=%s", sid)
            return
        if str(row.get("visibility") or "default") != "default":
            return
        payload = {
            "episode_id": f"reconciler:c2:{sid}",
            "class": "row_closed_tree_alive",
            "stream_id": sid,
            "host": row.get("host"),
            "visibility": row.get("visibility"),
            "suspected_cause": "closed_session_survivors",
            "survivors": survivors,
            "next_action": "inspect the session-reap ledger and survivor processes",
        }
        if self.alerts is not None:
            self.alerts.emit("reconciler_session_survivors", **payload)
        if self.notify is not None and exhausted:
            try:
                await self.notify.notification({
                    "type": "notification.create", "producer": "session_reconciler",
                    "dedup_key": payload["episode_id"], "severity": "warning",
                    "title": "Closed session still has survivor processes",
                    "body": f"{sid} still has survivor processes after bounded reap attempts.",
                    "actions": [],
                })
            except Exception:  # noqa: BLE001
                log.exception("reconciler survivor notification failed stream=%s", sid)

    async def _closed_tree_alive_count(self) -> int:
        return await self.store.count_closed_tree_alive(
            lookback_h=self.cfg.lookback_h, now=iso_now(),
        )

    async def status(self, *, host: str | None = None) -> dict[str, Any]:
        """Return fleet counts/details without mutating sessions or panes."""
        rows = await self.store.list_sessions(None)
        if host:
            rows = [row for row in rows if str(row.get("host") or "") == host]
        counts = {
            "row_open_session_dead": 0,
            "row_closed_tree_alive": 0,
            "row_open_host_unreachable": 0,
            "unmanaged_tree": 0,
        }
        details: list[dict[str, Any]] = []
        reap_rows = await self.store.list_session_reap_all()
        for row in sorted(rows, key=lambda item: (str(item.get("host") or ""), str(item.get("session_name") or ""))):
            sid = str(row.get("stream_id") or "")
            is_open = str(row.get("status") or "open") == "open" and not row.get("closed_at")
            if is_open:
                episode = self._dead_episodes.get(sid)
                row_host = str(row.get("host") or "")
                observation = self._last_observations.get(row_host)
                local_observation = (
                    self._local_observations.get(sid)
                    if row_host == str(self.hosts.local_host)
                    else None
                )
                observed_dead = bool(
                    (local_observation is not None and local_observation[0] == "dead")
                    or (observation is not None and (
                        observation.confirms_absence
                        or (
                            observation.state == "host_online_tmux_present"
                            and str(row.get("session_name") or "") not in observation.alive
                            and (
                                str(row.get("session_name") or "") in observation.known
                                or is_agent_session_name(str(row.get("session_name") or ""))
                            )
                        )
                    ))
                )
                if episode is not None or observed_dead:
                    counts["row_open_session_dead"] += 1
                    details.append({
                        "class": "row_open_session_dead", "stream_id": sid,
                        "host": row.get("host"), "session_name": row.get("session_name"),
                        "visibility": row.get("visibility"),
                        "parent_stream_id": row.get("parent_stream_id"),
                        "episode_id": episode.get("episode_id") if episode else None,
                        "observations": episode.get("observations", 0) if episode else 0,
                        "presumed_dead_at": (
                            row.get("presumed_dead_at")
                            or (episode.get("episode_start_ts") if episode else None)
                        ),
                        "suspected_cause": "dead_pane",
                    })
                offline = self._offline_episodes.get(str(row.get("host") or ""))
                observed_offline = bool(observation is not None and observation.transport_failed)
                if offline is not None or observed_offline:
                    counts["row_open_host_unreachable"] += 1
                    details.append({
                        "class": "row_open_host_unreachable", "stream_id": sid,
                        "host": row.get("host"), "session_name": row.get("session_name"),
                        "visibility": row.get("visibility"),
                        "parent_stream_id": row.get("parent_stream_id"),
                        "episode_id": offline.get("episode_id") if offline else None,
                        "observations": offline.get("observations", 0) if offline else 0,
                        "suspected_cause": "host_unreachable",
                    })
                continue
            reap = reap_rows.get(sid)
            if row.get("pane_status") == "pane_alive" or (
                reap is not None and reap.get("reap_status") != "reaped"
            ):
                counts["row_closed_tree_alive"] += 1
                details.append({
                    "class": "row_closed_tree_alive", "stream_id": sid,
                    "host": row.get("host"), "session_name": row.get("session_name"),
                    "reap_status": reap.get("reap_status") if reap else "online_summary",
                    "survivors": reap.get("survivors", []) if reap else [],
                    "attempts": reap.get("attempts", 0) if reap else 0,
                    "exhausted_at": reap.get("exhausted_at") if reap else None,
                })
        managed_by_host: dict[str, set[str]] = {}
        for row in rows:
            if str(row.get("status") or "open") == "open":
                managed_by_host.setdefault(str(row.get("host") or ""), set()).add(
                    str(row.get("session_name") or "")
                )
        observed = self._last_observations.items()
        for observed_host, observation in observed:
            if host and observed_host != host:
                continue
            unmanaged = [
                name for name in observation.raw_sessions
                if name not in managed_by_host.get(observed_host, set())
            ]
            counts["unmanaged_tree"] += len(unmanaged)
            details.extend({
                "class": "unmanaged_tree",
                "stream_id": f"{observed_host}:{name}",
                "host": observed_host,
                "session_name": name,
            } for name in unmanaged)
        return {"host": host, "counts": counts, "details": details}

    async def run_forever(self) -> None:
        while True:
            try:
                await self.reconcile_once()
                if self.on_reconcile_tick is not None:
                    await self.on_reconcile_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                log.exception("session reconciler pass failed")
                await asyncio.sleep(self.cfg.error_backoff_s)
                continue
            await asyncio.sleep(self.cfg.interval_s)
