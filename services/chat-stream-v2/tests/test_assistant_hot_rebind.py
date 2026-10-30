"""Direct pin changes preserve resolved dispatch authority and durable routing."""
from __future__ import annotations

import asyncio
import json

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from store import Store


CHAT = "fixture-chat:assistant"
A = "fixture-a:visible"
B = "fixture-b:visible"
C = "fixture-c:visible"
RECOVERY_SPEC = "spec_example__assistant_recovery"


async def _seat(store, stream_id, **extra):
    host, name = stream_id.split(":", 1)
    return await store.open_session(
        host, name, provider="codex", role="assistant", visibility="default",
        pane_status="pane_alive", effective_model="gpt-6-sol",
        effective_effort="high", **extra,
    )


def _config(generation, *, recovery=False):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": A,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": generation,
        "PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS": json.dumps([RECOVERY_SPEC] if recovery else []),
    })


def _request(request_id, revision, target=None, *, clear=False):
    return {"type": "assistant.rebind", "request_id": request_id,
            "expected_revision": revision, "target_stream_id": target, "clear": clear}


async def _persist_recovery_claim(store, stream_id, qualified, provenance):
    """Seed raw retained rows: the writer sanitizes malformed/legacy vectors."""
    def write(conn):
        conn.execute("UPDATE sessions SET qualified_spec_ids=?,spec_binding_provenance=? "
                     "WHERE host=? AND session_name=?",
                     (json.dumps(qualified), json.dumps(provenance), *stream_id.split(":", 1)))
        conn.commit()
    await store.submit(write)


@pytest.mark.parametrize("provenance", ["spawn_explicit", "drive_explicit", "handoff_inherited"])
def test_hidden_same_stream_recovery_uses_qualified_provenance_and_replays(provenance):
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            old = await _seat(store, A)
            resumed = await store.open_session(
                *A.split(":", 1), provider="claude", visibility="hidden", pane_status="pane_alive",
                effective_model="claude-opus-5-5", effective_effort="high",
                spec_id=RECOVERY_SPEC, spec_ids=[RECOVERY_SPEC],
                qualified_spec_ids=[RECOVERY_SPEC],
                spec_binding_provenance=[{"spec_id": RECOVERY_SPEC, "provenance": provenance,
                                          "granting_principal": "operator", "granted_at": "2026-10-08T00:00:00Z"}],
            )
            await _persist_recovery_claim(store, A, [RECOVERY_SPEC], [{
                "spec_id": RECOVERY_SPEC, "provenance": provenance,
                "granting_principal": "operator", "granted_at": "2026-10-08T00:00:00Z",
            }])
            kw = dict(env_binding={"stream_id": A, "generation": old["session_generation"]},
                      actor_stream_id=A, actor_generation=resumed["session_generation"],
                      target_stream_id=A, target_generation=resumed["session_generation"],
                      request_id="hidden-self", expected_revision=0,
                      authorized_spec_ids=frozenset({RECOVERY_SPEC}))
            receipt = await store.rebind_assistant(**kw)
            assert receipt["new_binding"]["revision"] == 1
            assert receipt["new_binding"]["generation"] == resumed["session_generation"]
            assert (await store.rebind_assistant(**kw))["duplicate"] is True
            assert len(await store.submit(lambda c: c.execute(
                "SELECT * FROM v2_assistant_rebind_audit").fetchall())) == 1
        finally:
            store.stop()
    asyncio.run(run())


REFUSED_CODES = {
    "stale_actor": "assistant_rebind_actor_generation_mismatch",
    "stale_revision": "assistant_rebind_stale_revision",
    "dead_actor": "assistant_rebind_actor_closed",
    "tuple_mismatch": "assistant_rebind_target_integrity_mismatch",
    "clear": "assistant_rebind_clear_unavailable",
    "stale_target": "assistant_rebind_generation_mismatch",
}


@pytest.mark.parametrize("case", [
    "clear", "parented", "absent_spec", "wrong_spec",
    "not_configured", "not_qualified", "malformed_qualified", "malformed_provenance",
    "blank_principal", "operator_v2", "parent_inherited", "stale_actor", "stale_target",
    "stale_revision", "daff", "dead_actor", "tuple_mismatch",
])
def test_hidden_recovery_exception_refuses_every_broader_authority(case):
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            old = await _seat(store, A)
            other = await _seat(store, B)
            actor_sid = A
            fields = dict(provider="claude", visibility="hidden", pane_status="pane_alive",
                          effective_model="claude-opus-5-5", effective_effort="high",
                          spec_id=RECOVERY_SPEC, spec_ids=[RECOVERY_SPEC],
                          qualified_spec_ids=[RECOVERY_SPEC],
                          spec_binding_provenance=[{"spec_id": RECOVERY_SPEC, "provenance": "spawn_explicit",
                                                    "granting_principal": "operator", "granted_at": "2026-10-08T00:00:00Z"}])
            if case == "parented":
                fields["parent_stream_id"] = B
            if case in {"absent_spec", "not_qualified"}:
                fields["qualified_spec_ids"] = []
            if case == "absent_spec":
                fields["spec_id"] = None
                fields["spec_ids"] = []
                fields["spec_binding_provenance"] = []
            if case == "wrong_spec":
                fields["qualified_spec_ids"] = ["spec_other"]
            if case == "malformed_qualified":
                fields["qualified_spec_ids"] = {"unexpected": RECOVERY_SPEC}
            if case == "malformed_provenance":
                fields["spec_binding_provenance"] = {"unexpected": "spawn_explicit"}
            if case in {"operator_v2", "parent_inherited"}:
                fields["spec_binding_provenance"][0]["provenance"] = case
            if case == "blank_principal":
                fields["spec_binding_provenance"][0]["granting_principal"] = "  "
            if case == "dead_actor":
                fields["pane_status"] = "pane_dead"
            if case == "tuple_mismatch":
                fields["routing_integrity"] = "mismatch"
            actor = await store.open_session(*actor_sid.split(":", 1), **fields)
            await _persist_recovery_claim(store, actor_sid, fields["qualified_spec_ids"], fields["spec_binding_provenance"])
            generation = actor["session_generation"]
            kw = dict(env_binding={"stream_id": A, "generation": old["session_generation"]},
                      actor_stream_id=actor_sid, actor_generation=generation,
                      target_stream_id=actor_sid, target_generation=generation,
                      request_id="refuse-" + case, expected_revision=0,
                      authorized_spec_ids=frozenset() if case == "not_configured" else frozenset({RECOVERY_SPEC}))
            if case == "clear":
                kw["clear"] = True
            if case == "stale_actor":
                kw["actor_generation"] = old["session_generation"]
            if case == "stale_target":
                kw["target_generation"] = old["session_generation"]
            if case == "stale_revision":
                kw["expected_revision"] = 99
            if case == "daff":
                kw["name"] = "daff"
            with pytest.raises(ValueError) as exc:
                await store.rebind_assistant(**kw)
            assert exc.value.args[0] == REFUSED_CODES.get(case, "assistant_rebind_unauthorized")
            bindings = await store.submit(lambda c: c.execute("SELECT * FROM v2_assistant_direct_binding").fetchall())
            assert bindings == []
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("shape", ["wrong_actor", "target_other", "missing_target_generation"])
def test_hidden_qualified_primary_actor_has_the_authority_of_a_default_one(shape):
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            old = await _seat(store, A)
            other = await _seat(store, B)
            fields = dict(provider="claude", visibility="hidden", pane_status="pane_alive",
                          effective_model="claude-opus-5-5", effective_effort="high",
                          spec_id=RECOVERY_SPEC, spec_ids=[RECOVERY_SPEC], qualified_spec_ids=[RECOVERY_SPEC],
                          spec_binding_provenance=[{"spec_id": RECOVERY_SPEC, "provenance": "spawn_explicit",
                                                    "granting_principal": "operator", "granted_at": "2026-10-08T00:00:00Z"}])
            actor_sid = B if shape == "wrong_actor" else A
            actor = await store.open_session(*actor_sid.split(":", 1), **fields)
            await _persist_recovery_claim(store, actor_sid, fields["qualified_spec_ids"], fields["spec_binding_provenance"])
            target, generation = (B, other["session_generation"]) if shape == "target_other" else (
                actor_sid, actor["session_generation"])
            kw = dict(env_binding={"stream_id": A, "generation": old["session_generation"]},
                      actor_stream_id=actor_sid, actor_generation=actor["session_generation"],
                      target_stream_id=target,
                      target_generation=None if shape == "missing_target_generation" else generation,
                      request_id="allow-" + shape, expected_revision=0,
                      authorized_spec_ids=frozenset({RECOVERY_SPEC}))
            receipt = await store.rebind_assistant(**kw)
            assert receipt["new_binding"]["stream_id"] == target
            assert receipt["new_binding"]["revision"] == 1
            audit = await store.submit(lambda c: c.execute(
                "SELECT outcome,actor_stream_id FROM v2_assistant_rebind_audit WHERE request_id=?",
                ("allow-" + shape,)).fetchall())
            assert [(r["outcome"], r["actor_stream_id"]) for r in audit] == [("ok", actor_sid)]
        finally:
            store.stop()
    asyncio.run(run())


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
            composite = AssistantComposite(store, config=_config(a["session_generation"], recovery=True))
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
                                     spec_id=RECOVERY_SPEC,
                                     qualified_spec_ids=[RECOVERY_SPEC],
                                     spec_binding_provenance=[{"spec_id": RECOVERY_SPEC,
                                         "provenance": "spawn_explicit", "granting_principal": "operator",
                                         "granted_at": "2026-09-27T00:00:00Z"}])
            receipt = await composite.rebind(_request("configured_recovery-after-close", 0, B), actor_stream_id=C)
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


def test_configured_recovery_actor_recovers_closed_pin_only_with_verified_tag_and_healthy_target():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, _b = await _seat(store, A), await _seat(store, B)
            provenance = [{"spec_id": RECOVERY_SPEC, "provenance": "spawn_explicit",
                           "granting_principal": "operator", "granted_at": "2026-09-27T00:00:00Z"}]
            await store.open_session("fixture-c", "visible", provider="codex", role="assistant",
                                     visibility="default", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     spec_id=RECOVERY_SPEC,
                                     qualified_spec_ids=[RECOVERY_SPEC],
                                     spec_binding_provenance=provenance)
            await store.open_session("fixture-e", "subagent", provider="codex", role="assistant",
                                     visibility="subagent", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     qualified_spec_ids=[RECOVERY_SPEC],
                                     spec_binding_provenance=provenance)
            composite = AssistantComposite(store, config=_config(a["session_generation"], recovery=True))
            await composite.load_binding()
            await store.update_session("fixture-a", "visible", status="closed",
                                       closed_at="2026-09-27T00:00:00Z", pane_status="pane_dead")
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(_request("wrong-actor", 0, B), actor_stream_id=B)
            with pytest.raises(ValueError, match="assistant_rebind_unauthorized"):
                await composite.rebind(_request("subagent-configured_recovery", 0, B),
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
            receipt = await composite.rebind(_request("configured_recovery-recovery", 0, B), actor_stream_id=C)
            assert receipt["new_binding"]["stream_id"] == B
            assert receipt["new_binding"]["effective_model"] == "gpt-6-sol"
            audits = await store.submit(lambda conn: [row[0] for row in conn.execute(
                "SELECT outcome FROM v2_assistant_rebind_audit ORDER BY audit_id")])
            assert audits == ["assistant_rebind_unauthorized", "assistant_rebind_unauthorized",
                              "assistant_rebind_target_integrity_mismatch",
                              "assistant_rebind_target_tuple_unknown",
                              "assistant_rebind_target_tuple_unknown", "ok"]
        finally:
            store.stop()
    asyncio.run(run())


def test_hidden_configured_recovery_actor_rebinds_the_primary_assistant():
    async def run():
        store = Store(":memory:")
        store.start()
        try:
            a, b = await _seat(store, A), await _seat(store, B)
            await store.open_session("fixture-d", "hidden", provider="codex", role="assistant",
                                     visibility="hidden", pane_status="pane_alive",
                                     effective_model="gpt-6-sol", effective_effort="high",
                                     spec_id=RECOVERY_SPEC, qualified_spec_ids=[RECOVERY_SPEC],
                                     spec_binding_provenance=[{"spec_id": RECOVERY_SPEC, "provenance": "spawn_explicit",
                                                               "granting_principal": "operator",
                                                               "granted_at": "2026-09-27T00:00:00Z"}])
            composite = AssistantComposite(store, config=_config(a["session_generation"], recovery=True))
            await composite.load_binding()
            receipt = await composite.rebind(_request("hidden-configured_recovery", 0, B),
                                             actor_stream_id="fixture-d:hidden")
            assert receipt["new_binding"]["stream_id"] == B
            assert receipt["new_binding"]["generation"] == b["session_generation"]
            audits = await store.submit(lambda conn: [(row[0], row[1]) for row in conn.execute(
                "SELECT outcome,actor_stream_id FROM v2_assistant_rebind_audit ORDER BY audit_id")])
            assert audits == [("ok", "fixture-d:hidden")]
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
                "UPDATE v2_assistant_direct_binding SET stream_id=?,generation=NULL WHERE name='assistant'", (A,)
            ))
            with pytest.raises(ValueError, match="assistant_binding_corrupt"):
                await composite.binding()
        finally:
            store.stop()
    asyncio.run(run())
