"""Phone consent persistence. Called exclusively by the Store worker under authority_lock.

The daemon verifies possession of an operator-confirmed P-256 key. It cannot
attest remote hardware or biometrics. All wire assertions are untrusted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import store_lifecycle_authority as authority


class ConsentError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS v2_consent_keys (
      key_id TEXT PRIMARY KEY, credential_id TEXT NOT NULL, spki TEXT NOT NULL,
      fingerprint TEXT UNIQUE NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL,
      created_at REAL NOT NULL, expires_at REAL, confirmed_at REAL, revoked_at REAL);
    CREATE UNIQUE INDEX IF NOT EXISTS ix_consent_active_device
      ON v2_consent_keys(credential_id) WHERE state='active';
    CREATE TABLE IF NOT EXISTS v2_consent_enrollment_codes (
      code_hash TEXT PRIMARY KEY, purpose TEXT NOT NULL, nonce TEXT NOT NULL,
      created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
    CREATE TABLE IF NOT EXISTS v2_consent_challenges (
      challenge_id TEXT PRIMARY KEY, action TEXT NOT NULL, target_stream_id TEXT NOT NULL,
      target_generation TEXT NOT NULL, expected_revision INTEGER NOT NULL,
      requester TEXT NOT NULL, audience_key_ids TEXT NOT NULL, audience_hash TEXT NOT NULL,
      display_text TEXT NOT NULL, nonce TEXT NOT NULL, created_at REAL NOT NULL,
      expires_at REAL NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
      approved_by_key_id TEXT, approved_by_credential_id TEXT, signature TEXT,
      consumed_at REAL, lifecycle_receipt TEXT, audit_id INTEGER);
    CREATE TABLE IF NOT EXISTS v2_consent_audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, verb TEXT NOT NULL, challenge_id TEXT,
      actor TEXT NOT NULL, credential_snapshot_at REAL NOT NULL,
      result TEXT NOT NULL, refusal_code TEXT, created_at REAL NOT NULL);
    ''')


def encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode('ascii')


def decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ConsentError('consent_invalid_encoding') from None


def framed(domain: str, fields: list[Any]) -> bytes:
    values = [domain, *fields]
    return b''.join(struct.pack('>I', len(raw)) + raw for raw in
                    (str(v).encode('utf-8') for v in values))


def challenge_bytes(row: dict) -> bytes:
    requester = json.loads(row['requester'])
    return framed('pentacle-consent-v1', [row['challenge_id'], row['action'],
        row['target_stream_id'], row['target_generation'], row['expected_revision'],
        requester['kind'], requester['identity'], requester['generation'],
        row['audience_hash'], row['nonce'], row['expires_at']])


def enrollment_bytes(code_hash: str, credential_id: str, spki: str, nonce: str) -> bytes:
    return framed('pentacle-consent-enrol', [code_hash, credential_id, spki, nonce])


def verify(spki: str, signature: str, message: bytes) -> None:
    try:
        public = serialization.load_der_public_key(decode(spki))
        if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
            raise ConsentError('consent_wrong_algorithm')
        public.verify(decode(signature), message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        raise ConsentError('consent_bad_signature') from None


def rowdict(conn: sqlite3.Connection, table: str, column: str, value: str) -> dict | None:
    # Table and column are internal constants, never wire data.
    cur = conn.execute(f'SELECT * FROM {table} WHERE {column}=?', (value,))
    row = cur.fetchone()
    return dict(zip([c[0] for c in cur.description], row)) if row else None


def principal(conn: sqlite3.Connection, auth: dict, credentials: dict) -> dict:
    if auth.get('token_verified') and auth.get('stream_id'):
        identity = str(auth['stream_id'])
        generation = str(auth.get('session_generation') or '')
        seat = authority._session(conn, identity)
        if not seat or seat.get('status') != 'open' or authority._generation(conn, identity) != generation:
            raise ConsentError('consent_principal_stale')
        return {'kind': 'seat', 'identity': identity, 'generation': generation}
    identity = str(auth.get('operator_principal') or '')
    cid = identity.removeprefix('operator:')
    record = credentials.get(cid)
    if (not auth.get('operator_authenticated') or not identity.startswith('operator:')
            or not record or record.get('revoked_at')
            or record.get('client_kind') != auth.get('connection_client')):
        raise ConsentError('consent_principal_invalid')
    return {'kind': 'operator', 'identity': identity, 'generation': ''}


def audit(conn: sqlite3.Connection, verb: str, cid: str | None, actor: dict,
          snapshot_at: float, result: str, code: str | None = None) -> int:
    cur = conn.execute('INSERT INTO v2_consent_audit '
        '(verb,challenge_id,actor,credential_snapshot_at,result,refusal_code,created_at) VALUES (?,?,?,?,?,?,?)',
        (verb, cid, json.dumps(actor, sort_keys=True), snapshot_at, result, code, time.time()))
    return int(cur.lastrowid)


def view(row: dict) -> dict:
    result = {k: row[k] for k in ('challenge_id', 'action', 'target_stream_id',
        'target_generation', 'expected_revision', 'audience_hash', 'display_text',
        'created_at', 'expires_at', 'state')}
    result['requester'] = json.loads(row['requester'])
    result['audience_key_ids'] = json.loads(row['audience_key_ids'])
    result['challenge_bytes'] = encode(challenge_bytes(row))
    if row.get('lifecycle_receipt'):
        result['receipt'] = json.loads(row['lifecycle_receipt'])
    return result


def request(conn: sqlite3.Connection, msg: dict, actor: dict, credentials: dict,
            protected_role: str, now: float, snapshot_at: float) -> dict:
    action = str(msg.get('action') or '')
    if action not in {'lifecycle.designate', 'lifecycle.revoke'}:
        raise ConsentError('consent_action_invalid')
    grant = authority.current(conn)
    expected = msg.get('expected_revision')
    if not isinstance(expected, int) or isinstance(expected, bool) or expected != grant['revision']:
        raise ConsentError('authority_revision_conflict')
    if action == 'lifecycle.revoke':
        target, generation = grant['stream_id'], grant['session_generation']
        if not target:
            raise ConsentError('authority_not_held')
    else:
        target, generation = str(msg.get('target_stream_id') or ''), str(msg.get('target_generation') or '')
        code = authority.eligible(conn, target, generation, protected_role)
        if code:
            raise ConsentError(code)
    reason = authority.scrub(msg.get('reason'), authority.MAX_REASON)
    if not reason:
        raise ConsentError('authority_reason_required')
    pending = conn.execute("SELECT challenge_id,requester FROM v2_consent_challenges WHERE "
        "action=? AND target_stream_id=? AND state='pending'", (action, target)).fetchall()
    for prior_id, prior_requester in pending:
        if actor['kind'] != 'operator' and json.loads(prior_requester) != actor:
            return {'code': 'consent_pending_exists', 'challenge': view(rowdict(conn, 'v2_consent_challenges', 'challenge_id', prior_id))}
    keys = conn.execute("SELECT key_id,credential_id,label FROM v2_consent_keys WHERE state='active' ORDER BY key_id").fetchall()
    keys = [k for k in keys if credentials.get(k[1]) and not credentials[k[1]].get('revoked_at')
            and credentials[k[1]].get('client_kind') == 'pentacle-mobile']
    if not keys:
        raise ConsentError('consent_no_active_key')
    for prior_id, _ in pending:
        conn.execute("UPDATE v2_consent_challenges SET state='superseded' WHERE challenge_id=?", (prior_id,))
        audit(conn, 'consent.supersede', prior_id, actor, snapshot_at, 'superseded')
    audience = json.dumps([k[0] for k in keys], separators=(',', ':'))
    cid = str(uuid.uuid4())
    display = f'{action}: {target} ({generation}); requester {actor["identity"]}; approve on: ' + ', '.join(k[2] for k in keys)
    conn.execute('INSERT INTO v2_consent_challenges '
        '(challenge_id,action,target_stream_id,target_generation,expected_revision,requester,audience_key_ids,audience_hash,display_text,nonce,created_at,expires_at,state,reason) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (cid, action, target, generation, expected,
        json.dumps(actor, sort_keys=True), audience, hashlib.sha256(audience.encode()).hexdigest(),
        display, encode(secrets.token_bytes(32)), now, now + 120, 'pending', reason))
    return {'code': 'consent_pending', 'challenge': view(rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid))}


def transition(conn: sqlite3.Connection, verb: str, msg: dict, auth: dict,
               credentials: dict, snapshot_at: float, protected_role: str) -> dict:
    now = time.time()
    actor = principal(conn, auth, credentials)
    if verb == 'consent.request':
        return request(conn, msg, actor, credentials, protected_role, now, snapshot_at)
    cid = str(msg.get('challenge_id') or '')
    row = rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid)
    if not row:
        raise ConsentError('consent_not_found')
    if verb == 'consent.status':
        return {'challenge': view(row)}
    credential_id = actor['identity'].removeprefix('operator:')
    keys = conn.execute("SELECT key_id FROM v2_consent_keys WHERE credential_id=? AND state='active'",
                        (credential_id,)).fetchall()
    audience = set(json.loads(row['audience_key_ids']))
    mobile = actor['kind'] == 'operator' and auth.get('connection_client') == 'pentacle-mobile'
    if verb in {'consent.approve', 'consent.deny'}:
        if not mobile or not any(k[0] in audience for k in keys):
            raise ConsentError('consent_audience_required')
    elif verb == 'consent.cancel':
        if actor['kind'] != 'operator' and actor != json.loads(row['requester']):
            raise ConsentError('consent_requester_required')
    else:
        raise ConsentError('consent_verb_invalid')
    if verb == 'consent.approve':
        key_id, signature = str(msg.get('key_id') or ''), str(msg.get('signature') or '')
        key = rowdict(conn, 'v2_consent_keys', 'key_id', key_id)
        if not key or key['state'] != 'active' or key['credential_id'] != credential_id or key_id not in audience:
            raise ConsentError('consent_key_invalid')
        if (row['state'] == 'approved' and row['approved_by_key_id'] == key_id
                and row['approved_by_credential_id'] == credential_id and row['signature'] == signature):
            return {'challenge': view(row), 'receipt': json.loads(row['lifecycle_receipt']), 'replayed': True}
    if row['state'] != 'pending':
        raise ConsentError('consent_conflict')
    if row['expires_at'] <= now:
        conn.execute("UPDATE v2_consent_challenges SET state='expired' WHERE challenge_id=?", (cid,))
        raise ConsentError('consent_expired')
    if verb != 'consent.approve':
        state = 'denied' if verb == 'consent.deny' else 'cancelled'
        conn.execute('UPDATE v2_consent_challenges SET state=? WHERE challenge_id=?', (state, cid))
        return {'challenge': view(rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid))}
    verify(key['spki'], signature, challenge_bytes(row))
    # Savepoint rolls back EVERY lifecycle write on refusal, preserving pending consent.
    conn.execute('SAVEPOINT consent_mutation')
    try:
        receipt = authority.mutate(conn, {'action': row['action'].removeprefix('lifecycle.'),
            'target_stream_id': row['target_stream_id'], 'target_generation': row['target_generation'],
            'expected_revision': row['expected_revision'], 'reason': row['reason'], 'request_id': cid},
            {'operator_authenticated': True, 'operator_principal': actor['identity'], '_consent_id': cid}, protected_role)
    except BaseException:
        conn.execute('ROLLBACK TO consent_mutation')
        conn.execute('RELEASE consent_mutation')
        raise
    conn.execute('RELEASE consent_mutation')
    receipt['consent_id'] = cid
    aid = audit(conn, verb, cid, actor, snapshot_at, 'approved')
    conn.execute("UPDATE v2_consent_challenges SET state='approved',approved_by_key_id=?,"
        'approved_by_credential_id=?,signature=?,consumed_at=?,lifecycle_receipt=?,audit_id=? WHERE challenge_id=?',
        (key_id, credential_id, signature, now, json.dumps(receipt, sort_keys=True), aid, cid))
    return {'challenge': view(rowdict(conn, 'v2_consent_challenges', 'challenge_id', cid)), 'receipt': receipt}


def key_operation(conn: sqlite3.Connection, verb: str, msg: dict, auth: dict,
                  credentials: dict) -> dict:
    now = time.time()
    local = auth.get('local_admin_verified') is True and not auth.get('token_verified')
    if verb in {'consent_key.enroll_code', 'consent_key.confirm', 'consent_key.revoke', 'consent_key.list'}:
        if not local:
            raise ConsentError('consent_local_admin_required')
        if verb == 'consent_key.enroll_code':
            code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            nonce = encode(secrets.token_bytes(32))
            conn.execute('INSERT INTO v2_consent_enrollment_codes VALUES (?,?,?,?,?,NULL)',
                         (code_hash, 'consent-key', nonce, now, now + 600))
            return {'code': code, 'expires_at': now + 600}
        if verb == 'consent_key.list':
            cur = conn.execute('SELECT key_id,credential_id,fingerprint,label,state,created_at FROM v2_consent_keys')
            return {'keys': [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]}
        fingerprint = str(msg.get('fingerprint') or '').replace(' ', '').lower()
        key = rowdict(conn, 'v2_consent_keys', 'fingerprint', fingerprint)
        if not key:
            raise ConsentError('consent_fingerprint_unknown')
        if verb == 'consent_key.revoke':
            conn.execute("UPDATE v2_consent_keys SET state='revoked',revoked_at=? WHERE key_id=?", (now, key['key_id']))
        else:
            if key['state'] != 'pending_confirm' or key['expires_at'] <= now:
                raise ConsentError('consent_key_not_pending')
            record = credentials.get(key['credential_id'])
            if not record or record.get('revoked_at') or record.get('client_kind') != 'pentacle-mobile':
                raise ConsentError('consent_principal_invalid')
            conn.execute("UPDATE v2_consent_keys SET state='retired',revoked_at=? WHERE credential_id=? AND state='active'", (now, key['credential_id']))
            conn.execute("UPDATE v2_consent_keys SET state='active',confirmed_at=? WHERE key_id=?", (now, key['key_id']))
        return {'key_id': key['key_id'], 'fingerprint': fingerprint,
                'state': 'active' if verb == 'consent_key.confirm' else 'revoked'}
    actor = principal(conn, auth, credentials)
    if actor['kind'] != 'operator' or auth.get('connection_client') != 'pentacle-mobile':
        raise ConsentError('consent_mobile_required')
    code = str(msg.get('code') or '').strip().upper()
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    entry = rowdict(conn, 'v2_consent_enrollment_codes', 'code_hash', code_hash)
    if not entry or entry['purpose'] != 'consent-key' or entry['expires_at'] <= now:
        raise ConsentError('enrollment_code_invalid')
    if entry['used_at'] is not None:
        raise ConsentError('enrollment_code_used')
    cid = actor['identity'].removeprefix('operator:')
    if verb == 'consent_key.prepare':
        return {'code_hash': code_hash, 'credential_id': cid, 'nonce': entry['nonce']}
    if verb != 'consent_key.enroll':
        raise ConsentError('consent_verb_invalid')
    spki, signature = str(msg.get('spki') or ''), str(msg.get('signature') or '')
    verify(spki, signature, enrollment_bytes(code_hash, cid, spki, entry['nonce']))
    fingerprint = hashlib.sha256(decode(spki)).hexdigest()
    if rowdict(conn, 'v2_consent_keys', 'fingerprint', fingerprint):
        raise ConsentError('consent_key_exists')
    key_id = str(uuid.uuid4())
    conn.execute('INSERT INTO v2_consent_keys VALUES (?,?,?,?,?,?,?,?,NULL,NULL)',
        (key_id, cid, spki, fingerprint, str(credentials[cid].get('label') or 'Phone'), 'pending_confirm', now, now + 600))
    conn.execute('UPDATE v2_consent_enrollment_codes SET used_at=? WHERE code_hash=?', (now, code_hash))
    return {'key_id': key_id, 'fingerprint': fingerprint, 'state': 'pending_confirm', 'expires_at': now + 600}


def notification(challenge: dict) -> dict:
    """Presentation only; generic notification answers never authorize consent."""
    return {'notification_id': 'consent:' + challenge['challenge_id'], 'producer': 'consent.v1',
            'title': 'Approve privileged action', 'body': challenge['display_text'],
            'state': 'open' if challenge['state'] == 'pending' else 'expired' if challenge['state'] == 'expired' else 'resolved',
            'severity': 'info', 'resolution': None,
            'created_at': datetime.fromtimestamp(challenge['created_at'], timezone.utc).isoformat(),
            'expires_at': datetime.fromtimestamp(challenge['expires_at'], timezone.utc).isoformat(),
            'actions': [{'action_id': 'consent', 'kind': 'consent', 'challenge': challenge}],
            'consent': challenge}
