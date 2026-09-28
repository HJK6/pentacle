"""Platform source policy tests: every process and HTTP boundary is mocked."""
import importlib.util
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def load(relative):
    path = Path(__file__).resolve().parents[1]/relative
    spec = importlib.util.spec_from_file_location('owned_platform_subject', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('output', ['/Applications/Unowned.app','/System/Unowned.app','/Library/Unowned.app','/usr/Unowned.app'])
def test_launcher_refuses_installed_targets_before_process_call(tmp_path, monkeypatch, output):
    module = load('macos-app/build.py')
    run = Mock()
    monkeypatch.setattr(module.subprocess,'run',run)
    with pytest.raises(ValueError, match='owned candidate'):
        module.build(tmp_path/'template', Path(output))
    run.assert_not_called()


def test_launcher_refuses_existing_candidate(tmp_path, monkeypatch):
    module = load('macos-app/build.py')
    run = Mock()
    monkeypatch.setattr(module.subprocess,'run',run)
    output = tmp_path/'candidate.app'
    output.mkdir()
    with pytest.raises(ValueError, match='already exists'):
        module.build(tmp_path/'template',output)
    run.assert_not_called()


def test_launcher_receipt_with_fake_compiler_and_signer(tmp_path, monkeypatch):
    module = load('macos-app/build.py')
    template = tmp_path/'template.app'
    (template/'Contents/MacOS').mkdir(parents=True)
    output = tmp_path/'candidate.app'
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        assert kwargs['check'] is True
        if argv[0].endswith('swiftc'):
            Path(argv[argv.index('-o')+1]).write_bytes(b'synthetic binary stand-in')
    monkeypatch.setattr(module.subprocess,'run',run)
    module.build(template,output)
    receipt = json.loads(output.with_suffix('.build.json').read_text())
    assert receipt['installation_performed'] is False
    assert len(receipt['binary_sha256']) == 64
    assert len(commands) == 4
    assert template.exists()


def config(tmp_path):
    path = tmp_path/'synthetic.plist'
    path.write_bytes(plistlib.dumps({'Label':'synthetic.mic.service'}))
    return path


def test_recovery_leaves_responding_job_alone(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    monkeypatch.setattr(module,'status',lambda: {'mode':'off'})
    run = Mock()
    monkeypatch.setattr(module.subprocess,'run',run)
    module.recover(config(tmp_path))
    run.assert_not_called()


@pytest.mark.parametrize('loaded', [True, False])
def test_recovery_bootstraps_only_unloaded_unresponsive_job(tmp_path, monkeypatch, loaded):
    module = load('recover-local-service.py')
    values = iter([None, {'mode':'off'}])
    monkeypatch.setattr(module,'status',lambda: next(values))
    run = Mock(return_value=SimpleNamespace(returncode=0 if loaded else 1))
    monkeypatch.setattr(module.subprocess,'run',run)
    module.recover(config(tmp_path))
    verbs = [call.args[0][1] for call in run.call_args_list]
    assert verbs == (['print'] if loaded else ['print','bootstrap'])
    assert not any(verb in verbs for verb in ('kickstart','bootout','kill'))
