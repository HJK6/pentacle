"""Host offers/open-time intents: real Store, registry, cryptography and handlers."""
import asyncio
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from _shared import operator_auth
import store_consent as consent
from sessions import VerbError
from test_lifecycle_authority import scenario


class Phone:
    def __init__(self, env, registry, cid):
        self.env, self.registry, self.cid = env, registry, cid
        self.auth = {'operator_authenticated': True, 'operator_principal': 'operator:' + cid,
                     'connection_client': 'pentacle-mobile', 'transport': 'v2'}
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.spki = consent.encode(self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))

    async def call(self, verb, auth=None, **fields):
        return await self.env.server._on_consent({'type': verb, '_auth_context': auth or self.auth, **fields})

    def sign(self, value):
        return consent.encode(self.private.sign(value, ec.ECDSA(hashes.SHA256())))

    async def offer(self, **fields):
        return (await self.call('consent_key.offer', {'peer_loopback': True, 'local_admin_verified': True},
            credential_id=self.cid, offer_request_id='offer-' + self.cid, **fields))['offer']

    async def enroll(self):
        offer = await self.offer()
        assert not offer.get('nonce') and not offer.get('challenge_id')
        opened = (await self.call('consent_key.open', offer_id=offer['offer_id']))['offer']
        fields = dict(offer_id=offer['offer_id'], challenge_id=opened['challenge_id'], spki=self.spki,
                      signature=self.sign(consent.offer_bytes(opened, self.spki)))
        result = await self.call('consent_key.accept', **fields)
        self.key_id = result['receipt']['key_id']
        return opened, fields, result


async def setup(env, tmp_path):
    registry = operator_auth.OperatorCredentialRegistry(tmp_path / 'credentials.json')
    registry.initialize()
    env.server.operator_credential_registry = registry
    phones = []
    for label in ['A', 'B']:
        cid, _ = registry.issue('pentacle-mobile', label=label)
        phone = Phone(env, registry, cid)
        # The actual authenticated hello is the protocol negotiation boundary.
        peer = type('Peer', (), {'remote_address': ('127.0.0.1', 1)})()
        env.server._connection_trust[peer] = operator_auth.ConnectionTrust('v2', cid, 'pentacle-mobile')
        env.server._client_identities[peer] = 'pentacle-mobile'
        await env.server._on_hello({'client': 'pentacle-mobile', '_client_websocket': peer,
            'capabilities': {'consent_enrollment_offer_v1': True, 'consent_open_v1': True},
            'subscribe': {'mode': 'rpc', 'snapshot': False}})
        phones.append(phone)
    return phones


async def refuses(call, code):
    with pytest.raises(VerbError) as raised:
        await call
    assert raised.value.code == code


def test_host_offer_activates_and_exact_retry_does_not_repeat_effect(tmp_path):
    async def check(env):
        a, b = await setup(env, tmp_path)
        opened, fields, result = await a.enroll()
        assert result['offer']['state'] == 'accepted'
        assert result['offer']['key_state'] == 'active'
        assert (await env.grant())['revision'] == 0
        retry = await a.call('consent_key.accept', **fields)
        assert retry['replayed'] and retry['receipt'] == result['receipt']
        await refuses(b.call('consent_key.status', offer_id=opened['offer_id']), 'consent_target_required')
        await refuses(b.call('consent_key.decline', offer_id=opened['offer_id']), 'consent_target_required')
    scenario(check)


def test_offer_signature_refusal_leaves_pending_and_conflict_never_replaces_winner(tmp_path):
    async def check(env):
        a, _ = await setup(env, tmp_path)
        offer = await a.offer()
        opened = (await a.call('consent_key.open', offer_id=offer['offer_id']))['offer']
        await refuses(a.call('consent_key.accept', offer_id=offer['offer_id'], challenge_id=opened['challenge_id'],
            spki=a.spki, signature=consent.encode(b'bad')), 'consent_bad_signature')
        assert (await a.call('consent_key.status', offer_id=offer['offer_id']))['offer']['state'] == 'pending'
        fields = dict(offer_id=offer['offer_id'], challenge_id=opened['challenge_id'], spki=a.spki,
                      signature=a.sign(consent.offer_bytes(opened, a.spki)))
        accepted = await a.call('consent_key.accept', **fields)
        other = Phone(env, a.registry, a.cid)
        await refuses(a.call('consent_key.accept', **{**fields, 'spki': other.spki,
            'signature': other.sign(consent.offer_bytes(opened, other.spki))}), 'consent_conflict')
        assert (await a.call('consent_key.status', offer_id=offer['offer_id']))['offer']['accepted_key_id'] == accepted['receipt']['key_id']
    scenario(check)


@pytest.mark.parametrize('issuer', ['mobile', 'seat', 'service', 'unverified_local'])
def test_offer_issuer_is_fresh_host_operator_or_verified_loopback(tmp_path, issuer):
    async def check(env):
        a, _ = await setup(env, tmp_path)
        await env.open('seat', role='lead')
        auth = {'mobile': a.auth, 'seat': await env.seat('seat'),
                'service': {'service_authenticated': True},
                'unverified_local': {'local_admin_verified': True, 'peer_loopback': False}}[issuer]
        await refuses(a.call('consent_key.offer', auth, credential_id=a.cid, offer_request_id='bad'), 'consent_host_operator_required')
    scenario(check)


def test_durable_intent_has_no_challenge_until_explicit_open_and_one_atomic_effect(tmp_path, monkeypatch):
    async def check(env):
        a, b = await setup(env, tmp_path)
        await a.enroll(); await b.enroll()
        await env.open('bart', role='lead')
        now = time.time()
        result = await a.call('consent.request', action='lifecycle.designate', target_stream_id='node-a:bart',
            target_generation=await env.gen('bart'), expected_revision=0, reason='Synthetic manager')
        intent = result['intent']
        assert not intent.get('challenge_bytes') and not intent.get('nonce')
        assert intent['expires_at'] >= now + 86399
        monkeypatch.setattr(consent.time, 'time', lambda: now + 180)
        opened = (await a.call('consent.open', intent_id=intent['request_id'], key_id=a.key_id))['challenge']
        assert opened['expires_at'] <= now + 300
        assert (await a.call('consent.open', intent_id=intent['request_id'], key_id=a.key_id))['challenge'] == opened
        other = (await b.call('consent.open', intent_id=intent['request_id'], key_id=b.key_id))['challenge']
        await refuses(b.call('consent.approve', challenge_id=opened['challenge_id'], key_id=b.key_id,
            signature=b.sign(consent.decode(opened['challenge_bytes']))), 'consent_key_invalid')
        fields = dict(challenge_id=opened['challenge_id'], key_id=a.key_id,
                      signature=a.sign(consent.decode(opened['challenge_bytes'])))
        approved = await a.call('consent.approve', **fields)
        assert approved['receipt']['revision'] == 1
        assert (await a.call('consent.approve', **fields))['replayed']
        await refuses(b.call('consent.approve', challenge_id=other['challenge_id'], key_id=b.key_id,
            signature=b.sign(consent.decode(other['challenge_bytes']))), 'consent_conflict')
        assert (await env.grant())['revision'] == 1
    scenario(check)


def test_reopen_replaces_challenge_without_refreshing_parent(tmp_path, monkeypatch):
    async def check(env):
        a, _ = await setup(env, tmp_path)
        await a.enroll(); await env.open('bart', role='lead')
        intent = (await a.call('consent.request', action='lifecycle.designate', target_stream_id='node-a:bart',
            target_generation=await env.gen('bart'), expected_revision=0, reason='Synthetic manager'))['intent']
        first = (await a.call('consent.open', intent_id=intent['request_id'], key_id=a.key_id))['challenge']
        monkeypatch.setattr(consent.time, 'time', lambda: first['expires_at'] + 1)
        second = (await a.call('consent.open', intent_id=intent['request_id'], key_id=a.key_id))['challenge']
        assert first['challenge_id'] != second['challenge_id']
        await refuses(a.call('consent.approve', challenge_id=first['challenge_id'], key_id=a.key_id,
            signature=a.sign(consent.decode(first['challenge_bytes']))), 'consent_conflict')
        assert (await a.call('consent.status', intent_id=intent['request_id']))['intent']['expires_at'] == intent['expires_at']
    scenario(check)


def test_repeated_request_creation_is_idempotent_and_cannot_rebind(tmp_path, monkeypatch):
    async def check(env):
        a, _ = await setup(env, tmp_path)
        await a.enroll(); await env.open('bart', role='lead')
        fields = dict(request_id='same-wire-request', action='lifecycle.designate', target_stream_id='node-a:bart',
            target_generation=await env.gen('bart'), expected_revision=0, reason='Synthetic manager')
        first = await a.call('consent.request', **fields)
        monkeypatch.setattr(consent.time, 'time', lambda: first['intent']['created_at'] + 180)
        second = await a.call('consent.request', **fields)
        assert second['replayed'] and second['intent'] == first['intent']
        await refuses(a.call('consent.request', **{**fields, 'reason': 'Different action text'}), 'consent_conflict')
        count = await env.store.submit(lambda conn: conn.execute('SELECT COUNT(*) FROM v2_consent_push_jobs WHERE request_id=?', (first['intent']['request_id'],)).fetchone()[0])
        assert count == 1
    scenario(check)


def test_shared_cross_language_offer_framing_fixture():
    import pathlib
    fixture = json.loads((pathlib.Path(__file__).resolve().parents[3] / 'docs/consent-offer-transcript.json').read_text())
    assert consent.encode(consent.framed(fixture['domain'], fixture['fields'])) == fixture['base64']


@pytest.mark.parametrize('field', ['offer_id','host_id','credential_id','issuer','challenge_id','nonce','challenge_expires_at','expected_prior_key_id','spki','domain'])
def test_offer_transcript_binds_each_field_and_refusal_leaves_pending(tmp_path, field):
    async def check(env):
        a, _ = await setup(env, tmp_path)
        offer = await a.offer()
        opened = (await a.call('consent_key.open', offer_id=offer['offer_id']))['offer']
        tampered = dict(opened); presented = a.spki
        if field == 'issuer': tampered['issuer'] = {'kind':'operator','identity':'operator:other'}
        elif field == 'challenge_expires_at': tampered[field] += 1
        elif field == 'spki': presented = Phone(env, a.registry, a.cid).spki
        elif field != 'domain': tampered[field] = 'different'
        message = consent.offer_bytes(tampered, presented)
        if field == 'domain': message = message.replace(b'pentacle-consent-enroll-offer-v1', b'pentacle-consent-enroll-other-v1')
        await refuses(a.call('consent_key.accept', offer_id=offer['offer_id'],challenge_id=opened['challenge_id'],
            spki=a.spki,signature=a.sign(message)), 'consent_bad_signature')
        assert (await a.call('consent_key.status',offer_id=offer['offer_id']))['offer']['state'] == 'pending'
        assert (await env.grant())['revision'] == 0
    scenario(check)


def test_concurrent_valid_accepts_have_one_winner_and_one_deduplicated_notice(tmp_path):
    async def check(env):
        a, b = await setup(env, tmp_path)
        await b.enroll()
        offer = await a.offer()
        opened = (await a.call('consent_key.open',offer_id=offer['offer_id']))['offer']
        other = Phone(env,a.registry,a.cid)
        def reply(phone): return dict(offer_id=offer['offer_id'],challenge_id=opened['challenge_id'],spki=phone.spki,
            signature=phone.sign(consent.offer_bytes(opened,phone.spki)))
        results = await asyncio.gather(a.call('consent_key.accept',**reply(a)),a.call('consent_key.accept',**reply(other)),return_exceptions=True)
        assert sum(isinstance(result,dict) for result in results)==1
        winner=next(result for result in results if isinstance(result,dict))
        loser=other if winner['receipt']['spki_hash']==__import__('hashlib').sha256(consent.decode(a.spki)).hexdigest() else a
        await refuses(a.call('consent_key.accept',**reply(loser)),'consent_conflict')
        notices=await env.store.submit(lambda conn: conn.execute('SELECT COUNT(*) FROM v2_consent_security_notices').fetchone()[0])
        assert notices==1
        keys=await env.store.submit(lambda conn: conn.execute("SELECT credential_id,key_id FROM v2_consent_keys WHERE state='active'").fetchall())
        assert set(tuple(row) for row in keys)=={(a.cid,winner['receipt']['key_id']),(b.cid,b.key_id)}
        await a.call('consent_key.revoke',{'peer_loopback':True,'local_admin_verified':True},fingerprint=winner['receipt']['spki_hash'])
        exact=reply(a) if loser is other else reply(other)
        # ECDSA may produce a different valid signature: replay the stored exact tuple.
        exact=await env.store.submit(lambda conn: json.loads(conn.execute('SELECT data FROM v2_consent_offers WHERE offer_id=?',(offer['offer_id'],)).fetchone()[0])['response'])
        replay=await a.call('consent_key.accept',offer_id=offer['offer_id'],**exact)
        assert replay['replayed'] and replay['offer']['key_state']=='revoked'
        assert (await env.grant())['revision']==0
    scenario(check)


def test_generic_notification_store_cannot_forge_or_leak_consent_records(tmp_path):
    from notify import Notify
    async def check():
        notify=Notify(str(tmp_path/'notifications.sqlite'))
        await notify.start()
        try:
            result=await notify.notification({'type':'notification.create','producer':'consent.enrollment.v1','title':'Forged offer','request_id':'create'})
            assert result['error_code']=='notification_invalid'
            # Preserve, but never publish, historical malformed reserved records.
            row=await notify._db.call('create_notification',producer='consent.enrollment.v1',title='Historical',body=None,severity='info',dedup_key=None,actions=None,ttl_seconds=None,answer_to_stream_id=None)
            assert not await notify.snapshot_notifications()
            assert not (await notify._notif_list({'notification_ids':[row['notification_id']]},'list'))['notifications']
            assert not (await notify._notif_list({},'list'))['notifications']
        finally: await notify.stop()
    asyncio.run(check())


def test_real_socket_offer_creation_reconnect_expiry_and_terminal_are_target_only(tmp_path, monkeypatch):
    from websockets.asyncio.client import connect
    import hashlib
    async def check(env):
        a,b=await setup(env,tmp_path)
        web,_=a.registry.issue('pentacle',label='Host operator')
        await env.open('seat',role='lead')
        seat_token='synthetic-seat-token'
        assert await env.store.grant_stream_token('node-a','seat',hashlib.sha256(seat_token.encode()).hexdigest(),'sha256:v1')=='ok'
        env.server.port=0;await env.server.bind()
        endpoint=f'ws://127.0.0.1:{env.server._ws_server.sockets[0].getsockname()[1]}'
        clients=[]
        async def open_client(cid=None,kind='pentacle-mobile',seat=False):
            ws=await connect(endpoint);clients.append(ws)
            greeting=json.loads(await ws.recv())
            hello={'type':'hello','client':kind,'capabilities':{'consent_enrollment_offer_v1':True,'consent_open_v1':True},
                'subscribe':{'events_mode':'summary','exclude_event_types':['event.upsert','hosts.stats']}}
            if cid:
                key=operator_auth.decode_b64url(a.registry.load().credentials[cid]['proof_key'])
                hello['auth_v2']={'scheme':operator_auth.AUTH_SCHEME,'credential_id':cid,
                    'proof':operator_auth.make_proof(key,greeting['auth']['operator']['nonce'],cid,kind)}
            if seat: hello.update(stream_token=seat_token,from_stream_id='node-a:seat')
            await ws.send(json.dumps(hello))
            while True:
                frame=json.loads(await ws.recv())
                assert frame['type']!='hello.error'
                if frame['type']=='snapshot':return ws,frame
        async def notifications(ws):
            result=[]
            while True:
                try: frame=json.loads(await asyncio.wait_for(ws.recv(),.06))
                except asyncio.TimeoutError: return result
                if frame.get('type')=='notification':result.append(frame['notification'])
        try:
            wa,sa=await open_client(a.cid);wb,sb=await open_client(b.cid)
            ww,sw=await open_client(web,'pentacle');wt,st=await open_client(kind='agent-orch',seat=True)
            wu,su=await open_client(kind='pentacle')
            offer=await a.offer()
            expected='consent-offer:'+offer['offer_id']
            rows=await notifications(wa)
            assert [r['notification_id'] for r in rows]==[expected]
            assert 'nonce' not in rows[0]['consent_offer'] and rows[0]['consent_offer']['credential_id']==a.cid
            for ws in (wb,ww,wt,wu):assert not await notifications(ws)
            # The generic fanout path cannot send an unprojected consent payload.
            await env.server.broadcast({'type':'notification','notification':rows[0]})
            for ws in (wa,wb,ww,wt,wu):assert not await notifications(ws)
            wr,sr=await open_client(a.cid)
            assert [r['notification_id'] for r in sr['notifications']]==[expected]
            assert not sb['notifications'] and not sw['notifications'] and not st['notifications'] and not su['notifications']
            await wa.send(json.dumps({'type':'consent_key.decline','request_id':'decline','offer_id':offer['offer_id']}))
            terminal=await notifications(wa)
            assert terminal[0]['consent_offer']['state']=='declined'
            assert (await notifications(wr))[0]['consent_offer']['state']=='declined'
            for ws in (wb,ww,wt,wu):assert not await notifications(ws)
            second=(await a.call('consent_key.offer',{'peer_loopback':True,'local_admin_verified':True},credential_id=a.cid,offer_request_id='expiry'))['offer']
            await notifications(wa);await notifications(wr)
            monkeypatch.setattr(consent.time,'time',lambda:second['expires_at']+1)
            await env.store.consent_expire(a.registry,env.sessions.assistant.role);await env.server._publish_consent()
            assert (await notifications(wa))[0]['consent_offer']['state']=='expired'
            assert (await notifications(wr))[0]['consent_offer']['state']=='expired'
            for ws in (wb,ww,wt,wu):assert not await notifications(ws)
        finally:
            for ws in clients: await ws.close()
            await env.server.close()
    scenario(check)


@pytest.mark.parametrize('effect',['enrollment','approval'])
def test_final_audit_failure_rolls_back_the_entire_effect(tmp_path,monkeypatch,effect):
    async def check(env):
        a,_=await setup(env,tmp_path)
        await a.enroll()
        if effect=='enrollment':
            offer=(await a.call('consent_key.offer',{'peer_loopback':True,'local_admin_verified':True},credential_id=a.cid,offer_request_id='replacement'))['offer']
            opened=(await a.call('consent_key.open',offer_id=offer['offer_id']))['offer']
            other=Phone(env,a.registry,a.cid)
            fields=dict(offer_id=offer['offer_id'],challenge_id=opened['challenge_id'],spki=other.spki,signature=other.sign(consent.offer_bytes(opened,other.spki)))
            verb='consent_key.accept'
        else:
            await env.open('bart',role='lead')
            intent=(await a.call('consent.request',action='lifecycle.designate',target_stream_id='node-a:bart',target_generation=await env.gen('bart'),expected_revision=0,reason='Atomic fixture'))['intent']
            ch=(await a.call('consent.open',intent_id=intent['request_id'],key_id=a.key_id))['challenge']
            fields=dict(challenge_id=ch['challenge_id'],key_id=a.key_id,signature=a.sign(consent.decode(ch['challenge_bytes'])))
            verb='consent.approve'
        original=consent.audit
        def failed(conn,called,*args,**kwargs):
            if called==verb:raise RuntimeError('synthetic final audit failure')
            return original(conn,called,*args,**kwargs)
        monkeypatch.setattr(consent,'audit',failed)
        with pytest.raises(RuntimeError,match='synthetic final audit failure'):await a.call(verb,**fields)
        assert (await env.grant())['revision']==0
        keys=await env.store.submit(lambda conn:conn.execute("SELECT key_id FROM v2_consent_keys WHERE state='active'").fetchall())
        assert [row[0] for row in keys]==[a.key_id]
        if effect=='enrollment':assert (await a.call('consent_key.status',offer_id=offer['offer_id']))['offer']['state']=='pending'
        else:assert (await a.call('consent.status',intent_id=intent['request_id']))['intent']['state']=='pending'
    scenario(check)


@pytest.mark.parametrize('changed',['issuer','target','prior_key'])
def test_offer_acceptance_revalidates_issuer_target_and_prior_key(tmp_path,changed):
    async def check(env):
        a,b=await setup(env,tmp_path);await a.enroll();await b.enroll()
        web,_=a.registry.issue('pentacle',label='Host operator')
        issuer={'operator_authenticated':True,'operator_principal':'operator:'+web,'connection_client':'pentacle'}
        offer=(await a.call('consent_key.offer',issuer,credential_id=a.cid,offer_request_id='web-replacement'))['offer']
        opened=(await a.call('consent_key.open',offer_id=offer['offer_id']))['offer']
        other=Phone(env,a.registry,a.cid)
        fields=dict(offer_id=offer['offer_id'],challenge_id=opened['challenge_id'],spki=other.spki,signature=other.sign(consent.offer_bytes(opened,other.spki)))
        if changed=='issuer':a.registry.revoke(web);expected='consent_issuer_stale'
        elif changed=='target':a.registry.revoke(a.cid);expected='consent_principal_invalid'
        else:
            key=await env.store.submit(lambda conn:consent.rowdict(conn,'v2_consent_keys','key_id',a.key_id))
            await a.call('consent_key.revoke',{'peer_loopback':True,'local_admin_verified':True},fingerprint=key['fingerprint']);expected='consent_prior_key_conflict'
        await refuses(a.call('consent_key.accept',**fields),expected)
        assert (await env.grant())['revision']==0
        assert (await b.call('consent_key.status',offer_id=(await b.offer())['offer_id']))['offer']['key_state']=='active'
        count=await env.store.submit(lambda conn:conn.execute('SELECT COUNT(*) FROM v2_consent_keys').fetchone()[0])
        assert count==2
    scenario(check)


def test_cutover_retains_active_keys_and_disables_legacy_pending_bootstrap(tmp_path):
    async def check(env):
        a,b=await setup(env,tmp_path);await a.enroll();await b.enroll()
        def cutover(conn):
            before=dict(consent.rowdict(conn,'v2_consent_keys','key_id',a.key_id))
            conn.execute("UPDATE v2_consent_keys SET state='pending_confirm' WHERE key_id=?",(b.key_id,))
            conn.execute('INSERT INTO v2_consent_enrollment_codes VALUES (?,?,?,?,?,NULL)',('synthetic-hash','consent-key','synthetic-nonce',time.time(),time.time()+600));conn.commit()
            consent.initialize(conn);conn.commit()
            assert consent.rowdict(conn,'v2_consent_keys','key_id',a.key_id)==before
            assert consent.rowdict(conn,'v2_consent_keys','key_id',b.key_id)['state']=='expired'
            assert conn.execute('SELECT purpose FROM v2_consent_enrollment_codes').fetchone()[0]=='retired'
        await env.store.submit(cutover)
        await env.open('bart',role='lead')
        intent=(await a.call('consent.request',action='lifecycle.designate',target_stream_id='node-a:bart',target_generation=await env.gen('bart'),expected_revision=0,reason='Existing-key continuity'))['intent']
        assert intent['audience_key_ids']==[a.key_id]
        child=(await a.call('consent.open',intent_id=intent['request_id'],key_id=a.key_id))['challenge']
        approved=await a.call('consent.approve',challenge_id=child['challenge_id'],key_id=a.key_id,signature=a.sign(consent.decode(child['challenge_bytes'])))
        assert approved['receipt']['revision']==1
    scenario(check)
