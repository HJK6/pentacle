from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from tools import gate_host_preflight as probe


class Child:
    pid = 12345
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def terminate(self):
        pass
    def wait(self, timeout):
        pass


@pytest.fixture
def short_path():
    with tempfile.TemporaryDirectory(prefix='gate-probe-', dir='/tmp') as folder:
        yield Path(folder)


@pytest.mark.parametrize('failure', ['process enumeration', 'ssh control socket', 'tmux socket/session'])
def test_owned_preflight_reports_failed_operation_and_cleans(short_path, monkeypatch, capsys, failure):
    monkeypatch.setattr(probe.subprocess, 'Popen', lambda *a, **kw: Child())
    directory = short_path / 'control'
    if failure == 'ssh control socket':
        directory.write_text('not a directory')
    monkeypatch.setenv('PENTACLE_SSH_CONTROL_DIR', str(directory))
    def command(args):
        if args[0] == 'ps':
            if failure == 'process enumeration':
                raise PermissionError('denied')
            return '12345' if '-axo' in args else 'Tue Nov 24 12:00:00 2026 python gate-owned-probe'
        if args[0] == 'tmux' and failure == 'tmux socket/session':
            raise RuntimeError('tmux denied')
        return ''
    monkeypatch.setattr(probe, 'command', command)
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    assert probe.main() == 2
    assert failure in capsys.readouterr().err
    if directory.is_dir():
        assert list(directory.iterdir()) == []


def test_owned_preflight_positive_cleans_socket(short_path, monkeypatch):
    directory = short_path / 'control'
    directory.mkdir()
    directory.chmod(0o750)
    monkeypatch.setattr(probe.subprocess, 'Popen', lambda *a, **kw: Child())
    monkeypatch.setenv('PENTACLE_SSH_CONTROL_DIR', str(short_path / 'control'))
    monkeypatch.setattr(probe, 'command', lambda args: ('12345' if '-axo' in args else
        'Tue Nov 24 12:00:00 2026 python gate-owned-probe' if args[0] == 'ps' else ''))
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    assert probe.main() == 0
    assert list(directory.iterdir()) == []
    assert directory.stat().st_mode & 0o777 == 0o750


def test_hosted_home_binds_the_product_socket(short_path, monkeypatch):
    import os
    import machines

    # Hosted daemon shards use a 63-byte isolated HOME.
    home = short_path / ('h' * (63 - len(str(short_path)) - 1))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMPDIR', str(home / 'long-runner-temp-root'))
    monkeypatch.delenv('PENTACLE_SSH_CONTROL_DIR', raising=False)
    nonce = 'a' * 32
    monkeypatch.setattr(probe.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
    monkeypatch.setattr(probe.subprocess, 'Popen', lambda *a, **kw: Child())
    monkeypatch.setattr(probe, 'command', lambda args: ('12345' if '-axo' in args else
        'Tue Nov 24 12:00:00 2026 python gate-owned-probe' if args[0] == 'ps' else ''))
    monkeypatch.setattr(probe.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    bound = []
    bind = probe.socket.socket.bind
    def record_bind(sock, path):
        bound.append(path)
        return bind(sock, path)
    monkeypatch.setattr(probe.socket.socket, 'bind', record_bind)
    assert probe.main() == 0
    target = machines.ssh_control_path('probe-' + nonce)
    assert bound == [str(target)] and len(os.fsencode(target)) <= 100
    assert not target.exists()
    assert target.parent.stat().st_mode & 0o777 == 0o700
