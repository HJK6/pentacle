'use strict';

const { openMockSession } = require('../lib/mock_chat_scenarios');

const SCENARIO_META = {
  target_compat: ['hostc'],
  requires: [],
  providers: ['codex', 'claude'],
  scripted_daemon_fixture: 'attachment_send_reconcile',
};

const ONE_BY_ONE_PNG_BASE64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=';

async function run(ctx) {
  const opened = await openMockSession(ctx);
  const cell = `#cell-${opened.slot}`;
  await ctx.waitFor(
    `(() => { const input = document.querySelector('${cell} .slot-chat-attachment-input'); return !!input; })()`,
    { timeoutMs: 15000, label: 'attachment input mounted' },
  );

  const attached = await ctx.eval(`(() => {
    const input = document.querySelector('${cell} .slot-chat-attachment-input');
    if (!input) return false;
    const b64 = ${JSON.stringify(ONE_BY_ONE_PNG_BASE64)};
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    const file = new File([bytes], 'fixture.png', { type: 'image/png' });
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  })()`);
  ctx.assert('G1: image file attached through composer input', attached === true);
  await ctx.waitFor(
    `document.querySelectorAll('${cell} .slot-chat-attachment-chip img').length === 1`,
    { timeoutMs: 10000, label: 'attachment chip visible' },
  );

  const before = ctx.beaconSeq();
  await ctx.type(`${cell} .slot-chat-compose-input`, 'caption from fixture');
  await ctx.click(`${cell} .slot-chat-compose-send`);
  await ctx.awaitBeacon((b) => b.seq > before && b.name === 'chat.compose.optimistic_insert', {
    timeoutMs: 10000,
    label: 'attachment optimistic insert',
  });
  await ctx.waitFor(
    `!!document.querySelector('${cell} .slot-chat-row.is-user .slot-chat-media-button .slot-chat-media-img')`,
    { timeoutMs: 15000, label: 'inline media bubble visible' },
  );
  ctx.assert('G1: pending attachment tray cleared after send',
    await ctx.eval(`document.querySelectorAll('${cell} .slot-chat-attachment-chip').length === 0`));

  await ctx.waitFor(
    `(() => {
      const media = document.querySelector('${cell} .slot-chat-media-button[data-attachment-key]');
      const img = media && media.querySelector('.slot-chat-media-img');
      return !!(media && img && img.src);
    })()`,
    { timeoutMs: 30000, label: 'attachment media has image source' },
  );
  await ctx.click(`${cell} .slot-chat-media-button`);
  await ctx.waitFor(
    `!!document.querySelector('.slot-chat-image-viewer.is-open .slot-chat-image-viewer-img[src]')`,
    { timeoutMs: 10000, label: 'image viewer opened' },
  );
  ctx.assert('G1: click opens image viewer', true);
  await ctx.screenshot('send-image-attachment');
}

module.exports = { SCENARIO_META, run };

