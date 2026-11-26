"""B04 gate boundary controls against a disposable source checkout.

Journey: the candidate CLI checks the frozen baseline and a trusted base.
Subject: B04 admission at the same CLI used by the merge runner and CI.
Faults: new imports, aliases, writer and recurring work, bulk callers, and
changed exceptions. Controls: unchanged baseline and named clean mutations.
Runner: Python 3.13 and pytest. Dependencies: Git and isolated temporary clones;
no network, production database, daemon or web host.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVICE = Path('services/chat-stream-v2')
BASE = '36255a907c7b330bc7e18e156df62d3ff4d20733'
# The strict checker controls mutate a known subject. New product findings belong to the report.
FIXTURE = 'bf3a11397a1dda9e451fe7bb4c1c8562f677224b'


@pytest.fixture(scope="session")
def b04_excluded_inputs(tmp_path_factory):
    from tools.run_gate import _b04_input

    names = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z"])
    excluded = [path for path in os.fsdecode(names).split("\0") if path and not _b04_input(path)]
    target = tmp_path_factory.mktemp("b04-inputs") / "excluded.json"
    target.write_text(json.dumps(excluded), encoding="utf-8")
    return target


@pytest.fixture(autouse=True)
def b04_check_reads(b04_excluded_inputs, tmp_path, monkeypatch):
    from tests.fixtures.b04_reads import sitecustomize as guard

    report = tmp_path / "off-list-reads"
    config = ([str(ROOT), str((tmp_path / "repo").resolve())], str(b04_excluded_inputs), str(report))
    monkeypatch.setenv("PENTACLE_B04_READ_GUARD", json.dumps(config))
    monkeypatch.setenv("PYTHONPATH", str(Path(guard.__file__).parent), prepend=os.pathsep)
    guard.enable(config)
    try:
        yield
    finally:
        guard.disable()
    if report.exists():
        pytest.fail("B04 self-test read outside routing inputs: " + ", ".join(sorted(set(report.read_text().splitlines()))))


def fixture_source(path):
    return subprocess.check_output(['git', 'show', f'{FIXTURE}:{SERVICE}/{path}'], cwd=ROOT, text=True)


@pytest.fixture
def checkout(tmp_path):
    clone = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(clone)], check=True)
    subprocess.run(['git', 'checkout', '--quiet', '--detach', FIXTURE], cwd=clone, check=True)
    for name in ('daemon_import_census.py', 'daemon_b04_gate.py', 'daemon_b04_anchors.py'):
        shutil.copy2(ROOT / SERVICE / 'tools' / name, clone / SERVICE / 'tools' / name)
    # Test the current checker with the fixture's admissions data and its matching pin.
    checker = clone / SERVICE / 'tools/daemon_b04_gate.py'
    digest = hashlib.sha256((clone / SERVICE / 'daemon_contract/b04_f1_admissions.json').read_bytes()).hexdigest()
    checker.write_text(re.sub(r"^ADMISSIONS_SHA256 = .+$", f"ADMISSIONS_SHA256 = '{digest}'",
                              checker.read_text(), flags=re.MULTILINE))
    return clone


def run_gate(root, base=BASE):
    return subprocess.run([sys.executable, str(root / SERVICE / 'tools/daemon_b04_gate.py'),
                           '--base', base], cwd=root, capture_output=True, text=True)


def test_projection_import_gate_baseline(checkout):
    baseline = run_gate(checkout)
    assert baseline.returncode == 0, baseline.stderr


def test_projection_import_gate_cli(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\nimport work_lanes_projection as new_view\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F2 import' in rejected.stderr


def test_harmless_library_import_passes(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\nimport json as harmless_library\n')
    control = run_gate(checkout)
    assert control.returncode == 0, control.stderr


def test_unresolved_dynamic_import_fails_closed(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\nimport importlib as loader\nloader.import_module(computed_name)\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'unresolved dynamic import' in rejected.stderr


def test_aliased_literal_dynamic_import_fails(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '''
import importlib as imp
load_view = getattr(imp, "import_module")
def b04_wrapper():
    return load_view("work_lanes_projection")
''')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F2 import' in rejected.stderr


@pytest.mark.parametrize('candidate', [
    '\ndef wrapper(store):\n    work = store.submit\n    return work\n',
    '\ndef timer(loop, job):\n    return loop.call_later(1, job)\n',
    '\ndef task(asyncio, job):\n    return asyncio.create_task(job())\n',
], ids=['writer-alias', 'delayed-callback', 'task'])
def test_new_f1_writer_alias_and_timer_fail(checkout, candidate):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + candidate)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


def test_f1_clean_function_passes(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\ndef clean(value):\n    return value + 1\n')
    control = run_gate(checkout)
    assert control.returncode == 0, control.stderr


def test_existing_recurring_loop_piggyback_fails(checkout):
    path = checkout / SERVICE / 'work_lanes_projection.py'
    original = path.read_text()
    marker = '                await asyncio.sleep(min(interval_s, self.sweep_interval_s))\n'
    assert original.count(marker) == 1
    path.write_text(original.replace(marker, marker + '                await feature.refresh()\n'))
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'recurring-call' in rejected.stderr


def test_frozen_or_mergebase_site_passes_but_absent_site_fails():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        empty = 'def action():\n    pass\n'
        submit = 'def action():\n    store.submit(job)\n'
        extra = 'def action():\n    store.submit(job)\n    store.submit(other)\n'
        assert not gate.new_primitives(submit, empty, submit, 'fixture.py')
        assert not gate.new_primitives(empty, submit, submit, 'fixture.py')
        assert gate.new_primitives(submit, empty, extra, 'fixture.py')
        projection = 'import work_lanes_projection\n'
        another = projection + 'import store_usage\n'
        assert not gate.new_imports(projection, '', projection, 'sessions.py')
        assert not gate.new_imports('', projection, projection, 'sessions.py')
        assert gate.new_imports(projection, '', another, 'sessions.py')
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize('candidate', [
    '\nasync def b04(store):\n    return await store.work_lane_rows()\n',
    '\nasync def b04(store):\n    view = store.work_lane_rows\n    return await view()\n',
    '\nasync def b04(store):\n    return await store.assistant_composite_activity(stream_id="x")\n',
], ids=['direct', 'alias', 'unbounded-activity'])
def test_new_bulk_call_alias_and_unbounded_activity_fail(checkout, candidate):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + candidate)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F2 bulk reference' in rejected.stderr


def test_bounded_activity_passes(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\nasync def b04(store):\n    return await store.assistant_composite_activity(stream_id="x", input_ids=["x"])\n')
    assert run_gate(checkout).returncode == 0


@pytest.mark.parametrize('change', ['addition', 'move', 'removal', 'unchanged'])
def test_exception_addition_move_and_stale_removal_fail(checkout, change):
    path = checkout / SERVICE / 'daemon_contract/active/F2_projections.txt'
    original = path.read_text()
    entry = 'store.py:55 store_usage'
    assert entry in original
    mutations = {
        'addition': original + 'store.py:999 store_usage\n',
        'move': original.replace(entry, 'store.py:56 store_usage'),
        'removal': original.replace(entry + '\n', ''),
        'unchanged': original,
    }
    path.write_text(mutations[change])
    result = run_gate(checkout)
    if change == 'unchanged':
        assert result.returncode == 0
    else:
        assert result.returncode != 0 and 'exception' in result.stderr


def test_new_feature_alias_from_existing_core_import_fails(checkout):
    path = checkout / SERVICE / 'store.py'
    original = path.read_text()
    path.write_text(original + '\ndef b04_alias():\n    alias = store_usage\n    return alias\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new feature alias/reference' in rejected.stderr


def test_new_feature_work_inside_existing_writer_callback_fails(checkout):
    path = checkout / SERVICE / 'store_routing.py'
    original = path.read_text()
    marker = ('            strict = self._strict_publication_state_conn(conn, stream_id)["enabled"]\n'
              '            return _activity_summary_conn(conn, stream_id, strict=strict)\n')
    assert original.count(marker) == 1
    path.write_text(original.replace(marker, '            feature_work(conn)\n' + marker))
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'protected-callback' in rejected.stderr


@pytest.mark.parametrize('code', [
    '\nimport sqlite3 as sql\ndef b04():\n    return sql.connect("x")\n',
    '\ndef b04(store, job):\n    store._queue.put(job)\n',
], ids=['sqlite-connect', 'queue-put'])
def test_alternate_sqlite_and_queue_writes_fail(checkout, code):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    path.write_text(original + code)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


def test_retired_exception_cannot_be_reintroduced(checkout):
    inventory = checkout / SERVICE / 'daemon_contract/active/F2_projections.txt'
    source = checkout / SERVICE / 'store.py'
    original_inventory, original_source = inventory.read_text(), source.read_text()
    inventory.write_text(original_inventory.replace('store.py:55 store_usage\n', ''))
    source.write_text(original_source.replace('import store_usage\n', '', 1))
    subprocess.run(['git', 'add', str(SERVICE / 'daemon_contract/active/F2_projections.txt'),
                    str(SERVICE / 'store.py')], cwd=checkout, check=True)
    subprocess.run(['git', '-c', 'user.name=B04 Fixture', '-c', 'user.email=b04@example.invalid',
                    'commit', '-qm', 'retire fixture exception'], cwd=checkout, check=True)
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
    inventory.write_text(original_inventory)
    source.write_text(original_source)
    rejected = run_gate(checkout, base)
    assert rejected.returncode != 0 and 'exception reintroduced since merge base' in rejected.stderr


def test_unowned_local_import_rejected_and_main_composition_exempt(checkout):
    core = checkout / SERVICE / 'sessions.py'
    (checkout / SERVICE / 'b04_unowned_feature.py').write_text('VALUE = 1\n')
    core.write_text(core.read_text() + '\nimport b04_unowned_feature\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'has no owner' in rejected.stderr


@pytest.mark.parametrize("importer", ["sessions.py", "main.py"])
@pytest.mark.parametrize("feature_path", ["b04_feature.py", "b04_feature/__init__.py"])
def test_new_local_feature_ownership(checkout, importer, feature_path):
    feature = checkout / SERVICE / feature_path
    feature.parent.mkdir(exist_ok=True)
    feature.write_text("VALUE = 1\n")
    path = checkout / SERVICE / importer
    path.write_text(path.read_text() + "\nimport b04_feature\n")
    result = run_gate(checkout)
    if importer == "sessions.py":
        assert result.returncode == 1 and "local import b04_feature has no owner" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


def test_main_composition_exemption_passes(checkout):
    main = checkout / SERVICE / 'main.py'
    original_main = main.read_text()
    main.write_text(original_main + '\nimport work_lanes_projection as b04_composition\n')
    allowed = run_gate(checkout)
    assert allowed.returncode == 0, allowed.stderr


def test_census_modification_fails(checkout):
    manifest = checkout / SERVICE / 'daemon_contract/b04_core_imports.json'
    manifest.write_text(manifest.read_text() + ' ')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'census digest mismatch' in rejected.stderr


@pytest.mark.parametrize(('candidate', 'expected'), [
    ('\nfrom . import store_usage as b04_alias\n', 'new F2 import'),
    ('\ndef b04(work_lanes):\n    return getattr(work_lanes, "current")()\n', 'new F2 bulk reference'),
    ('\ndef b04(store):\n    alias = store.assistant_composite_activity\n    return alias(stream_id="x")\n', 'new F2 bulk reference'),
], ids=['relative-import', 'getattr', 'activity-alias'])
def test_relative_import_and_bulk_getattr_bypasses_fail(checkout, candidate, expected):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    path.write_text(original + candidate)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and expected in rejected.stderr


@pytest.mark.parametrize('code', [
    '\nfrom asyncio import create_task as launch\ndef b04(task):\n    return launch(task)\n',
    '\ndef b04(store, job):\n    q = store._queue\n    return q.put(job)\n',
    '\nimport sqlite3 as sql\nconnect = sql.connect\ndef b04():\n    return connect("x")\n',
    '\nfrom threading import Thread as T\ndef b04(fn):\n    worker = T(target=fn)\n    worker.start()\n',
], ids=['task', 'queue', 'sqlite', 'thread'])
def test_aliases_of_scheduler_queue_and_sqlite_fail(checkout, code):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    path.write_text(original + code)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


def test_merge_runner_executes_b04_cli_and_workflow_requires_it(tmp_path, checkout, monkeypatch, capsys):
    import yaml
    sys.path.insert(0, str(ROOT / SERVICE))
    try:
        from tools import run_gate as runner
    finally:
        sys.path.pop(0)
    monkeypatch.setattr(runner, 'REPO_ROOT', checkout)
    target = checkout / SERVICE / 'sessions.py'
    with target.open('a') as out:
        out.write('\ndef report_fixture(loop, task): return loop.create_task(task)\n')
    # A stale pin must be visible without hiding the new product finding.
    (checkout / SERVICE / 'daemon_contract/b04_f1_admissions.json').write_text('[]\n')
    result = runner._run_daemon_b04_selectors(tmp_path, FIXTURE, 90)
    output = capsys.readouterr().out
    assert result['passed'] and result['report_complete']
    assert 'REVIEW: admissions: B04 F1 admissions digest mismatch' in output
    assert 'sessions.py:report_fixture: new F1 primitive call:create_task' in output
    assert 'report-only findings=' in output
    assert output == (tmp_path / 'daemon_b04_selectors.log').read_text()
    workflow = yaml.safe_load((ROOT / '.github/workflows/predeploy-tests.yml').read_text())
    for script in ('daemon_b04_gate.py', 'daemon_layer_gate.py'):
        step, = [s for s in workflow['jobs']['contracts']['steps'] if script in s.get('run', '')]
        assert step['continue-on-error'] and '--base "$RELEASE_BASE" --report-only' in step['run']


def test_existing_sqlite_user_cannot_add_a_connection(checkout):
    path = checkout / SERVICE / 'store.py'
    path.write_text(path.read_text() + '\nimport sqlite3\ndef b04():\n    return sqlite3.connect("new.db")\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive call:connect' in rejected.stderr


def test_new_account_callback_wrapper_fails(checkout):
    path = checkout / SERVICE / 'account_executor.py'
    path.write_text(path.read_text() + '\nasync def b04(runtime):\n    return await runtime.run(lambda account: feature(account))\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


def test_untyped_store_kv_alias_is_rejected(checkout):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\nasync def b04(store):\n    read = store.get\n    return await read("untyped")\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


@pytest.mark.parametrize('mutation', ['chat-queue', 'bulk-duplicate'])
def test_feature_chat_queue_and_bulk_multiplicity_fail(checkout, mutation):
    core = checkout / SERVICE / 'sessions.py'
    original = core.read_text()
    if mutation == 'chat-queue':
        core.write_text(original + '\nasync def b04(queue):\n    return await queue.submit_chat(feature_work)\n')
        expected = 'new F1 primitive'
    else:
        path = checkout / SERVICE / 'work_lanes_projection.py'
        original = path.read_text()
        line = '                    rows = await self.store.work_lane_rows(include_done=True)\n'
        assert original.count(line) == 1
        path.write_text(original.replace(line, line + line))
        expected = 'new F2 bulk reference'
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and expected in rejected.stderr


@pytest.mark.parametrize('change', ['addition', 'move'])
def test_f1_exception_addition_and_move_fail(checkout, change):
    path = checkout / SERVICE / 'daemon_contract/active/F1_protected_core.txt'
    original = path.read_text()
    entry = 'services/_shared/specs_service.py:317 SpecsSubsystem._retry_loop'
    assert entry in original
    changed = (original + 'services/_shared/specs_service.py:999 SpecsSubsystem._retry_loop\n'
               if change == 'addition' else original.replace(entry, entry.replace(':317', ':318')))
    path.write_text(changed)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'exception' in rejected.stderr


def test_new_caller_of_existing_bulk_forwarder_is_rejected():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = 'def view(store):\n    return store.work_lane_rows()\n'
        after = before + '\ndef caller(store):\n    return view(store)\n'
        assert gate.new_bulk_sites(before, before, after, 'fixture.py')
        assert not gate.new_bulk_sites(before, before, before, 'fixture.py')
    finally:
        sys.path.pop(0)


def test_qualified_bulk_receiver_alias_is_rejected():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = 'def frame(self):\n    return None\n'
        after = 'def frame(self):\n    board = self.work_lanes\n    return board.current()\n'
        assert gate.new_bulk_sites(before, before, after, 'fixture.py')
        assert not gate.new_bulk_sites(before, before, before, 'fixture.py')
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize('source', [
    '\ndef b04(store):\n    writer = getattr(store, "submit")\n    return writer(job)\n',
    '\ndef b04(store, name):\n    return getattr(store, name)(job)\n',
], ids=['literal-getattr', 'computed-getattr'])
def test_dynamic_writer_attribute_bypass_fails(checkout, source):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    path.write_text(original + source)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


def test_nested_op_name_does_not_taint_an_unrelated_function():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = ('def first(store):\n    def _op():\n        return store.work_lane_rows()\n'
                  '    return _op()\n\ndef _op():\n    return 1\n\ndef caller():\n    return 1\n')
        after = before.replace('def caller():\n    return 1', 'def caller():\n    return _op()')
        assert not gate.new_bulk_sites(before, before, after, 'fixture.py')
    finally:
        sys.path.pop(0)


def test_unrelated_local_name_does_not_inherit_feature_alias():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = ('import store_usage\n\ndef first():\n    proof = store_usage.reader\n    return proof\n'
                  '\ndef second():\n    return 1\n')
        clean = before.replace('def second():\n    return 1', 'def second():\n    proof = 1\n    return proof')
        bad = before + '\ndef added():\n    reader = store_usage.reader\n    return reader\n'
        assert not gate.new_feature_references(before, before, clean, 'store.py')
        assert gate.new_feature_references(before, before, bad, 'store.py')
    finally:
        sys.path.pop(0)


def test_unrelated_same_named_callback_does_not_become_protected():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = ('async def first(store):\n    def _op(conn):\n        return 1\n'
                  '    return await store.submit(_op)\n\n'
                  'def second():\n    def _op(conn):\n        return 2\n    return _op(None)\n')
        clean = before.replace('        return 2', '        return 3')
        bad = before.replace('        return 1', '        return feature_work()')
        assert not gate.new_primitives(before, before, clean, 'fixture.py')
        assert gate.new_primitives(before, before, bad, 'fixture.py')
    finally:
        sys.path.pop(0)


def test_unrelated_local_primitive_alias_does_not_collide():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = ('def first(store):\n    writer = store.submit\n    return writer\n'
                  '\ndef second():\n    return 1\n')
        clean = before.replace('def second():\n    return 1', 'def second():\n    writer = ordinary\n    return writer()')
        bad = before.replace('    return writer\n', '    return writer(feature)\n')
        assert not gate.new_primitives(before, before, clean, 'fixture.py')
        assert gate.new_primitives(before, before, bad, 'fixture.py')
    finally:
        sys.path.pop(0)


def test_unrelated_local_bulk_alias_does_not_collide():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = ('def first(store):\n    view = store.work_lane_rows\n    return view\n'
                  '\ndef second():\n    return 1\n')
        clean = before.replace('def second():\n    return 1', 'def second():\n    view = ordinary\n    return view()')
        bad = before.replace('    return view\n', '    return view()\n')
        assert not gate.new_bulk_sites(before, before, clean, 'fixture.py')
        assert gate.new_bulk_sites(before, before, bad, 'fixture.py')
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(('expression', 'row'), [
    ('event_push.check_pin_drift, ', 'services/chat-stream-v2/main.py:846 EventPush.check_pin_drift'),
    ('watch_wake.missed_wake_alarm,', 'services/chat-stream-v2/main.py:846 WatchWake.missed_wake_alarm'),
    ('account_runtime.tick, ', 'services/chat-stream-v2/main.py:847 AccountRuntime.tick'),
    ('capacity_retry.tick, ', 'services/chat-stream-v2/main.py:847 CapacityRetry.tick'),
    ('queued_send_now.tick', 'services/chat-stream-v2/main.py:847 QueuedSendNow.tick'),
    ('watch_wake.tick, ', 'services/chat-stream-v2/main.py:847 WatchWake.tick'),
], ids=['event-push', 'missed-wake', 'account', 'capacity', 'queued-send', 'watch-wake'])
def test_named_scheduler_reference_can_retire(checkout, expression, row):
    main = checkout / SERVICE / 'main.py'
    inventory = checkout / SERVICE / 'daemon_contract/active/F1_protected_core.txt'
    original_main, original_inventory = main.read_text(), inventory.read_text()
    assert original_main.count(expression) == 1 and row in original_inventory
    main.write_text(original_main.replace(expression, '', 1))
    inventory.write_text(original_inventory.replace(row + '\n', '', 1))
    control = run_gate(checkout)
    assert control.returncode == 0, (row, control.stderr)


@pytest.mark.parametrize(('old', 'new', 'diagnostics'), [
    ('watch_wake.tick, ', 'watch_wake.tick, watch_wake.tick, ', ('duplicated frozen selector', 'recurring-reference')),
    ('watch_wake.tick, ', 'watch_wake.tick, feature_job.tick, ', ('recurring-reference',)),
    ('queued_send_now.tick', '', ('stale exception',)),
], ids=['duplicate', 'added', 'stale'])
def test_invalid_scheduler_reference_fails(checkout, old, new, diagnostics):
    main = checkout / SERVICE / 'main.py'
    main.write_text(main.read_text().replace(old, new, 1))
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and any(d in rejected.stderr for d in diagnostics)


def test_one_of_two_identical_bulk_sites_can_retire(checkout):
    path = checkout / SERVICE / 'assistant_composite.py'
    inventory = checkout / SERVICE / 'daemon_contract/active/F2_projections.txt'
    original, original_inventory = path.read_text(), inventory.read_text()
    marker = '            await self.refresh_activity()\n            return {"type": "assistant.operation.ok", **audit'
    assert original.count(marker) == 1
    path.write_text(original.replace(marker, '            return {"type": "assistant.operation.ok", **audit', 1))
    inventory.write_text(original_inventory.replace('assistant_composite.py:2092 refresh_activity\n', '', 1))
    control = run_gate(checkout)
    assert control.returncode == 0, control.stderr


def test_noncall_change_to_writer_callback_is_rejected():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        before = 'async def write(store):\n    def _op(conn):\n        value = 1\n        return value\n    return await store.submit(_op)\n'
        after = before.replace('value = 1', 'value = 2')
        assert gate.new_primitives(before, before, after, 'fixture.py')
        assert not gate.new_primitives(before, before, before, 'fixture.py')
    finally:
        sys.path.pop(0)


def test_import_level_resolution_cli_and_fail_closed_gate(checkout, tmp_path):
    import json
    fixture = tmp_path / 'invented_source.py'
    fixture.write_text("__import__('child', globals(), locals(), (), 1)\n")
    cli = subprocess.run([sys.executable, str(checkout / SERVICE / 'tools/daemon_import_census.py'),
                          '--path', 'pkg/core.py', '--source', str(fixture)],
                         cwd=checkout, capture_output=True, text=True)
    assert cli.returncode == 0, cli.stderr
    sites = json.loads(cli.stdout)
    assert sites[0]['form'] == 'dynamic' and sites[0]['module'] == 'pkg.child'
    fixture.write_text("__import__('child', foreign_globals, level=1)\n")
    unknown = subprocess.run([sys.executable, str(checkout / SERVICE / 'tools/daemon_import_census.py'),
                              '--path', 'pkg/core.py', '--source', str(fixture)],
                             cwd=checkout, capture_output=True, text=True)
    assert unknown.returncode == 0 and json.loads(unknown.stdout)[0]['form'] == 'unresolved'


@pytest.mark.parametrize('code', [
    "__import__('json', level=unknown)",
    "__import__('json', level=1)",
    "__import__('json', foreign_globals, level=1)",
], ids=['unknown', 'relative', 'foreign-globals'])
def test_unresolved_import_level_fails(checkout, code):
    core = checkout / SERVICE / 'sessions.py'
    core.write_text(core.read_text() + '\n' + code + '\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'unresolved dynamic import' in rejected.stderr


def test_absolute_import_level_passes(checkout):
    core = checkout / SERVICE / 'sessions.py'
    core.write_text(core.read_text() + "\n__import__('json', level=0)\n")
    clean = run_gate(checkout)
    assert clean.returncode == 0, clean.stderr


@pytest.mark.parametrize(('case', 'path', 'mutation', 'message'), [
    ('scheduler_append', 'main.py', 'scheduler', 'recurring-reference'),
    ('sqlite_module_alias', 'sessions.py', '\nimport sqlite3 as sql\ndef b04():\n    db = sql\n    return db.connect("invented.db")\n', 'new F1 primitive'),
    ('computed_bulk_getattr', 'sessions.py', '\ndef b04(work_lanes, name):\n    return getattr(work_lanes, name)()\n', 'new F2 bulk reference'),
])
def test_reviewed_bypass_controls(checkout, case, path, mutation, message):
    """CLI admission: each rejected bypass fails, with an unchanged clean baseline.

    Subject: committed B04 selector CLI in a disposable Git clone. Faults are
    the three cycle-1 QA reproductions; no runtime, network or database is used.
    """
    target = checkout / SERVICE / path
    source = target.read_text()
    if mutation == 'scheduler':
        marker = '        if server.assistant_restore is not None:\n'
        assert source.count(marker) == 1
        source = source.replace(marker, '        if enabled:\n            callbacks.append(feature_job.tick)\n' + marker, 1)
    else:
        source += mutation
    target.write_text(source)
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and message in rejected.stderr, (case, rejected.stdout, rejected.stderr)


@pytest.mark.parametrize('code', [
    '\ndef b04(other, name):\n    return getattr(other, name)()\n',
    '\ndef b04(work_lanes):\n    return getattr(work_lanes, "status")()\n',
], ids=['unrelated-object', 'permitted-status'])
def test_reviewed_bypass_clean_reflection_controls(checkout, code):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    path.write_text(original + code)
    control = run_gate(checkout)
    assert control.returncode == 0, control.stderr


@pytest.mark.parametrize(('case', 'path', 'mutation', 'diagnostic'), [
    ('scheduler_append', 'main.py', 'append', 'recurring'),
    ('sqlite_module_alias', 'sessions.py', '\nimport sqlite3 as sql\ndef b04():\n    db = sql\n    return db.connect("invented.db")\n', 'F1'),
    ('computed_getattr', 'sessions.py', '\ndef b04(work_lanes, name):\n    return getattr(work_lanes, name)()\n', 'F2'),
    ('scheduler_rebinding', 'main.py', 'rebind', 'recurring'),
    ('aliased_getattr', 'sessions.py', '\ngetter = getattr\ndef b04(work_lanes, name):\n    return getter(work_lanes, name)()\n', 'F2'),
])
def test_cycle2_prior_qa_controls(checkout, case, path, mutation, diagnostic):
    """Real CLI admission: five reviewed bypasses, clean baseline, no live data."""
    target = checkout / SERVICE / path
    source = target.read_text()
    if mutation in {'append', 'rebind'}:
        marker = '        if server.assistant_restore is not None:\n'
        assert source.count(marker) == 1
        statement = ('callbacks.append(feature_job.tick)' if mutation == 'append'
                     else 'callbacks = callbacks + [feature_job.tick]')
        source = source.replace(marker, f'        if enabled:\n            {statement}\n' + marker, 1)
    else:
        source += mutation
    target.write_text(source)
    result = run_gate(checkout)
    assert result.returncode == 1 and diagnostic in result.stderr and path in result.stderr, (case, result.stdout, result.stderr)
    assert not any(marker in result.stderr for marker in ('Traceback', 'SyntaxError', 'timed out'))


@pytest.fixture
def cycle2_gate():
    sys.path.insert(0, str(ROOT / SERVICE / 'tools'))
    try:
        import daemon_b04_gate as gate
        yield gate
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize('subject,expected', [
    ('from builtins import getattr as g\ndef f(work_lanes, n): return g(work_lanes, n)()', 'F2'),
    ('import builtins as b\ndef f(work_lanes, n): return b.getattr(work_lanes, n)()', 'F2'),
    ('g = getattr\nh: object = g\ndef f(work_lanes, n):\n def inner(): return h(work_lanes, n)()\n return inner()', 'F2'),
    ('def f(work_lanes, n):\n view: object = work_lanes\n other = view\n return getattr(other, n)()', 'F2'),
    ('def f(work_lanes): return getattr(work_lanes, "current")', 'F2'),
    ('g = getattr\nf = lambda work_lanes, n: g(work_lanes, n)()', 'F2'),
    ('def f(work_lanes): return getattr(work_lanes, "current")()', 'F2'),
    ('def f(work_lanes, x, n):\n if x: view = work_lanes\n else: view = object()\n return getattr(view, n)()', 'F2'),
    ('def f(work_lanes, n): return getattr(*(work_lanes, n))()', 'F2'),
    ('def f(work_lanes, n): return work_lanes[n]()', 'F2'),
    ('def f(work_lanes, n): return work_lanes.__dict__[n]()', 'F2'),
    ('def f(work_lanes): return opaque(work_lanes)', 'F2'),
    ('def f(work_lanes): yield work_lanes', 'F2'),
    ('def f(work_lanes, n, xs):\n view = work_lanes\n for x in xs: view = object()\n return getattr(view, n)()', 'F2'),
    ('def f(work_lanes, n):\n view = work_lanes\n try: view = object()\n except Exception: pass\n return getattr(view, n)()', 'F2'),
    ('def f(work_lanes, n, x):\n view = work_lanes\n match x:\n  case 1: view = object()\n return getattr(view, n)()', 'F2'),
    ('def f(work_lanes, n):\n match work_lanes:\n  case view: return getattr(view, n)()', 'F2'),
    ('def f(work_lanes, n, cond):\n view = work_lanes\n def inner():\n  nonlocal view\n  if cond: view = object()\n  return getattr(view, n)()\n return inner()', 'F2'),
    ('def f(work_lanes, n):\n for view in [work_lanes]:\n  pass\n return getattr(view, n)()', 'F2'),
    ('def f(work_lanes): return opaque({work_lanes: 1})', 'F2'),
    ('import sqlite3 as sql\ndef f(): return opaque({sql: "connect"})', 'F1'),
    ('def f(work_lanes):\n global escaped\n escaped = work_lanes', 'F2'),
    ('import sqlite3 as s\ndef f():\n db = s\n db2: object = db\n return db2.connect("x")', 'F1'),
    ('import sqlite3 as s\ndef f():\n db = s\n connect: object = db.connect\n return connect("x")', 'F1'),
    ('import sqlite3 as s\ng = getattr\ndef f():\n connect = g(s, "connect")\n return connect("x")', 'F1'),
    ('import sqlite3 as s\ndef f(n): return getattr(s, n)("x")', 'F1'),
    ('import sqlite3 as s\ndef f(): return opaque(s.connect)', 'F1'),
    ('import sqlite3 as s\ndef f(): return s.connect', 'F1'),
])
def test_cycle2_identity_subjects(cycle2_gate, subject, expected):
    errors = cycle2_gate.new_identity_sites('', '', subject, 'fixture.py')
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize(('filename', 'mutation', 'diagnostic'), [
    ('main.py', '        if feature_job:\n            callbacks.append(feature_job.tick)\n', 'F1 recurring'),
    ('main.py', '        if feature_job:\n            callbacks = callbacks + [feature_job.tick]\n', 'F1 recurring'),
    ('sessions.py', '\nimport sqlite3 as sql\ndef b04():\n db = sql\n return db.connect("invented.db")\n', 'F1'),
    ('sessions.py', '\ndef b04(work_lanes, name): return getattr(work_lanes, name)()\n', 'F2'),
    ('sessions.py', '\ngetter = getattr\ndef b04(work_lanes, name): return getter(work_lanes, name)()\n', 'F2'),
    ('sessions.py', '\ndef b04(work_lanes, name, cond):\n view = work_lanes\n def inner():\n  nonlocal view\n  if cond: view = object()\n  return getattr(view, name)()\n return inner()\n', 'F2'),
    ('sessions.py', '\ndef b04(work_lanes, name):\n for view in [work_lanes]:\n  pass\n return getattr(view, name)()\n', 'F2'),
    ('sessions.py', '\ndef b04(work_lanes): return opaque({work_lanes: 1})\n', 'F2'),
    ('sessions.py', '\nimport sqlite3 as sql\ndef b04(): return opaque({sql: "connect"})\n', 'F1'),
], ids=['scheduler-append', 'scheduler-add', 'sqlite-connect', 'dynamic-getattr', 'getattr-alias',
        'closure', 'loop', 'opaque-view', 'opaque-sqlite'])
def test_cycle2_seven_qa_subjects_fail_candidate_cli_and_permitted_controls(checkout, filename, mutation, diagnostic):
    path = checkout / SERVICE / filename
    original = path.read_text()
    if filename == 'main.py':
        marker = '        if server.assistant_restore is not None:\n'
        assert original.count(marker) == 1
        path.write_text(original.replace(marker, mutation + marker, 1))
    else:
        path.write_text(original + mutation)
    rejected = run_gate(checkout)
    assert rejected.returncode == 1 and diagnostic in rejected.stderr, (diagnostic, rejected.stderr)


def test_cycle2_cli_baseline_and_permitted_controls(checkout):
    sessions = checkout / SERVICE / 'sessions.py'
    sessions.write_text(sessions.read_text() + '''
import sqlite3 as safe_sqlite
def b04_permitted(work_lanes, name):
    return getattr(work_lanes, "status")()
def b04_permitted_sqlite():
    return safe_sqlite.complete_statement("select 1")
''')
    permitted = run_gate(checkout)
    assert permitted.returncode == 0, permitted.stderr


@pytest.mark.parametrize('subject', [
    'def f(getattr, work_lanes, n): return getattr(work_lanes, n)()',
    'f = lambda getattr, work_lanes, n: getattr(work_lanes, n)()',
    'def f(work_lanes, n):\n def getattr(*args): return lambda: 1\n return getattr(work_lanes, n)()',
    'def f(other, n): return getattr(other, n)()',
    'def f(work_lanes): return getattr(work_lanes, "status")()',
    'def f(other): return other.connect("x")',
    'import sqlite3 as s\ndef f():\n connect = s.connect\n return 1',
    'def f(work_lanes, n):\n if False: view = work_lanes\n else: view = object()\n return getattr(view, n)()',
])
def test_cycle2_identity_clean(cycle2_gate, subject):
    assert not cycle2_gate.new_identity_sites('', '', subject, 'fixture.py')


def test_cycle2_scheduler_closed_grammar(cycle2_gate):
    main = fixture_source('main.py')
    active = fixture_source('daemon_contract/active/F1_protected_core.txt')
    assert not cycle2_gate.scheduler_grammar(main, active)
    start = '        if server.assistant_restore is not None:\n'
    init = '        callbacks = [event_push.check_pin_drift, watch_wake.missed_wake_alarm,\n'
    dispatch = '        await run_reconcile_callbacks(*callbacks)'
    mutations = [
        (start, '        callbacks = other\n' + start),
        (init, init.replace('callbacks =', 'callbacks: list =')),
        ('event_push.check_pin_drift,', '*[event_push.check_pin_drift],'),
        (start, '        alias = callbacks\n        alias.append(feature.tick)\n' + start),
        (start, '        add = callbacks.append\n        add(feature.tick)\n' + start),
        (start, '        helper(callbacks)\n' + start),
        (dispatch, dispatch.replace('*callbacks)', '*callbacks, feature.tick)')),
        (start, start.replace(' is not None', ' is not None or feature')),
        ('on_reconcile_tick=reconcile_callbacks', 'on_reconcile_tick=other'),
        ('watch_wake.tick, ', 'watch_wake.tick, watch_wake.tick, '),
        (dispatch, dispatch + '\n' + dispatch),
    ]
    for old, new in mutations:
        assert main.count(old) == 1, old
        assert cycle2_gate.scheduler_grammar(main.replace(old, new, 1), active), old
    row = 'services/chat-stream-v2/main.py:849 AssistantRestore.tick\n'
    block = start + '            callbacks.append(server.assistant_restore.tick)\n'
    assert not cycle2_gate.scheduler_grammar(main.replace(block, ''), active.replace(row, ''))
    assert not cycle2_gate.scheduler_grammar(main.replace(block, start + '            pass\n'), active.replace(row, ''))
    assert cycle2_gate.scheduler_grammar(main.replace(block, ''), active)
    assert cycle2_gate.scheduler_grammar(main, active.replace(row, ''))
    assert not cycle2_gate.scheduler_grammar(main + '\ndef reconcile_callbacks():\n callbacks = []\n callbacks.append(1)\n', active)


def test_cycle2_scheduler_each_row_and_source_retire_together(cycle2_gate):
    main = fixture_source('main.py')
    active = fixture_source('daemon_contract/active/F1_protected_core.txt')
    cases = (
        ('event_push.check_pin_drift, ', 'services/chat-stream-v2/main.py:846 EventPush.check_pin_drift'),
        ('watch_wake.missed_wake_alarm,', 'services/chat-stream-v2/main.py:846 WatchWake.missed_wake_alarm'),
        ('account_runtime.tick, ', 'services/chat-stream-v2/main.py:847 AccountRuntime.tick'),
        ('capacity_retry.tick, ', 'services/chat-stream-v2/main.py:847 CapacityRetry.tick'),
        ('queued_send_now.tick', 'services/chat-stream-v2/main.py:847 QueuedSendNow.tick'),
        ('watch_wake.tick, ', 'services/chat-stream-v2/main.py:847 WatchWake.tick'),
    )
    for member, row in cases:
        changed = main.replace(member, '', 1)
        retired = active.replace(row + '\n', '', 1)
        assert not cycle2_gate.scheduler_grammar(changed, retired), row
        assert cycle2_gate.scheduler_grammar(changed, active), row
        assert cycle2_gate.scheduler_grammar(main, retired), row


@pytest.mark.parametrize(('text', 'diagnostic'), [
    ('from builtins import getattr as g\ndef b04(work_lanes, n): return g(work_lanes, n)()', 'F2 bulk'),
    ('import builtins as b\ndef b04(work_lanes, n): return b.getattr(work_lanes, n)()', 'F2 bulk'),
    ('g = getattr\nh: object = g\ndef b04(work_lanes, n): return h(work_lanes, n)()', 'F2 bulk'),
    ('def b04(work_lanes): return getattr(work_lanes, "current")', 'F2 bulk'),
    ('def b04(work_lanes): return getattr(work_lanes, "current")()', 'F2 bulk'),
    ('def b04(work_lanes, x, n):\n if x: view = work_lanes\n else: view = object()\n return getattr(view, n)()', 'F2 bulk'),
    ('def b04(work_lanes, n):\n view: object = work_lanes\n def inner(): return getattr(view, n)()\n return inner()', 'F2 bulk'),
    ('import sqlite3 as s\ndef b04():\n db = s\n connect = db.connect\n return connect("x")', 'F1 SQLite'),
    ('import sqlite3 as s\ndef b04():\n db = s\n second: object = db\n return second.connect("x")', 'F1 SQLite'),
    ('import sqlite3 as s\ng = getattr\ndef b04(): return g(s, "connect")("x")', 'F1 SQLite'),
], ids=['builtin-alias', 'builtin-module', 'annotated-getter', 'current', 'current-call', 'branch',
        'closure', 'sqlite-alias', 'sqlite-annotation', 'sqlite-getter'])
def test_cycle2_cli_identity_rejects_seams(checkout, text, diagnostic):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\n' + text + '\n')
    result = run_gate(checkout)
    assert result.returncode == 1 and 'sessions.py:b04' in result.stderr and diagnostic in result.stderr, result.stderr


@pytest.mark.parametrize('text', [
    'def b04(getattr, work_lanes, n): return getattr(work_lanes, n)()',
    'def b04(other, n): return getattr(other, n)()',
    'def b04(work_lanes): return getattr(work_lanes, "status")()',
    'import sqlite3 as s\ndef b04():\n connect = s.connect\n return 1',
    'import sqlite3 as s\ndef b04():\n connect = getattr(s, "connect")\n return 1',
], ids=['shadowed', 'unrelated', 'status', 'connection-reference', 'getter-reference'])
def test_cycle2_cli_identity_clean(checkout, text):
    path = checkout / SERVICE / 'sessions.py'
    path.write_text(path.read_text() + '\n' + text + '\n')
    result = run_gate(checkout)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(('retire_source', 'retire_row', 'add_unrelated', 'diagnostic'), [
    (True, True, False, None),
    (True, False, False, 'stale exception'),
    (False, True, False, 'recurring'),
    (False, False, True, None),
], ids=['retirement', 'stale-row', 'missing-row', 'unrelated-scope'])
def test_cycle2_scheduler_restore_retirement_and_unrelated_scope_cli(checkout, retire_source, retire_row, add_unrelated, diagnostic):
    main = checkout / SERVICE / 'main.py'
    inventory = checkout / SERVICE / 'daemon_contract/active/F1_protected_core.txt'
    original, rows = main.read_text(), inventory.read_text()
    block = '        if server.assistant_restore is not None:\n            callbacks.append(server.assistant_restore.tick)\n'
    row = 'services/chat-stream-v2/main.py:849 AssistantRestore.tick\n'
    assert original.count(block) == 1 and row in rows
    if retire_source:
        main.write_text(original.replace(block, '        if server.assistant_restore is not None:\n            pass\n'))
    if retire_row:
        inventory.write_text(rows.replace(row, ''))
    if add_unrelated:
        main.write_text(original + '\ndef reconcile_callbacks():\n    callbacks = []\n    callbacks.append(1)\n')
    result = run_gate(checkout)
    if diagnostic:
        assert result.returncode == 1 and diagnostic in result.stderr, result.stderr
    else:
        assert result.returncode == 0, result.stderr


SCHEDULER_START = '        if server.assistant_restore is not None:\n'
SCHEDULER_INIT = '        callbacks = [event_push.check_pin_drift, watch_wake.missed_wake_alarm,\n'
SCHEDULER_DISPATCH = '        await run_reconcile_callbacks(*callbacks)'


@pytest.mark.parametrize(('old', 'new'), [
    (SCHEDULER_START, '        callbacks = other\n' + SCHEDULER_START),
    (SCHEDULER_INIT, SCHEDULER_INIT.replace('callbacks =', 'callbacks: list =')),
    ('event_push.check_pin_drift,', '*[event_push.check_pin_drift],'),
    (SCHEDULER_START, '        alias = callbacks\n        alias.append(feature.tick)\n' + SCHEDULER_START),
    (SCHEDULER_START, '        add = callbacks.append\n        add(feature.tick)\n' + SCHEDULER_START),
    (SCHEDULER_START, '        helper(callbacks)\n' + SCHEDULER_START),
    (SCHEDULER_DISPATCH, SCHEDULER_DISPATCH.replace('*callbacks)', '*callbacks, feature.tick)')),
    (SCHEDULER_START, SCHEDULER_START.replace(' is not None', ' is not None or feature')),
    ('on_reconcile_tick=reconcile_callbacks', 'on_reconcile_tick=other'),
    ('watch_wake.tick, ', 'watch_wake.tick, watch_wake.tick, '),
    (SCHEDULER_DISPATCH, SCHEDULER_DISPATCH + '\n' + SCHEDULER_DISPATCH),
], ids=['replace', 'annotation', 'starred', 'alias', 'method', 'helper', 'extra-argument', 'condition',
        'wrong-callback', 'duplicate', 'double-dispatch'])
def test_cycle2_scheduler_matrix_cli(checkout, old, new):
    main = checkout / SERVICE / 'main.py'
    original = main.read_text()
    assert old in original
    main.write_text(original.replace(old, new, 1))
    result = run_gate(checkout)
    assert result.returncode == 1 and 'main.py:run.reconcile_callbacks' in result.stderr and 'F1 recurring' in result.stderr, (old, result.stderr)


@pytest.mark.parametrize(('member', 'row'), [
    ('event_push.check_pin_drift, ', 'services/chat-stream-v2/main.py:846 EventPush.check_pin_drift'),
    ('watch_wake.missed_wake_alarm,', 'services/chat-stream-v2/main.py:846 WatchWake.missed_wake_alarm'),
    ('account_runtime.tick, ', 'services/chat-stream-v2/main.py:847 AccountRuntime.tick'),
    ('capacity_retry.tick, ', 'services/chat-stream-v2/main.py:847 CapacityRetry.tick'),
    ('queued_send_now.tick', 'services/chat-stream-v2/main.py:847 QueuedSendNow.tick'),
    ('watch_wake.tick, ', 'services/chat-stream-v2/main.py:847 WatchWake.tick'),
], ids=['event-push', 'missed-wake', 'account', 'capacity', 'queued-send', 'watch-wake'])
@pytest.mark.parametrize('retirement', ['source', 'row'], ids=['source', 'row'])
def test_cycle2_scheduler_six_one_sided_retirements_cli(checkout, member, row, retirement):
    main = checkout / SERVICE / 'main.py'
    inventory = checkout / SERVICE / 'daemon_contract/active/F1_protected_core.txt'
    original, rows = main.read_text(), inventory.read_text()
    if retirement == 'source':
        main.write_text(original.replace(member, '', 1))
        inventory.write_text(rows)
        expected = 'stale exception'
    else:
        main.write_text(original)
        inventory.write_text(rows.replace(row + '\n', '', 1))
        expected = 'F1 recurring'
    result = run_gate(checkout)
    assert result.returncode == 1 and expected in result.stderr, (row, result.stderr)


def test_cycle2_identity_ratchet_uses_per_site_maximum_and_allows_retirement(cycle2_gate):
    site = 'import sqlite3 as sql\ndef f():\n db = sql\n return db.connect("x")\n'
    doubled = site.replace(' return db.connect("x")', ' db.connect("x")\n return db.connect("x")')
    assert not cycle2_gate.new_identity_sites(site, '', site, 'fixture.py')
    assert not cycle2_gate.new_identity_sites('', site, site, 'fixture.py')
    assert not cycle2_gate.new_identity_sites(site, site, '', 'fixture.py')
    assert cycle2_gate.new_identity_sites('', '', site, 'fixture.py')
    assert cycle2_gate.new_identity_sites(site, '', doubled, 'fixture.py')
    other = site.replace('"x"', '"other"')
    assert cycle2_gate.new_identity_sites(site, site, other, 'fixture.py')


@pytest.mark.parametrize('scenario,expected', [
    ('admitted', None),
    ('no-entry', 'new F1 primitive call:submit'),
    ('wrong-signature', 'new F1 primitive call:submit'),
    ('stale', 'stale or ineligible F1 admission'),
    ('digest-mismatch', 'F1 admissions digest mismatch'),
    ('scheduler', 'invalid B04 F1 admission kind'),
])
def test_reviewed_store_admission(checkout, scenario, expected):
    store = checkout / SERVICE / 'store.py'
    call = 'self.submit(_op)' if scenario != 'scheduler' else 'asyncio.create_task(_op)'
    expression = ast.parse(call, mode='eval').body
    scope = 'Store.reviewed_write'
    def node(kind, tree):
        signature = hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()
        return {'kind': kind, 'sha256': signature, 'count': 1}

    path = store.relative_to(checkout).as_posix()
    rows = [
        dict(path=path, scope=scope, nodes=[node('call:' + expression.func.attr, expression),
                                           node(expression.func.attr, expression.func)]),
        dict(path=path, scope=scope + '._op', nodes=[
            node('protected-callback-statement', ast.parse('return None').body[0])]),
    ]
    if scenario != 'stale':
        method = ("    async def reviewed_write(self):\n"
                  "        def _op(conn):\n            return None\n"
                  f"        return await {call}\n\n")
        source = store.read_text()
        marker = '    async def submit('
        assert source.count(marker) == 1
        store.write_text(source.replace(marker, method + marker))
    if scenario == 'wrong-signature':
        rows[0]['nodes'][0]['sha256'] = '0' * 64
    if scenario != 'no-entry':
        manifest = checkout / SERVICE / 'daemon_contract/b04_f1_admissions.json'
        checker = checkout / SERVICE / 'tools/daemon_b04_gate.py'
        prior = hashlib.sha256(manifest.read_bytes()).hexdigest()
        block = dict(spec_id='spec_fixture', spec_review_id='fixture-review', reason='Fixture Store write.', scopes=rows)
        raw = json.dumps(block, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        block['sha256'] = hashlib.sha256(raw).hexdigest()
        manifest.write_text(json.dumps([block]) + '\n')
        if scenario != 'digest-mismatch':
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            checker.write_text(checker.read_text().replace(prior, digest))
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
    result = run_gate(checkout, base)
    assert result.returncode == (1 if expected else 0), result.stderr
    if expected:
        assert expected in result.stderr, result.stderr


def test_unsupported_python_reports_only_the_signature_format(cycle2_gate, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'version_info', (3, 12, 0))
    monkeypatch.setattr(sys, 'argv', ['daemon_b04_gate.py', '--base', '0' * 40])
    assert cycle2_gate.main() == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.splitlines() == [
        '[daemon-b04] FAIL: B04 admission signatures require the Python 3.13 AST format. Run with python3.13 (found 3.12.0)'
    ]


def test_merged_store_admission_cannot_pay_for_another_identical_site(cycle2_gate):
    path = 'services/chat-stream-v2/store.py'
    source = ('class Store:\n    async def reviewed_write(self):\n'
              '        def _op(conn):\n            return None\n'
              '        await self.submit(_op)\n')
    admissions = {(scope, kind, hashlib.sha256(signature.encode()).hexdigest()): count
                  for (scope, kind, signature), count in cycle2_gate._primitive_sites(source, path).items()}
    check = cycle2_gate.new_primitives
    assert not check('', source, source, path, admissions=admissions)
    assert check('', source, source + '        await self.submit(_op)\n', path, admissions=admissions)
    assert check('', source, '', path, admissions=admissions)
    assert check(source, source, source, path, admissions=admissions)

    # An existing scheduled callback cannot acquire another submit via an admission.
    scheduler = source.replace('return None', 'time.sleep(1)')
    submit_only = {key: 1 for key in admissions if key[1] in {'call:submit', 'submit'}}
    assert check(scheduler, scheduler, scheduler + '        await self.submit(_op)\n', path,
                 admissions=submit_only)


def test_admission_block_digest_is_independent_of_other_items(cycle2_gate):
    gate = cycle2_gate
    key = 'a' * 64
    def block(spec_id):
        scope = {'path': 'services/chat-stream-v2/account_executor.py', 'scope': 'Worker.call',
                 'nodes': [{'kind': 'run', 'sha256': key, 'count': 1}]}
        item = dict(spec_id=spec_id, spec_review_id='accepted-spec', reason='Correct the operation.', scopes=[scope])
        item['sha256'] = gate.admission_digest(item)
        return item

    a, b = block('item_a'), block('item_b')
    path = a['scopes'][0]['path']
    assert gate._admission_counts([a, b])[path]['Worker.call', 'run', key] == 2
    assert gate._admission_counts([b, a]) == gate._admission_counts([a, b])
    with pytest.raises(ValueError, match='duplicate'):
        gate._admission_counts([a, a])
    with pytest.raises(ValueError, match='block digest'):
        gate._admission_counts([dict(a, reason='Different text.')])


@pytest.mark.parametrize('primitive', sorted({
    'create_task', 'ensure_future', 'call_later', 'call_at', 'Timer', 'Thread',
    'sleep', 'put_nowait', 'submit_chat', 'enqueue_chat',
}))
def test_callback_admission_cannot_admit_a_new_scheduling_call(cycle2_gate, primitive):
    gate = cycle2_gate
    path = 'services/chat-stream-v2/account_executor.py'
    source = ('async def operation(runtime):\n    async def complete():\n'
              f'        queue.{primitive}(1)\n    await runtime.run(complete)\n')
    eligible = set()
    sites = gate._primitive_sites(source, path, admissible=eligible)
    grants = {(scope, kind, hashlib.sha256(sig.encode()).hexdigest()): count
              for (scope, kind, sig), count in sites.items() if (scope, kind, sig) in eligible}
    assert any(kind == 'protected-callback-call' for _, kind, _ in grants)
    assert any(f'new F1 primitive call:{primitive}' in e
               for e in gate.new_primitives('', '', source, path, admissions=grants))
