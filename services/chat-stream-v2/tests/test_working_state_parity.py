from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mirror import _extract_live_state
from server import Server
from working_state import WorkingStateTracker


FIXTURES = Path(__file__).parent / "fixtures" / "working_state"


@pytest.mark.parametrize(
    ("provider", "fixture", "label_fragment"),
    [
        ("claude", "claude_v5_determining.txt", "9m 52s"),
        ("claude", "claude_v5_galloping.txt", "30m 4s"),
        ("claude", "claude_v5_honking.txt", "30m 7s"),
        ("codex", "codex_working.txt", "Working 3s"),
        ("codex", "codex_waiting.txt", "Waiting for background terminal 5s"),
    ],
)
def test_pane_working_fixtures_emit_active_state(
    provider: str, fixture: str, label_fragment: str,
) -> None:
    state = _extract_live_state((FIXTURES / fixture).read_text(), provider)
    assert state["working"] is True
    assert label_fragment in str(state["working_label"])


@pytest.mark.parametrize(
    ("provider", "fixture"),
    [("claude", "claude_idle_prompt.txt"), ("codex", "codex_idle_prompt.txt")],
)
def test_pane_idle_fixtures_clear_working_state(provider: str, fixture: str) -> None:
    state = _extract_live_state((FIXTURES / fixture).read_text(), provider)
    assert state["working"] is False
    assert state["working_label"] == ""


def test_claude_live_spinner_surfaces_past_completed_scrollback_label() -> None:
    """A live turn whose bounded status region also holds a *completed* spinner
    label from a prior turn (glyph line, but no live timer+hint) must still
    surface as working. The fixture holds `✻ Cooked for 48s · done 3:17 PM`
    above the live `· Contemplating… (26s · ↓ 126 tokens)`. The parser must
    ignore the completed label while retaining the active spinner."""
    state = _extract_live_state(
        (FIXTURES / "claude_v5_live_spinner_below_completed_label.txt").read_text(),
        "claude",
    )
    assert state["working"] is True
    assert "Contemplating…" in str(state["working_label"])
    assert "26s" in str(state["working_label"])


# The seventh frame is a plain ASCII "*" (U+002A); the six before it are the
# sparkle glyphs. All seven must surface as live — a dropped frame is the
# peer-host early-clear case (see test below).
@pytest.mark.parametrize("glyph", list("✢✽✶✻✳·*"))
def test_claude_spinner_glyph_variants_are_live(glyph: str) -> None:
    text = f"{glyph} Determining… (2s · thinking)\n"
    state = _extract_live_state(text, "claude")
    assert state["working"] is True
    assert state["working_label"] == "Determining… (2s · thinking)"


def test_claude_star_frame_fixture_surfaces_working() -> None:
    """A synthetic transport capture of a provider seat on hostb mid-turn whose
    live spinner landed on the ASCII "*" animation frame:
    `* Drizzling… (35s · ↓ 94 tokens)`. The accepted glyph class must include
    the ASCII star frame as well as the sparkle variants."""
    state = _extract_live_state(
        (FIXTURES / "claude_v5_star_frame_remote_early_clear.txt").read_text(),
        "claude",
    )
    assert state["working"] is True
    assert "Drizzling…" in str(state["working_label"])
    assert "35s" in str(state["working_label"])


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_tracker_working_transitions_down_then_idle(provider: str) -> None:
    tracker = WorkingStateTracker()
    stream_id = f"hosta:{provider}-working-state"
    active = tracker.observe(
        {"objective": "Exercise the existing spawn contract",
            "stream_id": stream_id,
            "host": "hosta",
            "provider": provider,
            "session_name": f"{provider}-working-state",
            "kind": "WORKING",
            "raw": {
                "working": True,
                "working_label": "Determining… (3s · thinking)" if provider == "claude" else "Working 3s",
                "state": "working",
            },
        },
        10_000,
    )
    assert active is not None
    assert active["tokens_phase"] == "down"
    assert active["elapsed_ms"] == 3_000
    assert tracker.snapshot()[stream_id]["tokens_phase"] == "down"

    idle = tracker.observe(
        {"objective": "Exercise the existing spawn contract",
            "stream_id": stream_id,
            "host": "hosta",
            "provider": provider,
            "session_name": f"{provider}-working-state",
            "kind": "WORKING",
            "raw": {"working": False, "working_label": "", "state": "idle"},
        },
        10_500,
    )
    assert idle is not None
    assert idle["tokens_phase"] == "idle"
    assert idle["elapsed_ms"] == 0


def test_hello_snapshot_contains_tracker_working_states(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = WorkingStateTracker()
    stream_id = "hosta:claude-snapshot"
    tracker.observe(
        {"objective": "Exercise the existing spawn contract",
            "stream_id": stream_id,
            "host": "hosta",
            "provider": "claude",
            "session_name": "claude-snapshot",
            "kind": "WORKING",
            "raw": {
                "working": True,
                "working_label": "Determining… (9m 52s · thinking)",
                "state": "working",
            },
        },
        10_000,
    )
    server = Server(local_host="hosta")
    server.working_state_trackers.append(tracker)
    monkeypatch.setattr("server.time.time", lambda: 602.0)

    snapshot = asyncio.run(server._on_hello({}))[1]

    assert isinstance(snapshot["working_states"], dict)
    assert snapshot["working_states"][stream_id]["tokens_phase"] == "down"
    # The 9m52s label anchors the turn at -582s. Hello renders that same
    # anchor at daemon-now (602s), rather than returning an older cached frame.
    assert snapshot["working_states"][stream_id]["elapsed_ms"] == 1_184_000
