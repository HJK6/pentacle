"""Daff seat recovery (E): single-owner respawn + rebind + queued-input replay,
degrade-after-retries, and Bart's preserve path untouched."""
from __future__ import annotations

import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from recovery import DaffRecovery
from sessions import Sessions
from store import Store


DAFF_CHAT = "daff:assistant"
DAFF_ROLE = "daff-assistant"
DEAD_SEAT = "fixture-host:daff-dead"
LOCAL = "fixture-host"


async def _noop_sleep(_seconds):
    return None


def _open_seat(store, stream_id, role=DAFF_ROLE):
    host, name = stream_id.split(":", 1)
    return store.open_session(
        host, name, provider="claude", role=role, visibility="default",
        pane_status="pane_alive", effective_model="claude-opus-5-5", effective_effort="high")


def _daff_config(generation):
    return AssistantCompositeConfig.from_env({
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED": "1",
        "PENTACLE_ASSISTANT_DAFF_COMPOSITE_STREAM_ID": DAFF_CHAT,
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_STREAM_ID": DEAD_SEAT,
        "PENTACLE_ASSISTANT_DAFF_DIRECT_PRIMARY_GENERATION": generation,
    }, name="daff", env_prefix="DAFF_")


class _FakeSpawnctl:
    """Spawns a fresh live Daff seat and returns spawn.ok (or a scripted error)."""

    def __init__(self, store, *, fail_times=0):
        self.store = store
        self.calls = 0
        self.fail_times = fail_times

    async def spawn(self, msg, host):
        self.calls += 1
        if self.calls <= self.fail_times:
            return {"type": "spawn.error", "error_code": "spawn_failed"}
        sid = f"{LOCAL}:daff-new-{self.calls}"
        row = await _open_seat(self.store, sid)
        return {"type": "spawn.ok", "stream_id": sid,
                "session": {"session_generation": row["session_generation"]}}


async def _build(monkeypatch, *, fail_times=0):
    monkeypatch.setenv("PENTACLE_ASSISTANT_ROLE", "assistant")
    monkeypatch.setenv("PENTACLE_ASSISTANT_DAFF_ROLE", DAFF_ROLE)
    store = Store(":memory:")
    store.start()
    dead = await _open_seat(store, DEAD_SEAT)
    daff = AssistantComposite(store, config=_daff_config(dead["session_generation"]))
    await daff.load_binding()
    await daff.ensure_projection()
    sessions = Sessions(store, tmux=None, local_host=LOCAL)
    # Mark the bound Daff pane preserved-dead, exactly as the reconciler does for
    # a protected seat (open row, pane_status pane_dead), so recovery proceeds.
    await sessions.mark_reconciled_dead(
        "fixture-host", "daff-dead", presumed_dead_at="2026-10-03T00:00:00Z",
        closed_at="2026-10-03T00:00:01Z", expected_generation=dead["session_generation"])
    flushed = []
    tells = []

    async def _flush(composite):
        flushed.append(composite.config.name)
        return 0

    async def _tell_bart(text):
        tells.append(text)

    spawnctl = _FakeSpawnctl(store, fail_times=fail_times)
    recovery = DaffRecovery(
        sessions=sessions, spawnctl=spawnctl, store=store, local_host=LOCAL,
        composites=lambda: {"daff": daff}, flush_composite_tells=_flush,
        tell_bart=_tell_bart, startup_prompt="daff startup", sleep=_noop_sleep,
    )
    return store, daff, sessions, spawnctl, recovery, flushed, tells


def test_bart_row_is_not_recovered(monkeypatch):
    async def run():
        store, daff, sessions, spawnctl, recovery, flushed, tells = await _build(monkeypatch)
        try:
            # A Bart-role protected row must be declined (Bart has no auto-respawn).
            await recovery.on_dead({"host": LOCAL, "session_name": "bart-dead",
                                    "role": "assistant", "session_generation": "g"})
            assert spawnctl.calls == 0
            assert flushed == [] and tells == []
        finally:
            store.stop()
    asyncio.run(run())


def test_recovers_daff_rebinds_and_flushes(monkeypatch):
    async def run():
        store, daff, sessions, spawnctl, recovery, flushed, tells = await _build(monkeypatch)
        try:
            dead = await store.fetch_session("fixture-host", "daff-dead")
            await recovery.on_dead(dict(dead))
            await recovery.drain()  # recovery runs as a background task now
            assert spawnctl.calls == 1
            # Binding now points at the fresh seat.
            bound = await daff.binding()
            assert bound["stream_id"] == f"{LOCAL}:daff-new-1"
            seat = await store.fetch_session(LOCAL, "daff-new-1")
            assert bound["generation"] == seat["session_generation"]
            # Queued input was flushed in order for daff.
            assert flushed == ["daff"]
            assert tells == []  # healthy recovery does not tell Bart
        finally:
            store.stop()
    asyncio.run(run())


def test_concurrent_triggers_produce_one_seat(monkeypatch):
    async def run():
        store, daff, sessions, spawnctl, recovery, flushed, tells = await _build(monkeypatch)
        try:
            dead = dict(await store.fetch_session("fixture-host", "daff-dead"))
            await asyncio.gather(recovery.on_dead(dead), recovery.on_dead(dead))
            await recovery.drain()
            # The per-generation guard means only one spawn ran.
            assert spawnctl.calls == 1
        finally:
            store.stop()
    asyncio.run(run())


def test_three_failures_degrade_and_tell_bart_once(monkeypatch):
    async def run():
        # 4 attempts all fail (initial + 3 retries) -> degrade + one Bart tell.
        store, daff, sessions, spawnctl, recovery, flushed, tells = await _build(
            monkeypatch, fail_times=99)
        try:
            dead = dict(await store.fetch_session("fixture-host", "daff-dead"))
            await recovery.on_dead(dead)
            await recovery.drain()
            assert spawnctl.calls == 4  # initial + 3 retries
            assert len(tells) == 1
            assert "degraded" in tells[0]
            # Status card records the degraded state on the preserved seat.
            card = await store.fetch_session("fixture-host", "daff-dead")
            updates = (card.get("status_card") or {}).get("updates") or []
            assert any("DEGRADED" in str(u.get("text", "")) for u in updates)
        finally:
            store.stop()
    asyncio.run(run())


def test_exhausted_episode_not_retried_on_next_reconcile(monkeypatch):
    """Finding 6: the preserved-dead row stays open, so the reconciler re-fires
    on_dead every pass.  An exhausted dead generation must NOT respawn or tell
    Bart again."""
    async def run():
        store, daff, sessions, spawnctl, recovery, flushed, tells = await _build(
            monkeypatch, fail_times=99)
        try:
            dead = dict(await store.fetch_session("fixture-host", "daff-dead"))
            await recovery.on_dead(dead)
            await recovery.drain()
            assert spawnctl.calls == 4 and len(tells) == 1
            # A later reconcile pass fires on_dead again for the SAME dead row.
            await recovery.on_dead(dead)
            await recovery.drain()
            assert spawnctl.calls == 4  # no second respawn storm
            assert len(tells) == 1      # no second degraded tell
        finally:
            store.stop()
    asyncio.run(run())


def test_on_dead_returns_before_backoff_completes(monkeypatch):
    """Finding 7: the reconciler awaits on_dead inline on its serial pass, so
    on_dead must return promptly (schedule a background task) rather than block
    through the retry backoffs."""
    async def run():
        gate = asyncio.Event()

        async def _blocking_sleep(_seconds):
            # Simulate a long retry backoff: block until the test releases it.
            await gate.wait()

        store, daff, sessions, spawnctl, recovery, flushed, tells = await _build(
            monkeypatch, fail_times=99)
        recovery._sleep = _blocking_sleep
        try:
            dead = dict(await store.fetch_session("fixture-host", "daff-dead"))
            # First attempt fails, then recovery awaits the (blocked) backoff.
            await asyncio.wait_for(recovery.on_dead(dead), timeout=1.0)
            # on_dead returned while recovery is still in-flight inside a backoff.
            assert any(not t.done() for t in recovery._tasks)
            gate.set()
            await recovery.drain()
        finally:
            store.stop()
    asyncio.run(run())
