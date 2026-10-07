'use strict';

// ── dashboard_catalog: runtime catalog delivery through the real web host ────
// Synthetic catalog and report assets live in the fixture daemon; private
// files live in a scratch catalog root (or PENTACLE_TEST_CATALOG_ROOT). Every
// read goes through the page's real `window.cc` bridge → host → daemon, and
// private files through the host's authenticated versioned route.

const { reportChecks } = require('./web_voice_scenario');
const { reloadDashboardPage } = require('./dashboard_scenario');

const pageFetch = (url) => `fetch(${JSON.stringify(url)}, { cache: 'no-store' })
  .then(async (r) => ({ status: r.status, text: r.status === 200 ? await r.text() : '' }))`;

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

async function dashboardCatalog(ctx) {
  const { session, report, catalog } = ctx;
  if (!catalog) { report.note('no hermetic profile: skipping dashboard_catalog'); return; }
  const { fixture } = catalog;
  const [first, second] = fixture.versions;
  try {
    // Empty public default: the gate's default profile names no catalog.
    const before = await session.eval('window.cc.getConfig().then((c) => c.dashboards || null)');
    reportChecks(report, [['default profile has no dashboard catalog', !before || !before.catalogSpecId, { before }]]);

    catalog.publish(0);
    catalog.seedReports();
    await catalog.configure(true);
    await reloadDashboardPage(ctx);
    const config = await session.eval('window.cc.getConfig().then((c) => c.dashboards || null)');
    reportChecks(report, [
      ['catalogSpecId reaches the renderer', config?.catalogSpecId === fixture.specId, { config }],
      ['catalogRoot never reaches the renderer', config && !('catalogRoot' in config), { config }],
    ]);

    const n = await readCatalogAsset(session, fixture.specId);
    reportChecks(report, [
      ['spec-scoped list carries the owner stream_id', !!n.meta?.stream_id, n],
      [`asset.get with the listed stream_id returns catalog ${first.version}`, n.version === first.version, n],
    ]);

    // Private files: every lib/css/script of the installed version is served.
    const entries = first.catalog.libs.map((l) => l.path)
      .concat(...first.catalog.boards.filter((b) => b.kind === 'web-adapter')
        .map((b) => [b.web.script, b.web.css].filter(Boolean)));
    const served = [];
    for (const rel of entries) {
      served.push({ rel, ...(await session.eval(pageFetch(`/dashboards/private/${first.version}/${rel}`))) });
    }
    reportChecks(report, [
      [`version ${first.version} serves every listed file`, served.every((r) => r.status === 200), served.map((r) => [r.rel, r.status])],
      ['an unlisted path is 404', (await session.eval(pageFetch(`/dashboards/private/${first.version}/catalog.json`))).status === 404, {}],
    ]);

    if (!fixture.override) {
      // Report namespace: one bounded, server-filtered, id-sorted request.
      const d = fixture.descriptor;
      const window_ = await session.eval(`window.cc.assetList(${JSON.stringify({ spec_id: d.spec_id,
        asset_id_prefix: d.asset_id_prefix, producer: d.producer_stream_id, sort: 'asset_id_desc',
        limit: Math.min(4 * d.history_limit, 400) })}).then((r) => (r.assets || r.result?.assets || []).map((m) => m.asset_id))`);
      reportChecks(report, [['report window is prefix/producer filtered and id-sorted before the limit',
        JSON.stringify(window_) === JSON.stringify(fixture.expectedReportIds), { got: window_, want: fixture.expectedReportIds }]]);

      // Release N+1: install the version directory with the host running,
      // then publish the asset (the only pointer). No rebuild, no restart.
      const hostUp = await session.eval("fetch('/api/health').then((r) => r.ok)");
      catalog.install(1);
      const nextServed = await session.eval(pageFetch(`/dashboards/private/${second.version}/web/example-board.js`));
      const oldServed = await session.eval(pageFetch(`/dashboards/private/${first.version}/web/example-board.js`));
      catalog.publish(1);
      const n1 = await readCatalogAsset(session, fixture.specId);
      reportChecks(report, [
        ['N+1 files are served by the running host', nextServed.status === 200 && nextServed.text.includes(second.version), { status: nextServed.status }],
        ['N files stay served alongside N+1', oldServed.status === 200 && oldServed.text.includes(first.version), { status: oldServed.status }],
        [`republished asset reads back ${second.version}`, n1.version === second.version && hostUp === true, n1],
      ]);

      // Rollback rehearsal: republish N; its directory is still installed.
      catalog.publish(0);
      const back = await readCatalogAsset(session, fixture.specId);
      reportChecks(report, [[`rollback republish reads back ${first.version}`, back.version === first.version, back]]);
    }
  } finally {
    try { catalog.remove(); } catch {}
    await catalog.configure(false);
    await reloadDashboardPage(ctx);
  }
}

module.exports = { dashboardCatalog, readCatalogAsset };
