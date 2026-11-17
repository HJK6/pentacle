"""send-image through the real CLI and a real socket: the bound front desk's image
must land on the composite the operator reads; an unbound seat keeps its own chat."""
import asyncio
import hashlib
import json

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from notification_answer_fixture import fixture
from server import Server
from store import STREAM_TOKEN_HASH_VERSION
from test_front_desk_digest import _production_dispatcher

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def test_socket_send_image_reaches_composite_for_bound_seat_only(tmp_path, monkeypatch, capsys):
    from agent_orch import cli
    from agent_orch.config import Config

    async def run():
        async with fixture(tmp_path, host='fixture-host') as (_, queue, comms, provider, sessions, store):
            target, chat = 'fixture-host:v2-test', 'fixture-host:assistant'
            generation = sessions.get(target)['session_generation']
            await store.update_session('fixture-host', 'v2-test', pane_pid='4242')
            await store.grant_stream_token('fixture-host', 'v2-test', hashlib.sha256(b'fd-token').hexdigest(),
                                           STREAM_TOKEN_HASH_VERSION)
            composite = AssistantComposite(store, config=AssistantCompositeConfig(
                enabled=True, name='assistant', stream_id=chat, direct_primary_stream_id=target,
                direct_primary_generation=generation, strict_publication_default=True),
                dispatch=_production_dispatcher(comms))
            server = Server(host='127.0.0.1', port=0, store=store, sessions=sessions,
                            comms=comms, local_host='fixture-host')
            from blobs import BlobStore
            blobs = BlobStore(str(tmp_path / 'blobs'), attachment_store=store)
            await blobs.start()
            comms.blob_store = blobs
            server.blobs = blobs
            server.handlers.update(blobs.wire_handlers())
            server.assistant_composite = composite
            composite.publication_attachments = server._assistant_publication_attachments
            await composite.ensure_projection()
            await sessions.refresh()
            def seed(conn):
                conn.execute("INSERT INTO v2_tell_deliveries(tell_id,reply,created_at) VALUES ('real-tell','{}',0)")
                conn.commit()
            await store.submit(seed)
            port = await server.bind()
            png = tmp_path / 'shot.png'
            png.write_bytes(PNG)
            monkeypatch.setattr(cli, 'load_config', lambda: Config(f'ws://127.0.0.1:{port}', '', 'fixture-host', tmp_path))
            monkeypatch.setattr('agent_orch.send_boundary.stream_token_from_env', lambda: 'fd-token')
            monkeypatch.setattr('agent_orch.send_boundary._stream_token_from_file', lambda *a, **k: 'fd-token')
            monkeypatch.setenv('AGENT_ORCH_STREAM_ID', target)
            monkeypatch.setenv('PENTACLE_STREAM_ID', target)
            monkeypatch.delenv('PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID', raising=False)
            parse = cli.build_parser().parse_args
            try:
                # Bound seat without note fields: loud failure, nothing posted.
                code = await asyncio.to_thread(cli.send_image, parse(['send-image', str(png), '--caption', 'send-image test']))
                first = capsys.readouterr()
                assert code == 2, first
                # Bound seat with note fields: one attachment event on the composite.
                code = await asyncio.to_thread(cli.send_image, parse([
                    'send-image', str(png), '--caption', 'send-image test',
                    '--note-type', 'result', '--ref', 'tell:real-tell']))
                cap = capsys.readouterr()
                out = cap.out + cap.err
                assert code == 0, out
                events = [e for e in await store.fetch_session_event_tail(chat, limit=20) if e.get('attachments')]
                assert len(events) == 1 and events[0]['text'] == 'send-image test', events
                assert events[0]['attachments'][0]['key'] == hashlib.sha256(PNG).hexdigest()
                # An unbound seat is refused the composite publish and never lands on it.
                await sessions.open('fixture-host', 'v2-other', provider='codex')
                await store.grant_stream_token('fixture-host', 'v2-other', hashlib.sha256(b'other-token').hexdigest(),
                                               STREAM_TOKEN_HASH_VERSION)
                monkeypatch.setattr('agent_orch.send_boundary.stream_token_from_env', lambda: 'other-token')
                monkeypatch.setattr('agent_orch.send_boundary._stream_token_from_file', lambda *a, **k: 'other-token')
                monkeypatch.setenv('AGENT_ORCH_STREAM_ID', 'fixture-host:v2-other')
                monkeypatch.setenv('PENTACLE_STREAM_ID', 'fixture-host:v2-other')
                code = await asyncio.to_thread(cli.send_image, parse([
                    'send-image', str(png), '--caption', 'own chat']))
                cap = capsys.readouterr()
                # Not the bound front desk: today's behaviour, its own conversation only.
                assert code == 0 and json.loads(cap.out)['stream_id'] == 'fixture-host:v2-other', cap
                assert len([e for e in await store.fetch_session_event_tail(chat, limit=20) if e.get('attachments')]) == 1
            finally:
                await composite.stop()
                await server.close()
    asyncio.run(run())
