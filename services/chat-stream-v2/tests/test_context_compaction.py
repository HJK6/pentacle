"""Claude compaction threshold and durable crossing regression tests."""
from __future__ import annotations

import asyncio
import json
import time

from context_adapters import ContextReading, context_fields
from routing_integrity import RoutingIntegrity
from test_context_nudges import _context_harness
from test_nudges import HOST, _iso, _new_store


def test_claude_compact_threshold_and_codex_backend_exemption(monkeypatch):
    for suffix in ("ADVISORY_ABS", "ADVISORY_PCT", "COMPACT_ABS", "COMPACT_PCT"):
        monkeypatch.delenv("PENTACLE_CONTEXT_" + suffix, raising=False)
    reading = lambda tokens: ContextReading(tokens, model="claude-fable-5-1")
    assert context_fields("claude", reading(399_999))[2] == "none"
    assert context_fields("claude", reading(400_000))[2] == "advisory"
    assert context_fields("claude", reading(500_000))[2] == "compact"
    assert context_fields(
        "codex", ContextReading(220_000, model_context_window=258_400),
        assistant_backend=True,
    ) == (220_000, 258_400, "none")


def test_compact_episode_persists_across_advisory_dip_and_restart(tmp_path):
    async def run():
        store = _new_store(str(tmp_path / "compact-episodes.db"))
        try:
            h = await _context_harness(store)
            now = time.time()

            async def read(tokens, offset):
                return await RoutingIntegrity(store, h.sessions).observe_context(
                    HOST, "child", provider="claude",
                    reading=ContextReading(tokens, model="claude-fable-5-1"),
                    observed_at=_iso(now + offset),
                )

            await read(500_000, -30)
            first = json.loads((await store.nudge_state(f"{HOST}:child", "context_compact"))["basis"])
            assert first["active"] is True
            await read(450_000, -20)
            await read(500_000, -10)
            second = json.loads((await store.nudge_state(f"{HOST}:child", "context_compact"))["basis"])
            assert second["epoch"] == first["epoch"]
            store.stop()
            store.start()
            await h.sessions.refresh()
            third = json.loads((await store.nudge_state(f"{HOST}:child", "context_compact"))["basis"])
            assert third["epoch"] == first["epoch"]
            await read(399_999, -5)
            await read(500_000, -1)
            reset = json.loads((await store.nudge_state(f"{HOST}:child", "context_compact"))["basis"])
            assert reset["epoch"] != first["epoch"]
        finally:
            store.stop()

    asyncio.run(run())


def test_pending_input_fences_new_episode_until_saved_proof_reconciles():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            observer = RoutingIntegrity(store, h.sessions)
            now = time.time()

            async def read(tokens, offset):
                await observer.observe_context(
                    HOST, "child", provider="claude",
                    reading=ContextReading(tokens, model="claude-fable-5-1"),
                    observed_at=_iso(now + offset),
                )

            await read(450_000, -8)
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            await read(500_000, -7)
            await h.job.run_pass()
            first = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert first["attempt"]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1

            await read(399_999, -6)
            await read(500_000, -5)
            h.tmux.set_capture("child", h.tmux.IDLE)
            await h.job.run_pass()
            second = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert second["epoch"] != first["epoch"]
            assert second["pending_prior_attempt"]["id"] == first["attempt"]["id"]
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1

            await store.append_session_event(
                sid, {
                    "stream_id": sid, "provider": "claude", "kind": "USER",
                    "text": CONTEXT_COMPACT_COMMAND, "timestamp": _iso(time.time()),
                }, identity="late-proof-of-first-compact", limit=500,
            )
            await h.job.run_pass()
            resolved = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert resolved["pending_prior_attempt"]["outcome"] == "submitted"
            assert resolved["pending_prior_attempt"]["proof_event_id"] > first["attempt"]["watermark"]
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1

            h.tmux.set_capture("child", h.tmux.IDLE)
            await h.job.run_pass()
            third = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert third["attempt"]["id"] != first["attempt"]["id"]
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 2
        finally:
            store.stop()

    asyncio.run(run())


def test_codex_parent_receives_no_claude_context_pressure():
    from ledger import NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, config=NudgeConfig(compact_enabled=False),
            )
            await store.update_session(HOST, "parent", provider="codex")
            await h.sessions.refresh()
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 5),
            )
            await h.job.run_pass()
            assert [name for name, _ in h.tmux.pasted_by_name] == ["child"]
            assert "context_advisory" in h.tmux.pasted[0]
        finally:
            store.stop()

    asyncio.run(run())


def test_occupied_composer_defers_compaction_without_input():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            now = time.time()
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 5),
            )
            await h.job.run_pass()
            draft = (
                "⏵⏵ bypass permissions on (bypass)\n"
                "❯ user draft in progress\n"
                "────────────────────────\n"
                "⏵⏵ bypass permissions on (bypass)\n"
            )
            h.tmux.set_capture("child", draft)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 4),
            )
            await h.job.run_pass()
            assert await h.tmux.capture("child") == draft
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            basis = json.loads((await store.nudge_state(f"{HOST}:child", "context_compact"))["basis"])
            assert "attempt" not in basis
        finally:
            store.stop()

    asyncio.run(run())


def test_pane_mode_defers_compaction_without_input():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 6),
            )
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            h.tmux.input_mode_clear = lambda name: asyncio.sleep(0, result=False)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 5),
            )
            await h.job.run_pass()
            basis = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert "attempt" not in basis
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
        finally:
            store.stop()

    asyncio.run(run())


def test_disable_switch_preserves_episode_until_reenabled():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig, NudgeJob

    async def run():
        store = _new_store()
        try:
            disabled = NudgeConfig.from_env({"PENTACLE_CONTEXT_COMPACT_ENABLED": "0"})
            enabled = NudgeConfig.from_env({"PENTACLE_CONTEXT_COMPACT_ENABLED": "1"})
            assert disabled.compact_enabled is False
            assert enabled.compact_enabled is True
            h = await _context_harness(store, parent=False, config=disabled)
            sid = f"{HOST}:child"
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 5),
            )
            await h.job.run_pass()
            before = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert "attempt" not in before
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            h.tmux.set_capture("child", h.tmux.IDLE)
            h.job = NudgeJob(h.sessions, h.comms, store, enabled)
            await h.job.run_pass()
            after = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert after["epoch"] == before["epoch"]
            assert after["attempt"]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            h.job = NudgeJob(h.sessions, h.comms, store, disabled)
            await h.job.run_pass()
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_pre_input_failure_retries_only_after_compact_cooldown(monkeypatch):
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig
    from sessions import VerbError

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False,
                config=NudgeConfig(compact_enabled=True, compact_cooldown_s=600),
            )
            sid = f"{HOST}:child"
            now = time.time()
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 5),
            )
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 4),
            )
            original_paste = h.tmux.paste

            async def fail_before_input(name, text):
                if text == CONTEXT_COMPACT_COMMAND:
                    raise VerbError("paste_failed", "injected load-buffer refusal", phase="not_started")
                await original_paste(name, text)

            h.tmux.paste = fail_before_input
            await h.job.run_pass()
            first = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert first["attempt"]["outcome"] == "no_input"
            assert first["attempt"]["ordinal"] == 1
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            await h.job.run_pass()
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            h.tmux.paste = original_paste
            monkeypatch.setattr("ledger.time.time", lambda: now + 601)
            await h.job.run_pass()
            second = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert second["attempt"]["ordinal"] == 2
            assert second["attempt"]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_turn_becoming_busy_while_pane_lock_waits_defers_input():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            now = time.time()
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 5),
            )
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 4),
            )
            lock = h.comms._pane_input_lock(sid)
            await lock.acquire()
            task = asyncio.create_task(h.job.run_pass())
            await asyncio.sleep(.01)
            h.sessions.apply_live(sid, working=True)
            lock.release()
            await task
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            basis = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert "attempt" not in basis
        finally:
            store.stop()

    asyncio.run(run())


def test_exact_command_left_in_draft_gets_one_enter_only_recovery():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            now = time.time()
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 5),
            )
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 4),
            )
            original_paste = h.tmux.paste
            enters = []

            async def paste(name, text):
                await original_paste(name, text)
                if text == CONTEXT_COMPACT_COMMAND:
                    h.tmux.set_capture(
                        name, f"⏵⏵ bypass permissions on (bypass)\n❯ {text}\n",
                    )

            async def send_enter(name):
                enters.append(name)
                await store.append_session_event(
                    sid, {
                        "stream_id": sid, "provider": "claude", "kind": "USER",
                        "text": CONTEXT_COMPACT_COMMAND,
                        "timestamp": _iso(time.time()),
                    }, identity="enter-only-command", limit=500,
                )

            h.tmux.paste = paste
            h.tmux.send_enter = send_enter
            await h.job.run_pass()
            basis = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert basis["attempt"]["outcome"] == "submitted"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            assert enters == ["child"]
        finally:
            store.stop()

    asyncio.run(run())


def test_collapsed_or_mixed_draft_never_gets_enter_recovery():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run(draft):
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 6),
            )
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 5),
            )
            original_paste = h.tmux.paste
            enters = []

            async def paste(name, text):
                await original_paste(name, text)
                if text == CONTEXT_COMPACT_COMMAND:
                    h.tmux.set_capture(name, draft)

            h.tmux.paste = paste
            h.tmux.send_enter = lambda name: enters.append(name)
            await h.job.run_pass()
            basis = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert basis["attempt"]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            assert enters == []
        finally:
            store.stop()

    asyncio.run(run("⏵⏵ bypass permissions on (bypass)\n❯ [Pasted text #1]\n"))
    asyncio.run(run(f"⏵⏵ bypass permissions on (bypass)\n❯ {CONTEXT_COMPACT_COMMAND} extra\n"))


def test_mode_entered_after_paste_blocks_enter_recovery():
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig

    async def run():
        store = _new_store()
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            observer = RoutingIntegrity(store, h.sessions)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 6),
            )
            await h.job.run_pass()
            h.tmux.set_capture("child", h.tmux.IDLE)
            await observer.observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(time.time() - 5),
            )
            original_paste = h.tmux.paste
            mode_active = False
            enters = []

            async def paste(name, text):
                nonlocal mode_active
                await original_paste(name, text)
                if text == CONTEXT_COMPACT_COMMAND:
                    h.tmux.set_capture(name, f"⏵⏵ bypass permissions on (bypass)\n❯ {text}\n")
                    mode_active = True

            h.tmux.paste = paste
            h.tmux.input_mode_clear = lambda name: asyncio.sleep(0, result=not mode_active)
            h.tmux.send_enter = lambda name: enters.append(name)
            await h.job.run_pass()
            basis = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert basis["attempt"]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            assert enters == []
        finally:
            store.stop()

    asyncio.run(run())


def test_idle_compaction_has_durable_user_proof_and_no_duplicate(tmp_path):
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig, NudgeJob

    async def run():
        store = _new_store(str(tmp_path / "confirmed.db"))
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            original_paste = h.tmux.paste

            async def paste(name, text):
                await original_paste(name, text)
                h.tmux.set_capture(
                    name, h.tmux._panes[name] + "⏵⏵ bypass permissions on (bypass)\n",
                )
                if text == CONTEXT_COMPACT_COMMAND:
                    await store.append_session_event(
                        sid, {
                            "stream_id": sid, "provider": "claude", "kind": "USER",
                            "text": text, "timestamp": _iso(time.time()),
                        }, identity="compact-command-1", limit=500,
                    )

            h.tmux.paste = paste
            now = time.time()
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 5),
            )
            h.sessions.apply_live(sid, working=True)
            await h.job.run_pass()
            assert CONTEXT_COMPACT_COMMAND not in h.tmux.pasted
            h.sessions.apply_live(sid, working=False)
            await h.job.run_pass()
            basis = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert basis["attempt"]["outcome"] == "submitted"
            assert basis["attempt"]["proof_event_id"] > basis["attempt"]["watermark"]
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            h.job = NudgeJob(h.sessions, h.comms, store)
            await h.job.run_pass()
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(450_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 4),
            )
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 3),
            )
            await h.job.run_pass()
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
        finally:
            store.stop()

    asyncio.run(run())


def test_uncertain_input_stays_fenced_until_user_event_reconciliation(tmp_path):
    from ledger import CONTEXT_COMPACT_COMMAND, NudgeConfig, NudgeJob

    async def run():
        store = _new_store(str(tmp_path / "pending.db"))
        try:
            h = await _context_harness(
                store, parent=False, config=NudgeConfig(compact_enabled=True),
            )
            sid = f"{HOST}:child"
            original_paste = h.tmux.paste

            async def paste(name, text):
                await original_paste(name, text)
                h.tmux.set_capture(
                    name, h.tmux._panes[name] + "⏵⏵ bypass permissions on (bypass)\n",
                )
                if text == CONTEXT_COMPACT_COMMAND:
                    raise RuntimeError("injected failure after body input")

            h.tmux.paste = paste
            now = time.time()
            await RoutingIntegrity(store, h.sessions).observe_context(
                HOST, "child", provider="claude",
                reading=ContextReading(500_000, model="claude-fable-5-1"),
                observed_at=_iso(now - 5),
            )
            await h.job.run_pass()
            first = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert first["attempt"]["outcome"] == "pending_input"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            store.stop()
            store.start()
            await h.sessions.refresh()
            h.job = NudgeJob(h.sessions, h.comms, store, NudgeConfig(compact_enabled=True))
            await h.job.run_pass()
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
            await store.append_session_event(
                sid, {
                    "stream_id": sid, "provider": "claude", "kind": "USER",
                    "text": CONTEXT_COMPACT_COMMAND, "timestamp": _iso(time.time()),
                }, identity="reconciled-command", limit=500,
            )
            await h.job.run_pass()
            last = json.loads((await store.nudge_state(sid, "context_compact"))["basis"])
            assert last["attempt"]["outcome"] == "submitted"
            assert h.tmux.pasted.count(CONTEXT_COMPACT_COMMAND) == 1
        finally:
            store.stop()

    asyncio.run(run())
