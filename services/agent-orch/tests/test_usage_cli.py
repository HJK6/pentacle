"""The existing CLI surfaces preserve provider-labelled token snapshots."""
import json
from argparse import Namespace

from agent_orch import cli


USAGE = {'host': 'amaterasu', 'provider': 'codex', 'incomplete': True,
         'tokens': {'input_total': 100, 'cached_input': 40, 'output': 20, 'reasoning': 7}}


def test_list_preserves_usage_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load_config', lambda: object())
    monkeypatch.setattr(cli, 'fetch_snapshot', lambda *a, **k: {
        'sessions': [{'stream_id': 'amaterasu:test', 'usage': USAGE}]})
    assert cli.list_sessions(Namespace(timeout=1)) == 0
    assert json.loads(capsys.readouterr().out)[0]['usage'] == USAGE


def test_pretty_inspect_displays_provider_usage(capsys):
    cli._print_inspect_pretty({'stream_id': 'amaterasu:test', 'session': {'usage': USAGE}})
    rendered = capsys.readouterr().out
    assert 'usage:' in rendered
    assert 'input_total' in rendered and '100' in rendered
    assert 'incomplete' in rendered and 'amaterasu' in rendered
