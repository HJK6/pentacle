from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]

from store import Store  # noqa: E402
from ingest import _identity_key  # noqa: E402
import client_contract_probe as probe  # noqa: E402
from inventory import InventoryEmitter  # noqa: E402
from server import Server, STREAM_EVENTS_FRAME_BUDGET_BYTES, _welcome_frame  # noqa: E402
from sessions import Sessions  # noqa: E402


def _process_group_popen_kwargs() -> dict[str, bool]:
    """Start the local fixture service in its own process group."""
    return {"start_new_session": True}


def terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop a fixture process and its children without a deployment helper."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        proc.wait(timeout=2)


def _ev(uuid: str, text: str, *, stream_id: str) -> dict:
    return {
        "stream_id": stream_id, "provider": "claude", "kind": "USER", "text": text,
        "timestamp": "2026-08-05T00:00:00Z",
        "raw": {"jsonl_record_uuid": uuid, "jsonl_event_index": 0},
    }


def _good(n: int) -> list[dict]:
    return [{"stream_id": "h:s", "kind": "USER", "timestamp": f"t{i}", "daemon_seq": i + 1}
            for i in range(n)]


def test_assert_flags_missing_daemon_seq_the_shipped_bug() -> None:
    events = [{"stream_id": "h:s", "kind": "USER", "timestamp": "t"} for _ in range(500)]
    problems = probe.assert_client_consumable(events, min_events=1)
    assert any("DROP" in p and "daemon_seq" in p for p in problems), problems


def test_assert_passes_a_wellformed_reply() -> None:
    assert probe.assert_client_consumable(_good(3), min_events=1) == []


def _stream_event(seq: int, *, stream_id: str = "h:s", final: bool = False) -> dict:
    return {
        "stream_id": stream_id,
        "kind": "ASSIST_TEXT",
        "timestamp": f"t{seq}",
        "daemon_seq": seq,
        **({"final": True} if final else {}),
    }


def test_event_completeness_compares_authoritative_set_not_contiguous_global_seq() -> None:
    expected = [_stream_event(10), _stream_event(17), _stream_event(44, final=True)]
    received = [expected[0], expected[2], expected[1], expected[2]]

    audit = probe.audit_event_completeness(
        expected, received, stream_id="h:s", final_event=expected[-1],
    )

    assert audit["passed"] is True, audit
    assert audit["missing"] == []
    assert audit["duplicate_seqs"] == [44]
    assert audit["final_received"] is True
    assert audit["final_is_last"] is True


def test_event_completeness_fails_missing_final_and_later_event() -> None:
    expected = [_stream_event(10), _stream_event(17), _stream_event(44, final=True)]

    missing = probe.audit_event_completeness(
        expected, expected[:2], stream_id="h:s", final_event=expected[-1],
    )
    later = probe.audit_event_completeness(
        expected, [expected[0], expected[1], expected[2], _stream_event(91)],
        stream_id="h:s", final_event=expected[-1],
    )

    assert missing["passed"] is False
    assert missing["missing"] == [44]
    assert missing["final_received"] is False
    assert later["passed"] is False
    assert later["unexpected_seqs"] == [91]
    assert later["final_is_last"] is False


def test_event_completeness_rejects_wrong_rendered_content_at_an_expected_seq() -> None:
    expected = [_stream_event(10), _stream_event(44, final=True)]
    received = [expected[0], {**expected[1], "text": "wrong final"}]

    audit = probe.audit_event_completeness(
        expected, received, stream_id="h:s", final_event=expected[-1],
    )

    assert audit["passed"] is False
    assert audit["content_mismatch_seqs"] == [44]


def test_probe_reply_uses_independent_authoritative_set_for_missing_final() -> None:
    expected = [_stream_event(10), _stream_event(44, final=True)]
    reply = {
        "authoritative_events": expected,
        "wire_events": expected[:1],
    }

    audit = probe.audit_probe_reply(reply, stream_id="h:s")

    assert audit["passed"] is False
    assert audit["missing"] == [44]
    assert audit["final_received"] is False


def test_probe_reply_large_history_still_checks_independent_final() -> None:
    expected_final = _stream_event(44, final=True)
    reply = {
        "authoritative_events": [expected_final],
        "authoritative_complete": False,
        "wire_events": [_stream_event(10), expected_final],
    }

    audit = probe.audit_probe_reply(reply, stream_id="h:s")

    assert audit["passed"] is True, audit
    assert audit["authority_scope"] == "final_event"


def test_reconnect_recovery_accepts_replay_duplicates_but_requires_final_once() -> None:
    expected = [_stream_event(10), _stream_event(17), _stream_event(44, final=True)]
    initial = expected[:2]
    replay = [expected[0], expected[1], expected[2]]

    recovered = probe.audit_reconnect_recovery(
        expected, initial, replay, stream_id="h:s", final_event=expected[-1],
    )
    failed = probe.audit_reconnect_recovery(
        expected, initial, [expected[0]], stream_id="h:s", final_event=expected[-1],
    )

    assert recovered["passed"] is True, recovered
    assert recovered["recovered_seqs"] == [44]
    assert recovered["final_occurrences"] == 1
    assert failed["passed"] is False
    assert failed["missing"] == [44]


def test_control_plane_latency_reports_p95_and_rejects_breach() -> None:
    assert probe.audit_control_plane_latency([10.0, 20.0, 40.0], p95_limit_ms=50.0)["passed"] is True
    breach = probe.audit_control_plane_latency([10.0, None, 80.0], p95_limit_ms=50.0)
    assert breach["passed"] is False
    assert breach["timeouts"] == 1
    assert breach["p95_ms"] > 50.0


def test_disconnect_and_reconnect_spiral_are_gate_failures() -> None:
    assert probe.audit_disconnects([], reconnect_spiral_count=0)["passed"] is True
    disconnect = probe.audit_disconnects([{"code": 1011, "client": "desktop-main"}], reconnect_spiral_count=0)
    spiral = probe.audit_disconnects([], reconnect_spiral_count=1)
    attempts = probe.audit_disconnects(
        [], reconnect_spiral_count=0,
        reconnect_attempts=[{"at_monotonic": 10.0, "kind": "new_connection"}],
    )
    assert disconnect["passed"] is False
    assert spiral["passed"] is False
    assert spiral["reconnect_spiral_count"] == 1
    assert attempts["passed"] is True
    assert attempts["reconnect_attempts_in_window"] == 1


def test_final_roster_status_must_converge_after_pressure_clears() -> None:
    expected = [{"stream_id": "h:s", "bootstrap_state": "ready", "working": False}]
    assert probe.audit_final_state_convergence(
        expected, [{"stream_id": "h:s", "bootstrap_state": "ready", "working": False}],
    )["passed"] is True
    failed = probe.audit_final_state_convergence(
        expected, [{"stream_id": "h:s", "bootstrap_state": "ready", "working": True}],
    )
    assert failed["passed"] is False


def test_locked_convergence_quiet_boundary_is_inclusive() -> None:
    expected = [{"stream_id": "h:s", "status": "idle"}]
    at_limit = probe.audit_final_state_convergence(
        expected, expected, quiet_ms=probe.CONVERGENCE_QUIET_LIMIT_MS,
    )
    over_limit = probe.audit_final_state_convergence(
        expected, expected, quiet_ms=probe.CONVERGENCE_QUIET_LIMIT_MS + 0.1,
    )

    assert at_limit["passed"] is True
    assert over_limit["passed"] is False


def test_active_summary_roster_omits_persistence_status() -> None:
    expected = [{"stream_id": "h:s", "status": "open", "working": False,
                 "bootstrap_state": "ready", "visibility": "hidden"}]
    compact = Server._summary_snapshot_sessions(expected)
    assert "status" not in compact[0]
    assert probe.audit_final_state_convergence(expected, compact)["passed"] is True
    assert probe.audit_final_state_convergence(expected, [])["passed"] is False
    for status in ("closed", None):
        assert probe.audit_final_state_convergence(
            expected, [{**compact[0], "status": status}],
        )["passed"] is False


def test_slice_two_numeric_envelope_is_locked_in_the_guard() -> None:
    assert probe.LINK_FLOOR_BYTES_PER_SECOND == 40 * 1024
    assert probe.HEARTBEAT_DEADLINE_MS == 1000
    assert probe.HISTORY_HEARTBEAT_DEADLINE_MS == 6000
    assert probe.CONTROL_PLANE_P95_LIMIT_MS == 1000
    assert probe.MAX_QUEUED_BYTES == 64 * 1024
    assert probe.MAX_QUEUED_AGE_MS == 2000
    assert probe.RECONNECT_SPIRAL_LIMIT == 0
    assert probe.RECONNECT_SPIRAL_WINDOW_SECONDS == 60
    assert probe.FULL_HELLO_BYTES == 287809


def test_welcome_reports_the_boot_captured_daemon_sha() -> None:
    sha = "a" * 40
    assert _welcome_frame(nonce="nonce", expires_at=1, runtime_sha=sha)["runtime_sha"] == sha


def test_assert_flags_empty_non_unique_and_unordered() -> None:
    assert any("non-empty" in p for p in probe.assert_client_consumable([], min_events=1))
    dup = _good(2); dup[1]["daemon_seq"] = 1
    assert any("unique" in p for p in probe.assert_client_consumable(dup, min_events=1))
    rev = _good(3)[::-1]
    assert any("increasing" in p for p in probe.assert_client_consumable(rev, min_events=1))


def test_hosts_stats_contract_requires_a_non_empty_exact_host_entry() -> None:
    sample = {
        "host": "hosta",
        "cpu_load_1m": 0.4,
        "memory_used_bytes": 20,
        "memory_total_bytes": 100,
        "disk_used_bytes": 200,
        "disk_total_bytes": 1000,
        "uptime_seconds": 42,
        "sampled_at": "2026-09-05T00:00:00Z",
    }
    assert probe.assert_hosts_stats_frame({
        "type": "hosts.stats", "hosts": {"hosta": sample},
    }) == []
    for inner_host in (None, "", "hostc"):
        malformed = {**sample, "host": inner_host}
        problems = probe.assert_hosts_stats_frame({
            "type": "hosts.stats", "hosts": {"hosta": malformed},
        })
        assert any("host key mismatch" in problem for problem in problems), problems


def test_session_tuple_is_client_visible_in_snapshot_inventory_and_inspect() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            sessions = Sessions(store, local_host="testhost")
            await sessions.open("testhost", "tuple", visibility="visible",
                                requested_model="requested-model", requested_effort="low",
                                effective_model="effective-model", effective_effort="high")
            await sessions.open("testhost", "fallback", visibility="visible",
                                requested_model="fallback-model", requested_effort="medium")
            server = Server(store=store, sessions=sessions, local_host="testhost")
            snapshot = (await server._on_hello({"subscribe": {"include_subagents": True}}))[1]
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            await InventoryEmitter(sessions, broadcast, min_interval_s=0).emit_if_changed()
            inspected = await server._on_inspect_stream({"stream_id": "testhost:tuple", "event_tail": 0})
            expected = {
                "testhost:tuple": ("effective-model", "high"),
                "testhost:fallback": ("fallback-model", "medium"),
            }
            for rows in (snapshot["sessions"], frames[0]["sessions"]):
                by_id = {row["stream_id"]: row for row in rows}
                for stream_id, pair in expected.items():
                    assert (by_id[stream_id]["model"], by_id[stream_id]["effort"]) == pair
            assert (inspected["session"]["model"], inspected["session"]["effort"]) == expected["testhost:tuple"]
        finally:
            store.stop()

    asyncio.run(run())


def test_mobile_summary_reports_last_event_at_for_a_session_with_history() -> None:
    async def run() -> None:
        store = Store(":memory:")
        store.start()
        try:
            stream_id = "testhost:v2-mobile-activity"
            opened = await store.open_session(
                "testhost", "v2-mobile-activity", visibility="visible",
                created_at="2026-08-05T00:00:00Z", session_generation="mobile-a",
            )
            earlier = {
                **_ev("mobile-activity-1", "history", stream_id=stream_id),
                "timestamp": "2026-08-05T01:30:00+02:00",
            }
            event = {
                **_ev("mobile-activity-2", "history", stream_id=stream_id),
                "timestamp": "2026-08-05T00:00:00.100Z",
            }
            for item in (earlier, event):
                await store.append_session_event(
                    stream_id, item, identity=_identity_key(item), limit=500,
                )
            sessions = Sessions(store, local_host="testhost")
            await sessions.refresh()
            server = Server(store=store, sessions=sessions, local_host="testhost")

            snapshot = (await server._on_hello({
                "client": "example-client",
                "subscribe": {"events_mode": "summary", "include_subagents": True},
            }))[1]
            row = next(item for item in snapshot["sessions"] if item["stream_id"] == stream_id)
            assert row["last_event_at"] == event["timestamp"]
            frames: list[dict] = []

            async def broadcast(frame: dict) -> None:
                frames.append(frame)

            await InventoryEmitter(sessions, broadcast, min_interval_s=0).emit_if_changed()
            inventory_row = next(
                item for item in frames[0]["sessions"] if item["stream_id"] == stream_id
            )
            assert inventory_row["last_event_at"] == event["timestamp"]

            newer = {**event, "timestamp": "2026-08-05T00:01:00Z"}
            sessions.apply_genuine_activity_event(stream_id, newer)
            assert sessions.get(stream_id)["last_event_at"] == newer["timestamp"]
            sessions.apply_genuine_activity_event(stream_id, event)
            assert sessions.get(stream_id)["last_event_at"] == newer["timestamp"]

            await store.mark_closed(
                "testhost", "v2-mobile-activity",
                closed_at="2026-08-05T00:02:00Z", pane_status="pane_dead",
                expected_generation=opened["session_generation"],
            )
            await store.open_session(
                "testhost", "v2-mobile-activity", visibility="visible",
                created_at="2026-08-05T00:03:00Z", session_generation="mobile-b",
            )
            replacement = {
                **_ev("mobile-activity-3", "replacement", stream_id=stream_id),
                "timestamp": "2026-08-04T00:00:00Z",
            }
            await store.append_session_event(
                stream_id, replacement, identity=_identity_key(replacement), limit=500,
            )
            await sessions.refresh()
            assert sessions.get(stream_id)["last_event_at"] == replacement["timestamp"]
        finally:
            store.stop()

    asyncio.run(run())


def _seed(db: str, stream_id: str, n: int, *, text_bytes: int = 0) -> None:
    async def _go() -> None:
        store = Store(db); store.start()
        try:
            host, name = stream_id.split(":", 1)
            await store.open_session(host, name, visibility="visible")
            for i in range(n):
                text = f"m{i}" + ("x" * max(0, text_bytes - len(f"m{i}")))
                ev = _ev(f"u{i}", text, stream_id=stream_id)
                await store.append_session_event(stream_id, ev, identity=_identity_key(ev), limit=500)
        finally:
            store.stop()
    asyncio.run(_go())


def test_probe_passes_against_a_fixed_daemon_over_the_wire(tmp_path: Path) -> None:
    db = str(tmp_path / "probe.db")
    stream_id = "testhost:v2-probe"
    _seed(db, stream_id, 4)

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.setdefault("EXAMPLE_MACHINES_JSON", '{"machines":[{"name":"testhost"}]}')
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", "0", "--db", db, "--local-host", "testhost",
         "--notifications-db", str(tmp_path / "n.db"), "--blob-root", str(tmp_path / "b"),
         "--assets-db", str(tmp_path / "a.db")],
        cwd=str(SERVICE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        **_process_group_popen_kwargs(),
    )
    try:
        port = None
        deadline = time.monotonic() + 5.0
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "listening on" in line:
                port = int(line.rsplit(":", 1)[1].strip())
                break
        assert port, f"daemon did not bind (exit={proc.poll()})"

        events = probe.desktop_handshake_fetch(
            f"ws://localhost:{port}", stream_id, limit=500,
            include_subagents=True, events_mode="summary", timeout=5.0,
        )
        assert len(events) == 4, events
        assert probe.assert_client_consumable(events, min_events=1) == [], events
        # The reducer-facing guarantee covered by this public fixture.
        assert all(isinstance(e["daemon_seq"], int) for e in events), events
    finally:
        terminate_process_group(proc)


def test_probe_can_resume_an_older_cursor_page(tmp_path: Path) -> None:
    db = str(tmp_path / "cursor.db")
    stream_id = "testhost:v2-cursor"
    _seed(db, stream_id, 12)

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.setdefault("EXAMPLE_MACHINES_JSON", '{"machines":[{"name":"testhost"}]}')
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", "0", "--db", db, "--local-host", "testhost",
         "--notifications-db", str(tmp_path / "n.db"), "--blob-root", str(tmp_path / "b"),
         "--assets-db", str(tmp_path / "a.db")],
        cwd=str(SERVICE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        **_process_group_popen_kwargs(),
    )
    try:
        port = None
        deadline = time.monotonic() + 5.0
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "listening on" in line:
                port = int(line.rsplit(":", 1)[1].strip())
                break
        assert port, f"daemon did not bind (exit={proc.poll()})"

        first = probe.desktop_handshake_fetch_reply(
            f"ws://localhost:{port}", stream_id, limit=4,
            include_subagents=True, events_mode="summary", timeout=5.0,
        )
        first_events = first["events"]
        assert [event["text"] for event in first_events] == ["m8", "m9", "m10", "m11"]
        cursor = first_events[0]["daemon_seq"]
        resumed = probe.desktop_handshake_fetch_reply(
            f"ws://localhost:{port}", stream_id, limit=4,
            include_subagents=True, events_mode="summary", timeout=5.0,
            before_daemon_seq=cursor,
        )
        resumed_events = resumed["events"]
        assert [event["text"] for event in resumed_events] == ["m4", "m5", "m6", "m7"]
        assert all(event["daemon_seq"] < cursor for event in resumed_events)
    finally:
        terminate_process_group(proc)


def test_probe_receives_all_oversized_history_as_bounded_chunks(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "oversized.db")
    stream_id = "testhost:v2-oversized"
    # 500 recent events at 36 KiB each create a large history without pruning
    # durable rows.
    event_count = 500
    _seed(db, stream_id, event_count, text_bytes=36 * 1024)

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.setdefault("EXAMPLE_MACHINES_JSON", '{"machines":[{"name":"testhost"}]}')
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", "0", "--db", db, "--local-host", "testhost",
         "--notifications-db", str(tmp_path / "n.db"), "--blob-root", str(tmp_path / "b"),
         "--assets-db", str(tmp_path / "a.db")],
        cwd=str(SERVICE_DIR), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        **_process_group_popen_kwargs(),
    )
    try:
        port = None
        deadline = time.monotonic() + 5.0
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "listening on" in line:
                port = int(line.rsplit(":", 1)[1].strip())
                break
        assert port, f"daemon did not bind (exit={proc.poll()})"

        reply, frame_sizes, chunk_count = probe.desktop_handshake_fetch_reply_observed(
            f"ws://localhost:{port}", stream_id, limit=500,
            include_subagents=True, events_mode="summary", timeout=5.0,
        )
        events = reply["events"]
        assert len(events) == event_count
        assert chunk_count > 0
        assert len(frame_sizes) == chunk_count + 1  # chunks plus terminal ok
        assert max(frame_sizes) <= STREAM_EVENTS_FRAME_BUDGET_BYTES
        assert reply["complete"] is True
        assert "events_truncated" not in reply
        assert len(reply["wire_events"]) == event_count
        assert probe.assert_client_consumable(events, min_events=1) == [], events
        assert probe.main([
            "--url", f"ws://localhost:{port}", "--stream", stream_id,
            "--timeout", "5",
        ]) == 0
        output = capsys.readouterr().out
        assert "bootstrap_peak_bytes" in output
    finally:
        terminate_process_group(proc)
