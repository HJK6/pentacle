"""Bound-seat restore: death proof, durable episode, attempt resolution, bind
compare-and-set, operator verb and status (assistant_restore.py)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from types import SimpleNamespace

import pytest

import assistant_restore as ar
import store_assistant_binding
import store_lifecycle_authority as lifecycle_authority
from assistant_composite import AssistantComposite, AssistantCompositeConfig
from assistant_restore import AssistantRestore
from server import Server
from sessions import Sessions, VerbError
from store import Store

LOCAL = "fixture-host"
SEAT = f"{LOCAL}:fd"
OTHER = f"{LOCAL}:other"
CHAT = "fixture-chat:assistant"
G0 = "0" * 31 + "a"
CLAUDE_ID = "11111111-2222-4333-8444-555555555555"
PID = "4242"
STARTED = "Thu Oct  8 01:47:01 2026"
PS_GONE = (1, "", "")
PS_SAME = (0, f"{STARTED}\n", "")
PS_REUSED = (0, "Thu Oct  8 09:00:00 2026\n", "")


class FakeTmux:
    def __init__(self) -> None:
        self.states: dict[str, object] = {}

    async def session_state(self, name: str) -> str:
        value = self.states.get(name, "gone")
        if isinstance(value, Exception):
            raise value
        return str(value)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeSpawnctl:
    """Scripted stand-in for SpawnCtl's resume spawn and its key lookup."""

    def __init__(self, store: Store, sessions: Sessions, tmux: FakeTmux) -> None:
        self.store, self.sessions, self.tmux = store, sessions, tmux
        self.mode = "ok"
        self.row_overrides: dict = {}
        self.sent: list[dict] = []
        self.outcomes: list[dict] = []
        self.reservations: list[dict] = []

    async def spawn(self, msg, host):
        self.sent.append(dict(msg))
        key = msg["idempotency_key"]
        generation = ar.service_session_generation(msg)
        assert generation, "restore spawn must carry its own generation"
        if self.mode == "error":
            self.outcomes.append({"idempotency_key": key, "state": "failed", "reason": "boot_not_ready"})
            raise VerbError("boot_not_ready", "stub")
        if self.mode == "lost":
            self.reservations.append({"idempotency_key": key})
            return {"type": "spawn.ok", "state": "starting"}
        await self.store.open_session(LOCAL, "fd", **{
            "provider": "claude", "role": "lead", "visibility": "hidden",
            "pane_status": "pane_alive", "effective_model": "claude-opus-5-5", "effective_effort": "high",
            "claude_session_id": msg["resume_session_id"], "session_generation": generation,
            "bootstrap_state": "ready" if self.mode == "ok" else "starting", **self.row_overrides,
        })
        if self.mode == "rollback":
            await self.sessions.mark_closed(
                LOCAL, "fd", reason="boot_not_ready", expected_generation=generation,
                close_kind="spawn_rollback")
            self.outcomes.append({"idempotency_key": key, "state": "failed", "reason": "boot_not_ready"})
            raise VerbError("boot_not_ready", "stub")
        self.tmux.states["fd"] = "alive"
        self.outcomes.append({"idempotency_key": key, "state": "delivered"})
        return {"type": "spawn.ok", "stream_id": SEAT, "session": {"session_generation": generation}}

    async def spawn_status(self, msg, host):
        return {"type": "spawn_status.ok", "outcomes": list(self.outcomes),
                "reservations": list(self.reservations)}


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, auto=True, role=None) -> None:
        if role:
            monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", role)
        else:
            monkeypatch.delenv("PENTACLE_ASSISTANT_ROLE", raising=False)
        monkeypatch.delenv("PENTACLE_ASSISTANT_DAFF_ROLE", raising=False)
        self.tmp_path, self.auto = tmp_path, auto
        self.store = Store(str(tmp_path / "sessions.db"))
        self.tmux = FakeTmux()
        self.clock = Clock()
        # The store stamps a new episode's next_attempt_at; it must read the
        # same clock as the owner, or the episode is never due once wall time
        # passes the fixture instant.
        monkeypatch.setattr(store_assistant_binding, "_stamp", lambda: ar._iso(self.clock()))
        self.ps: object = PS_GONE
        self.ps_calls: list[str] = []
        self.broadcasts: list[dict] = []
        self.notices: list[tuple[str, str]] = []
        self.flushes = 0
        self.inhibit = tmp_path / ar.INHIBIT_FILE_NAME

    async def start(self, *, provider="claude", claude_id=CLAUDE_ID, host=LOCAL, observer=True):
        self.store.start()
        seat = f"{host}:fd"
        await self.store.open_session(
            host, "fd", provider=provider, role="lead", visibility="hidden",
            pane_status="pane_alive", effective_model="claude-opus-5-5", effective_effort="high",
            claude_session_id=claude_id, session_generation=G0, bootstrap_state="ready",
            **({"observer_binding": {"executable": "claude", "pane_pid": PID,
                                     "pane_started_at": STARTED, "generation": G0}} if observer else {}),
        )
        self.sessions = Sessions(self.store, tmux=self.tmux, local_host=LOCAL)
        self.composite = AssistantComposite(self.store, config=AssistantCompositeConfig.from_env({
            "PENTACLE_ASSISTANT_COMPOSITE_ENABLED": "1",
            "PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID": CHAT,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID": seat,
            "PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION": G0,
        }), broadcast=self._composite_broadcast)
        await self.composite.load_binding()
        await self.composite.ensure_projection()
        self.spawnctl = FakeSpawnctl(self.store, self.sessions, self.tmux)
        self.restore = self.owner()
        return self

    def owner(self) -> AssistantRestore:
        return AssistantRestore(
            store=self.store, sessions=self.sessions, spawnctl=self.spawnctl,
            composite=self.composite, local_host=LOCAL, auto_enabled=self.auto,
            inhibit_path=self.inhibit, flush_composite_tells=self._flush,
            deliver_notice=self._notice, broadcast=self._broadcast, ps_runner=self._ps,
            clock=self.clock,
        )

    async def _composite_broadcast(self, frame):
        return None

    async def _broadcast(self, frame):
        self.broadcasts.append(frame)

    async def _flush(self, composite):
        self.flushes += 1
        return 0

    async def _notice(self, composite, text, tell_id):
        if tell_id not in [t for t, _ in self.notices]:
            self.notices.append((tell_id, text))

    async def _ps(self, pid):
        self.ps_calls.append(pid)
        if isinstance(self.ps, Exception):
            raise self.ps
        return self.ps

    async def kill(self, *, reconciled=True):
        self.tmux.states["fd"] = "gone"
        if reconciled:
            await self.sessions.mark_reconciled_dead(
                LOCAL, "fd", presumed_dead_at="2026-10-08T11:58:00Z",
                closed_at="2026-10-08T11:59:00Z", expected_generation=G0)

    async def episodes(self):
        return await self.store.submit(lambda c: [dict(r) for r in c.execute(
            "SELECT * FROM v2_assistant_restore_episode ORDER BY episode_id")])

    async def audit(self, event=None):
        rows = await self.store.restore_audit_rows()
        return [r for r in rows if event is None or r["event"] == event]

    async def rebind_audit(self):
        return await self.store.submit(lambda c: [dict(r) for r in c.execute(
            "SELECT actor_stream_id,actor_generation,outcome,request_id FROM v2_assistant_rebind_audit "
            "ORDER BY audit_id")])

    async def move_binding(self, stream_id, generation, revision):
        def _op(conn):
            conn.execute(
                "INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at) "
                "VALUES(?,?,?,?,'t') ON CONFLICT(name) DO UPDATE SET stream_id=excluded.stream_id,"
                "generation=excluded.generation,revision=excluded.revision", (self.restore.name, stream_id, generation, revision))
            conn.commit()
        await self.store.submit(_op)
        await self.composite.load_binding()


def run(tmp_path, monkeypatch, body, **rig_kwargs):
    async def _go():
        rig = Rig(tmp_path, monkeypatch, **rig_kwargs)
        try:
            await body(rig)
        finally:
            rig.store.stop()
    asyncio.run(_go())


# -- pure helpers ------------------------------------------------------------- #


@pytest.mark.parametrize("value,expected", [("1", True), ("", False), ("0", False), (None, False)])
def test_flag_parsing(value, expected):
    env = {} if value is None else {ar.AUTO_RESTORE_ENV: value}
    assert ar.parse_auto_restore_flag(env) is expected


@pytest.mark.parametrize("value", ["true", "yes", "2", "on"])
def test_flag_rejects_other_values(value):
    with pytest.raises(ValueError):
        ar.parse_auto_restore_flag({ar.AUTO_RESTORE_ENV: value})


@pytest.mark.parametrize("rc,out,err,expected", [
    (1, "", "", "gone"),
    (0, f"{STARTED}\n", "", "alive"),
    (0, "Thu Oct  8 09:00:00 2026\n", "", "gone"),       # pid reused by another process
    (0, "Thu Oct 8 01:47:01 2026", "", "alive"),          # whitespace-normalized equality
    (2, "", "", "unknown"),
    (1, "", "ps: illegal option", "unknown"),
    (1, "noise", "", "unknown"),
    (1, " ", "", "unknown"),                              # whitespace is output, not "no such pid"
    (1, "\n", "", "unknown"),
    (1, "", " \n", "unknown"),
    (0, "", "", "unknown"),
    (0, "not a date\n", "", "unknown"),
    (0, f"{STARTED}\n{STARTED}\n", "", "unknown"),
    (0, f"{STARTED}\n", "warning", "unknown"),
    (0, "Thu Oct  8 09:00:00 2026\n", " \n", "unknown"),    # reused pid but whitespace on stderr
    (0, "Thu Oct  8 09:00:00 2026\n", " ", "unknown"),
    (0, "\nThu Oct  8 09:00:00 2026\n", "", "unknown"),     # blank line beside the start line
    (0, "Thu Oct  8 09:00:00 2026\n\n", "", "unknown"),
    (0, " \n", "", "unknown"),
])
def test_classify_ps(rc, out, err, expected):
    assert ar.classify_ps(rc, out, err, STARTED) == expected


def test_generation_override_only_for_the_daemon_scheduler():
    gen = "ab" * 16
    service = {"service_authenticated": True, "service_actor": "daemon:scheduler"}
    assert ar.service_session_generation({"_auth_context": service, "_session_generation": gen}) == gen
    for auth in (
        {"operator_authenticated": True},
        {"token_verified": True, "stream_id": SEAT},
        {"service_authenticated": True, "service_actor": "daemon:other"},
        {"service_authenticated": "yes", "service_actor": "daemon:scheduler"},
        None,
    ):
        assert ar.service_session_generation({"_auth_context": auth, "_session_generation": gen}) == ""
    for bad in ("", "xyz", gen.upper(), gen + "0", None, 5):
        assert ar.service_session_generation({"_auth_context": service, "_session_generation": bad}) == ""


# -- death proof (the T2 status table) ----------------------------------------- #


def test_live_holder_is_healthy_and_never_spawns(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "alive"
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("healthy", None)
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("pane,ps,state,reason", [
    ("unreachable", PS_GONE, "waiting_evidence", "pane_probe_unknown"),
    (RuntimeError("tmux timeout"), PS_GONE, "waiting_evidence", "pane_probe_unknown"),
    ("gone", PS_SAME, "waiting_evidence", "holder_process_alive"),
    ("gone", (2, "", ""), "waiting_evidence", "process_probe_unknown"),
    ("gone", (1, "", "ps: boom"), "waiting_evidence", "process_probe_unknown"),
    ("gone", (0, "garbage\n", ""), "waiting_evidence", "process_probe_unknown"),
    ("gone", asyncio.TimeoutError(), "waiting_evidence", "process_probe_unknown"),
    ("gone", FileNotFoundError("ps"), "waiting_evidence", "process_probe_unknown"),
    ("gone", PermissionError("ps"), "waiting_evidence", "process_probe_unknown"),
])
def test_unknown_or_alive_evidence_never_creates_an_episode(tmp_path, monkeypatch, pane, ps, state, reason):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.tmux.states["fd"] = pane
        rig.ps = ps
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == (state, reason)
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body)


def test_closed_row_with_a_revived_pane_is_ineligible(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.tmux.states["fd"] = "alive"   # stale close evidence, pane is back
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("ineligible", "holder_revived")
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body)


def test_missing_pane_identity_is_unknown(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start(observer=False)
        await rig.kill()
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("waiting_evidence", "pane_identity_unrecorded")
        assert rig.ps_calls == [] and await rig.episodes() == []
    run(tmp_path, monkeypatch, body)


def test_other_generation_pane_identity_is_unknown(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.store.update_session(LOCAL, "fd", observer_binding={
            "executable": "claude", "pane_pid": PID, "pane_started_at": STARTED, "generation": "f" * 32})
        await rig.kill()
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("waiting_evidence", "pane_identity_unrecorded")
    run(tmp_path, monkeypatch, body)


def test_intentional_close_is_ineligible(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "gone"
        await rig.sessions.mark_closed(LOCAL, "fd", reason="operator", expected_generation=G0)
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("ineligible", "intentional_close")
        assert await rig.episodes() == []
    run(tmp_path, monkeypatch, body)


def test_whitespace_close_kind_is_not_a_reconciler_close(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()                              # reconciler-closed row, restorable as is
        await rig.store.update_session(LOCAL, "fd", close_kind=" ")
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("ineligible", "intentional_close")
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body)


def test_protected_holder_is_refused(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "gone"
        proven, state, reason, _row = await rig.restore.death_proof(SEAT, G0)
        assert (proven, state, reason) == (False, "ineligible", "protected_row")
        await rig.restore.advance()
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body, role="lead")


def test_non_claude_and_unknown_and_remote_holders_are_refused(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start(provider="codex")
        assert (await rig.restore.death_proof(SEAT, G0))[1:3] == ("ineligible", "not_resumable")
        assert (await rig.restore.death_proof(f"{LOCAL}:missing", G0))[1:3] == ("ineligible", "holder_unknown")
        assert (await rig.restore.death_proof("elsewhere:fd", G0))[1:3] == ("ineligible", "remote_holder")
        await rig.kill()
        await rig.restore.advance()
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body)


def test_generation_mismatch_is_refused(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        assert (await rig.restore.death_proof(SEAT, "9" * 32))[1:3] == ("ineligible", "generation_mismatch")
    run(tmp_path, monkeypatch, body)


def test_seat_without_a_claude_session_is_not_resumable(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start(claude_id=None)
        assert (await rig.restore.death_proof(SEAT, G0))[1:3] == ("ineligible", "not_resumable")
    run(tmp_path, monkeypatch, body)


def test_router_backed_composite_is_not_direct_primary(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.composite.config = SimpleNamespace(enabled=True, direct_primary=False, name=rig.restore.name, stream_id=CHAT)
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("ineligible", "not_direct_primary")
        assert await rig.episodes() == []
    run(tmp_path, monkeypatch, body)


# -- the restore journey ------------------------------------------------------- #


def test_dead_seat_is_resumed_and_rebound_once(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        before = await rig.composite.binding()
        status = await rig.restore.advance()
        assert status["state"] == "restored" and status["reason"] is None
        episode = (await rig.episodes())[0]
        after = await rig.composite.binding()
        assert after["stream_id"] == SEAT and after["revision"] == before["revision"] + 1
        assert after["generation"] == episode["attempt_generation"] == episode["last_generation"] != G0
        sent = rig.spawnctl.sent
        assert len(sent) == 1 and sent[0]["resume_session_id"] == CLAUDE_ID
        assert sent[0]["idempotency_key"] == sent[0]["request_id"] == episode["spawn_key"] \
            == f"assistant-restore:{episode['episode_id']}:1"
        assert sent[0]["_session_generation"] == episode["attempt_generation"]
        assert not {"role", "parent_stream_id", "handoff", "session_name", "initial_prompt"} & set(sent[0])
        row = await rig.store.fetch_session(LOCAL, "fd")
        assert row["status"] == "open" and row["claude_session_id"] == CLAUDE_ID
        assert [r["event"] for r in await rig.audit()] == [
            "episode_created", "attempt_started", "attempt_outcome", "bound", "routing_completed"]
        assert await rig.rebind_audit() == [{
            "actor_stream_id": "daemon:assistant-restore", "actor_generation": G0, "outcome": "ok",
            "request_id": f"assistant-restore:{episode['episode_id']}"}]
        assert rig.notices == [(f"assistant-restore-notice:{episode['episode_id']}", rig.notices[0][1])]
        assert f"binding revision {before['revision'] + 1}" in rig.notices[0][1]
        assert rig.flushes == 1
        # A later pass on the restored seat does nothing more.
        await rig.restore.advance()
        assert len(rig.spawnctl.sent) == 1 and len(await rig.episodes()) == 1
    run(tmp_path, monkeypatch, body)


def test_open_row_with_a_dead_pane_is_restored_without_the_reconciler(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill(reconciled=False)   # no tmux server: the reconciler never closes the row
        # The real resume path closes the open prior row itself; the fake reopens it.
        status = await rig.restore.advance()
        assert status["state"] == "restored"
    run(tmp_path, monkeypatch, body)


def test_pid_reuse_counts_as_gone(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.ps = PS_REUSED
        assert (await rig.restore.advance())["state"] == "restored"
    run(tmp_path, monkeypatch, body)


def test_flag_off_creates_nothing_and_reports_disabled(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        await rig.restore.tick()
        await rig.restore.drain()
        assert rig.restore._task is None      # one read, no background pass
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("disabled", None)
        assert await rig.episodes() == [] and rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body, auto=False)


# -- crash points -------------------------------------------------------------- #


def test_crash_before_spawn_sends_the_recorded_key_once(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("before_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        episode = (await rig.episodes())[0]
        assert episode["state"] == "spawning" and rig.spawnctl.sent == []
        restarted = rig.owner()
        assert (await restarted.advance())["state"] == "restored"
        assert [m["idempotency_key"] for m in rig.spawnctl.sent] == [episode["spawn_key"]]
        assert rig.spawnctl.sent[0]["_session_generation"] == episode["attempt_generation"]
        assert (await rig.episodes())[0]["attempt_seq"] == 1
        assert len(await rig.audit("attempt_started")) == 1
    run(tmp_path, monkeypatch, body)


def test_crash_after_spawn_does_not_resend(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("after_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        assert len(rig.spawnctl.sent) == 1 and (await rig.episodes())[0]["state"] == "spawning"
        restarted = rig.owner()
        assert (await restarted.advance())["state"] == "restored"
        assert len(rig.spawnctl.sent) == 1          # resolved from the row, never re-sent
        episode = (await rig.episodes())[0]
        assert (await rig.composite.binding())["generation"] == episode["attempt_generation"]
        assert len(await rig.audit("bound")) == 1
    run(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("point", ["after_bind", "during_routing"])
def test_crash_after_bind_completes_routing_once(tmp_path, monkeypatch, point):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at(point)
        with pytest.raises(_Crash):
            await rig.restore.advance()
        assert (await rig.episodes())[0]["state"] == "bound"
        restarted = rig.owner()
        assert (await restarted.advance())["state"] == "restored"
        assert len(rig.spawnctl.sent) == 1 and len(rig.notices) == 1
        assert len(await rig.audit("bound")) == 1 and len(await rig.rebind_audit()) == 1
        assert (await rig.composite.binding())["revision"] == 1
    run(tmp_path, monkeypatch, body)


class _Crash(BaseException):
    pass


def _stop_at(point):
    async def fault(reached):
        if reached == point:
            raise _Crash()
    return fault


# -- attempt resolution (T4.2) ------------------------------------------------- #


def test_unresolved_attempt_never_starts_another_and_ends_degraded(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.spawnctl.mode = "lost"                    # admitted, outcome unknown
        assert (await rig.restore.advance())["state"] == "spawning"
        for _ in range(3):
            rig.clock.advance(120)
            assert (await rig.restore.advance())["state"] == "spawning"
        episode = (await rig.episodes())[0]
        assert episode["attempt_seq"] == 1 and len(rig.spawnctl.sent) == 1
        outcomes = await rig.audit("attempt_outcome")
        assert [r["outcome"] for r in outcomes] == ["uncertain"]
        rig.clock.advance(239)                        # 599 s: still inside the bound
        assert (await rig.restore.advance())["state"] == "spawning"
        rig.clock.advance(1)                          # exactly the 600 s bound
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("degraded", "attempt_unresolved")
        assert (await rig.episodes())[0]["attempt_seq"] == 1 and len(rig.spawnctl.sent) == 1
        # A retry never starts an attempt on top of the unresolved one.
        refused = {"error_code": "assistant_restore_attempt_unresolved"}
        assert await rig.restore.manual("retry", "retry-held") == refused
        rig.spawnctl.reservations.clear()             # released, but no outcome recorded yet
        assert await rig.restore.manual("retry", "retry-no-outcome") == refused
        await rig.restore.advance()
        episode = (await rig.episodes())[0]
        assert episode["state"] == "degraded" and episode["attempt_seq"] == 1
        assert len(rig.spawnctl.sent) == 1 and await rig.audit("budget_reset") == []
        rig.spawnctl.outcomes.append({"idempotency_key": episode["spawn_key"], "state": "failed",
                                      "reason": "boot_not_ready"})
        rig.spawnctl.mode = "ok"                      # the request ended failed: retry is allowed
        assert (await rig.restore.manual("retry", "retry-resolved"))["duplicate"] is False
        await rig.restore.drain()
        episode = (await rig.episodes())[0]
        assert episode["state"] == "restored" and episode["attempt_seq"] == 2
        assert len(rig.spawnctl.sent) == 2
    run(tmp_path, monkeypatch, body)


def test_row_open_but_not_ready_is_unresolved(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.spawnctl.mode = "starting"                # row opened, bootstrap not ready
        assert (await rig.restore.advance())["state"] == "spawning"
        assert (await rig.composite.binding())["generation"] == G0
        await rig.store.update_session(LOCAL, "fd", bootstrap_state="ready")
        assert (await rig.restore.advance())["state"] == "restored"
        assert len(rig.spawnctl.sent) == 1
    run(tmp_path, monkeypatch, body)


def test_failed_attempts_back_off_and_stop_at_four_across_restarts(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.spawnctl.mode = "rollback"                # opens the row, then rolls the boot back
        generations = []
        for attempt, delay in enumerate((30, 120, 600, None), start=1):
            owner = rig.owner()                       # a new daemon instance every attempt
            status = await owner.advance()
            episode = (await rig.episodes())[0]
            assert episode["attempt_seq"] == attempt == episode["budget_used"]
            assert episode["last_generation"] == episode["attempt_generation"]
            generations.append(episode["attempt_generation"])
            row = await rig.store.fetch_session(LOCAL, "fd")
            assert row["close_kind"] == "spawn_rollback"
            if delay is None:
                assert (status["state"], status["reason"]) == ("degraded", "boot_not_ready")
                break
            assert status["state"] == "pending" and status["reason"] == "boot_not_ready"
            due = datetime.fromisoformat(episode["next_attempt_at"].replace("Z", "+00:00"))
            assert (due - rig.clock.now).total_seconds() == delay
            rig.clock.advance(delay - 1)
            await rig.owner().advance()               # not due yet: nothing starts
            assert (await rig.episodes())[0]["attempt_seq"] == attempt
            rig.clock.advance(2)
        assert len(set(generations)) == 4 and len(rig.spawnctl.sent) == 4
        assert len(await rig.audit("attempt_started")) == 4
        assert [r["outcome"] for r in await rig.audit("exhausted")] == ["boot_not_ready"]
        for _ in range(2):                            # further restarts never add a fifth
            rig.clock.advance(3600)
            assert (await rig.owner().advance())["state"] == "degraded"
        assert len(rig.spawnctl.sent) == 4
        assert (await rig.composite.binding())["generation"] == G0
    run(tmp_path, monkeypatch, body)


def test_spawn_refused_before_a_row_opens_fails_the_attempt(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.spawnctl.mode = "error"
        status = await rig.restore.advance()
        assert (status["state"], status["reason"]) == ("pending", "boot_not_ready")
        episode = (await rig.episodes())[0]
        assert episode["last_generation"] == G0      # no row was opened by the attempt
    run(tmp_path, monkeypatch, body)


def test_rollback_provenance_is_only_this_episodes_own_generation(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "gone"
        await rig.sessions.mark_closed(
            LOCAL, "fd", reason="boot_not_ready", expected_generation=G0, close_kind="spawn_rollback")
        # A rollback of the ORIGINAL generation is not this restore's own failed boot.
        assert (await rig.restore.death_proof(SEAT, G0))[1:3] == ("ineligible", "intentional_close")
        foreign = {"generation": G0, "last_generation": "c" * 32, "spawn_key": "k"}
        assert (await rig.restore.death_proof(SEAT, G0, foreign))[1:3] == ("ineligible", "intentional_close")
    run(tmp_path, monkeypatch, body)


def test_seat_resumed_by_someone_else_supersedes(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("before_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        await rig.store.open_session(
            LOCAL, "fd", provider="claude", role="lead", pane_status="pane_alive",
            claude_session_id=CLAUDE_ID, session_generation="e" * 32, bootstrap_state="ready")
        rig.tmux.states["fd"] = "alive"
        status = await rig.owner().advance()
        assert rig.spawnctl.sent == []
        episode = (await rig.episodes())[0]
        assert (episode["state"], episode["reason"]) == ("superseded", "holder_resumed_externally")
        assert (await rig.composite.binding())["generation"] == G0
        assert status["state"] == "superseded"
    run(tmp_path, monkeypatch, body)


# -- bind compare-and-set ------------------------------------------------------- #


def test_operator_rebind_before_the_bind_wins_and_the_seat_stays_open(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        other = await rig.store.open_session(
            LOCAL, "other", provider="claude", role="lead", pane_status="pane_alive",
            effective_model="claude-opus-5-5", effective_effort="high")
        rig.tmux.states["other"] = "alive"
        rig.restore._fault = _stop_at("after_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        await rig.move_binding(OTHER, other["session_generation"], 1)
        await rig.owner().advance()
        episode = (await rig.episodes())[0]
        assert (episode["state"], episode["reason"]) == ("superseded", "binding_moved")
        binding = await rig.composite.binding()
        assert (binding["stream_id"], binding["revision"]) == (OTHER, 1)
        assert [r["outcome"] for r in await rig.rebind_audit()] == ["assistant_restore_superseded"]
        resumed = await rig.store.fetch_session(LOCAL, "fd")
        assert resumed["status"] == "open"            # left open and unbound, like any hot rebind
        assert rig.notices == []
    run(tmp_path, monkeypatch, body)


def test_operator_rebind_before_the_spawn_supersedes_without_a_spawn(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.spawnctl.mode = "error"
        await rig.restore.advance()                   # attempt 1 fails, episode pending
        other = await rig.store.open_session(
            LOCAL, "other", provider="claude", role="lead", pane_status="pane_alive")
        await rig.move_binding(OTHER, other["session_generation"], 1)
        rig.clock.advance(31)
        await rig.restore.advance()
        episode = (await rig.episodes())[0]
        assert (episode["state"], episode["reason"]) == ("superseded", "binding_moved")
        assert len(rig.spawnctl.sent) == 1
    run(tmp_path, monkeypatch, body)


def test_operator_rebind_after_the_bind_is_an_ordinary_rebind(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        other = await rig.store.open_session(
            LOCAL, "other", provider="claude", role="lead", pane_status="pane_alive")
        rig.restore._fault = _stop_at("after_bind")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        await rig.move_binding(OTHER, other["session_generation"], 2)
        await rig.owner().advance()
        episode = (await rig.episodes())[0]
        assert episode["state"] == "restored"
        binding = await rig.composite.binding()
        assert (binding["stream_id"], binding["revision"]) == (OTHER, 2)
        assert (await rig.store.fetch_session(LOCAL, "fd"))["status"] == "open"
        assert len(rig.notices) == 1
    run(tmp_path, monkeypatch, body)


def test_bind_replay_is_a_duplicate_and_a_lost_seat_fails_the_attempt(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("after_bind")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        episode = (await rig.episodes())[0]
        again = await rig.store.restore_assistant_binding(
            episode_id=episode["episode_id"], env_binding=rig.composite._env_binding())
        assert again["outcome"] == "duplicate" and again["duplicate"] is True
        assert (await rig.composite.binding())["revision"] == 1 and len(await rig.rebind_audit()) == 1
    run(tmp_path, monkeypatch, body)

    async def lost(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("after_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        episode = (await rig.episodes())[0]
        await rig.store.restore_transition(
            episode_id=episode["episode_id"], expect_state="spawning", expect_attempt_seq=1,
            fields={"state": "spawned", "last_generation": episode["attempt_generation"]}, audits=[])
        await rig.store.update_session(LOCAL, "fd", pane_status="pane_dead")
        result = await rig.store.restore_assistant_binding(
            episode_id=episode["episode_id"], env_binding=rig.composite._env_binding())
        assert result["outcome"] == "holder_lost"
        assert (await rig.composite.binding())["generation"] == G0
        assert await rig.rebind_audit() == []
    (tmp_path / "lost").mkdir()
    run(tmp_path / "lost", monkeypatch, lost)


def test_transition_cas_and_audit_are_atomic(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("before_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        episode = (await rig.episodes())[0]
        rows = len(await rig.audit())
        # A stale observer loses and writes nothing.
        assert await rig.store.restore_transition(
            episode_id=episode["episode_id"], expect_state="pending", expect_attempt_seq=0,
            fields={"state": "degraded"}, audits=[("exhausted", "x", {})]) is None
        assert await rig.store.restore_transition(
            episode_id=episode["episode_id"], expect_state="spawning", expect_attempt_seq=7,
            fields={"state": "degraded"}, audits=[("exhausted", "x", {})]) is None
        # An audit row that cannot be written rolls the transition back.
        with pytest.raises(TypeError):
            await rig.store.restore_transition(
                episode_id=episode["episode_id"], expect_state="spawning", expect_attempt_seq=1,
                fields={"state": "degraded"}, audits=[("exhausted", "x", {"bad": object()})])
        assert (await rig.episodes())[0]["state"] == "spawning" and len(await rig.audit()) == rows
        with pytest.raises(ValueError):
            await rig.store.restore_transition(
                episode_id=episode["episode_id"], expect_state="spawning", expect_attempt_seq=1,
                fields={"name": "x"}, audits=[])
    run(tmp_path, monkeypatch, body)


# -- inhibit and the operator verb --------------------------------------------- #


def test_inhibit_file_blocks_creation_and_new_automatic_attempts(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.inhibit.write_text("maintenance")
        status = await rig.restore.advance()
        assert status["state"] == "suspended" and await rig.episodes() == []
        rig.inhibit.unlink()
        rig.spawnctl.mode = "error"
        assert (await rig.restore.advance())["state"] == "pending"
        rig.inhibit.write_text("maintenance")      # appears while the episode is pending
        rig.clock.advance(60)
        rig.spawnctl.mode = "ok"
        status = await rig.restore.advance()
        assert status["state"] == "suspended" and len(rig.spawnctl.sent) == 1
        rig.inhibit.unlink()
        assert (await rig.restore.advance())["state"] == "restored"
    run(tmp_path, monkeypatch, body)


def test_inhibit_placed_during_the_death_proof_blocks_creation(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        probe = rig._ps

        async def marker_appears_mid_probe(pid):
            rig.inhibit.write_text("maintenance")
            return await probe(pid)

        rig.restore._ps = marker_appears_mid_probe
        status = await rig.restore.advance()
        assert len(rig.ps_calls) == 1 and status["state"] == "suspended"
        assert await rig.episodes() == [] and await rig.audit() == []
        assert rig.spawnctl.sent == []
    run(tmp_path, monkeypatch, body)


def test_inhibit_does_not_stop_effects_already_admitted(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.restore._fault = _stop_at("after_spawn")
        with pytest.raises(_Crash):
            await rig.restore.advance()
        rig.inhibit.write_text("maintenance")
        assert (await rig.owner().advance())["state"] == "restored"
    run(tmp_path, monkeypatch, body)


def test_manual_restore_works_with_the_flag_off_and_is_idempotent(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.inhibit.write_text("maintenance")      # never applies to a manual episode
        first = await rig.restore.manual("restore", "req-1")
        second = await rig.restore.manual("restore", "req-2")
        await rig.restore.drain()
        assert first["duplicate"] is False and second["duplicate"] is False
        episodes = await rig.episodes()
        assert len(episodes) == 1 and episodes[0]["trigger"] == "manual"
        await rig.restore.tick()                   # the reconciler callback carries it on
        await rig.restore.drain()
        assert (await rig.restore.status())["state"] == "restored"
        assert len(rig.spawnctl.sent) == 1
        replay = await rig.restore.manual("restore", "req-1")
        assert replay["duplicate"] is True and len(await rig.episodes()) == 1
        assert len(await rig.audit("manual_request")) == 2
    run(tmp_path, monkeypatch, body, auto=False)


def test_manual_retry_opens_one_new_budget_epoch_and_keeps_history(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        rig.spawnctl.mode = "rollback"
        for _ in range(4):
            await rig.restore.advance()
            rig.clock.advance(601)
        assert (await rig.restore.status())["state"] == "degraded"
        rig.spawnctl.mode = "ok"
        first = await rig.restore.manual("retry", "retry-1")
        await rig.restore.drain()
        second = await rig.restore.manual("retry", "retry-1")
        await rig.restore.drain()
        assert first["duplicate"] is False and second["duplicate"] is True
        episode = (await rig.episodes())[0]
        assert episode["state"] == "restored" and episode["budget_epoch"] == 2
        assert episode["attempt_seq"] == 5 and episode["budget_used"] == 1
        assert rig.spawnctl.sent[-1]["idempotency_key"] == f"assistant-restore:{episode['episode_id']}:5"
        assert len(await rig.audit("budget_reset")) == 1
        assert len(await rig.audit("attempt_started")) == 5
        assert len(await rig.audit("exhausted")) == 1
    run(tmp_path, monkeypatch, body)


def test_manual_retry_is_refused_unless_degraded(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await rig.kill()
        assert (await rig.restore.manual("retry", "r-1")) == {"error_code": "assistant_restore_not_degraded"}
        await rig.restore.advance()
        assert (await rig.restore.manual("retry", "r-2")) == {"error_code": "assistant_restore_not_degraded"}
        assert await rig.audit("budget_reset") == []
    run(tmp_path, monkeypatch, body)


def test_verb_requires_operator_authority():
    class Stub:
        assistant_restore = object()

    async def call(auth, **extra):
        return await Server._on_assistant_restore(
            Stub(), {"_auth_context": auth, "request_id": "r", "action": "restore", **extra})

    for auth in ({}, {"token_verified": True, "stream_id": SEAT}, {"service_authenticated": True}):
        with pytest.raises(VerbError) as caught:
            asyncio.run(call(auth))
        assert caught.value.code == "assistant_restore_unauthorized"
    with pytest.raises(VerbError) as caught:
        asyncio.run(call({"operator_authenticated": True}, action="delete"))
    assert caught.value.code == "bad_request"


# -- status contract ------------------------------------------------------------ #

STATUS_KEYS = {"state", "reason", "episode_id", "trigger", "attempt_seq", "budget_used",
               "max_attempts", "next_attempt_at", "predecessor_generation", "updated_at"}


def test_status_object_shape_in_every_branch(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "alive"
        idle = await rig.restore.advance()
        assert set(idle) == STATUS_KEYS and idle["max_attempts"] == 4
        assert (idle["episode_id"], idle["trigger"], idle["next_attempt_at"],
                idle["predecessor_generation"], idle["reason"]) == (None, None, None, None, None)
        assert idle["attempt_seq"] == 0 and idle["budget_used"] == 0 and isinstance(idle["updated_at"], str)
        await rig.kill()
        rig.spawnctl.mode = "error"
        pending = await rig.restore.advance()
        assert set(pending) == STATUS_KEYS and pending["state"] == "pending"
        assert isinstance(pending["episode_id"], int) and pending["trigger"] == "auto"
        assert pending["attempt_seq"] == 1 and pending["budget_used"] == 1
        assert pending["predecessor_generation"] == G0 and isinstance(pending["next_attempt_at"], str)
        rig.clock.advance(31)
        rig.spawnctl.mode = "ok"
        done = await rig.restore.advance()
        assert set(done) == STATUS_KEYS and done["state"] == "restored" and done["reason"] is None
        assert done["predecessor_generation"] == G0
    run(tmp_path, monkeypatch, body)


def test_each_change_emits_one_broadcast_and_one_warning(tmp_path, monkeypatch, caplog):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "alive"
        caplog.set_level(logging.WARNING, logger="chat_streamd_v2.assistant_restore")
        await rig.restore.advance()                 # baseline: the state found first is no change
        await rig.restore.advance()
        assert rig.broadcasts == [] and _warnings(caplog) == 0
        await rig.kill()
        rig.tmux.states["fd"] = "unreachable"
        await rig.restore.advance()
        assert [b["restore"]["state"] for b in rig.broadcasts] == ["waiting_evidence"]
        assert rig.broadcasts[0]["type"] == "assistant.restore.changed" and rig.broadcasts[0]["name"] == rig.restore.name
        assert set(rig.broadcasts[0]["restore"]) == STATUS_KEYS and _warnings(caplog) == 1
        await rig.restore.advance()                 # identical pass: nothing emitted
        await rig.restore.advance()
        assert len(rig.broadcasts) == 1 and _warnings(caplog) == 1
        rig.tmux.states["fd"] = "gone"
        await rig.restore.advance()                 # one pass, every persisted change emitted
        assert [b["restore"]["state"] for b in rig.broadcasts] == [
            "waiting_evidence", "pending", "spawning", "spawned", "bound", "restored"]
        assert _warnings(caplog) == 6
    run(tmp_path, monkeypatch, body)


def _warnings(caplog) -> int:
    return sum(1 for r in caplog.records
               if r.name == "chat_streamd_v2.assistant_restore" and r.levelno == logging.WARNING)


def test_binding_reply_carries_the_restore_status(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        rig.tmux.states["fd"] = "alive"

        class Stub:
            assistant_restore = rig.restore

            def _composite_for_message(self, msg):
                return rig.composite

        reply = await Server._on_assistant_binding(Stub(), {"_auth_context": {"operator_authenticated": True}})
        assert reply["type"] == "assistant.binding.ok" and set(reply["restore"]) == STATUS_KEYS
        other = Stub()
        other.assistant_restore = None
        assert "restore" not in await Server._on_assistant_binding(
            other, {"_auth_context": {"operator_authenticated": True}})
    run(tmp_path, monkeypatch, body)


# -- lifecycle-manager grant across an automatic restore ---------------------- #

CONSENT = "consent-fixture-1"


async def designate(rig, stream_id=SEAT, generation=G0, revision=5):
    """Seed an operator-designated grant with its consent-linked audit row."""
    def _op(conn):
        conn.execute(
            "INSERT INTO v2_lifecycle_manager(id,stream_id,session_generation,revision,updated_at) "
            "VALUES(1,?,?,?,0) ON CONFLICT(id) DO UPDATE SET stream_id=excluded.stream_id,"
            "session_generation=excluded.session_generation,revision=excluded.revision",
            (stream_id, generation, revision))
        if stream_id:
            lifecycle_authority.audit(
                conn, action="designate", actor_kind="operator", actor_identity="operator-fixture",
                target_stream_id=stream_id, target_generation=generation, old_revision=revision - 1,
                new_revision=revision, result="applied", consent_id=CONSENT)
        conn.commit()
    await rig.store.submit(_op)


async def grant(rig):
    return await rig.store.submit(lambda c: lifecycle_authority.current(c))


async def continuity_audit(rig):
    return await rig.store.submit(lambda c: [dict(r) for r in c.execute(
        "SELECT action,actor_kind,actor_identity,actor_generation,target_stream_id,target_generation,"
        "old_revision,new_revision,prior_stream_id,prior_generation,request_id,result,refusal_code,"
        "consent_id FROM v2_lifecycle_authority_audit WHERE action='restore_continuity' ORDER BY id")])


def test_automatic_restore_carries_the_grant_to_the_resumed_generation(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        new = episode["last_generation"]
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": new, "revision": 6}
        # The resumed generation holds manager authority; the dead one does not.
        assert await rig.store.lifecycle_authority_holder(SEAT, new) is True
        assert await rig.store.lifecycle_authority_holder(SEAT, G0) is False
        assert await continuity_audit(rig) == [{
            "action": "restore_continuity", "actor_kind": "daemon",
            "actor_identity": "daemon:assistant-restore", "actor_generation": G0,
            "target_stream_id": SEAT, "target_generation": new, "old_revision": 5, "new_revision": 6,
            "prior_stream_id": SEAT, "prior_generation": G0,
            "request_id": f"assistant-restore:{episode['episode_id']}", "result": "applied",
            "refusal_code": None, "consent_id": CONSENT}]
        assert (episode["grant_stream_id"], episode["grant_generation"], episode["grant_revision"]) == (SEAT, G0, 5)
        assert episode["grant_carry"] == "applied"
        bound = (await rig.audit("bound"))[0]
        assert json.loads(bound["detail_json"])["grant_carry"] == "applied"
        assert "grant moved with the seat (revision 6)" in rig.notices[0][1]
        # A later pass changes nothing.
        await rig.restore.advance()
        assert (await grant(rig))["revision"] == 6 and len(await continuity_audit(rig)) == 1
    run(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("holder", [None, OTHER])
def test_no_matching_grant_restores_chat_only(tmp_path, monkeypatch, holder):
    async def body(rig):
        await rig.start()
        if holder:
            await designate(rig, stream_id=holder, generation="9" * 32)
        before = await grant(rig)
        await rig.kill()
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        assert await grant(rig) == before
        assert episode["grant_stream_id"] is None and episode["grant_carry"] == "skipped:no_grant"
        assert await continuity_audit(rig) == []
        assert "grant is not restored" in rig.notices[0][1]
    run(tmp_path, monkeypatch, body)


@pytest.mark.parametrize("change,expected", [
    (dict(stream_id=None, generation=None, revision=6), {"stream_id": None, "session_generation": None, "revision": 6}),
    (dict(stream_id=OTHER, generation="9" * 32, revision=6), {"stream_id": OTHER, "session_generation": "9" * 32, "revision": 6}),
    (dict(stream_id=SEAT, generation=G0, revision=6), {"stream_id": SEAT, "session_generation": G0, "revision": 6}),
])
def test_grant_changed_during_the_episode_is_never_overwritten(tmp_path, monkeypatch, change, expected):
    """Revoked, replaced, or re-issued to the same holder while the seat was down."""
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        assert (await rig.restore._create("auto"))["state"] == "pending"
        await designate(rig, **change)
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        assert await grant(rig) == expected
        assert episode["grant_carry"] == "skipped:grant_changed"
        rows = await continuity_audit(rig)
        assert [(r["result"], r["refusal_code"], r["new_revision"]) for r in rows] == [("refused", "grant_changed", None)]
        assert await rig.store.lifecycle_authority_holder(SEAT, episode["last_generation"]) is False
        assert (await rig.composite.binding())["generation"] == episode["last_generation"]
        assert "grant is not restored" in rig.notices[0][1]
    run(tmp_path, monkeypatch, body)


def test_binding_moved_supersedes_and_leaves_the_grant(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        assert (await rig.restore._create("auto"))["state"] == "pending"
        await rig.store.open_session(LOCAL, "other", provider="claude", role="lead", pane_status="pane_alive",
                                     session_generation="9" * 32, bootstrap_state="ready")
        await rig.move_binding(OTHER, "9" * 32, 7)
        await rig.restore.advance()
        episode = (await rig.episodes())[0]
        assert (episode["state"], episode["reason"], episode["grant_carry"]) == ("superseded", "binding_moved", None)
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert await continuity_audit(rig) == []
    run(tmp_path, monkeypatch, body)


def test_operator_started_restore_does_not_carry(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        reply = await rig.restore.manual("restore", "req-manual-1")
        assert reply["restore"]["trigger"] == "manual"
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert episode["grant_stream_id"] is None and episode["grant_carry"] == "skipped:not_automatic"
        assert await continuity_audit(rig) == []
    run(tmp_path, monkeypatch, body, auto=False)


def test_retried_episode_does_not_carry(tmp_path, monkeypatch):
    """An operator retry turns the episode manual; authority then needs the operator."""
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        rig.spawnctl.mode = "rollback"
        for _ in range(4):
            await rig.restore.advance()
            rig.clock.advance(700)
        assert (await rig.restore.advance())["state"] == "degraded"
        rig.spawnctl.mode = "ok"
        await rig.restore.manual("retry", "req-retry-1")
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        assert episode["trigger"] == "manual" and episode["grant_carry"] == "skipped:not_automatic"
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
    run(tmp_path, monkeypatch, body)


def test_failed_attempts_then_success_carry_from_the_original_generation(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        rig.spawnctl.mode = "rollback"
        await rig.restore.advance()
        failed = (await rig.episodes())[0]
        assert failed["state"] == "pending" and failed["last_generation"] != G0
        # The failed boot never held the grant.
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        rig.spawnctl.mode = "ok"
        rig.clock.advance(60)
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        assert episode["attempt_seq"] == 2 and episode["last_generation"] not in {G0, failed["last_generation"]}
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": episode["last_generation"], "revision": 6}
        rows = await continuity_audit(rig)
        assert [(r["prior_generation"], r["target_generation"], r["result"]) for r in rows] == [
            (G0, episode["last_generation"], "applied")]
    run(tmp_path, monkeypatch, body)


def test_replay_after_a_later_revoke_does_not_reapply(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        await designate(rig, stream_id=None, generation=None, revision=7)
        again = await rig.store.restore_assistant_binding(
            episode_id=episode["episode_id"], env_binding=rig.composite._env_binding())
        assert again["outcome"] == "duplicate"
        assert await grant(rig) == {"stream_id": None, "session_generation": None, "revision": 7}
        assert len(await continuity_audit(rig)) == 1
    run(tmp_path, monkeypatch, body)


def test_resumed_seat_that_may_not_hold_authority_restores_chat_only(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        rig.spawnctl.row_overrides = {"role": "worker"}
        assert (await rig.restore.advance())["state"] == "restored"
        episode = (await rig.episodes())[0]
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert episode["grant_carry"] == "skipped:authority_target_ineligible"
        assert [(r["result"], r["refusal_code"]) for r in await continuity_audit(rig)] == [
            ("refused", "authority_target_ineligible")]
    run(tmp_path, monkeypatch, body)


def test_any_recipient_refusal_skips_the_carry(tmp_path, monkeypatch):
    """The carry uses the existing recipient check and honours whatever it refuses."""
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        monkeypatch.setattr(lifecycle_authority, "eligible", lambda *a: "authority_target_not_ready")
        assert (await rig.restore.advance())["state"] == "restored"
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert (await rig.episodes())[0]["grant_carry"] == "skipped:authority_target_not_ready"
    run(tmp_path, monkeypatch, body)


def test_other_provider_session_never_binds_or_carries(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        rig.spawnctl.row_overrides = {"claude_session_id": "99999999-2222-4333-8444-555555555555"}
        status = await rig.restore.advance()
        assert status["state"] == "pending" and status["reason"] == "holder_lost"
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert (await rig.composite.binding())["generation"] == G0
        assert await continuity_audit(rig) == []
    run(tmp_path, monkeypatch, body)


def test_externally_resumed_seat_never_carries(tmp_path, monkeypatch):
    """A manual resume at its own generation supersedes the episode; no authority follows it."""
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        assert (await rig.restore._create("auto"))["state"] == "pending"
        await rig.store.open_session(
            LOCAL, "fd", provider="claude", role="lead", visibility="hidden", pane_status="pane_alive",
            claude_session_id=CLAUDE_ID, session_generation="7" * 32, bootstrap_state="ready")
        rig.tmux.states["fd"] = "alive"
        status = await rig.restore.advance()
        assert status["state"] != "restored"
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert await rig.store.lifecycle_authority_holder(SEAT, "7" * 32) is False
        assert await continuity_audit(rig) == []
    run(tmp_path, monkeypatch, body)


def test_episode_without_captured_grant_restores_chat_only(tmp_path, monkeypatch):
    """An episode created before this change has no captured grant; nothing is inferred."""
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        assert (await rig.restore._create("auto"))["state"] == "pending"
        def _op(conn):
            conn.execute("UPDATE v2_assistant_restore_episode SET grant_stream_id=NULL,"
                         "grant_generation=NULL,grant_revision=NULL")
            conn.commit()
        await rig.store.submit(_op)
        assert (await rig.restore.advance())["state"] == "restored"
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert (await rig.episodes())[0]["grant_carry"] == "skipped:no_grant"
    run(tmp_path, monkeypatch, body)


def test_failure_inside_the_bind_transaction_rolls_back_binding_and_grant(tmp_path, monkeypatch):
    async def body(rig):
        await rig.start()
        await designate(rig)
        await rig.kill()
        real = lifecycle_authority.audit

        def failing(conn, **fields):
            if fields.get("action") == "restore_continuity":
                raise RuntimeError("fixture: crash before commit")
            return real(conn, **fields)
        monkeypatch.setattr(lifecycle_authority, "audit", failing)
        with pytest.raises(RuntimeError):
            await rig.restore.advance()
        episode = (await rig.episodes())[0]
        assert episode["state"] == "spawned" and episode["grant_carry"] is None
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": G0, "revision": 5}
        assert (await rig.composite.binding())["generation"] == G0
        assert await rig.audit("bound") == []
        # The next boot continues the recorded episode and carries once.
        monkeypatch.setattr(lifecycle_authority, "audit", real)
        rig.restore = rig.owner()
        assert (await rig.restore.advance())["state"] == "restored"
        assert await grant(rig) == {"stream_id": SEAT, "session_generation": episode["last_generation"], "revision": 6}
        assert len(rig.spawnctl.sent) == 1 and len(await continuity_audit(rig)) == 1
    run(tmp_path, monkeypatch, body)


def test_grant_columns_are_added_to_an_existing_episode_table(tmp_path, monkeypatch):
    async def body(rig):
        import sqlite3
        path = str(tmp_path / "sessions.db")
        conn = sqlite3.connect(path)
        ddl = store_assistant_binding.ASSISTANT_RESTORE_EPISODE_DDL
        for column, kind in store_assistant_binding.RESTORE_GRANT_COLUMNS:
            ddl = ddl.replace(f"    {column} {kind},\n", "")
        assert "grant_" not in ddl
        conn.execute(ddl)
        conn.commit()
        conn.close()
        await rig.start()
        columns = await rig.store.submit(lambda c: {r[1] for r in c.execute(
            "PRAGMA table_info(v2_assistant_restore_episode)")})
        assert {"grant_stream_id", "grant_generation", "grant_revision", "grant_carry"} <= columns
    run(tmp_path, monkeypatch, body)
