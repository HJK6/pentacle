"""Proven rollout continuity and historical identity refusals (issue 201)."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace

import pytest
import ingest as module
from ingest import _StreamIngest, _close_stream
from prockill import canonical_start
from test_ingest_provider_root_binding import Harness, Store, PID, BIRTH, EXE, write_log, _append_turn

CHANGED = "Wed Sep 9 11:00:07 2026"


def append(path, provider, turn):
    if provider == "codex":
        _append_turn(path, turn)
    else:
        with path.open("a") as out:
            out.write(json.dumps({"type": "user", "sessionId": "native-a", "uuid": turn,
                "timestamp": "2026-09-09T16:22:00Z", "message": {"role": "user", "content": "Goal " + turn}}) + "\n")


@asynccontextmanager
async def subject(tmp_path, monkeypatch, provider="codex", store=None):
    owned = store is None
    if owned:
        store = Store(":memory:"); store.start()
    state = _StreamIngest()
    path = tmp_path / (".codex/sessions" if provider == "codex" else ".claude/projects") / "a.jsonl"
    if provider == "codex":
        write_log(path)
    else:
        path.parent.mkdir(parents=True); append(path, provider, "one")
    h = Harness(store, monkeypatch, path, provider)
    async def tree(root):
        return [h.pid]
    monkeypatch.setattr(module, "process_tree", tree)
    await h.open()
    reader = h.ingest()
    s = SimpleNamespace(store=store, state=state, path=path, h=h, reader=reader, provider=provider)
    try:
        yield s
    finally:
        _close_stream(state)
        if owned: store.stop()


async def binding(s):
    return (await s.store.fetch_session("h", "v2-root"))["observer_binding"]


@pytest.mark.parametrize("value,expected", [(None, ""), (7, ""), ({}, ""), ("", ""), (" \t\n", ""),
    (" Wed Sep  9\t11:00:00 2026 \n", BIRTH), (BIRTH, BIRTH)])
def test_start_canonicalization(value, expected):
    assert canonical_start(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_same_pid_anchored_recovery_replays_gap_once(tmp_path, monkeypatch, provider):
    async with subject(tmp_path, monkeypatch, provider) as s:
        assert await s.h.run(s.reader, s.state) == 1
        before = await binding(s)
        append(s.path, provider, "two")
        s.h.birth = CHANGED
        assert await s.h.run(s.reader, s.state) == 1
        assert await binding(s) == {**before, "pane_pid": PID, "pane_started_at": CHANGED}
        assert await s.h.run(s.reader, s.state) == 0
        append(s.path, provider, "three")
        assert await s.h.run(s.reader, s.state) == 1
        restarted = _StreamIngest()
        try:
            assert await s.h.run(s.h.ingest(), restarted) == 0
        finally:
            _close_stream(restarted)
        assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,live", [(BIRTH.replace("Sep 9", "Sep  9"), BIRTH),
                                        (BIRTH, BIRTH.replace("Sep 9", "Sep  9"))])
async def test_whitespace_is_strict_happy_path_without_rebind_or_alert(tmp_path, monkeypatch, stored, live):
    async with subject(tmp_path, monkeypatch) as s:
        before = await binding(s)
        await s.store.update_session("h", "v2-root", observer_binding={**before, "pane_started_at": stored})
        await s.h.sessions.refresh(); s.h.birth = live
        async def forbidden(*args, **kwargs):
            pytest.fail("presentation equality must never invoke rebind or alert")
        monkeypatch.setattr(s.store, "rebind_observer_pane", forbidden)
        s.reader.alerts = SimpleNamespace(error=forbidden)
        assert await s.h.run(s.reader, s.state) == 1
        assert await s.h.run(s.reader, s.state) == 0
        assert (await binding(s))["pane_started_at"] == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("case", ["no_anchor", "writer_gap", "readonly", "helper", "ambiguous", "partial",
    "missing_probe", "native", "inode", "uid", "executable", "provider", "empty_start", "empty_prior"])
async def test_same_pid_mismatch_refuses_unproven_or_foreign_identity(tmp_path, monkeypatch, provider, case):
    async with subject(tmp_path, monkeypatch, provider) as s:
        if case != "no_anchor":
            assert await s.h.run(s.reader, s.state) == 1
        before = await binding(s)
        append(s.path, provider, "two"); s.h.birth = CHANGED
        if case == "writer_gap": s.h.descriptors = []
        if case == "readonly": s.h.descriptors = [(PID, "12", "r", s.path)]
        if case == "helper": s.h.descriptors = [("9000", "12", "w", s.path)]
        if case == "ambiguous":
            other = s.path.with_name("b.jsonl"); other.write_bytes(s.path.read_bytes())
            s.h.descriptors.append((PID, "13", "w", other))
        if case in {"partial", "missing_probe"}:
            execute = module._exec
            async def unavailable(*args):
                if case == "missing_probe": raise FileNotFoundError("probe missing")
                _, text = await execute(*args)
                return 1, text
            monkeypatch.setattr(module, "_exec", unavailable)
        if case == "native": s.path.write_text(s.path.read_text().replace("native-a", "native-b"))
        if case == "inode":
            other = s.path.with_name("replacement"); other.write_bytes(s.path.read_bytes()); os.replace(other, s.path)
        if case == "uid":
            async def record(pid):
                return {"pid": int(pid), "uid": os.getuid()+1, "start_id": s.h.birth, "command": EXE}
            monkeypatch.setattr(module, "process_record", record)
        if case == "executable": s.h.command = "/other/provider"
        if case == "provider":
            await s.store.update_session("h", "v2-root", provider="unsupported")
            await s.h.sessions.refresh()
        if case == "empty_start": s.h.birth = " "
        if case == "empty_prior":
            before = {**before, "pane_started_at": " "}
            await s.store.update_session("h", "v2-root", observer_binding=before)
            await s.h.sessions.refresh()
        assert await s.h.run(s.reader, s.state) == 0
        assert await binding(s) == before
        assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == (0 if case == "no_anchor" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["start", "pid", "uid", "executable", "pane", "unavailable"])
async def test_proof_observation_fence_refuses_change_before_cas(tmp_path, monkeypatch, change):
    async with subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        before = await binding(s); append(s.path, "codex", "two"); s.h.birth = CHANGED
        # Change the OS counterpart only after transcript native-ID proof.
        native = module._native_session_identity_from_fd
        phase = {"proven": False}
        def prove(fd, provider):
            result = native(fd, provider); phase["proven"] = True; return result
        monkeypatch.setattr(module, "_native_session_identity_from_fd", prove)
        async def record(pid):
            if phase["proven"] and change == "unavailable": return None
            return {"pid": int(pid)+(1 if phase["proven"] and change == "pid" else 0),
                "uid": os.getuid()+(1 if phase["proven"] and change == "uid" else 0),
                "start_id": "later" if phase["proven"] and change == "start" else s.h.birth,
                "command": "/other/provider" if phase["proven"] and change == "executable" else EXE}
        async def pane(name):
            return "9001" if phase["proven"] and change == "pane" else PID
        monkeypatch.setattr(module, "process_record", record); s.h.pane_pid = pane
        async def forbidden(*args, **kwargs): pytest.fail("fence failure must precede CAS")
        monkeypatch.setattr(s.store, "rebind_observer_pane", forbidden)
        assert await s.h.run(s.reader, s.state) == 0
        assert phase["proven"] and await binding(s) == before
        assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("race", ["close", "generation", "binding", "post_cas"])
async def test_store_races_and_post_cas_root_mismatch_never_append(tmp_path, monkeypatch, race):
    async with subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        append(s.path, "codex", "two"); s.h.birth = CHANGED
        before = await binding(s); cas = s.store.rebind_observer_pane
        facts = []
        async def emit(fact): facts.append(fact); return "notification"
        s.reader.alerts = SimpleNamespace(error=emit)
        async def raced(*args, **kwargs):
            if race == "close": await s.store.update_session("h", "v2-root", status="closed")
            if race == "generation":
                await s.store.update_session("h", "v2-root", status="closed")
                await s.h.sessions.open("h", "v2-root", provider="codex", pane_pid=PID, session_generation="gen-b")
            if race == "binding":
                await s.store.update_session("h", "v2-root", observer_binding={**before, "executable": "/other/provider"})
            result = await cas(*args, **kwargs)
            if race == "post_cas": s.h.birth = "Wed Sep 9 11:00:08 2026"
            return result
        monkeypatch.setattr(s.store, "rebind_observer_pane", raced)
        assert await s.h.run(s.reader, s.state) == 0
        assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == (0 if race == "generation" else 1)
        if race != "post_cas": assert facts == []
        else:
            assert len(facts) == 1
            assert (await binding(s))["pane_started_at"] == CHANGED
            # A later normal pass can recover the current rollout; no replay loss.
            monkeypatch.setattr(s.store, "rebind_observer_pane", cas)
            assert await s.h.run(s.reader, s.state) == 1


@pytest.mark.asyncio
async def test_store_noop_uses_presentation_equality_but_cas_is_exact(tmp_path, monkeypatch):
    async with subject(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        before = await binding(s)
        raw = {**before, "pane_started_at": BIRTH.replace("Sep 9", "Sep  9")}
        await s.store.update_session("h", "v2-root", observer_binding=raw)
        assert await s.store.rebind_observer_pane("h:v2-root", generation="gen-a", pane_pid=PID,
            pane_started_at=BIRTH, expected=raw) is None
        assert await binding(s) == raw
        assert await s.store.rebind_observer_pane("h:v2-root", generation="gen-a", pane_pid=PID,
            pane_started_at=CHANGED, expected=before) is None
        assert await binding(s) == raw
        assert await s.store.rebind_observer_pane("h:v2-root", generation="gen-a", pane_pid=PID,
            pane_started_at="", expected=raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [" Wed Sep  9\t11:00:00 2026 \n", "", " \t", None, 12])
async def test_remote_capture_uses_public_start_helper(monkeypatch, raw):
    from spawnctl import SpawnCtl
    from sessions import Sessions
    store = Store(":memory:"); store.start()
    try:
        async def command(*args, **kwargs): return 0, raw
        ctl = SpawnCtl(store, Sessions(store, local_host="local"), hosts=SimpleNamespace(run_command=command))
        assert await ctl._pane_started_at("42420", host="remote") == canonical_start(raw)
    finally:
        store.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, 7, "", " "])
async def test_invalid_starts_never_match_each_other(tmp_path, monkeypatch, raw):
    async with subject(tmp_path, monkeypatch) as s:
        before = await binding(s)
        before = {**before, "pane_started_at": raw}
        await s.store.update_session("h", "v2-root", observer_binding=before)
        await s.h.sessions.refresh(); s.h.birth = raw
        assert await s.h.run(s.reader, s.state) == 0
        assert await binding(s) == before
        assert await s.store.fetch_session_event_tail("h:v2-root", limit=500) == []


@pytest.mark.asyncio
async def test_first_bind_cannot_create_anchor_after_start_changes_during_open(tmp_path, monkeypatch):
    async with subject(tmp_path, monkeypatch) as s:
        before = await binding(s)
        opened = module._open_descriptor
        def change_after_open(path):
            fd = opened(path); s.h.birth = CHANGED; return fd
        monkeypatch.setattr(module, "_open_descriptor", change_after_open)
        assert await s.h.run(s.reader, s.state) == 0
        assert await binding(s) == before and "transcript" not in before
        assert await s.store.fetch_session_event_tail("h:v2-root", limit=500) == []


# Real Claude seats carry a launch-recorded jsonl_path/claude_session_id and do
# not hold their transcript open, so no writable descriptor is visible to lsof.
@asynccontextmanager
async def claude_seat(tmp_path, monkeypatch, session_id="native-a"):
    async with subject(tmp_path, monkeypatch, "claude") as s:
        await s.store.update_session("h", "v2-root", jsonl_path=str(s.path), claude_session_id=session_id)
        await s.h.sessions.refresh()
        s.h.descriptors = []
        yield s


def claude_anchor(path):
    info = path.stat()
    return {"path": str(path), "provider": "claude", "session_id": "native-a",
            "file_id": [info.st_dev, info.st_ino]}


@pytest.mark.asyncio
async def test_claude_launch_recorded_seat_mints_anchor_on_healthy_pass(tmp_path, monkeypatch):
    async with claude_seat(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        assert (await binding(s))["transcript"] == claude_anchor(s.path)


@pytest.mark.asyncio
async def test_claude_launch_recorded_same_pid_shift_recovers_and_keeps_ingesting(tmp_path, monkeypatch):
    async with claude_seat(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        before = await binding(s)
        append(s.path, "claude", "two"); s.h.birth = CHANGED
        assert await s.h.run(s.reader, s.state) == 1
        assert await binding(s) == {**before, "pane_pid": PID, "pane_started_at": CHANGED}
        assert s.state.identity_incident_id is None
        assert await s.h.run(s.reader, s.state) == 0
        append(s.path, "claude", "three")
        assert await s.h.run(s.reader, s.state) == 1
        restarted = _StreamIngest()
        try:
            assert await s.h.run(s.h.ingest(), restarted) == 0
        finally:
            _close_stream(restarted)
        assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["session_mismatch", "empty_session", "usage_partial"])
async def test_claude_launch_recorded_seat_never_mints_unproven_anchor(tmp_path, monkeypatch, case):
    sid = {"session_mismatch": "native-other", "empty_session": ""}.get(case, "native-a")
    async with claude_seat(tmp_path, monkeypatch, session_id=sid) as s:
        if case == "usage_partial":
            partial = {**await binding(s), "transcript": {
                "source_file_identity_digest": "0" * 64, "native_session_id": "native-a", "provider": "claude"}}
            await s.store.update_session("h", "v2-root", observer_binding=partial)
            await s.h.sessions.refresh()
        before = await binding(s)
        await s.h.run(s.reader, s.state)
        assert await binding(s) == before
        append(s.path, "claude", "two"); s.h.birth = CHANGED
        assert await s.h.run(s.reader, s.state) == 0
        assert await binding(s) == before
        assert s.state.identity_incident_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_pid", "native", "inode", "path_moved", "session_changed",
                                  "uid", "executable"])
async def test_claude_anchored_shift_refuses_unproven_continuity(tmp_path, monkeypatch, case):
    async with claude_seat(tmp_path, monkeypatch) as s:
        assert await s.h.run(s.reader, s.state) == 1
        append(s.path, "claude", "two"); s.h.birth = CHANGED
        if case == "other_pid":
            # The reconciler already followed a replacement pane; Claude has no
            # writer proof for a different process, so this stays refused.
            s.h.pid = "8124"
            await s.store.update_session("h", "v2-root", pane_pid="8124")
        if case == "native": s.path.write_text(s.path.read_text().replace("native-a", "native-b"))
        if case == "inode":
            other = s.path.with_name("replacement"); other.write_bytes(s.path.read_bytes()); os.replace(other, s.path)
        if case == "path_moved":
            moved = s.path.with_name("moved.jsonl"); os.link(s.path, moved)
            await s.store.update_session("h", "v2-root", jsonl_path=str(moved))
        if case == "session_changed":
            await s.store.update_session("h", "v2-root", claude_session_id="native-other")
        if case == "uid":
            async def record(pid):
                return {"pid": int(pid), "uid": os.getuid()+1, "start_id": s.h.birth, "command": EXE}
            monkeypatch.setattr(module, "process_record", record)
        if case == "executable": s.h.command = "/other/provider"
        await s.h.sessions.refresh()
        before = await binding(s)
        assert await s.h.run(s.reader, s.state) == 0
        assert await binding(s) == before
        assert s.state.identity_incident_id
        assert len(await s.store.fetch_session_event_tail("h:v2-root", limit=500)) == 1


@pytest.mark.asyncio
async def test_claude_mint_refuses_path_replaced_between_open_and_stat(tmp_path, monkeypatch):
    async with claude_seat(tmp_path, monkeypatch) as s:
        original = s.path.read_text()
        stat = module._file_stat
        def replace_then_stat(path):
            if not s.state.file_id:
                other = s.path.with_name("foreign"); other.write_text(original.replace("native-a", "native-b"))
                os.replace(other, s.path)
            return stat(path)
        monkeypatch.setattr(module, "_file_stat", replace_then_stat)
        async def forbidden(*args, **kwargs): pytest.fail("a descriptor/path identity split must not reach the CAS")
        bind = s.store.bind_observer_transcript
        monkeypatch.setattr(s.store, "bind_observer_transcript", forbidden)
        before = await binding(s)
        assert await s.h.run(s.reader, s.state) == 0
        assert await binding(s) == before
        monkeypatch.setattr(module, "_file_stat", stat)
        monkeypatch.setattr(s.store, "bind_observer_transcript", bind)
        restored = s.path.with_name("restored"); restored.write_text(original); os.replace(restored, s.path)
        for _ in range(3):
            await s.h.run(s.reader, s.state)
        assert (await binding(s))["transcript"] == claude_anchor(s.path)


@pytest.mark.asyncio
async def test_claude_lost_anchor_cas_keeps_legacy_ingest(tmp_path, monkeypatch):
    async with claude_seat(tmp_path, monkeypatch) as s:
        async def lost(*args, **kwargs): return False
        monkeypatch.setattr(s.store, "bind_observer_transcript", lost)
        before = await binding(s)
        assert await s.h.run(s.reader, s.state) == 1
        append(s.path, "claude", "two")
        assert await s.h.run(s.reader, s.state) == 1
        assert await binding(s) == before
