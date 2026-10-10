"""Synthetic lane/card publication must stay off the operator connection."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sys

import pytest

import spawn_fleet_smoke
from tools.disposable_smoke import disposable_composite, guard_target


def test_operator_target_refused_before_state_creation():
    with pytest.raises(ValueError, match="smoke_operator_composite_forbidden"):
        guard_target("smoke-fixture:assistant", "smoke-fixture:assistant")
    with pytest.raises(ValueError, match="smoke_operator_composite_forbidden"):
        guard_target("smoke-fixture:assistant", "")


def test_real_cli_lane_and_card_are_disposable():
    async def run():
        operator = "operator-fixture:assistant"
        async with disposable_composite(operator_composite=operator) as harness:
            target = harness["target"]
            plan = harness["state_path"] / "plan.json"
            plan.write_text(json.dumps([{"adoption_key": "stream:postdeploy-override-smoke-test",
                "title": "Daemon override smoke", "summary": "Disposable override test.",
                "owner_kind": "operator", "work_state": "paused", "visible_chat": {"stream_id": target},
                "no_spec_reason": "One-off deployment override test, not ongoing work."}]))

            async def cli(*args):
                proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_orch.cli", "work-lane", *args,
                    env=harness["cli_env"], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    out, err = await asyncio.wait_for(proc.communicate(), 15)
                except BaseException:
                    proc.kill(); await proc.wait(); raise
                assert proc.returncode == 0, (out, err)
                return json.loads(out)

            result = await cli("adopt", "--apply", str(plan), "--composite-stream-id", target)
            lane = result["response"]["lane"]
            closed = await cli("set-state", lane["lane_id"], "--to", "done", "--outcome", "Override verified.",
                "--reason", "FD verifies the deployed administrative override.", "--expected-version", str(lane["version"]),
                "--request-id", "smoke-close", "--composite-stream-id", target)
            assert closed["lane"]["work_state"] == "done"
            assert closed["event"]["payload"]["override"]["actor_stream_id"] == "smoke-fixture:fd"
            await harness["rpc"]({"type": "assistant.publish", "request_id": "smoke-status",
                "composite_stream_id": target, "publish_kind": "status", "message": "Synthetic smoke status."})
            events = await harness["store"].fetch_session_event_tail(target, limit=100)
            assert {e.get("publish_kind") for e in events} >= {"lane_update", "status"}
            assert await harness["store"].fetch_session_event_tail(operator, limit=100) == []
        assert not harness["state_path"].exists()
        assert harness["server"]._ws_server is None
        assert harness["server"]._tls_ws_server is None
    asyncio.run(run())


def test_cleanup_on_failure():
    async def run():
        with pytest.raises(RuntimeError, match="fixture failure"):
            async with disposable_composite(operator_composite="operator:assistant") as harness:
                raise RuntimeError("fixture failure")
        assert not harness["state_path"].exists()
        assert harness["server"]._ws_server is None
    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError("event failed"),
    spawn_fleet_smoke.CodexQuotaExhausted("fixture-host", None),
    RuntimeError("host unavailable")])
def test_matrix_failure_never_publishes_on_operator_rpc(monkeypatch, error):
    calls = []

    @contextmanager
    def connection(*args):
        def rpc(payload, kind):
            calls.append(payload)
            raise AssertionError("synthetic publication reached live RPC")
        yield rpc, lambda *a: None, lambda *a: None, lambda *a: None, lambda *a: None

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(spawn_fleet_smoke, "_operator_connection", connection)
    monkeypatch.setattr(spawn_fleet_smoke, "run_cell", fail)
    results = spawn_fleet_smoke.run_matrix("ws://unreachable-fixture", Path("unused"), 1,
        cells=(("fixture-host", "codex", "promptless"),))
    assert not calls
    assert len(results) == 1
    assert results[0]["smoke_card"]["operator_event_count"] == 0
    assert results[0]["smoke_card"]["cleaned_up"] is True
