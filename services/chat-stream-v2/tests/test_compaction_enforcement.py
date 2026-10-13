"""Busy admission, possible-input fencing and bounded defer journeys."""
import asyncio
import json
import time

import pytest

from context_adapters import ContextReading
from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig, NudgeJob
from routing_integrity import RoutingIntegrity
from test_context_compaction import IDLE_CAPTURED_LAYOUT, MAIN_ROSTER
from test_context_nudges import _context_harness
from test_nudges import HOST, _iso, _new_store


async def cross(h, at, tokens=500_000):
    await RoutingIntegrity(h.store, h.sessions).observe_context(
        HOST, "child", provider="claude",
        reading=ContextReading(tokens, model="claude-fable-5-1"), observed_at=_iso(at),
    )


async def basis(h):
    return json.loads((await h.store.nudge_state(f"{HOST}:child", "context_compact"))["basis"])


@pytest.mark.parametrize("prior_episode", [False, True])
@pytest.mark.parametrize("working", [True, None])
def test_possible_input_survives_cooldown_restart_and_late_proof(tmp_path, monkeypatch, prior_episode, working):
    async def run():
        store = _new_store(str(tmp_path / "pending.db"))
        try:
            h = await _context_harness(store, parent=False, config=NudgeConfig(compact_enabled=True))
            now = time.time()
            await cross(h, now - 8, 450_000)
            await h.job.run_pass()
            await cross(h, now - 7)
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            await h.job.run_pass()
            admitted_row = h.sessions.get(f"{HOST}:child")
            capture = {key: admitted_row[key] for key in (
                "local_mirror", "mirror_local", "mirror_generation", "local_mirror_generation",
                "capture_generation", "capture_liveness", "pane_status",
            ) if key in admitted_row}
            assert h.job._known_capture(admitted_row)
            original = (await basis(h))["attempt"]
            assert original["outcome"] == "pending_input"
            if prior_episode:
                await cross(h, now - 6, 399_999)
                await cross(h, now - 5)
            store.stop()
            store.start()
            await h.sessions.refresh()
            h.job = NudgeJob(h.sessions, h.comms, store, NudgeConfig(compact_enabled=True))
            # Refresh discards capture metadata; unchanged synthetic pane text
            # does not refresh Mirror's cached capture. Restore the admitted view.
            h.sessions.apply_live(f"{HOST}:child", working=working, **capture)
            assert h.job._known_capture(h.sessions.get(f"{HOST}:child")) is (working is not None)
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            monkeypatch.setattr("ledger.time.time", lambda: now + 601)
            await h.job.run_pass()
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            await h.job.run_pass()
            pending_key = "pending_prior_attempt" if prior_episode else "attempt"
            assert (await basis(h))[pending_key]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            await store.append_session_event(f"{HOST}:child", {
                "stream_id": f"{HOST}:child", "provider": "claude", "kind": "USER",
                "text": CONTEXT_COMPACT_COMMAND, "timestamp": _iso(now + 601),
            }, identity="late-queued-compact", limit=500)
            await h.job.run_pass()
            assert (await basis(h))[pending_key]["outcome"] == "submitted"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            for restart in (False, True):
                if restart:
                    store.stop()
                    store.start()
                    await h.sessions.refresh()
                    h.job = NudgeJob(h.sessions, h.comms, store, NudgeConfig(compact_enabled=True))
                for next_working in (True, False):
                    h.sessions.apply_live(f"{HOST}:child", working=next_working, **capture)
                    row = h.sessions.get(f"{HOST}:child")
                    assert h.job._known_capture(row)
                    assert h.job._context_fresh(row, now + 601)
                    await h.job.run_pass()
                    assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            # A genuinely new crossing rearms after the proven one is consumed.
            await cross(h, now + 602, 399_999)
            await cross(h, now + 603)
            monkeypatch.setattr("ledger.time.time", lambda: now + 604)
            h.sessions.apply_live(f"{HOST}:child", working=True, **capture)
            await h.job.run_pass()  # the new advisory occupies the synthetic composer
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            await h.job.run_pass()
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 2
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["busy_retry", "draft", "mode", "generation", "unknown"])
def test_locked_admission_rechecks_safety(change):
    async def run():
        store = _new_store()
        try:
            h = await _context_harness(store, parent=False, config=NudgeConfig(compact_enabled=True))
            sid = f"{HOST}:child"
            await cross(h, time.time() - 8, 450_000)
            await h.job.run_pass()
            await cross(h, time.time() - 7)
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            count = 0
            if change == "busy_retry":
                await h.job.run_pass()
                count = 1
                h.job = NudgeJob(h.sessions, h.comms, store,
                                 NudgeConfig(compact_enabled=True, compact_cooldown_s=0))
                h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            lock = h.comms._pane_input_lock(sid)
            await lock.acquire()
            task = asyncio.create_task(h.job.run_pass())
            await asyncio.sleep(.01)
            if change in {"busy_retry", "unknown"}:
                h.sessions.apply_live(sid, working=True if change == "busy_retry" else None)
            elif change == "draft":
                h.tmux.set_capture("child", MAIN_ROSTER.replace("❯ ", "❯ synthetic draft"))
            elif change == "mode":
                h.tmux.input_mode_clear = lambda name: asyncio.sleep(0, result=False)
            else:
                await store.update_session(HOST, "child", created_at="2099-01-01T00:00:00Z")
            lock.release()
            await task
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == count
            if count:
                assert (await basis(h))["attempt"]["ordinal"] == 1
            else:
                assert "attempt" not in await basis(h)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("enabled,stale", [(True, False), (False, False), (True, True)])
def test_defer_alert_deadline_dedup_restart_and_suppression(tmp_path, monkeypatch, enabled, stale):
    async def run():
        store = _new_store(str(tmp_path / "defer.db"))
        try:
            h = await _context_harness(store, parent=False, config=NudgeConfig(compact_enabled=enabled))
            now = time.time()
            # Crossing must follow creation. Advance the clock, not its timestamp backwards.
            await cross(h, now - 5, 450_000)
            await h.job.run_pass()
            await cross(h, now - 1)
            notifications = []
            class Notify:
                async def create_internal_notification(self, **kwargs):
                    if kwargs["producer"] == "context_compact":
                        notifications.append(kwargs)
                    return {"notification_id": "synthetic-notification"}
            notify = Notify()
            h.job.notify = notify
            placeholder = IDLE_CAPTURED_LAYOUT.replace("❯ ", "❯ Press up to edit queued messages")
            h.tmux.set_capture("child", placeholder)
            monkeypatch.setattr("ledger.time.time", lambda: now + 598)
            await h.job.run_pass()
            assert notifications == []
            monkeypatch.setattr("ledger.time.time", lambda: now + (1900 if stale else 601))
            await h.job.run_pass()
            store.stop()
            store.start()
            await h.sessions.refresh()
            h.job = NudgeJob(h.sessions, h.comms, store, NudgeConfig(compact_enabled=enabled), notify=notify)
            await h.job.run_pass()
            assert len(notifications) == (1 if enabled and not stale else 0)
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            assert await h.tmux.capture("child") == placeholder
            if notifications:
                assert notifications[0]["dedup_key"].startswith("context-compact-deferred:")
                assert "Press up" not in json.dumps(notifications)
                assert "composer_not_proven_empty" in notifications[0]["body"]
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("previous_possible_input", [False, True])
@pytest.mark.parametrize("recross", [False, True])
def test_no_input_retry_queues_busy_only_without_older_possible_input(monkeypatch, previous_possible_input, recross):
    from sessions import VerbError

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(store, parent=False, config=NudgeConfig(compact_enabled=True))
            now = time.time()
            await cross(h, now - 8, 450_000)
            await h.job.run_pass()
            await cross(h, now - 7)
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            original_paste = h.tmux.paste
            if previous_possible_input:
                await h.job.run_pass()
                assert (await basis(h))["attempt"]["outcome"] == "pending_input"
                monkeypatch.setattr("ledger.time.time", lambda: now + 601)
                h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)

            refusals = []
            async def refuse(name, text):
                refusals.append(text)
                raise VerbError("paste_failed", "synthetic pre-input refusal", phase="not_started")
            h.tmux.paste = refuse
            await h.job.run_pass()
            assert refusals == [CONTEXT_COMPACT_COMMAND]
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == int(previous_possible_input)
            h.tmux.paste = original_paste
            if recross:
                await cross(h, now + 602, 399_999)
                await cross(h, now + 603)
            h.sessions.apply_live(f"{HOST}:child", working=True)
            h.tmux.set_capture("child", MAIN_ROSTER)
            monkeypatch.setattr("ledger.time.time", lambda: now + 1202)
            await h.job.run_pass()
            # A new crossing can deliver its ordinary advisory first. Clear
            # only the owned fake pane, then exercise the next policy sweep.
            h.tmux.set_capture("child", MAIN_ROSTER)
            await h.job.run_pass()
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            final = await basis(h)
            if previous_possible_input:
                saved = final["pending_prior_attempt" if recross else "attempt"]
                assert saved["outcome"] == "pending_input"
            else:
                assert final["attempt"]["outcome"] == "pending_input"
        finally:
            store.stop()
    asyncio.run(run())


def test_failed_idle_retry_retains_receipt_that_lands_before_new_watermark():
    from sessions import VerbError

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(store, parent=False, config=NudgeConfig(compact_enabled=True))
            sid = f"{HOST}:child"
            await cross(h, time.time() - 8, 450_000)
            await h.job.run_pass()
            await cross(h, time.time() - 7)
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            await h.job.run_pass()
            original_fence = (await basis(h))["attempt"]["watermark"]
            h.job = NudgeJob(h.sessions, h.comms, store,
                             NudgeConfig(compact_enabled=True, compact_cooldown_s=0))
            h.tmux.set_capture("child", IDLE_CAPTURED_LAYOUT)
            original_watermark = h.comms._watermark_before_action

            async def receipt_during_lock_wait(stream_id):
                await store.append_session_event(sid, {
                    "stream_id": sid, "provider": "claude", "kind": "USER",
                    "text": CONTEXT_COMPACT_COMMAND, "timestamp": _iso(time.time()),
                }, identity="old-queued-command-before-retry-watermark", limit=500)
                return await original_watermark(stream_id)

            async def no_input(name, text):
                raise VerbError("paste_failed", "synthetic pre-input refusal", phase="not_started")

            h.comms._watermark_before_action = receipt_during_lock_wait
            h.tmux.paste = no_input
            await h.job.run_pass()
            pending = (await basis(h))["attempt"]
            assert pending["outcome"] == "pending_input"
            assert pending["last_retry_outcome"] == "no_input"
            assert pending["watermark"] == original_fence
            h.sessions.apply_live(sid, working=True)
            await h.job.run_pass()
            assert (await basis(h))["attempt"]["outcome"] == "submitted"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
        finally:
            store.stop()
    asyncio.run(run())
