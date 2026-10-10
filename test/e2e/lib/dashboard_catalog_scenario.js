'use strict';

// Runtime catalog delivery through the real web host and fixture daemon.
// The bridge observer below records requests and delegates unchanged. Only the
// the empty-list case confirms there is no registration fallback;
// no catalog/report reply, adapter registration, or browser SRI result is faked.
const crypto = require('node:crypto');
const { reportChecks } = require('./web_voice_scenario');
const { reloadDashboardPage, readDashboardObservation, viewerChecks, chatChecks, waitForDashboardChatFiles } = require('./dashboard_scenario');

const pageFetch = (url) => `fetch(${JSON.stringify(url)}, { cache: 'no-store' })
  .then(async (r) => ({ status: r.status, text: r.status === 200 ? await r.text() : '' }))`;
const pinnedIntegrity = hash => `sha256-${Buffer.from(hash, 'hex').toString('base64')}`;

// Installed before boot, so an erroneous catalog request during startup cannot
// evade the unset-config assertion. Session-scoped chat asset reads stay real.
function observeCatalogBridge(hideStatic = false) {
  const seen = window.__dashboardCatalogGate = { lists: [], gets: [], tags: [], tagErrors: [] };
  const tagInfo = tag => ({ url: tag.getAttribute('src') || tag.getAttribute('href'),
    integrity: tag.getAttribute('integrity'), crossorigin: tag.getAttribute('crossorigin') });
  const isPrivateTag = tag => tag?.matches?.('script[src], link[href]')
    && (tag.getAttribute('src') || tag.getAttribute('href')).startsWith('/dashboards/private/');
  new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) {
      if (isPrivateTag(node)) seen.tags.push(tagInfo(node));
    }
  }).observe(document, { childList: true, subtree: true });
  document.addEventListener('error', event => {
    if (isPrivateTag(event.target)) seen.tagErrors.push(tagInfo(event.target));
  }, true);
  Object.defineProperty(window, 'cc', { configurable: true, set(bridge) {
    for (const [method, collection] of [['assetList', 'lists'], ['assetGet', 'gets']]) {
      const original = bridge[method];
      bridge[method] = function (args) {
        seen[collection].push(args == null ? null : JSON.parse(JSON.stringify(args)));
        return original.call(this, args);
      };
    }
    if (hideStatic) {
      const getConfig = bridge.getConfig;
      bridge.getConfig = async function (...args) {
        const config = await getConfig.apply(this, args);
        return { ...config, dashboards: { ...config.dashboards, hidden: ['shared-demo', 'modeler-3d'] } };
      };
    }
    Object.defineProperty(window, 'cc', { configurable: true, writable: true, value: bridge });
  } });
}

async function observedReload(ctx, hideStatic = false) {
  const { identifier } = await ctx.session.send('Page.addScriptToEvaluateOnNewDocument', {
    source: `(${observeCatalogBridge.toString()})(${JSON.stringify(hideStatic)})`,
  });
  try { await reloadDashboardPage(ctx); }
  finally { await ctx.session.send('Page.removeScriptToEvaluateOnNewDocument', { identifier }); }
}

function readCatalogObservation(doc = document) {
  const content = doc.getElementById('dashboard-content');
  return {
    ids: Array.from(doc.querySelectorAll('#dashboard-list [data-dashboard-id]'), el => el.dataset.dashboardId),
    registered: (doc.defaultView.DASHBOARDS || []).map(board => board.id),
    version: content.dataset.catalogVersion || null,
    state: content.dataset.boardState || null,
    text: content.textContent,
    latest: content.querySelector('[data-testid="dashboard-report-latest"]')?.dataset.assetId || null,
    history: Array.from(content.querySelectorAll('[data-testid="dashboard-report-list"] [data-asset-id]'), el => el.dataset.assetId),
    body: content.querySelector('.dashboard-report-viewer')?.textContent || '',
    adapterVersion: content.querySelector('[data-example-board-version]')?.dataset.exampleBoardVersion || null,
    boardError: content.querySelector('[data-testid="dashboard-board-error"]')?.textContent || null,
    catalogError: doc.querySelector('[data-testid="dashboard-catalog-error"]')?.textContent || null,
  };
}

async function readCatalogAsset(session, specId) {
  return session.eval(`(async () => {
    const listed = await window.cc.assetList({ spec_id: ${JSON.stringify(specId)} });
    const rows = (listed.assets || listed.result?.assets || []);
    const meta = rows.find((m) => m.asset_id === 'dashboard-catalog' && m.content_type === 'dashboard-catalog');
    if (!meta) return { meta: null, rows: rows.length };
    const got = await window.cc.assetGet({ stream_id: meta.stream_id, asset_id: meta.asset_id, spec_id: ${JSON.stringify(specId)} });
    const asset = got.asset || got.result?.asset;
    return { meta, version: asset ? JSON.parse(asset.body).catalog_version : null };
  })()`);
}

async function prepareCatalogChat(ctx) {
  const { session, fixture } = ctx;
  await session.waitFor(`window.cc.getChatStreamState().then(s => s.connected && s.sessions.some(x => x.stream_id === ${JSON.stringify(fixture.streamId)}))`);
  await session.eval(`window.focusStreamId(${JSON.stringify(fixture.streamId)})`);
  await session.waitFor("!!document.querySelector('[id^=header-] [data-mode=chat]')");
  await session.eval("document.querySelectorAll('[id^=header-] [data-mode=chat]').forEach(button => button.click())");
  await session.waitFor(`!!document.querySelector('.slot-chat-list[data-stream-id="${fixture.streamId}"]')?.textContent.includes(${JSON.stringify(fixture.transcript[0].text)})`);
  const answered = await session.eval(`window.cc.promptList({ producer_stream_id: ${JSON.stringify(fixture.streamId)}, open: false })
    .then(reply => (reply?.questions || []).filter(q => q && q.state !== 'open' && q.answer).length)`);
  await session.waitFor(`document.querySelectorAll('.slot-chat-list[data-stream-id="${fixture.streamId}"] .slot-chat-v3-answer-entry').length >= ${Number(answered) || 0}`);
  await waitForDashboardChatFiles(session, fixture.streamId);
  await session.eval(`(() => {
    const list = document.querySelector('.slot-chat-list[data-stream-id="${fixture.streamId}"]');
    const input = list.closest('.slot-chat-shell').querySelector('.slot-chat-compose-input');
    const previousDraft = input.value;
    input.value = 'unsent catalog round-trip'; input.dispatchEvent(new Event('input', { bubbles: true }));
    window.__catalogChatBefore = { list, input, grid: document.querySelector('.grid'), previousDraft,
      transcript: list.textContent, streamId: list.dataset.streamId, draft: input.value };
  })()`);
}

async function assertCatalogChat(ctx, after) {
  const { session, fixture, report } = ctx;
  await session.click('#view-chats');
  const observation = await session.eval(`(() => {
    const before = window.__catalogChatBefore;
    const list = document.querySelector('.slot-chat-list[data-stream-id="${fixture.streamId}"]');
    const input = list?.closest('.slot-chat-shell').querySelector('.slot-chat-compose-input');
    const sameDraft = input?.value === before.draft;
    let usable = !!input && !input.disabled && !input.readOnly;
    if (usable) {
      input.focus(); input.value += ' edited'; input.dispatchEvent(new Event('input', { bubbles: true }));
      usable = document.activeElement === input && input.value === before.draft + ' edited';
      input.value = before.draft; input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    return { sameNodes: list === before.list && input === before.input && document.querySelector('.grid') === before.grid,
      sameStream: list?.dataset.streamId === before.streamId, sameTranscript: list?.textContent === before.transcript, sameDraft,
      chatsVisible: document.querySelector('.grid').style.display !== 'none',
      dashboardsHidden: document.getElementById('panel-dashboards').style.display === 'none' && document.getElementById('dashboard-content').style.display === 'none',
      frames: document.querySelectorAll('#dashboard-content iframe').length, usable };
  })()`);
  reportChecks(report, [...chatChecks(observation), ['chat composer remains editable', observation.usable, observation]]
    .map(([name, pass, detail]) => [`after ${after}: ${name}`, pass, detail]));
}

async function dashboardCatalog(ctx) {
  const { session, report, catalog } = ctx;
  if (!catalog) { report.note('no hermetic profile: skipping dashboard_catalog'); return; }
  const { fixture } = catalog;
  const [first, second] = fixture.versions;
  const observe = () => session.eval(`(${readCatalogObservation.toString()})()`);
  async function enter(version) {
    await session.click('#view-chats');
    await session.eval("window.__catalogPreviousList = document.getElementById('dashboard-list').firstChild");
    await session.click('#view-dashboards');
    // View entry empties the list at once and keeps the previous version
    // attribute until the load settles; wait for the re-rendered list, since
    // board clicks are ignored while the catalog is loading.
    await session.waitFor(`document.getElementById('dashboard-content').dataset.catalogVersion === ${JSON.stringify(version)}
      && document.getElementById('dashboard-list').firstChild !== window.__catalogPreviousList
      && !!document.querySelector('#dashboard-list [data-dashboard-id]')`);
  }
  try {
    await observedReload(ctx);
    const before = await session.eval('window.cc.getConfig().then(c => c.dashboards || null)');
    reportChecks(report, [['default profile has no dashboard catalog', !before || !before.catalogSpecId, { before }]]);
    await session.click('#view-dashboards');
    await session.waitFor("document.getElementById('dashboard-content').dataset.boardState === 'empty'");
    const defaults = await observe();
    // Normal chat inventory calls use a stream/session scope. A catalog read,
    // even an erroneous unscoped read with the config unset, has neither.
    const noCatalogRequests = () => session.eval("window.__dashboardCatalogGate.lists.filter(args => !args?.stream_id && !args?.host && !args?.session_name)");
    const defaultRequests = await noCatalogRequests();
    reportChecks(report, [
      ['unset catalog has no separately ordered fallback', defaults.ids.length === 0 && defaults.version === null, defaults],
      ['unset catalog issues zero catalog asset.list calls, including boot', defaultRequests.length === 0, { calls: defaultRequests }],
    ]);

    // Profile visibility settings cannot create catalog membership.
    await observedReload(ctx, true);
    await session.click('#view-dashboards');
    await session.waitFor("document.getElementById('dashboard-content').dataset.boardState === 'empty'");
    const empty = await observe();
    const emptyRequests = await noCatalogRequests();
    reportChecks(report, [
      ['profile visibility cannot create membership', empty.ids.length === 0 && empty.text.includes('No dashboards configured'), empty],
      ['synthetic empty view also makes no catalog request', emptyRequests.length === 0, { calls: emptyRequests }],
    ]);

    catalog.publish(0);
    catalog.seedReports();
    await catalog.configure(true);
    await observedReload(ctx);
    const config = await session.eval('window.cc.getConfig().then(c => c.dashboards || null)');
    reportChecks(report, [
      ['catalogSpecId reaches the renderer', config?.catalogSpecId === fixture.specId, { config }],
      ['catalogRoot never reaches the renderer', config && !('catalogRoot' in config), { config }],
    ]);
    await prepareCatalogChat(ctx);
    await enter(first.version);
    const loaded = await observe();
    reportChecks(report, [[`catalog ${first.version} renders in catalog array order`, loaded.version === first.version
      && JSON.stringify(loaded.ids) === JSON.stringify(first.catalog.boards.filter(board => board.visible !== false).map(board => board.id)), loaded]]);

    const n = await readCatalogAsset(session, fixture.specId);
    reportChecks(report, [
      ['spec-scoped list carries the owner stream_id', !!n.meta?.stream_id, n],
      [`asset.get with the listed stream_id returns catalog ${first.version}`, n.version === first.version, n],
    ]);
    const entries = (first.catalog.libs || []).map(lib => lib.path)
      .concat(...first.catalog.boards.filter(board => board.kind === 'web-adapter').map(board => [board.web.script, board.web.css].filter(Boolean)));
    const served = [];
    for (const rel of entries) served.push({ rel, ...(await session.eval(pageFetch(`/dashboards/private/${first.version}/${rel}`))) });
    reportChecks(report, [
      [`version ${first.version} serves every listed file`, served.every(row => row.status === 200), served.map(row => [row.rel, row.status])],
      ['an unlisted path is 404', (await session.eval(pageFetch(`/dashboards/private/${first.version}/catalog.json`))).status === 404, {}],
    ]);

    if (!fixture.override) {
      const d = fixture.descriptor;
      const request = { spec_id: d.spec_id, asset_id_prefix: d.asset_id_prefix, producer: d.producer_stream_id,
        sort: 'asset_id_desc', limit: Math.min(4 * d.history_limit, 400) };
      const rows = await session.eval(`window.cc.assetList(${JSON.stringify(request)}).then(r => r.assets || r.result?.assets || [])`);
      reportChecks(report, [['report window is prefix/producer filtered and id-sorted before the limit',
        JSON.stringify(rows.map(row => row.asset_id)) === JSON.stringify(fixture.expectedReportIds), { got: rows.map(row => row.asset_id), want: fixture.expectedReportIds }]]);
      const requestStart = await session.eval('({ lists: window.__dashboardCatalogGate.lists.length, gets: window.__dashboardCatalogGate.gets.length })');
      await session.click('[data-dashboard-id="example-report"]');
      await session.waitFor("document.getElementById('dashboard-content').dataset.boardState === 'ready' && !!document.querySelector('[data-testid=\"dashboard-report-latest\"]')");
      const reportView = await observe();
      const reportCalls = await session.eval(`({ lists: window.__dashboardCatalogGate.lists.slice(${requestStart.lists}).filter(args => args?.spec_id === ${JSON.stringify(d.spec_id)}),
        gets: window.__dashboardCatalogGate.gets.slice(${requestStart.gets}).filter(args => args?.spec_id === ${JSON.stringify(d.spec_id)}) })`);
      const latest = 'example-report-20261007T1300Z';
      reportChecks(report, [
        ['report UI shows the seeded latest and its generic rendered body', reportView.latest === latest && reportView.body.includes(`Synthetic report ${latest}.`), reportView],
        ['report UI history keeps distinct keys at their highest revisions', JSON.stringify(reportView.history) === JSON.stringify([latest, 'example-report-20261006T1300Z-r02']), reportView],
        ['report mount makes exactly one bounded list request', reportCalls.lists.length === 1 && Object.keys(request).every(key => reportCalls.lists[0][key] === request[key]), reportCalls],
        ['report viewer fetches through the listed owner stream', reportCalls.gets.length === 1 && reportCalls.gets[0].stream_id === rows[0].stream_id && reportCalls.gets[0].asset_id === latest, reportCalls],
      ]);

      await session.click('[data-dashboard-id="example-board"]');
      await session.waitFor(`document.querySelector('[data-example-board-version]')?.dataset.exampleBoardVersion === ${JSON.stringify(first.version)}`);
      const adapter = await observe();
      const published = JSON.parse(first.assetBody);
      const adapterEntry = published.boards.find(board => board.id === 'example-board');
      const expectedTags = [...published.libs, { path: adapterEntry.web.css, sha256: adapterEntry.web.css_sha256 }, { path: adapterEntry.web.script, sha256: adapterEntry.web.sha256 }];
      const tags = await session.eval('window.__dashboardCatalogGate.tags');
      reportChecks(report, [
        ['example-board renders version N', adapter.state === 'ready' && adapter.adapterVersion === first.version && adapter.text.includes(`lib ${first.version}`), adapter],
        ['adapter loads version-pinned lib, css, script in order with SRI', expectedTags.every((entry, index) => tags[index]?.url === `/dashboards/private/${first.version}/${entry.path}` && tags[index].integrity === pinnedIntegrity(entry.sha256) && tags[index].crossorigin === 'anonymous'), { tags }],
      ]);

      await session.click('[data-dashboard-id="example-broken"]');
      await session.waitFor("!!document.querySelector('#dashboard-content [data-testid=\"dashboard-board-error\"]')");
      const broken = await observe();
      const brokenFile = served.find(row => row.rel === 'web/example-broken.js');
      const brokenEntry = published.boards.find(board => board.id === 'example-broken');
      const sri = await session.eval('({ executed: window.exampleBrokenLoaded === true, errors: window.__dashboardCatalogGate.tagErrors })');
      reportChecks(report, [
        ['SRI rejects HTTP-200 bytes that differ from the published hash', brokenFile.status === 200 && crypto.createHash('sha256').update(brokenFile.text).digest('hex') !== brokenEntry.web.sha256 && !sri.executed
          && sri.errors.some(tag => tag.url === `/dashboards/private/${first.version}/web/example-broken.js` && tag.integrity === pinnedIntegrity(brokenEntry.web.sha256) && tag.crossorigin === 'anonymous'), { status: brokenFile.status, ...sri }],
        ['example-broken renders its board error card', broken.state === 'error' && broken.boardError?.startsWith('Board failed to load:'), broken],
      ]);
      await assertCatalogChat(ctx, 'adapter SRI error');
      await enter(first.version);
      await session.click('[data-dashboard-id="example-hosted"]');
      await session.waitFor("document.querySelector('[data-modeler-state]')?.dataset.modelerState === 'unavailable'");
      const hosted = await session.eval(`(${readDashboardObservation.toString()})()`);
      reportChecks(report, [['loopback unknown auth mode refuses hosted entry', hosted.state === 'unavailable' && !await session.eval("!!document.querySelector('#dashboard-content iframe')"), hosted]]);
      // Identity/cache/isolation browser proof is required at integration.
      await assertCatalogChat(ctx, 'hosted-view navigation');

      // N+1 is installed and published with the host running. From this point
      // through rollback there is no configure(), page reload, or build call.
      const marker = await session.eval("window.__catalogDocumentMarker = `${performance.timeOrigin}:${Math.random()}`");
      const hostUp = await session.eval("fetch('/api/health').then(r => r.ok)");
      catalog.install(1);
      const nextServed = await session.eval(pageFetch(`/dashboards/private/${second.version}/web/example-board.js`));
      const oldServed = await session.eval(pageFetch(`/dashboards/private/${first.version}/web/example-board.js`));
      catalog.publish(1);
      await enter(second.version);
      await session.click('[data-dashboard-id="example-board"]');
      await session.waitFor(`document.querySelector('[data-example-board-version]')?.dataset.exampleBoardVersion === ${JSON.stringify(second.version)}`);
      const n1View = await observe();
      const n1 = await readCatalogAsset(session, fixture.specId);
      reportChecks(report, [
        ['N+1 files are served by the running host', nextServed.status === 200 && nextServed.text.includes(second.version), { status: nextServed.status }],
        ['N files stay served alongside N+1', oldServed.status === 200 && oldServed.text.includes(first.version), { status: oldServed.status }],
        ['view re-entry renders N+1 without rebuilding or reloading the document', n1View.version === second.version && n1View.adapterVersion === second.version && n1View.text.includes(`lib ${second.version}`) && n1View.state === 'ready' && await session.eval('window.__catalogDocumentMarker') === marker, n1View],
        [`republished asset reads back ${second.version}`, n1.version === second.version && hostUp === true, n1],
      ]);

      const newer = JSON.parse(first.assetBody);
      newer.requires.host_api = 3;
      catalog.publishBody(`${JSON.stringify(newer, null, 2)}\n`);
      await session.click('#view-chats');
      await session.click('#view-dashboards');
      await session.waitFor("document.querySelector('[data-testid=\"dashboard-catalog-error\"]')?.textContent.includes('requires.host_api 3')");
      const unsupported = await observe();
      reportChecks(report, [['host_api 3 renders unsupported catalog card without stale catalog boards', unsupported.catalogError.startsWith('Dashboard catalog unsupported/malformed:')
        && unsupported.catalogError.includes(`(catalog ${first.version})`) && unsupported.version === null && !unsupported.ids.some(id => id.startsWith('example-')), unsupported]]);
      await assertCatalogChat(ctx, 'unsupported catalog');

      catalog.corrupt('{"schema_version": 1, "boards": [');
      await session.click('#view-dashboards');
      await session.waitFor("document.querySelector('[data-testid=\"dashboard-catalog-error\"]')?.textContent.includes('(catalog unknown)')");
      const malformed = await observe();
      reportChecks(report, [['malformed raw catalog renders its error card without stale catalog boards', malformed.catalogError.startsWith('Dashboard catalog unsupported/malformed:')
        && malformed.version === null && !malformed.ids.some(id => id.startsWith('example-')), malformed]]);
      await assertCatalogChat(ctx, 'malformed catalog');

      catalog.publish(0);
      await enter(first.version);
      await session.click('[data-dashboard-id="example-board"]');
      await session.waitFor(`document.querySelector('[data-example-board-version]')?.dataset.exampleBoardVersion === ${JSON.stringify(first.version)}`);
      const back = await readCatalogAsset(session, fixture.specId);
      const rollbackView = await observe();
      reportChecks(report, [[`rollback republish restores ${first.version} in the same document`, back.version === first.version && rollbackView.state === 'ready' && rollbackView.text.includes(`lib ${first.version}`) && !rollbackView.text.includes(`lib ${second.version}`) && await session.eval('window.__catalogDocumentMarker') === marker, { ...back, rollbackView }]]);
      await assertCatalogChat(ctx, 'catalog rollback');
    }
  } finally {
    try { await session.eval(`(() => { const before = window.__catalogChatBefore; if (before?.input?.isConnected) {
      before.input.value = before.previousDraft; before.input.dispatchEvent(new Event('input', { bubbles: true })); }
      delete window.__catalogChatBefore; })()`); } catch {}
    try { catalog.remove(); } catch {}
    await catalog.configure(false);
    await reloadDashboardPage(ctx);
  }
}

module.exports = { dashboardCatalog, readCatalogAsset, observeCatalogBridge, readCatalogObservation };
