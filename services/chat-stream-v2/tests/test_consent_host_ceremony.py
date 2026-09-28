"""One fixed CLI/real-WebSocket ceremony, entirely inside an owned child home.

Synthetic signatures prove the protocol, never Secure Enclave or Face ID.
The existing lifecycle fixture supplies disposable seats, not host sessions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


# Reuse the accepted path-isolation fixture: both resolvers, before imports.
BOOTSTRAP = r'''
import os, pathlib, runpy, sys
root = pathlib.Path(sys.argv[1]).resolve()
source = pathlib.Path(sys.argv[2]).resolve()
def expand(value):
    raw = os.fspath(value)
    if raw == '~': return str(root)
    if raw.startswith('~/'): return str(root / raw[2:])
    if raw.startswith('~'): raise ValueError('named-user home expansion refused')
    return raw
pathlib.Path.home = classmethod(lambda cls: root)
os.path.expanduser = expand
service = source.parent.parent
sys.path[:0] = [str(service), str(source.parent), str(service.parent), str(service.parent / 'agent-orch')]
from agent_orch import config
import local_admin
paths = [config._agent_config_path(), config._token_path(), local_admin.DEFAULT_PATH,
         pathlib.Path(os.environ['AGENT_ORCH_RUNTIME_DIR']).expanduser(),
         pathlib.Path(os.environ['AGENT_ORCH_MEMORY_REPO']).expanduser()]
assert all(p.resolve().is_relative_to(root) for p in paths)
assert config._file_token() == 'synthetic-config-token'
loaded = config.load_config()
assert loaded.runtime_dir.is_relative_to(root) and loaded.memory_repo_path.is_relative_to(root)
assert loaded.host_id == 'node-a'
sys.argv = [str(source), *sys.argv[3:]]
if sys.argv[1] == '--cli':
    from agent_orch.cli import main
    sys.argv = ['agent-orch', *sys.argv[2:]]
    raise SystemExit(main())
runpy.run_path(str(source), run_name='__main__')
'''


def _owned_env(root: Path) -> dict[str, str]:
    # Exclude every inherited agent/seat/runtime/config input. HOME stays intact;
    # process-local resolvers above own all candidate runtime paths instead.
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
    env.update(AGENT_ORCH_WS_URL='ws://127.0.0.1:1', AGENT_ORCH_HOST_ID='node-a',
               AGENT_ORCH_RUNTIME_DIR=str(root / 'runtime'),
               AGENT_ORCH_MEMORY_REPO=str(root / 'memory'), PENTACLE_HOST_ID='node-a')
    return env


def test_isolated_cli_host_ceremony(tmp_path):
    source = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix='consent-host-', dir=tmp_path) as owned:
        root = Path(owned)
        config = root / '.agent-orch/config.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'local_host_id': 'node-a'}))
        token = root / '.config/pentacle-stream/token'
        token.parent.mkdir(mode=0o700, parents=True)
        token.write_text('synthetic-config-token')
        token.chmod(0o600)
        admin = token.with_name('local-admin.token')
        admin.write_text('1' * 64 + '\n')
        admin.chmod(0o600)
        child = subprocess.run([sys.executable, '-I', '-c', BOOTSTRAP, str(root), str(source), '--ceremony'],
                               env=_owned_env(root), capture_output=True, text=True, timeout=45)
        # The child emits only a redacted receipt, never RPC bodies/codes/tokens.
        assert child.returncode == 0, child.stderr
        receipt = json.loads(child.stdout)
        assert receipt['result'] == 'PASS'
        assert receipt['cleanup']['servers_closed'] == receipt['cleanup']['servers_expected'] == 2
        assert receipt['cleanup']['cli_processes_completed'] == receipt['cleanup']['cli_processes_started']
        assert receipt['cleanup']['store_stopped'] is True
        assert receipt['cleanup']['sessions_closed'] == receipt['cleanup']['sessions_expected'] == 2
        assert not admin.exists() and not config.exists() and not (root / 'daemon.sqlite').exists()
    assert not root.exists()
    print(json.dumps({**receipt, 'temporary_root_removed': True}, sort_keys=True))


async def _ceremony(root: Path):
    import asyncio
    import hashlib
    import stat
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from websockets.asyncio.client import connect
    from _shared import operator_auth
    import local_admin
    import store_consent as consent
    from server import Server
    from store import STREAM_TOKEN_HASH_VERSION
    from test_lifecycle_authority import Env, IdleTmux
    from sessions import Sessions
    from store import Store

    db = root / 'daemon.sqlite'
    assert db.resolve().is_relative_to(root)
    store = Store(str(db))
    sessions = None
    servers = []
    sockets = []
    cli_started = cli_completed = 0
    cells = []
    store.start()
    try:
        tmux = IdleTmux()
        sessions = Sessions(store, tmux=tmux, local_host='node-a')
        server = Server(store=store, sessions=sessions, local_host='node-a', port=0)
        assert server.operator_credential_registry.path.resolve().is_relative_to(root)
        registry = server.operator_credential_registry
        registry.initialize()
        cid, _ = registry.issue('pentacle-mobile', label='Synthetic phone')
        web_cid, _ = registry.issue('pentacle', label='Synthetic web')
        env = Env(store, sessions, server, tmux)
        await env.open('requester', role='lead')
        await env.open('bart', role='lead')
        seat_token = 'synthetic-seat-token-for-this-ceremony'
        seat_path = root / 'runtime/seat.token'
        seat_path.parent.mkdir()
        seat_path.write_text(seat_token)
        seat_path.chmod(0o600)
        await store.update_session('node-a', 'requester', token_hash=hashlib.sha256(seat_token.encode()).hexdigest(),
                                   token_hash_version=STREAM_TOKEN_HASH_VERSION)
        assert local_admin.DEFAULT_PATH.is_relative_to(root)
        assert stat.S_IMODE(local_admin.DEFAULT_PATH.stat().st_mode) == 0o600
        await server.bind()
        servers.append(server)
        await server.start_consent()
        endpoint = f'ws://127.0.0.1:{server.port}'

        async def cli(*args, seat_auth=False, expected=0):
            nonlocal cli_started, cli_completed
            process_env = _owned_env(root)
            process_env['AGENT_ORCH_WS_URL'] = endpoint
            if seat_auth:
                process_env.update(AGENT_ORCH_STREAM_ID='node-a:requester', AGENT_ORCH_STREAM_TOKEN_FILE=str(seat_path))
            cli_started += 1
            proc = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', BOOTSTRAP, str(root),
                str(Path(__file__).resolve()), '--cli', *args, env=process_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), 8)
                assert proc.returncode == expected, 'candidate CLI returned an unexpected status'
                return json.loads(stdout)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                cli_completed += 1

        async def socket(kind=None, credential=None, target=endpoint, seat_auth=False):
            ws = await connect(target)
            sockets.append(ws)
            welcome = json.loads(await ws.recv())
            assert welcome['type'] == 'welcome'  # No readiness before current hello/snapshot.
            hello = {'type': 'hello', 'client': kind or 'agent-orch'}
            if credential:
                record = registry.load().credentials[credential]
                proof = operator_auth.make_proof(operator_auth.decode_b64url(record['proof_key']),
                    welcome['auth']['operator']['nonce'], credential, kind)
                hello['auth_v2'] = {'scheme': operator_auth.AUTH_SCHEME, 'credential_id': credential, 'proof': proof}
            if seat_auth:
                hello.update(from_stream_id='node-a:requester', stream_token=seat_token)
            await ws.send(json.dumps(hello))
            while True:
                frame = json.loads(await ws.recv())
                if frame['type'] == 'snapshot':
                    return ws, frame
                assert frame['type'] != 'hello.error'

        async def rpc(ws, verb, **fields):
            import uuid
            rid = str(uuid.uuid4())
            await ws.send(json.dumps({'type': verb, 'request_id': rid, **fields}))
            while True:
                result = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if result.get('request_id') == rid:
                    return result

        def error(result, code):
            assert result.get('error_code') == code, f'admission: expected {code}, got {result.get("error_code")}'

        phone, snapshot = await socket('pentacle-mobile', cid)
        assert snapshot['capabilities']['consent_enrollment_v1'] is True
        web, snapshot = await socket('pentacle', web_cid)
        assert 'consent_enrollment_v1' not in snapshot['capabilities']
        seat_ws, snapshot = await socket(seat_auth=True)
        assert 'consent_enrollment_v1' not in snapshot['capabilities']
        local_ws, snapshot = await socket()
        assert 'consent_enrollment_v1' not in snapshot['capabilities']

        code = (await cli('consent-key', 'enroll-code'))['code']
        for ws in (web, seat_ws, local_ws):
            for verb in ('consent_key.prepare', 'consent_key.enroll'):
                error(await rpc(ws, verb, code=code, local_admin_token=local_admin.read()),
                      'consent_principal_invalid' if ws is local_ws else 'consent_mobile_required')
        cells.append('web/seat/local-admin refused mobile enrollment')
        for condition in ('expired', 'wrong-purpose'):
            invalid_code = (await cli('consent-key', 'enroll-code'))['code']
            digest = hashlib.sha256(invalid_code.encode()).hexdigest()
            def invalidate(conn):
                column, value = ('expires_at', 0) if condition == 'expired' else ('purpose', 'other')
                conn.execute(f'UPDATE v2_consent_enrollment_codes SET {column}=? WHERE code_hash=?', (value, digest))
                conn.commit()
            await store.submit(invalidate)
            for verb in ('consent_key.prepare', 'consent_key.enroll'):
                error(await rpc(phone, verb, code=invalid_code), 'enrollment_code_invalid')
        cells.append('expired/wrong-purpose codes refused')

        async def enroll(enrollment_code):
            private = ec.generate_private_key(ec.SECP256R1())
            spki = consent.encode(private.public_key().public_bytes(serialization.Encoding.DER,
                                                                   serialization.PublicFormat.SubjectPublicKeyInfo))
            prepared = await rpc(phone, 'consent_key.prepare', code=enrollment_code)
            signature = consent.encode(private.sign(consent.enrollment_bytes(prepared['code_hash'], cid,
                spki, prepared['nonce']), ec.ECDSA(hashes.SHA256())))
            key = await rpc(phone, 'consent_key.enroll', code=enrollment_code, spki=spki, signature=signature)
            assert key['state'] == 'pending_confirm'
            return private, key

        private, key = await enroll(code)
        error(await rpc(phone, 'consent_key.prepare', code=code), 'enrollment_code_used')
        pending = await cli('consent', 'request', 'designate', '--target', 'node-a:bart', '--reason', 'Isolated ceremony',
                            seat_auth=True, expected=1)
        error(pending, 'consent_no_active_key')
        error(await cli('consent-key', 'confirm', '0' * 64, expected=1), 'consent_fingerprint_unknown')
        assert (await cli('consent-key', 'list'))['keys'][0]['state'] == 'pending_confirm'
        assert (await cli('consent-key', 'confirm', key['fingerprint']))['state'] == 'active'
        cells.append('one-use/pending-unusable/phone-fingerprint confirm')

        new_private, replacement = await enroll((await cli('consent-key', 'enroll-code'))['code'])
        states = {k['key_id']: k['state'] for k in (await cli('consent-key', 'list'))['keys']}
        assert states[key['key_id']] == 'active' and states[replacement['key_id']] == 'pending_confirm'
        challenge = (await cli('consent', 'request', 'designate', '--target', 'node-a:bart', '--reason',
                               'Exact disposable target', seat_auth=True))['challenge']
        assert challenge['audience_key_ids'] == [key['key_id']]

        async def approve(ch, signing_key, enrolled):
            signature = consent.encode(signing_key.sign(consent.decode(ch['challenge_bytes']), ec.ECDSA(hashes.SHA256())))
            result = await rpc(phone, 'consent.approve', challenge_id=ch['challenge_id'], key_id=enrolled['key_id'],
                               signature=signature)
            assert result['receipt']['consent_id'] == ch['challenge_id']
            return result

        error(await rpc(phone, 'consent.approve', challenge_id=challenge['challenge_id'],
                        key_id=replacement['key_id'], signature=consent.encode(b'invalid')), 'consent_key_invalid')
        assert (await approve(challenge, private, key))['receipt']['revision'] == 1
        assert (await cli('consent', 'status', challenge['challenge_id'], seat_auth=True))['challenge']['state'] == 'approved'
        await cli('consent-key', 'confirm', replacement['fingerprint'])
        states = {k['key_id']: k['state'] for k in (await cli('consent-key', 'list'))['keys']}
        assert states[key['key_id']] == 'retired' and states[replacement['key_id']] == 'active'
        cells.append('prior key approves until replacement confirmed')
        revoke = (await cli('consent', 'request', 'revoke', '--reason', 'Consent reduction', seat_auth=True))['challenge']
        assert (await approve(revoke, new_private, replacement))['receipt']['revision'] == 2
        assert (await env.grant())['stream_id'] is None
        cells.append('synthetic-signature designate and normal revoke')
        cancelled = (await cli('consent', 'request', 'designate', '--target', 'node-a:bart', '--reason',
                               'Cancel rehearsal', seat_auth=True))['challenge']
        error(await cli('consent', 'deny', cancelled['challenge_id'], seat_auth=True, expected=1), 'consent_audience_required')
        assert (await cli('consent', 'cancel', cancelled['challenge_id'], seat_auth=True))['challenge']['state'] == 'cancelled'
        cells.append('CLI status/cancel and seat deny refusal')

        await phone.close()
        phone, snapshot = await socket('pentacle-mobile', cid)
        assert snapshot['capabilities']['consent_enrollment_v1'] is True
        old_host = Server(store=store, sessions=sessions, local_host='other-host', port=0)
        await old_host.bind()
        servers.append(old_host)
        switched, snapshot = await socket('pentacle-mobile', cid, target=f'ws://127.0.0.1:{old_host.port}')
        assert 'consent_enrollment_v1' not in snapshot['capabilities']
        await switched.close()
        reconnected, snapshot = await socket('pentacle-mobile', cid)
        assert snapshot['capabilities']['consent_enrollment_v1'] is True
        cells.append('disconnect/reconnect/host switch fresh snapshots')

        designation = (await cli('consent', 'request', 'designate', '--target', 'node-a:bart', '--reason',
                                  'Emergency reducing proof', seat_auth=True))['challenge']
        await approve(designation, new_private, replacement)
        emergency = dict(action='revoke', emergency=True, expected_revision=3, reason='Disposable lost phone')
        error(await rpc(phone, 'assistant.lifecycle', **emergency), 'emergency_local_only')
        error(await rpc(phone, 'assistant.lifecycle', **emergency, local_admin_token='0' * 64), 'emergency_local_only')
        local_admin.DEFAULT_PATH.chmod(0o644)
        error(await rpc(phone, 'assistant.lifecycle', **emergency, local_admin_token='1' * 64), 'emergency_local_only')
        local_admin.DEFAULT_PATH.chmod(0o600)
        error(await rpc(phone, 'assistant.lifecycle', action='designate', emergency=True,
                        local_admin_token=local_admin.read()), 'emergency_local_only')
        # A verified nonloopback transport is covered in test_consent's reducing-path matrix.
        reduced = await cli('lifecycle', 'revoke', '--emergency', '--reason', 'Isolated emergency reduction')
        assert reduced['receipt']['revision'] == 4 and (await env.grant())['stream_id'] is None
        assert (await env.audit())[-1]['action'] == 'emergency_revoke'
        await cli('consent-key', 'revoke', replacement['fingerprint'])
        error(await cli('consent', 'request', 'designate', '--target', 'node-a:bart', '--reason',
                        'Revoked key refuses', seat_auth=True, expected=1), 'consent_no_active_key')
        cells.append('0600 loopback-admin emergency reduce-only/key revoke')
    finally:
        from v2_runtime import iso_now
        cleanup_errors = []
        closed = servers_closed = 0
        for ws in sockets:
            try:
                await ws.close()
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        for server in servers:
            try:
                await server.close()
                servers_closed += int(server._ws_server is None and server._consent_expiry_task is None)
            except Exception as exc:
                cleanup_errors.append(type(exc).__name__)
        try:
            if sessions:
                for name in ('requester', 'bart'):
                    await store.mark_closed('node-a', name, closed_at=iso_now(), pane_status='pane_dead')
                    closed += int((await store.fetch_session('node-a', name) or {}).get('status') == 'closed')
        finally:
            store.stop()
            for path in root.rglob('*'):
                if path.is_file():
                    path.unlink()
        assert not cleanup_errors, 'CLEANUP_FAIL: ' + ','.join(cleanup_errors)
    return {'result': 'PASS', 'signature_scope': 'synthetic protocol only', 'cells': cells,
            'cleanup': {'servers_closed': servers_closed, 'servers_expected': 2,
                        'cli_processes_completed': cli_completed, 'cli_processes_started': cli_started,
                        'store_stopped': store._thread is None, 'sessions_closed': closed, 'sessions_expected': 2}}


if __name__ == '__main__':
    import asyncio
    print(json.dumps(asyncio.run(_ceremony(Path.home())), sort_keys=True))
