const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const { confirmDialog } = rendererRequire('./confirm_dialog');

function freshDom() {
  const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>');
  return { dom, doc: dom.window.document };
}

function overlay(doc) {
  return doc.getElementById('confirm-overlay');
}

test('renders an in-DOM .modal-overlay with the message and two buttons', () => {
  const { doc } = freshDom();
  confirmDialog('Clear output 42?', { doc });
  const ov = overlay(doc);
  assert.ok(ov, 'overlay is created');
  assert.ok(ov.classList.contains('modal-overlay'));
  assert.equal(ov.getAttribute('role'), 'dialog');
  assert.match(ov.textContent, /Clear output 42\?/);
  const buttons = ov.querySelectorAll('button');
  assert.equal(buttons.length, 2, 'Cancel + Confirm');
});

test('Confirm button resolves true and removes the overlay', async () => {
  const { doc } = freshDom();
  const p = confirmDialog('proceed?', { doc });
  const confirmBtn = overlay(doc).querySelector('.sb-btn-blue');
  confirmBtn.click();
  assert.equal(await p, true);
  assert.equal(overlay(doc), null, 'overlay removed after resolve');
});

test('Cancel button resolves false and removes the overlay', async () => {
  const { doc } = freshDom();
  const p = confirmDialog('proceed?', { doc });
  const cancelBtn = overlay(doc).querySelector('.sb-btn:not(.sb-btn-blue)');
  cancelBtn.click();
  assert.equal(await p, false);
  assert.equal(overlay(doc), null);
});

test('Enter key resolves true, Escape resolves false', async () => {
  const { dom, doc } = freshDom();
  const pEnter = confirmDialog('a?', { doc });
  doc.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter' }));
  assert.equal(await pEnter, true);

  const pEsc = confirmDialog('b?', { doc });
  doc.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(await pEsc, false);
});

test('a click on the backdrop (outside the modal) cancels', async () => {
  const { dom, doc } = freshDom();
  const p = confirmDialog('x?', { doc });
  const ov = overlay(doc);
  ov.dispatchEvent(new dom.window.MouseEvent('mousedown', { bubbles: true }));
  assert.equal(await p, false);
});

test('restores focus to the previously focused element on close', async () => {
  const { doc } = freshDom();
  const input = doc.createElement('input');
  doc.body.appendChild(input);
  input.focus();
  assert.equal(doc.activeElement, input, 'precondition: input has focus');

  const p = confirmDialog('y?', { doc });
  // While open, focus moved to the confirm button (a DOM element, not native).
  assert.notEqual(doc.activeElement, input);
  overlay(doc).querySelector('.sb-btn-blue').click();
  await p;
  assert.equal(doc.activeElement, input, 'focus handed back to the terminal/prior element');
});

test('removes its keydown listener after close (stale keys do not re-resolve)', async () => {
  const { dom, doc } = freshDom();
  const p = confirmDialog('z?', { doc });
  overlay(doc).querySelector('.sb-btn-blue').click();
  await p;
  // A later Enter must not throw or resolve anything (listener detached).
  assert.doesNotThrow(() => {
    doc.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Enter' }));
  });
  assert.equal(overlay(doc), null);
});

test('resolves false (never throws) when no document is available', async () => {
  assert.equal(await confirmDialog('no doc', { doc: null }), false);
});
