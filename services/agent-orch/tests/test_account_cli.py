import json
import pytest
from agent_orch import cli


def test_fixed_pool_and_generation_override_fields_use_existing_account_rpc(monkeypatch,capsys):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,'load_config',lambda:object())
    monkeypatch.setattr(stream_id,'discover_leader_stream_id_short',lambda _:'host-a:front-desk')
    seen=[]
    async def rpc(config,payload,**kwargs): seen.append(payload);return {'type':'account.ok'}
    monkeypatch.setattr(wsclient,'account_once',rpc)
    pool=['account','pool-control-set','--provider','codex','--pool-id','pool-primary','--expected-revision','0',
          '--operation-id','pool-order','--blocked','false','--stop-pct','100','--window','codex:10080',
          '--reason','bounded ceiling','--authority-ref','operator-order']
    args=cli.build_parser().parse_args(pool)
    assert args.func(args)==0
    assert seen[-1]['stop_pct']==100 and seen[-1]['blocked'] is False
    assert seen[-1]['window']=='codex:10080' and 'login_generation' not in seen[-1]
    seat=['account','seat-override-set','--stream','host-a:worker','--expected-generation','worker-one',
          '--expected-revision','0','--operation-id','seat-order','--enabled','true','--reason','owned exception',
          '--authority-ref','operator-order']
    args=cli.build_parser().parse_args(seat)
    assert args.func(args)==0 and seen[-1]['enabled'] is True
    assert seen[-1]['expected_generation']=='worker-one' and 'login_generation' not in seen[-1]


@pytest.mark.parametrize('pct',['nan','inf','-1','101'])
def test_pool_cli_rejects_nonfinite_and_out_of_range_before_transport(pct,monkeypatch):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,'load_config',lambda:object())
    monkeypatch.setattr(stream_id,'discover_leader_stream_id_short',lambda _:'host-a:front-desk')
    async def forbidden(*args,**kwargs): raise AssertionError('invalid control reached transport')
    monkeypatch.setattr(wsclient,'account_once',forbidden)
    args=cli.build_parser().parse_args(['account','pool-control-set','--blocked','false','--stop-pct',pct,'--window','weekly'])
    assert args.func(args)==2


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
    if action=="repin":
        argv += ["--provider","claude","--account","primary","--login-generation","old","--new-generation","new",
                 "--operation-id","fixture-repin","--reason","reviewed","--target-pin-file",str(pair),
                 "--expected-pin-revision","0","--expected-pin-digest","a"*64]
    elif action=="pin-set":
        argv += ["--pin-file",str(pair),"--expected-revision","0","--operation-id","fixture-pin-set",
                 "--reason","reviewed","--initial-parked-cells-file",str(parked),"--expected-readiness-digest","a"*64]
    else:
        argv += ["--pin-file",str(pair),"--initial-parked-cells-file",str(parked),"--expected-readiness-digest","a"*64]
    args=cli.build_parser().parse_args(argv)
    assert args.func(args)==0
    payload,options=calls[0]
    assert payload["target_pin" if action=="repin" else "pin"]=={"schema":1,"revision":1}
    assert options["timeout"]==330
    assert not {"home","executable","profile_ref","binding"}&payload.keys()
    if action!="repin":assert payload["initial_parked_cells"]==[] and payload["expected_readiness_digest"]=="a"*64


def _mock_account_rpc(monkeypatch):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"fixture-host:front-desk")
    calls=[]
    async def rpc(config,payload,**options):calls.append((payload,options));return {"type":"account.ok"}
    monkeypatch.setattr(wsclient,"account_once",rpc)
    return calls


def test_rpc_level_explicit_null_cap_clear_is_preserved(monkeypatch):
    calls=_mock_account_rpc(monkeypatch)
    clear=cli.build_parser().parse_args(["account","pool-control-set","--provider","codex","--pool-id","pool-a",
        "--expected-revision","0","--operation-id","clear-one","--blocked","false","--stop-pct","none",
        "--window","none","--reason","clear cap","--authority-ref","operator-order"])
    assert clear.func(clear)==0
    assert calls[-1][0]["blocked"] is False
    assert calls[-1][0]["stop_pct"] is None and calls[-1][0]["window"] is None

def test_rpc_level_absent_pin_recovery_sentinel_is_preserved(monkeypatch):
    calls=_mock_account_rpc(monkeypatch)
    recover=cli.build_parser().parse_args(["account","pin-set","--recover-receipt","approved-one",
        "--expected-raw-digest","absent","--expected-high-water-revision","4","--operation-id","recover-one",
        "--reason","restore pair"])
    assert recover.func(recover)==0
    assert calls[-1][0]["recover_receipt"]=="approved-one"
    assert calls[-1][0]["expected_raw_digest"]=="absent"
    assert calls[-1][0]["expected_high_water_revision"]==4
    assert "pin" not in calls[-1][0] and "expected_revision" not in calls[-1][0]


@pytest.mark.parametrize("entry,argv",[
    ("main-argv", ["--unrecognized=SECRET_MARKER","account","status"]),
    ("main-argv", ["account","status","--unrecognized=SECRET_MARKER"]),
    ("main-argv", ["account","status","--timeout=SECRET_MARKER"]),
    ("main-argv", ["account","status","--kind=SECRET_MARKER"]),
    ("main-argv", ["account","--unrecognized=SECRET_MARKER"]),
    ("main-process", ["--unrecognized=SECRET_MARKER","account","status"]),
    ("main-process", ["account","status","--unrecognized=SECRET_MARKER"]),
    ("parse-args", ["--unrecognized=SECRET_MARKER","account","status"]),
    ("parse-args", ["account","status","--unrecognized=SECRET_MARKER"]),
    ("parse-args", ["account","--unrecognized=SECRET_MARKER"]),
])
def test_account_parse_errors_never_echo_supplied_text(entry,argv,capsys,monkeypatch):
    monkeypatch.setattr(cli,"_invoke_command",lambda _args:pytest.fail("parse failure dispatched a command"))
    with pytest.raises(SystemExit) as exc:
        if entry=="main-argv":
            cli.main(argv)
        elif entry=="main-process":
            monkeypatch.setattr("sys.argv",["agent-orch",*argv])
            cli.main()
        else:
            cli.build_parser().parse_args(argv)
    assert exc.value.code==2
    captured=capsys.readouterr()
    assert "SECRET_MARKER" not in captured.out
    assert "SECRET_MARKER" not in captured.err


def test_account_parse_state_does_not_leak_when_parser_is_reused(capsys):
    parser=cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--unrecognized=ACCOUNT_SECRET","account","status"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--unrecognized=NON_ACCOUNT_DETAIL","start"])
    captured=capsys.readouterr()
    assert "ACCOUNT_SECRET" not in captured.out+captured.err
    assert "NON_ACCOUNT_DETAIL" in captured.err


def test_direct_account_subparser_parse_error_uses_safe_format(capsys):
    parser=cli.build_parser()
    subparsers=next(action for action in parser._actions if getattr(action,"choices",None) and "account" in action.choices)
    with pytest.raises(SystemExit) as exc:
        subparsers.choices["account"].parse_args(["status","--unrecognized=SUBPARSER_SECRET"])
    assert exc.value.code==2
    captured=capsys.readouterr()
    assert "SUBPARSER_SECRET" not in captured.out+captured.err


def test_non_account_parser_errors_keep_standard_diagnostics(capsys):
    with pytest.raises(SystemExit):
        cli.main(["start","--unrecognized=NON_ACCOUNT_DETAIL"])
    assert "NON_ACCOUNT_DETAIL" in capsys.readouterr().err


def test_local_file_parse_error_keeps_failing_option_label(monkeypatch,capsys,tmp_path):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"fixture-host:front-desk")
    async def forbidden(*a,**k):pytest.fail("invalid file input reached RPC")
    monkeypatch.setattr(wsclient,"account_once",forbidden)
    bad_file=tmp_path/"bad-pin.json";bad_file.write_text('{"broken":')
    args=cli.build_parser().parse_args(["account","pin-status","--pin-file",str(bad_file)])
    assert args.func(args)==2
    response=json.loads(capsys.readouterr().out)
    assert response=={"type":"account.error","error_code":"account_request_invalid","option":"--pin-file"}


def test_successful_first_file_does_not_keep_failure_attribution(monkeypatch,capsys,tmp_path):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"fixture-host:front-desk")
    async def forbidden(*a,**k):pytest.fail("invalid file combination reached RPC")
    monkeypatch.setattr(wsclient,"account_once",forbidden)
    pin_file=tmp_path/"pin.json";pin_file.write_text(json.dumps({"schema":1,"revision":1}))
    target_file=tmp_path/"target.json";target_file.write_text(json.dumps({"schema":1,"revision":1}))
    args=cli.build_parser().parse_args(["account","pin-status","--pin-file",str(pin_file),
        "--target-pin-file",str(target_file)])
    assert args.func(args)==2
    response=json.loads(capsys.readouterr().out)
    assert response=={"type":"account.error","error_code":"account_request_invalid"}


def test_report_failure_kind_remains_optional_like_main(monkeypatch):
    calls=_mock_account_rpc(monkeypatch)
    args=cli.build_parser().parse_args(["account","report-failure","--host","fixture-host",
        "--provider","claude","--account","primary"])
    assert args.func(args)==0
    assert calls[-1][0]["action"]=="report-failure" and "kind" not in calls[-1][0]


def test_identity_kind_remains_rejected_for_status_like_main(monkeypatch):
    monkeypatch.setattr(cli,"_invoke_command",lambda _args:pytest.fail("invalid kind reached dispatch"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["account","status","--kind","identity"])
    assert exc.value.code==2


def test_incomplete_upgrade_and_revalidation_keep_existing_local_refusals(monkeypatch,capsys):
    from agent_orch import config,wsclient
    monkeypatch.setattr(config,"load_config",lambda:pytest.fail("invalid request reached configuration/RPC"))
    monkeypatch.setattr(wsclient,"account_once",lambda *a,**k:pytest.fail("invalid request reached RPC"))
    for argv in (["account","upgrade","--host","h","--provider","codex","--account","a"],
                 ["account","revalidate-health","--host","h","--provider","claude","--account","a"]):
        args=cli.build_parser().parse_args(argv)
        assert args.func(args)==2
        assert json.loads(capsys.readouterr().out)["error_code"]=="account_request_invalid"


def test_active_repin_refuses_pre_enforcement_target_file_before_rpc(tmp_path,monkeypatch):
    from agent_orch import config,stream_id,wsclient
    monkeypatch.setattr(config,"load_config",lambda:object())
    monkeypatch.setattr(stream_id,"discover_leader_stream_id_short",lambda _:"fixture-host:front-desk")
    async def forbidden(*args,**kwargs):pytest.fail("active repin sent an initial target")
    monkeypatch.setattr(wsclient,"account_once",forbidden)
    pair=tmp_path/"pair.json";pair.write_text('{}')
    args=cli.build_parser().parse_args(["account","repin","--target-pin-file",str(pair),"--expected-pin-revision","1"])
    assert args.func(args)==2


@pytest.mark.parametrize(
    "selector, json_output, expected",
    [
        pytest.param(["--account", ""], True, (2, 0, 0, 0), id="empty"),
        pytest.param(["--account", " \t\n"], True, (2, 0, 0, 0), id="whitespace"),
        pytest.param(["--account="], False, (2, 0, 0, 0), id="empty-equals"),
        pytest.param(["--account", "fixture-account"], True, (0, 0, 1, 1), id="named-json"),
        pytest.param(["--account", "fixture-account"], False, (0, 0, 1, 1), id="named-text"),
        pytest.param([], True, (0, 1, 0, 0), id="omitted-json"),
        pytest.param([], False, (0, 1, 0, 0), id="omitted-text"),
    ],
)
def test_usage_selector_routes_through_the_public_cli(
    monkeypatch, capsys, selector, json_output, expected
):
    from agent_orch import config, stream_id, usage_readback, wsclient

    calls = {"host": [], "registry": [], "config": []}
    fixture_config = object()
    host_rows = [{"host": "fixture-host", "provider": "codex", "pct": 25, "outcome": "fresh"}]
    account = {
        "provider": "codex", "alias": "fixture-account", "host": "fixture-host",
        "generation": "fixture-generation", "pool_id": "fixture-pool", "health": "unknown",
    }
    pool = {"provider": "codex", "pool_id": "fixture-pool", "fresh": False, "age_seconds": 901}

    def load_fixture():
        calls["config"].append(True)
        return fixture_config

    def read_host(host, *, max_age_s):
        calls["host"].append((host, max_age_s))
        return host_rows

    async def registry(conf, payload, *, timeout):
        assert conf is fixture_config
        calls["registry"].append((payload["action"], payload["from_stream_id"], timeout))
        return {
            "type": "account.ok", "enabled": True,
            "accounts": [
                account,
                {**account, "host": "fixture-other-host", "pool_id": "fixture-other-pool"},
            ],
            "quota_pools": [pool, {**pool, "pool_id": "fixture-other-pool"}],
        }

    monkeypatch.setattr(config, "load_config", load_fixture)
    monkeypatch.setattr(cli, "load_config", load_fixture)
    monkeypatch.setattr(stream_id, "discover_leader_stream_id_short", lambda _: "fixture-host:front-desk")
    monkeypatch.setattr(usage_readback, "read_host", read_host)
    monkeypatch.setattr(wsclient, "account_once", registry)

    argv = ["usage", "--host", "fixture-host", "--max-age-seconds", "900", *selector]
    if json_output:
        argv.append("--json")
    try:
        status = cli.main(argv)
    except SystemExit as exc:
        status = exc.code
    output = capsys.readouterr()

    # Exit status plus host reader, registry transport and config call counts.
    assert (status, len(calls["host"]), len(calls["registry"]), len(calls["config"])) == expected
    if status:
        assert not output.out
        assert "usage:" in output.err and "--account" in output.err
        assert "non-empty" in output.err
    elif selector:
        assert calls["registry"] == [("status", "fixture-host:front-desk", 30)]
        assert not output.err
        if json_output:
            assert json.loads(output.out) == {"enabled": True, "accounts": [account], "quota_pools": [pool]}
        else:
            assert output.out.splitlines() == [
                "PROVIDER ACCOUNT HOST GENERATION POOL HEALTH",
                "codex fixture-account fixture-host fixture-generation fixture-pool unknown",
                "codex fixture-pool: unknown/stale (age_seconds=901)",
            ]
    else:
        assert calls["host"] == [("fixture-host", 900)]
        assert not output.err
        if json_output:
            assert json.loads(output.out) == host_rows
        else:
            assert output.out == usage_readback.render_table(host_rows) + "\n"
