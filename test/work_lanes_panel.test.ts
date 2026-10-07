// Web work-lanes panel (spec_pentacle__first_class_work_lanes_2026_10 D8, M4):
// header stats line and "Lanes (N)" sidebar section rendered from the daemon projection fixture.
import test from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import fixture from '../pentacle-chat-core/tests/fixtures/work-lanes-inventory.json';
import {
  applyWorkLanesFrame, emptyWorkLanesInventory, workLaneEtaLabel, workLaneTapTarget,
} from 'pentacle-chat-core';
import { workLanesStatsText, renderWorkLanesPanelHtml } from '../renderer/work_lanes_panel.js';

const NOW = Date.parse('2026-10-07T19:01:00Z');
const inventory = applyWorkLanesFrame(emptyWorkLanesInventory(), fixture.inventory_frame);
const helpers = { tapTarget: workLaneTapTarget, etaLabel: workLaneEtaLabel, nowMs: NOW };

function mount(html: string) {
  const dom = new JSDOM(`<div id="root">${html}</div>`);
  return dom.window.document;
}

test('stats line leads with the open-lane count and keeps the session counts', () => {
  assert.equal(workLanesStatsText(inventory, { sessions: 6, needsAnswer: 1, working: 2, search: '', sourceFiltered: 6 }),
    '4 lanes | 6 sessions | 1 need answer | 2 working');
  assert.equal(workLanesStatsText(inventory, { sessions: 2, needsAnswer: 0, working: 0, search: 'x', sourceFiltered: 6 }),
    '4 lanes | 2 of 6 sessions');
});

test('stats line is the legacy session line until a lane inventory has been received', () => {
  assert.equal(workLanesStatsText(null, { sessions: 6, needsAnswer: 1, working: 2, search: '', sourceFiltered: 6 }),
    '6 sessions | 1 need answer | 2 working');
  assert.equal(workLanesStatsText(emptyWorkLanesInventory(), { sessions: 3, needsAnswer: 0, working: 0, search: '', sourceFiltered: 3 }),
    '3 sessions | 0 need answer | 0 working');
});

test('zero open lanes from a received inventory still shows 0 lanes', () => {
  const empty = applyWorkLanesFrame(emptyWorkLanesInventory(), { type: 'work_lanes.inventory', lanes: [],
    counts: { open: 0, active: 0, paused: 0, blocked: 0 }, truncated: false, generated_at: '2026-10-07T19:01:00.000Z' });
  assert.equal(workLanesStatsText(empty, { sessions: 3, needsAnswer: 0, working: 0, search: '', sourceFiltered: 3 }),
    '0 lanes | 3 sessions | 0 need answer | 0 working');
});

test('panel lists lanes in server order under "Lanes (N)" with state badges', () => {
  const doc = mount(renderWorkLanesPanelHtml(inventory, helpers));
  assert.match(doc.querySelector('.sidebar-group-label')!.textContent!, /^Lanes \(4\)$/);
  const rows = [...doc.querySelectorAll<HTMLElement>('.lane-row')];
  assert.deepEqual(rows.map((r) => r.dataset.laneId), fixture.expected.order);
  assert.deepEqual(rows.map((r) => r.querySelector('.lane-state')!.textContent),
    ['BLOCKED', 'ACTIVE', 'PAUSED', 'PAUSED']);
  assert.deepEqual(rows.map((r) => r.dataset.laneState), ['blocked', 'active', 'paused', 'paused']);
  assert.equal(doc.querySelector('[data-lane-state="done"]'), null);
});

test('blocked lane shows its blocker; stale ETA marker and owner glyph are present', () => {
  const doc = mount(renderWorkLanesPanelHtml(inventory, helpers));
  const row = (id: string) => doc.querySelector<HTMLElement>(`.lane-row[data-lane-id="${id}"]`)!;
  assert.match(row('wl-blocked-0001').querySelector('.lane-blocker')!.textContent!, /Waiting for operator deploy window/);
  assert.equal(row('wl-active-0002').querySelector('.lane-blocker'), null);
  for (const id of fixture.expected.order) {
    const stale = row(id).querySelector('.lane-eta.is-stale');
    assert.equal(!!stale, fixture.expected.eta_stale[id as keyof typeof fixture.expected.eta_stale], id);
    if (stale) assert.equal(stale.textContent, 'ETA stale');
  }
  assert.match(row('wl-active-0002').querySelector('.lane-eta')!.textContent!, /^~\d+(m|h)$/);
  assert.equal(row('wl-paused-0003').dataset.ownerKind, 'operator');
  assert.ok(row('wl-paused-0003').querySelector('.lane-owner.is-operator'));
  assert.ok(row('wl-blocked-0001').querySelector('.lane-owner.is-fd'));
});

test('presence dot reflects the lead: working, idle, offline, and none when the lead is gone', () => {
  const doc = mount(renderWorkLanesPanelHtml(inventory, helpers));
  const dot = (id: string) => doc.querySelector(`.lane-row[data-lane-id="${id}"] .lane-presence`)!;
  assert.ok(dot('wl-active-0002').classList.contains('is-working'));
  assert.ok(dot('wl-blocked-0001').classList.contains('is-idle'));
  assert.ok(dot('wl-paused-0003').classList.contains('is-offline'));
  assert.ok(dot('wl-paused-0004').classList.contains('is-lost'));
});

test('each row carries the exact tap target for its visible_chat.available', () => {
  const doc = mount(renderWorkLanesPanelHtml(inventory, helpers));
  for (const [id, tap] of Object.entries(fixture.expected.tap)) {
    const el = doc.querySelector<HTMLElement>(`.lane-row[data-lane-id="${id}"]`)!;
    assert.equal(el.dataset.tapAction, (tap as any).action, id);
    assert.equal(el.dataset.streamId ?? '', (tap as any).stream_id ?? '', id);
    assert.equal(el.dataset.generation ?? '', (tap as any).generation ?? '', id);
  }
  const unavailable = doc.querySelector('.lane-row[data-lane-id="wl-paused-0004"]')!;
  assert.match(unavailable.textContent!, /Chat unavailable/);
  assert.match(doc.querySelector('.lane-row[data-lane-id="wl-paused-0003"]')!.textContent!, /History/);
  assert.ok(doc.querySelector('.lane-row[role="button"][tabindex="0"]'));
});

test('lead status card renders through the supplied card renderer', () => {
  const seen: unknown[] = [];
  const doc = mount(renderWorkLanesPanelHtml(inventory, { ...helpers, renderLeadCard: (session: unknown) => {
    seen.push(session); return '<div class="slot-status-card">card</div>';
  } }));
  assert.equal(doc.querySelectorAll('.lane-row .slot-status-card').length, 4);
  assert.equal((seen[0] as any).status_card.goal, 'Deploy daemon');
  assert.equal(doc.querySelector('.lane-row[data-lane-id="wl-blocked-0001"] .lane-step')!.textContent, 'Await window');
});

test('titles, summaries and blockers are HTML-escaped', () => {
  const evil = { ...fixture.inventory_frame, lanes: [{ ...fixture.inventory_frame.lanes[0],
    title: '<img src=x onerror=alert(1)>', blocker: '<script>x()</script>' }] };
  const html = renderWorkLanesPanelHtml(applyWorkLanesFrame(emptyWorkLanesInventory(), evil), helpers);
  assert.doesNotMatch(html, /<img|<script/);
  assert.match(html, /&lt;img/);
});

test('empty inventory renders nothing; truncated inventory says so', () => {
  assert.equal(renderWorkLanesPanelHtml(null, helpers), '');
  assert.equal(renderWorkLanesPanelHtml(emptyWorkLanesInventory(), helpers), '');
  const truncated = applyWorkLanesFrame(emptyWorkLanesInventory(), { ...fixture.inventory_frame, truncated: true,
    counts: { open: 70, active: 1, paused: 2, blocked: 1 } });
  const doc = mount(renderWorkLanesPanelHtml(truncated, helpers));
  assert.match(doc.querySelector('.sidebar-group-label')!.textContent!, /Lanes \(70\)/);
  assert.match(doc.querySelector('.lanes-truncated')!.textContent!, /Showing 4 of 70/);
});
