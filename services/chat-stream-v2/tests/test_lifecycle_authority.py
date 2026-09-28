"""Operator-designated fleet lifecycle authority (designate/transfer/revoke).

Covers the locked contract: operator-only designation and revocation, holder
own-generation transfer, recipient eligibility re-read in the mutation
transaction, verified attribution, idempotent receipts, durability across a
store reopen, and the manager close/reparent fences (re-checked under the
lifecycle lock before any kill).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import store_lifecycle_authority as lifecycle_authority
from server import Server
from sessions import Sessions, VerbError
from store import Store

HOST = "node-a"
OPERATOR = {"operator_authenticated": True, "operator_principal": "operator:11111111-1111-4111-8111-111111111111",
            "transport": "v2"}
OTHER_OPERATOR = {"operator_authenticated": True, "operator_principal": "operator:22222222-2222-4222-8222-222222222222",
                  "transport": "v2"}


class IdleTmux:
    def __init__(self, working: set[str] | None = None) -> None:
        self.live: set[str] = set()
        self.working = working or set()
        self.killed: list[str] = []

    async def has_session(self, name):
        return name in self.live

    async def pane_pid(self, name):
        return ""

    async def pane_identity(self, name):
        return None

    async def capture_checked(self, name, **_):
        return True, ("Working (esc to interrupt)\n" if name in self.working else "ready\n")

    async def kill_session(self, name):
        self.killed.append(name)
        self.live.discard(name)


class Env:
    def __init__(self, store: Store, sessions: Sessions, server: Server, tmux: IdleTmux) -> None:
        self.store, self.sessions, self.server, self.tmux = store, sessions, server, tmux

    async def open(self, name: str, **fields):
        self.tmux.live.add(name)
        fields.setdefault("pane_status", "pane_alive")
        fields.setdefault("bootstrap_state", "ready")
        row = await self.store.open_session(HOST, name, **fields)
        await self.sessions.refresh()
        return row

    async def gen(self, name: str) -> str:
        return str((await self.store.fetch_session(HOST, name))["session_generation"])

    async def seat(self, name: str) -> dict:
        return {"token_verified": True, "stream_id": f"{HOST}:{name}", "session_generation": await self.gen(name)}

    async def grant(self) -> dict:
        return await self.store.lifecycle_authority_current()

    async def lifecycle(self, auth: dict, action: str, *, target: str | None = None, request_id: str = "r1",
                        reason: str = "operator designation", expected: int | None = None, **extra):
        msg = {"type": "assistant.lifecycle", "action": action, "request_id": request_id,
               "reason": reason, "_auth_context": auth, **extra}
        if target is not None:
            msg["target_stream_id"] = f"{HOST}:{target}"
            msg.setdefault("target_generation", await self.gen(target))
        msg["expected_revision"] = (await self.grant())["revision"] if expected is None else expected
        return await self.server._on_assistant_lifecycle(msg)

    async def mutate(self, auth: dict, action: str, *, target: str | None = None,
                     request_id: str = "r1", reason: str = "operator designation",
                     expected: int | None = None, **extra):
        """Exercise low-level lifecycle fences with a trusted test consent context.

        Actual wire admission/signature proof lives in test_consent. This fixture
        does not add a product bypass or patch the component under test.
        """
        if action == "inspect":
            return await self.lifecycle(auth, action, target=target)
        msg = {"action": action, "request_id": request_id, "reason": reason, **extra}
        if target is not None:
            msg["target_stream_id"] = f"{HOST}:{target}"
            msg.setdefault("target_generation", await self.gen(target))
        msg["expected_revision"] = (await self.grant())["revision"] if expected is None else expected
        try:
            async with self.sessions.assistant.authority_lock:
                receipt = await self.store.lifecycle_authority_mutate(
                    msg, {**auth, "_consent_id": "fixture-consent"}, self.sessions.assistant.role)
        except lifecycle_authority.AuthorityError as exc:
            raise VerbError(exc.code, str(exc)) from exc
        return {"receipt": receipt}

    async def report(self, name: str, status: str = "done", generation: str | None = None) -> None:
        generation = generation or await self.gen(name)

        def _op(conn):
            conn.execute("INSERT INTO v2_reports (report_id, from_stream_id, session_generation, status, summary, "
                         "created_at, ingested_at) VALUES (?, ?, ?, ?, 'done', '2026-09-26T00:00:00Z', '2026-09-26T00:00:00Z')",
                         (f"rep-{name}-{status}-{generation}", f"{HOST}:{name}", generation, status))
            conn.commit()

        await self.store.submit(_op)

    async def audit(self) -> list[dict]:
        return list(reversed(await self.store.lifecycle_authority_audit_rows(limit=200)))


def scenario(fn, *, path: str = ":memory:", working: set[str] | None = None):
    async def run():
        store = Store(path)
        store.start()
        tmux = IdleTmux(working)
        try:
            sessions = Sessions(store, tmux=tmux, local_host=HOST)
            server = Server(store=store, sessions=sessions, local_host=HOST)
            await fn(Env(store, sessions, server, tmux))
        finally:
            store.stop()
    asyncio.run(run())


async def _until(predicate, timeout: float = 5.0) -> None:
    """Wait for a condition instead of a fixed number of loop turns: store
    calls cross a thread, so their latency varies by machine."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.001)


async def _refused(coro, code: str) -> None:
    with pytest.raises(VerbError) as exc:
        await coro
    assert exc.value.code == code, exc.value.code


# -- designation / revocation authority ------------------------------------

def test_only_authenticated_operator_designates_and_attribution_is_verified(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        await env.open("lead2", role="lead")
        forged = {"actor_kind": "operator", "operator_principal": "operator:forged", "role": "assistant"}
        # Unauthenticated, forged wire claims, and an ordinary lead's own seat token.
        await _refused(env.mutate({}, "designate", target="bart", **forged), "authority_operator_required")
        await _refused(env.mutate(await env.seat("lead2"), "designate", target="bart", request_id="r2",
                                     **forged), "authority_operator_required")
        # A connection that carries a seat token never acts as operator.
        both = {**OPERATOR, **await env.seat("lead2")}
        await _refused(env.mutate(both, "designate", target="lead2", request_id="r3"), "authority_operator_required")
        assert (await env.grant())["stream_id"] is None
        reply = await env.mutate(OPERATOR, "designate", target="bart", request_id="d1")
        receipt = reply["receipt"]
        assert receipt["holder_stream_id"] == "node-a:bart"
        assert receipt["holder_generation"] == await env.gen("bart")
        assert receipt["actor_kind"] == "operator"
        assert receipt["actor_identity"] == OPERATOR["operator_principal"]
        assert receipt["actor_generation"] is None
        assert receipt["revision"] == 1
        rows = await env.audit()
        assert [(r["actor_kind"], r["actor_identity"], r["result"]) for r in rows] == [
            ("unauthenticated", None, "refused"),
            ("seat", "node-a:lead2", "refused"),
            ("seat", "node-a:lead2", "refused"),
            ("operator", OPERATOR["operator_principal"], "applied"),
        ]
        assert rows[-1]["actor_generation"] is None and rows[-1]["new_revision"] == 1
        assert all("forged" not in json.dumps(r) for r in rows)

    scenario(check)


@pytest.mark.parametrize("role,fields,code", [
    ("worker", {}, "authority_target_ineligible"),
    ("qa", {}, "authority_target_ineligible"),
    ("planner", {}, "authority_target_ineligible"),
    (None, {}, "authority_target_ineligible"),
    ("lead", {"pane_status": "pane_dead"}, "authority_target_not_ready"),
    ("lead", {"bootstrap_state": "booting"}, "authority_target_not_ready"),
    ("lead", {"offline_since_ts": 123}, "authority_target_not_ready"),
    ("lead", {"pane_status": "pane_unknown"}, "authority_target_not_ready"),
    ("lead", {"bootstrap_state": None}, "authority_target_not_ready"),
])
@pytest.mark.parametrize("action", ["designate", "replace"])
def test_recipient_matrix_refuses_ineligible(monkeypatch, role, fields, code, action):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        await env.open("cand", role=role, **fields)
        if action != "designate":
            await env.mutate(OPERATOR, "designate", target="bart", request_id="d0")
        before = await env.grant()
        # A caller-supplied role/title never establishes eligibility.
        forged = {"role": "lead", "target_role": "lead", "title": "Bart right hand"}
        coro = env.mutate(OPERATOR, "designate", target="cand", request_id="d1", **forged)
        await _refused(coro, code)
        assert await env.grant() == before
        assert (await env.store.fetch_session(HOST, "cand"))["role"] == role

    scenario(check)


def test_recipient_generation_closed_and_unknown_refused(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        stale = await env.gen("bart")
        await _refused(env.mutate(OPERATOR, "designate", target="bart", target_generation="nope"),
                       "authority_target_generation_mismatch")
        await env.store.mark_closed(HOST, "bart", closed_at="2026-09-26T00:00:00Z", pane_status="pane_dead")
        await _refused(env.mutate(OPERATOR, "designate", target="bart", request_id="r2",
                                     target_generation=stale), "authority_target_unavailable")
        await env.open("bart", role="lead")  # same name, new generation
        assert await env.gen("bart") != stale
        await _refused(env.mutate(OPERATOR, "designate", target="bart", request_id="r3",
                                     target_generation=stale), "authority_target_generation_mismatch")
        await _refused(env.mutate(OPERATOR, "designate", request_id="r4",
                                     target_stream_id="node-a:ghost", target_generation="g"),
                       "authority_target_unavailable")
        assert (await env.grant())["stream_id"] is None

    scenario(check)


def test_protected_assistant_generation_is_eligible(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        await env.open("helper", role="assistant")
        await env.open("worker", role="worker")
        reply = await env.mutate(OPERATOR, "designate", target="helper")
        assert reply["receipt"]["holder_stream_id"] == "node-a:helper"
        # Eligibility is a prerequisite only; a protected role never confers authority.
        assert (await env.store.fetch_session(HOST, "helper"))["role"] == "assistant"

    scenario(check)


def test_eligibility_change_between_read_and_commit_refuses(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        generation = await env.gen("bart")
        readback = (await env.mutate(OPERATOR, "inspect", target="bart"))["target"]
        assert readback == {"stream_id": "node-a:bart", "session_generation": generation, "role": "lead",
                            "eligible": True, "refusal_code": None}
        # Role changes after the operator's read but before the mutation commits.
        await env.store.update_session(HOST, "bart", role="worker")
        await _refused(env.mutate(OPERATOR, "designate", target="bart", target_generation=generation),
                       "authority_target_ineligible")
        assert (await env.grant())["stream_id"] is None

    scenario(check)


def test_revision_conflict_and_idempotent_operator_retry(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        await env.open("lead2", role="lead")
        await _refused(env.mutate(OPERATOR, "designate", target="bart", expected=5), "authority_revision_conflict")
        first = await env.mutate(OPERATOR, "designate", target="bart", request_id="d1", expected=0)
        again = await env.mutate(OPERATOR, "designate", target="bart", request_id="d1", expected=0)
        assert again["receipt"]["replayed"] is True
        assert {k: v for k, v in again["receipt"].items() if k != "replayed"} == first["receipt"]
        assert (await env.grant())["revision"] == 1
        # Altered payload under the same key refuses; a different operator
        # principal with the same request id is a distinct request.
        await _refused(env.mutate(OPERATOR, "designate", target="lead2", request_id="d1", expected=1),
                       "authority_request_conflict")
        await _refused(env.mutate(OTHER_OPERATOR, "designate", target="lead2", request_id="d1", expected=0),
                       "authority_revision_conflict")
        replaced = await env.mutate(OTHER_OPERATOR, "designate", target="lead2", request_id="d1", expected=1)
        assert replaced["receipt"]["prior_stream_id"] == "node-a:bart"
        assert replaced["receipt"]["actor_identity"] == OTHER_OPERATOR["operator_principal"]
        assert (await env.grant())["stream_id"] == "node-a:lead2"

    scenario(check)


def test_missing_reason_or_request_id_refuses(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        await _refused(env.mutate(OPERATOR, "designate", target="bart", reason="  "), "authority_reason_required")
        await _refused(env.mutate(OPERATOR, "designate", target="bart", request_id=""),
                       "authority_request_id_required")
        await _refused(env.mutate(OPERATOR, "revoke", request_id="x"), "authority_not_held")
        with pytest.raises(VerbError) as exc:
            await env.server._on_assistant_lifecycle({"action": "grant_all", "_auth_context": OPERATOR})
        assert exc.value.code == "invalid_request"

    scenario(check)


# -- transfer ----------------------------------------------------------------

def test_transfer_disabled_for_operator_holder_and_other_seat(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    async def check(env: Env):
        await env.open("bart", role="lead")
        await env.open("lead2", role="lead")
        await env.mutate(OPERATOR, "designate", target="bart", request_id="d1")
        before = await env.grant()
        for auth in (OPERATOR, await env.seat("bart"), await env.seat("lead2")):
            await _refused(env.mutate(auth, "transfer", target="lead2"), "authority_transfer_disabled")
            await _refused(env.lifecycle(auth, "transfer", target="lead2"), "authority_transfer_disabled")
        assert await env.grant() == before
    scenario(check)


def test_reopened_holder_generation_does_not_inherit(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        await env.open("lead2", role="lead")
        await env.mutate(OPERATOR, "designate", target="bart", request_id="d1")
        old = await env.seat("bart")
        await env.store.mark_closed(HOST, "bart", closed_at="2026-09-26T00:00:00Z", pane_status="pane_dead")
        await env.open("bart", role="lead")
        new = await env.seat("bart")
        assert new["session_generation"] != old["session_generation"]
        for auth in (old, new):
            assert not await env.sessions.assistant.manager_holds(auth)
            await _refused(env.mutate(auth, "transfer", target="lead2", request_id=f"t-{auth['session_generation']}"),
                           "authority_transfer_disabled")

    scenario(check)


def test_concurrent_replace_and_revoke_leave_one_outcome(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    async def check(env: Env):
        for name in ("bart", "lead2"):
            await env.open(name, role="lead")
        await env.mutate(OPERATOR, "designate", target="bart", request_id="d1")
        results = await asyncio.gather(
            env.mutate(OPERATOR, "designate", target="lead2", request_id="d2", expected=1),
            env.mutate(OPERATOR, "revoke", request_id="v1", expected=1), return_exceptions=True)
        applied = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, VerbError)]
        assert len(applied) == 1 and len(refused) == 1
        assert refused[0].code == "authority_revision_conflict"
        grant = await env.grant()
        assert grant["revision"] == 2
        assert grant["stream_id"] == applied[0]["receipt"]["holder_stream_id"]
    scenario(check)


def test_audit_failure_prevents_authority_mutation(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    real = lifecycle_authority.audit

    def failing(conn, **fields):
        if fields.get("result") == "applied":
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, **fields)

    async def check(env: Env):
        await env.open("bart", role="lead")
        monkeypatch.setattr(lifecycle_authority, "audit", failing)
        with pytest.raises(sqlite3.OperationalError):
            await env.mutate(OPERATOR, "designate", target="bart")
        monkeypatch.setattr(lifecycle_authority, "audit", real)
        assert await env.grant() == {"stream_id": None, "session_generation": None, "revision": 0}
        assert [r["result"] for r in await env.audit()] == []

    scenario(check)


def test_revocation_and_grant_survive_restart_without_revival(monkeypatch, tmp_path):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    path = str(tmp_path / "v2.db")
    state: dict = {}

    async def first(env: Env):
        await env.open("bart", role="lead")
        await env.open("lead2", role="lead")
        await env.mutate(OPERATOR, "designate", target="bart", request_id="d1")
        state["bart"] = await env.seat("bart")
        await env.mutate(OPERATOR, "designate", target="lead2", request_id="d2")
        state["lead2"] = await env.seat("lead2")

    async def second(env: Env):
        assert (await env.grant())["stream_id"] == "node-a:lead2"
        assert await env.sessions.assistant.manager_holds(state["lead2"])
        assert not await env.sessions.assistant.manager_holds(state["bart"])
        replay = await env.mutate(OPERATOR, "designate", target="lead2", request_id="d2", expected=1)
        assert replay["receipt"]["replayed"] is True
        await env.mutate(OPERATOR, "revoke", request_id="v1", reason="rollback drill")

    async def third(env: Env):
        grant = await env.grant()
        assert grant == {"stream_id": None, "session_generation": None, "revision": 3}
        for auth in state.values():
            assert not await env.sessions.assistant.manager_holds(auth)
        actions = [(r["action"], r["result"]) for r in await env.audit()]
        assert actions == [("designate", "applied"), ("designate", "applied"), ("revoke", "applied")]
        raw = json.dumps(await env.audit())
        assert "token" not in raw.lower()

    for fn in (first, second, third):
        scenario(fn, path=path)


# -- manager close / reparent ----------------------------------------------

async def _manager(env: Env) -> dict:
    await env.open("bart", role="lead")
    await env.mutate(OPERATOR, "designate", target="bart", request_id="d-manager")
    return await env.seat("bart")


def _close(auth: dict, target: str, reason: str = "reported childless cleanup", **extra) -> dict:
    return {"type": "close", "stream_id": f"{HOST}:{target}", "reason": reason,
            "request_id": f"close-{target}", "_auth_context": auth, **extra}


def test_ordinary_lead_denied_until_designated_then_closes_reported_non_child(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("bart", role="lead")
        await env.open("other", role="lead")
        await env.open("fixture", role="worker", parent_stream_id="node-a:other")
        await env.report("fixture")
        bart = await env.seat("bart")
        await _refused(env.server._on_close(_close(bart, "fixture")), "close_unauthorized")
        assert (await env.store.fetch_session(HOST, "fixture"))["status"] == "open"
        await env.mutate(OPERATOR, "designate", target="bart", request_id="d1")
        generation = await env.gen("fixture")
        reply = await env.server._on_close(_close(bart, "fixture"))
        assert reply["type"] == "close.ok"
        assert env.tmux.killed == ["fixture"]
        assert (await env.store.fetch_session(HOST, "fixture"))["status"] == "closed"
        rows = [r for r in await env.audit() if r["action"] == "manager_close"]
        assert [(r["actor_kind"], r["result"], r["refusal_code"], r["actor_identity"], r["actor_generation"],
                 r["target_generation"], r["old_revision"], r["new_revision"]) for r in rows] == [
            ("seat", "refused", "close_unauthorized", "node-a:bart", bart["session_generation"], generation, 0, 0),
            ("manager", "admitted", None, "node-a:bart", bart["session_generation"], generation, 1, 1),
            ("manager", "applied", None, "node-a:bart", bart["session_generation"], generation, 1, 1),
        ]
        # Normal parent close is unchanged.
        await env.open("kid", role="worker", parent_stream_id="node-a:other")
        assert (await env.server._on_close(_close(await env.seat("other"), "kid")))["type"] == "close.ok"

    scenario(check)


@pytest.mark.parametrize("case,code", [
    ("unreported", "lifecycle_report_required"),
    ("progress_only", "lifecycle_report_required"),
    ("prior_generation_report", "lifecycle_report_required"),
    ("live_child", "close_live_children"),
    ("pending_spawn", "close_pending_spawn"),
    ("default_reason", "lifecycle_reason_required"),
    ("offline", "lifecycle_target_unavailable"),
    ("revoked", "authority_transfer_disabled"),
])
def test_manager_close_negative_controls_refuse_before_kill(monkeypatch, case, code):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("fixture", role="worker", offline_since_ts=1 if case == "offline" else None)
        if case == "progress_only":
            await env.report("fixture", status="progress")
        elif case == "prior_generation_report":
            await env.report("fixture", generation="older-generation")
        elif case != "unreported":
            await env.report("fixture")
        if case == "live_child":
            await env.open("child", role="worker", parent_stream_id="node-a:fixture")
        if case == "pending_spawn":
            assert await env.store.reserve_stream_id(HOST, "pending", ttl_s=60, request_id="sp1", nonce="n")
            assert await env.store.record_spawn_intent(
                HOST, "pending", {"open_fields": {"parent_stream_id": "node-a:fixture"}}, request_id="sp1", nonce="n")
        if case == "revoked":
            await env.mutate(OPERATOR, "revoke", request_id="v1", reason="stop")
        reason = "manual" if case == "default_reason" else "reported childless cleanup"
        expected = "close_unauthorized" if case == "revoked" else code
        await _refused(env.server._on_close(_close(manager, "fixture", reason=reason)), expected)
        assert (await env.store.fetch_session(HOST, "fixture"))["status"] == "open"
        assert "fixture" not in env.tmux.killed
        if case != "revoked":
            rows = [r for r in await env.audit() if r["action"] == "manager_close"]
            assert [(r["result"], r["refusal_code"]) for r in rows] == [("refused", code)]

    scenario(check)


def test_manager_close_of_protected_or_working_target_refuses(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("helper", role="assistant")
        await env.report("helper")
        await _refused(env.server._on_close(_close(manager, "helper")), "close_protected")
        await env.open("busy", role="worker")
        await env.report("busy")
        reply = await env.server._on_close(_close(manager, "busy"))
        assert reply["type"] != "close.ok"
        assert env.tmux.killed == []
        for name in ("helper", "busy"):
            assert (await env.store.fetch_session(HOST, name))["status"] == "open"

    scenario(check, working={"busy"})


@pytest.mark.parametrize("race", ["revoke", "child_spawn", "reopen"])
def test_fences_rechecked_under_lifecycle_lock(monkeypatch, race):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("fixture", role="worker")
        await env.report("fixture")
        lock = env.sessions._lifecycle_lock(HOST, "fixture")
        await lock.acquire()
        task = asyncio.create_task(env.server._on_close(_close(manager, "fixture")))
        await _until(lambda: env.sessions.assistant.authority_lock.locked())
        assert not task.done()  # admitted pre-lock, waiting at the barrier
        if race == "revoke":
            # The admitted close holds the authority lock: revocation waits for
            # it and is linearized after the close, never under it.
            revoke = asyncio.create_task(env.mutate(OPERATOR, "revoke", request_id="v1", reason="stop", expected=1))
            for _ in range(50):
                await asyncio.sleep(0)
            assert not revoke.done()
            lock.release()
            assert (await task)["type"] == "close.ok"
            await revoke
            rows = await env.audit()
            assert [(r["action"], r["result"]) for r in rows][-3:] == [
                ("manager_close", "admitted"), ("manager_close", "applied"), ("revoke", "applied")]
            return
        elif race == "child_spawn":
            await env.store.open_session(HOST, "late-child", role="worker", parent_stream_id="node-a:fixture")
            expected = "close_live_children"
        else:
            await env.store.mark_closed(HOST, "fixture", closed_at="2026-09-26T00:00:00Z", pane_status="pane_dead")
            await env.store.open_session(HOST, "fixture", role="worker")
            expected = "lifecycle_generation_mismatch"
        lock.release()
        await _refused(task, expected)
        assert "fixture" not in env.tmux.killed
        assert (await env.store.fetch_session(HOST, "fixture"))["status"] == "open"

    scenario(check)


def test_manager_reparent_fences(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="lead")
        await env.open("b", role="worker", parent_stream_id="node-a:a")
        await env.open("c", role="worker", parent_stream_id="node-a:b")
        await env.open("helper", role="assistant")
        await env.open("elsewhere", role="lead")

        def reparent(worker, parent, reason="adopt orphan", auth=manager):
            return env.server._on_reparent({"stream_id": f"{HOST}:{worker}", "new_parent_stream_id": f"{HOST}:{parent}",
                                            "reason": reason, "request_id": f"rp-{worker}-{parent}",
                                            "_auth_context": auth})

        await _refused(reparent("b", "c"), "reparent_cycle")
        await _refused(reparent("helper", "a"), "reparent_protected")
        await _refused(reparent("b", "elsewhere", reason="reparent"), "lifecycle_reason_required")
        reply = await reparent("c", "elsewhere")
        assert reply["new_parent_stream_id"] == "node-a:elsewhere"
        assert (await env.store.fetch_session(HOST, "c"))["parent_stream_id"] == "node-a:elsewhere"
        rows = [(r["result"], r["refusal_code"]) for r in await env.audit() if r["action"] == "manager_reparent"]
        assert rows == [("admitted", None), ("refused", "reparent_cycle"), ("admitted", None),
                        ("refused", "reparent_protected"), ("refused", "lifecycle_reason_required"),
                        ("admitted", None), ("applied", None)]
        await env.mutate(OPERATOR, "revoke", request_id="v1", reason="stop")
        await _refused(reparent("c", "a"), "reparent_unauthorized")

    scenario(check)


# -- protected handoff carry -----------------------------------------------

def test_handoff_carries_authority_only_from_exact_holder(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        await env.open("helper", role="assistant")
        await env.open("succ", role="assistant")
        await env.open("worker", role="worker")
        source_generation = await env.gen("helper")
        # Not the holder: no implicit grant.
        assert await env.store.lifecycle_authority_carry_on_handoff(
            "node-a:helper", source_generation, "node-a:succ", await env.gen("succ"), "assistant") is None
        await env.mutate(OPERATOR, "designate", target="helper", request_id="d1")
        # Ineligible successor: refused and audited; grant unchanged.
        assert await env.store.lifecycle_authority_carry_on_handoff(
            "node-a:helper", source_generation, "node-a:worker", await env.gen("worker"), "assistant") is None
        assert (await env.grant())["stream_id"] == "node-a:helper"
        moved = await env.store.lifecycle_authority_carry_on_handoff(
            "node-a:helper", source_generation, "node-a:succ", await env.gen("succ"), "assistant")
        assert moved["holder_stream_id"] == "node-a:succ" and moved["revision"] == 2
        # A stale source generation cannot carry it again.
        assert await env.store.lifecycle_authority_carry_on_handoff(
            "node-a:helper", source_generation, "node-a:worker", await env.gen("worker"), "assistant") is None

    scenario(check)


def test_unrelated_seat_cannot_reparent_top_level_stream(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        await env.open("helper", role="assistant")
        await env.open("loose", role="worker")
        await env.open("seat", role="lead")
        await env.open("a", role="lead")
        for worker in ("helper", "loose"):
            await _refused(env.server._on_reparent({
                "stream_id": f"{HOST}:{worker}", "new_parent_stream_id": "node-a:a", "reason": "grab",
                "request_id": "rp", "_auth_context": await env.seat("seat")}), "reparent_unauthorized")
            assert not (await env.store.fetch_session(HOST, worker)).get("parent_stream_id")

    scenario(check)


# -- retired protected source: exact receipt replay only ---------------------

class SpawnTmux(IdleTmux):
    def __init__(self):
        super().__init__()
        self.created = 0

    async def new_session(self, name, command, cwd=None, env=None):
        self.live.add(name)
        self.created += 1

    async def capture(self, name):
        return "READY"

    async def pane_pid(self, name):
        return "1234"

    async def session_state(self, name):
        return "alive" if name in self.live else "gone"


def test_retired_source_replays_only_its_exact_receipt(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    from spawnctl import SpawnCtl

    async def check(env: Env):
        tmux = SpawnTmux()
        env.sessions.tmux = tmux
        await env.open("old", role="assistant", token_hash="h-old", provider="claude",
                       effective_model="claude-opus-4-8", effective_effort="high")
        tmux.live.add("old")
        await env.mutate(OPERATOR, "designate", target="old", request_id="d1")
        source = await env.seat("old")
        ctl = SpawnCtl(env.store, env.sessions, tmux=tmux)
        msg = {"objective": "Rotate assistant", "handoff": True, "handoff_from_stream_id": "node-a:old",
               "command": "stub", "ready_marker": "READY", "idempotency_key": "hk1", "role": "assistant"}
        first = await ctl.spawn({**msg, "request_id": "q1", "_auth_context": source}, HOST)
        assert first["type"] == "spawn.ok"
        await asyncio.gather(*list(ctl._background_spawns))
        assert (await env.store.fetch_session(HOST, "old"))["status"] == "closed"
        successor = first["stream_id"]
        grant = await env.grant()
        assert grant["stream_id"] == successor and grant["revision"] == 2
        created = tmux.created
        reservations = len(await env.store.reservations(include_expired=True))
        retired = {"retired_handoff_owner": {"stream_id": "node-a:old", "session_generation": source["session_generation"],
                                             "token_hash": "h-old"}}
        replay = await ctl.spawn({**msg, "request_id": "q2", "_auth_context": retired}, HOST)
        assert replay["stream_id"] == successor and replay["do_not_respawn"] is True
        assert tmux.created == created
        assert len(await env.store.reservations(include_expired=True)) == reservations
        assert (await env.grant()) == grant  # no authority revival for the retired source
        bad = [
            ({**msg, "objective": "Changed"}, retired, "idempotency_key_conflict"),
            ({**msg, "idempotency_key": "other"}, retired, "assistant_handoff_receipt_unavailable"),
            (msg, {"retired_handoff_owner": {**retired["retired_handoff_owner"], "token_hash": "h-x"}},
             "assistant_handoff_replay_unauthorized"),
            (msg, {"retired_handoff_owner": {**retired["retired_handoff_owner"], "session_generation": "g-x"}},
             "assistant_handoff_replay_unauthorized"),
            (msg, {"retired_handoff_owner": {**retired["retired_handoff_owner"], "stream_id": "node-a:worker"}},
             "assistant_handoff_replay_unauthorized"),
        ]
        for payload, auth, code in bad:
            await _refused(ctl.spawn({**payload, "request_id": "q3", "_auth_context": auth}, HOST), code)
        assert tmux.created == created

    scenario(check)


def test_manager_close_binds_caller_generation_and_unknown_target(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("fixture", role="worker")
        await env.report("fixture")
        await _refused(env.server._on_close(_close(manager, "fixture", expected_generation="older")),
                       "lifecycle_generation_mismatch")
        await _refused(env.server._on_close(_close(manager, "ghost")), "lifecycle_target_unavailable")
        assert env.tmux.killed == []
        reply = await env.server._on_close(_close(manager, "fixture", expected_generation=await env.gen("fixture")))
        assert reply["type"] == "close.ok"

    scenario(check)


def test_opposite_concurrent_manager_reparents_cannot_form_cycle(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="worker")
        await env.open("b", role="worker")
        real_update = env.store.update_session
        gate = asyncio.Event()

        async def gated_update(*args, **kwargs):
            await gate.wait()  # both moves validated before either writes (pre-fix)
            return await real_update(*args, **kwargs)

        env.store.update_session = gated_update

        def move(worker, parent):
            return env.server._on_reparent({"stream_id": f"{HOST}:{worker}", "new_parent_stream_id": f"{HOST}:{parent}",
                                            "reason": "adopt", "request_id": f"rp-{worker}", "_auth_context": manager})

        tasks = [asyncio.create_task(move("a", "b")), asyncio.create_task(move("b", "a"))]
        for _ in range(50):
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        env.store.update_session = real_update
        ok = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, VerbError)]
        assert len(ok) == 1 and [r.code for r in refused] == ["reparent_cycle"]
        parents = {n: (await env.store.fetch_session(HOST, n)).get("parent_stream_id") for n in ("a", "b")}
        assert not (parents["a"] == f"{HOST}:b" and parents["b"] == f"{HOST}:a")

    scenario(check)


def test_caller_text_is_scrubbed_and_inspect_requires_auth(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
    canary = "Zq7" + "xK9mPw2LrT8vNc4Hs6Jd1YbQ"

    async def check(env: Env):
        await env.open("bart", role="lead")
        await _refused(env.mutate({}, "designate", target="bart", reason=f"leak {canary}", request_id=canary),
                       "authority_operator_required")
        await env.mutate(OPERATOR, "designate", target="bart", request_id="d1", reason=f"ok {canary}\x00")
        rows = await env.audit()
        assert rows[0]["reason"] is None and rows[0]["request_id"] is None  # unverified caller text dropped
        assert rows[1]["reason"] == "ok [redacted]"
        assert canary not in json.dumps(rows)
        with pytest.raises(VerbError) as exc:
            await env.server._on_assistant_lifecycle({"action": "inspect", "_auth_context": {}})
        assert exc.value.code == "authentication_required"
        assert (await env.mutate(await env.seat("bart"), "inspect"))["grant"]["stream_id"] == "node-a:bart"

    scenario(check)


def test_unauthorized_seat_reparent_is_audited(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        await env.open("seat", role="lead")
        await env.open("a", role="lead")
        await env.open("b", role="lead")
        await env.open("w", role="worker", parent_stream_id="node-a:a")
        seat = await env.seat("seat")
        await _refused(env.server._on_reparent({"stream_id": "node-a:w", "new_parent_stream_id": "node-a:b",
                                                "reason": "grab", "request_id": "rp", "_auth_context": seat}),
                       "reparent_unauthorized")
        rows = await env.audit()
        assert [(r["action"], r["actor_kind"], r["actor_identity"], r["actor_generation"], r["result"],
                 r["refusal_code"], r["old_revision"]) for r in rows] == [
            ("manager_reparent", "seat", "node-a:seat", seat["session_generation"], "refused",
             "reparent_unauthorized", 0)]

    scenario(check)


# -- authority mutations are linearized with manager effects ---------------

async def _pause_during(env: Env, attr: str, action) -> tuple[bool, object]:
    """Pause the manager action inside `store.<attr>`, run `action` meanwhile.

    Returns whether `action` finished while the manager action was paused
    (it must not: the grant cannot change under an admitted effect).
    """
    real = getattr(env.store, attr)
    paused, release = asyncio.Event(), asyncio.Event()

    async def gated(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real(*args, **kwargs)

    setattr(env.store, attr, gated)
    return paused, release, real


async def _race(env: Env, manager_call, attr: str, authority_call):
    paused, release, real = await _pause_during(env, attr, None)
    effect = asyncio.create_task(manager_call())
    await asyncio.wait_for(paused.wait(), 5)
    mutation = asyncio.create_task(authority_call())
    for _ in range(100):
        await asyncio.sleep(0)
    finished_while_paused = mutation.done()
    release.set()
    results = await asyncio.gather(effect, mutation, return_exceptions=True)
    setattr(env.store, attr, real)
    return finished_while_paused, results


def _no_effect_after_authority_change(rows: list[dict], manager_action: str) -> bool:
    change = next(i for i, r in enumerate(rows) if r["action"] in {"revoke", "designate"} and r["result"] == "applied" and r["new_revision"] > 1)
    return not any(r["action"] == manager_action and r["result"] in {"admitted", "applied"}
                   for r in rows[change + 1:])


@pytest.mark.parametrize("change", ["revoke", "replace"])
def test_authority_change_is_linearized_with_manager_close(monkeypatch, change):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("lead2", role="lead")
        await env.open("fixture", role="worker")
        await env.report("fixture")

        async def authority():
            if change == "revoke":
                return await env.mutate(OPERATOR, "revoke", request_id="v1", reason="stop now", expected=1)
            return await env.mutate(OPERATOR, "designate", target="lead2", request_id="d2", reason="replace holder",
                                       expected=1)

        finished_early, results = await _race(env, lambda: env.server._on_close(_close(manager, "fixture")),
                                              "find_report", authority)
        assert not finished_early, "authority changed while an admitted manager close was in flight"
        rows = await env.audit()
        assert _no_effect_after_authority_change(rows, "manager_close")
        applied = [r for r in rows if r["action"] == "manager_close" and r["result"] in {"admitted", "applied"}]
        assert all(r["old_revision"] == 1 for r in applied)

    scenario(check)


def test_revoke_is_linearized_with_manager_reparent(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="lead")
        await env.open("w", role="worker")
        finished_early, results = await _race(
            env,
            lambda: env.server._on_reparent({"stream_id": f"{HOST}:w", "new_parent_stream_id": f"{HOST}:a",
                                             "reason": "adopt", "request_id": "rp-w", "_auth_context": manager}),
            "update_session",
            lambda: env.mutate(OPERATOR, "revoke", request_id="v1", reason="stop now", expected=1))
        assert not finished_early, "revocation committed while an admitted manager reparent was in flight"
        assert _no_effect_after_authority_change(await env.audit(), "manager_reparent")

    scenario(check)


def test_authority_lock_ordering_has_no_deadlock(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        for name in ("lead2", "a"):
            await env.open(name, role="lead")
        for name in ("f1", "f2", "w1"):
            await env.open(name, role="worker")
            await env.report(name)
        calls = [
            env.server._on_close(_close(manager, "f1")),
            env.server._on_reparent({"stream_id": f"{HOST}:w1", "new_parent_stream_id": f"{HOST}:a",
                                     "reason": "adopt", "request_id": "rp", "_auth_context": manager}),
            env.server._on_close(_close(manager, "f2")),
            env.server._on_close({"type": "close", "stream_id": f"{HOST}:w1", "reason": "operator",
                                  "request_id": "op", "operator_confirm": True, "_auth_context": OPERATOR}),
            env.mutate(OPERATOR, "inspect"),
            env.mutate(OPERATOR, "revoke", request_id="v1", reason="stop", expected=1),
        ]
        results = await asyncio.wait_for(asyncio.gather(*calls, return_exceptions=True), 10)
        assert not any(isinstance(r, asyncio.TimeoutError) for r in results)
        assert (await env.grant())["stream_id"] is None

    scenario(check)


# -- cancellation after commit: the outcome audit is durable ------------------

def _pause_after(env: Env, method: str, when=lambda *a, **k: True):
    """Pause the named store write after it commits; return (committed, resume)."""
    committed, resume = asyncio.Event(), asyncio.Event()
    original = getattr(env.store, method)

    async def paused(*args, **kwargs):
        result = await original(*args, **kwargs)
        if when(*args, **kwargs):
            committed.set()
            await resume.wait()
        return result

    setattr(env.store, method, paused)
    return committed, resume


async def _cancel_after_commit(task: asyncio.Task, committed: asyncio.Event, resume: asyncio.Event) -> None:
    await committed.wait()
    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_manager_close_cancelled_after_commit_keeps_applied_audit(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("fixture", role="worker")
        await env.report("fixture")
        committed, resume = _pause_after(env, "mark_closed")
        task = asyncio.create_task(env.server._on_close(_close(manager, "fixture")))
        await _cancel_after_commit(task, committed, resume)
        assert (await env.store.fetch_session(HOST, "fixture"))["status"] == "closed"
        assert env.tmux.killed == ["fixture"]
        rows = [r for r in await env.audit() if r["action"] == "manager_close"]
        assert [(r["result"], r["old_revision"]) for r in rows] == [("admitted", 1), ("applied", 1)]

    scenario(check)


def test_manager_reparent_cancelled_after_commit_keeps_applied_audit(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="lead")
        await env.open("orphan", role="worker")
        committed, resume = _pause_after(env, "update_session", lambda *a, **k: "parent_stream_id" in k)
        task = asyncio.create_task(env.server._on_reparent({
            "stream_id": f"{HOST}:orphan", "new_parent_stream_id": f"{HOST}:a", "reason": "adopt orphan",
            "request_id": "rp-cancel", "_auth_context": manager}))
        await _cancel_after_commit(task, committed, resume)
        assert (await env.store.fetch_session(HOST, "orphan"))["parent_stream_id"] == "node-a:a"
        rows = [r for r in await env.audit() if r["action"] == "manager_reparent"]
        assert [(r["result"], r["old_revision"]) for r in rows] == [("admitted", 1), ("applied", 1)]

    scenario(check)


@pytest.mark.parametrize("action", ["close", "reparent"])
def test_manager_action_cancelled_before_authority_lock_has_no_effect(monkeypatch, action):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="lead")
        await env.open("fixture", role="worker")
        await env.report("fixture")
        lock = env.sessions.assistant.authority_lock
        await lock.acquire()
        if action == "close":
            request = env.server._on_close(_close(manager, "fixture"))
        else:
            request = env.server._on_reparent({
                "stream_id": f"{HOST}:fixture", "new_parent_stream_id": f"{HOST}:a", "reason": "adopt orphan",
                "request_id": "rp-wait", "_auth_context": manager})
        task = asyncio.create_task(request)
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(50):
            await asyncio.sleep(0)
        row = await env.store.fetch_session(HOST, "fixture")
        assert row["status"] == "open" and not row.get("parent_stream_id")
        assert env.tmux.killed == []
        assert not [r for r in await env.audit() if r["action"].startswith("manager_")]

    scenario(check)


def test_manager_refusal_cancelled_after_audit_stays_refused(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("fixture", role="worker")  # unreported: refused by the fences
        committed, resume = _pause_after(env, "lifecycle_authority_audit", lambda *a, **k: k.get("result") == "refused")
        task = asyncio.create_task(env.server._on_close(_close(manager, "fixture")))
        await committed.wait()
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        resume.set()
        with pytest.raises((asyncio.CancelledError, VerbError)):
            await task
        assert (await env.store.fetch_session(HOST, "fixture"))["status"] == "open"
        assert env.tmux.killed == []
        rows = [r for r in await env.audit() if r["action"] == "manager_close"]
        assert [(r["result"], r["refusal_code"]) for r in rows] == [("refused", "lifecycle_report_required")]

    scenario(check)


# -- reparent requires ownership: naming yourself the new parent grants nothing

def test_unrelated_seat_cannot_self_adopt(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        await env.open("helper", role="assistant")
        await env.open("loose", role="worker")
        await env.open("other", role="lead")
        await env.open("kid", role="worker", parent_stream_id="node-a:other")
        await env.open("seat", role="lead")
        seat = await env.seat("seat")
        for worker, parent in (("helper", None), ("loose", None), ("kid", "node-a:other")):
            await _refused(env.server._on_reparent({
                "stream_id": f"{HOST}:{worker}", "new_parent_stream_id": "node-a:seat", "reason": "adopt",
                "request_id": f"rp-{worker}", "_auth_context": seat}), "reparent_unauthorized")
            assert (await env.store.fetch_session(HOST, worker)).get("parent_stream_id") == parent
        rows = [(r["actor_kind"], r["actor_identity"], r["actor_generation"], r["result"], r["refusal_code"],
                 r["target_stream_id"]) for r in await env.audit() if r["action"] == "manager_reparent"]
        assert rows == [("seat", "node-a:seat", seat["session_generation"], "refused", "reparent_unauthorized",
                         f"node-a:{w}") for w in ("helper", "loose", "kid")]

    scenario(check)


def test_owner_and_successor_reparent_still_work(monkeypatch):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")

    async def check(env: Env):
        await env.open("other", role="lead")
        await env.open("seat", role="lead")
        await env.open("kid", role="worker", parent_stream_id="node-a:other")
        await env.open("kid2", role="worker", parent_stream_id="node-a:other")
        await env.open("succ", role="lead", handoff_from_stream_id="node-a:other")
        # The current parent hands its child to another stream.
        reply = await env.server._on_reparent({
            "stream_id": f"{HOST}:kid", "new_parent_stream_id": "node-a:seat", "reason": "handover",
            "request_id": "rp-own", "_auth_context": await env.seat("other")})
        assert reply["old_parent_stream_id"] == "node-a:other"
        assert (await env.store.fetch_session(HOST, "kid"))["parent_stream_id"] == "node-a:seat"
        # A live handoff successor adopts its predecessor's child.
        reply = await env.server._on_reparent({
            "stream_id": f"{HOST}:kid2", "new_parent_stream_id": "node-a:succ", "reason": "adopt",
            "request_id": "rp-succ", "_auth_context": await env.seat("succ")})
        assert (await env.store.fetch_session(HOST, "kid2"))["parent_stream_id"] == "node-a:succ"

    scenario(check)


@pytest.mark.parametrize("action", ["close", "reparent"])
def test_manager_action_cancelled_inside_authority_lock_finishes_then_cancels(monkeypatch, action):
    """Once the completion task starts (authority lock held) the action reaches
    an outcome: a cancel inside it still ends with the effect and applied audit."""
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="lead")
        await env.open("fixture", role="worker")
        await env.report("fixture")
        if action == "close":
            # Held past admission: the close waits on the inner graph lock.
            graph = env.sessions._graph_lock
            await graph.acquire()
            task = asyncio.create_task(env.server._on_close(_close(manager, "fixture")))
            await _until(lambda: env.sessions.assistant.authority_lock.locked())
            assert not task.done() and env.sessions.assistant.authority_lock.locked()
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            graph.release()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            # The reparent's ordinary attempt holds the graph lock before the
            # authority lock, so cancel once admission is recorded instead.
            committed, resume = _pause_after(
                env, "lifecycle_authority_audit", lambda *a, **k: k.get("result") == "admitted")
            task = asyncio.create_task(env.server._on_reparent({
                "stream_id": f"{HOST}:fixture", "new_parent_stream_id": f"{HOST}:a", "reason": "adopt orphan",
                "request_id": "rp-inner", "_auth_context": manager}))
            await _cancel_after_commit(task, committed, resume)
        row = await env.store.fetch_session(HOST, "fixture")
        if action == "close":
            assert row["status"] == "closed" and env.tmux.killed == ["fixture"]
        else:
            assert row["parent_stream_id"] == "node-a:a"
        rows = [r["result"] for r in await env.audit() if r["action"] == f"manager_{action}"]
        assert rows == ["admitted", "applied"]
        assert not env.sessions.assistant.authority_lock.locked()

    scenario(check)


def test_manager_reparent_cancelled_waiting_for_lifecycle_lock_has_no_effect(monkeypatch):
    monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)

    async def check(env: Env):
        manager = await _manager(env)
        await env.open("a", role="lead")
        await env.open("fixture", role="worker")
        lock = env.sessions._lifecycle_lock(HOST, "fixture")
        await lock.acquire()
        task = asyncio.create_task(env.server._on_reparent({
            "stream_id": f"{HOST}:fixture", "new_parent_stream_id": f"{HOST}:a", "reason": "adopt orphan",
            "request_id": "rp-lc", "_auth_context": manager}))
        await _until(lambda: env.sessions.assistant.authority_lock.locked())
        assert not task.done() and env.sessions.assistant.authority_lock.locked()
        task.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not (await env.store.fetch_session(HOST, "fixture")).get("parent_stream_id")
        assert not [r for r in await env.audit() if r["action"] == "manager_reparent"]
        assert not env.sessions.assistant.authority_lock.locked()

    scenario(check)
