import test from 'node:test';
import assert from 'node:assert/strict';
import fixture from './fixtures/image_echo_bridge_order.json';
import {
  applyPentacleEvent, applySnapshotWithOptimisticReconciliation, initialPentacleStreamState,
  markOptimisticDispatchedByRequestId, selectSessionDetail, sendOptimisticMessage,
  type PentacleEvent, type PentacleStreamState,
} from '../src/index.ts';

const stream = fixture.event.stream_id;
const at = Date.parse(fixture.created_at);
const native = fixture.event as unknown as PentacleEvent;
const empty: PentacleStreamState = { ...initialPentacleStreamState, connected: true, sessions: [{
  stream_id: stream, host: 'fixture', provider: 'claude', session_name: 'image-echo',
  last_event_at: fixture.event.timestamp, last_text: '', last_kind: '', draft: '',
  online: true, pending: false, working: false,
}] };
function queued(state = empty, id = 'image-send', text = fixture.caption, created = at) {
  return sendOptimisticMessage(state, { streamId: stream, text, optimisticId: id,
    requestId: `request-${id}`, createdAt: created, windowStartedAt: created,
    attachments: [fixture.attachment] });
}
const rows = (state: PentacleStreamState) => selectSessionDetail(state, stream)!.transcriptItems.filter(row => row.isUser);

for (const reverse of [false, true]) {
  test(`queued image echo within window reconciles before dispatch, reverse Unicode=${reverse}`, () => {
    const caption = reverse ? fixture.caption.normalize('NFC') : fixture.caption;
    const event = reverse ? { ...native, text: native.text.normalize('NFD') } : native;
    const state = applyPentacleEvent(queued(empty, 'image-send', caption), event);
    assert.equal(rows(state).length, 1);
    assert.equal(rows(state)[0].text, caption);
    assert.equal(rows(state)[0].sendState, undefined);
    assert.equal(rows(state)[0].attachments?.[0].key, fixture.attachment.key);
  });
}

for (const timestamp of ['', 'invalid', new Date(at + 60_001).toISOString(), new Date(at - 60_001).toISOString()]) {
  test(`queued wrapper does not gain an invalid/outside-window match: ${timestamp}`, () => {
    const state = applyPentacleEvent(queued(), { ...native, timestamp });
    assert.equal(rows(state).length, 2);
    assert.equal(state.optimisticSends?.['image-send'].status, 'queued');
  });
}

test('queued captionless keyed image reconciles in snapshot without a dispatch result', () => {
  const event = { ...native, text: `Image at /fixture/${fixture.attachment.key}.png` };
  const state = applySnapshotWithOptimisticReconciliation(queued(empty, 'image-send', ''), { events: [event], sessions: empty.sessions });
  assert.equal(rows(state).length, 1);
  assert.equal(rows(state)[0].sendState, undefined);
});

for (const mismatch of ['key', 'caption', 'case', 'compatibility', 'optimistic-id', 'message-id', 'client-origin'] as const) {
  test(`queued early wrapper preserves the ${mismatch} mismatch guard`, () => {
    let state = queued();
    let event = { ...native };
    if (mismatch === 'key') event.text = event.text.replaceAll(fixture.attachment.key, '0'.repeat(64));
    if (mismatch === 'caption') event.text = event.text.replace('IMAGE_ECHO_FIXTURE', 'OTHER_MESSAGE');
    if (mismatch === 'case') event.text = event.text.replace('Owned Unicode', 'owned Unicode');
    if (mismatch === 'compatibility') {
      state = queued(empty, 'image-send', 'value fi literal');
      event.text = `Look at the image file at /fixture/${fixture.attachment.key}.png, then respond to the user's message: value ﬁ literal`;
    }
    if (mismatch === 'optimistic-id') event.optimistic_id = 'another-send';
    if (mismatch === 'message-id') {
      const send = state.optimisticSends!['image-send'];
      state = { ...state, optimisticSends: { 'image-send': { ...send, message_id: 'owned-message' } } };
      event.message_id = 'another-message';
    }
    if (mismatch === 'client-origin') event.client_origin = true;
    const next = applyPentacleEvent(state, event);
    // Client-origin rows are filtered by the transcript; they must still leave
    // the real optimistic send queued rather than prove server reconciliation.
    assert.equal(rows(next).length, mismatch === 'client-origin' ? 1 : 2);
    assert.equal(next.optimisticSends?.['image-send'].status, 'queued');
  });
}

test('two queued same-key images with a superset caption stay ambiguous', () => {
  const state = queued(queued(empty, 'short', 'caption'), 'long', 'caption extended', at + 1);
  const event = { ...native, text: `Look at the image file at /fixture/${fixture.attachment.key}.png, then respond to the user's message: caption extended` };
  const next = applyPentacleEvent(state, event);
  assert.equal(rows(next).length, 3);
  assert.equal(next.optimisticSends?.short.status, 'queued');
  assert.equal(next.optimisticSends?.long.status, 'queued');
});

test('both matching images inside the window remain ambiguous', () => {
  const state = queued(queued(empty, 'one'), 'two', fixture.caption, at + 1);
  const next = applyPentacleEvent(state, native);
  assert.equal(rows(next).length, 3);
  assert.equal(next.optimisticSends?.one.status, 'queued');
  assert.equal(next.optimisticSends?.two.status, 'queued');
});

test('window precedence selects the sole in-window dispatched image, preserving the older send', () => {
  let state = queued(queued(empty, 'old', fixture.caption, at - 120_000), 'current');
  for (const id of ['old', 'current']) state = markOptimisticDispatchedByRequestId(state, `request-${id}`, at);
  const next = applyPentacleEvent(state, native);
  assert.equal(rows(next).length, 2);
  assert.equal(next.optimisticSends?.old.status, 'dispatched');
  assert.equal(next.optimisticSends?.current, undefined);
  assert.equal(rows(next).find(row => row.optimisticId === 'current')?.sendState, undefined);
});

test('synthetic structured echo preserves a literal pasted_content caption comparison', () => {
  // Additional guard vector, not claimed as captured live input. Comparison
  // text must not be parsed as a newly authenticated provider wrapper.
  const caption = '<pasted_content id="abc123">\nliteral caption\n</pasted_content id="abc123">';
  const event = { ...native, text: `Look at the image file at /fixture/${fixture.attachment.key}.png, then respond to the user's message: ${caption}`,
    raw: { source: 'structured', transport: 'claude-jsonl' } } as PentacleEvent;
  const next = applyPentacleEvent(queued(empty, 'literal', caption), event);
  assert.equal(rows(next).length, 1);
  assert.equal(rows(next)[0].text, caption);
  assert.equal(rows(next)[0].sendState, undefined);
});
