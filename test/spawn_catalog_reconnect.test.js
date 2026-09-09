'use strict';

// public_behavior_spec (desktop half).
// The New Chat catalog load must fail visibly (error + Retry) instead of an
// infinite spinner, and a WS socket cycle must re-issue the catalog/roster so
// no stale "loading" survives a reconnect.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const app = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'app.js'), 'utf8');
const { nextChatStreamSessions } = require('../renderer/sidebar_filter');

function extractFunction(name) {
  const src = app.match(new RegExp(`function ${name}\\([^]*?\\n}`));
  assert.ok(src, `${name} function found in renderer/app.js`);
  return src[0];
}

// ── Requirement 2: visible error + Retry ────────────────────────────────────

function renderProfileError(newSessionError) {
  const dom = new JSDOM('<main><h1 id="new-session-title"></h1><p id="new-session-subtitle"></p><div id="profile"></div></main>');
  const retries = [];
  const statuses = [];
  const render = new Function(
    'document', 'esc', 'updateNewSessionStatus', 'loadSpawnCatalog',
    'newSessionSelection', 'newSessionCatalog', 'newSessionError',
    `${extractFunction('renderSpawnProfileOptions')}; return renderSpawnProfileOptions;`,
  )(
    dom.window.document,
    String,
    (msg, isError) => statuses.push({ msg, isError }),
    () => retries.push(true),
    { provider: 'codex', model: 'gpt-5.6-sol', effort: 'high' },
    null, // no catalog → the loading/error branch
    newSessionError,
  );
  render(dom.window.document.getElementById('profile'));
  return { dom, container: dom.window.document.getElementById('profile'), retries, statuses };
}

test('a failed catalog load renders a visible error + Retry, not an infinite loading spinner', () => {
  const { container, retries, statuses } = renderProfileError('spawn_catalog_get timed out after 15000ms');
  const retryBtn = container.querySelector('#spawn-catalog-retry');
  assert.ok(retryBtn, 'a Retry control is rendered on catalog failure');
  assert.match(container.innerHTML, /spawn_catalog_get timed out/);
  assert.doesNotMatch(container.innerHTML, /Loading available spawn profiles/);
  assert.ok(statuses.some((s) => s.isError), 'the status line reflects the error');
  retryBtn.dispatchEvent(new (container.ownerDocument.defaultView.Event)('click'));
  assert.deepEqual(retries, [true], 'Retry re-issues loadSpawnCatalog');
});

test('a pending (error-free) catalog load still shows the loading placeholder', () => {
  const { container, retries } = renderProfileError('');
  assert.equal(container.querySelector('#spawn-catalog-retry'), null, 'no Retry while merely loading');
  assert.match(container.innerHTML, /Loading available spawn profiles/);
  assert.deepEqual(retries, []);
});

// ── Requirement 3: reconnect re-issues the catalog, no stale loading ─────────

function makeResync({ overlayDisplay, step, catalog }) {
  const dom = new JSDOM(`<div id="new-session-overlay" style="display:${overlayDisplay}"></div>`);
  const calls = { load: 0, warm: 0 };
  const resync = new Function(
    'document', 'newSessionStep', 'newSessionCatalog', 'loadSpawnCatalog', 'warmSpawnCatalog',
    `${extractFunction('newSessionModalIsOpen')}; ${extractFunction('resyncSpawnCatalogAfterReconnect')}; return resyncSpawnCatalogAfterReconnect;`,
  )(
    dom.window.document,
    step,
    catalog,
    () => { calls.load += 1; },
    () => { calls.warm += 1; },
  );
  resync();
  return calls;
}

test('reconnect re-issues the catalog load when the dialog is parked on profile with no catalog', () => {
  const calls = makeResync({ overlayDisplay: 'flex', step: 'profile', catalog: null });
  assert.deepEqual(calls, { load: 1, warm: 0 });
});

test('reconnect only re-warms the cache when the dialog is closed', () => {
  const calls = makeResync({ overlayDisplay: 'none', step: 'profile', catalog: null });
  assert.deepEqual(calls, { load: 0, warm: 1 });
});

test('reconnect re-warms (not reloads) when the open dialog already has a catalog', () => {
  const calls = makeResync({ overlayDisplay: 'flex', step: 'profile', catalog: { profiles: {} } });
  assert.deepEqual(calls, { load: 0, warm: 1 });
});

test('the reconnect edge in applyChatStreamState calls resyncSpawnCatalogAfterReconnect', () => {
  const fn = extractFunction('applyChatStreamState');
  assert.match(fn, /!wasConnected/);
  assert.match(fn, /resyncSpawnCatalogAfterReconnect\(\)/);
});

// ── Requirement 4: roster refreshes from the reconnect snapshot ──────────────

test('a reconnect snapshot replaces the roster so chats spawned during the outage appear', () => {
  const previous = [{ session_name: 'a', stream_id: 'hosta:a' }];
  const snapshot = { sessions: [
    { session_name: 'a', stream_id: 'hosta:a' },
    { session_name: 'b', stream_id: 'hosta:b' },
  ] };
  const next = nextChatStreamSessions(previous, snapshot);
  assert.deepEqual(next.map((s) => s.stream_id), ['hosta:a', 'hosta:b']);
});

test('a status-only reconnect frame (no sessions) preserves the last roster until the snapshot lands', () => {
  const previous = [{ session_name: 'a', stream_id: 'hosta:a' }];
  const next = nextChatStreamSessions(previous, { connected: true });
  assert.equal(next, previous);
});

test('applyChatStreamState feeds the roster from nextChatStreamSessions every frame', () => {
  const fn = extractFunction('applyChatStreamState');
  assert.match(fn, /state\.chatStream\.sessions = nextChatStreamSessions\(/);
  assert.match(fn, /scheduleSidebarRerender\(\)/);
});

