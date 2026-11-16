from agent_orch import cli
from agent_orch.config import Config


def test_explicit_capacity_hold_and_resume_use_exact_target_generation(monkeypatch, tmp_path):
    calls=[]
    async def rpc(config, payload, timeout):
        calls.append(payload)
        return {'type':'capacity_retry.control.ok'}
    monkeypatch.setattr(cli,'load_config',lambda:Config('ws://test','token','fixture',tmp_path))
    monkeypatch.setattr(cli,'discover_leader_stream_id_short',lambda config:'fixture:manager')
    monkeypatch.setattr(cli,'capacity_retry_once',rpc)
    for action in ('hold','resume','status'):
        args=cli.build_parser().parse_args(['capacity-retry',action,'--target','fixture:seat','--generation','selected'])
        assert args.func(args)==0
        assert calls[-1]['expected_session_generation']=='selected'
        assert calls[-1]['action']==action
    args=cli.build_parser().parse_args(['capacity-retry','hold','--all'])
    assert args.func(args)==0 and calls[-1]['all'] is True
    count=len(calls)
    args=cli.build_parser().parse_args(['capacity-retry','resume','--target','fixture:seat'])
    assert args.func(args)==2 and len(calls)==count
