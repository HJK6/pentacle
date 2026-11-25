"""Retain distinct classified first attempts beyond the scorecard window."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

from .quarantine_transition import _save


def accumulate(path, observations):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {'version': 1, 'tests': {}}
        if state.get('version') != 1 or not isinstance(state.get('tests'), dict):
            raise ValueError('invalid quarantine occurrence ledger')
        changed = False
        for test, occurrences in observations.items():
            if not isinstance(test, str) or '::' not in test:
                raise ValueError('invalid collected test identity')
            ledger = state.setdefault('native_tests', {}) if test.startswith('test/') else state['tests']
            known = ledger.setdefault(test, {})
            for occurrence in occurrences:
                oid = occurrence.get('id')
                if not isinstance(oid, str) or not oid or not occurrence.get('source'):
                    raise ValueError('invalid first-attempt occurrence')
                old = known.get(oid)
                if old:
                    immutable_old = {k: v for k, v in old.items() if k != 'later_attempts'}
                    immutable_new = {k: v for k, v in occurrence.items() if k != 'later_attempts'}
                    if immutable_old != immutable_new:
                        raise ValueError('conflicting first-attempt occurrence')
                    # A later retry enriches, never erases, the original receipt.
                    attempts = {int(a['number']): a for a in old.get('later_attempts', [])}
                    for attempt in occurrence.get('later_attempts', []):
                        n = int(attempt['number'])
                        if n in attempts:
                            prior = attempts[n]
                            if ({k: v for k, v in prior.items() if k != 'outcome'} !=
                                    {k: v for k, v in attempt.items() if k != 'outcome'}
                                    or (prior.get('outcome') is not None and attempt.get('outcome') is not None
                                        and prior['outcome'] != attempt['outcome'])):
                                raise ValueError('conflicting retry evidence')
                            attempts[n] = {**attempt, 'outcome': attempt.get('outcome') or prior.get('outcome')}
                        else:
                            attempts[n] = attempt
                    updated = {**old, 'later_attempts': [attempts[n] for n in sorted(attempts)]}
                    if updated != old:
                        known[oid] = updated
                        changed = True
                else:
                    known[oid] = occurrence
                    changed = True
        if changed:
            _save(path, state)
        # Historical Node entries in tests came from lossy XML. Keep the Python history.
        eligible = {test: rows for test, rows in state['tests'].items() if not test.startswith('test/')}
        eligible.update(state.get('native_tests', {}))
        return {test: [items[key] for key in sorted(items)] for test, items in eligible.items()}
