"""Daemon-owned restore of the direct-primary assistant's bound seat.

When the seat a direct-primary composite is bound to is proven dead (a host
restart or logout killed its pane), this owner resumes the SAME Claude session
on the SAME stream and moves the binding to the resumed generation by
compare-and-set.  It covers a local, Claude-backed seat that is not protected
by ``PENTACLE_ASSISTANT_ROLE``; a protected holder keeps its explicit recovery.

One durable episode exists per dead generation (``v2_assistant_restore_episode``)
and every transition is written with its audit row in one transaction, so a
daemon restart at any point continues the same episode instead of starting a
second one.  The automatic trigger is off unless ``PENTACLE_ASSISTANT_AUTO_RESTORE=1``;
an operator can start or retry an episode with the ``assistant.restore`` verb.

Nothing here designates fleet lifecycle authority: the lifecycle-manager grant
stays with the operator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("chat_streamd_v2.assistant_restore")

AUTO_RESTORE_ENV = "PENTACLE_ASSISTANT_AUTO_RESTORE"
INHIBIT_FILE_NAME = "assistant-auto-restore.inhibit"
MAX_ATTEMPTS = 4
#: Delay before the 2nd, 3rd and 4th attempt of one budget epoch.
BACKOFFS_S = (30.0, 120.0, 600.0)
#: An attempt still unresolved this long after it started ends the episode.
UNRESOLVED_LIMIT_S = 600.0
PS_TIMEOUT_S = 5.0
SERVICE_ACTOR = "daemon:scheduler"
NOTICE_FROM = "daemon:assistant-restore"

_GENERATION_RE = re.compile(r"[0-9a-f]{32}\Z")
_FAILED_OUTCOME_STATES = frozenset({"failed", "cancelled"})


def parse_auto_restore_flag(environ: dict[str, str]) -> bool:
    """``1`` enables; unset, empty or ``0`` disables; anything else is an error."""
    raw = str(environ.get(AUTO_RESTORE_ENV, "")).strip()
    if raw in {"", "0"}:
        return False
    if raw == "1":
        return True
    raise ValueError(f"{AUTO_RESTORE_ENV} must be 1, 0 or unset")


def service_session_generation(msg: dict[str, Any]) -> str:
    """The caller-chosen generation of an internal restore spawn, else ``""``.

    Only the daemon's own scheduler identity may choose a generation.  The
    server strips underscore-prefixed fields from client messages and builds
    ``_auth_context`` itself, so no client can reach this.
    """
    auth = msg.get("_auth_context")
    value = msg.get("_session_generation")
    if (isinstance(auth, dict) and auth.get("service_authenticated") is True
            and auth.get("service_actor") == SERVICE_ACTOR
            and isinstance(value, str) and _GENERATION_RE.fullmatch(value)):
        return value
    return ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _normalize_start(value: object) -> str:
    return " ".join(str(value or "").split())


async def run_ps(pid: str, *, ps_bin: str = "ps", timeout_s: float = PS_TIMEOUT_S) -> tuple[int, str, str]:
    """Run ``ps -o lstart= -p <pid>``; raise on a missing binary or a timeout."""
    proc = await asyncio.create_subprocess_exec(
        ps_bin, "-o", "lstart=", "-p", pid,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return int(proc.returncode or 0), out.decode(errors="replace"), err.decode(errors="replace")


def classify_ps(rc: int, stdout: str, stderr: str, recorded_start: str) -> str:
    """``gone`` / ``alive`` / ``unknown`` for one explicit ``ps`` observation.

    Only two results prove the recorded process is gone: ``ps`` found no such
    pid (exit 1, no output at all), or the pid now belongs to a process with a
    different start time.  Everything else is unknown, never "no process".
    """
    if rc == 1:
        # "No such pid" prints nothing at all; whitespace is still output.
        return "gone" if stdout == "" and stderr == "" else "unknown"
    if rc == 0 and stderr == "":
        # Exactly one line; blank lines around it are not ignored.
        lines = stdout.splitlines()
        if len(lines) != 1:
            return "unknown"
        try:
            datetime.strptime(_normalize_start(lines[0]), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return "unknown"
        return "alive" if _normalize_start(lines[0]) == _normalize_start(recorded_start) else "gone"
    return "unknown"


def test_hooks(environ: dict[str, str]) -> tuple[Any, Any]:
    """Fault-injection hooks for the disposable-daemon soak; inert in service.

    Read only when ``PENTACLE_FORCE_LIVE_DAEMON=1``.  Returns ``(fault, ps_runner)``:
    ``PENTACLE_TEST_RESTORE_CRASH_AT`` exits the daemon at that point,
    ``PENTACLE_TEST_RESTORE_PAUSE_AT`` waits there until
    ``PENTACLE_TEST_RESTORE_PAUSE_FILE`` exists, and
    ``PENTACLE_TEST_RESTORE_PS_BIN`` replaces the ``ps`` binary.
    """
    if environ.get("PENTACLE_FORCE_LIVE_DAEMON") != "1":
        return None, None
    crash_at = environ.get("PENTACLE_TEST_RESTORE_CRASH_AT", "").strip()
    pause_at = environ.get("PENTACLE_TEST_RESTORE_PAUSE_AT", "").strip()
    pause_file = environ.get("PENTACLE_TEST_RESTORE_PAUSE_FILE", "").strip()
    ps_bin = environ.get("PENTACLE_TEST_RESTORE_PS_BIN", "").strip()

    async def fault(point: str) -> None:
        if crash_at and point == crash_at:
            log.warning("assistant restore test crash at %s", point)
            os._exit(86)
        if pause_at and point == pause_at and pause_file:
            log.warning("assistant restore test pause at %s", point)
            while not os.path.exists(pause_file):
                await asyncio.sleep(0.1)

    async def ps_runner(pid: str) -> tuple[int, str, str]:
        return await run_ps(pid, ps_bin=ps_bin)

    return (fault if (crash_at or pause_at) else None), (ps_runner if ps_bin else None)


class AssistantRestore:
    """Single owner of one composite's restore episodes."""

    def __init__(
        self,
        *,
        store: Any,
        sessions: Any,
        spawnctl: Any,
        composite: Any,
        local_host: str,
        auto_enabled: bool,
        inhibit_path: Path | None,
        flush_composite_tells: Callable[[Any], Awaitable[int]],
        deliver_notice: Callable[[Any, str, str], Awaitable[None]],
        broadcast: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        ps_runner: Callable[[str], Awaitable[tuple[int, str, str]]] | None = None,
        clock: Callable[[], datetime] = _now,
        fault: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.spawnctl = spawnctl
        self.composite = composite
        self.local_host = local_host
        self.auto_enabled = auto_enabled
        self.inhibit_path = inhibit_path
        self._flush = flush_composite_tells
        self._deliver_notice = deliver_notice
        self._broadcast = broadcast
        self._ps = ps_runner or run_ps
        self._clock = clock
        self._fault = fault
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        # Last death-proof finding with no episode: (state, reason).
        self._last_result: tuple[str, str | None] | None = None
        self._emitted: tuple[str, str | None] | None = None
        self._baselined = False
        # Spawn keys this process already sent, so an admitted-but-unrecorded
        # request is never sent twice by one daemon instance.
        self._sent: set[str] = set()

    @property
    def name(self) -> str:
        return str(self.composite.config.name)

    # -- triggers -------------------------------------------------------------

    def inhibited(self) -> bool:
        try:
            return bool(self.inhibit_path is not None and self.inhibit_path.exists())
        except OSError:
            # An unreadable marker is treated as present: maintenance wins.
            return True

    async def tick(self) -> None:
        """Reconciler callback: never blocks the reconcile pass.

        A resume waits for the seat's boot-ready proof (up to minutes), so the
        work runs in one tracked background task; a pass that finds it still
        running does nothing.
        """
        if self._task is not None and not self._task.done():
            return
        if not self.auto_enabled and await self.store.restore_active_episode(name=self.name) is None:
            # Automatic trigger off and nothing admitted: one read, no work.
            return
        self._task = asyncio.create_task(self._run("auto"), name="assistant-restore")

    async def _run(self, trigger: str) -> None:
        try:
            await self.advance(trigger)
        except Exception:  # noqa: BLE001 - a restore crash must not break the reconciler
            log.exception("assistant restore pass failed name=%s", self.name)

    async def drain(self) -> None:
        """Await the in-flight pass (shutdown / test synchronisation)."""
        if self._task is not None and not self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)

    async def advance(self, trigger: str = "auto") -> dict[str, Any]:
        """Move the episode as far as it can go now; return the status object."""
        async with self._lock:
            await self._baseline()
            for _ in range(16):
                if not await self._step():
                    break
                # One event per persisted change, not one per pass.
                await self._emit()
            return await self._emit()

    # -- status ---------------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        episode = await self.store.restore_active_episode(name=self.name)
        state: str
        reason: str | None
        if episode is not None:
            state, reason = str(episode["state"]), episode.get("reason")
            if state == "pending" and self._held(episode):
                state = "suspended"
        else:
            episode = await self._episode_for_current_binding()
            if episode is not None:
                state, reason = str(episode["state"]), episode.get("reason")
            elif not self.auto_enabled:
                state, reason = "disabled", None
            elif self.inhibited():
                state, reason = "suspended", None
            elif self._last_result is not None:
                state, reason = self._last_result
            else:
                state, reason = "healthy", None
        return {
            "state": state,
            "reason": reason,
            "episode_id": int(episode["episode_id"]) if episode else None,
            "trigger": episode.get("trigger") if episode else None,
            "attempt_seq": int(episode["attempt_seq"]) if episode else 0,
            "budget_used": int(episode["budget_used"]) if episode else 0,
            "max_attempts": MAX_ATTEMPTS,
            "next_attempt_at": episode.get("next_attempt_at") if episode else None,
            "predecessor_generation": episode.get("generation") if episode else None,
            "updated_at": episode.get("updated_at") if episode else _iso(self._clock()),
        }

    async def _episode_for_current_binding(self) -> dict[str, Any] | None:
        try:
            binding = await self.composite.binding()
        except ValueError:
            return None
        stream_id = str(binding.get("stream_id") or "")
        generation = str(binding.get("generation") or "")
        if not stream_id or not generation:
            return None
        return await self.store.restore_episode_for_binding(
            name=self.name, stream_id=stream_id, generation=generation,
        )

    def _held(self, episode: dict[str, Any]) -> bool:
        return episode.get("trigger") == "auto" and (not self.auto_enabled or self.inhibited())

    async def _baseline(self) -> None:
        # The state found when this daemon first looks is not a change.
        if not self._baselined:
            status = await self.status()
            self._emitted = (status["state"], status["reason"])
            self._baselined = True

    async def _emit(self) -> dict[str, Any]:
        status = await self.status()
        key = (status["state"], status["reason"])
        if key != self._emitted:
            self._emitted = key
            log.warning(
                "assistant restore name=%s state=%s reason=%s episode=%s attempt=%s",
                self.name, status["state"], status["reason"], status["episode_id"],
                status["attempt_seq"],
            )
            if self._broadcast is not None:
                try:
                    await self._broadcast({
                        "type": "assistant.restore.changed", "name": self.name, "restore": status,
                    })
                except Exception:  # noqa: BLE001 - a client fan-out failure changes nothing
                    log.exception("assistant restore broadcast failed name=%s", self.name)
        return status

    # -- death proof ----------------------------------------------------------

    async def death_proof(
        self, stream_id: str, expected_generation: str, episode: dict[str, Any] | None = None,
    ) -> tuple[bool, str, str | None, dict[str, Any] | None]:
        """Return ``(proven, state, reason, row)`` for the bound seat.

        Proven only when the row is the expected generation of a restorable
        seat, its close (if any) was a recorded pane death, a fresh tmux probe
        says the pane is gone and an explicit ``ps`` observation says the
        recorded pane process is gone.
        """
        host, _, session_name = stream_id.partition(":")
        if host != self.local_host:
            return False, "ineligible", "remote_holder", None
        row = await self.store.fetch_session(host, session_name)
        if row is None:
            return False, "ineligible", "holder_unknown", None
        if str(row.get("provider") or "") != "claude" or not str(row.get("claude_session_id") or ""):
            return False, "ineligible", "not_resumable", row
        policy = getattr(self.sessions, "assistant", None)
        if policy is not None and policy.protects(row):
            return False, "ineligible", "protected_row", row
        if str(row.get("session_generation") or "") != expected_generation:
            return False, "ineligible", "generation_mismatch", row
        is_open = str(row.get("status") or "") == "open"
        own_rollback = False
        if not is_open:
            reconciler_close = bool(
                str(row.get("presumed_dead_at") or "").strip()
                and str(row.get("dead_open_closed_at") or "").strip()
                and row.get("dead_open_closed_at") == row.get("closed_at")
                and row.get("close_kind") in (None, "")
            )
            own_rollback = bool(
                episode is not None
                and str(row.get("close_kind") or "") == "spawn_rollback"
                and expected_generation == str(episode.get("last_generation") or "")
                and expected_generation != str(episode.get("generation") or "")
            )
            if not (reconciler_close or own_rollback):
                return False, "ineligible", "intentional_close", row
        pane = await self._pane_state(session_name)
        if pane == "alive":
            if is_open:
                return False, "healthy", None, row
            return False, "ineligible", "holder_revived", row
        if pane != "gone":
            return False, "waiting_evidence", "pane_probe_unknown", row
        process, reason = await self._process_state(row, expected_generation, episode, own_rollback)
        if process == "alive":
            return False, "waiting_evidence", "holder_process_alive", row
        if process != "gone":
            return False, "waiting_evidence", reason, row
        return True, "pending", None, row

    async def _pane_state(self, session_name: str) -> str:
        probe = getattr(getattr(self.sessions, "tmux", None), "session_state", None)
        if not callable(probe):
            return "unknown"
        try:
            state = str(await probe(session_name) or "")
        except Exception:  # noqa: BLE001 - an unreadable pane is unknown, never gone
            return "unknown"
        return state if state in {"alive", "gone"} else "unknown"

    async def _process_state(
        self, row: dict[str, Any], expected_generation: str,
        episode: dict[str, Any] | None, own_rollback: bool,
    ) -> tuple[str, str | None]:
        binding = row.get("observer_binding")
        if isinstance(binding, str):
            try:
                binding = json.loads(binding)
            except ValueError:
                binding = None
        pid = str((binding or {}).get("pane_pid") or "").strip() if isinstance(binding, dict) else ""
        started = str((binding or {}).get("pane_started_at") or "").strip() if isinstance(binding, dict) else ""
        recorded = str((binding or {}).get("generation") or "") if isinstance(binding, dict) else ""
        if not pid or not started or not pid.isdigit() or recorded != expected_generation:
            if own_rollback and episode is not None and await self._attempt_recorded_failed(row, episode):
                # A boot rolled back before it recorded a pane process: the
                # stored failed outcome is the durable record of that rollback.
                return "gone", None
            return "unknown", "pane_identity_unrecorded"
        try:
            rc, out, err = await self._ps(pid)
        except Exception:  # noqa: BLE001 - missing ps, timeout, permission: unknown
            return "unknown", "process_probe_unknown"
        result = classify_ps(rc, out, err, started)
        return result, (None if result != "unknown" else "process_probe_unknown")

    async def _attempt_recorded_failed(self, row: dict[str, Any], episode: dict[str, Any]) -> bool:
        outcomes, reservations = await self._spawn_records(str(episode.get("spawn_key") or ""))
        return bool(not reservations and any(
            str(o.get("state") or "") in _FAILED_OUTCOME_STATES for o in outcomes
        ))

    # -- driver ---------------------------------------------------------------

    async def _step(self) -> bool:
        episode = await self.store.restore_active_episode(name=self.name)
        if episode is None:
            if not self.auto_enabled or self.inhibited():
                return False
            return await self._create("auto") is not None
        state = episode["state"]
        if state == "pending":
            return await self._start_attempt(episode)
        if state == "spawning":
            return await self._resolve_attempt(episode)
        if state == "spawned":
            return await self._bind(episode)
        if state == "bound":
            return await self._complete_routing(episode)
        return False

    async def _binding(self) -> dict[str, Any] | None:
        config = self.composite.config
        if not getattr(config, "enabled", False) or not getattr(config, "direct_primary", False):
            return None
        try:
            binding = await self.composite.binding()
        except ValueError:
            return None
        if not binding.get("stream_id") or not binding.get("generation"):
            return None
        return binding

    async def _create(self, trigger: str, request_id: str | None = None) -> dict[str, Any] | None:
        binding = await self._binding()
        if binding is None:
            self._last_result = ("ineligible", "not_direct_primary")
            return None
        stream_id, generation = str(binding["stream_id"]), str(binding["generation"])
        proven, state, reason, row = await self.death_proof(stream_id, generation)
        self._last_result = (state, reason)
        if not proven or row is None:
            return None
        self._last_result = None
        # The proof awaited probes; a marker placed meanwhile still wins.
        if trigger == "auto" and (not self.auto_enabled or self.inhibited()):
            return None
        return await self.store.restore_create_episode(
            name=self.name, stream_id=stream_id, generation=generation,
            expected_revision=int(binding.get("revision") or 0),
            claude_session_id=str(row["claude_session_id"]), provider="claude",
            model=str(row.get("effective_model") or row.get("requested_model") or ""),
            effort=str(row.get("effective_effort") or row.get("requested_effort") or ""),
            trigger=trigger, request_id=request_id,
        )

    async def _start_attempt(self, episode: dict[str, Any]) -> bool:
        now = self._clock()
        due = _parse_iso(episode.get("next_attempt_at"))
        if due is not None and now < due:
            return False
        if self._held(episode):
            return False
        proven, _state, _reason, _row = await self.death_proof(
            str(episode["stream_id"]), str(episode["last_generation"]), episode,
        )
        if not proven:
            return False
        binding = await self._binding()
        if (binding is None or binding.get("stream_id") != episode["stream_id"]
                or binding.get("generation") != episode["generation"]
                or int(binding.get("revision") or 0) != int(episode["expected_revision"])):
            moved = await self.store.restore_transition(
                episode_id=episode["episode_id"], expect_state="pending",
                expect_attempt_seq=episode["attempt_seq"],
                fields={"state": "superseded", "reason": "binding_moved"},
                audits=[("superseded", "binding_moved", {"stage": "before_spawn"})],
            )
            return moved is not None
        # The inhibit marker is read again right before the commit that makes
        # the attempt real, so a marker placed after the pass began still wins.
        if self._held(episode):
            return False
        attempt_seq = int(episode["attempt_seq"]) + 1
        generation = uuid.uuid4().hex
        spawn_key = f"assistant-restore:{episode['episode_id']}:{attempt_seq}"
        started = await self.store.restore_transition(
            episode_id=episode["episode_id"], expect_state="pending",
            expect_attempt_seq=episode["attempt_seq"],
            fields={
                "state": "spawning", "reason": None, "attempt_seq": attempt_seq,
                "budget_used": int(episode["budget_used"]) + 1, "spawn_key": spawn_key,
                "attempt_generation": generation, "attempt_started_at": _iso(now),
            },
            audits=[("attempt_started", None, {
                "spawn_key": spawn_key, "attempt_generation": generation,
                "budget_epoch": int(episode["budget_epoch"]),
            })],
        )
        if started is None:
            return False
        await self._hook("before_spawn")
        return True

    def _spawn_message(self, episode: dict[str, Any]) -> dict[str, Any]:
        return {
            # Internal daemon identity; never accepted from a client payload.
            "_auth_context": {"service_authenticated": True, "service_actor": SERVICE_ACTOR},
            "_session_generation": episode["attempt_generation"],
            "type": "spawn",
            "request_id": episode["spawn_key"],
            "idempotency_key": episode["spawn_key"],
            "host": self.local_host,
            "provider": episode["provider"],
            "model": episode["model"] or None,
            "effort": episode["effort"] or None,
            "resume_session_id": episode["claude_session_id"],
        }

    async def _spawn_records(self, spawn_key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not spawn_key:
            return [], []
        status = await self.spawnctl.spawn_status({"target": spawn_key, "host": self.local_host},
                                                  self.local_host)
        # An outcome row is keyed by stream name and may belong to an earlier
        # request that used the name; only rows carrying this key count.
        outcomes = [o for o in status.get("outcomes") or []
                    if str(o.get("idempotency_key") or "") == spawn_key
                    or str(o.get("request_id") or "") == spawn_key]
        reservations = [r for r in status.get("reservations") or []
                        if str(r.get("idempotency_key") or "") == spawn_key
                        or str(r.get("request_id") or "") == spawn_key]
        return outcomes, reservations

    async def _resolve_attempt(self, episode: dict[str, Any]) -> bool:
        """Learn the outcome of the current attempt from durable state.

        The request is never re-sent once it may have been admitted: a resume
        of a live seat is refused before the idempotency lookup, so the row at
        this attempt's own pre-recorded generation is the proof instead.
        """
        host, _, session_name = str(episode["stream_id"]).partition(":")
        spawn_key = str(episode["spawn_key"] or "")
        attempt_generation = str(episode["attempt_generation"] or "")
        previous = str(episode["last_generation"] or "")
        row = await self.store.fetch_session(host, session_name) or {}
        row_generation = str(row.get("session_generation") or "")
        is_open = str(row.get("status") or "") == "open"
        outcomes, reservations = await self._spawn_records(spawn_key)

        if row_generation == attempt_generation and is_open:
            if (str(row.get("bootstrap_state") or "") == "ready"
                    and await self._pane_state(session_name) == "alive"):
                await self._hook("after_spawn")
                done = await self.store.restore_transition(
                    episode_id=episode["episode_id"], expect_state="spawning",
                    expect_attempt_seq=episode["attempt_seq"],
                    fields={"state": "spawned", "reason": None,
                            "last_generation": attempt_generation},
                    audits=[("attempt_outcome", "ok", {"generation": attempt_generation})],
                )
                return done is not None
            return await self._unresolved(episode)
        if row_generation == attempt_generation:
            reason = next((str(o.get("reason") or "") for o in outcomes if o.get("reason")), "")
            return await self._fail_attempt(
                episode, (reason.split(":", 1)[0].strip() or "holder_lost"),
                last_generation=attempt_generation,
            )
        failed = [o for o in outcomes if str(o.get("state") or "") in _FAILED_OUTCOME_STATES]
        if failed and not reservations and row_generation == previous:
            reason = str(failed[0].get("reason") or failed[0].get("state") or "spawn_failed")
            return await self._fail_attempt(episode, reason.split(":", 1)[0].strip() or "spawn_failed")
        if is_open and row_generation not in {attempt_generation, previous}:
            ended = await self.store.restore_transition(
                episode_id=episode["episode_id"], expect_state="spawning",
                expect_attempt_seq=episode["attempt_seq"],
                fields={"state": "superseded", "reason": "holder_resumed_externally"},
                audits=[("superseded", "holder_resumed_externally",
                         {"row_generation": row_generation})],
            )
            return ended is not None
        never_admitted = (
            not outcomes and not reservations and row_generation == previous
            and not (is_open and await self._pane_state(session_name) == "alive")
        )
        if never_admitted and spawn_key not in self._sent:
            self._sent.add(spawn_key)
            try:
                reply = await self.spawnctl.spawn(self._spawn_message(episode), self.local_host)
            except Exception as exc:  # noqa: BLE001 - classified below
                code = getattr(exc, "code", None)
                if code:
                    return await self._fail_sent_attempt(episode, str(code))
                log.exception("assistant restore spawn raised name=%s key=%s", self.name, spawn_key)
                return True
            if isinstance(reply, dict) and reply.get("type") != "spawn.ok":
                return await self._fail_sent_attempt(
                    episode, str(reply.get("error_code") or "spawn_failed"))
            return True
        return await self._unresolved(episode)

    async def _fail_sent_attempt(self, episode: dict[str, Any], reason: str) -> bool:
        """Fail an attempt whose request just returned an error.

        A boot that opened the row before failing leaves it at this attempt's
        generation; the episode then owns that generation for its next proof.
        """
        host, _, session_name = str(episode["stream_id"]).partition(":")
        row = await self.store.fetch_session(host, session_name) or {}
        opened = str(row.get("session_generation") or "") == str(episode["attempt_generation"] or "")
        if opened and str(row.get("status") or "") == "open":
            # Still open at this attempt's generation: not a failure to record
            # yet; the next pass resolves it from the row.
            return True
        return await self._fail_attempt(
            episode, reason,
            last_generation=str(episode["attempt_generation"]) if opened else None,
        )

    async def _unresolved(self, episode: dict[str, Any]) -> bool:
        await self.store.restore_note_uncertain(
            episode_id=episode["episode_id"], attempt_seq=episode["attempt_seq"],
        )
        started = _parse_iso(episode.get("attempt_started_at"))
        if started is not None and (self._clock() - started).total_seconds() >= UNRESOLVED_LIMIT_S:
            # Never start another attempt on top of one whose outcome is unknown.
            ended = await self.store.restore_transition(
                episode_id=episode["episode_id"], expect_state="spawning",
                expect_attempt_seq=episode["attempt_seq"],
                fields={"state": "degraded", "reason": "attempt_unresolved"},
                audits=[("attempt_outcome", "attempt_unresolved", {})],
            )
            return ended is not None
        return False

    async def _attempt_still_unresolved(self, episode: dict[str, Any]) -> bool:
        """True while a degraded ``attempt_unresolved`` episode's request is open.

        A retry never starts an attempt on top of one whose outcome is unknown:
        the recorded key must have no reservation and a failed outcome first.
        """
        if episode.get("state") != "degraded" or episode.get("reason") != "attempt_unresolved":
            return False
        outcomes, reservations = await self._spawn_records(str(episode.get("spawn_key") or ""))
        return bool(reservations or not outcomes or any(
            str(o.get("state") or "") not in _FAILED_OUTCOME_STATES for o in outcomes
        ))

    async def _fail_attempt(
        self, episode: dict[str, Any], reason: str, *, last_generation: str | None = None,
        expect_state: str = "spawning",
    ) -> bool:
        used = int(episode["budget_used"])
        fields: dict[str, Any] = {"reason": reason}
        if last_generation:
            fields["last_generation"] = last_generation
        audits: list[tuple[str, str | None, dict[str, Any]]] = [
            ("attempt_outcome", reason, {"budget_used": used}),
        ]
        if used < MAX_ATTEMPTS:
            delay = BACKOFFS_S[min(max(used, 1), len(BACKOFFS_S)) - 1]
            fields.update(state="pending",
                          next_attempt_at=_iso(self._clock() + timedelta(seconds=delay)))
        else:
            fields.update(state="degraded")
            audits.append(("exhausted", reason, {"budget_epoch": int(episode["budget_epoch"])}))
        moved = await self.store.restore_transition(
            episode_id=episode["episode_id"], expect_state=expect_state,
            expect_attempt_seq=episode["attempt_seq"], fields=fields, audits=audits,
        )
        return moved is not None

    async def _bind(self, episode: dict[str, Any]) -> bool:
        result = await self.store.restore_assistant_binding(
            episode_id=episode["episode_id"], env_binding=self.composite._env_binding(),
        )
        if result["outcome"] == "holder_lost":
            return await self._fail_attempt(episode, "holder_lost", expect_state="spawned")
        if result["outcome"] in {"ok", "duplicate"}:
            await self._hook("after_bind")
        return True

    async def _complete_routing(self, episode: dict[str, Any]) -> bool:
        """Point live routing at the current binding; every step is repeatable.

        An operator rebind after the bind is an ordinary hot rebind: this acts
        on whatever the composite's binding is now and still ends ``restored``.
        """
        composite = self.composite
        await composite.load_binding()
        binding = await composite.binding()
        broadcast = getattr(composite, "broadcast", None)
        if broadcast is not None:
            await broadcast({
                "type": "assistant.binding.changed",
                "stream_id": binding.get("stream_id"), "generation": binding.get("generation"),
            })
        await self._flush(composite)
        await composite.recover()
        wake = getattr(composite, "wake_route_worker", None)
        if callable(wake):
            wake()
        revision = int(episode["expected_revision"]) + 1
        await self._deliver_notice(
            composite,
            f"Seat restored after a host restart; binding revision {revision}. "
            "The lifecycle-manager grant is not restored.",
            f"assistant-restore-notice:{episode['episode_id']}",
        )
        await self._hook("during_routing")
        done = await self.store.restore_transition(
            episode_id=episode["episode_id"], expect_state="bound",
            expect_attempt_seq=episode["attempt_seq"],
            fields={"state": "restored", "reason": None},
            audits=[("routing_completed", "ok", {"binding_revision": revision})],
        )
        return done is not None

    async def _hook(self, point: str) -> None:
        if self._fault is not None:
            await self._fault(point)

    # -- operator verb --------------------------------------------------------

    async def manual(self, action: str, request_id: str) -> dict[str, Any]:
        """Operator ``restore`` / ``retry``; idempotent by ``request_id``."""
        async with self._lock:
            await self._baseline()
            prior = await self.store.restore_request_receipt(request_id=request_id)
            if prior is not None:
                return {"duplicate": True, "restore": await self.status()}
            if action == "retry":
                episode = await self._episode_for_current_binding()
                if episode is not None and await self._attempt_still_unresolved(episode):
                    return {"error_code": "assistant_restore_attempt_unresolved"}
                result = (
                    await self.store.restore_reset_budget(
                        episode_id=episode["episode_id"], request_id=request_id,
                        env_binding=self.composite._env_binding(),
                    ) if episode is not None else {"error_code": "assistant_restore_not_degraded"}
                )
                if result.get("error_code"):
                    return {"error_code": result["error_code"]}
                if result.get("duplicate"):
                    return {"duplicate": True, "restore": await self.status()}
            else:
                episode = await self.store.restore_active_episode(name=self.name)
                created = None if episode is not None else await self._create("manual", request_id)
                if created is None:
                    current = episode or await self._episode_for_current_binding()
                    await self.store.restore_record_request(
                        request_id=request_id,
                        episode_id=int(current["episode_id"]) if current else None,
                        outcome="restore",
                        detail={"action": "restore", "effect": "none",
                                "episode_id": int(current["episode_id"]) if current else None,
                                "result": list(self._last_result) if self._last_result else None},
                    )
            status = await self._emit()
        # The reply does not wait for the resume; the same background pass the
        # reconciler uses carries the episode on.
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run("manual"), name="assistant-restore")
        return {"duplicate": False, "restore": status}
