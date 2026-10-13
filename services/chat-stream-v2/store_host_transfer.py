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
        raise TransferError('transfer_dispatch_expired')


def _descendant(conn,sid,root):
    seen=set()
    while sid and sid not in seen:
        if sid==root:
            return True
        seen.add(sid)
        row=_seat_conn(conn,sid)
        sid=(row or {}).get('parent_stream_id')
    return False


def claim(conn,msg,auth,*,name,composite,env_binding,advisor,helpers,trusted_hosts,registry,now=None):
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
            if (not isinstance(ref,dict) or set(ref)!={'host','path','sha256'} or ref['host'] not in trusted_hosts
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
    if payload['destination'] not in trusted_hosts:
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
    if conn.execute('SELECT 1 FROM v2_host_transfers WHERE composite=? AND input_identity=?',
                    (composite,route['input_identity'])).fetchone():
        raise TransferError('transfer_input_consumed')
    for sid,source in row['sources'].items():
        if conn.execute('''SELECT 1 FROM v2_host_transfer_seats s JOIN v2_host_transfers t USING(command_id)
            WHERE s.source=? AND s.source_generation=? AND t.state='preparing' ''',
            (sid,source['session_generation'])).fetchone():
            raise TransferError('transfer_source_busy')
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
    _fresh(row,time.time() if now is None else now)
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


def prepared(conn,cid,target,generation):
    row=load(conn,cid)
    if not row or row['state']!='preparing': raise TransferError('transfer_not_preparing')
    participant=conn.execute('SELECT * FROM v2_host_transfer_seats WHERE command_id=? AND target=?',(cid,target)).fetchone()
    if not participant: raise TransferError('transfer_target_unknown')
    if participant['target_generation'] not in {None,generation}: raise TransferError('transfer_target_changed')
    live=_live(conn,target)
    if live['session_generation']!=generation or live.get('role')!='worker' or live.get('handoff_from_stream_id'):
        raise TransferError('transfer_preparation_not_inert')
    conn.execute('UPDATE v2_host_transfer_seats SET target_generation=? WHERE command_id=? AND target=?',(generation,cid,target))


def ready(conn,cid,auth,pointer_digest):
    row=load(conn,cid)
    if not row or row['state']!='preparing': raise TransferError('transfer_not_preparing')
    sid,gen=auth.get('stream_id'),auth.get('session_generation')
    part=conn.execute('SELECT * FROM v2_host_transfer_seats WHERE command_id=? AND target=?',(cid,sid)).fetchone()
    if auth.get('token_verified') and part and part['target_generation'] is None:
        prepared(conn,cid,sid,gen)
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


def cancel(conn,cid,auth):
    row=load(conn,cid)
    if not row: raise TransferError('transfer_unknown')
    admin=bool(auth.get('operator_authenticated') and not auth.get('token_verified')) or bool(auth.get('peer_loopback') and auth.get('local_admin_verified'))
    owner=auth.get('token_verified') and (auth.get('stream_id'),auth.get('session_generation'))==(row['actor'],row['actor_generation'])
    if not admin and not owner: raise TransferError('transfer_cancel_unauthorized')
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


def commit(conn,cid,registry,*,now=None):
    """Caller holds registry file lock and lifecycle/graph admission locks.

    All authoritative references and retirement fences linearize together.
    Notifications projection and physical cleanup happen after this commit.
    """
    row=load(conn,cid)
    if not row: raise TransferError('transfer_unknown')
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
    row['state']='committed'
    row['receipt']={'command_id':cid,'digest':row['digest'],'operator':row['operator'],
        'dispatch_id':row['dispatch_id'],'mapping':parts,'binding_revision':row['binding']['revision']+1,
        'grant_revision':grant['revision']+1,'committed_at':stamp()}
    save(conn,row)
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
    return conn.execute('SELECT command_id FROM v2_host_transfer_seats WHERE target=?',(sid,)).fetchone() is not None


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
