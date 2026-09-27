"""Direct pin changes preserve resolved dispatch authority and durable routing."""
from __future__ import annotations

import asyncio

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store


CHAT = "fixture-chat:assistant"
A = "fixture-a:visible"
B = "fixture-b:visible"
C = "fixture-c:visible"
PORTFOLIO_SPEC = "spec_pentacle__bart_portfolio_coordination_2026_09"


async def _seat(store, stream_id, **extra):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol",
        effective_effort="high", **extra,
    )


def _config(generation):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": A,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
    })


def _request(request_id, revision, target=None, *, clear=False):
    return {"type": "assistant.rebind", "request_id": request_id,
            "expected_revision": revision, "target_stream_id": target, "clear": clear}


async def _resolved(store, input_id):
    for _ in range(100):
        route = await store.get_assistant_composite_route(
            stream_id=CHAT, input_identity=input_id,
        )
        if route and route["routing_state"] == "resolved":
            return route
        await asyncio.sleep(0.01)
    raise AssertionError("direct route did not resolve")


def test_rebind_changes_next_route_but_old_final_keeps_dispatch_authority(tmp_path):
    async def run():
        db = tmp_path / "sessions.db"
        store = Store(str(db))
        store.start()
        try:
            a, b = await _seat(store, A), await _seat(store, B)
            delivered = []
            async def dispatch(route):
                delivered.append(route)
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(a["session_generation"]), dispatch=dispatch)
            await composite.load_binding()
            await composite.ensure_projection()
            await composite.accept_input(
                {"text": "old", "request_id": "old-rpc", "optimistic_id": "old-input"},
                operator_principal="operator:fixture",
            )
            old = await _resolved(store, "old-input")
            assert old["route_target"] == A
            first = await composite.rebind(
                _request("move-a-b", 0, B), actor_stream_id=A,
            )
            assert first["new_binding"]["stream_id"] == B
            assert first["new_binding"]["generation"] == b["session_generation"]
            assert (await composite.rebind(
                _request("move-a-b", 0, B), actor_stream_id=A,
            ))["duplicate"] is True
            with pytest.raises(ValueError, match="assistant_rebind_request_conflict"):
                await composite.rebind(_request("move-a-b", 0, B), actor_stream_id=B)
            assert (await composite.rebind(
                _request("move-a-b", 1, B), actor_stream_id=A,
            ))["duplicate"] is True
            await composite.accept_input(
                {"text": "new", "request_id": "new-rpc", "optimistic_id": "new-input"},
                operator_principal="operator:fixture",
            )
            new = await _resolved(store, "new-input")
            assert new["route_target"] == B
            assert new["route_target_generation"] == b["session_generation"]
            old_final = {
                "request_id": "publish:" + old["dispatch_id"], "composite_stream_id": CHAT,
                "dispatch_id": old["dispatch_id"], "reply_to_message_id": "old-input",
                "publish_kind": "prose", "response_state": "final", "message": "old final",
                "attachment_ids": [], "evidence_refs": [],
            }
            result = await composite.publish(old_final, actor_stream_id=A)
            assert result["event_id"]
            assert (await composite.publish(old_final, actor_stream_id=A))["duplicate"]
            with pytest.raises(ValueError, match="assistant_publish_provenance_unverified"):
                await composite.publish(old_final, actor_stream_id=B)
            events = await store.fetch_session_event_tail(CHAT, limit=10)
            assert [e["text"] for e in events if e["kind"] == "ASSIST_TEXT"] == ["old final"]
            await composite.stop()
        finally:
            store.stop()
        reopened = Store(str(db))
        reopened.start()
        try:
            restored = AssistantComposite(reopened, config=_config(a["session_generation"]))
            assert (await restored.load_binding())["stream_id"] == B
            assert (await restored.binding())["source"] == "durable"
        finally:
            reopened.stop()
    asyncio.run(run())


def test_rebind_cas_conflict_audit_and_clear_after_predecessor_close():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, b, c = await _seat(store, A), await _seat(store, B), await _seat(store, C)
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            await composite.rebind(_request("move-a-b", 0, B), actor_stream_id=A)
            with pytest.raises(ValueError, match="assistant_rebind_request_conflict"):
                await composite.rebind(_request("move-a-b", 1, C), actor_stream_id=A)
            with pytest.raises(ValueError, match="assistant_rebind_stale_revision"):
                await composite.rebind(_request("stale-b-c", 0, C), actor_stream_id=B)
            with pytest.raises(ValueError, match="assistant_rebind_generation_mismatch"):
                await composite.rebind({
                    **_request("bad-generation", 1, C),
                    "target_generation": "retired-generation",
                }, actor_stream_id=B)
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            with pytest.raises(ValueError, match="assistant_rebind_clear_env_unusable"):
                await composite.rebind(_request("clear-closed-env", 1, clear=True), actor_stream_id=B)
            with pytest.raises(ValueError, match="assistant_rebind_actor_closed"):
                await composite.rebind(_request("closed-actor", 1, C), actor_stream_id=A)
            assert (await composite.binding())["stream_id"] == B
            assert (await composite.binding())["revision"] == 1
            audits = await store.submit(lambda conn: [
                dict(row) for row in conn.execute(
                    "SELECT request_id,outcome FROM v2_assistant_rebind_audit ORDER BY audit_id"
                )
            ])
            assert [row["outcome"] for row in audits] == [
                "ok", "assistant_rebind_request_conflict", "assistant_rebind_stale_revision",
                "assistant_rebind_generation_mismatch", "assistant_rebind_clear_env_unusable",
                "assistant_rebind_actor_closed",
            ]
        finally:
            store.stop()
    asyncio.run(run())


def test_hidden_live_front_desk_is_eligible_but_dead_pane_is_not():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a = await _seat(store, A)
            hidden = await store.open_session(
                "fixture-b", "visible", provider="codex", role="assistant",
                visibility="hidden", pane_status="pane_alive",
                effective_model="gpt-6-sol", effective_effort="high",
            )
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            await store.update_session("fixture-b", "visible", pane_status="pane_dead")
            with pytest.raises(ValueError, match="assistant_rebind_target_closed"):
                await composite.rebind(_request("hidden-dead", 0, B), actor_stream_id=A)
            await store.update_session("fixture-b", "visible", pane_status="pane_alive")
            receipt = await composite.rebind(_request("hidden-live", 0, B), actor_stream_id=A)
            assert receipt["new_binding"]["generation"] == hidden["session_generation"]
        finally:
            store.stop()
    asyncio.run(run())


def test_closed_predecessor_cannot_authorize_late_linked_successor(tmp_path):
    async def run():
        store = Store(str(tmp_path / "sessions.db"))
        store.start()
        try:
            a = await _seat(store, A)
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            await _seat(store, B, handoff_from_stream_id=A)
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(_request("late-linked", 0, B), actor_stream_id=B)
            assert (await composite.binding())["stream_id"] == A
            await store.open_session("fixture-c", "visible", provider="codex", role="assistant",
                                     visibility="default", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     spec_id=PORTFOLIO_SPEC,
                                     qualified_spec_ids=[PORTFOLIO_SPEC],
                                     spec_binding_provenance=[{"spec_id": PORTFOLIO_SPEC,
                                         "provenance": "spawn_explicit", "granting_principal": "operator",
                                         "granted_at": "2026-09-27T00:00:00Z"}])
            receipt = await composite.rebind(_request("portfolio-after-close", 0, B), actor_stream_id=C)
            assert receipt["new_binding"]["stream_id"] == B
        finally:
            store.stop()
    asyncio.run(run())


def test_handoff_created_while_predecessor_open_keeps_generation_proof_after_close():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a = await _seat(store, A)
            b = await _seat(store, B, handoff_from_stream_id=A)
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            receipt = await composite.rebind(_request("timely-linked", 0, B), actor_stream_id=B)
            assert receipt["new_binding"]["generation"] == b["session_generation"]
            proof = await store.submit(lambda conn: conn.execute(
                "SELECT predecessor_generation FROM v2_assistant_direct_handoff_proofs "
                "WHERE successor_stream_id=? AND successor_generation=?",
                (B, b["session_generation"]),
            ).fetchone()[0])
            assert proof == a["session_generation"]
        finally:
            store.stop()
    asyncio.run(run())


def test_predecessor_close_terminalizes_unanswered_old_dispatch():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            async def dispatch(_route):
                return {"delivery": "landed"}
            composite = AssistantComposite(store, config=_config(a["session_generation"]), dispatch=dispatch)
            await composite.load_binding()
            await composite.ensure_projection()
            await composite.accept_input(
                {"text": "pending", "request_id": "pending-rpc",
                 "optimistic_id": "pending-input"},
                operator_principal="operator:fixture",
            )
            route = await _resolved(store, "pending-input")
            await composite.rebind(_request("move-before-close", 0, B), actor_stream_id=A)
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            await composite.target_closed(A, a["session_generation"])
            failed = await store.get_assistant_composite_route(
                stream_id=CHAT, input_identity="pending-input",
            )
            assert failed["delivery_state"] == "failed"
            assert failed["error_code"] == "assistant_target_closed"
            assert composite.activity_snapshot()["inputs"]["pending-input"]["response_state"] == "failed"
            with pytest.raises(ValueError):
                await composite.publish({
                    "request_id": "publish:" + route["dispatch_id"], "composite_stream_id": CHAT,
                    "dispatch_id": route["dispatch_id"], "reply_to_message_id": "pending-input",
                    "publish_kind": "prose", "response_state": "final", "message": "late",
                }, actor_stream_id=A)
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(run())


def test_late_dispatch_receipt_cannot_revive_closed_target():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            dispatch_started = asyncio.Event()
            allow_receipt = asyncio.Event()

            async def dispatch(_route):
                dispatch_started.set()
                await allow_receipt.wait()
                return {"delivery": "landed"}

            composite = AssistantComposite(store, config=_config(a["session_generation"]), dispatch=dispatch)
            await composite.load_binding()
            await composite.ensure_projection()
            await composite.accept_input(
                {"text": "pending", "request_id": "pending-rpc", "optimistic_id": "pending-input"},
                operator_principal="operator:fixture",
            )
            await asyncio.wait_for(dispatch_started.wait(), 2)
            await composite.rebind(_request("move-while-dispatching", 0, B), actor_stream_id=A)
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            await composite.target_closed(A, a["session_generation"])
            allow_receipt.set()
            await asyncio.sleep(0.05)
            route = await store.get_assistant_composite_route(
                stream_id=CHAT, input_identity="pending-input",
            )
            assert route["delivery_state"] == "failed"
            assert route["error_code"] == "assistant_target_closed"
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(run())


def test_rebind_waits_for_in_progress_input_admission():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            await composite.ensure_projection()
            original_admit = store.admit_assistant_composite_input
            admission_started = asyncio.Event()
            allow_admission = asyncio.Event()
            admitted = []

            async def held_admit(**kwargs):
                admitted.append(kwargs)
                admission_started.set()
                await allow_admission.wait()
                return await original_admit(**kwargs)

            store.admit_assistant_composite_input = held_admit
            input_task = asyncio.create_task(composite.accept_input(
                {"text": "first", "request_id": "first-rpc", "optimistic_id": "first-input"},
                operator_principal="operator:fixture",
            ))
            await asyncio.wait_for(admission_started.wait(), 2)
            rebind_task = asyncio.create_task(composite.rebind(
                _request("move-during-admission", 0, B), actor_stream_id=A,
            ))
            await asyncio.sleep(0)
            assert not rebind_task.done()
            allow_admission.set()
            await asyncio.wait_for(input_task, 2)
            await asyncio.wait_for(rebind_task, 2)
            assert admitted[0]["direct_target_stream_id"] == A
            assert admitted[0]["direct_target_generation"] == a["session_generation"]
            assert (await composite.binding())["stream_id"] == B
            await composite.stop()
        finally:
            store.stop()
    asyncio.run(run())


def test_portfolio_actor_recovers_closed_pin_only_with_verified_tag_and_healthy_target():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            provenance = [{"spec_id": PORTFOLIO_SPEC, "provenance": "spawn_explicit",
                           "granting_principal": "operator", "granted_at": "2026-09-27T00:00:00Z"}]
            await store.open_session("fixture-c", "visible", provider="codex", role="assistant",
                                     visibility="default", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     spec_id=PORTFOLIO_SPEC,
                                     qualified_spec_ids=[PORTFOLIO_SPEC],
                                     spec_binding_provenance=provenance)
            await store.open_session("fixture-d", "hidden", provider="codex", role="assistant",
                                     visibility="hidden", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     spec_id=PORTFOLIO_SPEC,
                                     qualified_spec_ids=[PORTFOLIO_SPEC],
                                     spec_binding_provenance=provenance)
            await store.open_session("fixture-e", "subagent", provider="codex", role="assistant",
                                     visibility="subagent", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     qualified_spec_ids=[PORTFOLIO_SPEC],
                                     spec_binding_provenance=provenance)
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(_request("wrong-actor", 0, B), actor_stream_id=B)
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(_request("hidden-portfolio", 0, B),
                                       actor_stream_id="fixture-d:hidden")
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(_request("subagent-portfolio", 0, B),
                                       actor_stream_id="fixture-e:subagent")
            await store.update_session("fixture-b", "visible", routing_integrity="mismatch")
            with pytest.raises(ValueError, match="assistant_rebind_target_integrity_mismatch"):
                await composite.rebind(_request("mismatched-target", 0, B), actor_stream_id=C)
            await store.update_session("fixture-b", "visible", routing_integrity=None,
                                       effective_model=None)
            with pytest.raises(ValueError, match="assistant_rebind_target_tuple_unknown"):
                await composite.rebind(_request("unknown-tuple", 0, B), actor_stream_id=C)
            await store.update_session("fixture-b", "visible", effective_model="gpt-6-sol")
            await store.update_session("fixture-b", "visible", provider=None)
            with pytest.raises(ValueError, match="assistant_rebind_target_tuple_unknown"):
                await composite.rebind(_request("unknown-provider", 0, B), actor_stream_id=C)
            await store.update_session("fixture-b", "visible", provider="codex")
            receipt = await composite.rebind(_request("portfolio-recovery", 0, B), actor_stream_id=C)
            assert receipt["new_binding"]["stream_id"] == B
            assert receipt["new_binding"]["effective_model"] == "gpt-6-sol"
            audits = await store.submit(lambda conn: [row[0] for row in conn.execute(
                "SELECT outcome FROM v2_assistant_rebind_audit ORDER BY audit_id")])
            assert audits == ["assistant_rebind_unauthorized", "assistant_rebind_unauthorized",
                              "assistant_rebind_unauthorized",
                              "assistant_rebind_target_integrity_mismatch",
                              "assistant_rebind_target_tuple_unknown",
                              "assistant_rebind_target_tuple_unknown", "ok"]
        finally:
            store.stop()
    asyncio.run(run())


def test_live_clear_returns_to_env_and_partial_durable_pair_fails_closed():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            assert (await store.get_assistant_binding(env_binding={
                "stream_id": "", "generation": "",
            }))["source"] == "unconfigured"
            composite = AssistantComposite(store, config=_config(a["session_generation"]))
            await composite.load_binding()
            initial = await composite.binding()
            assert initial["source"] == "env"
            assert (initial["effective_provider"], initial["effective_model"],
                    initial["effective_effort"]) == ("codex", "gpt-6-sol", "high")
            await composite.rebind(_request("move-before-clear", 0, B), actor_stream_id=A)
            assert (await composite.binding())["source"] == "durable"
            cleared = await composite.rebind(_request("clear-live-env", 1, clear=True), actor_stream_id=B)
            assert cleared["new_binding"]["source"] == "env"
            assert cleared["new_binding"]["stream_id"] == A
            assert cleared["new_binding"]["revision"] == 2
            await store.submit(lambda conn: conn.execute(
                "UPDATE v2_assistant_direct_binding SET stream_id=?,generation=NULL WHERE id=1", (A,)
            ))
            with pytest.raises(ValueError, match="assistant_binding_corrupt"):
                await composite.binding()
        finally:
            store.stop()
    asyncio.run(run())
