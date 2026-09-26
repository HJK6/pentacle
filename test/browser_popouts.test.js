'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createBrowserPopouts, parseAssetContext, PROTOCOL } = require('../renderer/browser_popouts');

const origin = 'https://pentacle.example';
const chat = { stream_id: 'mock:one', host: 'mock', desktop_host: 'mock', session_name: 'one', title: 'One' };
const asset = { asset_id: 'report-1', stream_id: 'mock:one', host: 'mock', session_name: 'one',
  asset: { asset_id: 'report-1', title: 'Report', content_type: 'report',
    session_key: { host: 'mock', session_name: 'one', stream_id: 'mock:one' } } };

function fakeWindow({ opener = null } = {}) {
  const listeners = new Map();
  return {
    opener, closed: false, location: { origin }, sent: [], opened: [], focused: 0,
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
    emit(data, source, eventOrigin = origin) { listeners.get('message')?.({ data, source, origin: eventOrigin }); },
    postMessage(data, targetOrigin) { this.sent.push({ data, targetOrigin }); },
    focus() { this.focused++; },
    close() { this.closed = true; },
    open(url, name, features) { this.opened.push({ url, name, features }); return this.nextChild || null; },
  };
}

test('browser open is synchronous, reuses the matching window, and exposes no asset body in URL', async () => {
  const parent = fakeWindow();
  const child = fakeWindow({ opener: parent });
  parent.nextChild = child;
  const bridge = createBrowserPopouts({ win: parent, location: { origin, href: `${origin}/` } });
  const first = bridge.openAsset({ ...asset, asset: { ...asset.asset, body: 'SECRET REPORT BODY' } });
  assert.equal(parent.opened.length, 1, 'window.open ran before awaiting the result');
  assert.equal((await first).ok, true);
  assert.equal((await bridge.openAsset(asset)).reused, true);
  assert.equal(parent.opened.length, 1);
  assert.equal(child.focused, 1);
  const url = new URL(parent.opened[0].url);
  assert.equal(url.origin, origin);
  assert.equal(url.pathname, '/asset-popout.html');
  assert.equal(url.href.includes('SECRET REPORT BODY'), false);
  assert.equal(parseAssetContext(url.search).asset_id, 'report-1');
  bridge.destroy();
});

test('blocked popup keeps the original view and reports a useful fallback', async () => {
  const parent = fakeWindow();
  const messages = [];
  const bridge = createBrowserPopouts({ win: parent, location: { origin, href: `${origin}/` }, toast: text => messages.push(text) });
  assert.deepEqual(await bridge.openChat(chat), { ok: false, error: 'popup_blocked', fallback: 'original_view' });
  assert.match(messages[0], /remains open here/);
  assert.equal(bridge.size(), 0);
  bridge.destroy();
});

test('dock requires same origin, registered source, and exact chat identity before ack and close', async () => {
  const parent = fakeWindow();
  const child = fakeWindow({ opener: parent });
  parent.nextChild = child;
  const owner = createBrowserPopouts({ win: parent, location: { origin, href: `${origin}/` } });
  const opened = await owner.openChat(chat);
  const detached = createBrowserPopouts({ win: child, location: { origin, href: `${origin}/` }, chatPopoutContext: chat });
  let docks = 0;
  owner.onChatDock(payload => { docks++; assert.equal(payload.transfer_state.draft, 'kept'); return true; });
  const pending = detached.dockChat({ ...chat, transfer_state: { draft: 'kept' } });
  const request = parent.sent.at(-1).data;
  assert.equal(request.protocol, PROTOCOL);
  parent.emit(request, child, 'https://attacker.example');
  parent.emit(request, fakeWindow());
  parent.emit({ ...request, context: { ...request.context, stream_id: 'mock:other' } }, child);
  assert.equal(docks, 0);
  parent.emit(request, child);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(docks, 1);
  const ack = child.sent.at(-1).data;
  assert.equal(ack.ok, true);
  child.emit(ack, parent);
  assert.deepEqual(await pending, { ok: true, docked: true });
  assert.equal(child.closed, true);
  assert.equal(owner.size(), 0);
  assert.equal(opened.ok, true);
  detached.destroy(); owner.destroy();
});

test('ordinary close never invokes dock or backing lifecycle', async () => {
  const parent = fakeWindow();
  const child = fakeWindow({ opener: parent });
  parent.nextChild = child;
  const owner = createBrowserPopouts({ win: parent, location: { origin, href: `${origin}/` } });
  await owner.openChat(chat);
  let docks = 0;
  owner.onChatDock(() => { docks++; return true; });
  child.close();
  assert.equal(docks, 0);
  assert.equal((await owner.openChat(chat)).reused, false);
  owner.destroy();
});

test('direct assistant context includes source and generation in dock identity', async () => {
  const parent = fakeWindow();
  const child = fakeWindow({ opener: parent });
  parent.nextChild = child;
  const direct = { ...chat, assistant_source_stream_id: 'bart:assistant', assistant_generation: 'generation-1' };
  const owner = createBrowserPopouts({ win: parent, location: { origin, href: `${origin}/` } });
  await owner.openChat(direct);
  const detached = createBrowserPopouts({ win: child, location: { origin, href: `${origin}/` }, chatPopoutContext: direct });
  assert.deepEqual(await detached.dockChat({ ...direct, assistant_generation: 'generation-2' }),
    { ok: false, error: 'popout_identity_mismatch' });
  assert.deepEqual(await detached.dockChat({ ...direct, assistant_source_stream_id: '' }),
    { ok: false, error: 'popout_identity_mismatch' });
  assert.equal(parent.sent.length, 0);
  detached.destroy(); owner.destroy();
});

test('lost opener leaves a visible return path and keeps the asset window open', async () => {
  const parent = fakeWindow();
  const child = fakeWindow({ opener: parent });
  const nodes = [];
  child.document = {
    body: { appendChild(node) { nodes.push(node); } },
    getElementById(id) { return nodes.find(node => node.id === id) || null; },
    createElement() { return { style: {} }; },
  };
  parent.closed = true;
  const detached = createBrowserPopouts({ win: child, location: { origin, href: `${origin}/` }, assetPopoutContext: asset });
  assert.deepEqual(await detached.dockAsset(asset), { ok: false, error: 'opener_unavailable' });
  assert.equal(child.closed, false);
  assert.equal(nodes[0].href, '/');
  assert.match(nodes[0].textContent, /Open Pentacle main window/);
  detached.destroy();
});
