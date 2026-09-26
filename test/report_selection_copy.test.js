'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');
const { renderAsset, updateReportComments } = require('../renderer/asset_render');

let nextId = 0;

function mountReport() {
  const doc = new JSDOM('<!doctype html><html><body></body></html>').window.document;
  const key = `spec:selection-copy:report-${++nextId}`;
  const report = {
    schema_version: 1,
    title: 'Selection copy',
    sections: [{
      id: 'section-1',
      title: 'Findings',
      status: 'in_progress',
      blocks: [
        { id: 'block-1', type: 'para', runs: [{ type: 'text', text: 'Copy this exact report passage.' }] },
        { id: 'block-2', type: 'para', runs: [{ type: 'text', text: 'Second block remains readable.' }] },
      ],
    }],
  };
  const options = { classPrefix: 'slot-asset', asset: { asset_id: `report-${nextId}`, asset_key: key }, comments: [], actions: {} };
  const root = renderAsset(doc, 'report', report, options);
  doc.body.appendChild(root);
  return { doc, key, report, options, root };
}

function body(root, blockId) {
  return root.querySelector(`[data-block-id="${blockId}"] .slot-asset-report-block-body`);
}

function select(doc, startNode, startOffset, endNode, endOffset) {
  const range = doc.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  doc.getSelection().removeAllRanges();
  doc.getSelection().addRange(range);
  return doc.getSelection().toString();
}

test('selecting report body text keeps its range through the release click', () => {
  const { doc, root } = mountReport();
  const target = body(root, 'block-1');
  const text = target.querySelector('p').firstChild;
  const chosen = select(doc, text, 0, text, 20);
  assert.equal(chosen, 'Copy this exact repo');

  target.click();

  assert.equal(doc.getSelection().toString(), chosen);
  assert.equal(text.isConnected, true);
  assert.equal(root.querySelector('.slot-asset-report-panel.is-open'), null);
});

test('cross-block selection survives the release click on its final block', () => {
  const { doc, root } = mountReport();
  const start = body(root, 'block-1').querySelector('p').firstChild;
  const finishBody = body(root, 'block-2');
  const finish = finishBody.querySelector('p').firstChild;
  const chosen = select(doc, start, 5, finish, 12);
  assert.match(chosen, /this exact report passage/);

  finishBody.click();

  assert.equal(doc.getSelection().toString(), chosen);
  assert.equal(start.isConnected, true);
  assert.equal(finish.isConnected, true);
});

test('comment refresh updates every mounted copy without replacing selected report text', () => {
  const { doc, key, report, options, root } = mountReport();
  const sibling = renderAsset(doc, 'report', report, options);
  doc.body.appendChild(sibling);
  body(sibling, 'block-2').click();
  const siblingDraft = sibling.querySelector('.slot-asset-report-comment-input');
  siblingDraft.value = 'Unsent note in the other copy';
  siblingDraft.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  const text = body(root, 'block-1').querySelector('p').firstChild;
  const chosen = select(doc, text, 0, text, 20);
  const comment = {
    comment_id: 'comment-1', section_id: 'section-1', block_id: 'block-1',
    excerpt: 'Copy this exact report passage.', body: 'A new comment', resolved: false,
  };

  assert.equal(updateReportComments(key, [comment]), true);

  assert.equal(doc.getSelection().toString(), chosen);
  assert.equal(text.isConnected, true);
  for (const mount of [root, sibling]) {
    assert.equal(mount.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin').textContent, '1');
    assert.equal(mount.querySelector('[data-block-id="block-1"]').classList.contains('has-unresolved-comments'), true);
  }
  let refreshedDraft = sibling.querySelector('.slot-asset-report-comment-input');
  assert.equal(refreshedDraft.value, 'Unsent note in the other copy');
  doc.getSelection().removeAllRanges();
  refreshedDraft.focus();
  refreshedDraft.setSelectionRange(7, 11);
  assert.equal(updateReportComments(key, [{ ...comment, resolved: true }]), true);
  for (const mount of [root, sibling]) {
    assert.equal(mount.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin').textContent, '1');
    assert.equal(mount.querySelector('[data-block-id="block-1"]').classList.contains('has-unresolved-comments'), false);
  }
  refreshedDraft = sibling.querySelector('.slot-asset-report-comment-input');
  assert.equal(doc.activeElement, refreshedDraft);
  assert.deepEqual([refreshedDraft.selectionStart, refreshedDraft.selectionEnd], [7, 11]);
  assert.equal(updateReportComments(key, []), true);
  for (const mount of [root, sibling]) {
    assert.equal(mount.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin').textContent, '+');
  }
  refreshedDraft = sibling.querySelector('.slot-asset-report-comment-input');
  assert.equal(refreshedDraft.value, 'Unsent note in the other copy');
  assert.equal(doc.activeElement, refreshedDraft);
  assert.deepEqual([refreshedDraft.selectionStart, refreshedDraft.selectionEnd], [7, 11]);
});

test('ordinary body click without text selection still opens the comment composer', () => {
  const { doc, root } = mountReport();

  body(root, 'block-1').click();

  assert.equal(doc.getSelection().isCollapsed, true);
  assert.ok(root.querySelector('.slot-asset-report-panel.is-open .slot-asset-report-comment-input'));
});
