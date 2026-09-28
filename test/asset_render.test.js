const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const {
  escapeHtml,
  renderMarkdownHtml,
  renderRawJsonFallback,
  renderTablePreview,
  renderAsset,
  updateReportComments,
} = require('../renderer/asset_render');

function document() {
  return new JSDOM('<!doctype html><html><body></body></html>').window.document;
}

function sampleReport(overrides = {}) {
  return {
    schema_version: 1,
    title: 'Slice report',
    sections: [{
      id: 'sec-1',
      title: 'Renderer',
      status: 'in_progress',
      blocks: [
        {
          id: 'para-1',
          type: 'para',
          runs: [
            { type: 'text', text: 'Render ' },
            { type: 'chip', text: 'feat/report', variant: 'typed', kind: 'branch' },
            { type: 'text', text: ' safely.' },
          ],
        },
        {
          id: 'table-1',
          type: 'table',
          columns: ['Item', 'State'],
          rows: [
            [[{ type: 'chip', text: 'db', variant: 'typed', kind: 'db' }], [{ type: 'chip', text: 'verified', variant: 'status', status: 'ok' }]],
          ],
        },
        {
          id: 'callout-1',
          type: 'callout',
          kind: 'warn',
          title: 'Watch',
          runs: [{ type: 'code', text: 'javascript:alert(1)' }],
        },
      ],
    }],
    ...overrides,
  };
}

test('escapeHtml escapes text for HTML contexts', () => {
  assert.equal(escapeHtml('<tag attr="x">Tom & Jerry</tag>'), '&lt;tag attr=&quot;x&quot;&gt;Tom &amp; Jerry&lt;/tag&gt;');
  assert.equal(escapeHtml(null), '');
});

test('renderMarkdownHtml renders headings, lists, inline markup, and links', () => {
  const html = renderMarkdownHtml([
    '# Title',
    '',
    'A `code` **bold** *em* [link](https://example.com).',
    '',
    '- first',
    '* second',
  ].join('\n'));

  assert.match(html, /<h1>Title<\/h1>/);
  assert.match(html, /<code>code<\/code>/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<em>em<\/em>/);
  assert.match(html, /<a href="https:\/\/example.com">link<\/a>/);
  assert.match(html, /<ul>\n<li>first<\/li>\n<li>second<\/li>\n<\/ul>/);
});

test('renderMarkdownHtml renders fenced code with and without language', () => {
  const withLanguage = renderMarkdownHtml('```js\nconst n = 1 < 2;\n```');
  const withoutLanguage = renderMarkdownHtml('```\nplain\n```');

  assert.match(withLanguage, /<pre><code class="language-js">const n = 1 &lt; 2;<\/code><\/pre>/);
  assert.match(withoutLanguage, /<pre><code>plain<\/code><\/pre>/);
});

test('renderTablePreview renders headers, rows, numeric cells, and ragged rows', () => {
  const doc = document();
  const node = renderTablePreview(doc, {
    columns: ['Name', 'Count', 'Notes'],
    rows: [
      ['alpha', 3, 'ok'],
      ['beta', '4.5'],
      ['gamma', '', 'missing count'],
    ],
  });

  assert.equal(node.className, 'pi-control-preview-table-wrap');
  assert.deepEqual(Array.from(node.querySelectorAll('th')).map((th) => th.textContent), ['Name', 'Count', 'Notes']);
  assert.deepEqual(Array.from(node.querySelectorAll('tbody tr')).map((tr) => Array.from(tr.children).map((td) => td.textContent)), [
    ['alpha', '3', 'ok'],
    ['beta', '4.5', ''],
    ['gamma', '', 'missing count'],
  ]);
  assert.deepEqual(Array.from(node.querySelectorAll('tbody tr')).map((tr) => tr.children[1].className), ['numeric', 'numeric', 'numeric']);
  assert.equal(node.querySelector('tbody tr:last-child td:first-child').className, '');
});

test('renderTablePreview parameterizes wrapper and numeric classes', () => {
  const doc = document();
  const node = renderTablePreview(doc, { columns: ['Count'], rows: [[1]] }, 'slot-asset');

  assert.equal(node.className, 'slot-asset-preview-table-wrap');
  assert.equal(node.querySelector('td').className, 'slot-asset-numeric');
});

test('renderAsset renders markdown and json_table nodes', () => {
  const doc = document();
  const markdown = renderAsset(doc, 'markdown', '# Hi', { classPrefix: 'slot-asset' });
  const table = renderAsset(doc, 'json_table', { columns: ['Count'], rows: [[2]] }, { classPrefix: 'slot-asset' });

  assert.equal(markdown.tagName, 'DIV');
  assert.equal(markdown.className, 'slot-asset-markdown-preview');
  assert.match(markdown.innerHTML, /<h1>Hi<\/h1>/);
  assert.equal(table.className, 'slot-asset-preview-table-wrap');
});

test('renderAsset renders report sections, blocks, chips, and review status', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    reviewStatus: 'changes_requested',
    comments: [{
      comment_id: 'c1',
      section_id: 'sec-1',
      block_id: 'para-1',
      excerpt: 'Render feat/report safely.',
      body: 'Clarify status',
      author: 'operator@node1',
      created_at: '2026-07-05T12:00:00Z',
      resolved: false,
    }],
  });

  assert.equal(node.className, 'slot-asset-report');
  assert.equal(node.querySelector('h1').textContent, 'Slice report');
  assert.equal(node.querySelector('.slot-asset-report-section h2').textContent, 'Renderer');
  assert.equal(node.querySelector('.slot-asset-report-chip-kind-branch').textContent.includes('feat/report'), true);
  assert.equal(node.querySelector('.slot-asset-report-table-wrap th').textContent, 'Item');
  assert.match(node.querySelector('.slot-asset-report-callout-title').textContent, /Watch/);
  assert.equal(node.querySelector('.slot-asset-report-comment-pin').textContent, '1');
});

test('renderAsset report comments: add (clears draft, shows notice), edit, delete via icons, send confirms', async () => {
  const doc = document();
  const calls = [];
  const node = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    comments: [{
      comment_id: 'c1',
      section_id: 'sec-1',
      block_id: 'para-1',
      excerpt: 'Render feat/report safely.',
      body: 'Clarify status',
      author: 'operator@node1',
      created_at: '2026-07-05T12:00:00Z',
      resolved: false,
    }],
    actions: {
      addComment: async (comment) => calls.push(['add', comment]),
      editComment: async (id, body) => calls.push(['edit', id, body]),
      deleteComment: async (id) => calls.push(['delete', id]),
      sendToChat: async () => calls.push(['send']),
    },
  });

  // Select the block → compose bar appears.
  node.querySelector('[data-block-id="para-1"] .slot-asset-report-block-body').dispatchEvent(new doc.defaultView.MouseEvent('click', { bubbles: true }));
  const input = node.querySelector('.slot-asset-report-comment-input');
  input.value = 'Needs one more fact';
  input.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  node.querySelector('.slot-asset-report-comment-submit').dispatchEvent(new doc.defaultView.MouseEvent('click', { bubbles: true }));
  await new Promise((resolve) => setImmediate(resolve));
  // Draft is cleared after adding, and a success notice is shown.
  assert.equal(node.querySelector('.slot-asset-report-comment-input').value, '');
  assert.match(node.querySelector('.slot-asset-report-action-notice').textContent, /added/i);
  // No resolve control in the operator UI.
  assert.equal(node.querySelector('.slot-asset-report-comment-actions [title="Resolve"]'), null);

  // Edit the existing comment via its pencil icon, then save via the check icon.
  node.querySelector('.slot-asset-report-comment-actions [title="Edit"]').click();
  const editInput = node.querySelector('.slot-asset-report-comment-input');
  assert.equal(editInput.value, 'Clarify status');
  editInput.value = 'Edited';
  editInput.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  node.querySelector('.slot-asset-report-comment-submit').click();
  await new Promise((resolve) => setImmediate(resolve));

  // Delete via the trash icon.
  node.querySelector('.slot-asset-report-comment-actions [title="Delete"]').click();
  await new Promise((resolve) => setImmediate(resolve));

  Array.from(node.querySelectorAll('.slot-asset-report-review-row button')).find((button) => button.textContent === 'Send to chat').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(node.querySelector('.slot-asset-report-action-notice').textContent, /sent to chat/i);
  // No approve control in the feedback UI.
  assert.equal(Array.from(node.querySelectorAll('.slot-asset-report-review-row button')).some((b) => b.textContent === 'Approve'), false);

  assert.deepEqual(calls.map((call) => call[0]), ['add', 'edit', 'delete', 'send']);
  assert.equal(calls[0][1].block_id, 'para-1');
  assert.equal(calls[0][1].body, 'Needs one more fact');
  assert.equal(calls[1][2], 'Edited');
});

test('renderAsset report shows action failures instead of treating them as success', async () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    actions: {
      sendToChat: async () => {
        throw new Error('closed session');
      },
    },
  });

  Array.from(node.querySelectorAll('.slot-asset-report-review-row button'))
    .find((button) => button.textContent === 'Send to chat')
    .click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(node.querySelector('.slot-asset-report-action-error').textContent, 'closed session');
});

test('renderAsset report opens a block thread from its comment pin and edits from there', async () => {
  const doc = document();
  const calls = [];
  const node = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    comments: [{
      comment_id: 'c1',
      section_id: 'sec-1',
      block_id: 'para-1',
      excerpt: 'Render feat/report safely.',
      body: 'Original',
      author: 'operator@node1',
      created_at: '2026-07-05T12:00:00Z',
      resolved: false,
    }],
    actions: {
      editComment: async (id, body) => calls.push([id, body]),
    },
  });

  // Open the block's thread via its comment pin (the count marker).
  node.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').dispatchEvent(new doc.defaultView.MouseEvent('click', { bubbles: true }));
  node.querySelector('.slot-asset-report-comment-actions [title="Edit"]').click();
  const input = node.querySelector('.slot-asset-report-comment-input');
  assert.equal(input.value, 'Original');
  input.value = 'Edited from thread';
  input.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
  node.querySelector('.slot-asset-report-comment-submit').click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(calls, [['c1', 'Edited from thread']]);
});

test('renderAsset report keeps script-bearing strings inert and drops unsafe hrefs', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport({
    sections: [{
      id: 'sec-xss',
      title: '<img src=x onerror=alert(1)>',
      status: 'reference',
      blocks: [{
        id: 'block-xss',
        type: 'para',
        runs: [
          { type: 'text', text: '<script>window.__xss=1</script>' },
          { type: 'link', text: 'bad link', href: 'javascript:alert(1)' },
          { type: 'chip', text: 'bad chip', variant: 'link', href: 'javascript:alert(2)' },
          { type: 'link', text: 'good link', href: 'https://example.test/path' },
        ],
      }],
    }],
  }), { classPrefix: 'slot-asset' });

  assert.equal(node.querySelector('script'), null);
  assert.match(node.textContent, /<script>window.__xss=1<\/script>/);
  assert.equal(Array.from(node.querySelectorAll('a')).some((link) => link.href.startsWith('javascript:')), false);
  assert.equal(node.querySelector('a[href="https://example.test/path"]').textContent, 'good link');
  assert.equal(node.querySelector('.slot-asset-report-chip-link').tagName, 'SPAN');
});

test('renderAsset report shows tolerant placeholders and schema mismatch banner', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport({
    schema_version: 2,
    sections: [{
      id: 'sec-1',
      title: 'Skew',
      status: 'reference',
      blocks: [
        { id: 'unknown-block', type: 'timeline', runs: [] },
        { id: 'unknown-run', type: 'para', runs: [{ type: 'sparkline', text: 'later' }] },
      ],
    }],
  }), { classPrefix: 'slot-asset' });

  assert.match(node.querySelector('.slot-asset-report-banner').textContent, /schema 2/);
  assert.match(node.textContent, /Unsupported block type: timeline/);
  assert.match(node.textContent, /Unsupported run type: sparkline/);
});

test('renderAsset report has raw JSON fallback when support is unavailable', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport(), { classPrefix: 'slot-asset', reportSupport: false });

  assert.equal(node.className, 'slot-asset-raw-json-fallback');
  assert.match(node.querySelector('.slot-asset-report-banner').textContent, /cannot render/);
  assert.match(node.querySelector('pre').textContent, /"schema_version": 1/);
});

test('renderAsset report shows unanchored comments with excerpt snapshot', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    comments: [{
      comment_id: 'c-orphan',
      section_id: 'sec-1',
      block_id: 'old-block',
      excerpt: 'Old block text',
      body: 'Still needs handling',
      author: 'operator@node1',
      created_at: '2026-07-05T12:00:00Z',
      resolved: false,
    }],
  });

  assert.match(node.querySelector('.slot-asset-report-unanchored').textContent, /Old block text/);
  assert.match(node.querySelector('.slot-asset-report-unanchored').textContent, /Still needs handling/);
});

function sampleComment(overrides = {}) {
  return {
    comment_id: 'c-ip',
    section_id: 'sec-1',
    block_id: 'para-1',
    excerpt: 'Render feat/report safely.',
    body: 'In-place note',
    author: 'operator@node1',
    created_at: '2026-07-09T12:00:00Z',
    resolved: false,
    ...overrides,
  };
}

test('updateReportComments refreshes a mounted report in place, keyed by scoped identity', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    asset: { asset_id: 'ip-asset-1', asset_key: 'spec:spec_ip:ip-asset-1' },
    comments: [],
    actions: {},
  });
  // A live in-place update targets DOM-connected mounts (the real report root
  // lives inside the slot's assetMount, in the document); mount it accordingly.
  doc.body.appendChild(node);
  const pin = () => node.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent;
  assert.equal(pin(), '+');

  assert.equal(updateReportComments('spec:spec_ip:ip-asset-1', [sampleComment()]), true);
  // Same mounted root redrawn in place with the new marker.
  assert.equal(pin(), '1');

  // Unknown keys report unmounted so callers fall back to a full render, and the
  // raw asset_id is not the store key when a scoped asset_key exists.
  assert.equal(updateReportComments('asset:ip-asset-nope', []), false);
  assert.equal(updateReportComments('ip-asset-1', []), false);
  assert.equal(pin(), '1');
});

test('full report render re-seeds comment state, clearing in-place overrides (republish)', () => {
  const doc = document();
  const asset = { asset_id: 'ip-asset-2', asset_key: 'asset:ip-asset-2' };
  const first = renderAsset(doc, 'report', sampleReport(), { classPrefix: 'slot-asset', asset, comments: [], actions: {} });
  doc.body.appendChild(first);
  updateReportComments('asset:ip-asset-2', [sampleComment({ comment_id: 'c-old' })]);
  assert.equal(first.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent, '1');

  // Republish renders fresh with server-cleared comments; the stale in-place
  // override must not resurrect on the new document.
  const second = renderAsset(doc, 'report', sampleReport(), { classPrefix: 'slot-asset', asset, comments: [], actions: {} });
  assert.equal(second.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent, '+');
});

test('reports sharing an asset_id across scopes keep isolated comment state', () => {
  const doc = document();
  const specNode = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    asset: { asset_id: 'shared-ip', asset_key: 'spec:spec_a:shared-ip' },
    comments: [],
    actions: {},
  });
  const sessionNode = renderAsset(doc, 'report', sampleReport(), {
    classPrefix: 'slot-asset',
    asset: { asset_id: 'shared-ip', asset_key: 'asset:shared-ip' },
    comments: [],
    actions: {},
  });
  doc.body.appendChild(specNode);
  doc.body.appendChild(sessionNode);
  const sessionBlock = sessionNode.querySelector('[data-block-id="para-1"]');

  assert.equal(updateReportComments('spec:spec_a:shared-ip', [sampleComment()]), true);
  assert.equal(specNode.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent, '1');
  // The session-scoped twin was neither updated nor redrawn (same child nodes).
  assert.equal(sessionNode.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent, '+');
  assert.equal(sessionNode.querySelector('[data-block-id="para-1"]'), sessionBlock);
});

test('updateReportComments redraws every live mount of a scoped report (same asset open in two slots)', () => {
  const doc = document();
  const opts = {
    classPrefix: 'slot-asset',
    asset: { asset_id: 'dup-ip', asset_key: 'spec:spec_dup:dup-ip' },
    comments: [],
    actions: {},
  };
  // The slot router can route the same spec-scoped report into two slots at once
  // (two sessions carrying the spec). A comment update must reach BOTH mounts —
  // the pre-fix single render callback only redrew the last-rendered mount.
  // Callers append each root synchronously after render (the real-app contract),
  // so a live sibling is connected by the time the next mount registers.
  const slotA = renderAsset(doc, 'report', sampleReport(), opts);
  doc.body.appendChild(slotA);
  const slotB = renderAsset(doc, 'report', sampleReport(), opts);
  doc.body.appendChild(slotB);
  const pinA = () => slotA.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent;
  const pinB = () => slotB.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent;
  assert.equal(pinA(), '+');
  assert.equal(pinB(), '+');

  assert.equal(updateReportComments('spec:spec_dup:dup-ip', [sampleComment()]), true);
  assert.equal(pinA(), '1');
  assert.equal(pinB(), '1');

  // A mount torn down (detached from the document) is pruned: no error, no
  // repaint of the dead node, and the surviving mount still updates in place.
  slotB.remove();
  assert.equal(updateReportComments('spec:spec_dup:dup-ip', []), true);
  assert.equal(pinA(), '+');
  const staleB = slotB.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').textContent;
  assert.equal(staleB, '1'); // detached copy was NOT repainted

  // When the last live copy is detached too, a comment update reports "nothing
  // mounted" (false) so the caller falls back to a full render — and it must not
  // repaint or count the dead root.
  slotA.remove();
  assert.equal(updateReportComments('spec:spec_dup:dup-ip', [sampleComment()]), false);
  assert.equal(pinA(), '+');
});

test('interactive block selection redraws only the acting mount (two slots, same report)', () => {
  const doc = document();
  const opts = {
    classPrefix: 'slot-asset',
    asset: { asset_id: 'int-dup', asset_key: 'spec:spec_int:int-dup' },
    comments: [],
    actions: {},
  };
  const slotA = renderAsset(doc, 'report', sampleReport(), opts);
  doc.body.appendChild(slotA);
  const slotB = renderAsset(doc, 'report', sampleReport(), opts);
  doc.body.appendChild(slotB);
  const activeIn = (node) => node.querySelector('[data-block-id="para-1"]').classList.contains('is-active');
  assert.equal(activeIn(slotA), false);
  assert.equal(activeIn(slotB), false);

  // Selecting a block in slot A must mark it active / open its composer in slot A
  // ONLY — the pre-fix shared render callback redrew the last-mounted copy (B).
  slotA.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').click();
  assert.equal(activeIn(slotA), true);
  assert.equal(activeIn(slotB), false);
});

test('composer draft is per-mount — a sibling cannot clear a slot\'s unsent draft (two slots)', async () => {
  const doc = document();
  const added = [];
  const opts = {
    classPrefix: 'slot-asset',
    asset: { asset_id: 'draft-dup', asset_key: 'spec:spec_draft:draft-dup' },
    comments: [],
    actions: { addComment: (arg) => { added.push(arg && arg.body); } },
  };
  const slotA = renderAsset(doc, 'report', sampleReport(), opts);
  doc.body.appendChild(slotA);
  const slotB = renderAsset(doc, 'report', sampleReport(), opts);
  doc.body.appendChild(slotB);

  // A opens the composer on para-1 and types an unsent draft.
  slotA.querySelector('[data-block-id="para-1"] .slot-asset-report-comment-pin').click();
  const fieldA = slotA.querySelector('.slot-asset-report-comment-input');
  fieldA.value = 'draft from A';
  fieldA.dispatchEvent(new doc.defaultView.Event('input'));

  // A comment mutation lands and fans out to every open copy.
  updateReportComments('spec:spec_draft:draft-dup', [sampleComment({ comment_id: 'c-x' })]);

  // B must NOT have adopted A's composer (composer/draft state is per-mount); A
  // still holds its unsent draft after the fan-out redraw.
  assert.equal(slotB.querySelector('.slot-asset-report-comment-input'), null);
  assert.equal(slotA.querySelector('.slot-asset-report-comment-input').value, 'draft from A');

  // B closing its own (empty) panel must not touch A's draft.
  slotB.querySelector('.slot-asset-report-panel-close').click();

  // A submits: the draft is intact so addComment fires with it. Pre-fix, B's
  // close cleared the shared draft and this submit was a silent no-op.
  slotA.querySelector('.slot-asset-report-comment-submit').click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(added, ['draft from A']);
});

test('renderAsset raw JSON fallback escapes by textContent', () => {
  const doc = document();
  const node = renderRawJsonFallback(doc, { body: '<script>alert(1)</script>' }, 'slot-asset');

  assert.equal(node.querySelector('script'), null);
  assert.match(node.textContent, /<script>alert\(1\)<\/script>/);
});

test('report CSS carries the DesignSync palette through scoped report tokens', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
  const reportPalette = {
    '--report-base': '#0d1117',
    '--report-surface': '#12171f',
    '--report-ok': '#4ee38a',
    '--report-warn': '#e3b341',
    '--report-stop': '#f0776c',
    '--report-info': '#6aa8ff',
    '--report-db': '#b98cff',
    '--report-story': '#38d0c0',
  };
  for (const [token, color] of Object.entries(reportPalette)) {
    assert.match(css, new RegExp(`${token}:\\s*${color}`, 'i'));
  }
  assert.match(css, /\.slot-asset-layer\.cosmic,\s*\.asset-popout\.cosmic\s*\{/);
  // Report text uses the self-hosted DesignSync faces, not the cosmic aliases.
  assert.match(css, /--report-font-ui: 'Space Grotesk'/);
  assert.match(css, /--report-font-prose: 'IBM Plex Sans'/);
  assert.match(css, /--report-font-mono: 'IBM Plex Mono'/);
  assert.doesNotMatch(css, /--report-font-ui: var\(--cosmic-font-display\)/);
});

test('report design faces are self-hosted (@font-face, no runtime remote fetch)', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'renderer', 'styles.css'), 'utf8');
  assert.match(css, /@font-face/);
  for (const file of ['SpaceGrotesk-600.woff2', 'SpaceGrotesk-400.woff2', 'IBMPlexSans-400.woff2', 'IBMPlexMono-400.woff2']) {
    assert.match(css, new RegExp(`assets/fonts/${file.replace('.', '\\.')}`));
    assert.ok(fs.existsSync(path.join(__dirname, '..', 'renderer', 'assets', 'fonts', file)), `${file} bundled`);
  }
  assert.doesNotMatch(css, /fonts\.googleapis\.com|fonts\.gstatic\.com|https?:\/\/[^)'"]*\.woff2/);
});

test('report maps section status to the mockup tones (dispatched=ok, stalled=stop)', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport({
    sections: [
      { id: 's-d', title: 'Dispatched work', status: 'dispatched', blocks: [{ id: 'b1', type: 'para', runs: [{ type: 'text', text: 'x' }] }] },
      { id: 's-s', title: 'Stalled work', status: 'stalled', blocks: [{ id: 'b2', type: 'para', runs: [{ type: 'text', text: 'y' }] }] },
    ],
  }), { classPrefix: 'slot-asset' });

  const dChip = node.querySelector('#s-d .slot-asset-report-section-head .slot-asset-report-chip-status');
  const sChip = node.querySelector('#s-s .slot-asset-report-section-head .slot-asset-report-chip-status');
  assert.ok(dChip.classList.contains('slot-asset-report-chip-status-ok'));
  assert.ok(sChip.classList.contains('slot-asset-report-chip-status-stop'));
});

test('typed chips colour the leading glyph by kind and keep the label neutral', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport({
    sections: [{ id: 's1', title: 'T', status: 'reference', blocks: [{
      id: 'b1', type: 'para', runs: [
        { type: 'chip', text: 'lead_lock', variant: 'typed', kind: 'epic' },
        { type: 'chip', text: 'promote.py', variant: 'typed', kind: 'file' },
      ],
    }] }],
  }), { classPrefix: 'slot-asset' });

  assert.equal(node.querySelector('.slot-asset-report-chip-glyph-epic').textContent, '◆');
  assert.ok(node.querySelector('.slot-asset-report-chip-glyph-file'));
  const epicChip = node.querySelector('.slot-asset-report-chip-kind-epic');
  assert.match(epicChip.textContent, /lead_lock/);
});

test('report renders filter pills and collapsible sections', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport({
    sections: [
      { id: 's-d', title: 'D', status: 'dispatched', blocks: [{ id: 'b1', type: 'para', runs: [{ type: 'text', text: 'x' }] }] },
      { id: 's-b', title: 'B', status: 'blocked', blocks: [{ id: 'b2', type: 'para', runs: [{ type: 'text', text: 'y' }] }] },
    ],
  }), { classPrefix: 'slot-asset' });

  const pills = Array.from(node.querySelectorAll('.slot-asset-report-pill'));
  assert.equal(pills.length, 3); // All + Dispatched + Blocked

  // Collapse the dispatched section.
  assert.ok(node.querySelector('#s-d .slot-asset-report-section-body'));
  node.querySelector('#s-d .slot-asset-report-section-head').dispatchEvent(new doc.defaultView.MouseEvent('click', { bubbles: true }));
  assert.ok(node.querySelector('#s-d').classList.contains('is-collapsed'));
  assert.equal(node.querySelector('#s-d .slot-asset-report-section-body'), null);

  // Filter to blocked hides the dispatched section entirely.
  Array.from(node.querySelectorAll('.slot-asset-report-pill')).find((p) => /Blocked/.test(p.textContent))
    .dispatchEvent(new doc.defaultView.MouseEvent('click', { bubbles: true }));
  assert.equal(node.querySelector('#s-d'), null);
  assert.ok(node.querySelector('#s-b'));
});

test('report links suppress default navigation (route to OS browser, no blank window)', () => {
  const doc = document();
  const node = renderAsset(doc, 'report', sampleReport({
    sections: [{ id: 's1', title: 'T', status: 'reference', blocks: [{
      id: 'b1', type: 'para', runs: [{ type: 'link', text: 'docs', href: 'https://example.test/x' }],
    }] }],
  }), { classPrefix: 'slot-asset' });

  const link = node.querySelector('a[href="https://example.test/x"]');
  assert.ok(link);
  const evt = new doc.defaultView.MouseEvent('click', { bubbles: true, cancelable: true });
  link.dispatchEvent(evt);
  assert.equal(evt.defaultPrevented, true);
});

test('renderAsset returns a non-throwing node for unknown content types', () => {
  const doc = document();
  const node = renderAsset(doc, 'image', { src: 'later' }, { classPrefix: 'slot-asset' });

  assert.equal(node.tagName, 'DIV');
  assert.equal(node.className, 'slot-asset-asset-empty');
  assert.equal(node.textContent, '');
});
