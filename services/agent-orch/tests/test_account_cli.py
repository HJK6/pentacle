import json
from agent_orch import cli


def test_account_default_payload_has_no_home_or_profile_overrides(monkeypatch, capsys):
    from agent_orch import config, stream_id, wsclient
    monkeypatch.setattr(config, "load_config", lambda: object())
    monkeypatch.setattr(stream_id, "discover_leader_stream_id_short", lambda _: "host-a:front-desk")
    seen = []
    async def rpc(config, payload, **kwargs):
        seen.append(payload); return {"type": "account.ok", "accepted": True}
    monkeypatch.setattr(wsclient, "account_once", rpc)
    args = cli.build_parser().parse_args(["account", "default", "set", "--host", "host-a", "--provider", "codex", "--account", "primary"])
    assert args.func(args) == 0
    assert seen[0]["action"] == "default-set" and seen[0]["host"] == "host-a"
    assert seen[0]["from_stream_id"] == "host-a:front-desk"
    assert "home" not in seen[0] and "profile" not in seen[0]


def test_usage_account_reads_daemon_metadata_without_legacy_probe(monkeypatch, capsys):
    from agent_orch import config, stream_id, wsclient, usage_readback
    monkeypatch.setattr(config, "load_config", lambda: object())
    monkeypatch.setattr(stream_id, "discover_leader_stream_id_short", lambda _: "host-a:front-desk")
    def forbidden(*args, **kwargs): raise AssertionError("default-home probe reached")
    monkeypatch.setattr(usage_readback, "read_host", forbidden)
    async def rpc(config, payload, **kwargs):
        assert payload["action"] == "status"
        return {"type": "account.ok", "enabled": True, "accounts": [
            {"provider": "codex", "alias": "primary", "host": "host-a", "pool_id": "pool-a"},
            {"provider": "codex", "alias": "spare", "host": "host-a", "pool_id": "pool-b"}],
            "quota_pools": [{"provider": "codex", "pool_id": "pool-a", "fresh": True},
                            {"provider": "codex", "pool_id": "pool-b", "fresh": False}]}
    monkeypatch.setattr(wsclient, "account_once", rpc)
    args = cli.build_parser().parse_args(["usage", "--host", "host-a", "--account", "primary", "--json"])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["accounts"]) == 1 and result["accounts"][0]["alias"] == "primary"
    assert len(result["quota_pools"]) == 1 and result["quota_pools"][0]["pool_id"] == "pool-a"


def test_front_desk_can_request_existing_handoff_to_a_named_account(monkeypatch,tmp_path,capsys):
    from agent_orch.config import Config
    config=Config("ws://unused","","host-a",tmp_path)
    monkeypatch.setattr(cli,"load_config",lambda:config)
    monkeypatch.setattr(cli,"discover_leader_stream_id_short",lambda _:"host-a:front-desk")
    monkeypatch.setattr(cli,"_handoff_source_row",lambda *a,**k:{"stream_id":"host-a:conversation","provider":"codex",
        "effective_model":"gpt-5.6-sol","effective_effort":"high","role":None})
    seen=[]
    async def spawn(config,payload,timeout):
        seen.append(payload)
        return {"type":"spawn.ok","request_id":payload["request_id"],"session":{"stream_id":"host-a:successor"},
            "initial_prompt_delivery":{"delivery_status":"delivered","to_stream_id":"host-a:successor"}}
    monkeypatch.setattr(cli,"spawn_once",spawn)
    args=cli.build_parser().parse_args(["spawn","--handoff","--handoff-from","host-a:conversation","--account","spare",
        "--initial-prompt","Checkpoint: continue the existing conversation; retain its pending work."])
    assert args.func(args)==0
    assert seen[0]["account"]=="spare" and seen[0]["handoff_from_stream_id"]=="host-a:conversation"
    assert seen[0]["initial_prompt"].startswith("Checkpoint:")
    assert "resume_session_id" not in seen[0]
