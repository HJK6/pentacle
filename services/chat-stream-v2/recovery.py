"""Daff seat recovery (E): a single daemon-owned owner that respawns the Daff
assistant when its bound pane dies, rebinds, and replays queued input.

Bart is never recovered here — the hook declines any non-Daff row, and Bart's
preserve-and-do-not-spawn path in the reconciler/sessions is untouched.

Continuity note: recovery uses a handoff spawn, which supersedes a plain Claude
``--resume`` (the resume path refuses a role/handoff spawn).  The Daff chat
history lives in the daemon store and survives the seat change regardless, so a
fresh handoff seat is functionally continuous; a true ``--resume`` would need a
separate non-handoff launch and is out of scope here.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Callable

log = logging.getLogger("chat_streamd_v2.recovery")


class DaffRecovery:
    #: Backoffs between the 3 retries that follow the initial attempt.
    BACKOFFS = (30.0, 120.0, 600.0)

    def __init__(
        self,
        *,
        sessions: Any,
        spawnctl: Any,
        store: Any,
        local_host: str,
        composites: Callable[[], dict[str, Any]],
        flush_composite_tells: Callable[[Any], Awaitable[int]],
        tell_bart: Callable[[str], Awaitable[None]],
        startup_prompt: str,
        model: str = "claude-opus-5-5",
        effort: str = "high",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessions = sessions
        self.spawnctl = spawnctl
        self.store = store
        self.local_host = local_host
        self._composites = composites
        self._flush = flush_composite_tells
        self._tell_bart = tell_bart
        self.startup_prompt = startup_prompt
        self.model = model
        self.effort = effort
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._inflight = False

    def _daff_composite(self) -> Any:
        return (self._composites() or {}).get("daff")

    def _daff_role(self) -> str | None:
        policy = getattr(self.sessions, "assistant", None)
        roles = sorted(getattr(policy, "extra_roles", set()) or set()) if policy else []
        return roles[0] if roles else None

    def _is_daff_row(self, row: dict[str, Any]) -> bool:
        role = self._daff_role()
        return bool(role and row.get("role") == role)

    async def on_dead(self, row: dict[str, Any]) -> None:
        """Reconciler hook: respawn only the Daff seat; decline everything else."""
        if not self._is_daff_row(row):
            return
        daff = self._daff_composite()
        if daff is None or not getattr(daff, "enabled", False):
            return
        if self._inflight:  # one recovery at a time -> never a duplicate seat
            return
        self._inflight = True
        try:
            await self._recover(row, daff)
        finally:
            self._inflight = False

    async def _recover(self, row: dict[str, Any], daff: Any) -> bool:
        predecessor = f"{row['host']}:{row['session_name']}"
        for index, backoff in enumerate((0.0, *self.BACKOFFS)):
            if backoff:
                await self._sleep(backoff)
            async with self._lock:
                if await self._already_healthy(daff):
                    return True
                resp = await self._spawn(row)
                if resp.get("type") == "spawn.ok":
                    new_sid = str(resp.get("stream_id") or "")
                    new_gen = str((resp.get("session") or {}).get("session_generation") or "")
                    await self.store.recover_assistant_binding(
                        name=daff.config.name, target_stream_id=new_sid, target_generation=new_gen,
                    )
                    await daff.load_binding()
                    if daff.broadcast is not None:
                        bound = await daff.binding()
                        await daff.broadcast({
                            "type": "assistant.binding.changed",
                            "stream_id": bound.get("stream_id"), "generation": bound.get("generation"),
                        })
                    # Deliver input queued while unbound, in order, then re-queue
                    # and wake the composite's own ordered worker for any routes.
                    await self._flush(daff)
                    await daff.recover()
                    log.info("daff recovery succeeded predecessor=%s new=%s", predecessor, new_sid)
                    return True
                log.warning("daff recovery attempt %d failed code=%s", index, resp.get("error_code"))
        # Exhausted: degrade and notify Bart exactly once.
        try:
            await self.sessions.set_status_card(
                row["host"], row["session_name"],
                {"update": "DEGRADED: automatic Daff recovery failed after 3 retries."},
            )
        except Exception:  # noqa: BLE001 - the degraded tell still goes out
            log.exception("daff degraded status card failed")
        await self._tell_bart(
            "daff:assistant is degraded: automatic recovery failed after 3 retries; "
            "manual attention needed.",
        )
        log.error("daff recovery exhausted; marked degraded predecessor=%s", predecessor)
        return False

    async def _already_healthy(self, daff: Any) -> bool:
        """A late pane revival (restore_reconciled) may have fixed it already."""
        bound = await daff.binding()
        target = str(bound.get("stream_id") or "")
        if not target:
            return False
        host, _, name = target.partition(":")
        seat = await self.store.fetch_session(host, name)
        # A preserved-dead seat keeps status 'open' but carries presumed_dead_at;
        # liveness is not reflected in the stored pane_status, so gate on that.
        return bool(
            seat and seat.get("status") == "open" and not seat.get("presumed_dead_at")
            and not seat.get("closed_at")
            and str(seat.get("session_generation") or "") == str(bound.get("generation") or "")
        )

    async def _spawn(self, row: dict[str, Any]) -> dict[str, Any]:
        predecessor = f"{row['host']}:{row['session_name']}"
        msg = {
            # Internal daemon identity; never accepted from a client payload.
            "_auth_context": {
                "service_authenticated": True, "service_actor": "daemon:scheduler",
                "token_verified": True, "stream_id": predecessor,
                "session_generation": row.get("session_generation") or row.get("created_at"),
            },
            "type": "spawn",
            "request_id": f"daff-recovery-{uuid.uuid4().hex}",
            "idempotency_key": f"daff-recovery-{predecessor}-{uuid.uuid4().hex[:8]}",
            "host": self.local_host,
            "provider": "claude", "model": self.model, "effort": self.effort,
            "role": row.get("role"),
            "handoff_from_stream_id": predecessor, "handoff": True,
            "initial_prompt": self.startup_prompt,
        }
        try:
            return await self.spawnctl.spawn(msg, self.local_host)
        except Exception as exc:  # noqa: BLE001 - absence of spawn.ok is a retryable failure
            return {"type": "spawn.error", "error_code": getattr(exc, "code", "spawn_failed"),
                    "error": str(exc)}
