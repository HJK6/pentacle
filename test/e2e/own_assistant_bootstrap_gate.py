#!/usr/bin/env python3
"""Cold bootstrap -> owner restart -> real browser -> backend CLI publication.

Requires candidate Python dependencies, npm dependencies/web build, tmux and
Chrome. No paid provider, shared daemon or service changes. Failures retain raw
receipts, while finally removes all plaintext credentials and owned processes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'services/chat-stream-v2'), str(ROOT / 'services')]
from tools.live_window import authenticated_operator_connection


def source_bounds():
    """Content identity of runtime Python, Web artifact and this harness."""
    files = [ROOT / 'tools/bootstrap_assistant.py', Path(__file__).resolve(),
             ROOT / 'test/e2e/own_assistant_bootstrap_browser.cjs',
             ROOT / 'test/e2e/lib/own_assistant_provider.py', ROOT / 'test/e2e/lib/web_gate_daemon.py']
    for directory in ('services/chat-stream-v2', 'services/_shared', 'services/agent-orch/agent_orch'):
        files.extend(p for p in (ROOT / directory).rglob('*.py')
                     if not {'tests', 'test', '__pycache__'}.intersection(p.relative_to(ROOT).parts))
    files.extend((ROOT / 'server').rglob('*.js'))
    files.extend(ROOT / 'renderer/dist/web' / name for name in ('bundle.js', 'chat_core.bundle.js', 'styles.css'))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(files))}


def stop(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def wait_for(path, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = predicate(path.read_text())
            if value:
                return value
        time.sleep(.05)
    raise TimeoutError('missing evidence: ' + path.name)


def run(out):
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('output directory must be fresh')
    daemon = browser_runner = None
    tmux = shutil.which('tmux')
    node = shutil.which('node')
    if not tmux or not node:
        raise RuntimeError('tmux and Node are required')
    socket = 'pentacle-own-assistant-' + uuid.uuid4().hex
    env = {'PATH': os.pathsep.join(dict.fromkeys([str(Path(sys.executable).parent), str(Path(tmux).parent), str(Path(node).parent), '/usr/bin', '/bin'])),
           'LANG': 'en_US.UTF-8', 'SHELL': '/bin/sh', 'PENTACLE_INGEST_INTERVAL_S': '.1',
           'PENTACLE_FIXTURE_ROOT': str(out), 'PENTACLE_ASSISTANT_ROLE': 'assistant'}
    if os.environ.get('PENTACLE_TEST_BROWSER'):
        env['PENTACLE_TEST_BROWSER'] = os.environ['PENTACLE_TEST_BROWSER']
    work = out / 'work'
    projects = out / '.claude/projects'
    work.mkdir(mode=0o700)
    projects.mkdir(mode=0o700, parents=True)
    launcher = out / 'provider'
    launcher.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(ROOT / "test/e2e/lib/own_assistant_provider.py")!r}, run_name="__main__")\n')
    launcher.chmod(0o700)
    terminal = out / 'tmux'
    terminal.write_text(f'#!/bin/sh\nexec {shlex.quote(tmux)} -L {shlex.quote(socket)} -f /dev/null "$@"\n')
    terminal.chmod(0o700)
    env['PENTACLE_MACHINES_JSON'] = json.dumps({'machines': [{'name': 'local', 'ssh_target': None,
        'claude_bin': str(launcher), 'codex_bin': '', 'tmux_bin': str(terminal),
        'cwd': str(work), 'projects_root': str(projects), 'agent_orch_bin_dir': str(Path(sys.executable).parent)}]})
    wrapper = ROOT / 'test/e2e/lib/web_gate_daemon.py'
    failure_class = 'HARNESS_ERROR'
    cleanup = {}
    try:
        initial_source = source_bounds()
        (out / 'source-bounds-start.json').write_text(json.dumps(initial_source, indent=2, sort_keys=True))
        subprocess.run([sys.executable, str(wrapper), str(out), '--issue'], cwd=ROOT, env=env, check=True, capture_output=True)
        base_args = [sys.executable, str(wrapper), str(out), '--host', '127.0.0.1', '--local-host', 'local',
            '--db', str(out / 'sessions.db'), '--notifications-db', str(out / 'notifications.db'),
            '--assets-db', str(out / 'assets.db'), '--blob-root', str(out / 'blobs'),
            '--claude-bin', str(launcher), '--codex-bin', '', '--spawn-cwd', str(work),
            '--projects-root', str(projects), '--tmux-bin', str(terminal),
            '--agent-orch-bin-dir', str(Path(sys.executable).parent)]
        def start(port, environment, logname):
            nonlocal daemon
            logpath = out / logname
            with logpath.open('w') as log:
                daemon = subprocess.Popen([*base_args, '--port', str(port)], cwd=ROOT, env=environment,
                                          stdout=log, stderr=log, start_new_session=True)
            match = wait_for(logpath, lambda text: re.search(r'chat_streamd_v2 listening on 127\.0\.0\.1:(\d+)', text))
            return int(match[1])
        port = start(0, env, 'daemon-bootstrap.log')
        endpoint = f'ws://127.0.0.1:{port}'
        (out / 'endpoint').write_text(endpoint)
        instructions = out / 'instructions.md'
        instructions.write_text('You are Nova, a synthetic personal assistant. Follow daemon-authored publication contracts.')
        args = [sys.executable, str(ROOT / 'tools/bootstrap_assistant.py'), '--url', endpoint,
            '--credential-file', str(out / 'operator-auth/token'), '--physical-host', 'local',
            '--name', 'Nova', '--provider', 'claude', '--model', 'claude-sonnet-5', '--effort', 'high',
            '--private-workspace', str(work), '--instructions-file', str(instructions)]
        dry = subprocess.run([*args, '--dry-run'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
        (out / 'dry-run.json').write_text(dry.stdout)
        assert dry.returncode == 0 and not (work / 'assistant.env').exists(), dry.stderr
        bootstrap = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        (out / 'bootstrap.stdout').write_text(bootstrap.stdout)
        (out / 'bootstrap.stderr').write_text(bootstrap.stderr)
        assert bootstrap.returncode == 0, bootstrap.stderr
        receipt = json.loads(bootstrap.stdout)
        assert receipt['backend_stream_id'] != receipt['composite_stream_id']
        assert all((Path(path).stat().st_mode & 0o777) == 0o600 for path in receipt['output_paths'])
        stop(daemon)
        active_env = {**env, **receipt['daemon_environment']}
        assert 'PENTACLE_ASSISTANT_REBIND_AUTHORIZED_SPEC_IDS' not in active_env
        assert start(port, active_env, 'daemon-active.log') == port
        readback = subprocess.run([*args, '--readback'], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        (out / 'bootstrap-readback.stdout').write_text(readback.stdout)
        (out / 'bootstrap-readback.stderr').write_text(readback.stderr)
        assert readback.returncode == 0, readback.stderr
        assert json.loads(readback.stdout)['activation'] == 'restart_binding_verified'
        with authenticated_operator_connection(endpoint, out / 'operator-auth/token', 20) as operator:
            binding = operator.rpc({'type': 'assistant.binding'})
            assert binding['stream_id'] == receipt['backend_stream_id']
            assert binding['generation'] == receipt['backend_generation']
            assert binding['source'] == 'env' and binding['revision'] == 0
            (out / 'binding.json').write_text(json.dumps(binding, indent=2))
        with (out / 'browser.log').open('w') as log:
            browser_runner = subprocess.Popen([node, str(ROOT / 'test/e2e/own_assistant_bootstrap_browser.cjs'), str(out)],
                                             cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
            browser_runner.wait(timeout=90)
        assert browser_runner.returncode == 0, (out / 'browser.log').read_text()
        proof = json.loads(wait_for(out / 'provider-proof.json', lambda text: text))
        failure_class = 'PRODUCT_FAIL'
        assert proof['publish']['exit'] == proof['duplicate']['exit'] == 0, proof
        first = json.loads(proof['publish']['stdout'])
        replay = json.loads(proof['duplicate']['stdout'])
        assert first['event_id'] == replay['event_id'], (first, replay)
        assert proof['stale_generation']['exit'] != 0
        assert 'generation' in proof['stale_generation']['stdout'] + proof['stale_generation']['stderr']
        assert proof['dispatch']['target_generation'] == receipt['backend_generation']
        assert proof['dispatch']['target_stream_id'] == receipt['backend_stream_id']
        with authenticated_operator_connection(endpoint, out / 'operator-auth/token', 20) as operator:
            inspected = operator.rpc({'type': 'inspect_stream', 'stream_id': 'local:assistant', 'event_tail': 100})
            events = [event for event in inspected['recent_events'] if event.get('text') == 'Nova: Hello from my own assistant bootstrap']
            assert len(events) == 1, events
            (out / 'composite-inspect.json').write_text(json.dumps(inspected, indent=2))
        # Same stores/registry, same live backend, second restart readback.
        stop(daemon)
        start(port, active_env, 'daemon-readback.log')
        with authenticated_operator_connection(endpoint, out / 'operator-auth/token', 20) as operator:
            readback = operator.rpc({'type': 'assistant.binding'})
            assert readback['stream_id'] == binding['stream_id'] and readback['generation'] == binding['generation']
            (out / 'restart-binding.json').write_text(json.dumps(readback, indent=2))
        result = {'status': 'PASS', 'scope': 'fresh isolated state; actual bootstrap/operator proof, daemon/tmux/native counterpart/candidate publish CLI/real browser; no paid provider/mobile/mic',
                  'source_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                  'bootstrap_sha256': hashlib.sha256((ROOT / 'tools/bootstrap_assistant.py').read_bytes()).hexdigest(),
                  'backend': receipt['backend_stream_id'], 'generation': receipt['backend_generation'],
                  'dispatch': proof['dispatch'], 'duplicate_event_id': first['event_id'], 'restart_readback': True}
        final_source = source_bounds()
        (out / 'source-bounds-end.json').write_text(json.dumps(final_source, indent=2, sort_keys=True))
        failure_class = 'HARNESS_ERROR'
        assert final_source == initial_source, 'runtime/harness/Web content changed during this run; freeze and requalify'
        result['source_bounds_sha256'] = hashlib.sha256(json.dumps(initial_source, sort_keys=True).encode()).hexdigest()
        browser_proof = json.loads((out / 'browser-proof.json').read_text())
        for file, digest in browser_proof['servedAssets'].items():
            assert digest == initial_source['renderer/dist/web/' + file], 'served artifact mismatch: ' + file
        (out / 'verdict.json').write_text(json.dumps(result, indent=2))
        return result
    except Exception as error:
        (out / 'failure.json').write_text(json.dumps({'status': failure_class, 'error': str(error)}, indent=2))
        raise
    finally:
        stop(browser_runner)
        stop(daemon)
        tmux_stop = subprocess.run([tmux, '-L', socket, 'kill-server'], capture_output=True)
        remaining = subprocess.run([tmux, '-L', socket, 'list-sessions'], capture_output=True)
        # Registry contains proof keys too; fixture credentials never remain.
        credentials = list(out.rglob('*.token')) + [out / 'operator-auth/token', out / 'operator-auth/registry.json']
        credentials += list((out / 'home').rglob('*token*')) if (out / 'home').exists() else []
        for credential in credentials:
            if credential.is_file():
                credential.unlink()
        cleanup.update(daemon_stopped=daemon is None or daemon.poll() is not None,
            browser_runner_stopped=browser_runner is None or browser_runner.poll() is not None,
            owned_tmux_socket=socket, tmux_stop_exit=tmux_stop.returncode,
            owned_tmux_absent=remaining.returncode != 0,
            plaintext_credentials_removed=not any(path.exists() for path in credentials if not path.is_dir()))
        browser_cleanup = out / 'browser-cleanup.json'
        if browser_cleanup.exists():
            cleanup['browser_web_cleanup'] = json.loads(browser_cleanup.read_text())
        (out / 'cleanup.json').write_text(json.dumps(cleanup, indent=2))
        browser_ok = all(cleanup.get('browser_web_cleanup', {}).values())
        if not (cleanup['daemon_stopped'] and cleanup['browser_runner_stopped']
                and cleanup['owned_tmux_absent'] and cleanup['plaintext_credentials_removed'] and browser_ok):
            failed = {'status': 'CLEANUP_FAIL', 'cleanup': cleanup}
            (out / 'failure.json').write_text(json.dumps(failed, indent=2))
            (out / 'verdict.json').write_text(json.dumps(failed, indent=2))
            raise RuntimeError('owned cleanup did not reach terminal state; inspect cleanup.json')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir', type=Path)
    arguments = p.parse_args()
    destination = arguments.output_dir or Path(tempfile.mkdtemp(prefix='pentacle-own-assistant-'))
    print(json.dumps(run(destination.resolve())))
