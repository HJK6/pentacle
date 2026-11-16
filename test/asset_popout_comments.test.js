// Popout report viewer: comment mutations update in place (no content wipe, no
// body re-fetch, scroll container children stable); a republish (changed
// updated_at) keeps its full re-fetch + re-render.
// public_contract
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const POPOUT_REPORT = {
  schema_version: 1,
  title: 'Popout report',
  sections: [{
    id: 'sec-1',
    title: 'Section',
    status: 'in_progress',
    blocks: [{ id: 'block-1', type: 'para', runs: [{ type: 'text', text: 'Popout body text' }] }],
  }],
};

function installPopout() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'asset_popout.html'), 'utf8');
  const dom = new JSDOM(html, { url: 'https://example.test/renderer/asset_popout.html' });
  const fixture = {
    body: JSON.stringify(POPOUT_REPORT),
    updatedAt: '2026-07-09T10:00:00Z',
    comments: [],
  };
  const calls = { assetGet: 0, list: 0, add: 0 };
  const captured = { init: null, chatFrame: null };
  dom.window.cc = {
    assetGet: async () => {
      calls.assetGet += 1;
      return {
        ok: true,
        asset: {
          asset_id: 'popout-ip',
          title: 'Popout report',
          content_type: 'report',
          review_status: 'pending_review',
          updated_at: fixture.updatedAt,
          body: fixture.body,
          session_key: { host: 'hostb', session_name: 'provider_c-pop', stream_id: 'hostb:provider_c-pop' },
        },
      };
    },
    assetCommentsList: async () => {
      calls.list += 1;
      return { ok: true, comments: fixture.comments };
    },
    assetCommentAdd: async (args) => {
      calls.add += 1;
      return { ok: true, comment: { comment_id: 'c-new', ...args } };
    },
    assetCommentEdit: async (args) => {
      const comment = fixture.comments.find((c) => c.comment_id === args.comment_id);
      if (comment) comment.body = args.body;
      return { ok: true, comment };
    },
    assetCommentDelete: async (args) => {
      const index = fixture.comments.findIndex((c) => c.comment_id === args.comment_id);
      const removed = index >= 0 ? fixture.comments.splice(index, 1)[0] : null;
      return { ok: true, comment: removed };
    },
    assetDock: async () => ({ ok: true }),
    onAssetPopoutInit: (cb) => { captured.init = cb; },
    onChatStreamFrame: (cb) => { captured.chatFrame = cb; },
  };
  global.window = dom.window;
  global.document = dom.window.document;
  delete require.cache[require.resolve('../renderer/asset_popout')];
  require('../renderer/asset_popout');
  return { dom, fixture, calls, captured };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

test('popout comment add and comment-only push update in place; republish re-fetches', async () => {
  const { dom, fixture, calls, captured } = installPopout();
  const doc = dom.window.document;
  captured.init({
    stream_id: 'hostb:provider_c-pop',
    asset: {
      asset_id: 'popout-ip',
      title: 'Popout report',
      content_type: 'report',
      review_status: 'pending_review',
      updated_at: fixture.updatedAt,
      session_key: { host: 'hostb', session_name: 'provider_c-pop', stream_id: 'hostb:provider_c-pop' },
    },
  });
  await flush();
  await flush();

  const content = doc.getElementById('asset-popout-content');
  assert.match(content.textContent, /Popout body text/);
  assert.equal(calls.assetGet, 1);
  const reportRoot = content.querySelector('.slot-asset-report');

  // Local comment add: in-place redraw, same mounted root, no body re-fetch.
  content.querySelector('[data-block-id="block-1"] .slot-asset-report-block-body')
    .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  const input = content.querySelector('.slot-asset-report-comment-input');
  input.value = 'Popout note';
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  fixture.comments = [{
    comment_id: 'c-1',
    section_id: 'sec-1',
    block_id: 'block-1',
    excerpt: 'Popout body text',
    body: 'Popout note',
    author: 'user@example.com',
    created_at: '2026-07-09T10:01:00Z',
    resolved: false,
  }];
  content.querySelector('.slot-asset-report-comment-submit').click();
  await flush();
  await flush();

  assert.equal(calls.add, 1);
  assert.equal(content.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin').textContent, '1');
  assert.equal(content.querySelector('.slot-asset-report'), reportRoot);
  assert.equal(calls.assetGet, 1);

  // Comment-only push (updated_at unchanged, e.g. CLI resolve): comments refresh
  // in place without re-fetching the asset body.
  const listsBefore = calls.list;
  fixture.comments = [{ ...fixture.comments[0], resolved: true }];
  captured.chatFrame({
    type: 'asset.update',
    asset_id: 'popout-ip',
    title: 'Popout report',
    content_type: 'report',
    review_status: 'pending_review',
    updated_at: fixture.updatedAt,
    session_key: { host: 'hostb', session_name: 'provider_c-pop', stream_id: 'hostb:provider_c-pop' },
  });
  await flush();
  await flush();

  assert.equal(calls.list, listsBefore + 1);
  assert.equal(calls.assetGet, 1);
  assert.equal(content.querySelector('.slot-asset-report'), reportRoot);
  assert.equal(
    content.querySelector('[data-block-id="block-1"]').classList.contains('has-unresolved-comments'),
    false,
  );

  // Local edit through the real controls: in-place, no body re-fetch.
  content.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin')
    .dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  content.querySelector('.slot-asset-report-comment-actions [title="Edit"]').click();
  const editInput = content.querySelector('.slot-asset-report-comment-input');
  editInput.value = 'Popout note (edited)';
  editInput.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  content.querySelector('.slot-asset-report-comment-submit').click();
  await flush();
  await flush();
  assert.match(content.querySelector('.slot-asset-report-comment-body').textContent, /edited/);
  assert.equal(content.querySelector('.slot-asset-report'), reportRoot);
  assert.equal(calls.assetGet, 1);

  // Local delete: in-place, marker clears, still no body re-fetch.
  content.querySelector('.slot-asset-report-comment-actions [title="Delete"]').click();
  await flush();
  await flush();
  assert.equal(content.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin').textContent, '+');
  assert.equal(content.querySelector('.slot-asset-report'), reportRoot);
  assert.equal(calls.assetGet, 1);
  fixture.comments = [];

  // Republish push (changed updated_at): full re-fetch and document swap, with
  // the prior revision's comments cleared.
  fixture.body = JSON.stringify({
    ...POPOUT_REPORT,
    sections: [{
      ...POPOUT_REPORT.sections[0],
      blocks: [{ id: 'block-1', type: 'para', runs: [{ type: 'text', text: 'Popout second revision' }] }],
    }],
  });
  fixture.updatedAt = '2026-07-09T11:00:00Z';
  fixture.comments = [];
  captured.chatFrame({
    type: 'asset.update',
    asset_id: 'popout-ip',
    title: 'Popout report',
    content_type: 'report',
    review_status: 'pending_review',
    updated_at: fixture.updatedAt,
    session_key: { host: 'hostb', session_name: 'provider_c-pop', stream_id: 'hostb:provider_c-pop' },
  });
  await flush();
  await flush();

  assert.equal(calls.assetGet, 2);
  assert.match(content.textContent, /Popout second revision/);
  assert.notEqual(content.querySelector('.slot-asset-report'), reportRoot);
  assert.equal(content.querySelector('[data-block-id="block-1"] .slot-asset-report-comment-pin').textContent, '+');
});
