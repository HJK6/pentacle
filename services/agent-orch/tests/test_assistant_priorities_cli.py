"""Priority command requests and user-facing output."""
import json
import pytest
import agent_orch.cli as cli

@pytest.mark.parametrize('action,tail', [
    ('list',['--revision','2','--json']),
    ('add',['--rank','1','--label','label','--words','quoted words','--source','message:abc']),
    ('move',['p1','--rank','2','--source','seat:host:seat']),
    ('remove',['p1','--done']),
    ('owner',['p1','--owner','none']),
])
def test_priorities(monkeypatch, capsys, action, tail):
    calls=[]
    async def rpc(config, payload, timeout):
        calls.append(payload)
        return {'type':payload['type']+'.ok','revision':2,'items':[{'rank':1,'id':'p1','label':'label','words':'quoted words','owner':None,'source':'message:abc','spec_id':None,'words_checked':True}]}
    monkeypatch.setattr(cli, 'assistant_once', rpc)
    monkeypatch.setattr(cli, 'load_config', lambda: None)
    args=cli.build_parser().parse_args(['assistant','priorities',action,*tail])
    assert args.func(args)==0
    assert calls[0]['type']=='assistant.priorities.'+action
    out=capsys.readouterr().out
    if action=='list': assert json.loads(out)['revision']==2
    else: assert 'quoted words' in out and 'checked' in out
