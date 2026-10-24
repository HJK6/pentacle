"""Hand-computed attempt/coverage oracles; no live ledger or provider effects."""
from __future__ import annotations
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT / 'scripts/lib'))
sys.path.insert(0,str(ROOT / 'services/chat-stream-v2/tools'))
import gate_attempts as ga
SPEC = 'spec_fixture__attempts'
BASE = datetime(2026,10,24,tzinfo=timezone.utc)

def metrics():
    return importlib.import_module('test_attempt_metrics')

def utc(s):return (BASE+timedelta(seconds=s)).isoformat().replace('+00:00','Z')

def attempt(tmp, aid, series='a', outcome='pass', *, start=0, subject=True, terminal=True,
            purpose='acceptance', history=True, prior=None, owner='local:owner', generation='g', target='unit'):
    evidence=tmp/aid;folder=evidence/'attempts'/aid;folder.mkdir(parents=True)
    states=['started']+(['prepared','subject_started'] if subject else [])+(['terminal'] if terminal else [])
    offsets={'started':0,'prepared':2,'subject_started':3,'terminal':10}
    identity=dict(spec_id=SPEC,series_id=series,purpose=purpose,history='first_observed' if not prior else 'continues',
                  history_complete=history,prior_attempt_id=prior,host='local',owner_stream_id=owner,
                  owner_generation=generation,target=target,profile='fixture',started_at=utc(start),
                  identities={'source':{'sha':'a'*40},'harness':'b'*64,'config':'c'*64})
    for seq,state in enumerate(states,1):
        event=dict(schema=ga.SCHEMA,attempt_id=aid,seq=seq,state=state,at=utc(start+offsets[state]),monotonic_s=offsets[state])
        if state=='started':event.update(identity)
        if state=='terminal':event.update(outcome=outcome,first_failure=None if outcome=='pass' else {'stage':'setup' if not subject else 'subject','code':'fixture'},secondary={})
        event['event_digest']=ga.digest_of(event)
        (folder/f'{seq:04d}-{state}.json').write_text(json.dumps(event))
    ga.write_index(evidence)
    return dict(attempt_id=aid,spec_id=SPEC,series_id=series,evidence_root=str(evidence),host='local')

def manifest(tmp,entries,name='manifest.json'):
    p=tmp/name;p.write_text(json.dumps({'schema':ga.MANIFEST_SCHEMA,'attempts':entries}));return p

def read(tmp,entries,**kw):
    data=metrics().read_manifests([manifest(tmp,entries)],local_hosts={'local'})
    return metrics().summarize(data,{SPEC},now=BASE.timestamp()+100,**kw)

def test_failed_setup_then_pass_keeps_first_denominator_and_stable_lineage(tmp_path):
    a=attempt(tmp_path,'first',outcome='setup_failure',subject=False)
    b=attempt(tmp_path,'second',start=20,prior='first',owner='local:replacement')
    health=read(tmp_path,[a,b])
    assert health['first_attempt_setup']=={'numerator':0,'denominator':1}
    assert health['first_attempt_acceptance']=={'numerator':0,'denominator':1}
    assert health['churn']['additional_acceptance_attempts']==1
    assert health['churn']['replacement_owners']==1
    assert health['primary_outcomes']=={'pass':1,'setup_failure':1}
    assert health['duration_s']=={'n':2,'p50':10.0,'p95':10.0,'method':'nearest_rank'}

def test_diagnostic_and_negative_controls_do_not_displace_first_acceptance(tmp_path):
    entries=[attempt(tmp_path,'diag',purpose='diagnostic',outcome='harness_failure'),
             attempt(tmp_path,'neg',purpose='negative_control',outcome='product_failure',start=20),
             attempt(tmp_path,'accept',start=40)]
    h=read(tmp_path,entries)
    assert h['first_attempt_acceptance']=={'numerator':1,'denominator':1}
    assert h['purposes']=={'acceptance':1,'diagnostic':1,'negative_control':1}
    assert h['primary_outcomes']=={'pass':1}

def test_pending_unknown_and_missing_prior_are_censored(tmp_path):
    entries=[attempt(tmp_path,'pending',terminal=False,subject=False),
             attempt(tmp_path,'unknown',series='u',history=False),
             attempt(tmp_path,'missing',series='m',prior='lost',history=False)]
    h=read(tmp_path,entries)
    assert h['first_attempt_acceptance']['denominator']==0
    assert h['coverage']['pending_series']==1
    assert h['coverage']['unknown_history_series']==2
    assert h['duration_s']['n']==2

def test_duplicate_manifests_dedupe_and_conflicting_ids_are_invalid(tmp_path):
    a=attempt(tmp_path,'a');p=manifest(tmp_path,[a])
    m=metrics().read_manifests([p,p],local_hosts={'local'})
    assert len(m['attempts'])==1
    copy=tmp_path/'other';copy.mkdir();b=attempt(copy,'a',outcome='setup_failure',subject=False)
    h=read(tmp_path,[a,b])
    assert h['coverage']['invalid_attempts']==1
    assert h['first_attempt_acceptance']['denominator']==0

def test_secondary_cleanup_and_repeated_signature_dont_erase_assertion(tmp_path):
    a=attempt(tmp_path,'a',outcome='product_failure');b=attempt(tmp_path,'b',outcome='product_failure',start=20,prior='a')
    p=Path(a['evidence_root'])/'attempts/a/0004-terminal.json';e=json.loads(p.read_text());e['secondary']={'cleanup':'failed','reporting':['missing_junit']};e['event_digest']=ga.digest_of({k:v for k,v in e.items() if k!='event_digest'});p.write_text(json.dumps(e));ga.write_index(Path(a['evidence_root']))
    h=read(tmp_path,[a,b]);assert h['primary_outcomes']=={'product_failure':2}
    assert h['secondary_outcomes']=={'cleanup':1,'reporting':1}
    assert h['churn']['repeated_failure_signatures']==1

def test_timeout_is_not_invented_product_failure(tmp_path):
    h=read(tmp_path,[attempt(tmp_path,'a',outcome='timeout_unclassified')])
    assert h['primary_outcomes']=={'timeout_unclassified':1}

@pytest.mark.parametrize('damage',['partial','skew','duration','digest','symlink'])
def test_corrupt_timing_and_escaped_evidence_fail_closed(tmp_path,damage):
    a=attempt(tmp_path,'a');p=Path(a['evidence_root'])/'attempts/a/0004-terminal.json'
    if damage=='partial':p.write_text('{')
    elif damage=='symlink':
        outside=tmp_path/'outside.json';outside.write_bytes(p.read_bytes());p.unlink();p.symlink_to(outside)
    else:
        e=json.loads(p.read_text())
        if damage=='skew':e['at']=utc(-1)
        elif damage=='duration':e['monotonic_s']=10000
        else:e['outcome']='setup_failure'
        if damage!='digest':e['event_digest']=ga.digest_of({k:v for k,v in e.items() if k!='event_digest'})
        p.write_text(json.dumps(e))
    h=read(tmp_path,[a]);assert h['coverage']['invalid_attempts']==1
    assert h['first_attempt_acceptance']['denominator']==0

def test_remote_missing_artifact_is_coverage_without_network(tmp_path):
    a=dict(attempt_id='a',spec_id=SPEC,series_id='s',evidence_root='/unavailable/remote',host='another-host')
    h=read(tmp_path,[a]);assert h['coverage']['missing_artifacts']==1
    assert h['first_attempt_setup']['denominator']==0

def test_index_drift_is_reported_but_events_remain_authoritative(tmp_path):
    a=attempt(tmp_path,'a');p=Path(a['evidence_root'])/'test-attempts.v1.json';p.write_text('{}')
    h=read(tmp_path,[a]);assert h['coverage']['index_drift_roots']==1
    assert h['first_attempt_acceptance']['numerator']==1

def test_delivery_is_artifact_bound_and_runtime_open_is_censored(tmp_path):
    a=attempt(tmp_path,'a')
    reports=[{'report_id':'r','spec_id':SPEC,'delivery':dict(kind='runtime',source_ready_at=utc(0),runtime_accepted_at=None,artifact_identity='sha-a',proof_ref='receipt')}]
    h=read(tmp_path,[a],reports=reports)
    assert h['delivery'][0]['censored'] is True
    assert h['delivery'][0]['elapsed_so_far_s']==100
    reports[0]['delivery']['runtime_accepted_at']=utc(40)
    h=read(tmp_path,[a],reports=reports);assert h['delivery'][0]['duration_s']==40
    assert h['delivery'][0]['endpoint']=='runtime_accepted'

def test_qa_dedup_by_report_and_unknown_join_stays_unknown(tmp_path):
    row={'report_id':'r','spec_id':SPEC,'qa_verdict':'reject','qa_surface':'surface','qa_cycle':1,'target_sha':'a'*40}
    h=read(tmp_path,[attempt(tmp_path,'a')],reports=[row,row,{'report_id':'u','spec_id':SPEC,'qa_verdict':'accept'}])
    assert h['qa']['scoped_reports']==1 and h['qa']['unknown_provenance_reports']==1
    assert h['qa']['surfaces'][0]['reject']==1

def test_usage_requires_matching_generation_time_and_unambiguous_root(tmp_path):
    a=attempt(tmp_path,'a')
    rows=[dict(id='known',provider='claude',stream_id='local:owner',generation='g',observed=BASE.timestamp()+5,tokens={'output':5}),
          dict(id='unknown',provider='codex',stream_id='local:owner',generation='unknown',observed=BASE.timestamp()+5,tokens={'output':9},reasoning=3)]
    h=read(tmp_path,[a],usage=rows);assert h['usage']['attributed_records']==1
    assert h['usage']['unknown_records']==1
    assert h['usage']['by_provider']['claude']=={'output':5}
    b=attempt(tmp_path,'b',series='concurrent',start=1)
    h=read(tmp_path,[a,b],usage=rows);assert h['usage']['attributed_records']==0

def test_rollup_parser_admits_repeatable_manifest_without_changing_old_flags():
    import usage_rollup as ur
    args=ur.build_parser().parse_args(['--spec',SPEC,'--attempt-manifest','a','--attempt-manifest','b','--json'])
    assert args.attempt_manifest==['a','b'] and args.spec==[SPEC]


def test_target_drift_withholds_first_claim_but_preserves_failures(tmp_path):
    a=attempt(tmp_path,'old',target='original',outcome='product_failure')
    b=attempt(tmp_path,'new',target='changed',prior='old',start=20)
    h=read(tmp_path,[a,b]);assert h['coverage']['unknown_history_series']==1
    assert h['first_attempt_acceptance']['denominator']==0
    assert h['primary_outcomes']=={'pass':1,'product_failure':1}


def test_malformed_manifest_is_visible_and_cannot_claim_complete_history(tmp_path):
    a=attempt(tmp_path,'a');good=manifest(tmp_path,[a]);bad=tmp_path/'bad.json';bad.write_text('{')
    m=metrics().read_manifests([good,bad],local_hosts={'local'})
    h=metrics().summarize(m,{SPEC},now=BASE.timestamp()+100)
    assert h['coverage']['manifest_errors'] and h['first_attempt_acceptance']['denominator']==0
