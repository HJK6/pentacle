from __future__ import annotations

import json
from argparse import Namespace

import pytest

from agent_orch import cli
from agent_orch.config import Config


def test_inspect_json_prints_daemon_response(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_inspect(config, stream_id, **kwargs):
        calls.append((config, stream_id, kwargs))
        return {
            "type": "inspect_stream.ok",
            "stream_id": stream_id,
            "session": {"status": "running"},
            "existing_report": {"report_id": "r1"},
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostb", tmp_path))
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect)

    rc = cli.inspect(
        Namespace(
            stream_id="hostb:codex-x",
            msg_id=None,
            event_tail=5,
            json=True,
            timeout=1.0,
        )
    )

    assert rc == 0
    assert calls[0][1] == "hostb:codex-x"
    assert calls[0][2]["event_tail"] == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "inspect_stream.ok"
    assert payload["existing_report"]["report_id"] == "r1"


def test_inspect_pretty_prints_requested_and_effective_routing(monkeypatch, capsys, tmp_path):
    async def fake_inspect(_config, stream_id, **_kwargs):
        return {
            "type": "inspect_stream.ok",
            "stream_id": stream_id,
            "session": {
                "status": "running",
                "requested_model": "gpt-5.6-sol",
                "requested_effort": "xhigh",
                "effective_model": "gpt-5.6-luna",
                "effective_effort": "low",
                "routing_integrity": "mismatch",
                "routing_integrity_reason": "requested_effective_mismatch",
                "routing_integrity_updated_at": "2026-07-15T00:13:17Z",
                "bootstrap_state": "unsubmitted",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostc", tmp_path))
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect)

    rc = cli.inspect(
        Namespace(
            stream_id="hostc:codex-x",
            msg_id=None,
            event_tail=5,
            json=False,
            timeout=1.0,
        )
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "requested_model: gpt-5.6-sol" in output
    assert "requested_effort: xhigh" in output
    assert "effective_model: gpt-5.6-luna" in output
    assert "effective_effort: low" in output
    assert "routing_integrity: mismatch" in output
    assert "routing_integrity_reason: requested_effective_mismatch" in output
    assert "routing_integrity_updated_at: 2026-07-15T00:13:17Z" in output
    assert "bootstrap_state: unsubmitted" in output


def test_inspect_pretty_prints_offline_close_and_deferred_state(capsys):
    cli._print_inspect_pretty({
        "stream_id": "hostb:v2-offline",
        "session": {"status": "closed", "close_kind": "operator_offline_close"},
        "deferred_reap": {"host": "hostb", "requested_at": "2026-09-10T00:00:00Z",
                          "attempts": 5, "last_error": "ssh_unreachable",
                          "done_at": None, "exhausted_at": "2026-09-10T00:05:00Z"},
    })
    output = capsys.readouterr().out
    assert "close_kind: operator_offline_close" in output
    assert "deferred_reap:" in output
    assert "attempts: 5" in output
    assert "last_error: ssh_unreachable" in output
    assert "done_at: None" in output
    assert "exhausted_at: 2026-09-10T00:05:00Z" in output


def test_inspect_pretty_prints_close_audit_attribution(monkeypatch, capsys, tmp_path):
    async def fake_inspect(_config, stream_id, **_kwargs):
        return {
            "type": "inspect_stream.ok",
            "stream_id": stream_id,
            "session": {"status": "closed"},
            "close_audit": {
                "disposition": "closed",
                "close_kind": "operator_close",
                "actor_kind": "operator",
                "closed_by": "operator:cred1",
                "reason": "operator asked",
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostc", tmp_path))
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect)

    rc = cli.inspect(
        Namespace(stream_id="hosta:v2-x", msg_id=None, event_tail=5, json=False, timeout=1.0)
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "close_audit:" in output
    assert "disposition: closed" in output
    assert "close_kind: operator_close" in output
    assert "actor_kind: operator" in output
    assert "closed_by: operator:cred1" in output
    assert "reason: operator asked" in output


def test_inspect_pretty_prints_report_degradation(monkeypatch, capsys, tmp_path):
    async def fake_inspect(_config, stream_id, **_kwargs):
        return {
            "type": "inspect_stream.ok",
            "stream_id": stream_id,
            "session": {"status": "closed"},
            "existing_report": {
                "report_id": "legacy",
                "degraded": True,
                "degradation_reason": "structured_core_incomplete",
                "missing_fields": ["findings", "next_action"],
            },
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hosta", tmp_path))
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect)
    args = cli.build_parser().parse_args(["inspect", "hosta:codex-child"])

    assert cli.inspect(args) == 0
    output = capsys.readouterr().out
    assert "degraded: True" in output
    assert "degradation_reason: structured_core_incomplete" in output
    assert "missing_fields: ['findings', 'next_action']" in output


def test_inspect_full_prints_untruncated_event_text(monkeypatch, capsys, tmp_path):
    full_text = "brief:" + "x" * 500

    async def fake_inspect(_config, stream_id, **_kwargs):
        return {
            "type": "inspect_stream.ok",
            "stream_id": stream_id,
            "session": {"status": "running"},
            "recent_events": [{"kind": "tell", "text": full_text}],
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://test", "tok", "hostc", tmp_path))
    monkeypatch.setattr(cli, "inspect_stream_once", fake_inspect)
    args = cli.build_parser().parse_args(["inspect", "hosta:codex-worker", "--full"])

    assert cli.inspect(args) == 0
    assert full_text in capsys.readouterr().out


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(1, "."), (2, ".."), (3, "..."), (160, "x" * 157 + "...")],
)
def test_inspect_max_text_honors_exact_cap(limit, expected):
    rendered = cli._truncate_inspect_text("x" * 200, limit)

    assert rendered == expected
    assert len(rendered) == limit
