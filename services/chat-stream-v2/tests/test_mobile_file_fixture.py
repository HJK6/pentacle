import asyncio
import importlib.util
from pathlib import Path
import pytest


def module():
    path=Path(__file__).resolve().parents[3]/'test/e2e/file_delivery/prepare_mobile_fixture.py'
    spec=importlib.util.spec_from_file_location('mobile_fixture',path)
    result=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_preparer_uses_ready_receipts_and_real_publications(tmp_path):
    async def run():
        from store import Store
        from blobs import BlobStore
        m=module();root=tmp_path/'new-fixture';result=await m.prepare(root)
        store=Store(str(root/'sessions.db'));store.start()
        blobs=BlobStore(str(root/'blobs'),attachment_store=store);await blobs.start()
        try:
            assert await blobs.read_verified(result['present_sha256'])==m.BODY
            with pytest.raises(ValueError,match='blob_unknown'):
                await blobs.read_verified(result['publications'][1]['sha256'])
            for publication in result['publications']:
                assert await store.attachment_upload(publication['upload_id'])
                assert await store.blob_referenced_in_stream(sha=publication['sha256'],stream_id=m.CHAT)
            events=await store.fetch_session_event_tail(m.CHAT,limit=20)
            assert len([e for e in events if e['kind']=='ASSIST_TEXT'])==2
            assert result['runtime']=='not_run' and result['authentication_handshake']=='not_run'
        finally:store.stop()
    asyncio.run(run())


def test_existing_directory_is_never_modified(tmp_path):
    root=tmp_path/'existing';root.mkdir();sentinel=root/'keep';sentinel.write_text('untouched')
    with pytest.raises(FileExistsError):asyncio.run(module().prepare(root))
    assert list(root.iterdir())==[sentinel] and sentinel.read_text()=='untouched'
