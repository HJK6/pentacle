const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const { createBoard, reportIdGrammar, selectReports, reportRequest } = require('../renderer/dashboards/report_board');
// Consume the fleet's shared fixtures directly, without modifying their rows.
const fixtures = require('./fixtures/dashboard_catalog/report_retrieval_cases.json');
const catalogFixtures = require('./fixtures/dashboard_catalog/catalog_cases.json');

function entry(overrides = {}) {
  return {
    id: 'example-report', name: 'Example Reports', kind: 'report',
    report: { ...fixtures.descriptor, ...overrides },
  };
}

function container() {
  return new JSDOM('<!doctype html><div id="board"></div>').window.document.getElementById('board');
}

function metadata(id, overrides = {}) {
  return { asset_id: id, spec_id: fixtures.descriptor.spec_id, content_type: 'report', stream_id: 'local:example-owner', ...overrides };
}

function reportBody(title = 'Example body') {
  return { schema_version: 1, title, sections: [] };
}

function assetReply(args, overrides = {}) {
  return { asset: { ...metadata(args.asset_id), body: JSON.stringify(reportBody()), ...overrides } };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fakeDaemon(fixture, calls) {
  return async (request) => {
    calls.push(request);
    // Model the supplied server contract, including filters before limit. This
    // catches an omitted producer even when its foreign row has a newer key.
    const assets = fixture.rows
      .map((row) => metadata(row.asset_id, row))
      .filter((row) => row.spec_id === request.spec_id)
      .filter((row) => !request.asset_id_prefix || row.asset_id.startsWith(request.asset_id_prefix))
      .filter((row) => !request.producer || row.producer === request.producer)
      .sort((a, b) => a.asset_id < b.asset_id ? 1 : a.asset_id > b.asset_id ? -1 : 0)
      .slice(0, request.limit);
    assert.deepEqual(assets.map((row) => row.asset_id), fixture.server_ids);
    return { assets };
  };
}

for (const fixture of fixtures.cases) {
  test(`shared retrieval fixture: ${fixture.name}`, async () => {
    const listCalls = [];
    const getCalls = [];
    const board = createBoard(entry(), {
      assetList: fakeDaemon(fixture, listCalls),
      assetGet: async (args) => { getCalls.push(args); return assetReply(args); },
    });
    const root = container();
    const refs = board.mount(root);
    assert.equal(root.dataset.boardState, 'loading');
    await refs.ready;
    assert.equal(listCalls.length, 1);
    assert.deepEqual(listCalls[0], fixture.request);
    const outcome = refs.outcome;
    for (const [key, value] of Object.entries(fixture.outcome)) assert.deepEqual(outcome[key], value, key);
    if (outcome.state === 'error') {
      assert.equal(root.dataset.boardState, 'error');
      assert.equal(root.querySelector('[data-testid="dashboard-board-error"]').textContent,
        `unsupported asset id ${fixture.outcome.unexpected_id} in namespace ${entry().report.asset_id_prefix}`);
      assert.equal(root.querySelector('[data-testid="dashboard-report-latest"]'), null);
      assert.equal(getCalls.length, 0);
    } else if (outcome.state === 'empty') {
      assert.equal(root.dataset.boardState, 'empty');
      assert.match(root.querySelector('[data-testid="dashboard-report-empty"]').textContent,
        /^No Example Reports yet \(last checked \d{4}-\d\d-\d\dT.+Z\)$/);
      assert.equal(getCalls.length, 0);
    } else {
      assert.equal(root.dataset.boardState, outcome.state === 'complete' ? 'ready' : 'partial');
      assert.equal(root.querySelector('[data-testid="dashboard-report-latest"]').dataset.assetId, fixture.outcome.latest);
      assert.equal(root.querySelectorAll('[data-testid="dashboard-report-list"] li').length, fixture.outcome.keys.length);
      assert.deepEqual(getCalls, [{ stream_id: 'local:example-owner', asset_id: fixture.outcome.latest, spec_id: entry().report.spec_id }]);
      assert.equal(root.querySelector('.slot-asset-report-header h1').textContent, 'Example body');
      const notice = root.querySelector('[data-testid="dashboard-report-truncated"]');
      assert.equal(notice ? notice.textContent : null, fixture.outcome.notice || null);
    }
    assert.equal(board.pollFn, undefined);
    assert.equal(board.pollInterval, undefined);
    board.unmount(refs);
  });
}

test('grammar uses the authoritative report rules including revision zero and escaped literals', () => {
  const grammar = reportIdGrammar('example.report-', '[A-Z]{2}\\.[0-9]', 2);
  assert.ok(grammar.test('example.report-AB.1'));
  assert.ok(grammar.test('example.report-AB.1-r01'));
  assert.ok(grammar.test('example.report-AB.1-r99'));
  for (const id of ['exampleXreport-AB.1', 'example.report-ABX1', 'example.report-AB.1-r00', 'example.report-AB.1-r0',
    'example.report-AB.1-r1', 'example.report-AB.1-r001', 'example.report-AB.1-r99\n', 'example.report-AB.1\n']) {
    assert.equal(grammar.test(id), false, id);
  }
  const zeroWidth = reportIdGrammar('example-report-', '[0-9]{4}', 0);
  assert.ok(zeroWidth.test('example-report-2026'));
  assert.equal(zeroWidth.test('example-report-2026-r01'), false);
  assert.ok(reportIdGrammar('example-report-', '[0-9]{01}[A-Z]_A', 3).test('example-report-1A_A-r001'));
  assert.equal(reportIdGrammar('example-report-', '[0-9]{4}', 3).test('example-report-2026-r000'), false);
  assert.equal(reportIdGrammar('example-report-', '[0-9]{4}', 1).test('example-report-2026-r0'), false);
});

test('grammar rejects every shared invalid key format and unsafe variable-width syntax', () => {
  const invalidFormats = catalogFixtures.invalid
    .filter((fixture) => fixture.error.includes('key_format'))
    .map((fixture) => fixture.catalog.boards[0].report.key_format);
  assert.ok(invalidFormats.length > 0);
  for (const format of [...invalidFormats, '[0-9]{100}', '[0-9]{00}', '[0-9]{3}', '[A-Z]{33}', '[0-9]+', '[0-9]*',
    '[0-9]{4,8}', '[0-9]{4}?', '(ABCD)', 'AB|CD', '\\d{4}', '[a-z]{4}', 'ABC.', '[0-9]{4}\n']) {
    assert.throws(() => reportIdGrammar('example-report-', format, 2), /key_format/, String(format));
  }
  for (const width of [-1, 4, 1.5, true]) assert.throws(() => reportIdGrammar('example-report-', 'ABCD', width), /rev_width/);
});

test('selection preserves raw order and latest while retaining each distinct key at its highest revision', () => {
  const rows = [
    metadata('example-report-20261005T1300Z', { updated_at: '2026-10-08T00:00:00Z' }),
    metadata('example-report-20261004T1300Z-r02', { updated_at: '2026-10-09T00:00:00Z' }),
    metadata('example-report-20261004T1300Z-r11', { updated_at: '2026-10-01T00:00:00Z' }),
    metadata('example-report-20261005T1300Z-r01'),
    metadata('example-report-20261003T1300Z'),
    metadata('example-report-20261002T1300Z'),
  ];
  const before = JSON.stringify(rows);
  const outcome = selectReports(entry().report, rows);
  assert.equal(outcome.latest, rows[0].asset_id);
  assert.equal(outcome.latestEntry.row, rows[0]);
  assert.deepEqual(outcome.keys, [['20261005T1300Z', 1], ['20261004T1300Z', 11], ['20261003T1300Z', 0]]);
  assert.equal(outcome.entries[1].row, rows[2]);
  assert.equal(JSON.stringify(rows), before);
});

test('unsupported id later in a window takes precedence over a valid latest', () => {
  const outcome = selectReports(entry().report, [metadata('example-report-20261007T1300Z'), metadata('example-report-20261005T1300Z-r00')]);
  assert.equal(outcome.state, 'error');
  assert.equal(outcome.latest, null);
  assert.equal(outcome.unexpected_id, 'example-report-20261005T1300Z-r00');
});

test('full window header is qualified unless writer enforcement is declared', async () => {
  const fixture = fixtures.cases.find((fixture) => fixture.name.startsWith('F-D'));
  for (const enforced of [true, false]) {
    const root = container();
    const board = createBoard(entry({ writer_enforced: enforced }), { assetList: fakeDaemon(fixture, []), assetGet: async (args) => assetReply(args) });
    const refs = board.mount(root);
    await refs.ready;
    assert.equal(root.dataset.boardState, 'partial');
    assert.equal(root.querySelector('[data-testid="dashboard-report-latest"] h2').textContent, enforced ? 'latest' : 'latest in loaded window');
    assert.equal(root.querySelector('[data-testid="dashboard-report-truncated"]').textContent, 'history truncated: 1 of up to 3 loaded');
  }
});

test('full window is partial without a truncation notice when it includes the requested history keys', () => {
  const report = entry({ history_limit: 1, writer_enforced: false }).report;
  const rows = ['04', '03', '02', '01'].map((day) => metadata(`example-report-202610${day}T1300Z`));
  const result = selectReports(report, rows);
  assert.equal(result.state, 'partial');
  assert.equal(result.truncated, false);
  assert.equal(result.notice, '');
  assert.equal(result.entries.length, 1);
});

test('request is bounded to 400 rows and omits absent producer', () => {
  const report = entry({ history_limit: 100 }).report;
  delete report.producer_stream_id;
  assert.deepEqual(reportRequest(report), { spec_id: report.spec_id, asset_id_prefix: report.asset_id_prefix, sort: 'asset_id_desc', limit: 400 });
});

test('list false still displays the latest generic viewer and hides history', async () => {
  const root = container();
  const board = createBoard(entry({ list: false }), {
    assetList: async () => ({ assets: [metadata('example-report-20261007T1300Z')] }), assetGet: async (args) => assetReply(args),
  });
  const refs = board.mount(root);
  await refs.ready;
  assert.ok(root.querySelector('[data-testid="dashboard-report-latest"]'));
  assert.equal(root.querySelector('[data-testid="dashboard-report-list"]'), null);
  assert.ok(root.querySelector('.slot-asset-report'));
});

test('history selection uses the listed owner, template title, and existing renderer without another list call', async () => {
  const root = container();
  const fixture = fixtures.cases.find((fixture) => fixture.name.startsWith('F-B'));
  const listCalls = [];
  const getCalls = [];
  const rendered = [];
  const board = createBoard(entry(), {
    assetList: fakeDaemon(fixture, listCalls),
    assetGet: async (args) => { getCalls.push(args); return assetReply(args); },
    renderAsset: (doc, type, body, options) => {
      rendered.push({ type, body, options });
      const node = doc.createElement('p');
      node.textContent = body.title;
      return node;
    },
  });
  const refs = board.mount(root);
  await refs.ready;
  const history = root.querySelectorAll('[data-testid="dashboard-report-list"] button');
  assert.deepEqual(Array.from(history).map((button) => button.textContent), ['Report 20261007T1300Z r0', 'Report 20261006T1300Z r2']);
  history[1].click();
  await refs.ready;
  assert.equal(listCalls.length, 1);
  assert.deepEqual(getCalls[1], { stream_id: 'local:example-owner', asset_id: 'example-report-20261006T1300Z-r02', spec_id: fixtures.descriptor.spec_id });
  assert.equal(rendered[1].type, 'report');
  assert.deepEqual(rendered[1].body, reportBody());
  assert.equal(rendered[1].options.asset.stream_id, 'local:example-owner');
  assert.equal(rendered[1].options.asset.asset_key, 'spec:example__dashboard_reports:example-report-20261006T1300Z-r02');
  assert.equal(root.querySelector('.dashboard-report-viewer h3').textContent, 'Report 20261006T1300Z r2');
});

test('each explicit refresh performs exactly one new list and no scheduled polling', async () => {
  const calls = [];
  const root = container();
  const board = createBoard(entry(), { assetList: async (args) => { calls.push(args); return { assets: [] }; }, assetGet: async () => assert.fail('empty board must not fetch a body') });
  const refs = board.mount(root);
  await refs.ready;
  await refs.refresh();
  assert.equal(calls.length, 2);
  root.querySelector('button').click();
  await refs.ready;
  assert.equal(calls.length, 3);
  await board.refresh(refs);
  assert.equal(calls.length, 4);
  board.unmount(refs);
  await refs.refresh();
  assert.equal(calls.length, 4);
});

for (const [name, response] of [
  ['transport exception', () => { throw new Error('example disconnected'); }],
  ['daemon error', () => ({ ok: false, error: 'example denied' })],
  ['missing asset array', () => ({})],
  ['outside namespace', () => ({ assets: [metadata('example-digest-20261007T1300Z')] })],
]) {
  test(`report list failure is a visible card: ${name}`, async () => {
    const root = container();
    const board = createBoard(entry(), { assetList: async () => response(), assetGet: async () => assert.fail('failed list must not fetch a body') });
    const refs = board.mount(root);
    await refs.ready;
    assert.equal(root.dataset.boardState, 'error');
    assert.match(root.querySelector('[data-testid="dashboard-board-error"]').textContent, /^Board failed to load: /);
  });
}

for (const [name, get] of [
  ['transport exception', () => { throw new Error('example disconnected'); }],
  ['daemon error', () => ({ ok: false, error: { code: 'example_not_found' } })],
  ['missing body', () => ({ asset: {} })],
  ['invalid body JSON', () => ({ asset: { content_type: 'report', body: '{example' } })],
]) {
  test(`report body failure is a visible card: ${name}`, async () => {
    const root = container();
    const board = createBoard(entry(), { assetList: async () => ({ assets: [metadata('example-report-20261007T1300Z')] }), assetGet: async () => get() });
    const refs = board.mount(root);
    await refs.ready;
    assert.equal(root.dataset.boardState, 'error');
    assert.match(root.querySelector('[data-testid="dashboard-board-error"]').textContent, /^Board failed to load: /);
  });
}

test('missing owner stream fails visibly instead of using the producer or active session', async () => {
  const row = metadata('example-report-20261007T1300Z');
  delete row.stream_id;
  const root = container();
  const board = createBoard(entry(), { assetList: async () => ({ assets: [row] }), assetGet: async () => assert.fail('must not invent an owner') });
  const refs = board.mount(root);
  await refs.ready;
  assert.equal(root.dataset.boardState, 'error');
  assert.equal(root.querySelector('[data-testid="dashboard-board-error"]').textContent, 'Board failed to load: report is missing its owner stream_id');
});

test('new refresh supersedes a slow list response and unmount stops late writes', async () => {
  const pending = deferred();
  let calls = 0;
  const root = container();
  const board = createBoard(entry(), {
    assetList: async () => ++calls === 1 ? pending.promise : { assets: [] },
    assetGet: async () => assert.fail('stale list must not fetch a body'),
  });
  const refs = board.mount(root);
  const first = refs.ready;
  await refs.refresh();
  assert.equal(root.dataset.boardState, 'empty');
  pending.resolve({ assets: [metadata('example-report-20261007T1300Z')] });
  await first;
  assert.equal(root.dataset.boardState, 'empty');
  board.unmount(refs);
  assert.equal(root.textContent, '');
  assert.equal(root.hasAttribute('data-board-state'), false);
});

test('unmount during listing discards results and errors', async () => {
  for (const rejected of [false, true]) {
    const pending = deferred();
    const root = container();
    const board = createBoard(entry(), { assetList: () => pending.promise, assetGet: async () => assert.fail('unmounted board must not fetch') });
    const refs = board.mount(root);
    board.unmount(refs);
    if (rejected) pending.reject(new Error('example disconnected'));
    else pending.resolve({ assets: [metadata('example-report-20261007T1300Z')] });
    await refs.ready;
    assert.equal(root.textContent, '');
    assert.equal(root.hasAttribute('data-board-state'), false);
  }
});

test('newer history selection wins over a slower body and detached controls cannot fetch', async () => {
  const first = deferred();
  const rows = [metadata('example-report-20261007T1300Z'), metadata('example-report-20261006T1300Z')];
  let calls = 0;
  const root = container();
  const board = createBoard(entry(), {
    assetList: async () => ({ assets: rows }),
    assetGet: async (args) => ++calls === 1 ? first.promise : assetReply(args, { body: reportBody('Example newer selection') }),
  });
  const refs = board.mount(root);
  const initial = refs.ready;
  await new Promise((resolve) => setImmediate(resolve));
  const history = root.querySelectorAll('[data-testid="dashboard-report-list"] button');
  history[1].click();
  await refs.ready;
  first.resolve(assetReply({ asset_id: rows[0].asset_id }, { body: reportBody('Example obsolete selection') }));
  await initial;
  assert.equal(root.querySelector('.slot-asset-report-header h1').textContent, 'Example newer selection');
  assert.equal(root.querySelector('.dashboard-report-viewer').dataset.assetId, rows[1].asset_id);
  board.unmount(refs);
  history[0].click();
  await refs.ready;
  assert.equal(calls, 2);
  assert.equal(root.textContent, '');
});

test('refresh and unmount invalidate a pending body result before calling the renderer', async () => {
  for (const dispose of [false, true]) {
    const body = deferred();
    let lists = 0;
    const root = container();
    const board = createBoard(entry(), {
      assetList: async () => ({ assets: ++lists === 1 ? [metadata('example-report-20261007T1300Z')] : [] }),
      assetGet: () => body.promise,
      renderAsset: () => assert.fail('stale body must not render'),
    });
    const refs = board.mount(root);
    const first = refs.ready;
    await new Promise((resolve) => setImmediate(resolve));
    if (dispose) board.unmount(refs);
    else await refs.refresh();
    body.resolve(assetReply({ asset_id: 'example-report-20261007T1300Z' }));
    await first;
    assert.equal(root.dataset.boardState, dispose ? undefined : 'empty');
  }
});

test('browser script exports the API and resolves the existing renderer after app boot', async () => {
  const dom = new JSDOM('<!doctype html><div id="board"></div>', { runScripts: 'outside-only' });
  for (const name of ['report_board', 'catalog_loader']) {
    const source = fs.readFileSync(path.join(__dirname, `../renderer/dashboards/${name}.js`), 'utf8');
    vm.runInContext(source, dom.getInternalVMContext());
  }
  assert.equal(typeof dom.window.DashboardReportBoard.createBoard, 'function');
  const root = dom.window.document.getElementById('board');
  dom.window.cc = { assetList: async () => ({ assets: [metadata('example-report-20261007T1300Z')] }), assetGet: async (args) => assetReply(args) };
  const { renderAsset } = require('../renderer/asset_render');
  dom.window.PentacleAssetRender = { renderAsset };
  const refs = dom.window.DashboardReportBoard.createBoard(entry()).mount(root);
  await refs.ready;
  assert.equal(root.dataset.boardState, 'ready');
  assert.equal(root.querySelector('.slot-asset-report-header h1').textContent, 'Example body');
});
