'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { snapshotClipboard, restoreClipboard } = require('./clipboard_snapshot');

class ClipboardItem {
  constructor(data) {
    if (!Object.keys(data).length) throw new TypeError('at least one MIME type is required');
    this.data = data;
  }
}

test('empty native read item snapshots without constructing an empty ClipboardItem and restores empty', async () => {
  for (const items of [[], [{ types: [] }]]) {
    let clears = 0;
    const clipboard = { read: async () => items, clear: () => clears++, write: () => assert.fail('empty write') };
    const saved = await snapshotClipboard(clipboard, ClipboardItem);
    assert.deepEqual(saved, []);
    assert.equal(clears, 0, 'snapshot must not mutate clipboard');
    await restoreClipboard(clipboard, saved);
    assert.equal(clears, 1);
  }
});

test('snapshot eagerly retains every standard and native format before clipboard replacement', async () => {
  const original = {
    'text/plain': new Blob(['text']),
    'text/html': new Blob(['<b>text</b>']),
    'electron application/osclipboard;format="fixture.custom"': new Blob([new Uint8Array([0, 255, 42])]),
    'electron application/bookmark': { title: 'Fixture', url: 'https://example.invalid/' },
  };
  let contents = original, written;
  const clipboard = {
    read: async () => [{ types: [] }, { types: Object.keys(contents), getType: async type => contents[type] }],
    write: async items => { written = items; }, clear: () => assert.fail('must retain formats'),
  };
  const saved = await snapshotClipboard(clipboard, ClipboardItem);
  contents = { 'text/plain': new Blob(['replacement']) };
  assert.equal(written, undefined);
  await restoreClipboard(clipboard, saved);
  assert.equal(written.length, 1);
  assert.deepEqual(written[0].data, original);
  assert.equal(await written[0].data['text/plain'].text(), 'text');
});

test('unreadable MIME rejects the entire snapshot before any clipboard mutation', async () => {
  const clipboard = {
    read: async () => [{ types: ['text/plain', 'application/custom'], getType: async type => {
      if (type === 'application/custom') throw new Error('unreadable custom format');
      return new Blob(['text']);
    } }],
    write: () => assert.fail('snapshot must not write'), clear: () => assert.fail('snapshot must not clear'),
  };
  await assert.rejects(snapshotClipboard(clipboard, ClipboardItem), /unreadable custom format/);
});
