import pytest
from agent_orch import cli


@pytest.mark.parametrize(('argv', 'action', 'value', 'brief'), [
    ([], 'read', None, None),
    (['--set', 'host:advisor', '--brief-file', '/tmp/brief'], 'set', 'host:advisor', '/tmp/brief'),
    (['--restore'], 'restore', None, None),
    (['--restore', '--brief-file', '/tmp/brief'], 'restore', None, '/tmp/brief'),
    (['--retry'], 'retry', None, None),
    (['--disable'], 'set', 'disabled', None),
])
def test_authority_actions(monkeypatch, argv, action, value, brief):
    captured = {}
    monkeypatch.setattr(cli, '_assistant_call', lambda args, payload: captured.update(payload) or 0)
    args = cli.build_parser().parse_args(['assistant', 'authority', *argv])
    assert cli.assistant_authority(args) == 0
    assert captured['action'] == action
    assert captured.get('value') == value
    assert captured.get('brief_file') == brief


def test_authority_actions_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['assistant', 'authority', '--restore', '--retry'])
