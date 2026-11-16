"""Prompt boundaries tested against real builders, with provider execution intercepted."""
import copy
import json
from types import SimpleNamespace
import pytest
from _shared.spawn_profiles import resolve_test_profile, test_prompt_tuple as profile_tuple
from tools import spawn_fleet_smoke as smoke
from tools import busy_seat_send_probe as busy


def test_busy_probe_reads_catalog_instead_of_its_own_model_constants(monkeypatch):
    profile = resolve_test_profile()
    calls = []
    monkeypatch.setattr(busy, 'run', lambda argv: (calls.append(argv) or 0,
        json.dumps({'test_prompt_profile': profile})))
    assert busy.test_profile_from_daemon() == profile
    assert calls == [['agent-orch', 'profiles']]
    monkeypatch.setattr(busy, 'run', lambda argv: (0, '{}'))
    with pytest.raises(ValueError): busy.test_profile_from_daemon()




@pytest.mark.parametrize('change', ['missing', 'digest', 'tuple', 'settings'])
def test_smoke_refuses_bad_or_drifted_profile_before_spawn(change):
    profile = resolve_test_profile()
    current = copy.deepcopy(profile)
    if change == 'missing': current = None
    elif change == 'digest': current['profile_sha256'] = '0' * 64
    elif change == 'tuple': current['providers']['codex']['effort'] = 'invalid'
    else: current['settings_sha256']['spawn_defaults.json'] = '0' * 64
    calls = []
    def rpc(payload, expected):
        calls.append(payload['type'])
        return {'type': 'spawn_catalog_get.ok', 'test_prompt_profile': current}
    with pytest.raises(RuntimeError):
        smoke.run_cell('fixture', 'codex', 'prompted', rpc=rpc, wait_ready=lambda *_: None,
            wait_event=lambda *_: None, verify_teardown=None, test_profile_snapshot=profile)
    assert calls == ['spawn_catalog_get']




def test_direct_deployer_rejects_runner_without_sealed_input_support(tmp_path):
    import subprocess
    import sys
    from .test_deploy_script import deploy_mod
    profile = resolve_test_profile()
    class Service:
        requirements_path = 'services/chat-stream-v2/requirements.txt'
    # Actual parser behavior of a pre-snapshot runner, no provider executable.
    script = tmp_path / 'services/chat-stream-v2/tools/spawn_fleet_smoke.py'
    script.parent.mkdir(parents=True)
    script.write_text("import argparse,json\np=argparse.ArgumentParser();p.add_argument('--dry-run',action='store_true');p.add_argument('url');p.add_argument('host');p.parse_args()\nprint(" + repr(json.dumps({'test_profile': profile})) + ")\n")
    python = tmp_path / 'services/chat-stream-v2/.venv/bin/python'
    python.parent.mkdir(parents=True); python.symlink_to(sys.executable)
    def runner(argv, cwd): return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    with pytest.raises(deploy_mod.DeployError):
        deploy_mod._release_test_profile(tmp_path, Service(), runner)
