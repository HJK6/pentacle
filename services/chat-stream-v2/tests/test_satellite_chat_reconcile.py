"""Only a stopped writer + unchanged original proof can replace an initial range."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

import satellite
from store import Store
from test_satellite_source_ack import producer, assert_7dd_load
from test_satellite_recovery import HOST, TARGET, NATIVE, _append, _record, _sink, _Wire

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import satellite_chat_reconcile as repair


@pytest.fixture(params=['claude', 'codex'])
def retained(tmp_path, request):
    store = Store(str(tmp_path / 'sessions.db'))
    store.start()
    async def prepare():
        provider = request.param
        path = tmp_path / 'native.jsonl'
        if provider == 'codex':
            _append(path, {'type': 'session_meta', 'payload': {'id': NATIVE}})
        _append(path, _record('initial', provider))
        await store.put('event_push.target_sha', TARGET)
        original = await store.open_session(HOST, 'seat', provider=provider, pane_pid='7001')
        sat = producer(tmp_path)
        sink = _sink(store, [])
        sat._usage_fences = {HOST + ':seat': (await sink._usage_fences(HOST))[0]}
        panes = {'seat': satellite._DiscoveredPane('seat', provider, str(path), pane_pid=7001)}
        events, hw, _ = sat._collect(panes)
        sat._apply_ack(await sat._push(_Wire(sink), events, hw), hw)
        token = sat._tails['seat'].binding['checkpoint_id']
        _append(path, {'type': 'cost-state', 'sessionId': NATIVE})
        invalid_start = path.stat().st_size
        _append(path, _record('missing-tail', provider))
        sat._collect(panes)  # source proof/observed EOF; no delivery
        await store.mark_closed(HOST, 'seat', closed_at='2026-10-28T20:00:00Z',
                                pane_status='pane_dead', expected_generation=original['session_generation'])
        cp_path = tmp_path / 'chat.json'
        data = json.loads(cp_path.read_bytes())
        source = data['sources'][0]
        # Exact old-producer initial-range shape already proven RED on both real
        # smoke ranges. This is disposable metadata, never production mutation.
        source['offset'] = invalid_start
        source['recovery_range'] = {'start': invalid_start, 'end': path.stat().st_size,
                                    'digest': repair.digest(path.read_bytes())}
        source['recovery_range_id'] = uuid.uuid4().hex
        cp_path.write_bytes(repair.encoded(data))
        with repair.ro_database(tmp_path / 'sessions.db') as conn:
            rows = repair.original_rows(conn, HOST, [token])
        snapshot = repair.inspect_checkpoint(cp_path, HOST, rows)
        plan = {'host': HOST, 'coordinator': {'rows': rows}, 'peer': {'snapshot': snapshot}}
        return {'store': store, 'path': path, 'checkpoint': cp_path, 'token': token, 'rows': rows,
                'plan': plan, 'original': original, 'sink': sink, 'provider': provider}
    try:
        yield asyncio.run(prepare())
    finally:
        store.stop()


def authority(state):
    def check():
        with repair.ro_database(state['checkpoint'].parent / 'sessions.db') as conn:
            assert repair.original_rows(conn, HOST, [state['token']]) == state['rows']
    return check


def test_plan_apply_replay_and_7dd_compatibility(retained, tmp_path):
    state = retained
    old = state['checkpoint'].read_bytes()
    before = json.loads(old)
    # Unselected metadata survives the whole-file replacement.
    unselected = copy.deepcopy(before['sources'][0])
    unselected['binding'] = {}
    unselected['session_name'] = 'unselected'
    unselected['path'] = ''
    before['sources'].append(unselected)
    state['checkpoint'].write_bytes(repair.encoded(before))
    state['plan']['peer']['snapshot'] = repair.inspect_checkpoint(state['checkpoint'], HOST, state['rows'])
    old = state['checkpoint'].read_bytes()
    with repair.ro_database(tmp_path / 'sessions.db') as conn:
        db_before = '\n'.join(conn.iterdump())
    receipt = repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    assert receipt['old_sha256'] == repair.digest(old)
    assert (tmp_path / 'receipt/preimage.json').read_bytes() == old
    assert receipt['mapping'][0]['old_id'] != receipt['mapping'][0]['new_id']
    changed = json.loads(state['checkpoint'].read_bytes())
    assert changed['sources'][1] == unselected
    assert changed['sources'][0]['offset'] == state['rows'][state['token']]['checkpoint']['ack_offset']
    assert changed['sources'][0]['binding'] == before['sources'][0]['binding']
    assert_7dd_load(state['checkpoint'])
    with repair.ro_database(tmp_path / 'sessions.db') as conn:
        assert '\n'.join(conn.iterdump()) == db_before
    assert repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))['idempotent']
    async def replay():
        active = producer(tmp_path)
        events, hw, _ = active._collect({})
        ack = await active._push(_Wire(state['sink']), events, hw)
        assert ack['chat_recovery'][0]['status'] == 'drained'
        active._apply_ack(ack, hw)
        assert_7dd_load(state['checkpoint'])
        history = await state['store'].fetch_session_event_tail(HOST + ':seat', limit=100,
            generation=state['original']['session_generation'])
        assert [e['text'] for e in history] == ['initial', 'missing-tail']
        assert (await state['store'].fetch_session(HOST, 'seat'))['status'] == 'closed'
    asyncio.run(replay())
    # An admitted replacement is progress: reapplying an old plan is forbidden.
    with pytest.raises((ValueError, AssertionError)):
        repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))


@pytest.mark.parametrize('fault', ['checkpoint', 'prefix', 'replace', 'truncate', 'mode', 'missing', 'partial'])
def test_changed_plan_or_native_source_never_writes(retained, tmp_path, fault):
    state = retained
    if fault == 'checkpoint':
        state['checkpoint'].write_bytes(state['checkpoint'].read_bytes() + b' ')
    elif fault == 'prefix':
        data = state['path'].read_bytes().replace(b'initial', b'changed')
        state['path'].write_bytes(data)
    elif fault == 'replace':
        old = state['path'].read_bytes()
        state['path'].unlink()
        state['path'].write_bytes(old)
    elif fault == 'truncate':
        state['path'].write_bytes(b'{}\n')
    elif fault == 'mode':
        state['checkpoint'].chmod(0o644)
    elif fault == 'missing':
        state['path'].unlink()
    else:
        raw = json.loads(state['checkpoint'].read_bytes())
        raw['sources'][0]['recovery_range']['start'] -= 1
        state['checkpoint'].write_bytes(repair.encoded(raw))
    checkpoint = state['checkpoint'].read_bytes()
    with pytest.raises((ValueError, OSError)):
        repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    assert state['checkpoint'].read_bytes() == checkpoint
    assert not (tmp_path / 'receipt').exists()


def test_native_append_beyond_fixed_eof_does_not_widen_replacement(retained, tmp_path):
    state = retained
    fixed = json.loads(state['checkpoint'].read_bytes())['sources'][0]['recovery_range']
    _append(state['path'], _record('later-successor', state['provider']))
    repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    after = json.loads(state['checkpoint'].read_bytes())['sources'][0]['recovery_range']
    assert (after['end'], after['digest']) == (fixed['end'], fixed['digest'])
    async def drain_successor():
        sat = producer(tmp_path)
        for _ in range(2):
            events, hw, _ = sat._collect({})
            ack = await sat._push(_Wire(state['sink']), events, hw)
            assert ack['chat_recovery'][0]['status'] == 'drained'
            sat._apply_ack(ack, hw)
        history = await state['store'].fetch_session_event_tail(HOST + ':seat', limit=100,
            generation=state['original']['session_generation'])
        assert [e['text'] for e in history] == ['initial', 'missing-tail', 'later-successor']
        assert_7dd_load(state['checkpoint'])
    asyncio.run(drain_successor())


@pytest.mark.parametrize('boundary', ['before_temp_fsync', 'before_replace', 'after_replace'])
def test_interruption_keeps_old_or_new_and_retry_identity(retained, tmp_path, monkeypatch, boundary):
    state = retained
    before = state['checkpoint'].read_bytes()
    original_fsync, original_replace, original_exclusive = repair.os.fsync, repair.os.replace, repair.exclusive_write
    mkstemp = repair.tempfile.mkstemp
    temporary_fds = set()
    def capture_temp(*args, **kwargs):
        fd, path = mkstemp(*args, **kwargs)
        temporary_fds.add(fd)
        return fd, path
    monkeypatch.setattr(repair.tempfile, 'mkstemp', capture_temp)
    def fail_fsync(fd):
        if boundary == 'before_temp_fsync' and fd in temporary_fds:
            raise OSError('injected before replacement fsync')
        original_fsync(fd)
    def fail_replace(source, target):
        if boundary == 'before_replace':
            raise OSError('injected before replace')
        original_replace(source, target)
    def fail_applied(path, raw):
        if boundary == 'after_replace' and Path(path).name == 'applied.json':
            raise OSError('injected after replace')
        original_exclusive(path, raw)
    monkeypatch.setattr(repair.os, 'fsync', fail_fsync)
    monkeypatch.setattr(repair.os, 'replace', fail_replace)
    monkeypatch.setattr(repair, 'exclusive_write', fail_applied)
    with pytest.raises(OSError):
        repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    prepared = json.loads((tmp_path / 'receipt/prepared.json').read_bytes())
    assert repair.digest(state['checkpoint'].read_bytes()) == prepared['new_sha256' if boundary == 'after_replace' else 'old_sha256']
    monkeypatch.setattr(repair.os, 'fsync', original_fsync)
    monkeypatch.setattr(repair.os, 'replace', original_replace)
    monkeypatch.setattr(repair, 'exclusive_write', original_exclusive)
    result = repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    assert result['mapping'] == prepared['mapping']
    assert state['checkpoint'].read_bytes() != before
    assert_7dd_load(state['checkpoint'])


@pytest.mark.parametrize('at_check', [1, 3, 4])
def test_authority_changes_stop_apply_without_rewinding_progress(retained, tmp_path, at_check):
    state = retained
    before = state['checkpoint'].read_bytes()
    calls = 0
    def changed():
        nonlocal calls
        calls += 1
        if calls == at_check:
            raise ValueError('FD generation or authoritative ACK changed')
        authority(state)()
    with pytest.raises(ValueError, match='changed'):
        repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, changed)
    assert calls == at_check
    if at_check < 4:
        assert state['checkpoint'].read_bytes() == before
    else:
        assert state['checkpoint'].read_bytes() != before
        assert_7dd_load(state['checkpoint'])  # retain progress; never auto-restore old metadata


def test_actual_owned_writer_and_competing_lock_refuse_apply(retained, tmp_path):
    import subprocess
    state = retained
    before = state['checkpoint'].read_bytes()
    writer = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        def stopped():
            repair.require(repair.process_absent(writer.pid), 'writer still alive')
        with pytest.raises(ValueError, match='writer still alive'):
            repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', stopped, authority(state))
        assert state['checkpoint'].read_bytes() == before
    finally:
        writer.terminate()
        writer.wait(timeout=10)
    lock_path = state['checkpoint'].with_name(state['checkpoint'].name + '.reconcile.lock')
    code = ('import fcntl,sys,time; f=open(sys.argv[1],"a"); fcntl.flock(f,fcntl.LOCK_EX); '
            'print("locked",flush=True); time.sleep(60)')
    locker = subprocess.Popen([sys.executable, '-c', code, str(lock_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert locker.stdout.readline().strip() == 'locked'
        with pytest.raises(BlockingIOError):
            repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
        assert state['checkpoint'].read_bytes() == before
    finally:
        locker.terminate()
        locker.wait(timeout=10)


@pytest.mark.parametrize('fault', ['head', 'accepted_range', 'live', 'reused', 'history_native',
                                   'parent', 'ack_at_start', 'wrong_binding', 'wrong_range_id'])
def test_eligibility_refuses_accepted_or_unproved_sources(retained, tmp_path, fault):
    state = retained
    async def mutate():
        if fault == 'head':
            await state['store'].submit(lambda c: c.execute(
                'UPDATE v2_chat_checkpoints SET recovery_head=? WHERE checkpoint_id=?', ('a' * 32, state['token'])))
        elif fault == 'accepted_range':
            await state['store'].submit(lambda c: c.execute('INSERT INTO v2_chat_recovery_ranges '
                '(checkpoint_id,range_id,predecessor,fixed,cursor,recorded_at) VALUES (?,?,?,?,?,?)',
                (state['token'], 'a' * 32, '{}', '{}', 0, 1)))
        elif fault == 'live':
            await state['store'].submit(lambda c: c.execute("UPDATE sessions SET status='open' WHERE host=? AND session_name='seat'", (HOST,)))
        elif fault == 'reused':
            await state['store'].submit(lambda c: c.execute("UPDATE v2_session_generations SET generation=? WHERE host=? AND session_name='seat'", ('a' * 32, HOST)))
        elif fault == 'history_native':
            await state['store'].submit(lambda c: c.execute('UPDATE v2_session_generation_history SET native_session_id=?', ('foreign',)))
        else:
            raw = json.loads(state['checkpoint'].read_bytes())
            source = raw['sources'][0]
            if fault == 'parent':
                source['recovery_parent'] = {'range_id': 'a' * 32, 'end': 0, 'digest': 'a' * 64}
            elif fault == 'ack_at_start':
                source['recovery_range']['start'] = state['rows'][state['token']]['checkpoint']['ack_offset']
            elif fault == 'wrong_binding':
                source['binding']['generation'] = 'f' * 32
            else:
                source['recovery_range_id'] = 'invalid'
            state['checkpoint'].write_bytes(repair.encoded(raw))
    asyncio.run(mutate())
    # submit does not commit arbitrary test SQL; prove the fault is durable
    # before asking the read-only coordinator view to classify it.
    asyncio.run(state['store'].submit(lambda c: c.commit()))
    with pytest.raises(ValueError):
        with repair.ro_database(tmp_path / 'sessions.db') as conn:
            rows = repair.original_rows(conn, HOST, [state['token']])
        repair.inspect_checkpoint(state['checkpoint'], HOST, rows)


def test_readonly_connection_cannot_write(retained, tmp_path):
    import sqlite3
    with repair.ro_database(tmp_path / 'sessions.db') as conn:
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute('DELETE FROM v2_chat_checkpoints')


def test_multiple_sources_replace_as_one_whole_checkpoint(retained, tmp_path):
    state = retained
    async def prepare_second():
        path = tmp_path / 'second.jsonl'
        provider = state['provider']
        if provider == 'codex':
            _append(path, {'type': 'session_meta', 'payload': {'id': NATIVE}})
        _append(path, _record('second-initial', provider))
        original = await state['store'].open_session(HOST, 'second', provider=provider, pane_pid='8001')
        sat = producer(tmp_path)
        sat._usage_fences = {r['stream_id']: r for r in await state['sink']._usage_fences(HOST)}
        panes = {'second': satellite._DiscoveredPane('second', provider, str(path), pane_pid=8001)}
        events, hw, _ = sat._collect(panes)
        sat._apply_ack(await sat._push(_Wire(state['sink']), events, hw), hw)
        token = sat._tails['second'].binding['checkpoint_id']
        _append(path, {'type': 'cost-state', 'sessionId': NATIVE})
        start = path.stat().st_size
        _append(path, _record('second-missing', provider))
        sat._collect(panes)
        await state['store'].mark_closed(HOST, 'second', closed_at='2026-10-28T20:00:00Z',
            pane_status='pane_dead', expected_generation=original['session_generation'])
        raw = json.loads(state['checkpoint'].read_bytes())
        source = next(s for s in raw['sources'] if s['session_name'] == 'second')
        source['offset'] = start
        source['recovery_range'] = {'start': start, 'end': path.stat().st_size, 'digest': repair.digest(path.read_bytes())}
        source['recovery_range_id'] = uuid.uuid4().hex
        state['checkpoint'].write_bytes(repair.encoded(raw))
        return token
    second = asyncio.run(prepare_second())
    with repair.ro_database(tmp_path / 'sessions.db') as conn:
        rows = repair.original_rows(conn, HOST, [state['token'], second])
    plan = {'host': HOST, 'coordinator': {'rows': rows}, 'peer': {'snapshot': repair.inspect_checkpoint(state['checkpoint'], HOST, rows)}}
    plan = json.loads(repair.encoded(plan))  # canonical on-disk key order cannot change source order
    def check():
        with repair.ro_database(tmp_path / 'sessions.db') as conn:
            assert repair.original_rows(conn, HOST, list(rows)) == rows
    result = repair.apply_checkpoint(plan, tmp_path / 'receipt', lambda: None, check)
    assert len(result['mapping']) == 2
    after = json.loads(state['checkpoint'].read_bytes())
    assert {s['binding']['checkpoint_id']: s['offset'] for s in after['sources']} == {
        token: proof['checkpoint']['ack_offset'] for token, proof in rows.items()}
    assert_7dd_load(state['checkpoint'])


def test_cli_plan_apply_and_current_fd_fences(retained, tmp_path, monkeypatch, capsys):
    import machines
    from types import SimpleNamespace
    state = retained
    fd_name = 'v2-fd'
    async def configure():
        fd = await state['store'].open_session(HOST, fd_name, provider='claude', pane_pid='9001')
        def bind(conn):
            from v2_runtime import primary_assistant_name
            conn.execute('INSERT OR REPLACE INTO v2_assistant_direct_binding '
                         '(name,stream_id,generation,revision,updated_at) VALUES (?,?,?,?,?)',
                         ('runtime-primary', HOST + ':' + fd_name, fd['session_generation'], 1, 'fixture'))
            conn.commit()
        await state['store'].submit(bind)
        await state['store'].put('event_push.runtime.' + HOST, json.dumps({
            'sha': TARGET, 'pid': os.getpid(), 'incarnation': 'fixture', 'admitted': True,
            'recovery_pending': 1, 'recovery_gaps': [{'stream_id': HOST + ':seat', 'reason': 'recovery_range_invalid'}]}))
        return fd
    fd = asyncio.run(configure())
    monkeypatch.setattr(machines, 'load_machines', lambda: [SimpleNamespace(name=HOST, ssh_target='fixture-peer')])
    monkeypatch.setattr(repair, 'run', lambda args: '' if '--porcelain' in args else TARGET)
    monkeypatch.setenv('PENTACLE_ASSISTANT_PRIMARY_NAME', 'different-developer-default')
    daemon = {'checkout': str(tmp_path / 'daemon'), 'sha': TARGET, 'pid': os.getpid(), 'birth': 'fixture',
              'assistant_name': 'runtime-primary'}
    monkeypatch.setattr(repair, 'daemon_identity', lambda path, db: daemon)
    peer_service = {'checkout': str(tmp_path), 'sha': TARGET, 'checkpoint_path': str(state['checkpoint']),
                    'pid': os.getpid(), 'birth': 'fixture', 'stopped': False}
    calls = []
    def peer(machine, python, program, request, check_authority=None):
        calls.append(request['action'])
        if request['action'] == 'inspect':
            return {'service': peer_service, 'snapshot': repair.inspect_checkpoint(state['checkpoint'], HOST, request['rows'])}
        check_authority()
        return repair.apply_checkpoint(request['plan'], request['receipt_dir'], lambda: None, check_authority)
    monkeypatch.setattr(repair, 'peer_call', peer)
    args = ['--db', str(tmp_path / 'sessions.db'), '--host', HOST, '--fd-stream', HOST + ':' + fd_name,
            '--fd-generation', fd['session_generation'], '--directive', 'fixture-FD-window',
            '--daemon-checkout', str(tmp_path / 'daemon'), '--peer-checkout', str(tmp_path),
            '--peer-python', sys.executable, '--plan', str(tmp_path / 'plan.json')]
    before = state['checkpoint'].read_bytes()
    repair.main(['plan', *args, '--checkpoint-id', state['token']])
    plan_output = json.loads(capsys.readouterr().out)
    assert plan_output['production_mutations'] is False and state['checkpoint'].read_bytes() == before
    plan = json.loads((tmp_path / 'plan.json').read_bytes())
    assert sum(not e['stored_original'] for s in plan['inventory'] for e in s['events']) == 1
    apply_args = ['apply', *args, '--plan-sha256', plan_output['sha256'], '--receipt-dir', str(tmp_path / 'receipt')]
    # Wrong current FD generation is refused before invoking the mutating peer.
    wrong = list(apply_args)
    wrong[wrong.index('--fd-generation') + 1] = 'f' * 32
    with pytest.raises(ValueError, match='FD binding'):
        repair.main(wrong)
    assert calls == ['inspect']
    repair.main(apply_args)
    result = json.loads(capsys.readouterr().out)
    assert result['satellite_stopped'] and calls == ['inspect', 'apply']
    assert_7dd_load(state['checkpoint'])


def test_peer_transport_revalidates_authority_each_requested_boundary(monkeypatch):
    import machines
    from types import SimpleNamespace
    monkeypatch.setattr(machines, 'ssh_command', lambda target, remote, multiplex: __import__('shlex').split(remote))
    program = '''import json,sys

def peer_entry(request):
    for i in range(3):
        print(json.dumps({'check_authority': True}), flush=True)
        assert json.loads(sys.stdin.readline()) == {'ok': True}
    print(json.dumps({'complete': True}), flush=True)
'''
    checks = []
    result = repair.peer_call(SimpleNamespace(ssh_target='owned-local-fixture'), sys.executable, program, {},
                              check_authority=lambda: checks.append(True))
    assert result == {'complete': True} and len(checks) == 3
    def refuse():
        raise ValueError('changed current FD')
    with pytest.raises(ValueError, match='changed current FD'):
        repair.peer_call(SimpleNamespace(ssh_target='owned-local-fixture'), sys.executable, program, {}, refuse)


def test_readonly_context_closes_connection(retained, tmp_path):
    import sqlite3
    with repair.ro_database(tmp_path / 'sessions.db') as conn:
        conn.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        conn.execute('SELECT 1')


@pytest.mark.parametrize('fault', ['replacement', 'preimage', 'mapping', 'mode'])
def test_interrupted_receipt_cannot_authorize_other_bytes(retained, tmp_path, monkeypatch, fault):
    state = retained
    before = state['checkpoint'].read_bytes()
    replace = repair.os.replace
    monkeypatch.setattr(repair.os, 'replace', lambda *args: (_ for _ in ()).throw(OSError('before replace')))
    with pytest.raises(OSError):
        repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    monkeypatch.setattr(repair.os, 'replace', replace)
    prepared_path = tmp_path / 'receipt/prepared.json'
    prepared = json.loads(prepared_path.read_bytes())
    if fault == 'replacement':
        path = tmp_path / 'receipt/replacement.json'
        content = json.loads(path.read_bytes())
        content['host'] = 'other-host'
        path.write_bytes(repair.encoded(content))
        prepared['new_sha256'] = repair.digest(path.read_bytes())
        prepared_path.write_bytes(repair.encoded(prepared))
    elif fault == 'preimage':
        prepared['old_sha256'] = '0' * 64
        prepared_path.write_bytes(repair.encoded(prepared))
    elif fault == 'mapping':
        prepared['mapping'][0]['new_offset'] += 1
        prepared_path.write_bytes(repair.encoded(prepared))
    else:
        prepared_path.chmod(0o644)
    with pytest.raises(ValueError):
        repair.apply_checkpoint(state['plan'], tmp_path / 'receipt', lambda: None, authority(state))
    assert state['checkpoint'].read_bytes() == before


def test_daemon_proof_refuses_a_database_copy_or_stale_loaded_arguments(tmp_path, monkeypatch):
    import plistlib
    from types import SimpleNamespace
    from deploy import deploy
    root = tmp_path / 'daemon'
    (root / '.pentacle-deploy').mkdir(parents=True)
    (root / '.pentacle-deploy/chat-streamd-v2.json').write_text(json.dumps({'sha': TARGET}))
    db = tmp_path / 'actual.db'
    db.write_bytes(b'placeholder database identity')
    args = [str(root / '.venv/bin/python'), '-u', str(root / 'services/chat-stream-v2/main.py'), '--db', str(db)]
    definition = tmp_path / 'daemon.plist'
    definition.write_bytes(plistlib.dumps({'ProgramArguments': args,
        'EnvironmentVariables': {'PENTACLE_ASSISTANT_PRIMARY_NAME': 'runtime-primary'}}))
    monkeypatch.setattr(deploy, '_launchd_plist_path', lambda label: definition)
    monkeypatch.setattr(deploy, '_launchd_state', lambda label, runner: SimpleNamespace(pid=os.getpid()))
    actual_argv = ['python ' + ' '.join(args[1:])]
    monkeypatch.setattr(repair, 'run', lambda argv: actual_argv[0] if argv[0] == 'ps' else TARGET)
    observed = repair.daemon_identity(root, db)
    assert observed['database']['inode'] == db.stat().st_ino
    assert observed['assistant_name'] == 'runtime-primary'
    copied = tmp_path / 'copy.db'
    copied.write_bytes(db.read_bytes())
    with pytest.raises(ValueError, match='supervised daemon database'):
        repair.daemon_identity(root, copied)
    actual_argv[0] = 'python unrelated-daemon --db ' + str(db)
    with pytest.raises(ValueError, match='loaded daemon arguments'):
        repair.daemon_identity(root, db)


def test_supported_systemd_configuration_and_writer_exclusion(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    env = home / '.config/pentacle/satellite.env'
    env.parent.mkdir(parents=True)
    root = tmp_path / 'peer'
    root.mkdir()
    env.write_text('PENTACLE_SATELLITE_HOST=worker\nPENTACLE_SATELLITE_CHECKOUT=/old/env-file/checkout\nPENTACLE_EVENT_PUSH_SECRET=must-never-export\n')
    env.chmod(0o600)
    definition = tmp_path / 'satellite.service'
    definition.write_text('fixture unit definition')
    checkpoint = tmp_path / 'configured-chat.json'
    monkeypatch.setattr(repair.Path, 'home', lambda: home)
    monkeypatch.setattr(repair.sys, 'platform', 'linux')
    writers = ['']
    def command(args):
        if args[0] == 'systemctl':
            return '\n'.join(['MainPID=0', 'ActiveState=inactive', 'FragmentPath=' + str(definition),
                'ExecStart={ path=/usr/bin/env ; argv[]=/usr/bin/env PENTACLE_SATELLITE_CHECKOUT=' + str(root) + ' '
                + str(root / '.venv/bin/python') + ' ' + str(root / 'services/chat-stream-v2/satellite.py')
                + ' ; ignore_errors=no ; start_time=fixture }',
                'EnvironmentFiles=' + str(env) + ' (ignore_errors=no)',
                'Environment=PENTACLE_SATELLITE_CHAT_CHECKPOINT_PATH=' + str(checkpoint)])
        if args[0] == 'git':
            return '' if '--porcelain' in args else TARGET
        if args[0] == 'ps':
            return writers[0]
        raise AssertionError(args)
    monkeypatch.setattr(repair, 'run', command)
    observed = repair.service_snapshot(root, HOST)
    assert observed['checkpoint_path'] == str(checkpoint)
    assert observed['stopped'] and observed['stop_command'] == ['systemctl', '--user', 'stop', 'pentacle-satellite.service']
    assert 'must-never-export' not in json.dumps(observed)
    # A second checkout still counts as a writer of the default shared state.
    writers[0] = '123 /other-checkout/.venv/bin/python /other-checkout/services/chat-stream-v2/satellite.py'
    with pytest.raises(ValueError, match='duplicate satellite writer'):
        repair.service_snapshot(root, HOST)


def test_supported_launchd_requires_unloaded_supervisor(tmp_path, monkeypatch):
    import plistlib
    from types import SimpleNamespace
    home = tmp_path / 'home'
    env = home / '.config/pentacle/satellite.env'
    env.parent.mkdir(parents=True)
    root = tmp_path / 'peer'
    env.write_text('PENTACLE_SATELLITE_HOST=worker\n')
    env.chmod(0o600)
    definition = home / 'Library/LaunchAgents/com.pentacle.satellite.plist'
    definition.parent.mkdir(parents=True)
    definition.write_bytes(plistlib.dumps({'ProgramArguments': [str(root / '.venv/bin/python'),
        str(root / 'services/chat-stream-v2/satellite.py')], 'WorkingDirectory': str(root / 'services/chat-stream-v2'),
        'EnvironmentVariables': {'PENTACLE_SATELLITE_HOST': HOST}}))
    monkeypatch.setattr(repair.Path, 'home', lambda: home)
    monkeypatch.setattr(repair.sys, 'platform', 'darwin')
    monkeypatch.setattr(repair, 'run', lambda args: TARGET if args[0] == 'git' and '--porcelain' not in args else '')
    answer = [113]
    monkeypatch.setattr(repair.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=answer[0], stdout=''))
    assert repair.service_snapshot(root, HOST)['stopped']
    answer[0] = 0  # loaded but idle can auto-restart; not proved stopped
    assert not repair.service_snapshot(root, HOST)['stopped']
    answer[0] = 1
    with pytest.raises(ValueError, match='ambiguous launchd status'):
        repair.service_snapshot(root, HOST)
