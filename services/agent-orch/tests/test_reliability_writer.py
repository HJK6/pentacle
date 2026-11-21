"""Exercise the command and revision-checked publisher transport in isolated work."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from agent_orch import cli, reliability_run
from agent_orch.quarantine_decisions import MemoryPublisher
from test_reliability_score import NOW, change_json, make_work, pkg, sources, stamp, write


def prepare_item(root, name, *, status='backlog', owner='component-owner'):
    summary = Path(make_work(root, name, status))
    summary.write_text(summary.read_text().replace('owner: operator', 'owner: ' + owner))
    summary.with_name('spec.md').write_text(f'---\nid: spec_{name}\nstatus: {status}\n---\n\n## Tracking\n')
    return summary.parent


class FixturePublisher:
    def __init__(self, root):
        self.root = root
        self.requests = []
        self.interrupt_after = None

    def __call__(self, request):
        self.requests.append(request)
        operation = request['operation']
        if operation == 'read':
            path = self.root / request['path']
            return {'ok': True, 'revision': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'content': path.read_text()}
        if operation == 'create':
            metadata = request['metadata']
            name = metadata['repo'] + '__' + metadata['topic']
            assert not list((self.root / 'work').glob('*/' + name))
            prepare_item(self.root / 'work', name, owner=metadata['owner'])
        else:
            for path, rev in request['expected'].items():
                assert hashlib.sha256((self.root / path).read_bytes()).hexdigest() == rev
            if operation == 'update':
                for path, document in request['documents'].items():
                    dest = self.root / path
                    dest.write_text('---' + dest.read_text().split('---')[1] + '---\n' + document['body'])
            elif operation == 'move':
                old = (self.root / next(iter(request['expected']))).parent
                new = self.root / 'work' / request['status'] / old.name
                new.parent.mkdir(exist_ok=True)
                previous = old.parent.name
                old.rename(new)
                for path in new.glob('*.md'):
                    path.write_text(path.read_text().replace('status: ' + previous,
                                                              'status: ' + request['status']))
            else:
                raise AssertionError(operation)
        if operation == self.interrupt_after:
            self.interrupt_after = None
            raise OSError('simulated lost publisher reply')
        return {'ok': True, 'saved': 'accepted'}


@pytest.fixture
def writer(tmp_path, sources, monkeypatch):
    wire = FixturePublisher(tmp_path)
    alerts = []
    monkeypatch.setattr(reliability_run, 'MemoryPublisher', lambda *args, **kw:
                        MemoryPublisher(*args, **kw, submit=wire))
    monkeypatch.setattr(reliability_run.ProductionAdapter, 'scorecard_alert',
                        lambda self, key, message: alerts.append(key))
    config = dict(sources=write(tmp_path / 'sources.json', sources), memory_root=str(tmp_path),
                  owner='front-desk', epic='epic_goal', machine='fixture', requesting_stream_id='fixture:fd',
                  start_at=min(sources["nightly"]["bindings"]))
    path = tmp_path / 'writer.json'
    write(path, config)
    return wire, alerts, path


def cli_args(config, surface=None, now=NOW):
    return ['reliability', 'reconcile', '--config', str(config), '--as-of', stamp(now)] + (
        ['--surface', surface] if surface else [])


def records(sources):
    return json.loads(Path(sources['work']['register']).read_text())['records']


def fail_workflow(sources, index=0):
    path = Path(sources['hosted']['snapshot'])
    runs = json.loads(path.read_text())
    runs[index].update(first='failure', latest='success', attempt=2, failures=[pkg()])
    write(path, runs)


def fail_night(sources, index, **checks):
    aliases = sources['nightly']['bindings']
    slot = list(aliases)[index]
    binding = aliases[slot]
    path = Path(sources['nightly']['root']) / binding['directory'] / 'evidence/receipt.json'
    change_json(path, result='FAIL', checks=checks)
    binding['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return slot, path


def test_interrupted_publisher_and_concurrent_replay(writer, sources):
    wire, alerts, config = writer
    fail_workflow(sources)
    args = cli_args(config, 'hosted')
    wire.interrupt_after = 'update'
    assert cli.main(args) == 2
    register = json.loads(Path(sources['work']['register']).read_text())
    assert register['pending'][0]['id'] == 'public-checks/0/1'
    assert register['records'] == []
    assert len(alerts) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(cli.main, [args, args])) == [0, 0]
    record, = records(sources)
    assert record['occurrences'] == ['public-checks/0/1']
    work = wire.root / 'work/backlog' / record['item']
    assert 'owner: front-desk' in (work / 'summary.md').read_text()
    assert (work / 'spec.md').read_text().count(MARKER) == 1
    assert [r['operation'] for r in wire.requests].count('create') == 1
    assert [r['operation'] for r in wire.requests].count('update') == 1
    assert len(alerts) == 1


def test_lost_event_and_absent_night_replayed_beyond_score_windows(writer, sources):
    _, alerts, config = writer
    fail_workflow(sources)
    p = Path(sources['hosted']['snapshot'])
    rows = json.loads(p.read_text())
    rows.extend({**rows[-1], 'id': n} for n in range(100, 132))
    write(p, rows)
    slot = next(iter(sources['nightly']['bindings']))
    del sources['nightly']['bindings'][slot]
    write(Path(json.loads(config.read_text())['sources']), sources)
    assert cli.main(cli_args(config, 'hosted')) == 0
    assert cli.main(cli_args(config, 'nightly')) == 0
    assert {r['occurrences'][0] for r in records(sources)} == {
        'public-checks/0/1', 'nightly-soak/' + slot}
    assert alerts == []


def test_real_delivery_reopens_original_item_and_preserves_history(writer, sources):
    wire, alerts, config = writer
    target = 'pentacle__proactive_fixture'
    old = prepare_item(wire.root / 'work', target, status='completed')
    with (old / 'spec.md').open('a') as stream:
        stream.write('Original evidence and closure report.\n')
    slot, _ = fail_night(sources, -1, client_delivery=dict(outcome='failed',
        classification='delivery_failure', correlation_id='proof-123', cause_target=target))
    write(Path(json.loads(config.read_text())['sources']), sources)
    change_json(config, delivery_item=target)
    args = cli_args(config, 'nightly')
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    item, = records(sources)
    assert (item['item'], item['occurrences']) == (target, ['nightly-soak/' + slot])
    now = wire.root / 'work/backlog' / target
    assert not old.exists()
    assert 'Original evidence and closure report.' in (now / 'spec.md').read_text()
    assert 'owner: component-owner' in (now / 'summary.md').read_text()
    assert [r['operation'] for r in wire.requests].count('move') == 1
    assert alerts == []


def test_reviewed_merge_keeps_one_open_owner_and_both_links(writer, sources):
    wire, _, config = writer
    fail_night(sources, -2, slow_consumer={'outcome': 'failed'})
    fail_night(sources, -1, fleet_smoke={'outcome': 'failed'})
    write(Path(json.loads(config.read_text())['sources']), sources)
    args = cli_args(config, 'nightly')
    assert cli.main(args) == 0
    before = {r['cause']: r for r in records(sources)}
    source, target = 'nightly/slow_consumer', 'nightly/fleet_smoke'
    change_json(config, cause_aliases={source: dict(cause=target, reviewer='component-owner',
        revision='accepted', evidence='bound receipt cross-check', closure_report='qualified-report')})
    assert cli.main(args) == 0
    assert cli.main(args) == 0
    result = {r['cause']: r for r in records(sources)}
    assert result[source]['merged_into'] == target
    assert set(result[target]['occurrences']) == {r['occurrences'][0] for r in before.values()}
    assert not (wire.root / 'work/backlog' / before[source]['item']).exists()
    spec = wire.root / 'work/backlog' / before[target]['item'] / 'spec.md'
    assert spec.read_text().count(MARKER) == 3
    assert any(r.get('closure') == {'reference': 'qualified-report'} for r in wire.requests)


MARKER = '- Scorecard observation: '


def test_ordinary_refusal_precedes_later_success_on_same_release(writer, sources):
    _, alerts, config = writer
    root = Path(sources['work']['work_root']).parent / 'forward-results'
    sha, prior = 'a' * 40, 'b' * 40
    write(root / 'later.json', dict(target_sha=sha, prior_sha=prior,
        created_at=stamp(NOW), result='complete',
        stamp={'serving': True, 'disposition': 'active and checks passed'}))
    first = min(sources['nightly']['bindings'])
    write(root / 'earlier.json', dict(target_sha=sha, prior_sha=prior, created_at=first, result='refused'))
    sources['releases']['attempt_sources'] = [
        {'root': str(root), 'pattern': '*.json', 'type': 'ordinary_forward'}]
    write(Path(json.loads(config.read_text())['sources']), sources)
    assert cli.main(cli_args(config, 'releases')) == 0
    result, = records(sources)
    assert result['occurrences'] == [f'release/{sha}/{prior}/1']
    assert result['cause'] == 'releases/refused before activation'
    assert alerts == []


def test_two_independent_occurrences_attach_one_preventive_action(writer, sources):
    wire, _, config = writer
    fail_workflow(sources, 0)
    fail_workflow(sources, 1)
    args = cli_args(config, 'hosted')
    assert cli.main(args) == cli.main(args) == 0
    record, = records(sources)
    path = wire.root / 'work/backlog' / record['item'] / 'spec.md'
    assert path.read_text().count('- Scorecard preventive action: ') == 1
    assert path.read_text().count(MARKER) == 2


def test_on_watch_closure_keeps_first_miss_until_recurrence(writer, sources):
    from agent_orch.reliability_score import report

    wire, _, config = writer
    target = 'pentacle__watched_fixture'
    closed = prepare_item(wire.root / 'work', target, status='completed')
    disposition = closed / 'spec.md'
    with disposition.open('a') as stream:
        stream.write('Disposition: unreproduced, on watch.\n')
    original, _ = fail_night(sources, -2, client_delivery=dict(outcome='failed',
        classification='delivery_failure', correlation_id='old-proof', cause_target=target))
    register = Path(sources['work']['register'])
    change_json(register, records=[dict(cause='proactive-note-delivery', item=target,
        first_seen=original, disposition='unreproduced, on watch', disposition_source=str(disposition),
        recurrence_signature='client delivery failure', occurrences=['nightly-soak/' + original])])
    state = report(sources, NOW)
    assert state['conditions']['open_failure_items']['numerator'] == 0
    assert next(row for row in state['observations']['nightly'] if row['id'] == original)['result'] == 'failed'

    new, _ = fail_night(sources, -1, client_delivery=dict(outcome='failed',
        classification='delivery_failure', correlation_id='new-proof', cause_target=target))
    write(Path(json.loads(config.read_text())['sources']), sources)
    change_json(config, delivery_item=target)
    assert cli.main(cli_args(config, 'nightly')) == 0
    record, = records(sources)
    assert record['item'] == target
    assert record['occurrences'] == ['nightly-soak/' + original, 'nightly-soak/' + new]
    assert report(sources, NOW)['conditions']['open_failure_items']['numerator'] == 1
