"""Replay a disposable seat's real provider turn into an isolated mirror sink."""
import argparse
import asyncio
import json
from pathlib import Path
import sys


async def verify(args):
    sys.path.insert(0, str(Path(args.repo)/'services/chat-stream-v2'))
    sys.path.insert(0, str(Path(args.repo)/'services'))
    from codex_rollout_norm import normalize_codex_rollout_record
    from assistant_composite import AssistantComposite, AssistantCompositeConfig
    from store import Store
    delivery = json.loads(Path(args.delivery).read_text())
    match = delivery['turn']['conversationId']
    records = [json.loads(line) for line in Path(args.transcript).read_text().splitlines()]
    session = next(record['payload']['id'] for record in records if record.get('type') == 'session_meta')
    selected = []
    started = False
    for record in records:
        events = normalize_codex_rollout_record(record,host='fixture-root',session_name='voice-seat',session_id=session)
        for event in events:
            if event['kind'] == 'USER' and match in event['text']:
                started = True
            if not started:
                continue
            selected.append(event)
            if event['kind'] == 'ASSIST_TEXT' and event['raw'].get('phase') == 'final_answer':
                started = False
                break
        if selected and not started:
            break
    if not selected or selected[-1]['raw'].get('phase') != 'final_answer':
        raise AssertionError('The actual disposable provider turn has not reached final')
    store = Store(':memory:')
    store.start()
    composite = None
    try:
        root = await store.open_session('fixture-root','voice-seat',provider='codex',pane_pid='4242')
        config = AssistantCompositeConfig.from_env({
            'PENTACLE_ASSISTANT_COMPOSITE_ENABLED':'1',
            'PENTACLE_ASSISTANT_COMPOSITE_STREAM_ID':'fixture-chat:voice-mirror',
            'PENTACLE_ASSISTANT_DIRECT_PRIMARY_STREAM_ID':'fixture-root:voice-seat',
            'PENTACLE_ASSISTANT_DIRECT_PRIMARY_GENERATION':root['session_generation'],
        })
        composite = AssistantComposite(store,config=config)
        await composite.ensure_projection()
        exported = []
        sources = {}
        for ordinal,event in enumerate(selected,1):
            lifecycle = await store.fetch_open_session_lifecycle('fixture-root:voice-seat',pane_pid='4242')
            inserted = await store.append_session_events_lifecycle_cas([{'stream_id':'fixture-root:voice-seat','event':event,'identity':f'voice-event-{ordinal}','lifecycle':lifecycle}],limit=500)
            sources[inserted[0]] = event
            exported.append({**event,'stream_id':delivery['turn']['streamId'],'daemon_seq':ordinal})
        tail = await store.fetch_session_event_tail('fixture-chat:voice-mirror',limit=500)
        replies = [event for event in tail if event['kind'] == 'ASSIST_TEXT']
        finals = [event for event in replies if sources.get(event.get('raw',{}).get('mirrored_from',{}).get('event_id'),{}).get('raw',{}).get('phase') == 'final_answer']
        if len(finals) != 1:
            raise AssertionError(f'Expected one mirrored final; got {len(finals)}')
        if any('[pentacle-input' in event['text'] for event in replies):
            raise AssertionError('Machine header leaked into the chat reply')
        result = {'conversation_id':match,'mirror_replies':len(finals),'commentary_messages':len(replies)-len(finals),'reply':finals[0]['text'],'events':exported,
                  'mirror_sink':'isolated in-memory fixture-chat:voice-mirror','live_binding_used':False}
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'conversation_id':match,'mirror_replies':len(finals),'commentary_messages':len(replies)-len(finals),'provider_events':len(selected)}))
    finally:
        if composite:
            await composite.stop()
        store.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('repo','delivery','transcript','output'):
        parser.add_argument('--'+name,required=True)
    asyncio.run(verify(parser.parse_args()))


if __name__ == '__main__':
    main()
