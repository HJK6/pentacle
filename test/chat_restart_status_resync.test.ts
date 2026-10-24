import test from 'node:test';
import assert from 'node:assert/strict';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
const id = 'fixture:owned-status';
const generation = 'fixture-generation-1';
function session(over = {}) {
  return { stream_id: id, host: 'fixture', session_name: 'owned-status', provider: 'codex',
    session_generation: generation, working: true, working_label: 'Working 106s',
    online: true, pane_status: 'pane_alive', capture_liveness: 'idle', ...over };
}
function active() {
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', sessions: [session()], events: [] });
  store.setState({ ...store.getState(), workingByStream: { [id]: { phase: 'working' } } });
  assert.equal(store.getTurnPhase(id), 'working');
  return store;
}
const unknown = () => session({ working: false, working_label: undefined,
  capture_liveness: 'transport_unknown', online: false, host_status_reason: 'pending_probe' });
for (const type of ['snapshot', 'session.inventory']) {
  test(`${type} preserves a proven same-generation active turn through unobserved bootstrap false`, () => {
    const store = active();
    store.applyFrame({ type, sessions: [unknown()], events: [] });
    assert.equal(store.getTurnPhase(id), 'working');
    assert.equal(store.getState().sessions[0].working, true);
    assert.equal(store.getState().sessions[0].capture_liveness, 'transport_unknown');
    store.applyFrame({ type: 'working.state', stream_id: id, tokens_phase: 'down', elapsed_ms: 128000 });
    assert.equal(store.getTurnPhase(id), 'working');
  });
}
test('a native working root survives unknown resync before its summary catches up', () => {
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', sessions: [unknown()], events: [] });
  store.applyFrame({ type: 'chat.event', event: { stream_id: id, host: 'fixture', session_name: 'owned-status', provider: 'codex',
    daemon_seq: 100, kind: 'USER', text: 'Held tool turn', timestamp: '2026-10-24T07:00:10Z', raw: { working: true } } });
  assert.equal(store.getTurnPhase(id), 'working');
  assert.equal(store.getState().sessions[0].working, false);
  store.applyFrame({ type: 'snapshot', sessions: [unknown()], events: [] });
  assert.equal(store.getTurnPhase(id), 'working');
  assert.equal(store.getState().sessions[0].working, true);
});
test('a checked idle capture settles a preserved turn', () => {
  const store = active();
  store.applyFrame({ type: 'snapshot', sessions: [unknown()], events: [] });
  store.applyFrame({ type: 'session.inventory', sessions: [session({ working: false, capture_liveness: 'idle' })] });
  assert.equal(store.getTurnPhase(id), 'idle');
  assert.equal(store.getState().sessions[0].working, false);
});
test('a terminal event during recovery stays authoritative on the next unknown snapshot', () => {
  const store = active();
  store.applyFrame({ type: 'snapshot', sessions: [unknown()], events: [] });
  store.applyFrame({ type: 'chat.event', event: { stream_id: id, host: 'fixture', session_name: 'owned-status', provider: 'codex',
    daemon_seq: 2, kind: 'SYSTEM', text: 'Worked for 1s', timestamp: '2026-10-24T07:00:11Z', raw: { subtype: 'turn-summary' } } });
  store.applyFrame({ type: 'snapshot', sessions: [unknown()], events: [] });
  assert.equal(store.getTurnPhase(id), 'idle');
  assert.equal(store.getState().sessions[0].working, false);
});
for (const type of ['snapshot', 'session.inventory']) {
  test(`${type} discards predecessor active phase on a new generation`, () => {
    const store = active();
    store.applyFrame({ type, sessions: [session({ session_generation: 'fixture-generation-2', working: false,
      capture_liveness: 'transport_unknown' })], events: [] });
    assert.equal(store.getTurnPhase(id), 'idle');
    assert.equal(store.getState().sessions[0].working, false);
  });
}
test('a working successor summary does not inherit its predecessor turn phase', () => {
  const store = active();
  store.applyFrame({ type: 'snapshot', sessions: [session({ session_generation: 'fixture-generation-2' })], events: [] });
  assert.equal(store.getTurnPhase(id), 'idle');
  assert.equal(store.getState().sessions[0].working, true);
});
test('removed streams and explicitly dead panes cannot inherit active state', () => {
  const store = active();
  store.applyFrame({ type: 'snapshot', sessions: [unknown()], events: [] });
  store.applyFrame({ type: 'session.inventory', sessions: [] });
  assert.equal(store.getTurnPhase(id), 'idle');
  const dead = active();
  dead.applyFrame({ type: 'snapshot', sessions: [session({ working: false, capture_liveness: 'transport_unknown', pane_status: 'pane_dead' })], events: [] });
  assert.equal(dead.getTurnPhase(id), 'idle');
});
test('legacy working:false without explicit unobserved capture still settles', () => {
  const store = active();
  store.applyFrame({ type: 'snapshot', sessions: [session({ working: false, capture_liveness: undefined })], events: [] });
  assert.equal(store.getTurnPhase(id), 'idle');
});
