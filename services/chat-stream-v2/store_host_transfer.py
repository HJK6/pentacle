"""Fixed, operator-dispatch-authorized seat succession in the daemon Store.

Only the Store thread calls these functions. The journal is the authority
linearization record; physical death remains the ordinary session lifecycle.
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import time

from store_assistant_binding import _binding_conn, _seat_conn
import store_lifecycle_authority as lifecycle

# Copied into deferred tasks, then rechecked on the Store thread at effect time.
# Daemon-owned reconciliation explicitly clears it and uses the frozen command.
actor_context: ContextVar[tuple[str, str] | None] = ContextVar('transfer_actor', default=None)
TERMINAL = {'committed', 'complete', 'cancelled', 'failed', 'expired'}
COMMITTED = {'committed', 'complete'}
SOURCE_FIELDS = ('session_generation', 'role', 'parent_stream_id', 'provider',
                 'effective_model', 'effective_effort')


class TransferError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfers (
        command_id TEXT PRIMARY KEY, composite TEXT NOT NULL, input_identity TEXT NOT NULL,
        dispatch_id TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
        expires_at REAL NOT NULL, data TEXT NOT NULL,
        UNIQUE(composite,input_identity))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_seats (
        command_id TEXT NOT NULL, source TEXT NOT NULL, source_generation TEXT NOT NULL,
        target TEXT NOT NULL UNIQUE, target_generation TEXT, ready INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(command_id,source))''')
    if 'attempt_id' not in {r[1] for r in conn.execute('PRAGMA table_info(v2_host_transfer_seats)')}:
        conn.execute('ALTER TABLE v2_host_transfer_seats ADD COLUMN attempt_id TEXT')
    initialize_authorizations(conn)
    conn.execute('CREATE INDEX IF NOT EXISTS ix_transfer_source ON v2_host_transfer_seats(source,source_generation)')
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_retirements (
        stream_id TEXT NOT NULL,generation TEXT NOT NULL,request_id TEXT NOT NULL,
        created_at REAL NOT NULL,PRIMARY KEY(stream_id,generation))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_revoke_receipts (
        request_id TEXT PRIMARY KEY,digest TEXT NOT NULL,receipt TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT, event TEXT NOT NULL,
        actor TEXT, generation TEXT, detail TEXT NOT NULL, created_at REAL NOT NULL)''')


def audit(conn, cid, event, actor=None, generation=None, **detail):
    if event in {'authorized','attempt_started','resumed','authorization_revoked'}:
        detail.update(subsystem='assistant.transfer',bug_ref='standing-transfer/v1')
    conn.execute('INSERT INTO v2_host_transfer_audit(command_id,event,actor,generation,detail,created_at) VALUES(?,?,?,?,?,?)',
                 (cid,event,actor,generation,canonical(detail),time.time()))


def load(conn, cid):
    row = conn.execute('SELECT data FROM v2_host_transfers WHERE command_id=?', (cid,)).fetchone()
    return json.loads(row[0]) if row else None


def save(conn, row):
    conn.execute('UPDATE v2_host_transfers SET state=?,data=? WHERE command_id=?',
                 (row['state'],canonical(row),row['command_id']))


def seats(conn, cid):
    return [dict(r) for r in conn.execute('SELECT * FROM v2_host_transfer_seats WHERE command_id=? ORDER BY source',(cid,))]


def authority_state(conn, sid, generation):
    if conn.execute('SELECT 1 FROM v2_host_transfer_retirements WHERE stream_id=? AND generation=?',(sid,generation)).fetchone():
        return 'retired'
    # A reservation is inert before its generation is known. The target name
    # is daemon-reserved and unique, so a competing open cannot acquire power.
    row = conn.execute('''SELECT t.state,s.source,s.source_generation,s.target,s.target_generation
        FROM v2_host_transfer_seats s JOIN v2_host_transfers t USING(command_id)
        WHERE (s.source=? AND s.source_generation=? AND t.state IN ('committed','complete'))
           OR (s.target=? AND (s.target_generation IS NULL OR s.target_generation=?)
               AND t.state NOT IN ('committed','complete')) LIMIT 1''',
        (sid,generation,sid,generation)).fetchone()
    if not row:
        for attempt in conn.execute('SELECT data FROM v2_host_transfer_attempts'):
            historical=json.loads(attempt[0])
            if historical['state'] in {'failed','cancelled','expired'} and any(p['target']==sid and p['target_generation'] in (None,generation) for p in historical['seats']):
                return 'prepared'
        return None
    return 'retired' if row['source']==sid else 'prepared'


def require_active(conn, sid, generation):
    state = authority_state(conn,sid,generation)
    if state:
        raise TransferError('authority_' + state)


def successor(conn, sid, generation):
    seen=set()
    while (sid,generation) not in seen:
        seen.add((sid,generation))
        row=conn.execute('''SELECT s.target,s.target_generation FROM v2_host_transfer_seats s
            JOIN v2_host_transfers t USING(command_id) WHERE s.source=? AND s.source_generation=?
            AND t.state IN ('committed','complete')''',(sid,generation)).fetchone()
        if not row:
            return sid,generation
        sid,generation=row[0],row[1]
    raise TransferError('transfer_succession_cycle')


def _live(conn,sid,*,ready=True):
    row=_seat_conn(conn,sid)
    if not row or row['status']!='open' or not row.get('session_generation'):
        raise TransferError('transfer_seat_unavailable')
    if ready and (row.get('pane_status')!='pane_alive' or row.get('bootstrap_state')!='ready'
                  or row.get('offline_since_ts') or row.get('presumed_dead_at')
                  or row.get('routing_integrity')=='mismatch'):
        raise TransferError('transfer_seat_not_ready')
    return row


def _credential(registry,principal):
    if not re.fullmatch(r'operator:[0-9a-fA-F-]{36}',principal):
        raise TransferError('transfer_operator_origin_required')
    record=registry.load().credentials.get(principal.removeprefix('operator:'))
    if not record or record.get('revoked_at') or record.get('scope'):
        raise TransferError('transfer_operator_revoked')


def _fresh(row,now):
    if not row['admitted_at'] <= now < row['expires_at']:
        raise TransferError('transfer_authorization_expired' if row.get('authorization_id') else 'transfer_dispatch_expired')


def _descendant(conn,sid,root):
    seen=set()
    while sid and sid not in seen:
        if sid==root:
            return True
        seen.add(sid)
        row=_seat_conn(conn,sid)
        sid=(row or {}).get('parent_stream_id')
    return False


def claim(conn,msg,auth,*,name,composite,env_binding,advisor,helpers,trusted_hosts,registry,now=None,snapshot_only=False):
    now=time.time() if now is None else now
    cid=msg.get('command_id')
    if not isinstance(cid,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',cid):
        raise TransferError('transfer_command_required')
    actor,generation=auth.get('stream_id'),auth.get('session_generation')
    if not auth.get('token_verified') or not actor or not generation:
        raise TransferError('transfer_fd_required')
    members=msg.get('members')
    if not isinstance(members,list) or not members or len(members)>16 or any(not isinstance(x,str) for x in members) or len(set(members))!=len(members):
        raise TransferError('transfer_roster_invalid')
    pointers=msg.get('pointers',{})
    if not isinstance(pointers,dict) or set(pointers)-set(members):
        raise TransferError('transfer_pointers_invalid')
    for refs in pointers.values():
        if not isinstance(refs,list) or len(refs)>16:
            raise TransferError('transfer_pointers_invalid')
        for ref in refs:
            if (not isinstance(ref,dict) or set(ref)!={'host','path','sha256'} or not isinstance(ref['host'],str) or ref['host'] not in trusted_hosts
                or not isinstance(ref['path'],str) or not ref['path'].startswith('/')
                or not re.fullmatch(r'[a-f0-9]{64}',str(ref['sha256']))):
                raise TransferError('transfer_pointers_invalid')
    payload={'dispatch_id':msg.get('dispatch_id'),'destination':msg.get('destination'),
             'members':sorted(members),'pointers':pointers,'intent':'full-authority-transfer/v1'}
    prior=load(conn,cid)
    if prior:
        if prior['digest']!=digest(payload) or (prior['actor'],prior['actor_generation'])!=(actor,generation):
            raise TransferError('transfer_command_conflict')
        return prior
    require_active(conn,actor,generation)
    binding=_binding_conn(conn,env_binding,name=name)
    grant=lifecycle.current(conn)
    if (binding['stream_id'],binding['generation'])!=(actor,generation) or not lifecycle.holder_is(conn,actor,generation):
        raise TransferError('transfer_fd_required')
    if not isinstance(payload['destination'],str) or payload['destination'] not in trusted_hosts:
        raise TransferError('transfer_host_untrusted')
    route=conn.execute('SELECT * FROM v2_assistant_composite_routes WHERE stream_id=? AND dispatch_id=?',
                       (composite,payload['dispatch_id'])).fetchone()
    if not route:
        raise TransferError('transfer_dispatch_required')
    route=dict(route)
    _credential(registry,str(route.get('actor_stream_id') or ''))
    if (route['routing_state']!='resolved' or route['delivery_state']!='landed'
        or (route['route_target'],route['route_target_generation'])!=(actor,generation)
        or json.loads(route['route_json'] or '{}').get('admission_mode')!='direct_primary'):
        raise TransferError('transfer_dispatch_not_addressed')
    material={'body':route['body'],'attachments':json.loads(route['attachments_json']),
              'reply_to_message_id':route['reply_to_message_id'],'reply_to_question_id':route['reply_to_question_id']}
    if digest(material)!=route['payload_digest']:
        raise TransferError('transfer_dispatch_corrupt')
    try:
        admitted=datetime.fromisoformat(route['created_at'].replace('Z','+00:00'))
        if admitted.tzinfo is None: raise ValueError()
        admitted=admitted.timestamp()
    except (ValueError,TypeError,AttributeError):
        raise TransferError('transfer_dispatch_expired') from None
    row={'command_id':cid,'composite':composite,'name':name,'env_binding':env_binding,
         **payload,'digest':digest(payload),'actor':actor,'actor_generation':generation,
         'operator':route['actor_stream_id'],'input_identity':route['input_identity'],
         'route_id':route['route_id'],'input_digest':route['payload_digest'],
         'route_snapshot':{k:route[k] for k in ('actor_stream_id','input_identity','payload_digest','created_at','route_target','route_target_generation','dispatch_id','routing_state','delivery_state')},
         'admitted_at':admitted,'expires_at':admitted+600,'binding':binding,'grant':grant,
         'advisor':advisor,'helpers':helpers,'state':'preparing','sources':{},'cleanup':{}}
    _fresh(row,now)
    required={actor,*helpers.values()}
    if advisor and advisor not in {'disabled',actor}: required.add(advisor)
    if not required.issubset(set(members)):
        raise TransferError('transfer_roster_incomplete')
    for sid in sorted(members):
        source=_live(conn,sid)
        require_active(conn,sid,source['session_generation'])
        if sid!=advisor and sid not in helpers.values() and not _descendant(conn,sid,actor):
            raise TransferError('transfer_member_unowned')
        if not all(source.get(k) for k in ('provider','effective_model','effective_effort','role')):
            raise TransferError('transfer_member_authority_unknown')
        if conn.execute('SELECT 1 FROM v2_qa_commissions WHERE reviewer=? AND generation=? AND report_id IS NULL',
                        (sid,source['session_generation'])).fetchone():
            raise TransferError('transfer_member_authority_unknown')
        # Preserve source metadata, not credentials or transcript fields.
        row['sources'][sid]={k:source.get(k) for k in (*SOURCE_FIELDS,'visibility','phase','objective',
            'objective_source','spec_id','spec_ids','spec_resolution','qualified_spec_ids',
            'spec_binding_provenance','self_close_on_completion','no_watch')}
    if conn.execute('SELECT 1 FROM v2_host_transfer_authorizations WHERE command_id=? OR (composite=? AND input_identity=?)',(cid,composite,route['input_identity'])).fetchone():
        raise TransferError('transfer_input_consumed')
    if conn.execute('SELECT 1 FROM v2_host_transfers WHERE composite=? AND input_identity=?',
                    (composite,route['input_identity'])).fetchone():
        raise TransferError('transfer_input_consumed')
    for sid,source in row['sources'].items():
        if conn.execute('''SELECT 1 FROM v2_host_transfer_seats s JOIN v2_host_transfers t USING(command_id)
            WHERE s.source=? AND s.source_generation=? AND t.state='preparing' ''',
            (sid,source['session_generation'])).fetchone():
            raise TransferError('transfer_source_busy')
    if snapshot_only:
        return row
    checkpoint_required(row)
    conn.execute('INSERT INTO v2_host_transfers VALUES(?,?,?,?,?,?,?,?)',
        (cid,composite,route['input_identity'],payload['dispatch_id'],row['digest'],row['state'],row['expires_at'],canonical(row)))
    for sid,source in row['sources'].items():
        target=payload['destination']+':v2-transfer-'+hashlib.sha256((cid+'\0'+sid).encode()).hexdigest()[:16]
        if _seat_conn(conn,target): raise TransferError('transfer_target_conflict')
        conn.execute('INSERT INTO v2_host_transfer_seats(command_id,source,source_generation,target) VALUES(?,?,?,?)',
                     (cid,sid,source['session_generation'],target))
    audit(conn,cid,'claimed',actor,generation,digest=row['digest'],dispatch_id=row['dispatch_id'])
    return row


def check_source(conn,row,registry,*,now=None):
    now=time.time() if now is None else now
    if row.get('authorization_id'):
        check_authorization(conn,row,registry,now=now)
    _fresh(row,now)
    _credential(registry,row['operator'])
    route=conn.execute('SELECT * FROM v2_assistant_composite_routes WHERE route_id=?',(row['route_id'],)).fetchone()
    if not route or any(route[k]!=v for k,v in row['route_snapshot'].items()):
        raise TransferError('transfer_dispatch_changed')
    if row['state']!='preparing': raise TransferError('transfer_not_preparing')
    binding=_binding_conn(conn,row['env_binding'],name=row['name'])
    if any(binding[k]!=row['binding'][k] for k in ('stream_id','generation','revision')) or lifecycle.current(conn)!=row['grant']:
        raise TransferError('transfer_authority_changed')
    for sid,frozen in row['sources'].items():
        current=_live(conn,sid)
        require_active(conn,sid,current['session_generation'])
        if any(current.get(k)!=v for k,v in frozen.items()):
            raise TransferError('transfer_source_changed')


def prepared(conn,cid,target,generation,attempt_id=None):
    row=load(conn,cid)
    if not row or row['state']!='preparing': raise TransferError('transfer_not_preparing')
    require_attempt(row,attempt_id)
    participant=conn.execute('SELECT * FROM v2_host_transfer_seats WHERE command_id=? AND target=?',(cid,target)).fetchone()
    if not participant: raise TransferError('transfer_target_unknown')
    if participant['target_generation'] not in {None,generation}: raise TransferError('transfer_target_changed')
    live=_live(conn,target)
    if live['session_generation']!=generation or live.get('role')!='worker' or live.get('handoff_from_stream_id'):
        raise TransferError('transfer_preparation_not_inert')
    conn.execute('UPDATE v2_host_transfer_seats SET target_generation=? WHERE command_id=? AND target=?',(generation,cid,target))


def ready(conn,cid,auth,pointer_digest,attempt_id=None):
    row=load(conn,cid)
    if not row or row['state']!='preparing': raise TransferError('transfer_not_preparing')
    require_attempt(row,attempt_id)
    sid,gen=auth.get('stream_id'),auth.get('session_generation')
    part=conn.execute('SELECT * FROM v2_host_transfer_seats WHERE command_id=? AND target=?',(cid,sid)).fetchone()
    if auth.get('token_verified') and part and part['target_generation'] is None:
        prepared(conn,cid,sid,gen,attempt_id)
        part=conn.execute('SELECT * FROM v2_host_transfer_seats WHERE command_id=? AND target=?',(cid,sid)).fetchone()
    if not auth.get('token_verified') or not part or part['target_generation']!=gen:
        raise TransferError('transfer_ready_unauthorized')
    if pointer_digest!=digest(row['pointers'].get(part['source'],[])):
        raise TransferError('transfer_pointer_conflict')
    live=_live(conn,sid)
    source=row['sources'][part['source']]
    if live['session_generation']!=gen or any(live.get(k)!=source[k] for k in ('provider','effective_model','effective_effort')):
        raise TransferError('transfer_target_changed')
    conn.execute('UPDATE v2_host_transfer_seats SET ready=1 WHERE command_id=? AND target=?',(cid,sid))
    audit(conn,cid,'ready',sid,gen)


def cancel(conn,cid,auth,msg=None):
    row=load(conn,cid)
    if not row: raise TransferError('transfer_unknown')
    admin=bool(auth.get('operator_authenticated') and not auth.get('token_verified')) or bool(auth.get('peer_loopback') and auth.get('local_admin_verified'))
    owner=auth.get('token_verified') and (auth.get('stream_id'),auth.get('session_generation'))==(row['actor'],row['actor_generation'])
    if not admin and not owner: raise TransferError('transfer_cancel_unauthorized')
    if row.get('authorization_id'):
        return cancel_authorized(conn,row,auth,msg or {})
    if row['state'] in COMMITTED: raise TransferError('transfer_already_committed')
    if row['state'] not in TERMINAL:
        row['state']='cancelled'; save(conn,row)
        audit(conn,cid,'cancelled',auth.get('stream_id') or auth.get('operator_principal'),auth.get('session_generation'))
    return row


def _pending_effects(conn, sources):
    for reservation in conn.execute('SELECT payload FROM v2_stream_reservations'):
        payload=json.loads(reservation[0] or '{}')
        payload=payload.get('open_fields',payload)
        if payload.get('parent_stream_id') in sources or payload.get('handoff_from_stream_id') in sources:
            raise TransferError('transfer_pending_spawn')
    for sid in sources:
        # Claims increment attempts before releasing the Store transaction.
        # Even an expired lease can represent a delayed external paste. Refuse
        # unresolved attempted notices until their existing queue settles them.
        if conn.execute("""SELECT 1 FROM v2_outbound_notices WHERE recipient_stream_id=?
            AND delivered_at IS NULL AND terminal_at IS NULL
            AND (attempts>0 OR lease_owner IS NOT NULL)""",(sid,)).fetchone():
            raise TransferError('transfer_uncertain_notice')
        if conn.execute('''SELECT 1 FROM v2_schedules WHERE
            (owner_stream_id=? OR parent_stream_id=? OR handoff_from_stream_id=?)
            AND state IN ('firing','indeterminate')''',(sid,sid,sid)).fetchone():
            raise TransferError('transfer_uncertain_schedule')


def commit(conn,cid,registry,*,now=None,attempt_id=None):
    """Caller holds registry file lock and lifecycle/graph admission locks.

    All authoritative references and retirement fences linearize together.
    Notifications projection and physical cleanup happen after this commit.
    """
    row=load(conn,cid)
    if not row: raise TransferError('transfer_unknown')
    require_attempt(row,attempt_id)
    if row['state'] in COMMITTED: return row
    check_source(conn,row,registry,now=now)
    parts=seats(conn,cid)
    if not parts or any(not p['ready'] or not p['target_generation'] for p in parts):
        raise TransferError('transfer_not_ready')
    _pending_effects(conn,row['sources'])
    mapping={p['source']:(p['target'],p['target_generation']) for p in parts}
    for part in parts:
        live=_live(conn,part['target'])
        source=row['sources'][part['source']]
        if (live['session_generation']!=part['target_generation'] or live['role']!='worker'
            or live.get('handoff_from_stream_id') or live.get('parent_stream_id')
            or any(live.get(k)!=source[k] for k in ('provider','effective_model','effective_effort'))):
            raise TransferError('transfer_target_changed')
    # Check helper/advisor bindings again; configuration changes invalidate the
    # frozen plan, even if all participants still exist.
    advisor=conn.execute("SELECT v FROM kv WHERE k='assistant.authority.stream_id'").fetchone()
    if advisor and advisor[0]!=row['advisor']:
        raise TransferError('transfer_helper_changed')
    for part in parts:
        sid,gen,target,tgen=part['source'],part['source_generation'],part['target'],part['target_generation']
        source=row['sources'][sid]
        host,name=target.split(':',1)
        fields={k:source[k] for k in ('role','visibility','phase','objective','objective_source','spec_id','spec_ids','spec_resolution',
                                    'qualified_spec_ids','spec_binding_provenance','self_close_on_completion','no_watch')}
        fields['parent_stream_id']=mapping.get(source['parent_stream_id'],(source['parent_stream_id'],None))[0]
        fields['handoff_from_stream_id']=sid
        conn.execute('UPDATE sessions SET '+','.join(k+'=?' for k in fields)+' WHERE host=? AND session_name=?',
                     (*fields.values(),host,name))
        conn.execute('INSERT INTO v2_assistant_direct_handoff_proofs VALUES(?,?,?,?,?)',
                     (target,tgen,sid,gen,stamp()))
        conn.execute("UPDATE sessions SET parent_stream_id=? WHERE parent_stream_id=? AND status='open'",
                     (target,sid))
        # Source stays physically open but loses its role and every authority
        # path through the committed exact-generation retirement record.
        shost,sname=sid.split(':',1)
        conn.execute("UPDATE sessions SET role='worker' WHERE host=? AND session_name=?",(shost,sname))
        conn.execute('INSERT OR REPLACE INTO v2_session_role(host,session_name,role_source,actor,changed_at) VALUES(?,?,?,?,?)',
                     (host,name,'handoff',row['actor'],stamp()))
        _move_custody(conn,cid,sid,gen,target,tgen)
    target,tgen=mapping[row['actor']]
    conn.execute('''INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET stream_id=excluded.stream_id,
        generation=excluded.generation,revision=excluded.revision,updated_at=excluded.updated_at''',
        (row['name'],target,tgen,row['binding']['revision']+1,stamp()))
    grant=row['grant']
    changed=conn.execute('''UPDATE v2_lifecycle_manager SET stream_id=?,session_generation=?,revision=revision+1,
        updated_at=? WHERE id=1 AND revision=? AND stream_id=? AND session_generation=?''',
        (target,tgen,time.time(),grant['revision'],grant['stream_id'],grant['session_generation']))
    if changed.rowcount!=1: raise TransferError('transfer_authority_changed')
    lifecycle.audit(conn,action='host_transfer',actor_kind='manager',actor_identity=row['actor'],
        actor_generation=row['actor_generation'],target_stream_id=target,target_generation=tgen,
        old_revision=grant['revision'],new_revision=grant['revision']+1,prior_stream_id=grant['stream_id'],
        prior_generation=grant['session_generation'],request_id=cid,result='applied')
    if row['advisor'] in mapping:
        conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('assistant.authority.stream_id',?)",
                     (mapping[row['advisor']][0],))
    overrides={key:mapping[sid][0] for key,sid in row['helpers'].items()}
    conn.execute('INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)',
                 ('assistant.transfer.helpers.'+row['name'],canonical(overrides)))
    if row.get('authorization_id'):
        grant_record=authorization(conn,row['authorization_id'])
        grant_record.update(state='consumed',revision=grant_record['revision']+1)
        save_authorization(conn,grant_record)
    row['state']='committed'
    row['receipt']={'command_id':cid,'digest':row['digest'],'operator':row['operator'],
        'dispatch_id':row['dispatch_id'],'mapping':parts,'binding_revision':row['binding']['revision']+1,
        'grant_revision':grant['revision']+1,'committed_at':stamp()}
    save(conn,row)
    if row.get('authorization_id'):
        grant_record['receipt']={k:v for k,v in row['receipt'].items() if k!='operator'}
        save_authorization(conn,grant_record)
        finalize_attempt(conn,row)
    audit(conn,cid,'committed',row['actor'],row['actor_generation'],receipt=row['receipt'])
    return row


def _move_custody(conn,cid,sid,gen,target,tgen):
    # Current custody is mutable; original intent, decisions and reports remain
    # unchanged. The journal explains every exact-generation substitution.
    for key,gkey in (('bound_stream_id','bound_generation'),('visible_chat_stream_id','visible_chat_generation')):
        conn.execute(f'''UPDATE v2_assistant_composite_lanes SET {key}=?,{gkey}=?,version=version+1,updated_at=?
            WHERE {key}=? AND {gkey}=?''',(target,tgen,stamp(),sid,gen))
    # Lane members are spec identifiers, not stream ids: preserve members_json.
    for schedule in conn.execute('''SELECT * FROM v2_schedules WHERE state IN ('pending','retry_pending')
            AND (owner_stream_id=? OR parent_stream_id=? OR handoff_from_stream_id=?)''',(sid,sid,sid)).fetchall():
        receipt=conn.execute("SELECT actor_id,measured_state_json FROM v2_operation_receipts WHERE request_id=? AND phase='row_committed'",(schedule['request_id'],)).fetchone()
        original_gen=json.loads(receipt['measured_state_json']).get('owner_session_generation') if receipt else None
        if not receipt or not original_gen or successor(conn,receipt['actor_id'],original_gen)!=(sid,gen):
            raise TransferError('transfer_schedule_custody_unknown')
        fields={k:target for k in ('owner_stream_id','parent_stream_id','handoff_from_stream_id') if schedule[k]==sid}
        conn.execute('UPDATE v2_schedules SET '+','.join(k+'=?' for k in fields)+',updated_at=? WHERE schedule_id=?',
                     (*fields.values(),stamp(),schedule['schedule_id']))
    for table in ('v2_watch_wake','v2_watch_facts','v2_watch_cancellations'):
        conn.execute(f'UPDATE {table} SET owner=?,owner_generation=? WHERE owner=? AND owner_generation=?',
                     (target,tgen,sid,gen))
    conn.execute('UPDATE v2_watch_pairs SET parent=?,parent_generation=? WHERE parent=? AND parent_generation=?',
                 (target,tgen,sid,gen))
    for watch in conn.execute('SELECT id,data FROM v2_watch_wake WHERE child=? AND child_generation=?',(sid,gen)).fetchall():
        data=json.loads(watch['data']);data['child_stream_id']=target
        conn.execute('UPDATE v2_watch_wake SET child=?,child_generation=?,data=? WHERE id=?',
                     (target,tgen,canonical(data),watch['id']))
    conn.execute('UPDATE v2_watch_facts SET child=?,child_generation=? WHERE child=? AND child_generation=?',(target,tgen,sid,gen))
    old_pair=conn.execute('SELECT * FROM v2_watch_pairs WHERE child=? AND child_generation=?',(sid,gen)).fetchone()
    if old_pair:
        # A prepared top-level seat can have an inert default-pair observation.
        # It has never had a parent or active watch; replace only that record.
        conn.execute('DELETE FROM v2_watch_pairs WHERE child=? AND parent IS NULL',(target,))
        conn.execute('UPDATE v2_watch_pairs SET child=?,child_generation=? WHERE child=? AND child_generation=?',
                     (target,tgen,sid,gen))
    for table,skey,gkey in (('v2_fleet_lane_state','root','root_generation'),('v2_fleet_lane_state','lane','lane_generation'),
                            ('v2_fleet_digest_state','root','root_generation')):
        conn.execute(f'UPDATE {table} SET {skey}=?,{gkey}=? WHERE {skey}=? AND {gkey}=?',(target,tgen,sid,gen))

    # Undelivered durable notices keep ids/deduplication and delivery history.
    # Generation-bearing notification metadata is handled by the question
    # resolver/projection; watch facts use the exact transferred owner above.
    for notice in conn.execute('SELECT * FROM v2_outbound_notices WHERE recipient_stream_id=? AND delivered_at IS NULL AND terminal_at IS NULL',(sid,)).fetchall():
        metadata=json.loads(notice['metadata'] or '{}')
        if metadata.get('owner_generation') not in (None,gen): continue
        for key in ('owner_generation','authority_generation','root_generation'):
            if metadata.get(key)==gen: metadata[key]=tgen
        if (metadata.get('producer_stream_id'),metadata.get('producer_session_generation')) == (sid,gen):
            metadata.update(producer_stream_id=target,producer_session_generation=tgen)
        from store_routing import _outbound_notice_digest
        metadata_json=json.dumps(metadata,sort_keys=True,separators=(',',':'))
        projected_digest=_outbound_notice_digest(notice['kind'],notice['dedupe_key'],target,
            notice['tell_id'],notice['body'],metadata_json,notice['source_stream_id'] or '',notice['episode_id'] or '')
        conn.execute('UPDATE v2_outbound_notices SET recipient_stream_id=?,metadata=?,payload_digest=? WHERE notice_id=?',
                     (target,metadata_json,projected_digest,notice['notice_id']))


def effective_ruling(conn,row):
    if not row: return row
    row=dict(row)
    row['_original_custody']={}
    for key in ('requester','authority','target','primary'):
        sidkey,genkey=key+'_stream_id',key+'_generation'
        if row.get(sidkey) and row.get(genkey):
            row['_original_custody'][key]={'stream_id':row[sidkey],'generation':row[genkey]}
            row[sidkey],row[genkey]=successor(conn,row[sidkey],row[genkey])
    return row


def transaction(conn,fn,registry=None):
    """Registry revoke and migration commit serialize on the existing lock.

    Acquire on the Store worker, never block the asyncio thread on flock and
    never hold a SQLite transaction across network/process preparation.
    """
    from contextlib import nullcontext
    from _shared.operator_auth import file_lock
    with file_lock(registry.lock_path) if registry is not None else nullcontext():
        conn.execute('BEGIN IMMEDIATE')
        try:
            result=fn(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def reserved_target(conn,sid):
    if conn.execute('SELECT command_id FROM v2_host_transfer_seats WHERE target=?',(sid,)).fetchone():
        return True
    return any(any(p['target']==sid for p in json.loads(r[0])['seats']) for r in conn.execute('SELECT data FROM v2_host_transfer_attempts'))


def emergency_revoke(conn,msg,auth,protected_role):
    """Opt-in exact FD retirement; caller rolls back every write on failure."""
    if not (auth.get('_emergency_verified') and auth.get('local_admin_verified') and auth.get('peer_loopback')
            and not auth.get('token_verified') and msg.get('action')=='revoke'):
        raise TransferError('transfer_revoke_unauthorized')
    config=msg.get('_transfer_binding') or {}
    if not config.get('name'): raise TransferError('transfer_composite_unavailable')
    rid=msg.get('request_id')
    payload={k:msg.get(k) for k in ('action','target_stream_id','target_generation','reason','expected_revision')}
    payload.update(retire_fd_generation=True,binding_name=config['name'])
    hashed=digest(payload)
    previous=conn.execute('SELECT digest,receipt FROM v2_host_transfer_revoke_receipts WHERE request_id=?',(rid,)).fetchone()
    if previous:
        if previous['digest']!=hashed: raise TransferError('authority_request_conflict')
        return {**json.loads(previous['receipt']),'replayed':True}
    if conn.execute("SELECT 1 FROM v2_lifecycle_authority_receipts WHERE actor_identity='operator:local-admin' AND request_id=?",(rid,)).fetchone():
        raise TransferError('authority_request_conflict')
    binding=_binding_conn(conn,config['env_binding'],name=config['name'])
    grant=lifecycle.current(conn)
    pair=(msg.get('target_stream_id'),msg.get('target_generation'))
    if not all(pair) or pair!=(binding['stream_id'],binding['generation']) or pair!=(grant['stream_id'],grant['session_generation']):
        raise TransferError('transfer_revoke_target_conflict')
    receipt=lifecycle.mutate(conn,msg,auth,protected_role)
    conn.execute('INSERT INTO v2_host_transfer_retirements VALUES(?,?,?,?)',(*pair,rid,time.time()))
    # Pin even environment-backed bindings, preventing a restart fallback.
    conn.execute('''INSERT INTO v2_assistant_direct_binding(name,stream_id,generation,revision,updated_at)
        VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET stream_id=excluded.stream_id,
        generation=excluded.generation,revision=excluded.revision,updated_at=excluded.updated_at''',
        (config['name'],*pair,binding['revision']+1,stamp()))
    for r in conn.execute('''SELECT DISTINCT t.command_id FROM v2_host_transfer_seats s
        JOIN v2_host_transfers t USING(command_id) WHERE s.source=? AND s.source_generation=? AND t.state='preparing' ''',pair).fetchall():
        row=load(conn,r[0]);row['state']='cancelled';row['last_error']='fd_generation_revoked';save(conn,row)
        audit(conn,r[0],'cancelled_by_revoke',pair[0],pair[1],request_id=rid)
    receipt.update(retire_fd_generation=True,binding_name=config['name'],binding_revision=binding['revision']+1)
    conn.execute('INSERT INTO v2_host_transfer_revoke_receipts VALUES(?,?,?)',(rid,hashed,canonical(receipt)))
    audit(conn,None,'fd_generation_revoked',auth.get('operator_principal'),None,
          target=pair,request_id=rid,revision=receipt['revision'])
    return receipt


def preparation_may_open(conn,sid):
    row=conn.execute('''SELECT t.data FROM v2_host_transfer_seats s
        JOIN v2_host_transfers t USING(command_id) WHERE s.target=?''',(sid,)).fetchone()
    if row:
        command=json.loads(row[0])
        if command['state']!='preparing':
            raise TransferError('transfer_preparation_cancelled')
        _fresh(command,time.time())
    elif reserved_target(conn,sid):
        raise TransferError('transfer_preparation_cancelled')


STANDING_MODE = 'standing-transfer/v1'
RESUME_CODES = {'transfer_uncertain_notice','transfer_effect_busy','transfer_preparation_failed'}
ID_PATTERN = r'[A-Za-z0-9_.:-]{1,128}'


def initialize_authorizations(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_authorizations (
        authorization_id TEXT PRIMARY KEY, command_id TEXT NOT NULL UNIQUE,
        composite TEXT NOT NULL, input_identity TEXT NOT NULL, state TEXT NOT NULL,
        expires_at REAL NOT NULL, data TEXT NOT NULL, UNIQUE(composite,input_identity))''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_transfer_authorization_expiry ON v2_host_transfer_authorizations(state,expires_at)')
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_attempts (
        command_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
        attempt_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
        PRIMARY KEY(command_id,attempt_number))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS v2_host_transfer_requests (
        request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL)''')


def authorization(conn,aid):
    require_id(aid)
    r=conn.execute('SELECT data FROM v2_host_transfer_authorizations WHERE authorization_id=?',(aid,)).fetchone()
    if not r: raise TransferError('transfer_authorization_required')
    return json.loads(r[0])


def save_authorization(conn,a):
    conn.execute('UPDATE v2_host_transfer_authorizations SET state=?,expires_at=?,data=? WHERE authorization_id=?',
                 (a['state'],expiry_value(a['expires_at']),canonical(a),a['authorization_id']))


def public_authorization(a):
    return {k:v for k,v in a.items() if k not in {'operator','scope_row'}}


def public_command(row):
    if not row or not row.get('authorization_id'): return row
    return {k:v for k,v in row.items() if k not in {'operator','route_snapshot','receipt'}} | {
        'receipt':{k:v for k,v in (row.get('receipt') or {}).items() if k!='operator'}}


def request_identity(conn,msg,auth):
    rid=msg.get('request_id')
    if not isinstance(rid,str) or not re.fullmatch(ID_PATTERN,rid):
        raise TransferError('transfer_authorization_conflict')
    payload={k:v for k,v in msg.items() if not k.startswith('_') and k not in {'type','from_stream_id','stream_token','local_admin_token'}}
    identity={'payload':payload,'actor':auth.get('stream_id') if auth.get('token_verified') else auth.get('operator_principal') or 'operator:local-admin',
              'generation':auth.get('session_generation') if auth.get('token_verified') else None}
    hashed=digest(identity)
    r=conn.execute('SELECT digest,response FROM v2_host_transfer_requests WHERE request_id=?',(rid,)).fetchone()
    if r:
        if r['digest']!=hashed: raise TransferError('transfer_authorization_conflict')
        response=json.loads(r['response'])
        if response.get('_error'): raise TransferError(response['_error'])
        return rid,hashed,response
    return rid,hashed,None


def request_receipt(conn,rid,hashed,response):
    conn.execute('INSERT INTO v2_host_transfer_requests VALUES(?,?,?)',(rid,hashed,canonical(response)))
    return response


def require_id(value):
    if not isinstance(value,str) or not re.fullmatch(ID_PATTERN,value):
        raise TransferError('transfer_authorization_conflict')
    return value


def independent_admin(auth):
    return not auth.get('token_verified') and bool(auth.get('operator_authenticated') or
        auth.get('peer_loopback') and auth.get('local_admin_verified'))


def authorization_reader(conn,a,auth):
    pair=(auth.get('stream_id'),auth.get('session_generation'))
    if independent_admin(auth): return
    parts=seats(conn,a['command_id'])
    if not auth.get('token_verified') or pair not in {(a['actor'],a['actor_generation']),*((p['target'],p['target_generation']) for p in parts)}:
        raise TransferError('transfer_status_unauthorized')


def require_owner(a,auth):
    if not auth.get('token_verified') or (auth.get('stream_id'),auth.get('session_generation'))!=(a['actor'],a['actor_generation']):
        raise TransferError('transfer_fd_required')


def observe_authorization(conn,a,now):
    if now<expiry_value(a['last_observed_at']): raise TransferError('transfer_clock_regressed')
    if not expiry_value(a['issued_at'])<=now<expiry_value(a['expires_at']):
        raise TransferError('transfer_authorization_expired')
    if a['state'] in {'revoked','consumed','expired','exhausted'}:
        raise TransferError({'revoked':'transfer_authorization_revoked','consumed':'transfer_authorization_consumed',
                             'expired':'transfer_authorization_expired','exhausted':'transfer_attempt_limit'}[a['state']])
    a['last_observed_at']=timestamp(now)
    save_authorization(conn,a)


def check_authorization(conn,row,registry,*,now=None):
    now=time.time() if now is None else now
    a=authorization(conn,row['authorization_id'])
    observe_authorization(conn,a,now)
    if a['revision']!=row['authorization_revision']: raise TransferError('transfer_authorization_revision_conflict')
    if a['command_id']!=row['command_id'] or a['scope_digest']!=row['scope_digest']:
        raise TransferError('transfer_authorization_conflict')
    _credential(registry,a['operator'])
    return a


def checkpoint_required(row):
    # Every included helper also supplies a disposition pointer, so an empty
    # list never masquerades as a completed no-pending-work checkpoint.
    if not row.get('pointers') or any(not row['pointers'].get(sid) for sid in row['sources']):
        raise TransferError('transfer_checkpoint_required')


def validate_pointers(row,pointers,trusted_hosts):
    if not isinstance(pointers,dict) or set(pointers)!=set(row['sources']):
        raise TransferError('transfer_checkpoint_required')
    for refs in pointers.values():
        if not isinstance(refs,list) or not refs or len(refs)>16:
            raise TransferError('transfer_checkpoint_required')
        for r in refs:
            if (not isinstance(r,dict) or set(r)!={'host','path','sha256'} or not isinstance(r.get('host'),str) or r.get('host') not in trusted_hosts
                or not isinstance(r.get('path'),str) or not r['path'].startswith('/')
                or not re.fullmatch(r'[a-f0-9]{64}',str(r.get('sha256')))):
                raise TransferError('transfer_pointers_invalid')


def timestamp(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat().replace('+00:00','Z')


def expiry_value(value):
    try:
        t=datetime.fromisoformat(value.replace('Z','+00:00'))
        if t.tzinfo is None: raise ValueError()
        return t.timestamp()
    except (ValueError,TypeError,AttributeError):
        raise TransferError('transfer_expiry_invalid') from None


def authorize(conn,msg,auth,**context):
    rid,hashed,replay=request_identity(conn,msg,auth)
    if replay is not None: return replay
    aid=require_id(msg.get('authorization_id'))
    if conn.execute('SELECT 1 FROM v2_host_transfer_authorizations WHERE authorization_id=?',(aid,)).fetchone():
        raise TransferError('transfer_authorization_conflict')
    now=context.pop('now',None)
    now=time.time() if now is None else now
    base=claim(conn,{**msg,'pointers':{}},auth,**context,now=now,snapshot_only=True)
    expiry=base['admitted_at']+6*3600 if msg.get('expires_at') is None else expiry_value(msg['expires_at'])
    if not now<expiry<=base['admitted_at']+24*3600:
        raise TransferError('transfer_expiry_invalid')
    evidence=msg.get('extension_evidence')
    if evidence is not None or expiry>base['admitted_at']+6*3600:
        route=conn.execute('SELECT body FROM v2_assistant_composite_routes WHERE route_id=?',(base['route_id'],)).fetchone()
        body=route['body']
        if not isinstance(evidence,dict) or set(evidence)!={'body_start','body_end','quote','timezone','requested_expires_at'}:
            raise TransferError('transfer_expiry_evidence_required')
        start,end=evidence['body_start'],evidence['body_end']
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(evidence['timezone'])
            valid=type(start) is int and type(end) is int and 0<=start<end<=len(body)
            valid=valid and isinstance(evidence['quote'],str) and body[start:end]==evidence['quote']
            valid=valid and expiry_value(evidence['requested_expires_at'])==expiry
        except (ValueError,TypeError,ZoneInfoNotFoundError,TransferError):
            valid=False
        if not valid: raise TransferError('transfer_expiry_evidence_required')
    if evidence is not None:
        evidence={**evidence,'requested_expires_at':timestamp(expiry)}
    scope={'destination':base['destination'],'sources':base['sources'],'binding':base['binding'],'grant':base['grant'],
           'advisor':base['advisor'],'helpers':base['helpers'],'intent':base['intent']}
    a={'authorization_id':aid,'revision':1,'mode':STANDING_MODE,'state':'issued','command_id':base['command_id'],
       'composite':base['composite'],'actor':base['actor'],'actor_generation':base['actor_generation'],
       'operator':base['operator'],'dispatch_id':base['dispatch_id'],'input_identity':base['input_identity'],
       'input_digest':base['input_digest'],'original_input_at':timestamp(base['admitted_at']),'issued_at':timestamp(now),'expires_at':timestamp(expiry),
       'extension_evidence':evidence,'scope':scope,'scope_digest':digest(scope),'attempts_started':0,'max_attempts':3,
       'last_observed_at':timestamp(now),'revocation':None,'receipt':None,'scope_row':base}
    conn.execute('INSERT INTO v2_host_transfer_authorizations VALUES(?,?,?,?,?,?,?)',
                 (aid,a['command_id'],a['composite'],a['input_identity'],a['state'],expiry,canonical(a)))
    audit(conn,a['command_id'],'authorized',a['actor'],a['actor_generation'],authorization_id=aid,expires_at=expiry)
    return request_receipt(conn,rid,hashed,public_authorization(a))


def require_attempt(row,attempt_id):
    if not row.get('authorization_id') and attempt_id is not None:
        raise TransferError('transfer_action_invalid')
    if row.get('authorization_id') and row.get('attempt_id')!=attempt_id:
        raise TransferError('transfer_attempt_conflict')


def new_attempt(conn,a,row,*,resume_evidence=None):
    import uuid
    attempt_id=str(uuid.uuid4())
    a.update(revision=a['revision']+1,attempts_started=a['attempts_started']+1,state='reserved')
    save_authorization(conn,a)
    row.update(state='preparing',authorization_id=a['authorization_id'],authorization_revision=a['revision'],
               mode=STANDING_MODE,scope_digest=a['scope_digest'],expires_at=expiry_value(a['expires_at']),attempt_id=attempt_id,
               attempt_number=a['attempts_started'],cleanup={},cleanup_complete=False,last_error=None)
    if resume_evidence: row['resume_evidence']=resume_evidence
    conn.execute('DELETE FROM v2_host_transfer_seats WHERE command_id=?',(row['command_id'],))
    for sid,source in row['sources'].items():
        target=row['destination']+':v2-transfer-'+hashlib.sha256((row['command_id']+'\0'+attempt_id+'\0'+sid).encode()).hexdigest()[:16]
        if _seat_conn(conn,target): raise TransferError('transfer_target_conflict')
        conn.execute('INSERT INTO v2_host_transfer_seats(command_id,source,source_generation,target,attempt_id) VALUES(?,?,?,?,?)',
                     (row['command_id'],sid,source['session_generation'],target,attempt_id))
    return row


def start_authorized(conn,msg,auth,registry,trusted_hosts,*,now=None):
    rid,hashed,replay=request_identity(conn,msg,auth)
    if replay is not None: return replay
    a=authorization(conn,msg.get('authorization_id'));require_owner(a,auth)
    if msg.get('command_id')!=a['command_id']: raise TransferError('transfer_authorization_conflict')
    if type(msg.get('expected_revision')) is not int or msg['expected_revision']!=a['revision']: raise TransferError('transfer_authorization_revision_conflict')
    observe_authorization(conn,a,time.time() if now is None else now)
    if a['state']!='issued': raise TransferError('transfer_authorization_conflict')
    row=a['scope_row'].copy()
    for key in ('destination','members','dispatch_id'):
        if key in msg and (sorted(msg[key]) if key=='members' else msg[key])!=row[key]:
            raise TransferError('transfer_authorization_conflict')
    if row['destination'] not in trusted_hosts: raise TransferError('transfer_host_untrusted')
    pointers=msg.get('pointers')
    validate_pointers(row,pointers,trusted_hosts)
    row.update(pointers=pointers,pointer_manifest_digest=digest(pointers),scope_digest=a['scope_digest'],
               authorization_id=a['authorization_id'],authorization_revision=a['revision'],expires_at=expiry_value(a['expires_at']))
    check_source(conn,row,registry,now=now)
    row=new_attempt(conn,a,row)
    row['digest']=digest({'scope_digest':a['scope_digest'],'pointer_manifest_digest':row['pointer_manifest_digest']})
    conn.execute('INSERT INTO v2_host_transfers VALUES(?,?,?,?,?,?,?,?)',
                 (row['command_id'],row['composite'],row['input_identity'],row['dispatch_id'],row['digest'],row['state'],row['expires_at'],canonical(row)))
    audit(conn,row['command_id'],'attempt_started',a['actor'],a['actor_generation'],attempt_id=row['attempt_id'],attempt_number=1)
    return request_receipt(conn,rid,hashed,public_command(row))


def finalize_attempt(conn,row):
    if not row.get('authorization_id'): return
    record={'command_id':row['command_id'],'attempt_id':row['attempt_id'],'attempt_number':row['attempt_number'],
            'state':row['state'],'seats':seats(conn,row['command_id']),'failure_code':row.get('last_error'),
            'cleanup':row.get('cleanup',{}),'cleanup_complete':row.get('cleanup_complete',False),
            'resume_evidence':row.get('resume_evidence'),'receipt':row.get('receipt'),'finalized_at':stamp()}
    conn.execute('INSERT OR IGNORE INTO v2_host_transfer_attempts VALUES(?,?,?,?)',
                 (row['command_id'],row['attempt_number'],row['attempt_id'],canonical(record)))


def fail_attempt(conn,row,code):
    row['last_error']=code
    if row['state']=='preparing': row['state']='expired' if row['expires_at']<=time.time() else 'failed'
    save(conn,row)
    if row.get('authorization_id'):
        a=authorization(conn,row['authorization_id'])
        if a['state'] not in {'revoked','consumed','expired','exhausted'}:
            if row['state']=='expired': a.update(state='expired',revision=a['revision']+1)
            elif code not in RESUME_CODES or a['attempts_started']>=3: a.update(state='exhausted',revision=a['revision']+1)
            save_authorization(conn,a)


def independent_cleanup(conn,row):
    if not row.get('cleanup_complete'): raise TransferError('transfer_cleanup_unproved')
    evidence=[]
    for p in seats(conn,row['command_id']):
        host,name=p['target'].split(':',1)
        live=_seat_conn(conn,p['target'])
        pending=conn.execute('SELECT 1 FROM v2_stream_reservations WHERE host=? AND session_name=?',(host,name)).fetchone()
        outcome=conn.execute('SELECT request_id,state FROM v2_spawn_outcomes WHERE host=? AND session_name=?',(host,name)).fetchone()
        key='transfer:'+row['command_id']+':'+name
        if (pending or row['cleanup'].get(p['target']) not in {'closed','closed_or_not_created'}
            or live and (live['status']=='open' or p['target_generation'] is not None and live['session_generation']!=p['target_generation'])
            or outcome and (outcome['request_id']!=key or outcome['state'] not in {'done','failed','cancelled','ready'})):
            raise TransferError('transfer_cleanup_unproved')
        if live and p['target_generation'] is None and not outcome:
            raise TransferError('transfer_cleanup_unproved')
        evidence.append({'target':p['target'],'generation':p['target_generation'],'status':live['status'] if live else 'not_created',
                         'spawn':dict(outcome) if outcome else None,'reservation_absent':True})
    if not evidence: raise TransferError('transfer_cleanup_unproved')
    return evidence


def resume_authorized(conn,msg,auth,registry,trusted_hosts,*,now=None):
    rid,hashed,replay=request_identity(conn,msg,auth)
    if replay is not None: return replay
    a=authorization(conn,msg.get('authorization_id'));require_owner(a,auth)
    row=load(conn,a['command_id'])
    if msg.get('command_id')!=a['command_id']: raise TransferError('transfer_authorization_conflict')
    if type(msg.get('expected_revision')) is not int or msg['expected_revision']!=a['revision']: raise TransferError('transfer_authorization_revision_conflict')
    observe_authorization(conn,a,time.time() if now is None else now)
    if not row or type(msg.get('expected_attempt')) is not int or msg['expected_attempt']!=row['attempt_number']: raise TransferError('transfer_attempt_conflict')
    if a['attempts_started']>=3: raise TransferError('transfer_attempt_limit')
    if row['state']!='failed' or row.get('last_error') not in RESUME_CODES or row.get('receipt'):
        raise TransferError('transfer_resume_not_allowed')
    if conn.execute('SELECT 1 FROM v2_host_transfer_retirements WHERE request_id=?',(a['command_id'],)).fetchone():
        raise TransferError('transfer_resume_not_allowed')
    if row['destination'] not in trusted_hosts: raise TransferError('transfer_host_untrusted')
    cleanup=independent_cleanup(conn,row)
    _pending_effects(conn,row['sources'])
    # Reuse all current-source checks with an unchanged, preparing snapshot.
    check_source(conn,{**row,'state':'preparing','authorization_revision':a['revision']},registry,now=now)
    evidence={'failed_attempt_id':row['attempt_id'],'failure_code':row['last_error'],'request_id':rid,
              'cleanup':cleanup,'cleanup_digest':digest(cleanup)}
    finalize_attempt(conn,row)
    row=new_attempt(conn,a,row,resume_evidence=evidence)
    save(conn,row)
    audit(conn,row['command_id'],'resumed',a['actor'],a['actor_generation'],**evidence,next_attempt_id=row['attempt_id'])
    return request_receipt(conn,rid,hashed,public_command(row))


def revoke_authorization(conn,msg,auth):
    if not independent_admin(auth): raise TransferError('transfer_revoke_unauthorized')
    rid,hashed,replay=request_identity(conn,msg,auth)
    if replay is not None: return replay
    a=authorization(conn,msg.get('authorization_id'))
    if msg.get('composite_stream_id')!=a['composite']: raise TransferError('transfer_authorization_conflict')
    if a['state']=='consumed':
        request_receipt(conn,rid,hashed,{'_error':'transfer_authorization_consumed'})
        return {'_error':'transfer_authorization_consumed'}
    if type(msg.get('expected_revision')) is not int or msg['expected_revision']!=a['revision']: raise TransferError('transfer_authorization_revision_conflict')
    if not isinstance(msg.get('reason'),str) or not msg['reason'].strip(): raise TransferError('transfer_authorization_conflict')
    a.update(state='revoked',revision=a['revision']+1,revocation={'request_id':rid,'reason':msg['reason'],'at':stamp()})
    save_authorization(conn,a)
    row=load(conn,a['command_id'])
    if row and row['state'] not in COMMITTED:
        row.update(state='cancelled',last_error='transfer_authorization_revoked');save(conn,row)
    audit(conn,a['command_id'],'authorization_revoked',request_id=rid,reason=msg['reason'])
    return request_receipt(conn,rid,hashed,public_authorization(a))


def cancel_authorized(conn,row,auth,msg):
    rid,hashed,replay=request_identity(conn,msg,auth)
    if replay is not None: return replay
    if type(msg.get('expected_attempt')) is not int or msg['expected_attempt']!=row['attempt_number']: raise TransferError('transfer_attempt_conflict')
    a=authorization(conn,row['authorization_id'])
    if a['state']=='consumed': raise TransferError('transfer_authorization_consumed')
    a.update(state='revoked',revision=a['revision']+1,revocation={'request_id':rid,'reason':'cancelled','at':stamp()})
    save_authorization(conn,a)
    row['state']='cancelled';save(conn,row)
    audit(conn,row['command_id'],'cancelled',auth.get('stream_id'),auth.get('session_generation'),request_id=rid)
    return request_receipt(conn,rid,hashed,public_command(row))


def attempts(conn,cid):
    records=[json.loads(r[0]) for r in conn.execute('SELECT data FROM v2_host_transfer_attempts WHERE command_id=? ORDER BY attempt_number',(cid,))]
    for record in records:
        if record.get('receipt'): record['receipt']={k:v for k,v in record['receipt'].items() if k!='operator'}
    return records
