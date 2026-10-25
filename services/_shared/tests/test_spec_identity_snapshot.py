"""Polling identity inputs: incremental, immutable, bounded and fail closed."""
import asyncio
import os
from pathlib import Path
import threading

import pytest
from _shared import specs_parser
from _shared import specs_service
from _shared.specs_service import SpecsSubsystem, SpecResolutionUnavailable, spec_resolution_view


def service(root):
    return SpecsSubsystem(memory_root=root.resolve(), session_summaries=lambda: [], changed_callback=lambda ids: None)


def document(root, folder='fixture__one', identity='spec_fixture__one', status='in_progress'):
    path = root / 'work' / status / folder / 'spec.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nid: {identity}\n---\n')
    return path


def test_incremental_snapshot_is_immutable_and_retains_only_current_inputs(tmp_path, monkeypatch):
    first = document(tmp_path)
    second = document(tmp_path, 'fixture__two', 'spec_fixture__two')
    subject = service(tmp_path)
    calls = []
    original = specs_service._parse_identity_bytes
    monkeypatch.setattr(specs_service, '_parse_identity_bytes', lambda path, content: calls.append(str(path)) or original(path, content))
    before = subject.spec_resolution_snapshot()
    assert len(calls) == 2
    calls.clear()
    subject.spec_resolution_snapshot()
    assert calls == []
    stamp = first.stat().st_mtime_ns
    first.write_text('---\nid: spec_fixture__new\n---\n')
    os.utime(first, ns=(stamp, stamp))
    after = subject.spec_resolution_snapshot()
    assert calls == [str(first)]
    # Readers captured before mutation never consult the live filesystem.
    monkeypatch.setattr(specs_service, '_parse_identity_bytes', lambda path, content: pytest.fail('snapshot parsed live files'))
    assert before.canonical_spec_identity('fixture__one') == 'spec_fixture__one'
    assert after.canonical_spec_identity('fixture__one') is None
    with pytest.raises(TypeError):
        before._tree['fake'] = []
    result = before.resolve_for_spawn('fixture__one')
    result['tree_candidates'].clear()
    assert before.resolve_for_spawn('fixture__one')['tree_candidates']
    second.unlink(); second.parent.rmdir()
    subject.spec_resolution_snapshot()
    assert len(subject._identity_cache) == 1


@pytest.mark.parametrize('change', ['rename', 'status', 'duplicate', 'remove_duplicate', 'replace_inode', 'replace_id', 'missing_file'])
def test_polling_matches_cold_uncached_identity_oracle(tmp_path, change):
    path = document(tmp_path)
    duplicate = document(tmp_path, 'fixture__other', 'spec_fixture__one', 'completed') if change == 'remove_duplicate' else None
    subject = service(tmp_path)
    subject.spec_resolution_snapshot()
    if change == 'rename':
        path.parent.rename(path.parent.with_name('fixture__renamed'))
    elif change == 'status':
        target = tmp_path / 'work/completed'
        target.mkdir(); path.parent.rename(target / path.parent.name)
    elif change == 'duplicate':
        document(tmp_path, 'fixture__duplicate', 'spec_fixture__one', 'completed')
    elif change == 'remove_duplicate':
        duplicate.unlink(); duplicate.parent.rmdir()
    elif change == 'replace_inode':
        replacement = path.with_suffix('.tmp'); replacement.write_text(path.read_text()); replacement.replace(path)
    elif change == 'replace_id':
        path.write_text('---\nid: spec_fixture__new\n---\n')
    elif change == 'missing_file':
        path.unlink()
    values = ['fixture__one', 'spec_fixture__one', 'spec_fixture__new', 'fixture__renamed', 'spec_fixture__missing']
    assert subject.canonical_spec_identities(values) == service(tmp_path).canonical_spec_identities(values)


def test_read_failure_never_authorizes_last_success(tmp_path, monkeypatch):
    path = document(tmp_path)
    subject = service(tmp_path)
    subject.spec_resolution_snapshot()
    path.write_text('---\nid: spec_fixture__two\n---\n')
    monkeypatch.setattr(specs_service, '_parse_identity_bytes', lambda path, content: (_ for _ in ()).throw(PermissionError('fixture')))
    with pytest.raises(SpecResolutionUnavailable):
        subject.spec_resolution_snapshot()


def test_mutation_during_parse_retries_and_does_not_publish_partial(tmp_path, monkeypatch):
    path = document(tmp_path)
    original = specs_service._parse_identity_bytes
    calls = []
    def changing(source, content):
        value = original(source, content)
        if not calls:
            source.write_text('---\nid: spec_fixture__new\n---\n')
        calls.append(1)
        return value
    monkeypatch.setattr(specs_service, '_parse_identity_bytes', changing)
    subject = service(tmp_path)
    assert subject.canonical_spec_identity('spec_fixture__new') == 'spec_fixture__new'
    assert len(calls) == 2


def test_async_capture_coalesces_and_bounds_waiters_without_worker_queue(tmp_path, monkeypatch):
    document(tmp_path)
    subject = service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = subject._scan_folders_by_id
    calls = []
    def blocked(*args, **kwargs):
        calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(subject, '_scan_folders_by_id', blocked)
    async def run():
        tasks = [asyncio.create_task(spec_resolution_view(subject)) for _ in range(24)]
        try:
            await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(.02)
            assert len(calls) == 1
            assert subject._resolution_async[2] <= 16
            tasks[0].cancel()
        finally:
            release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(item, SpecResolutionUnavailable) for item in results) == 8
        assert sum(isinstance(item, asyncio.CancelledError) for item in results) == 1
        assert len(calls) == 1
    asyncio.run(run())


def test_environment_root_status_config_preserved_mtime_matches_cold_oracle(tmp_path, monkeypatch):
    import json
    document(tmp_path,status='allowed')
    config=tmp_path/'work/statuses.json'
    config.write_text(json.dumps({'statuses':[{'name':'allowed','order':1}]}))
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT',str(tmp_path.resolve()))
    def configured():
        return SpecsSubsystem(session_summaries=lambda:[],changed_callback=lambda ids:None)
    subject=configured()
    assert subject.canonical_spec_identity('spec_fixture__one')=='spec_fixture__one'
    stamp=config.stat()
    config.write_text(json.dumps({'statuses':[{'name':'removed','order':1}]}))
    os.utime(config,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert subject.canonical_spec_identity('spec_fixture__one') is None
    assert subject.canonical_spec_identities(['spec_fixture__one'])==configured().canonical_spec_identities(['spec_fixture__one'])


def alias_file_timestamps(path, monkeypatch):
    """Real bytes and inode, with one file's recorded timestamps held constant."""
    original_stat, original_lstat, original_fstat = os.stat, os.lstat, os.fstat
    recorded = original_lstat(path)

    class AliasedStat:
        def __init__(self, current):
            self.current = current

        def __getattr__(self, name):
            if name in ('st_mtime', 'st_ctime', 'st_mtime_ns', 'st_ctime_ns'):
                return getattr(recorded, name)
            return getattr(self.current, name)

    def alias(value):
        if (value.st_dev, value.st_ino) == (recorded.st_dev, recorded.st_ino):
            return AliasedStat(value)
        return value

    def stat(current, *args, **kwargs):
        return alias(original_stat(current, *args, **kwargs))

    def lstat(current, *args, **kwargs):
        return alias(original_lstat(current, *args, **kwargs))

    def fstat(fd):
        return alias(original_fstat(fd))

    monkeypatch.setattr(os, 'stat', stat)
    monkeypatch.setattr(os, 'lstat', lstat)
    monkeypatch.setattr(os, 'fstat', fstat)


def test_same_metadata_changed_id_matches_current_bytes(tmp_path, monkeypatch):
    path = document(tmp_path)
    alias_file_timestamps(path, monkeypatch)
    subject = service(tmp_path)
    before = subject.spec_resolution_snapshot()
    fingerprint = subject._identity_fingerprint(path)
    path.write_text('---\nid: spec_fixture__new\n---\n')
    assert subject._identity_fingerprint(path) == fingerprint
    after = subject.spec_resolution_snapshot()
    assert after.canonical_spec_identity('spec_fixture__one') is None
    assert after.canonical_spec_identity('spec_fixture__new') == 'spec_fixture__new'
    assert subject._identity_cache[path][0] == fingerprint
    assert before.canonical_spec_identity('spec_fixture__one') == 'spec_fixture__one'


def test_same_metadata_changed_status_never_authorizes_removed_bucket(tmp_path, monkeypatch):
    import json
    document(tmp_path, status='allowed')
    config = tmp_path / 'work/statuses.json'
    config.write_text(json.dumps({'statuses': [{'name': 'allowed', 'order': 1}]}))
    alias_file_timestamps(config, monkeypatch)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))
    subject = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None)
    assert subject.canonical_spec_identity('spec_fixture__one') == 'spec_fixture__one'
    fingerprint = subject._identity_fingerprint(config)
    config.write_text(json.dumps({'statuses': [{'name': 'removed', 'order': 1}]}))
    assert subject._identity_fingerprint(config) == fingerprint
    assert subject.canonical_spec_identity('spec_fixture__one') is None
    assert subject._statuses_fingerprint[0] == fingerprint


def test_same_metadata_scan_to_consume_changed_identity_is_rejected(tmp_path, monkeypatch):
    from _shared.spec_reader import SpecReader
    path = document(tmp_path)
    alias_file_timestamps(path, monkeypatch)
    reader = SpecReader(tmp_path)
    fingerprint = reader.service._identity_fingerprint(path)
    path.write_text('---\nid: spec_fixture__new\n---\n')
    assert reader.service._identity_fingerprint(path) == fingerprint
    with pytest.raises(ValueError, match='spec_changed'):
        reader.read('spec_fixture__one')
    assert reader.service._identity_cache[path][0] == fingerprint


def deny_fixture_open(path, monkeypatch, *, alternating=False):
    original = os.open
    parent = path.parent.stat()
    calls = []

    def opened(name, flags, *args, **kwargs):
        directory = kwargs.get('dir_fd')
        matches = Path(name) == path
        if directory is not None and str(name) == path.name:
            value = os.fstat(directory)
            matches = (value.st_dev, value.st_ino) == (parent.st_dev, parent.st_ino)
        if matches:
            calls.append(1)
            if not alternating or len(calls) % 2:
                raise PermissionError('fixture read denied')
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', opened)
    return calls


def test_same_metadata_warm_unreadable_spec_never_authorizes(tmp_path, monkeypatch):
    path = document(tmp_path)
    subject = service(tmp_path)
    subject.spec_resolution_snapshot()
    deny_fixture_open(path, monkeypatch)
    with pytest.raises(SpecResolutionUnavailable):
        subject.spec_resolution_snapshot()


def patch_identity_parse(monkeypatch, after_parse):
    """Keep the path-labelled observation across the captured-parser migration."""
    if hasattr(specs_service, '_parse_identity_bytes'):
        original = specs_service._parse_identity_bytes
        def parsed(path, content):
            front = original(path, content)
            after_parse(path)
            return front
        monkeypatch.setattr(specs_service, '_parse_identity_bytes', parsed)
    else:
        original = specs_parser._frontmatter
        def parsed(path):
            front = original(path)
            after_parse(path)
            return front
        monkeypatch.setattr(specs_parser, '_frontmatter', parsed)


def test_same_metadata_early_file_mutation_retries_whole_capture(tmp_path, monkeypatch):
    first = document(tmp_path, 'fixture__aaa')
    second = document(tmp_path, 'fixture__zzz', 'spec_fixture__two')
    alias_file_timestamps(first, monkeypatch)
    original_iterdir = Path.iterdir
    def ordered(path):
        values = original_iterdir(path)
        return iter(sorted(values)) if path == first.parent.parent else values
    monkeypatch.setattr(Path, 'iterdir', ordered)
    mutations = []
    def mutate(path):
        if path == second and not mutations:
            first.write_text('---\nid: spec_fixture__new\n---\n')
            mutations.append(1)
    patch_identity_parse(monkeypatch, mutate)
    result = service(tmp_path).spec_resolution_snapshot()
    assert mutations == [1]
    assert result.canonical_spec_identity('spec_fixture__new') == 'spec_fixture__new'
    assert result.canonical_spec_identity('spec_fixture__one') is None


def test_same_metadata_perpetual_mutation_exhausts_two_attempts(tmp_path, monkeypatch):
    path = document(tmp_path)
    alias_file_timestamps(path, monkeypatch)
    calls = []
    def mutate(source):
        assert source == path
        calls.append(1)
        identity = 'new' if len(calls) % 2 else 'one'
        path.write_text(f'---\nid: spec_fixture__{identity}\n---\n')
    patch_identity_parse(monkeypatch, mutate)
    with pytest.raises(SpecResolutionUnavailable):
        service(tmp_path).spec_resolution_snapshot()
    assert len(calls) == 2


@pytest.mark.parametrize('alternating', [False, True])
def test_environment_status_read_outcome_is_captured_and_sealed(tmp_path, monkeypatch, alternating):
    import json
    document(tmp_path, status='allowed')
    config = tmp_path / 'work/statuses.json'
    config.write_text(json.dumps({'statuses': [{'name': 'allowed', 'order': 1}]}))
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))
    subject = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None)
    assert subject.canonical_spec_identity('spec_fixture__one') == 'spec_fixture__one'
    calls = deny_fixture_open(config, monkeypatch, alternating=alternating)
    if alternating:
        with pytest.raises(SpecResolutionUnavailable):
            subject.spec_resolution_snapshot()
        assert len(calls) == 4
    else:
        assert subject.canonical_spec_identity('spec_fixture__one') is None
        assert len(calls) == 2


@pytest.mark.parametrize('explicit', [False, True])
def test_root_with_symlinked_ancestor_pins_canonical_path(tmp_path, monkeypatch, explicit):
    real = tmp_path / 'real/nested'
    document(real)
    alias = tmp_path / 'alias'
    alias.symlink_to(real.parent, target_is_directory=True)
    root = alias / 'nested'
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(root))
    subject = SpecsSubsystem(memory_root=root if explicit else None,
                             session_summaries=lambda: [], changed_callback=lambda ids: None)
    result = subject.resolve_for_spawn('spec_fixture__one')
    assert result['resolution'] == 'resolved'
    assert subject.memory_root == real.resolve()
    assert subject.spec_id_for_path(real / 'work/in_progress/fixture__one/spec.md') == 'fixture__one'


def test_symlinked_child_cannot_authorize(tmp_path):
    outside = document(tmp_path / 'outside')
    root = tmp_path / 'root'
    bucket = root / 'work/in_progress'
    bucket.mkdir(parents=True)
    (bucket / 'fixture__one').symlink_to(outside.parent, target_is_directory=True)
    assert service(root).canonical_spec_identity('spec_fixture__one') is None


@pytest.mark.parametrize('newline', ['\r\n', '\r'])
def test_identity_newline_parity_and_consumed_text_are_preserved(tmp_path, newline):
    from _shared.spec_reader import SpecReader
    path = document(tmp_path)
    content = '---\nid: spec_fixture__one\n---\n'.replace('\n', newline)
    path.write_bytes(content.encode('utf-8'))
    assert service(tmp_path).canonical_spec_identity('spec_fixture__one') == 'spec_fixture__one'
    assert SpecReader(tmp_path).read('spec_fixture__one')[3] == content


@pytest.mark.parametrize('payload, expected', [
    (b'{}', 'empty'), (b'{"statuses":null}', 'empty'), (b'{"statuses":[]}', 'empty'),
    (b'[]', 'defaults'), (b'{"statuses":1}', 'defaults'), (b'!', 'defaults'),
    (b'\xff', 'defaults'), (b'{"statuses":"x"}', TypeError),
    (b'{"statuses":[{"name":"in_progress"}]}', KeyError),
    (b'{"statuses":[{"name":"in_progress","order":"bad"}]}', ValueError),
    (b'{"statuses":[{"name":"in_progress","order":true}]}', 'valid'),
    (b'{"statuses":[{"name":"in_progress","order":"1"}]}', 'valid'),
    (b'{"statuses":[{"name":null,"order":1}]}', 'bad_name'),
])
def test_environment_status_policy_and_exception_boundaries(tmp_path, monkeypatch, payload, expected):
    document(tmp_path)
    (tmp_path / 'work/statuses.json').write_bytes(payload)
    monkeypatch.setenv('PENTACLE_MEMORY_ROOT', str(tmp_path.resolve()))
    if isinstance(expected, type):
        with pytest.raises(expected):
            specs_parser.parse_statuses_json(tmp_path)
    else:
        statuses, _ = specs_parser.parse_statuses_json(tmp_path)
        if expected == 'empty':
            assert statuses == []
        elif expected == 'defaults':
            assert statuses == specs_parser.DEFAULT_STATUSES
    subject = SpecsSubsystem(session_summaries=lambda: [], changed_callback=lambda ids: None)
    original, calls = subject._load_statuses, []
    def loaded(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(subject, '_load_statuses', loaded)
    if expected in (TypeError, KeyError, ValueError, 'bad_name'):
        error = TypeError if expected == 'bad_name' else expected
        with pytest.raises(SpecResolutionUnavailable if error is ValueError else error):
            subject.spec_resolution_snapshot()
        assert len(calls) == (2 if error is ValueError else 1)
    else:
        assert subject.canonical_spec_identity('spec_fixture__one') == (None if expected == 'empty' else 'spec_fixture__one')


@pytest.mark.parametrize('order', [True, '1'])
def test_explicit_status_order_does_not_inherit_environment_coercion(tmp_path, order):
    import json
    document(tmp_path)
    (tmp_path / 'work/statuses.json').write_text(json.dumps({'statuses': [{'name': 'in_progress', 'order': order}]}))
    with pytest.raises(ValueError, match='work_statuses_invalid'):
        service(tmp_path)._load_statuses()
