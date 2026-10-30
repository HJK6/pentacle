'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// Run the installed composer handler with event-shaped clipboard data. Browser
// coverage separately checks the actual editing command, decoding and upload.
function harness({ command = false } = {}) {
  const app = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const start = app.indexOf("inputEl.addEventListener('paste'");
  const end = app.indexOf("inputEl.addEventListener('keydown'", start);
  const uploads = [];
  const owner = { record: {} };
  let handler, inputs = 0, captures = 0;
  const inputEl = { value: 'before AFTER tail', selectionStart: 7, selectionEnd: 12,
    addEventListener: (_, fn) => { handler = fn; },
    setRangeText(text, start, end, mode) {
      assert.equal(mode, 'end');
      this.value = this.value.slice(0, start) + text + this.value.slice(end);
      this.selectionStart = this.selectionEnd = start + text.length;
    },
    dispatchEvent() { inputs++; },
  };
  vm.runInNewContext(app.slice(start, end), { slot: 0, inputEl, Event,
    document: { execCommand(name, ui, text) {
      assert.equal(name, 'insertText'); assert.equal(ui, false);
      if (command) { inputEl.setRangeText(text, inputEl.selectionStart, inputEl.selectionEnd, 'end'); inputs++; }
      return command;
    } },
    window: {}, captureComposer: () => { captures++; return owner; },
    addSlotAttachmentFiles: async (...args) => { uploads.push(args); },
    composerMounted: () => true, setSlotSendError() {},
  });
  return { inputEl, uploads, owner, get inputs() { return inputs; }, get captures() { return captures; },
    paste(clipboardData) {
      const event = { clipboardData, prevented: 0, preventDefault() { this.prevented++; } };
      handler(event); return event;
    } };
}
const file = type => ({ type, name: type });
const item = value => ({ kind: 'file', type: value.type, getAsFile: () => value });
const clipboard = (items = [], files = [], text = '') => ({ items, files, getData: type => type === 'text/plain' ? text : '<b>never inserted</b>' });

test('items are collected synchronously, in order, without duplicating files', () => {
  const h = harness(), png = file('image/png'), jpeg = file('image/jpeg');
  const event = h.paste(clipboard([item(png), item(jpeg)], [png, jpeg]));
  assert.equal(event.prevented, 1);
  assert.deepEqual(Array.from(h.uploads[0][1]), [png, jpeg]);
  assert.equal(h.uploads[0][2], h.owner);
  h.paste(clipboard([item(png)], [png]));
  assert.equal(h.uploads.length, 2, 'intentional repeat pastes remain separate');
});

test('files fallback applies only when items yield no image files', () => {
  const h = harness(), png = file('image/png');
  h.paste(clipboard([{ kind: 'file', type: 'image/png', getAsFile: () => null }], [png]));
  assert.deepEqual(Array.from(h.uploads[0][1]), [png]);
  h.paste(clipboard([], [png]));
  assert.deepEqual(Array.from(h.uploads[1][1]), [png]);
});

for (const data of [undefined, clipboard(), clipboard([], [], 'ordinary text'),
  clipboard([item(file('application/pdf'))], [file('application/pdf')], 'ordinary text'),
  clipboard([{ kind: 'file', type: 'image/png', getAsFile: () => null }])]) {
  test('no usable image preserves native paste', () => {
    const h = harness(); const event = h.paste(data);
    assert.equal(event.prevented, 0); assert.equal(h.uploads.length, 0);
    assert.equal(h.captures, 0); assert.equal(h.inputEl.value, 'before AFTER tail');
  });
}

for (const command of [false, true]) test(`mixed text replaces selection once (editing command ${command})`, () => {
  const h = harness({ command }), gif = file('image/gif');
  const event = h.paste(clipboard([item(gif)], [], 'caption'));
  assert.equal(event.prevented, 1); assert.equal(h.inputEl.value, 'before caption tail');
  assert.equal(h.inputEl.selectionStart, 14); assert.equal(h.inputEl.selectionEnd, 14);
  assert.equal(h.inputs, 1); assert.equal(h.captures, 1);
  assert.equal(h.uploads[0][1][0], gif, 'unsupported images reach existing validation');
});
