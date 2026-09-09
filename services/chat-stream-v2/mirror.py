"""mirror.py — bounded local-pane observation.

The mirror iterates the O(open) local registry, captures panes through async
tmux transport, and publishes observed preview/working state. It never scans a
whole tmux server, makes a lifecycle verdict, closes a row, or kills a pane;
those decisions remain with the lifecycle and reconciler paths. Its background
loop has a cadence, cap, backoff, and `--disable-mirror` kill switch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from inventory import InventoryEmitter
from v2_runtime import env_number

log = logging.getLogger("chat_streamd_v2.mirror")


# --------------------------------------------------------------------------- #
# Lifted pane parsers (v1 chat_streamd.py — verbatim except where noted)
# --------------------------------------------------------------------------- #


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text or "")


def _normalize_pane(text: str) -> str:
    cleaned = _strip_ansi(text).replace("\r", "").replace(" ", " ")
    lines = [line.rstrip() for line in cleaned.split("\n")]
    return "\n".join(lines).strip()


def _extract_preview_line(pane: str) -> str:
    """Last non-blank line from a tmux pane capture. Cheap — operates on
    already-captured pane text in memory; no extra tmux call."""
    if not pane:
        return ""
    for line in reversed(pane.splitlines()):
        stripped = line.rstrip()
        if stripped:
            return stripped
    return ""


def _provider_from_session_name(session_name: str) -> str | None:
    if session_name.startswith(("codex-", "viewer-codex-")):
        return "codex"
    if session_name.startswith(("claude-", "viewer-")):
        return "claude"
    return None


# Claude Code v5 animates the spinner through seven glyph frames; six are the
# ✢✽✶✻✳· sparkles, the seventh is a plain ASCII ``*``. A pane capture that lands
# on the ``*`` frame yields zero spinner candidates, so the turn parses idle for
# that frame — harmless on a local seat (the miss self-heals within the
# genuine-activity TTL), but on a remote SSH-captured seat a ``*``-frame miss
# after the TTL cleared working AND dropped the row from the fast-refresh hot
# set, stranding working=false for the rest of the turn (remote-host early clear).
_CLAUDE_SPINNER_RE = re.compile(r"^[✢✽✶✻✳·*]\s+(.+?)\s*$")
_CLAUDE_PROMPT_RE = re.compile(r"^\s*[❯>›]\s*$")
_CLAUDE_ACTIVE_HINT_RE = re.compile(r"(?:…|\.\.\.|thinking\)|still thinking)", re.IGNORECASE)
_CLAUDE_TIMER_RE = re.compile(r"\([^)]*\d+\s*[hms]\b[^)]*\)", re.IGNORECASE)


def _extract_claude_working_status(lines: list[str]) -> tuple[bool, str]:
    """Extract one current Claude Code v5 spinner from a bounded pane region.

    Claude leaves completed spinner labels in scrollback, so an idle input
    marker bounds the status region. While a turn is active the input marker is
    absent and the bounded tail accepts exactly one current spinner candidate.
    Requiring both an elapsed parenthesized timer and an active hint prevents
    completed labels such as ``Baked for 4m 52s`` from becoming live state.
    """
    nonempty = [(index, line) for index, line in enumerate(lines) if line.strip()]
    prompt_slot = next(
        (slot for slot in range(len(nonempty) - 1, -1, -1)
         if _CLAUDE_PROMPT_RE.match(nonempty[slot][1])),
        None,
    )
    if prompt_slot is None:
        status_lines = nonempty[-12:]
    else:
        status_lines = nonempty[max(0, prompt_slot - 12):prompt_slot]
    candidates = [
        match.group(1).strip()
        for _, line in status_lines
        if (match := _CLAUDE_SPINNER_RE.match(line))
    ]
    # A completed turn leaves its final spinner label (e.g. "Cooked for 48s ·
    # done 3:17 PM") in scrollback: a glyph line with neither a parenthesized
    # elapsed timer nor an active hint. Require both to isolate the one *live*
    # spinner first, so a stale completed label sharing the bounded region can
    # never collide with the count and reject the turn.
    live = [
        label for label in candidates
        if _CLAUDE_TIMER_RE.search(label) and _CLAUDE_ACTIVE_HINT_RE.search(label)
    ]
    if len(live) != 1:
        return False, ""
    return True, live[0]


def _extract_codex_working_status(normalized: list[str]) -> tuple[bool, str]:
    """Keep the lifted Codex ``Working (`` / waiting behavior unchanged."""
    status_line = ""
    prompt_seen = False
    distance_after_prompt = 0
    for line in reversed(normalized[-8:]):
        if re.match(r"^[❯›]\s*$", line):
            prompt_seen = True
            continue
        if line.startswith("Working (") or line.startswith("Waiting for background terminal ("):
            if not prompt_seen or distance_after_prompt <= 2:
                status_line = line
            break
        if prompt_seen:
            distance_after_prompt += 1
    if not status_line:
        return False, ""
    match = re.match(r"^(?:Working|Waiting for background terminal) \(([^)]*)", status_line)
    elapsed = match.group(1).split("•")[0].strip() if match else ""
    if status_line.startswith("Waiting for background terminal"):
        return True, f"Waiting for background terminal {elapsed}".strip()
    return True, f"Working {elapsed}".strip()


def _extract_live_state(pane_text: str, provider: str) -> dict[str, object]:
    """Capture-derived live state for Claude and Codex panes.

    The `text` (unsubmitted draft) and `question`
    fields are left empty here on purpose — their v1 parsers (`_extract_live_draft`
    and `parse_pane_question`) pull in large subtrees owned by other subsystems
    (draft parser / questions, lane 6) and are additive follow-ups; the
    working-state core does not depend on them."""
    text = _normalize_pane(pane_text)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    normalized = [re.sub(r"^[•⎿]\s+", "", line) for line in lines]
    last_line = normalized[-1] if normalized else ""
    if str(provider or "").lower() == "claude":
        is_working, working_label = _extract_claude_working_status(normalized)
        # Explicit-command smoke panes have no provider metadata and use the
        # existing Codex-shaped stub status. Preserve that contract while the
        # real Claude v5 spinner takes precedence when present.
        if not is_working:
            is_working, working_label = _extract_codex_working_status(normalized)
    else:
        is_working, working_label = _extract_codex_working_status(normalized)
    return {
        "text": "",            # draft parser not lifted (see docstring)
        "pending": last_line.startswith("Messages to be submitted after next tool call"),
        "working": is_working,
        "working_label": working_label,
        "question": None,      # pane_question parser not lifted (lane 6)
    }


# --------------------------------------------------------------------------- #
# Pump scheduling — REDESIGNED (four loop rules; no poll-based eviction)
# --------------------------------------------------------------------------- #

DEFAULT_INTERVAL_S = 10.0
#: v1 `INVENTORY_BROADCAST_MIN_INTERVAL_S` default — throttle pure field-churn
#: rebroadcasts so a working fleet cannot degenerate into a full-inventory push
#: every tick. A membership change (spawn/close) always bypasses it.
DEFAULT_INVENTORY_MIN_INTERVAL_S = 2.0
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_MAX_S = 30.0
ENV_PREFIX = "PENTACLE_MIRROR_"


@dataclass
class MirrorConfig:
    """Loop-rule knobs (v2_design.md § Event loop rules, rule 2).

    cadence      `interval_s`, default 10s, env `PENTACLE_MIRROR_INTERVAL_S`
    per-pass work exactly one `tmux list-panes` for the local socket
    backoff      exponential from `backoff_base_s`, capped at `backoff_max_s`
    kill switch  `--disable-mirror` (main.py never constructs the job)
    """

    interval_s: float = DEFAULT_INTERVAL_S
    #: None => wait a full interval before the first pass. Tests set this to a
    #: fraction of a second; it is the "forced pass" trigger for a real daemon
    #: process, so no test-only RPC verb has to exist on the wire.
    first_delay_s: float | None = None
    inventory_min_interval_s: float = DEFAULT_INVENTORY_MIN_INTERVAL_S
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S

    @classmethod
    def from_env(cls, env: dict | None = None) -> "MirrorConfig":
        e = os.environ if env is None else env
        return cls(
            interval_s=env_number(
                e, "INTERVAL_S", DEFAULT_INTERVAL_S, float, prefix=ENV_PREFIX,
            ),
            first_delay_s=env_number(
                e, "FIRST_DELAY_S", None, float, prefix=ENV_PREFIX,
            ),
            inventory_min_interval_s=env_number(
                e, "INVENTORY_MIN_INTERVAL_S", DEFAULT_INVENTORY_MIN_INTERVAL_S, float,
                prefix=ENV_PREFIX,
            ),
        )


@dataclass
class _Obs:
    """Per-stream liveness state; semantic fields never enter this pump."""

    pane_pid: str = ""
    online: bool = True
    session_generation: str = ""
    seen: bool = False
    died_emitted: bool = False


Broadcast = Callable[[dict[str, Any]], Awaitable[None]]


class Mirror:
    """Coarse batched liveness observation of open local managed sessions."""

    def __init__(
        self,
        store: Any,
        sessions: Any,
        tmux: Any,
        broadcast: Broadcast,
        *,
        local_host: str,
        config: MirrorConfig | None = None,
        inventory_emitter: InventoryEmitter | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.tmux = tmux
        self.broadcast = broadcast
        self.local_host = local_host
        self.config = config or MirrorConfig()
        self._obs: dict[str, _Obs] = {}
        self.inventory_emitter = inventory_emitter or InventoryEmitter(
            sessions, broadcast, min_interval_s=self.config.inventory_min_interval_s,
        )

    @property
    def _last_inv_signature(self) -> list[dict] | None:
        """Compatibility readback for the shared emitter's dedup identity."""
        return self.inventory_emitter.last_signature

    # -- loop --------------------------------------------------------------

    async def run_forever(self) -> None:
        """Cadence + backoff. Cancelled at shutdown; never swallows CancelledError."""
        cfg = self.config
        delay = cfg.interval_s if cfg.first_delay_s is None else cfg.first_delay_s
        failures = 0
        while True:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self.run_pass()
                failures = 0
                delay = cfg.interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad pass never kills the pump
                failures += 1
                delay = min(cfg.backoff_max_s, cfg.backoff_base_s * (2 ** (failures - 1)))
                log.warning("mirror pass failed (%d): %s; retrying in %.0fs", failures, exc, delay)

    async def run_pass(self) -> int:
        """One bounded observation pass. Returns the number of panes observed.
        Also the test-only forced trigger (called directly)."""
        cfg = self.config
        rows = [r for r in self.sessions.list_open() if str(r.get("host") or "") == self.local_host]
        open_ids = {r["stream_id"] for r in rows}
        # Forget observation state for streams no longer open. Membership follows
        # the O(open) REGISTRY, not a tmux re-enumeration — a lagging capture can
        # never evict a live session (the A8 fix).
        for sid in list(self._obs):
            if sid not in open_ids:
                self._obs.pop(sid, None)
        if not rows:
            return 0
        rc, output = await self.tmux.run("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}")
        if rc not in (0, 1): raise RuntimeError(f"tmux list-panes failed ({rc})")  # 1 = empty server
        pane_pids = dict(
            line.split("\t", 1) for line in output.splitlines() if "\t" in line
        )

        dirty = False
        for row in rows:
            try:
                if await self._observe_liveness(row, pane_pids.get(str(row.get("session_name") or ""), "")):
                    dirty = True
            except Exception:  # noqa: BLE001 - one bad pane never fails the pass
                log.debug("mirror observe failed sid=%s", row.get("stream_id"), exc_info=True)

        if dirty:
            await self._maybe_emit_inventory()
        return len(rows)

    # -- per-session liveness ----------------------------------------------

    async def _observe_liveness(self, row: dict[str, Any], pane_pid: str) -> bool:
        sid = row["stream_id"]
        host, name = self.sessions.split(sid)
        generation = str(row.get("session_generation") or "").strip()
        obs = self._obs.get(sid)
        if obs is None or obs.session_generation != generation:
            obs = _Obs(session_generation=generation)
            self._obs[sid] = obs

        if not pane_pid:
            return await self._observe_dead(sid, host, name, obs)

        first = not obs.seen
        changed = first or not obs.online or pane_pid != obs.pane_pid
        if changed:
            current = self.sessions.get(sid) or row
            working = current.get("working")
            self.sessions.apply_live(
                sid,
                online=True,
                pane_status="pane_alive",
                pane_pid=pane_pid,
                working=working if isinstance(working, bool) else False,
                working_label=str(current.get("working_label") or "") if working else "",
                local_mirror=bool(generation),
                mirror_local=bool(generation),
                mirror_generation=generation,
                local_mirror_generation=generation,
            )
        if first or pane_pid != obs.pane_pid or not obs.online or obs.died_emitted:
            await self._persist(host, name, pane_status="pane_alive", pane_pid=pane_pid)
        obs.pane_pid = pane_pid
        obs.online = True
        obs.seen = True
        obs.died_emitted = False
        return changed

    async def _observe_dead(self, sid: str, host: str, name: str, obs: _Obs) -> bool:
        """Evidence-backed death of a LOCAL pane (no pid AND no session).
        OBSERVATION ONLY: flag offline, persist `pane_dead`, broadcast
        `session.died` exactly once. Never kills, never closes the row — that is
        `sessions.close` / the reconciler's job (spec constraint 3)."""
        if obs.died_emitted:
            obs.seen = True
            return False
        obs.died_emitted = True
        obs.online = False
        self.sessions.apply_live(
            sid,
            online=False,
            pane_status="pane_dead",
            local_mirror=bool(obs.session_generation),
            mirror_local=bool(obs.session_generation),
            mirror_generation=obs.session_generation,
            local_mirror_generation=obs.session_generation,
        )
        await self._persist(host, name, pane_status="pane_dead")
        await self.broadcast({
            "type": "session.died", "stream_id": sid, "host": host,
            "session_name": name, "reason": "pane_pid_gone",
            "offline_since_ts": int(time.time()),
        })
        obs.seen = True
        return True

    async def _maybe_emit_inventory(self) -> None:
        """Delegate all inventory bounds to the shared emitter."""
        await self.inventory_emitter.emit_if_changed()

    # -- plumbing ----------------------------------------------------------

    async def _persist(self, host: str, name: str, **cols: Any) -> None:
        try:
            await self.store.update_session(host, name, **cols)
        except Exception:  # noqa: BLE001 - a row may have just closed; never fail the pass
            log.debug("mirror persist failed %s:%s", host, name, exc_info=True)
