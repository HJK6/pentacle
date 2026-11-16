"""Retired protected source: the CLI replays its exact cached handoff request.

A retired seat token cannot read the fleet snapshot, so the retry must not
fetch one; it resends the stored logical payload (fresh request id only).
"""
from __future__ import annotations

import json
import stat
from argparse import Namespace

import pytest

from agent_orch import cli
from agent_orch.config import Config


def _args(**overrides):
    data = {"objective": "Rotate the assistant", "workspace": None, "provider": "claude", "model": None,
            "effort": None, "host": "node-b", "role": None, "phase": None, "visibility": None, "parent": None,
            "handoff": True, "confirm_model_change": False, "initial_prompt": "short prompt",
            "initial_prompt_file": None, "timeout": 1.0}
    data.update(overrides)
    return Namespace(**data)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_ORCH_MEMORY_REPO", str(tmp_path))
    config = Config("ws://test", "tok", "node-b", tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "discover_leader_stream_id_short", lambda _config: "node-b:old")
    sent: list[dict] = []

    async def fake_spawn_once(_config, payload, timeout):
        sent.append(dict(payload))
        return {"type": "spawn.ok", "request_id": payload["request_id"], "session": {"stream_id": "node-b:new"},
                "stream_id": "node-b:new"}

    monkeypatch.setattr(cli, "spawn_once", fake_spawn_once)
    return config, sent


def test_first_handoff_caches_exact_payload_owner_only(monkeypatch, env):
    config, sent = env
    monkeypatch.setattr(cli, "_handoff_source_row", lambda *_a, **_k: {
        "stream_id": "node-b:old", "provider": "claude", "effective_model": "claude-opus-5-5",
        "effective_effort": "high", "role": "assistant"})
    assert cli.spawn(_args()) == 0
    path = cli._handoff_request_path(config, "node-b:old")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert stored["source_stream_id"] == "node-b:old"
    assert stored["payload"] == {k: v for k, v in sent[0].items() if k != "request_id"}
    assert "tok" not in path.read_text()


def test_retired_retry_skips_snapshot_and_resends_logical_payload(monkeypatch, env):
    config, sent = env
    monkeypatch.setattr(cli, "_handoff_source_row", lambda *_a, **_k: {
        "stream_id": "node-b:old", "provider": "claude", "effective_model": "claude-opus-5-5",
        "effective_effort": "high", "role": "assistant"})
    assert cli.spawn(_args()) == 0

    def no_snapshot(*_a, **_k):
        raise AssertionError("retired replay must not read the fleet snapshot")

    monkeypatch.setattr(cli, "_handoff_source_row", no_snapshot)
    monkeypatch.setattr(cli, "fetch_snapshot", no_snapshot)
    assert cli.spawn(_args(idempotency_key=sent[0]["idempotency_key"])) == 0
    first, retry = sent
    assert retry["request_id"] != first["request_id"]
    assert {k: v for k, v in retry.items() if k != "request_id"} == {k: v for k, v in first.items() if k != "request_id"}


def test_foreign_or_mismatched_cache_is_ignored(monkeypatch, env):
    config, sent = env
    path = cli._handoff_request_path(config, "node-b:old")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source_stream_id": "node-b:other",
                                "payload": {"handoff_from_stream_id": "node-b:other", "idempotency_key": "k"}}))
    assert cli._stored_handoff_request(config, "node-b:old", None) is None
    path.write_text(json.dumps({"source_stream_id": "node-b:old",
                                "payload": {"handoff_from_stream_id": "node-b:old", "idempotency_key": "k"}}))
    assert cli._stored_handoff_request(config, "node-b:old", "other-key") is None
    assert cli._stored_handoff_request(config, "node-b:old", "k")["idempotency_key"] == "k"


def test_lifecycle_transfer_binds_fresh_readback(monkeypatch, env):
    calls: list[dict] = []

    async def fake_lifecycle(_config, fields, *, timeout, from_stream_id):
        calls.append({**fields, "_from": from_stream_id})
        if fields["action"] == "inspect":
            return {"type": "assistant.lifecycle.ok", "grant": {"revision": 4},
                    "target": {"stream_id": "node-b:lead2", "session_generation": "g2", "eligible": True}}
        return {"type": "assistant.lifecycle.ok", "receipt": {"revision": 5}}

    monkeypatch.setattr(cli, "assistant_lifecycle_once", fake_lifecycle)
    args = cli.build_parser().parse_args(["lifecycle", "transfer", "node-b:lead2", "--reason", "hand over",
                                          "--request-id", "req-1"])
    assert cli.lifecycle(args) == 0
    assert calls[1] == {"action": "transfer", "request_id": "req-1", "reason": "hand over",
                        "target_stream_id": "node-b:lead2", "target_generation": "g2", "expected_revision": 4,
                        "_from": "node-b:old"}


def test_lifecycle_transfer_requires_reason():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["lifecycle", "transfer", "node-b:lead2"])
