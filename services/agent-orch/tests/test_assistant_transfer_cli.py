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


def test_standing_authorize_and_resume_wire_shapes(tmp_path,monkeypatch):
    evidence=tmp_path/'extension.json';evidence.write_text(json.dumps({'body_start':0,'body_end':4,'quote':'Move','timezone':'UTC','requested_expires_at':'2026-10-15T00:00:00Z'}))
    calls=[]
    async def call(config,payload,**kwargs):
        calls.append(payload);return {'type':'assistant.transfer.ok'}
    monkeypatch.setattr(cli,'assistant_once',call);monkeypatch.setattr(cli,'load_config',lambda:object())
    base=['assistant','transfer']
    args=cli.build_parser().parse_args(base+['authorize','--authorization-id','grant-1','--command-id','move-1',
        '--request-id','authorize-1','--composite-stream-id','fixture:assistant','--dispatch-id','original',
        '--destination','peer','--member','host:fd','--expires-at','2026-10-15T00:00:00Z','--extension-evidence-file',str(evidence)])
    assert args.func(args)==0
    assert calls[-1]['mode']=='standing-transfer/v1'
    assert calls[-1]['extension_evidence']['quote']=='Move'
    assert 'extension_evidence_file' not in calls[-1]
    args=cli.build_parser().parse_args(base+['resume','--authorization-id','grant-1','--command-id','move-1',
        '--request-id','resume-1','--composite-stream-id','fixture:assistant','--expected-revision','2','--expected-attempt','1'])
    assert args.func(args)==0
    assert calls[-1]['expected_revision']==2 and calls[-1]['expected_attempt']==1
    assert 'pointers' not in calls[-1] and 'dispatch_id' not in calls[-1]


def test_standing_request_requires_stable_identity_and_manifest(monkeypatch,tmp_path):
    import pytest
    pointers=tmp_path/'pointers.json';pointers.write_text('{}')
    args=cli.build_parser().parse_args(['assistant','transfer','request','--authorization-id','grant-1',
        '--command-id','move-1','--expected-revision','1','--composite-stream-id','fixture:assistant',
        '--pointers-file',str(pointers)])
    with pytest.raises(SystemExit,match='--request-id'): args.func(args)


def test_authorization_revoke_uses_independent_transport(monkeypatch):
    args=cli.build_parser().parse_args(['assistant','transfer','authorization-revoke','--authorization-id','grant-1',
        '--request-id','revoke-1','--expected-revision','2','--composite-stream-id','fixture:assistant','--reason','Fixture'])
    calls=[]
    async def call(config,verb,payload,**kwargs):
        calls.append((verb,payload,kwargs));return {'type':'assistant.transfer.ok'}
    monkeypatch.setattr(cli,'consent_once',call);monkeypatch.setattr(cli,'load_config',lambda:object())
    monkeypatch.setattr(cli,'_local_admin_token',lambda _:'fixture-independent-token')
    assert args.func(args)==0
    verb,payload,kw=calls[-1]
    assert verb=='assistant.transfer' and kw['local_admin'] is True
    assert not {'stream_token','from_stream_id'} & payload.keys()
    assert payload['expected_revision']==2
