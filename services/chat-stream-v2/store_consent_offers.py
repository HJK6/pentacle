"""Credential-targeted enrollment offers, called in the consent transaction."""
import hashlib
import json
import secrets
import time
import uuid

import store_consent as c

DURABLE_TTL = 86400
SIGNING_TTL = 120
FEATURES = ('consent_enrollment_offer_v1', 'consent_open_v1')


def initialize(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS v2_consent_offers (
      offer_id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, state TEXT NOT NULL,
      expires_at REAL NOT NULL, data TEXT NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS ix_consent_pending_offer
      ON v2_consent_offers(credential_id) WHERE state='pending';
    CREATE TABLE IF NOT EXISTS v2_consent_clients (
      credential_id TEXT PRIMARY KEY, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v2_consent_security_notices (
      offer_id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, created_at REAL NOT NULL);
    ''')
    # Retirement is irreversible; retain historical evidence and existing active keys.
    conn.execute("UPDATE v2_consent_enrollment_codes SET purpose='retired' WHERE purpose='consent-key'")
    conn.execute("UPDATE v2_consent_keys SET state='expired' WHERE state='pending_confirm'")


def load(conn, oid):
    row = conn.execute('SELECT data FROM v2_consent_offers WHERE offer_id=?', (oid,)).fetchone()
    return json.loads(row[0]) if row else None


def save(conn, row):
    conn.execute('INSERT INTO v2_consent_offers VALUES (?,?,?,?,?) ON CONFLICT(offer_id) DO UPDATE '
        'SET state=excluded.state,data=excluded.data',
        (row['offer_id'], row['credential_id'], row['state'], row['expires_at'], json.dumps(row, sort_keys=True)))


def mobile(conn, auth, credentials):
    actor = c.principal(conn, auth, credentials)
    if actor['kind'] != 'operator' or auth.get('connection_client') != 'pentacle-mobile' or auth.get('token_verified'):
        raise c.ConsentError('consent_mobile_required')
    return actor['identity'].removeprefix('operator:')


def issuer(conn, auth, credentials):
    if auth.get('connection_client') == 'pentacle-mobile' or auth.get('token_verified') or auth.get('service_authenticated'):
        raise c.ConsentError('consent_host_operator_required')
    if (auth.get('peer_loopback') is True and auth.get('local_admin_verified') is True
            and not auth.get('token_verified') and not auth.get('service_authenticated')):
        return {'kind': 'local-admin', 'identity': 'local-admin', 'generation': ''}
    try:
        actor = c.principal(conn, auth, credentials)
    except c.ConsentError:
        raise c.ConsentError('consent_host_operator_required') from None
    if actor['kind'] != 'operator' or auth.get('connection_client') != 'pentacle' or auth.get('token_verified'):
        raise c.ConsentError('consent_host_operator_required')
    return actor


def support(conn, cid, feature):
    row = conn.execute('SELECT data FROM v2_consent_clients WHERE credential_id=?', (cid,)).fetchone()
    return bool(row and json.loads(row[0]).get(feature) is True)


def current_key(conn, cid):
    row = conn.execute("SELECT key_id FROM v2_consent_keys WHERE credential_id=? AND state='active'", (cid,)).fetchone()
    return row[0] if row else None


def key_epoch(conn, cid):
    return [r[0] for r in conn.execute('SELECT key_id FROM v2_consent_keys WHERE credential_id=? ORDER BY key_id', (cid,))]


def valid_binding(conn, row, credentials):
    target = credentials.get(row['credential_id'])
    if not target or target.get('revoked_at') or target.get('client_kind') != 'pentacle-mobile':
        raise c.ConsentError('consent_target_invalid')
    actor = row['issuer']
    if actor['kind'] == 'operator':
        record = credentials.get(actor['identity'].removeprefix('operator:'))
        if not record or record.get('revoked_at') or record.get('client_kind') != 'pentacle':
            raise c.ConsentError('consent_issuer_stale')
    if row['state'] == 'pending' and (current_key(conn, row['credential_id']) != row['expected_prior_key_id'] or key_epoch(conn, row['credential_id']) != row['expected_key_epoch']):
        raise c.ConsentError('consent_prior_key_conflict')


def view(conn, row, signing=False):
    fields = ('offer_id', 'host_id', 'credential_id', 'label', 'issuer', 'created_at',
              'expires_at', 'expected_prior_key_id', 'state', 'accepted_key_id')
    result = {k: row.get(k) for k in fields}
    if row.get('accepted_key_id'):
        key = c.rowdict(conn, 'v2_consent_keys', 'key_id', row['accepted_key_id'])
        result['key_state'] = key['state'] if key else 'unavailable'
    if signing:
        result.update({k: row.get(k) for k in ('challenge_id', 'nonce', 'challenge_expires_at')})
    return result


def offer_bytes(row, spki):
    # Expiry is an integer Unix second, encoded as decimal UTF-8 without a fraction.
    return c.framed('pentacle-consent-enroll-offer-v1', [row['offer_id'], row['host_id'],
        row['credential_id'], row['issuer']['kind'], row['issuer']['identity'], row['challenge_id'],
        row['nonce'], int(row['challenge_expires_at']), row.get('expected_prior_key_id') or '', spki])


def operation(conn, verb, msg, auth, credentials, snapshot_at):
    now = time.time()
    if verb == 'consent.client_support':
        cid = mobile(conn, auth, credentials)
        caps = msg.get('capabilities') if isinstance(msg.get('capabilities'), dict) else {}
        conn.execute('INSERT INTO v2_consent_clients VALUES (?,?) ON CONFLICT(credential_id) DO UPDATE SET data=excluded.data',
            (cid, json.dumps({k: caps.get(k) is True for k in FEATURES})))
        return {}
    if verb in {'consent_key.offer', 'consent_key.devices'}:
        actor = issuer(conn, auth, credentials)
        if verb == 'consent_key.devices':
            return {'devices': [{'credential_id': cid, 'label': r.get('label') or 'Phone',
                'supported': support(conn, cid, FEATURES[0]), 'online': cid in msg.get('_online_mobile_credentials', ())} for cid, r in credentials.items()
                if r.get('client_kind') == 'pentacle-mobile' and not r.get('revoked_at')]}
        cid = str(msg.get('credential_id') or '')
        record = credentials.get(cid)
        if not record or record.get('revoked_at') or record.get('client_kind') != 'pentacle-mobile':
            raise c.ConsentError('consent_target_invalid')
        if not support(conn, cid, FEATURES[0]):
            raise c.ConsentError('consent_update_required')
        registration = conn.execute('SELECT host_id FROM v2_consent_push_registrations WHERE credential_id=?', (cid,)).fetchone()
        if cid not in msg.get('_online_mobile_credentials', ()) and (not registration or registration[0] != msg['_daemon_host']):
            raise c.ConsentError('consent_target_offline')
        rid = str(msg.get('offer_request_id') or '')
        if not rid or len(rid) > 200:
            raise c.ConsentError('consent_request_id_required')
        request_binding = {'request_id': rid, 'credential_id': cid, 'issuer': actor}
        for raw, in conn.execute('SELECT data FROM v2_consent_offers'):
            prior = json.loads(raw)
            if prior['request_id'] == rid and prior['issuer'] == actor:
                if prior['request_binding'] != request_binding:
                    raise c.ConsentError('consent_conflict')
                return {'offer': view(conn, prior), 'replayed': True}
        pending = conn.execute("SELECT offer_id FROM v2_consent_offers WHERE credential_id=? AND state='pending'", (cid,)).fetchone()
        if pending:
            prior = load(conn, pending[0])
            if prior['expires_at'] > now:
                return {'offer': view(conn, prior), 'existing': True}
            prior['state'] = 'expired'; save(conn, prior)
        row = {'offer_id': str(uuid.uuid4()), 'request_id': rid, 'request_binding': request_binding,
            'credential_id': cid, 'host_id': str(msg['_daemon_host']), 'label': record.get('label') or 'Phone',
            'issuer': actor, 'created_at': now, 'expires_at': int(now) + DURABLE_TTL,
            'expected_prior_key_id': current_key(conn, cid), 'expected_key_epoch': key_epoch(conn, cid), 'state': 'pending', 'accepted_key_id': None}
        save(conn, row)
        import store_consent_push as push
        push.enqueue(conn, 'enrollment', row['offer_id'], row['host_id'], [cid], now)
        return {'offer': view(conn, row), 'delivery': push.status(conn, row['offer_id'])}
    oid = str(msg.get('offer_id') or '')
    row = load(conn, oid)
    if not row:
        raise c.ConsentError('consent_not_found')
    if verb in {'consent_key.cancel', 'consent_key.host_status'}:
        issuer(conn, auth, credentials)
    else:
        cid = mobile(conn, auth, credentials)
        if cid != row['credential_id']:
            raise c.ConsentError('consent_target_required')
    if verb in {'consent_key.status','consent_key.host_status'}:
        import store_consent_push as push
        return {'offer': view(conn, row), 'delivery': push.status(conn, oid), **({'receipt': row['receipt']} if row.get('receipt') else {})}
    if verb == 'consent_key.accept':
        fields = {k: str(msg.get(k) or '') for k in ('challenge_id', 'spki', 'signature')}
        if row['state'] == 'accepted' and row.get('response') == fields:
            return {'offer': view(conn, row), 'receipt': row['receipt'], 'replayed': True}
        # Verify the bound response even on conflict so unauthenticated noise cannot alert a target.
        if fields['challenge_id'] != row.get('challenge_id'):
            raise c.ConsentError('consent_conflict')
        c.verify(fields['spki'], fields['signature'], offer_bytes(row, fields['spki']))
        if row['state'] == 'accepted':
            error = c.ConsentError('consent_conflict')
            error.security_notice = (oid, row['credential_id'])
            raise error
    if row['state'] != 'pending':
        raise c.ConsentError('consent_conflict')
    if row['expires_at'] <= now:
        raise c.ConsentError('consent_expired')
    valid_binding(conn, row, credentials)
    if verb == 'consent_key.open':
        if not row.get('challenge_id') or row['challenge_expires_at'] <= now:
            row.update(challenge_id=str(uuid.uuid4()), nonce=c.encode(secrets.token_bytes(32)),
                       challenge_expires_at=min(int(now) + SIGNING_TTL, row['expires_at']))
            save(conn, row)
        return {'offer': view(conn, row, signing=True)}
    if verb in {'consent_key.decline', 'consent_key.cancel'}:
        row['state'] = 'declined' if verb.endswith('decline') else 'cancelled'
        save(conn, row)
        return {'offer': view(conn, row)}
    if verb != 'consent_key.accept':
        raise c.ConsentError('consent_verb_invalid')
    if row.get('challenge_expires_at', 0) <= now:
        raise c.ConsentError('consent_expired')
    fingerprint = hashlib.sha256(c.decode(fields['spki'])).hexdigest()
    if c.rowdict(conn, 'v2_consent_keys', 'fingerprint', fingerprint):
        raise c.ConsentError('consent_key_exists')
    key_id = str(uuid.uuid4())
    if row['expected_prior_key_id']:
        conn.execute("UPDATE v2_consent_keys SET state='retired',revoked_at=? WHERE key_id=?", (now, row['expected_prior_key_id']))
    conn.execute('INSERT INTO v2_consent_keys VALUES (?,?,?,?,?,?,?,?,?,NULL)',
        (key_id, cid, fields['spki'], fingerprint, row['label'], 'active', now, None, now))
    receipt = {'offer_id': oid, 'key_id': key_id, 'credential_id': cid, 'host_id': row['host_id'],
               'spki_hash': fingerprint, 'state': 'active'}
    row.update(state='accepted', accepted_key_id=key_id, response=fields, receipt=receipt)
    save(conn, row)
    return {'offer': view(conn, row), 'receipt': receipt}


def notification(offer):
    return {'notification_id': 'consent-offer:' + offer['offer_id'], 'producer': 'consent.enrollment.v1',
        'title': 'Set up Approval key?', 'body': 'Use Face ID to approve privileged requests from this host.',
        'severity': 'info', 'state': 'open' if offer['state'] == 'pending' else 'resolved',
        'created_at': c.datetime.fromtimestamp(offer['created_at'], c.timezone.utc).isoformat(),
        'expires_at': c.datetime.fromtimestamp(offer['expires_at'], c.timezone.utc).isoformat(),
        'actions': [{'kind': 'consent_enrollment', 'action_id': 'offer'}], 'consent_offer': offer}
