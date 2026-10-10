"""Synthetic-only portable conformance; no real provider or persistence effects."""
import copy
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import comm_contract as cc

FIXTURE = json.loads((pathlib.Path(__file__).parent / 'fixtures/comm_contract.json').read_text())
CONFIG = cc.ParserConfig(tuple(c['id'] for c in FIXTURE['contacts'] if c['email_default']))


def contact(lines, heading='SMS grant', identifier='fixture_person_01'):
    return {'id': identifier, 'heading': heading, 'lines': lines}


def parsed(lines):
    return cc.parse_sections([contact(lines)], CONFIG)[0]


@pytest.mark.parametrize('record', FIXTURE['contacts'], ids=lambda c: c['id'])
def test_cg1_legacy_23(record):
    row = cc.parse_sections([record], CONFIG)[0]
    assert row['valid'] is True
    assert len(row['grants']) == len(record['lines'])
    for line, grant in zip(record['lines'], row['grants']):
        original, handling = line[2:].split(': ', 1)
        mode, scope = original.split(' ')
        expected_mode = 'outreach' if mode == 'answer' and scope == 'legacy_outreach' else mode
        assert grant == dict(contact_id=record['id'], line_id=f"{record['id']}:{expected_mode}:{scope}",
                             mode=expected_mode, original_mode=mode, scope=scope, line=line,
                             handling=handling, channels=['sms', 'email'] if record['email_default'] else ['sms'],
                             voice='as_agent', autonomy='send', authority=None)
        # Independent old-shape admission oracle, not an implementation helper.
        assert (grant['mode'] != 'change') == (mode in {'answer', 'outreach'})


@pytest.mark.parametrize('line', FIXTURE['valid_qualifiers'])
def test_cg1_qualifiers_valid(line):
    assert parsed([line])['valid'] is True


@pytest.mark.parametrize('line', FIXTURE['invalid_lines'] + [
    '- answer topic [channels=sms,sms]: x', '- answer topic [channels=]: x',
    '- answer topic [autonomy=send;]: x', '- answer topic:x', '- answer topic: x [channels=sms]',
    '- answer topic [authority=2026-10-10 two ids]: x', '- answer topic [voice=as_agent] [autonomy=send]: x',
])
def test_cg1_qualifiers_invalid(line):
    # A qualifier-looking sequence in handling remains exact opaque content.
    if line == '- answer topic: x [channels=sms]':
        assert parsed([line])['grants'][0]['handling'] == 'x [channels=sms]'
    else:
        row = parsed(['- answer good: kept', line])
        assert row == dict(contact_id='fixture_person_01', valid=False, reason='malformed_grant', grants=[])


def test_cg1_qualifiers_defaults_and_bytes():
    row = parsed(['- answer topic [channels=email; voice=as_agent; autonomy=draft]:   café\r\n'])
    grant = row['grants'][0]
    assert grant['handling'] == '  café\r\n'
    assert grant['channels'] == ['email'] and grant['autonomy'] == 'draft'
    assert parsed(['- answer topic [authority=2026-10-10 fixture-card]: exact'])['valid']
    assert parsed(['- change topic [autonomy=send]: metadata'])['grants'][0]['mode'] == 'change'


@pytest.mark.parametrize('record', [
    contact(['- answer legacy_outreach: x', '- outreach legacy_outreach: y']),
    {'id':'fixture_person_01','sections':[{'heading':'SMS grant','lines':['- answer topic: x']},
                                      {'heading':'Communication grant','lines':['- answer topic2: y']}]},
    {'id':'fixture_person_01','sections':[{'heading':'SMS grant','lines':['- answer topic: x']},
                                      {'heading':'SMS grant','lines':['- answer topic2: y']}]},
    contact(['- answer topic: valid','bad']), contact(['- answer Topic: invalid']),
])
def test_cg1_contact_invalid(record):
    row = cc.parse_sections([record], CONFIG)[0]
    assert row['valid'] is False and row['grants'] == []


def test_cg1_contact_valid_heading_and_config():
    assert cc.parse_sections([contact(['- answer topic: x'], 'Communication grant')],CONFIG)[0]['valid']
    for cfg in [None, {'default_voice_token':'as_agent'}, cc.ParserConfig(default_voice_token='unknown'),
                cc.ParserConfig(legacy_outreach_scope='Bad'), cc.ParserConfig(('duplicate','duplicate'))]:
        assert not cc.parse_sections([contact(['- answer topic: x'])],cfg)[0]['valid']
    assert all(not r['valid'] for r in cc.parse_sections([contact(['- answer topic: x'])]*2,CONFIG))

class Authority:
    """Owner-double: only issued opaque refs resolve; data flags do not count."""
    def __init__(self):
        self.records = {}
        self.serial = 0

    def issue(self, kind, data, subject='fixture-request', version='v1',
              issued='2026-10-10T00:00:00Z', expires='2026-10-12T00:00:00Z'):
        self.serial += 1
        reference = f'fixture-proof-{self.serial}'
        self.records[reference] = cc.Evidence(kind,subject,version,issued,expires,
                                            json.dumps(data,sort_keys=True,separators=(',',':')))
        return reference

    def resolve(self, reference, kind):
        record = self.records.get(reference)
        return record if record and record.kind == kind else None


def envelope(channel='email'):
    value=copy.deepcopy(FIXTURE['envelope']);value['channel']=channel
    value['introduction']='Synthetic assistant: ' if channel=='sms' else ''
    return value


def context(env=None, line='- answer topic [channels=sms,email,botcomm]: Synthetic handling.'):
    env = env or envelope()
    auth = Authority()
    cfg = dict(email_default_contacts=[],default_voice_token='as_agent',legacy_outreach_scope='legacy_outreach')
    contacts = [contact([line])]
    grants = cc.parse_sections(contacts, cc.ParserConfig())
    data=copy.deepcopy(FIXTURE['evidence_templates'])
    data['identity'].update(account_ref=env['account_ref'],sender_ref=env['sender_ref'])
    template=next(iter(data['identity']['recipients'].values()))
    data['identity']['recipients']={r['destination']:dict(template,contact_ids=[r['contact_id']]) for r in env['recipients']}
    data['source'].update(contacts=contacts,config=cfg)
    refs = tuple((kind,auth.issue(kind,value)) for kind,value in data.items())
    return cc.TrustedContext(auth,'2026-10-10T12:00:01Z',refs),grants


def change_evidence(ctx,kind,**changes):
    old = ctx.resolver.resolve(ctx.reference(kind),kind)
    payload = old.data
    payload.update(changes)
    ref=ctx.resolver.issue(kind,payload,old.subject,old.version,old.issued_at,old.expires_at)
    return cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,ref if k==kind else r) for k,r in ctx.proofs))


def mutate_env(component):
    return copy.deepcopy(FIXTURE['envelope_mutations'][component])


MUTATIONS = ['channel','account_ref','sender_ref','recipient_contact_id','recipient_role','recipient_destination',
 'subject','body','text','html','newline','whitespace','unicode','introduction','mime_text','mime_base64',
 'mime_content_type','mime_order','protected_refs','thread_id','reply_id','references','attachment_sha256',
 'attachment_size','attachment_media_type','attachment_name_ref','attachment_content_ref','attachment_order',
 'voice','voice_config_version','voice_authority_ref']


@pytest.mark.parametrize('component',MUTATIONS)
def test_cg2_envelope(component):
    before = cc.bind_envelope(envelope())
    after = cc.bind_envelope(mutate_env(component))
    assert before.digest != after.digest
    assert cc.envelope_digest(before) == before.digest


def test_cg2_envelope_normalization():
    e=envelope();e['recipients'] += [dict(contact_id='fixture_person_02',role='to',destination='person02@fixture.invalid')]
    a=cc.bind_envelope(e);e['recipients'].reverse();e['recipients']+=e['recipients'][:1]
    assert cc.bind_envelope(e).digest==a.digest
    e['recipients'][0]['role']='bcc'
    assert cc.bind_envelope(e).digest != a.digest
    e=envelope();e['body']='café';a=cc.bind_envelope(e);e['body']='cafe\u0301'
    assert cc.bind_envelope(e).digest!=a.digest
    assert 'café' in a.logical_json


@pytest.mark.parametrize('mutation', ['unknown','nan','bad_hash','bad_size','bad_b64','missing','role','bool_version'])
def test_cg2_envelope_invalid(mutation):
    e=envelope()
    if mutation=='unknown': e['unknown']='x'
    elif mutation=='nan':e['body']=float('nan')
    elif mutation=='bad_hash': e['attachments'][0]['sha256']='bad'
    elif mutation=='bad_size': e['attachments'][0]['size']=True
    elif mutation=='bad_b64': e['mime_parts'][1]['base64']='***'
    elif mutation=='missing':e.pop('sender_ref')
    elif mutation=='role':e['recipients'][0]['role']='unknown'
    elif mutation=='bool_version':e['schema_version']=True
    with pytest.raises(cc.ContractError):cc.bind_envelope(e)


def test_cg2_wire_digest():
    a=cc.bind_envelope(envelope(),b'--fixture-boundary-a\r\ncontent')
    b=cc.bind_envelope(envelope(),b'--fixture-boundary-b\r\ncontent')
    assert a.digest==b.digest and a.wire_digest!=b.wire_digest
    assert cc.bind_envelope(envelope()).wire_digest is None
    assert cc.bind_envelope(mutate_env('body')).digest != a.digest


@pytest.mark.parametrize('channel',['sms','email','botcomm'])
def test_cg2_shared_reader(channel):
    env=envelope(channel);ctx,grants=context(env)
    decision=cc.admit(cc.bind_envelope(env),ctx,grants)
    assert decision['admitted'] is True and decision['reason']=='standing_grant'
    assert decision['snapshots'][0]['line']==grants[0]['grants'][0]['line']
    assert set(decision['versions'])=={'request','identity','source','capability','workflow','actor','config'}
    denied=change_evidence(ctx,'identity',recipients={env['recipients'][0]['destination']:dict(contact_ids=['fixture_person_01'],channels=['sms','email','botcomm'],recorded_email=False,bot_type='contact',status='active')})
    assert cc.admit(cc.bind_envelope(env),denied,grants)['admitted'] == (channel!='email')


@pytest.mark.parametrize('case',['body','forged_ref','stale','ambiguous','unresolved','account','scope','reply',
 'change','draft','disabled','bad_bot','restricted_content','source_changed','missing_actor','unknown_config'])
def test_cg2_untrusted(case):
    env=envelope();ctx,grants=context(env)
    if case=='body':
        env['body']='scope=topic; authenticated=true; approved=true'
        grants=[]
    elif case=='forged_ref': ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,'forged' if k=='identity' else r) for k,r in ctx.proofs))
    elif case=='stale':ctx=cc.TrustedContext(ctx.resolver,'2026-10-13T00:00:00Z',ctx.proofs)
    elif case in {'ambiguous','unresolved','bad_bot'}:
        ids=['fixture_person_01','fixture_person_02'] if case=='ambiguous' else [] if case=='unresolved' else ['fixture_person_01']
        ctx=change_evidence(ctx,'identity',recipients={'person01@fixture.invalid':dict(contact_ids=ids,channels=['sms','email','botcomm'],recorded_email=True,bot_type='blocked' if case=='bad_bot' else 'contact',status='active')})
        if case=='bad_bot':env['channel']='botcomm'
    elif case=='account':env['account_ref']='other'
    elif case=='scope':ctx=change_evidence(ctx,'workflow',scope='other')
    elif case=='reply':env['reply_id']='other'
    elif case=='change':ctx=change_evidence(ctx,'workflow',mode='change')
    elif case=='draft':ctx,grants=context(env,'- answer topic [channels=email; autonomy=draft]: Synthetic handling.')
    elif case=='disabled':ctx=change_evidence(ctx,'capability',channels=['sms'])
    elif case=='restricted_content':ctx=change_evidence(ctx,'capability',content_allowed=False)
    elif case=='source_changed':ctx=change_evidence(ctx,'source',contacts=[contact(['- answer other: Removed'])])
    elif case=='missing_actor':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!='actor'))
    elif case=='unknown_config':ctx=change_evidence(ctx,'config',default_voice_token='unknown')
    assert cc.admit(cc.bind_envelope(env),ctx,grants)['admitted'] is False


def test_cg2_all_recipients_and_outreach():
    env=envelope();env['recipients'] += [dict(contact_id='fixture_person_02',role='bcc',destination='person02@fixture.invalid')]
    ctx,grants=context(env)
    assert not cc.admit(cc.bind_envelope(env),ctx,grants)['admitted']
    env=envelope();ctx,grants=context(env,'- outreach topic [channels=email]: Synthetic handling.')
    ctx=change_evidence(ctx,'workflow',mode='outreach',dispatch_ref='fixture-dispatch')
    assert cc.admit(cc.bind_envelope(env),ctx,grants)['admitted']
    assert not cc.admit(cc.bind_envelope(env),change_evidence(ctx,'workflow',dispatch_ref=None),grants)['admitted']


def add_evidence(ctx,kind,data,version='v1',subject='fixture-request'):
    ref=ctx.resolver.issue(kind,data,subject,version)
    return cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!=kind)+((kind,ref),))


def draft_context(env=None,line='- answer topic [channels=email; autonomy=draft]: Synthetic handling.'):
    env=env or envelope();ctx,grants=context(env,line)
    ctx=add_evidence(ctx,'persistence',dict(status='available',restricted=True,atomic=True,policy_version=1))
    bound=cc.bind_envelope(env)
    ctx=add_evidence(ctx,'protected',dict(access='allowed',envelope_digest=bound.digest,content_ref='fixture-content',subject_ref='fixture-subject',snapshot_ref='fixture-snapshot',
        attachment_refs=[a['content_ref'] for a in env['attachments']],recipient_refs=[dict(contact_id=r['contact_id'],role=r['role'],destination_ref=f'fixture-address-{i}') for i,r in enumerate(bound.data['recipients'])],
        immutable_snapshot=True,retention='owner_enforced',source_expires_at=None))
    decision=cc.admit(bound,ctx,grants)
    return bound,ctx,grants,decision


def saved_draft(bound,ctx,decision):
    plan=cc.draft_command(bound,ctx,decision)
    assert plan['status']=='draft_pending'
    assert [c.kind for c in plan['commands']]==['put_draft_and_handoff']
    draft=plan['draft']
    ctx=add_evidence(ctx,'draft',dict(draft,committed_command_id=plan['commands'][0].command_id))
    return ctx,draft


def operator_event(bound,ctx,draft,action='approve'):
    data=dict(event_id='fixture-event',origin='operator',original=True,occurred_at=ctx.now,action=action,
              draft_id=draft['draft_id'],draft_version=draft['version'],envelope_digest=bound.digest,
              channel=bound.data['channel'],account_ref=bound.data['account_ref'],recipients=bound.data['recipients'],
              versions=draft['versions'],actor_ref='fixture-operator')
    ctx=add_evidence(ctx,'approval_state',dict(draft_id=draft['draft_id'],draft_version=draft['version'],status='absent'))
    return add_evidence(ctx,'operator_event',data)


def approved_context(bound,ctx,draft):
    ctx=operator_event(bound,ctx,draft)
    plan=cc.approval_binding(bound,ctx,draft)
    assert plan['bound'] and [c.kind for c in plan['commands']]==['bind_approval']
    ctx=add_evidence(ctx,'approval',dict(status='available',binding=plan['binding'],
                        committed_command_id=plan['commands'][0].command_id),version='v2')
    ctx=add_evidence(ctx,'approval_state',dict(status='available',draft_id=draft['draft_id'],draft_version=draft['version'],
                        approval_id=plan['binding']['approval_id']),version='v2')
    return ctx,plan['binding']


def test_cg3_draft_replay():
    bound,ctx,grants,decision=draft_context()
    first=cc.draft_command(bound,ctx,decision)
    second=cc.draft_command(bound,ctx,decision)
    assert first==second and first['status']=='draft_pending'
    assert len(first['commands'])==1 and first['commands'][0].kind=='put_draft_and_handoff'
    ctx,draft=saved_draft(bound,ctx,decision)
    handoff=cc.draft_command(bound,ctx,decision)
    assert [c.kind for c in handoff['commands']]==['request_handoff']
    unavailable=add_evidence(ctx,'handoff',dict(handoff_id=draft['handoff_id'],status='unavailable'))
    assert cc.draft_command(bound,unavailable,decision)==handoff
    acknowledged=add_evidence(ctx,'handoff',dict(handoff_id=draft['handoff_id'],status='acknowledged',
                                              committed_command_id=handoff['commands'][0].command_id))
    assert cc.draft_command(bound,acknowledged,decision)['commands']==[]


@pytest.mark.parametrize('status',['missing','unavailable','stage_failed','stage_stored'])
def test_cg3_draft_replay_failure(status):
    bound,ctx,grants,decision=draft_context()
    if status=='missing':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!='persistence'))
    else:ctx=change_evidence(ctx,'persistence',status='unavailable')
    plan=cc.draft_command(bound,ctx,decision)
    if status=='missing':
        assert plan['status']=='held' and plan['commands']==[]
    else:
        assert [c.kind for c in plan['commands']]==['stage_draft']
        assert plan['commands'][0].body['restricted'] is True and plan['commands'][0].body['atomic'] is True
        if status.startswith('stage_'):
            ctx=add_evidence(ctx,'staging',dict(draft_id=plan['draft']['draft_id'],envelope_digest=bound.digest,
                version=1,status='failed' if status=='stage_failed' else 'stored',restricted=True,atomic=True,
                committed_command_id=plan['commands'][0].command_id))
            retry=cc.draft_command(bound,ctx,decision)
            assert retry['status']=='held' and retry['commands']==[]
            assert retry['reason']==('staging_failed' if status=='stage_failed' else 'staged_unadmitted')
            if status=='stage_stored':
                recovered=change_evidence(ctx,'persistence',status='available')
                assert [c.kind for c in cc.draft_command(bound,recovered,decision)['commands']]==['put_draft_and_handoff']


def test_cg3_approval():
    bound,ctx,grants,decision=draft_context();ctx,draft=saved_draft(bound,ctx,decision)
    ctx=operator_event(bound,ctx,draft)
    first=cc.approval_binding(bound,ctx,draft)
    assert first['bound'] and first==cc.approval_binding(bound,ctx,draft)
    approved,binding=approved_context(bound,ctx,draft)
    admitted=cc.admit(bound,approved,grants,approval=approved.reference('approval'))
    assert admitted['admitted'] and admitted['reason']=='one_shot'
    assert binding['voice']=='as_agent' and binding['envelope_digest']==bound.digest
    for status in ['canceled','revoked','consumed']:
        canceled=change_evidence(approved,'approval',status=status)
        assert not cc.admit(bound,canceled,grants,approval=canceled.reference('approval'))['admitted']
        assert not cc.approval_binding(bound,canceled,draft)['bound']
    disabled=change_evidence(approved,'capability',channels=[])
    assert not cc.admit(bound,disabled,grants,approval=disabled.reference('approval'))['admitted']


@pytest.mark.parametrize('component',MUTATIONS)
def test_cg3_approval_edits(component):
    bound,ctx,grants,decision=draft_context();ctx,draft=saved_draft(bound,ctx,decision)
    ctx=operator_event(bound,ctx,draft)
    assert not cc.approval_binding(cc.bind_envelope(mutate_env(component)),ctx,draft)['bound']


@pytest.mark.parametrize('failure',['forged','inbound','copied','stale_draft','stale_versions','cancel','revoke','bad_date','future','no_ack'])
def test_cg3_approval_restrictions(failure):
    bound,ctx,grants,decision=draft_context();ctx,draft=saved_draft(bound,ctx,decision);ctx=operator_event(bound,ctx,draft)
    if failure=='forged':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,'forged' if k=='operator_event' else r) for k,r in ctx.proofs))
    elif failure=='inbound':ctx=change_evidence(ctx,'operator_event',origin='inbound')
    elif failure=='copied':ctx=change_evidence(ctx,'operator_event',original=False)
    elif failure=='stale_draft':draft=dict(draft,version=2)
    elif failure=='stale_versions':ctx=change_evidence(ctx,'operator_event',versions={})
    elif failure in {'cancel','revoke'}:ctx=change_evidence(ctx,'operator_event',action=failure)
    elif failure=='bad_date':ctx=change_evidence(ctx,'operator_event',occurred_at='2026-02-30T12:00:00Z')
    elif failure=='future':ctx=change_evidence(ctx,'operator_event',occurred_at='2026-10-11T12:00:00Z')
    elif failure=='no_ack':ctx=change_evidence(ctx,'draft',committed_command_id=None)
    result=cc.approval_binding(bound,ctx,draft)
    assert not result['bound']
    if failure in {'cancel','revoke'}:
        assert [c.kind for c in result['commands']]==['cancel_approval']

import threading
from concurrent.futures import ThreadPoolExecutor


def lifecycle_context(env=None,line='- answer topic [channels=email]: Synthetic handling.'):
    env=env or envelope();ctx,grants=context(env,line);bound=cc.bind_envelope(env)
    versions={k:ctx.resolver.resolve(r,k).version for k,r in ctx.proofs}
    extras={
        'persistence':dict(status='available',restricted=True,atomic=True,policy_version=1),
        'preview':dict(policy_version=1,fields=['channel','direction','state','attachment_count']),
        'replay':dict(status='new',request_id='fixture-request',issuer_ref=ctx.reference('request'),canonical_key=None,envelope_digest=None),
        'publication':dict(status='committed',transaction_ref='fixture-version-transaction',versions=versions,pending=[],tombstones=[]),
        'protected':dict(access='allowed',envelope_digest=bound.digest,content_ref='fixture-content',subject_ref='fixture-subject',
                         snapshot_ref='fixture-snapshot',attachment_refs=[a['content_ref'] for a in env['attachments']],
                         recipient_refs=[dict(contact_id=r['contact_id'],role=r['role'],destination_ref=f'fixture-address-{i}') for i,r in enumerate(bound.data['recipients'])],
                         immutable_snapshot=True,retention='owner_enforced',source_expires_at='2026-10-11T00:00:00Z'),
        'clock':dict(now=ctx.now,source='trusted_utc'),
        'attempt_control':dict(status='open',envelope_digest=bound.digest,transaction_ref='fixture-attempt-control'),
    }
    for k,v in extras.items():ctx=add_evidence(ctx,k,v)
    versions.update({k:ctx.resolver.resolve(ctx.reference(k),k).version for k in cc._ENFORCEMENT_KINDS})
    ctx=change_evidence(ctx,'publication',versions=versions)
    return bound,ctx,grants


class Store:
    """One in-memory logical table with atomic CAS and authenticated readbacks."""
    def __init__(self):
        self.lock=threading.Lock();self.records={};self.requests={};self.associations={};self.snapshots={};self.attempts={}
        self.drafts={};self.handoffs={};self.stages={};self.cleanups={};self.handoff_attempts=[];self.stage_available=True;self.handoff_available=True;self.delete_available=True
        self.creations={};self.claims={};self.approvals={};self.approval_versions={};self.versions={};self.publication_version='v1';self.trace=[];self.prior_history=None;self.closed=False

    @property
    def observed_trace(self):
        return list(self.prior_history or [])+self.trace

    def apply(self,command):
        with self.lock:
            body,condition=command.body,command.conditions
            if command.kind=='put_draft_and_handoff':
                rid=command.request_id;old=self.drafts.get(rid)
                if old==body:return True
                if old and (old['version']!=condition['expected_prior_version'] or old['envelope_digest']!=condition['previous_digest']):return False
                if body['envelope_json'] is not None and body['source_expires_at'] is not None and cc._instant(body['source_expires_at'])<=cc._instant(getattr(self,'now','2026-10-10T12:00:01Z')):return False
                self.drafts[rid]=copy.deepcopy(body);self.handoffs.setdefault(body['handoff_id'],dict(status='pending'))
                self.trace.extend(['storage:draft_committed','handoff:pending']);return True
            if command.kind=='request_handoff':
                row=self.drafts.get(command.request_id)
                if row is None or row['version']!=body['version'] or cc._draft_write(row).command_id!=condition['draft_committed']:return False
                self.handoff_attempts.append(body['handoff_id'])
                if self.handoffs[body['handoff_id']]['status']=='acknowledged':return True
                if not self.handoff_available:self.trace.append('handoff:unavailable');return False
                self.handoffs[body['handoff_id']]=dict(status='acknowledged',committed_command_id=command.command_id)
                self.trace.append('prompt:handoff');return True
            if command.kind=='stage_draft':
                if not self.stage_available:self.trace.append('storage:stage_failed');return False
                if body.get('restricted') is not True or body.get('atomic') is not True:return False
                draft=body['draft'];old=self.stages.get(command.request_id)
                if draft['envelope_json'] is not None and draft['source_expires_at'] is not None and cc._instant(draft['source_expires_at'])<=cc._instant(getattr(self,'now','2026-10-10T12:00:01Z')):return False
                if old and old['draft']!=draft:return False
                if not old:self.stages[command.request_id]=dict(draft=copy.deepcopy(draft),command_id=command.command_id,protected_ref='fixture-stage');self.trace.append('storage:staged')
                return True
            if command.kind=='delete_staging':
                rid=command.request_id;row=self.records.get(rid);stage=self.stages.get(rid)
                if rid in self.cleanups:return self.cleanups[rid]==command.command_id
                if row is None or stage is None or row['envelope_digest']!=condition['envelope_digest'] or [row['pk'],row['sk']]!=condition['canonical_key'] or stage['protected_ref']!=body['protected_ref']:return False
                if not self.delete_available:self.trace.append('cleanup:failed');return False
                self.stages.pop(rid);self.cleanups[rid]=command.command_id;self.trace.append('cleanup:deleted');return True
            if command.kind=='cancel_intent':
                rid=command.request_id;row=self.records.get(rid)
                if row is None or row['state']!='ready' or row['revision']!=condition['canonical_revision'] or self.versions.get('attempt_control')!=condition['attempt_control_version']:return False
                self.records[rid]=copy.deepcopy(body['record']);self.trace.append('cancel:committed');return True
            if command.kind=='create_intent':
                row=body['canonical'];rid=row['request_id']
                if rid in self.requests:
                    return self.requests[rid]['envelope_digest']==row['envelope_digest']
                if self.versions!=condition['versions'] or self.publication_version!=condition['publication_version']:return False
                self.records[rid]=copy.deepcopy(row);self.requests[rid]=copy.deepcopy(body['request_pointer'])
                self.creations[rid]=command;self.snapshots[row['snapshot_ref']]=copy.deepcopy(body['protected_snapshot'])
                for a in body['associations']:self.associations[(a['pk'],a['sk'])]=a
                self.trace.append('storage:intent_committed');return True
            if command.kind=='claim_attempt':
                rid=command.request_id;old=self.records.get(rid)
                if not old or old['revision']!=condition['canonical_revision'] or old['state']!=condition['state'] or self.versions!=condition['versions'] or condition['request_pointer']!=self.requests[rid]['canonical_key'] or self.publication_version!=condition['publication_version']:
                    return False
                approval=condition.get('approval_id')
                if approval and (self.approvals.get(approval)!='available' or self.approval_versions.get(approval)!=condition['approval_version']):return False
                if approval:self.approvals[approval]='consumed'
                self.records[rid]=copy.deepcopy(body['record']);self.claims[rid]=command;self.attempts[body['attempt']['attempt_id']]=copy.deepcopy(body['attempt'])
                self.trace.append('storage:claim_committed');return True
            if command.kind in {'record_result','reconcile_receipt'}:
                rid=command.request_id;old=self.records[rid]
                if old['revision']!=condition['canonical_revision']:return False
                self.records[rid]=copy.deepcopy(body['record'])
                if command.kind=='record_result':self.attempts[body['record']['attempt_id']].update(body['attempt_result'])
                self.trace.append('receipt:'+command.kind);return True
            if command.kind=='publish_version':
                kind=body['kind']
                if self.versions.get(kind)!=condition['previous_version']:return False
                self.versions[kind]=body['version'];self.trace.append('storage:version_committed');return True
            raise AssertionError(command.kind)

    def view(self,ctx):
        rid='fixture-request';row=self.records[rid]
        create=self.creations[rid]
        ctx=add_evidence(ctx,'intent',dict(record=row,creation=dict(kind=create.kind,request_id=create.request_id,
                           body_json=create.body_json,conditions_json=create.conditions_json),
                           committed_command_id=create.command_id,transaction_ref='fixture-create-transaction'))
        ctx=change_evidence(ctx,'replay',status='active',canonical_key=[row['pk'],row['sk']],envelope_digest=row['envelope_digest'])
        if rid in self.claims:
            claim=self.claims[rid]
            ctx=add_evidence(ctx,'claim',dict(command=dict(kind=claim.kind,request_id=claim.request_id,body_json=claim.body_json,
                  conditions_json=claim.conditions_json),committed_command_id=claim.command_id,transaction_ref='fixture-claim-transaction'))
        return ctx,copy.deepcopy(row)

    def draft_view(self,ctx):
        row=self.drafts['fixture-request'];ctx=add_evidence(ctx,'draft',dict(row,committed_command_id=cc._draft_write(row).command_id))
        handoff=self.handoffs[row['handoff_id']]
        if handoff['status']=='acknowledged':ctx=add_evidence(ctx,'handoff',dict(handoff,handoff_id=row['handoff_id']))
        return ctx

    def stage_view(self,ctx):
        stage=self.stages['fixture-request'];row=stage['draft']
        return add_evidence(ctx,'staging',dict(status='stored',draft_id=row['draft_id'],version=row['version'],envelope_digest=row['envelope_digest'],restricted=True,atomic=True,protected_ref=stage['protected_ref'],committed_command_id=stage['command_id']))

    def close(self):
        self.drafts.clear();self.handoffs.clear();self.stages.clear();self.cleanups.clear();self.handoff_attempts.clear()
        self.records.clear();self.requests.clear();self.associations.clear();self.snapshots.clear();self.attempts.clear()
        if hasattr(self,'dispatched'):self.dispatched.clear();self.activations.clear();self.live_invocations.clear()
        self.creations.clear();self.claims.clear();self.approvals.clear();self.approval_versions.clear();self.prior_history=None;self.closed=True


def stored_intent():
    bound,ctx,grants=lifecycle_context();store=Store()
    store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
    plan=cc.create_intent(bound,ctx,grants)
    assert plan['status']=='intent_pending' and len(plan['commands'])==1
    assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
    return bound,ctx,grants,store,row


def claimed_intent():
    bound,ctx,grants,store,row=stored_intent()
    plan=cc.claim_attempt(row,bound,ctx,grants)
    assert plan['status']=='claim_pending' and store.apply(plan['commands'][0])
    ctx,row=store.view(ctx)
    return bound,ctx,grants,store,row


def provider(ctx,row,outcome='accepted',delivery='pending',at='2026-10-10T12:00:02.000000001Z'):
    # The owner-observed provider result is evidence, never a naked return boolean.
    ctx=cc.TrustedContext(ctx.resolver,at,ctx.proofs)
    ctx=change_evidence(ctx,'clock',now=at)
    return add_evidence(ctx,'provider_result',dict(attempt_id=row['attempt_id'],envelope_digest=row['envelope_digest'],
       outcome=outcome,provider_message_id='fixture-provider-message' if outcome=='accepted' else None,
       occurred_at=at,delivery_state=delivery))


def accepted_intent(delivery='pending'):
    bound,ctx,grants,store,row=claimed_intent();ctx=provider(ctx,row,delivery=delivery)
    plan=cc.record_result(row,ctx);assert plan['status']=='result_pending' and store.apply(plan['commands'][0])
    ctx,row=store.view(ctx);return bound,ctx,grants,store,row


def test_cg4_request_and_concurrency():
    bound,ctx,grants=lifecycle_context();store=Store()
    store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
    plans=[cc.create_intent(bound,ctx,grants) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(store.apply,[p['commands'][0] for p in plans]))
    assert len(store.records)==len(store.requests)==len(store.snapshots)==1
    ctx,row=store.view(ctx)
    assert row['pk']=='CONTACT#fixture_person_01' and row['sk']=='SEND#2026-10-10T12:00:00.000000001Z#fixture-send'
    assert row['state']=='ready' and row['delivery_state']=='not_attempted'
    assert cc.create_intent(bound,ctx,grants)['status']=='existing'
    assert cc.create_intent(cc.bind_envelope(mutate_env('body')),ctx,grants)['status']=='held'
    plans=[cc.claim_attempt(row,bound,ctx,grants) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(store.apply,[p['commands'][0] for p in plans]))
    assert sum(results)==1 and store.trace.count('storage:claim_committed')==1
    store.close();assert store.closed and not store.records


@pytest.mark.parametrize('kind',['request','identity','source','capability','workflow','actor','config','persistence',
 'preview','replay','publication','protected','clock','attempt_control'])
@pytest.mark.parametrize('failure',['missing','forged','stale','wrong_subject'])
def test_cg4_external_inputs(kind,failure):
    bound,ctx,grants=lifecycle_context()
    if failure=='missing':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!=kind))
    elif failure=='forged':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,'forged' if k==kind else r) for k,r in ctx.proofs))
    else:
        old=ctx.resolver.resolve(ctx.reference(kind),kind)
        ref=ctx.resolver.issue(kind,old.data,'wrong-subject' if failure=='wrong_subject' else old.subject,old.version,
              expires='2026-10-09T00:00:00Z' if failure=='stale' else old.expires_at)
        ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,ref if k==kind else r) for k,r in ctx.proofs))
    result=cc.create_intent(bound,ctx,grants)
    assert result['status']=='held' and result['commands']==[]


@pytest.mark.parametrize('failure',['source_removed','source_edited','capability','approval','pending','tombstone','unacknowledged','race'])
def test_cg4_versions(failure):
    bound,ctx,grants,store,row=stored_intent()
    if failure=='source_removed':ctx=change_evidence(ctx,'source',contacts=[])
    elif failure=='source_edited':ctx=change_evidence(ctx,'source',contacts=[contact(['- answer other: Changed'])])
    elif failure=='capability':ctx=change_evidence(ctx,'capability',channels=[])
    elif failure=='approval':ctx=change_evidence(ctx,'publication',versions={})
    elif failure=='pending':ctx=change_evidence(ctx,'publication',pending=['source'])
    elif failure=='tombstone':ctx=change_evidence(ctx,'publication',tombstones=['source'])
    elif failure=='unacknowledged':ctx=change_evidence(ctx,'publication',status='pending')
    plan=cc.claim_attempt(row,bound,ctx,grants)
    if failure=='race':
        assert plan['status']=='claim_pending';store.versions['source']='v2';assert not store.apply(plan['commands'][0])
    else:assert plan['status']=='held' and plan['commands']==[]
    assert 'storage:claim_committed' not in store.trace
    store.close()


def test_cg4_versions_publication():
    bound,ctx,grants=lifecycle_context()
    source=ctx.resolver.resolve(ctx.reference('source'),'source')
    newref=ctx.resolver.issue('source',source.data,version='v2')
    ctx=add_evidence(ctx,'refresh',dict(kind='source',previous_version='v1',version='v2',target_ref=newref,status='validated',tombstone=False))
    plan=cc.publish_version(ctx,'source')
    assert plan['status']=='publication_pending' and [c.kind for c in plan['commands']]==['publish_version']
    store=Store();store.versions={'source':'v1'};assert store.apply(plan['commands'][0]);assert store.versions['source']=='v2'
    assert cc.publish_version(change_evidence(ctx,'refresh',status='pending'),'source')['status']=='held'
    store.close()


@pytest.mark.parametrize('crash',['before_claim','after_claim','timeout','ambiguous','receipt_write_failure','rejected'])
def test_cg4_crashes(crash):
    bound,ctx,grants,store,row=stored_intent();effects=0
    if crash=='before_claim':
        assert row['state']=='ready' and effects==0
    else:
        plan=cc.claim_attempt(row,bound,ctx,grants);assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
        effects+=1
        if crash!='after_claim':
            ctx=provider(ctx,row,{'timeout':'timeout','ambiguous':'ambiguous','receipt_write_failure':'accepted','rejected':'rejected'}[crash])
            plan=cc.record_result(row,ctx)
            if crash!='receipt_write_failure':assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
        assert cc.claim_attempt(row,bound,ctx,grants)['status']=='held'
        if crash in {'timeout','ambiguous'}:assert row['state']=='acceptance_unknown'
        if crash=='rejected':assert row['state']=='rejected'
    assert effects<=1
    store.close()


def receipt(ctx,row,delivery='delivered',at='2026-10-10T12:00:03.000000001Z',identifier='fixture-receipt'):
    now=at if cc._instant(at)>cc._instant(ctx.now) else ctx.now
    ctx=cc.TrustedContext(ctx.resolver,now,ctx.proofs);ctx=change_evidence(ctx,'clock',now=now)
    return add_evidence(ctx,'receipt',dict(receipt_id=identifier,attempt_id=row['attempt_id'],
        provider_message_id='fixture-provider-message',envelope_digest=row['envelope_digest'],
        acceptance='accepted',delivery_state=delivery,occurred_at=at))


def test_cg4_receipts():
    bound,ctx,grants,store,row=accepted_intent()
    assert row['state']=='accepted' and row['delivery_state']=='pending' and row['terminal_at'] is None
    ctx=receipt(ctx,row);plan=cc.reconcile_receipt(row,ctx)
    assert plan['status']=='reconciliation_pending' and store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
    assert row['state']=='accepted' and row['delivery_state']=='delivered'
    duplicate=cc.reconcile_receipt(row,ctx);assert duplicate['commands']==[]
    ctx=receipt(ctx,row,'unknown','2026-10-10T12:00:01Z','fixture-late')
    plan=cc.reconcile_receipt(row,ctx);assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
    assert row['delivery_state']=='delivered'
    ctx=receipt(ctx,row,'failed',identifier='fixture-conflict');plan=cc.reconcile_receipt(row,ctx)
    assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
    assert row['state']=='accepted' and row['delivery_state']=='unknown' and row['receipt_conflict']
    assert cc.claim_attempt(row,bound,ctx,grants)['commands']==[]
    assert all('send' not in effect for effect in store.trace)
    store.close()


@pytest.mark.parametrize('state,delivery,conflict',[
 ('draft','not_attempted',False),('held','not_attempted',False),('ready','not_attempted',False),
 ('attempting','not_attempted',False),('acceptance_unknown','unknown',False),
 ('accepted','pending',False),('accepted','unknown',False),('accepted','delivered',True)])
def test_cg4_retention_replay_nonterminal(state,delivery,conflict):
    bound,ctx,grants,store,row=accepted_intent()
    row.update(state=state,delivery_state=delivery,receipt_conflict=conflict,terminal_at=None)
    store.records['fixture-request']=row;ctx,row=store.view(ctx)
    policy=cc.retention_policy(row,ctx)
    assert not policy['eligible'] and policy['metadata_expires_at'] is None and policy['snapshot_expires_at'] is None
    store.close()


def test_cg4_retention_replay():
    bound,ctx,grants,store,row=accepted_intent('delivered')
    snapshot=copy.deepcopy(store.snapshots[row['snapshot_ref']]);policy=cc.retention_policy(row,ctx)
    assert policy['eligible'] and policy['metadata_expires_at']==policy['snapshot_expires_at']=='2027-10-10T12:00:02.000000001Z'
    assert policy['source_expires_at']=='2026-10-11T00:00:00Z'
    ctx=change_evidence(ctx,'source',contacts=[])
    assert store.snapshots[row['snapshot_ref']]==snapshot
    expired=change_evidence(ctx,'replay',status='expired',barrier_ref='fixture-expired-barrier')
    assert cc.check_request_replay(expired,bound.digest)['reason']=='expired_request_rejected'
    store.records.clear();store.snapshots.clear()
    assert cc.create_intent(bound,expired,grants)['commands']==[]
    assert cc.claim_attempt(row,bound,expired,grants)['commands']==[]
    unknown=change_evidence(ctx,'replay',status='unknown',canonical_key=None)
    assert cc.create_intent(bound,unknown,grants)['commands']==[]
    store.close()


@pytest.mark.parametrize('field',['subject','body','name','address','hash','<script>','policy_version'])
def test_cg4_preview_policy(field):
    bound,ctx,grants=lifecycle_context()
    assert cc.validate_preview_policy(ctx)['valid']
    ctx=change_evidence(ctx,'preview',**({'policy_version':2} if field=='policy_version' else {'fields':['channel',field]}))
    assert not cc.validate_preview_policy(ctx)['valid']
    assert cc.create_intent(bound,ctx,grants)['commands']==[]


def test_cg4_staging_cleanup():
    bound,ctx,grants=lifecycle_context()
    ctx=add_evidence(ctx,'staging',dict(status='stored',draft_id='fixture-draft',envelope_digest=bound.digest,
                                     protected_ref='fixture-stage',restricted=True,atomic=True))
    assert cc.staging_cleanup(ctx)['commands']==[]
    store=Store();store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
    plan=cc.create_intent(bound,ctx,grants);assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
    clean=cc.staging_cleanup(ctx)
    assert [c.kind for c in clean['commands']]==['delete_staging']
    failed=add_evidence(ctx,'cleanup',dict(status='failed',command_id=clean['commands'][0].command_id))
    assert cc.staging_cleanup(failed)['commands']==clean['commands']
    done=add_evidence(ctx,'cleanup',dict(status='deleted',command_id=clean['commands'][0].command_id))
    assert cc.staging_cleanup(done)['commands']==[]
    store.close()


def test_cg3_approval_prior_state_and_edit():
    bound,ctx,grants,decision=draft_context();ctx,draft=saved_draft(bound,ctx,decision);ctx=operator_event(bound,ctx,draft)
    for reference in ['forged', True, '']:
        forged=cc.TrustedContext(ctx.resolver,ctx.now,ctx.proofs+(('approval',reference),))
        assert not cc.approval_binding(bound,forged,draft)['bound']
    missing=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!='approval_state'))
    assert not cc.approval_binding(bound,missing,draft)['bound']
    approved=cc.approval_binding(bound,ctx,draft)
    assert approved['commands'][0].conditions['approval_state']=='absent'
    assert approved['commands'][0].conditions['approval_state_version']=='v1'
    edited=cc.bind_envelope(mutate_env('body'));edit_ctx=change_evidence(ctx,'request',draft_version=2)
    plan=cc.draft_command(edited,edit_ctx,decision)
    assert plan['status']=='draft_pending' and plan['draft']['version']==2
    assert plan['commands'][0].conditions['expected_prior_version']==1
    assert plan['commands'][0].conditions['previous_digest']==bound.digest
    assert not cc.approval_binding(edited,edit_ctx,plan['draft'])['bound']


@pytest.mark.parametrize('bad',['2026-10-10T12:00:00','not-a-date','2026-02-30T12:00:00Z',None,True])
def test_cg4_timestamps_reject_naive_or_malformed(bad):
    with pytest.raises(cc.ContractError):cc._instant(bad)


def test_cg4_timestamps_exact():
    assert cc._instant('2026-10-10T12:00:00Z')==cc._instant('2026-10-10T07:00:00-05:00')
    assert cc._instant('2026-10-10T12:00:00.123456789012345678901Z')<cc._instant('2026-10-10T12:00:00.123456789012345678902Z')
    assert cc._add_seconds('2026-10-10T12:00:00.123456789012345678901Z',365*86400)=='2027-10-10T12:00:00.123456789012345678901Z'


def test_cg4_unresolved_and_cancellation():
    env=envelope();env['recipients'][0]['contact_id']=None
    bound,ctx,grants,decision=draft_context(env)
    assert not decision['admitted']
    draft=cc.draft_command(bound,ctx,decision)['draft']
    assert draft['pk']=='CONTACT#UNRESOLVED#'+draft['draft_id']
    bound,ctx,grants,store,row=stored_intent()
    canceled=change_evidence(ctx,'attempt_control',status='canceled')
    assert cc.claim_attempt(row,bound,canceled,grants)['commands']==[]
    claim=cc.claim_attempt(row,bound,ctx,grants);store.versions['attempt_control']='v2'
    assert not store.apply(claim['commands'][0])
    store.close()


@pytest.mark.parametrize('kind,patch',[
 ('persistence',{'policy_version':True}),('protected',{'access':'unknown'}),
 ('protected',{'immutable_snapshot':False}),('protected',{'attachment_refs':[]}),
 ('protected',{'source_expires_at':'2026-10-09T00:00:00Z'}),
 ('attempt_control',{'transaction_ref':None}),('attempt_control',{'status':'canceled'}),
 ('replay',{'issuer_ref':'forged'}),('request',{'original_utc':'2026-10-10T12:00:00'}),
 ('preview',{'fields':['channel','direction','state','attachment_count','hash']})])
def test_cg4_external_inputs_malformed(kind,patch):
    bound,ctx,grants=lifecycle_context();ctx=change_evidence(ctx,kind,**patch)
    assert cc.create_intent(bound,ctx,grants)['commands']==[]


def test_cg3_approval_current_state_and_historical_ack():
    bound,ctx,grants,decision=draft_context();ctx,draft=saved_draft(bound,ctx,decision)
    ctx=operator_event(bound,ctx,draft);pending=cc.approval_binding(bound,ctx,draft)
    approved,binding=approved_context(bound,ctx,draft)
    replay=cc.approval_binding(bound,approved,draft)
    assert replay['bound'] and replay['binding']==pending['binding']
    assert replay['commands']==pending['commands']
    assert cc.admit(bound,approved,grants,approval=approved.reference('approval'))['admitted']
    for status in ['revoked','canceled','consumed','absent']:
        conflict=change_evidence(approved,'approval_state',status=status)
        assert not cc.admit(bound,conflict,grants,approval=conflict.reference('approval'))['admitted']
    for extra in [[('approval',None)],[('approval','a'),('approval','b')]]:
        invalid=cc.TrustedContext(ctx.resolver,ctx.now,ctx.proofs+tuple(extra))
        assert not cc.approval_binding(bound,invalid,draft)['bound']
    assert not cc.approval_binding(bound,change_evidence(ctx,'approval_state',draft_version=True),draft)['bound']


def test_cg4_review_regressions():
    bound,ctx,grants,store,row=stored_intent()
    forged=dict(row,state='accepted',delivery_state='delivered',terminal_at=ctx.now)
    assert not cc.retention_policy(forged,ctx)['eligible']
    assert 'person01@fixture.invalid' not in json.dumps(row)
    edited=change_evidence(ctx,'source',contacts=[contact(['- answer topic [channels=email]: Changed exact historical bytes.'])])
    new_grants=cc._source_grants(edited.resolver.resolve(edited.reference('source'),'source'))
    assert cc.claim_attempt(row,bound,edited,new_grants)['commands']==[]
    store.close()
    bound,ctx,grants,store,row=accepted_intent('delivered')
    stale=receipt(ctx,row,at='2020-01-01T00:00:00Z')
    assert cc.reconcile_receipt(row,stale)['commands']==[]
    contradiction=receipt(ctx,row)
    contradiction=change_evidence(contradiction,'receipt',acceptance='rejected')
    plan=cc.reconcile_receipt(row,contradiction)
    assert plan['record']['state']=='accepted' and plan['record']['delivery_state']=='unknown'
    assert plan['record']['receipt_conflict'] and plan['record']['terminal_at'] is None
    store.close()


def test_cg4_recorded_terminal_time():
    bound,ctx,grants,store,row=claimed_intent();ctx=provider(ctx,row,delivery='delivered')
    ctx=cc.TrustedContext(ctx.resolver,'2026-10-10T14:00:00.123456789Z',ctx.proofs)
    ctx=change_evidence(ctx,'clock',now=ctx.now)
    result=cc.record_result(row,ctx)
    assert result['record']['acceptance_at']=='2026-10-10T12:00:02.000000001Z'
    assert result['record']['terminal_at']=='2026-10-10T14:00:00.123456789Z'
    assert store.apply(result['commands'][0]);ctx,row=store.view(ctx)
    assert cc.retention_policy(row,ctx)['metadata_expires_at']=='2027-10-10T14:00:00.123456789Z'
    store.close()


def test_cg4_pointer_conflict():
    bound,ctx,grants,store,row=stored_intent()
    forged=change_evidence(ctx,'replay',canonical_key=['CONTACT#fixture_person_99','SEND#fixture-other'])
    assert cc.claim_attempt(row,bound,forged,grants)['commands']==[]
    plan=cc.claim_attempt(row,bound,ctx,grants)
    store.requests['fixture-request']['canonical_key']=['CONTACT#fixture_person_99','SEND#fixture-other']
    assert not store.apply(plan['commands'][0]);store.close()


@pytest.mark.parametrize('kind',['intent','claim','provider_result','receipt'])
@pytest.mark.parametrize('failure',['missing','forged','stale','wrong_subject','boolean_ack'])
def test_cg4_external_inputs_lifecycle(kind,failure):
    bound,ctx,grants,store,row=claimed_intent();ctx=provider(ctx,row);ctx=receipt(ctx,row)
    if failure=='missing':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!=kind))
    elif failure=='forged':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,'forged' if k==kind else r) for k,r in ctx.proofs))
    else:
        old=ctx.resolver.resolve(ctx.reference(kind),kind);data=old.data
        if failure=='boolean_ack':
            if kind in {'intent','claim'}:data['committed_command_id']=True
            elif kind=='provider_result':data['outcome']=True
            else:data['acceptance']=True
        ref=ctx.resolver.issue(kind,data,'wrong' if failure=='wrong_subject' else old.subject,old.version,
                    expires='2026-10-09T00:00:00Z' if failure=='stale' else old.expires_at)
        ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,ref if k==kind else r) for k,r in ctx.proofs))
    plan=cc.reconcile_receipt(row,ctx) if kind=='receipt' else cc.record_result(row,ctx)
    assert plan['commands']==[];store.close()


def test_cg4_one_shot_atomic_consumption():
    bound,ctx,grants=lifecycle_context(line='- answer topic [channels=email; autonomy=draft]: Synthetic handling.')
    decision=cc.admit(bound,ctx,grants);ctx,draft=saved_draft(bound,ctx,decision);ctx,binding=approved_context(bound,ctx,draft)
    versions=cc._enforcement_versions(ctx,'fixture-request',cc._current_versions(ctx,'fixture-request'),ctx.reference('approval'))
    ctx=change_evidence(ctx,'publication',versions=versions)
    store=Store();store.versions=versions
    plan=cc.create_intent(bound,ctx,grants,approval=ctx.reference('approval'))
    assert store.apply(plan['commands'][0]);ctx,row=store.view(ctx)
    claim=cc.claim_attempt(row,bound,ctx,grants,approval=ctx.reference('approval'))
    assert not store.apply(claim['commands'][0])  # Missing approval state never defaults available.
    store.approvals[binding['approval_id']]='available';store.approval_versions[binding['approval_id']]='v2'
    assert store.apply(claim['commands'][0]) and store.approvals[binding['approval_id']]=='consumed'
    assert not store.apply(claim['commands'][0])
    ctx,row=store.view(ctx);ctx=provider(ctx,row,'timeout')
    result=cc.record_result(row,ctx);assert store.apply(result['commands'][0])
    ctx,row=store.view(ctx);assert row['state']=='acceptance_unknown'
    assert cc.claim_attempt(row,bound,ctx,grants,approval=ctx.reference('approval'))['commands']==[]
    store.close()


@pytest.mark.parametrize('field,value',[('version',True),('attachment_count',True),('voice','<script>'),
 ('recipients',[{'contact_id':'fixture_person_01','role':'bcc','destination':'leak@fixture.invalid'}]),
 ('delivery_state','hostile'),('state','hostile'),('envelope_digest','bad')])
def test_cg4_external_inputs_record_schema(field,value):
    bound,ctx,grants,store,row=stored_intent();store.records['fixture-request'][field]=value;ctx,row=store.view(ctx)
    assert cc.claim_attempt(row,bound,ctx,grants)['commands']==[]
    assert not cc.retention_policy(row,ctx)['eligible'];store.close()


@pytest.mark.parametrize('field,value',[('terminal_at','2020-01-01T00:00:00Z'),
 ('terminal_at','2027-01-01T00:00:00Z'),('receipts',[123]),('receipts',[{'receipt_id':'fixture-incomplete'}])])
def test_cg4_persisted_record_chronology(field,value):
    bound,ctx,grants,store,row=accepted_intent('delivered')
    store.records['fixture-request'][field]=value;ctx,row=store.view(ctx)
    assert not cc.retention_policy(row,ctx)['eligible']
    assert cc.reconcile_receipt(row,receipt(ctx,row))['commands']==[]
    store.close()


def voice_context(authority=True,enabled=True,contact_enabled=True):
    env=envelope();env.update(voice='as_operator',body='I will handle this synthetic task.',text='I will handle this synthetic task.',html='<p>I will handle this synthetic task.</p>')
    line='- answer topic [channels=email; voice=as_operator; authority=2026-10-10 fixture-authority]: Synthetic handling.'
    bound,ctx,grants=lifecycle_context(env,line)
    if authority:
        ctx=add_evidence(ctx,'voice_authority',dict(authority='2026-10-10 fixture-authority',origin='operator_ledger',
                         account_ref='fixture-account',contact_ids=['fixture_person_01']))
    ctx=add_evidence(ctx,'voice_setup',dict(status='provisioned',account_ref='fixture-account',channel='email',capability_version='v1'))
    ctx=change_evidence(ctx,'capability',operator_voice=enabled,contacts=['fixture_person_01'] if contact_enabled else [],setup_ref=ctx.reference('voice_setup'))
    env['voice_authority_ref']=ctx.reference('voice_authority')
    return env,ctx,grants


def test_cg5_enabled():
    env,ctx,grants=voice_context();composed=cc.compose_voice(env,ctx,grants)
    assert composed['status']=='composed' and composed['envelope']['body']=='I will handle this synthetic task.'
    assert composed['envelope']['introduction']=='' and 'assistant' not in composed['envelope']['body']
    bound=cc.bind_envelope(composed['envelope'])
    decision=cc.admit(bound,ctx,grants)
    assert decision['admitted'] and decision['snapshots'][0]['authority']=='2026-10-10 fixture-authority'
    ctx=change_evidence(ctx,'protected',envelope_digest=bound.digest)
    ctx=change_evidence(ctx,'attempt_control',envelope_digest=bound.digest)
    versions=cc._enforcement_versions(ctx,'fixture-request',decision['versions'])
    ctx=change_evidence(ctx,'publication',versions=versions)
    store=Store();store.versions=versions
    intent=cc.create_intent(bound,ctx,grants);assert store.apply(intent['commands'][0]);ctx,row=store.view(ctx)
    claim=cc.claim_attempt(row,bound,ctx,grants);assert store.apply(claim['commands'][0]);ctx,row=store.view(ctx)
    effects=['provider:fake_send'];ctx=provider(ctx,row);result=cc.record_result(row,ctx);assert store.apply(result['commands'][0])
    assert len(effects)==1 and row['voice']=='as_operator' and row['voice_authority_ref']==ctx.reference('voice_authority')
    assert row['voice_config_version']=='v1' and row['observed_versions']['voice_setup']=='v1'
    store.close()


@pytest.mark.parametrize('authority,enabled,contact_enabled',[
 (False,False,False),(False,False,True),(False,True,False),(False,True,True),
 (True,False,False),(True,False,True),(True,True,False)])
def test_cg5_each_condition(authority,enabled,contact_enabled):
    env,ctx,grants=voice_context(authority,enabled,contact_enabled)
    expected='authority_missing' if not authority else 'operator_voice_disabled' if not enabled else 'contact_not_enabled'
    result=cc.compose_voice(env,ctx,grants)
    assert result['status']=='held' and result['reason']==expected
    bound=cc.bind_envelope(env);decision=cc.admit(bound,ctx,grants)
    assert not decision['admitted'] and decision['reason']==expected
    draft=cc.draft_command(bound,ctx,decision)
    assert draft['status']=='draft_pending' and [c.kind for c in draft['commands']]==['put_draft_and_handoff']
    assert cc.create_intent(bound,ctx,grants)['commands']==[]


def test_cg5_default_and_disabled_setup():
    env=envelope('sms');ctx,grants=context(env)
    composed=cc.compose_voice(env,ctx,grants)
    assert composed['envelope']['body'].endswith('\n\nSynthetic assistant')
    assert composed['envelope']['introduction']=='Synthetic assistant: '
    assert cc.compose_voice(cc.bind_envelope(composed['envelope']),ctx,grants)['status']=='held'
    env,ctx,grants=voice_context();ctx=change_evidence(ctx,'voice_setup',channel='sms')
    assert cc.compose_voice(env,ctx,grants)['reason']=='operator_voice_disabled'
    ctx=change_evidence(ctx,'config',default_voice_token='unknown')
    assert cc.compose_voice(env,ctx,grants)['reason']=='config_untrusted'


def test_cg5_composition_required_before_freeze():
    env=envelope();ctx,grants=context(env)
    env['body']='Unsigned synthetic message'
    assert not cc.admit(cc.bind_envelope(env),ctx,grants)['admitted']
    once=cc.compose_voice(env,ctx,grants)
    twice=cc.compose_voice(once['envelope'],ctx,grants)
    assert once==twice and cc.admit(cc.bind_envelope(once['envelope']),ctx,grants)['admitted']


PATHS=FIXTURE['paths']


def path_context(case):
    env=envelope(case['channel']);bound,ctx,grants=lifecycle_context(env,line='- answer topic [channels=sms,email,botcomm]: Synthetic handling.')
    ctx=add_evidence(ctx,'path',dict(path=case['path'],channel=case['channel'],status='enabled',source_status='available',access='allowed',
                                   prior_trace=['prompt:inbound'],base_trace=case['base_trace']))
    ctx=add_evidence(ctx,'execution',dict(invocation_id='fixture-invocation',phase='live'))
    return bound,ctx,grants


def with_path(ctx,case,prior=None):
    return add_evidence(ctx,'path',dict(path=case['path'],channel=case['channel'],status='enabled',source_status='available',access='allowed',
               prior_trace=[] if prior is None else prior,base_trace=case['base_trace']))


def activate(ctx,store,row):
    claim=store.claims[row['request_id']]
    return add_evidence(ctx,'dispatch_activation',dict(attempt_id=row['attempt_id'],claim_command_id=claim.command_id,
                        activation_id='fixture-activation',invocation_id=claim.body.get('execution_id'),status='fresh',transaction_ref='fixture-activation-transaction'))


def apply_gate(store,plan,ctx):
    """Synthetic adapter commits storage then performs each authorized fake once."""
    request=cc._resolve(ctx,'request')
    proof=cc._resolve(ctx,'path',request.subject) if request else None
    prior=proof.data.get('prior_trace',[]) if proof else []
    assert type(prior) is list and all(type(x) is str and x in {'prompt:inbound','prompt:acknowledged','cancel:observed'} for x in prior)
    with store.lock:
        if store.prior_history is None:store.prior_history=prior[:]
        else:assert store.prior_history==prior
        if plan.get('status')=='excluded-with-owner-evidence' and 'workflow:excluded' not in store.trace:
            store.trace.append('workflow:excluded')
    for command in plan['commands']:
        if command.kind=='invoke_provider_once':
            return apply_provider_once(store,command)
        if not store.apply(command):return False
    return True


def apply_provider_once(store,command):
    with store.lock:
            token=command.conditions['activation_id'];key=(command.request_id,command.body['attempt_id'])
            if not hasattr(store,'dispatched'):store.dispatched=set();store.activations=set();store.live_invocations={'fixture-invocation'}
            if key in store.dispatched or token in store.activations or command.conditions['execution_id'] not in store.live_invocations:return False
            if command.conditions['protected_version']!=store.versions.get('protected') or getattr(store,'content_access','allowed')!='allowed':return False
            if command.conditions['source_expires_at'] is not None and cc._instant(command.conditions['source_expires_at'])<=cc._instant(getattr(store,'now','2026-10-10T12:00:01Z')):return False
            if store.records[command.request_id]['state']!='attempting' or store.claims[command.request_id].command_id!=command.body['claim_command_id']:return False
            store.dispatched.add(key);store.activations.add(token);store.trace.append('provider:send');return True


@pytest.mark.parametrize('case',PATHS,ids=lambda x:x['path'])
def test_cg6_paths(case):
    bound,ctx,grants=path_context(case);plan=cc.gate_path(case['path'],bound,ctx,grants)
    if not case['enabled']:
        assert plan['status']=='held' and [c.kind for c in plan['commands']]==['put_draft_and_handoff']
        assert not any('provider' in x or 'browser' in x or 'applescript' in x or 'receipt:success' in x for x in plan['trace'])
        return
    assert [c.kind for c in plan['commands']]==['create_intent']
    store=Store();store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
    assert apply_gate(store,plan,ctx);ctx,row=store.view(ctx)
    claim=cc.gate_path(case['path'],bound,ctx,grants)
    assert [c.kind for c in claim['commands']]==['claim_attempt'];assert apply_gate(store,claim,ctx);ctx,row=store.view(ctx)
    assert cc.gate_path(case['path'],bound,ctx,grants)['commands']==[]  # Crash/replay has no live activation.
    ctx=activate(ctx,store,row);send=cc.gate_path(case['path'],bound,ctx,grants)
    assert [c.kind for c in send['commands']]==['invoke_provider_once']
    assert apply_gate(store,send,ctx) and not apply_gate(store,send,ctx)
    ctx=provider(ctx,row);finish=cc.gate_path(case['path'],bound,ctx,grants)
    assert [c.kind for c in finish['commands']]==['record_result'];assert apply_gate(store,finish,ctx)
    ctx,row=store.view(ctx)
    assert row['state']=='accepted' and len(store.records)==1 and store.trace==[
        'storage:intent_committed','storage:claim_committed','provider:send','receipt:record_result']
    normalized=[('receipt:result' if label.startswith('receipt:') else label) for label in store.observed_trace if label.startswith(('prompt:','provider:','receipt:'))]
    assert normalized==case['base_trace']
    denied=change_evidence(ctx,'capability',channels=[])
    # Completed accepted records do not become a fresh send after permission changes.
    assert cc.gate_path(case['path'],bound,denied,grants)['commands']==[]
    store.close()


@pytest.mark.parametrize('case',[p for p in PATHS if p['enabled']],ids=lambda x:x['path'])
def test_cg6_paths_denied(case):
    bound,ctx,grants=path_context(case);ctx=change_evidence(ctx,'capability',channels=[])
    plan=cc.gate_path(case['path'],bound,ctx,grants)
    assert plan['status']=='held' and [c.kind for c in plan['commands']]==['put_draft_and_handoff']
    assert plan['trace']==['prompt:inbound','storage:draft_requested','handoff:pending']
    store=Store();assert apply_gate(store,plan,ctx) and apply_gate(store,plan,ctx)
    ctx=store.draft_view(ctx);next_plan=cc.gate_path(case['path'],bound,ctx,grants);assert apply_gate(store,next_plan,ctx)
    assert len(store.drafts)==len(store.handoffs)==1
    assert store.trace==['storage:draft_committed','handoff:pending','prompt:handoff']
    store.close()


@pytest.mark.parametrize('classification',['delegated_assignment','business_product_texting'])
def test_cg6_exclusion(classification):
    bound,ctx,grants=path_context(PATHS[0])
    forged=cc.gate_path(classification,bound,ctx,grants)
    assert forged['status']=='held'
    ctx=change_evidence(ctx,'workflow',classification=classification,owner_evidence_ref='forged')
    assert cc.gate_path(PATHS[0]['path'],bound,ctx,grants)['status']=='held'
    ctx=add_evidence(ctx,'exclusion',dict(classification=classification,owner_ref='fixture-workflow-owner',workflow_id='fixture-workflow'))
    ctx=change_evidence(ctx,'workflow',owner_evidence_ref=ctx.reference('exclusion'),workflow_id='fixture-workflow')
    plan=cc.gate_path(PATHS[0]['path'],bound,ctx,grants)
    assert plan['status']=='excluded-with-owner-evidence' and plan['commands']==[]
    assert plan['prior_trace']==['prompt:inbound'] and plan['next_trace']==['workflow:excluded']
    assert plan['trace']==['prompt:inbound','workflow:excluded']
    store=Store()
    try:
        assert apply_gate(store,plan,ctx) and apply_gate(store,plan,ctx)
        assert store.observed_trace==plan['trace'] and not store.records
    finally:store.close()


def history_context(rows=None,sources=None):
    rows=copy.deepcopy(FIXTURE['history_rows'] if rows is None else rows)
    auth=Authority();now='2026-12-01T00:00:00Z'
    sources=sources or {name:dict(status='available',access='allowed',version='v1',expires_at=None) for name in ['sms','email','botcomm','outbox','archive']}
    proofs=[]
    for kind,data in [('request',dict(request_id='fixture-request')),
                      ('preview',dict(policy_version=1,fields=['channel','direction','state','attachment_count'])),
                      ('clock',dict(now=now,source='trusted_utc')),('history',dict(rows=rows,sources=sources))]:
        proofs.append((kind,auth.issue(kind,data,expires='2027-12-01T00:00:00Z')))
    return rows,cc.TrustedContext(auth,now,tuple(proofs))


def test_cg6_thread():
    rows,ctx=history_context();projection=cc.project_thread(rows,ctx)
    assert projection['status']=='complete' and len(projection['rows'])==2
    assert '-05:00' in projection['rows'][0]['time_local'] and '-06:00' in projection['rows'][1]['time_local']
    assert projection['rows'][0]['acceptance']=='accepted' and projection['rows'][0]['delivery']=='pending'
    assert projection['rows'][1]['placeholder']=='protected_recipient'
    rendered=json.dumps(projection)
    for secret in ['SYNTHETIC_SECRET_CANARY','person01@fixture.invalid','<script>','<b>','Synthetic subject','a'*64]:assert secret not in rendered
    assert 'sms outbound accepted' in projection['cli'] and 'email inbound received' in projection['cli']
    first=cc.project_thread(rows,ctx,limit=1);second=cc.project_thread(rows,ctx,cursor=first['cursor'],limit=1)
    assert first['rows']+second['rows']==projection['rows'] and second['cursor'] is None
    assert cc.project_thread(rows,ctx,cursor='forged')['status']=='held'
    mirror=dict(rows[0],source_type='archive',source_record_id='fixture-mirror',stable_id='fixture-mirror')
    rows,ctx=history_context(rows+[mirror]);assert len(cc.project_thread(rows,ctx)['rows'])==2
    bot=dict(rows[0],channel='botcomm',source_type='botcomm',provider_message_id='fixture-bot',stable_id='fixture-bot')
    rows,ctx=history_context([bot]);assert cc.project_thread(rows,ctx)['rows'][0]['channel']=='botcomm'


def test_cg6_thread_fractional_ties_and_partial():
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[1].update(original_utc='2026-11-01T06:30:00.123456789012345678902Z')
    rows,ctx=history_context(list(reversed(rows)));output=cc.project_thread(rows,ctx)
    assert [r['channel'] for r in output['rows']]==['sms','email']
    sources={name:dict(status='unavailable' if name=='sms' else 'available',access='allowed',version='v1',expires_at=None) for name in ['sms','email']}
    rows,ctx=history_context(sources=sources);output=cc.project_thread(rows,ctx)
    assert output['status']=='partial_history' and output['missing_sources']==['sms']
    assert output['rows'][0]['placeholder']=='source_unavailable'
    sources['sms']['status']='available';sources['sms']['access']='denied'
    rows,ctx=history_context(sources=sources);assert cc.project_thread(rows,ctx)['rows'][0]['placeholder']=='protected_unavailable'


@pytest.mark.parametrize('field,value,placeholder',[
 ('metadata_expires_at','2026-11-30T00:00:00Z','terminal_expired'),('snapshot_status','expired','snapshot_expired'),
 ('source_expires_at','2026-11-30T00:00:00Z','content_expired'),('content_status','unavailable','content_unavailable'),
 ('content_status','unknown','protected_unavailable')])
def test_cg6_retention_preview(field,value,placeholder):
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[0][field]=value
    if field=='metadata_expires_at':rows[0]['delivery_state']='delivered'
    rows,ctx=history_context(rows);result=cc.project_thread(rows,ctx)
    assert result['rows'][0]['placeholder']==placeholder
    assert 'SYNTHETIC_SECRET_CANARY' not in json.dumps(result)
    ctx=change_evidence(ctx,'preview',policy_version=2)
    assert cc.project_thread(rows,ctx)['status']=='held'


@pytest.mark.parametrize('case',PATHS,ids=lambda x:x['path'])
@pytest.mark.parametrize('rule,branch',list(FIXTURE['effect_failures'].items()),ids=list(FIXTURE['effect_failures']))
def test_cg6_effect_order(case,rule,branch):
    bound,ctx,grants=path_context(case)
    if branch=='expired':
        ctx=change_evidence(ctx,'replay',status='expired',barrier_ref='fixture-expired')
        plan=cc.gate_path(case['path'],bound,ctx,grants)
        assert plan['commands']==[] and not any(x.startswith(('provider:','receipt:','prompt:handoff')) for x in plan['trace'])
    elif branch=='persistence':
        ctx=change_evidence(ctx,'capability',channels=[]);ctx=change_evidence(ctx,'persistence',status='unavailable')
        plan=cc.gate_path(case['path'],bound,ctx,grants)
        assert [c.kind for c in plan['commands']]==['stage_draft']
        assert plan['trace']==['prompt:inbound','storage:stage_requested','hold:unadmitted']
    elif branch in {'preclaim','voice','cancel'}:
        if rule in {'cg1_invalid','cg1_contact_invalid'}:
            records=[contact(['- answer Topic: Malformed'])] if rule=='cg1_invalid' else [contact(['- answer topic: First','- answer topic: Duplicate'])]
            ctx=change_evidence(ctx,'source',contacts=records)
            grants=cc._source_grants(ctx.resolver.resolve(ctx.reference('source'),'source'))
        elif rule=='cg2_shared_reader':
            identity=ctx.resolver.resolve(ctx.reference('identity'),'identity').data
            for target in identity['recipients'].values():target['channels']=[]
            ctx=change_evidence(ctx,'identity',recipients=identity['recipients'])
        elif rule in {'cg2_envelope','cg2_wire'}:
            bound=cc.BoundEnvelope(bound.logical_json,'0'*64 if rule=='cg2_envelope' else bound.digest,'bad' if rule=='cg2_wire' else None)
        elif rule=='cg2_untrusted':ctx=change_evidence(ctx,'workflow',scope='body_claimed_scope')
        elif rule=='cg3_approval':ctx=change_evidence(ctx,'attempt_control',status='canceled')
        elif rule=='cg4_external_inputs':ctx=cc.TrustedContext(ctx.resolver,ctx.now,tuple((k,r) for k,r in ctx.proofs if k!='protected'))
        elif rule=='cg4_versions':ctx=change_evidence(ctx,'publication',pending=['source'])
        elif rule=='cg4_request_concurrency':ctx=change_evidence(ctx,'replay',status='active',canonical_key=['CONTACT#fixture-other','SEND#fixture-other'],envelope_digest=bound.digest)
        elif rule=='cg4_preview_policy':ctx=change_evidence(ctx,'preview',policy_version=2)
        elif rule=='cg5_each_condition':
            env=bound.data;env.update(voice='as_operator',voice_authority_ref=None,body='I will handle the synthetic task.',text='I will handle the synthetic task.',html='<p>I will handle the synthetic task.</p>',introduction='');bound=cc.bind_envelope(env)
            ctx=change_evidence(ctx,'source',contacts=[contact(['- answer topic [channels=sms,email,botcomm; voice=as_operator; authority=2026-10-10 fixture-authority]: Synthetic.'])])
            grants=cc._source_grants(ctx.resolver.resolve(ctx.reference('source'),'source'))
        else:ctx=change_evidence(ctx,'capability',channels=[])
        plan=cc.gate_path(case['path'],bound,ctx,grants)
        if rule=='cg4_request_concurrency' and case['enabled']:
            assert plan['commands']==[] and plan['trace']==['prompt:inbound','hold:replay']
        else:
            assert [c.kind for c in plan['commands']]==['put_draft_and_handoff']
            assert plan['trace']==['prompt:inbound','storage:draft_requested','handoff:pending']
            store=Store()
            assert apply_gate(store,plan,ctx);ctx=store.draft_view(ctx)
            handoff=cc.gate_path(case['path'],bound,ctx,grants);assert apply_gate(store,handoff,ctx)
            assert store.trace==['storage:draft_committed','handoff:pending','prompt:handoff']
            assert not store.records and not store.attempts;store.close()
    elif not case['enabled']:
        plan=cc.gate_path(case['path'],bound,ctx,grants)
        assert plan['status']=='held' and [c.kind for c in plan['commands']]==['put_draft_and_handoff']
    elif branch=='history':
        rows,hctx=history_context(sources={'sms':dict(status='unavailable',access='allowed',version='v1',expires_at=None),'email':dict(status='available',access='allowed',version='v1',expires_at=None)})
        result=cc.project_thread(rows,hctx)
        assert result['status']=='partial_history' and 'commands' not in result
    elif branch=='exclusion':
        ctx=add_evidence(ctx,'exclusion',dict(classification='delegated_assignment',owner_ref='fixture-owner',workflow_id='fixture-workflow'))
        ctx=change_evidence(ctx,'workflow',classification='delegated_assignment',owner_evidence_ref=ctx.reference('exclusion'),workflow_id='fixture-workflow')
        plan=cc.gate_path(case['path'],bound,ctx,grants)
        assert plan['prior_trace']==['prompt:inbound'] and plan['next_trace']==['workflow:excluded']
        assert plan['trace']==['prompt:inbound','workflow:excluded'] and plan['commands']==[]
        store=Store()
        try:
            assert apply_gate(store,plan,ctx) and apply_gate(store,plan,ctx)
            assert store.observed_trace==plan['trace'] and not store.records
        finally:store.close()
    else:
        store=Store();store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
        plan=cc.gate_path(case['path'],bound,ctx,grants);assert apply_gate(store,plan,ctx);ctx,row=store.view(ctx)
        if branch=='before_claim':
            assert store.trace==['storage:intent_committed']
        elif branch=='cleanup_only':
            ctx=add_evidence(ctx,'staging',dict(status='stored',envelope_digest=bound.digest,protected_ref='fixture-stage',restricted=True,atomic=True))
            plan=cc.gate_path(case['path'],bound,ctx,grants)
            assert [c.kind for c in plan['commands']]==['delete_staging'] and plan['next_trace']==['cleanup:delete_staging']
        else:
            claim=cc.gate_path(case['path'],bound,ctx,grants);assert apply_gate(store,claim,ctx);ctx,row=store.view(ctx)
            if branch=='claimed_unknown':
                plan=cc.gate_path(case['path'],bound,ctx,grants)
                assert plan['commands']==[] and store.trace==['storage:intent_committed','storage:claim_committed']
            elif branch=='receipt_only':
                ctx=receipt(ctx,row);plan=cc.gate_path(case['path'],bound,ctx,grants)
                assert [c.kind for c in plan['commands']]==['reconcile_receipt'] and plan['next_trace']==['receipt:reconcile_requested']
            elif branch=='combined':
                ctx=activate(ctx,store,row);assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx)
                ctx=provider(ctx,row);assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx)
                assert store.trace==['storage:intent_committed','storage:claim_committed','provider:send','receipt:record_result']
        store.close();assert store.closed


def test_cg6_combined():
    owned=[];effects=[]
    try:
        for case in [p for p in PATHS if p['enabled']]:
            bound,ctx,grants=path_context(case);store=Store();owned.append(store)
            store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
            assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
            assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
            ctx=activate(ctx,store,row);assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx)
            ctx=provider(ctx,row);assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx)
            effects+=store.trace
        assert effects.count('provider:send')==9 and not any('real' in x for x in effects)
    finally:
        for store in owned:store.close()
    assert len(owned)==9 and all(s.closed and not s.records and not s.requests and not s.snapshots for s in owned)


def test_cg6_activation_crash_recovery_and_races():
    case=PATHS[4];bound,ctx,grants=path_context(case);store=Store()
    store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
    assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
    assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
    ctx=activate(ctx,store,row);proposal=cc.gate_path(case['path'],bound,ctx,grants)
    recovered=change_evidence(ctx,'execution',phase='recovery',invocation_id='fixture-recovery')
    assert cc.gate_path(case['path'],bound,recovered,grants)['commands']==[]
    store.dispatched=set();store.activations=set();store.live_invocations=set()
    assert not apply_gate(store,proposal,ctx) and 'provider:send' not in store.trace
    store.live_invocations={'fixture-invocation'}
    # A crash after consumption and before effect is permanently consumed, with zero sends.
    store.dispatched.add((row['request_id'],row['attempt_id']));store.activations.add('fixture-activation')
    assert not apply_gate(store,proposal,ctx)
    reissued=change_evidence(ctx,'dispatch_activation',activation_id='fixture-reissued')
    assert not apply_gate(store,cc.gate_path(case['path'],bound,reissued,grants),reissued)
    assert 'provider:send' not in store.trace;store.close()


def test_cg6_receipt_recovery_ignores_current_admission():
    bound,ctx,grants,store,row=accepted_intent()
    case=PATHS[4];ctx=with_path(ctx,case);ctx=receipt(ctx,row)
    ctx=change_evidence(ctx,'source',contacts=[]);ctx=change_evidence(ctx,'capability',channels=[])
    ctx=change_evidence(ctx,'path',status='disabled',source_status='unavailable',access='denied')
    plan=cc.gate_path(case['path'],bound,ctx,grants)
    assert [c.kind for c in plan['commands']]==['reconcile_receipt']
    assert plan['next_trace']==['receipt:reconcile_requested'];store.close()


def test_cg6_terminal_expiry_and_unknown_stay_visible():
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[0].update(delivery_state='delivered',metadata_expires_at='2026-11-30T00:00:00Z')
    sources={'sms':dict(status='unavailable',access='denied',version='v1',expires_at=None),'email':dict(status='available',access='allowed',version='v1',expires_at=None)}
    data,ctx=history_context(rows,sources);result=cc.project_thread(data,ctx)
    assert result['rows'][0]=={'placeholder':'terminal_expired'}
    for state,delivery,conflict in [('acceptance_unknown','unknown',False),('accepted','pending',False),('accepted','delivered',True)]:
        rows[0].update(state=state,delivery_state=delivery,receipt_conflict=conflict)
        data,ctx=history_context(rows);result=cc.project_thread(data,ctx)
        assert result['rows'][0]['state']==state and result['rows'][0]['placeholder']!='terminal_expired'
    data,ctx=history_context();first=cc.project_thread(data,ctx,limit=1)
    changed=add_evidence(ctx,'history',ctx.resolver.resolve(ctx.reference('history'),'history').data,version='v2')
    # A replacement evidence snapshot cannot reuse the prior cursor.
    assert cc.project_thread(data,changed,cursor=first['cursor'])['status']=='held'


@pytest.mark.parametrize('field,value',[('state','<script>'),('direction','<b>'),('attachment_count',True),
 ('grant_line_ids',['<a href=evil>']),('channel','javascript:synthetic'),('role','unknown')])
def test_cg6_thread_hostile_metadata(field,value):
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[0][field]=value;rows,ctx=history_context(rows)
    output=cc.project_thread(rows,ctx)
    assert output['status']=='held' and output['rows']==[]


def test_cg6_outgoing_bcc_and_attempt_item():
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[0]['role']='bcc';rows,ctx=history_context(rows)
    output=cc.project_thread(rows,ctx)['rows'][0]
    assert output['line_ids']==[] and output['linked_messages']==0 and output['placeholder']=='protected_recipient'
    bound,ctx,grants,store,row=claimed_intent()
    assert len(store.attempts)==1 and store.attempts[row['attempt_id']]['state']=='attempting'
    ctx=provider(ctx,row);plan=cc.record_result(row,ctx);assert store.apply(plan['commands'][0])
    assert store.attempts[row['attempt_id']]['state']=='accepted';store.close();assert not store.attempts


@pytest.mark.parametrize('case',PATHS,ids=lambda x:x['path'])
def test_cg6_stateful_denial_and_handoff(case):
    bound,ctx,grants=path_context(case);ctx=change_evidence(ctx,'capability',channels=[]);store=Store()
    try:
        plan=cc.gate_path(case['path'],bound,ctx,grants)
        assert apply_gate(store,plan,ctx) and apply_gate(store,plan,ctx)
        assert len(store.drafts)==1 and len(store.handoffs)==1
        ctx=store.draft_view(ctx);handoff=cc.gate_path(case['path'],bound,ctx,grants)
        store.handoff_available=False
        assert not apply_gate(store,handoff,ctx) and not apply_gate(store,handoff,ctx)
        assert len(set(store.handoff_attempts))==1
        store.handoff_available=True;assert apply_gate(store,handoff,ctx)
        ctx=store.draft_view(ctx);assert cc.gate_path(case['path'],bound,ctx,grants)['commands']==[]
        assert store.trace==['storage:draft_committed','handoff:pending','handoff:unavailable','handoff:unavailable','prompt:handoff']
        assert store.observed_trace[0]==case['base_trace'][0] and store.observed_trace==['prompt:inbound']+store.trace
        assert not any(x.startswith(('provider:','receipt:')) for x in store.trace)
    finally:store.close()
    assert not store.drafts and not store.handoffs and not store.stages


@pytest.mark.parametrize('case',PATHS,ids=lambda x:x['path'])
def test_cg6_stateful_staging_failure(case):
    bound,ctx,grants=path_context(case);ctx=change_evidence(ctx,'capability',channels=[])
    ctx=change_evidence(ctx,'persistence',status='unavailable');store=Store()
    try:
        plan=cc.gate_path(case['path'],bound,ctx,grants);command=plan['commands'][0]
        store.stage_available=False;assert not apply_gate(store,plan,ctx)
        draft=command.body['draft']
        failed=add_evidence(ctx,'staging',dict(status='failed',draft_id=draft['draft_id'],version=draft['version'],envelope_digest=bound.digest,
                  restricted=True,atomic=True,committed_command_id=command.command_id))
        assert cc.gate_path(case['path'],bound,failed,grants)['commands']==[]
        store.stage_available=True;assert apply_gate(store,plan,ctx);ctx=store.stage_view(ctx)
        assert cc.gate_path(case['path'],bound,ctx,grants)['commands']==[]
        recovered=change_evidence(ctx,'persistence',status='available')
        replay=cc.gate_path(case['path'],bound,recovered,grants);assert apply_gate(store,replay,recovered)
        assert store.trace==['storage:stage_failed','storage:staged','storage:draft_committed','handoff:pending']
        assert not store.records and not any(x.startswith(('provider:','receipt:','cleanup:')) for x in store.trace)
    finally:store.close()


@pytest.mark.parametrize('case',[p for p in PATHS if p['enabled']],ids=lambda x:x['path'])
def test_cg6_stateful_cleanup_and_cancellation(case):
    bound,ctx,grants=path_context(case);store=Store()
    try:
        unavailable=change_evidence(ctx,'persistence',status='unavailable')
        stage=cc.draft_command(bound,unavailable,cc._deny('held'))
        assert apply_gate(store,stage,unavailable);ctx=store.stage_view(ctx)
        assert cc.staging_cleanup(ctx)['commands']==[]
        store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
        intent=cc.gate_path(case['path'],bound,ctx,grants);assert apply_gate(store,intent,ctx);ctx,row=store.view(ctx)
        cleanup=cc.gate_path(case['path'],bound,ctx,grants)
        assert cleanup['next_trace']==['cleanup:delete_staging'] and cleanup['prior_trace']==['prompt:inbound']
        store.delete_available=False;assert not apply_gate(store,cleanup,ctx)
        store.delete_available=True;assert apply_gate(store,cleanup,ctx)
        ctx=add_evidence(ctx,'cleanup',dict(status='deleted',command_id=cleanup['commands'][0].command_id))
        control=ctx.resolver.resolve(ctx.reference('attempt_control'),'attempt_control').data
        control['status']='canceled';ctx=add_evidence(ctx,'attempt_control',control,version='v2');store.versions['attempt_control']='v2'
        cancel=cc.gate_path(case['path'],bound,ctx,grants)
        assert cancel['next_trace']==['cancel:requested'] and cancel['prior_trace']==['prompt:inbound'] and apply_gate(store,cancel,ctx)
        ctx,row=store.view(ctx)
        assert row['state']=='canceled' and cc.retention_policy(row,ctx)['eligible']
        assert store.observed_trace==['prompt:inbound','storage:staged','storage:intent_committed','cleanup:failed','cleanup:deleted','cancel:committed']
        assert not any(x.startswith(('provider:','receipt:','prompt:')) for x in store.trace)
    finally:store.close()
    assert not store.cleanups and not store.stages and not store.records


def test_cg6_final_boundary_regressions():
    case=PATHS[4];bound,ctx,grants=path_context(case)
    expired=change_evidence(ctx,'protected',source_expires_at='2026-10-09T00:00:00Z')
    for storage in ['available','unavailable']:
        denied=change_evidence(expired,'persistence',status=storage)
        plan=cc.gate_path(case['path'],bound,denied,grants)
        draft=plan['commands'][0].body if storage=='available' else plan['commands'][0].body['draft']
        assert draft['envelope_json'] is None and draft['content_disposition']=='expired'
        assert draft['source_expires_at']=='2026-10-09T00:00:00Z'
        assert 'Synthetic body' not in plan['commands'][0].body_json
    # Positive copies carry the source's exact independent expiry, including staging.
    staged=cc.draft_command(bound,change_evidence(ctx,'persistence',status='unavailable'),cc._deny('held'))
    assert staged['draft']['source_expires_at']=='2026-10-11T00:00:00Z'
    bound,ctx,grants,store,row=claimed_intent();ctx=with_path(ctx,case)
    ctx=add_evidence(ctx,'execution',dict(phase='live',invocation_id=None));ctx=activate(ctx,store,row)
    assert cc.gate_path(case['path'],bound,ctx,grants)['commands']==[];store.close()
    bound,ctx,grants,store,row=stored_intent();ctx=with_path(ctx,case)
    ctx=change_evidence(ctx,'attempt_control',status='canceled',envelope_digest='0'*64,transaction_ref=None)
    assert cc.gate_path(case['path'],bound,ctx,grants)['commands']==[];store.close()


def test_cg6_effect_expiry_between_plan_and_execution():
    case=PATHS[4];bound,ctx,grants=path_context(case);store=Store()
    try:
        store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
        ctx=activate(ctx,store,row);proposal=cc.gate_path(case['path'],bound,ctx,grants)
        later='2026-10-11T00:00:01Z';late=cc.TrustedContext(ctx.resolver,later,ctx.proofs);late=change_evidence(late,'clock',now=later)
        assert cc.gate_path(case['path'],bound,late,grants)['commands']==[]
        store.now=later;assert not apply_gate(store,proposal,ctx)
        assert store.records['fixture-request']['state']=='attempting' and 'provider:send' not in store.trace
    finally:store.close()


@pytest.mark.parametrize('disposition',['content_expired','source_expired','content_unavailable','identity_unresolved'])
def test_cg6_history_privacy_intersections(disposition):
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[0]['role']='bcc'
    if disposition=='content_expired':rows[0]['content_status']='expired'
    elif disposition=='source_expired':rows[0]['source_expires_at']='2026-11-30T00:00:00Z'
    elif disposition=='content_unavailable':rows[0]['content_status']='unavailable'
    else:rows[0].update(role='to',identity_status='unresolved',content_status='expired')
    rows,ctx=history_context(rows);output=cc.project_thread(rows,ctx)
    assert output['rows'][0]['line_ids']==[] and 'fixture_person_01:answer:topic' not in output['cli']


def test_cg6_history_sources_mirrors_and_cursor_privacy():
    source=dict(status='available',access='allowed',version='v1',expires_at='2026-11-30T00:00:00Z')
    rows,ctx=history_context([],{'email':source});output=cc.project_thread(rows,ctx)
    assert output['status']=='partial_history' and output['missing_sources']==['email']
    rows=copy.deepcopy(FIXTURE['history_rows']);rows[0].pop('source_version')
    source.pop('version');rows,ctx=history_context(rows,{'sms':source})
    assert cc.project_thread(rows,ctx)['status']=='held'
    row=copy.deepcopy(FIXTURE['history_rows'][0]);row.update(source_type='outbox',role='bcc')
    mirror=dict(row,source_type='archive',role='to',stable_id='fixture-mirror',source_record_id='fixture-mirror')
    rows,ctx=history_context([row,mirror]);output=cc.project_thread(rows,ctx)
    assert len(output['rows'])==1 and output['rows'][0]['line_ids']==[]
    row.update(role='to',delivery_state='delivered',metadata_expires_at='2026-11-30T00:00:00Z')
    mirror.update(delivery_state='delivered',metadata_expires_at=None)
    rows,ctx=history_context([row,mirror,FIXTURE['history_rows'][1]])
    page=cc.project_thread(rows,ctx,limit=1)
    assert page['rows']==[{'placeholder':'terminal_expired'}]
    assert page['cursor'].startswith('cursor-') and row['original_utc'] not in page['cursor'] and row['stable_id'] not in page['cursor']
    duplicate=dict(FIXTURE['history_rows'][0],provider_message_id='fixture-different-provider')
    rows,ctx=history_context([FIXTURE['history_rows'][0],duplicate])
    assert cc.project_thread(rows,ctx,limit=1)['status']=='held'


def test_cg5_operator_rejects_assistant_composition():
    env,ctx,grants=voice_context()
    env['body']+='\n\nSynthetic assistant';env['text']+='\n\nSynthetic assistant';env['html']+='<p>Synthetic assistant</p>'
    assert cc.compose_voice(env,ctx,grants)['status']=='held'
    assert not cc.admit(cc.bind_envelope(env),ctx,grants)['admitted']
    env,ctx,grants=voice_context();env['introduction']='Synthetic assistant: '
    assert not cc.admit(cc.bind_envelope(env),ctx,grants)['admitted']


def test_cg6_provider_command_uses_shortest_source_bound():
    case=PATHS[4];bound,ctx,grants=path_context(case);store=Store()
    try:
        store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx);ctx=activate(ctx,store,row)
        shorter=change_evidence(ctx,'protected',source_expires_at='2026-10-10T13:00:00Z')
        plan=cc.gate_path(case['path'],bound,shorter,grants)
        assert plan['commands'][0].conditions['source_expires_at']=='2026-10-10T13:00:00Z'
        store.now='2026-10-10T13:00:00Z';assert not apply_gate(store,plan,shorter)
        extended=change_evidence(ctx,'protected',source_expires_at='2026-10-12T00:00:00Z')
        assert cc.gate_path(case['path'],bound,extended,grants)['commands'][0].conditions['source_expires_at']=='2026-10-11T00:00:00Z'
    finally:store.close()


def test_cg6_concurrent_provider_activation_once():
    case=PATHS[4];bound,ctx,grants=path_context(case);store=Store()
    try:
        store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx);ctx=activate(ctx,store,row)
        proposal=cc.gate_path(case['path'],bound,ctx,grants)
        with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:apply_gate(store,proposal,ctx),range(12)))
        assert sum(results)==1 and store.trace.count('provider:send')==1
    finally:store.close()


def test_cg2_attachment_bytes_change_logical_digest():
    import base64
    import hashlib
    before=envelope();after=envelope()
    for env,encoded in zip((before,after),FIXTURE['attachment_byte_cases']):
        payload=base64.b64decode(encoded,validate=True)
        env['attachments'][0]['sha256']=hashlib.sha256(payload).hexdigest()
        env['attachments'][0]['size']=len(payload)
    assert before['attachments'][0]['size']==after['attachments'][0]['size']
    assert cc.bind_envelope(before).digest!=cc.bind_envelope(after).digest


@pytest.mark.parametrize('case',[p for p in PATHS if p['enabled']],ids=lambda x:x['path'])
@pytest.mark.parametrize('mutation',['missing','forged','stale','wrong_subject','disagrees'])
def test_cg6_activation_requires_authenticated_clock(case,mutation):
    bound,ctx,grants=path_context(case);store=Store()
    try:
        store.versions=ctx.resolver.resolve(ctx.reference('publication'),'publication').data['versions']
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx)
        assert apply_gate(store,cc.gate_path(case['path'],bound,ctx,grants),ctx);ctx,row=store.view(ctx);ctx=activate(ctx,store,row)
        assert cc.gate_path(case['path'],bound,ctx,grants)['status']=='effect_pending'
        refs=tuple((k,r) for k,r in ctx.proofs if k!='clock')
        if mutation=='forged':refs+=(('clock','fixture-forged-clock'),)
        elif mutation in {'stale','wrong_subject','disagrees'}:
            reference=ctx.resolver.issue('clock',dict(now='2026-10-10T12:00:00Z' if mutation=='disagrees' else ctx.now,source='trusted_utc'),
                subject='fixture-other-request' if mutation=='wrong_subject' else 'fixture-request',
                expires='2026-10-10T12:00:00Z' if mutation=='stale' else '2026-10-12T00:00:00Z')
            refs+=(('clock',reference),)
        broken=cc.TrustedContext(ctx.resolver,ctx.now,refs)
        result=cc.gate_path(case['path'],bound,broken,grants)
        assert result['commands']==[] and result['reason']=='clock_untrusted'
        assert store.records[row['request_id']]['state']=='attempting' and 'provider:send' not in store.trace
    finally:store.close()


@pytest.mark.parametrize('prior',[['prompt:acknowledged'],['prompt:inbound','prompt:acknowledged','cancel:observed']])
def test_cg6_prior_history_is_retained_once_without_replay(prior):
    bound,ctx,grants,store,row=accepted_intent();case=PATHS[4]
    try:
        ctx=with_path(ctx,case,prior);ctx=receipt(ctx,row)
        result=cc.gate_path(case['path'],bound,ctx,grants)
        assert result['prior_trace']==prior and result['next_trace']==['receipt:reconcile_requested']
        assert result['trace']==prior+result['next_trace']
        before=store.trace[:]
        assert apply_gate(store,result,ctx)
        assert store.observed_trace==prior+before+['receipt:reconcile_receipt']
        assert not apply_gate(store,result,ctx)  # committed CAS cannot be applied twice
        assert store.observed_trace==prior+before+['receipt:reconcile_receipt']
        assert not any(x.startswith(('prompt:','provider:','cancel:')) for x in store.trace[len(before):])
    finally:store.close()
