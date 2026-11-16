from __future__ import annotations

import json

from agent_orch import cli
from agent_orch.config import Config


def test_ledger_get_reads_full_tell_over_direct_cross_host_rpc(monkeypatch, capsys, tmp_path):
    full_text = "cross-host brief\n" + "x" * 1000
    calls = []

    async def fake_ledger_get(config, tell_id, *, timeout):
        calls.append((config.host_id, tell_id, timeout))
        return {
            "type": "ledger_get.ok",
            "tell_id": tell_id,
            "tell": {"text": full_text, "delivery_status": "delivered", "delivery_attempts": 1},
        }

    monkeypatch.setattr(cli, "load_config", lambda: Config("ws://127.0.0.1:7791", "tok", "hostc", tmp_path))
    monkeypatch.setattr(cli, "ledger_get_once", fake_ledger_get)
    args = cli.build_parser().parse_args(["ledger", "get", "tell-123", "--timeout", "4"])

    assert cli.ledger_get(args) == 0
    assert calls == [("hostc", "tell-123", 4.0)]
    assert json.loads(capsys.readouterr().out)["tell"]["text"] == full_text

