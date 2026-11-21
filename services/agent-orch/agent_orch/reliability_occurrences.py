"""Normalize source-proven failures into the scorecard's stable first-attempt identities."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from .reliability_receipts import instant, nights, releases, runtime_history, stamp
from .reliability_score import hosted_score

SURFACES = ('releases', 'hosted', 'nightly')


def observations(sources, surface, since, now, *, known_tests=()):
    if surface == 'hosted':
        hosted = sources['hosted']
        _, rows = hosted_score(hosted, now, since=since)
        if not hosted.get('snapshot'):
            from .quarantine_identity import reviewed_bindings
            from .quarantine_sweep import enrich_rows, qualify_bindings
            enriched, raw_bindings, _ = enrich_rows(rows, hosted)
            bindings = reviewed_bindings(enriched, raw_bindings, hosted.get('signatures', []))
            qualify_bindings(bindings, known_tests)
            for row in rows:
                for cause in row['causes']:
                    key = (str(row['id']), cause.get('job'), cause.get('step'), cause.get('test'))
                    if key in bindings:
                        cause['decision_test'] = bindings[key]['test']
        return rows
    if surface == 'releases':
        rows, _ = releases(sources['releases'], now, sources['required_hosts'], since=since)
        for row in rows:
            row['occurrence'] = f"release/{row['id']}/1"
        return rows
    if not Path(sources['nightly']['root']).is_dir():
        raise FileNotFoundError('nightly receipt root unavailable')
    runtime = sources.get('releases', {}).get('runtime')
    activations = runtime_history(runtime) if runtime else []
    rows = nights(sources['nightly'], now, sources['required_hosts'], activations, since=since)
    for row in rows:
        row['occurrence'] = 'nightly-soak/' + row['id']
    return rows


def occurrence(row, surface, cause, cause_class, *, item=None, test=None, provisional=False, source=None,
               observed_at=None):
    return dict(id=row['occurrence'], created=stamp(instant(row['created'])), surface=surface,
                observed_at=stamp(instant(observed_at or row['created'])),
                cause=cause, cause_class=cause_class, source=source or row['source'],
                item=item, test=test, provisional=provisional,
                next_action='Reproduce the failure and confirm its cause from the original evidence.')


def source_events(surface, row, now, grace):
    if surface == 'hosted':
        # A workflow that has not completed may still produce a miss.
        if row['first'] in ('pending', 'queued', 'in_progress', 'requested', 'waiting', None):
            return [], True
        if row['classification'] not in ('false_red', 'incomplete', 'genuine_red'):
            return [], False
        causes = [occurrence(row, surface, c['cause'], c.get('cause_class'), test=c.get('decision_test') or c.get('test'),
                             source=c.get('evidence') or row['first_source'], observed_at=row.get('finished'),
                             provisional=c['kind'] == 'unclassified')
                  for c in row['causes']
                  if row['classification'] == 'false_red' or c['kind'] == 'unclassified']
        if not causes and row['classification'] == 'incomplete':
            causes.append(occurrence(row, surface, 'first workflow attempt needs classification', None,
                                     provisional=True, source=row['first_source']))
        return causes, any(c['provisional'] for c in causes)
    if surface == 'releases':
        first = [a for a in row['attempts'] if a['attempt'] == 1]
        detail = first[0] if len(first) == 1 else {}
        if detail.get('pending') and instant(row['created']) + timedelta(hours=2) > now:
            return [], True
    else:
        detail = row
        if row['result'] in ('missing', 'incomplete') and instant(row['created']) + grace > now:
            return [], True

    if row['result'] == 'passed':
        return [], False
    if row['result'] == 'incomplete':
        # Ambiguous evidence must have a decision before it can produce work.
        return [occurrence(row, surface, row['cause'], None, provisional=True,
                           source=detail.get('source'))], True
    if row['result'] not in ('failed', 'missing'):
        return [], False

    observed_at = detail.get('observed_at')
    if observed_at and instant(observed_at) > now:
        return [], True
    events = []
    checks = detail.get('failure_checks', {})
    delivery = checks.get('client_delivery', {})
    if delivery.get('classification') == 'delivery_failure':
        if delivery.get('correlation_id'):
            events.append(occurrence(row, surface, 'proactive-note-delivery', 'incomplete journey',
                                     item=delivery.get('cause_target'), source=detail.get('source'),
                                     observed_at=observed_at))
        else:
            events.append(occurrence(row, surface, 'proactive-note-delivery', None, provisional=True,
                                     source=detail.get('source')))
        checks = {name: check for name, check in checks.items() if name != 'client_delivery'}
        if not checks:
            return events, any(event['provisional'] for event in events)

    for name, check in checks.items():
        # The first failing check and bounded phase are a provisional cause
        # signature. Reason text, PIDs and run-specific values are evidence.
        phase = check.get('phase')
        key = name + ('/' + phase if phase in ('precondition', 'subscribe', 'publish', 'receive',
                                                'reconnect', 'recover', 'merge', 'teardown') else '')
        events.append(occurrence(row, surface, surface + '/' + key, 'incomplete journey',
                                 source=detail.get('source'), observed_at=observed_at))
    if not events:
        events.append(occurrence(row, surface, surface + '/' + row['cause'], 'incomplete journey',
                                 item=detail.get('work_item'), source=detail.get('source'), observed_at=observed_at))
    return events, False


def replay_start(cursor, start, overlap=timedelta(minutes=10)):
    return max(start, instant(cursor) - overlap) if cursor else start
