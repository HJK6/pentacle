"""Synthetic replay of observed refusal classes; original request inputs were unavailable."""
from _shared.spawn_objective import resolve_objective


def test_missing_objective_is_derived_for_parented_spawn():
    objective, source, error = resolve_objective(
        None, objective_supported=True, parent_stream_id="host:parent",
        brief="Implement the requested work\nFurther details")
    assert (objective, source, error) == ("Implement the requested work", "derived", None)


def test_long_objective_is_truncated():
    objective, source, error = resolve_objective(
        "x" * 200, objective_supported=True, parent_stream_id="host:parent", brief="brief")
    assert (objective, source, error) == ("x" * 120, "explicit", None)


def test_failed_key_can_be_retried_while_successful_key_replays():
    import asyncio
    from store import Store

    async def run():
        store = Store(":memory:")
        store.start()
        try:
            for key, state in (("failed-key", "failed"), ("delivered-key", "delivered")):
                await store.set_spawn_outcome("host", key, state, reason="boot_not_ready" if state == "failed" else "",
                                              idempotency_key=key)
            failed = await store.atomic_claim_or_replay("host", "retry", idempotency_key="failed-key",
                                                        request_payload_hash="", ttl_s=60)
            delivered = await store.atomic_claim_or_replay("host", "duplicate", idempotency_key="delivered-key",
                                                           request_payload_hash="", ttl_s=60)
            assert failed["status"] == "claimed"
            assert delivered["status"] == "replay"
        finally:
            store.stop()
    asyncio.run(run())


def test_operator_request_found_under_tracking_heading_variants(monkeypatch):
    import _shared.spec_reader as spec_reader
    from assistant_lane_rulings import _work_item

    for heading, line in (("# Tracking", 'Operator request: "Ship the change"'),
                          ("### Tracking", '- **Operator request**: "Ship the change"'),
                          ("## Tracking and decisions", "Operator request — Ship the change")):
        text = f'{heading}\n{line}\n## Goal\nOther text\n'
        class Reader:
            def __init__(self, root): pass
            def read(self, spec_id): return "in_progress", None, None, text
        monkeypatch.setattr(spec_reader, "SpecReader", Reader)
        item, error = _work_item("spec_example")
        assert error is None
        assert item["origin"] == "Ship the change"



def test_owed_disposition_close_succeeds_with_warning():
    import asyncio
    from test_lead_close_gate import environment, request

    async def run():
        store, sessions, server, tmux, row = await environment()
        try:
            result = await server._on_close(request(row))
            assert result["type"] == "close.ok"
            assert any(w["kind"] == "disposition" for w in result["warnings"])
            assert tmux.kills == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_missing_report_still_requests_advisor_and_close_warns():
    import asyncio
    import json
    from test_approved_close_retry import journey

    async def run():
        async with journey() as env:
            await env.store.submit(lambda conn: conn.execute("DELETE FROM v2_reports"))
            pending = await env.request()
            assert pending["type"] == "close.pending_ruling"
            env.tmux.text = "ready\n"
            await env.approve()
            row = await env.row()
            assert row["state"] == "done"
            result = json.loads(row["outcome_json"])
            assert any(w["kind"] == "report" for w in result["warnings"])
    asyncio.run(run())


def test_unknown_disposition_warns_instead_of_refusing():
    import asyncio
    from test_lead_close_gate import environment, request

    async def run():
        store, sessions, server, tmux, row = await environment()
        async def unavailable(*args, **kwargs):
            raise TimeoutError("store unavailable")
        sessions.store.lead_close_context = unavailable
        sessions.store.lead_close_contexts = unavailable
        try:
            result = await server._on_close(request(row))
            assert result["type"] == "close.ok"
            assert {"kind": "disposition", "status": "unknown"} in result["warnings"]
        finally:
            store.stop()
    asyncio.run(run())


def test_close_warns_for_each_unfinished_assignment():
    import asyncio
    from test_lead_close_gate import environment, request

    async def run():
        store, sessions, server, tmux, row = await environment()
        async def contexts(*args, **kwargs):
            return [dict(spec_id=spec, status="in_progress", observed_at="now", document_revisions={},
                         stream_id=request(row)["stream_id"], session_generation="fixture") for spec in ("spec_one", "spec_two")]
        store.lead_close_contexts = contexts
        try:
            result = await server._on_close(request(row))
            assert {w["spec_id"] for w in result["warnings"] if w["kind"] == "disposition"} == {"spec_one", "spec_two"}
        finally:
            store.stop()
    asyncio.run(run())


def test_tracking_origin_skips_fenced_examples(monkeypatch):
    import _shared.spec_reader as spec_reader
    from assistant_lane_rulings import _work_item
    text = '## Tracking\n```text\nOperator request: example only\n```\nOperator request: actual work\n'
    class Reader:
        def __init__(self, root): pass
        def read(self, spec_id): return "in_progress", None, None, text
    monkeypatch.setattr(spec_reader, "SpecReader", Reader)
    item, _ = _work_item("spec_example")
    assert item["origin"] == "actual work"


def test_parented_schedule_preserves_derived_provenance():
    import asyncio
    from tests.test_spawn_objective_compat import EchoPane
    from sessions import Sessions
    from spawnctl import SpawnCtl
    from store import Store

    async def run():
        store = Store(); store.start(); pane = EchoPane()
        try:
            sessions = Sessions(store, tmux=pane)
            await sessions.open("localhost", "parent")
            ctl = SpawnCtl(store, sessions, tmux=pane)
            await ctl.spawn({"command": "run", "session_name": "scheduled", "request_id": "schedule-derived",
                "parent_stream_id": "localhost:parent", "objective": "Frozen derived goal",
                "objective_source": "derived", "_auth_context": {"service_authenticated": True,
                "service_actor": "daemon:scheduler"}}, "localhost")
            await asyncio.gather(*list(ctl._background_spawns))
            row = await store.fetch_session("localhost", "scheduled")
            assert row["objective_source"] == "derived"
        finally:
            if pane.alive:
                await pane.kill_session("scheduled")
            store.stop()
    asyncio.run(run())


def test_advisor_release_rederives_absent_objective(tmp_path):
    import asyncio
    from front_desk_ingress_replay import harness, HOST, DESK
    from _shared.spawn_objective import resolve_objective

    async def run():
        async with harness(tmp_path) as h:
            result = await h["dispatch"]({"type": "spawn", "host": HOST, "session_name": "derived",
                "request_id": "derived", "idempotency_key": "derived", "spec_id": "spec_fixture__ingress",
                "role": "lead", "parent_stream_id": DESK, "objective_supported": True,
                "initial_prompt": "Deliver the task\nDetails"}, "desk")
            assert result["type"] == "spawn.pending_ruling"
            advisor = await h["store"].fetch_session(HOST, "advisor")
            await h["server"]._on_assistant_ruling({"request_id": "approve", "ruling_request_id": result["ruling_request_id"],
                "ruling": "approve", "_auth_context": {"token_verified": True, "stream_id": f"{HOST}:advisor",
                "session_generation": advisor["session_generation"]}})
            intent, _ = h["spawned"].calls[0]
            assert intent.get("objective") is None
            assert resolve_objective(intent.get("objective"), objective_supported=True,
                parent_stream_id=DESK, brief=intent["initial_prompt"])[:2] == ("Deliver the task", "derived")
    asyncio.run(run())


def test_advisor_long_objective_same_key_retry_reuses_ruling(tmp_path):
    import asyncio
    from front_desk_ingress_replay import harness, HOST, DESK

    async def run():
        async with harness(tmp_path) as h:
            msg = {"type": "spawn", "host": HOST, "session_name": "long", "request_id": "long",
                "idempotency_key": "long", "spec_id": "spec_fixture__ingress", "role": "lead",
                "parent_stream_id": DESK, "objective_supported": True, "objective": "x" * 200}
            first = await h["dispatch"](msg, "desk")
            assert first["type"] == "spawn.pending_ruling"
            retry = await h["dispatch"]({**msg, "request_id": "long-retry"}, "desk")
            assert retry["ruling_request_id"] == first["ruling_request_id"]
    asyncio.run(run())


def test_advisor_does_not_trust_caller_derived_provenance(tmp_path):
    import asyncio
    from front_desk_ingress_replay import harness, HOST, DESK

    async def run():
        async with harness(tmp_path) as h:
            result = await h["dispatch"]({"type": "spawn", "host": HOST, "session_name": "explicit",
                "request_id": "explicit", "idempotency_key": "explicit", "spec_id": "spec_fixture__ingress",
                "role": "lead", "parent_stream_id": DESK, "objective_supported": True,
                "objective": "Explicit task", "objective_source": "derived", "initial_prompt": "Other brief"}, "desk")
            advisor = await h["store"].fetch_session(HOST, "advisor")
            await h["server"]._on_assistant_ruling({"request_id": "approve", "ruling_request_id": result["ruling_request_id"],
                "ruling": "approve", "_auth_context": {"token_verified": True, "stream_id": f"{HOST}:advisor",
                "session_generation": advisor["session_generation"]}})
            intent, _ = h["spawned"].calls[0]
            assert intent["objective"] == "Explicit task"
            assert intent.get("objective_source") != "derived"
    asyncio.run(run())


def test_concurrent_owner_closes_warn_and_preserve_owed_work():
    import asyncio
    from test_lead_close_gate import environment, request, HOST

    async def run():
        store, sessions, server, tmux, row = await environment()
        other = await store.open_session(HOST, "other", role="lead", assigned_spec_id="spec_demo__item_00")
        ready, release = asyncio.Event(), asyncio.Event()
        initial, passed = sessions._guard_lead_disposition, []
        async def barrier(*args, **kwargs):
            result = await initial(*args, **kwargs)
            passed.append(True)
            if len(passed) == 2: ready.set()
            await release.wait()
            return result
        sessions._guard_lead_disposition = barrier
        tasks = [asyncio.create_task(server._on_close(request(owner))) for owner in (row, other)]
        try:
            await asyncio.wait_for(ready.wait(), 2)
            release.set()
            replies = await asyncio.gather(*tasks)
            assert all(r["type"] == "close.ok" for r in replies)
            assert any(w.get("spec_id") == "spec_demo__item_00" for r in replies for w in r["warnings"])
            assert (await store.work_owed())["total"] == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            store.stop()
    asyncio.run(run())
