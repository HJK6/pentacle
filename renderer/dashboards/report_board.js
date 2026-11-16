// Generic, one-window report viewer for validated dashboard catalog entries.
(function (root) {
  'use strict';

  function keyFormatWidth(format) {
    const catalog = (root && root.DashboardCatalogLoader)
      || (typeof require === 'function' && require('./catalog_loader'));
    if (!catalog) throw new Error('catalog grammar validator is unavailable');
    return catalog.keyFormatWidth(format);
  }

  function reportIdGrammar(prefix, keyFormat, revWidth) {
    keyFormatWidth(keyFormat);
    if (!Number.isInteger(revWidth) || revWidth < 0 || revWidth > 3) throw new Error('rev_width must be an integer 0-3');
    const escaped = String(prefix).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const suffix = revWidth ? `(-r(?!0+$)[0-9]{${revWidth}})?` : '';
    // JavaScript's $ can match before a final newline; require the true end,
    // matching the daemon reader's full-match semantics.
    return new RegExp(`^${escaped}(${keyFormat})${suffix}$(?![\\s\\S])`);
  }

  function reportRequest(report) {
    const request = {
      spec_id: report.spec_id,
      asset_id_prefix: report.asset_id_prefix,
      sort: 'asset_id_desc',
      limit: Math.min(4 * report.history_limit, 400),
    };
    if (report.producer_stream_id !== undefined) request.producer = report.producer_stream_id;
    return request;
  }

  // The daemon filters and sorts before limiting. Do not re-sort this window,
  // especially by updated_at, or filter client-side and pretend it is complete.
  function selectReports(report, rows) {
    if (!Array.isArray(rows)) throw new Error('report list is missing assets');
    const window = reportRequest(report).limit;
    const grammar = reportIdGrammar(report.asset_id_prefix, report.key_format, report.rev_width);
    const parsed = [];
    for (const row of rows) {
      const id = row && row.asset_id;
      if (typeof id !== 'string' || !id.startsWith(report.asset_id_prefix)) {
        throw new Error('report list returned an asset outside its namespace');
      }
      const match = grammar.exec(id);
      if (!match) {
        return {
          state: 'error',
          unexpected_id: id,
          latest: null,
          notice: `unsupported asset id ${id} in namespace ${report.asset_id_prefix}`,
        };
      }
      parsed.push({ row, key: match[1], rev: match[2] ? Number(match[2].slice(2)) : 0 });
    }
    if (!rows.length) return { state: 'empty', latest: null, keys: [], entries: [] };
    if (rows.length > window) throw new Error('report list exceeded its requested window');
    const byKey = new Map();
    for (const item of parsed) {
      if (!byKey.has(item.key) || byKey.get(item.key).rev < item.rev) byKey.set(item.key, item);
    }
    const entries = Array.from(byKey.values()).slice(0, report.history_limit);
    const windowFull = rows.length === window;
    const truncated = windowFull && entries.length < report.history_limit;
    return {
      state: windowFull ? 'partial' : 'complete',
      latest: rows[0].asset_id,
      latestEntry: parsed[0],
      keys: entries.map(({ key, rev }) => [key, rev]),
      entries,
      window_full: windowFull,
      truncated,
      global_latest_when_writer_enforced: windowFull && report.writer_enforced,
      header: windowFull && !report.writer_enforced ? 'latest in loaded window' : 'latest',
      notice: truncated ? `history truncated: ${entries.length} of up to ${report.history_limit} loaded` : '',
    };
  }

  function errorReason(error) {
    if (error && typeof error === 'object') return error.message || error.code || 'unknown error';
    return String(error || 'unknown error');
  }

  function titleFor(report, item) {
    const template = report.title_template === undefined ? '{key} r{rev}' : report.title_template;
    return template.replace(/\{(key|rev)\}/g, (_, name) => String(item[name]));
  }

  function defaultRenderer() {
    if (root && root.PentacleAssetRender) return root.PentacleAssetRender.renderAsset;
    if (typeof require === 'function') return require('../asset_render').renderAsset;
    throw new Error('generic report renderer is unavailable');
  }

  function createBoard(entry, dependencies = {}) {
    const report = entry.report;
    const assetList = dependencies.assetList || ((args) => root.cc.assetList(args));
    const assetGet = dependencies.assetGet || ((args) => root.cc.assetGet(args));

    function element(refs, tag, text, testId) {
      const node = refs.container.ownerDocument.createElement(tag);
      if (text !== undefined) node.textContent = text;
      if (testId) node.dataset.testid = testId;
      return node;
    }

    function setState(refs, state) {
      refs.container.dataset.boardState = state;
    }

    function fail(refs, message) {
      setState(refs, 'error');
      refs.content.replaceChildren(element(refs, 'p', message, 'dashboard-board-error'));
    }

    async function openReport(refs, item, generation) {
      if (refs.disposed || refs.generation !== generation) return;
      const viewerGeneration = ++refs.viewerGeneration;
      const current = () => !refs.disposed && refs.generation === generation && refs.viewerGeneration === viewerGeneration;
      const viewer = refs.viewer;
      setState(refs, refs.outcome.state === 'partial' ? 'partial' : 'loading');
      const heading = element(refs, 'h3', titleFor(report, item));
      viewer.replaceChildren(heading, element(refs, 'p', 'Loading report…'));
      viewer.dataset.assetId = item.row.asset_id;
      try {
        const row = item.row;
        if (typeof row.stream_id !== 'string' || !row.stream_id) throw new Error('report is missing its owner stream_id');
        const reply = await assetGet({ stream_id: row.stream_id, asset_id: row.asset_id, spec_id: report.spec_id });
        if (!current()) return;
        if (!reply || reply.ok === false || reply.error) throw new Error(errorReason(reply && reply.error));
        const asset = reply.asset;
        if (!asset || typeof asset !== 'object' || !Object.prototype.hasOwnProperty.call(asset, 'body')) {
          throw new Error('report response is missing its asset body');
        }
        const contentType = asset.content_type || row.content_type;
        let body = asset.body;
        if ((contentType === 'report' || contentType === 'json_table') && typeof body === 'string') body = JSON.parse(body);
        const render = dependencies.renderAsset || defaultRenderer();
        const node = render(refs.container.ownerDocument, contentType, body, {
          classPrefix: 'slot-asset',
          asset: { ...row, ...asset, asset_key: `spec:${report.spec_id}:${row.asset_id}`, stream_id: row.stream_id, spec_id: report.spec_id },
          reviewStatus: asset.review_status || row.review_status,
        });
        if (!current()) return;
        viewer.replaceChildren(heading, node);
        setState(refs, refs.outcome.state === 'partial' ? 'partial' : 'ready');
      } catch (error) {
        if (!current()) return;
        setState(refs, 'error');
        viewer.replaceChildren(heading, element(refs, 'p', `Board failed to load: ${errorReason(error)}`, 'dashboard-board-error'));
      }
    }

    function renderOutcome(refs, outcome, generation) {
      refs.outcome = outcome;
      refs.content.replaceChildren();
      if (outcome.state === 'error') {
        fail(refs, outcome.notice);
        return Promise.resolve();
      }
      if (outcome.state === 'empty') {
        setState(refs, 'empty');
        refs.content.appendChild(element(refs, 'p', `No ${entry.name} yet (last checked ${refs.lastChecked})`, 'dashboard-report-empty'));
        return Promise.resolve();
      }
      setState(refs, outcome.state === 'partial' ? 'partial' : 'loading');
      const latest = element(refs, 'section', undefined, 'dashboard-report-latest');
      latest.dataset.assetId = outcome.latest;
      latest.appendChild(element(refs, 'h2', outcome.header));
      const latestButton = element(refs, 'button', titleFor(report, outcome.latestEntry));
      latestButton.type = 'button';
      latestButton.dataset.assetId = outcome.latest;
      latestButton.addEventListener('click', () => { refs.ready = openReport(refs, outcome.latestEntry, generation); });
      latest.appendChild(latestButton);
      refs.content.appendChild(latest);
      if (report.list) {
        const list = element(refs, 'ul', undefined, 'dashboard-report-list');
        for (const item of outcome.entries) {
          const li = element(refs, 'li');
          const button = element(refs, 'button', titleFor(report, item));
          button.type = 'button';
          button.dataset.assetId = item.row.asset_id;
          button.addEventListener('click', () => { refs.ready = openReport(refs, item, generation); });
          li.appendChild(button);
          list.appendChild(li);
        }
        refs.content.appendChild(list);
      }
      if (outcome.truncated) refs.content.appendChild(element(refs, 'p', outcome.notice, 'dashboard-report-truncated'));
      refs.viewer = element(refs, 'section');
      refs.viewer.className = 'dashboard-report-viewer';
      refs.content.appendChild(refs.viewer);
      return openReport(refs, outcome.latestEntry, generation);
    }

    async function refresh(refs) {
      if (!refs || refs.disposed) return;
      const generation = ++refs.generation;
      ++refs.viewerGeneration;
      setState(refs, 'loading');
      refs.content.replaceChildren(element(refs, 'p', 'Loading reports…'));
      try {
        const reply = await assetList(reportRequest(report));
        if (refs.disposed || refs.generation !== generation) return;
        if (!reply || reply.ok === false || reply.error) throw new Error(errorReason(reply && reply.error));
        refs.lastChecked = new Date().toISOString();
        await renderOutcome(refs, selectReports(report, reply.assets), generation);
      } catch (error) {
        if (refs.disposed || refs.generation !== generation) return;
        fail(refs, `Board failed to load: ${errorReason(error)}`);
      }
    }

    function mount(container) {
      const refs = { container, generation: 0, viewerGeneration: 0, disposed: false };
      const title = element(refs, 'h1', entry.name);
      const refreshButton = element(refs, 'button', 'Refresh');
      refreshButton.type = 'button';
      refreshButton.addEventListener('click', () => { refs.ready = refresh(refs); });
      refs.content = element(refs, 'div');
      container.replaceChildren(title, refreshButton, refs.content);
      refs.refresh = () => { refs.ready = refresh(refs); return refs.ready; };
      refs.ready = refresh(refs);
      return refs;
    }

    function unmount(refs) {
      if (!refs) return;
      refs.disposed = true;
      ++refs.generation;
      ++refs.viewerGeneration;
      refs.container.replaceChildren();
      delete refs.container.dataset.boardState;
    }

    return { id: entry.id, name: entry.name, description: entry.description || '', mount, unmount, refresh };
  }

  const api = { createBoard, reportIdGrammar, selectReports, reportRequest };
  if (root) root.DashboardReportBoard = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : null);
