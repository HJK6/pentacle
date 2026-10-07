'use strict';

// ── Fixture dashboard catalog for the web gate ──────────────────────────────
// Builds a synthetic catalog root (immutable version directories N and N+1)
// and the matching `dashboard-catalog` asset bodies, and seeds them plus
// synthetic report assets into the fixture daemon's asset DB.
//
// Documented test override (private dashboard packages' e2e):
//   PENTACLE_TEST_CATALOG_ROOT=<dir of version dirs>   use a built private
//     catalog root instead of the synthetic one; each `<version>/catalog.json`
//     is published verbatim, in version-directory name order.
//   PENTACLE_TEST_CATALOG_REPORT_ROWS=<rows.json>      optional report rows
//     {spec_id, producer, asset_ids[]} to seed for report boards.
// The override exists only inside the gate process: it changes the scratch
// profile the gate writes, never a host default.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..', '..', '..');
const SEEDER = path.join(__dirname, 'seed_dashboard_assets.py');
const FIXTURES = path.join(ROOT, 'test', 'fixtures', 'dashboard_catalog');
const CATALOG_SPEC_ID = 'example__dashboard_catalog';
const CATALOG_STREAM = 'local:web-gate-catalog-publisher';
const VERSIONS = ['0.2.0+aaaaaaa', '0.2.1+bbbbbbb'];

const sha256 = (text) => crypto.createHash('sha256').update(text).digest('hex');

function adapterSource(version) {
  // A self-contained classic script: registers under its catalog id and
  // renders a version marker the gate can read.
  return `(function () {
  window.DASHBOARDS = window.DASHBOARDS || [];
  window.DASHBOARDS.push({
    id: 'example-board',
    name: 'Example Board',
    mount(container) {
      const el = document.createElement('div');
      el.className = 'example-board';
      el.dataset.exampleBoardVersion = ${JSON.stringify(version)};
      el.textContent = 'Example board ' + ${JSON.stringify(version)} + ' ' + (window.exampleBoardLib || 'no-lib');
      container.appendChild(el);
    },
  });
})();
`;
}

function syntheticVersion(root, version, { hostedUrl }) {
  const files = {
    'web/example-lib.js': `window.exampleBoardLib = ${JSON.stringify(`lib ${version}`)};\n`,
    'web/example-board.js': adapterSource(version),
    'web/example-board.css': `.example-board { outline: 1px dashed currentColor; }\n`,
    'web/example-broken.js': `window.exampleBrokenLoaded = true;\n`,
  };
  const retrieval = JSON.parse(fs.readFileSync(path.join(FIXTURES, 'report_retrieval_cases.json'), 'utf8'));
  const catalog = {
    schema_version: 1,
    catalog_version: version,
    package: { repo: 'example/dashboards', commit: version.endsWith('aaaaaaa') ? 'a'.repeat(40) : 'b'.repeat(40) },
    requires: { host_api: 1 },
    libs: [{ path: 'web/example-lib.js', sha256: sha256(files['web/example-lib.js']) }],
    boards: [
      { id: 'example-report', name: version === VERSIONS[0] ? 'Example Reports' : 'Example Reports (N+1)',
        kind: 'report', report: retrieval.descriptor },
      { id: 'example-board', name: 'Example Board', kind: 'web-adapter',
        web: { script: 'web/example-board.js', sha256: sha256(files['web/example-board.js']),
          css: 'web/example-board.css', css_sha256: sha256(files['web/example-board.css']) },
        actions: ['assetList'] },
      { id: 'example-broken', name: 'Example Broken', kind: 'web-adapter',
        web: { script: 'web/example-broken.js', sha256: sha256(files['web/example-broken.js']) } },
      { id: 'example-hosted', name: 'Example Hosted', kind: 'hosted-view', hosted: { url: hostedUrl } },
    ],
  };
  const dir = path.join(root, version);
  fs.mkdirSync(path.join(dir, 'web'), { recursive: true });
  for (const [rel, text] of Object.entries(files)) fs.writeFileSync(path.join(dir, rel), text);
  const installed = `${JSON.stringify(catalog, null, 2)}\n`;
  fs.writeFileSync(path.join(dir, 'catalog.json'), installed);
  // The published asset pins a different hash for example-broken, so the host
  // serves the installed bytes and the browser's SRI check must refuse them.
  const published = JSON.parse(installed);
  published.boards.find((b) => b.id === 'example-broken').web.sha256 = sha256('not the installed bytes');
  return { version, dir, files, catalog, assetBody: `${JSON.stringify(published, null, 2)}\n` };
}

// Returns { root, specId, versions: [{version, dir, assetBody, catalog}], override, reportRows }.
// Synthetic versions are written under `scratch`; only N is installed at
// first, so a scenario can install N+1 at runtime (no host restart).
function buildCatalogFixture(scratch, { hostedUrl = 'http://127.0.0.1:9/' } = {}) {
  const overrideRoot = process.env.PENTACLE_TEST_CATALOG_ROOT;
  if (overrideRoot) {
    const root = path.resolve(overrideRoot);
    const versions = fs.readdirSync(root).sort()
      .filter((name) => fs.existsSync(path.join(root, name, 'catalog.json')))
      .map((version) => {
        const assetBody = fs.readFileSync(path.join(root, version, 'catalog.json'), 'utf8');
        return { version, dir: path.join(root, version), assetBody, catalog: JSON.parse(assetBody), installed: true };
      });
    if (!versions.length) throw new Error(`PENTACLE_TEST_CATALOG_ROOT has no <version>/catalog.json: ${root}`);
    const rowsFile = process.env.PENTACLE_TEST_CATALOG_REPORT_ROWS;
    const reportRows = rowsFile ? JSON.parse(fs.readFileSync(rowsFile, 'utf8')) : [];
    return { root, specId: CATALOG_SPEC_ID, versions, override: true, reportRows };
  }
  const root = path.join(scratch, 'dashboard-catalogs');
  const staging = path.join(scratch, 'dashboard-catalogs-staging');
  fs.mkdirSync(root, { recursive: true });
  const versions = VERSIONS.map((version) => syntheticVersion(staging, version, { hostedUrl }));
  fs.renameSync(versions[0].dir, path.join(root, versions[0].version));
  versions[0].dir = path.join(root, versions[0].version);
  versions[0].installed = true;
  const retrieval = JSON.parse(fs.readFileSync(path.join(FIXTURES, 'report_retrieval_cases.json'), 'utf8'));
  const fb = retrieval.cases.find((c) => c.name.startsWith('F-B'));
  const fa = retrieval.cases.find((c) => c.name.startsWith('F-A'));
  const reportRows = [{ spec_id: retrieval.descriptor.spec_id,
    rows: [...fb.rows, ...fa.rows.filter((r) => r.content_type === 'report' && r.producer !== retrieval.descriptor.producer_stream_id),
      ...fa.rows.filter((r) => r.asset_id.startsWith('example-digest-'))] }];
  return { root, specId: CATALOG_SPEC_ID, versions, override: false, reportRows,
    expectedReportIds: fb.server_ids, descriptor: retrieval.descriptor };
}

// Move a staged synthetic version directory into the live root (atomic rename).
function installVersion(fixture, index) {
  const entry = fixture.versions[index];
  if (entry.installed) return entry;
  const target = path.join(fixture.root, entry.version);
  fs.renameSync(entry.dir, target);
  entry.dir = target;
  entry.installed = true;
  return entry;
}

function seed(python, assetsDb, args) {
  return execFileSync(python, [SEEDER, '--assets-db', assetsDb, ...args], { cwd: ROOT, encoding: 'utf8' });
}

function publishCatalog(fixture, { python, assetsDb, scratch }, index) {
  const entry = fixture.versions[index];
  const file = path.join(scratch, `catalog-asset-${index}.json`);
  fs.writeFileSync(file, entry.assetBody);
  return JSON.parse(seed(python, assetsDb, ['catalog', '--spec-id', fixture.specId, '--stream', CATALOG_STREAM, '--file', file]));
}

function seedReports(fixture, { python, assetsDb, scratch }) {
  fixture.reportRows.forEach((group, index) => {
    const file = path.join(scratch, `report-rows-${index}.json`);
    const rows = group.rows || (group.asset_ids || []).map((asset_id) => ({ asset_id, producer: group.producer }));
    fs.writeFileSync(file, JSON.stringify(rows.filter((r) => !r.content_type || r.content_type === 'report')));
    seed(python, assetsDb, ['reports', '--spec-id', group.spec_id, '--stream', 'local:web-gate-report-seed', '--rows', file]);
  });
}

function deleteCatalog(fixture, { python, assetsDb }) {
  return seed(python, assetsDb, ['delete', '--spec-id', fixture.specId, '--asset-id', 'dashboard-catalog']);
}

module.exports = { buildCatalogFixture, installVersion, publishCatalog, seedReports, deleteCatalog,
  CATALOG_SPEC_ID, CATALOG_STREAM, VERSIONS, adapterSource };
