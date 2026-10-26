"""Screen lease argument, envelope, authentication and retry contracts."""

import asyncio
import json
import pytest
from agent_orch import cli, wsclient


@pytest.mark.parametrize(
    "argv,fields",
    [
        (
            ["acquire", "--host", "test", "--reason", "test work"],
            {"host": "test", "reason": "test work"},
        ),
        (["renew", "lease-1", "--ttl", "20"], {"lease_id": "lease-1", "ttl": 20}),
        (["release", "lease-1"], {"lease_id": "lease-1", "force": False}),
        (["release", "--host", "test", "--force"], {"host": "test", "force": True}),
        (["status", "--host", "test"], {"host": "test"}),
        (["status"], {}),
    ],
)
def test_wire(monkeypatch, capsys, argv, fields):
    calls = []

    async def once(config, payload, **kw):
        calls.append(payload)
        return {
            "type": payload["type"] + ".ok",
            "hosts": [{"host": "test", "state": "free", "lease": None}],
        }

    monkeypatch.setattr(cli, "screen_lease_once", once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    args = cli.build_parser().parse_args(["screen", *argv, "--json"])
    assert args.func(args) == 0
    assert calls == [{"type": "screen.lease." + argv[0], **fields}]
    assert json.loads(capsys.readouterr().out)["type"].endswith(".ok")


def test_errors_and_plain_status(monkeypatch, capsys):
    reply = {
        "type": "screen.lease.release.error",
        "error_code": "screen_lease_not_found",
        "state": "released",
    }

    async def once(*a, **kw):
        return reply

    monkeypatch.setattr(cli, "screen_lease_once", once)
    monkeypatch.setattr(cli, "load_config", lambda: object())
    args = cli.build_parser().parse_args(["screen", "release", "lease-1"])
    assert args.func(args) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "released"
    reply = {
        "type": "screen.lease.status.ok",
        "hosts": [{"host": "test", "state": "free", "lease": None}],
    }
    args = cli.build_parser().parse_args(["screen", "status"])
    assert args.func(args) == 0 and capsys.readouterr().out == "test: free\n"
    for argv in (
        ["release"],
        ["release", "lease-1", "--host", "test"],
        ["acquire", "--host", "test"],
        ["renew"],
    ):
        with pytest.raises(SystemExit) as e:
            cli.build_parser().parse_args(["screen", *argv])
        assert e.value.code == 2


def test_rpc_public_status_and_authenticated_mutators(monkeypatch):
    calls = []

    async def rpc(config, payload, **kw):
        calls.append((payload, kw))
        return {"type": payload["type"] + ".ok"}

    monkeypatch.setattr(wsclient, "_one_shot_rpc", rpc)
    monkeypatch.setattr(wsclient, "_resolved_rpc_from_stream_id", lambda: "test:seat")
    monkeypatch.setattr(wsclient, "_stream_token_from_env", lambda: "fixture-token")
    asyncio.run(wsclient.screen_lease_once(object(), {"type": "screen.lease.status"}))
    assert "stream_token" not in calls[-1][0] and calls[-1][1]["from_stream_id"] is None
    for v in ["acquire", "renew", "release"]:
        asyncio.run(wsclient.screen_lease_once(object(), {"type": "screen.lease." + v}))
        assert (
            calls[-1][0]["from_stream_id"] == "test:seat"
            and calls[-1][0]["stream_token"] == "fixture-token"
        )
        assert "screen.lease." + v not in wsclient.RPC_RETRY_ELIGIBLE_TYPES
    with pytest.raises(ValueError):
        asyncio.run(wsclient.screen_lease_once(object(), {"type": "tell"}))
