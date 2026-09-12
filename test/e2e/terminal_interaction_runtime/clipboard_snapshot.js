'use strict';

async function snapshotClipboard(clipboard, ClipboardItem) {
  // Electron 44 returns one read-side item even when the clipboard has no
  // formats. Its public constructor rejects an empty MIME map. Keep the empty
  // snapshot as [], and eagerly read ALL formats of nonempty items before the
  // harness writes anything: read-side getType() accesses the live clipboard.
  const items = await clipboard.read();
  return Promise.all(items.filter(item => item.types.length > 0).map(async item =>
    new ClipboardItem(Object.fromEntries(await Promise.all(item.types.map(async type =>
      [type, await item.getType(type)]))))));
}

async function restoreClipboard(clipboard, saved) {
  if (saved.length === 0) clipboard.clear();
  else await clipboard.write(saved);
}

module.exports = { snapshotClipboard, restoreClipboard };
