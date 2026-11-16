"""Portable router transport and fixture contract, with no private imports."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from assistant_router import AssistantRouterAdapter


def test_router_fixture_covers_30_meaningful_lane_and_ambiguity_cases() -> None:
    cases = json.loads((Path(__file__).parent / "fixtures" / "assistant_router_v1_cases.json").read_text())
    assert len(cases) >= 30
    assert {case["expected"]["disposition"] for case in cases} == {
        "lane", "new_topic", "clarify", "conversation", "defer",
    }
    for case in cases:
        result = case["expected"]
        assert set(result) == {"schema_version", "disposition", "lane_id", "depends_on_message_id", "reason"}
        assert result["schema_version"] == "assistant-router/v1"
        if result["disposition"] == "lane":
            assert result["lane_id"] in case["lanes"]
            assert result["depends_on_message_id"] is None
        elif result["disposition"] == "defer":
            assert result["depends_on_message_id"] in case["unresolved"]
            assert result["lane_id"] is None
        else:
            assert result["lane_id"] is None and result["depends_on_message_id"] is None


def test_adapter_requires_private_action_path_and_bounds_transport(monkeypatch) -> None:
    try:
        AssistantRouterAdapter("ssh://fixture-router/assistant-router-v1", timeout_s=10, action_path="")
    except ValueError as exc:
        assert str(exc) == "assistant_router_action_path_invalid"
    else:  # pragma: no cover
        raise AssertionError("adapter accepted no private action path")

    adapter = AssistantRouterAdapter(
        "ssh://fixture-router/assistant-router-v1", timeout_s=1,
        action_path="/opt/pentacle/local_actions.py",
    )

    class HangingProcess:
        returncode = None

        async def communicate(self, _payload):
            await asyncio.Event().wait()

        def kill(self):
            return None

        async def wait(self):
            return None

    async def fake_exec(*_args, **_kwargs):
        return HangingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def _go() -> None:
        try:
            await adapter.classify({"schema_version": "assistant-router/v1"})
        except TimeoutError as exc:
            assert str(exc) == "assistant_router_timeout"
        else:  # pragma: no cover
            raise AssertionError("bounded adapter did not timeout")

    asyncio.run(_go())
