"""Phone-signed privileged consent; isolated registry, Store and transport context."""
import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from _shared import operator_auth
import store_consent as consent
from test_lifecycle_authority import Env, scenario
from sessions import VerbError


def test_messages_without_local_admin_token_do_not_yield_to_token_verifier(monkeypatch):
    """Ordinary fire-and-forget blob chunks retain their arrival ordering."""
    import asyncio
    import local_admin
    from server import Server

    class Peer:
        remote_address = ('127.0.0.1', 12345)

    def unexpected_verify(*args):
        raise AssertionError('ordinary messages must not enter the recovery-token verifier')

    monkeypatch.setattr(local_admin, 'verify', unexpected_verify)
    result = asyncio.run(Server()._auth_context(Peer(), {'type': 'upload_blob_chunk'}))
    assert result['local_admin_verified'] is False


class Ceremony:
    def __init__(self, env, tmp_path):
        self.env = env
        self.registry = operator_auth.OperatorCredentialRegistry(tmp_path / 'credentials.json')
        self.registry.initialize()
        self.cid, _ = self.registry.issue('pentacle-mobile', label='Paired test phone')
        self.auth = {'operator_authenticated': True, 'operator_principal': f'operator:{self.cid}',
                     'connection_client': 'pentacle-mobile', 'transport': 'v2'}
        self.local = {'peer_loopback': True, 'local_admin_verified': True}
        env.server.operator_credential_registry = self.registry
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.spki = consent.encode(self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))

    async def call(self, verb, auth=None, **msg):
        return await self.env.server._on_consent({'type': verb, '_auth_context': auth or self.auth, **msg})

    def sign(self, message):
        return consent.encode(self.private.sign(message, ec.ECDSA(hashes.SHA256())))

    async def enroll(self, confirm=True):
        code = (await self.call('consent_key.enroll_code', self.local))['code']
        prepared = await self.call('consent_key.prepare', code=code)
        signature = self.sign(consent.enrollment_bytes(prepared['code_hash'], self.cid, self.spki, prepared['nonce']))
        key = await self.call('consent_key.enroll', code=code, spki=self.spki, signature=signature)
        self.key_id = key['key_id']
        if confirm:
            await self.call('consent_key.confirm', self.local, fingerprint=key['fingerprint'])
        return code, key

    async def request(self, auth=None):
        reply = await self.call('consent.request', auth, action='lifecycle.designate',
            target_stream_id='node-a:bart', target_generation=await self.env.gen('bart'),
            expected_revision=(await self.env.grant())['revision'], reason='Approve this exact manager')
        return reply['challenge']

    async def approve(self, challenge, **extra):
        return await self.call('consent.approve', challenge_id=challenge['challenge_id'],
            key_id=self.key_id, signature=self.sign(consent.decode(challenge['challenge_bytes'])), **extra)


async def refused(coro, code):
    with pytest.raises(VerbError) as caught:
        await coro
    assert caught.value.code == code


def test_designation_cannot_activate_without_phone_consent(tmp_path):
    async def check(env: Env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await refused(env.lifecycle(c.auth, 'designate', target='bart'), 'consent_no_active_key')
        assert (await env.grant())['revision'] == 0
    scenario(check)


def test_transfer_disabled_even_for_authenticated_seat(tmp_path):
    async def check(env: Env):
        Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await env.open('other', role='lead')
        await refused(env.lifecycle(await env.seat('bart'), 'transfer', target='other'), 'authority_transfer_disabled')
        assert (await env.grant())['revision'] == 0
    scenario(check)


def test_enrollment_confirm_then_signed_designation_and_exact_replay(tmp_path):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        code, key = await c.enroll(confirm=False)
        await refused(c.request(), 'consent_no_active_key')
        await refused(c.call('consent_key.confirm', c.local, fingerprint='00'*32), 'consent_fingerprint_unknown')
        await c.call('consent_key.confirm', c.local, fingerprint=key['fingerprint'])
        challenge = await c.request()
        assert challenge['requester']['identity'] == f'operator:{c.cid}'
        assert (await env.grant())['revision'] == 0
        await refused(c.call('consent.approve', challenge_id=challenge['challenge_id'], key_id=c.key_id,
                             signature=consent.encode(b'forged')), 'consent_bad_signature')
        assert (await c.call('consent.status', challenge_id=challenge['challenge_id']))['challenge']['state'] == 'pending'
        signature = c.sign(consent.decode(challenge['challenge_bytes']))
        approved = await c.call('consent.approve', challenge_id=challenge['challenge_id'], key_id=c.key_id, signature=signature)
        assert approved['receipt']['consent_id'] == challenge['challenge_id']
        assert (await env.grant())['stream_id'] == 'node-a:bart'
        replay = await c.call('consent.approve', challenge_id=challenge['challenge_id'], key_id=c.key_id, signature=signature)
        assert replay['replayed'] is True
        assert (await env.grant())['revision'] == 1
        await refused(c.approve(challenge), 'consent_conflict')  # ECDSA signs a different tuple.
        await refused(c.call('consent_key.enroll', code=code, spki=c.spki, signature=signature), 'enrollment_code_used')
    scenario(check)


@pytest.mark.parametrize('verb', ['consent.approve', 'consent.deny', 'consent.cancel'])
def test_revoked_live_operator_leaves_pending_unchanged(tmp_path, verb):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        challenge = await c.request()
        c.registry.revoke(c.cid)
        await refused(c.call(verb, challenge_id=challenge['challenge_id'], key_id=c.key_id,
            signature=c.sign(consent.decode(challenge['challenge_bytes']))), 'consent_principal_invalid')
        def read(conn):
            return conn.execute('SELECT state FROM v2_consent_challenges WHERE challenge_id=?', (challenge['challenge_id'],)).fetchone()[0]
        assert await env.store.submit(read) == 'pending'
        assert (await env.grant())['revision'] == 0
    scenario(check)


def test_cross_requester_supersede_and_cancel_are_refused(tmp_path):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await env.open('requester', role='lead')
        await env.open('stranger', role='lead')
        await c.enroll()
        requester, stranger = await env.seat('requester'), await env.seat('stranger')
        first = await c.request(requester)
        other = await c.request(stranger)
        assert first['challenge_id'] == other['challenge_id']
        await refused(c.call('consent.cancel', stranger, challenge_id=first['challenge_id']), 'consent_requester_required')
        next_challenge = await c.request(requester)
        assert next_challenge['challenge_id'] != first['challenge_id']
        assert (await c.call('consent.status', challenge_id=first['challenge_id']))['challenge']['state'] == 'superseded'
    scenario(check)


@pytest.mark.parametrize('field', ['challenge_id','action','target_stream_id','target_generation',
    'expected_revision','requester','audience_hash','nonce','expires_at'])
def test_substitution_of_each_bound_field_fails_signature(tmp_path, field):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        challenge = await c.request()
        def get(conn):
            return consent.rowdict(conn, 'v2_consent_challenges', 'challenge_id', challenge['challenge_id'])
        row = await env.store.submit(get)
        if field == 'requester':
            row[field] = json.dumps({'kind': 'seat', 'identity': 'attacker', 'generation': 'fake'})
        else:
            row[field] = str(row[field]) + '-substituted'
        signature = c.sign(consent.challenge_bytes(row))
        await refused(c.call('consent.approve', challenge_id=challenge['challenge_id'], key_id=c.key_id,
                             signature=signature), 'consent_bad_signature')
        assert (await env.grant())['revision'] == 0
    scenario(check)


@pytest.mark.parametrize('cause', ['revision', 'target'])
def test_mutation_refusal_rolls_back_and_keeps_pending(tmp_path, cause):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        challenge = await c.request()
        def intervene(conn):
            if cause == 'revision':
                conn.execute('INSERT INTO v2_lifecycle_manager VALUES (1,NULL,NULL,1,?)', (time.time(),))
            else:
                conn.execute("UPDATE sessions SET role='worker' WHERE host='node-a' AND session_name='bart'")
            conn.commit()
        await env.store.submit(intervene)
        before = await env.grant()
        await refused(c.approve(challenge), 'authority_revision_conflict' if cause == 'revision' else 'authority_target_ineligible')
        assert await env.grant() == before
        assert (await c.call('consent.status', challenge_id=challenge['challenge_id']))['challenge']['state'] == 'pending'
        assert all(r['result'] != 'applied' for r in await env.audit())
    scenario(check)


def test_deny_cancel_expiry_and_revoke_key(tmp_path):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        _, key = await c.enroll()
        ch = await c.request()
        assert (await c.call('consent.deny', challenge_id=ch['challenge_id']))['challenge']['state'] == 'denied'
        await refused(c.approve(ch), 'consent_conflict')
        ch = await c.request()
        assert (await c.call('consent.cancel', challenge_id=ch['challenge_id']))['challenge']['state'] == 'cancelled'
        ch = await c.request()
        def expire(conn):
            conn.execute('UPDATE v2_consent_challenges SET expires_at=? WHERE challenge_id=?', (time.time()-1, ch['challenge_id']))
            conn.commit()
        await env.store.submit(expire)
        async with env.sessions.assistant.authority_lock:
            rows = await env.store.consent_expire(c.registry)
        assert rows[0]['consent']['state'] == 'expired'
        ch = await c.request()
        await c.call('consent_key.revoke', c.local, fingerprint=key['fingerprint'])
        await refused(c.approve(ch), 'consent_audience_required')
        assert (await c.call('consent.status', challenge_id=ch['challenge_id']))['challenge']['state'] == 'pending'
        assert (await env.grant())['revision'] == 0
    scenario(check)


@pytest.mark.parametrize('bad_actor', ['web', 'seat', 'other_mobile'])
def test_non_audience_approval_and_deny_refused(tmp_path, bad_actor):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await env.open('seat', role='lead')
        await c.enroll()
        ch = await c.request()
        if bad_actor == 'seat':
            auth = await env.seat('seat')
        else:
            kind = 'pentacle' if bad_actor == 'web' else 'pentacle-mobile'
            cid, _ = c.registry.issue(kind, label='Non-audience client')
            auth = {'operator_authenticated': True, 'operator_principal': f'operator:{cid}', 'connection_client': kind}
        for verb in ('consent.approve', 'consent.deny'):
            await refused(c.call(verb, auth, challenge_id=ch['challenge_id'], key_id=c.key_id,
                signature=c.sign(consent.decode(ch['challenge_bytes']))), 'consent_audience_required')
        assert (await c.call('consent.status', challenge_id=ch['challenge_id']))['challenge']['state'] == 'pending'
    scenario(check)


def test_emergency_revoke_is_reduce_only_and_audits_claims(tmp_path):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        ch = await c.request()
        await c.approve(ch)
        for auth in ({'peer_loopback': False, 'local_admin_verified': True}, {'peer_loopback': True}):
            await refused(env.lifecycle(auth, 'revoke', request_id='emergency', emergency=True), 'emergency_local_only')
        await refused(env.lifecycle(c.local, 'designate', target='bart', emergency=True), 'emergency_local_only')
        receipt = await env.lifecycle(c.local, 'revoke', request_id='emergency', emergency=True,
            caller_claims={'os_user': 'claimed', 'pid': 123, 'executable': 'agent-orch'}, reason='Lost phone')
        assert receipt['receipt']['holder_stream_id'] is None
        audit = (await env.audit())[-1]
        assert audit['action'] == 'emergency_revoke'
        assert json.loads(audit['caller_claims'])['pid'] == '123'
    scenario(check)


def test_file_backed_local_admin_verifies_real_socket_emergency_revoke(tmp_path, monkeypatch):
    """With-input recovery rehearsal: actual file verifier and wire admission."""
    import asyncio
    import stat
    import local_admin
    from websockets.asyncio.client import connect

    token_path = tmp_path / 'local-admin.token'
    local_admin.initialize(token_path)
    token = local_admin.read(token_path)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    # Change only the input location; retain the actual verifier and file checks.
    monkeypatch.setattr(local_admin.verify, '__defaults__', (token_path,))

    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        await c.approve(await c.request())
        env.server.port = 0
        await env.server.bind()
        port = env.server._ws_server.sockets[0].getsockname()[1]
        try:
            async with connect(f'ws://127.0.0.1:{port}') as ws:
                welcome = json.loads(await ws.recv())
                key = operator_auth.decode_b64url(c.registry.load().credentials[c.cid]['proof_key'])
                proof = operator_auth.make_proof(key, welcome['auth']['operator']['nonce'], c.cid, 'pentacle-mobile')
                await ws.send(json.dumps({'type': 'hello', 'client': 'pentacle-mobile', 'auth_v2': {
                    'scheme': operator_auth.AUTH_SCHEME, 'credential_id': c.cid, 'proof': proof},
                    'subscribe': {'mode': 'rpc', 'snapshot': False}}))
                assert json.loads(await ws.recv())['type'] == 'ready'

                async def revoke(request_id, **fields):
                    await ws.send(json.dumps({'type': 'assistant.lifecycle', 'request_id': request_id,
                        'action': 'revoke', 'emergency': True, 'expected_revision': 1,
                        'reason': 'Disposable recovery rehearsal', **fields}))
                    while True:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
                        if reply.get('request_id') == request_id:
                            return reply

                assert (await revoke('missing-file-token'))['error_code'] == 'emergency_local_only'
                assert (await revoke('wrong-file-token', local_admin_token='0'*64))['error_code'] == 'emergency_local_only'
                token_path.chmod(0o644)
                assert (await revoke('unsafe-file-token', local_admin_token=token))['error_code'] == 'emergency_local_only'
                assert (await env.grant())['revision'] == 1
                token_path.chmod(0o600)
                result = await revoke('valid-file-token', local_admin_token=token,
                    caller_claims={'os_user': 'fixture', 'pid': 123, 'executable': 'agent-orch'})
                assert result['type'] == 'assistant.lifecycle.ok'
                assert result['receipt']['holder_stream_id'] is None
                assert (await env.grant())['revision'] == 2
                assert (await env.audit())[-1]['action'] == 'emergency_revoke'
        finally:
            await env.server.close()

    try:
        scenario(check)
    finally:
        token_path.unlink()
    assert not token_path.exists()


def test_concurrent_approvals_and_cancel_linearize_under_authority_lock(tmp_path):
    import asyncio
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        ch = await c.request()
        results = await asyncio.gather(c.approve(ch), c.call('consent.cancel', challenge_id=ch['challenge_id']), return_exceptions=True)
        assert (await env.grant())['revision'] == 1
        assert results[0]['receipt']['revision'] == 1
        assert isinstance(results[1], VerbError) and results[1].code == 'consent_conflict'
    scenario(check)


@pytest.mark.parametrize('interval', ['before_snapshot', 'snapshot_to_commit', 'after_commit'])
def test_credential_revocation_linearization_intervals(tmp_path, monkeypatch, interval):
    import asyncio
    import threading
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        ch = await c.request()
        if interval == 'before_snapshot':
            c.registry.revoke(c.cid)
            await refused(c.approve(ch), 'consent_principal_invalid')
            assert (await env.grant())['revision'] == 0
            return
        if interval == 'snapshot_to_commit':
            entered, release = threading.Event(), threading.Event()
            original = consent.transition
            def barrier(*args, **kwargs):
                if args[1] == 'consent.approve':
                    entered.set()
                    assert release.wait(5)
                return original(*args, **kwargs)
            monkeypatch.setattr(consent, 'transition', barrier)
            task = asyncio.create_task(c.approve(ch))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                await asyncio.to_thread(c.registry.revoke, c.cid)
            finally:
                release.set()
            result = await task
        else:
            result = await c.approve(ch)
            c.registry.revoke(c.cid)
        assert result['receipt']['revision'] == 1
        assert (await env.grant())['revision'] == 1
        await refused(c.call('consent.cancel', challenge_id=ch['challenge_id']), 'consent_principal_invalid')
    scenario(check)


def test_cancelled_approval_holds_authority_lock_until_worker_commit(tmp_path, monkeypatch):
    import asyncio
    import threading
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        ch = await c.request()
        entered, release = threading.Event(), threading.Event()
        original = consent.transition
        def barrier(*args, **kwargs):
            if args[1] == 'consent.approve':
                entered.set()
                assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(consent, 'transition', barrier)
        task = asyncio.create_task(c.approve(ch))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            # A later task must not enter while the committing worker is paused.
            entered_lock = asyncio.Event()
            async def contend():
                async with env.sessions.assistant.authority_lock:
                    entered_lock.set()
            contender = asyncio.create_task(contend())
            await asyncio.sleep(0.02)
            assert not entered_lock.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if 'contender' in locals():
                await contender
        assert (await env.grant())['revision'] == 1
    scenario(check)


@pytest.mark.parametrize('condition', ['expired','wrong_purpose','web','seat'])
def test_enrollment_admission_negative_cells(tmp_path, condition):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('seat', role='lead')
        code = (await c.call('consent_key.enroll_code', c.local))['code']
        auth = c.auth
        if condition == 'web':
            cid, _ = c.registry.issue('pentacle', label='Web fixture')
            auth = {'operator_authenticated': True, 'operator_principal': f'operator:{cid}', 'connection_client': 'pentacle'}
        elif condition == 'seat':
            auth = await env.seat('seat')
        else:
            def invalidate(conn):
                if condition == 'expired':
                    conn.execute('UPDATE v2_consent_enrollment_codes SET expires_at=0')
                else:
                    conn.execute("UPDATE v2_consent_enrollment_codes SET purpose='other'")
                conn.commit()
            await env.store.submit(invalidate)
        await refused(c.call('consent_key.prepare', auth, code=code), 'consent_mobile_required' if condition in {'web','seat'} else 'enrollment_code_invalid')
    scenario(check)


def test_rotation_keeps_prior_active_until_confirmation(tmp_path):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        _, old = await c.enroll()
        old_private, old_id = c.private, c.key_id
        c.private = ec.generate_private_key(ec.SECP256R1())
        c.spki = consent.encode(c.private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
        _, new = await c.enroll(confirm=False)
        ch = await c.request()
        assert ch['audience_key_ids'] == [old_id]
        await c.call('consent_key.confirm', c.local, fingerprint=new['fingerprint'])
        await refused(c.call('consent.approve', challenge_id=ch['challenge_id'], key_id=old_id,
            signature=consent.encode(old_private.sign(consent.decode(ch['challenge_bytes']), ec.ECDSA(hashes.SHA256())))), 'consent_audience_required')
        ch = await c.request()
        assert ch['audience_key_ids'] == [new['key_id']]
        await c.approve(ch)
        assert (await env.grant())['revision'] == 1
    scenario(check)


@pytest.mark.parametrize('changed', ['revoke','replace'])
def test_handoff_after_revocation_or_replacement_carries_nothing(tmp_path, monkeypatch, changed):
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    async def check(env):
        c = Ceremony(env, tmp_path)
        for name in ('bart','successor','replacement'):
            await env.open(name, role='assistant')
        await c.enroll()
        await c.approve(await c.request())
        source_generation = await env.gen('bart')
        action = 'lifecycle.revoke' if changed == 'revoke' else 'lifecycle.designate'
        ch = (await c.call('consent.request', action=action, target_stream_id='node-a:replacement',
            target_generation=await env.gen('replacement'), expected_revision=1, reason='Change manager'))['challenge']
        await c.approve(ch)
        before = await env.grant()
        # All three admitted journeys converge at the same commit-time carry.
        async with env.sessions.assistant.authority_lock:
            result = await env.store.lifecycle_authority_carry_on_handoff('node-a:bart', source_generation,
                'node-a:successor', await env.gen('successor'), 'assistant')
        assert result is None
        assert await env.grant() == before
    scenario(check)


@pytest.mark.parametrize('mode', ['live','operator','scheduled'])
def test_real_handoff_finish_emits_informational_continuity_card(tmp_path, monkeypatch, mode):
    from spawnctl import SpawnCtl
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='assistant')
        await env.open('successor', role='assistant')
        await c.enroll()
        await c.approve(await c.request())
        ctl = SpawnCtl(env.store, env.sessions, tmux=env.tmux)
        records = []
        class Notify:
            async def notification(self, msg):
                records.append(msg)
        ctl.consent_notify = Notify()
        auth = (await env.seat('bart')) if mode == 'live' else c.auth if mode == 'operator' else {'service_authenticated': True, 'service_actor': 'daemon:scheduler'}
        await ctl._finish_handoff({'handoff_from_stream_id': 'node-a:bart', 'reparent_children': False,
            '_auth_context': auth}, 'node-a:successor')
        assert (await env.grant())['stream_id'] == 'node-a:successor'
        assert len(records) == 1 and records[0]['actions'] == []
        assert records[0]['body'] == 'Lifecycle manager continued to node-a:successor (' + ('scheduled' if mode == 'scheduled' else 'live') + ')'
    scenario(check)


def test_real_socket_signed_designation_and_revoked_live_deny(tmp_path):
    import asyncio
    from websockets.asyncio.client import connect
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        env.server.port = 0
        await env.server.bind()
        port = env.server._ws_server.sockets[0].getsockname()[1]
        try:
            async with connect(f'ws://127.0.0.1:{port}') as ws:
                welcome = json.loads(await ws.recv())
                key = operator_auth.decode_b64url(c.registry.load().credentials[c.cid]['proof_key'])
                proof = operator_auth.make_proof(key, welcome['auth']['operator']['nonce'], c.cid, 'pentacle-mobile')
                await ws.send(json.dumps({'type': 'hello', 'client': 'pentacle-mobile', 'auth_v2': {
                    'scheme': operator_auth.AUTH_SCHEME, 'credential_id': c.cid, 'proof': proof},
                    'subscribe': {'mode': 'rpc', 'snapshot': False}}))
                assert json.loads(await ws.recv())['type'] == 'ready'
                async def rpc(verb, **fields):
                    rid = 'wire-' + verb
                    await ws.send(json.dumps({'type': verb, 'request_id': rid, **fields}))
                    while True:
                        reply = json.loads(await asyncio.wait_for(ws.recv(), 3))
                        if reply.get('request_id') == rid:
                            return reply
                pending = await rpc('assistant.lifecycle', action='designate', target_stream_id='node-a:bart',
                    target_generation=await env.gen('bart'), expected_revision=0, reason='Real wire rehearsal')
                assert pending['type'] == 'assistant.lifecycle.ok' and pending['code'] == 'consent_pending'
                assert (await env.grant())['revision'] == 0
                ch = pending['challenge']
                signature = c.sign(consent.decode(ch['challenge_bytes']))
                approved = await rpc('consent.approve', challenge_id=ch['challenge_id'], key_id=c.key_id, signature=signature)
                assert approved['receipt']['consent_id'] == ch['challenge_id']
                assert (await env.grant())['revision'] == 1
                pending = await rpc('consent.request', action='lifecycle.revoke', expected_revision=1, reason='Wire revoke test')
                c.registry.revoke(c.cid)
                denied = await rpc('consent.deny', challenge_id=pending['challenge']['challenge_id'])
                assert denied['error_code'] == 'consent_principal_invalid'
                assert (await env.grant())['revision'] == 1
        finally:
            await env.server.close()
    scenario(check)


def test_two_audience_keys_can_only_commit_one_approval(tmp_path):
    import asyncio
    async def check(env):
        a = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await a.enroll()
        b = Ceremony(env, tmp_path)
        await b.enroll()
        ch = await a.request()
        assert set(ch['audience_key_ids']) == {a.key_id, b.key_id}
        results = await asyncio.gather(a.approve(ch), b.approve(ch), return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 1
        failure = next(r for r in results if isinstance(r, VerbError))
        assert failure.code == 'consent_conflict'
        assert (await env.grant())['revision'] == 1
    scenario(check)


def test_key_revoke_waits_for_approved_commit_and_never_clears_grant(tmp_path, monkeypatch):
    import asyncio
    import threading
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        _, key = await c.enroll()
        ch = await c.request()
        entered, release = threading.Event(), threading.Event()
        original = consent.transition
        def barrier(*args, **kwargs):
            if args[1] == 'consent.approve':
                entered.set()
                assert release.wait(5)
            return original(*args, **kwargs)
        monkeypatch.setattr(consent, 'transition', barrier)
        approving = asyncio.create_task(c.approve(ch))
        revoking = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            revoking = asyncio.create_task(c.call('consent_key.revoke', c.local, fingerprint=key['fingerprint']))
            await asyncio.sleep(0.02)
            assert not revoking.done()
        finally:
            release.set()
            await asyncio.gather(approving, *([revoking] if revoking else []))
        assert (await env.grant())['revision'] == 1
        await refused(c.request(), 'consent_no_active_key')
    scenario(check)
