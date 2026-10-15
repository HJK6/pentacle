"""A seat (and the lane it leads) reads busy while owned background work runs.

`working` keeps its foreground meaning (the pane is generating); it gates
nudges, compaction input and close. `busy` is the display truth: generating,
provider-reported background shells/subagents, or a busy open descendant.
Fixtures are sanitized excerpts of real Claude Code / Codex captures.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents_roster import project
from mirror import _extract_live_state
from presence import PresenceConfig, RemotePresence
from sessions import Sessions
from store import Store
from message_envelopes import match_message_envelope
from work_lanes_projection import _lead

FIXTURES = Path(__file__).parent / "fixtures" / "working_state"


def _parse(fixture: str, provider: str) -> dict[str, object]:
    return _extract_live_state((FIXTURES / fixture).read_text(), provider)


# --------------------------------------------------------------------------- #
# Capture parser
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("fixture", "provider", "working", "background"),
    [
        ("claude_bg_shell_idle.txt", "claude", False, ["1 shell"]),
        ("claude_bg_subagent_idle.txt", "claude", False, ["1 subagent"]),
        ("claude_working_with_shells.txt", "claude", True, ["2 shells"]),
        ("codex_bg_terminal_hollow_frame.txt", "codex", True, ["1 background terminal"]),
    ],
)
def test_provider_background_signals_are_parsed(fixture, provider, working, background) -> None:
    state = _parse(fixture, provider)
    assert state["working"] is working
    assert state["background"] == background


@pytest.mark.parametrize(
    "fixture",
    [
        # `← N agent` and `/tasks to see subagents` persist on idle seats.
        "claude_bg_done_idle.txt",
        # Footer and panel text quoted in scrollback above the composer.
        "claude_shell_text_in_scrollback.txt",
        # Monitors are long-lived watches, not counted (no RED evidence).
        "claude_monitor_footer_idle.txt",
        "claude_idle_prompt.txt",
    ],
)
def test_non_owned_or_finished_footers_are_not_background(fixture) -> None:
    state = _parse(fixture, "claude")
    assert state["working"] is False
    assert state["background"] == []


def test_codex_hollow_spinner_frame_reads_working() -> None:
    """Codex alternates `•`/`◦` spinner frames; the `◦` frame read idle."""
    state = _parse("codex_working_hollow_frame.txt", "codex")
    assert state["working"] is True
    assert state["working_label"] == "Working 27s"
    assert state["background"] == []


def test_codex_idle_prompt_has_no_background() -> None:
    assert _parse("codex_idle_prompt.txt", "codex")["background"] == []


# --------------------------------------------------------------------------- #
# Capture path -> live overlay
# --------------------------------------------------------------------------- #


class _Tmux:
    def __init__(self, pane: str) -> None:
        self.pane = pane

    async def run(self, *args: str, **kwargs: object) -> tuple[int, str]:
        sep = "|" if "-F" in args and "|" in args[args.index("-F") + 1] else "\t"
        return 0, f"v2-bg{sep}4242\n"

    async def capture_checked(self, _name: str, **kwargs: object) -> tuple[bool, str]:
        return True, self.pane


class _Hosts:
    local_host = "hosta"

    def __init__(self, tmux: _Tmux) -> None:
        self.peers = {"hostb": object()}
        self._tmux = tmux

    def is_online(self, host: str) -> bool:
        return True

    def tmux_for(self, host: str) -> _Tmux:
        return self._tmux

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {"hostb": {"online": True, "host_status": "online", "host_status_reason": ""}}


def _capture_once(presence: RemotePresence) -> None:
    async def run() -> None:
        observations = await presence.observe_once()
        for host, rows in presence.last_rows.items():
            presence.apply_observation(rows, observations[host])
        await presence.capture_previews()
    asyncio.run(run())


def test_capture_sets_background_busy_and_returns_to_idle() -> None:
    sessions = Sessions(Store(":memory:"), tmux=None, local_host="hosta")
    sessions._inv = {"hostb:v2-bg": {
        "host": "hostb", "session_name": "v2-bg", "stream_id": "hostb:v2-bg",
        "provider": "claude", "session_generation": "g1", "status": "open",
    }}
    tmux = _Tmux((FIXTURES / "claude_bg_shell_idle.txt").read_text())
    presence = RemotePresence(sessions, _Hosts(tmux), config=PresenceConfig(preview_cache_ttl_s=0))

    _capture_once(presence)
    row = next(r for r in sessions.list_open() if r["stream_id"] == "hostb:v2-bg")
    assert row["working"] is False
    assert row["background_busy"] is True
    assert row["background_label"] == "1 shell"
    assert row["busy"] is True
    assert row["busy_reasons"] == ["background"]

    tmux.pane = (FIXTURES / "claude_bg_done_idle.txt").read_text()
    _capture_once(presence)
    row = next(r for r in sessions.list_open() if r["stream_id"] == "hostb:v2-bg")
    assert row["background_busy"] is False
    assert row["background_label"] == ""
    assert row["busy"] is False
    assert row["busy_reasons"] == []


# --------------------------------------------------------------------------- #
# Row projection: busy, reasons, descendants
# --------------------------------------------------------------------------- #


def _r(sid: str, parent: str | None = None, **live: object) -> dict[str, object]:
    return {"stream_id": sid, "session_generation": "g", "status": "open",
            "parent_stream_id": parent, "online": True, "pane_status": "pane_alive", **live}


def _by_id(rows):
    return {row["stream_id"]: row for row in project(rows, {})}


def test_busy_reasons_cover_generating_background_and_busy_descendants() -> None:
    rows = _by_id([
        _r("h:root"),
        _r("h:lead", "h:root"),
        _r("h:qa", "h:lead"),
        _r("h:worker", "h:qa", background_busy=True),
        _r("h:gen", working=True),
    ])
    assert rows["h:worker"]["busy_reasons"] == ["background"]
    assert rows["h:qa"]["busy_reasons"] == ["child_seat"]
    assert rows["h:lead"]["busy_reasons"] == ["child_seat"]
    assert rows["h:root"]["busy"] is True
    assert rows["h:gen"]["busy_reasons"] == ["generating"]
    # Foreground semantics are untouched: projection never invents `working`.
    assert "working" not in rows["h:lead"]
    assert rows["h:gen"]["working"] is True


def test_open_idle_descendant_does_not_make_parent_busy() -> None:
    rows = _by_id([_r("h:lead"), _r("h:child", "h:lead", working=False, background_busy=False)])
    assert rows["h:lead"]["busy"] is False
    assert rows["h:lead"]["busy_reasons"] == []


def test_dead_or_offline_rows_contribute_nothing() -> None:
    rows = _by_id([
        _r("h:lead"),
        {**_r("h:dead", "h:lead", working=True), "pane_status": "pane_dead"},
        {**_r("h:off", "h:lead", background_busy=True), "online": False},
    ])
    assert rows["h:lead"]["busy"] is False
    assert rows["h:dead"]["busy"] is False
    assert rows["h:off"]["busy"] is False


def test_parent_cycle_terminates() -> None:
    rows = _by_id([_r("h:a", "h:b"), _r("h:b", "h:a", background_busy=True)])
    assert rows["h:b"]["busy"] is True
    assert rows["h:a"]["busy_reasons"] == ["child_seat"]


# --------------------------------------------------------------------------- #
# Lanes and fleet digest
# --------------------------------------------------------------------------- #


def _lane() -> dict[str, object]:
    return {"lane_id": "l1", "bound_stream_id": "h:lead", "bound_generation": "g",
            "_qualifies": True, "_lead_row": {"status": "open"}}


def test_lane_lead_presence_reads_working_while_busy_in_background() -> None:
    presence = {"online": True, "working": False, "busy": True, "busy_reasons": ["child_seat"]}
    lead = _lead(_lane(), presence, "2026-10-15T00:00:00Z")
    assert lead["presence"]["working"] is True
    # The shared lane presence contract keeps its exact key set.
    assert set(lead["presence"]) == {"online", "working", "capture_liveness", "last_activity"}


def test_lane_lead_presence_without_busy_field_keeps_working() -> None:
    lead = _lead(_lane(), {"online": True, "working": True}, "2026-10-15T00:00:00Z")
    assert lead["presence"]["working"] is True
    lead = _lead(_lane(), {"online": True, "working": False, "busy": False}, "2026-10-15T00:00:00Z")
    assert lead["presence"]["working"] is False


def _live_root(root):
    return {"session_generation": root["session_generation"], "online": True, "pane_status": "pane_alive"}


def test_background_busy_lane_is_not_idle_and_digest_renders_working(monkeypatch) -> None:
    monkeypatch.setenv("PENTACLE_TREE_IDLE_S", "1200")

    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="hosta")
            root = await sessions.open("hosta", "root", provider="shell", no_watch=True)
            await store.update_session("hosta", "root", pane_status="pane_alive")
            lead = await sessions.open("hosta", "lead", provider="shell", role="lead",
                                       no_watch=True, parent_stream_id="hosta:root")
            binding = ("hosta:root", root["session_generation"])
            obs = {"hosta:root": _live_root(root),
                   "hosta:lead": {"session_generation": lead["session_generation"],
                                  "working": False, "busy": True, "busy_reasons": ["background"]}}
            for now in (100, 1600, 2900, 4000):
                await store.evaluate_watch_wake(obs, now=now, root_binding=binding)
            idle = await store.submit(lambda c: c.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'").fetchone()[0])
            assert idle == 0
            digests = await store.submit(lambda c: [r[0] for r in c.execute(
                "SELECT body FROM v2_outbound_notices WHERE kind='lane_digest' ORDER BY created_at")])
            assert digests
            assert match_message_envelope(digests[-1])["lanes"][0]["working"] is True

            # Background work ends -> the seat starts its idle clock again.
            obs["hosta:lead"].update(busy=False, busy_reasons=[])
            await store.evaluate_watch_wake(obs, now=4001, root_binding=binding)
            await store.evaluate_watch_wake(obs, now=5202, root_binding=binding)
            idle = await store.submit(lambda c: c.execute(
                "SELECT COUNT(*) FROM v2_outbound_notices WHERE kind='tree_idle'").fetchone()[0])
            assert idle == 1
        finally:
            store.stop()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Final-QA repairs: composer drafts and cycle order
# --------------------------------------------------------------------------- #

_RULE = "─" * 40


@pytest.mark.parametrize(
    "draft",
    [
        "❯ Explain this footer:\n  1 shell",
        "❯ Quote the panel:\n  ● main\n  ◯ general-purpose  sample",
    ],
)
def test_multiline_composer_draft_is_not_background(draft) -> None:
    pane = f"{_RULE}\n{draft}\n{_RULE}\n  ⏵⏵ bypass permissions on · ← 1 agent\n"
    state = _extract_live_state(pane, "claude")
    assert state["working"] is False
    assert state["background"] == []


def test_footer_after_multiline_draft_still_counts() -> None:
    pane = f"{_RULE}\n❯ first line\n  second line\n{_RULE}\n  ⏵⏵ bypass permissions on · 1 shell · ↓ to manage\n"
    assert _extract_live_state(pane, "claude")["background"] == ["1 shell"]


def test_composer_without_closing_rule_reads_no_background() -> None:
    assert _extract_live_state("❯ \n  ⏵⏵ bypass permissions on · 1 shell\n", "claude")["background"] == []


@pytest.mark.parametrize("busy_node", ["h:a", "h:b"])
@pytest.mark.parametrize("reverse", [False, True])
def test_parent_cycle_is_order_independent(busy_node, reverse) -> None:
    rows = [_r("h:a", "h:b"), _r("h:b", "h:a")]
    for row in rows:
        if row["stream_id"] == busy_node:
            row["working"] = True
    other = "h:b" if busy_node == "h:a" else "h:a"
    projected = _by_id(list(reversed(rows)) if reverse else rows)
    assert projected[busy_node]["busy_reasons"] == ["generating"]
    assert projected[other]["busy_reasons"] == ["child_seat"]
