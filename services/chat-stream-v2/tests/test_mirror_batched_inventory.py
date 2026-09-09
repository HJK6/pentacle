from __future__ import annotations

import asyncio
from typing import Any

from mirror import Mirror


class _Store:
    async def fetch_session_event_tail(self, *_args: Any, **_kwargs: Any) -> list[dict]:
        return []

    async def update_session(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Sessions:
    def __init__(self) -> None:
        self.rows = {
            f"hosta:{name}": {"objective": "Exercise the existing spawn contract",
                "stream_id": f"hosta:{name}",
                "host": "hosta",
                "session_name": name,
                "session_generation": f"gen-{name}",
                "provider": "claude",
                "online": True,
            }
            for name in ("alpha", "beta", "gone")
        }

    def list_open(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.values()]

    def get(self, stream_id: str) -> dict[str, Any] | None:
        return self.rows.get(stream_id)

    @staticmethod
    def split(stream_id: str) -> tuple[str, str]:
        return tuple(stream_id.split(":", 1))  # type: ignore[return-value]

    def apply_live(self, stream_id: str, **overlay: Any) -> dict[str, Any] | None:
        row = self.rows.get(stream_id)
        if row is None:
            return None
        row.update(overlay)
        return dict(row)

    def restore_genuine_activity(self, *_args: Any) -> None:
        return None


class _Tmux:
    def __init__(self) -> None:
        self.run_calls = 0
        self.pane_pid_calls = 0
        self.has_session_calls = 0
        self.capture_calls: list[str] = []

    async def run(self, *args: str, **_kwargs: Any) -> tuple[int, str]:
        self.run_calls += 1
        assert args == (
            "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}",
        )
        return 0, "alpha\t101\nbeta\t202\n"

    async def pane_pid(self, name: str) -> str:
        self.pane_pid_calls += 1
        return {"alpha": "101", "beta": "202"}.get(name, "")

    async def has_session(self, name: str) -> bool:
        self.has_session_calls += 1
        return name != "gone"

    async def capture(self, name: str) -> str:
        self.capture_calls.append(name)
        return f"idle {name}\n❯ \n"


def test_one_inventory_subprocess_proves_liveness_without_per_session_capture() -> None:
    async def run() -> tuple[int, _Tmux, _Sessions, list[dict[str, Any]]]:
        sessions = _Sessions()
        tmux = _Tmux()
        frames: list[dict[str, Any]] = []

        async def broadcast(frame: dict[str, Any]) -> None:
            frames.append(frame)

        observed = await Mirror(
            _Store(), sessions, tmux, broadcast, local_host="hosta",
        ).run_pass()
        return observed, tmux, sessions, frames

    observed, tmux, sessions, frames = asyncio.run(run())

    assert observed == 3
    assert tmux.run_calls == 1
    assert tmux.pane_pid_calls == 0
    assert tmux.has_session_calls == 0
    assert tmux.capture_calls == []
    assert sessions.rows["hosta:gone"]["pane_status"] == "pane_dead"
    assert any(
        frame.get("type") == "session.died" and frame.get("stream_id") == "hosta:gone"
        for frame in frames
    )


def test_empty_tmux_server_is_an_authoritative_empty_inventory() -> None:
    class EmptyTmux(_Tmux):
        async def run(self, *args: str, **_kwargs: Any) -> tuple[int, str]:
            self.run_calls += 1
            return 1, "no server running on /tmp/tmux-1000/default"

    async def run() -> tuple[_Sessions, list[dict[str, Any]]]:
        sessions = _Sessions()
        tmux = EmptyTmux()
        frames: list[dict[str, Any]] = []

        async def broadcast(frame: dict[str, Any]) -> None:
            frames.append(frame)

        assert await Mirror(
            _Store(), sessions, tmux, broadcast, local_host="hosta",
        ).run_pass() == 3
        return sessions, frames

    sessions, frames = asyncio.run(run())

    assert all(row["pane_status"] == "pane_dead" for row in sessions.rows.values())
    assert {
        frame["stream_id"] for frame in frames if frame.get("type") == "session.died"
    } == set(sessions.rows)
