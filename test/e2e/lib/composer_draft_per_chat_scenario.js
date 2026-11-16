'use strict';

const { WORKSPACE_KEY, waitFor, reloadWith } = require('./workspace_scenario');
const json = JSON.stringify;

// Exercise real sidebar navigation and the built composer against synthetic chats.
async function composerDraftPerChat(ctx) {
  const { session, report, fixture } = ctx;
  if (!fixture) throw new Error('composer-draft-per-chat requires the isolated fixture');
  const frames = [];
  const onFrame = raw => { try { const event = JSON.parse(raw); if (event.method === 'Network.webSocketFrameSent') frames.push(event.params.response.payloadData || ''); } catch {} };
  session.ws.on('message', onFrame);
  await session.send('Network.enable');
  try {
  await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 1, activeSlot: 0 }) });
  const open = async id => {
    const selector = `#session-list .session-item[data-stream-id="${id}"]`;
    await waitFor(ctx, `!!document.querySelector(${json(selector)})`, `sidebar ${id}`);
    await session.eval(`document.querySelector(${json(selector)}).click(), true`);
    await waitFor(ctx, `document.querySelector('#header-0 .cell-copyid')?.dataset.streamId === ${json(id)}`, `bound ${id}`);
    if (id !== 'local:web-gate-assistant') {
      await waitFor(ctx, "!!document.querySelector('#header-0 [data-mode=chat]')", 'chat control');
      await session.eval("document.querySelector('#header-0 [data-mode=chat]').click(), true");
    }
    await waitFor(ctx, "!!document.querySelector('#cell-0 .slot-chat-compose-input')", 'composer');
  };
  const text = 'Synthetic first line\nSynthetic second line';
  const read = () => session.eval("document.querySelector('#cell-0 .slot-chat-compose-input').value");
  await open(fixture.streamId);
  await session.eval(`(() => { const input = document.querySelector('#cell-0 .slot-chat-compose-input'); input.value = ${json(text)}; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  await session.eval(`(() => { window.__draftLeaks = []; const cell = document.querySelector('#cell-0');
    window.__draftObserver = new MutationObserver(() => { if (document.querySelector('#header-0 .cell-copyid')?.dataset.streamId === 'local:web-gate-survivor' && document.querySelector('#cell-0 .slot-chat-compose-input')?.value === ${json(text)}) window.__draftLeaks.push('text'); });
    window.__draftObserver.observe(cell, { subtree: true, childList: true, attributes: true, characterData: true }); return true; })()`);
  await open('local:web-gate-survivor');
  report.ok('no outgoing draft flashes in B during switching', await session.eval('window.__draftLeaks.length === 0'));
  await session.eval('window.__draftObserver.disconnect(), true');
  report.ok('other chat does not receive the outgoing draft', await read() === '');
  await open(fixture.streamId);
  const actual = await read();
  report.ok('returning to the first chat restores its exact multiline draft', actual === text, { actual });
  const write = value => session.eval(`(() => { const input = document.querySelector('#cell-0 .slot-chat-compose-input'); input.value = ${json(value)}; input.dispatchEvent(new Event('input', { bubbles: true })); return true; })()`);
  await open('local:web-gate-survivor');
  await write('Draft B');
  await open(fixture.streamId);
  report.ok('A survives B having its own draft', await read() === text);
  await write('');
  await open('local:web-gate-survivor');
  report.ok('B draft remains independent', await read() === 'Draft B');
  await open(fixture.streamId);
  report.ok('explicitly emptied A remains empty', await read() === '');

  // A tiny real PNG, delivered through the normal file-input event.
  await session.eval(`(() => { const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'synthetic.png', { type: 'image/png' }));
    const input = document.querySelector('#cell-0 .slot-chat-attachment-input'); input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true })); return true; })()`);
  await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1", 'image decoded');
  await open('local:web-gate-survivor');
  report.ok('image does not migrate to B', await session.eval("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 0"));
  await open(fixture.streamId);
  report.ok('image preview returns with A', await session.eval("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip img[src^=blob]').length === 1"));
  await session.eval("document.querySelector('#cell-0 .slot-chat-attachment-remove').click(), true");
  await open('local:web-gate-survivor'); await open(fixture.streamId);
  report.ok('removed image stays removed', await session.eval("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 0"));

  await session.eval(`(() => { window.__draftSends = []; window.PentacleChatStore.setSendBridge(args => { window.__draftSends.push(args); return new Promise(() => {}); }); return true; })()`);
  await write('Send only A');
  await session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').click(), true");
  await waitFor(ctx, 'window.__draftSends.length === 1', 'A optimistic send');
  await open('local:web-gate-survivor');
  report.ok('sending A preserves B', await read() === 'Draft B');
  await open(fixture.streamId);
  report.ok('successful send clears A', await read() === '');
  report.ok('send targeted A', await session.eval(`window.__draftSends[0].streamId === ${json(fixture.streamId)}`));
  // Hold an upload across navigation; the send must use A and clear only A.
  await session.eval(`(() => { const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'synthetic.png', { type: 'image/png' }));
    const input = document.querySelector('#cell-0 .slot-chat-attachment-input'); input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true }));
    window.cc.chatUploadBlob = () => new Promise(resolve => { window.__finishUpload = resolve; }); return true; })()`);
  await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1", 'delayed upload image');
  await write('Delayed A');
  await session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').click(), true");
  await waitFor(ctx, 'typeof window.__finishUpload === "function"', 'upload held');
  await open('local:web-gate-survivor');
  await session.eval("window.__finishUpload({ ok: true, blob_sha: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }), true");
  await waitFor(ctx, 'window.__draftSends.length === 2', 'delayed A sent');
  report.ok('delayed upload sends to A and preserves B', await read() === 'Draft B' && await session.eval(`window.__draftSends[1].streamId === ${json(fixture.streamId)}`));
  await open(fixture.streamId);
  report.ok('delayed successful A send clears its own draft', await read() === '' && await session.eval("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 0"));
  // Returning and editing A before the upload resolves must preserve the new text.
  await session.eval(`(() => { const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'synthetic.png', { type: 'image/png' }));
    const input = document.querySelector('#cell-0 .slot-chat-attachment-input'); input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true }));
    window.__finishUpload = null; return true; })()`);
  await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1", 'second image');
  await write('Old A');
  await session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').click(), true");
  await waitFor(ctx, 'typeof window.__finishUpload === "function"', 'second upload held');
  await open('local:web-gate-survivor'); await open(fixture.streamId);
  await write('New A');
  await session.eval("window.__finishUpload({ ok: true, blob_sha: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' }), true");
  await waitFor(ctx, 'window.__draftSends.length === 3', 'second delayed A sent');
  report.ok('delayed completion preserves newer A text', await read() === 'New A');
  await session.eval("document.querySelector('#cell-0 .slot-chat-attachment-remove').click(), true");
  // Rejected precommit upload leaves the originating draft intact.
  await session.eval(`(() => { const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aWQAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'synthetic.png', { type: 'image/png' }));
    const input = document.querySelector('#cell-0 .slot-chat-attachment-input'); input.files = transfer.files; input.dispatchEvent(new Event('change', { bubbles: true }));
    window.__finishUpload = null; return true; })()`);
  await waitFor(ctx, "document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1", 'failure image');
  await session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').click(), true");
  await waitFor(ctx, 'typeof window.__finishUpload === "function"', 'failing upload held');
  await open('local:web-gate-survivor');
  await session.eval("window.__finishUpload({ ok: false, error: 'synthetic rejection' }), true");
  await ctx.cdp.sleep(100);
  report.ok('upload rejection leaves B intact', await read() === 'Draft B');
  await open(fixture.streamId);
  report.ok('upload rejection retains A text and image', await read() === 'New A' && await session.eval("document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 1"));
  await session.eval("document.querySelector('#cell-0 .slot-chat-attachment-remove').click(), true");
  const composite = 'local:web-gate-assistant';
  await open(composite);
  await write('Composite draft');
  await open('local:web-gate-survivor');
  report.ok('ordinary draft survives composite switch', await read() === 'Draft B');
  await open(composite);
  report.ok('composite draft stays on composite', await read() === 'Composite draft');
  await session.eval("document.querySelector('#cell-0 .slot-chat-compose-send').click(), true");
  await waitFor(ctx, 'window.__draftSends.length === 4', 'composite send');
  report.ok('composite send targets composite', await session.eval(`window.__draftSends[3].streamId === ${json(composite)}`));
  await open('local:web-gate-survivor');
  report.ok('composite send preserves ordinary draft', await read() === 'Draft B');
  await open(composite);
  report.ok('composite successful send clears composite draft', await read() === '');
  report.ok('typing and switching do not transmit unsent draft text', !frames.some(frame => frame.includes('Composite draft') || frame.includes('Draft B') || frame.includes('New A')));
  session.ws.off('message', onFrame);

  // Return to A in a different pane while B stays mounted.
  await open('local:web-gate-survivor');
  await session.eval("document.querySelector('[data-pane-count-button=\"2\"]').click(), true");
  await session.eval(`document.querySelector(${json(`#session-list .session-item[data-stream-id="${fixture.streamId}"]`)}).click(), true`);
  await waitFor(ctx, `document.querySelector('#header-1 .cell-copyid')?.dataset.streamId === ${json(fixture.streamId)}`, 'A in second pane');
  report.ok('A draft restores in another pane without changing B', await session.eval("document.querySelector('#cell-1 .slot-chat-compose-input')?.value === 'New A' && document.querySelector('#cell-0 .slot-chat-compose-input')?.value === 'Draft B'"));

  // A separate browser context has its own page-local cache and profile state.
  const WebSocket = require('ws');
  const version = await fetch(`http://127.0.0.1:${ctx.cdpPort}/json/version`).then(response => response.json());
  const browserSocket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { browserSocket.once('open', resolve); browserSocket.once('error', reject); });
  const browser = new ctx.cdp.CdpSession(browserSocket, {});
  const { browserContextId } = await browser.send('Target.createBrowserContext');
  let page, targetId;
  try {
    const target = await browser.send('Target.createTarget', { url: ctx.url + '?composer-isolation=1', browserContextId });
    targetId = target.targetId;
    page = await ctx.cdp.connect(ctx.cdpPort, { match: /composer-isolation=1/, timeoutMs: 15000 });
    if (page.target.id !== targetId) throw new Error('isolation context attached to the wrong target');
    const second = { ...ctx, session: page };
    await waitFor(second, `!!document.querySelector(${json(`#session-list .session-item[data-stream-id="${fixture.streamId}"]`)})`, 'second context inventory');
    await page.eval(`document.querySelector(${json(`#session-list .session-item[data-stream-id="${fixture.streamId}"]`)}).click(), true`);
    await waitFor(second, "!!document.querySelector('#cell-0 .slot-chat-compose-input')", 'second context composer');
    report.ok('independent browser context has no first-context draft or images', await page.eval("document.querySelector('#cell-0 .slot-chat-compose-input').value === '' && document.querySelectorAll('#cell-0 .slot-chat-attachment-chip').length === 0"));
  } finally {
    page?.close();
    if (targetId) await session.send('Target.closeTarget', { targetId }).catch(() => {});
    await browser.send('Target.disposeBrowserContext', { browserContextId });
    browser.close();
  }
  } finally {
    session.ws.off('message', onFrame);
    await reloadWith(ctx, { [WORKSPACE_KEY]: json({ paneCount: 4, activeSlot: 0 }) });
  }
}
module.exports = { composerDraftPerChat };
