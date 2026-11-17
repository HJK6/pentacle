"""The two publication commands share owed fields, but not batch support."""
import pytest
import agent_orch.cli as cli
from test_send_file_cli import wire

@pytest.mark.parametrize('action,tail',[('open',['--label','result','--message-id','input-a','--due','+2h']),('close',['owed-012345abcdef','--void','--reason','This was opened in error']),('list',['--closed','--since','2026-01-01T00:00:00Z']),('show',['owed-012345abcdef'])])
def test_owed_verbs(monkeypatch,action,tail):
    seen=[];monkeypatch.setattr(cli,'_assistant_call',lambda a,p:seen.append(p) or 0)
    args=cli.build_parser().parse_args(['assistant','owed',action,'--composite-stream-id','local:assistant',*tail])
    assert args.func(args)==0 and seen[0]['type']=='assistant.owed.'+action


def test_publish_fields(monkeypatch):
    seen=[];monkeypatch.setattr(cli,'_assistant_call',lambda a,p:seen.append(p) or 0)
    a=cli.build_parser().parse_args(['assistant','publish','--request-id','note-a','--composite-stream-id','local:assistant','--proactive','--publish-kind','prose','--message','Result','--note-type','result','--ref','owed:owed-012345abcdef','--owes','next','--owes-due','+1h','--owes-lane','lane-a','--closes','owed-012345abcdef'])
    a.func(a);assert seen[0]['owes']=='next' and seen[0]['closes']==['owed-012345abcdef']


def test_send_file_fields(wire):
    a=cli.build_parser().parse_args(['send-file','--upload-id','synthetic-upload','--to','local:assistant','--dispatch-id','dispatch-a','--reply-to-message-id','input-a','--publish-kind','prose','--owes','next','--owes-due','+1h','--owes-spec','spec-a','--closes','owed-012345abcdef'])
    assert cli.send_file(a)==0
    assert wire[0][1]['owes']=='next' and wire[0][1]['owes_spec']=='spec-a'
    with pytest.raises(SystemExit):cli.build_parser().parse_args(['send-file','--upload-id','synthetic-upload','--reply-to-message-ids','a','b'])

@pytest.mark.parametrize('valid',[True,False])
def test_send_file_returns_only_validated_owed_receipts(wire,monkeypatch,capsys,valid):
    import json
    async def publish(*a,**k):
        return {'type':'assistant.publish.ok','owed_id':'owed-012345abcdef' if valid else 'arbitrary text','closed_owed_ids':['owed-fedcba543210'] if valid else ['arbitrary text']}
    monkeypatch.setattr(cli,'assistant_once',publish)
    a=cli.build_parser().parse_args(['send-file','--upload-id','synthetic-upload','--to','local:assistant','--dispatch-id','dispatch-a','--reply-to-message-id','input-a','--publish-kind','prose','--owes','next'])
    assert cli.send_file(a)==0
    result=json.loads(capsys.readouterr().out)
    if valid:assert result['owed_id']=='owed-012345abcdef' and result['closed_owed_ids']==['owed-fedcba543210']
    else:assert 'owed_id' not in result and 'closed_owed_ids' not in result
