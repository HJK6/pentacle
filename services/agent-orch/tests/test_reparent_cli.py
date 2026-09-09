"""CLI coverage for ``agent-orch reparent`` + the handoff ``--no-reparent-children``
opt-out. The public reparent RPC contract.

The headline test is the contract-drift boundary guard: it drives the REAL
``reparent_once`` payload construction (through a faked websocket) and asserts
the wire frame carries exactly the field names the daemon's
``_handle_reparent_message`` reads. This is the guard the spec calls out (the
class of bug B2's ``--prompt-inline`` miss motivated): a CLI that builds a frame
the daemon silently ignores.
"""
from __future__ import annotations

import asyncio
import json
from argparse import Namespace

from agent_orch import cli, wsclient
from agent_orch.config import Config


# Field names the daemon handler (_handle_reparent_message) actually reads off
# the wire. The boundary test asserts the CLI builds a frame within this set.
DAEMON_ACCEPTED_FIELDS = {
    "type",
    "request_id",
    "host",
    "session_name",
    "new_parent_stream_id",
    "from_stream_id",
    "caller_stream_id",
    "stream_token",
    "reason",
}


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        # Echo the caller's request_id back on a reparent.ok so the
        # request/response match in reparent_once resolves.
        request_id = self.sent[-1]["request_id"]
        return json.dumps(
            {
                "type": "reparent.ok",
                "request_id": request_id,
                "worker_stream_id": "hostb:codex-child",
                "old_parent_stream_id": "hostb:leader-old",
                "new_parent_stream_id": "hostb:leader-new",
                "auth_row": "old_parent",
            }
        )

    async def close(self) -> None:
        return None


def test_reparent_once_builds_daemon_accepted_wire_frame(monkeypatch, tmp_path):
    """Contract-drift guard: the constructed ``reparent`` RPC frame uses the
    real field names the daemon reads (host/session_name/new_parent_stream_id/
    from_stream_id/caller_stream_id), and carries no field outside that set."""
    fake = _FakeWS()

    async def fake_connect_ready(_config, from_stream_id=None):
        return fake

    monkeypatch.setattr(wsclient, "_connect_rpc_ready", fake_connect_ready)

    config = Config("ws://test", "tok", "hostb", tmp_path)
    response = asyncio.run(
        wsclient.reparent_once(
            config,
            "hostb:codex-child",
            "hostb:leader-new",
            reason="reparent",
            from_stream_id="hostb:leader-old",
            caller_stream_id="hostb:leader-old",
        )
    )
    assert response["type"] == "reparent.ok"

    assert len(fake.sent) == 1
    payload = fake.sent[0]
    assert payload["type"] == "reparent"
    # Worker is carried as host/session_name (mirrors close), not a packed id.
    assert payload["host"] == "hostb"
    assert payload["session_name"] == "codex-child"
    assert payload["new_parent_stream_id"] == "hostb:leader-new"
    assert payload["from_stream_id"] == "hostb:leader-old"
    assert payload["caller_stream_id"] == "hostb:leader-old"
    assert payload["reason"] == "reparent"
    # No drift: every emitted key is one the daemon handler reads.
    assert set(payload) <= DAEMON_ACCEPTED_FIELDS, set(payload) - DAEMON_ACCEPTED_FIELDS


def _reparent_args(**overrides):
    data = {
        "stream_id": "hostb:codex-child",
        "new_parent": "hostb:leader-new",
        "from_stream_id": None,
        "caller_stream_id": None,
        "reason": "reparent",
        "timeout": 5.0,
    }
    data.update(overrides)
    return Namespace(**data)


def test_reparent_cli_routes_args_to_reparent_once(monkeypatch, tmp_path, capsys):
    """The CLI forwards worker/--to/caller identity to ``reparent_once`` and
    maps exit 0 off ``reparent.ok``. Leader discovery fills the caller default,
    exactly like ``close``."""
    calls: list[dict] = []

    async def fake_reparent_once(
        config,
        worker_stream_id,
        new_parent_stream_id,
        *,
        reason="reparent",
        timeout=30.0,
        from_stream_id=None,
        caller_stream_id=None,
    ):
        calls.append(
            {
                "worker_stream_id": worker_stream_id,
                "new_parent_stream_id": new_parent_stream_id,
                "reason": reason,
                "timeout": timeout,
                "from_stream_id": from_stream_id,
                "caller_stream_id": caller_stream_id,
            }
        )
        return {"type": "reparent.ok", "request_id": "reparent-1"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:leader")
    monkeypatch.setattr(cli, "reparent_once", fake_reparent_once)

    rc = cli.reparent(_reparent_args())
    assert rc == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["worker_stream_id"] == "hostb:codex-child"
    assert call["new_parent_stream_id"] == "hostb:leader-new"
    # caller identity defaults to the resolved local leader (mirrors close).
    assert call["caller_stream_id"] == "hostb:leader"
    assert call["from_stream_id"] == "hostb:leader"
    assert call["timeout"] == 5.0
    assert json.loads(capsys.readouterr().out)["type"] == "reparent.ok"


def test_reparent_cli_explicit_caller_overrides(monkeypatch, tmp_path):
    calls: list[dict] = []

    async def fake_reparent_once(config, worker, new_parent, *, reason="reparent", timeout=30.0, from_stream_id=None, caller_stream_id=None):
        calls.append({"from_stream_id": from_stream_id, "caller_stream_id": caller_stream_id})
        return {"type": "reparent.ok", "request_id": "reparent-1"}

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:leader")
    monkeypatch.setattr(cli, "reparent_once", fake_reparent_once)

    rc = cli.reparent(
        _reparent_args(from_stream_id="hostb:override", caller_stream_id="hostb:caller")
    )
    assert rc == 0
    assert calls[0]["from_stream_id"] == "hostb:override"
    assert calls[0]["caller_stream_id"] == "hostb:caller"


def test_reparent_cli_error_exits_nonzero(monkeypatch, tmp_path, capsys):
    async def fake_reparent_once(*_args, **_kwargs):
        return {
            "type": "reparent.error",
            "request_id": "reparent-1",
            "error": "reparent_cross_host",
            "error_code": "reparent_cross_host",
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:leader")
    monkeypatch.setattr(cli, "reparent_once", fake_reparent_once)

    rc = cli.reparent(_reparent_args())
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "reparent_cross_host"


# ---------------------------------------------------------------------------
# Spawn handoff carries the reparent_children opt-out on the wire.
# ---------------------------------------------------------------------------


def _spawn_args(**overrides):
    data = {"objective": "Exercise the existing spawn contract",
        "workspace": None,
        "provider": "codex",
        "host": "hostb",
        "role": None,
        "phase": "stage-c",
        "visibility": None,
        "parent": None,
        "handoff": True,
        "reparent_children": True,
        "initial_prompt": "short prompt",
        "initial_prompt_file": None,
        "timeout": 1.0,
    }
    data.update(overrides)
    return Namespace(**data)


def _capture_spawn_payload(monkeypatch, tmp_path):
    captured: dict = {}

    async def fake_spawn_once(_config, payload, timeout):
        captured["payload"] = payload
        request_id = payload["request_id"]
        stream_id = "hostb:codex-new"
        return {
            "type": "spawn.ok",
            "request_id": request_id,
            "session": {"stream_id": stream_id},
            "initial_prompt_delivery": {
                "request_id": request_id,
                "tell_id": f"handoff-{request_id}",
                "ledger_row_id": 19,
                "to_stream_id": stream_id,
                "delivery_status": "delivered",
                "delivery_ack_at": "2026-08-01T00:00:01Z",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "hostb:codex-old")
    monkeypatch.setattr(
        cli,
        "_handoff_source_row",
        lambda *_args, **_kwargs: {
            "provider": "codex",
            "effective_model": "gpt-5.6-sol",
            "effective_effort": "high",
        },
    )
    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    return captured


def test_handoff_default_omits_reparent_children_flag(monkeypatch, tmp_path):
    """Default handoff auto-re-parents, so the opt-out is NOT put on the wire
    (the daemon defaults reparent_children to true when the field is absent)."""
    captured = _capture_spawn_payload(monkeypatch, tmp_path)
    assert cli.spawn(_spawn_args()) == 0
    payload = captured["payload"]
    assert payload["handoff"] is True
    assert "reparent_children" not in payload


def test_handoff_no_reparent_children_sets_wire_flag(monkeypatch, tmp_path):
    """``--no-reparent-children`` (reparent_children=False) is carried on the
    spawn frame so the daemon skips the same-host auto-re-parent."""
    captured = _capture_spawn_payload(monkeypatch, tmp_path)
    assert cli.spawn(_spawn_args(reparent_children=False)) == 0
    assert captured["payload"]["reparent_children"] is False
