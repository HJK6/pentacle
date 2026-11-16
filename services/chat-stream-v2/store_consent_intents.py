"""Durable approval parents with independently opened, immutable key challenges."""
import hashlib
import json
import secrets
import time
import uuid

import store_consent as c
import store_consent_offers as offers
import store_lifecycle_authority as authority


def initialize(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS v2_consent_intents (
      request_id TEXT PRIMARY KEY, state TEXT NOT NULL, expires_at REAL NOT NULL, data TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS v2_consent_intent_requests (
      actor TEXT NOT NULL, client_request_id TEXT NOT NULL, request_id TEXT NOT NULL, binding TEXT NOT NULL,
      PRIMARY KEY(actor,client_request_id));
    CREATE TABLE IF NOT EXISTS v2_consent_intent_challenges (
      challenge_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, key_id TEXT NOT NULL);
    ''')
    legacy = [r[0] for r in conn.execute("SELECT challenge_id FROM v2_consent_challenges WHERE state='pending' "
        "AND challenge_id NOT IN (SELECT challenge_id FROM v2_consent_intent_challenges)")]
    for cid in legacy:
        conn.execute("UPDATE v2_consent_challenges SET state='cancelled' WHERE challenge_id=?", (cid,))
        c.audit(conn, 'consent.cutover', cid, {'kind': 'server'}, time.time(), 'cancelled', 'consent_update_required')


def load(conn, rid):
    row = conn.execute('SELECT data FROM v2_consent_intents WHERE request_id=?', (rid,)).fetchone()
    return json.loads(row[0]) if row else None


def save(conn, row):
    conn.execute('INSERT INTO v2_consent_intents VALUES (?,?,?,?) ON CONFLICT(request_id) DO UPDATE '
        'SET state=excluded.state,data=excluded.data', (row['request_id'], row['state'], row['expires_at'], json.dumps(row, sort_keys=True)))


def view(row):
    return {k: row[k] for k in ('request_id', 'host_id', 'action', 'target_stream_id', 'target_generation',
        'expected_revision', 'requester', 'audience_key_ids', 'display_text', 'created_at', 'expires_at', 'state')}


def visible(row, actor, auth):
    return (actor == row['requester'] or
        (actor['kind'] == 'operator' and auth.get('connection_client') == 'pentacle') or
        (actor['kind'] == 'operator' and auth.get('connection_client') == 'pentacle-mobile'
         and actor['identity'].removeprefix('operator:') in row['audience_bindings'].values()))


def validate_parent(conn, row, credentials, protected_role):
    requester = row['requester']
    if requester['kind'] == 'seat':
        seat = authority._session(conn, requester['identity'])
        if not seat or seat.get('status') != 'open' or authority._generation(conn, requester['identity']) != requester['generation']:
            raise c.ConsentError('consent_principal_stale')
    else:
        record = credentials.get(requester['identity'].removeprefix('operator:'))
        if not record or record.get('revoked_at') or record.get('client_kind') != row['requester_client_kind']:
            raise c.ConsentError('consent_principal_invalid')
    grant = authority.current(conn)
    if grant['revision'] != row['expected_revision']:
        raise c.ConsentError('authority_revision_conflict')
    if row['action'] == 'lifecycle.designate':
        code = authority.eligible(conn, row['target_stream_id'], row['target_generation'], protected_role)
        if code:
            raise c.ConsentError(code)
    elif grant['stream_id'] != row['target_stream_id'] or grant['session_generation'] != row['target_generation']:
        raise c.ConsentError('authority_revision_conflict')


def terminal(conn, row, state):
    row['state'] = state; save(conn, row)
    conn.execute("UPDATE v2_consent_challenges SET state=? WHERE state='pending' AND challenge_id IN "
        '(SELECT challenge_id FROM v2_consent_intent_challenges WHERE request_id=?)', (state, row['request_id']))


def request(conn, msg, actor, auth, credentials, role, now, snapshot_at):
    import store_consent_push as push
    client_id = str(msg.get('request_id') or '')
    actor_binding = json.dumps([actor, auth.get('connection_client')], sort_keys=True)
    request_binding = json.dumps({k: msg.get(k) for k in ('action', 'target_stream_id', 'target_generation', 'expected_revision', 'reason', '_daemon_host')}, sort_keys=True)
    if client_id:
        if len(client_id) > 200: raise c.ConsentError('consent_request_id_invalid')
        prior = conn.execute('SELECT request_id,binding FROM v2_consent_intent_requests WHERE actor=? AND client_request_id=?', (actor_binding, client_id)).fetchone()
        if prior:
            if prior[1] != request_binding: raise c.ConsentError('consent_conflict')
            row = load(conn, prior[0])
            return {'code': 'consent_pending' if row['state'] == 'pending' else 'consent_' + row['state'], 'intent': view(row), 'replayed': True, 'delivery': push.status(conn, row['request_id'])}
    action = str(msg.get('action') or '')
    if action not in {'lifecycle.designate', 'lifecycle.revoke'}:
        raise c.ConsentError('consent_action_invalid')
    grant = authority.current(conn)
    expected = msg.get('expected_revision')
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != grant['revision']:
        raise c.ConsentError('authority_revision_conflict')
    if action == 'lifecycle.revoke':
        target, generation = grant['stream_id'], grant['session_generation']
        if not target:
            raise c.ConsentError('authority_not_held')
    else:
        target, generation = str(msg.get('target_stream_id') or ''), str(msg.get('target_generation') or '')
        code = authority.eligible(conn, target, generation, role)
        if code:
            raise c.ConsentError(code)
    reason = authority.scrub(msg.get('reason'), authority.MAX_REASON)
    if not reason:
        raise c.ConsentError('authority_reason_required')
    keys = conn.execute("SELECT key_id,credential_id,label FROM v2_consent_keys WHERE state='active' ORDER BY key_id").fetchall()
    keys = [k for k in keys if credentials.get(k[1]) and not credentials[k[1]].get('revoked_at')
            and credentials[k[1]].get('client_kind') == 'pentacle-mobile']
    if not keys:
        raise c.ConsentError('consent_no_active_key')
    keys = [k for k in keys if offers.support(conn, k[1], 'consent_open_v1')]
    if not keys:
        raise c.ConsentError('consent_update_required')
    pending = [json.loads(r[0]) for r in conn.execute("SELECT data FROM v2_consent_intents WHERE state='pending'")]
    for prior in pending:
        if prior['action'] == action and prior['target_stream_id'] == target:
            if actor['kind'] != 'operator' and prior['requester'] != actor:
                return {'code': 'consent_pending_exists', 'intent': view(prior)}
            terminal(conn, prior, 'superseded')
            c.audit(conn, 'consent.supersede', prior['request_id'], actor, snapshot_at, 'superseded')
    rid = str(uuid.uuid4())
    row = {'request_id': rid, 'host_id': msg['_daemon_host'], 'action': action, 'target_stream_id': target,
        'target_generation': generation, 'expected_revision': expected, 'requester': actor, 'requester_client_kind': auth.get('connection_client'),
        'audience_key_ids': [k[0] for k in keys], 'audience_bindings': {k[0]: k[1] for k in keys},
        'display_text': f'{action}: {target}; reason: {reason}; approve on: ' + ', '.join(k[2] for k in keys),
        'created_at': now, 'expires_at': now + offers.DURABLE_TTL, 'state': 'pending', 'reason': reason}
    save(conn, row)
    if client_id:
        conn.execute('INSERT INTO v2_consent_intent_requests VALUES (?,?,?,?)', (actor_binding, client_id, rid, request_binding))
    push.enqueue(conn, 'approval', rid, row['host_id'], list(set(row['audience_bindings'].values())), now)
    return {'code': 'consent_pending', 'intent': view(row), 'delivery': push.status(conn, rid)}


def operation(conn, verb, msg, auth, credentials, snapshot_at, protected_role):
    actor = c.principal(conn, auth, credentials)
    now = time.time()
    if verb == 'consent.request':
        return request(conn, msg, actor, auth, credentials, protected_role, now, snapshot_at)
    cid = str(msg.get('challenge_id') or '')
    child = conn.execute('SELECT request_id,key_id FROM v2_consent_intent_challenges WHERE challenge_id=?', (cid,)).fetchone() if cid else None
    rid = child[0] if child else str(msg.get('intent_id') or '')
    row = load(conn, rid)
    if not row:
        raise c.ConsentError('consent_not_found')
    if not visible(row, actor, auth):
        raise c.ConsentError('consent_audience_required')
    credential_id = actor['identity'].removeprefix('operator:')
    if verb == 'consent.status':
        result = {'intent': view(row)}
        if child and actor['kind'] == 'operator' and auth.get('connection_client') == 'pentacle-mobile' and row['audience_bindings'].get(child[1]) == credential_id:
            result['challenge'] = c.view(c.rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid))
        if row.get('receipt'):
            result['receipt'] = row['receipt']
        return result
    if verb == 'consent.cancel':
        if actor['kind'] != 'operator' and actor != row['requester']:
            raise c.ConsentError('consent_requester_required')
        if actor['kind'] == 'operator' and auth.get('connection_client') == 'pentacle-mobile' and actor != row['requester']:
            raise c.ConsentError('consent_requester_required')
    else:
        offers.mobile(conn, auth, credentials)
        key_id = str(msg.get('key_id') or (child[1] if child else ''))
        key = c.rowdict(conn, 'v2_consent_keys', 'key_id', key_id)
        if not key or key['credential_id'] != credential_id or row['audience_bindings'].get(key_id) != credential_id:
            raise c.ConsentError('consent_key_invalid')
        if child and child[1] != key_id:
            raise c.ConsentError('consent_key_invalid')
        signature = str(msg.get('signature') or '')
        if verb == 'consent.approve' and row['state'] == 'approved' and row.get('response') == {'challenge_id': cid, 'key_id': key_id, 'signature': signature, 'credential_id': credential_id}:
            return {'intent': view(row), 'challenge': c.view(c.rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid)), 'receipt': row['receipt'], 'replayed': True}
        if key['state'] != 'active':
            raise c.ConsentError('consent_key_invalid')
    if row['state'] != 'pending':
        raise c.ConsentError('consent_conflict')
    if row['expires_at'] <= now:
        raise c.ConsentError('consent_expired')
    validate_parent(conn, row, credentials, protected_role)
    if verb == 'consent.open':
        if not offers.support(conn, credential_id, 'consent_open_v1'):
            raise c.ConsentError('consent_update_required')
        old = conn.execute("SELECT c.challenge_id,c.expires_at FROM v2_consent_challenges c JOIN v2_consent_intent_challenges b "
            "ON b.challenge_id=c.challenge_id WHERE b.request_id=? AND b.key_id=? AND c.state='pending'", (rid, key_id)).fetchone()
        if old and old[1] > now:
            return {'intent': view(row), 'challenge': c.view(c.rowdict(conn, 'v2_consent_challenges', 'challenge_id', old[0]))}
        if old:
            conn.execute("UPDATE v2_consent_challenges SET state='expired' WHERE challenge_id=?", (old[0],))
        cid = str(uuid.uuid4())
        audience = json.dumps(row['audience_key_ids'], separators=(',', ':'))
        conn.execute('INSERT INTO v2_consent_challenges '
            '(challenge_id,action,target_stream_id,target_generation,expected_revision,requester,audience_key_ids,audience_hash,display_text,nonce,created_at,expires_at,state,reason) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (cid, row['action'], row['target_stream_id'], row['target_generation'],
            row['expected_revision'], json.dumps(row['requester'], sort_keys=True), audience, hashlib.sha256(audience.encode()).hexdigest(),
            row['display_text'], c.encode(secrets.token_bytes(32)), now, min(now + offers.SIGNING_TTL, row['expires_at']), 'pending', row['reason']))
        conn.execute('INSERT INTO v2_consent_intent_challenges VALUES (?,?,?)', (cid, rid, key_id))
        return {'intent': view(row), 'challenge': c.view(c.rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid))}
    if verb in {'consent.deny', 'consent.cancel'}:
        terminal(conn, row, 'denied' if verb == 'consent.deny' else 'cancelled')
        return {'intent': view(row)}
    if verb != 'consent.approve' or not child:
        raise c.ConsentError('consent_verb_invalid')
    challenge = c.rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid)
    if challenge['state'] != 'pending':
        raise c.ConsentError('consent_conflict')
    if challenge['expires_at'] <= now:
        raise c.ConsentError('consent_expired')
    c.verify(key['spki'], signature, c.challenge_bytes(challenge))
    receipt = authority.mutate(conn, {'action': row['action'].removeprefix('lifecycle.'),
        'target_stream_id': row['target_stream_id'], 'target_generation': row['target_generation'],
        'expected_revision': row['expected_revision'], 'reason': row['reason'], 'request_id': cid},
        {'operator_authenticated': True, 'operator_principal': actor['identity'], '_consent_id': cid}, protected_role)
    receipt['consent_id'] = cid; receipt['consent_request_id'] = rid
    terminal(conn, row, 'superseded')
    row.update(state='approved', receipt=receipt, response={'challenge_id': cid, 'key_id': key_id, 'signature': signature, 'credential_id': credential_id})
    save(conn, row)
    conn.execute("UPDATE v2_consent_challenges SET state='approved',approved_by_key_id=?,approved_by_credential_id=?,"
        'signature=?,consumed_at=?,lifecycle_receipt=? WHERE challenge_id=?',
        (key_id, credential_id, signature, now, json.dumps(receipt, sort_keys=True), cid))
    return {'intent': view(row), 'challenge': c.view(c.rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid)), 'receipt': receipt}


def notification(intent):
    return {'notification_id': 'consent:' + intent['request_id'], 'producer': 'consent.v1',
        'title': 'Approval requested', 'body': intent['display_text'], 'severity': 'info',
        'state': 'open' if intent['state'] == 'pending' else 'resolved',
        'created_at': c.datetime.fromtimestamp(intent['created_at'], c.timezone.utc).isoformat(),
        'expires_at': c.datetime.fromtimestamp(intent['expires_at'], c.timezone.utc).isoformat(),
        'actions': [{'kind': 'consent', 'action_id': 'consent'}], 'consent': intent}
