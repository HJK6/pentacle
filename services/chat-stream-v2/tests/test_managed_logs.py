"""Bounded managed output and retained-generation readers on owned real inputs."""
from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from managed_logs import Config, ManagedStream, retained_paths
from deploy.packet.log_scan import freeze, lines_many
from test_deploy_script import deploy_mod, V2_SERVICE

SERVICE = Path(__file__).resolve().parents[1]


def config(tmp_path, maximum=128, backups=3):
    tmp_path.chmod(0o700)
    return Config(tmp_path / 'out', tmp_path / 'err', maximum, backups)


def retained(cfg, path):
    return b''.join(p.read_bytes() for p in reversed(cfg.paths(path)) if p.exists())


def assert_bound(cfg):
    for path in (cfg.stdout, cfg.stderr):
        files = [p for p in cfg.paths(path) if p.exists()]
        assert len(files) <= cfg.backups + 1
        assert all(p.stat().st_size <= cfg.max_bytes for p in files)
        for p in files:
            p.read_bytes().decode('utf-8')


@pytest.mark.parametrize('maximum', [4, 17, 128])
def test_unicode_oversized_writes_preserve_newest_suffix_and_exact_byte_caps(tmp_path, maximum):
    cfg = config(tmp_path, maximum)
    cfg.validate((io.StringIO(), io.StringIO()))
    emergency = io.StringIO()
    streams = [ManagedStream(p, cfg, emergency, emergency) for p in (cfg.stdout, cfg.stderr)]
    try:
        for stream, path in zip(streams, (cfg.stdout, cfg.stderr)):
            expected = b''
            # More than three full retention wraps, including no-newline writes.
            for text in ['A雪🙂\n', 'x' * 280 + '終\n', 'tail\n'] * 12:
                assert stream.write(text) == len(text)
                expected += text.encode()
                actual = retained(cfg, path)
                assert actual and expected.endswith(actual)
                assert_bound(cfg)
            assert stream.handler.failed_writes == 0
    finally:
        for stream in streams:
            stream.close()
    assert emergency.getvalue() == ''


def test_concurrent_writers_exactly_once_before_expiry_and_reopen(tmp_path):
    cfg = config(tmp_path, 4096)
    emergency = io.StringIO()
    stream = ManagedStream(cfg.stdout, cfg, emergency, emergency)
    records = [f'writer-{i:03d} 雪\n' for i in range(100)]
    try:
        threads = [threading.Thread(target=stream.write, args=(record,)) for record in records]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(retained(cfg, cfg.stdout).decode().splitlines()) == sorted(r.rstrip() for r in records)
    finally:
        stream.close()
    reopened = ManagedStream(cfg.stdout, cfg, emergency, emergency)
    reopened.write('reopened\n')
    reopened.close()
    assert retained(cfg, cfg.stdout).endswith(b'reopened\n')
    assert_bound(cfg)


@pytest.mark.parametrize('case', ['active-backup', 'backup-emergency', 'hardlink', 'symlink', 'backup-symlink', 'oversized', 'extra-generation'])
def test_invalid_namespace_rejected_before_either_sink_changes(tmp_path, case):
    cfg = config(tmp_path)
    cfg.stdout.write_bytes(b'preserve stdout\n')
    cfg.stderr.write_bytes(b'preserve stderr\n')
    emergency = tmp_path / 'emergency'
    emergency.write_bytes(b'preserve emergency\n')
    if case == 'active-backup':
        cfg = Config(cfg.stdout, Path(str(cfg.stdout) + '.1'), cfg.max_bytes, cfg.backups)
    elif case == 'backup-emergency':
        emergency = Path(str(cfg.stdout) + '.1')
        emergency.write_bytes(b'preserve emergency\n')
    elif case == 'hardlink':
        cfg.stderr.unlink()
        os.link(cfg.stdout, cfg.stderr)
    elif case in ('symlink', 'backup-symlink'):
        target = cfg.stderr if case == 'symlink' else Path(str(cfg.stdout) + '.1')
        target.unlink(missing_ok=True)
        target.symlink_to(emergency)
    elif case == 'oversized':
        cfg.stderr.write_bytes(b'X' * (cfg.max_bytes + 1))
    else:
        Path(str(cfg.stdout) + '.9').write_bytes(b'preserve older generation\n')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with emergency.open('a') as handle, pytest.raises(ValueError):
        cfg.validate((handle, io.StringIO()))
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


@pytest.mark.parametrize('env', [
    {'PENTACLE_DAEMON_STDOUT_PATH': '/fixture/out'},
    {'PENTACLE_DAEMON_STDOUT_PATH': 'relative', 'PENTACLE_DAEMON_STDERR_PATH': '/fixture/err'},
    {'PENTACLE_DAEMON_STDOUT_PATH': '/fixture/out', 'PENTACLE_DAEMON_STDERR_PATH': '/fixture/err', 'PENTACLE_DAEMON_LOG_MAX_BYTES': '3'},
    {'PENTACLE_DAEMON_STDOUT_PATH': '/fixture/out', 'PENTACLE_DAEMON_STDERR_PATH': '/fixture/err', 'PENTACLE_DAEMON_LOG_BACKUPS': '0'},
])
def test_invalid_environment_fails_clearly(env):
    with pytest.raises(ValueError):
        Config.from_env(env)
    assert Config.from_env({}) is None


class IOProxy:
    def __init__(self, stream, write):
        self.stream, self._write = stream, write
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def write(self, text):
        return self._write(self.stream, text)


def test_positive_short_writes_complete_without_duplicates(tmp_path):
    cfg = config(tmp_path, 1024)
    emergency = io.StringIO()
    stream = ManagedStream(cfg.stdout, cfg, emergency, emergency)
    stream.handler.stream = IOProxy(stream.handler.stream, lambda file, text: file.write(text[:2]))
    try:
        stream.write('雪🙂positive-short-write\n')
        assert cfg.stdout.read_text() == '雪🙂positive-short-write\n'
        assert stream.handler.failed_writes == 0
    finally:
        stream.close()


@pytest.mark.parametrize('failure', ['zero', 'enospc-prefix', 'rename'])
def test_failures_are_bounded_nonrecursive_and_reader_never_passes_partial(tmp_path, monkeypatch, failure):
    cfg = config(tmp_path, 64)
    emergency = io.StringIO()
    stream = ManagedStream(cfg.stdout, cfg, emergency, emergency)
    try:
        if failure == 'rename':
            stream.write('intact prior record\n')
            before = cfg.stdout.read_bytes()
            def fail_rename(*args):
                raise PermissionError('fixture rename denied')
            monkeypatch.setattr(os, 'rename', fail_rename)
            text = 'Y' * 60 + '\n'
        else:
            calls = 0
            def write(file, text):
                nonlocal calls
                calls += 1
                if failure == 'zero':
                    return 0
                if calls == 1:
                    result = file.write(text[:1])
                    file.flush()
                    return result
                raise OSError(errno.ENOSPC, 'fixture disk full')
            stream.handler.stream = IOProxy(stream.handler.stream, write)
            text = '雪partial-record\n'
        for _ in range(5):
            stream.write(text)
        assert stream.handler.failed_writes == 5
        assert emergency.getvalue().count('managed_log_error') == 1
        assert 'partial-record' not in emergency.getvalue()
        if failure == 'rename':
            assert cfg.stdout.read_bytes() == before
        elif failure == 'enospc-prefix':
            with pytest.raises(ValueError, match='unfinished'):
                list(lines_many([freeze(cfg.stdout)]))
    finally:
        stream.close()


def env_for(cfg):
    return {'PENTACLE_DAEMON_STDOUT_PATH': str(cfg.stdout), 'PENTACLE_DAEMON_STDERR_PATH': str(cfg.stderr),
            'PENTACLE_DAEMON_LOG_MAX_BYTES': str(cfg.max_bytes), 'PENTACLE_DAEMON_LOG_BACKUPS': str(cfg.backups)}


def test_deployment_reads_both_managed_streams_and_retained_boot_boundary(tmp_path, monkeypatch):
    cfg = config(tmp_path, 128)
    cfg.stdout.touch(); cfg.stderr.touch()
    monkeypatch.setattr(deploy_mod, '_launchd_environment', lambda _: env_for(cfg))
    assert deploy_mod._daemon_log_paths(V2_SERVICE) == (cfg.stdout, cfg.stderr)
    before = deploy_mod._capture_log_offsets((cfg.stdout, cfg.stderr))
    emergency = io.StringIO()
    stdout = ManagedStream(cfg.stdout, cfg, emergency, emergency)
    stderr = ManagedStream(cfg.stderr, cfg, emergency, emergency)
    try:
        stdout.write(deploy_mod.V2_BOOT_LINE + ' fixture:0\n')
        stdout.write('x' * 90 + '\n')
        stderr.write('ordinary warning\n')
        result = deploy_mod._scan_slow_consumer_window(V2_SERVICE, before)
        assert result['scan_complete'] and result['boot_marker_seen'], result
        assert result['outcome'] == deploy_mod.SLOW_CONSUMER_PASSED
        reader = deploy_mod._ManagedAppendedLogScanner(cfg.stdout, before, cfg)
        assert deploy_mod.V2_BOOT_LINE in reader.read_new()
        assert reader.incomplete_reason is None
        # Preserve exact Unicode and record fragments across retained files.
        paths = retained_paths(cfg.stdout, before[str(cfg.stdout)]['inode'], before[str(cfg.stdout)]['device'], cfg.backups)
        items = [freeze(p) for p in paths]
        assert ''.join(lines_many(items)).startswith(deploy_mod.V2_BOOT_LINE)
        stdout.write('Z' * 1000 + '\n')
        result = deploy_mod._scan_slow_consumer_window(V2_SERVICE, before)
        assert not result['scan_complete'] and result['outcome'] == deploy_mod.SLOW_CONSUMER_FAILED
        assert 'expired' in result['incomplete_reason']
    finally:
        stdout.close(); stderr.close()


def test_split_record_and_rotation_during_frozen_read_fail_closed(tmp_path):
    first, second = tmp_path / 'old', tmp_path / 'new'
    first.write_bytes('hello 雪'.encode()[:8])
    second.write_bytes('hello 雪'.encode()[8:] + b'\n')
    assert list(lines_many([freeze(first), freeze(second)])) == ['hello 雪\n']
    item = freeze(first)
    first.rename(tmp_path / 'moved')
    first.write_bytes(b'replacement\n')
    with pytest.raises(ValueError, match='rotated'):
        list(lines_many([item]))


def test_fresh_entrypoint_routes_python_output_and_leaves_raw_fds_emergency(tmp_path):
    cfg = config(tmp_path, 4096)
    script = '''
import asyncio, logging, os, sys, threading, traceback
import main
async def probe(args):
 print('chat_streamd_v2 listening on fixture:0', flush=True)
 print('stdout-record 雪', flush=True)
 print('stderr-record', file=sys.stderr, flush=True)
 main.log.warning('logged-warning')
 try: raise ValueError('caught-traceback')
 except ValueError: traceback.print_exc()
 def thread_failure(): raise RuntimeError('thread-traceback')
 thread = threading.Thread(target=thread_failure); thread.start(); thread.join()
 os.write(1, b'raw-stdout\\n'); os.write(2, b'raw-stderr\\n')
 return 0
main.run=probe
assert main.main(['--log-level','DEBUG']) == 0
raise RuntimeError('uncaught-traceback')
'''
    env = {**os.environ, **env_for(cfg), 'PYTHONPATH': str(SERVICE)}
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert result.stdout == 'raw-stdout\n' and result.stderr == 'raw-stderr\n'
    output, errors = retained(cfg, cfg.stdout).decode(), retained(cfg, cfg.stderr).decode()
    assert output.count('stdout-record 雪') == 1
    for text in ['stderr-record', 'logged-warning', 'caught-traceback', 'thread-traceback', 'uncaught-traceback']:
        assert text in errors
    assert 'raw-stdout' not in output and 'raw-stderr' not in errors
    assert_bound(cfg)


def test_owned_real_daemon_progress_and_rpc_continue_through_wraps_and_sink_errors(tmp_path, isolated_tmux_env):
    import time
    import re
    from websockets.sync.client import connect
    from loop_watchdog import cause_at
    root = tmp_path.resolve()
    cfg = config(root, 1024)
    script = r'''
import asyncio, errno, json, logging, os, sys
from pathlib import Path
import main, managed_logs, server, local_admin
root=Path(sys.argv[1])
# Isolate optional account/catalog dependencies; log/daemon/watchdog subjects stay real.
main._spec_catalog=lambda _sessions: None
original_registry=server.operator_auth.OperatorCredentialRegistry.__init__
def registry_init(self, path=None): original_registry(self, root/'auth'/'registry.json')
server.operator_auth.OperatorCredentialRegistry.__init__=registry_init
registry=server.operator_auth.OperatorCredentialRegistry(); registry.initialize()
original_initialize=local_admin.initialize
local_admin.initialize=lambda: original_initialize(root/'auth'/'local-admin.token')
original_bind=main.Server.bind
async def bind(self):
 port=await original_bind(self)
 (root/'ready.json').write_text(json.dumps({'port':port,'pid':os.getpid()}))
 return port
main.Server.bind=bind
class FailureProxy:
 def __init__(self, stream): self.stream=stream
 def __getattr__(self,name): return getattr(self.stream,name)
 def write(self,text):
  if (root/'fail').exists(): raise OSError(errno.ENOSPC,'owned injected disk full')
  return self.stream.write(text)
async def flood():
 i=0
 while True:
  if (root/'flood').exists():
   handler=managed_logs._streams[1].handler
   if (root/'fail').exists() and handler.stream is not None and not isinstance(handler.stream,FailureProxy):
    handler.stream=FailureProxy(handler.stream)
   print(f'iteration-{i:06d} '+('O'*200),flush=True)
   main.log.warning('iteration-%06d %s',i,'E'*200)
   i+=1
  await asyncio.sleep(.005)
original_run=main.run
async def run(args):
 task=asyncio.create_task(flood())
 try: return await original_run(args)
 finally:
  task.cancel()
  await asyncio.gather(task,return_exceptions=True)
main.run=run
flags=['mirror','ingest','hosts','reconciler','retention','nudges','notification-expiry','remote-presence',
 'event-push-ingest','routing-integrity','outbound-notices','awaiter-resolution','window-schedule','usage-state-publisher']
args=['--bind','127.0.0.1','--port','0','--local-host','fixture', '--db',str(root/'sessions.db'),
 '--notifications-db',str(root/'notifications.db'),'--assets-db',str(root/'assets.db'),
 '--blob-root',str(root/'blobs'),'--tmux-bin',sys.argv[2]]
raise SystemExit(main.main(args+['--disable-'+flag for flag in flags]))
'''
    env = {'PATH': os.environ['PATH'], 'PYTHONPATH': str(SERVICE), **env_for(cfg),
           'PENTACLE_HOST_ID': 'fixture',
           'PENTACLE_MACHINES_JSON': json.dumps([{'name': 'fixture', 'ssh_target': None, 'cwd': str(root)}]),
           'PENTACLE_MEMORY_ROOT': str(root / 'memory'),
           'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '0', 'PENTACLE_ASSISTANT_DAFF_COMPOSITE_ENABLED': '0',
           'PENTACLE_DAEMON_PROGRESS_PATH': str(root / 'progress.json'),
           'PENTACLE_DAEMON_INSTANCE_ID': 'fixture-log-retention'}
    with (root / 'emergency.out').open('w') as out, (root / 'emergency.err').open('w') as err:
        proc = subprocess.Popen([sys.executable, '-c', script, str(root), isolated_tmux_env],
                                env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            deadline = time.monotonic() + 15
            while not (root / 'ready.json').exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            assert (root / 'ready.json').exists(), (root / 'emergency.err').read_text()
            ready = json.loads((root / 'ready.json').read_text())
            def rpc(kind):
                with connect(f"ws://127.0.0.1:{ready['port']}", open_timeout=5) as socket:
                    welcome = json.loads(socket.recv(timeout=5))
                    socket.send(json.dumps({'type':'hello','client':'public-smoke',
                                            'subscribe':{'snapshot':False,'mode':'rpc'}}))
                    socket.send(json.dumps({'type':kind,'request_id':'managed-probe'}))
                    while True:
                        reply = json.loads(socket.recv(timeout=5))
                        if reply.get('request_id') == 'managed-probe':
                            return welcome, reply
            welcome, initial = rpc('daemon.stats')
            assert initial['type'] == 'daemon.stats.ok', initial
            assert (root / 'progress.json').exists()
            first = json.loads((root / 'progress.json').read_text())
            (root / 'flood').touch()
            time.sleep(1.4)
            (root / 'flood').unlink()
            time.sleep(.03)
            # Freeze the producer before enumerating rotation filenames.
            assert max(map(int, re.findall(rb'iteration-(\d+)', retained(cfg, cfg.stdout)))) > 64
            assert max(map(int, re.findall(rb'iteration-(\d+)', retained(cfg, cfg.stderr)))) > 64
            assert_bound(cfg)
            (root / 'flood').touch()
            assert rpc('ping')[1]['type'] == 'pong'
            later_welcome, later = rpc('daemon.stats')
            assert later['stats']['lifecycle']['instance_id'] == initial['stats']['lifecycle']['instance_id']
            assert later_welcome.get('runtime_sha') == welcome.get('runtime_sha')
            assert later['stats']['managed_logs']['stderr']['failed_writes'] == 0
            assert later['stats']['managed_logs']['stdout']['failed_writes'] == 0
            (root / 'fail').touch()
            time.sleep(.15)
            degraded = rpc('daemon.stats')[1]
            assert degraded['stats']['managed_logs']['stderr']['failed_writes'] > 0
            assert rpc('ping')[1]['type'] == 'pong'
            (root / 'fail').unlink()
            (root / 'flood').unlink()
            time.sleep(1.05)
            current = json.loads((root / 'progress.json').read_text())
            assert current['pid'] == first['pid'] == proc.pid == ready['pid']
            assert current['boot_id'] == first['boot_id']
            assert current['loop_seq'] > first['loop_seq']
            assert current['sample_mono_s'] > first['sample_mono_s']
            assert cause_at(current, time.monotonic()) is None
            assert proc.poll() is None
            assert_bound(cfg)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=5)
        assert proc.returncode == 0, (root / 'emergency.err').read_text()
    assert (root / 'emergency.out').read_text() == ''
    assert (root / 'emergency.err').read_text().count('managed_log_error') == 1


def test_stale_boot_and_same_or_larger_replacement_never_establish_readiness(tmp_path):
    cfg = config(tmp_path, 128)
    cfg.stdout.write_text(deploy_mod.V2_BOOT_LINE + ' stale\n')
    before = deploy_mod._capture_log_offsets((cfg.stdout,))
    emergency = io.StringIO()
    stream = ManagedStream(cfg.stdout, cfg, emergency, emergency)
    try:
        stream.write('N' * 110 + '\n')
        reader = deploy_mod._ManagedAppendedLogScanner(cfg.stdout, before, cfg)
        assert deploy_mod.V2_BOOT_LINE not in reader.read_new()
        # A fresh marker can straddle two polls without replaying the old marker.
        stream.write(deploy_mod.V2_BOOT_LINE[:12])
        assert deploy_mod.V2_BOOT_LINE not in reader.read_new()
        stream.write(deploy_mod.V2_BOOT_LINE[12:] + '\n')
        assert deploy_mod.V2_BOOT_LINE in reader.read_new()
    finally:
        stream.close()
    saved = deploy_mod._capture_log_offsets((cfg.stdout,))
    cfg.stdout.rename(tmp_path / 'unretained')
    cfg.stdout.write_text(deploy_mod.V2_BOOT_LINE + ' replacement\n' + 'R' * 200)
    reader = deploy_mod._ManagedAppendedLogScanner(cfg.stdout, saved, cfg)
    assert reader.read_new() == ''
    assert 'expired' in reader.incomplete_reason


def test_rotation_after_frozen_yield_is_incomplete(tmp_path):
    path = tmp_path / 'log'
    path.write_text('first\nsecond\n')
    iterator = lines_many([freeze(path)])
    assert next(iterator) == 'first\n'
    path.rename(tmp_path / 'archive')
    path.write_text('first\nsecond\n')
    with pytest.raises(ValueError, match='rotated'):
        list(iterator)


def test_startup_open_failure_keeps_streams_and_existing_bytes(tmp_path, monkeypatch):
    import builtins
    import managed_logs
    cfg = config(tmp_path)
    cfg.stdout.write_bytes(b'preserved-out\n')
    cfg.stderr.write_bytes(b'preserved-err\n')
    original_open = builtins.open
    def open_file(path, *args, **kwargs):
        if Path(path) == cfg.stderr:
            raise PermissionError('owned injected open failure')
        return original_open(path, *args, **kwargs)
    before = sys.stdout, sys.stderr
    monkeypatch.setattr(builtins, 'open', open_file)
    with pytest.raises(PermissionError):
        managed_logs.install(env_for(cfg))
    assert (sys.stdout, sys.stderr) == before
    assert managed_logs.status() is None
    assert cfg.stdout.read_bytes() == b'preserved-out\n'
    assert cfg.stderr.read_bytes() == b'preserved-err\n'


def test_machine_sampler_captures_child_errors_and_uses_logging(monkeypatch, caplog):
    import machine_stats
    def fail_output(command, **kwargs):
        assert kwargs['stderr'] == subprocess.PIPE
        raise subprocess.CalledProcessError(1, command, stderr='owned probe failure')
    monkeypatch.setattr(machine_stats.subprocess, 'check_output', fail_output)
    assert machine_stats._sysctl_int('fixture-counter') == 0
    assert 'machine sampler command failed' in caplog.text


@pytest.mark.parametrize('tail', [b'', b'\xff\n'])
def test_real_readiness_rejects_unfinished_or_corrupt_boot_record(tmp_path, monkeypatch, tail):
    cfg = config(tmp_path, 1024)
    cfg.stdout.touch(); cfg.stderr.touch()
    monkeypatch.setattr(deploy_mod, '_launchd_environment', lambda _: env_for(cfg))
    monkeypatch.setattr(deploy_mod, '_launchd_state', lambda *_: deploy_mod.LaunchdState(True, 222))
    before = deploy_mod._capture_log_offsets((cfg.stdout, cfg.stderr))
    cfg.stdout.write_bytes(deploy_mod.V2_BOOT_LINE.encode() + b' fixture:0' + tail)
    result = deploy_mod.verify_fresh_daemon_log_line(V2_SERVICE, previous_size=before,
        prior_pid=111, wait_seconds=0, runner=lambda *_: None)
    assert result.outcome == deploy_mod.BOOT_NOT_OBSERVED
    assert 'incomplete' in result.detail
