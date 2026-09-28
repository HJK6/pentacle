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
        peer = type('Peer', (), {'remote_address': ('127.0.0.1', 1)})()
        self.env.server._connection_trust[peer] = operator_auth.ConnectionTrust('v2', self.cid, 'pentacle-mobile')
        self.env.server._client_identities[peer] = 'pentacle-mobile'
        await self.env.store.consent_operation('consent.client_support', {'capabilities': {
            'consent_enrollment_offer_v1': True, 'consent_open_v1': True}}, self.auth, self.registry, self.env.sessions.assistant.role)
        offer = (await self.call('consent_key.offer', self.local, credential_id=self.cid, offer_request_id=str(time.time_ns())))['offer']
        opened = (await self.call('consent_key.open', offer_id=offer['offer_id']))['offer']
        if not confirm:
            return opened, None
        result = await self.call('consent_key.accept', offer_id=offer['offer_id'], challenge_id=opened['challenge_id'],
            spki=self.spki, signature=self.sign(consent.offer_bytes(opened, self.spki)))
        self.key_id = result['receipt']['key_id']
        keys = (await self.call('consent_key.list', self.local))['keys']
        return opened, next(key for key in keys if key['key_id'] == self.key_id)

    async def request(self, auth=None):
        reply = await self.call('consent.request', auth, action='lifecycle.designate',
            target_stream_id='node-a:bart', target_generation=await self.env.gen('bart'),
            expected_revision=(await self.env.grant())['revision'], reason='Approve this exact manager')
        if reply.get('code') == 'consent_pending_exists':
            prior = await self.env.store.submit(lambda conn: conn.execute(
                'SELECT challenge_id FROM v2_consent_intent_challenges WHERE request_id=? AND key_id=? ORDER BY rowid DESC LIMIT 1',
                (reply['intent']['request_id'], self.key_id)).fetchone())
            return (await self.call('consent.status', challenge_id=prior[0]))['challenge']
        return (await self.call('consent.open', intent_id=reply['intent']['request_id'], key_id=self.key_id))['challenge']

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


def test_host_offer_then_signed_designation_and_exact_replay(tmp_path):
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        offer, key = await c.enroll()
        await refused(c.call('consent_key.confirm', c.local, fingerprint='00'*32), 'unsupported_verb')
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
        await refused(c.call('consent_key.enroll', code='retired', spki=c.spki, signature=signature), 'unsupported_verb')
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
        audit = await env.store.submit(lambda conn: conn.execute(
            "SELECT result,refusal_code FROM v2_consent_audit WHERE verb='consent.request' ORDER BY id DESC LIMIT 1").fetchone())
        assert tuple(audit) == ('refused', 'consent_pending_exists')
        await refused(c.call('consent.cancel', stranger, challenge_id=first['challenge_id']), 'consent_audience_required')
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
        assert (await c.call('consent.deny', challenge_id=ch['challenge_id']))['intent']['state'] == 'denied'
        await refused(c.approve(ch), 'consent_conflict')
        ch = await c.request()
        assert (await c.call('consent.cancel', challenge_id=ch['challenge_id']))['intent']['state'] == 'cancelled'
        ch = await c.request()
        def expire(conn):
            rid=conn.execute('SELECT request_id FROM v2_consent_intent_challenges WHERE challenge_id=?',(ch['challenge_id'],)).fetchone()[0]
            import store_consent_intents as intents
            row=intents.load(conn,rid);row['expires_at']=time.time()-1;intents.save(conn,row)
            conn.commit()
        await env.store.submit(expire)
        async with env.sessions.assistant.authority_lock:
            rows = await env.store.consent_expire(c.registry)
        assert rows[0]['consent']['state'] == 'expired'
        ch = await c.request()
        await c.call('consent_key.revoke', c.local, fingerprint=key['fingerprint'])
        await refused(c.approve(ch), 'consent_key_invalid')
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
                signature=c.sign(consent.decode(ch['challenge_bytes']))), 'consent_mobile_required' if bad_actor == 'web' else 'consent_audience_required')
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
                    'capabilities': {'consent_enrollment_offer_v1':True,'consent_open_v1':True}, 'capabilities': {'consent_enrollment_offer_v1': True, 'consent_open_v1': True},
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


@pytest.mark.parametrize('verb', ['enroll_code','prepare','enroll','confirm'])
def test_retired_bootstrap_is_unsupported(tmp_path, verb):
    async def check(env):
        c=Ceremony(env,tmp_path)
        await refused(c.call('consent_key.'+verb,c.local,code='retired'), 'unsupported_verb')
    scenario(check)


def test_rotation_keeps_prior_active_until_offer_acceptance(tmp_path):
    async def check(env):
        c=Ceremony(env,tmp_path);await env.open('bart',role='lead')
        _,old=await c.enroll();old_private,old_id=c.private,c.key_id
        c.private=ec.generate_private_key(ec.SECP256R1())
        c.spki=consent.encode(c.private.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo))
        offer,_=await c.enroll(confirm=False)
        ch=await c.request();assert ch['audience_key_ids']==[old_id]
        accepted=await c.call('consent_key.accept',offer_id=offer['offer_id'],challenge_id=offer['challenge_id'],spki=c.spki,signature=c.sign(consent.offer_bytes(offer,c.spki)))
        c.key_id=accepted['receipt']['key_id']
        await refused(c.call('consent.approve',challenge_id=ch['challenge_id'],key_id=old_id,
            signature=consent.encode(old_private.sign(consent.decode(ch['challenge_bytes']),ec.ECDSA(hashes.SHA256())))), 'consent_key_invalid')
        ch=await c.request();assert ch['audience_key_ids']==[c.key_id]
        await c.approve(ch);assert (await env.grant())['revision']==1
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
            target_generation=await env.gen('replacement'), expected_revision=1, reason='Change manager'))['intent']
        ch = (await c.call('consent.open', intent_id=ch['request_id'], key_id=c.key_id))['challenge']
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
            async def transfer_questions_for_handoff(self, source, successor):
                return 0
            async def notification(self, msg):
                records.append(msg)
        ctl.consent_notify = Notify()
        auth = (await env.seat('bart')) if mode == 'live' else c.auth if mode == 'operator' else {
            **(await env.seat('bart')), 'service_authenticated': True, 'service_actor': 'daemon:scheduler'}
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
                    'capabilities': {'consent_enrollment_offer_v1': True, 'consent_open_v1': True},
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
                ch = (await rpc('consent.open',intent_id=pending['intent']['request_id'],key_id=c.key_id))['challenge']
                signature = c.sign(consent.decode(ch['challenge_bytes']))
                approved = await rpc('consent.approve', challenge_id=ch['challenge_id'], key_id=c.key_id, signature=signature)
                assert approved['receipt']['consent_id'] == ch['challenge_id']
                # The phone's receipt predicate needs both the signer and receipt
                # from the actual stored view, rather than a test-only reply.
                assert approved['challenge']['state'] == 'approved'
                assert approved['challenge']['approved_by_key_id'] == c.key_id
                assert approved['challenge']['receipt']['consent_id'] == ch['challenge_id']
                assert (await env.grant())['revision'] == 1
                pending = await rpc('consent.request', action='lifecycle.revoke', expected_revision=1, reason='Wire revoke test')
                ch = (await rpc('consent.open', intent_id=pending['intent']['request_id'], key_id=c.key_id))['challenge']
                c.registry.revoke(c.cid)
                denied = await rpc('consent.deny', challenge_id=ch['challenge_id'])
                assert denied['error_code'] == 'consent_principal_invalid'
                assert (await env.grant())['revision'] == 1
        finally:
            await env.server.close()
    scenario(check)


@pytest.mark.parametrize('binding', ['stale', 'legacy'])
def test_scheduled_handoff_never_carries_unbound_or_prior_generation(tmp_path, monkeypatch, binding):
    from spawnctl import SpawnCtl
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='assistant')
        admitted_generation = await env.gen('bart')
        await env.sessions.close('node-a', 'bart', close_kind='handed_off', reason='End G1')
        await env.open('bart', role='assistant')
        assert await env.gen('bart') != admitted_generation
        await env.open('successor', role='assistant')
        await c.enroll()
        await c.approve(await c.request())
        before = await env.grant()
        auth = {'service_authenticated': True, 'service_actor': 'daemon:scheduler',
                'token_verified': True, 'stream_id': 'node-a:bart'}
        if binding == 'stale':
            auth['session_generation'] = admitted_generation
        ctl = SpawnCtl(env.store, env.sessions, tmux=env.tmux)
        await env.open('child', parent_stream_id='node-a:bart')
        killed = list(env.tmux.killed)
        await refused(ctl._finish_handoff({'handoff_from_stream_id': 'node-a:bart',
            'reparent_children': True, '_auth_context': auth}, 'node-a:successor'),
            'stale_owner_generation')
        assert await env.grant() == before
        assert (await env.store.fetch_session('node-a', 'bart'))['status'] == 'open'
        assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == 'node-a:bart'
        assert env.tmux.killed == killed
    scenario(check)


@pytest.mark.parametrize('binding', ['current', 'stale', 'missing'])
def test_real_schedule_admission_dispatch_and_carry_generation_boundary(tmp_path, monkeypatch, binding):
    import uuid
    from spawnctl import SpawnCtl
    from window_schedule import WindowSchedule
    from test_window_schedule_contract import FakeSpawn, future_time, SPEC
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    async def check(env):
        c = Ceremony(env, tmp_path)
        fields = {'role': 'assistant', 'spec_ids': [SPEC], 'spec_binding_provenance': [{
            'spec_id': SPEC, 'provenance': 'spawn_explicit', 'granting_principal': 'operator:fixture',
            'granted_at': '2026-09-28T00:00:00Z'}]}
        await env.open('bart', **fields)
        admitted_generation = await env.gen('bart')
        await c.enroll()
        await c.approve(await c.request())
        ctl = SpawnCtl(env.store, env.sessions, tmux=env.tmux)
        native = FakeSpawn()
        async def finish(msg, _host):
            native.calls.append(msg)
            await env.open('successor', role='assistant')
            await ctl._finish_handoff(msg, 'node-a:successor')
            return {'type': 'spawn.ok', 'stream_id': 'node-a:successor'}
        native.spawn = finish
        surface = WindowSchedule(env.store, env.sessions, None, native, local_host='node-a')
        surface.mark_store_ready()
        inserted = await surface.schedule_insert({'type': 'schedule.insert', 'request_id': str(uuid.uuid4()),
            'from_stream_id': 'node-a:bart', '_auth_context': await env.seat('bart'),
            'handoff': True, 'handoff_from_stream_id': 'node-a:bart', 'role': 'assistant',
            'spec_ids': [SPEC], 'objective': 'Continue the approved manager',
            'fires_at_utc': future_time(), 'reparent_children': True})
        if binding == 'stale':
            await env.sessions.close('node-a', 'bart', close_kind='handed_off', reason='End G1')
            await env.open('bart', **fields)
            await c.approve(await c.request())  # independent phone grant for G2
        if binding == 'missing':
            await env.store.submit(lambda conn: (conn.execute(
                "UPDATE v2_operation_receipts SET measured_state_json='{}' WHERE request_id=? AND phase='row_committed'",
                (inserted['schedule']['request_id'],)), conn.commit()))
        await env.open('child', parent_stream_id='node-a:bart')
        before = await env.grant()
        killed = list(env.tmux.killed)
        sid = inserted['schedule']['schedule_id']
        if binding == 'current':
            await surface._fire_schedule(sid)
        else:
            await refused(surface._fire_schedule(sid), 'failed')
        after = await env.grant()
        if binding != 'current':
            assert native.calls == []
            assert await env.store.fetch_session('node-a', 'successor') is None
            assert (await env.store.fetch_session('node-a', 'bart'))['status'] == 'open'
            assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == 'node-a:bart'
            assert env.tmux.killed == killed
            assert after == before
            row = await env.store.submit(lambda conn: dict(conn.execute(
                'SELECT * FROM v2_schedules WHERE schedule_id=?', (sid,)).fetchone()))
            assert row['state'] == 'failed' and row['last_error_code'] == 'stale_owner_generation'
        else:
            assert native.calls[0]['_auth_context']['session_generation'] == admitted_generation
            assert after['stream_id'] == 'node-a:successor'
            assert after['revision'] == before['revision'] + 1
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
        rid=await env.store.submit(lambda conn:conn.execute('SELECT request_id FROM v2_consent_intent_challenges WHERE challenge_id=?',(ch['challenge_id'],)).fetchone()[0])
        other=(await b.call('consent.open',intent_id=rid,key_id=b.key_id))['challenge']
        results = await asyncio.gather(a.approve(ch), b.approve(other), return_exceptions=True)
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


@pytest.mark.parametrize('first', ['approve', 'supersede'])
def test_approve_vs_authorized_supersede_barrier(tmp_path, monkeypatch, first):
    import asyncio
    import threading
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        ch = await c.request()
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        original_transition = consent.transition
        first_verb = 'consent.approve' if first == 'approve' else 'consent.request'
        def barrier(*args, **kwargs):
            result = original_transition(*args, **kwargs)
            if args[1] == first_verb:
                entered.set()
                assert release.wait(5)
            return result
        monkeypatch.setattr(consent, 'transition', barrier)
        original_operation = env.store.consent_operation
        async def observe(verb, *args, **kwargs):
            if verb != first_verb:
                second_entered.set()
            return await original_operation(verb, *args, **kwargs)
        monkeypatch.setattr(env.store, 'consent_operation', observe)
        async def supersede():
            return await c.call('consent.request', action='lifecycle.designate',
                target_stream_id=ch['target_stream_id'], target_generation=ch['target_generation'],
                expected_revision=ch['expected_revision'], reason='Authorized replacement')
        first_task = asyncio.create_task(c.approve(ch) if first == 'approve' else supersede())
        second_task = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second_task = asyncio.create_task(supersede() if first == 'approve' else c.approve(ch))
            await asyncio.sleep(0.02)
            assert not second_entered.is_set()  # blocked at authority_lock, not merely Store's queue
            assert not second_task.done()
        finally:
            release.set()
            results = await asyncio.gather(first_task, *([second_task] if second_task else []), return_exceptions=True)
        assert isinstance(results[0], dict)
        assert isinstance(results[1], VerbError)
        assert results[1].code == ('authority_revision_conflict' if first == 'approve' else 'consent_conflict')
        status = await c.call('consent.status', challenge_id=ch['challenge_id'])
        assert status['challenge']['state'] == ('approved' if first == 'approve' else 'superseded')
        assert (await env.grant())['revision'] == (1 if first == 'approve' else 0)
    scenario(check)


@pytest.mark.parametrize('first', ['approve', 'expiry'])
def test_approve_vs_expiry_barrier(tmp_path, monkeypatch, first):
    import asyncio
    import threading
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='lead')
        await c.enroll()
        ch = await c.request()
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()
        original_transition, original_load = consent.transition, c.registry.load
        if first == 'approve':
            def barrier(*args, **kwargs):
                result = original_transition(*args, **kwargs)
                if args[1] == 'consent.approve':
                    entered.set()
                    assert release.wait(5)
                return result
            monkeypatch.setattr(consent, 'transition', barrier)
        else:
            def barrier_load(*args, **kwargs):
                result = original_load(*args, **kwargs)
                entered.set()
                assert release.wait(5)
                return result
            monkeypatch.setattr(c.registry, 'load', barrier_load)
            monkeypatch.setattr(consent.time, 'time', lambda: ch['expires_at'] + 1)
        original_operation, original_expire = env.store.consent_operation, env.store.consent_expire
        async def observe_operation(*args, **kwargs):
            if first == 'expiry':
                second_entered.set()
            return await original_operation(*args, **kwargs)
        async def observe_expire(*args, **kwargs):
            if first == 'approve':
                second_entered.set()
            return await original_expire(*args, **kwargs)
        monkeypatch.setattr(env.store, 'consent_operation', observe_operation)
        monkeypatch.setattr(env.store, 'consent_expire', observe_expire)
        async def expire():
            # Same lock and Store operation as the daemon expiry loop.
            async with env.sessions.assistant.authority_lock:
                return await env.store.consent_expire(c.registry)
        first_task = asyncio.create_task(c.approve(ch) if first == 'approve' else expire())
        second_task = None
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            if first == 'approve':
                # Approval has verified and mutated but has not committed yet.
                monkeypatch.setattr(consent.time, 'time', lambda: ch['expires_at'] + 1)
            second_task = asyncio.create_task(expire() if first == 'approve' else c.approve(ch))
            await asyncio.sleep(0.02)
            assert not second_entered.is_set()
            assert not second_task.done()
        finally:
            release.set()
            results = await asyncio.gather(first_task, *([second_task] if second_task else []), return_exceptions=True)
        if first == 'approve':
            assert isinstance(results[0], dict) and results[1] == []
        else:
            assert results[0] == []  # signing expiry does not terminalize the durable parent
            assert isinstance(results[1], VerbError) and results[1].code == 'consent_conflict'
        status = await c.call('consent.status', challenge_id=ch['challenge_id'])
        assert status['challenge']['state'] == ('approved' if first == 'approve' else 'expired')
        assert (await env.grant())['revision'] == (1 if first == 'approve' else 0)
    scenario(check)


@pytest.mark.parametrize('kind,seat,initialized,expected', [
    ('pentacle-mobile', False, True, True),
    ('pentacle', False, True, False),
    (None, False, True, False),
    ('pentacle-mobile', True, True, False),
    (None, True, True, False),
    ('pentacle-mobile', False, False, False),
])
def test_enrollment_readiness_is_current_mobile_operator_only(tmp_path, monkeypatch, kind, seat, initialized, expected):
    import asyncio
    import local_admin

    async def check(env):
        c = Ceremony(env, tmp_path)
        token_path = tmp_path / 'readiness.token'
        monkeypatch.setattr(local_admin.initialize, '__defaults__', (token_path,))
        task = await env.server.start_consent() if initialized else None
        peer = type('LoopbackPeer', (), {'remote_address': ('127.0.0.1', 1)})()
        if kind:
            env.server._client_identities[peer] = kind
            env.server._connection_trust[peer] = operator_auth.ConnectionTrust('v2', c.cid, kind)
        if seat:
            env.server._client_authenticated_streams[peer] = 'node-a:seat'
        try:
            frames = await env.server._on_hello({'client': kind or 'agent-orch', '_client_websocket': peer,
                '_auth_context': {'token_verified': seat}, 'subscribe': {'snapshot': True}})
            snapshot = next(frame for frame in frames if frame['type'] == 'snapshot')
            assert snapshot['capabilities'].get('consent_enrollment_offer_v1') is (True if expected else None)
            assert snapshot['capabilities']['close_expected_generation'] is True
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    scenario(check)


def test_failed_consent_initialization_never_advertises_readiness(tmp_path, monkeypatch):
    import asyncio
    import local_admin

    async def check(env):
        c = Ceremony(env, tmp_path)
        bad_path = tmp_path / 'unsafe.token'
        bad_path.write_text('0' * 64)
        bad_path.chmod(0o644)
        monkeypatch.setattr(local_admin.initialize, '__defaults__', (bad_path,))
        with pytest.raises(ValueError, match='unsafe local admin token'):
            await env.server.start_consent()
        peer = type('LoopbackPeer', (), {'remote_address': ('127.0.0.1', 1)})()
        env.server._client_identities[peer] = 'pentacle-mobile'
        env.server._connection_trust[peer] = operator_auth.ConnectionTrust('v2', c.cid, 'pentacle-mobile')
        frames = await env.server._on_hello({'client': 'pentacle-mobile', '_client_websocket': peer})
        snapshot = next(frame for frame in frames if frame['type'] == 'snapshot')
        assert 'consent_enrollment_offer_v1' not in snapshot['capabilities']
    scenario(check)


@pytest.mark.parametrize('change_point', ['before_spawn', 'during_spawn'])
def test_scheduled_handoff_generation_fence_barrier(tmp_path, monkeypatch, change_point):
    """The real guarded-spawn boundary owns the generation through retirement."""
    import asyncio
    from spawnctl import SpawnCtl
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='assistant')
        await env.open('child', parent_stream_id='node-a:bart')
        await c.enroll()
        await c.approve(await c.request())
        before = await env.grant()
        auth = {**await env.seat('bart'), 'service_authenticated': True,
                'service_actor': 'daemon:scheduler'}
        msg = {'handoff': True, 'handoff_from_stream_id': 'node-a:bart',
               'role': 'assistant', '_auth_context': auth, 'reparent_children': True}
        ctl = SpawnCtl(env.store, env.sessions, tmux=env.tmux)
        entered, release = asyncio.Event(), asyncio.Event()
        boots = []
        async def boot_then_finish(_msg, _host, **_kwargs):
            entered.set()
            await release.wait()
            boots.append('successor')
            await env.open('successor', role='assistant')
            await ctl._finish_handoff(_msg, 'node-a:successor')
            return {'type': 'spawn.ok', 'stream_id': 'node-a:successor'}
        monkeypatch.setattr(ctl, '_spawn_resume_guarded', boot_then_finish)
        async def reopen():
            if change_point == 'before_spawn':
                # A confirmed process death can leave children; requested close
                # now refuses them. Keep this generation-race fixture truthful.
                await env.tmux.kill_session('bart')
                await env.sessions.mark_closed('node-a', 'bart', reason='fixture process death',
                                               expected_generation=auth['session_generation'])
            else:
                await env.sessions.close('node-a', 'bart', close_kind='handed_off',
                                         expected_generation=auth['session_generation'])
            return await env.sessions.open('node-a', 'bart', role='assistant')
        if change_point == 'before_spawn':
            # FIRE's earlier read can race with a new generation. Guarded spawn
            # must re-read under the lifecycle lock before any boot effect.
            await reopen()
            killed = list(env.tmux.killed)
            release.set()
            await refused(ctl._spawn_guarded(msg, 'node-a'), 'stale_owner_generation')
            assert boots == []
            assert await env.store.fetch_session('node-a', 'successor') is None
            assert (await env.store.fetch_session('node-a', 'bart'))['status'] == 'open'
            assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == 'node-a:bart'
            assert env.tmux.killed == killed
            assert await env.grant() == before
            return
        handoff = asyncio.create_task(ctl._spawn_guarded(msg, 'node-a'))
        writer = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            writer = asyncio.create_task(reopen())
            try:
                await asyncio.wait_for(asyncio.shield(writer), .1)
            except asyncio.TimeoutError:
                pass
            assert not writer.done(), 'generation writer crossed the handoff fence'
            assert await env.gen('bart') == auth['session_generation']
            assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == 'node-a:bart'
            assert await env.grant() == before
            assert boots == []
        finally:
            release.set()
            await asyncio.gather(handoff, *([writer] if writer else []), return_exceptions=True)
        assert handoff.result()['type'] == 'spawn.ok'
        assert writer.result()['session_generation'] != auth['session_generation']
        assert (await env.store.fetch_session('node-a', 'bart'))['status'] == 'open'
        assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == 'node-a:successor'
        assert env.tmux.killed == ['bart']  # only admitted G1 was retired
        assert (await env.grant())['stream_id'] == 'node-a:successor'
        assert (await env.grant())['revision'] == before['revision'] + 1
    scenario(check)


@pytest.mark.parametrize('grant_state', ['none', 'revoked', 'replaced'])
def test_same_generation_scheduled_handoff_never_revives_old_authority(tmp_path, monkeypatch, grant_state):
    from spawnctl import SpawnCtl
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    async def check(env):
        c = Ceremony(env, tmp_path)
        await env.open('bart', role='assistant')
        await env.open('successor', role='assistant')
        if grant_state != 'none':
            await c.enroll()
            await c.approve(await c.request())
            if grant_state == 'revoked':
                pending = await c.call('consent.request', action='lifecycle.revoke',
                    expected_revision=(await env.grant())['revision'], reason='Revoke before handoff')
            else:
                await env.open('other', role='lead')
                pending = await c.call('consent.request', action='lifecycle.designate',
                    target_stream_id='node-a:other', target_generation=await env.gen('other'),
                    expected_revision=(await env.grant())['revision'], reason='Replace before handoff')
            await c.approve((await c.call('consent.open',intent_id=pending['intent']['request_id'],key_id=c.key_id))['challenge'])
        before = await env.grant()
        auth = {**await env.seat('bart'), 'service_authenticated': True,
                'service_actor': 'daemon:scheduler'}
        ctl = SpawnCtl(env.store, env.sessions, tmux=env.tmux)
        await ctl._finish_handoff({'handoff_from_stream_id': 'node-a:bart',
            '_auth_context': auth, 'reparent_children': False}, 'node-a:successor')
        assert (await env.store.fetch_session('node-a', 'bart'))['status'] == 'closed'
        assert await env.grant() == before
    scenario(check)


def test_scheduled_handoff_real_spawn_serializes_generation_through_finish(tmp_path, monkeypatch):
    import asyncio
    from spawnctl import SpawnCtl
    from test_lifecycle_authority import IdleTmux
    monkeypatch.setenv('PENTACLE_ASSISTANT_ROLE', 'assistant')
    class BootTmux(IdleTmux):
        created = 0
        async def new_session(self, name, command, cwd=None, env=None):
            self.created += 1
            self.live.add(name)
        async def capture(self, name):
            return 'READY'
        async def session_state(self, name):
            return 'alive' if name in self.live else 'gone'
    async def check(env):
        env.tmux = env.sessions.tmux = BootTmux()
        await env.open('bart', role='assistant', provider='claude',
            effective_model='claude-opus-4-8', effective_effort='high')
        await env.open('child', parent_stream_id='node-a:bart')
        c = Ceremony(env, tmp_path)
        await c.enroll()
        await c.approve(await c.request())
        auth = {**await env.seat('bart'), 'service_authenticated': True,
                'service_actor': 'daemon:scheduler'}
        ctl = SpawnCtl(env.store, env.sessions, tmux=env.tmux)
        entered, release = asyncio.Event(), asyncio.Event()
        original = ctl._spawn_fenced
        async def barrier(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        monkeypatch.setattr(ctl, '_spawn_fenced', barrier)
        msg = {'handoff': True, 'handoff_from_stream_id': 'node-a:bart', 'role': 'assistant',
            'command': 'stub', 'ready_marker': 'READY', 'request_id': 'scheduled-native-boundary',
            'objective': 'Exercise generation-bound scheduled boot', '_auth_context': auth}
        handoff = asyncio.create_task(ctl._spawn_guarded(msg, 'node-a'))
        async def reopen():
            await env.sessions.close('node-a', 'bart', close_kind='handed_off',
                expected_generation=auth['session_generation'])
            return await env.sessions.open('node-a', 'bart', role='assistant')
        writer = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            writer = asyncio.create_task(reopen())
            try:
                await asyncio.wait_for(asyncio.shield(writer), .1)
            except asyncio.TimeoutError:
                pass
            assert not writer.done(), 'generation changed between admission and native boot'
            assert env.tmux.created == 0
            assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == 'node-a:bart'
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(handoff, *([writer] if writer else []),
                return_exceptions=True), 5)
        reply = handoff.result()
        assert reply['type'] == 'spawn.ok'
        assert env.tmux.created == 1
        assert env.tmux.killed == ['bart']
        assert writer.result()['session_generation'] != auth['session_generation']
        assert (await env.store.fetch_session('node-a', 'bart'))['status'] == 'open'
        assert (await env.store.fetch_session('node-a', 'child'))['parent_stream_id'] == reply['stream_id']
        assert (await env.grant())['stream_id'] == reply['stream_id']
        assert not ctl._handoff_fences
    scenario(check)
