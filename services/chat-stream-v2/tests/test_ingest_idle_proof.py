"""Idle visits through run_pass, with real transcripts and probe-boundary counters."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import logging
import os
from types import SimpleNamespace

import pytest
import ingest as module
import prockill
from ingest import _IdentityGrace, _close_stream
from tmux_transport import Tmux
from test_ingest_identity_recovery import subject, append
from test_ingest_provider_root_binding import PID, BIRTH, EXE

SID = "h:v2-root"


@asynccontextmanager
async def ready(tmp_path, monkeypatch, *, interval="30", count=1):
    monkeypatch.setenv("PENTACLE_INGEST_IDLE_PROOF_S", interval)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(module, "monotonic", lambda: clock.now)
    async with subject(tmp_path, monkeypatch) as s:
        s.clock = clock
        s.launches = []
        s.proofs = []
        s.reader._streams[SID] = s.state
        for i in range(1, count):
            name = f"v2-idle-{i}"
            key = f"spawn-{i}"
            assert await s.store.reserve_stream_id("h", name, ttl_s=60, request_id=key, nonce=key)
            assert await s.store.record_spawn_intent("h", name, {"open_fields": {"session_generation": "gen-a"}}, request_id=key, nonce=key)
            assert await s.store.commit_tmux_created_fenced("h", name, request_id=key, nonce=key, pane_pid=PID, pane_started_at=BIRTH)
            await s.h.sessions.open("h", name, fence=key, provider="codex", pane_pid=PID,
                                    session_generation="gen-a", observer_binding={"executable": EXE})
            assert await s.store.release_stream_id_fenced("h", name, key)

        async def run(self, *args, **kwargs):
            assert args[0] == "list-panes"
            s.launches.append("tmux")
            return 0, s.h.pid + "\n"

        async def record(pid):
            s.launches.append("ps")
            return {"pid": int(pid), "uid": os.getuid(), "start_id": s.h.birth, "command": s.h.command}

        root = s.reader._provider_root
        async def proof(row, name, **kwargs):
            s.proofs.append(row["stream_id"])
            return await root(row, name, **kwargs)

        monkeypatch.setattr(Tmux, "run", run)
        monkeypatch.setattr(prockill, "process_record", record)
        monkeypatch.setattr(module, "process_record", prockill.process_record)
        monkeypatch.setattr(s.reader, "_provider_root", proof)
        s.reader.tmux = Tmux()
        try:
            assert await s.reader.run_pass() == count
            assert all(st.replay_complete for st in s.reader._streams.values())
            s.launches.clear(); s.proofs.clear()
            yield s
        finally:
            for st in s.reader._streams.values():
                _close_stream(st)


def forbid(*args, **kwargs):
    pytest.fail("skipped visit performed transcript or store work")


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 10, 40])
async def test_skip_has_no_effects_or_sliding_age(tmp_path, monkeypatch, caplog, count):
    caplog.set_level(logging.INFO, logger=module.log.name)
    async with ready(tmp_path, monkeypatch, count=count) as s:
        assert sum(r.message.startswith("ingest_replay_complete ") for r in caplog.records) == count
        before = [dict(st.__dict__) for st in s.reader._streams.values()]
        with monkeypatch.context() as guards:
            for name in ("_native_session_identity_from_fd", "_codex_session_identity_from_fd", "_cursor_header", "_read_bound_span",
                         "normalize_codex_rollout_record", "normalize_claude_jsonl_records", "append_ingested_event"):
                guards.setattr(module, name, forbid)
            for name in ("_ingest_stream", "_discover_path", "_prepare_position", "_finish_position"):
                guards.setattr(s.reader, name, forbid)
            for name in ("bind_observer_transcript", "save_ingest_position", "fetch_session", "fetch_open_session_lifecycle",
                         "prepare_native_input_events", "stamp_event_with_send_receipt", "append_session_events_lifecycle_cas"):
                guards.setattr(s.store, name, forbid)
            for now in (101, 120, 129.9):
                s.clock.now = now
                assert await s.reader.run_pass() == 0
                assert not s.reader._replay_backlog()
                assert [st.__dict__ for st in s.reader._streams.values()] == before
        assert s.launches == s.proofs == []
        s.clock.now = 130
        assert await s.reader.run_pass() == 0
        assert len(s.proofs) == 2 * count
        assert s.launches == ["tmux", "ps"] * (2 * count)


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["0", "30"])
@pytest.mark.parametrize("change", ["generation", "binding", "pid", "account", "growth", "shorter", "inode", "links", "closed_fd", "unbound", "refusal", "grace", "incident", "failed", "replay"])
async def test_changes_force_normal_entry_before_transcript_work(tmp_path, monkeypatch, change, interval):
    async with ready(tmp_path, monkeypatch, interval=interval) as s:
        row = s.h.sessions.get(SID)
        if change == "generation": s.h.sessions.apply_durable(SID, session_generation="gen-b")
        if change == "binding": s.h.sessions.apply_durable(SID, observer_binding={**row["observer_binding"], "extra": "changed"})
        if change == "pid": s.h.sessions.apply_durable(SID, pane_pid="9123")
        if change == "account": s.h.sessions.apply_durable(SID, account_binding={"provider": "codex", "host": "h", "generation": "new"})
        if change == "growth": append(s.path, "codex", "two")
        if change == "shorter": s.path.write_text("")
        if change == "inode":
            replacement = s.path.with_name("new.jsonl"); replacement.write_bytes(s.path.read_bytes()); replacement.replace(s.path)
        if change == "links": os.link(s.path, s.path.with_name("link.jsonl"))
        if change == "closed_fd": _close_stream(s.state)
        if change == "unbound": s.state.path = ""
        if change == "refusal": s.state.identity_refusal = (row, {})
        if change == "grace": s.state.identity_grace = _IdentityGrace((SID, "gen-a", "x", PID), 100)
        if change == "incident": s.state.identity_incident_id = "incident"
        if change == "failed":
            s.h.command = "/different/provider"
            assert await s.reader._provider_root(row, "v2-root") is None
            s.h.command = EXE
        if change == "replay": s.state.replay_complete = False
        s.proofs.clear(); s.launches.clear()
        native = module._native_session_identity_from_fd
        span = module._read_bound_span
        def guarded_native(*args):
            assert s.proofs, "header read preceded entry proof"
            return native(*args)
        def guarded_span(*args):
            assert s.proofs, "span read preceded entry proof"
            return span(*args)
        monkeypatch.setattr(module, "_native_session_identity_from_fd", guarded_native)
        monkeypatch.setattr(module, "_read_bound_span", guarded_span)
        await s.reader.run_pass()
        assert s.proofs, change
        assert s.proofs[0] == SID


@pytest.mark.asyncio
async def test_bytes_after_stat_wait_then_drain_and_skip(tmp_path, monkeypatch):
    async with ready(tmp_path, monkeypatch) as s:
        stat = module._idle_file_unchanged
        def raced(*args):
            result = stat(*args)
            append(s.path, "codex", "two")
            return result
        with monkeypatch.context() as patch:
            patch.setattr(module, "_idle_file_unchanged", raced)
            patch.setattr(module, "_read_bound_span", forbid)
            assert await s.reader.run_pass() == 0
        assert not s.proofs
        assert await s.reader.run_pass() == 1
        assert s.launches == ["tmux", "ps"] * 2
        s.proofs.clear(); s.launches.clear()
        assert await s.reader.run_pass() == 0
        assert not s.proofs
        assert len(await s.store.fetch_session_event_tail(SID, limit=500)) == 2


@pytest.mark.asyncio
async def test_header_rewrite_and_restart_require_new_checks(tmp_path, monkeypatch):
    async with ready(tmp_path, monkeypatch) as s:
        s.path.write_text(s.path.read_text().replace("native-a", "native-b"))
        assert await s.reader.run_pass() == 0
        assert not s.proofs
        s.clock.now = 130
        with monkeypatch.context() as patch:
            patch.setattr(module, "_read_bound_span", forbid)
            assert await s.reader.run_pass() == 0
        assert s.proofs and s.state.fd is None
    async with ready(tmp_path / "restart", monkeypatch) as s:
        _close_stream(s.state)
        s.reader = s.h.ingest()
        s.reader.tmux = Tmux()
        assert await s.reader.run_pass() == 0
        assert s.launches
        assert len(await s.store.fetch_session_event_tail(SID, limit=500)) == 1


@pytest.mark.asyncio
async def test_delayed_visit_and_backoff_do_not_extend_proof(tmp_path, monkeypatch):
    async with ready(tmp_path, monkeypatch) as s:
        s.state.next_attempt_monotonic = asyncio.get_running_loop().time() + 100
        s.clock.now = 150
        assert await s.reader.run_pass() == 0
        assert not s.proofs and s.state.idle_proof_at == 100
        s.state.next_attempt_monotonic = 0
        assert await s.reader.run_pass() == 0
        assert len(s.proofs) == 2


@pytest.mark.asyncio
async def test_replaced_pane_refusal_grace_restore_then_skip(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=module.log.name)
    async with ready(tmp_path, monkeypatch) as s:
        async def error(fact): return "notification"
        s.reader.alerts = SimpleNamespace(error=error)
        monkeypatch.setattr(module, "pid_alive", lambda pid: True)
        s.h.pid = "9123"
        assert await s.reader.run_pass() == 0
        assert not s.proofs
        for now, attempts in ((130, 1), (131, 2), (140, 3)):
            s.clock.now = now
            s.proofs.clear()
            assert await s.reader.run_pass() == 0
            assert s.proofs
            assert s.state.identity_grace.attempts == attempts
        assert s.state.identity_incident_id.startswith("ingest_identity_blocked:")
        s.h.pid = PID
        assert await s.reader.run_pass() == 0
        assert s.state.identity_incident_id is None and s.state.identity_grace is None
        assert any("strict root restored" in r.message for r in caplog.records)
        s.proofs.clear()
        assert await s.reader.run_pass() == 0
        assert not s.proofs


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", ["0", "30"])
async def test_active_launch_sequence_and_disabled_idle_path(tmp_path, monkeypatch, interval):
    async with ready(tmp_path, monkeypatch, interval=interval) as s:
        for turn in ("two", "three"):
            append(s.path, "codex", turn)
            assert await s.reader.run_pass() == 1
            assert s.launches == ["tmux", "ps"] * 2
            s.launches.clear(); s.proofs.clear()
        assert await s.reader.run_pass() == 0
        assert s.launches == (["tmux", "ps"] * 2 if interval == "0" else [])


@pytest.mark.asyncio
async def test_summary_scripted_minute_excludes_sleep_and_counts_visits(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=module.log.name)
    async with ready(tmp_path, monkeypatch) as s:
        warm_proofs = s.reader._proof_calls
        body = s.reader._prove_provider_root
        async def timed(*args, **kwargs):
            result = await body(*args, **kwargs)
            s.clock.now += 0.125
            return result
        monkeypatch.setattr(s.reader, "_prove_provider_root", timed)
        s.clock.now = 110
        assert await s.reader.run_pass() == 0
        s.state.next_attempt_monotonic = asyncio.get_running_loop().time() + 100
        s.clock.now = 120
        assert await s.reader.run_pass() == 0
        s.state.next_attempt_monotonic = 0
        s.clock.now = 159
        assert await s.reader.run_pass() == 0
        assert not any(r.message.startswith("ingest_pass_summary ") for r in caplog.records)
        s.clock.now = 160
        assert await s.reader.run_pass() == 0
        lines = [r.message for r in caplog.records if r.message.startswith("ingest_pass_summary ")]
        assert len(lines) == 1
        assert json.loads(lines[0].split(" ", 1)[1]) == {
            "passes": 5, "pass_ms_median": 0, "pass_ms_max": 250, "seats_median": 1,
            "backoff_skips": 1, "idle_skips": 2, "proof_calls": warm_proofs + 2, "window_s": 60,
        }


@pytest.mark.parametrize("value", ["bad", "-1", "nan", "inf"])
def test_invalid_switch_warns_once_and_is_read_only_at_construction(monkeypatch, caplog, value):
    monkeypatch.setenv("PENTACLE_INGEST_IDLE_PROOF_S", value)
    reader = module.Ingest(None, None, None, None, local_host="h", recent_limit=500)
    assert reader.idle_proof_s == 30
    assert sum("invalid PENTACLE_INGEST_IDLE_PROOF_S" in r.message for r in caplog.records) == 1
    monkeypatch.setenv("PENTACLE_INGEST_IDLE_PROOF_S", "0")
    assert reader.idle_proof_s == 30


@pytest.mark.asyncio
async def test_proof_start_snapshot_and_exception_preserve_guard_contract(tmp_path, monkeypatch):
    async with ready(tmp_path, monkeypatch) as s:
        row = s.h.sessions.get(SID)
        saved = deepcopy(row["observer_binding"])
        body = s.reader._prove_provider_root
        async def delayed(row, name, **kwargs):
            result = await body(row, name, **kwargs)
            s.clock.now += 40
            row["observer_binding"]["changed_after_probe"] = True
            return result
        with monkeypatch.context() as patch:
            patch.setattr(s.reader, "_prove_provider_root", delayed)
            assert await s.reader._provider_root(row, "v2-root") == (PID, BIRTH)
        assert s.state.idle_proof_at == 100
        assert s.state.idle_proof_inputs[1] == saved
        failure = RuntimeError("probe failed")
        async def raises(*args, **kwargs): raise failure
        monkeypatch.setattr(s.reader, "_prove_provider_root", raises)
        with pytest.raises(RuntimeError) as caught:
            await s.reader._provider_root(row, "v2-root")
        assert caught.value is failure
        assert s.state.idle_proof_at is None


@pytest.mark.asyncio
async def test_stat_error_falls_through_and_cheap_rejection_avoids_stat(tmp_path, monkeypatch):
    async with ready(tmp_path, monkeypatch) as s:
        fstat = os.fstat
        called = False
        def fail_once(fd):
            nonlocal called
            if not called:
                called = True
                raise OSError("descriptor unavailable")
            return fstat(fd)
        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", fail_once)
            assert await s.reader.run_pass() == 0
        assert called and len(s.proofs) == 2
        s.proofs.clear()
        s.clock.now += 30
        with monkeypatch.context() as patch:
            patch.setattr(module, "_idle_file_unchanged", forbid)
            assert await s.reader.run_pass() == 0
        assert len(s.proofs) == 2


@pytest.mark.asyncio
async def test_skip_preserves_failure_and_replay_bookkeeping(tmp_path, monkeypatch):
    async with ready(tmp_path, monkeypatch) as s:
        s.state.failures = 2
        s.state.next_attempt_monotonic = asyncio.get_running_loop().time() - 1
        s.state.replay_pending = True
        before = dict(s.state.__dict__)
        assert await s.reader.run_pass() == 0
        assert s.state.__dict__ == before
        assert not s.reader._replay_backlog()
        assert not s.proofs
