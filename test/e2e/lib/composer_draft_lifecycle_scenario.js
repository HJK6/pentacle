'use strict';
const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');
const { fixtureRequest } = require('./closed_chat_scenario');
const json = JSON.stringify;
const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII=';

async function setup(ctx) {
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 1, activeSlot: 0 }) });
  return async id => {
    const selector = `#session-list .session-item[data-stream-id="${id}"]`;
    await waitFor(ctx, `!!document.querySelector(${json(selector)})`, `sidebar ${id}`);
    await ctx.session.eval(`document.querySelector(${json(selector)}).click(), true`);
    await waitFor(ctx, `document.querySelector('#header-0 .cell-copyid')?.dataset.streamId === ${json(id)}`, `bound ${id}`);
    await waitFor(ctx, "!!document.querySelector('#header-0 [data-mode=chat]')", 'chat control');
    await ctx.session.eval("document.querySelector('#header-0 [data-mode=chat]').click(), true");
    await waitFor(ctx, "!!document.querySelector('#cell-0 .slot-chat-compose-input')", 'composer');
  };
}
async function attachImage(ctx) {
  await ctx.session.eval(`(() => { const bytes = Uint8Array.from(atob(${json(png)}), c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'synthetic.png', { type: 'image/png' }));
    const input = document.querySelector('#cell-0 .slot-chat-attachment-input'); input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
  await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1", 'image loaded');
}
async function composerDraftRetire(ctx) {
  if (!ctx.fixture || !ctx.runtime.fixtureTokens) throw new Error('requires disposable fixture');
  const open = await setup(ctx);
  try {
    const id = 'local:web-gate-seam';
    await open(id);
    await ctx.session.eval(`(() => { window.__revokedDrafts = []; const revoke = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = url => { window.__revokedDrafts.push(url); revoke(url); }; return true; })()`);
    await attachImage(ctx);
    const image = await ctx.session.eval("document.querySelector('#cell-0 .slot-chat-attachment-chip img').src");
    await open(ctx.fixture.streamId);
    const closed = await fixtureRequest(ctx.runtime, id, { type: 'close', host: 'local', session_name: 'web-gate-seam' });
    if (closed.type !== 'close.ok' || !closed.ok) throw new Error('fixture close failed');
    await ctx.cdp.sleep(2200);
    const revoked = await ctx.session.eval(`window.__revokedDrafts.includes(${json(image)})`);
    ctx.report.ok('authoritative close retires unmounted image preview', revoked, { revoked });
  } finally {
    await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  }
}
async function composerDraftPendingSend(ctx) {
  if (!ctx.fixture) throw new Error('requires disposable fixture');
  const open = await setup(ctx);
  try {
    await open(ctx.fixture.streamId); await attachImage(ctx);
    await ctx.session.eval(`(() => { window.__heldUploads = []; window.__pendingSends = [];
      window.cc.chatUploadBlob = () => new Promise(resolve => window.__heldUploads.push(resolve));
      window.PentacleChatStore.setSendBridge(args => { window.__pendingSends.push(args); return new Promise(() => {}); });
      const input = document.querySelector('#cell-0 .slot-chat-compose-input'); input.value = 'pending fixture'; input.dispatchEvent(new Event('input', { bubbles: true }));
      document.querySelector('#cell-0 .slot-chat-compose-send').click(); return true; })()`);
    await waitFor(ctx, 'window.__heldUploads.length === 1', 'upload held');
    await open('local:web-gate-survivor'); await open(ctx.fixture.streamId);
    const disabled = await ctx.session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').disabled");
    await ctx.session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').click(), true");
    await ctx.cdp.sleep(100);
    const count = await ctx.session.eval('window.__heldUploads.length');
    await ctx.session.eval("window.__heldUploads.forEach(resolve => resolve({ ok: true, blob_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' })), true");
    await waitFor(ctx, 'window.__pendingSends.length >= 1', 'pending send dispatched');
    await ctx.cdp.sleep(100);
    const sends = await ctx.session.eval('window.__pendingSends.length');
    ctx.report.ok('switching back keeps pending send disabled and sends only once', disabled && count === 1 && sends === 1, { disabled, uploads: count, sends });
  } finally {
    await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  }
}
module.exports = { composerDraftRetire, composerDraftPendingSend };
