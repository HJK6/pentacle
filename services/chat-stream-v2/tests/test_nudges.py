"""Status and title nudge gating for synthetic sessions."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from comms import Comms  # noqa: E402
from ledger import (  # noqa: E402
    NUDGE_CARD_TEXT,
    NUDGE_DEFAULT_COOLDOWN_S,
    NUDGE_DEFAULT_ENGAGED_WINDOW_S,
    NUDGE_DEFAULT_INTERVAL_S,
    NUDGE_DEFAULT_STATUS_STALE_S,
    NUDGE_TITLE_TEXT,
    NudgeConfig,
    NudgeJob,
)
from main import parse_args  # noqa: E402
from mirror import Mirror, MirrorConfig  # noqa: E402
from sessions import Sessions, genuine_activity_epoch  # noqa: E402
from spawnctl import SpawnCtl  # noqa: E402
from store import Store  # noqa: E402

HOST = "hosta"


class RecordingTmux:
    """A real Comms target with controllable pane output and liveness."""

    IDLE = "⏵⏵ bypass permissions on (bypass)\n❯ \n"

    def __init__(self) -> None:
        self._panes: dict[str, str] = {}
        self.names: set[str] = set()
        self.dead: set[str] = set()
        self.pasted: list[str] = []
        self.pasted_by_name: list[tuple[str, str]] = []

    async def has_session(self, name: str) -> bool:
        return name not in self.dead

    async def pane_pid(self, name: str) -> str:
        return "1234" if name not in self.dead else ""

    async def paste(self, name: str, text: str) -> None:
        if name in self.dead:
            raise RuntimeError("dead pane")
        self._panes[name] = f"⏺ {text}\n❯ \n"
        self.pasted.append(text)
        self.pasted_by_name.append((name, text))

    async def capture(self, name: str) -> str:
        return self._panes.get(name, self.IDLE)

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        if args == ("list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"):
            return 0, "\n".join(f"{name}\t1234" for name in self.names - self.dead)
        return (0, "")

    def set_capture(self, name: str, text: str) -> None:
        self._panes[name] = text


async def _broadcast(_frame: dict) -> None:
    return None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _card(epoch: float) -> dict[str, str]:
    return {"goal": "ship it", "updated_at": _iso(epoch)}


class Harness:
    def __init__(self, store: Store, config: NudgeConfig | None = None) -> None:
        self.store = store
        self.tmux = RecordingTmux()
        self.sessions = Sessions(store, tmux=self.tmux, local_host=HOST)
        self.spawnctl = SpawnCtl(store, self.sessions, tmux=self.tmux)
        self.comms = Comms(store, self.sessions, self.spawnctl)
        self.mirror = Mirror(
            store,
            self.sessions,
            self.tmux,
            _broadcast,
            local_host=HOST,
            config=MirrorConfig(),
        )
        self.job = NudgeJob(self.sessions, self.comms, store, config or NudgeConfig())
        self._activity_index = 0
        self._restored_activity: set[tuple[str, str]] = set()

    async def open(self, name: str, *, host: str = HOST, **fields: object) -> None:
        user_event_count = int(fields.pop("user_event_count", 2))
        sidechain_event_count = int(fields.pop("sidechain_event_count", 0))
        base: dict[str, object] = {
            "provider": "claude",
            "visibility": "default",
            "pane_status": "pane_unknown",
            "created_at": "2026-08-01T00:00:00Z",
        }
        base.update(fields)
        await self.sessions.open(host, name, **base)
        if host == HOST:
            self.tmux.names.add(name)
        if str(base.get("provider") or "").lower() == "claude":
            generation = str(base.get("session_generation") or "default")
            stream_id = f"{host}:{name}"
            for index in range(max(0, user_event_count) + max(0, sidechain_event_count)):
                is_sidechain = index >= max(0, user_event_count)
                await self.store.append_session_event(
                    stream_id,
                    {
                        "stream_id": stream_id,
                        "provider": "claude",
                        "kind": "USER",
                        "text": f"{'sidechain' if is_sidechain else 'human'} turn {index}",
                        "timestamp": f"2026-08-01T00:00:{index:02d}Z",
                        "raw": {
                            "jsonl_record_uuid": f"{generation}-{index}",
                            "is_sidechain": is_sidechain,
                        },
                    },
                    identity=f"test-user-{generation}-{index}",
                    limit=500,
                )
        if host == HOST and fields.get("pane_status") == "pane_dead":
            self.tmux.dead.add(name)

    async def observe(self) -> None:
        await self.mirror.run_pass()
        for row in self.sessions.list_open():
            if str(row.get("host") or "") != HOST:
                continue
            stream_id = str(row.get("stream_id") or "")
            key = (stream_id, str(row.get("session_generation") or ""))
            if key in self._restored_activity:
                continue
            self.sessions.restore_genuine_activity(
                stream_id,
                await self.store.fetch_session_event_tail(stream_id, limit=64),
            )
            self._restored_activity.add(key)

    async def qualify(self, name: str, text: str | None = None, *, user_activity: bool = True) -> None:
        await self.observe()  # first observation establishes only a baseline
        self._activity_index += 1
        sid = f"{HOST}:{name}"
        event = {
            "stream_id": sid,
            "provider": str((self.sessions.get(sid) or {}).get("provider") or "claude"),
            "kind": "USER" if user_activity else "TOOL_USE",
            "text": "test operator task" if user_activity else "test tool activity",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "raw": {"jsonl_record_uuid": f"activity-{name}-{self._activity_index}"},
        }
        await self.store.append_session_event(
            sid, event, identity=f"activity-{name}-{self._activity_index}", limit=500,
        )
        self.sessions.apply_genuine_activity_event(sid, event)
        self.tmux.set_capture(name, text or f"operator activity {name}\n❯ \n")
        await self.observe()

    def set_activity(self, name: str, at: object) -> None:
        sid = f"{HOST}:{name}"
        row = self.sessions.get(sid) or {}
        self.sessions.apply_live(
            sid,
            operator_activity_at=at,
            operator_activity_generation=str(row.get("session_generation") or ""),
            genuine_activity_at=at,
            genuine_activity_generation=str(row.get("session_generation") or ""),
        )


def _run(coro):
    return asyncio.run(coro)


def _new_store(path: str = ":memory:") -> Store:
    store = Store(path)
    store.start()
    return store


def test_claude_nudges_wait_until_second_user_event() -> None:
    async def _go() -> tuple[int, int, list[str]]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("first", user_event_count=1)
            await h.qualify("first", user_activity=False)
            first = await h.job.run_pass()

            await h.open("second", user_event_count=2)
            await h.qualify("second")
            second = await h.job.run_pass()
            return first.sent, second.sent, list(h.tmux.pasted)
        finally:
            store.stop()

    assert _run(_go()) == (0, 1, [f"{NUDGE_TITLE_TEXT}\n{NUDGE_CARD_TEXT}"])


def test_codex_one_real_turn_does_not_bypass_grace() -> None:
    async def _go() -> int:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("codex", provider="codex", user_event_count=0)
            await h.qualify("codex")
            return (await h.job.run_pass()).sent
        finally:
            store.stop()

    assert _run(_go()) == 0


def test_claude_turn_grace_is_scoped_to_the_current_session_lifecycle() -> None:
    async def _go() -> int:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("reused", user_event_count=2)
            await store.update_session(HOST, "reused", status="closed")
            await h.open("reused", user_event_count=1)
            await h.qualify("reused")
            return (await h.job.run_pass()).sent
        finally:
            store.stop()

    assert _run(_go()) == 0


def test_claude_turn_grace_ignores_sidechain_user_events() -> None:
    async def _go() -> int:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("sidechain", user_event_count=1, sidechain_event_count=1)
            await h.qualify("sidechain", user_activity=False)
            return (await h.job.run_pass()).sent
        finally:
            store.stop()

    assert _run(_go()) == 0


# Case 1: no qualifying activity, including after a fresh mirror restart.
def test_never_used_session_has_no_nudges_before_or_after_restart(tmp_path) -> None:
    db = str(tmp_path / "never-used.db")

    async def _go() -> tuple[int, int]:
        first_store = _new_store(db)
        try:
            first = Harness(first_store)
            await first.open("s1", user_event_count=0)
            await first.observe()
            before = (await first.job.run_pass()).sent
        finally:
            first_store.stop()

        second_store = _new_store(db)
        try:
            second = Harness(second_store)
            await second.sessions.refresh()
            await second.observe()  # restart baseline is not genuine activity
            after = (await second.job.run_pass()).sent
        finally:
            second_store.stop()
        return before, after

    assert _run(_go()) == (0, 0)


# Case 2: an old activity floor permits a title-only fallback but never status.
def test_parked_title_only_does_not_reengage_from_its_own_paste() -> None:
    async def _go() -> tuple[int, int, list[str]]:
        store = _new_store()
        try:
            h = Harness(store, NudgeConfig(status_stale_s=0.0, engaged_window_s=60.0))
            await h.open("parked", status_card=_card(time.time() - 3600))
            await h.qualify("parked")
            h.set_activity("parked", time.time() - 3600)
            first = await h.job.run_pass()
            await h.observe()  # RecordingTmux echoes the title tell
            second = await h.job.run_pass()
            return first.sent, second.sent, list(h.tmux.pasted)
        finally:
            store.stop()

    assert _run(_go()) == (1, 0, [NUDGE_TITLE_TEXT])


# Case 3: recent activity after card write engages status, with durable cooldown.
def test_engaged_by_activity_status_nudges_once_per_cooldown() -> None:
    async def _go() -> tuple[int, int, list[str]]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("active", title="active", status_card=_card(time.time() - 3600))
            await h.qualify("active")
            first = await h.job.run_pass()
            second = await h.job.run_pass()
            return first.sent, second.sent, list(h.tmux.pasted)
        finally:
            store.stop()

    assert _run(_go()) == (1, 0, [NUDGE_CARD_TEXT])


def test_live_0502_pane_redraw_does_not_pass_durable_activity_gate(monkeypatch) -> None:
    last_nudge = 1_787_456_359.0
    card_updated = 1_787_456_853.0
    last_turn_event = 1_787_456_856.103
    pane_redraw = 1_787_461_356.181990
    monkeypatch.setattr("mirror.time.time", lambda: pane_redraw)
    monkeypatch.setattr("ledger.time.time", lambda: pane_redraw)

    async def _go() -> tuple[float | None, int, bool]:
        store = _new_store()
        try:
            h = Harness(store, NudgeConfig(engaged_window_s=7200.0, cooldown_s=3600.0))
            await h.open(
                "status-0502",
                title="Status 05:02 regression",
                created_at="2026-08-22T16:40:58Z",
                status_card={"goal": "hold idle", "updated_at": _iso(card_updated)},
            )
            sid = f"{HOST}:status-0502"
            event = {
                "stream_id": sid,
                "provider": "claude",
                "kind": "ASSIST_TEXT",
                "text": "Holding idle to prove the nudge fix.",
                "timestamp": datetime.fromtimestamp(last_turn_event, timezone.utc).isoformat(),
                "raw": {"jsonl_record_uuid": "status-0502-last-turn"},
            }
            await store.append_session_event(
                sid, event, identity="status-0502-last-turn", limit=500,
            )
            await store.record_nudge(sid, "status_card", last_nudge, _iso(last_nudge))

            await h.observe()
            h.tmux.set_capture("status-0502", "idle pane redraw\n❯ \n")
            await h.observe()
            summary = h.sessions.get(sid) or {}
            result = await h.job.run_pass()
            states = await store.nudge_states()
            return (
                summary.get("genuine_activity_at"),
                result.sent,
                states[(sid, "status_card")]["last_nudged_at"] == last_nudge,
            )
        finally:
            store.stop()

    activity, sent, state_unchanged = _run(_go())
    assert activity == pytest.approx(last_turn_event)
    assert (sent, state_unchanged) == (0, True)


@pytest.mark.parametrize(
    "kind,raw,text,expected",
    [
        ("USER", {}, "operator message", True),
        ("USER", {"from_stream_id": "daemon:status-sweep"}, "status", False),
        ("USER", {}, NUDGE_CARD_TEXT, False),
        ("TOOL_USE", {}, "agent tool", True),
        ("ASSIST_TEXT", {}, "agent reply", True),
        ("THINKING", {}, "thinking", False),
        ("TOOL_RESULT", {}, "result", False),
        ("SYSTEM", {}, "pane refresh", False),
        ("ASSIST_TEXT", {"is_sidechain": True}, "worker reply", False),
    ],
)
def test_genuine_activity_event_vocabulary(kind, raw, text, expected) -> None:
    event = {
        "kind": kind,
        "raw": raw,
        "text": text,
        "timestamp": "2026-08-23T05:02:36.181990Z",
    }
    assert (genuine_activity_epoch(event) is not None) is expected


# Case 4: an open child is not a substitute for durable parent turn activity.
def test_engaged_by_children_does_not_rearm_idle_parent() -> None:
    async def _go() -> int:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("parent", title="parent", status_card=_card(time.time() - 3600))
            await h.open("child", parent_stream_id=f"{HOST}:parent")
            await h.observe()
            return (await h.job.run_pass()).sent
        finally:
            store.stop()

    assert _run(_go()) == 0


# Case 5: presence/unknown overlays do not establish the local-mirror seam.
def test_peer_presence_and_unknown_state_are_silent() -> None:
    async def _go() -> int:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("unknown", status_card=_card(time.time() - 60))
            await h.open("peer", host="peer", title="peer", status_card=_card(time.time() - 60))
            h.sessions.apply_live(
                "peer:peer", online=True, pane_status="pane_alive", working=False,
                genuine_activity_at=time.time(), genuine_activity_generation="ignored",
            )
            return (await h.job.run_pass()).sent
        finally:
            store.stop()

    assert _run(_go()) == 0


# Case 6: timestamp parsing is strict and fails closed for malformed input.
def test_timestamp_cases_fail_closed_except_offset() -> None:
    async def _case(*, activity: object, card: object = None) -> int:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("s1", title="named", status_card=card or _card(time.time() - 3600))
            await h.qualify("s1")
            h.set_activity("s1", activity)
            return (await h.job.run_pass()).sent
        finally:
            store.stop()

    now = time.time()
    naive = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
    malformed = "not-a-timestamp"
    future = now + 3600
    offset = datetime.fromtimestamp(now - 5, timezone(timedelta(hours=2))).isoformat()
    assert _run(_case(activity=naive)) == 0
    assert _run(_case(activity=malformed)) == 0
    assert _run(_case(activity=future)) == 0
    assert _run(_case(activity=offset)) == 1
    assert _run(_case(activity=now, card={"goal": "missing timestamp"})) == 0


# Case 7: combined sends, cooldowns, caps, failures, and rollback are bounded.
def test_combined_nudge_atomicity_partial_cooldown_and_cap() -> None:
    async def _combined() -> tuple[int, list[str], dict]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("both")
            await h.qualify("both")
            result = await h.job.run_pass()
            states = await store.nudge_states()
            return result.sent, list(h.tmux.pasted), states
        finally:
            store.stop()

    sent, pasted, states = _run(_combined())
    assert sent == 1
    assert pasted == [f"{NUDGE_TITLE_TEXT}\n{NUDGE_CARD_TEXT}"]
    assert set(states) == {("hosta:both", "title"), ("hosta:both", "status_card")}

    async def _partial() -> list[str]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("partial")
            await h.qualify("partial")
            await store.record_nudge("hosta:partial", "title", time.time(), "")
            await h.job.run_pass()
            return list(h.tmux.pasted)
        finally:
            store.stop()

    assert _run(_partial()) == [NUDGE_CARD_TEXT]

    async def _cap() -> tuple[int, int, bool, list[str]]:
        store = _new_store()
        try:
            h = Harness(store, NudgeConfig(max_per_pass=1))
            await h.open("a")
            await h.open("b", status_card=_card(time.time() - 60))
            await h.qualify("a")
            await h.qualify("b")
            result = await h.job.run_pass()
            return result.candidates, result.sent, result.capped, list(h.tmux.pasted)
        finally:
            store.stop()

    candidates, sent, capped, pasted = _run(_cap())
    assert (candidates, sent, capped) == (2, 1, True)
    assert pasted == [f"{NUDGE_TITLE_TEXT}\n{NUDGE_CARD_TEXT}"]

    async def _rollback() -> dict:
        store = _new_store()
        try:
            with pytest.raises(Exception):
                await store.record_nudges([
                    ("hosta:s1", "title", 1.0, ""),
                    (None, "status_card", 1.0, ""),  # NOT NULL forces rollback
                ])
            return await store.nudge_states()
        finally:
            store.stop()

    assert _run(_rollback()) == {}


# Case 7 also stamps both combined kinds after a send failure.
def test_combined_send_failure_stamps_both_kinds() -> None:
    async def _go() -> tuple[int, int, set[tuple[str, str]]]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("fail")
            await h.qualify("fail")

            async def _fail(_msg: dict) -> None:
                raise RuntimeError("forced tell failure")

            h.comms.tell = _fail
            result = await h.job.run_pass()
            return result.sent, result.errors, set(await store.nudge_states())
        finally:
            store.stop()

    assert _run(_go()) == (0, 1, {("hosta:fail", "title"), ("hosta:fail", "status_card")})


# Case 8: generation reuse clears state, while same-generation restart preserves it.
def test_generation_reuse_clears_state_and_successor_needs_activity() -> None:
    async def _go() -> tuple[int, int, int, set[tuple[str, str]]]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("reuse", session_generation="old")
            await h.qualify("reuse")
            assert (await h.job.run_pass()).sent == 1
            assert set(await store.nudge_states()) == {
                ("hosta:reuse", "title"), ("hosta:reuse", "status_card")
            }

            await store.update_session(HOST, "reuse", status="closed", pane_status="pane_dead")
            await h.sessions.refresh()
            await h.mirror.run_pass()
            await h.open("reuse", session_generation="new", user_event_count=0)
            await h.observe()  # successor baseline, no floor
            before = (await h.job.run_pass()).sent
            for index in range(2):
                sid = f"{HOST}:reuse"
                event = {
                    "stream_id": sid,
                    "provider": "claude",
                    "kind": "USER",
                    "text": f"successor human turn {index}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "raw": {"jsonl_record_uuid": f"successor-user-{index}"},
                }
                await store.append_session_event(
                    sid, event, identity=f"successor-user-{index}", limit=500,
                )
                h.sessions.apply_genuine_activity_event(sid, event)
            await h.qualify("reuse", "successor activity\n❯ \n")
            after = (await h.job.run_pass()).sent
            states = set(await store.nudge_states())
            return before, after, len(h.tmux.pasted), states
        finally:
            store.stop()

    assert _run(_go()) == (0, 1, 2, {("hosta:reuse", "title"), ("hosta:reuse", "status_card")})


# Case 9: title nudges require an observed, known-false working value.
def test_title_nudge_skips_working_true_and_unknown() -> None:
    async def _go() -> tuple[int, int]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("working", status_card=_card(time.time()))
            await h.qualify("working", "Working (1s)\n❯ \n")
            h.sessions.apply_live("hosta:working", working=True, working_label="Working")
            await h.observe()
            working = (await h.job.run_pass()).sent
            row = h.sessions.get("hosta:working")
            assert row is not None
            h.sessions.apply_live(row["stream_id"], working=None)
            unknown = (await h.job.run_pass()).sent
            return working, unknown
        finally:
            store.stop()

    assert _run(_go()) == (0, 0)


# Case 10: defaults, overrides, reset, and the kill switch.
def test_defaults_overrides_compliance_reset_and_disable_flag() -> None:
    defaults = NudgeConfig()
    assert defaults.interval_s == NUDGE_DEFAULT_INTERVAL_S
    assert defaults.status_stale_s == NUDGE_DEFAULT_STATUS_STALE_S == 1800.0
    assert defaults.engaged_window_s == NUDGE_DEFAULT_ENGAGED_WINDOW_S == 1800.0
    assert defaults.cooldown_s == NUDGE_DEFAULT_COOLDOWN_S == 3600.0
    overridden = NudgeConfig.from_env({
        "PENTACLE_NUDGE_INTERVAL_S": "7",
        "PENTACLE_NUDGE_STATUS_STALE_S": "8",
        "PENTACLE_NUDGE_ENGAGED_WINDOW_S": "9",
        "PENTACLE_NUDGE_COOLDOWN_S": "11",
    })
    assert (overridden.interval_s, overridden.status_stale_s, overridden.engaged_window_s,
            overridden.cooldown_s) == (7, 8, 9, 11)
    assert parse_args([]).disable_nudges is False
    assert parse_args(["--disable-nudges"]).disable_nudges is True


# Locked population test: exact tell set for mixed sweep inputs.
def test_run_pass_population_exact_tell_set() -> None:
    async def _go() -> list[tuple[str, str]]:
        store = _new_store()
        try:
            h = Harness(store)
            await h.open("parked", status_card=_card(time.time() - 3600))
            await h.open("engaged", title="engaged", status_card=_card(time.time() - 3600))
            await h.open("lead", title="lead", status_card=_card(time.time() - 3600))
            await h.open("lead-child", parent_stream_id="hosta:lead")
            await h.open("never", title="never", status_card=_card(time.time() - 3600))
            await h.open("peer", host="peer", title="peer", status_card=_card(time.time() - 3600))
            await h.qualify("parked")
            await h.qualify("engaged")
            await h.qualify("lead")
            await h.observe()
            h.set_activity("parked", time.time() - 3600)
            h.set_activity("engaged", time.time() - 10)
            h.set_activity("lead", time.time() - 3600)
            await h.job.run_pass()
            return list(h.tmux.pasted_by_name)
        finally:
            store.stop()

    assert _run(_go()) == [
        ("engaged", NUDGE_CARD_TEXT),
        ("parked", NUDGE_TITLE_TEXT),
    ]
