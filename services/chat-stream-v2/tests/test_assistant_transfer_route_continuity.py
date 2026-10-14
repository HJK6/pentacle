"""Route custody through a committed host transfer (spec_pentacle__assistant_transfer_route_continuity_2026_10).

The isolated transfer fixture composes only question expiry into the session-close
hook; production (main.py) also calls ``composite.target_closed`` on EVERY close.
These tests wire that production composition and drive the real post-commit path
(``project_and_cleanup``), which is where the incident routes were fail-closed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

import store_host_transfer as transfer
from store import STREAM_TOKEN_HASH_VERSION
from test_assistant_recovery_command import fixture, OLD, NEW, CHILD, ADVISOR, CHAT
from test_host_transfer_authority import claim_bundle, prepare_bundle, dispatch, checkpoint_pointers


def wire_production_close_hook(server, composite):
    """Compose ``composite.target_closed`` into the close resolver as main.py does."""
    async def on_close(stream_id, *, session_generation=None, reason="confirmed_dead_close"):
        await composite.target_closed(stream_id, session_generation)
    server.sessions.set_awaiter_resolver(on_close)


async def successor_token(store, target):
    token = "fresh-successor-fixture-token"
    await store.grant_stream_token(*target.split(":", 1), hashlib.sha256(token.encode()).hexdigest(), STREAM_TOKEN_HASH_VERSION)
    return token


def route_state(route):
    return {k: route[k] for k in ("route_target", "route_target_generation", "routing_state", "delivery_state", "error_code")}


async def incident_journey(tmp_path, *, run_cleanup=True):
    """Incident shape: one unanswered landed input (the transfer trigger) at the
    outgoing seat; commit; real post-commit source close; successor answers under the
    original dispatch id."""
    async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
        wire_production_close_hook(server, composite)
        row, _, auth, _ = await claim_bundle(store, server, gateway, composite)
        parts = await prepare_bundle(store, server, row)
        fd = next(p for p in parts if p["source"] == OLD)
        row = await server.host_transfer.commit(row)
        assert row["state"] == "committed"
        after_commit = await store.get_assistant_composite_route(stream_id=CHAT, input_identity="operator-move")
        if run_cleanup:
            await server.host_transfer.project_and_cleanup(row)
            assert (await server.host_transfer.read(row["command_id"]))["state"] == "complete"
            assert (await store.fetch_session("node-a", "old"))["status"] == "closed"
        after_cleanup = await store.get_assistant_composite_route(stream_id=CHAT, input_identity="operator-move")
        token = await successor_token(store, fd["target"])
        gateway.actor, gateway.token = fd["target"], token
        reply = await gateway.rpc({"type": "assistant.publish", "request_id": "publish:dispatch-move",
            "composite_stream_id": CHAT, "dispatch_id": "dispatch-move", "reply_to_message_id": "operator-move",
            "publish_kind": "prose", "response_state": "final", "message": "The isolated move completed.",
            "attachment_ids": [], "evidence_refs": []})
        activity = await composite.activity_snapshot() if asyncio.iscoroutinefunction(getattr(composite, "activity_snapshot", None)) else composite.activity_snapshot()
        return fd, auth, after_commit, after_cleanup, reply, activity


def test_4a_committed_transfer_carries_unanswered_route_to_successor(tmp_path):
    """C1/C4/C5: after commit the route's CURRENT custody is the successor; the retired
    seat's close fails nothing; the successor's final under the ORIGINAL dispatch id lands."""
    fd, auth, after_commit, after_cleanup, reply, activity = asyncio.run(incident_journey(tmp_path))
    assert (after_commit["route_target"], after_commit["route_target_generation"]) == (fd["target"], fd["target_generation"]), route_state(after_commit)
    assert after_commit["dispatch_id"] == "dispatch-move" and after_commit["input_identity"] == "operator-move"
    assert after_cleanup["delivery_state"] != "failed" and after_cleanup["error_code"] is None, route_state(after_cleanup)
    assert reply["type"] == "assistant.publish.ok", reply
    assert activity["inputs"]["operator-move"]["response_state"] not in {"failed"}


def test_control_ordinary_close_without_succession_still_fails_closed(tmp_path):
    """Retained distinction (C4): a plain close of the bound target with no committed
    successor terminalizes its unanswered route."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            old = await store.fetch_session("node-a", "old")
            await dispatch(store, old, "operator:" + gateway.ceremony.cid)
            await composite.target_closed(OLD, old["session_generation"])
            route = await store.get_assistant_composite_route(stream_id=CHAT, input_identity="operator-move")
            assert (route["delivery_state"], route["error_code"]) == ("failed", "assistant_target_closed"), route_state(route)
    asyncio.run(run())


# ---------------------------------------------------------------------------
# Helpers for the remaining accepted cells (spec Plan step 4, tests 4b–4l)
# ---------------------------------------------------------------------------

from assistant_composite import direct_dispatch_envelope, carry_direct_envelope, CARRY_WARNING_TEMPLATE

ANCHOR = "<assistant-original-input-json>\n"


async def admit_landed(store, composite, *, seat, generation, input_id, dispatch_id, text, principal):
    """Admit one direct operator input already landed at ``seat`` with a real frozen envelope."""
    route = await store.admit_assistant_composite_input(
        stream_id=CHAT, input_identity=input_id, input_request_id=input_id, body=text, attachments=[],
        reply_to_message_id=None, reply_to_question_id=None, actor_stream_id=principal,
        direct_primary=True, direct_target_stream_id=seat, direct_target_generation=generation)
    envelope = direct_dispatch_envelope(composite.config, route, dispatch_id=dispatch_id, target=seat, generation=generation)
    return await store.update_assistant_composite_route(
        route["route_id"], routing_state="resolved", delivery_state="landed", dispatch_id=dispatch_id,
        route_target=seat, route_target_generation=generation,
        route_payload={"admission_mode": "direct_primary", "direct_envelope": envelope})


async def route_of(store, input_id):
    return await store.get_assistant_composite_route(stream_id=CHAT, input_identity=input_id)


def envelope_of(route):
    return json.loads(route["route_json"] or "{}").get("direct_envelope") or {}


def carries_of(route):
    return json.loads(route["route_json"] or "{}").get("carry") or []


def warning_for(predecessor, generation):
    return CARRY_WARNING_TEMPLATE.format(predecessor=predecessor, generation=generation)


async def claim_from(store, server, gateway, composite, *, actor, actor_generation, cid, dispatch_id, input_id):
    """Claim a transfer whose requesting FD is ``actor`` (used for the chained B->C hop)."""
    await admit_landed(store, composite, seat=actor, generation=actor_generation, input_id=input_id,
                       dispatch_id=dispatch_id, text="Move the desk again.", principal="operator:" + gateway.ceremony.cid)
    advisor = await store.get("assistant.authority.stream_id")
    auth = {"token_verified": True, "stream_id": actor, "session_generation": actor_generation}
    msg = {"command_id": cid, "dispatch_id": dispatch_id, "destination": "node-b", "members": [actor, advisor],
           "pointers": checkpoint_pointers(store, members=(actor, advisor))}
    def claim(c):
        return transfer.claim(c, msg, auth, name="fixture", composite=CHAT, env_binding=composite._env_binding(),
                              advisor=advisor, helpers={}, trusted_hosts={"node-a", "node-b"},
                              registry=server.operator_credential_registry)
    return await store.submit(lambda c: transfer.transaction(c, claim, server.operator_credential_registry))


async def committed_transfer(store, server, gateway, composite):
    """claim_bundle + prepare + commit; returns (row, parts, fd_part, old_generation)."""
    old = await store.fetch_session("node-a", "old")
    row, _, auth, _ = await claim_bundle(store, server, gateway, composite)
    parts = await prepare_bundle(store, server, row)
    fd = next(p for p in parts if p["source"] == OLD)
    row = await server.host_transfer.commit(row)
    assert row["state"] == "committed"
    return row, parts, fd, old["session_generation"]


def test_4a_carried_envelope_keeps_original_input_and_lands_on_successor(tmp_path):
    """C1/C2/C3: a landed input with a real frozen envelope is repointed, its wire body carries the
    exact predecessor block immediately before the original-input block, the original input JSON is
    byte-identical, and the post-commit driver delivers it to the successor (at least once)."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            delivered = []
            async def dispatch(route):
                delivered.append((route["dispatch_id"], route["route_target"], route["route_target_generation"]))
                return {"delivery": "landed"}
            composite.dispatch = dispatch
            old = await store.fetch_session("node-a", "old")
            before = await admit_landed(store, composite, seat=OLD, generation=old["session_generation"],
                                        input_id="inp-env", dispatch_id="dispatch-env", text="Please run the follow-up task",
                                        principal="operator:" + gateway.ceremony.cid)
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            carried = await route_of(store, "inp-env")
            assert (carried["route_target"], carried["route_target_generation"], carried["delivery_state"], carried["error_code"]) == (fd["target"], fd["target_generation"], "intent", None)
            assert carries_of(carried) == [{**carries_of(carried)[0], "command_id": row["command_id"], "from_stream_id": OLD, "from_generation": old_gen, "prior_delivery_state": "landed"}]
            env = envelope_of(carried); orig = envelope_of(before)
            assert env["target_stream_id"] == fd["target"] and env["target_generation"] == fd["target_generation"]
            assert env["dispatch_id"] == "dispatch-env" and env["reply_to_message_id"] == "inp-env"
            assert env["original_input"] == orig["original_input"] and env["publish_command"] == orig["publish_command"]
            assert env["wire_body"].count(ANCHOR) == 1
            warning = warning_for(OLD, old_gen)
            assert warning + ANCHOR in env["wire_body"], env["wire_body"]
            original_block = orig["wire_body"].split(ANCHOR, 1)[1]
            assert env["wire_body"].split(ANCHOR, 1)[1] == original_block  # original input JSON byte-identical
            assert f"target_stream_id: {fd['target']}\ntarget_generation: {fd['target_generation']}\n" in env["wire_body"]
            await server.host_transfer.project_and_cleanup(row)
            after = await route_of(store, "inp-env")
            assert (after["route_target"], after["delivery_state"], after["error_code"]) == (fd["target"], "landed", None)
            assert delivered == [("dispatch-env", fd["target"], fd["target_generation"])] + [("dispatch-move", fd["target"], fd["target_generation"])] or \
                   sorted(delivered) == sorted([("dispatch-env", fd["target"], fd["target_generation"]), ("dispatch-move", fd["target"], fd["target_generation"])])
            assert (await store.fetch_session("node-a", "old"))["status"] == "closed"
    asyncio.run(run())


def test_4b_already_started_task_child_reparented_and_warning_names_predecessor(tmp_path):
    """C2/C5 (already-started task): the predecessor had already commissioned a child for the input;
    commit reparents that child to the successor and the carried wire body names the predecessor."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            old = await store.fetch_session("node-a", "old")
            await admit_landed(store, composite, seat=OLD, generation=old["session_generation"], input_id="inp-task",
                               dispatch_id="dispatch-task", text="Start the analysis",
                               principal="operator:" + gateway.ceremony.cid)
            # The predecessor acted: a child lane commissioned after the input landed.
            await store.open_session("node-a", "task-child", parent_stream_id=OLD, role="worker", visibility="hidden",
                                     pane_status="pane_alive", bootstrap_state="ready", provider="codex",
                                     effective_model="gpt-6.1-sol", effective_effort="medium")
            server.sessions.tmux.live.add("task-child")
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            child = await store.fetch_session("node-a", "task-child")
            assert child["parent_stream_id"] == fd["target"] and child["status"] == "open"
            env = envelope_of(await route_of(store, "inp-task"))
            assert warning_for(OLD, old_gen) + ANCHOR in env["wire_body"]
            assert "may have started this" in env["wire_body"]
    asyncio.run(run())


def test_4c_chained_transfer_repoints_twice_and_middle_close_fails_nothing(tmp_path):
    """C1/C4: A->B->C. The same unanswered route is repointed on each commit (two carry entries),
    the warning names the most recent predecessor only, and B's close after the second commit
    fail-closes nothing."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            old = await store.fetch_session("node-a", "old")
            await admit_landed(store, composite, seat=OLD, generation=old["session_generation"], input_id="inp-chain",
                               dispatch_id="dispatch-chain", text="Still waiting on this one",
                               principal="operator:" + gateway.ceremony.cid)
            row1, parts1, fd1, old_gen = await committed_transfer(store, server, gateway, composite)
            await server.host_transfer.project_and_cleanup(row1)
            b, b_gen = fd1["target"], fd1["target_generation"]
            mid = await route_of(store, "inp-chain")
            assert (mid["route_target"], mid["delivery_state"]) == (b, "landed")
            row2 = await claim_from(store, server, gateway, composite, actor=b, actor_generation=b_gen,
                                    cid="bundle-2", dispatch_id="dispatch-move-2", input_id="operator-move-2")
            parts2 = await prepare_bundle(store, server, row2)
            fd2 = next(p for p in parts2 if p["source"] == b)
            row2 = await server.host_transfer.commit(row2)
            assert row2["state"] == "committed"
            c, c_gen = fd2["target"], fd2["target_generation"]
            carried = await route_of(store, "inp-chain")
            assert (carried["route_target"], carried["route_target_generation"], carried["delivery_state"]) == (c, c_gen, "intent")
            chain = carries_of(carried)
            assert [(x["command_id"], x["from_stream_id"]) for x in chain] == [("bundle-1", OLD), ("bundle-2", b)]
            body = envelope_of(carried)["wire_body"]
            assert warning_for(b, b_gen) + ANCHOR in body and OLD not in body.split(ANCHOR, 1)[0].split("[inherited dispatch]", 1)[1]
            assert body.count("[inherited dispatch]") == 1
            # The fixture's physical close is localhost-only (B lives on node-b), so drive the
            # same post-commit effects directly: the production close hook for B, then the
            # driver's redelivery of this commit's carried routes to C.
            host_b, name_b = b.split(":", 1)
            await store.update_session(host_b, name_b, status="closed", closed_at="2026-10-14T00:00:00Z", pane_status="pane_dead")
            await composite.target_closed(b, b_gen)
            await server.host_transfer.redeliver_carried(row2, parts2)
            assert (await store.fetch_session(host_b, name_b))["status"] == "closed"
            final = await route_of(store, "inp-chain")
            assert (final["route_target"], final["delivery_state"], final["error_code"]) == (c, "landed", None)
            trigger2 = await route_of(store, "operator-move-2")
            assert (trigger2["route_target"], trigger2["error_code"]) == (c, None)
            for input_id in ("operator-move", "inp-chain", "operator-move-2"):
                assert (await route_of(store, input_id))["error_code"] != "assistant_target_closed"
    asyncio.run(run())


def test_4d_ordinary_close_of_successor_after_carry_fails_carried_routes(tmp_path):
    """C4: once carried, a route belongs to the successor; an ORDINARY close of the successor
    (no committed transfer away from it) fail-closes it like any route."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            await server.host_transfer.project_and_cleanup(row)
            before = await route_of(store, "operator-move")
            assert (before["route_target"], before["delivery_state"]) == (fd["target"], "landed")
            host, name = fd["target"].split(":", 1)
            await store.update_session(host, name, status="closed", closed_at="2026-10-14T00:00:00Z", pane_status="pane_dead")
            await composite.target_closed(fd["target"], fd["target_generation"])
            after = await route_of(store, "operator-move")
            assert (after["delivery_state"], after["error_code"]) == ("failed", "assistant_target_closed")
    asyncio.run(run())


def test_4e_repeated_commit_is_idempotent_for_routes(tmp_path):
    """C6: commit on an already committed command returns the row and touches no route."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            first = await route_of(store, "operator-move")
            again = await store.submit(lambda c: transfer.transaction(
                c, lambda c: transfer.commit(c, row["command_id"], server.operator_credential_registry), server.operator_credential_registry))
            assert again["state"] == "committed"
            second = await route_of(store, "operator-move")
            assert second == first
            assert len(carries_of(second)) == 1
    asyncio.run(run())


def test_4f_restart_in_redelivery_window_keeps_successor_custody_and_redelivers(tmp_path):
    """C3/C6: a daemon restart between commit and redelivery marks the in-flight carried route
    uncertain (existing startup recovery); it stays targeted at the successor, is never failed,
    and the next drive of the committed command delivers it."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            delivered = []
            async def dispatch(route):
                delivered.append(route["dispatch_id"])
                return {"delivery": "landed"}
            composite.dispatch = dispatch
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            # Restart: startup recovery sweeps in-flight routes before any driver runs.
            recovered = await store.recover_assistant_composite_routes(stream_id=CHAT)
            assert recovered["retained_uncertain"] >= 1
            mid = await route_of(store, "operator-move")
            assert (mid["route_target"], mid["delivery_state"], mid["error_code"]) == (fd["target"], "uncertain", "recovery_delivery_evidence_required")
            await server.host_transfer.project_and_cleanup(row)
            after = await route_of(store, "operator-move")
            assert (after["route_target"], after["delivery_state"], after["error_code"]) == (fd["target"], "landed", None)
            assert delivered == ["dispatch-move"]
    asyncio.run(run())


def test_4g_successor_final_under_original_dispatch_id_once_then_duplicate(tmp_path):
    """C5: the successor's final is accepted once under the ORIGINAL dispatch id; the same
    publication key replays as duplicate."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            await server.host_transfer.project_and_cleanup(row)
            gateway.actor, gateway.token = fd["target"], await successor_token(store, fd["target"])
            msg = {"type": "assistant.publish", "request_id": "publish:dispatch-move", "composite_stream_id": CHAT,
                   "dispatch_id": "dispatch-move", "reply_to_message_id": "operator-move", "publish_kind": "prose",
                   "response_state": "final", "message": "Done after the move.", "attachment_ids": [], "evidence_refs": []}
            first = await gateway.rpc(msg)
            second = await gateway.rpc(msg)
            assert first["type"] == "assistant.publish.ok" and not first.get("duplicate")
            assert second["type"] == "assistant.publish.ok" and second.get("duplicate") is True
            activity = await store.assistant_composite_activity(stream_id=CHAT)
            assert activity["operator-move"]["response_state"] == "answered"
    asyncio.run(run())


def test_4h_final_under_original_id_clears_failed_state_in_activity(tmp_path):
    """C5 UI error clear: a route recorded failed/assistant_target_closed (the incident state) that is
    later answered under its original dispatch id projects a final and no error."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            route = await route_of(store, "operator-move")
            await store.update_assistant_composite_route(route["route_id"], routing_state="resolved",
                                                         delivery_state="failed", error_code="assistant_target_closed")
            failed = (await store.assistant_composite_activity(stream_id=CHAT))["operator-move"]
            assert failed["response_state"] == "failed" and failed["error_code"] == "assistant_target_closed"
            gateway.actor, gateway.token = fd["target"], await successor_token(store, fd["target"])
            reply = await gateway.rpc({"type": "assistant.publish", "request_id": "publish:dispatch-move",
                "composite_stream_id": CHAT, "dispatch_id": "dispatch-move", "reply_to_message_id": "operator-move",
                "publish_kind": "prose", "response_state": "final", "message": "Answered late, once.",
                "attachment_ids": [], "evidence_refs": []})
            assert reply["type"] == "assistant.publish.ok"
            cleared = (await store.assistant_composite_activity(stream_id=CHAT))["operator-move"]
            assert cleared["response_state"] == "answered" and cleared["error_code"] is None and cleared["final_visible_at"]
    asyncio.run(run())


def test_4i_cancelled_transfer_leaves_routes_untouched(tmp_path):
    """C6: cancel/expiry before commit changes no route custody."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            old = await store.fetch_session("node-a", "old")
            row, _, auth, _ = await claim_bundle(store, server, gateway, composite)
            await prepare_bundle(store, server, row)
            before = await route_of(store, "operator-move")
            cancelled = await store.submit(lambda c: transfer.transaction(
                c, lambda c: transfer.cancel(c, row["command_id"], {"operator_authenticated": True})))
            assert cancelled["state"] == "cancelled"
            after = await route_of(store, "operator-move")
            assert after == before
            assert (after["route_target"], after["route_target_generation"], after["delivery_state"]) == (OLD, old["session_generation"], "landed")
            assert carries_of(after) == []
    asyncio.run(run())


def test_4j_new_input_after_commit_batches_into_carried_open_dispatch(tmp_path):
    """C6 batching compatibility: after carry the inherited route is an open dispatch of the
    successor, so the next operator input batches into it (oldest open)."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            await server.host_transfer.project_and_cleanup(row)
            await composite.accept_input({"message": "and one more thing", "msg_id": "inp-later", "request_id": "inp-later"},
                                         operator_principal="operator:" + gateway.ceremony.cid)
            later = None
            for _ in range(300):
                later = await route_of(store, "inp-later")
                if later and later["routing_state"] == "resolved":
                    break
                await asyncio.sleep(0.01)
            assert later and later["routing_state"] == "resolved", later
            assert (later["route_target"], later["route_target_generation"]) == (fd["target"], fd["target_generation"])
            assert later["batched_into_dispatch_id"] == "dispatch-move", later
    asyncio.run(run())


def test_4k_real_main_dispatch_accepts_a_carried_envelope(tmp_path):
    """C1/C2 envelope identity: the real main.py._dispatch_assistant_route checks (target, generation,
    dispatch id, reply id, original input, publish command) accept a carried envelope and the paste
    carries the predecessor block."""
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    from test_send_semantics import FakeTmux, _new_comms, _open, HOST, NAME
    from test_voice_context import main_dispatch_subject

    async def run():
        tmux = FakeTmux()
        comms, store, sessions = _new_comms(tmux, tmp_path)
        composite = None
        try:
            await _open(comms, sessions)
            root = await store.fetch_session(HOST, NAME)
            successor, successor_gen = f"{HOST}:{NAME}", root["session_generation"]
            config = AssistantCompositeConfig.from_env({
                "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
                "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": successor,
                "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": successor_gen,
            })
            composite = AssistantComposite(store, config=config, dispatch=main_dispatch_subject(comms, store))
            await composite.ensure_projection()
            route = await store.admit_assistant_composite_input(
                stream_id=CHAT, input_identity="inp-k", input_request_id="inp-k", body="Carry me over", attachments=[],
                reply_to_message_id=None, reply_to_question_id=None, actor_stream_id="operator:fixture",
                direct_primary=True, direct_target_stream_id=successor, direct_target_generation=successor_gen)
            # Freeze the envelope as the (now gone) predecessor would have held it, then carry it.
            frozen = direct_dispatch_envelope(config, route, dispatch_id="dispatch-k", target="fixture-old:seat", generation="oldgen")
            carried = carry_direct_envelope(frozen, target=successor, generation=successor_gen,
                                            predecessor="fixture-old:seat", predecessor_generation="oldgen")
            route = await store.update_assistant_composite_route(
                route["route_id"], routing_state="resolved", delivery_state="intent", dispatch_id="dispatch-k",
                route_target=successor, route_target_generation=successor_gen,
                route_payload={"admission_mode": "direct_primary", "direct_envelope": carried})
            result = await main_dispatch_subject(comms, store)(dict(route))
            assert result.get("reason") != "assistant_direct_envelope_invalid", result
            assert result.get("submission_confirmed") is True or str(result.get("delivery")) in {"landed", "delivered"}, result
            assert len(tmux.pastes) == 1 and warning_for("fixture-old:seat", "oldgen") in tmux.pastes[0]
            assert tmux.pastes[0].count(ANCHOR) == 1
        finally:
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["landed", "failed", "exception"])
def test_4l_late_predecessor_receipt_cannot_touch_successor_custody(tmp_path, outcome):
    """C3a: a dispatch started under the predecessor's custody resumes after commit; its receipt
    (landed, failed or exception) is not applied to the successor's row, and the driver then
    delivers to the successor exactly through the ordinary path."""
    async def run():
        async with fixture(tmp_path, authority=True) as (store, server, composite, gateway, _, __):
            wire_production_close_hook(server, composite)
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def dispatch(route):
                calls.append((route["dispatch_id"], route["route_target"]))
                if route["route_target"] == OLD:
                    started.set()
                    await release.wait()
                    if outcome == "exception":
                        raise RuntimeError("late pane error")
                    return {"delivery": outcome}
                return {"delivery": "landed"}
            composite.dispatch = dispatch
            await composite.accept_input({"message": "in flight at the old desk", "msg_id": "inp-flight", "request_id": "inp-flight"},
                                         operator_principal="operator:" + gateway.ceremony.cid)
            await asyncio.wait_for(started.wait(), 5)
            inflight = await route_of(store, "inp-flight")
            assert (inflight["route_target"], inflight["delivery_state"]) == (OLD, "intent")
            row, parts, fd, old_gen = await committed_transfer(store, server, gateway, composite)
            carried = await route_of(store, "inp-flight")
            assert (carried["route_target"], carried["delivery_state"]) == (fd["target"], "intent")
            release.set()
            for _ in range(200):
                if len(calls) >= 1 and not any(t.done() is False for t in composite._dispatch_tasks):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            still = await route_of(store, "inp-flight")
            assert (still["route_target"], still["route_target_generation"], still["delivery_state"], still["error_code"]) == (fd["target"], fd["target_generation"], "intent", None), still
            await server.host_transfer.project_and_cleanup(row)
            after = await route_of(store, "inp-flight")
            assert (after["route_target"], after["delivery_state"], after["error_code"]) == (fd["target"], "landed", None)
            assert [c for c in calls if c[1] == fd["target"] and c[0] == inflight["dispatch_id"]] == [(inflight["dispatch_id"], fd["target"])]
            assert calls[0] == (inflight["dispatch_id"], OLD)
    asyncio.run(run())
