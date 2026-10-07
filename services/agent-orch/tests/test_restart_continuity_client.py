"""Client side of daemon restart continuity (spec_pentacle__daemon_restart_continuity_2026_10).

Restart-matrix cells C1 (await across an outage) and C2 post-admission (the
early `spawn.ok state=starting` reply followed by a dropped socket). The real
daemon journeys live in `services/chat-stream-v2/tests/soak/test_restart_continuity.py`;
these pin the client contract without a daemon.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import websockets
from websockets.frames import Close

from agent_orch import wsclient
from agent_orch.config import Config


class ReplyingSocket:
    def __init__(self, reply_type: str) -> None:
        self.reply_type = reply_type
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        request = self.sent[-1]
        return json.dumps({"type": self.reply_type, "ok": True, "request_id": request["request_id"]})

    async def close(self) -> None:
        return None


def fast_retry(monkeypatch, *, deadline: str | None = None) -> None:
    monkeypatch.delenv("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_BACKOFF_BASE_S", "0.001")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_BACKOFF_CAP_S", "0.001")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_JITTER_FRACTION", "0")
    if deadline is None:
        monkeypatch.delenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", raising=False)
    else:
        monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", deadline)


def refused_then(monkeypatch, refusals: int, reply_type: str) -> list[int]:
    connects = [0]

    async def connect(config, **_kwargs):
        connects[0] += 1
        if connects[0] <= refusals:
            raise ConnectionRefusedError(111, "Connect call failed")
        return ReplyingSocket(reply_type)

    monkeypatch.setattr(wsclient, "_connect_rpc_ready", connect)
    return connects


def config(tmp_path) -> Config:
    return Config("ws://127.0.0.1:9", "", "testhost", tmp_path)


def test_await_survives_outage_longer_than_three_attempts(monkeypatch, tmp_path):
    """C1: a refused daemon for longer than the old 3-attempt bound is survived
    inside the verb's own deadline, and the retried await returns the report."""
    fast_retry(monkeypatch)
    connects = refused_then(monkeypatch, 12, "await_report.ok")
    payload = {"type": "await_report", "request_id": "await-c1", "stream_id": "testhost:child", "msg_id": 0}

    response = asyncio.run(wsclient._one_shot_rpc(config(tmp_path), payload, prefix="await_report", timeout=5))

    assert response["type"] == "await_report.ok"
    assert connects[0] == 13


def test_transport_retry_is_bounded_by_the_verb_deadline(monkeypatch, tmp_path):
    fast_retry(monkeypatch, deadline="0.3")
    connects = refused_then(monkeypatch, 10**9, "await_report.ok")
    payload = {"type": "await_report", "request_id": "await-dl", "stream_id": "testhost:child", "msg_id": 0}

    response = asyncio.run(wsclient._one_shot_rpc(config(tmp_path), payload, prefix="await_report", timeout=5))

    assert response["type"] == "await_report.indeterminate"
    assert response["reason"] == "retry_deadline_exceeded"
    assert response["attempts"] == connects[0] > 3


def test_explicit_max_attempts_still_bounds_transport_retries(monkeypatch, tmp_path):
    """Regression control: the env override keeps its exact meaning."""
    fast_retry(monkeypatch)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", "3")
    connects = refused_then(monkeypatch, 10**9, "await_report.ok")
    payload = {"type": "await_report", "request_id": "await-max", "stream_id": "testhost:child", "msg_id": 0}

    response = asyncio.run(wsclient._one_shot_rpc(config(tmp_path), payload, prefix="await_report", timeout=5))

    assert response == {"type": "await_report.indeterminate", "request_id": "await-max",
                        "reason": "retry_exhausted", "attempts": 3,
                        "message": "[Errno 111] Connect call failed"}
    assert connects[0] == 3


def test_non_retry_eligible_verb_still_fails_fast(monkeypatch, tmp_path):
    """Regression control: `close` is single-shot and raises the transport error."""
    fast_retry(monkeypatch)
    connects = refused_then(monkeypatch, 10**9, "close.ok")
    payload = {"type": "close", "request_id": "close-1", "host": "testhost", "session_name": "x"}

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(wsclient._one_shot_rpc(config(tmp_path), payload, prefix="close", timeout=5))
    assert connects[0] == 1


def _closed_1001() -> Exception:
    return websockets.exceptions.ConnectionClosedOK(Close(1001, ""), Close(1001, ""), True)


def _accepted() -> dict:
    return {"type": "spawn.ok", "ok": True, "request_id": "spawn-c2", "stream_id": "testhost:v2-c2",
            "state": "starting", "reason": "admitted",
            "session": {"stream_id": "testhost:v2-c2", "state": "starting"},
            "initial_prompt_delivery": {"state": "staged"}}


@pytest.mark.parametrize("loss", [_closed_1001, lambda: ConnectionRefusedError(111, "refused")])
def test_admitted_spawn_recovers_terminal_state_after_socket_loss(monkeypatch, tmp_path, loss):
    """C2 post-admission: after `spawn.ok state=starting` the daemon restarts;
    the client resolves the same request through await_spawn instead of
    returning a bare `connection_dropped`."""
    async def connect_ready(*_args, **_kwargs):
        raise loss()

    calls: list[dict] = []

    async def await_spawn_once(_config, payload, *, timeout, **_kwargs):
        calls.append(payload)
        if len(calls) == 1:
            return {"type": "await_spawn.ok", "ok": True, "state": "starting", "stream_id": "testhost:v2-c2",
                    "pending_reconcile": True}
        return {"type": "await_spawn.ok", "ok": True, "state": "ready", "stream_id": "testhost:v2-c2",
                "session": {"stream_id": "testhost:v2-c2", "state": "ready"}}

    monkeypatch.setattr(wsclient, "_connect_ready", connect_ready)
    monkeypatch.setattr(wsclient, "await_spawn_once", await_spawn_once)

    result = asyncio.run(wsclient._await_starting_spawn(config(tmp_path), _accepted(),
                                                        deadline=wsclient.time.monotonic() + 30))

    assert result["type"] == "spawn.ok" and result["state"] == "ready"
    assert result["stream_id"] == "testhost:v2-c2" and result["request_id"] == "spawn-c2"
    assert all(call == {"spawn_request_id": "spawn-c2"} for call in calls)


def test_admitted_spawn_socket_loss_reports_recorded_failure(monkeypatch, tmp_path):
    async def connect_ready(*_args, **_kwargs):
        raise _closed_1001()

    async def await_spawn_once(_config, payload, *, timeout, **_kwargs):
        return {"type": "await_spawn.error", "ok": False, "error_code": "spawn_failed",
                "error": "boot_not_ready: claude TUI not ready", "stream_id": "testhost:v2-c2"}

    monkeypatch.setattr(wsclient, "_connect_ready", connect_ready)
    monkeypatch.setattr(wsclient, "await_spawn_once", await_spawn_once)

    result = asyncio.run(wsclient._await_starting_spawn(config(tmp_path), _accepted(),
                                                        deadline=wsclient.time.monotonic() + 30))

    assert result["type"] == "spawn.error"
    assert result["error"] == "boot_not_ready: claude TUI not ready"
    assert result["request_id"] == "spawn-c2" and result["stream_id"] == "testhost:v2-c2"


def test_admitted_spawn_socket_loss_until_deadline_is_typed_indeterminate(monkeypatch, tmp_path):
    async def connect_ready(*_args, **_kwargs):
        raise _closed_1001()

    async def await_spawn_once(_config, payload, *, timeout, **_kwargs):
        return {"type": "await_spawn.indeterminate", "request_id": "x", "reason": "retry_deadline_exceeded",
                "attempts": 9}

    monkeypatch.setattr(wsclient, "_connect_ready", connect_ready)
    monkeypatch.setattr(wsclient, "await_spawn_once", await_spawn_once)

    result = asyncio.run(wsclient._await_starting_spawn(config(tmp_path), _accepted(),
                                                        deadline=wsclient.time.monotonic() + 30))

    assert result["type"] == "spawn.indeterminate"
    assert result["request_id"] == "spawn-c2" and result["stream_id"] == "testhost:v2-c2"
    assert result["initial_prompt_delivery"]["state"] == "indeterminate"


class MalformedSocket(ReplyingSocket):
    async def recv(self) -> str:
        return "{not json"


def test_non_transport_failure_keeps_the_attempt_bound(monkeypatch, tmp_path):
    """Final QA B3: only a refused/reset/dropped socket gets the deadline-bound
    reconnect; a malformed frame on a healthy socket keeps the prior attempt
    limit and its error."""
    fast_retry(monkeypatch, deadline="0.3")
    connects = [0]

    async def connect(config, **_kwargs):
        connects[0] += 1
        return MalformedSocket("await_report.ok")

    monkeypatch.setattr(wsclient, "_connect_rpc_ready", connect)
    payload = {"type": "await_report", "request_id": "await-bad", "stream_id": "testhost:child", "msg_id": 0}

    response = asyncio.run(wsclient._one_shot_rpc(config(tmp_path), payload, prefix="await_report", timeout=5))

    assert connects[0] == wsclient._rpc_retry_policy_from_env(5).max_attempts
    assert response["reason"] == "retry_exhausted" and response["attempts"] == connects[0]


def test_admitted_spawn_recovery_never_outlives_the_spawn_deadline(monkeypatch, tmp_path):
    """Final QA B4: the real nested await_spawn retry (a larger configured retry
    window, daemon still down) is capped by the spawn's own outer deadline."""
    fast_retry(monkeypatch, deadline="30")

    async def connect_ready(*_args, **_kwargs):
        raise _closed_1001()

    async def refused(config, **_kwargs):
        raise ConnectionRefusedError(111, "Connect call failed")

    monkeypatch.setattr(wsclient, "_connect_ready", connect_ready)
    monkeypatch.setattr(wsclient, "_connect_rpc_ready", refused)
    started = wsclient.time.monotonic()

    result = asyncio.run(wsclient._await_starting_spawn(config(tmp_path), _accepted(), deadline=started + 0.3))

    assert wsclient.time.monotonic() - started < 0.3 + 0.15
    assert result["type"] == "spawn.indeterminate" and result["request_id"] == "spawn-c2"


@pytest.mark.parametrize("stall", ["recv-then-slow-close", "send-then-slow-close", "slow-connect"])
@pytest.mark.parametrize("max_attempts", ["1", "3"])
def test_spawn_deadline_includes_socket_cleanup(monkeypatch, tmp_path, stall, max_attempts):
    """Cycle 2 B4: a connected socket that stalls (reply, send or connect) and
    then closes slowly never stretches the spawn deadline. The cancelled
    RPC's close is bounded by the remaining budget (the transport is
    aborted), and the outer wait never awaits that cleanup."""
    fast_retry(monkeypatch, deadline="30")
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_MAX_ATTEMPTS", max_attempts)
    events: list[str] = []

    async def connect_ready(*_args, **_kwargs):
        raise _closed_1001()

    class Transport:
        def abort(self):
            events.append("abort")

    class Socket:
        transport = Transport()

        async def send(self, _raw):
            events.append("sent")
            if stall == "send-then-slow-close":
                await asyncio.Event().wait()

        async def recv(self):
            await asyncio.Event().wait()

        async def close(self):
            events.append("close")
            await asyncio.sleep(5)

    async def connected(config, **_kwargs):
        if stall == "slow-connect":
            await asyncio.sleep(5)
        await asyncio.sleep(0.02)
        return Socket()

    monkeypatch.setattr(wsclient, "_connect_ready", connect_ready)
    monkeypatch.setattr(wsclient, "_connect_rpc_ready", connected)
    deadline_s = 0.15

    async def scenario():
        started = wsclient.time.monotonic()
        result = await wsclient._await_starting_spawn(config(tmp_path), _accepted(), deadline=started + deadline_s)
        return result, wsclient.time.monotonic() - started

    run_started = wsclient.time.monotonic()
    result, elapsed = asyncio.run(scenario())
    assert elapsed <= deadline_s + 0.05, (elapsed, events)
    assert wsclient.time.monotonic() - run_started <= deadline_s + 0.15  # loop teardown is quick too
    assert result["type"] == "spawn.indeterminate" and result["request_id"] == "spawn-c2"
    if stall != "slow-connect":
        assert "abort" in events, events  # cleanup past the deadline aborts, never waits


def test_close_within_without_a_deadline_keeps_the_plain_close():
    calls = []

    class Socket:
        async def close(self):
            await asyncio.sleep(0.05)
            calls.append("closed")

    asyncio.run(wsclient._close_within(Socket(), None))
    assert calls == ["closed"]


def test_backoff_saturates_at_large_attempt_counts():
    """Final QA B5: a deadline-bound retry can pass attempt 1024; the backoff
    saturates at its cap instead of overflowing float conversion."""
    policy = wsclient.RetryPolicy(max_attempts=3, backoff_base_s=0.25, backoff_cap_s=2.0, jitter_fraction=0.0,
                                  deadline_s=3600.0, transport_deadline_bound=True)
    for attempt in (1025, 10**6):
        assert wsclient._retry_backoff_s(policy, attempt) == 2.0
    delay, reason = wsclient._transport_retry_next_delay(policy, wsclient.time.monotonic() + 60, 10**6)
    assert (delay, reason) == (2.0, "")


@pytest.mark.parametrize("inventory", ["reset", "stalled-read", "starting-then-reset"])
def test_spawn_deadline_includes_the_inventory_socket_cleanup(monkeypatch, tmp_path, inventory):
    """Cycle 2 re-read B4-INVENTORY-CLOSE: once the inventory socket is
    acquired, a transport loss or stalled read followed by a slow close still
    returns a typed spawn.indeterminate within the spawn deadline."""
    fast_retry(monkeypatch, deadline="30")
    events: list[str] = []

    class Transport:
        def abort(self):
            events.append("abort")

    class Socket:
        transport = Transport()

        async def recv(self):  # the real reader bounds this by the deadline
            await asyncio.Event().wait()

        async def close(self):
            events.append("inventory-close")
            await asyncio.sleep(5)

    snapshot = {"sessions": [{"stream_id": "fixture:v2-c2", "state": "starting"}]} if inventory == "starting-then-reset" \
        else {"sessions": []}

    async def connect_ready(*_args, **_kwargs):
        return Socket(), snapshot

    async def await_spawn(*_args, **_kwargs):
        await asyncio.sleep(5)  # a daemon slow to answer the outcome readback
        return {"type": "await_spawn.ok", "state": "starting"}

    async def read_frame(*_args, **_kwargs):
        raise ConnectionResetError("fixture reset")

    monkeypatch.setattr(wsclient, "_connect_ready", connect_ready)
    monkeypatch.setattr(wsclient, "await_spawn_once", await_spawn)
    if inventory != "stalled-read":
        monkeypatch.setattr(wsclient, "_read_rpc_frame", read_frame)
    deadline_s = 0.15

    async def scenario():
        started = wsclient.time.monotonic()
        result = await wsclient._await_starting_spawn(config(tmp_path), _accepted(), deadline=started + deadline_s)
        return result, wsclient.time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert elapsed <= deadline_s + 0.05, (elapsed, events)
    assert result["request_id"] == "spawn-c2" and result["type"] == "spawn.indeterminate", result
    assert result["stream_id"] == "testhost:v2-c2"
    assert "inventory-close" in events or "abort" in events
