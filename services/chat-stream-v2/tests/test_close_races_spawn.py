"""Close of a pane whose sessions row was never persisted (public regression).

Close can arrive between `tmux new-session` and the spawn's row write: the pane
is live, but `fetch_session` finds nothing and `mark_closed`'s UPDATE matches no
row. The kill still happened, so the daemon answers `close.ok` — but it must not
answer with `session: null`; a caller keys on the session descriptor.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import pytest

from ledger import Ledger  # noqa: E402
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402

_EXAMPLE_DEPENDENCY_AVAILABLE = False

HOST = "hosta"
NAME = "v2-orphan"


class FakeTmux:
    """A live pane with no sessions row, killed on demand."""

    def __init__(self) -> None:
        self.alive = True

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def pane_pid(self, name: str) -> str:
        return ""

    async def kill_session(self, name: str) -> None:
        self.alive = False


def test_close_of_never_persisted_row_never_returns_null_session() -> None:
    async def _go() -> dict:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, tmux=FakeTmux(), local_host=HOST)
            # No open_session call: the row does not exist, exactly as when a
            # spawn has created the pane but not yet written its row.
            return await sessions.close(HOST, NAME)
        finally:
            store.stop()

    reply = asyncio.run(_go())
    assert reply["failed"] is False
    assert reply["already_closed"] is False
    session = reply["session"]
    assert session is not None, "close.ok must never carry a null session (public regression)"
    assert session["stream_id"] == f"{HOST}:{NAME}"
    assert session["status"] == "closed"


def test_stale_release_does_not_delete_a_reopened_reservation() -> None:
    async def run() -> list[dict]:
        store = Store(":memory:")
        store.start()
        try:
            assert await store.reserve_stream_id(HOST, NAME, ttl_s=60, request_id="rA")
            assert await store.release_stream_id_fenced(HOST, NAME, "rA")
            assert await store.reserve_stream_id(HOST, NAME, ttl_s=60, request_id="rB")
            assert not await store.release_stream_id_fenced(HOST, NAME, "rA")
            return await store.reservations(include_expired=True)
        finally:
            store.stop()

    reservations = asyncio.run(run())
    assert [(row["host"], row["session_name"], row["request_id"]) for row in reservations] == [
        (HOST, NAME, "rB"),
    ]


@pytest.mark.skipif(
    not _EXAMPLE_DEPENDENCY_AVAILABLE,
    reason="optional specification resolver is not part of this public fixture",
)
def test_close_reprobes_store_over_self_report(tmp_path, monkeypatch) -> None:
    async def _go() -> dict:
        memory = tmp_path / "memory"
        memory.mkdir()
        (memory / "spec.md").write_text(
            "- [ ] check-1\n- [ ] check-2\n- [ ] check-3\n- [ ] check-4\n- [ ] validation\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=memory, check=True)
        subprocess.run(["git", "add", "spec.md"], cwd=memory, check=True)
        subprocess.run(
            ["git", "-c", "user.name=pytest", "-c", "user.email=user@example.com",
             "commit", "-q", "-m", "fixture"],
            cwd=memory, check=True,
        )
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=memory, text=True).strip()
        monkeypatch.setenv("EXAMPLE_MEMORY_ROOT", str(memory))

        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host=HOST)
            await store.open_session(HOST, "parent", visibility="hidden")
            await store.open_session(
                HOST, "child", visibility="hidden", parent_stream_id=f"{HOST}:parent",
            )
            await sessions.refresh()
            reply = await Ledger(store, sessions=sessions).report({
                "report_id": "close-race-authority",
                "from_stream_id": f"{HOST}:parent",
                "msg_id": 1,
                "status": "done",
                "summary": "all ACs checked and children reaped",
                "findings": [],
                "next_action": "continue",
                "terminate": True,
                "ac_claim": {
                    "spec_id": "example_spec__close_truth",
                    "spec_source": {"path": "spec.md", "sha": sha},
                    "claims": [{"index": i, "checked": True} for i in range(1, 6)],
                },
            })
            return reply
        finally:
            store.stop()

    reply = asyncio.run(_go())
    assert reply["ac_claim_verified"] == "mismatch"
    assert reply["ac_claim_mismatch"]["unverified_indices"] == [1, 2, 3, 4, 5]
    assert reply["live_children"] == [{
        "stream_id": f"{HOST}:child",
        "host": HOST,
        "status": "open",
        "parent_stream_id": f"{HOST}:parent",
    }]
