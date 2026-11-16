"""Credential-bound registrations and bounded durable Expo send/receipt work."""
import json
import os
import re
import time
import uuid

import store_consent as c
import store_consent_offers as offers

MAX_ATTEMPTS = 3
MAX_RECEIPT_ATTEMPTS = 12
RECEIPT_INITIAL_DELAY = 15 * 60
RECEIPT_RETRY_DELAY = 5 * 60


def initialize(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS v2_consent_push_registrations (
      credential_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL,
      environment TEXT NOT NULL, host_id TEXT NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS v2_consent_push_jobs (
      request_id TEXT NOT NULL, credential_id TEXT NOT NULL, kind TEXT NOT NULL, host_id TEXT NOT NULL,
      state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL,
      ticket_id TEXT, outcome TEXT, PRIMARY KEY(request_id,credential_id));
    ''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(v2_consent_push_jobs)')}
    for name, declaration in [('receipt_attempts', 'INTEGER NOT NULL DEFAULT 0'), ('send_token', 'TEXT')]:
        if name not in columns:
            conn.execute(f'ALTER TABLE v2_consent_push_jobs ADD COLUMN {name} {declaration}')


def register(conn, verb, msg, auth, credentials):
    cid = offers.mobile(conn, auth, credentials)
    if verb == 'consent.push_unregister':
        conn.execute('DELETE FROM v2_consent_push_registrations WHERE credential_id=?', (cid,))
        return {'push_status': 'removed'}
    if verb != 'consent.push_register':
        raise c.ConsentError('consent_verb_invalid')
    if msg.get('permission') == 'denied':
        conn.execute('DELETE FROM v2_consent_push_registrations WHERE credential_id=?', (cid,))
        return {'push_status': 'unavailable', 'reason': 'permission_denied'}
    token, project = str(msg.get('push_token') or ''), str(msg.get('project_id') or '')
    environment = str(msg.get('environment') or '')
    if not re.fullmatch(r'(?:Expo|Exponent)PushToken\[[A-Za-z0-9_-]+\]', token):
        raise c.ConsentError('consent_push_token_invalid')
    try:
        uuid.UUID(project)
    except ValueError:
        raise c.ConsentError('consent_push_project_invalid') from None
    if msg.get('platform') != 'ios' or environment not in {'development', 'production'}:
        raise c.ConsentError('consent_push_environment_invalid')
    owner = conn.execute('SELECT credential_id FROM v2_consent_push_registrations WHERE token=?', (token,)).fetchone()
    if owner and owner[0] != cid:
        raise c.ConsentError('consent_push_token_bound')
    conn.execute('INSERT INTO v2_consent_push_registrations VALUES (?,?,?,?,?,?) ON CONFLICT(credential_id) '
        'DO UPDATE SET token=excluded.token,project_id=excluded.project_id,environment=excluded.environment,host_id=excluded.host_id,updated_at=excluded.updated_at',
        (cid, token, project, environment, msg['_daemon_host'], time.time()))
    conn.execute("UPDATE v2_consent_push_jobs SET state='pending',next_at=? WHERE credential_id=? AND state='unavailable' AND attempts=0", (time.time(), cid))
    return {'push_status': 'registered' if os.environ.get('EXPO_PROJECT_ID') == project else 'unavailable',
            **({} if os.environ.get('EXPO_PROJECT_ID') == project else {'reason': 'sender_project_unconfigured'})}


def enqueue(conn, kind, rid, host, cids, now):
    for cid in cids:
        conn.execute('INSERT OR IGNORE INTO v2_consent_push_jobs '
            '(request_id,credential_id,kind,host_id,state,next_at) VALUES (?,?,?,?,?,?)', (rid, cid, kind, host, 'pending', now))


def status(conn, rid):
    rows = conn.execute('SELECT credential_id,state,outcome FROM v2_consent_push_jobs WHERE request_id=?', (rid,)).fetchall()
    result=[]
    for cid,state,outcome in rows:
        if state=='pending':
            reg=conn.execute('SELECT project_id FROM v2_consent_push_registrations WHERE credential_id=?',(cid,)).fetchone()
            if not reg: state,outcome='unavailable','registration_unavailable'
            elif reg[0]!=os.environ.get('EXPO_PROJECT_ID'): state,outcome='unavailable','sender_project_unconfigured'
        result.append({'credential_id':cid,'state':state,'reason':outcome})
    return result


def due(conn, credentials, now, protected_role):
    import store_consent_intents as intents
    jobs = []
    for rid, cid, kind, host, state, attempts, ticket, receipt_attempts, send_token in conn.execute(
        "SELECT request_id,credential_id,kind,host_id,state,attempts,ticket_id,receipt_attempts,send_token FROM v2_consent_push_jobs "
        "WHERE state IN ('pending','retry','receipt') AND next_at<=? ORDER BY next_at LIMIT 1", (now,)).fetchall():
        if state == 'receipt':
            # A receipt query cannot send to a phone or grant authority. Finish the
            # already accepted ticket even if the request/account has since ended.
            if not ticket or receipt_attempts >= MAX_RECEIPT_ATTEMPTS:
                conn.execute("UPDATE v2_consent_push_jobs SET state='failed',outcome='receipt_unavailable' WHERE request_id=? AND credential_id=?", (rid, cid))
                continue
            conn.execute('UPDATE v2_consent_push_jobs SET receipt_attempts=receipt_attempts+1,next_at=? WHERE request_id=? AND credential_id=?', (now + RECEIPT_RETRY_DELAY, rid, cid))
            jobs.append({'request_id': rid, 'credential_id': cid, 'kind': kind, 'host_id': host,
                         'token': send_token, 'ticket_id': ticket, 'state': state, 'attempts': receipt_attempts + 1})
            continue
        row = offers.load(conn, rid) if kind == 'enrollment' else intents.load(conn, rid)
        record = credentials.get(cid)
        registration = conn.execute('SELECT token,project_id,environment,host_id FROM v2_consent_push_registrations WHERE credential_id=?', (cid,)).fetchone()
        reason = None
        if attempts >= MAX_ATTEMPTS:
            reason = 'send_retry_exhausted'
        elif not row or row['state'] != 'pending' or row['expires_at'] <= now:
            reason = 'request_terminal'
        elif not record or record.get('revoked_at') or record.get('client_kind') != 'pentacle-mobile':
            reason = 'credential_invalid'
        elif not registration:
            reason = 'registration_unavailable'
        elif registration[3] != host or registration[1] != os.environ.get('EXPO_PROJECT_ID'):
            reason = 'sender_project_unconfigured'
        elif kind == 'enrollment':
            try:
                offers.valid_binding(conn, row, credentials)
            except c.ConsentError as exc:
                reason = exc.code
        else:
            try:
                # Same requester/target/revision checks as explicit open/commit.
                intents.validate_parent(conn, row, credentials, protected_role)
            except c.ConsentError as exc:
                reason = exc.code
            if not reason and not any(kid for kid, owner in row['audience_bindings'].items() if owner == cid
                and (key := c.rowdict(conn, 'v2_consent_keys', 'key_id', kid)) and key['state'] == 'active'):
                reason = 'audience_key_invalid'
        if reason:
            conn.execute("UPDATE v2_consent_push_jobs SET state='unavailable',outcome=? WHERE request_id=? AND credential_id=?", (reason, rid, cid))
            continue
        # Commit admission before I/O. A crash consumes an attempt as well.
        conn.execute('UPDATE v2_consent_push_jobs SET attempts=attempts+1,send_token=?,next_at=? WHERE request_id=? AND credential_id=?', (registration[0], now + 30, rid, cid))
        jobs.append({'request_id': rid, 'credential_id': cid, 'kind': kind, 'host_id': host,
                     'token': registration[0], 'ticket_id': ticket, 'state': state, 'attempts': attempts + 1})
    return jobs


def finish(conn, job, outcome, now):
    code, ticket = outcome.get('code'), outcome.get('ticket_id')
    if code == 'DeviceNotRegistered':
        # The ticket concerns the token at send time, never a later rotated token.
        conn.execute('DELETE FROM v2_consent_push_registrations WHERE credential_id=? AND token=?', (job['credential_id'], job['token']))
    delay = min(60, 2 ** job['attempts'])
    if job['state'] == 'receipt':
        retry = (outcome.get('pending') or outcome.get('transient')) and job['attempts'] < MAX_RECEIPT_ATTEMPTS
        state = 'delivered' if outcome.get('receipt_ok') else 'receipt' if retry else 'failed'
        delay = RECEIPT_RETRY_DELAY
    elif ticket:
        state, delay = 'receipt', RECEIPT_INITIAL_DELAY
    else:
        state = 'retry' if outcome.get('transient') and job['attempts'] < MAX_ATTEMPTS else 'failed'
    conn.execute('UPDATE v2_consent_push_jobs SET state=?,next_at=?,ticket_id=COALESCE(?,ticket_id),outcome=? WHERE request_id=? AND credential_id=?',
        (state, now + delay, ticket, str(code or ('accepted' if ticket else 'receipt_ok' if outcome.get('receipt_ok') else 'provider_failure')), job['request_id'], job['credential_id']))
