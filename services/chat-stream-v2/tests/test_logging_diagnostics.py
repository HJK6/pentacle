"""Exercise the production entrypoints' logging in fresh non-UTC processes."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


SERVICE = Path(__file__).resolve().parents[1]
RECORD = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z) (DEBUG|INFO|WARNING|ERROR) (\S+) (.*)$')


def invoke(entrypoint: str, level: str = 'DEBUG', preconfigured: bool = False):
    script = f'''
import asyncio, importlib, logging, time
entry = importlib.import_module({entrypoint!r})
if {preconfigured!r}:
    logging.basicConfig(level=logging.ERROR, format="OWNED %(levelname)s %(message)s")
original_handlers = tuple(logging.getLogger().handlers)
async def probe(*args):
    entry.log.debug("startup probe")
    entry.log.warning("disconnect probe", extra={{"payload": "PRIVATE_PAYLOAD", "token": "PRIVATE_TOKEN"}})
    await asyncio.sleep(0.025)
    entry.log.warning("retry probe")
    try:
        raise ValueError("traceback probe")
    except ValueError:
        entry.log.exception("exception probe")
    return 0
if {entrypoint!r} == 'main':
    entry.run = probe
    result = entry.main(['--log-level', {level!r}])
else:
    entry.Satellite.run_forever = probe
    result = entry.main([])
assert result == 0
assert len(logging.getLogger().handlers) == 1
if {preconfigured!r}:
    assert tuple(logging.getLogger().handlers) == original_handlers
    assert logging.getLogger().level == logging.ERROR
'''
    env = {**os.environ, 'TZ': 'Pacific/Honolulu', 'PYTHONPATH': str(SERVICE),
           'PENTACLE_HOST_ID': 'test-host', 'PENTACLE_SATELLITE_HOST': 'test-host',
           'PENTACLE_SATELLITE_LOG_LEVEL': level}
    return subprocess.run([sys.executable, '-c', script], env=env, text=True,
                          capture_output=True, timeout=20, check=True).stderr


@pytest.mark.parametrize('entrypoint', ['main', 'satellite'])
def test_entrypoint_emits_utc_milliseconds_levels_logger_and_traceback(entrypoint):
    before = datetime.now(timezone.utc)
    output = invoke(entrypoint)
    after = datetime.now(timezone.utc)
    records = [RECORD.fullmatch(line) for line in output.splitlines()]
    records = [match for match in records if match and match[3].startswith('chat_streamd_v2')]
    assert len(records) == 4, output
    times = [datetime.fromisoformat(match[1]) for match in records]
    assert before.timestamp() - 0.001 <= times[0].timestamp() <= after.timestamp()
    assert times[2] > times[1]
    assert [match[2] for match in records] == ['DEBUG', 'WARNING', 'WARNING', 'ERROR']
    expected_logger = 'chat_streamd_v2' + ('.satellite' if entrypoint == 'satellite' else '')
    assert all(match[3] == expected_logger for match in records)
    assert 'Traceback (most recent call last):' in output
    assert 'ValueError: traceback probe' in output
    assert 'PRIVATE_PAYLOAD' not in output and 'PRIVATE_TOKEN' not in output


@pytest.mark.parametrize('entrypoint', ['main', 'satellite'])
def test_entrypoint_respects_configured_level(entrypoint):
    output = invoke(entrypoint, level='ERROR')
    assert 'startup probe' not in output and 'disconnect probe' not in output and 'retry probe' not in output
    assert output.count('exception probe') == 1


@pytest.mark.parametrize('entrypoint', ['main', 'satellite'])
def test_entrypoint_preserves_existing_handler_formatter_and_level(entrypoint):
    output = invoke(entrypoint, preconfigured=True)
    assert output.startswith('OWNED ERROR exception probe\n')
    assert 'disconnect probe' not in output


# Closed schemas are deliberately independent of the implementation's helpers.
# Every fixture below reaches a production hook; none calls the emitter.
COMMON = frozenset({
    "schema", "event", "conn_id", "age_ms", "transport", "tls", "client_kind",
    "client_name", "client_metadata_source", "app_build", "stream_id",
})
QUEUE = frozenset({"queue_depth", "queue_max", "queue_peak", "queued_by_type"})
TIMING = frozenset({
    "last_rx_age_ms", "last_tx_age_ms", "last_ping_age_ms", "last_pong_age_ms",
    "send_lock_wait_max_ms", "send_call_max_ms",
})
BUCKETS = frozenset({
    "snapshot", "session.inventory", "work_lanes.inventory", "host.status",
    "working.state", "schedule.inventory", "hosts.stats", "limits.update",
    "chat.event", "pong", "other",
})
COUNTERS = frozenset({
    "broadcast_enqueued", "broadcast_sent", "broadcast_sent_bytes", "direct_sent",
    "direct_sent_bytes", "coalesced", "deduped",
})
AUTH = frozenset({"auth_method", "auth_stage", "auth_elapsed_ms", "auth_suppressed"})
EVENT_FIELDS = {
    "connect": {"queue_max"},
    "auth_ok": AUTH | {"events_mode", "snapshot_requested", "work_lanes_v1"},
    "auth_fail": AUTH | {"reason"},
    "slow_consumer": QUEUE | TIMING | {"traffic", "phase", "episode", "episode_ms", "pressure_episodes_suppressed"},
    "force_close": QUEUE | TIMING | {"traffic", "initiator", "cause", "cause_source", "close_code", "close_reason"},
    "close": QUEUE | TIMING | {
        "traffic", "initiator", "termination", "close_code", "close_reason", "close_sent_code",
        "close_received_code", "close_sent_reason", "close_received_reason", "duration_ms", "auth_state",
        "rx_messages", "rx_bytes", "tx_messages", "tx_bytes", "queue_snapshot_age_ms",
        "auth_suppressed_total", "pressure_episodes_suppressed_total",
    },
}
CLOSE_REASONS = frozenset({
    "empty", "normal", "going_away", "slow_consumer", "liveness_force_close",
    "focused_heartbeat_timeout", "keepalive_ping_timeout", "redacted", "unknown",
})


def _uint(value):
    assert type(value) is int and value >= 0, f"expected a nonnegative int, got {value!r}"


def assert_conn_diag_schema(payload):
    """Assert exact frozen fields, JSON types, enumerations and counter identities."""
    import uuid

    assert type(payload) is dict
    event = payload["event"]
    assert event in EVENT_FIELDS
    assert set(payload) == COMMON | EVENT_FIELDS[event], (event, set(payload) ^ (COMMON | EVENT_FIELDS[event]))
    assert type(payload["schema"]) is int and payload["schema"] == 1
    assert re.fullmatch(r"[0-9a-f]{32}", payload["conn_id"])
    assert uuid.UUID(hex=payload["conn_id"]).version == 4
    _uint(payload["age_ms"])
    assert payload["transport"] in {"unix", "loopback", "tailnet", "other", "unknown"}
    assert type(payload["tls"]) is bool
    assert payload["client_kind"] in {"mobile", "web", "desktop", "cli", "service", "unknown"}
    assert payload["client_name"] in {"pentacle-mobile", "pentacle-web", "pentacle", "agent-orch", "system-producer", "unknown"}
    assert payload["client_metadata_source"] in {"unavailable", "hello_claim", "verified_credential"}
    build = payload["app_build"]
    assert build is None or (type(build) is str and re.fullmatch(
        r"(?:[0-9a-f]{7,40}|[0-9]{1,10}|[0-9]{1,10}\.[0-9]{1,10}\.[0-9]{1,10}(?:\+[0-9]{1,10})?)", build
    ))
    stream = payload["stream_id"]
    assert stream is None or (type(stream) is str and len(stream) <= 128 and re.fullmatch(r"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+", stream))
    if event == "connect":
        assert payload["age_ms"] == 0
        assert payload["client_kind"] == payload["client_name"] == "unknown"
        assert payload["client_metadata_source"] == "unavailable"
        assert payload["app_build"] is payload["stream_id"] is None
        _uint(payload["queue_max"])
    if event in {"auth_ok", "auth_fail"}:
        assert payload["auth_method"] in {"operator_v2", "scoped_v2", "seat_token", "system_token", "local_admin", "loopback", "none"}
        assert payload["auth_stage"] in {"hello", "request", "revalidation"}
        _uint(payload["auth_elapsed_ms"])
        _uint(payload["auth_suppressed"])
        if event == "auth_ok":
            assert payload["events_mode"] in {"full", "summary", "unknown"}
            assert payload["snapshot_requested"] is None or type(payload["snapshot_requested"]) is bool
            assert payload["work_lanes_v1"] is None or type(payload["work_lanes_v1"]) is bool
        else:
            assert payload["reason"] in {
                "absent", "malformed", "expired", "wrong-seat", "internal-error", "operator_auth_invalid",
                "authentication_required", "operator_auth_required", "system_producer_auth_required", "revoked", "tls_required",
            }
    if event in {"slow_consumer", "force_close", "close"}:
        for key in QUEUE - {"queued_by_type"}:
            _uint(payload[key])
        assert type(payload["queued_by_type"]) is dict
        assert set(payload["queued_by_type"]) == BUCKETS
        for count in payload["queued_by_type"].values():
            _uint(count)
        assert sum(payload["queued_by_type"].values()) == payload["queue_depth"]
        assert payload["queue_peak"] >= payload["queue_depth"]
        assert type(payload["traffic"]) is dict and set(payload["traffic"]) == BUCKETS
        for bucket in payload["traffic"].values():
            assert type(bucket) is dict and set(bucket) == COUNTERS
            for count in bucket.values():
                _uint(count)
        for key in TIMING:
            if key.startswith("last_") and payload[key] is None:
                continue
            _uint(payload[key])
    if event == "slow_consumer":
        assert payload["phase"] in {"enter", "recover"}
        for key in ("episode", "episode_ms", "pressure_episodes_suppressed"):
            _uint(payload[key])
        assert payload["episode"] >= 1
    if event in {"force_close", "close"}:
        assert payload["initiator"] in ({"server", "peer"} if event == "force_close" else {"server", "peer", "unknown"})
        assert payload["close_reason"] in CLOSE_REASONS
        assert payload["close_code"] is None or type(payload["close_code"]) is int
    if event == "force_close":
        assert payload["cause"] in {"slow_consumer", "liveness"}
        assert payload["cause_source"] in {"server_policy", "peer_close_frame", "protocol_close"}
    if event == "close":
        assert payload["termination"] in {"handshake", "transport_lost", "welcome_failed", "writer_failed", "handler_cancelled", "handler_error"}
        assert payload["auth_state"] in {"never", "accepted", "failed"}
        for side in ("sent", "received"):
            assert payload[f"close_{side}_code"] is None or type(payload[f"close_{side}_code"]) is int
            assert payload[f"close_{side}_reason"] in CLOSE_REASONS
        for key in ("duration_ms", "rx_messages", "rx_bytes", "tx_messages", "tx_bytes", "queue_snapshot_age_ms", "auth_suppressed_total", "pressure_episodes_suppressed_total"):
            _uint(payload[key])
        assert payload["duration_ms"] == payload["age_ms"]
        assert payload["tx_messages"] == sum(b["direct_sent"] + b["broadcast_sent"] for b in payload["traffic"].values())
        assert payload["tx_bytes"] == sum(b["direct_sent_bytes"] + b["broadcast_sent_bytes"] for b in payload["traffic"].values())


def diagnostic_records(caplog):
    import json

    found = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("conn_diag "):
            assert record.name == "chat_streamd_v2.server"
            assert not record.exc_info and not record.stack_info
            assert "\n" not in message and "\r" not in message
            encoded = message[len("conn_diag "):]
            payload = json.loads(encoded, parse_constant=lambda value: pytest.fail(f"non-JSON number {value}"))
            # Compact JSON leaves no insignificant spaces outside strings.
            assert encoded == json.dumps(payload, separators=(",", ":"), allow_nan=False)
            assert_conn_diag_schema(payload)
            expected = "WARNING" if payload["event"] in {"auth_fail", "force_close"} or (
                payload["event"] == "slow_consumer" and payload["phase"] == "enter"
            ) else "INFO"
            assert record.levelname == expected
            found.append((record, payload))
    return found


def test_all_logs_discard_private_wire_values_and_exception_text(caplog, tmp_path):
    import asyncio
    import hashlib
    import json
    import logging

    import server
    from _shared import operator_auth
    from server import Server
    from sessions import Sessions
    from store import Store, STREAM_TOKEN_HASH_VERSION
    from test_slow_consumer_logging import _connection

    async def run():
        caplog.set_level(logging.INFO)
        caplog.set_level(logging.DEBUG, logger=server.log.name)
        # Do not enable the websockets frame-dump DEBUG logger: production INFO
        # is the secrecy boundary and its ordinary output is still captured.
        caplog.set_level(logging.INFO, logger="websockets")
        sentinels = {
            "stream_token": "SYNTHETIC_TOKEN_ZX921",
            "token": "SYNTHETIC_LEGACY_TOKEN_JK551",
            "local_admin_token": "SYNTHETIC_LOCAL_ADMIN_LM733",
            "secret": "SYNTHETIC_SECRET_VV119",
            "udid": "SYNTHETIC_UDID_DD481",
            "text": "SYNTHETIC_PAYLOAD_AA351",
            "file_content": "SYNTHETIC_FILE_CONTENT_MM257",
            "file_path": "/synthetic/private/path_PP709",
            "claim": "SYNTHETIC_UNVERIFIED:SEAT_WW645",
            "request": "SYNTHETIC_REQUEST_GG889",
            "client": "SYNTHETIC_CLIENT_BB173\r\nforged log line",
            "verb": "SYNTHETIC_UNKNOWN_VERB_CC391",
            "reason": "SYNTHETIC_CLOSE_REASON_HH447\r\nforged close line",
            "ip": "127.91.82.73",
        }
        sentinels["token_hash"] = hashlib.sha256(sentinels["stream_token"].encode()).hexdigest()
        store = Store(str(tmp_path / "privacy.db"))
        store.start()
        try:
            await store.open_session("fixture", "verified-seat", provider="codex", pane_status="pane_alive")
            await store.grant_stream_token("fixture", "verified-seat", hashlib.sha256(b"fixture-valid-seat-token").hexdigest(), STREAM_TOKEN_HASH_VERSION)
            sessions = Sessions(store, local_host="fixture")
            registry = operator_auth.OperatorCredentialRegistry(tmp_path / "credentials.json")
            _cid, envelope = registry.issue("pentacle", label="fixture")
            identity = operator_auth.decode_envelope(envelope)
            daemon = Server(store=store, sessions=sessions)
            daemon.operator_credential_registry = registry
            async with _connection(daemon=daemon) as (_d, peer, _queue, handler):
                peer.remote_address = (sentinels["ip"], 54991)
                welcome = json.loads(peer.sent[0])
                sentinels["nonce"] = welcome["auth"]["operator"]["nonce"]
                sentinels["credential"] = identity["credential_id"]
                sentinels["proof"] = operator_auth.make_proof(
                    identity["proof_key"], operator_auth.encode_b64url(b"x" * operator_auth.AUTH_NONCE_BYTES), identity["credential_id"], identity["client_kind"],
                )
                sentinels["proof_key"] = operator_auth.encode_b64url(identity["proof_key"])
                await daemon._serve(peer, json.dumps({
                    "type": "hello", "client": "pentacle", "request_id": sentinels["request"],
                    "auth_v2": {"scheme": operator_auth.AUTH_SCHEME, "credential_id": identity["credential_id"], "proof": sentinels["proof"]},
                }))
                assert json.loads(peer.sent[-1])["error_code"] == "operator_auth_invalid"
                await daemon._serve(peer, json.dumps({
                    "type": "hello", "client": sentinels["client"], "from_stream_id": sentinels["claim"],
                    **{k: sentinels[k] for k in ("stream_token", "token", "local_admin_token", "secret", "udid", "file_content", "file_path")},
                    "build_sha": sentinels["token_hash"], "app_build": {"private": sentinels["secret"]},
                    "build_number": sentinels["udid"], "request_id": sentinels["request"],
                    "subscribe": {"mode": "rpc", "snapshot": False},
                }))
                assert json.loads(peer.sent[-1])["type"] == "ready"  # unchanged loopback exemption
                await daemon._serve(peer, json.dumps({"type": sentinels["verb"], "request_id": sentinels["request"], "text": sentinels["text"]}))
                assert json.loads(peer.sent[-1])["error_code"] == "unsupported_in_v2"
                # An injected external business callback exercises the real
                # generic dispatch exception boundary, including its old log.
                async def broken_business_handler(_msg):
                    raise RuntimeError(sentinels["file_content"] + sentinels["file_path"])
                daemon.handlers["fixture-failure"] = broken_business_handler
                await daemon._serve(peer, json.dumps({"type": "fixture-failure", "request_id": sentinels["request"], "text": sentinels["text"]}))
                assert json.loads(peer.sent[-1])["error_code"] == "internal_error"
                await daemon._serve(peer, json.dumps({
                    "type": "notification.resolve", "request_id": sentinels["request"],
                    "notification_id": sentinels["udid"], "text": sentinels["text"],
                }))
                # A real credential owner survives while the failed arbitrary
                # claim above must never become stream identity or telemetry.
                await daemon._serve(peer, json.dumps({
                    "type": "hello", "client": "agent-orch", "stream_token": "fixture-valid-seat-token",
                    "from_stream_id": "fixture:verified-seat", "build_sha": "abc1234", "subscribe": {"mode": "rpc", "snapshot": False},
                }))
                peer.finish(reason=sentinels["reason"])
                await handler
            # Scan ALL captured logger output, not just conn_diag messages.
            output = caplog.text
            for key, value in sentinels.items():
                assert value not in output, f"private {key} escaped through captured logger output"
                for physical_part in value.splitlines():
                    if physical_part and physical_part != "forged log line":
                        assert physical_part not in output, f"private {key} line escaped"
            records = diagnostic_records(caplog)
            assert records, "the real connection journey must emit conn_diag records"
            payloads = [p for _r, p in records]
            assert any(p["event"] == "auth_fail" for p in payloads)
            assert any(p["stream_id"] == "fixture:verified-seat" and p["app_build"] == "abc1234" for p in payloads)
            assert payloads[-1]["event"] == "close" and payloads[-1]["close_reason"] == "redacted"
            poisoned = [p for p in payloads if p["event"] in {"auth_ok", "auth_fail"} and p["client_name"] == "unknown"]
            assert poisoned and all(p["app_build"] is None and p["stream_id"] is None for p in poisoned)
        finally:
            store.stop()
    asyncio.run(run())


@pytest.mark.parametrize("fields,expected", [
    ({"build_sha": "abc1234", "app_build": "1.2.3", "build_number": "42"}, "abc1234"),
    ({"build_sha": "A" * 7, "app_build": "1.2.3+4", "build_number": "42"}, "1.2.3+4"),
    ({"app_build": "invalid", "build_number": "00042"}, "00042"),
    ({"build_sha": "f" * 40}, "f" * 40),
    ({"build_sha": "f" * 41}, None),
    ({"build_sha": "f" * 6}, None),
    ({"app_build": "1.2.3-private-branch"}, None),
    ({"app_build": "12345678901"}, None),
    ({"app_build": "1.2.3+12345678901"}, None),
    ({"app_build": "１２３"}, None),
    ({"app_build": 123, "build_number": False}, None),
    ({"app_build": {"value": "1.2.3"}, "build_number": ["42"]}, None),
    ({"app_build": "1.2.3\n"}, None),
    ({"app_build": "1.2.3", "token": "1.2.3"}, None),
    ({"build_sha": "abc1234", "auth_v2": {"proof": "abc1234"}}, None),
])
def test_hello_release_metadata_is_typed_bounded_and_credential_safe(caplog, fields, expected):
    import asyncio
    import json
    import logging
    from test_slow_consumer_logging import _connection

    async def run():
        caplog.set_level(logging.INFO)
        async with _connection() as (daemon, peer, _queue, _handler):
            await daemon._serve(peer, json.dumps({
                "type": "hello", "client": "pentacle-web", "subscribe": {"snapshot": False, "mode": "rpc"}, **fields,
            }))
        payloads = [p for _r, p in diagnostic_records(caplog)]
        assert payloads, "accepted hello must project bounded metadata onto lifecycle records"
        close = [p for p in payloads if p["event"] == "close"][0]
        assert close["app_build"] == expected
        assert close["client_kind"] == "web"
        assert close["client_name"] == "pentacle-web"
        assert close["client_metadata_source"] == "hello_claim"
    asyncio.run(run())


@pytest.mark.parametrize("level,expected_events", [
    ("INFO", {"connect", "auth_ok", "auth_fail", "slow_consumer", "force_close", "close"}),
    ("WARNING", {"auth_fail", "slow_consumer", "force_close"}),
    ("ERROR", set()),
])
def test_connection_diagnostics_respect_existing_logger_levels(caplog, level, expected_events):
    import asyncio
    import json
    import logging
    import server
    from test_slow_consumer_logging import _connection

    async def run():
        caplog.set_level(getattr(logging, level), logger=server.log.name)
        async with _connection(maxsize=3) as (daemon, peer, _queue, handler):
            await daemon._serve(peer, json.dumps({"type": "hello", "client": "pentacle", "subscribe": {"snapshot": False, "mode": "rpc"}}))
            await daemon._serve(peer, json.dumps({"type": "ping", "stream_token": 17}))
            for i in range(4):
                daemon._enqueue(peer, "chat.event", json.dumps({"type": "chat.event", "seq": i}))
            await handler
        events = {p["event"] for _r, p in diagnostic_records(caplog)}
        assert events == expected_events
    asyncio.run(run())


def test_connection_diagnostics_use_production_formatter_single_physical_line(caplog):
    import asyncio
    import io
    import json
    import logging
    from logging_config import UTCFormatter
    import server
    from test_slow_consumer_logging import _connection

    async def run():
        output = io.StringIO()
        sink = logging.StreamHandler(output)
        sink.setFormatter(UTCFormatter("%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
        caplog.set_level(logging.INFO, logger=server.log.name)
        server.log.addHandler(sink)
        try:
            async with _connection() as (daemon, peer, _queue, _handler):
                await daemon._serve(peer, json.dumps({"type": "hello", "client": "malformed\r\nFORGED", "subscribe": {"snapshot": False, "mode": "rpc"}}))
            records = diagnostic_records(caplog)
            assert records, "real accepted connections must produce structured records"
            lines = output.getvalue().splitlines()
            diag_lines = [line for line in lines if " conn_diag " in line]
            assert len(diag_lines) == len(records)
            for line in diag_lines:
                match = RECORD.fullmatch(line)
                assert match and match[3] == server.log.name
                assert_conn_diag_schema(json.loads(match[4][len("conn_diag "):]))
            assert "FORGED" not in output.getvalue()
        finally:
            server.log.removeHandler(sink)
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["serialization", "emission"])
def test_diagnostic_failure_does_not_change_auth_or_wire_outcomes(caplog, monkeypatch, failure):
    import asyncio
    import json
    import logging
    import server
    from test_slow_consumer_logging import _connection

    async def run():
        caplog.set_level(logging.INFO)
        original_dumps = json.dumps
        failures = []
        def broken_dumps(value, *args, **kwargs):
            if isinstance(value, dict) and value.get("schema") == 1 and value.get("event") in EVENT_FIELDS:
                failures.append("serialization")
                raise ValueError("synthetic diagnostic serialization failure")
            return original_dumps(value, *args, **kwargs)
        class BrokenSink(logging.Handler):
            def emit(self, record):
                if record.getMessage().startswith("conn_diag "):
                    failures.append("emission")
                    raise OSError("synthetic diagnostic handler failure")
        sink = BrokenSink()
        with monkeypatch.context() as patch:
            if failure == "serialization":
                patch.setattr(server.json, "dumps", broken_dumps)
            else:
                server.log.addHandler(sink)
            try:
                async with _connection() as (daemon, peer, queue, _handler):
                    await daemon._serve(peer, original_dumps({"type": "hello", "client": "pentacle", "subscribe": {"snapshot": False, "mode": "rpc"}}))
                    await daemon._serve(peer, original_dumps({"type": "ping", "request_id": "fixture-ping"}))
                    assert [json.loads(f)["type"] for f in peer.sent] == ["welcome", "ready", "pong"]
                    assert json.loads(peer.sent[-1])["request_id"] == "fixture-ping"
                    assert daemon._client_events_mode[peer] == "full"
                    assert queue.maxsize == server.CLIENT_SEND_QUEUE_MAX
            finally:
                server.log.removeHandler(sink)
        # The negative control must actually reach the injected failure.
        assert failures, "real lifecycle hooks did not reach diagnostic fault injection"
        assert not diagnostic_records(caplog), "diagnostic injection did not intercept the actual emitter"
    asyncio.run(run())
