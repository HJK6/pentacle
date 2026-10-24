"""Bounded read-only projection of explicit local test-attempt manifests.

No network, evidence discovery, live writes, backfill or inferred success.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import socket
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts/lib'))
import gate_attempts as ga

MAX_MANIFESTS = 32
MAX_ATTEMPTS = 2048
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
ID = re.compile(r'^[A-Za-z0-9._:-]{1,128}$')
SHA = re.compile(r'^[a-f0-9]{40}$')


class EvidenceError(ValueError):
    pass


def _json(path, bound):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > bound:
        raise EvidenceError('missing, symlinked or oversized JSON')
    # Reject duplicate keys/nonfinite values rather than silently accepting a different identity.
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:raise EvidenceError('duplicate JSON key')
            result[k] = v
        return result
    def bad(value):raise EvidenceError('nonfinite JSON value')
    try:
        return json.loads(path.read_text('utf-8'), object_pairs_hook=pairs, parse_constant=bad)
    except (OSError, UnicodeError, ValueError) as exc:
        raise EvidenceError(str(exc)) from exc


def _utc(value):
    if not isinstance(value, str):raise EvidenceError('missing UTC timestamp')
    try:dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:raise EvidenceError('invalid UTC timestamp') from exc
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:raise EvidenceError('timestamp must be UTC')
    return dt.timestamp()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _child(root, *parts):
    p = root
    for part in parts:
        if not isinstance(part,str) or part in ('.','..') or not ID.fullmatch(part):raise EvidenceError('unsafe path component')
        p = p / part
        if p.is_symlink():raise EvidenceError('symlink escape')
    if not p.resolve().is_relative_to(root):raise EvidenceError('path escape')
    return p


def _read_attempt(entry):
    aid = entry.get('attempt_id')
    if not isinstance(aid,str) or not ID.fullmatch(aid):raise EvidenceError('invalid attempt ID')
    path = entry.get('evidence_root')
    if not isinstance(path,str) or not Path(path).is_absolute() or '..' in Path(path).parts:raise EvidenceError('evidence_root must be an explicit absolute local root')
    supplied = Path(path)
    if supplied.is_symlink():raise EvidenceError('symlinked evidence root')
    root = supplied.resolve()
    folder = _child(root,'attempts',aid)
    if not folder.is_dir():raise FileNotFoundError('attempt artifact unavailable')
    paths = list(itertools.islice(folder.iterdir(), 9))
    if len(paths) > 8:raise EvidenceError('event count bound exceeded')
    events = []
    for p in sorted(paths):
        if p.name == 'observations':
            if p.is_symlink():raise EvidenceError('symlinked observations')
            continue  # child observations never allocate another root attempt
        if not re.fullmatch(r'\d{4}-(started|prepared|subject_started|terminal)\.json',p.name):raise EvidenceError('unexpected event artifact')
        e = _json(p,256*1024)
        if not isinstance(e,dict) or e.get('schema') != ga.SCHEMA or e.get('attempt_id') != aid:raise EvidenceError('event schema/identity mismatch')
        if type(e.get('seq')) is not int or not _number(e.get('monotonic_s')):raise EvidenceError('invalid event sequence/timing')
        events.append(e)
    folded = ga.fold_attempt(folder)
    if folded['status'] == 'invalid':raise EvidenceError('; '.join(folded['problems']))
    if not events or events[0]['state'] != 'started':raise EvidenceError('missing started event')
    start = events[0]
    if any(not isinstance(start.get(k),str) or not start[k] for k in ('spec_id','series_id','purpose','host','owner_stream_id','owner_generation','history')):raise EvidenceError('missing identity fields')
    if start['purpose'] not in (*ga.PURPOSES,ga.UNKNOWN):raise EvidenceError('invalid purpose')
    for k in ('spec_id','series_id'):
        if entry.get(k) != start[k]:raise EvidenceError('manifest identity mismatch')
    if entry.get('host') != start['host']:raise EvidenceError('manifest host mismatch')
    if not isinstance(start.get('identities'),dict):raise EvidenceError('missing input identities')
    start_at = _utc(start.get('started_at'))
    previous_at, previous_mono = start_at, 0.0
    if start['monotonic_s'] > 1:raise EvidenceError('started monotonic origin mismatch')
    for event in events:
        at, mono = _utc(event.get('at')),event['monotonic_s']
        if at < previous_at or mono < previous_mono or abs((at-start_at)-mono) > 2:raise EvidenceError('invalid chronology/monotonic duration')
        previous_at,previous_mono = at,mono
    if folded['status'] == 'complete' and folded['primary_outcome'] not in ga.OUTCOMES:raise EvidenceError('invalid terminal outcome')
    # Verify explicit referenced local evidence; unavailable remote references stay unverified.
    refs_missing = 0
    terminal = events[-1] if events[-1]['state']=='terminal' else {}
    refs=terminal.get('evidence',[])
    if not isinstance(refs,list) or len(refs)>256:raise EvidenceError('evidence reference count/schema')
    for ref in refs:
        if not isinstance(ref,dict):raise EvidenceError('invalid evidence reference')
        if ref.get('host') != start['host']:
            refs_missing += 1;continue
        p=Path(ref.get('path',''))
        if not p.is_absolute() or '..' in p.parts or not p.resolve().is_relative_to(root):raise EvidenceError('evidence reference escapes root')
        rel=p.relative_to(root)
        current=root
        for part in rel.parts:
            current=current/part
            if current.is_symlink():raise EvidenceError('evidence symlink escape')
        if not p.is_file():refs_missing+=1;continue
        if type(ref.get('bytes')) is not int or not 0<=ref['bytes']<=64*1024*1024 or p.stat().st_size!=ref['bytes'] or ga.sha256_file(p)!=ref.get('sha256'):raise EvidenceError('evidence digest/size mismatch')
    index_drift=False
    try:
        idx=_json(root/'test-attempts.v1.json',MAX_INDEX_BYTES)
        rows=idx.get('attempts') if isinstance(idx,dict) else None
        if idx.get('schema')!=ga.INDEX_SCHEMA or not isinstance(rows,list) or len(rows)>MAX_ATTEMPTS:raise EvidenceError('invalid derived index')
        matches=[row for row in rows if isinstance(row,dict) and row.get('attempt_id')==aid]
        index_drift=len(matches)!=1 or matches[0]!=folded
    except (EvidenceError,AttributeError):index_drift=True
    selected = start.get('selected', {})
    if not isinstance(selected,dict):raise EvidenceError('invalid selected scope schema')
    requested = selected.get('select', [])
    if not isinstance(requested,list) or any(not isinstance(v,str) for v in requested):raise EvidenceError('invalid requested scope')
    snapshots=[]
    for event in events:
        if event['state'] not in ('prepared','terminal') or 'effective_selection' not in event:continue
        effective=event['effective_selection']
        if not isinstance(effective,list) or any(not isinstance(v,str) or not v for v in effective):raise EvidenceError('invalid resolved scope')
        snapshots.append(sorted(set(effective)))
    if snapshots and any(scope!=snapshots[0] for scope in snapshots):raise EvidenceError('conflicting resolved scope')
    if snapshots and requested and sorted(set(requested))!=snapshots[0]:raise EvidenceError('requested/resolved scope conflict')
    # No current-profile lookup or requested[] fallback can reconstruct a missing declaration.
    acceptance_scope = (ga.digest_of({'profile':start.get('profile'),'target':start.get('target'),'selectors':snapshots[0]})
                        if snapshots else None)
    result=dict(folded,events=events,acceptance_scope=acceptance_scope,history_complete=start.get('history_complete') is True,
                start=start_at,end=previous_at if terminal else None,duration=terminal.get('monotonic_s'),
                reached_subject=any(e['state']=='subject_started' for e in events),index_root=str(root),
                index_drift=index_drift,unverified_refs=refs_missing,
                fingerprint=ga.digest_of([e.get('event_digest') for e in events]))
    return result


def read_manifests(paths, *, local_hosts=None):
    """Manifest entries explicitly grant local roots; no other directory is searched."""
    hosts=set(local_hosts or {socket.gethostname(),socket.gethostname().split('.')[0]})
    if len(paths)>MAX_MANIFESTS:raise EvidenceError('manifest count bound exceeded')
    attempts={};invalid={};missing={};manifest_errors=[];count=0
    for supplied in paths:
        try:
            manifest=_json(Path(supplied).expanduser(),MAX_MANIFEST_BYTES)
            if not isinstance(manifest,dict) or manifest.get('schema')!=ga.MANIFEST_SCHEMA or not isinstance(manifest.get('attempts'),list):raise EvidenceError('manifest schema mismatch')
            entries=manifest['attempts'];count+=len(entries)
            if count>MAX_ATTEMPTS:raise EvidenceError('attempt count bound exceeded')
        except EvidenceError as exc:
            manifest_errors.append({'manifest':str(supplied),'reason':str(exc)});continue
        for index,entry in enumerate(entries):
            if not isinstance(entry,dict):
                manifest_errors.append({'manifest':str(supplied),'reason':'non-object entry'});continue
            key=entry.get('attempt_id',f'invalid-{index}')
            if not isinstance(key,str):key=f'invalid-{index}'
            info={'attempt_id':key,'spec_id':entry.get('spec_id'),'series_id':entry.get('series_id')}
            if entry.get('host') not in hosts:
                missing[key]=dict(info,reason='remote artifact not read');continue
            try:
                a=_read_attempt(entry)
                if key in attempts and attempts[key]['fingerprint']!=a['fingerprint']:raise EvidenceError('conflicting duplicate attempt ID')
                if key not in invalid:attempts[key]=a
            except FileNotFoundError:
                missing[key]=dict(info,reason='local artifact unavailable')
            except (EvidenceError,OSError,TypeError,KeyError,ValueError) as exc:
                attempts.pop(key,None);invalid[key]=dict(info,reason=str(exc))
    return {'attempts':list(attempts.values()),'invalid':list(invalid.values()),'missing':list(missing.values()),'manifest_errors':manifest_errors}


def _quantiles(values):
    values=sorted(float(v) for v in values)
    return {'n':len(values),'p50':values[math.ceil(.5*len(values))-1] if values else None,
            'p95':values[math.ceil(.95*len(values))-1] if values else None,'method':'nearest_rank'}


def _delivery(reports,now):
    output=[];artifacts={}
    for report in reports:
        d=report.get('delivery')
        if not isinstance(d,dict):continue
        try:
            if d.get('kind') not in ('source_only','runtime') or not d.get('artifact_identity') or not d.get('proof_ref'):raise EvidenceError('incomplete delivery identity')
            start=_utc(d.get('source_ready_at'))
            end=_utc(d['runtime_accepted_at']) if d.get('runtime_accepted_at') is not None else None
            if start>now or end is not None and (end<start or end>now):raise EvidenceError('delivery chronology')
            key=(d['kind'],ga.digest_of(d['artifact_identity']))
            previous=artifacts.get(key)
            if previous is None:
                artifacts[key]=dict(d,start=start,end=end)
            else:
                previous['start']=min(previous['start'],start)
                if end is not None and (previous['end'] is None or end<previous['end']):
                    previous.update(end=end,proof_ref=d['proof_ref'])
        except (EvidenceError,TypeError):
            output.append({'report_id':report.get('report_id'),'censored':True,'reason':'invalid delivery contract'})
    for d in artifacts.values():
        row={'artifact_identity':d['artifact_identity'],'proof_ref':d['proof_ref'],'kind':d['kind'],
             'endpoint':'runtime_accepted' if d['kind']=='runtime' else 'source_delivered','censored':d['end'] is None}
        if d['end'] is None:row['elapsed_so_far_s']=round(now-d['start'],6)
        else:row['duration_s']=round(d['end']-d['start'],6)
        output.append(row)
    return output


def summarize(data,specs,*,now,reports=(),usage=()):
    attempts=[a for a in data['attempts'] if a.get('spec_id') in specs]
    invalid=[a for a in data['invalid'] if a.get('spec_id') in specs]
    missing=[a for a in data['missing'] if a.get('spec_id') in specs]
    acceptance=[a for a in attempts if a['purpose']=='acceptance']
    by_series=defaultdict(list)
    all_by_series=defaultdict(list)
    for a in attempts:all_by_series[(a['spec_id'],a['series_id'])].append(a)
    for a in acceptance:by_series[(a['spec_id'],a['series_id'])].append(a)
    bad_series={(a.get('spec_id'),a.get('series_id')) for a in invalid+missing}
    setup_num=setup_den=pass_num=pass_den=pending=unknown=extra=owners=0
    for series,group in by_series.items():
        group.sort(key=lambda a:(a['start'],a['attempt_id']))
        first=group[0]
        lineage=sorted(all_by_series[series],key=lambda a:(a['start'],a['attempt_id']))
        root=lineage[0];seen=set()
        complete=not data['manifest_errors'] and series not in bad_series and root['history_complete'] and root.get('prior_attempt_id') is None
        for a in lineage:
            prior=a.get('prior_attempt_id')
            if prior and prior not in seen:complete=False
            seen.add(a['attempt_id'])
        # Diagnostics establish history but never select acceptance scope or displace its first attempt.
        scope=first['acceptance_scope']
        if any(a['acceptance_scope']!=scope for a in group):complete=False
        if any(a['status']=='complete' and a['acceptance_scope'] is None for a in group):complete=False
        extra+=max(0,len(group)-1)
        known_owners={a.get('owner_stream_id') for a in group}-{None,'unknown'}
        owners+=max(0,len(known_owners)-1)
        if not complete:
            unknown+=1;continue
        if first['reached_subject'] or first['status']=='complete':
            setup_den+=1;setup_num+=int(first['reached_subject'])
        if first['status']=='complete':
            pass_den+=1;pass_num+=int(first['primary_outcome']=='pass')
        else:pending+=1
    outcomes=Counter(a['primary_outcome'] for a in acceptance)
    secondary=Counter();signatures=Counter()
    for a in acceptance:
        for k,v in (a.get('secondary') or {}).items():
            if k in ('cleanup','reporting') and v and v not in ('clean','not_started'):secondary[k]+=1
        ff=a.get('first_failure')
        if a['primary_outcome']!='pass' and isinstance(ff,dict):
            signatures[ga.digest_of({k:ff[k] for k in ('stage','code','test_node') if k in ff})]+=1
    phases=defaultdict(list)
    for a in attempts:
        stages={e['state']:e['monotonic_s'] for e in a['events']}
        if 'prepared' in stages:phases['prepare_preflight'].append(stages['prepared']-stages['started'])
        if 'subject_started' in stages and 'terminal' in stages:phases['subject_cleanup'].append(stages['terminal']-stages['subject_started'])
    scoped={};unknown_qa=0;seen_reports=set();selected_reports=[]
    for row in reports:
        if (row.get('spec_id') not in specs and not set(row.get('spec_ids',[])).intersection(specs)) or row.get('report_id') in seen_reports:continue
        seen_reports.add(row.get('report_id'));selected_reports.append(row)
        verdict=row.get('qa_verdict')
        if verdict not in ('accept','reject'):continue
        if row.get('spec_id') not in specs:
            # Known QA commission for another spec is not QA for a broader seat tag.
            if row.get('spec_id') is None:unknown_qa+=1
            continue
        if not row.get('qa_surface') or type(row.get('qa_cycle')) is not int or row['qa_cycle']<=0 or not SHA.fullmatch(str(row.get('target_sha',''))):
            unknown_qa+=1;continue
        key=(row['spec_id'],row['qa_surface'],row['qa_cycle'])
        bucket=scoped.setdefault(key,{'spec_id':key[0],'surface':key[1],'cycle':key[2],'accept':0,'reject':0})
        bucket[verdict]+=1
    # Generation + timed placement in one nonoverlapping root is required. Child events are not charged again.
    attributed=unknown_usage=0;providers=defaultdict(Counter);seen_usage=set()
    for row in usage:
        rid=row.get('id')
        if rid in seen_usage:continue
        seen_usage.add(rid)
        generation=row.get('generation');at=row.get('observed')
        matches=[a for a in attempts if generation not in (None,'unknown') and a.get('owner_generation')==generation
                 and a.get('owner_stream_id')==row.get('stream_id') and type(at) in (int,float)
                 and a['end'] is not None and a['start']<=at<=a['end']]
        if len(matches)!=1:
            unknown_usage+=1;continue
        attributed+=1
        for k,v in row.get('tokens',{}).items():
            if k!='reasoning' and type(v) is int and v>=0:providers[row.get('provider','unknown')][k]+=v
    return {'schema':'pentacle.test-health.v1','evidence_scope':'all supplied histories; no inferred uninstrumented coverage',
            'first_attempt_setup':{'numerator':setup_num,'denominator':setup_den},
            'first_attempt_acceptance':{'numerator':pass_num,'denominator':pass_den},
            'coverage':{'valid_attempts':len(attempts),'invalid_attempts':len(invalid),'missing_artifacts':len(missing),
                        'pending_series':pending,'unknown_history_series':unknown,'observed_acceptance_series':len(by_series),
                        'index_drift_roots':len({a['index_root'] for a in attempts if a['index_drift']}),
                        'unverified_evidence_refs':sum(a['unverified_refs'] for a in attempts),
                        'manifest_errors':data['manifest_errors'],'issues':invalid+missing,
                        'uninstrumented_history':'unknown'},
            'purposes':dict(sorted(Counter(a['purpose'] for a in attempts).items())),
            'primary_outcomes':dict(sorted(outcomes.items())), 'secondary_outcomes':dict(sorted(secondary.items())),
            'churn':{'additional_acceptance_attempts':extra,'replacement_owners':owners,
                     'repeated_failure_signatures':sum(n-1 for n in signatures.values())},
            'duration_s':_quantiles([a['duration'] for a in attempts if a['duration'] is not None]),
            'phase_duration_s':{k:_quantiles(v) for k,v in sorted(phases.items())},
            'unemitted_phase_detail':'separate preparation/preflight/subject/cleanup endpoints unknown',
            'qa':{'scoped_reports':sum(r['accept']+r['reject'] for r in scoped.values()),'unknown_provenance_reports':unknown_qa,'surfaces':sorted(scoped.values(),key=lambda r:(r['spec_id'],r['surface'],r['cycle']))},
            'delivery':_delivery(selected_reports,now),
            'usage':{'attributed_records':attributed,'unknown_records':unknown_usage,
                     'by_provider':{k:dict(v) for k,v in sorted(providers.items())},
                     'reasoning':'subset of output, never added; untimed/cumulative/ambiguous placement unknown'}}
