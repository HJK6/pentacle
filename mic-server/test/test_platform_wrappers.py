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


READY = {'mode':'on','asr':{'loaded':True,'model':'large-v3'},'audio':{'health_state':'ok'},'audio_buffer':{'ready':True}}
NOT_READY = {**READY, 'audio':{'health_state':'stale'}}


class FakeHost:
    """Scripted /status answers, launchctl/ps results and a manual clock."""
    def __init__(self, module, monkeypatch, tmp_path, answers, loaded=True, pid=4242, age='01:00', probe_cost=0):
        self.answers, self.loaded, self.pid, self.age, self.probe_cost = list(answers), loaded, pid, age, probe_cost
        self.now, self.calls = 0.0, []
        monkeypatch.setattr(module, 'clock', lambda: self.now)
        monkeypatch.setattr(module, 'sleep', self.advance)
        monkeypatch.setattr(module, 'status', self.status)
        monkeypatch.setattr(module.subprocess, 'run', self.run)
        monkeypatch.setattr(module, 'LOCK_PATH', tmp_path/'lock'/'mic-recover.lock')

    def advance(self, seconds):
        self.now += seconds

    def status(self, timeout):
        assert 0 < timeout <= 3
        self.advance(min(timeout, self.probe_cost))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def run(self, argv, **kw):
        assert kw.get('timeout', 0) > 0
        self.calls.append(argv[1:] if argv[0] == '/bin/launchctl' else argv)
        if argv[0] == '/bin/ps':
            return SimpleNamespace(returncode=0, stdout=f'  {self.age}\n')
        if argv[1] == 'print':
            text = f'\tstate = running\n\tpid = {self.pid}\n' if self.pid else '\tstate = spawn scheduled\n'
            return SimpleNamespace(returncode=0 if self.loaded else 113, stdout=text)
        return SimpleNamespace(returncode=0, stdout='', check_returncode=lambda: None)

    def verbs(self):
        return [c[0] for c in self.calls if c[0] in ('print','bootstrap','kickstart','bootout','kill')]


def test_recovery_leaves_healthy_job_alone(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [READY])
    module.recover(config(tmp_path))
    assert host.calls == []


def test_recovery_leaves_responding_off_job_alone(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [{'mode':'off'}])
    module.recover(config(tmp_path))
    assert host.calls == []


def test_recovery_never_restarts_an_answering_unready_job(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [NOT_READY])
    with pytest.raises(TimeoutError):
        module.recover(config(tmp_path))
    assert host.calls == []
    assert host.now <= module.DEADLINE_SECS


def test_recovery_bootstraps_unloaded_unresponsive_job(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [None, None, READY], loaded=False)
    module.recover(config(tmp_path))
    assert host.verbs() == ['print', 'bootstrap']


def test_recovery_restarts_loaded_unresponsive_job_once(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [None, None, None, None, READY], age='2-03:04:05')
    module.recover(config(tmp_path))
    assert host.verbs() == ['print', 'kickstart']
    assert ['kickstart', '-k', f'gui/{module.os.getuid()}/synthetic.mic.service'] in host.calls


def test_recovery_does_not_restart_after_a_transient_miss(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [None, READY])
    module.recover(config(tmp_path))
    assert host.verbs() == ['print']


@pytest.mark.parametrize('pid,age', [(4242, '00:29'), (None, None)])
def test_recovery_waits_for_a_starting_job_instead_of_restarting(tmp_path, monkeypatch, pid, age):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [None, None, None, READY], pid=pid, age=age)
    module.recover(config(tmp_path))
    assert 'kickstart' not in host.verbs()


def test_recovery_worst_case_stays_inside_one_deadline(tmp_path, monkeypatch):
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [None], probe_cost=3)
    with pytest.raises(TimeoutError):
        module.recover(config(tmp_path))
    assert host.verbs() == ['print', 'kickstart']
    assert host.now <= module.DEADLINE_SECS < 75


def test_recovery_serializes_concurrent_runs(tmp_path, monkeypatch):
    import fcntl
    module = load('recover-local-service.py')
    host = FakeHost(module, monkeypatch, tmp_path, [None])
    module.LOCK_PATH.parent.mkdir(parents=True)
    with open(module.LOCK_PATH, 'a') as peer:
        fcntl.flock(peer, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            module.recover(config(tmp_path))
    assert host.calls == []
    host.answers = [READY]
    module.recover(config(tmp_path))
    assert host.calls == []


def test_recovery_probes_loopback_on_the_configured_port(monkeypatch):
    module = load('recover-local-service.py')
    urls = []
    monkeypatch.setattr(module, 'urlopen', lambda url, timeout: urls.append(url) or (_ for _ in ()).throw(OSError()))
    assert module.status() is None
    module.PORT = 17781
    module.status()
    assert urls == ['http://127.0.0.1:7780/status', 'http://127.0.0.1:17781/status']


@pytest.mark.parametrize('etime,seconds', [('00:29', 29), ('05:03', 303), ('01:02:03', 3723), ('2-03:04:05', 183845)])
def test_recovery_reads_process_age_from_ps_etime(monkeypatch, etime, seconds):
    module = load('recover-local-service.py')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=f'   {etime}\n'))
    assert module.process_age(4242, module.clock() + 10) == seconds
