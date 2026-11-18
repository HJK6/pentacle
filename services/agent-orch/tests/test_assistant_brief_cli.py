from agent_orch import cli


def test_brief_cli_payload(monkeypatch):
    calls=[]
    monkeypatch.setattr(cli,'_assistant_call',lambda args,payload:calls.append(payload) or 0)
    assert cli.main(['assistant','brief','--composite-stream-id','test:assistant'])==0
    assert calls==[{'type':'assistant.brief','composite_stream_id':'test:assistant'}]
