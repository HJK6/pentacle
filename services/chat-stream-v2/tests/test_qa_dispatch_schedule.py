"""Real schedule persistence/fire into the real QA admission path."""

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest

import launch
from sessions import VerbError
from spawnctl import SpawnCtl
from window_schedule import WindowSchedule
from test_spec_binding_provenance import SpawnTmux
from test_window_schedule_contract import RouteProbeSpecs
from test_qa_dispatch_counter import SPEC, LEAD, TOKEN, META, state, two_rejects, issue


def message(verb, **extra):
    return {
        "type": verb,
        "request_id": str(uuid.uuid4()),
        "from_stream_id": LEAD,
        "stream_token": TOKEN,
        "_auth_context": {"stream_id": LEAD, "token_verified": True},
        **extra,
    }


@pytest.mark.parametrize(
    "case",
    [
        "rejects",
        "stale",
        "owner_generation",
        "replay",
        "rearm",
        "worker",
        "off_legacy",
        "restart",
    ],
)
def test_schedule_persists_scope_and_checks_at_fire(monkeypatch, tmp_path, case):
    monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "enforce")

    async def run():
        store, sessions, _, comms, ledger, server = await state(
            tmp_path / "schedule.db"
        )

        class ScheduledTmux(SpawnTmux):
            async def stage_text(self, path, data):
                pass  # provider token counterpart; never write a real token file

            async def capture(self, name):
                return "❯ \n⏵⏵ bypass permissions"

        tmux = ScheduledTmux()
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
        schedule = WindowSchedule(store, sessions, comms, ctl, local_host="localhost")
        schedule.mark_store_ready()
        # Scheduled ownership requires qualified provenance, as in production.
        await store.update_session(
            "localhost",
            "lead",
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
        )
        # Sessions' projection otherwise still has the pre-update fixture.
        await sessions.refresh()
        try:
            fields = dict(META)
            if case in ("worker", "off_legacy"):
                fields = {}
            if case == "off_legacy":
                monkeypatch.setenv("PENTACLE_QA_DISPATCH_MODE", "off")
            insert = message(
                "schedule.insert",
                objective="Review scheduled QA surface",
                fires_at_utc=(
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
                target_host="localhost",
                provider="claude",
                model="claude-opus-4-8",
                effort="high",
                role="worker" if case == "worker" else "qa",
                spec_ids=[SPEC],
                parent_stream_id=LEAD,
                **fields
            )
            inserted = await schedule.schedule_insert(insert)
            row = inserted["schedule"]
            sid = row["schedule_id"]
            if fields:
                assert (
                    row["qa_spec_id"] == SPEC
                    and row["qa_surface"] == "admission"
                    and row["qa_cycle"] == 1
                )
                assert (
                    row["qa_owner_generation"]
                    == (await store.fetch_session("localhost", "lead"))[
                        "session_generation"
                    ]
                )
            assert (await issue(server, "show"))[
                "commissions"
            ] == []  # insertion reserved no QA pass
            if case == "restart":
                from store import Store

                store.stop()
                reopened = Store(str(tmp_path / "schedule.db"))
                reopened.start()
                try:
                    restored = WindowSchedule(
                        reopened, sessions, comms, ctl, local_host="localhost"
                    )
                    restored.mark_store_ready()
                    actual = (
                        await restored.schedule_get(
                            message("schedule.get", schedule_id=sid)
                        )
                    )["schedule"]
                    assert {k: actual[k] for k in (*META, "qa_owner_generation")} == {
                        k: row[k] for k in (*META, "qa_owner_generation")
                    }
                finally:
                    reopened.stop()
                return
            if case in ("rejects", "stale", "rearm", "worker", "off_legacy"):
                await two_rejects(comms, ledger, server)
            if case == "stale":
                await issue(
                    server,
                    "diagnose",
                    diagnosis_id="scheduled-pivot",
                    diagnosis="cause",
                    pivot="repair",
                )
            if case == "owner_generation":
                await store.update_session("localhost", "lead", status="closed")
                await sessions.open(
                    "localhost",
                    "lead",
                    role="lead",
                    spec_id=SPEC,
                    session_generation="reopened-owner",
                )
            if case == "rearm":
                row = (
                    await schedule.schedule_reschedule(
                        message(
                            "schedule.reschedule",
                            schedule_id=sid,
                            fires_at_utc=(
                                datetime.now(timezone.utc) + timedelta(minutes=20)
                            ).isoformat(),
                        )
                    )
                )["schedule"]
                assert row["generation"] == 2
            run_msg = message("schedule.run", schedule_id=sid)
            if case in ("rejects", "stale", "rearm", "owner_generation"):
                with pytest.raises(VerbError) as exc:
                    await schedule._fire_schedule(sid)
                result = exc.value.extra.get("spawn", {})
                expected = (
                    "qa_cycle_conflict"
                    if case == "stale"
                    else (
                        "qa_unauthorized"
                        if case == "owner_generation"
                        else "qa_dispatch_reject_limit"
                    )
                )
                assert result.get("error_code") == expected, (
                    exc.value.code,
                    exc.value.extra,
                )
                assert not tmux.live
            else:
                fired = await schedule.schedule_run(run_msg)
                assert fired["schedule"]["state"] == "fired", fired
                count = len(tmux.live)
                replay = await schedule.schedule_run(run_msg)
                assert replay == fired
                assert len(tmux.live) == count == 1
                if case == "replay":
                    assert len((await issue(server, "show"))["commissions"]) == 1
        finally:
            await asyncio.gather(*tuple(ctl._background_spawns), return_exceptions=True)
            for name in tuple(tmux.live):
                await tmux.kill_session(name)
            store.stop()

    asyncio.run(run())
