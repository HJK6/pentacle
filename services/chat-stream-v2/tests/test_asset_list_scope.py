"""asset.list session scoping.

Chat lists follow ownership; explicit specification lists remain separate.
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
        self.store = self

    def get(self, stream_id: str):
        return self._rows.get(stream_id)

    async def list_sessions(self, status=None):
        assert status is None
        return [{"stream_id": sid, **row} for sid, row in self._rows.items()]


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


def _run(sessions: _FakeSessions, coro_factory, tmp_path, **kwargs):
    async def _main():
        assets = Assets(str(tmp_path / "assets.db"), sessions=sessions, **kwargs)
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


def test_asset_list_excludes_unrelated_session_spec_tagged(tmp_path):
    """Shared tags cannot put another chat's reports in this chat."""

    sessions = _FakeSessions({"hosta:sessA": {"spec_ids": json.dumps(["spec_x"])}})

    async def scenario(assets):
        own = await _publish(assets, "hosta:sessA", "own")
        tagged = await _publish(assets, "hosta:sessB", "tagged", spec_id="spec_x")
        await _publish(assets, "hosta:sessC", "noise")  # unrelated, must be excluded
        rep = await _list(assets, stream_id="hosta:sessA")
        return own, tagged, rep

    own, tagged, rep = _run(sessions, scenario, tmp_path)
    got = {x["asset_id"] for x in rep["assets"]}
    assert got == {own}, got


def test_asset_list_includes_closed_cross_host_descendants_not_siblings(tmp_path):
    sessions = _FakeSessions({
        "hosta:lead": {"spec_id": "shared"},
        "hostb:child": {"parent_stream_id": "hosta:lead", "status": "closed"},
        "hostb:grandchild": {"parent_stream_id": "hostb:child"},
        "hosta:sibling": {"spec_id": "shared"},
    })

    async def scenario(assets):
        own = await _publish(assets, "hosta:lead", "own")
        child = await _publish(assets, "hostb:child", "child")
        grandchild = await _publish(assets, "hostb:grandchild", "grandchild")
        await _publish(assets, "hosta:sibling", "foreign", spec_id="shared")
        rep = await _list(assets, stream_id="hosta:lead", spec_ids=["shared"])
        return {own, child, grandchild}, rep

    expected, rep = _run(sessions, scenario, tmp_path)
    assert {x["asset_id"] for x in rep["assets"]} == expected
    assert set(rep["scope_stream_ids"]) == {"hosta:lead", "hostb:child", "hostb:grandchild"}


def test_composite_scope_tracks_generation_bound_root_and_transfer_descendants(tmp_path):
    sessions = _FakeSessions({
        "hosta:old": {"session_generation": "g-old"},
        "hostb:new": {"session_generation": "g-new", "parent_stream_id": "hosta:old"},
        "hostc:worker": {"parent_stream_id": "hostb:new", "status": "closed"},
        "hosta:other": {"session_generation": "g-other", "spec_id": "shared"},
    })
    binding = {"stream_id": "hosta:old", "generation": "g-old"}

    async def roots(viewer):
        return [binding] if viewer == "assistant:chat" else []

    async def scenario(assets):
        old = await _publish(assets, "hosta:old", "old")
        new = await _publish(assets, "hostb:new", "new")
        worker = await _publish(assets, "hostc:worker", "worker")
        other = await _publish(assets, "hosta:other", "other", spec_id="shared")
        first = await _list(assets, stream_id="assistant:chat")
        binding.update(stream_id="hosta:other", generation="stale")
        stale = await _list(assets, stream_id="assistant:chat")
        binding["generation"] = "g-other"
        rebound = await _list(assets, stream_id="assistant:chat")
        return {old, new, worker}, other, first, stale, rebound

    expected, other, first, stale, rebound = _run(sessions, scenario, tmp_path, chat_roots=roots)
    assert {r["asset_id"] for r in first["assets"]} == expected
    assert stale["assets"] == []
    assert [r["asset_id"] for r in rebound["assets"]] == [other]


def test_handoff_continuity_cycles_duplicate_ids_and_global_limit(tmp_path):
    sessions = _FakeSessions({
        "hosta:old": {"parent_stream_id": "hostb:child"},
        "hosta:new": {"handoff_from_stream_id": "hosta:old"},
        "hostb:child": {"parent_stream_id": "hosta:new"},
    })

    async def scenario(assets):
        for sid in ("hosta:old", "hosta:new", "hostb:child", "hostc:unrelated"):
            reply = await assets.asset({"type": "asset.publish", "stream_id": sid,
                                        "asset_id": "same", "title": sid,
                                        "content_type": "report", "body": _report_body()})
            assert reply["type"] == "asset.publish.ok", reply
        full = await _list(assets, stream_id="hosta:new")
        limited = await _list(assets, stream_id="hosta:new", limit=1)
        empty = await _list(assets, stream_id="hosta:new", limit=0)
        return full, limited, empty

    full, limited, empty = _run(sessions, scenario, tmp_path)
    assert {r["stream_id"] for r in full["assets"]} == {"hosta:old", "hosta:new", "hostb:child"}
    assert limited["assets"] == full["assets"][:1]
    assert empty["assets"] == []


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
