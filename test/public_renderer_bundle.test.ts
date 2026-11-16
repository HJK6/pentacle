import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import '../renderer/src/chat_core_entry';

test('shipped renderer entry applies daemon events and renders an actual assistant row', () => {
  const browser = globalThis as any;
  const stream = 'local:public-session';
  browser.PentacleChatStore.applyFrame({ type: 'snapshot', events: [], sessions: [{ stream_id: stream, host: 'local', provider: 'claude', session_name: 'public-session' }] });
  browser.PentacleChatStore.applyFrame({ type: 'chat.event', event: { daemon_seq: 1, stream_id: stream, host: 'local', provider: 'claude', session_name: 'public-session', kind: 'ASSIST_TEXT', timestamp: '2026-09-10T00:00:00Z', text: 'Rendered from the daemon', raw: { source: 'claude-jsonl' } } });
  const container = new JSDOM('<div></div>').window.document.querySelector('div')!;
  browser.PentacleChatView.renderStreamTranscript(stream, container, { store: browser.PentacleChatStore });
  assert.match(container.textContent!, /Rendered from the daemon/);
  assert.equal(browser.PentacleChatStore.getState().events.length, 1);
});
