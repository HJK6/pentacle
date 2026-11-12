"""Current-FD lifecycle authority, including final-admission races and lane fencing."""
import asyncio
import sqlite3

import pytest

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from sessions import VerbError
from test_lifecycle_authority import HOST, OPERATOR, Env, _refused, _until, scenario

FD = HOST + ":front-desk"
REASON = "Operator fixture-directive: close absorbed kickoff; custody with FD"


async def setup(env: Env):
    await env.open("front-desk", role="lead", provider="codex", effective_model="fixture-model",
                   effective_effort="medium")
    composite = AssistantComposite(env.store, config=AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": HOST + ":assistant",
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": FD,
        "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": await env.gen("front-desk"),
    }))
    env.server.assistant_composite = composite
    env.sessions.assistant.front_desk = composite
    await env.open("target")
    return await env.seat("front-desk")


def close(auth, **extra):
    return {"stream_id": HOST + ":target", "reason": REASON, "request_id": "close-fixture",
            "_auth_context": auth, **extra}


async def lane(env, *, owner="operator", pointer="chat"):
    composite = env.server.assistant_composite
    await composite.ensure_projection()
    payload = {"adoption_key": "stream:target", "title": "Disposable work", "summary": "Synthetic lane",
               "owner_kind": owner, "work_state": "paused", "no_spec_reason": "Isolated acceptance",
               "visible_chat": {"stream_id": HOST + ":target", "generation": await env.gen("target")}}
    if pointer == "lead":
        await env.store.update_session(HOST, "target", role="lead", provider="codex")
        payload["lead"] = {"stream_id": HOST + ":target", "generation": await env.gen("target")}
        payload["visible_chat"] = {"stream_id": HOST + ":assistant"}
    reply = await composite.operation({"type": "assistant.operation", "request_id": "adopt-fixture",
        "composite_stream_id": HOST + ":assistant", "dispatch_id": "none", "operation": "work_lane.adopt",
        "payload": payload, "_auth_context": await env.seat("front-desk")}, actor_stream_id=FD)
    return reply["lane"]


def test_fd_close_is_audited_without_lifecycle_or_custody_change():
    async def run(env):
        auth = await setup(env)
        grant = await env.grant()
        result = await env.server._on_close(close(auth, actor_stream_id="forged", operator_confirm=True,
                                                 operator_override=True))
        assert result["type"] == "close.ok" and env.tmux.killed == ["target"]
        assert (await env.store.fetch_session(HOST, "target"))["parent_stream_id"] is None
        assert await env.grant() == grant
        rows = [r for r in await env.audit() if r["action"].startswith("manager_close")]
        assert [(r["action"], r["result"]) for r in rows] == [
            ("manager_close", "admitted"), ("manager_close", "applied")]
        assert all((r["actor_kind"], r["actor_identity"], r["actor_generation"], r["reason"]) ==
                   ("front_desk", FD, auth["session_generation"], REASON) for r in rows)
        assert (await env.store.latest_close_audit(HOST + ":target"))["actor_kind"] == "front_desk"
    scenario(run)


@pytest.mark.parametrize("case,code", [
    ("ordinary", "close_unauthorized"), ("stale_fd", "close_unauthorized"),
    ("closed_fd", "close_unauthorized"), ("unverified", "close_unauthorized"),
    ("forged", "close_unauthorized"), ("protected", "close_protected"), ("missing_reason", "lifecycle_reason_required"),
    ("default_reason", "lifecycle_reason_required"), ("missing_request", "lifecycle_request_id_required"),
    ("generation", "lifecycle_generation_mismatch"), ("child", "close_live_children"),
    ("pending_spawn", "close_pending_spawn"),
])
def test_fd_refusals_never_terminate(monkeypatch, case, code):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def run(env):
        auth = await setup(env)
        msg = close(auth, operator_confirm=True, operator_override=True)
        if case in {"ordinary", "forged"}:
            await env.open("ordinary", role="lead", title="Front desk")
            msg["_auth_context"] = await env.seat("ordinary")
            msg.update(actor_stream_id=FD, from_stream_id=FD, role="assistant")
        elif case == "stale_fd":
            auth["session_generation"] = "stale"
        elif case == "closed_fd":
            await env.store.update_session(HOST, "front-desk", status="closed")
        elif case == "unverified":
            auth["token_verified"] = False
        elif case == "protected":
            await env.store.update_session(HOST, "target", role="assistant")
        elif case == "missing_reason":
            msg.pop("reason")
        elif case == "default_reason":
            msg["reason"] = "manual"
        elif case == "missing_request":
            msg.pop("request_id")
        elif case == "generation":
            msg["expected_generation"] = "old-target-generation"
        elif case == "child":
            await env.open("child", parent_stream_id=HOST + ":target")
        elif case == "pending_spawn":
            assert await env.store.reserve_stream_id(HOST, "pending", ttl_s=60, request_id="sp", nonce="n")
            assert await env.store.record_spawn_intent(HOST, "pending",
                {"open_fields": {"parent_stream_id": HOST + ":target"}}, request_id="sp", nonce="n")
        await _refused(env.server._on_close(msg), code)
        assert env.tmux.killed == []
        assert (await env.store.fetch_session(HOST, "target"))["status"] == "open"
        assert (await env.grant())["stream_id"] is None
    scenario(run)


@pytest.mark.parametrize("pointer", ["lead", "chat"])
def test_open_operator_lane_is_protected_despite_force_and_citation(pointer):
    async def run(env):
        auth = await setup(env)
        saved = await lane(env, pointer=pointer)
        await _refused(env.server._on_close(close(auth, operator_confirm=True, operator_override=True,
                                                 confirmation_question_id="forged")), "close_operator_lane_protected")
        assert (await env.store.get_work_lane(saved["lane_id"]))["lane"] == saved
        assert env.tmux.killed == []
    scenario(run)


def test_fd_cannot_force_a_working_target():
    async def run(env):
        auth = await setup(env)
        env.tmux.working.add("target")
        result = await env.server._on_close(close(auth, operator_override=True, operator_confirm=True))
        assert result["type"] == "close.failed" and "reap_fenced" in result["reason"]
        assert (await env.store.fetch_session(HOST, "target"))["status"] == "open"
        assert env.tmux.killed == []
    scenario(run)


@pytest.mark.parametrize("failure", ["audit", "lanes", "binding"])
def test_unavailable_authoritative_state_never_kills(failure):
    async def run(env):
        auth = await setup(env)
        async def broken(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic unavailable read/write")
        if failure == "audit":
            env.store.lifecycle_authority_audit = broken
        elif failure == "lanes":
            env.store.work_lane_rows = broken
        else:
            env.store.lifecycle_authority_holder = broken
        with pytest.raises((VerbError, sqlite3.OperationalError)):
            await env.server._on_close(close(auth))
        assert env.tmux.killed == [] and (await env.store.fetch_session(HOST, "target"))["status"] == "open"
    scenario(run)


@pytest.mark.parametrize("change,code", [("binding", "authority_holder_required"),
    ("fd_generation", "authority_holder_required"), ("target_generation", "lifecycle_generation_mismatch"),
    ("lane", "close_operator_lane_protected")])
def test_admission_rechecks_after_preliminary_authorization(change, code):
    async def run(env):
        auth = await setup(env)
        reached, resume = asyncio.Event(), asyncio.Event()
        original = env.sessions.close
        async def paused(*args, **kwargs):
            reached.set()
            await resume.wait()
            return await original(*args, **kwargs)
        env.sessions.close = paused
        task = asyncio.create_task(env.server._on_close(close(auth)))
        await asyncio.wait_for(reached.wait(), 5)
        try:
            if change == "binding":
                await env.open("replacement", provider="codex")
                await env.store.recover_assistant_binding(name=env.server.assistant_composite.env_config.name,
                    target_stream_id=HOST + ":replacement", target_generation=await env.gen("replacement"))
            elif change == "fd_generation":
                await env.open("front-desk", role="lead")
            elif change == "target_generation":
                await env.open("target")
            else:
                await lane(env)
        finally:
            resume.set()
        await _refused(task, code)
        assert env.tmux.killed == [] and (await env.store.fetch_session(HOST, "target"))["status"] == "open"
    scenario(run)


@pytest.mark.parametrize("mutation", ["lane", "binding", "visibility", "role"])
def test_mutations_wait_through_admitted_termination(mutation):
    async def run(env):
        auth = await setup(env)
        await env.open("replacement", provider="codex")
        reached, resume, trying = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = env.tmux.kill_session
        async def paused(name):
            reached.set()
            await resume.wait()
            await original(name)
        env.tmux.kill_session = paused
        closing = asyncio.create_task(env.server._on_close(close(auth)))
        await asyncio.wait_for(reached.wait(), 5)
        async def mutate():
            trying.set()
            if mutation == "binding":
                return await env.store.recover_assistant_binding(
                    name=env.server.assistant_composite.env_config.name,
                    target_stream_id=HOST + ":replacement", target_generation=await env.gen("replacement"))
            if mutation == "visibility":
                return await env.sessions.set_visibility(HOST, "target", "hidden")
            if mutation == "role":
                return await env.sessions.set_role(HOST, "target", "worker", auth_context=auth)
            return await lane(env)
        changing = asyncio.create_task(mutate())
        await trying.wait()
        await _until(lambda: bool(env.sessions._graph_lock._waiters))
        assert not changing.done()
        resume.set()
        assert (await asyncio.wait_for(closing, 5))["type"] == "close.ok"
        # The close wins this serialized race. A subsequent lane mutation may
        # reject its now-closed chat; it cannot retroactively protect a live seat.
        changed, = await asyncio.gather(changing, return_exceptions=True)
        if isinstance(changed, BaseException):
            if mutation == "role":
                assert isinstance(changed, VerbError) and changed.code == "unknown_session"
            else:
                assert mutation == "lane" and isinstance(changed, ValueError)
                assert str(changed) == "work_lane_visible_chat_invalid"
        assert env.tmux.killed == ["target"]
    scenario(run)


def test_owner_change_before_admission_preserves_lane_and_target():
    async def run(env):
        auth = await setup(env)
        saved = await lane(env)
        composite = env.server.assistant_composite
        async def set_owner(to, version):
            result = await composite.operation({"type": "assistant.operation", "request_id": "owner-" + to,
                "composite_stream_id": HOST + ":assistant", "dispatch_id": "none",
                "operation": "work_lane.set_owner", "lane_id": saved["lane_id"],
                "expected_lane_version": version, "payload": {"to": to, "reason": "Synthetic custody change"},
                "_auth_context": auth}, actor_stream_id=FD)
            return result["lane"]
        fd_lane = await set_owner("fd", saved["version"])
        reached, resume = asyncio.Event(), asyncio.Event()
        original = env.sessions.close
        async def paused(*args, **kwargs):
            reached.set()
            await resume.wait()
            return await original(*args, **kwargs)
        env.sessions.close = paused
        task = asyncio.create_task(env.server._on_close(close(auth)))
        await asyncio.wait_for(reached.wait(), 5)
        try:
            operator_lane = await set_owner("operator", fd_lane["version"])
        finally:
            resume.set()
        await _refused(task, "close_operator_lane_protected")
        assert (await env.store.get_work_lane(saved["lane_id"]))["lane"] == operator_lane
        assert env.tmux.killed == []
    scenario(run)


def test_fd_reparent_is_generation_fenced_and_audited():
    async def run(env):
        auth = await setup(env)
        await env.open("child", parent_stream_id=FD, role="lead")
        await env.open("grandchild", parent_stream_id=HOST + ":child")
        msg = {"stream_id": HOST + ":grandchild", "new_parent_stream_id": FD,
               "reason": REASON, "request_id": "reparent-fixture", "_auth_context": auth}
        await _refused(env.server._on_reparent(msg), "lifecycle_generation_required")
        await _refused(env.server._on_reparent({**msg, "expected_generation": "old"}), "lifecycle_generation_mismatch")
        assert (await env.store.fetch_session(HOST, "grandchild"))["parent_stream_id"] == HOST + ":child"
        await env.server._on_reparent({**msg, "expected_generation": await env.gen("grandchild")})
        assert (await env.store.fetch_session(HOST, "grandchild"))["parent_stream_id"] == FD
        rows = [r for r in await env.audit() if r["action"] == "manager_reparent"]
        assert [r["result"] for r in rows] == ["refused", "refused", "admitted", "applied"]
        assert {r["actor_kind"] for r in rows} == {"front_desk"}
    scenario(run)


@pytest.mark.parametrize("reported", [False, True])
def test_fd_preserves_external_ruling_with_or_without_report(reported):
    from test_approved_close_retry import HOST as LANE_HOST, TARGET, journey
    async def run():
        async with journey() as env:
            env.sessions.assistant.front_desk = env.server.assistant_composite
            await env.store.update_session(LANE_HOST, "lane", parent_stream_id=None)
            await env.sessions.refresh()
            if not reported:
                def progress(conn):
                    with conn:
                        conn.execute("UPDATE v2_reports SET status='progress'")
                await env.store.submit(progress)
            msg = {"stream_id": TARGET, "request_id": "fd-ruling-close", "reason": REASON,
                   "operator_confirm": True, "operator_override": True,
                   "_auth_context": await env.auth("primary")}
            pending = await env.server._on_close(msg)
            await env.rulings.stop()
            assert pending["type"] == "close.pending_ruling" and env.tmux.kills == 0
            env.rid = pending["ruling_request_id"]
            env.tmux.text = "ready\n"
            approved = await env.approve()
            assert approved["outcome"]["type"] == "close.ok" and env.tmux.kills == 1
            assert (await env.store.lifecycle_authority_current())["stream_id"] is None
            audits = await env.store.lifecycle_authority_audit_rows(limit=20)
            assert all(r["actor_kind"] == "front_desk" for r in audits if r["action"].startswith("manager_"))
    asyncio.run(run())


def test_fd_close_of_undisposed_lead_keeps_owed_debt_with_fd():
    from test_work_unattended import cached
    async def run(env):
        auth = await setup(env)
        await cached(env.store)
        await env.open("other", role="lead")
        await env.store.update_session(HOST, "target", role="lead", assigned_spec_id="spec_demo__item_00",
                                       parent_stream_id=HOST + ":other")
        await env.sessions.refresh()
        await env.store.update_session(HOST, "target", parent_stream_id=FD)
        env.tmux.working.add("target")
        result = await env.server._on_close(close(auth, force=True))
        assert result["type"] == "close.ok" and env.tmux.killed == ["target"]
        owed = await env.store.work_owed()
        assert [(i["spec_id"], i["closing_stream_id"]) for i in owed["items"]] == [
            ("spec_demo__item_00", HOST + ":target")]
        state = await env.store.submit(lambda c: __import__("store_work_index")._state_conn(c))
        debt, = state["_lead_dispositions"]["owed"].values()
        assert debt["front_desk_owned"] is True
    scenario(run)


def test_cancelled_fd_close_finishes_outcome_audit(monkeypatch):
    from test_lifecycle_authority import _pause_after, _cancel_after_commit
    async def run(env):
        auth = await setup(env)
        committed, resume = _pause_after(env, "mark_closed", monkeypatch=monkeypatch)
        task = asyncio.create_task(env.server._on_close(close(auth)))
        await _cancel_after_commit(task, committed, resume)
        assert (await env.store.fetch_session(HOST, "target"))["status"] == "closed"
        assert [r["result"] for r in await env.audit() if r["action"] == "manager_close"] == ["admitted", "applied"]
    scenario(run)


def test_old_close_receipt_cannot_kill_replacement_generation():
    async def run(env):
        auth = await setup(env)
        msg = close(auth, expected_generation=await env.gen("target"))
        assert (await env.server._on_close(msg))["type"] == "close.ok"
        replacement = await env.open("target")
        await _refused(env.server._on_close(msg), "lifecycle_generation_mismatch")
        assert env.tmux.killed == ["target"]
        row = await env.store.fetch_session(HOST, "target")
        assert row["status"] == "open" and row["session_generation"] == replacement["session_generation"]
    scenario(run)


# -- authority follows the current primary binding (spec 2026-11) ----------

B = HOST + ":front-desk-b"


async def swap(env):
    """Bound A, legacy grant to a since-closed X, then a real rebind A -> B."""
    auth_a = await setup(env)
    composite = env.server.assistant_composite
    # The pre-binding daemon designated X; X has since closed.
    env.sessions.assistant.front_desk = None
    await env.open("legacy", role="lead")
    await env.mutate(OPERATOR, "designate", target="legacy", request_id="legacy-grant")
    await env.store.update_session(HOST, "legacy", status="closed")
    env.sessions.assistant.front_desk = composite
    await env.open("front-desk-b", role="lead", provider="codex", effective_model="fixture-model",
                   effective_effort="medium")
    await composite.rebind({"type": "assistant.rebind", "request_id": "a-to-b", "expected_revision": 0,
                            "target_stream_id": B}, actor_stream_id=FD)
    await composite.load_binding()
    return auth_a, await env.seat("front-desk-b")


@pytest.mark.parametrize("shape", ["non_child", "hidden_child", "handoff_successor"])
def test_rebound_fd_closes_every_ownership_shape_without_consent(shape):
    async def run(env):
        _, auth_b = await swap(env)
        await env.open("other", role="lead")
        name = {"non_child": "target", "hidden_child": "hidden", "handoff_successor": "successor"}[shape]
        if shape == "hidden_child":
            await env.open("hidden", parent_stream_id=HOST + ":other", visibility="hidden")
        elif shape == "handoff_successor":
            await env.open("successor", handoff_from_stream_id=HOST + ":predecessor")
        await env.report(name)
        grant = await env.grant()
        result = await env.server._on_close(close(auth_b, stream_id=f"{HOST}:{name}", request_id="b-" + name))
        assert result["type"] == "close.ok" and env.tmux.killed == [name]
        assert await env.grant() == grant  # no designation, consent or grant movement
        rows = [r for r in await env.audit() if r["target_stream_id"] == f"{HOST}:{name}"]
        assert [r["result"] for r in rows][-1] == "applied"
        assert all(r["actor_identity"] == B for r in rows)
    scenario(run)


def test_rebound_fd_reparents_and_inspect_names_binding_holder():
    async def run(env):
        _, auth_b = await swap(env)
        await env.open("other", role="lead")
        await env.open("kid", parent_stream_id=HOST + ":other")
        reply = await env.server._on_reparent({"stream_id": HOST + ":kid", "new_parent_stream_id": B,
            "reason": REASON, "request_id": "b-reparent", "expected_generation": await env.gen("kid"),
            "_auth_context": auth_b})
        assert (await env.store.fetch_session(HOST, "kid"))["parent_stream_id"] == B, reply
        inspect = await env.server._on_assistant_lifecycle({"action": "inspect", "_auth_context": auth_b})
        effective = inspect["effective"]
        assert (effective["holder_stream_id"], effective["holder_generation"]) == (B, auth_b["session_generation"])
        assert effective["source"] == "binding" and effective["binding_revision"] == 1
        assert effective["policy_state"] == "enabled"
    scenario(run)


@pytest.mark.parametrize("caller", ["old_fd", "legacy_holder", "forged_title", "stale_generation",
                                    "unavailable_binding"])
def test_only_the_current_binding_generation_has_fd_authority(monkeypatch, caller):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    async def run(env):
        auth_a, auth_b = await swap(env)
        await env.report("target")
        if caller == "old_fd":
            auth = auth_a
        elif caller == "legacy_holder":
            auth = {"token_verified": True, "stream_id": HOST + ":legacy",
                    "session_generation": await env.gen("legacy")}
        elif caller == "forged_title":
            await env.open("pretender", role="lead", title="Front desk")
            auth = {**await env.seat("pretender"), "role": "assistant"}
        elif caller == "stale_generation":
            auth = {**auth_b, "session_generation": "stale"}
        else:
            auth = auth_b
            await env.store.update_session(HOST, "front-desk-b", status="closed")
        with pytest.raises(VerbError):
            await env.server._on_close(close(auth, request_id="refused-" + caller))
        assert env.tmux.killed == [] and (await env.store.fetch_session(HOST, "target"))["status"] == "open"
    scenario(run)


async def fd_changes(env):
    return [r for r in await env.audit() if r["action"] == "fd_binding_change"]


def test_rebind_audits_one_transition_and_replay_adds_none():
    async def run(env):
        await swap(env)
        changes = await fd_changes(env)
        assert [(r["prior_stream_id"], r["target_stream_id"], r["actor_identity"], r["request_id"], r["reason"])
                for r in changes] == [(FD, B, FD, "a-to-b", "assistant rebind")]
        replay = await env.server.assistant_composite.rebind({"type": "assistant.rebind", "request_id": "a-to-b",
            "expected_revision": 0, "target_stream_id": B}, actor_stream_id=FD)
        assert replay["duplicate"] is True and len(await fd_changes(env)) == 1
    scenario(run)


def test_policy_activation_is_audited_once_and_legacy_holder_has_no_privilege(tmp_path):
    async def run(env):
        auth_a, auth_b = await swap(env)
        assert await env.store.lifecycle_authority_activate() is True
        env.store.stop(); env.store.start()
        assert await env.store.lifecycle_authority_activate() is False
        rows = [r for r in await env.audit() if r["action"] == "fd_policy_activation"]
        assert [(r["prior_stream_id"], r["reason"]) for r in rows] == [(HOST + ":legacy", "front-desk policy enabled")]
        await env.store.update_session(HOST, "legacy", status="open")  # even reopened, a snapshot grants nothing
        legacy = {"token_verified": True, "stream_id": HOST + ":legacy", "session_generation": await env.gen("legacy")}
        assert await env.sessions.assistant.manager_holds(legacy) is False
        assert await env.sessions.assistant.manager_holds(auth_b) is True
    scenario(run, path=str(tmp_path / "sessions.db"))


def test_revocation_survives_rebind_and_designation_only_reenables_the_current_fd():
    async def run(env):
        auth_a, auth_b = await swap(env)
        await env.mutate(OPERATOR, "revoke", request_id="revoke", reason="operator pause")
        assert await env.sessions.assistant.manager_holds(auth_b) is False
        composite = env.server.assistant_composite
        await composite.rebind({"type": "assistant.rebind", "request_id": "b-to-a", "expected_revision": 1,
                                "target_stream_id": FD}, actor_stream_id=B)
        assert await env.sessions.assistant.manager_holds(auth_a) is False  # still revoked
        assert (await env.store.lifecycle_authority_effective())["policy_state"] == "revoked"
        await _refused(env.mutate(OPERATOR, "designate", target="front-desk-b", request_id="former"),
                       "authority_target_not_front_desk")
        await _refused(env.mutate(OPERATOR, "designate", target="front-desk", target_generation="stale",
                                  request_id="stale"), "authority_target_generation_mismatch")
        receipt = (await env.mutate(OPERATOR, "designate", target="front-desk", request_id="enable"))["receipt"]
        assert (receipt["holder_stream_id"], receipt["holder_generation"]) == (FD, auth_a["session_generation"])
        assert await env.sessions.assistant.manager_holds(auth_a) is True
        assert await env.sessions.assistant.manager_holds(auth_b) is False
    scenario(run)


@pytest.mark.parametrize("revoked", [False, True])
def test_rollback_projection_never_lets_old_code_grant_the_former_fd(revoked):
    async def run(env):
        auth_a, auth_b = await swap(env)  # A stays open after A -> B
        if revoked:
            await env.mutate(OPERATOR, "revoke", request_id="revoke", reason="operator pause")
        receipt = await env.store.lifecycle_authority_project_for_rollback("release-rollback")
        assert receipt["holder_stream_id"] == (None if revoked else B)
        env.sessions.assistant.front_desk = None  # the pre-binding reader
        assert await env.sessions.assistant.manager_holds(auth_a) is False
        assert await env.sessions.assistant.manager_holds(auth_b) is (not revoked)
        # Rolling forward again keeps an enabled policy enabled and a revoked one revoked.
        env.sessions.assistant.front_desk = env.server.assistant_composite
        assert (await env.store.lifecycle_authority_effective())["policy_state"] == (
            "revoked" if revoked else "enabled")
    scenario(run)


def test_reparent_rechecks_the_principal_under_the_graph_lock():
    async def run(env):
        _, auth_b = await swap(env)
        await env.open("other", role="lead")
        await env.open("kid", parent_stream_id=HOST + ":other")
        reached, resume = asyncio.Event(), asyncio.Event()
        original = env.sessions.reparent
        async def paused(*args, **kwargs):
            reached.set()
            await resume.wait()
            return await original(*args, **kwargs)
        env.sessions.reparent = paused
        task = asyncio.create_task(env.server._on_reparent({"stream_id": HOST + ":kid", "new_parent_stream_id": B,
            "reason": REASON, "request_id": "raced", "expected_generation": await env.gen("kid"),
            "_auth_context": auth_b}))
        await asyncio.wait_for(reached.wait(), 5)
        try:
            await env.server.assistant_composite.rebind({"type": "assistant.rebind", "request_id": "b-to-a",
                "expected_revision": 1, "target_stream_id": FD}, actor_stream_id=B)
        finally:
            resume.set()
        await _refused(task, "authority_holder_required")
        assert (await env.store.fetch_session(HOST, "kid"))["parent_stream_id"] == HOST + ":other"
    scenario(run)
