"""Real file/SQLite contention; the lock owner is a separate owned process."""
import asyncio
from contextlib import contextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest
import attachment_locks
import store_attachments
from blobs import BlobStore
from test_managed_attachment_upload import fixture, upload
from test_managed_attachment_publish import publication_fixture
from test_voice_upload_resume import voice_harness, metadata, init, send_chunk


async def measure(store, root, sha, operation, monkeypatch, label, size):
    await store.put('unrelated-point', 'available')
    reached = threading.Event()
    original_lock = attachment_locks.digest_lock
    trace = {}

    @contextmanager
    def observed_lock(*args, **kwargs):
        if args[1] == sha:
            trace['lock_wait_thread'] = threading.current_thread().name
            reached.set()
        with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(attachment_locks, 'digest_lock', observed_lock)
    if hasattr(store_attachments, 'digest_lock'):  # same probe on the old Store-owned path
        monkeypatch.setattr(store_attachments, 'digest_lock', observed_lock)
    code = ('import sys; from attachment_locks import digest_lock; '
            'ctx=digest_lock(sys.argv[1],sys.argv[2]); ctx.__enter__(); '
            'print("locked",flush=True); sys.stdin.readline(); ctx.__exit__(None,None,None)')
    env = {**os.environ, 'PYTHONPATH': str(Path(store_attachments.__file__).parent)}
    owner = subprocess.Popen([sys.executable, '-c', code, str(root), sha],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
    pending = point = None
    lock_released = False
    try:
        assert await asyncio.to_thread(owner.stdout.readline) == 'locked\n'
        pending = asyncio.create_task(operation())
        assert await asyncio.to_thread(reached.wait, 5), 'operation never reached digest lock'
        queued = time.monotonic()

        def lookup(conn):
            trace['callback_started'] = time.monotonic()
            result = conn.execute('SELECT v FROM kv WHERE k=?', ('unrelated-point',)).fetchone()[0]
            trace['callback_finished'] = time.monotonic()
            return result

        point = asyncio.create_task(store.submit(lookup))
        await asyncio.sleep(1.2)
        trace['point_done_before_release'] = point.done()
        released = time.monotonic()
        owner.stdin.write('\n'); owner.stdin.flush()
        lock_released = True
        assert await asyncio.wait_for(point, 10) == 'available'
        result = await asyncio.wait_for(pending, 10)
        assert isinstance(result, dict) and not result.get('error_code'), result
        elapsed = trace['callback_finished'] - queued
        record = dict(case=label, bytes=size, digest=sha, lock_owner_pid=owner.pid,
                      lock_owner_kind='separate-owned-process', bound_s=0.2,
                      controlled_hold_s=released-queued,
                      queued_s=trace['callback_started']-queued,
                      callback_s=trace['callback_finished']-trace['callback_started'],
                      lookup_s=elapsed, **trace)
        print('ATTACHMENT_TIMING ' + json.dumps(record, sort_keys=True), flush=True)
        output = os.environ.get('ATTACHMENT_TIMING_OUTPUT')
        if output:
            with open(output, 'a') as f:
                f.write(json.dumps(record, sort_keys=True) + '\n')
        assert elapsed <= 0.2, record
    finally:
        # Release once; an owner that already exited (or is exiting) has closed its pipe.
        if not lock_released and owner.poll() is None:
            with suppress(BrokenPipeError):
                owner.stdin.write('\n'); owner.stdin.flush()
        await asyncio.to_thread(owner.wait, 10)
        assert owner.returncode == 0, owner.stderr.read()
        for task in (point, pending):
            if task is not None and not task.done():
                await asyncio.wait_for(asyncio.shield(task), 10)


@pytest.mark.asyncio
async def test_managed_upload_keeps_store_responsive(tmp_path, monkeypatch):
    body = b'%PDF synthetic\n' + b'x' * 65536
    async with fixture(tmp_path) as (blobs, store):
        await measure(store, blobs._root, hashlib.sha256(body).hexdigest(),
                      lambda: upload(blobs, body), monkeypatch, 'managed-upload', len(body))


@pytest.mark.asyncio
async def test_publication_keeps_store_responsive(tmp_path, monkeypatch):
    body = b'%PDF synthetic\n' + b'x' * 65536
    async with publication_fixture(tmp_path) as (blobs, store, server, msg):
        receipt = await upload(blobs, body)
        msg['attachment_ids'] = [receipt['upload_id']]
        await measure(store, blobs._root, receipt['blob_sha'],
                      lambda: server._on_assistant_publish(msg), monkeypatch, 'publication', len(body))


@pytest.mark.asyncio
async def test_input_keeps_store_responsive(tmp_path, monkeypatch):
    from test_assistant_direct_primary import ASSISTANT
    body = b'%PDF synthetic\n' + b'x' * 65536
    async with publication_fixture(tmp_path) as (blobs, store, server, msg):
        receipt = await upload(blobs, body)
        async def admit():
            return await store.admit_assistant_composite_input(
                stream_id=ASSISTANT, input_identity='input-with-file',
                input_request_id='input-with-file', body='File input',
                attachments=[{'key': receipt['blob_sha'], 'mime': receipt['media_type']}],
                reply_to_message_id=None, reply_to_question_id=None,
                actor_stream_id='operator:fixture')
        await measure(store, blobs._root, receipt['blob_sha'], admit,
                      monkeypatch, 'input', len(body))


@pytest.mark.asyncio
async def test_protocol2_voice_keeps_store_responsive(voice_harness, monkeypatch):
    h = voice_harness
    body = bytes(range(256)) * 128
    meta = metadata(h, body)
    async with h.client() as client:
        assert (await init(client, meta))['type'] == 'upload_blob.init.ok'
        await measure(h.store, h.blobs._root, meta['sha256'],
                      lambda: send_chunk(client, meta, body, final=True),
                      monkeypatch, 'protocol2-voice', len(body))


def test_oversized_destination_rejected_without_hashing(tmp_path, monkeypatch):
    path = tmp_path / 'oversized'
    with path.open('wb') as handle:
        handle.truncate(26 * 1024 * 1024)
    original = hashlib.sha256
    hashed = []
    class ObservedHash:
        def __init__(self): self.inner = original()
        def update(self, data): hashed.append(len(data)); self.inner.update(data)
        def hexdigest(self): return self.inner.hexdigest()
    monkeypatch.setattr(hashlib, 'sha256', ObservedHash)
    with pytest.raises(ValueError, match='blob_digest_mismatch'):
        BlobStore._file_identity(path, expected_size=10)
    assert hashed == []


def test_hash_reader_rejects_symlink(tmp_path):
    real = tmp_path / 'real'; real.write_bytes(b'bytes')
    link = tmp_path / 'link'; link.symlink_to(real)
    with pytest.raises(OSError):
        BlobStore._file_identity(link, expected_size=5)


def test_growing_file_read_is_capped_at_expected_size_plus_one(tmp_path, monkeypatch):
    path = tmp_path / 'growing'; path.write_bytes(b'original')
    original = os.fdopen
    reads = []
    class GrowingReader:
        def __init__(self, fd, *args): self.inner = original(fd, *args)
        def __enter__(self): return self
        def __exit__(self, *args): self.inner.close()
        def fileno(self): return self.inner.fileno()
        def read(self, size):
            if not reads:
                with path.open('ab') as writer: writer.write(b'x' * 1024 * 1024)
            result = self.inner.read(size); reads.append(len(result)); return result
    monkeypatch.setattr(os, 'fdopen', GrowingReader)
    with pytest.raises(ValueError, match='blob_digest_mismatch'):
        BlobStore._file_identity(path, expected_size=8)
    assert sum(reads) == 9


@pytest.mark.asyncio
async def test_cancelled_executor_keeps_lock_until_real_completion(tmp_path):
    from types import SimpleNamespace
    owner = SimpleNamespace()
    sha = 'b' * 64
    entered, release = threading.Event(), threading.Event()
    def io():
        entered.set()
        assert release.wait(5)
    async def operation(deadline):
        await attachment_locks.attachment_io(io)
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(attachment_locks.with_digest_locks(owner, tmp_path, [sha], operation))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        owned = asyncio.all_tasks() - baseline
        for _ in range(3):
            for t in owned: t.cancel()
            await asyncio.sleep(0)
        with pytest.raises(TimeoutError):
            with attachment_locks.digest_lock(tmp_path, sha, timeout=0): pass
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    with attachment_locks.digest_lock(tmp_path, sha, timeout=0): pass
    assert owner._attachment_operations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['acquire', 'cleanup'])
async def test_cancelled_lock_boundary_retains_descriptor(tmp_path, monkeypatch, boundary):
    from types import SimpleNamespace
    owner = SimpleNamespace()
    sha = 'c' * 64
    entered, release = threading.Event(), threading.Event()
    original = attachment_locks.digest_lock
    @contextmanager
    def held(*args, **kwargs):
        with original(*args, **kwargs):
            if boundary == 'acquire':
                entered.set(); assert release.wait(5)
            try:
                yield
            finally:
                if boundary == 'cleanup':
                    entered.set(); assert release.wait(5)
    monkeypatch.setattr(attachment_locks, 'digest_lock', held)
    async def operation(deadline): return 'done'
    baseline = asyncio.all_tasks()
    task = asyncio.create_task(attachment_locks.with_digest_locks(owner, tmp_path, [sha], operation))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        owned = asyncio.all_tasks() - baseline
        for _ in range(3):
            for t in owned: t.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        with pytest.raises(TimeoutError):
            with original(tmp_path, sha, timeout=0): pass
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    with original(tmp_path, sha, timeout=0): pass
    assert owner._attachment_operations == 0


@pytest.mark.asyncio
async def test_opposite_digest_orders_finish_without_deadlock(tmp_path):
    from types import SimpleNamespace
    owner = SimpleNamespace()
    active, peak = 0, 0
    async def operation(deadline):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        await asyncio.sleep(.01)
        active -= 1
    keys = ['d' * 64, 'e' * 64]
    await asyncio.wait_for(asyncio.gather(
        attachment_locks.with_digest_locks(owner, tmp_path, keys, operation),
        attachment_locks.with_digest_locks(owner, tmp_path, keys[::-1], operation)), 2)
    assert peak == 1 and owner._attachment_operations == 0


@pytest.mark.asyncio
async def test_partial_acquisition_timeout_releases_owned_locks(tmp_path, monkeypatch):
    from types import SimpleNamespace
    owner = SimpleNamespace()
    keys = ['d' * 64, 'e' * 64]
    monkeypatch.setattr(attachment_locks, 'LOCK_BUDGET_SECONDS', .03)
    async def forbidden(deadline): pytest.fail('operation entered without all locks')
    with attachment_locks.digest_lock(tmp_path, keys[1]):
        with pytest.raises(TimeoutError):
            await attachment_locks.with_digest_locks(owner, tmp_path, keys, forbidden)
        with attachment_locks.digest_lock(tmp_path, keys[0], timeout=0): pass
    assert owner._attachment_operations == 0


@pytest.mark.asyncio
async def test_operation_admission_has_no_unbounded_wait_queue(tmp_path):
    from types import SimpleNamespace
    owner = SimpleNamespace()
    entered = asyncio.Queue()
    release = asyncio.Event()
    async def operation(deadline):
        entered.put_nowait(True)
        await release.wait()
    tasks = [asyncio.create_task(attachment_locks.with_digest_locks(
        owner, tmp_path, [f'{i:064x}'], operation)) for i in range(8)]
    try:
        for _ in tasks: await asyncio.wait_for(entered.get(), 2)
        with pytest.raises(RuntimeError, match='operations busy'):
            await attachment_locks.with_digest_locks(owner, tmp_path, ['f' * 64], operation)
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert owner._attachment_operations == 0


@pytest.mark.asyncio
async def test_gc_skips_busy_digest_without_mutation(tmp_path):
    from test_managed_attachment_retention import age, sweep
    async with fixture(tmp_path) as (blobs, store):
        # This real upload also covers pending/ready ownership before GC races it.
        receipt = await upload(blobs)
        await age(store, receipt)
        sha = receipt['blob_sha']
        with attachment_locks.digest_lock(blobs._root, sha):
            counts = await asyncio.wait_for(sweep(store, tmp_path), .2)
            assert counts['retained'] == 1 and counts['deleted'] == 0
            assert await store.attachment_upload(receipt['upload_id'])
            assert blobs._path_for(sha).exists()
        assert (await sweep(store, tmp_path))['deleted'] == 1


@pytest.mark.asyncio
async def test_shutdown_cancellation_waits_for_store_callback(tmp_path, monkeypatch):
    """Cancel every operation task while a real Store callback is queued."""
    async with fixture(tmp_path) as (blobs, store):
        receipt = await upload(blobs)
        sha = receipt['blob_sha']
        original_submit = store.submit
        entered, release = threading.Event(), threading.Event()
        callback_ran = threading.Event()
        def blocker(conn):
            entered.set(); assert release.wait(5)
        blocker_task = asyncio.create_task(original_submit(blocker))
        assert await asyncio.to_thread(entered.wait, 5)
        queued = asyncio.Event()
        async def observed_submit(fn, **kw):
            # create_task does not run before this coroutine reaches its await;
            # the real submit enqueues synchronously before yielding.
            queued.set()
            return await original_submit(fn, **kw)
        monkeypatch.setattr(store, 'submit', observed_submit)
        baseline = asyncio.all_tasks()
        task = asyncio.create_task(store.complete_unmanaged_upload(
            root=blobs._root, sha=sha, materialize=lambda: callback_ran.set()))
        try:
            await asyncio.wait_for(queued.wait(), 5)
            owned = asyncio.all_tasks() - baseline
            for _ in range(3):
                for t in owned: t.cancel()
                await asyncio.sleep(0)
            with pytest.raises(TimeoutError):
                with attachment_locks.digest_lock(blobs._root, sha, timeout=0): pass
            assert not task.done()
            # A bounded stop can expire while ownership remains with the same worker.
            assert await asyncio.to_thread(store.stop, 0.01) is False
            with pytest.raises(RuntimeError, match='still stopping'):
                store.start()
        finally:
            release.set()
            await asyncio.gather(blocker_task, task, return_exceptions=True)
        # Cancellation of the inner owner stops before materialize, but its
        # already-admitted protection transaction definitively committed.
        assert not callback_ran.is_set()
        assert await asyncio.to_thread(store.stop, 2)
        store.start()
        row = await store.attachment_upload(receipt['upload_id'])
        assert row['legacy_protected'] == 1
        with attachment_locks.digest_lock(blobs._root, sha, timeout=0): pass


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['provenance', 'generation'])
async def test_publication_rechecks_after_off_store_verification(tmp_path, monkeypatch, mutation):
    from test_assistant_direct_primary import ROOT
    async with publication_fixture(tmp_path) as (blobs, store, server, msg):
        receipt = await upload(blobs)
        msg['attachment_ids'] = [receipt['upload_id']]
        entered, release = threading.Event(), threading.Event()
        original = store_attachments.verify_publication_bytes
        def held(*args, **kwargs):
            assert threading.current_thread().name != 'store'
            assert threading.current_thread() is not threading.main_thread()
            original(*args, **kwargs)
            entered.set(); assert release.wait(5)
        monkeypatch.setattr(store_attachments, 'verify_publication_bytes', held)
        task = asyncio.create_task(server._on_assistant_publish(msg))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            def change(conn):
                if mutation == 'provenance':
                    conn.execute("UPDATE v2_attachment_uploads SET filename='changed.pdf' WHERE upload_id=?", (receipt['upload_id'],))
                else:
                    conn.execute("UPDATE v2_session_generations SET generation='revoked-generation' WHERE host=? AND session_name=?", tuple(ROOT.split(':')))
                conn.commit()
            await asyncio.wait_for(store.submit(change), .2)
        finally:
            release.set()
        with pytest.raises(Exception, match='unverified|authorized current publisher'):
            await task
        assert await store.submit(lambda c: c.execute('SELECT count(*) FROM v2_attachment_refs').fetchone()[0]) == 0
        assert await store.submit(lambda c: c.execute('SELECT count(*) FROM v2_assistant_composite_publications').fetchone()[0]) == 0


@pytest.mark.asyncio
async def test_opt_in_rejects_unadmitted_work_without_waiting(tmp_path):
    from store import Store
    store = Store(str(tmp_path / 'admission.db'), max_pending=1)
    with pytest.raises(RuntimeError, match='not running'):
        await store.submit(lambda c: None, settle_on_cancel=True)
    store.start()
    entered, release = threading.Event(), threading.Event()
    def hold(conn): entered.set(); assert release.wait(5)
    held = asyncio.create_task(store.submit(hold))
    queued = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        queued = asyncio.create_task(store.submit(lambda c: 1, settle_on_cancel=True))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match='overloaded'):
            await asyncio.wait_for(store.submit(lambda c: 2, settle_on_cancel=True), .2)
        assert await asyncio.to_thread(store.stop, .01) is False
        with pytest.raises(RuntimeError, match='not running'):
            await store.submit(lambda c: 3, settle_on_cancel=True)
    finally:
        release.set()
        await asyncio.gather(held, *([queued] if queued else []))
        assert await asyncio.to_thread(store.stop, 2)


@pytest.mark.asyncio
async def test_opt_in_cancelled_startup_failure_settles_drain(tmp_path, monkeypatch):
    import store as store_module
    entered, release = threading.Event(), threading.Event()
    ran = []
    def fail(*args, **kwargs):
        entered.set(); assert release.wait(5)
        raise RuntimeError('synthetic initialization failure')
    monkeypatch.setattr(store_module, 'prepare_history_schema', fail)
    store = store_module.Store(str(tmp_path / 'init-failure.db'))
    starter = asyncio.create_task(asyncio.to_thread(store.start))
    submit = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        submit = asyncio.create_task(store.submit(lambda c: ran.append(True), settle_on_cancel=True))
        await asyncio.sleep(0)
        for _ in range(3): submit.cancel(); await asyncio.sleep(0)
        assert not submit.done()
    finally:
        release.set()
        with pytest.raises(RuntimeError, match='synthetic initialization failure'): await starter
        if submit:
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(submit, 2)
        assert store.stop(timeout=2)
    assert ran == [] and store.pending() == 0


@pytest.mark.asyncio
async def test_opt_in_running_failure_joins_before_cancellation(tmp_path):
    from store import Store
    store = Store(str(tmp_path / 'callback-failure.db')); store.start()
    entered, release = threading.Event(), threading.Event()
    def fail(conn):
        entered.set(); assert release.wait(5)
        raise ValueError('synthetic callback failure')
    task = asyncio.create_task(store.submit(fail, settle_on_cancel=True))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(3): task.cancel(); await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert store.stop(timeout=2)


@pytest.mark.asyncio
async def test_opt_in_cancelled_before_first_execution_never_enqueues(tmp_path):
    from store import Store
    store = Store(str(tmp_path / 'never-enqueued.db')); store.start()
    ran = []
    try:
        task = asyncio.create_task(store.submit(lambda c: ran.append(True), settle_on_cancel=True))
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert await store.submit(lambda c: 1) == 1
        assert ran == []
    finally:
        assert store.stop(timeout=2)
