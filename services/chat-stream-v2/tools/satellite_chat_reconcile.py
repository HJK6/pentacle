#!/usr/bin/env python3
"""Plan or apply replacement of never-accepted initial satellite chat ranges.

The coordinator database is always read-only. Apply stops only the configured
satellite and leaves it stopped for receipt verification and supported restart.
This tool is not a deployer, an authority grant, or an accepted-range repair.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

MAX_PREFIX = 1024 * 1024 * 1024
MAX_REPLAY = 64 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    require(result.returncode == 0, 'command failed: ' + argv[0])
    return result.stdout.strip()


@contextmanager
def ro_database(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        yield conn
    finally:
        conn.close()


def original_rows(conn, host, tokens):
    """Freeze authoritative proof; an accepted range is categorically ineligible."""
    result = {}
    for token in tokens:
        row = conn.execute('SELECT * FROM v2_chat_checkpoints WHERE checkpoint_id=?', (token,)).fetchone()
        require(row and row['host'] == host, 'checkpoint not owned by selected host')
        cp = dict(row)
        require(cp['recovery_head'] is None, 'accepted recovery head')
        require(not conn.execute('SELECT 1 FROM v2_chat_recovery_ranges WHERE checkpoint_id=? LIMIT 1',
                                 (token,)).fetchone(), 'accepted recovery range')
        history = conn.execute('SELECT * FROM v2_session_generation_history WHERE host=? '
                               'AND session_name=? AND generation=?',
                               (host, cp['session_name'], cp['generation'])).fetchone()
        current = conn.execute('SELECT s.status,s.created_at,g.generation FROM sessions s '
                               'JOIN v2_session_generations g USING(host,session_name) '
                               'WHERE s.host=? AND s.session_name=?', (host, cp['session_name'])).fetchone()
        require(history and history['closed_at'] and current and current['status'] == 'closed'
                and current['created_at'] == cp['session_created_at']
                and current['generation'] == cp['generation'], 'source live, missing or reused')
        # Generation history has millisecond creation precision; the issued
        # checkpoint carries sessions.created_at. Those are different clocks.
        require(history['provider'] in (None, cp['provider'])
                and history['pane_pid'] in (None, '', cp['pane_pid'])
                and history['native_session_id'] == cp['native_session_id']
                and history['source_file_identity_digest'] == cp['file_digest'], 'original proof mismatch')
        result[token] = {'checkpoint': cp, 'history': dict(history), 'session': dict(current)}
    return result


def fd_binding(conn, stream, generation, assistant_name):
    require(re.fullmatch(r'[^: ]+:v2-[^: ]+', stream) and re.fullmatch(r'[0-9a-f]{32}', generation),
            'invalid FD identity')
    bindings = [dict(r) for r in conn.execute('SELECT name,stream_id,generation,revision '
                                             'FROM v2_assistant_direct_binding WHERE stream_id=? '
                                             'AND generation=?', (stream, generation))]
    require(len(bindings) == 1, 'FD binding changed or ambiguous')
    require(bindings[0]['name'] == assistant_name, 'not the current front desk')
    host, name = stream.split(':', 1)
    row = conn.execute('SELECT s.status,g.generation FROM sessions s JOIN v2_session_generations g '
                       'USING(host,session_name) WHERE host=? AND session_name=?', (host, name)).fetchone()
    require(row and row['status'] == 'open' and row['generation'] == generation, 'FD generation not open')
    return bindings[0]


def process_birth(pid):
    require(type(pid) is int and pid > 0, 'invalid PID')
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                            text=True, timeout=30, env={**os.environ, 'TZ': 'UTC0'})
    require(result.returncode == 0 and result.stdout.strip(), 'PID birth unavailable')
    return result.stdout.strip()


def process_absent(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def checkpoint_bytes(path):
    path = Path(path)
    require(not path.is_symlink() and path.resolve() == path.absolute(), 'checkpoint path contains symlink')
    st = path.stat()
    require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
            and stat.S_IMODE(st.st_mode) == 0o600, 'checkpoint owner/mode/type')
    require(st.st_size <= MAX_REPLAY, 'oversized checkpoint')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
        opened = os.fstat(handle.fileno())
        require((opened.st_dev, opened.st_ino) == (st.st_dev, st.st_ino), 'checkpoint replaced')
        raw = handle.read(MAX_REPLAY + 1)
    after = path.stat()
    require(all(getattr(after, k) == getattr(st, k) for k in
                ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')), 'checkpoint changed during read')
    return raw, {'path': str(path), 'owner': st.st_uid, 'mode': stat.S_IMODE(st.st_mode),
                 'sha256': digest(raw)}


def source_facts(source, cp, host):
    """Read original bytes twice, export identities/counts only, never text."""
    import chat_recovery
    from claude_jsonl_norm import normalize_claude_jsonl_records_grouped
    from codex_rollout_norm import normalize_codex_rollout_records_grouped
    from ingest import _identity_key
    binding = source.get('binding') or {}
    expected = {k: cp[k] for k in chat_recovery.SOURCE_FIELDS if k != 'stream_id'}
    expected['stream_id'] = host + ':' + cp['session_name']
    require(chat_recovery.source_valid(binding, host) and binding.get('checkpoint_id') == cp['checkpoint_id']
            and all(binding.get(k) == v for k, v in expected.items()), 'local original binding mismatch')
    require(source.get('session_name') == cp['session_name'] and source.get('provider') == cp['provider']
            and source.get('provider_session_id') == cp['native_session_id']
            and str(source.get('source_pane_pid') or '').removeprefix('!') == cp['pane_pid'], 'local source identity mismatch')
    fixed = source.get('recovery_range') or {}
    require(set(fixed) == {'start', 'end', 'digest'} and not source.get('recovery_parent')
            and re.fullmatch(r'[0-9a-f]{32}', str(source.get('recovery_range_id'))), 'not an initial fixed range')
    values = [cp['origin_offset'], cp['ack_offset'], fixed['start'], source.get('offset'), fixed['end']]
    require(all(chat_recovery.offset(x) for x in values)
            and values[0] <= values[1] < values[2] <= values[3] <= values[4], 'not ahead of actual ACK')
    require(not source.get('recovery_gap') and not source.get('partial_tail_bytes'), 'explicit source gap')
    path = Path(source['path'])
    require(not path.is_symlink(), 'native source symlink')
    before = path.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid(), 'native owner/type')
    identity = digest(f'{path}\0{before.st_dev}\0{before.st_ino}'.encode('utf-8', 'surrogateescape'))
    require(identity == cp['file_digest'] and fixed['end'] <= before.st_size
            and fixed['end'] <= MAX_PREFIX and fixed['end'] - cp['ack_offset'] <= MAX_REPLAY,
            'native file identity/bounds')
    def read_prefix():
        h = hashlib.sha256()
        with path.open('rb') as handle:
            opened = os.fstat(handle.fileno())
            require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino), 'native file replaced')
            left = fixed['end']
            while left:
                block = handle.read(min(left, 1024 * 1024))
                require(block, 'native source truncated')
                h.update(block)
                left -= len(block)
            data = {}
            for offset in {cp['ack_offset'], fixed['start'], source['offset'], fixed['end']}:
                if offset:
                    handle.seek(offset - 1)
                    require(handle.read(1) == b'\n', 'partial native record boundary')
            observed = source.get('observed_size')
            require(type(observed) is int and fixed['end'] <= observed <= min(before.st_size, MAX_PREFIX),
                    'invalid observed native prefix')
            handle.seek(0)
            anchor = hashlib.sha256()
            remaining = observed
            while remaining:
                block = handle.read(min(remaining, 1024 * 1024))
                require(block, 'observed source truncated')
                anchor.update(block)
                remaining -= len(block)
            require(anchor.hexdigest() == source.get('observed_digest'), 'observed prefix changed')
            handle.seek(cp['ack_offset'])
            data = handle.read(fixed['end'] - cp['ack_offset'])
        after = path.stat()
        require((after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
                and after.st_size >= fixed['end'], 'native source replaced/truncated')
        require(h.hexdigest() == fixed['digest'], 'retained prefix changed')
        return data
    data = read_prefix()
    records = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue  # same malformed-record behavior as the producer
        if not isinstance(record, dict):
            continue
        native = record.get('sessionId') or record.get('session_id')
        if cp['provider'] == 'codex':
            from codex_rollout_norm import codex_session_identity
            native = codex_session_identity(record)
        require(not native or native == cp['native_session_id'], 'native source reuse')
        records.append(record)
    if cp['provider'] == 'codex':
        from satellite import _codex_session_identity_from_bound_path
        require(_codex_session_identity_from_bound_path(str(path)) == cp['native_session_id'],
                'native header changed')
        groups = normalize_codex_rollout_records_grouped(records, host=host, session_name=cp['session_name'],
                                                         session_id=cp['native_session_id'])
    else:
        groups = normalize_claude_jsonl_records_grouped(records, host=host, session_name=cp['session_name'])
    events = [{'identity': _identity_key(e), 'kind': e.get('kind'),
               'text_length': len(str(e.get('text') or '')), 'text_sha256': digest(str(e.get('text') or '').encode())}
              for group in groups for e in group]
    require(read_prefix() == data, 'native prefix changed across normalization')
    require(source.get('observed_size', -1) >= fixed['end'], 'invalid observed bounds')
    return {'checkpoint_id': cp['checkpoint_id'], 'path': str(path), 'device': before.st_dev,
            'inode': before.st_ino, 'end': fixed['end'], 'prefix_sha256': fixed['digest'],
            'events': events}


def validate_checkpoint(path, host):
    # Use the installed producer's existing loader without constructing a
    # Satellite, starting discovery, or writing any runtime state.
    from types import SimpleNamespace
    from satellite import Satellite
    witness = SimpleNamespace(config=SimpleNamespace(host=host, chat_checkpoint_path=str(path)))
    Satellite._load_chat_checkpoint(witness)
    require(not witness._chat_gap, 'checkpoint fails installed loader')


def inspect_checkpoint(path, host, rows):
    raw, metadata = checkpoint_bytes(path)
    validate_checkpoint(path, host)
    data = json.loads(raw)
    require(data.get('version') == 1 and data.get('host') == host and isinstance(data.get('sources'), list),
            'checkpoint schema/host')
    facts = []
    for token, proof in rows.items():
        selected = [s for s in data['sources'] if (s.get('binding') or {}).get('checkpoint_id') == token]
        require(len(selected) == 1, 'selected source not unique')
        facts.append(source_facts(selected[0], proof['checkpoint'], host))
    require(checkpoint_bytes(path)[0] == raw, 'checkpoint changed across inspection')
    return {'checkpoint': metadata, 'sources': facts}


def replacement(raw, rows, range_ids=None):
    """Keep existing schema and every unselected row byte-for-byte semantically."""
    data = json.loads(raw)
    result = copy.deepcopy(data)
    mapping = []
    if range_ids is not None:
        require(set(range_ids) == set(rows), 'prepared source selection changed')
    for source in result['sources']:
        token = (source.get('binding') or {}).get('checkpoint_id')
        if token not in rows:
            continue
        old = source['recovery_range_id']
        source['offset'] = rows[token]['checkpoint']['ack_offset']
        source['recovery_range']['start'] = source['offset']
        source['recovery_range_id'] = range_ids[token] if range_ids is not None else uuid.uuid4().hex
        require(re.fullmatch(r'[0-9a-f]{32}', source['recovery_range_id'])
                and source['recovery_range_id'] != old, 'replacement ID collision/shape')
        mapping.append({'checkpoint_id': token, 'old_id': old, 'new_id': source['recovery_range_id'],
                        'old_offset': next(s['offset'] for s in data['sources']
                                           if (s.get('binding') or {}).get('checkpoint_id') == token),
                        'new_offset': source['offset']})
    require(len(mapping) == len(rows), 'replacement selection changed')
    return encoded(result), mapping


def exclusive_write(path, raw):
    with Path(path).open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_retained_preimage(raw, plan):
    data = json.loads(raw)
    actual = []
    for token, proof in plan['coordinator']['rows'].items():
        selected = [s for s in data['sources'] if (s.get('binding') or {}).get('checkpoint_id') == token]
        require(len(selected) == 1, 'preimage selected source changed')
        actual.append(source_facts(selected[0], proof['checkpoint'], plan['host']))
    require(actual == plan['peer']['snapshot']['sources'], 'retained native source changed')


def apply_checkpoint(plan, receipt_dir, check_stopped, check_authority):
    """No producer resumes here. Recovery always preserves the newest checkpoint."""
    path = Path(plan['peer']['snapshot']['checkpoint']['path'])
    receipt_dir = Path(receipt_dir)
    require(receipt_dir.resolve() != path.parent and receipt_dir.resolve() == receipt_dir.absolute()
            and not receipt_dir.is_symlink(), 'invalid receipt location')
    with path.with_name(path.name + '.reconcile.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_stopped()
        check_authority()
        require(receipt_dir.parent.is_dir(), 'receipt parent must already exist')
        if receipt_dir.exists():
            require(stat.S_IMODE(receipt_dir.stat().st_mode) == 0o700
                    and receipt_dir.stat().st_uid == os.getuid(), 'receipt owner/mode')
            prepared = json.loads(checkpoint_bytes(receipt_dir / 'prepared.json')[0])
            require(prepared['plan_sha256'] == digest(encoded(plan)), 'different apply owns receipt')
            current, _ = checkpoint_bytes(path)
            require(digest(current) in {prepared['old_sha256'], prepared['new_sha256']}, 'checkpoint CAS changed')
            preimage = checkpoint_bytes(receipt_dir / 'preimage.json')[0]
            require(digest(preimage) == prepared['old_sha256']
                    == plan['peer']['snapshot']['checkpoint']['sha256'], 'preimage changed')
            verify_retained_preimage(preimage, plan)
            new = checkpoint_bytes(receipt_dir / 'replacement.json')[0]
            expected, mapping = replacement(preimage, plan['coordinator']['rows'],
                {m['checkpoint_id']: m['new_id'] for m in prepared['mapping']})
            require(new == expected and mapping == prepared['mapping']
                    and digest(new) == prepared['new_sha256'], 'prepared replacement changed')
            if digest(current) == prepared['new_sha256']:
                check_stopped()
                check_authority()
                result = {**prepared, 'idempotent': True, 'satellite_stopped': True}
                if (receipt_dir / 'applied.json').exists():
                    applied = json.loads(checkpoint_bytes(receipt_dir / 'applied.json')[0])
                    require(all(applied.get(k) == v for k, v in prepared.items()), 'applied receipt changed')
                else:
                    exclusive_write(receipt_dir / 'applied.json', encoded(result))
                    fsync_dir(receipt_dir)
                return result
        else:
            require(inspect_checkpoint(path, plan['host'], plan['coordinator']['rows']) == plan['peer']['snapshot'],
                    'plan changed before apply')
            current, _ = checkpoint_bytes(path)
            new, mapping = replacement(current, plan['coordinator']['rows'])
            prepared = {'plan_sha256': digest(encoded(plan)), 'old_sha256': digest(current),
                        'new_sha256': digest(new), 'mapping': mapping}
            receipt_dir.mkdir(mode=0o700)
            exclusive_write(receipt_dir / 'preimage.json', current)
            exclusive_write(receipt_dir / 'replacement.json', new)
            exclusive_write(receipt_dir / 'prepared.json', encoded(prepared))
            fsync_dir(receipt_dir)
            fsync_dir(receipt_dir.parent)
        validate_checkpoint(receipt_dir / 'replacement.json', plan['host'])
        check_stopped()
        check_authority()
        require(inspect_checkpoint(path, plan['host'], plan['coordinator']['rows']) == plan['peer']['snapshot'],
                'source or checkpoint changed before replace')
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.reconcile-', dir=path.parent)
            with os.fdopen(descriptor, 'wb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(new)
                handle.flush()
                os.fsync(handle.fileno())
            require(checkpoint_bytes(path)[0] == current, 'checkpoint CAS changed at replace')
            check_stopped()
            check_authority()
            os.replace(temporary, path)
            fsync_dir(path.parent)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        require(checkpoint_bytes(path)[0] == new, 'replacement readback failed')
        verify_retained_preimage((receipt_dir / 'preimage.json').read_bytes(), plan)
        check_stopped()
        check_authority()
        result = {**prepared, 'idempotent': False, 'satellite_stopped': True}
        exclusive_write(receipt_dir / 'applied.json', encoded(result))
        fsync_dir(receipt_dir)
        return result


def service_snapshot(checkout, host):
    """Inspect the supported supervisor; never export environment secret values."""
    import plistlib
    checkout = Path(checkout)
    env_file = Path.home() / '.config/pentacle/satellite.env'
    require(env_file.is_file() and not env_file.is_symlink(), 'satellite environment missing')
    require(env_file.stat().st_uid == os.getuid() and stat.S_IMODE(env_file.stat().st_mode) == 0o600,
            'satellite environment owner/mode')
    safe_keys = {'PENTACLE_SATELLITE_HOST', 'PENTACLE_SATELLITE_CHECKOUT', 'PENTACLE_SATELLITE_CHAT_CHECKPOINT_PATH'}
    safe = {}
    for line in env_file.read_text().splitlines():
        line = line.removeprefix('export ').strip()
        key, sep, value = line.partition('=')
        if sep and key in safe_keys:
            parts = shlex.split(value, comments=True)
            require(len(parts) == 1 and not any(c in parts[0] for c in ('$', '`', '\n')), 'dynamic satellite path')
            safe[key] = parts[0]
    script = str(checkout / 'services/chat-stream-v2/satellite.py')
    python = str(checkout / '.venv/bin/python')
    if sys.platform == 'darwin':
        definition = Path.home() / 'Library/LaunchAgents/com.pentacle.satellite.plist'
        config = plistlib.loads(definition.read_bytes())
        args = config.get('ProgramArguments') or []
        require(args and python in ' '.join(args) and script in ' '.join(args), 'satellite program mismatch')
        require(config.get('WorkingDirectory') == str(checkout / 'services/chat-stream-v2'), 'satellite working directory')
        definition_safe = {k: v for k, v in (config.get('EnvironmentVariables') or {}).items() if k in safe_keys}
        if args[:2] == ['/bin/bash', '-lc']:
            require('. "$HOME/.config/pentacle/satellite.env"' in args[-1], 'unrecognized launchd environment source')
            safe = {**definition_safe, **safe}
        else:
            require(args == [python, script], 'unrecognized launchd program')
            safe = definition_safe
        target = f'gui/{os.getuid()}/com.pentacle.satellite'
        result = subprocess.run(['launchctl', 'print', target], capture_output=True, text=True, timeout=30)
        require(result.returncode in (0, 113), 'ambiguous launchd status')
        match = re.search(r'^\s*pid = (\d+)\s*$', result.stdout, re.M) if result.returncode == 0 else None
        pid = int(match[1]) if match else 0
        stopped = result.returncode == 113  # unloaded, not merely idle under KeepAlive
        stop = ['launchctl', 'bootout', target]
    else:
        target = 'pentacle-satellite.service'
        values = run(['systemctl', '--user', 'show', target, '--property=MainPID,ActiveState,FragmentPath,ExecStart,Environment,EnvironmentFiles'])
        props = dict(line.split('=', 1) for line in values.splitlines() if '=' in line)
        definition = Path(props.get('FragmentPath', ''))
        require(props.get('EnvironmentFiles') == f'{env_file} (ignore_errors=no)', 'unrecognized environment file')
        unit_safe = {}
        for assignment in shlex.split(props.get('Environment', '')):
            key, sep, value = assignment.partition('=')
            if sep and key in safe_keys:
                unit_safe[key] = value
        safe = {**unit_safe, **safe}
        require(python in props.get('ExecStart', '') and script in props.get('ExecStart', ''), 'satellite program mismatch')
        pid = int(props.get('MainPID') or 0)
        stopped = props.get('ActiveState') == 'inactive' and pid == 0
        stop = ['systemctl', '--user', 'stop', target]
    require(safe.get('PENTACLE_SATELLITE_HOST') == host, 'configured host mismatch')
    require(safe.get('PENTACLE_SATELLITE_CHECKOUT', str(checkout)) == str(checkout), 'configured checkout mismatch')
    from satellite import DEFAULT_CHAT_CHECKPOINT_PATH
    checkpoint = Path(safe.get('PENTACLE_SATELLITE_CHAT_CHECKPOINT_PATH') or DEFAULT_CHAT_CHECKPOINT_PATH).expanduser()
    sha = run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'])
    require(re.fullmatch(r'[0-9a-f]{40}', sha) and not run(['git', '-C', str(checkout), 'status', '--porcelain']),
            'satellite checkout not clean')
    writers = []
    for line in run(['ps', '-axo', 'pid=,args=']).splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and ('/services/chat-stream-v2/satellite.py' in fields[1]
                                 or re.search(r'(?:^|\s)-m\s+satellite(?:\s|$)', fields[1])):
            writers.append(int(fields[0]))
    require(sorted(writers) == ([pid] if pid else []), 'unmanaged or duplicate satellite writer')
    return {'checkout': str(checkout), 'sha': sha, 'definition': str(definition),
            'definition_sha256': digest(definition.read_bytes()), 'environment_sha256': digest(env_file.read_bytes()),
            'checkpoint_path': str(checkpoint), 'pid': pid, 'birth': process_birth(pid) if pid else None,
            'stopped': stopped, 'writers': writers, 'stop_command': stop}


def peer_main(request):
    root = Path(request['checkout'])
    require(Path(sys.prefix).resolve() == (root / '.venv').resolve(), 'peer interpreter must be the dedicated service venv')
    sys.path[:0] = [str(root / 'services/chat-stream-v2'), str(root / 'services/_shared'),
                    str(root / 'services/agent-orch')]
    host = request['host']
    service = service_snapshot(root, host)
    if request['action'] == 'inspect':
        result = {'service': service, 'snapshot': inspect_checkpoint(service['checkpoint_path'], host, request['rows'])}
    else:
        plan = request['plan']
        prior = plan['peer']['service']
        for key in ('checkout', 'sha', 'definition', 'definition_sha256', 'environment_sha256', 'checkpoint_path'):
            require(service[key] == prior[key], 'peer service/configuration changed')
        old_pid = prior['pid'] or plan['coordinator']['peer_runtime']['pid']
        if not service['stopped']:
            require(service['pid'] == old_pid and service['birth'] == prior['birth'], 'supervisor PID changed')
            run(service['stop_command'])
        deadline = time.monotonic() + 30
        while old_pid and not process_absent(old_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        def stopped():
            now = service_snapshot(root, host)
            require(now['stopped'] and not now['writers'] and (not old_pid or process_absent(old_pid)),
                    'satellite writer or auto-restart not excluded')
            for key in ('sha', 'definition_sha256', 'environment_sha256', 'checkpoint_path'):
                require(now[key] == prior[key], 'stopped supervisor configuration changed')
        def authority():
            # Read authoritative DB/FD/PID fences on the coordinator at each
            # write boundary; the peer never receives a writable database.
            print(json.dumps({'check_authority': True}), flush=True)
            response = json.loads(sys.stdin.readline())
            require(response == {'ok': True}, 'coordinator authority changed')
        result = apply_checkpoint(plan, request['receipt_dir'], stopped, authority)
    print(json.dumps(result, sort_keys=True))


def peer_entry(request):
    try:
        peer_main(request)
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'peer_error': type(exc).__name__, 'reason': str(exc),
                          'satellite_may_be_stopped': request.get('action') == 'apply'}), flush=True)
        raise SystemExit(2)


def peer_call(machine, python, program, request, check_authority=None):
    from machines import ssh_command
    import selectors
    bootstrap = ('import json,sys; b=json.loads(sys.stdin.readline()); '
                 's={"__name__":"reconcile_peer","__file__":"<reviewed-reconciliation-tool>"}; '
                 'exec(compile(b["program"],s["__file__"],"exec"),s); s["peer_entry"](b["request"])')
    argv = ssh_command(machine.ssh_target, shlex.join([python, '-I', '-B', '-c', bootstrap]), multiplex=False)
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, bufsize=1) as child:
        deadline = time.monotonic() + 180
        try:
            child.stdin.write(json.dumps({'program': program, 'request': request}) + '\n')
            child.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    require(selector.select(max(0, deadline - time.monotonic())), 'peer timeout; inspect stopped service')
                    line = child.stdout.readline()
                    require(line, 'peer refused; inspect stopped service and retain checkpoint/receipts')
                    result = json.loads(line)
                    if result == {'check_authority': True}:
                        require(check_authority is not None, 'unexpected mutation authority check')
                        check_authority()
                        child.stdin.write(json.dumps({'ok': True}) + '\n')
                        child.stdin.flush()
                    else:
                        require('peer_error' not in result, 'peer refused: ' + str(result.get('reason', 'unknown')))
                        child.stdin.close()
                        require(child.wait(timeout=30) == 0, 'peer reconciliation failed; inspect stopped service')
                        return result
        finally:
            if child.poll() is None:
                try:
                    child.stdin.close()  # EOF refuses the next authority boundary
                except OSError:
                    pass
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()  # only this owned SSH transport, never a provider/service
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)


def daemon_identity(checkout, db):
    import plistlib
    from deploy.deploy import (_launchd_state, V2_DAEMON_LABEL, _launchd_plist_path,
                               _validate_v2_launchd_program)
    def runner(argv, cwd):
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
    state = _launchd_state(V2_DAEMON_LABEL, runner)
    require(state.pid and state.pid > 0, 'daemon supervisor has no PID')
    definition = _launchd_plist_path(V2_DAEMON_LABEL).read_bytes()
    configured = plistlib.loads(definition)
    arguments = configured.get('ProgramArguments') or []
    from v2_runtime import primary_assistant_name
    assistant_name = primary_assistant_name(configured.get('EnvironmentVariables') or {})
    _validate_v2_launchd_program(Path(checkout), arguments)
    require(arguments.count('--db') == 1, 'daemon database argument ambiguous')
    configured_db = Path(arguments[arguments.index('--db') + 1])
    require(configured_db.is_absolute() and configured_db.resolve() == Path(db).resolve(),
            'database is not the supervised daemon database')
    argv = run(['ps', '-ww', '-p', str(state.pid), '-o', 'args='])
    require(' '.join(arguments[1:]) in argv, 'loaded daemon arguments changed')
    database = configured_db.stat()
    stamp_path = Path(checkout) / '.pentacle-deploy/chat-streamd-v2.json'
    stamp_raw = stamp_path.read_bytes()
    sha = run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'])
    require(json.loads(stamp_raw).get('sha') == sha, 'daemon checkout/stamp mismatch')
    return {'checkout': str(checkout), 'sha': sha, 'assistant_name': assistant_name,
            'stamp_sha256': digest(stamp_raw),
            'pid': state.pid, 'birth': process_birth(state.pid), 'definition_sha256': digest(definition),
            'argv_sha256': digest(argv.encode()), 'database': {'path': str(configured_db.resolve()),
                'device': database.st_dev, 'inode': database.st_ino}}


def coordinator_snapshot(db, host, tokens, stream, generation, assistant_name):
    with ro_database(db) as conn:
        conn.execute('BEGIN')
        binding = fd_binding(conn, stream, generation, assistant_name)
        rows = original_rows(conn, host, tokens)
        def kv(key):
            row = conn.execute('SELECT v FROM kv WHERE k=?', (key,)).fetchone()
            return row[0] if row else None
        runtime = json.loads(kv('event_push.runtime.' + host) or '{}')
        return {'fd': binding, 'rows': rows, 'pin': kv('event_push.target_sha'),
                'pending': runtime.get('recovery_pending'),
                'gaps': sorted(runtime.get('recovery_gaps') or [], key=lambda x: encoded(x)),
                'peer_runtime': {k: runtime.get(k) for k in ('sha', 'pid', 'incarnation', 'admitted')}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply'])
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--fd-stream', required=True)
    parser.add_argument('--fd-generation', required=True)
    parser.add_argument('--directive', required=True, help='Recorded FD window/directive provenance; not a bearer grant')
    parser.add_argument('--daemon-checkout', type=Path, required=True)
    parser.add_argument('--peer-checkout', type=Path, required=True)
    parser.add_argument('--peer-python', required=True)
    parser.add_argument('--checkpoint-id', action='append', default=[])
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--receipt-dir', help='Absolute owned directory on peer, created exclusively by this apply')
    args = parser.parse_args(argv)
    service = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(service), str(service.parent / '_shared'), str(service.parent / 'agent-orch')]
    from machines import load_machines
    machines = [m for m in load_machines() if m.name == args.host and m.ssh_target]
    require(len(machines) == 1, 'host must have one configured SSH route')
    require(args.peer_checkout.is_absolute() and Path(args.peer_python).is_absolute(), 'absolute peer paths required')
    require(args.directive.strip() and len(args.directive) <= 512, 'FD directive required')
    program = Path(__file__).read_text()
    tool_sha = run(['git', '-C', str(service.parents[1]), 'rev-parse', 'HEAD'])
    require(not run(['git', '-C', str(service.parents[1]), 'status', '--porcelain']), 'tool checkout must be frozen')
    if args.action == 'plan':
        require(args.checkpoint_id and len(set(args.checkpoint_id)) == len(args.checkpoint_id), 'exact unique source selection required')
        daemon = daemon_identity(args.daemon_checkout, args.db)
        state = coordinator_snapshot(args.db, args.host, args.checkpoint_id, args.fd_stream, args.fd_generation, daemon['assistant_name'])
        selected_streams = {args.host + ':' + p['checkpoint']['session_name'] for p in state['rows'].values()}
        require(selected_streams == {g.get('stream_id') for g in state['gaps']},
                'exact pending-source catalog required; unknown or omitted gap')
        peer = peer_call(machines[0], args.peer_python, program, {'action': 'inspect', 'host': args.host,
                         'checkout': str(args.peer_checkout), 'rows': state['rows']})
        runtime = state['peer_runtime']
        require(runtime['sha'] == peer['service']['sha'] and runtime['admitted'] is True
                and runtime['incarnation'] and (peer['service']['stopped'] or runtime['pid'] == peer['service']['pid']),
                'peer PID/source/incarnation admission mismatch')
        with ro_database(args.db) as conn:
            for source in peer['snapshot']['sources']:
                cp = state['rows'][source['checkpoint_id']]['checkpoint']
                for event in source['events']:
                    event['stored_original'] = bool(conn.execute('SELECT 1 FROM session_event_tail WHERE stream_id=? '
                        'AND identity=? AND session_created_at=? LIMIT 1',
                        (args.host + ':' + cp['session_name'], event['identity'], cp['session_created_at'])).fetchone())
        # Inventory is metadata only. The peer's CAS snapshot excludes coordinator
        # annotations, so retain them separately from the compared native proof.
        inventory = copy.deepcopy(peer['snapshot']['sources'])
        for source in peer['snapshot']['sources']:
            for event in source['events']:
                event.pop('stored_original', None)
        require(coordinator_snapshot(args.db, args.host, args.checkpoint_id, args.fd_stream, args.fd_generation, daemon['assistant_name']) == state,
                'coordinator changed across plan')
        require(daemon_identity(args.daemon_checkout, args.db) == daemon, 'daemon changed across plan')
        plan = {'version': 1, 'host': args.host, 'tool_sha': tool_sha, 'tool_sha256': digest(program.encode()),
                'directive': args.directive, 'observed_at': time.time(), 'daemon': daemon, 'peer': peer,
                'coordinator': state, 'inventory': inventory,
                'peer_python': args.peer_python, 'ssh_target': machines[0].ssh_target}
        exclusive_write(args.plan, encoded(plan))
        print(json.dumps({'plan': str(args.plan), 'sha256': digest(encoded(plan)), 'selected': len(state['rows']),
                          'production_mutations': False}))
        return
    require(args.plan_sha256 and args.receipt_dir and Path(args.receipt_dir).is_absolute(), 'digest and peer receipt required')
    raw = args.plan.read_bytes()
    require(digest(raw) == args.plan_sha256, 'plan digest mismatch')
    plan = json.loads(raw)
    require(plan['host'] == args.host and plan['tool_sha'] == tool_sha
            and plan['tool_sha256'] == digest(program.encode()) and plan['peer_python'] == args.peer_python
            and plan['peer']['service']['checkout'] == str(args.peer_checkout)
            and plan['ssh_target'] == machines[0].ssh_target, 'plan execution inputs changed')
    def revalidate():
        require(coordinator_snapshot(args.db, args.host, list(plan['coordinator']['rows']), args.fd_stream, args.fd_generation, plan['daemon']['assistant_name'])
                == plan['coordinator'], 'authoritative plan changed')
        require(daemon_identity(args.daemon_checkout, args.db) == plan['daemon'], 'daemon PID/artifact changed')
    revalidate()
    result = peer_call(machines[0], args.peer_python, program, {'action': 'apply', 'host': args.host,
                       'checkout': str(args.peer_checkout), 'plan': plan, 'receipt_dir': args.receipt_dir},
                       check_authority=revalidate)
    revalidate()
    print(json.dumps({'plan_sha256': args.plan_sha256, 'apply_directive': args.directive, 'result': result,
                      'receipt_dir': args.receipt_dir, 'satellite_stopped': True,
                      'next_action': 'FD verifies receipt then resumes captured supported satellite; never restore DB/checkpoint progress'}))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'ok': False, 'error': type(exc).__name__, 'reason': str(exc),
                          'next_action': 'retain all checkpoints/ranges/receipts; inspect with FD before resuming'}), file=sys.stderr)
        raise SystemExit(2)
