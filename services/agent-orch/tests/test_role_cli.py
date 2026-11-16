from __future__ import annotations

import json
from argparse import Namespace

from agent_orch import cli
from agent_orch.config import Config


def _config(tmp_path) -> Config:
    return Config("ws://test", "tok", "hosta", tmp_path)


# -- parser -------------------------------------------------------------------


def test_role_parser_accepts_set_and_get():
    s = cli.build_parser().parse_args(["role", "set", "hosta:v2-abc", "lead"])
    assert s.stream_id == "hosta:v2-abc"
    assert s.role == "lead"

    g = cli.build_parser().parse_args(["role", "get", "hosta:v2-abc"])
    assert g.stream_id == "hosta:v2-abc"


# -- role set: loads the baseline and passes it through -----------------------


def test_role_set_loads_baseline_and_sends_payload(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_role_set_once(config, stream_id, role, *, baseline_content, timeout):
        calls.append({
            "stream_id": stream_id, "role": role,
            "baseline_content": baseline_content, "timeout": timeout,
        })
        return {"type": "role.set.ok", "session": {"role": role, "role_source": "role_set"},
                "role_baseline": {"stamp": f"[role baseline: {role}]"}}

    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "role_set_once", fake_role_set_once)
    monkeypatch.setattr(
        cli.role_baseline, "load_role_baseline",
        lambda config, role: {"role": role, "source_path": "/x", "content": "BASELINE BODY"},
    )

    args = Namespace(stream_id="hosta:v2-abc", role="lead", timeout=1.0)
    assert cli.role_set(args) == 0
    assert calls == [{
        "stream_id": "hosta:v2-abc", "role": "lead",
        "baseline_content": "BASELINE BODY", "timeout": 1.0,
    }]
    assert json.loads(capsys.readouterr().out)["type"] == "role.set.ok"


def test_role_set_reports_missing_baseline_as_null(monkeypatch, capsys, tmp_path):
    seen = {}

    async def fake_role_set_once(config, stream_id, role, *, baseline_content, timeout):
        seen["baseline_content"] = baseline_content
        return {"type": "role.set.ok", "session": {"role": role}, "role_baseline": None}

    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "role_set_once", fake_role_set_once)
    monkeypatch.setattr(cli.role_baseline, "load_role_baseline", lambda config, role: None)

    args = Namespace(stream_id="hosta:v2-abc", role="lead", timeout=1.0)
    assert cli.role_set(args) == 0
    assert seen["baseline_content"] is None


def test_role_set_rejects_bad_stream_id(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    args = Namespace(stream_id="nocolon", role="lead", timeout=1.0)
    assert cli.role_set(args) == 2


# -- role get -----------------------------------------------------------------


def test_role_get_sends_payload(monkeypatch, capsys, tmp_path):
    async def fake_role_get_once(config, stream_id, *, timeout):
        return {"type": "role.get.ok", "stream_id": stream_id, "role": "qa",
                "role_source": "role_set", "role_actor": "hosta:nexus"}

    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(cli, "role_get_once", fake_role_get_once)

    args = Namespace(stream_id="hosta:v2-abc", timeout=1.0)
    assert cli.role_get(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["role"] == "qa"
    assert out["role_source"] == "role_set"
