"""Migration retry identity and independent emergency retirement wire contract."""
import json
from agent_orch import cli


def test_transfer_request_keeps_command_and_full_manifest_on_retry(tmp_path,monkeypatch):
    pointers=tmp_path/'pointers.json';pointers.write_text(json.dumps({'host:fd':[]}))
    args=cli.build_parser().parse_args(['assistant','transfer','request','--command-id','move-1',
        '--dispatch-id','original','--destination','peer','--member','host:fd',
        '--member','host:advisor','--pointers-file',str(pointers)])
    calls=[]
    async def call(config,payload,**kwargs):
        calls.append(payload);return {'type':'assistant.transfer.ok','command':{'state':'preparing'}}
    monkeypatch.setattr(cli,'assistant_once',call);monkeypatch.setattr(cli,'load_config',lambda:object())
    assert args.func(args)==args.func(args)==0
    assert calls[0]['command_id']==calls[1]['command_id']=='move-1'
    assert calls[0]['members']==['host:fd','host:advisor']
    assert calls[0]['pointers']=={'host:fd':[]}


def test_emergency_retirement_binds_explicit_generation_revision_and_request(monkeypatch):
    args=cli.build_parser().parse_args(['lifecycle','revoke','--emergency','--retire-fd-generation',
        '--target','host:fd','--generation','exact','--expected-revision','7',
        '--request-id','revoke-once','--composite-stream-id','example:assistant','--reason','Synthetic emergency'])
    calls=[]
    async def call(config,verb,payload,**kwargs):
        calls.append((payload,kwargs))
        return {'type':'assistant.lifecycle.ok','grant':{'revision':8}}
    monkeypatch.setattr(cli,'consent_once',call);monkeypatch.setattr(cli,'load_config',lambda:object())
    monkeypatch.setattr(cli,'discover_leader_stream_id_short',lambda _:None)
    monkeypatch.setattr(cli,'_local_admin_token',lambda _:'synthetic-local-token')
    assert args.func(args)==0
    payload,kw=calls[-1]
    assert kw['local_admin'] is True
    assert payload['expected_revision']==7  # fresh inspect must not rewrite replay CAS
    assert (payload['target_stream_id'],payload['target_generation'])==('host:fd','exact')
    assert payload['retire_fd_generation'] and payload['request_id']=='revoke-once'
