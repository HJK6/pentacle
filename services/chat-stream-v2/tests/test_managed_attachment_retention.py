"""Only temporary stores and synthetic bytes; no user/fleet state."""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sqlite3
import threading
import pytest
from attachment_retention import sweep_managed
from test_managed_attachment_upload import fixture, upload
from test_managed_attachment_publish import publication_fixture, ASSISTANT

NOW = datetime(2026, 10, 4, 13, tzinfo=timezone.utc)

async def age(store, receipt, hours=25, state='ready'):
    stamp = (NOW-timedelta(hours=hours)).isoformat()
    await store.submit(lambda c: (c.execute('UPDATE v2_attachment_uploads SET uploaded_at=?,updated_at=?,state=? WHERE upload_id=?', (stamp,stamp,state,receipt['upload_id'])), c.commit()))

async def sweep(store, root, **kw):
    return await store.submit(lambda c: sweep_managed(c, root/'blobs', now=NOW, **kw))

def path(root, receipt):
    return root/'blobs'/receipt['blob_sha'][:2]/receipt['blob_sha']

@pytest.mark.parametrize('hours,deleted', [(23.99,0),(24,1),(25,1)])
def test_orphan_age_is_server_record_not_mtime(tmp_path,hours,deleted):
    async def run():
        import os
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r,hours)
            os.utime(path(tmp_path,r),(1,1))
            assert (await sweep(store,tmp_path))['deleted']==deleted
            assert path(tmp_path,r).exists()==(not deleted)
            assert (await sweep(store,tmp_path))['deleted']==0
    asyncio.run(run())

@pytest.mark.parametrize('order',['before','after'])
@pytest.mark.parametrize('purpose',[None,'report'])
def test_generic_report_shared_bytes_are_never_swept(tmp_path,order,purpose):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            if order=='before':await upload(blobs,rid='generic',purpose=purpose)
            r=await upload(blobs);await age(store,r)
            if order=='after':await upload(blobs,rid='generic',purpose=purpose)
            assert (await sweep(store,tmp_path))['deleted']==0
            assert path(tmp_path,r).exists()
    asyncio.run(run())

@pytest.mark.parametrize('crash',['pending-absent','pending-present','ready-absent','pending-corrupt'])
def test_crash_reconciliation(tmp_path,crash):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r,state='pending' if crash.startswith('pending') else 'ready')
            if crash.endswith('absent'):path(tmp_path,r).unlink()
            if crash.endswith('corrupt'):path(tmp_path,r).write_bytes(b'corrupt synthetic')
            result=await sweep(store,tmp_path)
            row=await store.attachment_upload(r['upload_id'],ready_only=False)
            if crash=='pending-present':assert result['promoted']==1 and row['state']=='ready'
            else:assert result['reconciled']==1 and row is None and not path(tmp_path,r).exists()
    asyncio.run(run())


def test_shared_chat_owners_archive_and_legacy(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            for identity in ['first','second']:
                await store.append_session_event('fixture:chat',dict(kind='USER',text=identity,attachments=[dict(key=r['blob_sha'])]),identity=identity,limit=100)
            assert (await sweep(store,tmp_path))['deleted']==0
            await store.submit(lambda c:(c.execute("DELETE FROM session_event_tail WHERE identity='first'"),c.commit()))
            assert (await sweep(store,tmp_path))['deleted']==0
            archive=tmp_path/'archive.db';c=sqlite3.connect(archive)
            c.execute('CREATE TABLE session_event_tail(event_json TEXT)');c.execute('INSERT INTO session_event_tail VALUES(?)',(r['blob_sha'],));c.commit();c.close()
            await store.submit(lambda c:(c.execute('DELETE FROM session_event_tail'),c.commit()))
            assert (await sweep(store,tmp_path,archive_path=archive))['deleted']==0
            c=sqlite3.connect(archive);c.execute('DELETE FROM session_event_tail');c.commit();c.close()
            assert (await sweep(store,tmp_path,archive_path=archive))['deleted']==1
            legacy=tmp_path/'blobs'/'ab'/('ab'*32);legacy.parent.mkdir(exist_ok=True);legacy.write_bytes(b'legacy')
            await sweep(store,tmp_path)
            assert legacy.read_bytes()==b'legacy'
    asyncio.run(run())


def test_reupload_before_and_after_unlink(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            fresh=await upload(blobs,rid='new');await age(store,fresh,hours=1)
            assert (await sweep(store,tmp_path))['deleted']==0
            await age(store,fresh)
            assert (await sweep(store,tmp_path))['deleted']==1
            rebuilt=await upload(blobs,rid='after-unlink')
            assert await blobs.read_verified(rebuilt['blob_sha'])==b'%PDF synthetic'
            assert (await store.attachment_upload(rebuilt['upload_id']))['state']=='ready'
    asyncio.run(run())


def test_publication_is_retained_including_missing_byte_tombstone(tmp_path):
    async def run():
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);await age(store,r);msg['attachment_ids']=[r['upload_id']]
            await server._on_assistant_publish(msg)
            assert (await sweep(store,tmp_path))['deleted']==0
            path(tmp_path,r).unlink()
            assert (await sweep(store,tmp_path))['deleted']==0
            assert await store.blob_referenced_in_stream(sha=r['blob_sha'],stream_id=ASSISTANT)
            with pytest.raises(ValueError,match='blob_unknown'):await blobs.read_verified(r['blob_sha'])
    asyncio.run(run())


def test_fresh_pending_unknown_timestamp_and_bounded_cursor(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r,hours=.01,state='pending')
            assert (await sweep(store,tmp_path))['deleted']==0
            assert (await store.attachment_upload(r['upload_id'],ready_only=False))['state']=='pending'
            await store.submit(lambda c:(c.execute("UPDATE v2_attachment_uploads SET state='ready',uploaded_at='unknown'"),c.commit()))
            assert (await sweep(store,tmp_path))['deleted']==0
            rows=[r]+[await upload(blobs,b'%PDF '+bytes([n]),rid=str(n)) for n in range(3)]
            for receipt in rows:await age(store,receipt)
            pinned=min(rows,key=lambda x:x['blob_sha'])
            await store.append_session_event('fixture:chat',dict(kind='USER',text=pinned['blob_sha']),identity='pin',limit=100)
            first=await sweep(store,tmp_path,limit=1)
            assert first['deleted']==0 and first['next_cursor']==pinned['blob_sha']
            assert (await sweep(store,tmp_path,limit=1,after_sha=first['next_cursor']))['deleted']==1
    asyncio.run(run())

@pytest.mark.parametrize('first',['upload','gc'])
def test_distinct_store_workers_serialize_both_interleavings(tmp_path,monkeypatch,first):
    async def run():
        from store import Store
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            second=Store(str(tmp_path/'fixture.db'));second.start()
            entered=threading.Event();release=threading.Event()
            try:
                if first=='upload':
                    original=blobs._materialize_managed
                    def held(up,sha):
                        entered.set();assert release.wait(5);return original(up,sha)
                    monkeypatch.setattr(blobs,'_materialize_managed',held)
                    leading=asyncio.create_task(upload(blobs,rid='racing-upload'))
                    assert await asyncio.to_thread(entered.wait,5)
                    following=asyncio.create_task(sweep(second,tmp_path))
                else:
                    import attachment_retention
                    original=attachment_retention._unlink_owned
                    def held(p,*args,**kw):
                        result=original(p,*args,**kw)
                        if p.sha==r['blob_sha']:entered.set();assert release.wait(5)
                        return result
                    monkeypatch.setattr(attachment_retention,'_unlink_owned',held)
                    leading=asyncio.create_task(sweep(second,tmp_path))
                    assert await asyncio.to_thread(entered.wait,5)
                    following=asyncio.create_task(upload(blobs,rid='racing-rebuild'))
                await asyncio.sleep(.02);assert not following.done()
                release.set();a,b=await asyncio.gather(leading,following)
                receipt=a if first=='upload' else b
                assert receipt['type']=='upload_blob.ok'
                assert await blobs.read_verified(receipt['blob_sha'])==b'%PDF synthetic'
                assert (await store.attachment_upload(receipt['upload_id']))['state']=='ready'
            finally:release.set();second.stop()
    asyncio.run(run())


def test_unlink_then_row_delete_failure_recovers(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            await store.submit(lambda c:c.execute("CREATE TRIGGER synthetic_gc_failure BEFORE DELETE ON v2_attachment_uploads BEGIN SELECT RAISE(ABORT,'synthetic crash'); END"))
            with pytest.raises(sqlite3.IntegrityError,match='synthetic crash'):await sweep(store,tmp_path)
            assert not path(tmp_path,r).exists() and await store.attachment_upload(r['upload_id'])
            await store.submit(lambda c:c.execute('DROP TRIGGER synthetic_gc_failure'))
            assert (await sweep(store,tmp_path))['reconciled']==1
            assert (await sweep(store,tmp_path))['reconciled']==0
    asyncio.run(run())


def test_prompt_transport_shared_bytes_excluded(tmp_path):
    async def run():
        import base64
        from blobs import PromptBlob
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            await blobs._on_init(dict(request_id='prompt',_client_websocket=object()),PromptBlob)
            result=await blobs._on_chunk(dict(request_id='prompt',data_b64=base64.b64encode(b'%PDF synthetic').decode(),final=True),PromptBlob)
            assert result['type']==PromptBlob.upload_ok
            assert (await sweep(store,tmp_path))['deleted']==0
    asyncio.run(run())

@pytest.mark.parametrize('owner',['report','prompt','evidence'])
def test_other_owner_representations_pin_digest(tmp_path,owner):
    async def run():
        import json
        from test_retention import seed_schedule_row
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            def add(c):
                if owner=='report':c.execute("INSERT INTO v2_reports(report_id,from_stream_id,status,findings,ingested_at,created_at) VALUES('fixture-report','fixture:seat','success',?,?,0)",(r['blob_sha'],NOW.isoformat()))
                elif owner=='prompt':seed_schedule_row(c,'fixture-prompt',state='pending',terminal_days=None,prompt_blob_id=r['blob_sha'])
                else:c.execute("INSERT INTO v2_assistant_composite_operations(operation_id,operation,payload_digest,evidence_refs_json,created_at) VALUES('fixture-evidence','lane.admit','synthetic',?,?)",(json.dumps([r['upload_id']]),NOW.isoformat()))
                c.commit()
            await store.submit(add)
            assert (await sweep(store,tmp_path))['deleted']==0 and path(tmp_path,r).exists()
            if owner in ['report','prompt']:assert (await store.attachment_upload(r['upload_id']))['legacy_protected']==1
    asyncio.run(run())

@pytest.mark.parametrize('first',['publication','gc'])
def test_publication_gc_interleaving_never_commits_a_missing_file(tmp_path,monkeypatch,first):
    async def run():
        from store import Store
        from contextlib import contextmanager
        async with publication_fixture(tmp_path) as (blobs,store,server,msg):
            r=await upload(blobs);await age(store,r);msg['attachment_ids']=[r['upload_id']]
            second=Store(str(tmp_path/'fixture.db'));second.start()
            entered=threading.Event();release=threading.Event()
            try:
                if first=='publication':
                    original=store.publication_attachment_guard
                    @contextmanager
                    def held(*args,**kwargs):
                        with original(*args,**kwargs):
                            entered.set();assert release.wait(5);yield
                    monkeypatch.setattr(store,'publication_attachment_guard',held)
                    leading=asyncio.create_task(server._on_assistant_publish(msg))
                    assert await asyncio.to_thread(entered.wait,5)
                    following=asyncio.create_task(sweep(second,tmp_path))
                else:
                    import attachment_retention
                    original=attachment_retention._unlink_owned
                    def held(p,*args,**kwargs):
                        result=original(p,*args,**kwargs)
                        if p.sha==r['blob_sha']:entered.set();assert release.wait(5)
                        return result
                    monkeypatch.setattr(attachment_retention,'_unlink_owned',held)
                    leading=asyncio.create_task(sweep(second,tmp_path))
                    assert await asyncio.to_thread(entered.wait,5)
                    following=asyncio.create_task(server._on_assistant_publish(msg))
                await asyncio.sleep(.02);release.set()
                a,b=await asyncio.gather(leading,following,return_exceptions=True)
                published=a if first=='publication' else b
                events=[e for e in await store.fetch_session_event_tail(ASSISTANT,limit=20) if e['kind']=='ASSIST_TEXT']
                if first=='publication':
                    assert not isinstance(published,BaseException) and len(events)==1
                    assert path(tmp_path,r).exists()
                else:
                    assert isinstance(published,Exception) and events==[]
            finally:release.set();second.stop()
    asyncio.run(run())

@pytest.mark.parametrize('encoding',['upper','escaped'])
def test_retention_decodes_json_and_conservatively_pins_noncanonical_text(tmp_path,encoding):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            value=r['blob_sha'].upper() if encoding=='upper' else r['blob_sha']
            await store.append_session_event('fixture:chat',dict(kind='USER',attachments=[dict(key=value)]),identity='encoded',limit=100)
            if encoding=='escaped':
                await store.submit(lambda c:(c.execute("UPDATE session_event_tail SET event_json=replace(event_json,?,?)",(value,''.join('\\u%04x'%ord(char) for char in value))),c.commit()))
            assert (await sweep(store,tmp_path))['deleted']==0
            # Conservative storage retention never turns the same text into auth.
            assert not await store.blob_referenced_in_stream(sha=r['blob_sha'],stream_id='fixture:chat')
    asyncio.run(run())


def test_schedule_purge_preserves_prompt_exclusion_before_owner_release(tmp_path):
    async def run():
        from test_retention import seed_schedule_row
        from retention import RetentionJob,RetentionConfig
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            def add(c):
                seed_schedule_row(c,'retired-prompt',state='fired',terminal_days=-40,prompt_blob_id=r['blob_sha'])
                c.commit()
            await store.submit(add)
            await RetentionJob(store,RetentionConfig(blob_root=tmp_path/'blobs')).run_pass()
            assert (await store.attachment_upload(r['upload_id']))['legacy_protected']==1
            assert path(tmp_path,r).exists()
    asyncio.run(run())


def test_unexpected_symlink_is_never_removed_or_followed(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r);path(tmp_path,r).unlink()
            target=tmp_path/'unrelated';target.write_bytes(b'keep')
            path(tmp_path,r).symlink_to(target)
            assert (await sweep(store,tmp_path))['deleted']==0
            assert path(tmp_path,r).is_symlink() and target.read_bytes()==b'keep'
    asyncio.run(run())


def test_missing_blob_root_does_not_erase_provenance(tmp_path):
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs);await age(store,r)
            (tmp_path/'blobs').rename(tmp_path/'moved-blobs')
            assert (await sweep(store,tmp_path))['reconciled']==0
            assert await store.attachment_upload(r['upload_id'])
            assert not (tmp_path/'blobs').exists()
    asyncio.run(run())

@pytest.mark.parametrize('state', ['ready', 'pending'])
def test_symlinked_shard_preserves_external_bytes_and_provenance(tmp_path, state):
    async def run():
        async with fixture(tmp_path) as (blobs, store):
            r = await upload(blobs); await age(store, r, state=state)
            shard = path(tmp_path, r).parent
            outside = tmp_path / 'outside'
            shard.rename(outside)
            shard.symlink_to(outside, target_is_directory=True)
            before = await store.attachment_upload(r['upload_id'], ready_only=False)
            result = await sweep(store, tmp_path)
            assert result['deleted'] == result['reconciled'] == result['promoted'] == 0
            assert (outside / r['blob_sha']).is_file()
            assert await store.attachment_upload(r['upload_id'], ready_only=False) == before
    asyncio.run(run())

@pytest.mark.parametrize('entrypoint', ['direct', 'run_pass'])
def test_configured_missing_archive_fails_before_any_mutation(tmp_path, entrypoint):
    async def run():
        from retention import RetentionJob, RetentionConfig, RetentionError
        from test_retention import seed_schedule_row
        async with fixture(tmp_path) as (blobs, store):
            r = await upload(blobs); await age(store, r)
            archive = tmp_path / 'unavailable-archive.db'
            def seed(c):
                seed_schedule_row(c, 'must-retain', state='fired', terminal_days=-40)
                c.commit()
                return list(c.iterdump())
            before = await store.submit(seed)
            with pytest.raises((sqlite3.OperationalError, RetentionError)):
                if entrypoint == 'direct':
                    await sweep(store, tmp_path, archive_path=archive)
                else:
                    await RetentionJob(store, RetentionConfig(blob_root=tmp_path/'blobs', archive_path=archive)).run_pass()
            assert not archive.exists()
            assert path(tmp_path, r).is_file()
            assert await store.submit(lambda c: list(c.iterdump())) == before
    asyncio.run(run())

@pytest.mark.parametrize('component', ['ancestor', 'root', 'locks', 'missing-shard'])
def test_unavailable_or_symlinked_components_fail_closed(tmp_path, component):
    async def run():
        base = tmp_path / 'base'; base.mkdir()
        async with fixture(base) as (blobs, store):
            r = await upload(blobs); await age(store, r)
            original = path(base, r).read_bytes()
            if component == 'ancestor':
                base.rename(tmp_path/'moved'); base.symlink_to(tmp_path/'moved', target_is_directory=True)
            else:
                target = base/'blobs' if component == 'root' else base/'blobs'/'.attachment-locks' if component == 'locks' else path(base,r).parent
                moved = tmp_path/'moved'; target.rename(moved)
                if component != 'missing-shard': target.symlink_to(moved, target_is_directory=True)
            result = await sweep(store, base)
            assert result['deleted'] == result['reconciled'] == result['promoted'] == 0
            assert await store.attachment_upload(r['upload_id'])
            if component != 'missing-shard': assert path(base,r).read_bytes() == original
            else: assert (moved/r['blob_sha']).read_bytes() == original
    asyncio.run(run())


def test_shard_substitution_after_open_cannot_redirect_unlink(tmp_path, monkeypatch):
    import attachment_retention
    async def run():
        async with fixture(tmp_path) as (blobs,store):
            r=await upload(blobs); await age(store,r)
            shard=path(tmp_path,r).parent
            outside=tmp_path/'outside'; outside.mkdir()
            external=outside/r['blob_sha']; external.write_bytes(b'outside must survive')
            moved=tmp_path/'pinned-original'
            original=attachment_retention._unlink_owned
            def substitute(blob):
                shard.rename(moved); shard.symlink_to(outside, target_is_directory=True)
                return original(blob)
            monkeypatch.setattr(attachment_retention,'_unlink_owned',substitute)
            assert (await sweep(store,tmp_path))['deleted']==1
            assert external.read_bytes()==b'outside must survive'
            assert not (moved/r['blob_sha']).exists()
    asyncio.run(run())

@pytest.mark.parametrize('moment', ['after-preflight', 'after-schema', 'available'])
def test_configured_archive_reopen_never_creates_replacement(tmp_path, monkeypatch, moment):
    async def run():
        import retention
        async with fixture(tmp_path) as (blobs, store):
            r=await upload(blobs); await age(store,r)
            archive=tmp_path/'archive.db'
            c=sqlite3.connect(archive)
            c.execute('CREATE TABLE session_event_tail(event_json TEXT)')
            c.execute('INSERT INTO session_event_tail VALUES(?)',(r['blob_sha'],)); c.commit(); c.close()
            job=retention.RetentionJob(store,retention.RetentionConfig(blob_root=tmp_path/'blobs',archive_path=archive))
            if moment=='after-preflight':
                original=job._purge_schedule_classes
                def disappear(c,cfg):
                    archive.rename(tmp_path/'saved-archive.db')
                    return original(c,cfg)
                monkeypatch.setattr(job,'_purge_schedule_classes',disappear)
            elif moment=='after-schema':
                original=retention.ensure_archive
                def disappear(*args,**kwargs):
                    original(*args,**kwargs)
                    archive.rename(tmp_path/'saved-archive.db')
                monkeypatch.setattr(retention,'ensure_archive',disappear)
            if moment=='available':
                # Avoid an unrelated event-archive schema mismatch in this
                # minimal owner-inventory fixture.
                monkeypatch.setattr(job,'_prepare',lambda c,cfg:(set(),False))
                result=await job.run_pass()
                assert result.managed_blobs_deleted==0
            else:
                with pytest.raises(sqlite3.OperationalError):await job.run_pass()
                assert not archive.exists()
            assert path(tmp_path,r).is_file()
            assert await store.attachment_upload(r['upload_id'])
    asyncio.run(run())

@pytest.mark.parametrize('filename', [':memory:', '', 'literal?#%.db', 'file:literal.db'])
def test_store_uri_support_preserves_literal_and_temporary_paths(tmp_path, filename):
    from store import Store
    async def run():
        target=filename if filename in (':memory:', '') else str(tmp_path/filename)
        store=Store(target); store.start()
        try:
            assert await store.submit(lambda c:c.execute('SELECT 1').fetchone()[0])==1
        finally: store.stop()
        if filename not in (':memory:', ''): assert (tmp_path/filename).is_file()
    asyncio.run(run())


def test_default_archive_in_literal_file_prefix_directory(tmp_path, monkeypatch):
    from retention import RetentionJob
    from store import Store
    async def run():
        monkeypatch.chdir(tmp_path)
        Path('file:parent').mkdir()
        store=Store('file:parent/fixture.db');store.start()
        try:
            await RetentionJob(store).run_pass()
            assert Path('file:parent/sessions_archive.db').is_file()
            assert not Path('parent').exists()
        finally:store.stop()
    asyncio.run(run())
