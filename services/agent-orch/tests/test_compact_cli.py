"""Self-compaction CLI identity and real RPC reply/retry coverage."""
import asyncio

import pytest

from agent_orch import cli, wsclient
from test_watch_wake_rpc import ReplySocket, configure


def test_compact_payload_and_printed_retry_identity(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cli, "_coordination_request", lambda args, payload: seen.append(payload) or 0)
    args = cli.build_parser().parse_args(["compact", "--request-id", "checkpoint-one"])
    assert args.func(args) == 0
    assert seen == [{"type": "compact", "request_id": "checkpoint-one"}]
    assert "checkpoint-one" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["other:seat"], ["--from-stream-id", "other:seat"], ["--force"], ["--urgent"]])
def test_compact_has_no_target_or_force_option(extra):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["compact", *extra])
    assert exc.value.code == 2


@pytest.mark.parametrize("outcome", ["ok", "error"])
def test_compact_consumes_reply_and_retries_same_identity(monkeypatch, tmp_path, outcome):
    expected = {"type": f"compact.{outcome}", "request_id": "checkpoint-one"}
    sockets = [ReplySocket([]), ReplySocket([
        {**expected, "request_id": "other"},
        {"type": "send.result", "request_id": "checkpoint-one"}, expected,
    ])]
    config, connected = configure(monkeypatch, tmp_path, sockets, attempts=2)
    monkeypatch.setenv("AGENT_ORCH_RPC_RETRY_DEADLINE_S", "5")
    result = asyncio.run(wsclient.coordination_once(config, {
        "type": "compact", "request_id": "checkpoint-one", "from_stream_id": "test:owner",
    }, timeout=0.03))
    assert result == expected
    assert len(connected) == 2
    assert [s.sent[0]["request_id"] for s in sockets] == ["checkpoint-one"] * 2
    assert all(s.closed for s in sockets)
