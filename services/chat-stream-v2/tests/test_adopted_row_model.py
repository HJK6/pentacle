"""Adopted rows surface their persisted model."""

from __future__ import annotations

import asyncio
from sessions import Sessions  # noqa: E402
from store import Store  # noqa: E402


def test_adopted_row_serves_persisted_model() -> None:
    """A pre-existing open row with a model persisted is adopted with that model
    non-None and intact — the field a `list_sessions` consumer reads."""

    async def go() -> None:
        store = Store(":memory:")
        store.start()
        try:
            await store.open_session(
                "hostb", "claude-hostb-7e63e83d",
                provider="claude", requested_model="claude-fable-5",
                effective_model="claude-fable-5",
            )
            sessions = Sessions(store, tmux=None, local_host="hostb")

            n = await sessions.refresh()

            assert n == 1
            row = sessions.get("hostb:claude-hostb-7e63e83d")
            assert row is not None
            assert row["effective_model"] == "claude-fable-5"
            assert row["requested_model"] == "claude-fable-5"
            assert row["provider"] == "claude"
        finally:
            store.stop()

    asyncio.run(go())
