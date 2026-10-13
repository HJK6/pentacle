"""Guardian provenance belongs to the transcript header, never message text."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import satellite as satellite_module
from codex_rollout_norm import normalize_codex_rollout_records_grouped
from ingest import _StreamIngest, _close_stream
from satellite import Satellite, SatelliteConfig, _DiscoveredPane, _StreamTail
from store import Store
from test_ingest_provider_root_binding import Harness, PID
from test_ingest import _codex_ingest, _codex_row
from test_event_push import _sink, _push, _count_user_and_tell


FIXTURE = Path(__file__).parent / "fixtures" / "codex_guardian_review.jsonl"


def records():
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def primary_records():
    rows = records()
    rows[0]["payload"] = {"id": "primary-example", "thread_source": "user", "source": "cli"}
    return rows


def write_log(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize("marker", ["both", "thread_source", "source"])
def test_internal_request_and_verdict_are_hidden_bookkeeping(marker):
    rows = records()
    if marker == "thread_source": rows[0]["payload"].pop("source")
    if marker == "source": rows[0]["payload"].pop("thread_source")
    groups = normalize_codex_rollout_records_grouped(rows, host="example", session_name="v2-example")
    assert groups == [[], [], []]


def test_identical_content_in_primary_chat_keeps_both_messages_and_identity():
    rows = primary_records()
    events = [e for group in normalize_codex_rollout_records_grouped(rows, host="example", session_name="v2-example") for e in group]
    assert [e["kind"] for e in events] == ["USER", "ASSIST_TEXT"]
    assert [e["raw"]["jsonl_record_uuid"] for e in events] == ["request-example", "verdict-example"]
    assert all(e["session_id"] == "primary-example" for e in events)


@pytest.mark.parametrize("source", [None, "guardian", [], {"subagent": "guardian"},
    {"subagent": []}, {"subagent": {"other": "reviewer"}}, {"subagent": {"other": "Guardian"}}])
def test_other_and_malformed_sources_do_not_hide_real_messages(source):
    rows = primary_records()
    rows[0]["payload"].update(source=source, parent_thread_id="another-thread")
    groups = normalize_codex_rollout_records_grouped(rows, host="example", session_name="v2-example")
    assert [len(group) for group in groups] == [0, 1, 1]


def test_primary_header_resets_review_classification():
    rows = records() + primary_records()
    groups = normalize_codex_rollout_records_grouped(rows, host="example", session_name="v2-example")
    assert [len(group) for group in groups] == [0, 0, 0, 0, 1, 1]


def test_satellite_discovery_skips_newer_review_and_keeps_primary(tmp_path, monkeypatch):
    primary = tmp_path / ".codex" / "sessions" / "primary.jsonl"
    review = primary.with_name("review.jsonl")
    write_log(primary, primary_records()); write_log(review, records())
    os.utime(primary, (100, 100)); os.utime(review, (200, 200))
    monkeypatch.setattr(satellite_module, "_descendant_pids", lambda pid: [pid])
    monkeypatch.setattr(satellite_module.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=f"n{primary}\nn{review}\n"))
    assert satellite_module._codex_transcript_for_pid(8123) == str(primary)


@pytest.mark.parametrize("continued", [False, True])
def test_satellite_rejects_review_even_after_header_was_consumed(tmp_path, continued):
    review = tmp_path / ".codex" / "sessions" / "review.jsonl"
    write_log(review, records())
    sat = Satellite(SatelliteConfig(host="example", checkout=str(tmp_path), history_bytes=-1))
    if continued:
        sat._tails["v2-example"] = _StreamTail("v2-example", path=str(review), provider="codex", provider_session_id="review-example", offset=len(review.read_bytes().splitlines(keepends=True)[0]), primed=True)
    events, high_water, capped = sat._collect({"v2-example": _DiscoveredPane(session_name="v2-example", provider="codex", transcript_path=str(review), pane_pid="8123")})
    assert events == [] and high_water == {} and not capped


@pytest.mark.parametrize("include_primary", [False, True])
def test_local_ingest_excludes_review_before_binding(tmp_path, monkeypatch, include_primary):
    async def run():
        store = Store(":memory:"); store.start(); state = _StreamIngest()
        try:
            review = tmp_path / ".codex" / "sessions" / "review.jsonl"
            primary = review.with_name("primary.jsonl")
            write_log(review, records()); write_log(primary, primary_records())
            h = Harness(store, monkeypatch, primary if include_primary else review)
            await h.open()
            if include_primary: h.descriptors.append((PID, "13", "w", review))
            added = await h.run(h.ingest(), state)
            tail = await store.fetch_session_event_tail("h:v2-root", limit=500)
            assert added == (2 if include_primary else 0)
            assert all(e["session_id"] == "primary-example" for e in tail)
            assert len(tail) == (2 if include_primary else 0)
        finally:
            _close_stream(state); store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("continued", [False, True])
def test_recorded_local_path_rejects_review_before_normalizing(tmp_path, continued):
    async def run():
        store = Store(":memory:"); store.start(); state = _StreamIngest()
        try:
            review = tmp_path / ".codex" / "sessions" / "review.jsonl"
            write_log(review, records())
            await store.open_session("h", "v2-codex", visibility="visible", pane_pid="8123")
            if continued:
                state.path = str(review)
                state.session_id = "review-example"
                state.offset = len(review.read_bytes().splitlines(keepends=True)[0])
            row = await _codex_row(store, review)
            assert await _codex_ingest(store)._ingest_stream(row, state, 500) == 0
            assert await store.fetch_session_event_tail("h:v2-codex", limit=500) == []
        finally:
            _close_stream(state); store.stop()
    asyncio.run(run())


def test_satellite_primary_cursor_push_replay_and_turn_count(tmp_path):
    async def run():
        store = Store(":memory:"); store.start()
        try:
            primary = tmp_path / ".codex" / "sessions" / "primary.jsonl"
            rows = primary_records()
            rows += [json.loads(line) for line in (FIXTURE.parent / "codex_terminal_error.jsonl").read_text().splitlines() if line.strip()][1:]
            write_log(primary, rows)
            await store.open_session("example-host", "v2-example", visibility="visible", provider="codex", pane_pid="8123")
            sink, casts, _ = _sink(store)
            sat = Satellite(SatelliteConfig(host="example-host", checkout=str(tmp_path), history_bytes=-1, max_events_per_pass=1))
            panes = {"v2-example": _DiscoveredPane(session_name="v2-example", provider="codex", transcript_path=str(primary), pane_pid=8123)}
            all_events = []
            for _ in range(10):
                events, high_water, capped = sat._collect(panes)
                if events:
                    result = await sink.handle_push(_push(sink, events, host="example-host"))
                    assert result["inserted"] == len(events)
                    all_events.extend(events)
                if str(primary) in high_water: sat._tails["v2-example"].offset = high_water[str(primary)]
                if not capped: break
            assert sat._tails["v2-example"].offset == primary.stat().st_size
            assert [e["kind"] for e in all_events] == ["USER", "ASSIST_TEXT", "TOOL_USE", "TOOL_RESULT", "ERROR"]
            assert (await sink.handle_push(_push(sink, all_events, host="example-host")))["inserted"] == 0
            tail = await store.fetch_session_event_tail("example-host:v2-example", limit=500)
            assert [e["kind"] for e in tail] == [e["kind"] for e in all_events]
            assert await _count_user_and_tell(store, "example-host:v2-example") == 1
            assert len(casts) == len(all_events)
        finally: store.stop()
    asyncio.run(run())
