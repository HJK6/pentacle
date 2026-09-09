(function(root) {
'use strict';

// Section status -> visual tone. Mirrors the public report status metadata
// (public report fixture): dispatched=ok(green), in_progress=warn(amber),
// stalled=stop(red), blocked=stop(red), reference=info(blue).
const STATUS_TONES = {
  dispatched: 'ok',
  in_progress: 'warn',
  stalled: 'stop',
  blocked: 'stop',
  reference: 'info',
};

const STATUS_LABELS = {
  dispatched: 'Dispatched',
  in_progress: 'In progress',
  stalled: 'Stalled',
  blocked: 'Blocked',
  reference: 'Reference',
};

// Filter-pill order; only statuses actually present in the report are shown.
const FILTER_ORDER = ['dispatched', 'in_progress', 'stalled', 'blocked', 'reference'];

const KIND_GLYPHS = {
  epic: '◆',
  branch: '⎇',
  db: '⛁',
  database: '⛁',
  story: '◇',
  file: '⌗',
  generic: '',
};

// Per-asset UI state (filter, collapse, selection, draft, comments), keyed by the
// scoped render identity (asset_key when present, else asset_id) so report- and
// session-scoped assets sharing an asset_id never share state. Metadata changes
// (republish, review status) still re-render the whole asset — this store keeps
// the reader place across those. Comment mutations instead update in place
// via updateComments(), which redraws synchronously inside the mounted root so
// scroll position and the surrounding DOM survive.
const uiStore = new Map();

function updateComments(key, comments) {
  const shared = uiStore.get(String(key == null ? '' : key));
  if (!shared) return false;
  shared.comments = Array.isArray(comments) ? comments : [];
  // Redraw EVERY currently-mounted copy of this scoped report. A report-scoped
  // report can be open in multiple slots at once (two sessions carrying the report), so a single render callback would leave every copy but the last
  // stale. Each mount redraws from the shared comment data but its OWN composer
  // state. Only DOM-connected roots are live: a mount torn down by a full
  // re-render is detached, so it is pruned here and never counted or redrawn —
  // and the caller falls back to a full render when nothing is mounted.
  const live = (Array.isArray(shared.renderers) ? shared.renderers : [])
    .filter((entry) => entry.root && entry.root.isConnected);
  shared.renderers = live;
  for (const entry of live) entry.draw();
  return live.length > 0;
}

function safeText(value) {
  return String(value == null ? '' : value);
}

function isHttpUrl(value) {
  try {
    const url = new URL(String(value || ''));
    return url.protocol === 'http:' || url.protocol === 'https:';
  } catch (_) {
    return false;
  }
}

// Route external links through the OS browser instead of a blank Electron window
// (fixes public-link handling). Prefers the preload bridge; degrades to window.open, then no-op.
function openExternal(href) {
  if (!isHttpUrl(href)) return false;
  try {
    if (root && root.cc && typeof root.cc.openExternal === 'function') {
      root.cc.openExternal(href);
      return true;
    }
  } catch (_) { /* fall through */ }
  try {
    if (root && typeof root.open === 'function') {
      root.open(href, '_blank', 'noopener,noreferrer');
      return true;
    }
  } catch (_) { /* fall through */ }
  return false;
}

function bindExternal(node, href) {
  node.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    openExternal(href);
  });
}

function appendText(doc, parent, value) {
  parent.appendChild(doc.createTextNode(safeText(value)));
}

function parsePayload(payload) {
  if (typeof payload !== 'string') return { report: payload && typeof payload === 'object' ? payload : null, raw: payload };
  try {
    return { report: JSON.parse(payload), raw: payload };
  } catch (_) {
    return { report: null, raw: payload, parseError: true };
  }
}

function toneForStatus(status) {
  return STATUS_TONES[status] || 'info';
}

function toneHexVar(tone) {
  return `var(--report-${tone === 'ok' ? 'ok' : tone === 'warn' ? 'warn' : tone === 'stop' ? 'stop' : 'info'})`;
}

function makeChip(doc, run, prefix) {
  const variant = safeText(run.variant || (run.status ? 'status' : run.kind ? 'typed' : 'plain'));
  const kind = safeText(run.kind || 'generic');
  const status = safeText(run.status || 'ok');
  const text = safeText(run.text || run.chip);
  const isLink = variant === 'link';
  const href = run.href;
  const linkable = isLink && isHttpUrl(href);
  const node = linkable ? doc.createElement('a') : doc.createElement('span');
  node.className = `${prefix}-report-chip ${prefix}-report-chip-${variant}`;
  if (variant === 'typed' && kind) node.classList.add(`${prefix}-report-chip-kind-${kind}`);
  if (variant === 'status' && status) node.classList.add(`${prefix}-report-chip-status-${status}`);
  if (node.tagName === 'A') {
    node.href = href;
    node.target = '_blank';
    node.rel = 'noreferrer';
    bindExternal(node, href);
  }
  const glyph = KIND_GLYPHS[kind];
  // Typed chips: colour only the leading glyph by kind; the label stays neutral.
  if (variant === 'typed' && glyph) {
    const lead = doc.createElement('span');
    lead.className = `${prefix}-report-chip-glyph ${prefix}-report-chip-glyph-${kind}`;
    lead.textContent = glyph;
    node.appendChild(lead);
  }
  if (variant === 'status') {
    const dot = doc.createElement('span');
    dot.className = `${prefix}-report-chip-dot`;
    node.appendChild(dot);
  }
  appendText(doc, node, text);
  if (isLink) {
    const mark = doc.createElement('span');
    mark.className = `${prefix}-report-chip-ext`;
    mark.textContent = '↗';
    node.appendChild(mark);
  }
  return node;
}

function appendRuns(doc, parent, runs, prefix, placeholders) {
  if (!Array.isArray(runs)) {
    const placeholder = doc.createElement('span');
    placeholder.className = `${prefix}-report-placeholder`;
    placeholder.textContent = 'Unsupported run payload';
    parent.appendChild(placeholder);
    placeholders.count += 1;
    return;
  }
  runs.forEach((run, index) => {
    if (typeof run === 'string') {
      appendText(doc, parent, run);
      return;
    }
    if (!run || typeof run !== 'object') {
      const placeholder = doc.createElement('span');
      placeholder.className = `${prefix}-report-placeholder`;
      placeholder.textContent = `Unsupported run ${index + 1}`;
      parent.appendChild(placeholder);
      placeholders.count += 1;
      return;
    }
    const type = run.type || (run.chip ? 'chip' : 'text');
    if (type === 'text') {
      appendText(doc, parent, run.text);
    } else if (type === 'code') {
      const code = doc.createElement('code');
      code.className = `${prefix}-report-inline-code`;
      code.textContent = safeText(run.text);
      parent.appendChild(code);
    } else if (type === 'link') {
      if (isHttpUrl(run.href)) {
        const link = doc.createElement('a');
        link.className = `${prefix}-report-link`;
        link.href = run.href;
        link.target = '_blank';
        link.rel = 'noreferrer';
        link.textContent = safeText(run.text);
        bindExternal(link, run.href);
        parent.appendChild(link);
      } else {
        appendText(doc, parent, run.text);
      }
    } else if (type === 'chip') {
      parent.appendChild(makeChip(doc, run, prefix));
    } else {
      const placeholder = doc.createElement('span');
      placeholder.className = `${prefix}-report-placeholder`;
      placeholder.textContent = `Unsupported run type: ${safeText(type)}`;
      parent.appendChild(placeholder);
      placeholders.count += 1;
    }
  });
}

function runsText(runs) {
  if (typeof runs === 'string') return runs;
  if (!Array.isArray(runs)) return '';
  return runs.map((run) => {
    if (typeof run === 'string') return run;
    if (!run || typeof run !== 'object') return '';
    return safeText(run.text || run.chip);
  }).join('');
}

function blockExcerpt(block) {
  if (!block || typeof block !== 'object') return '';
  if (block.type === 'para' || block.type === 'callout') return runsText(block.runs || []);
  if (block.type === 'list') return (block.items || []).map(runsText).join('\n');
  if (block.type === 'table') {
    const lines = Array.isArray(block.columns) ? [block.columns.join(' | ')] : [];
    for (const row of block.rows || []) {
      if (Array.isArray(row)) lines.push(row.map(runsText).join(' | '));
    }
    return lines.join('\n');
  }
  return '';
}

function commentKey(sectionId, blockId) {
  return `${sectionId || ''}\u0000${blockId || ''}`;
}

function commentsByAnchor(comments) {
  const map = new Map();
  for (const comment of Array.isArray(comments) ? comments : []) {
    if (!comment || typeof comment !== 'object') continue;
    const key = commentKey(comment.section_id, comment.block_id);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(comment);
  }
  return map;
}

function sectionNum(index) {
  return String(index + 1).padStart(2, '0');
}

function renderBlockBody(doc, block, prefix, placeholders) {
  const body = doc.createElement('div');
  body.className = `${prefix}-report-block-body`;
  if (!block || typeof block !== 'object') {
    body.textContent = 'Unsupported block payload';
    body.classList.add(`${prefix}-report-placeholder`);
    placeholders.count += 1;
    return body;
  }
  if (block.type === 'para') {
    const p = doc.createElement('p');
    appendRuns(doc, p, block.runs, prefix, placeholders);
    body.appendChild(p);
  } else if (block.type === 'list') {
    const list = doc.createElement('div');
    list.className = `${prefix}-report-list ${block.ordered ? 'is-ordered' : 'is-unordered'}`;
    const items = Array.isArray(block.items) ? block.items : [];
    items.forEach((item, index) => {
      const row = doc.createElement('div');
      row.className = `${prefix}-report-list-item`;
      const marker = doc.createElement('span');
      marker.className = `${prefix}-report-list-marker`;
      marker.textContent = block.ordered ? String(index + 1) : '●';
      const content = doc.createElement('div');
      content.className = `${prefix}-report-list-content`;
      appendRuns(doc, content, item, prefix, placeholders);
      row.appendChild(marker);
      row.appendChild(content);
      list.appendChild(row);
    });
    body.appendChild(list);
  } else if (block.type === 'table') {
    const scroller = doc.createElement('div');
    scroller.className = `${prefix}-report-table-wrap`;
    const table = doc.createElement('table');
    const thead = doc.createElement('thead');
    const headRow = doc.createElement('tr');
    for (const column of Array.isArray(block.columns) ? block.columns : []) {
      const th = doc.createElement('th');
      th.textContent = safeText(column);
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = doc.createElement('tbody');
    for (const row of Array.isArray(block.rows) ? block.rows : []) {
      const tr = doc.createElement('tr');
      const cells = Array.isArray(row) ? row : [];
      cells.forEach((cell, cellIndex) => {
        const td = doc.createElement('td');
        if (cellIndex === 0) td.className = `${prefix}-report-table-lead`;
        appendRuns(doc, td, cell, prefix, placeholders);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    scroller.appendChild(table);
    body.appendChild(scroller);
  } else if (block.type === 'callout') {
    const kind = safeText(block.kind || 'info');
    const callout = doc.createElement('div');
    callout.className = `${prefix}-report-callout ${prefix}-report-callout-${kind}`;
    const title = doc.createElement('div');
    title.className = `${prefix}-report-callout-title`;
    const icon = doc.createElement('span');
    icon.className = `${prefix}-report-callout-icon`;
    icon.textContent = kind === 'warn' ? '⚠' : 'ℹ';
    title.appendChild(icon);
    title.appendChild(doc.createTextNode(safeText(block.title || (kind === 'warn' ? 'Note' : 'Info'))));
    callout.appendChild(title);
    const content = doc.createElement('div');
    content.className = `${prefix}-report-callout-body`;
    appendRuns(doc, content, block.runs, prefix, placeholders);
    callout.appendChild(content);
    body.appendChild(callout);
  } else {
    body.classList.add(`${prefix}-report-placeholder`);
    body.textContent = `Unsupported block type: ${safeText(block.type)}`;
    placeholders.count += 1;
  }
  return body;
}

function renderBlock(doc, block, section, context) {
  const { prefix, placeholders, anchorComments, knownAnchors, state } = context;
  const blockId = safeText(block && block.id);
  const sectionId = safeText(section && section.id);
  const key = commentKey(sectionId, blockId);
  const comments = anchorComments.get(key) || [];
  knownAnchors.add(key);

  const wrap = doc.createElement('div');
  wrap.className = `${prefix}-report-block`;
  wrap.dataset.sectionId = sectionId;
  wrap.dataset.blockId = blockId;
  const unresolved = comments.filter((comment) => !comment.resolved).length;
  if (unresolved) wrap.classList.add('has-unresolved-comments');
  if (comments.length) wrap.classList.add('has-comments');
  if (state.activeKey === key) wrap.classList.add('is-active');

  // Hover-reveal comment affordance; commented blocks keep a visible count marker.
  const pin = doc.createElement('button');
  pin.type = 'button';
  pin.className = `${prefix}-report-comment-pin`;
  pin.title = comments.length ? `${comments.length} comment${comments.length === 1 ? '' : 's'}` : 'Add comment';
  pin.textContent = comments.length ? String(comments.length) : '+';
  pin.addEventListener('click', (event) => {
    event.stopPropagation();
    context.selectBlock(section, block, key);
  });
  wrap.appendChild(pin);

  const body = renderBlockBody(doc, block, prefix, placeholders);
  body.addEventListener('click', () => context.selectBlock(section, block, key));
  wrap.appendChild(body);
  return wrap;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
const ICON_PATHS = {
  edit: 'M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z',
  delete: 'M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 4v6m4-6v6',
  send: 'M22 2 11 13M22 2l-7 20-4-9-9-4z',
  check: 'M20 6 9 17l-5-5',
};

function svgIcon(doc, name) {
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '15');
  svg.setAttribute('height', '15');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const path = doc.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICON_PATHS[name] || '');
  svg.appendChild(path);
  return svg;
}

function iconButton(doc, prefix, name, label, onClick, extraClass) {
  const btn = doc.createElement('button');
  btn.type = 'button';
  btn.className = `${prefix}-report-icon-btn${extraClass ? ' ' + extraClass : ''}`;
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.appendChild(svgIcon(doc, name));
  btn.addEventListener('click', onClick);
  return btn;
}

function formatTs(value) {
  const raw = safeText(value);
  if (!raw) return '';
  const d = new Date(raw);
  if (isNaN(d.getTime())) return raw;
  try {
    return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch (_) {
    return raw;
  }
}

function renderCommentCard(doc, comment, context, opts) {
  const { prefix, state, actions } = context;
  const card = doc.createElement('div');
  card.className = `${prefix}-report-comment-card`;

  // Feedback comments show only the comment text + a timestamp, plus edit/delete.
  const head = doc.createElement('div');
  head.className = `${prefix}-report-comment-head`;
  const time = doc.createElement('span');
  time.className = `${prefix}-report-comment-time`;
  time.textContent = formatTs(comment.created_at);
  head.appendChild(time);

  const row = doc.createElement('div');
  row.className = `${prefix}-report-comment-actions`;
  row.appendChild(iconButton(doc, prefix, 'edit', 'Edit', () => {
    state.panelOpen = true;
    state.activeKey = commentKey(comment.section_id, comment.block_id);
    state.activeBlock = {
      section_id: safeText(comment.section_id),
      block_id: safeText(comment.block_id),
      run_index: Number.isInteger(comment.run_index) ? comment.run_index : null,
      excerpt: safeText(comment.excerpt),
    };
    state.activePreview = state.activeBlock.excerpt;
    state.editCommentId = comment.comment_id;
    state.draft = safeText(comment.body);
    context.render();
  }));
  row.appendChild(iconButton(doc, prefix, 'delete', 'Delete', () => actions.deleteComment(comment.comment_id)));
  head.appendChild(row);
  card.appendChild(head);

  // Unanchored comments (block removed on re-publish) keep their excerpt as the
  // only remaining context, since there is no block thread to head it.
  if (opts && opts.showExcerpt && comment.excerpt) {
    const excerpt = doc.createElement('div');
    excerpt.className = `${prefix}-report-comment-excerpt`;
    excerpt.textContent = safeText(comment.excerpt);
    card.appendChild(excerpt);
  }

  const body = doc.createElement('div');
  body.className = `${prefix}-report-comment-body`;
  body.textContent = safeText(comment.body);
  card.appendChild(body);
  return card;
}

function renderToolbar(doc, context) {
  const { prefix, report, state, actions, comments } = context;
  const toolbar = doc.createElement('div');
  toolbar.className = `${prefix}-report-toolbar`;

  const left = doc.createElement('div');
  left.className = `${prefix}-report-toolbar-left`;
  const mark = doc.createElement('span');
  mark.className = `${prefix}-report-toolbar-mark`;
  mark.textContent = '◆';
  const label = doc.createElement('span');
  label.className = `${prefix}-report-toolbar-label`;
  label.textContent = 'Report';
  left.appendChild(mark);
  left.appendChild(label);
  toolbar.appendChild(left);

  const spacer = doc.createElement('div');
  spacer.className = `${prefix}-report-toolbar-spacer`;
  toolbar.appendChild(spacer);

  // Feedback model: no approve / review status. "Send to chat" is the one action
  // that passes the reader comments to the producing process.
  const review = doc.createElement('div');
  review.className = `${prefix}-report-review-row`;

  const collapse = doc.createElement('button');
  collapse.type = 'button';
  collapse.className = `${prefix}-report-toolbar-btn`;
  collapse.textContent = state.allOpen ? 'Collapse all' : 'Expand all';
  collapse.addEventListener('click', () => {
    state.allOpen = !state.allOpen;
    state.openSections = {};
    for (const section of Array.isArray(report.sections) ? report.sections : []) {
      state.openSections[safeText(section.id)] = state.allOpen;
    }
    context.render();
  });
  review.appendChild(collapse);

  const send = doc.createElement('button');
  send.type = 'button';
  send.className = `${prefix}-report-toolbar-btn ${prefix}-report-send-btn`;
  send.textContent = state.sending ? 'Sending…' : 'Send to chat';
  send.disabled = !!state.sending;
  send.addEventListener('click', () => actions.sendToChat());
  review.appendChild(send);

  toolbar.appendChild(review);
  return toolbar;
}

function renderFilterPills(doc, context, counts) {
  const { prefix, state } = context;
  const row = doc.createElement('div');
  row.className = `${prefix}-report-pills`;
  const defs = [['all', 'All']].concat(FILTER_ORDER.filter((k) => counts[k]).map((k) => [k, STATUS_LABELS[k] || k]));
  for (const [key, label] of defs) {
    const pill = doc.createElement('button');
    pill.type = 'button';
    pill.className = `${prefix}-report-pill`;
    if (key !== 'all') pill.classList.add(`${prefix}-report-status-${key}`);
    if (state.filter === key) pill.classList.add('is-active');
    const text = doc.createElement('span');
    text.textContent = label;
    pill.appendChild(text);
    const count = doc.createElement('span');
    count.className = `${prefix}-report-pill-count`;
    count.textContent = String(key === 'all' ? (counts.all || 0) : (counts[key] || 0));
    pill.appendChild(count);
    pill.addEventListener('click', () => { state.filter = key; context.render(); });
    row.appendChild(pill);
  }
  return row;
}

function renderToc(doc, context, sections) {
  const { prefix, state } = context;
  const wrap = doc.createElement('details');
  wrap.className = `${prefix}-report-toc-card`;
  wrap.open = state.tocOpen !== false;
  wrap.addEventListener('toggle', () => { state.tocOpen = wrap.open; });
  const summary = doc.createElement('summary');
  summary.className = `${prefix}-report-toc-summary`;
  const cap = doc.createElement('span');
  cap.className = `${prefix}-report-toc-cap`;
  cap.textContent = 'Contents';
  const badge = doc.createElement('span');
  badge.className = `${prefix}-report-toc-badge`;
  badge.textContent = String(sections.length);
  summary.appendChild(cap);
  summary.appendChild(badge);
  wrap.appendChild(summary);

  const nav = doc.createElement('nav');
  nav.className = `${prefix}-report-toc`;
  sections.forEach((section, index) => {
    const item = doc.createElement('a');
    item.href = `#${encodeURIComponent(safeText(section.id))}`;
    item.className = `${prefix}-report-toc-item`;
    const num = doc.createElement('span');
    num.className = `${prefix}-report-toc-num`;
    num.textContent = sectionNum(index);
    const title = doc.createElement('span');
    title.className = `${prefix}-report-toc-title`;
    title.textContent = safeText(section.title);
    const dot = doc.createElement('span');
    dot.className = `${prefix}-report-toc-dot ${prefix}-report-status-${safeText(section.status)}`;
    item.appendChild(num);
    item.appendChild(title);
    item.appendChild(dot);
    nav.appendChild(item);
  });
  wrap.appendChild(nav);
  return wrap;
}

function renderPanel(doc, context) {
  const { prefix, comments, anchorComments, knownAnchors, state } = context;
  const panel = doc.createElement('aside');
  panel.className = `${prefix}-report-panel`;
  if (state.panelOpen) panel.classList.add('is-open');

  const head = doc.createElement('div');
  head.className = `${prefix}-report-panel-head`;
  const heading = doc.createElement('div');
  heading.className = `${prefix}-report-comment-heading`;
  heading.textContent = state.activeBlock ? 'Comment thread' : 'Comments';
  head.appendChild(heading);
  const close = doc.createElement('button');
  close.type = 'button';
  close.className = `${prefix}-report-panel-close`;
  close.textContent = '✕';
  close.addEventListener('click', () => {
    state.panelOpen = false;
    state.activeBlock = null;
    state.activeKey = null;
    state.editCommentId = null;
    state.draft = '';
    context.render();
  });
  head.appendChild(close);
  panel.appendChild(head);

  // Action errors render at report root (always visible); the panel may be closed.

  const body = doc.createElement('div');
  body.className = `${prefix}-report-panel-body`;

  if (state.activeBlock) {
    const selected = doc.createElement('div');
    selected.className = `${prefix}-report-selected`;
    selected.textContent = state.activePreview || 'Selected block';
    body.appendChild(selected);

    const compose = doc.createElement('div');
    compose.className = `${prefix}-report-compose`;
    const field = doc.createElement('textarea');
    field.className = `${prefix}-report-comment-input`;
    field.placeholder = state.editCommentId ? 'Edit comment…' : 'Add a comment…';
    field.value = state.draft || '';
    field.addEventListener('input', () => { state.draft = field.value; });
    const submitLabel = state.editCommentId ? 'Save comment' : 'Add comment';
    const submit = iconButton(doc, prefix, state.editCommentId ? 'check' : 'send', submitLabel, () => {
      const value = safeText(state.draft).trim();
      if (!value) return;
      if (state.editCommentId) context.actions.editComment(state.editCommentId, value);
      else context.actions.addComment(state.activeBlock, value);
    }, `${prefix}-report-comment-submit`);
    submit.disabled = !!state.sending;
    compose.appendChild(field);
    compose.appendChild(submit);
    body.appendChild(compose);

    const activeComments = state.activeKey ? (anchorComments.get(state.activeKey) || []) : [];
    const list = doc.createElement('div');
    list.className = `${prefix}-report-comment-list`;
    for (const comment of activeComments) list.appendChild(renderCommentCard(doc, comment, context));
    body.appendChild(list);
  }

  // Orphaned comments (block removed on re-publish) are the only overview kept —
  // they have no pin to reach them. Rendered only when present, no empty state.
  const unanchored = comments.filter((comment) => !knownAnchors.has(commentKey(comment.section_id, comment.block_id)));
  if (unanchored.length) {
    const unanchoredHeading = doc.createElement('div');
    unanchoredHeading.className = `${prefix}-report-comment-heading`;
    unanchoredHeading.textContent = 'Unanchored';
    body.appendChild(unanchoredHeading);
    const bucket = doc.createElement('div');
    bucket.className = `${prefix}-report-unanchored`;
    for (const comment of unanchored) bucket.appendChild(renderCommentCard(doc, comment, context, { showExcerpt: true }));
    body.appendChild(bucket);
  }

  panel.appendChild(body);
  return panel;
}

function renderReport(doc, payload, options = {}) {
  const prefix = options.classPrefix || 'pi-control';
  const parsed = parsePayload(payload);
  const report = parsed.report;
  const root_ = doc.createElement('div');
  root_.className = `${prefix}-report`;

  // Persist UI state per asset id so filter/collapse/selection survive the full
  // app.js re-render after every comment/review mutation. Reports without asset
  // metadata (unit tests, transient previews) get fresh ephemeral state so they
  // never share a slot in the module store.
  const storeKey = safeText(options.asset && (options.asset.asset_key || options.asset.asset_id));
  // Shared per-key store: comment data + view preferences (filter/collapse) + the
  // live-mount registry. It persists across a single slot's full re-render so the
  // user keeps their place, and it is shared across simultaneous mounts of the
  // same scoped key. Reports without asset metadata (unit tests, transient
  // previews) get a fresh ephemeral store so they never share a slot.
  let shared = storeKey ? uiStore.get(storeKey) : null;
  if (!shared) {
    shared = { comments: [], filter: 'all', openSections: {}, allOpen: true, tocOpen: true, renderers: [] };
    if (storeKey) uiStore.set(storeKey, shared);
  }
  // Re-seed comment data from every full render: a republish is a fresh document
  // whose comments were cleared server-side, so a persisted override must not
  // survive it. updateComments() replaces this between full renders.
  shared.comments = Array.isArray(options.comments) ? options.comments : [];

  // Per-mount transient state: composer/panel/draft/selection/action feedback.
  // These MUST be per-mount — a comment fan-out redraw or a panel close in one
  // open slot must not replay into, or clear, another slot's visible unsent draft
  // (that would make the other slot's submit a silent no-op — data loss). The
  // shared fields below delegate to `shared` so every live copy sees the same
  // comment data and view prefs, and prefs survive a single slot's re-render.
  const state = {
    panelOpen: false,
    activeKey: null,
    activeBlock: null,
    activePreview: '',
    draft: '',
    editCommentId: null,
    error: '',
    notice: '',
    sending: false,
  };
  for (const field of ['comments', 'filter', 'openSections', 'allOpen', 'tocOpen']) {
    Object.defineProperty(state, field, {
      get: () => shared[field],
      set: (value) => { shared[field] = value; },
      enumerable: true,
      configurable: true,
    });
  }
  // Notices persist until the next action (which clears them) rather than on a
  // timer — a timer race was hiding the send-to-chat confirmation.
  function flashNotice(text) { state.notice = text; }
  // Interactive redraws target THIS mount's draw (a report can be mounted in
  // several slots at once; a shared render callback would redraw the wrong copy).
  const selectBlock = (section, block, key) => {
    state.panelOpen = true;
    state.activeKey = key;
    state.activeBlock = {
      section_id: safeText(section.id),
      block_id: safeText(block.id),
      run_index: null,
      excerpt: blockExcerpt(block),
    };
    state.activePreview = state.activeBlock.excerpt;
    state.editCommentId = null;
    state.draft = '';
    draw();
  };

  const actions = options.actions || {};
  function runAction(fn, onSuccess) {
    // Re-entry guard: prevents a double-click on Send/Add from firing twice while
    // the RPC is in flight.
    if (state.sending) return Promise.resolve();
    state.error = '';
    state.notice = '';
    state.sending = true;
    draw();
    return Promise.resolve()
      .then(fn)
      .then(() => {
        state.sending = false;
        if (typeof onSuccess === 'function') onSuccess();
        draw();
      })
      .catch((error) => {
        state.sending = false;
        state.error = safeText(error && (error.message || error.error) || error || 'Asset action failed');
        draw();
      });
  }
  const safeActions = {
    addComment(anchor, body) {
      return runAction(
        () => actions.addComment?.({ ...anchor, body }),
        () => { state.draft = ''; flashNotice('Comment added.'); },
      );
    },
    editComment(commentId, body) {
      return runAction(
        () => actions.editComment?.(commentId, body),
        () => { state.editCommentId = null; state.draft = ''; },
      );
    },
    deleteComment(commentId) { return runAction(() => actions.deleteComment?.(commentId)); },
    resolveComment(commentId, resolved) { return runAction(() => actions.resolveComment?.(commentId, resolved)); },
    sendToChat() {
      return runAction(() => actions.sendToChat?.(), () => flashNotice('Sent to chat — the agent was notified.'));
    },
    setReviewStatus(status) {
      return runAction(() => actions.setReviewStatus?.(status), () => flashNotice(status === 'approved' ? 'Approved — the agent was notified.' : 'Review status updated.'));
    },
  };

  function draw() {
    root_.innerHTML = '';
    const comments = Array.isArray(state.comments) ? state.comments : [];
    const anchorComments = commentsByAnchor(comments);
    const knownAnchors = new Set();
    const placeholders = { count: 0 };
    const context = {
      prefix,
      report,
      comments,
      anchorComments,
      knownAnchors,
      placeholders,
      state,
      actions: safeActions,
      // Per-mount redraw + block selection: builders/handlers redraw THIS copy,
      // never a sibling mount sharing the scoped key.
      render: draw,
      selectBlock,
      reviewStatus: options.reviewStatus || options.asset?.review_status || 'pending_review',
    };

    if (parsed.parseError || !report || typeof report !== 'object') {
      const fallback = doc.createElement('div');
      fallback.className = `${prefix}-raw-json-fallback`;
      const banner = doc.createElement('div');
      banner.className = `${prefix}-report-banner`;
      banner.textContent = 'Report JSON could not be parsed. Showing raw payload.';
      const pre = doc.createElement('pre');
      pre.textContent = typeof parsed.raw === 'string' ? parsed.raw : JSON.stringify(parsed.raw, null, 2);
      fallback.appendChild(banner);
      fallback.appendChild(pre);
      root_.appendChild(fallback);
      return;
    }

    if (report.schema_version !== 1) {
      const banner = doc.createElement('div');
      banner.className = `${prefix}-report-banner`;
      banner.textContent = `Report schema ${safeText(report.schema_version)} is not supported by this desktop.`;
      root_.appendChild(banner);
    }

    const allSections = Array.isArray(report.sections) ? report.sections : [];
    // Every rendered section defaults to open unless the user collapsed it.
    for (const section of allSections) {
      const id = safeText(section.id);
      if (!(id in state.openSections)) state.openSections[id] = true;
    }
    const counts = { all: allSections.length };
    for (const section of allSections) {
      const key = safeText(section.status);
      counts[key] = (counts[key] || 0) + 1;
    }
    const visible = allSections.filter((section) => state.filter === 'all' || section.status === state.filter);

    // Anchor set covers ALL blocks (not just visible/expanded ones) so filtering
    // or collapsing a section never mislabels its comments as unanchored.
    for (const section of allSections) {
      for (const block of Array.isArray(section.blocks) ? section.blocks : []) {
        knownAnchors.add(commentKey(safeText(section.id), safeText(block && block.id)));
      }
    }

    root_.appendChild(renderToolbar(doc, context));
    if (state.error) {
      const error = doc.createElement('div');
      error.className = `${prefix}-report-action-error`;
      error.textContent = state.error;
      root_.appendChild(error);
    }
    if (state.notice) {
      const notice = doc.createElement('div');
      notice.className = `${prefix}-report-action-notice`;
      const tick = svgIcon(doc, 'check');
      tick.classList.add(`${prefix}-report-notice-icon`);
      notice.appendChild(tick);
      notice.appendChild(doc.createTextNode(state.notice));
      root_.appendChild(notice);
    }

    const shell = doc.createElement('div');
    shell.className = `${prefix}-report-shell`;
    const main = doc.createElement('main');
    main.className = `${prefix}-report-main`;

    const header = doc.createElement('header');
    header.className = `${prefix}-report-header`;
    const eyebrow = doc.createElement('div');
    eyebrow.className = `${prefix}-report-eyebrow`;
    eyebrow.textContent = 'Run report';
    header.appendChild(eyebrow);
    const title = doc.createElement('h1');
    title.textContent = safeText(report.title || 'Untitled report');
    header.appendChild(title);
    main.appendChild(header);

    main.appendChild(renderFilterPills(doc, context, counts));
    if (visible.length) main.appendChild(renderToc(doc, context, visible));

    for (const [index, section] of visible.entries()) {
      const open = state.openSections[safeText(section.id)] !== false;
      const sectionNode = doc.createElement('section');
      sectionNode.className = `${prefix}-report-section ${prefix}-report-status-${safeText(section.status)}`;
      if (!open) sectionNode.classList.add('is-collapsed');
      sectionNode.id = safeText(section.id);

      const sectionHead = doc.createElement('div');
      sectionHead.className = `${prefix}-report-section-head`;
      const chevron = doc.createElement('span');
      chevron.className = `${prefix}-report-chevron`;
      chevron.textContent = '▸';
      const num = doc.createElement('span');
      num.className = `${prefix}-report-section-num`;
      num.textContent = sectionNum(index);
      const h2 = doc.createElement('h2');
      h2.textContent = safeText(section.title);
      const statusChip = makeChip(doc, {
        type: 'chip',
        text: STATUS_LABELS[section.status] || section.status || 'Unknown',
        variant: 'status',
        status: toneForStatus(section.status),
      }, prefix);
      sectionHead.appendChild(chevron);
      sectionHead.appendChild(num);
      sectionHead.appendChild(h2);
      sectionHead.appendChild(statusChip);
      sectionHead.addEventListener('click', () => {
        const id = safeText(section.id);
        state.openSections[id] = state.openSections[id] === false;
        context.render();
      });
      sectionNode.appendChild(sectionHead);

      if (open) {
        const bodyWrap = doc.createElement('div');
        bodyWrap.className = `${prefix}-report-section-body`;
        for (const block of Array.isArray(section.blocks) ? section.blocks : []) {
          bodyWrap.appendChild(renderBlock(doc, block, section, context));
        }
        sectionNode.appendChild(bodyWrap);
      }
      main.appendChild(sectionNode);
    }

    if (!visible.length) {
      const empty = doc.createElement('div');
      empty.className = `${prefix}-report-no-results`;
      empty.textContent = 'No sections match this filter.';
      main.appendChild(empty);
    }

    shell.appendChild(main);
    // Panel is always in the DOM (holds the unresolved/unanchored lists); it is a
    // floating overlay revealed on demand via the `is-open` class.
    shell.appendChild(renderPanel(doc, context));
    root_.appendChild(shell);
  }

  // Register this mount in the per-key live-mount registry so a comment update
  // fans out to EVERY open copy of the report, not just the last rendered (the
  // same report-scoped report can be mounted in multiple slots at once). Interactive
  // redraws stay mount-local (via context.render/selectBlock above); this registry
  // is only the comment-update fan-out path. Prune roots torn down by a prior full
  // re-render (detached from the DOM) so the registry can't retain or repaint dead
  // nodes; updateComments re-prunes on every call. Callers append the returned root
  // synchronously, so a still-live sibling is connected before the next registers.
  shared.renderers = (Array.isArray(shared.renderers) ? shared.renderers : [])
    .filter((entry) => entry.root !== root_ && entry.root && entry.root.isConnected);
  shared.renderers.push({ root: root_, draw });
  draw();
  return root_;
}

const api = {
  renderReport,
  updateComments,
  blockExcerpt,
  isHttpUrl,
};

if (root) root.PentacleReportRender = api;
if (typeof module !== 'undefined' && module.exports) module.exports = api;

})(typeof window !== 'undefined' ? window : null);
