import json
import pytest
from agent_orch import cli


def upgrade_args():
    return ["account", "upgrade", "--host", "host-target", "--provider", "codex", "--account", "ours",
        "--login-generation", "old", "--new-generation", "new", "--operation-id", "upgrade-one",
        "--expected-version", "fixture-new", "--expected-executable-sha256", "a"*64, "--reason", "approved_upgrade"]


def test_upgrade_sends_exact_artifact_contract_with_bounded_default_deadline(monkeypatch):
    from agent_orch import config, stream_id, wsclient
    monkeypatch.setattr(config, "load_config", lambda: object())
    monkeypatch.setattr(stream_id, "discover_leader_stream_id_short", lambda _: "host-a:front-desk")
    seen = []
    async def rpc(config, payload, **kwargs):
        seen.append((payload, kwargs)); return {"type":"account.ok", "result":{"outcome":"upgraded"}}
    monkeypatch.setattr(wsclient, "account_once", rpc)
    args = cli.build_parser().parse_args(upgrade_args())
    assert args.func(args) == 0
    payload, options = seen[0]
    assert options == {"timeout":210}
    assert payload["action"] == "upgrade" and payload["login_generation"] == "old" and payload["new_generation"] == "new"
    assert payload["expected_version"] == "fixture-new" and payload["expected_executable_sha256"] == "a"*64
    assert not ({"home", "executable", "profile", "binding"} & payload.keys())


@pytest.mark.parametrize("flag,value", [("--login-generation", "active"), ("--new-generation", "old"),
    ("--provider", "claude"), ("--expected-executable-sha256", "invalid"), ("--reason", "")])
def test_upgrade_invalid_payload_is_rejected_before_transport(monkeypatch, flag, value):
    from agent_orch import config
    monkeypatch.setattr(config, "load_config", lambda: pytest.fail("invalid request reached config/transport"))
    argv = upgrade_args(); argv[argv.index(flag)+1] = value
    args = cli.build_parser().parse_args(argv)
    assert args.func(args) == 2


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


@pytest.mark.parametrize("reconcile_of",[None,"prior-repin"])
def test_pin_snapshot_and_repin_payloads_use_existing_authenticated_transport(tmp_path,monkeypatch,reconcile_of):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"host-a:front-desk")
    seen=[]
    async def rpc(config,payload,**kwargs): seen.append((payload,kwargs));return {"type":"account.ok"}
    monkeypatch.setattr(wsclient,"account_once",rpc)
    snapshot=tmp_path/"pair.json";snapshot.write_text('{"schema":1,"revision":1}')
    args=cli.build_parser().parse_args(["account","pin-set","--pin-file",str(snapshot),"--expected-revision","0",
        "--operation-id","initial","--reason","reviewed"])
    assert args.func(args)==0
    payload,options=seen.pop()
    assert payload["pin"]=={"schema":1,"revision":1} and payload["expected_revision"]==0
    assert "login_generation" not in payload
    args=cli.build_parser().parse_args(["account","repin","--host","host-a","--provider","claude","--account","primary",
        "--login-generation","old","--new-generation","new","--operation-id","same-home",
        "--expected-pin-revision","2","--expected-pin-digest","a"*64,"--reason","reviewed"]+(["--reconcile-of",reconcile_of] if reconcile_of else []))
    assert args.func(args)==0
    payload,options=seen.pop()
    assert payload["action"]=="repin" and options["timeout"]==330
    assert payload.get("reconcile_of")==reconcile_of
    assert not {"home","executable","profile_ref"}&payload.keys()


def test_duplicate_pin_json_refuses_before_rpc(tmp_path,monkeypatch):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"host-a:front-desk")
    async def forbidden(*args,**kwargs): pytest.fail("duplicate snapshot reached transport")
    monkeypatch.setattr(wsclient,"account_once",forbidden)
    snapshot=tmp_path/"pair.json";snapshot.write_text('{"schema":1,"schema":2}')
    args=cli.build_parser().parse_args(["account","pin-set","--pin-file",str(snapshot)])
    assert args.func(args)==2


def test_local_admin_pin_transport_does_not_depend_on_stale_seat_identity(monkeypatch):
    import asyncio
    from agent_orch import wsclient
    seen=[]
    async def rpc(config,payload,**kwargs):
        seen.append((payload,kwargs)); return {"type":"account.ok"}
    monkeypatch.setattr(wsclient,"_one_shot_rpc",rpc)
    result=asyncio.run(wsclient.account_once(object(),{"type":"account","action":"pin-status",
        "from_stream_id":"host-a:dead-fd","stream_token":"fixture-stale-seat",
        "local_admin_token":"fixture-admin"}))
    assert result["type"]=="account.ok"
    payload,options=seen[0]
    assert "from_stream_id" not in payload and "stream_token" not in payload
    assert options["infer_from_env"] is False
    assert payload["local_admin_token"]=="fixture-admin"


@pytest.mark.parametrize("action",["repin","pin-status","pin-set"])
def test_initial_activation_pair_and_readiness_inputs_reach_existing_transport(tmp_path,monkeypatch,action):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"fixture-host:front-desk")
    calls=[]
    async def rpc(config,payload,**options):calls.append((payload,options));return {"type":"account.ok"}
    monkeypatch.setattr(wsclient,"account_once",rpc)
    pair=tmp_path/"pair.json";pair.write_text(json.dumps({"schema":1,"revision":1}))
    parked=tmp_path/"parked.json";parked.write_text("[]")
    argv=["account",action,"--host","fixture-host"]
    if action=="repin":argv += ["--target-pin-file",str(pair),"--expected-pin-revision","0"]
    else:argv += ["--pin-file",str(pair),"--initial-parked-cells-file",str(parked),"--expected-readiness-digest","a"*64]
    args=cli.build_parser().parse_args(argv)
    assert args.func(args)==0
    payload,options=calls[0]
    assert payload["target_pin" if action=="repin" else "pin"]=={"schema":1,"revision":1}
    assert options["timeout"]==330
    assert not {"home","executable","profile_ref","binding"}&payload.keys()
    if action!="repin":assert payload["initial_parked_cells"]==[] and payload["expected_readiness_digest"]=="a"*64


def test_active_repin_refuses_pre_enforcement_target_file_before_rpc(tmp_path,monkeypatch):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"fixture-host:front-desk")
    async def forbidden(*args,**kwargs):pytest.fail("active repin sent an initial target")
    monkeypatch.setattr(wsclient,"account_once",forbidden)
    pair=tmp_path/"pair.json";pair.write_text('{}')
    args=cli.build_parser().parse_args(["account","repin","--target-pin-file",str(pair),"--expected-pin-revision","1"])
    assert args.func(args)==2
