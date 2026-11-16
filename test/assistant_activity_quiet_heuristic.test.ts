import test from 'node:test';
import assert from 'node:assert/strict';
import { initialPentacleStreamState } from 'pentacle-chat-core';
import { ChatStoreController, STALE_TURN_GRACE_MS } from '../renderer/src/chat_store_controller';

const COMPOSITE = 'fixture-host-chat:assistant';
const ORDINARY = 'fixture-host-agent:agent';

function controllerFor(streamId: string, sessionKind: string): ChatStoreController {
  const state = {
    ...initialPentacleStreamState,
    sessions: [{ stream_id: streamId, session_kind: sessionKind }],
    workingByStream: {
      [streamId]: {
        phase: 'working',
        optimisticId: `${streamId}-turn`,
        sentAt: 1,
        firstServerEventAt: 2,
      },
    },
  } as any;
  return new ChatStoreController(state);
}

function event(streamId: string) {
  return {
    daemon_seq: 1,
    stream_id: streamId,
    host: streamId.split(':')[0],
    session_name: streamId.split(':')[1],
    provider: 'composite',
    kind: 'ASSIST_TEXT',
    timestamp: '2026-09-20T00:00:00.000Z',
    text: 'fixture activity',
  };
}

test('quiet composite activity never arms or settles the underlying working turn', () => {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const callbacks: Array<() => void> = [];
  const delays: number[] = [];
  globalThis.setTimeout = ((callback: TimerHandler, delay?: number) => {
    callbacks.push(callback as () => void);
    delays.push(Number(delay));
    return callbacks.length as unknown as ReturnType<typeof setTimeout>;
  }) as typeof setTimeout;
  globalThis.clearTimeout = (() => undefined) as typeof clearTimeout;

  const composite = controllerFor(COMPOSITE, 'assistant_composite');
  const ordinary = controllerFor(ORDINARY, 'agent');
  try {
    composite.setClockForTests(() => 13_000);
    assert.equal(composite.getTurnPhase(COMPOSITE), 'idle', 'idle is composite send eligibility');
    composite.applyFrame({ type: 'chat.event', event: event(COMPOSITE) });
    assert.equal(composite.getTurnPhase(COMPOSITE), 'idle', 'quiet activity cannot change eligibility');
    const compositeTurn = composite.getTurnState(COMPOSITE);
    assert.equal(compositeTurn?.phase, 'working');
    assert.equal(compositeTurn?.optimisticId, `${COMPOSITE}-turn`);
    assert.equal(compositeTurn?.endedAt, undefined);
    assert.equal(callbacks.length, 0, 'composite stream must not arm stale settle');

    ordinary.setClockForTests(() => 13_000);
    ordinary.applyFrame({ type: 'chat.event', event: event(ORDINARY) });
    assert.equal(ordinary.getTurnPhase(ORDINARY), 'working');
    assert.deepEqual(delays, [STALE_TURN_GRACE_MS]);
    callbacks[0](); // the captured callback represents expiry after the grace window
    assert.equal(ordinary.getTurnPhase(ORDINARY), 'idle');
    assert.equal(ordinary.getTurnState(ORDINARY), undefined);
  } finally {
    composite.dispose();
    ordinary.dispose();
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});
