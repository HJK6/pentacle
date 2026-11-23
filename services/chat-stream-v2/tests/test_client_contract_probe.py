from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

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
    assert probe.assert_hosts_stats_frame({
        "type": "hosts.stats", "hosts": {"hosta": {**sample, "cpu_usage_pct": None}},
    }) == []
    assert probe.assert_hosts_stats_frame({
        "type": "hosts.stats", "hosts": {"hosta": {**sample, "cpu_usage_pct": 0}},
    }) == []
    for cpu in (-1, 101, 10 ** 1000, True, "12"):
        assert any("cpu_usage_pct" in problem for problem in probe.assert_hosts_stats_frame({
            "type": "hosts.stats", "hosts": {"hosta": {**sample, "cpu_usage_pct": cpu}},
        }))
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


@pytest.mark.parametrize("events_mode", ["summary", "full"])
@pytest.mark.parametrize("oversized_field", ["text", "raw", "attachments", "peer_payload", "peer_payload_large"])
def test_single_oversized_event_backfills_without_blocking_history(
    tmp_path: Path, events_mode: str, oversized_field: str,
) -> None:
    db = str(tmp_path / "single-oversized.db")
    stream_id = "testhost:v2-single-oversized"
    # JSON escapes and multibyte characters must count as wire bytes, not chars.
    large = ('雪😀\n"\\' * (STREAM_EVENTS_FRAME_BUDGET_BYTES // 8))

    async def seed_or_read(seed: bool) -> list[dict]:
        store = Store(db)
        store.start()
        try:
            if seed:
                await store.open_session("testhost", "v2-single-oversized", visibility="visible")
                for i in range(11):
                    event = _ev(f"u{i}", f"m{i}", stream_id=stream_id)
                    if i == 5:
                        event["raw"].update(source="structured", transport="codex-rollout", sender="testhost:peer")
                        if oversized_field == "text":
                            event["text"] = large
                        elif oversized_field == "raw":
                            event["raw"]["tool_output"] = {"content": large}
                        elif oversized_field == "attachments":
                            event["attachments"] = [{"key": large, "mime": "image/png"}]
                        elif oversized_field == "peer_payload":
                            event.update(kind="TELL", text=large)
                            event["raw"]["peer_payload"] = "m5 peer body"
                        else:
                            event["kind"] = "TELL"
                            event["raw"]["peer_payload"] = large
                    if i == 5 and oversized_field in {"peer_payload", "peer_payload_large"}:
                        # This test exercises legacy peer display priority. Give
                        # that synthetic peer a valid current delivery occurrence.
                        from comms import payload_digest
                        from message_envelopes import build_message_envelope
                        target = await store.fetch_session("testhost", "v2-single-oversized")
                        wire = build_message_envelope("peer_delivery", from_stream_id="testhost:peer",
                            anchor="tell", anchor_id="backfill-fixture", payload=event["raw"]["peer_payload"])
                        digest = payload_digest(stream_id, wire)
                        event["raw"]["peer_wire_text"] = wire
                        await store.put_tell_delivery("backfill-fixture", {"payload_digest": digest,
                            "reply": {"tell_id": "backfill-fixture", "to_stream_id": stream_id,
                                      "delivery_status": "committed_pending_proof"},
                            "delivery": {"tell_id": "backfill-fixture", "to_stream_id": stream_id,
                                "text": wire, "from_stream_id": "testhost:peer", "sender_verified": True,
                                "sender_kind": "peer", "sender_generation": "synthetic-backfill-peer",
                                "proof_generation": target["session_generation"],
                                "proof_session_created_at": target["created_at"], "proof_watermark": 0,
                                "proof_watermark_state": "reachable", "request_payload_hash": digest,
                                "delivery_status": "committed_pending_proof"}})
                    await store.append_session_event(stream_id, event, identity=_identity_key(event), limit=500)
            return await store.fetch_session_event_tail(stream_id, limit=11)
        finally:
            store.stop()

    original = asyncio.run(seed_or_read(True))
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
        for before in (None, original[6]["daemon_seq"]):
            # This fixture already has an independent durable source above.
            # The general probe also fetches inspect_stream, whose unsplit raw
            # events intentionally exceed its client ceiling in these cases.
            events, sizes = [], []
            with probe.connect(f"ws://localhost:{port}", open_timeout=5.0) as ws:
                ws.send(json.dumps({
                    "type": "hello", "request_id": "oversized-hello",
                    "subscribe": {"include_subagents": True, "events_mode": events_mode},
                }))
                assert probe._recv_reply(ws, timeout=5.0)["type"] == "hello"
                assert probe._recv_reply(ws, timeout=5.0)["type"] == "snapshot"
                ws.send(json.dumps({
                    "type": "request_stream_events", "request_id": "oversized-events",
                    "stream_id": stream_id, "limit": 11, "before_daemon_seq": before,
                }))
                while True:
                    wire = ws.recv(timeout=5.0)
                    reply = json.loads(wire)
                    if reply.get("type") in probe._PUSH_TYPES:
                        continue
                    assert reply["request_id"] == "oversized-events"
                    assert reply["type"] in {"request_stream_events.chunk", "request_stream_events.ok"}, reply
                    sizes.append(len(wire.encode("utf-8")))
                    events.extend(reply["events"])
                    if reply["type"] == "request_stream_events.ok":
                        break
            events.sort(key=lambda e: e["daemon_seq"])
            expected = [e for e in original if before is None or e["daemon_seq"] < before]
            assert reply["complete"] is True
            assert max(sizes) <= STREAM_EVENTS_FRAME_BUDGET_BYTES
            assert [e["daemon_seq"] for e in events] == [e["daemon_seq"] for e in expected]
            assert probe.assert_client_consumable(events, min_events=1) == []
            for event, source in zip(events, expected):
                if source["daemon_seq"] == original[5]["daemon_seq"]:
                    assert "[Event truncated for backfill" in event["text"]
                    for key in ("daemon_seq", "stream_id", "kind", "timestamp"):
                        assert event[key] == source[key]
                    for key in ("jsonl_record_uuid", "jsonl_event_index", "source", "transport", "sender"):
                        assert event["raw"][key] == source["raw"][key]
                    if oversized_field in {"text", "peer_payload_large"}:
                        assert event["text"].startswith('雪😀\n"\\')
                    else:
                        assert event["text"].startswith("m5")
                    if oversized_field in {"peer_payload", "peer_payload_large"}:
                        assert event["raw"]["peer_payload"] == event["text"]
                else:
                    assert event == source
    finally:
        terminate_process_group(proc)
    assert asyncio.run(seed_or_read(False)) == original


def test_restore_status_broadcast_is_a_push_not_a_reply():
    """With automatic restore on, the daemon broadcasts its status to every client;
    a probe waiting for a reply must skip that frame like any other push."""
    from tools import client_contract_probe

    assert "assistant.restore.changed" in client_contract_probe._PUSH_TYPES


@asynccontextmanager
async def _contract_daemon(env, tmp_path, *, interleaved=None):
    """A real wire/Store/Comms fixture; only provider pane I/O is synthetic."""
    from uuid import uuid4

    from _shared import operator_auth
    from comms import Comms
    from notification_answer_fixture import Provider
    from spawnctl import SpawnCtl
    from work_lanes_projection import WorkLanesInventory

    store = env.store
    host = "fixture"
    name = f"v2-client-contract-{uuid4().hex}"
    stream = f"{host}:{name}"
    pastes = []

    async def append(identity, text, kind="USER"):
        event = _ev(identity, text, stream_id=stream)
        event.update(kind=kind, provider="codex", session_id=target["session_generation"])
        seq = await store.append_session_event(stream, event, identity=identity, limit=100)
        assert type(seq) is int
        await server.broadcast({"type": "chat.event", "event": {**event, "daemon_seq": seq}})
        return seq

    class ContractProvider(Provider):
        async def paste(self, pane, text):
            assert pane == name, "fixture provider refuses other panes"
            pastes.append(text)
            if interleaved is not None:
                # The provider is reached after both history pages and before
                # the durable echo; append() also sends the real live push.
                interleaved.append(await append("interleaved", "synthetic live event", "ASSIST_TEXT"))
            await append("echo:" + str(len(pastes)), text)

    provider = ContractProvider(store, host=host)
    sessions = Sessions(store, tmux=provider, local_host=host)
    target = await sessions.open(host, name, provider="codex", visibility="visible")
    hidden = await sessions.open(host, "v2-hidden-control", provider="codex", visibility="hidden")
    lane = (await env.adopt(key="stream:protocol", state="paused", owner="operator"))["lane"]
    comms = Comms(store, sessions, SpawnCtl(store, sessions, tmux=provider),
                  attachment_root=tmp_path / "attachments")
    server = Server(host="127.0.0.1", port=0, store=store, sessions=sessions, comms=comms, local_host=host)
    server.work_lanes = WorkLanesInventory(store, sessions, server.broadcast)
    registry = operator_auth.OperatorCredentialRegistry(tmp_path / "auth" / "credentials.json")
    credential_id, envelope = registry.issue("pentacle", label="Example Presentation")
    envelope_path = tmp_path / "operator-envelope"
    fd = os.open(envelope_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(envelope)
    server.operator_credential_registry = registry
    try:
        port = await server.bind()
        seed = [await append(f"seed-{i}", "synthetic history") for i in range(4)]
        yield (server, f"ws://127.0.0.1:{port}", stream, target["session_generation"],
               hidden["stream_id"], lane["lane_id"], registry, credential_id, envelope_path, seed, append, pastes)
    finally:
        try:
            await server.close()
        finally:
            try:
                registry.revoke(credential_id)
            finally:
                envelope_path.unlink()


@pytest.mark.parametrize("live_only, interleave", [
    pytest.param(False, False, id="backfill"),
    pytest.param(True, False, id="live-only-control"),
    pytest.param(False, True, id="interleaved-before-echo"),
])
def test_independent_client_contract_journey(tmp_path, monkeypatch, capsys, live_only, interleave):
    import independent_client_example as example
    from test_work_lanes import run

    async def journey(env):
        buffered = []
        async with _contract_daemon(env, tmp_path, interleaved=buffered if interleave else None) as fixture:
            server, url, stream, generation, hidden, lane, _, _, key_file, seed, append, pastes = fixture
            state = tmp_path / "checkpoint.json"
            options = ["--url", url, "--credential-file", str(key_file), "--stream", stream,
                       "--generation", generation, "--checkpoint", str(state)]
            assert await asyncio.to_thread(example.main, ["start", *options]) == 0
            start_counts = json.loads(capsys.readouterr().out)
            assert start_counts == {"snapshot_sessions": 1, "active_sessions": 1, "lanes": 1,
                                    "newer_rows": 2, "older_rows": 2, "send_results": 1,
                                    "live_echoes": 1}
            cursor = json.loads(state.read_text())
            assert len(buffered) == int(interleave)
            first_rows = [*seed, *buffered]
            assert cursor["sequences"] == [*first_rows, cursor["anchor"]]
            send_seq = cursor["sequences"][-1]
            assert cursor["anchor"] == send_seq
            async with asyncio.timeout(3):
                while server._clients:
                    await asyncio.sleep(0.01)  # Server has consumed the close.
            assert len(pastes) == 1
            missing = [await append(f"offline-{i}", "synthetic offline event", "ASSIST_TEXT")
                       for i in range(3)]
            durable = await env.store.fetch_session_event_tail(stream, limit=20)
            assert [e["daemon_seq"] for e in durable] == [*first_rows, send_seq, *missing]
            assert sum(e["text"] == example.FIXTURE_INPUT for e in durable) == 1
            assert await env.store.fetch_session_event_tail(hidden, limit=20) == []
            assert (await server.work_lanes.current())["lanes"][0]["lane_id"] == lane

            if live_only:
                from types import SimpleNamespace

                # Receive a real new live push after reconnect, without
                # requesting history. It is not the intervening offline gap.
                async def read_only_new_pushes(client, anchor):
                    live_seq = await append("live-control", "synthetic new live push", "ASSIST_TEXT")
                    while live_seq not in client.events:
                        await client.receive()
                    return client.events.copy()

                monkeypatch.setattr(example, "recover_history", read_only_new_pushes)
                counts = await example.run(
                    SimpleNamespace(phase="resume", url=url, stream=stream, generation=generation,
                                    checkpoint=state), example.load_credential(key_file),
                )
                expected_tail = (await env.store.fetch_session_event_tail(stream, limit=1))[0]["daemon_seq"]
            else:
                assert await asyncio.to_thread(example.main, ["resume", *options]) == 0
                counts = json.loads(capsys.readouterr().out)
                expected_tail = missing[-1]

            assert counts["resumed_sessions"] == 1
            assert len(pastes) == 1
            checkpoint = json.loads(state.read_text())
            assert checkpoint["anchor"] == expected_tail
            found = checkpoint["sequences"]
            expected = [*first_rows, send_seq, *missing]
            if live_only:
                expected.append(expected_tail)

            def check_order():
                assert found == expected, "offline durable events missing or out of order"

            if live_only:
                assert counts["new_rows"] == 1
                assert found == [*first_rows, send_seq, expected_tail]
                with pytest.raises(AssertionError, match="offline durable events missing"):
                    check_order()
            else:
                check_order()
                assert counts["new_rows"] == 3
                # A second reconnect replays the last identity again; it
                # must not create a new row or submit another turn.
                assert await asyncio.to_thread(example.main, ["resume", *options]) == 0
                assert json.loads(capsys.readouterr().out)["new_rows"] == 0
                assert json.loads(state.read_text())["sequences"] == expected
                assert len(pastes) == 1

    run(journey, str(tmp_path / "sessions.db"))


@pytest.mark.parametrize("case", ["missing", "invalid", "display-name", "revoked"])
def test_independent_client_contract_rejects_untrusted_inventory(tmp_path, case):
    import independent_client_example as example
    from test_work_lanes import run
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    async def check(env):
        async with _contract_daemon(env, tmp_path) as fixture:
            _, url, stream, _, _, _, registry, cid, key_file, _, _, _ = fixture
            credential = example.load_credential(key_file)
            # First establish that this same endpoint and credential work.
            async with example.open_client(url, credential, stream=stream) as client:
                assert client.snapshot["sessions"][0]["stream_id"] == stream
                assert client.snapshot["work_lanes"]["lanes"]
            if case == "revoked":
                registry.revoke(cid)
            async with connect(url, origin=example.FIXTURE_ORIGIN, proxy=None, open_timeout=3) as wire:
                welcome = json.loads(await wire.recv())
                hello = example.authenticated_hello(welcome, credential, "fixture-hello")
                if case == "missing":
                    del hello["auth_v2"]
                    hello["client"] = "Example Presentation"
                elif case == "invalid":
                    hello["auth_v2"]["proof"] = "A" * 43
                elif case == "display-name":
                    hello["client"] = "Example Presentation"
                await wire.send(json.dumps(hello))
                expected = "authentication_required" if case == "missing" else "operator_auth_invalid"
                first = json.loads(await asyncio.wait_for(wire.recv(), 3))
                assert first == {"type": "hello.error", "error_code": expected, "request_id": "fixture-hello"}
                # A denied peer neither sees a bootstrap projection nor can
                # fetch inventory, protected history or lanes explicitly.
                for verb in ("list_sessions", "request_stream_events", "work_lanes.list"):
                    try:
                        await wire.send(json.dumps({"type": verb, "request_id": verb, "stream_id": stream}))
                        reply = json.loads(await asyncio.wait_for(wire.recv(), 3))
                    except ConnectionClosed:
                        break
                    assert reply["type"] == f"{verb}.error"
                    assert reply["request_id"] == verb
                    assert reply["error_code"] in ("authentication_required", "operator_auth_invalid")
                    assert not {"sessions", "active", "events", "lanes", "work_lanes"} & reply.keys()
                else:
                    with pytest.raises((TimeoutError, ConnectionClosed)):
                        await asyncio.wait_for(wire.recv(), 0.1)

    run(check, str(tmp_path / "sessions.db"))


@pytest.mark.parametrize("mismatch", ["ordinary-stream", "wrong-generation"])
def test_independent_client_contract_send_is_fixture_only(tmp_path, capsys, mismatch):
    import independent_client_example as example
    from test_work_lanes import run

    async def check(env):
        async with _contract_daemon(env, tmp_path) as fixture:
            _, url, stream, generation, _, _, _, _, key_file, _, _, pastes = fixture
            if mismatch == "ordinary-stream":
                stream = "fixture:v2-ordinary"
            else:
                generation = "different-synthetic-generation"
            result = await asyncio.to_thread(example.main, [
                "start", "--url", url, "--credential-file", str(key_file),
                "--stream", stream, "--generation", generation,
                "--checkpoint", str(tmp_path / "checkpoint.json"),
            ])
            assert result == 1
            captured = capsys.readouterr()
            assert captured.out == ""
            assert captured.err in {
                "Select an explicitly provisioned disposable fixture stream and generation.\n",
                "Selected fixture stream and generation are not visible.\n",
            }
            assert pastes == []

    run(check, str(tmp_path / "sessions.db"))


def test_independent_client_contract_documented_credential_file(tmp_path, capsys):
    import re

    import independent_client_example as example
    from test_work_lanes import run

    root = SERVICE_DIR.parents[1]
    guide = (root / "docs/chat_protocol.md").read_text()
    issuance = re.search(r"<!-- fixture-credential-issuance -->\n```bash\n(.*?)\n```", guide, re.S)
    # Reproduce the former prose instruction literally on the base: direct
    # the whole CLI response to the private envelope file.
    issue_command = issuance[1] if issuance else (
        'umask 077; python services/chat-stream-v2/tools/operator_auth_cli.py '
        '--registry "$FIXTURE_REGISTRY" issue --client-kind pentacle > "$FIXTURE_KEY_FILE"'
    )

    async def check(env):
        async with _contract_daemon(env, tmp_path) as fixture:
            _, url, stream, generation, _, _, registry, _, _, _, _, _ = fixture
            key = tmp_path / "documented-envelope"
            cid_file = tmp_path / "documented-credential-id"
            registry_path = tmp_path / "auth" / "credentials.json"
            before = set(registry.load().credentials)
            process = await asyncio.create_subprocess_exec(
                "bash", "-c", issue_command, cwd=root,
                env={**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
                     "FIXTURE_REGISTRY": str(registry_path), "FIXTURE_KEY_FILE": str(key),
                     "FIXTURE_CREDENTIAL_ID_FILE": str(cid_file)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            issued = set(registry.load().credentials) - before
            try:
                assert process.returncode == 0 and not stdout and not stderr
                assert key.stat().st_mode & 0o777 == 0o600
                result = await asyncio.to_thread(example.main, [
                    "start", "--url", url, "--credential-file", str(key),
                    "--stream", stream, "--generation", generation,
                    "--checkpoint", str(tmp_path / "documented-checkpoint"),
                ])
                assert result == 0, "documented credential file must authenticate the example"
                captured = capsys.readouterr()
                assert not captured.err and json.loads(captured.out)["send_results"] == 1
                assert cid_file.stat().st_mode & 0o777 == 0o600
                assert len(issued) == 1 and cid_file.read_text().strip() in issued
            finally:
                for credential_id in issued:
                    registry.revoke(credential_id)
                key.unlink(missing_ok=True)
                cid_file.unlink(missing_ok=True)

    run(check, str(tmp_path / "sessions.db"))
