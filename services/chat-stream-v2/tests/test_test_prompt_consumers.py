"""Prompt boundaries tested against real builders, with provider execution intercepted."""
import copy
import json
from types import SimpleNamespace
import pytest
from _shared.spawn_profiles import resolve_test_profile, test_prompt_tuple as profile_tuple
from tools import spawn_fleet_smoke as smoke
from tools import busy_seat_send_probe as busy
from tools import context_handoff_proof as handoff


def test_busy_probe_reads_catalog_instead_of_its_own_model_constants(monkeypatch):
    profile = resolve_test_profile()
    calls = []
    monkeypatch.setattr(busy, 'run', lambda argv: (calls.append(argv) or 0,
        json.dumps({'test_prompt_profile': profile})))
    assert busy.test_profile_from_daemon() == profile
    assert calls == [['agent-orch', 'profiles']]
    monkeypatch.setattr(busy, 'run', lambda argv: (0, '{}'))
    with pytest.raises(ValueError): busy.test_profile_from_daemon()


def test_d2_real_builder_uses_frozen_test_profile(monkeypatch):
    from tools import d2_live_delivery as d2
    profile = resolve_test_profile()
    actor = d2.D2Journey.__new__(d2.D2Journey)
    actor.args = SimpleNamespace(parent_stream_id='fixture:parent')
    actor.result, actor.owned = {}, {}
    actor._event = lambda *_: None
    actor._save_progress = lambda: None
    captured = []
    def spawn(payload):
        captured.append(payload)
        return SimpleNamespace(stream_id='fixture:child', request_id='r', idempotency_key='i',
            session_generation='g', legacy_unique_name='n')
    actor._spawn(SimpleNamespace(spawn=spawn), {'test_prompt_profile': profile,
        'catalog_version': 'fixture'}, 'ready', 'claude')
    assert (captured[0]['model'], captured[0]['effort']) == profile_tuple('claude', profile)
    assert actor.result['test_profile'] == profile
    with pytest.raises(ValueError): actor._spawn(SimpleNamespace(spawn=spawn), {}, 'ready', 'claude')
    assert len(captured) == 1


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


def test_handoff_oracle_follows_frozen_manifest_instead_of_fable_convention():
    profile = resolve_test_profile()
    model, effort = profile_tuple('claude', profile)
    rows = {k: dict(stream_id=k, session_generation='g', created_at='now', status='open',
        provider='claude', effective_model=model, effective_effort=effort) for k in ['source', 'witness', 'successor']}
    rows['source'].update(status='closed', close_kind='handed_off')
    rows['witness']['parent_stream_id'] = 'successor'
    rows['successor']['handoff_from_stream_id'] = 'source'
    manifest = {k: {f: rows[k][f] for f in ['stream_id', 'session_generation', 'created_at']}
                for k in ['source', 'witness']}
    manifest['test_profile'] = profile
    receipt = {'stream_id': 'successor', 'initial_prompt_delivery': {'delivery_status': 'delivered'}}
    handoff.verify_handoff(manifest, rows, receipt)
    rows['successor']['effective_effort'] = 'high'
    with pytest.raises(ValueError, match='tuple mismatch'): handoff.verify_handoff(manifest, rows, receipt)


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
