"""Account binding verdict regression controls."""
import json

from .test_deploy_script import deploy_mod

def test_failed_row_without_binding_keeps_original_reason(tmp_path):
    import subprocess
    machines = tmp_path / 'machines.json'
    machines.write_text(json.dumps([{'name': 'primary', 'ssh_target': None}]))
    key = {'host': 'primary', 'provider': 'claude', 'prompt_mode': 'promptless'}
    failure = {**key, 'class': 'failure', 'reason': 'event_timeout'}
    result = subprocess.CompletedProcess([], 1, json.dumps({'ok': False, 'failures': [failure]}), '')
    verdict = deploy_mod._fleet_smoke_verdict(result, str(machines), required_cells=[key], accounts=['claude=ours'])
    row = verdict['cells'][0]
    assert row['outcome'] == 'failed' and row['reason'] == 'event_timeout'


def test_binding_mismatch_and_missing_binding_on_pass_still_fail(tmp_path):
    import subprocess
    machines = tmp_path / 'machines.json'
    machines.write_text(json.dumps([{'name': 'primary', 'ssh_target': None}]))
    key = {'host': 'primary', 'provider': 'claude', 'prompt_mode': 'promptless'}
    for binding in (None, {'host': 'primary', 'provider': 'claude', 'alias': 'wrong', 'generation': '1'}):
        row = {**key, 'outcome': 'passed', 'account': 'ours'}
        if binding is not None:
            row['account_binding'] = binding
        result = subprocess.CompletedProcess([], 0, json.dumps({'ok': True, 'cells': [row]}), '')
        verdict = deploy_mod._fleet_smoke_verdict(result, str(machines), required_cells=[key], accounts=['claude=ours'])
        assert verdict['cells'][0]['reason'] == 'account_binding_mismatch'
        assert verdict['cells'][0]['outcome'] == 'failed'
