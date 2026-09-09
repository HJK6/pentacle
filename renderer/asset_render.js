(function(root) {
'use strict';

let reportRender = null;
if (typeof require === 'function') {
  try {
    reportRender = require('./report_render');
  } catch (_) {
    reportRender = null;
  }
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2">$1</a>');
}

function flushParagraph(parts, html) {
  if (!parts.length) return;
  html.push(`<p>${inlineMarkdown(parts.join(' '))}</p>`);
  parts.length = 0;
}

function flushList(items, html) {
  if (!items.length) return;
  html.push('<ul>');
  for (const item of items) html.push(`<li>${inlineMarkdown(item)}</li>`);
  html.push('</ul>');
  items.length = 0;
}

function renderFence(lines, language) {
  const className = language ? ` class="language-${escapeHtml(language)}"` : '';
  return `<pre><code${className}>${escapeHtml(lines.join('\n'))}</code></pre>`;
}

function renderMarkdownHtml(markdown) {
  const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
  const html = [];
  const paragraph = [];
  const list = [];
  let fence = null;

  for (const line of lines) {
    const fenceMatch = line.match(/^```([A-Za-z0-9_-]+)?\s*$/);
    if (fenceMatch) {
      if (fence) {
        html.push(renderFence(fence.lines, fence.language));
        fence = null;
      } else {
        flushParagraph(paragraph, html);
        flushList(list, html);
        fence = { language: fenceMatch[1] || '', lines: [] };
      }
      continue;
    }
    if (fence) {
      fence.lines.push(line);
      continue;
    }
    if (!line.trim()) {
      flushParagraph(paragraph, html);
      flushList(list, html);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      flushParagraph(paragraph, html);
      flushList(list, html);
      html.push(`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph(paragraph, html);
      list.push(bullet[1]);
      continue;
    }
    paragraph.push(line.trim());
  }

  if (fence) html.push(renderFence(fence.lines, fence.language));
  flushParagraph(paragraph, html);
  flushList(list, html);
  return html.join('\n');
}

function numericClassName(classPrefix) {
  const prefix = classPrefix || 'pi-control';
  return prefix === 'pi-control' ? 'numeric' : `${prefix}-numeric`;
}

function renderTablePreview(doc, payload, classPrefix = 'pi-control') {
  const prefix = classPrefix || 'pi-control';
  const columns = Array.isArray(payload && payload.columns) ? payload.columns : [];
  const rows = Array.isArray(payload && payload.rows) ? payload.rows : [];
  const numeric = columns.map((_, index) => {
    const values = rows.map((row) => row && row[index]).filter((value) => value !== null && value !== undefined && value !== '');
    return values.length > 0 && values.every((value) => typeof value === 'number' || /^-?\d+(\.\d+)?$/.test(String(value)));
  });
  const wrap = doc.createElement('div');
  wrap.className = `${prefix}-preview-table-wrap`;
  const table = doc.createElement('table');
  const thead = doc.createElement('thead');
  const headRow = doc.createElement('tr');
  for (const column of columns) {
    const th = doc.createElement('th');
    th.textContent = String(column);
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = doc.createElement('tbody');
  for (const row of rows) {
    const tr = doc.createElement('tr');
    columns.forEach((_, index) => {
      const td = doc.createElement('td');
      td.textContent = row && row[index] !== undefined && row[index] !== null ? String(row[index]) : '';
      if (numeric[index]) td.className = numericClassName(prefix);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  return wrap;
}

function renderRawJsonFallback(doc, payload, classPrefix = 'pi-control', message = 'This desktop cannot render this asset type yet.') {
  const wrap = doc.createElement('div');
  wrap.className = `${classPrefix}-raw-json-fallback`;
  const banner = doc.createElement('div');
  banner.className = `${classPrefix}-report-banner`;
  banner.textContent = message;
  const pre = doc.createElement('pre');
  pre.textContent = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
  wrap.appendChild(banner);
  wrap.appendChild(pre);
  return wrap;
}

function renderAsset(doc, contentType, payload, options = {}) {
  const classPrefix = options.classPrefix || 'pi-control';
  if (contentType === 'markdown') {
    const markdown = doc.createElement('div');
    markdown.className = `${classPrefix}-markdown-preview`;
    markdown.innerHTML = renderMarkdownHtml(payload || '');
    return markdown;
  }
  if (contentType === 'json_table') {
    return renderTablePreview(doc, payload, classPrefix);
  }
  if (contentType === 'report') {
    if (options.reportSupport === false || !reportRender || typeof reportRender.renderReport !== 'function') {
      return renderRawJsonFallback(doc, payload, classPrefix);
    }
    return reportRender.renderReport(doc, payload, options);
  }
  const empty = doc.createElement('div');
  empty.className = `${classPrefix}-asset-empty`;
  return empty;
}

// In-place comment refresh for a mounted report (keyed by asset_key, falling
// back to asset_id — the same scoped identity renderAsset saw in options.asset).
// Returns false when no report with that key is mounted; callers fall back to a
// full re-render.
function updateReportComments(key, comments) {
  if (!reportRender || typeof reportRender.updateComments !== 'function') return false;
  return reportRender.updateComments(key, comments);
}

const api = {
  escapeHtml,
  renderMarkdownHtml,
  renderTablePreview,
  renderRawJsonFallback,
  renderAsset,
  updateReportComments,
};

if (root) root.PentacleAssetRender = api;
if (typeof module !== 'undefined' && module.exports) module.exports = api;

})(typeof window !== 'undefined' ? window : null);
