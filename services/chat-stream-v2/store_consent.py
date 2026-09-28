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
    import store_consent_offers as offers
    import store_consent_intents as intents
    import store_consent_push as push
    offers.initialize(conn)
    intents.initialize(conn)
    push.initialize(conn)


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
        result['approved_by_key_id'] = row['approved_by_key_id']
    return result


def transition(conn: sqlite3.Connection, verb: str, msg: dict, auth: dict,
               credentials: dict, snapshot_at: float, protected_role: str) -> dict:
    import store_consent_intents as intents
    import store_consent_offers as offers
    import store_consent_push as push
    if verb == 'consent.client_support':
        return offers.operation(conn, verb, msg, auth, credentials, snapshot_at)
    if verb in {'consent.push_register', 'consent.push_unregister'}:
        return push.register(conn, verb, msg, auth, credentials)
    return intents.operation(conn, verb, msg, auth, credentials, snapshot_at, protected_role)


def key_operation(conn: sqlite3.Connection, verb: str, msg: dict, auth: dict,
                  credentials: dict, snapshot_at: float = 0) -> dict:
    import store_consent_offers as offers
    if verb in {'consent_key.list', 'consent_key.revoke'}:
        if not (auth.get('peer_loopback') is True and auth.get('local_admin_verified') is True
                and not auth.get('token_verified') and not auth.get('service_authenticated')):
            raise ConsentError('consent_local_admin_required')
        if verb == 'consent_key.list':
            cur = conn.execute('SELECT key_id,credential_id,fingerprint,label,state,created_at FROM v2_consent_keys')
            return {'keys': [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]}
        fingerprint = str(msg.get('fingerprint') or '').replace(' ', '').lower()
        key = rowdict(conn, 'v2_consent_keys', 'fingerprint', fingerprint)
        if not key:
            raise ConsentError('consent_fingerprint_unknown')
        conn.execute("UPDATE v2_consent_keys SET state='revoked',revoked_at=? WHERE key_id=?", (time.time(), key['key_id']))
        return {'key_id': key['key_id'], 'state': 'revoked'}
    if verb not in {'consent_key.offer', 'consent_key.devices', 'consent_key.open', 'consent_key.accept',
                    'consent_key.status', 'consent_key.host_status', 'consent_key.decline', 'consent_key.cancel'}:
        raise ConsentError('unsupported_verb')
    return offers.operation(conn, verb, msg, auth, credentials, snapshot_at)


def offer_bytes(row: dict, spki: str) -> bytes:
    from store_consent_offers import offer_bytes as transcript
    return transcript(row, spki)


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
