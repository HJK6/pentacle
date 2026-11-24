"""B04 gate boundary controls against a disposable source checkout.

Journey: the candidate CLI checks the frozen baseline and a trusted base.
Subject: B04 admission at the same CLI used by the merge runner and CI.
Faults: new imports, aliases, writer and recurring work, bulk callers, and
changed exceptions. Controls: unchanged baseline and named clean mutations.
Runner: Python 3.13 and pytest. Dependencies: Git and isolated temporary clones;
no network, production database, daemon or web host.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVICE = Path('services/chat-stream-v2')
BASE = '36255a907c7b330bc7e18e156df62d3ff4d20733'


@pytest.fixture
def checkout(tmp_path):
    clone = tmp_path / 'repo'
    subprocess.run(['git', 'clone', '--quiet', '--shared', str(ROOT), str(clone)], check=True)
    for name in ('daemon_import_census.py', 'daemon_b04_gate.py', 'daemon_b04_anchors.py'):
        shutil.copy2(ROOT / SERVICE / 'tools' / name, clone / SERVICE / 'tools' / name)
    shutil.copy2(ROOT / SERVICE / 'daemon_contract/b04_core_imports.json',
                 clone / SERVICE / 'daemon_contract/b04_core_imports.json')
    return clone


def run_gate(root, base=BASE):
    return subprocess.run([sys.executable, str(root / SERVICE / 'tools/daemon_b04_gate.py'),
                           '--base', base], cwd=root, capture_output=True, text=True)


def test_projection_import_gate_cli(checkout):
    baseline = run_gate(checkout)
    assert baseline.returncode == 0, baseline.stderr
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    path.write_text(original + '\nimport work_lanes_projection as new_view\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F2 import' in rejected.stderr
    path.write_text(original + '\nimport json as harmless_library\n')
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


def test_new_f1_writer_alias_and_timer_fail(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    for candidate in (
        '\ndef wrapper(store):\n    work = store.submit\n    return work\n',
        '\ndef timer(loop, job):\n    return loop.call_later(1, job)\n',
        '\ndef task(asyncio, job):\n    return asyncio.create_task(job())\n',
    ):
        path.write_text(original + candidate)
        rejected = run_gate(checkout)
        assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr
    path.write_text(original + '\ndef clean(value):\n    return value + 1\n')
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
    path.write_text(original)
    assert run_gate(checkout).returncode == 0


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


def test_new_bulk_call_alias_and_unbounded_activity_fail(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    cases = (
        '\nasync def b04(store):\n    return await store.work_lane_rows()\n',
        '\nasync def b04(store):\n    view = store.work_lane_rows\n    return await view()\n',
        '\nasync def b04(store):\n    return await store.assistant_composite_activity(stream_id="x")\n',
    )
    for candidate in cases:
        path.write_text(original + candidate)
        rejected = run_gate(checkout)
        assert rejected.returncode != 0 and 'new F2 bulk reference' in rejected.stderr
    path.write_text(original + '\nasync def b04(store):\n    return await store.assistant_composite_activity(stream_id="x", input_ids=["x"])\n')
    assert run_gate(checkout).returncode == 0


def test_exception_addition_move_and_stale_removal_fail(checkout):
    path = checkout / SERVICE / 'daemon_contract/active/F2_projections.txt'
    original = path.read_text()
    entry = 'store.py:55 store_usage'
    assert entry in original
    for mutated in (original + 'store.py:999 store_usage\n',
                    original.replace(entry, 'store.py:56 store_usage'),
                    original.replace(entry + '\n', '')):
        path.write_text(mutated)
        rejected = run_gate(checkout)
        assert rejected.returncode != 0 and ('exception' in rejected.stderr)
    path.write_text(original)
    assert run_gate(checkout).returncode == 0


def test_new_feature_alias_from_existing_core_import_fails(checkout):
    path = checkout / SERVICE / 'store.py'
    original = path.read_text()
    path.write_text(original + '\ndef b04_alias():\n    alias = store_usage\n    return alias\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new feature alias/reference' in rejected.stderr
    path.write_text(original)
    assert run_gate(checkout).returncode == 0


def test_new_feature_work_inside_existing_writer_callback_fails(checkout):
    path = checkout / SERVICE / 'store_routing.py'
    original = path.read_text()
    marker = ('            strict = self._strict_publication_state_conn(conn, stream_id)["enabled"]\n'
              '            return _activity_summary_conn(conn, stream_id, strict=strict)\n')
    assert original.count(marker) == 1
    path.write_text(original.replace(marker, '            feature_work(conn)\n' + marker))
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'protected-callback' in rejected.stderr


def test_alternate_sqlite_and_queue_writes_fail(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    for code in ('\nimport sqlite3 as sql\ndef b04():\n    return sql.connect("x")\n',
                 '\ndef b04(store, job):\n    store._queue.put(job)\n'):
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
    main = checkout / SERVICE / 'main.py'
    original_core, original_main = core.read_text(), main.read_text()
    (checkout / SERVICE / 'b04_unowned_feature.py').write_text('VALUE = 1\n')
    core.write_text(original_core + '\nimport b04_unowned_feature\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'has no owner' in rejected.stderr
    core.write_text(original_core)
    main.write_text(original_main + '\nimport work_lanes_projection as b04_composition\n')
    allowed = run_gate(checkout)
    assert allowed.returncode == 0, allowed.stderr


def test_census_modification_fails(checkout):
    manifest = checkout / SERVICE / 'daemon_contract/b04_core_imports.json'
    manifest.write_text(manifest.read_text() + ' ')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'census digest mismatch' in rejected.stderr


def test_relative_import_and_bulk_getattr_bypasses_fail(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    for candidate, expected in (
        ('\nfrom . import store_usage as b04_alias\n', 'new F2 import'),
        ('\ndef b04(work_lanes):\n    return getattr(work_lanes, "current")()\n', 'new F2 bulk reference'),
        ('\ndef b04(store):\n    alias = store.assistant_composite_activity\n    return alias(stream_id="x")\n', 'new F2 bulk reference'),
    ):
        path.write_text(original + candidate)
        rejected = run_gate(checkout)
        assert rejected.returncode != 0 and expected in rejected.stderr


def test_aliases_of_scheduler_queue_and_sqlite_fail(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    cases = (
        '\nfrom asyncio import create_task as launch\ndef b04(task):\n    return launch(task)\n',
        '\ndef b04(store, job):\n    q = store._queue\n    return q.put(job)\n',
        '\nimport sqlite3 as sql\nconnect = sql.connect\ndef b04():\n    return connect("x")\n',
        '\nfrom threading import Thread as T\ndef b04(fn):\n    worker = T(target=fn)\n    worker.start()\n',
    )
    for code in cases:
        path.write_text(original + code)
        rejected = run_gate(checkout)
        assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr


def test_merge_runner_executes_b04_cli_and_workflow_requires_it(tmp_path):
    import yaml
    sys.path.insert(0, str(ROOT / SERVICE))
    try:
        from tools import run_gate as runner
        result = runner._run_daemon_b04_selectors(tmp_path, BASE)
    finally:
        sys.path.pop(0)
    assert result['passed'], (tmp_path / 'daemon_b04_selectors.log').read_text()
    assert '[daemon-b04] PASS' in (tmp_path / 'daemon_b04_selectors.log').read_text()
    workflow = yaml.safe_load((ROOT / '.github/workflows/predeploy-tests.yml').read_text())
    steps = workflow['jobs']['contracts']['steps']
    matches = [step for step in steps if step.get('name') == 'Daemon protected-core and projection selectors']
    assert len(matches) == 1
    assert 'daemon_b04_gate.py' in matches[0]['run'] and '--base "$RELEASE_BASE"' in matches[0]['run']


def test_offline_migration_exemption_does_not_allow_new_connections(checkout):
    assert run_gate(checkout).returncode == 0
    path = checkout / SERVICE / 'daemon_migrations.py'
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


def test_feature_chat_queue_and_bulk_multiplicity_fail(checkout):
    core = checkout / SERVICE / 'sessions.py'
    original = core.read_text()
    core.write_text(original + '\nasync def b04(queue):\n    return await queue.submit_chat(feature_work)\n')
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F1 primitive' in rejected.stderr
    core.write_text(original)
    path = checkout / SERVICE / 'work_lanes_projection.py'
    original = path.read_text()
    line = '                    rows = await self.store.work_lane_rows(include_done=True)\n'
    assert original.count(line) == 1
    path.write_text(original.replace(line, line + line))
    rejected = run_gate(checkout)
    assert rejected.returncode != 0 and 'new F2 bulk reference' in rejected.stderr


def test_f1_exception_addition_and_move_fail(checkout):
    path = checkout / SERVICE / 'daemon_contract/active/F1_protected_core.txt'
    original = path.read_text()
    entry = 'services/_shared/specs_service.py:317 SpecsSubsystem._retry_loop'
    assert entry in original
    for changed in (original + 'services/_shared/specs_service.py:999 SpecsSubsystem._retry_loop\n',
                    original.replace(entry, entry.replace(':317', ':318'))):
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


def test_dynamic_writer_attribute_bypass_fails(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    for source in ('\ndef b04(store):\n    writer = getattr(store, "submit")\n    return writer(job)\n',
                   '\ndef b04(store, name):\n    return getattr(store, name)(job)\n'):
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


def test_each_named_scheduler_reference_can_retire_and_new_references_fail(checkout):
    main = checkout / SERVICE / 'main.py'
    inventory = checkout / SERVICE / 'daemon_contract/active/F1_protected_core.txt'
    original_main, original_inventory = main.read_text(), inventory.read_text()
    cases = (
        ('event_push.check_pin_drift, ', 'services/chat-stream-v2/main.py:846 EventPush.check_pin_drift'),
        ('watch_wake.missed_wake_alarm,', 'services/chat-stream-v2/main.py:846 WatchWake.missed_wake_alarm'),
        ('account_runtime.tick, ', 'services/chat-stream-v2/main.py:847 AccountRuntime.tick'),
        ('capacity_retry.tick, ', 'services/chat-stream-v2/main.py:847 CapacityRetry.tick'),
        ('queued_send_now.tick', 'services/chat-stream-v2/main.py:847 QueuedSendNow.tick'),
        ('watch_wake.tick, ', 'services/chat-stream-v2/main.py:847 WatchWake.tick'),
    )
    for expression, row in cases:
        assert original_main.count(expression) == 1 and row in original_inventory
        main.write_text(original_main.replace(expression, '', 1))
        inventory.write_text(original_inventory.replace(row + '\n', '', 1))
        control = run_gate(checkout)
        assert control.returncode == 0, (row, control.stderr)
    inventory.write_text(original_inventory)
    main.write_text(original_main.replace('watch_wake.tick, ', 'watch_wake.tick, watch_wake.tick, ', 1))
    duplicate = run_gate(checkout)
    assert duplicate.returncode != 0 and ('duplicated frozen selector' in duplicate.stderr or 'recurring-reference' in duplicate.stderr)
    main.write_text(original_main.replace('watch_wake.tick, ', 'watch_wake.tick, feature_job.tick, ', 1))
    added = run_gate(checkout)
    assert added.returncode != 0 and 'recurring-reference' in added.stderr
    main.write_text(original_main.replace('queued_send_now.tick', '', 1))
    stale = run_gate(checkout)
    assert stale.returncode != 0 and 'stale exception' in stale.stderr


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
    core = checkout / SERVICE / 'sessions.py'
    original = core.read_text()
    for code in ("__import__('json', level=unknown)", "__import__('json', level=1)",
                 "__import__('json', foreign_globals, level=1)"):
        core.write_text(original + '\n' + code + '\n')
        rejected = run_gate(checkout)
        assert rejected.returncode != 0 and 'unresolved dynamic import' in rejected.stderr
    core.write_text(original + "\n__import__('json', level=0)\n")
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
    baseline = run_gate(checkout)
    assert baseline.returncode == 0, baseline.stderr
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


def test_reviewed_bypass_clean_reflection_controls(checkout):
    path = checkout / SERVICE / 'sessions.py'
    original = path.read_text()
    for code in ('\ndef b04(other, name):\n    return getattr(other, name)()\n',
                 '\ndef b04(work_lanes):\n    return getattr(work_lanes, "status")()\n'):
        path.write_text(original + code)
        control = run_gate(checkout)
        assert control.returncode == 0, control.stderr
