// lane_update cards in Bart's chat (spec_pentacle__first_class_work_lanes_2026_10 D6/D8, V12 client side):
// publish_kind 'lane_update' renders a typed card from raw.lane_update; every other
// event, and old clients, keep the summary prose.
import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import fixture from '../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json';
import { ChatStoreController } from '../renderer/src/chat_store_controller';
import { renderTranscriptTimelineHtml, renderTranscriptItemHtml } from '../renderer/src/shared_transcript_view';

const CHROME = { header: '#102a4a', accent: '#4da3ff', surface: '#0c1827', border: '#2f6ca5', title: 'bart' };
const STREAM = 'bart:assistant';

function load(extra: unknown[] = []) {
  const store = new ChatStoreController();
  store.applyFrame({ type: 'snapshot', connected: true, events: [], sessions: [{
    stream_id: STREAM, host: 'bart', provider: 'composite', session_name: 'assistant', session_kind: 'assistant_composite',
    visibility: 'visible', last_event_at: '2026-10-07T19:00:00.000Z',
  }] } as never);
  for (const wire of [...fixture.lane_update_events, ...extra] as any[]) store.applyFrame(wire);
  return store;
}

function render(store: ChatStoreController) {
  const detail = store.selectSessionDetail(STREAM, { visibleCount: 50, includeDraft: false })!;
  const dom = new JSDOM(`<div id="root">${renderTranscriptTimelineHtml(detail, CHROME, {})}</div>`);
  return dom.window.document;
}

test('every lane_update kind renders one typed card with its summary', () => {
  const doc = render(load());
  const cards = [...doc.querySelectorAll<HTMLElement>('.slot-chat-lane-update')];
  assert.equal(cards.length, 6);
  assert.deepEqual(cards.map((c) => c.dataset.updateKind).sort(),
    ['lane_blocked', 'lane_completed', 'lane_started', 'lane_unblocked', 'major_decision', 'milestone']);
  const blocked = doc.querySelector<HTMLElement>('.slot-chat-lane-update[data-update-kind="lane_blocked"]')!;
  assert.equal(blocked.dataset.laneId, 'wl-blocked-0001');
  assert.match(blocked.querySelector('.lane-update-kind')!.textContent!, /Blocked/);
  assert.match(blocked.querySelector('.lane-update-summary')!.textContent!, /Waiting for operator deploy window/);
  assert.match(blocked.querySelector('.lane-update-title')!.textContent!, /Sample daemon deploy/);
});

test('a card is not also rendered as an assistant prose bubble', () => {
  const doc = render(load());
  assert.equal(doc.querySelectorAll('.slot-chat-assistant-card').length, 0);
});

test('replayed history of the same publication does not duplicate a card', () => {
  const doc = render(load([fixture.lane_update_events[1], fixture.lane_update_events[1]]));
  assert.equal(doc.querySelectorAll('.slot-chat-lane-update').length, 6);
});

test('lane_update text is escaped; ordinary assistant publications are unchanged', () => {
  const evil = JSON.parse(JSON.stringify(fixture.lane_update_events[0]));
  evil.event.message_id = 'publication:lane-update:wl-x:evil';
  evil.event.daemon_seq = 2001;
  evil.event.raw.lane_update.update_id = 'lane-update:wl-x:evil';
  evil.event.raw.lane_update.summary = '<img src=x onerror=alert(1)>';
  evil.event.raw.lane_update.title = '<script>x()</script>';
  const prose = { type: 'chat.event', event: { ...fixture.lane_update_events[0].event, message_id: 'publication:plain-1',
    publish_kind: 'prose', text: 'Plain published prose', raw: { publish_kind: 'prose' }, daemon_seq: 2000 } };
  const store = load([evil, prose]);
  const detail = store.selectSessionDetail(STREAM, { visibleCount: 50, includeDraft: false })!;
  const html = renderTranscriptTimelineHtml(detail, CHROME, {});
  assert.doesNotMatch(html, /<img src=x|<script>/);
  const doc = new JSDOM(`<div>${html}</div>`).window.document;
  assert.equal(doc.querySelectorAll('.slot-chat-lane-update').length, 7);
  assert.match(doc.body.textContent!, /Plain published prose/);
});

test('item without laneUpdate keeps the legacy summary prose', () => {
  const store = load();
  const detail = store.selectSessionDetail(STREAM, { visibleCount: 50, includeDraft: false })!;
  const item = detail.transcriptItems.find((i) => i.laneUpdate)!;
  const { laneUpdate: _drop, ...legacy } = item as any;
  const html = renderTranscriptItemHtml(legacy, CHROME, {});
  assert.match(html, /slot-chat-assistant-card/);
  assert.match(html, new RegExp(item.text.slice(0, 12)));
});
