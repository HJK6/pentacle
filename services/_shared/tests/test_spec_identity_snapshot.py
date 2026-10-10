"""Polling identity inputs: incremental, immutable, bounded and fail closed."""
import asyncio
import os
from pathlib import Path
import threading

import pytest
from _shared import specs_parser
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
    original = specs_parser._frontmatter
    monkeypatch.setattr(specs_parser, '_frontmatter', lambda path: calls.append(str(path)) or original(path))
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
    monkeypatch.setattr(specs_parser, '_frontmatter', lambda path: pytest.fail('snapshot parsed live files'))
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
    monkeypatch.setattr(specs_parser, '_frontmatter', lambda path: (_ for _ in ()).throw(PermissionError('fixture')))
    with pytest.raises(SpecResolutionUnavailable):
        subject.spec_resolution_snapshot()


def test_mutation_during_parse_retries_and_does_not_publish_partial(tmp_path, monkeypatch):
    path = document(tmp_path)
    original = specs_parser._frontmatter
    calls = []
    def changing(source):
        value = original(source)
        if not calls:
            source.write_text('---\nid: spec_fixture__new\n---\n')
        calls.append(1)
        return value
    monkeypatch.setattr(specs_parser, '_frontmatter', changing)
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
