const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = require('jsdom');

const root = path.join(__dirname, '..');
const rendererRequire = createRequire(path.join(root, 'renderer', 'app.js'));
const { showToast } = rendererRequire('./toast');

function freshDoc() {
  const dom = new JSDOM('<!DOCTYPE html><html><body></body></html>');
  return dom.window.document;
}

test('appends a toast into #toast-container with the message text and error class', () => {
  const doc = freshDoc();
  const node = showToast('Failed to close chat', { type: 'error', doc });
  const container = doc.getElementById('toast-container');
  assert.ok(container, 'container is created on demand');
  assert.equal(container.children.length, 1);
  assert.equal(node.textContent, 'Failed to close chat');
  assert.ok(node.classList.contains('toast'));
  assert.ok(node.classList.contains('toast-error'));
});

test('reuses a single #toast-container across calls', () => {
  const doc = freshDoc();
  showToast('one', { doc });
  showToast('two', { doc });
  assert.equal(doc.querySelectorAll('#toast-container').length, 1);
  assert.equal(doc.getElementById('toast-container').children.length, 2);
});

test('auto-dismisses after timeoutMs', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const doc = freshDoc();
  showToast('bye', { doc, timeoutMs: 6000 });
  const container = doc.getElementById('toast-container');
  assert.equal(container.children.length, 1);
  t.mock.timers.tick(6000);
  assert.equal(container.children.length, 0, 'toast removed after the timeout fires');
});

test('timeoutMs <= 0 disables auto-dismiss', (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const doc = freshDoc();
  showToast('sticky', { doc, timeoutMs: 0 });
  const container = doc.getElementById('toast-container');
  t.mock.timers.tick(1_000_000);
  assert.equal(container.children.length, 1, 'toast stays when no timeout is set');
});

test('click dismisses the toast early', () => {
  const doc = freshDoc();
  const node = showToast('clickme', { doc, timeoutMs: 0 });
  const container = doc.getElementById('toast-container');
  assert.equal(container.children.length, 1);
  node.click();
  assert.equal(container.children.length, 0, 'click removes the toast');
});

test('coerces non-string messages and handles null', () => {
  const doc = freshDoc();
  assert.equal(showToast(undefined, { doc }).textContent, '');
  assert.equal(showToast(42, { doc }).textContent, '42');
});

// ── The bug guard: a toast must never take keyboard focus ──────────────────
// This is the whole point of the fix. If showToast ever calls .focus() or adds
// a focusable/auto-focused control, it could reintroduce a focus-steal that
// disrupts terminal input — the symptom we are eliminating. This test fails if
// any of that is reintroduced.
test('never moves keyboard focus and creates no focusable control', () => {
  const doc = freshDoc();
  const win = doc.defaultView;

  const sentinel = doc.createElement('input');
  doc.body.appendChild(sentinel);
  sentinel.focus();
  assert.equal(doc.activeElement, sentinel, 'precondition: sentinel holds focus');

  let focusCalls = 0;
  const origFocus = win.HTMLElement.prototype.focus;
  win.HTMLElement.prototype.focus = function patched(...args) {
    focusCalls += 1;
    return origFocus.apply(this, args);
  };
  try {
    const node = showToast('no focus steal', { doc, timeoutMs: 0 });
    assert.equal(focusCalls, 0, 'showToast must not call .focus() on any element');
    assert.equal(doc.activeElement, sentinel, 'focus must stay on the previously focused element');

    const focusables = node.querySelectorAll('a[href], button, input, select, textarea, [tabindex]');
    assert.equal(focusables.length, 0, 'toast subtree contains no focusable/tabbable controls');
    assert.ok(!node.hasAttribute('tabindex'), 'toast node itself is not tabbable');
  } finally {
    win.HTMLElement.prototype.focus = origFocus;
  }
});

test('showToast returns null when no document is available', () => {
  assert.equal(showToast('x', { doc: null }), null);
});
