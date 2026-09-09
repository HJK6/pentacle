from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from queue import Queue

import pytest
import websockets

import agent_orch.wsclient as wsclient
from agent_orch.config import Config
from agent_orch.wsclient import (
    PendingRpc,
    RetryPolicy,
    SnapshotTimeout,
    WebsocketClient,
    _is_rpc_retry_eligible,
    _retry_deadline,
    _retry_wait_timeout,
    _rpc_retry_policy_from_env,
    asset_comment_resolve_once,
    asset_comments_list_once,
    asset_get_once,
    asset_publish_once,
    await_report_once,
    fetch_blob_once,
    fetch_snapshot,
    inbox_once,
    notification_await_once,
    notification_create_once,
    notification_resolve_once,
    notification_resolve_by_dedup_once,
    prompt_ask_once,
    prompt_answer_once,
    prompt_cancel_once,
    rename_once,
    send_receipt_once,
    send_once,
    spec_update_once,
    spawn_once,
    tell_once,
)


def test_park_retry_eligible_but_unpark_is_single_shot() -> None:
    assert _is_rpc_retry_eligible({"type": "park", "request_id": "park-r"})
    assert not _is_rpc_retry_eligible({"type": "unpark", "request_id": "unpark-r"})


def test_prompt_terminal_verbs_are_retry_eligible() -> None:
    assert _is_rpc_retry_eligible({"type": "prompt.answer", "request_id": "answer-r"})
    assert _is_rpc_retry_eligible({"type": "prompt.cancel", "request_id": "cancel-r"})


def test_prompt_and_notification_resolution_helpers_carry_agent_identity(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hosta:relay-seat")
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "relay-token")
    seen = []

    async def fake_one_shot(_config, payload, *, prefix, timeout, from_stream_id=None, **kwargs):
        seen.append((prefix, dict(payload), from_stream_id))
        return {"type": f"{prefix}.ok"}

    monkeypatch.setattr(wsclient, "_one_shot_rpc", fake_one_shot)

    async def run() -> None:
        await prompt_answer_once(object(), {"type": "prompt.answer", "question_id": "q-1"})
        await prompt_cancel_once(object(), {"type": "prompt.cancel", "question_id": "q-2"})
        await notification_resolve_by_dedup_once(
            object(),
            {
                "type": "notification.resolve_by_dedup",
                "producer": "memory-cadence",
                "dedup_key": "cleanup:1",
            },
        )
        await notification_resolve_once(
            object(),
            {
                "type": "notification.resolve",
                "notification_id": "n-1",
                "action_kind": "ack",
            },
        )

    asyncio.run(run())
    assert [item[0] for item in seen] == [
        "prompt", "prompt", "notification.resolve_by_dedup", "notification.resolve"
    ]
    for _prefix, payload, stream_id in seen:
        assert stream_id == "hosta:relay-seat"
        assert payload["from_stream_id"] == "hosta:relay-seat"
        assert payload["stream_token"] == "relay-token"


def test_reparent_is_not_retry_eligible_documented_finding() -> None:
    """Characterization (Class 4b documented finding): `reparent` is NOT in
    RPC_RETRY_ELIGIBLE_TYPES and `reparent_once` is a bespoke one-shot with no
    retry loop — a single full-`timeout` attempt, then TimeoutError propagates.
    reparent is final-state idempotent but has no daemon-side request_id dedup,
    so making it retry-eligible would risk double audit/cascade side effects.
    Safe retry is deferred as a follow-up; this test pins the current gap so a
    future change that adds retry must consciously flip it. See spec Retro."""
    assert not _is_rpc_retry_eligible({"type": "reparent", "request_id": "reparent-r"})


def _clear_retry_env(monkeypatch) -> None:
    for name in (
        "AGENT_ORCH_RPC_RETRY_DEADLINE_S",
        "AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS",
        "AGENT_ORCH_RPC_RETRY_BACKOFF_BASE_S",
        "AGENT_ORCH_RPC_RETRY_BACKOFF_CAP_S",
        "AGENT_ORCH_RPC_RETRY_JITTER_FRACTION",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_retry_deadline_gives_each_attempt_its_full_call_budget(monkeypatch) -> None:
    """Regression for the tell/reparent rpc_timeout storm: with no env override
    the total retry deadline must be decoupled from a single call_timeout so all
    `max_attempts` attempts each get their full per-attempt budget. Before the
    fix the total == one call_timeout and `_retry_wait_timeout` starved attempt 1
    to ~call_timeout/attempts (≈10s for the 30s/3-attempt default)."""
    _clear_retry_env(monkeypatch)
    monkeypatch.setattr("agent_orch.wsclient.time.monotonic", lambda: 1000.0)

    call_timeout = 30.0
    policy = _rpc_retry_policy_from_env(call_timeout)
    assert policy.max_attempts == 3
    # Total budget scales with attempts, not one call.
    assert policy.deadline_s == pytest.approx(call_timeout * policy.max_attempts)

    deadline = _retry_deadline(1000.0, policy)
    # Attempt 1 (attempts_left == max_attempts) must still get a full call budget,
    # not deadline/attempts. Red pre-fix: this was ~10s.
    first_attempt_budget = _retry_wait_timeout(deadline, 1, policy.max_attempts)
    assert first_attempt_budget == pytest.approx(call_timeout)
    assert first_attempt_budget >= call_timeout - 0.01


def test_configured_retry_deadline_is_honored_as_total_not_capped_by_call_timeout(monkeypatch) -> None:
    """AGENT_ORCH_RPC_RETRY_DEADLINE_S is an explicit TOTAL budget. It must be
    honored even when larger than a single call_timeout; the old min() against
    one call capped it below the single-call value and made the knob useless."""
    _clear_retry_env(monkeypatch)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", "120")

    policy = _rpc_retry_policy_from_env(30.0)
    assert policy.deadline_s == pytest.approx(120.0)


class StubServer:
    def __init__(self, handler):
        self.handler = handler
        self.queue: Queue[str] = Queue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.url = self.queue.get(timeout=5)
        return self

    def __exit__(self, *_exc):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self):
        async def main():
            async with websockets.serve(self.handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                self.queue.put(f"ws://127.0.0.1:{port}")
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.05)

        asyncio.run(main())


class FakeWebSocket:
    def __init__(self):
        self.sent: list[dict] = []
        self._recv_messages = [
            {"type": "snapshot", "hosts": ["hostc"], "sessions": []},
        ]
        self._iter_sent_snapshot = False
        self.closed = False

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") == "send":
            self._recv_messages.append(
                {
                    "type": "send.result",
                    "request_id": message["request_id"],
                    "delivery": "landed",
                    "attempt": 1,
                }
            )

    async def recv(self):
        if not self._recv_messages:
            raise AssertionError("fake websocket recv exhausted")
        return json.dumps(self._recv_messages.pop(0))

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._iter_sent_snapshot:
            self._iter_sent_snapshot = True
            return json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []})
        while not self.closed:
            await asyncio.sleep(0)
        raise StopAsyncIteration


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    def __await__(self):
        async def ready():
            return self.ws

        return ready().__await__()

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *_exc):
        await self.ws.close()


def recording_connect(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return FakeConnect(FakeWebSocket())

    monkeypatch.setattr("agent_orch.wsclient.websockets.connect", connect)
    return calls


def assert_keepalive_kwargs(call, *, ping_interval, ping_timeout, close_timeout):
    kwargs = call["kwargs"]
    assert kwargs["ping_interval"] == ping_interval
    assert kwargs["ping_timeout"] == ping_timeout
    assert kwargs["close_timeout"] == close_timeout


def assert_persistent_subscribe(subscribe):
    assert isinstance(subscribe, dict)
    assert subscribe["include_subagents"] is True


def assert_rpc_subscribe(subscribe):
    assert isinstance(subscribe, dict)
    assert subscribe["snapshot"] is False
    assert subscribe["mode"] == "rpc"
    assert subscribe["include_subagents"] is False
    assert subscribe["events_mode"] == "summary"
    assert subscribe["opened_by_host_ids"] == ["__agent_orch_rpc__"]
    assert subscribe["exclude_event_types"] == [
        "codex.usage",
        "claude.usage",
        "machine.stats",
        "machine.stats.inventory",
    ]


async def rpc_handler(ws):
    hello = json.loads(await ws.recv())
    assert hello["token"] == "tok"
    if hello["subscribe"].get("snapshot") is False:
        assert_rpc_subscribe(hello["subscribe"])
    else:
        assert_persistent_subscribe(hello["subscribe"])
    await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
    await ws.send(
        json.dumps(
            {
                "type": "session.died",
                "host": "hostc",
                "session_name": "codex-x",
                "stream_id": "hostc:codex-x",
                "reason": "pane_pid_gone",
                "last_seen_ts": 1.0,
            }
        )
    )
    async for raw in ws:
        msg = json.loads(raw)
        if msg["type"] == "spawn":
            await ws.send(json.dumps({"type": "spawn.ok", "request_id": msg["request_id"], "session": {"stream_id": "hostc:codex-x", "session_name": "codex-x"}}))
        elif msg["type"] == "send":
            await ws.send(json.dumps({"type": "send.progress", "request_id": msg["request_id"], "msg_id": msg.get("msg_id"), "state": "pre_paste", "attempt": 1}))
            await ws.send(json.dumps({"type": "send.result", "request_id": msg["request_id"], "delivery": "landed", "attempt": 1}))
        elif msg["type"] == "tell":
            await ws.send(json.dumps({"type": "tell.ok", "request_id": msg["request_id"], "tell_id": msg["tell_id"], "ledger_row_id": 1, "delivery_status": "queued"}))
        elif msg["type"] == "upload_prompt_blob_init":
            await ws.send(json.dumps({"type": "upload_prompt_blob.init.ok", "request_id": msg["request_id"]}))
        elif msg["type"] == "upload_prompt_blob_chunk":
            await ws.send(json.dumps({"type": "upload_prompt_blob.ok", "request_id": msg["request_id"], "prompt_blob_sha": "a" * 64, "size_bytes": 5}))
        elif msg["type"] == "close":
            await ws.send(json.dumps({"type": "close.error", "request_id": msg["request_id"], "error": "unknown_session"}))
        elif msg["type"] == "set_visibility":
            await ws.send(
                json.dumps(
                    {
                        "type": "set_visibility.ok",
                        "request_id": msg["request_id"],
                        "session": {"stream_id": f"{msg['host']}:{msg['session_name']}", "visibility": msg["visibility"]},
                    }
                )
            )
        elif msg["type"] == "rename":
            await ws.send(
                json.dumps(
                    {
                        "type": "rename.ok",
                        "request_id": msg["request_id"],
                        "session": {
                            "stream_id": f"{msg['host']}:{msg['session_name']}",
                            "display_name": msg["display_name"],
                            "title": msg["display_name"],
                        },
                    }
                )
            )
        elif msg["type"] == "inspect_stream":
            await ws.send(
                json.dumps(
                    {
                        "type": "inspect_stream.ok",
                        "request_id": msg["request_id"],
                        "stream_id": msg["stream_id"],
                        "session": {"status": "running", "online": True},
                        "recent_events": [],
                        "existing_report": None,
                    }
                )
            )
        elif msg["type"] == "await_report":
            await ws.send(
                json.dumps(
                    {
                        "type": "await_report.ok",
                        "request_id": msg["request_id"],
                        "ok": True,
                        "stream_id": msg["stream_id"],
                        "msg_id": msg["msg_id"],
                        "report_id": "r7",
                        "ledger_row_id": 7,
                        "status": "done",
                    }
                )
            )


def test_websocket_connect_keepalive_defaults_for_all_call_sites(monkeypatch, tmp_path):
    calls = recording_connect(monkeypatch)
    config = Config("ws://example.invalid/ws", "tok", "hostc", tmp_path)

    client = WebsocketClient(config, snapshot_timeout=2, rpc_timeout=2)
    client.start()
    client.stop()

    fetch_snapshot(config, timeout=2)
    asyncio.run(
        send_once(
            config,
            {
                "type": "send",
                "host": "hostc",
                "session_name": "codex-x",
                "text": "hello",
                "request_id": "send-keepalive-defaults",
            },
            timeout=2,
        )
    )

    assert len(calls) == 3
    for call in calls:
        assert call["kwargs"]["max_size"] == 128 * 1024 * 1024
        assert_keepalive_kwargs(call, ping_interval=30.0, ping_timeout=60.0, close_timeout=5.0)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {
                "AGENT_ORCH_WS_PING_INTERVAL_S": "45.5",
                "AGENT_ORCH_WS_PING_TIMEOUT_S": "99999",
                "AGENT_ORCH_WS_CLOSE_TIMEOUT_S": "12.25",
            },
            (45.5, 600.0, 12.25),
        ),
        (
            {
                "AGENT_ORCH_WS_PING_INTERVAL_S": "abc",
                "AGENT_ORCH_WS_PING_TIMEOUT_S": "0",
                "AGENT_ORCH_WS_CLOSE_TIMEOUT_S": "-5",
            },
            (30.0, 60.0, 5.0),
        ),
        (
            # Invariant clamp: a pathological pong-timeout below the ping cadence
            # is raised up to the interval (timeout >= interval) on every site.
            {
                "AGENT_ORCH_WS_PING_INTERVAL_S": "50",
                "AGENT_ORCH_WS_PING_TIMEOUT_S": "10",
            },
            (50.0, 50.0, 5.0),
        ),
    ],
)
def test_websocket_connect_keepalive_env_override_and_clamp(monkeypatch, tmp_path, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    calls = recording_connect(monkeypatch)
    config = Config("ws://example.invalid/ws", "tok", "hostc", tmp_path)

    # Exercise all three connect sites so a per-site override/clamp regression
    # is caught, not only fetch_snapshot.
    client = WebsocketClient(config, snapshot_timeout=2, rpc_timeout=2)
    client.start()
    client.stop()

    fetch_snapshot(config, timeout=2)
    asyncio.run(
        send_once(
            config,
            {
                "type": "send",
                "host": "hostc",
                "session_name": "codex-x",
                "text": "hello",
                "request_id": "send-keepalive-env",
            },
            timeout=2,
        )
    )

    assert len(calls) == 3
    for call in calls:
        assert_keepalive_kwargs(
            call,
            ping_interval=expected[0],
            ping_timeout=expected[1],
            close_timeout=expected[2],
        )


def test_snapshot_timeout_env_override_and_clamp(monkeypatch, tmp_path):
    config = Config("ws://example.invalid/ws", "tok", "hostc", tmp_path)

    monkeypatch.setenv("AGENT_ORCH_WS_SNAPSHOT_TIMEOUT_S", "12.5")
    assert WebsocketClient(config).snapshot_timeout == 12.5

    monkeypatch.setenv("AGENT_ORCH_WS_SNAPSHOT_TIMEOUT_S", "0.01")
    assert WebsocketClient(config).snapshot_timeout == 0.25

    monkeypatch.setenv("AGENT_ORCH_WS_SNAPSHOT_TIMEOUT_S", "999")
    assert WebsocketClient(config).snapshot_timeout == 300.0


def test_fetch_snapshot_uses_snapshot_timeout_env_by_default(monkeypatch, tmp_path):
    captured = {}

    async def fake_fetch_snapshot_async(_config, timeout, *, events_mode=None):
        captured["timeout"] = timeout
        captured["events_mode"] = events_mode
        return {"type": "snapshot", "hosts": ["hostc"], "sessions": []}

    monkeypatch.setenv("AGENT_ORCH_WS_SNAPSHOT_TIMEOUT_S", "17")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", "60")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", "1")
    monkeypatch.setattr("agent_orch.wsclient._fetch_snapshot_async", fake_fetch_snapshot_async)

    snapshot = fetch_snapshot(Config("ws://example.invalid/ws", "tok", "hostc", tmp_path))

    assert snapshot["type"] == "snapshot"
    assert captured["timeout"] == pytest.approx(17.0, abs=0.01)
    assert captured["events_mode"] is None


def test_fetch_snapshot_summary_mode_sets_hello_events_mode(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="1")
    ws = ScriptedSnapshotWebSocket([{"type": "snapshot", "hosts": ["hostc"], "sessions": []}])
    patch_one_shot_connect(monkeypatch, [ws])

    snapshot = fetch_snapshot(
        Config("ws://unused", "tok", "hostc", tmp_path),
        timeout=5,
        events_mode="summary",
    )

    assert snapshot["type"] == "snapshot"
    assert ws.sent[0]["subscribe"]["all"] is True
    assert ws.sent[0]["subscribe"]["include_subagents"] is True
    assert ws.sent[0]["subscribe"]["events_mode"] == "summary"


def test_one_shot_rpc_sends_without_waiting_for_snapshot(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        hello = json.loads(await ws.recv())
        captured.put(hello)
        raw = await ws.recv()
        msg = json.loads(raw)
        captured.put(msg)
        await ws.send(
            json.dumps(
                {
                    "type": "tell.ok",
                    "request_id": msg["request_id"],
                    "tell_id": msg["tell_id"],
                    "ledger_row_id": 1,
                    "delivery_status": "queued",
                }
            )
        )

    with StubServer(handler) as server:
        response = asyncio.run(
            tell_once(
                Config(server.url, "tok", "hostc", tmp_path),
                {
                    "type": "tell",
                    "tell_id": "tell-fast",
                    "from_stream_id": "hostc:leader",
                    "to_stream_id": "hosta:worker",
                    "text": "hello",
                },
                timeout=2,
            )
        )

    hello = captured.get(timeout=2)
    msg = captured.get(timeout=2)
    assert response["type"] == "tell.ok"
    assert hello["from_stream_id"] == "hostc:leader"
    assert hello["stream_token"] == "stream-secret"
    assert hello["token"] == "tok"
    assert_rpc_subscribe(hello["subscribe"])
    assert msg["type"] == "tell"


def test_reparent_rpc_hello_carries_existing_identity_token(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        captured.put(json.loads(await ws.recv()))
        request = json.loads(await ws.recv())
        captured.put(request)
        await ws.send(json.dumps({"type": "reparent.ok", "request_id": request["request_id"]}))

    with StubServer(handler) as server:
        response = asyncio.run(
            wsclient.reparent_once(
                Config(server.url, "tok", "hostc", tmp_path),
                "hostc:codex-child",
                "hostc:leader-new",
                from_stream_id="hostc:leader-old",
                caller_stream_id="hostc:leader-old",
                timeout=2,
            )
        )

    hello = captured.get(timeout=2)
    request = captured.get(timeout=2)
    assert response["type"] == "reparent.ok"
    assert hello["from_stream_id"] == "hostc:leader-old"
    assert hello["stream_token"] == "stream-secret"
    assert request["stream_token"] == "stream-secret"


def test_terminal_report_rpc_hello_carries_existing_identity_token(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        captured.put(json.loads(await ws.recv()))
        request = json.loads(await ws.recv())
        captured.put(request)
        await ws.send(json.dumps({"type": "report.ok", "request_id": request["request_id"]}))

    with StubServer(handler) as server:
        response = asyncio.run(
            wsclient.report_once(
                Config(server.url, "tok", "hostc", tmp_path),
                {
                    "type": "report",
                    "report_id": "report-identity-token",
                    "from_stream_id": "hostc:leader",
                    "msg_id": 0,
                    "status": "done",
                    "summary": "done",
                    "findings": [],
                    "next_action": "leader_proceed",
                },
                timeout=2,
            )
        )

    hello = captured.get(timeout=2)
    request = captured.get(timeout=2)
    assert response["type"] == "report.ok"
    assert hello["from_stream_id"] == "hostc:leader"
    assert hello["stream_token"] == "stream-secret"
    assert request["stream_token"] == "stream-secret"


def test_rpc_hello_omits_identity_claim_without_stream_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostc:leader")
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)

    hello = wsclient._rpc_hello(Config("ws://unused", "tok", "hostc", tmp_path))

    assert "from_stream_id" not in hello
    assert "stream_token" not in hello


def test_prompt_ask_sends_verified_creator_identity(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        captured.put(json.loads(await ws.recv()))
        msg = json.loads(await ws.recv())
        captured.put(msg)
        await ws.send(
            json.dumps(
                {
                    "type": "prompt.ask.ok",
                    "request_id": msg["request_id"],
                    "ok": True,
                    "question": {"question_id": "q-handoff", "state": "open"},
                }
            )
        )

    with StubServer(handler) as server:
        response = asyncio.run(
            prompt_ask_once(
                Config(server.url, "tok", "hostc", tmp_path),
                {
                    "type": "prompt.ask",
                    "envelope": {
                        "question_id": "q-handoff",
                        "producer_stream_id": "hostc:leader",
                    },
                },
                timeout=2,
            )
        )

    hello = captured.get(timeout=2)
    msg = captured.get(timeout=2)
    assert response["type"] == "prompt.ask.ok"
    assert hello["from_stream_id"] == "hostc:leader"
    assert msg["from_stream_id"] == "hostc:leader"
    assert msg["stream_token"] == "stream-secret"


def test_one_shot_rpc_discards_old_daemon_snapshot_before_response(tmp_path):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        raw = await ws.recv()
        msg = json.loads(raw)
        await ws.send(
            json.dumps(
                {
                    "type": "tell.ok",
                    "request_id": msg["request_id"],
                    "tell_id": msg["tell_id"],
                    "ledger_row_id": 1,
                    "delivery_status": "queued",
                }
            )
        )

    with StubServer(handler) as server:
        response = asyncio.run(
            tell_once(
                Config(server.url, "tok", "hostc", tmp_path),
                {
                    "type": "tell",
                    "tell_id": "tell-old-daemon",
                    "from_stream_id": "hostc:leader",
                    "to_stream_id": "hosta:worker",
                    "text": "hello",
                },
                timeout=2,
            )
        )

    assert response["type"] == "tell.ok"


def test_fast_path_helper_raises_permission_error_on_auth_error(tmp_path):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "auth.error", "error": "invalid token"}))
        await ws.recv()

    payload = {
        "type": "notification.create",
        "producer": "hostc:codex-a",
        "title": "Question",
        "actions": [],
        "answer_to_stream_id": "hostc:codex-a",
    }
    started = time.monotonic()
    with StubServer(handler) as server:
        with pytest.raises(PermissionError, match="invalid token"):
            asyncio.run(
                notification_create_once(
                    Config(server.url, "bad-token", "hostc", tmp_path),
                    payload,
                    timeout=2,
                )
            )

    assert time.monotonic() - started < 1.0


def clean_close_mid_rpc_handler(rpc_type):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == rpc_type:
                await ws.close()
                return

    return handler


def test_wsclient_round_trip(tmp_path):
    with StubServer(rpc_handler) as server:
        client = WebsocketClient(Config(server.url, "tok", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        callbacks = []
        client.on_event(callbacks.append)
        client.start()
        assert client.connection_state == "connected"
        deadline = time.monotonic() + 2.0
        while not callbacks and time.monotonic() < deadline:
            time.sleep(0.01)
        assert any(message.get("type") == "session.died" for message in callbacks)
        assert client.spawn("hostc", "codex", None, "qa", None, "nested")["type"] == "spawn.ok"
        assert client.spawn(
            "hostc",
            "codex",
            None,
            "qa",
            None,
            None,
            handoff=True,
            handoff_from_stream_id="hostc:codex-parent",
            initial_prompt_blob_sha="b" * 64,
        )["type"] == "spawn.ok"
        send_response = client.send_rpc("hostc", "codex-x", "hello", "hostc:codex-parent")
        assert send_response["type"] == "send.result"
        assert send_response["delivery"] == "landed"
        assert send_response["progress"][0]["type"] == "send.progress"
        assert client.tell("tell-1", "hostc:codex-a", "hostc:codex-b", "hello")["delivery_status"] == "queued"
        assert client.upload_prompt_blob("hello", request_id="prompt-1") == "a" * 64
        assert client.close_rpc("hostc", "codex-x", "manual")["type"] == "close.error"
        visibility_response = client.set_visibility("hostc", "codex-x", "nested")
        assert visibility_response["type"] == "set_visibility.ok"
        assert visibility_response["session"]["visibility"] == "nested"
        rename_response = client.rename("hostc", "codex-x", "Self Naming", source="agent")
        assert rename_response["type"] == "rename.ok"
        assert rename_response["session"]["display_name"] == "Self Naming"
        assert client.inspect_stream("hostc:codex-x", msg_id=7, event_tail=3)["type"] == "inspect_stream.ok"
        assert client.await_report("hostc:codex-x", 7)["report_id"] == "r7"
        client.stop()


def test_rename_once_sends_daemon_rename_payload(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        raw = await ws.recv()
        msg = json.loads(raw)
        captured.put(msg)
        await ws.send(
            json.dumps(
                {
                    "type": "rename.ok",
                    "request_id": msg["request_id"],
                    "session": {
                        "stream_id": f"{msg['host']}:{msg['session_name']}",
                        "display_name": msg["display_name"],
                    },
                }
            )
        )

    with StubServer(handler) as server:
        response = asyncio.run(
            rename_once(
                Config(server.url, "tok", "hostc", tmp_path),
                "hostc",
                "codex-session-with-dashes",
                "Foo Bar",
                timeout=2,
            )
        )

    assert response["type"] == "rename.ok"
    msg = captured.get(timeout=2)
    assert set(msg) == {"type", "request_id", "host", "session_name", "display_name", "source"}
    assert msg["type"] == "rename"
    assert msg["request_id"].startswith("rename-")
    assert msg["host"] == "hostc"
    assert msg["session_name"] == "codex-session-with-dashes"
    assert msg["display_name"] == "Foo Bar"
    assert msg["source"] == "agent"


def test_wsclient_spawn_sends_spec_id(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "spawn":
                captured.put(msg)
                await ws.send(json.dumps({"type": "spawn.ok", "request_id": msg["request_id"], "session": {"stream_id": "hostc:codex-x", "session_name": "codex-x"}}))

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "tok", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            assert client.spawn("hostc", "codex", None, None, None, "hidden", spec_id="pentacle__spec_dashboard_2026_05_16")["type"] == "spawn.ok"
            sent = captured.get(timeout=2)
            assert sent["spec_id"] == "pentacle__spec_dashboard_2026_05_16"
            assert sent["idempotency_key"] == sent["request_id"]
        finally:
            client.stop()


def test_spec_update_once_sends_attach_payload(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "session.spec_update":
                captured.put(msg)
                await ws.send(json.dumps({"type": "session.spec_update.ok", "request_id": msg["request_id"], "session": {"stream_id": "hostc:codex-x"}}))

    with StubServer(handler) as server:
        response = asyncio.run(
            spec_update_once(
                Config(server.url, "tok", "hostc", tmp_path),
                "hostc:codex-x",
                "attach",
                "pentacle__spec_dashboard_2026_05_16",
                from_stream_id="hostc:leader",
                timeout=2,
            )
        )
    assert response["type"] == "session.spec_update.ok"
    sent = captured.get(timeout=2)
    assert sent["action"] == "attach"
    assert sent["spec_id"] == "pentacle__spec_dashboard_2026_05_16"
    assert sent["from_stream_id"] == "hostc:leader"
    assert sent["stream_token"] == "stream-secret"


def test_wsclient_threads_stream_token_for_identity_payloads(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            captured.put(msg)
            request_id = msg.get("request_id")
            if msg["type"] == "spawn":
                await ws.send(json.dumps({"type": "spawn.ok", "request_id": request_id, "session": {"stream_id": "hostc:codex-x", "session_name": "codex-x"}}))
            elif msg["type"] == "send":
                await ws.send(json.dumps({"type": "send.result", "request_id": request_id, "delivery": "landed", "attempt": 1}))
            elif msg["type"] == "tell":
                await ws.send(json.dumps({"type": "tell.ok", "request_id": request_id, "tell_id": msg["tell_id"], "ledger_row_id": 1, "delivery_status": "queued"}))
            elif msg["type"] == "report":
                await ws.send(json.dumps({"type": "report.ok", "request_id": request_id, "report_id": msg["report_id"], "ledger_row_id": 1}))
            elif msg["type"] == "close":
                await ws.send(json.dumps({"type": "close.ok", "request_id": request_id, "host": msg["host"], "session_name": msg["session_name"]}))

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "tok", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            client.spawn("hostc", "codex", "hostc:leader", None, None, "default")
            client.send_rpc("hostc", "worker", "hello", "hostc:leader", 1)
            client.tell("tell-token", "hostc:leader", "hostc:worker", "hello", urgent=True)
            client.report("report-token", "hostc:leader", 1, "done")
            client.close_rpc("hostc", "worker", "manual", from_stream_id="hostc:leader")
        finally:
            client.stop()

    identity_frames = [captured.get(timeout=2) for _ in range(5)]
    assert {frame["type"] for frame in identity_frames} == {"spawn", "send", "tell", "report", "close"}
    assert all(frame.get("stream_token") == "stream-secret" for frame in identity_frames)
    assert next(frame for frame in identity_frames if frame["type"] == "tell")["urgent"] is True


def test_close_rpc_includes_caller_stream_id_when_set(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "close":
                captured.put(msg)
                await ws.send(json.dumps({"type": "close.ok", "request_id": msg["request_id"]}))

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            assert client.close_rpc("hostc", "codex-x", "manual", caller_stream_id="hostc:leader")["type"] == "close.ok"
            assert captured.get(timeout=2)["caller_stream_id"] == "hostc:leader"
        finally:
            client.stop()


def test_close_rpc_includes_force_when_set(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "close":
                captured.put(msg)
                await ws.send(json.dumps({"type": "close.ok", "request_id": msg["request_id"]}))

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            assert client.close_rpc("hostc", "codex-x", "manual", force=True)["type"] == "close.ok"
            assert captured.get(timeout=2)["force"] is True
        finally:
            client.stop()


def test_close_rpc_includes_progeny_when_set(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "close":
                captured.put(msg)
                await ws.send(json.dumps({"type": "close.ok", "request_id": msg["request_id"]}))

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            assert (
                client.close_rpc(
                    "hostc",
                    "codex-x",
                    "manual",
                    progeny_stream_id="hostc:codex-successor",
                )["type"]
                == "close.ok"
            )
            assert captured.get(timeout=2)["progeny_stream_id"] == "hostc:codex-successor"
        finally:
            client.stop()


def test_close_rpc_omits_caller_stream_id_when_none(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "close":
                captured.put(msg)
                await ws.send(json.dumps({"type": "close.ok", "request_id": msg["request_id"]}))

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            assert client.close_rpc("hostc", "codex-x", "manual")["type"] == "close.ok"
            assert "caller_stream_id" not in captured.get(timeout=2)
        finally:
            client.stop()


def test_await_report_once_sends_direct_rpc(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)

    async def handler(ws):
        hello = json.loads(await ws.recv())
        assert "from_stream_id" not in hello
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "await_report":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "await_report.ok",
                            "request_id": msg["request_id"],
                            "ok": True,
                            "stream_id": msg["stream_id"],
                            "msg_id": msg["msg_id"],
                            "report_id": "r9",
                            "ledger_row_id": 9,
                            "status": "done",
                        }
                    )
                )

    with StubServer(handler) as server:
        response = asyncio.run(
            await_report_once(
                Config(server.url, "", "hostc", tmp_path),
                "hostc:codex-x",
                9,
                timeout=2,
                from_stream_id="hostc:leader",
                ownership_token="owner-token",
                include_details=True,
                include_extras=True,
                request_id="await-r9",
            )
        )

    sent = captured.get(timeout=2)
    assert response["report_id"] == "r9"
    assert sent["request_id"] == "await-r9"
    assert sent["stream_id"] == "hostc:codex-x"
    assert sent["msg_id"] == 9
    assert sent["timeout"] == 2
    assert sent["ownership_token"] == "owner-token"
    assert sent["include_details"] is True
    assert sent["include_extras"] is True


def test_notification_create_once_sends_direct_rpc(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)

    async def handler(ws):
        hello = json.loads(await ws.recv())
        assert "from_stream_id" not in hello
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "notification.create":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "notification.create.ok",
                            "request_id": msg["request_id"],
                            "notification": {"notification_id": "notif-1"},
                        }
                    )
                )

    payload = {
        "type": "notification.create",
        "producer": "hostc:codex-a",
        "title": "Question",
        "actions": [],
        "answer_to_stream_id": "hostc:codex-a",
    }
    with StubServer(handler) as server:
        response = asyncio.run(
            notification_create_once(
                Config(server.url, "tok", "hostc", tmp_path),
                payload,
                timeout=2,
            )
        )
    assert response["type"] == "notification.create.ok"
    assert captured.get(timeout=2)["answer_to_stream_id"] == "hostc:codex-a"


def test_asset_publish_once_sends_direct_rpc_with_session_identity(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    hellos: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        hello = json.loads(await ws.recv())
        hellos.put(hello)
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "asset.publish":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "asset.publish.ok",
                            "request_id": msg["request_id"],
                            "asset": {"asset_id": "asset-1"},
                        }
                    )
                )

    payload = {
        "type": "asset.publish",
        "stream_id": "hostc:codex-a",
        "from_stream_id": "hostc:codex-a",
        "title": "Stage notes",
        "content_type": "markdown",
        "body": "# Notes\n",
        "tags": [],
    }
    with StubServer(handler) as server:
        response = asyncio.run(
            asset_publish_once(
                Config(server.url, "tok", "hostc", tmp_path),
                payload,
                timeout=2,
            )
        )

    assert response["type"] == "asset.publish.ok"
    assert hellos.get(timeout=2)["from_stream_id"] == "hostc:codex-a"
    sent = captured.get(timeout=2)
    assert sent["stream_token"] == "stream-secret"
    assert sent["stream_id"] == "hostc:codex-a"
    assert sent["from_stream_id"] == "hostc:codex-a"


@pytest.mark.parametrize(
    ("client_func", "rpc_type", "ok_type", "payload"),
    [
        (
            asset_comments_list_once,
            "asset.comments.list",
            "asset.comments.list.ok",
            {"type": "asset.comments.list", "stream_id": "hostc:codex-a", "asset_id": "asset-1"},
        ),
        (
            asset_comment_resolve_once,
            "asset.comment.resolve",
            "asset.comment.resolve.ok",
            {
                "type": "asset.comment.resolve",
                "stream_id": "hostc:codex-a",
                "asset_id": "asset-1",
                "comment_id": "c-1",
                "resolved": True,
            },
        ),
    ],
)
def test_asset_comment_helpers_send_direct_rpcs(tmp_path, client_func, rpc_type, ok_type, payload):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == rpc_type:
                captured.put(msg)
                await ws.send(json.dumps({"type": ok_type, "request_id": msg["request_id"]}))

    with StubServer(handler) as server:
        response = asyncio.run(
            client_func(Config(server.url, "tok", "hostc", tmp_path), payload, timeout=2)
        )

    assert response["type"] == ok_type
    sent = captured.get(timeout=2)
    assert sent["type"] == rpc_type
    assert sent["request_id"].startswith(rpc_type.replace(".", "-"))


@pytest.mark.parametrize(
    ("client_func", "payload"),
    [
        (asset_get_once, {"type": "asset.get", "stream_id": "hostc:codex-a", "asset_id": "missing"}),
        (asset_comments_list_once, {"type": "asset.comments.list", "stream_id": "hostc:codex-a", "asset_id": "missing"}),
    ],
)
def test_asset_error_is_a_correlated_terminal_response(tmp_path, client_func, payload):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": [], "sessions": []}))
        message = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "asset.error", "request_id": message["request_id"], "error_code": "asset_not_found"}))

    with StubServer(handler) as server:
        response = asyncio.run(client_func(Config(server.url, "tok", "hostc", tmp_path), payload, timeout=0.2))

    assert response["type"] == "asset.error"
    assert response["error_code"] == "asset_not_found"
    assert response["request_id"] == payload["request_id"]


def test_notification_await_once_sends_direct_rpc(tmp_path):
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "notification.await":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "notification.await.ok",
                            "request_id": msg["request_id"],
                            "notification_id": msg["notification_id"],
                            "answer": {
                                "notification_id": msg["notification_id"],
                                "action_id": "a0",
                                "action_kind": "yes_no",
                                "label": "Yes",
                                "choice": True,
                                "by": "operator",
                                "at": "2026-06-19T00:00:00+00:00",
                            },
                        }
                    )
                )

    with StubServer(handler) as server:
        response = asyncio.run(
            notification_await_once(
                Config(server.url, "tok", "hostc", tmp_path),
                "notif-1",
                timeout=2,
            )
        )
    assert response["type"] == "notification.await.ok"
    request = captured.get(timeout=2)
    assert request["notification_id"] == "notif-1"
    assert request["timeout"] == 2


def test_await_report_once_returns_closed_without_report_frame(tmp_path):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "await_report":
                await ws.send(
                    json.dumps(
                        {
                            "type": "await_report.closed_without_report",
                            "request_id": msg["request_id"],
                            "ok": False,
                            "stream_id": msg["stream_id"],
                            "msg_id": msg["msg_id"],
                            "error": "closed_without_report",
                            "reason": "closed_without_report",
                        }
                    )
                )

    with StubServer(handler) as server:
        response = asyncio.run(
            await_report_once(
                Config(server.url, "", "hostc", tmp_path),
                "hostc:codex-x",
                9,
                timeout=2,
                request_id="await-closed",
            )
        )

    assert response["type"] == "await_report.closed_without_report"
    assert response["error"] == "closed_without_report"


def test_await_report_once_stream_mode_omits_msg_id_in_request(tmp_path):
    # Stream mode (msg_id=None): the request frame MUST NOT carry a msg_id key, so
    # the daemon resolves on the stream's terminal report (any msg_id) or close.
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "await_report":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "await_report.ok",
                            "request_id": msg["request_id"],
                            "ok": True,
                            "stream_id": msg["stream_id"],
                            "msg_id": None,
                            "report_id": "r-stream",
                            "ledger_row_id": 5,
                            "status": "done",
                        }
                    )
                )

    with StubServer(handler) as server:
        response = asyncio.run(
            await_report_once(
                Config(server.url, "", "hostc", tmp_path),
                "hostc:codex-x",
                None,
                timeout=2,
                request_id="await-stream",
            )
        )

    sent = captured.get(timeout=2)
    assert response["report_id"] == "r-stream"
    assert "msg_id" not in sent
    assert sent["stream_id"] == "hostc:codex-x"
    assert sent["timeout"] == 2


def test_wsclient_await_report_method_omits_msg_id_when_none(tmp_path):
    # WebsocketClient.await_report (the long-lived client path) must also drop the
    # msg_id key in stream mode — propagation through BOTH client surfaces.
    captured: Queue[dict] = Queue()

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "await_report":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "await_report.ok",
                            "request_id": msg["request_id"],
                            "ok": True,
                            "stream_id": msg["stream_id"],
                            "msg_id": msg.get("msg_id"),
                            "report_id": "r-method",
                            "ledger_row_id": 3,
                            "status": "done",
                        }
                    )
                )

    with StubServer(handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=2)
        client.start()
        try:
            # No msg_id → stream mode.
            assert client.await_report("hostc:codex-x")["report_id"] == "r-method"
            stream_frame = captured.get(timeout=2)
            assert "msg_id" not in stream_frame
            # Explicit msg_id → present in the frame (back-compat).
            assert client.await_report("hostc:codex-x", 7)["report_id"] == "r-method"
            msg_id_frame = captured.get(timeout=2)
            assert msg_id_frame["msg_id"] == 7
        finally:
            client.stop()


def test_send_once_sends_direct_rpc_with_inline_inbox_and_token(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        hello = json.loads(await ws.recv())
        assert hello["from_stream_id"] == "hostc:leader"
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "send":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "send.result",
                            "request_id": msg["request_id"],
                            "delivery": "landed",
                            "attempt": 1,
                        }
                    )
                )

    payload = {
        "type": "send",
        "host": "hostc",
        "session_name": "codex-x",
        "text": "hello",
        "from_stream_id": "hostc:leader",
        "msg_id": 9,
        "inbox": {
            "schema_version": "v1",
            "msg_id": 9,
            "from": "hostc:leader",
            "to": "hostc:codex-x",
            "phase": None,
            "role_hint": None,
            "task": "hello",
            "inputs": {},
            "extras": {},
        },
    }
    with StubServer(handler) as server:
        response = asyncio.run(send_once(Config(server.url, "", "hostc", tmp_path), payload, timeout=2))

    sent = captured.get(timeout=2)
    assert response["delivery"] == "landed"
    assert sent["stream_token"] == "stream-secret"
    assert sent["inbox"]["msg_id"] == 9


def test_send_receipt_once_uses_the_single_query_verb(tmp_path):
    captured: Queue[dict] = Queue()
    hellos: Queue[dict] = Queue()

    async def handler(ws):
        hellos.put(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostb"], "sessions": []}))
        async for raw in ws:
            message = json.loads(raw)
            captured.put(message)
            await ws.send(json.dumps({
                "type": "send.receipt.get.ok", "request_id": message["request_id"],
                "found": True,
                "receipts": [{"request_id": message["request_id"], "state": "landed"}],
            }))

    with StubServer(handler) as server:
        response = asyncio.run(send_receipt_once(
            Config(server.url, "", "hostb", tmp_path), "hostb:target", "send-query-1", timeout=2,
        ))

    request = captured.get(timeout=2)
    hello = hellos.get(timeout=2)
    assert hello["subscribe"] == {"all": True, "include_subagents": True}
    assert request == {
        "type": "send.receipt.get", "to_stream_id": "hostb:target", "request_id": "send-query-1",
    }
    assert response["receipts"] == [{"request_id": "send-query-1", "state": "landed"}]


def test_spawn_once_sends_direct_rpc_with_parent_identity_and_token(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN", "stream-secret")

    async def handler(ws):
        hello = json.loads(await ws.recv())
        assert hello["from_stream_id"] == "hostc:leader"
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "spawn":
                captured.put(msg)
                await ws.send(
                    json.dumps(
                        {
                            "type": "spawn.ok",
                            "request_id": msg["request_id"],
                            "session": {"stream_id": "hostc:codex-child", "session_name": "codex-child"},
                        }
                    )
                )

    payload = {"objective": "Exercise the existing spawn contract",
        "type": "spawn",
        "host": "hostc",
        "provider": "codex",
        "parent_stream_id": "hostc:leader",
        "role": "dev",
        "phase": "code",
        "visibility": None,
        "request_id": "spawn-direct",
    }
    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hostc", tmp_path), payload, timeout=2))

    sent = captured.get(timeout=2)
    assert response["type"] == "spawn.ok"
    assert sent["request_id"] == "spawn-direct"
    assert sent["parent_stream_id"] == "hostc:leader"
    assert sent["stream_token"] == "stream-secret"


def test_spawn_once_handoff_sends_handoff_identity_in_hello(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)

    async def handler(ws):
        hello = json.loads(await ws.recv())
        captured.put({"hello": hello})
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hostc"], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "spawn":
                captured.put({"spawn": msg})
                await ws.send(json.dumps({"type": "spawn.ok", "request_id": msg["request_id"], "session": {"stream_id": "hostc:codex-new"}}))

    payload = {"objective": "Exercise the existing spawn contract",
        "host": "hostc",
        "provider": "codex",
        "handoff": True,
        "handoff_from_stream_id": "hostc:leader",
        "visibility": "default",
        "request_id": "handoff-direct",
    }
    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hostc", tmp_path), payload, timeout=2))

    assert response["type"] == "spawn.ok"
    assert "from_stream_id" not in captured.get(timeout=2)["hello"]
    assert captured.get(timeout=2)["spawn"]["handoff_from_stream_id"] == "hostc:leader"


class _SpawnReconcileHandler:
    """StubServer handler: answers spawn with a scripted type, answers await_spawn
    with a scripted reconcile response. Records every message across connections."""

    def __init__(
        self, *, spawn_type, mint_stream_id=None, await_response=None,
        owner_request_id=None, spawn_receipt=None,
    ):
        self.spawn_type = spawn_type
        self.mint_stream_id = mint_stream_id
        self.await_response = await_response
        self.owner_request_id = owner_request_id
        self.spawn_receipt = spawn_receipt
        self.messages: list[dict] = []
        self.inventory_sessions: list[dict] = []

    async def __call__(self, ws):
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "snapshot", "hosts": ["hosta"], "sessions": self.inventory_sessions}))
        async for raw in ws:
            msg = json.loads(raw)
            self.messages.append(msg)
            if msg.get("type") == "spawn":
                if self.spawn_type in {"spawn.ok", "spawn.starting"}:
                    response = {
                        "type": "spawn.ok",
                        "request_id": msg["request_id"],
                        "ok": True,
                        "state": "starting" if self.spawn_type == "spawn.starting" else None,
                        "session": {"stream_id": self.mint_stream_id},
                    }
                    if isinstance(self.spawn_receipt, dict):
                        response["initial_prompt_delivery"] = self.spawn_receipt
                    await ws.send(json.dumps(response))
                    if self.spawn_type == "spawn.starting" and self.await_response is not None:
                        self.inventory_sessions = [dict(self.await_response.get("session") or {})]
                else:
                    await ws.send(json.dumps({
                        "type": "spawn.error", "ok": False,
                        "request_id": msg["request_id"], "error_code": "spawn_failed",
                    }))
            elif msg.get("type") == "await_spawn":
                resp = dict(self.await_response or {})
                resp["request_id"] = msg["request_id"]
                await ws.send(json.dumps(resp))

    def kinds(self):
        return [m.get("type") for m in self.messages]


def test_spawn_once_ok_response_does_not_reconcile(tmp_path):
    """Control: a clean spawn.ok is returned as-is and never triggers await_spawn."""
    handler = _SpawnReconcileHandler(spawn_type="spawn.ok", mint_stream_id="hosta:codex-clean")
    payload = {"objective": "Exercise the existing spawn contract", "type": "spawn", "host": "hosta", "provider": "codex", "request_id": "spawn-clean-1"}
    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hosta", tmp_path), payload, timeout=3))

    assert response["type"] == "spawn.ok"
    assert response.get("reconciled") is None
    assert "await_spawn" not in handler.kinds()


def test_spawn_transport_failure_is_typed_error():
    from agent_orch.wsclient import _retry_failure_response

    response = _retry_failure_response("spawn", "spawn-timeout", reason="rpc_timeout", attempts=1)

    assert response["type"] == "spawn.error"
    assert response["reason"] == "rpc_timeout"


def test_spawn_once_settles_starting_admission_before_returning(tmp_path):
    handler = _SpawnReconcileHandler(
        spawn_type="spawn.starting",
        mint_stream_id="hosta:codex-starting",
        await_response={
            "type": "await_spawn.ok", "ok": True, "stream_id": "hosta:codex-starting",
            "session": {"stream_id": "hosta:codex-starting", "state": "ready"},
        },
    )
    payload = {"objective": "Exercise the existing spawn contract", "type": "spawn", "host": "hosta", "provider": "codex", "request_id": "spawn-starting-1"}
    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hosta", tmp_path), payload, timeout=3))

    assert response["type"] == "spawn.ok"
    assert response["state"] == "ready"
    assert response["session"]["stream_id"] == "hosta:codex-starting"
    assert [message["type"] for message in handler.messages].count("await_spawn") == 0


def test_spawn_once_round_trips_explicit_handoff_tuple(tmp_path):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "snapshot", "hosts": [], "sessions": []}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] != "spawn":
                continue
            assert msg["handoff"] is True
            assert msg["model"] == "gpt-5.6-sol"
            assert msg["effort"] == "high"
            await ws.send(
                json.dumps(
                    {
                        "type": "spawn.ok",
                        "request_id": msg["request_id"],
                        "session": {"stream_id": "hosta:codex-successor"},
                    }
                )
            )
            return

    payload = {"objective": "Exercise the existing spawn contract",
        "type": "spawn",
        "request_id": "spawn-handoff-wire",
        "host": "hosta",
        "provider": "codex",
        "handoff": True,
        "model": "gpt-5.6-sol",
        "effort": "high",
    }
    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hosta", tmp_path), payload, timeout=3))

    assert response["type"] == "spawn.ok"
    assert response["session"]["stream_id"] == "hosta:codex-successor"




@pytest.mark.parametrize(
    "rpc_type,call,expected",
    [
        ("send", lambda client: client.send_rpc("hostc", "codex-x", "hello"), "send.transport"),
        ("close", lambda client: client.close_rpc("hostc", "codex-x", "manual"), "close.indeterminate"),
    ],
)
def test_clean_websocket_close_marks_pending_rpc_indeterminate(tmp_path, rpc_type, call, expected):
    with StubServer(clean_close_mid_rpc_handler(rpc_type)) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=5)
        client.start()
        response = call(client)
        assert response["type"] == expected
        if rpc_type == "send":
            assert response["outcome"] == "transmit_delivered_awaiting_result"
        else:
            assert response["reason"] == f"stream_disconnect_after_{rpc_type}"
        client.stop()


async def no_snapshot_handler(ws):
    await ws.recv()
    await asyncio.sleep(1)


def test_snapshot_timeout(tmp_path):
    with StubServer(no_snapshot_handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=0.1)
        with pytest.raises(SnapshotTimeout):
            client.start()


async def unknown_host_handler(ws):
    await ws.recv()
    await ws.send(json.dumps({"type": "snapshot", "hosts": ["hosta"], "sessions": []}))


def test_unknown_host(tmp_path):
    with StubServer(unknown_host_handler) as server:
        client = WebsocketClient(Config(server.url, "", "hostc", tmp_path), snapshot_timeout=2)
        with pytest.raises(RuntimeError, match="unknown_local_host"):
            client.start()


def test_rpc_send_failure_returns_transmit_failed(monkeypatch, tmp_path):
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path))
    client._loop = object()
    client._ws = object()

    def raise_send(*_args, **_kwargs):
        raise RuntimeError("closed")

    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", raise_send)
    response = client._rpc("send", {"type": "send"})
    assert response["type"] == "send.transport"
    assert response["outcome"] == "transmit_failed"
    assert response["reason"] == "websocket_send_failed"


def test_disconnect_transport_reason_is_prefix_specific(tmp_path):
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path))
    spawn = PendingRpcForTest("spawn")
    send = PendingRpcForTest("send")
    client._pending = {"spawn-1": spawn, "send-1": send}
    client._mark_pending_indeterminate()
    assert spawn.response["reason"] == "stream_disconnect_after_spawn"
    assert send.response["reason"] == "stream_disconnect_after_send"
    assert send.response["outcome"] == "transmit_delivered_awaiting_result"


class PendingRpcForTest:
    def __init__(self, prefix):
        self.prefix = prefix
        self.event = threading.Event()
        self.payload = {"type": prefix}
        self.retry_eligible = False
        self.retry_policy = None
        self.retry_deadline = 0.0
        self.verb = prefix
        self.response = None
        self.sent = True
        self.progress = []
        self.attempts = 1
        self.in_flight = True
        self.reconnect_waiting = False
        self.resume_in_flight = False


def configure_fast_retry(monkeypatch, *, max_attempts="2", deadline="5"):
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", max_attempts)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_BACKOFF_BASE_S", "0.01")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_BACKOFF_CAP_S", "0.01")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_JITTER_FRACTION", "0")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", deadline)


class ScriptedPersistentWebSocket:
    def __init__(self, script):
        self.script = script
        self.sent = []
        self.queue = [{"type": "snapshot", "hosts": ["hostc"], "sessions": []}]
        self.closed = False

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") in {"hello", "pong"}:
            return
        result = self.script(message, self)
        if isinstance(result, BaseException):
            raise result
        if result == "drop":
            self.closed = True
            return
        if isinstance(result, dict):
            self.queue.append(result)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.queue:
            return json.dumps(self.queue.pop(0))
        if self.closed:
            raise StopAsyncIteration
        while not self.queue and not self.closed:
            await asyncio.sleep(0)
        if self.queue:
            return json.dumps(self.queue.pop(0))
        raise StopAsyncIteration


def patch_persistent_connect(monkeypatch, websockets_for_attempts):
    calls = []

    def connect(*_args, **_kwargs):
        ws = websockets_for_attempts[len(calls)]
        calls.append(ws)
        return FakeConnect(ws)

    monkeypatch.setattr("agent_orch.wsclient.websockets.connect", connect)
    return calls


def install_threadsafe_fake_send(monkeypatch, client, handler):
    class FakeRpcWebSocket:
        async def send(self, raw):
            handler(json.loads(raw))

    def run_coroutine_threadsafe(coro, _loop):
        future = concurrent.futures.Future()
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:
            future.set_exception(exc)
        return future

    client._loop = object()
    client._ws = FakeRpcWebSocket()
    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", run_coroutine_threadsafe)


class CountingEvent:
    def __init__(self):
        self._event = threading.Event()
        self.set_count = 0

    def set(self):
        self.set_count += 1
        self._event.set()

    def clear(self):
        self._event.clear()

    def is_set(self):
        return self._event.is_set()

    def wait(self, timeout=None):
        return self._event.wait(timeout)


class HookedLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.on_release = None

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *_exc):
        self._lock.release()
        callback = self.on_release
        self.on_release = None
        if callback is not None:
            callback()


def pending_retry_policy():
    return RetryPolicy(max_attempts=3, backoff_base_s=0.01, backoff_cap_s=0.01, jitter_fraction=0.0, deadline_s=5.0)


@pytest.mark.parametrize(
    ("prefix", "payload", "response_type", "dedup_key"),
    [
        (
            "tell",
            {"type": "tell", "request_id": "tell-r", "tell_id": "tell-1"},
            "tell.ok",
            lambda msg: msg["tell_id"],
        ),
        (
            "report",
            {"type": "report", "request_id": "report-r", "report_id": "report-1"},
            "report.ok",
            lambda msg: msg["report_id"],
        ),
        (
            "set_visibility",
            {"type": "set_visibility", "request_id": "visibility-r", "host": "h", "session_name": "s", "visibility": "private"},
            "set_visibility.ok",
            lambda msg: (msg["host"], msg["session_name"], msg["visibility"]),
        ),
        (
            "send.cancel",
            {"type": "send.cancel", "request_id": "cancel-r", "msg_id": 7},
            "send.cancel.ok",
            lambda msg: msg["msg_id"],
        ),
        (
            "schedule",
            {"type": "schedule.list", "request_id": "schedule-r"},
            "schedule.list.ok",
            lambda msg: msg["request_id"],
        ),
        (
            "spawn",
            {"objective": "Exercise the existing spawn contract",
                "type": "spawn",
                "request_id": "spawn-r",
                "idempotency_key": "spawn-r",
                "host": "hostc",
                "provider": "codex",
                "parent_stream_id": None,
            },
            "spawn.ok",
            lambda msg: msg["idempotency_key"],
        ),
        (
            "send",
            {
                "type": "send",
                "request_id": "send-r",
                "host": "hostc",
                "session_name": "codex-x",
                "text": "hi",
                "msg_id": 42,
            },
            "send.result",
            lambda msg: msg["msg_id"],
        ),
        (
            "inspect_stream",
            {"type": "inspect_stream", "request_id": "inspect-r", "stream_id": "hostc:codex-x"},
            "inspect_stream.ok",
            lambda msg: msg["request_id"],
        ),
        (
            "await_report",
            {"type": "await_report", "request_id": "await-report-r", "stream_id": "hostc:codex-x", "timeout": 5},
            "await_report.ok",
            lambda msg: msg["request_id"],
        ),
        (
            "await_spawn",
            {"type": "await_spawn", "request_id": "await-spawn-r", "spawn_id": "spawn-r"},
            "await_spawn.ok",
            lambda msg: msg["request_id"],
        ),
        (
            "schedule",
            {"type": "schedule.get", "request_id": "schedule-get-r", "schedule_id": "sched-1"},
            "schedule.get.ok",
            lambda msg: msg["request_id"],
        ),
    ],
)
def test_retry_eligible_rpc_transient_send_failure_reuses_ids_and_executes_once(
    monkeypatch, tmp_path, prefix, payload, response_type, dedup_key
):
    configure_fast_retry(monkeypatch)
    sleeps = []
    monkeypatch.setattr("agent_orch.wsclient.time.sleep", lambda delay: sleeps.append(delay))
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)
    frames = []
    executions = {}

    def handler(message):
        frames.append(message)
        if len(frames) == 1:
            raise OSError("transient")
        key = dedup_key(message)
        executions[key] = executions.get(key, 0) + 1
        client._resolve_pending(message["request_id"], {"type": response_type, "request_id": message["request_id"]})

    install_threadsafe_fake_send(monkeypatch, client, handler)

    response = client._rpc(prefix, dict(payload))

    assert response["type"] == response_type
    assert [frame["request_id"] for frame in frames] == [payload["request_id"], payload["request_id"]]
    assert executions == {dedup_key(payload): 1}
    assert len(sleeps) == 1


@pytest.mark.parametrize(
    ("prefix", "payload", "response_type", "dedup_key"),
    [
        ("tell", {"type": "tell", "request_id": "tell-resume", "tell_id": "tell-1"}, "tell.ok", lambda msg: msg["tell_id"]),
        (
            "report",
            {"type": "report", "request_id": "report-resume", "report_id": "report-1"},
            "report.ok",
            lambda msg: msg["report_id"],
        ),
        (
            "set_visibility",
            {"type": "set_visibility", "request_id": "visibility-resume", "host": "h", "session_name": "s", "visibility": "private"},
            "set_visibility.ok",
            lambda msg: (msg["host"], msg["session_name"], msg["visibility"]),
        ),
        (
            "send.cancel",
            {"type": "send.cancel", "request_id": "cancel-resume", "msg_id": 7},
            "send.cancel.ok",
            lambda msg: msg["msg_id"],
        ),
        (
            "spawn",
            {"objective": "Exercise the existing spawn contract",
                "type": "spawn",
                "request_id": "spawn-resume",
                "idempotency_key": "spawn-resume",
                "host": "hostc",
                "provider": "codex",
                "parent_stream_id": None,
            },
            "spawn.ok",
            lambda msg: msg["idempotency_key"],
        ),
        (
            "send",
            {
                "type": "send",
                "request_id": "send-resume",
                "host": "hostc",
                "session_name": "codex-x",
                "text": "hi",
                "msg_id": 42,
            },
            "send.result",
            lambda msg: msg["msg_id"],
        ),
        (
            "inspect_stream",
            {"type": "inspect_stream", "request_id": "inspect-resume", "stream_id": "hostc:codex-x"},
            "inspect_stream.ok",
            lambda msg: msg["request_id"],
        ),
    ],
)
def test_reconnect_resume_resends_retry_eligible_pending_rpc_once(monkeypatch, tmp_path, prefix, payload, response_type, dedup_key):
    configure_fast_retry(monkeypatch, max_attempts="3")
    frames = []
    executions = {}

    def script(message, _ws):
        frames.append(message)
        key = dedup_key(message)
        if key not in executions:
            executions[key] = 1
        if len(frames) == 1:
            return "drop"
        return {"type": response_type, "request_id": message["request_id"], "ok": True}

    calls = patch_persistent_connect(
        monkeypatch,
        [ScriptedPersistentWebSocket(script), ScriptedPersistentWebSocket(script)],
    )
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=5)
    client.start()
    try:
        response = client._rpc(prefix, dict(payload))
    finally:
        client.stop()

    assert response["type"] == response_type
    assert len(calls) == 2
    assert [frame["request_id"] for frame in frames] == [payload["request_id"], payload["request_id"]]
    assert [dedup_key(frame) for frame in frames] == [dedup_key(payload), dedup_key(payload)]
    assert executions == {dedup_key(payload): 1}
    if prefix == "send":
        assert frames[0].get("retry") is None
        assert frames[1]["retry"] is True


@pytest.mark.parametrize(
    ("prefix", "payload", "expected_type"),
    [
        ("close", {"type": "close", "request_id": "close-drop", "host": "h", "session_name": "s"}, "close.indeterminate"),
        ("reparent", {"type": "reparent", "request_id": "reparent-drop"}, "reparent.indeterminate"),
        ("grant_token", {"type": "grant_token", "request_id": "grant-drop"}, "grant_token.indeterminate"),
        ("schedule", {"objective": "Exercise the existing spawn contract", "type": "schedule.insert", "request_id": "schedule-insert-drop"}, "schedule.indeterminate"),
        ("schedule", {"type": "schedule.cancel", "request_id": "schedule-cancel-drop"}, "schedule.indeterminate"),
        ("schedule", {"type": "schedule.reschedule", "request_id": "schedule-reschedule-drop"}, "schedule.indeterminate"),
        ("schedule", {"type": "schedule.run", "request_id": "schedule-run-drop"}, "schedule.indeterminate"),
    ],
)
def test_reconnect_does_not_resume_noneligible_pending_rpc(monkeypatch, tmp_path, prefix, payload, expected_type):
    configure_fast_retry(monkeypatch, max_attempts="3")
    frames = []

    def script(message, _ws):
        frames.append(message)
        return "drop"

    patch_persistent_connect(
        monkeypatch,
        [ScriptedPersistentWebSocket(script), ScriptedPersistentWebSocket(script)],
    )
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=5)
    client.start()
    try:
        response = client._rpc(prefix, dict(payload))
    finally:
        client.stop()

    assert response["type"] == expected_type
    assert response["reason"] == f"stream_disconnect_after_{prefix}"
    assert [frame["request_id"] for frame in frames] == [payload["request_id"]]


def test_reconnect_preserves_sent_msg_idless_send_transport(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3")
    frames = []

    def script(message, _ws):
        frames.append(message)
        return "drop"

    patch_persistent_connect(
        monkeypatch,
        [ScriptedPersistentWebSocket(script), ScriptedPersistentWebSocket(script)],
    )
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=5)
    client.start()
    try:
        response = client._rpc(
            "send",
            {
                "type": "send",
                "request_id": "send-no-msg-drop",
                "host": "hostc",
                "session_name": "codex-x",
                "text": "hi",
            },
        )
    finally:
        client.stop()

    assert response["type"] == "send.transport"
    assert response["outcome"] == "transmit_delivered_awaiting_result"
    assert response["reason"] == "stream_disconnect_after_send"
    assert [frame["request_id"] for frame in frames] == ["send-no-msg-drop"]


def test_reconnect_resume_deadline_expiry_returns_final_error(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3", deadline="0.5")
    now = [100.0]
    monkeypatch.setattr("agent_orch.wsclient.time.monotonic", lambda: now[0])
    frames = []

    def script(message, _ws):
        frames.append(message)
        now[0] = 101.0
        return "drop"

    patch_persistent_connect(
        monkeypatch,
        [ScriptedPersistentWebSocket(script), ScriptedPersistentWebSocket(script)],
    )
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), snapshot_timeout=2, rpc_timeout=5)
    client.start()
    try:
        response = client._rpc("tell", {"type": "tell", "request_id": "tell-deadline", "tell_id": "tell-1"})
    finally:
        client.stop()

    assert response["type"] == "tell.indeterminate"
    assert response["reason"] == "retry_deadline_exceeded"
    assert response["attempts"] == 1
    assert [frame["request_id"] for frame in frames] == ["tell-deadline"]


def test_reconnect_resume_timeout_race_does_not_double_send(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3")
    frames = []
    executions = {}
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)

    def initial_handler(message):
        frames.append(message)
        executions[message["tell_id"]] = executions.get(message["tell_id"], 0) + 1

    install_threadsafe_fake_send(monkeypatch, client, initial_handler)

    def resume_script(message, _ws):
        frames.append(message)
        return None

    resume_ws = ScriptedPersistentWebSocket(resume_script)
    resume_ws.queue = []
    wait_calls = {"count": 0}
    original_wait = threading.Event.wait

    def wait_with_reconnect_race(event, timeout=None):
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            client._mark_pending_indeterminate()
            client._ws = resume_ws
            asyncio.run(client._resume_pending_after_reconnect())
            return False
        if wait_calls["count"] == 2:
            client._resolve_pending("tell-race", {"type": "tell.ok", "request_id": "tell-race", "ok": True})
            return True
        return original_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", wait_with_reconnect_race)

    response = client._rpc("tell", {"type": "tell", "request_id": "tell-race", "tell_id": "tell-1"})

    assert response["type"] == "tell.ok"
    assert [frame["request_id"] for frame in frames] == ["tell-race", "tell-race"]
    assert executions == {"tell-1": 1}


def test_rpc_send_claim_blocks_resume_before_threadsafe_send_runs(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3")
    frames = []
    executions = {}
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)

    def script(message, _ws):
        frames.append(message)
        executions[message["tell_id"]] = executions.get(message["tell_id"], 0) + 1
        client._resolve_pending(message["request_id"], {"type": "tell.ok", "request_id": message["request_id"], "ok": True})

    ws = ScriptedPersistentWebSocket(script)
    ws.queue = []
    client._loop = object()
    client._ws = ws
    calls = {"count": 0}

    def run_coroutine_threadsafe(coro, _loop):
        future = concurrent.futures.Future()
        calls["count"] += 1
        if calls["count"] == 1:
            client._mark_pending_indeterminate()
            asyncio.run(client._resume_pending_after_reconnect())
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:
            future.set_exception(exc)
        return future

    monkeypatch.setattr("asyncio.run_coroutine_threadsafe", run_coroutine_threadsafe)

    response = client._rpc("tell", {"type": "tell", "request_id": "tell-ordering-race", "tell_id": "tell-1"})

    assert response["type"] == "tell.ok"
    assert [frame["request_id"] for frame in frames] == ["tell-ordering-race"]
    assert executions == {"tell-1": 1}


def test_reconnect_resume_skips_pending_popped_after_resumable_capture(tmp_path):
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)
    frames = []

    def script(message, _ws):
        frames.append(message)

    client._ws = ScriptedPersistentWebSocket(script)
    client._ws.queue = []
    lock = HookedLock()
    client._pending_lock = lock
    pending = PendingRpc(
        prefix="tell",
        event=threading.Event(),
        payload={"type": "tell", "request_id": "tell-pop-race", "tell_id": "tell-1"},
        retry_eligible=True,
        retry_policy=pending_retry_policy(),
        retry_deadline=time.monotonic() + 5,
        verb="tell",
        attempts=1,
        reconnect_waiting=True,
    )
    client._pending["tell-pop-race"] = pending
    popped = {"value": False}

    def pop_after_resumable_capture():
        with client._pending_lock:
            popped["value"] = client._pending.pop("tell-pop-race", None) is pending

    lock.on_release = pop_after_resumable_capture

    asyncio.run(client._resume_pending_after_reconnect())

    assert popped["value"] is True
    assert frames == []


def test_mark_pending_finalizes_under_lock_and_late_resolve_sets_event_once(tmp_path):
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)
    lock = HookedLock()
    client._pending_lock = lock
    event = CountingEvent()
    pending = PendingRpc(
        prefix="tell",
        event=event,
        payload={"type": "tell", "request_id": "tell-finalize-race", "tell_id": "tell-1"},
        retry_eligible=False,
        retry_policy=None,
        retry_deadline=0.0,
        verb="tell",
        attempts=1,
        in_flight=True,
    )
    client._pending["tell-finalize-race"] = pending

    def resolve_after_finalize_releases_lock():
        client._resolve_pending(
            "tell-finalize-race",
            {"type": "tell.ok", "request_id": "tell-finalize-race", "ok": True},
        )

    lock.on_release = resolve_after_finalize_releases_lock

    client._mark_pending_indeterminate()

    assert pending.response["type"] == "tell.indeterminate"
    assert event.set_count == 1


def test_rpc_returns_response_that_arrives_during_retry_backoff(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)
    frames = []

    def handler(message):
        frames.append(message)
        raise OSError("transient")

    def resolve_during_backoff(_delay):
        client._resolve_pending("tell-r", {"type": "tell.ok", "request_id": "tell-r", "ok": True})

    install_threadsafe_fake_send(monkeypatch, client, handler)
    monkeypatch.setattr("agent_orch.wsclient.time.sleep", resolve_during_backoff)

    response = client._rpc("tell", {"type": "tell", "request_id": "tell-r", "tell_id": "tell-1"})

    assert response["type"] == "tell.ok"
    assert len(frames) == 1


def test_retry_eligible_rpc_happy_path_one_send_no_backoff(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch)
    sleeps = []
    monkeypatch.setattr("agent_orch.wsclient.time.sleep", lambda delay: sleeps.append(delay))
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)
    frames = []

    def handler(message):
        frames.append(message)
        client._resolve_pending(message["request_id"], {"type": "tell.ok", "request_id": message["request_id"]})

    install_threadsafe_fake_send(monkeypatch, client, handler)

    response = client._rpc("tell", {"type": "tell", "request_id": "happy-r", "tell_id": "tell-happy"})

    assert response["type"] == "tell.ok"
    assert len(frames) == 1
    assert sleeps == []


def test_retry_exhausted_returns_structured_final_error(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")
    monkeypatch.setattr("agent_orch.wsclient.time.sleep", lambda _delay: None)
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)

    def handler(_message):
        raise OSError("still closed")

    install_threadsafe_fake_send(monkeypatch, client, handler)

    response = client._rpc("tell", {"type": "tell", "request_id": "tell-r", "tell_id": "tell-1"})

    assert response["type"] == "tell.indeterminate"
    assert response["reason"] == "retry_exhausted"
    assert response["attempts"] == 2


def test_retry_deadline_returns_structured_final_error(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3", deadline="0.001")
    now = [100.0]
    monkeypatch.setattr("agent_orch.wsclient.time.monotonic", lambda: now[0])
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)

    def handler(_message):
        now[0] = 101.0
        raise OSError("still closed")

    install_threadsafe_fake_send(monkeypatch, client, handler)

    response = client._rpc("tell", {"type": "tell", "request_id": "tell-r", "tell_id": "tell-1"})

    assert response["type"] == "tell.indeterminate"
    assert response["reason"] == "retry_deadline_exceeded"
    assert response["attempts"] == 1


def test_noneligible_rpc_send_failure_is_not_retried(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch)
    client = WebsocketClient(Config("ws://unused", "", "hostc", tmp_path), rpc_timeout=5)
    frames = []

    def handler(message):
        frames.append(message)
        raise OSError("closed")

    install_threadsafe_fake_send(monkeypatch, client, handler)

    unsafe_calls = [
        ("close", {"type": "close", "request_id": "close-r"}),
        ("reparent", {"type": "reparent", "request_id": "reparent-r"}),
        ("schedule", {"objective": "Exercise the existing spawn contract", "type": "schedule.insert", "request_id": "schedule-insert-r"}),
        ("schedule", {"type": "schedule.cancel", "request_id": "schedule-cancel-r"}),
        ("schedule", {"type": "schedule.reschedule", "request_id": "schedule-reschedule-r"}),
        ("schedule", {"type": "schedule.run", "request_id": "schedule-run-r"}),
        ("grant_token", {"type": "grant_token", "request_id": "grant-r"}),
        ("upload_blob", {"type": "upload_blob_init", "request_id": "upload-r"}),
        ("upload_prompt_blob", {"type": "upload_prompt_blob_init", "request_id": "upload-prompt-r"}),
    ]

    responses = [client._rpc(prefix, payload) for prefix, payload in unsafe_calls]
    msg_idless_send = client._rpc("send", {"type": "send", "request_id": "send-r"})

    for (prefix, _payload), response in zip(unsafe_calls, responses, strict=True):
        assert response["type"] == f"{prefix}.indeterminate"
        assert response["reason"] == "websocket_send_failed"
    assert msg_idless_send["type"] == "send.transport"
    assert msg_idless_send["outcome"] == "transmit_failed"
    assert msg_idless_send["reason"] == "websocket_send_failed"
    assert [frame["request_id"] for frame in frames] == [
        "close-r",
        "reparent-r",
        "schedule-insert-r",
        "schedule-cancel-r",
        "schedule-reschedule-r",
        "schedule-run-r",
        "grant-r",
        "upload-r",
        "upload-prompt-r",
        "send-r",
    ]


class ScriptedOneShotWebSocket:
    def __init__(self, script):
        self.script = script
        self.sent = []
        self.recv_queue = [{"type": "snapshot", "hosts": ["hostc"], "sessions": []}]
        self.closed = False

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") in {"hello", "pong"}:
            return
        result = self.script(message)
        if isinstance(result, BaseException):
            self.recv_queue.append(result)
        elif isinstance(result, dict):
            self.recv_queue.append(result)

    async def recv(self):
        if not self.recv_queue:
            raise asyncio.TimeoutError()
        item = self.recv_queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    async def close(self):
        self.closed = True


class ScriptedOneShotSendFailureWebSocket(ScriptedOneShotWebSocket):
    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") in {"hello", "pong"}:
            return
        raise OSError("send failed")


def patch_one_shot_connect(monkeypatch, websockets_for_attempts):
    calls = []

    def connect(*_args, **_kwargs):
        ws = websockets_for_attempts[len(calls)]
        calls.append(ws)
        return FakeConnect(ws)

    monkeypatch.setattr("agent_orch.wsclient.websockets.connect", connect)
    return calls


def test_send_once_msg_id_timeout_retry_sets_retry_true_and_dedupes(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("agent_orch.wsclient.asyncio.sleep", no_sleep)
    executions = {}
    frames = []

    def first_attempt(message):
        frames.append(message)
        executions[message["msg_id"]] = executions.get(message["msg_id"], 0) + 1
        return asyncio.TimeoutError()

    def second_attempt(message):
        frames.append(message)
        if not message.get("retry"):
            executions[message["msg_id"]] = executions.get(message["msg_id"], 0) + 1
        return {"type": "send.result", "request_id": message["request_id"], "delivery": "landed"}

    patch_one_shot_connect(
        monkeypatch,
        [ScriptedOneShotWebSocket(first_attempt), ScriptedOneShotWebSocket(second_attempt)],
    )
    payload = {"type": "send", "host": "hostc", "session_name": "codex-x", "text": "hi", "msg_id": 42, "request_id": "send-r"}

    response = asyncio.run(send_once(Config("ws://unused", "", "hostc", tmp_path), payload, timeout=5))

    assert response["type"] == "send.result"
    assert executions == {42: 1}
    assert [frame["request_id"] for frame in frames] == ["send-r", "send-r"]
    assert frames[0].get("retry") is None
    assert frames[1]["retry"] is True
    assert frames[1]["msg_id"] == 42


def test_send_once_msg_id_timeout_reports_transmitted_not_failed(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="1")

    def timeout_attempt(_message):
        return asyncio.TimeoutError()

    patch_one_shot_connect(monkeypatch, [ScriptedOneShotWebSocket(timeout_attempt)])
    payload = {"type": "send", "host": "hostc", "session_name": "codex-x", "text": "hi", "msg_id": 42, "request_id": "send-r"}

    response = asyncio.run(send_once(Config("ws://unused", "", "hostc", tmp_path), payload, timeout=5))

    assert response["type"] == "send.transport"
    assert response["outcome"] == "transmit_delivered_awaiting_result"


def test_send_once_msg_id_timeout_then_send_failure_reports_transmitted(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("agent_orch.wsclient.asyncio.sleep", no_sleep)

    def timeout_attempt(_message):
        return asyncio.TimeoutError()

    patch_one_shot_connect(
        monkeypatch,
        [ScriptedOneShotWebSocket(timeout_attempt), ScriptedOneShotSendFailureWebSocket(lambda _message: None)],
    )
    payload = {"type": "send", "host": "hostc", "session_name": "codex-x", "text": "hi", "msg_id": 42, "request_id": "send-r"}

    response = asyncio.run(send_once(Config("ws://unused", "", "hostc", tmp_path), payload, timeout=5))

    assert response["type"] == "send.transport"
    assert response["outcome"] == "transmit_delivered_awaiting_result"
    assert response["reason"] == "retry_exhausted"


def test_send_once_without_msg_id_timeout_is_not_retried(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3")
    attempts = []

    def timeout_attempt(message):
        attempts.append(message)
        return asyncio.TimeoutError()

    calls = patch_one_shot_connect(monkeypatch, [ScriptedOneShotWebSocket(timeout_attempt)])
    payload = {"type": "send", "host": "hostc", "session_name": "codex-x", "text": "hi", "request_id": "send-r"}

    with pytest.raises(TimeoutError):
        asyncio.run(send_once(Config("ws://unused", "", "hostc", tmp_path), payload, timeout=5))

    assert len(calls) == 1
    assert len(attempts) == 1


def test_spawn_once_idempotency_key_replay_and_conflict(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")
    seen = {}
    sent_keys = []

    def spawn_script(message):
        key = message["idempotency_key"]
        sent_keys.append(key)
        fingerprint = (message["host"], message["provider"], message.get("initial_prompt"))
        previous = seen.setdefault(key, fingerprint)
        if previous != fingerprint:
            return {"type": "spawn.error", "request_id": message["request_id"], "error_code": "idempotency_conflict"}
        return {"type": "spawn.ok", "request_id": message["request_id"], "session": {"stream_id": "hostc:codex-x"}}

    patch_one_shot_connect(
        monkeypatch,
        [
            ScriptedOneShotWebSocket(spawn_script),
            ScriptedOneShotWebSocket(spawn_script),
            ScriptedOneShotWebSocket(spawn_script),
        ],
    )
    config = Config("ws://unused", "", "hostc", tmp_path)
    base = {"objective": "Exercise the existing spawn contract",
        "host": "hostc",
        "provider": "codex",
        "parent_stream_id": None,
        "request_id": "spawn-r-1",
        "idempotency_key": "logical-spawn-key",
        "initial_prompt": "one",
    }

    first = asyncio.run(spawn_once(config, dict(base), timeout=5))
    replay_payload = dict(base)
    replay_payload["request_id"] = "spawn-r-2"
    replay = asyncio.run(spawn_once(config, replay_payload, timeout=5))
    conflict_payload = dict(base)
    conflict_payload["request_id"] = "spawn-r-3"
    conflict_payload["initial_prompt"] = "two"
    conflict = asyncio.run(spawn_once(config, conflict_payload, timeout=5))

    assert first["type"] == "spawn.ok"
    assert replay["type"] == "spawn.ok"
    assert conflict["type"] == "spawn.error"
    assert conflict["error_code"] == "idempotency_conflict"
    assert sent_keys == ["logical-spawn-key"] * 3


def test_spawn_once_retries_with_the_explicit_idempotency_key(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("agent_orch.wsclient.asyncio.sleep", no_sleep)
    frames = []

    def timeout_attempt(message):
        frames.append(message)
        return asyncio.TimeoutError()

    def successful_retry(message):
        frames.append(message)
        return {"type": "spawn.ok", "request_id": message["request_id"], "session": {"stream_id": "hostc:codex-x"}}

    patch_one_shot_connect(
        monkeypatch,
        [ScriptedOneShotWebSocket(timeout_attempt), ScriptedOneShotWebSocket(successful_retry)],
    )
    response = asyncio.run(
        spawn_once(
            Config("ws://unused", "", "hostc", tmp_path),
            {"objective": "Exercise the existing spawn contract",
                "host": "hostc",
                "provider": "codex",
                "request_id": "spawn-explicit-key-retry",
                "idempotency_key": "logical-spawn-key-retry",
            },
            timeout=5,
        )
    )

    assert response["type"] == "spawn.ok"
    assert len(frames) == 2
    assert {frame["idempotency_key"] for frame in frames} == {"logical-spawn-key-retry"}


def test_identityless_spawn_rpc_omits_discovered_from_stream_id(monkeypatch, tmp_path):
    captured: Queue[dict] = Queue()
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE", raising=False)

    async def handler(ws):
        captured.put(json.loads(await ws.recv()))
        msg = json.loads(await ws.recv())
        await ws.send(
            json.dumps(
                {
                    "type": "spawn.ok",
                    "request_id": msg["request_id"],
                    "session": {"stream_id": "hostc:codex-x"},
                }
            )
        )

    monkeypatch.setenv("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID", "hostc:internal")
    monkeypatch.setenv("AGENT_ORCH_STREAM_ID", "hostc:env")
    monkeypatch.setenv("PENTACLE_STREAM_ID", "hostc:pentacle")

    with StubServer(handler) as server:
        response = asyncio.run(
            spawn_once(
                Config(server.url, "tok", "hostc", tmp_path),
                {"objective": "Exercise the existing spawn contract", "host": "hostc", "provider": "codex", "request_id": "spawn-identityless"},
                timeout=2,
            )
        )

    hello = captured.get(timeout=2)
    assert response["type"] == "spawn.ok"
    assert "from_stream_id" not in hello


def test_fetch_blob_retry_reuses_request_id_and_dedupes(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("agent_orch.wsclient.asyncio.sleep", no_sleep)
    frames = []
    executions = {}

    def first_attempt(message):
        frames.append(message)
        executions[message["request_id"]] = executions.get(message["request_id"], 0) + 1
        return OSError("transient")

    def second_attempt(message):
        frames.append(message)
        return {"type": "fetch_blob.ok", "request_id": message["request_id"], "content_b64": "b2s=", "final": True}

    patch_one_shot_connect(
        monkeypatch,
        [ScriptedOneShotWebSocket(first_attempt), ScriptedOneShotWebSocket(second_attempt)],
    )

    blob = asyncio.run(fetch_blob_once(Config("ws://unused", "", "hostc", tmp_path), "sha256:abc", timeout=5))

    assert blob == b"ok"
    assert len(frames) == 2
    assert frames[0]["request_id"] == frames[1]["request_id"]
    assert executions == {frames[0]["request_id"]: 1}


class ScriptedSnapshotWebSocket:
    def __init__(self, recv_items):
        self.recv_items = list(recv_items)
        self.sent = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self.recv_items:
            raise asyncio.TimeoutError()
        item = self.recv_items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    async def close(self):
        self.closed = True


def test_fetch_snapshot_transient_failure_retries(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")
    monkeypatch.setattr("agent_orch.wsclient.time.sleep", lambda _delay: None)
    calls = patch_one_shot_connect(
        monkeypatch,
        [
            ScriptedSnapshotWebSocket([OSError("transient")]),
            ScriptedSnapshotWebSocket([{"type": "snapshot", "hosts": ["hostc"], "sessions": []}]),
        ],
    )

    snapshot = fetch_snapshot(Config("ws://unused", "", "hostc", tmp_path), timeout=5)

    assert snapshot["type"] == "snapshot"
    assert len(calls) == 2


def test_fetch_snapshot_dead_link_fails_after_retry_bound(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")
    monkeypatch.setattr("agent_orch.wsclient.time.sleep", lambda _delay: None)
    calls = patch_one_shot_connect(
        monkeypatch,
        [ScriptedSnapshotWebSocket([OSError("down")]), ScriptedSnapshotWebSocket([OSError("down")])],
    )

    with pytest.raises(SnapshotTimeout, match="attempts=2"):
        fetch_snapshot(Config("ws://unused", "", "hostc", tmp_path), timeout=5)

    assert len(calls) == 2


def test_inbox_list_transient_failure_retries(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("agent_orch.wsclient.asyncio.sleep", no_sleep)
    frames = []

    def first_attempt(message):
        frames.append(message)
        return OSError("transient")

    def second_attempt(message):
        frames.append(message)
        return {"type": "inbox.ok", "request_id": message["request_id"], "items": []}

    calls = patch_one_shot_connect(
        monkeypatch,
        [ScriptedOneShotWebSocket(first_attempt), ScriptedOneShotWebSocket(second_attempt)],
    )

    response = asyncio.run(inbox_once(Config("ws://unused", "", "hostc", tmp_path), {"stream_id": "hostc:codex-x"}, timeout=5))

    assert response["type"] == "inbox.ok"
    assert len(calls) == 2
    assert frames[0]["request_id"] == frames[1]["request_id"]


def test_inbox_list_dead_link_fails_after_retry_bound(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="2")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("agent_orch.wsclient.asyncio.sleep", no_sleep)

    def failed_attempt(_message):
        return OSError("down")

    calls = patch_one_shot_connect(
        monkeypatch,
        [ScriptedOneShotWebSocket(failed_attempt), ScriptedOneShotWebSocket(failed_attempt)],
    )

    with pytest.raises(TimeoutError, match="attempts=2"):
        asyncio.run(inbox_once(Config("ws://unused", "", "hostc", tmp_path), {"stream_id": "hostc:codex-x"}, timeout=5))

    assert len(calls) == 2


def test_inbox_drain_transient_failure_is_not_retried(monkeypatch, tmp_path):
    configure_fast_retry(monkeypatch, max_attempts="3")

    def failed_attempt(_message):
        return OSError("down")

    calls = patch_one_shot_connect(monkeypatch, [ScriptedOneShotWebSocket(failed_attempt)])

    with pytest.raises(OSError):
        asyncio.run(
            inbox_once(
                Config("ws://unused", "", "hostc", tmp_path),
                {"stream_id": "hostc:codex-x", "drain": True},
                timeout=5,
            )
        )

    assert len(calls) == 1


def test_spawn_starting_settles_from_inventory(tmp_path):
    handler = _SpawnReconcileHandler(
        spawn_type="spawn.starting",
        mint_stream_id="hosta:codex-child",
        await_response={
            "type": "await_spawn.ok",
            "ok": True,
            "stream_id": "hosta:codex-child",
            "session": {
                "stream_id": "hosta:codex-child", "state": "ready",
                "actual_launch_tuple": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
            },
        },
    )
    payload = {"objective": "Exercise the existing spawn contract",
        "type": "spawn",
        "host": "hosta",
        "provider": "codex",
        "request_id": "spawn-starting-receipt",
        "initial_prompt": "receipt brief",
    }

    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hosta", tmp_path), payload, timeout=3))

    assert response["type"] == "spawn.ok"
    assert response["state"] == "ready"
    assert response["session"]["stream_id"] == "hosta:codex-child"


@pytest.mark.parametrize(
    ("session", "durable_receipt", "spawn_receipt"),
    [
        (
            {
                "stream_id": "hosta:codex-child",
                "state": "failed",
                "status": "open",
                "closed_at": None,
                "bootstrap_state": "failed",
            },
            None,
            {
                "state": "failed",
                "delivery_status": "failed",
                "bootstrap_state": "failed",
                "delivery_failed_at": "not-a-timestamp",
            },
        ),
        (
            {
                "stream_id": "hosta:codex-child",
                "state": "starting",
                "status": "open",
                "closed_at": None,
                "bootstrap_state": "starting",
            },
            {
                "state": "indeterminate",
                "delivery_status": "indeterminate",
                "bootstrap_state": "starting",
                "proof_state": "pending",
            },
            {"state": "requested", "delivery_status": "pending"},
        ),
    ],
)
def test_await_starting_spawn_open_row_is_indeterminate(
    tmp_path, session, durable_receipt, spawn_receipt,
):
    handler = _SpawnReconcileHandler(
        spawn_type="spawn.starting",
        mint_stream_id="hosta:codex-child",
        await_response={
            "type": "await_spawn.ok",
            "session": session,
            **(
                {"initial_prompt_delivery": durable_receipt}
                if durable_receipt is not None else {}
            ),
        },
        spawn_receipt=spawn_receipt,
    )

    payload = {"objective": "Exercise the existing spawn contract",
        "type": "spawn",
        "host": "hosta",
        "provider": "codex",
        "request_id": "spawn-open-failed-row",
        "initial_prompt": "receipt brief",
    }

    with StubServer(handler) as server:
        response = asyncio.run(spawn_once(Config(server.url, "", "hosta", tmp_path), payload, timeout=3))

    assert response["type"] == "spawn.indeterminate"
    assert response["state"] == "starting"
    assert response["session"]["bootstrap_state"] == "starting"
    receipt = response["initial_prompt_delivery"]
    assert receipt["state"] == "indeterminate"
    assert receipt["delivery_status"] == "indeterminate"
    assert receipt["bootstrap_state"] == "starting"
    assert receipt["proof_state"] in {"pending", "unreachable"}
    assert receipt["proof_watermark"] is None or isinstance(receipt["proof_watermark"], int)
    assert receipt["proof_watermark_state"] in {"reachable", "unreachable", None}
    assert receipt["proof_watermark_reason"] is None or isinstance(receipt["proof_watermark_reason"], str)
    assert isinstance(receipt["failure_code"], str) and receipt["failure_code"]
    assert isinstance(receipt["failure_reason"], str) and receipt["failure_reason"]
    assert isinstance(receipt["delivery_failed_at"], str) and receipt["delivery_failed_at"].endswith("Z")
    if durable_receipt is not None:
        assert "await_spawn" in handler.kinds()
