"""asset.list session scoping.

The list operation must restrict results to the caller's session, its tagged
specifications, or an explicitly requested specification union.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from assets import Assets  # noqa: E402


def _report_body(title: str = "t") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "title": title,
            "sections": [{"id": "s1", "title": "S", "status": "reference", "blocks": []}],
        }
    )


class _FakeSessions:
    """Stands in for the v2 Sessions store: `.get(stream_id)` returns the row
    with raw `spec_ids`/`spec_id` (TEXT, as the store serves it)."""

    def __init__(self, rows: dict | None = None) -> None:
        self._rows = rows or {}

    def get(self, stream_id: str):
        return self._rows.get(stream_id)


async def _publish(assets: Assets, stream_id: str, title: str, *, spec_id: str | None = None) -> str:
    msg = {"type": "asset.publish", "request_id": "p", "stream_id": stream_id,
           "title": title, "content_type": "report", "body": _report_body(title)}
    if spec_id is not None:
        msg["spec_id"] = spec_id
    reply = await assets.asset(msg)
    assert reply["type"] == "asset.publish.ok", reply
    return reply["asset"]["asset_id"]


async def _list(assets: Assets, **msg) -> dict:
    return await assets.asset({"type": "asset.list", "request_id": "l", **msg})


def _run(sessions: _FakeSessions, coro_factory, tmp_path):
    async def _main():
        assets = Assets(str(tmp_path / "assets.db"), sessions=sessions)
        await assets.start()
        try:
            return await coro_factory(assets)
        finally:
            await assets.stop()

    return asyncio.run(_main())


def test_asset_list_scopes_to_own_session(tmp_path):
    """Two sessions, one asset each → each list returns only its own asset.
    Regression before the fix (list_all_assets returns both)."""

    async def scenario(assets):
        a_id = await _publish(assets, "hosta:sessA", "A")
        b_id = await _publish(assets, "hosta:sessB", "B")
        rep_a = await _list(assets, stream_id="hosta:sessA")
        rep_b = await _list(assets, stream_id="hosta:sessB")
        return a_id, b_id, rep_a, rep_b

    a_id, b_id, rep_a, rep_b = _run(_FakeSessions(), scenario, tmp_path)
    assert rep_a["type"] == "asset.list.ok"
    assert [x["asset_id"] for x in rep_a["assets"]] == [a_id]
    assert [x["asset_id"] for x in rep_b["assets"]] == [b_id]
    assert rep_a["session_key"]["stream_id"] == "hosta:sessA"


def test_asset_list_includes_session_spec_tagged(tmp_path):
    """Session path unions own assets with assets tagged by the session's spec
    ids (sourced from the sessions store), excluding unrelated assets."""

    sessions = _FakeSessions({"hosta:sessA": {"spec_ids": json.dumps(["spec_x"])}})

    async def scenario(assets):
        own = await _publish(assets, "hosta:sessA", "own")
        tagged = await _publish(assets, "hosta:sessB", "tagged", spec_id="spec_x")
        await _publish(assets, "hosta:sessC", "noise")  # unrelated, must be excluded
        rep = await _list(assets, stream_id="hosta:sessA")
        return own, tagged, rep

    own, tagged, rep = _run(sessions, scenario, tmp_path)
    got = {x["asset_id"] for x in rep["assets"]}
    assert got == {own, tagged}, got


def test_asset_list_explicit_spec_ids_union(tmp_path):
    """Explicit specification ids with no session identity return a union and
    leave the session key unset."""

    async def scenario(assets):
        y = await _publish(assets, "hosta:sessA", "y", spec_id="spec_y")
        await _publish(assets, "hosta:sessB", "z", spec_id="spec_z")
        rep = await _list(assets, spec_ids=["spec_y"])
        return y, rep

    y, rep = _run(_FakeSessions(), scenario, tmp_path)
    assert rep["session_key"] is None
    assert [x["asset_id"] for x in rep["assets"]] == [y]
