from __future__ import annotations

import json

from agent_orch import cli
from agent_orch.config import Config


def test_audit_inbound_cli_reads_bounded_recipient_view(monkeypatch, capsys, tmp_path):
    calls = []

    async def fake_inbound_audit(config, stream_id, *, limit, timeout):
        calls.append((config.host_id, stream_id, limit, timeout))
        return {
            "type": "inbound_audit.ok",
            "stream_id": stream_id,
            "frames": [{"text": "exact delivered text"}],
            "read_only": True,
        }

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: Config("ws://hosta-daemon", "tok", "hosta", tmp_path),
    )
    monkeypatch.setattr(cli, "inbound_audit_once", fake_inbound_audit)
    args = cli.build_parser().parse_args([
        "audit", "inbound", "hosta:recipient", "--limit", "7", "--timeout", "4",
    ])

    assert cli.inbound_audit(args) == 0
    assert calls == [("hosta", "hosta:recipient", 7, 4.0)]
    assert json.loads(capsys.readouterr().out)["frames"][0]["text"] == "exact delivered text"
