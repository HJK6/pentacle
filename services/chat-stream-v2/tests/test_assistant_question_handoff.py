"""A card release and producer handoff serialize on the real lifecycle lock."""
import asyncio
import json
import pytest

from notify import Notify
from test_lifecycle_continuity import environment, HOST, SOURCE


def test_approved_question_release_serializes_with_real_handoff(tmp_path):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            source = await store.fetch_session(HOST, 'source')
            successor = await opened('successor')
            notify = Notify(str(tmp_path / 'notifications.db'), sessions=sessions, notice_store=store,
                            lane_rulings=server.lane_rulings)
            await notify.start()
            server.notify = notify
            ctl.consent_notify = notify
            entered, proceed = asyncio.Event(), asyncio.Event()
            release = handoff = None
            try:
                frozen = {'envelope': {'schema_version': 1, 'question_id': 'qa-race', 'title': 'Probe',
                          'body': 'Proceed?', 'dedup_key': 'qa-race', 'producer_stream_id': SOURCE,
                          'producer_session_generation': source['session_generation'],
                          'response_mode': 'free_text', 'options': []}, 'actions': [], 'severity': 'info'}
                row = await server.lane_rulings._create(action='question', key='qa-race', intent=frozen,
                    requester=SOURCE, requester_generation=source['session_generation'],
                    binding=await server.lane_rulings.binding(), target=SOURCE, target_generation='', covered=True)
                rid = row['ruling_request_id']
                await store.submit(lambda c: (c.execute("UPDATE v2_assistant_lane_rulings SET state='approved',ruling='approve' WHERE ruling_request_id=?", (rid,)), c.commit()))
                original = notify._prompt_publish
                async def paused(*args, **kwargs):
                    entered.set()
                    await proceed.wait()
                    return await original(*args, **kwargs)
                notify._prompt_publish = paused
                release = asyncio.create_task(server.lane_rulings._release(row))
                await asyncio.wait_for(entered.wait(), 2)
                handoff = asyncio.create_task(ctl._finish_handoff({'handoff_from_stream_id': SOURCE}, successor['stream_id']))
                await asyncio.sleep(0.05)
                assert not handoff.done(), 'handoff must wait until publication completes'
                proceed.set()
                await asyncio.wait_for(release, 2)
                result = await asyncio.wait_for(handoff, 2)
                assert result['state'] == 'complete'
                question = await notify._db.call('get_agent_question', 'qa-race')
                assert question['state'] == 'open'
                assert question['producer_stream_id'] == successor['stream_id']
                assert (await server.lane_rulings._fetch(rid))['state'] == 'done'
            finally:
                proceed.set()
                for task in (release, handoff):
                    if task is not None and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                await server.lane_rulings.stop()
                await notify.stop()
    asyncio.run(run())


def test_handoff_before_approved_release_blocks_old_generation(tmp_path):
    async def run():
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            source = await store.fetch_session(HOST, 'source')
            successor = await opened('successor')
            notify = Notify(str(tmp_path / 'notifications.db'), sessions=sessions, notice_store=store,
                            lane_rulings=server.lane_rulings)
            await notify.start()
            server.notify = notify
            ctl.consent_notify = notify
            try:
                frozen = {'envelope': {'schema_version': 1, 'question_id': 'qa-retired', 'title': 'Probe',
                          'body': 'Proceed?', 'dedup_key': 'qa-retired', 'producer_stream_id': SOURCE,
                          'producer_session_generation': source['session_generation'],
                          'response_mode': 'free_text', 'options': []}, 'actions': [], 'severity': 'info'}
                row = await server.lane_rulings._create(action='question', key='qa-retired', intent=frozen,
                    requester=SOURCE, requester_generation=source['session_generation'],
                    binding=await server.lane_rulings.binding(), target=SOURCE, target_generation='', covered=True)
                rid = row['ruling_request_id']
                await store.submit(lambda c: (c.execute("UPDATE v2_assistant_lane_rulings SET state='approved',ruling='approve' WHERE ruling_request_id=?", (rid,)), c.commit()))
                assert (await ctl._finish_handoff({'handoff_from_stream_id': SOURCE}, successor['stream_id']))['state'] == 'complete'
                await server.lane_rulings._release(row)
                result = await server.lane_rulings._fetch(rid)
                assert result['state'] == 'release_blocked'
                assert json.loads(result['outcome_json'])['error'] == 'requester_generation_changed'
                assert await notify._db.call('get_agent_question', 'qa-retired') is None
                count = await store.submit(lambda c: c.execute("SELECT count(*) FROM v2_outbound_notices WHERE notice_id=?", ('assistant-lane-ruling-result:' + rid,)).fetchone()[0])
                assert count == 1
            finally:
                await server.lane_rulings.stop()
                await notify.stop()
    asyncio.run(run())


@pytest.mark.parametrize("value_kind", ["disable", "rebind"])
def test_concurrent_authority_change_and_fresh_question_do_not_deadlock(tmp_path, value_kind):
    from assistant_composite import AssistantComposite, AssistantCompositeConfig

    async def exercise(value_kind):
        async with environment() as (store, sessions, ctl, server, tmux, opened):
            source = await store.fetch_session(HOST, 'source')
            advisor = await opened('advisor', pane_status='pane_alive')
            replacement = await opened('replacement', pane_status='pane_alive')
            config = AssistantCompositeConfig.from_env({
                'PENTACLE_ASSISTANT_COMPOSITE_ENABLED': '1',
                'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID': HOST + ':assistant',
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID': SOURCE,
                'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION': source['session_generation'],
                'PENTACLE_ASSISTANT_AUTHORITY_STREAM_ID': advisor['stream_id'],
            })
            composite = AssistantComposite(store, config=config)
            server.assistant_composite = composite
            notify = Notify(str(tmp_path / f'{value_kind}.db'), sessions=sessions, notice_store=store,
                            lane_rulings=server.lane_rulings, assistant_binding=composite.binding,
                            assistant_stream_id=config.stream_id)
            server.notify = notify
            await notify.start()
            paused, proceed, ask_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            tasks = []
            try:
                frozen = {'envelope': {'schema_version': 1, 'question_id': 'old-question', 'title': 'Probe',
                    'body': 'Proceed?', 'dedup_key': 'old-question', 'producer_stream_id': SOURCE,
                    'producer_session_generation': source['session_generation'], 'response_mode': 'free_text',
                    'options': []}, 'actions': [], 'severity': 'info'}
                rulings = server.lane_rulings
                row = await rulings._create(action='question', key='old-question', intent=frozen,
                    requester=SOURCE, requester_generation=source['session_generation'],
                    binding=await rulings.binding(), target=SOURCE, target_generation='', covered=True)
                original_binding = rulings.binding
                configure_task = None
                async def paused_binding():
                    result = await original_binding()
                    if asyncio.current_task() is configure_task:
                        paused.set()
                        await proceed.wait()
                    return result
                rulings.binding = paused_binding
                target = 'disabled' if value_kind == 'disable' else replacement['stream_id']
                configure_task = asyncio.create_task(rulings.configure(target, actor_stream_id=SOURCE,
                                                     actor_generation=source['session_generation']))
                tasks.append(configure_task)
                await asyncio.wait_for(paused.wait(), 2)
                original_create = rulings._create
                async def observed_create(**kwargs):
                    ask_entered.set()
                    return await original_create(**kwargs)
                rulings._create = observed_create
                payload = {'type': 'prompt.ask', 'request_id': 'new-ask', 'from_stream_id': SOURCE,
                    '_auth_context': {'token_verified': True, 'stream_id': SOURCE, 'session_generation': source['session_generation']},
                    'envelope': {**frozen['envelope'], 'question_id': 'new-question', 'dedup_key': 'new-question'}, 'actions': []}
                ask = asyncio.create_task(notify.prompt(payload))
                tasks.append(ask)
                await asyncio.wait_for(ask_entered.wait(), 2)
                proceed.set()
                _, answer = await asyncio.wait_for(asyncio.gather(*tasks), 3)
                old = await rulings._fetch(row['ruling_request_id'])
                if value_kind == 'disable':
                    assert old['state'] == 'done' and answer['type'] == 'prompt.ask.ok'
                else:
                    assert old['state'] == 'pending' and old['authority_stream_id'] == replacement['stream_id']
                    assert answer['type'] == 'prompt.ask.held'
            finally:
                proceed.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await server.lane_rulings.stop()
                await notify.stop()
                await composite.stop()
    asyncio.run(exercise(value_kind))
