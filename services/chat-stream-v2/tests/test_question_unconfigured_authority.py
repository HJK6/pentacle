"""An unconfigured question authority must not strand a direct-primary ask."""
import asyncio

from assistant_composite import AssistantComposite, AssistantCompositeConfig
from notify import Notify
from server import Server
from sessions import Sessions
from store import Store


def test_question_without_configured_authority_is_published_as_bypassed(tmp_path):
    async def run():
        store = Store(str(tmp_path / 'sessions.db'))
        store.start()
        composite = notify = server = None
        try:
            root = await store.open_session('fixture-root', 'primary', provider='codex', visibility='hidden')
            config = AssistantCompositeConfig.from_env({
                'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '1',
                'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID': 'fixture-chat:assistant',
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID': 'fixture-root:primary',
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION': root['session_generation'],
            })
            composite = AssistantComposite(store, config=config)
            await composite.ensure_projection()
            sessions = Sessions(store, local_host='fixture-chat')
            await sessions.refresh()
            server = Server(host='127.0.0.1', port=0, store=store, sessions=sessions, local_host='fixture-chat')
            server.assistant_composite = composite
            notify = Notify(str(tmp_path / 'notifications.db'), sessions=sessions, notice_store=store,
                            assistant_binding=composite.binding, assistant_stream_id=config.stream_id,
                            lane_rulings=server.lane_rulings, broadcast=server.broadcast)
            server.notify = notify
            await notify.start()
            assert (await server.lane_rulings.binding())['state'] == 'unconfigured'
            ask = {'type': 'prompt.ask', 'request_id': 'ask-unconfigured', 'from_stream_id': 'fixture-root:primary',
                   '_auth_context': {'token_verified': True, 'stream_id': 'fixture-root:primary',
                                     'session_generation': root['session_generation']},
                   'envelope': {'schema_version': 1, 'question_id': 'unconfigured-q', 'title': 'Fixture',
                                'body': 'Proceed?', 'dedup_key': 'unconfigured-q',
                                'producer_stream_id': 'fixture-root:primary', 'response_mode': 'free_text',
                                'options': []}, 'actions': []}
            reply = await notify.prompt(ask)
            assert reply['type'] == 'prompt.ask.ok', reply
            row = await server.lane_rulings.question_row('fixture-root:primary', root['session_generation'], 'unconfigured-q')
            assert row['state'] == 'done' and row['reason'] == 'bypassed'
            assert (await notify._db.call('get_agent_question', 'unconfigured-q'))['state'] == 'open'
            assert (await notify.prompt(ask))['type'] == 'prompt.ask.ok'
        finally:
            if server is not None:
                await server.lane_rulings.stop()
            if notify is not None:
                await notify.stop()
            if composite is not None:
                await composite.stop()
            store.stop()
    asyncio.run(run())
