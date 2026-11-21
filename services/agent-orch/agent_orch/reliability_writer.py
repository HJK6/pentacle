"""Replay durable first attempts and converge their work through the memory publisher."""
from __future__ import annotations

from datetime import date, timedelta
import fcntl
import hashlib
from pathlib import Path
from subprocess import SubprocessError, check_output

from .quarantine_transition import _key, _save
from .reliability_occurrences import SURFACES, observations, replay_start, source_events
from .reliability_receipts import instant, read, stamp
from .reliability_work import canonical, item, weekly

TERMINAL = {'completed', 'deprecated'}
RECONCILE_ERRORS = (KeyError, OSError, TypeError, ValueError, SubprocessError)
MARKER = 'Scorecard observation'


def valid_item(work_root, name):
    if not isinstance(name, str):
        raise ValueError('work target unavailable')
    return item(work_root, name)


class WorkWriter:
    def __init__(self, work, settings, publisher, alert):
        self.root = Path(work['work_root'])
        self.path = Path(work['register'])
        self.settings = settings
        self.start = instant(settings['start_at'])
        self.grace = timedelta(seconds=settings.get('nightly_receipt_grace_seconds', 4500))
        self.publisher = publisher
        self.alert = alert
        if self.grace < timedelta(0):
            raise ValueError('invalid nightly receipt grace')

    def _persist(self):
        _save(self.path, self.register)

    def run(self, sources, now, surfaces=SURFACES):
        # Same register lock as quarantine. Mark each incoming event pending
        # before a publisher call and advance a source only after its ingest.
        with self.path.with_name(self.path.name + '.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.register = read(self.path)
            state = self.register.setdefault('writer', {'cursors': {}, 'revisit': {}})
            self.pending = self.register.setdefault('pending', [])
            failed = []
            for surface in surfaces:
                try:
                    cutoff = now
                    if surface == 'hosted' and sources[surface].get('snapshot'):
                        cutoff = min(now, instant(sources[surface]['complete_through']))
                    self._ingest(sources, state, surface, cutoff)
                    if cutoff < now:
                        raise ValueError('hosted snapshot predates requested cutoff')
                except RECONCILE_ERRORS as exc:
                    failed.append(self._fallback('source', surface, 'Cannot reconcile ' + surface, exc))
            aliases = self._aliases()
            links = {}
            unresolved_sources = set()
            confirmed_sources = set()
            for entry in tuple(self.pending):
                if entry.get('writer_key') is None or entry['surface'] not in surfaces:
                    continue
                try:
                    if entry['provisional'] and not entry['cause_class']:
                        raise ValueError('classification required')
                    target = self._write(entry, aliases)
                    links.setdefault(entry['source'], set()).add(target)
                except RECONCILE_ERRORS as exc:
                    issue = self._fallback('occurrence', entry['writer_key'],
                                           'Pending work for ' + entry['source'], exc)
                    failed.append(issue)
                    if issue['alert'] == 'confirmed':
                        confirmed_sources.add(entry['source'])
                    else:
                        unresolved_sources.add(entry['source'])
                    continue
                self.pending.remove(entry)
                self._persist()
            for source, decision in aliases.items():
                try:
                    self._merge(source, decision, aliases, now)
                except RECONCILE_ERRORS as exc:
                    failed.append(self._fallback('alias', source, 'Pending reviewed cause merge', exc))
            try:
                self._maintenance(now)
            except RECONCILE_ERRORS as exc:
                failed.append(self._fallback('prevention', 'work-history', 'Cannot attach scorecard follow-up', exc))
            return {'status': 'incomplete' if failed else 'complete', 'issues': failed,
                    'handled_sources': sorted((links.keys() | confirmed_sources) - unresolved_sources),
                    'pending': sorted({entry['id'] for entry in self.pending}),
                    'links': {source: sorted(items) for source, items in links.items()},
                    'cursors': state['cursors']}

    def _fallback(self, kind, key, text, exc):
        identity = _key('scorecard-' + kind, key)
        alerted = self.register.setdefault('writer_alerts', {})
        if identity not in alerted:
            try:
                self.alert(identity, text)
                alerted[identity] = True
                self._persist()
            except RECONCILE_ERRORS:
                return {'key': key, 'kind': kind, 'error': type(exc).__name__, 'alert': 'unconfirmed'}
        return {'key': key, 'kind': kind, 'error': type(exc).__name__, 'alert': 'confirmed'}

    def _ingest(self, sources, state, surface, now):
        old_cursor = state['cursors'].get(surface)
        if now < self.start or (old_cursor and instant(old_cursor) > now):
            raise ValueError('replay cutoff predates the source cursor')
        since = replay_start(old_cursor, self.start)
        revisits = state['revisit'].setdefault(surface, {})
        if revisits:
            since = min(since, *(instant(at) for at in revisits.values()))
        known_tests = {record['test'] for record in self.register['records'] if record.get('test')}
        rows = [(row, *source_events(surface, row, now, self.grace))
                for row in observations(sources, surface, since, now, known_tests=known_tests)]
        for row, events, revisit in rows:
            if revisit:
                revisits[row['occurrence']] = row['created']
            else:
                revisits.pop(row['occurrence'], None)
            # Replace the provisional snapshot with reviewed classification.
            # Published work is retained until a reviewed alias consolidates it.
            keys = {self._observation_key(e) for e in events}
            self.pending[:] = [e for e in self.pending if e.get('writer_key') is None
                               or e['id'] != row['occurrence'] or e['cause_class'] or e['writer_key'] in keys]
            for event in events:
                key = self._observation_key(event)
                matching = [old for old in self.pending if old.get('writer_key') == key]
                if matching:
                    matching[0].update(event)
                else:
                    self.pending.append({**event, 'writer_key': key})
        state['cursors'][surface] = stamp(now)
        self._persist()

    def _aliases(self):
        aliases = self.settings.get('cause_aliases', {})
        names = {}
        for source, decision in aliases.items():
            names[source] = decision['cause']
            if not all(decision.get(f) for f in ('cause', 'reviewer', 'revision', 'evidence')):
                raise ValueError('cause aliases require reviewed evidence')
        for source in aliases:
            canonical(source, names)
        return aliases

    @staticmethod
    def _observation_key(event):
        return _key('observation', [event['id'], event['cause'], event.get('test')])

    def _write(self, event, aliases):
        names = {source: decision['cause'] for source, decision in aliases.items()}
        cause = canonical(event['cause'], names)
        records = self.register['records']
        record = self._record(cause)
        suggested = self._target(event, cause, aliases)
        if record and cause == 'proactive-note-delivery' and record['item'] != suggested:
            raise ValueError('recorded and approved delivery targets disagree')
        target = record['item'] if record else suggested
        if record is None and target:
            same_item = [r for r in records if r['item'] == target and not r.get('merged_into')]
            if len(same_item) > 1:
                raise ValueError('multiple canonical records for work item')
            record = same_item[0] if same_item else None
        if record and event['id'] in record.get('occurrences', []):
            self._remember_alias(record, event['cause'])
            return target
        generated = target is None and record is None
        if target is None:
            target = 'pentacle__scorecard_' + hashlib.sha256(cause.encode()).hexdigest()[:24]
            owner = self.settings.get('owners', {}).get(cause, self.settings['owner'])
            if not owner:
                raise ValueError('scorecard owner unavailable')
            if not list(self.root.glob('*/' + target + '/summary.md')):
                self.publisher.create_item(target, 'Scorecard failure',
                                           'Diagnose a recorded release reliability failure.', owner=owner)
        current = valid_item(self.root, target)
        when = instant(event['created'])
        if current['status'] in TERMINAL:
            if not self._after_closure(current, record, instant(event.get('observed_at') or event['created'])):
                raise ValueError('occurrence predates the terminal work record')
            if record and record.get('test'):
                # The quarantine transition alone verifies a restored test's new series.
                raise ValueError('quarantine recurrence requires verified restoration')
            self.publisher.reactivate(target, event['id'], 'The same failure has recurred.', event['next_action'])
            if valid_item(self.root, target)['status'] in TERMINAL:
                raise ValueError('work reactivation readback incomplete')
        fact = {k: v for k, v in event.items() if k not in ('writer_key', 'item')}
        fact['occurrence'] = fact['id']
        fact['id'] = self._observation_key({**event, 'cause': cause})
        self.publisher.attach_observations(target, MARKER, [fact],
                                           next_action=event['next_action'] if generated else None)

        if record is None:
            record = dict(cause=cause, item=target, first_seen=event['created'], occurrences=[],
                          provisional=event['surface'] in ('releases', 'nightly')
                          and cause != 'proactive-note-delivery')
            records.append(record)
        self._remember_alias(record, event['cause'])
        record['first_seen'] = stamp(min(when, instant(record['first_seen'])))
        record.setdefault('occurrences', []).append(event['id'])
        self._history(event, fact['id'], target)
        return target

    @staticmethod
    def _remember_alias(record, cause):
        if cause != record['cause'] and cause not in record.setdefault('aliases', []):
            record['aliases'].append(cause)

    def _after_closure(self, current, record, when):
        boundary = (record or {}).get('closed_at')
        memory = self.root.parent
        if (memory / '.git').exists():
            path = Path(current['source']).relative_to(memory)
            landed = check_output(['git', '-C', str(memory), 'log', '-1', '--no-renames', '--diff-filter=A',
                                   '--format=%cI', '--', str(path)], text=True, timeout=15).strip()
            if landed:
                boundary = stamp(max(instant(landed), instant(boundary))) if boundary else landed
        return when > instant(boundary) if boundary else when.date() > date.fromisoformat(current['updated'])

    def _record(self, cause):
        matches = [r for r in self.register['records'] if not r.get('merged_into')
                   and (r['cause'] == cause or cause in r.get('aliases', []))]
        if len(matches) > 1:
            raise ValueError('conflicting cause owners')
        return matches[0] if matches else None

    def _target(self, event, cause, aliases):
        if cause == 'proactive-note-delivery':
            configured = self.settings.get('delivery_item')
            if not configured or event.get('item') != configured:
                raise ValueError('delivery receipt is missing its approved cause target')
            return configured
        if event.get('test'):
            tests = {event['test'], 'daemon::' + event['test']}
            matches = [r['item'] for r in self.register['records'] if r.get('test') in tests]
            if len(set(matches)) > 1:
                raise ValueError('ambiguous quarantine cause target')
            if matches:
                return matches[0]
        rule = aliases.get(event['cause'], {})
        receipt_item = event.get('item')
        return (rule.get('item') or self.settings.get('existing_items', {}).get(cause)
                or (receipt_item if receipt_item != 'none' else None))

    def _history(self, event, event_id, target):
        if not event['cause_class']:
            return
        events = self.register.setdefault('history', {}).setdefault('events', [])
        events.append({'id': event_id, 'kind': 'class', 'at': event['created'], 'class': event['cause_class'],
                       'occurrence': event['id'], 'item': target, 'test': event.get('test')})

    def _merge(self, source, decision, aliases, now):
        original = self._record(source)
        target_key = canonical(decision['cause'], {k: v['cause'] for k, v in aliases.items()})
        target = self._record(target_key)
        if not original or not target or original is target:
            return
        if (not original.get('provisional') or original.get('test')
                or not original['item'].startswith('pentacle__scorecard_') or not decision.get('closure_report')):
            raise ValueError('merge requires reviewed provisional closure authority')
        current = valid_item(self.root, target['item'])
        if current['status'] in TERMINAL:
            raise ValueError('canonical merge owner must be open')
        evidence = self.publisher.observations(original['item'], MARKER)
        self.publisher.attach_observations(target['item'], MARKER, evidence + [{
            'id': _key('cause-merge', [source, target_key, decision['revision']]),
            'cause': source, 'successor': target_key, 'reviewer': decision['reviewer'],
            'revision': decision['revision'], 'source': decision['evidence']}])
        self.publisher.supersede(original['item'], target['item'], decision['closure_report'])
        if valid_item(self.root, original['item'])['status'] not in TERMINAL:
            raise ValueError('superseded work readback incomplete')
        target['occurrences'] = sorted(set(target.get('occurrences', [])) | set(original.get('occurrences', [])))
        target['first_seen'] = stamp(min(instant(target['first_seen']), instant(original['first_seen'])))
        if source not in target.setdefault('aliases', []):
            target['aliases'].append(source)
        original.update(merged_into=target_key, merged_at=stamp(now))
        self._persist()


    def _maintenance(self, now):
        history = self.register.get('history', {})
        aliases = history.get('class_aliases', {})
        grouped = {}
        for event in history.get('events', []):
            if event.get('occurrence') and event.get('item') and instant(event['at']) <= now:
                name = canonical(event['class'], aliases)
                grouped.setdefault(name, {})[event['occurrence']] = event
        records = self.register['records']
        preventive = history.setdefault('preventive_actions', {})
        for name, entries in grouped.items():
            if len(entries) < 2 or any(canonical(previous, aliases) == name for previous in preventive):
                continue
            chosen = self._maintenance_owner(records, entries)
            if chosen:
                self.publisher.attach_observations(chosen, 'Scorecard preventive action', [{
                    'id': _key('prevention', name), 'class': name,
                    'occurrences': sorted(entries),
                    'tests': sorted({e['test'] for e in entries.values() if e.get('test')}),
                    'next_action': 'Review the repeated failure class and record one preventive check.'}])
                preventive[name] = chosen
                self._persist()
        review = weekly(history, now)['review'] if history.get('from_inception') else None
        if review and review['status'] == 'due' and review['id'] not in history.get('review_assignments', {}):
            candidates = {oid: event for name in review['dominant_classes']
                          for oid, event in grouped.get(name, {}).items()}
            target = self._maintenance_owner(records, candidates)
            if target:
                self.publisher.attach_observations(target, 'Scorecard weekly review', [{
                    'id': review['id'], 'classes': review['dominant_classes'],
                    'next_action': 'Review the dominant class and record evidence for its preventive check.'}])
                history.setdefault('review_assignments', {})[review['id']] = target
                self._persist()

    def _maintenance_owner(self, records, entries):
        name_by_item = {record['item']: record['cause'] for record in records}
        for event in sorted(entries.values(), key=lambda e: (instant(e['at']), e['occurrence'])):
            cause = name_by_item.get(event['item'])
            record = self._record(cause) if cause else None
            if record and valid_item(self.root, record['item'])['status'] not in TERMINAL:
                return record['item']
        return None
