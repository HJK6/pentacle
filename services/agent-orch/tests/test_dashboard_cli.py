from agent_orch import cli
import pytest

@pytest.mark.parametrize("command", ["add","remove"])
def test_dashboard_parser(command):
    argv=["dashboard",command,"--id","project-map"]
    if command=="add": argv += ["--title","Project map","--url","https://viewer.example.ts.net/app/","--order","2","--hidden"]
    args=cli.build_parser().parse_args(argv)
    assert args.func is cli.dashboard
    assert args.dashboard_command == command
    if command=="add": assert args.order==2 and args.hidden is True


def test_dashboard_authenticated_rpc_and_failure_exit(monkeypatch, capsys):
    calls=[]
    monkeypatch.setattr(cli,"load_config",lambda:object())
    monkeypatch.setattr(cli,"discover_leader_stream_id_short",lambda _:"node-alpha:seat")
    async def rpc(config,payload,**kw):
        calls.append(payload)
        return {"type":"dashboard.add.ok","id":payload["id"]}
    monkeypatch.setattr(cli,"dashboard_once",rpc)
    args=cli.build_parser().parse_args(["dashboard","add","--id","project-map","--title","Map","--url","https://viewer.example.ts.net/app/"])
    assert cli.dashboard(args)==0
    assert calls==[{"type":"dashboard.add","from_stream_id":"node-alpha:seat","id":"project-map","title":"Map","url":"https://viewer.example.ts.net/app/"}]
    async def denied(*a,**kw): return {"type":"dashboard.error","error_code":"dashboard_unauthorized"}
    monkeypatch.setattr(cli,"dashboard_once",denied)
    assert cli.dashboard(args)==1


def test_dashboard_transport_supplies_seat_token_and_expected_response_family(monkeypatch,tmp_path):
    from agent_orch import wsclient
    from agent_orch.config import Config
    import asyncio
    monkeypatch.delenv("AGENT_ORCH_INTERNAL_LEADER_STREAM_ID",raising=False)
    monkeypatch.delenv("AGENT_ORCH_STREAM_TOKEN_FILE",raising=False)
    monkeypatch.setenv("AGENT_ORCH_STREAM_TOKEN","synthetic-dashboard-token")
    async def rpc(config,payload,**kwargs):
        assert payload["stream_token"]=="synthetic-dashboard-token"
        assert kwargs["prefix"]=="dashboard"
        assert kwargs["from_stream_id"]=="node-alpha:seat"
        assert payload["request_id"].startswith("dashboard-")
        return {"type":"dashboard.remove.ok"}
    monkeypatch.setattr(wsclient,"_one_shot_rpc",rpc)
    result=asyncio.run(wsclient.dashboard_once(Config("ws://unused","","node-alpha",tmp_path),{"type":"dashboard.remove","id":"hosted-example","from_stream_id":"node-alpha:seat"}))
    assert result["type"]=="dashboard.remove.ok"
