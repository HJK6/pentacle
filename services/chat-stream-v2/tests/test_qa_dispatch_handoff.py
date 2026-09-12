"""QA reject limits do not freeze actual handoffs or non-QA spawns."""

import asyncio
import hashlib

import pytest

import launch
from spawnctl import SpawnCtl
from test_spec_binding_provenance import SpawnTmux
from test_window_schedule_contract import RouteProbeSpecs
from test_qa_dispatch_counter import LEAD, SPEC, TOKEN, state, two_rejects, issue


@pytest.mark.parametrize("kind", ["worker", "handoff", "scheduled_handoff"])
def test_real_spawn_exemptions(monkeypatch, tmp_path, kind):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, _, comms, ledger, server = await state()

        class Tmux(SpawnTmux):
            async def stage_text(self, path, data):
                pass

            async def capture(self, name):
                return "❯\n⏵⏵ bypass permissions"

        tmux = Tmux()
        machine = launch.local_machine(
            "localhost",
            cwd=str(tmp_path),
            claude_bin="/test/claude",
            projects_root=str(tmp_path),
            agent_orch_bin_dir="/test/bin",
        )
        ctl = SpawnCtl(
            store, sessions, tmux=tmux, machine=machine, specs=RouteProbeSpecs()
        )
        try:
            await two_rejects(comms, ledger, server)
            sessions.tmux = tmux
            msg = {
                "objective": "Continue owned work",
                "session_name": "exempt-" + kind,
                "request_id": "exempt-" + kind,
                "spec_id": SPEC,
                "provider": "claude",
                "model": "claude-opus-4-8",
                "effort": "high",
            }
            if kind == "worker":
                msg.update(role="worker", parent_stream_id=LEAD, stream_token=TOKEN)
            else:
                token = "handoff-source-token"
                await sessions.open(
                    "localhost",
                    "source",
                    role="qa",
                    provider="claude",
                    effective_model="claude-opus-4-8",
                    effective_effort="high",
                    spec_id=SPEC,
                    spec_ids=[SPEC],
                    spec_binding_provenance=[
                        {
                            "spec_id": SPEC,
                            "provenance": "spawn_explicit",
                            "granting_principal": "operator",
                            "granted_at": "2026-09-12T00:00:00Z",
                        }
                    ],
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    token_hash_version="sha256:v1",
                )
                tmux.live.add("source")
                msg.update(
                    handoff=True,
                    handoff_from_stream_id="localhost:source",
                    stream_token=token,
                    caller_stream_id="localhost:source",
                    from_stream_id="localhost:source",
                )
            if kind == "scheduled_handoff":
                from window_schedule import WindowSchedule
                from datetime import datetime, timedelta, timezone
                import uuid

                schedule = WindowSchedule(
                    store, sessions, comms, ctl, local_host="localhost"
                )
                schedule.mark_store_ready()
                scheduled = {
                    **msg,
                    "type": "schedule.insert",
                    "request_id": str(uuid.uuid4()),
                    "target_host": "localhost",
                    "spec_ids": [SPEC],
                    "fires_at_utc": (
                        datetime.now(timezone.utc) + timedelta(minutes=10)
                    ).isoformat(),
                    "_auth_context": {
                        "token_verified": True,
                        "stream_id": "localhost:source",
                    },
                }
                inserted = await schedule.schedule_insert(scheduled)
                result = await schedule._fire_schedule(
                    inserted["schedule"]["schedule_id"]
                )
                assert result["schedule"]["state"] == "fired"
                assert len(tmux.live) == 1
            else:
                result = await ctl.spawn(msg, "localhost")
                assert result["type"] == "spawn.ok"
                assert "exempt-" + kind in tmux.live
            assert len((await issue(server, "show"))["commissions"]) == 2
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            for name in tuple(tmux.live):
                await tmux.kill_session(name)
            store.stop()

    asyncio.run(run())
