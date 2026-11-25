"""Replay scorecard first attempts into the quarantine transition."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from . import reliability_hosted
from .quarantine_candidates import candidates, merge_seed_occurrences
from .quarantine_identity import enrich_other_python, reviewed_bindings
from .quarantine_ledger import accumulate
from .quarantine_node import enrich as enrich_node
from .quarantine_transition import _key, reconcile
from .reliability_receipts import instant, read
from .reliability_score import hosted_score, source_paths
from .reliability_selection import effective_tests


def sweep(sources, *, ledger_path, state_path, adapter, now=None, restorations=None):
    now = now or datetime.now(timezone.utc)
    hosted = sources['hosted']
    work = sources['work']
    register = read(work['register'])
    existing = {record.get('test') for record in register.get('records', []) if record.get('test')}
    _, rows = hosted_score(hosted, now)
    rows, raw_bindings, notes = enrich_rows(rows, hosted)
    binding = reviewed_bindings(rows, raw_bindings, hosted.get('signatures', []))
    non_code_keys = {(str(row.get('id')), cause.get('job'), cause.get('step'), cause.get('test'))
                     for row in rows for cause in row.get('causes', []) if cause.get('kind') == 'non_code'}
    for key in (raw_bindings.keys() - binding.keys()) & non_code_keys:
        adapter.alert(_key('unreviewed-signature', key),
                      'Quarantine signature or fixture phase needs review: ' + str(key[0]))
    qualify_bindings(binding, existing)
    for note in notes:
        adapter.alert(_key('identity', note), 'Quarantine identity needs triage: ' + note)
    for row in rows:
        for cause in row.get('causes', []):
            if cause.get('kind') == 'unclassified':
                key = [str(row.get('id')), cause.get('job'), cause.get('step'), cause.get('test')]
                adapter.alert(_key('classification', key), 'First-attempt failure needs classification: '
                              + str(row.get('first_source') or row.get('source')))
    observed = candidates(rows, binding)
    retained = accumulate(ledger_path, observed)
    selected = effective_tests(work['selector_checkout'])
    merged = merge_seed_occurrences(register, retained, selected)
    if restorations:
        # Old observations cannot establish a new series after a repair.
        for test, restoration in restorations.items():
            if test in merged:
                if adapter.verify_restoration(test, restoration):
                    boundary = instant(restoration['at'])
                    merged[test] = [row for row in merged[test]
                                    if row.get('created') and instant(row['created']) > boundary]
                else:
                    adapter.alert(_key('restoration', test), 'Restoration evidence incomplete for ' + test)
                    del merged[test]
    return reconcile(state_path, merged, selected, adapter, restorations=restorations, history=existing)



def enrich_rows(rows, hosted):
    rows, node, node_missing = enrich_node(rows, hosted, api=reliability_hosted.api,
                                           download=reliability_hosted.download)
    rows, python, python_missing = enrich_other_python(rows, hosted, api=reliability_hosted.api,
                                                        download=reliability_hosted.download)
    return rows, {**node, **python}, [*node_missing, *python_missing]


def qualify_bindings(binding, existing):
    for value in binding.values():
        suite = value.get('suite', 'daemon')
        if suite != 'javascript' and (suite != 'daemon' or value['test'] not in existing):
            value['test'] = suite + '::' + value['test']

def load_sources(path):
    path = Path(path).expanduser()
    return source_paths(json.loads(path.read_text()), path.resolve().parent)
