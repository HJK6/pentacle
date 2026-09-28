const test = require('node:test');
const assert = require('node:assert/strict');
const { JSDOM } = require('jsdom');

function snapshot(label = 'Gate A') {
  const updated = '2026-07-16T12:00:00Z';
  return {
    schema_version: 1,
    generated_at: updated,
    _updated_at: updated,
    _data_stale: false,
    _transport_stale: false,
    panels: {
      now_running: { status: 'ok', updated_at: updated, lanes: [{ title: label, state: 'working', phase: 'qa', last_update: 'running' }], sim_queue: { resource: 'ios-simulator', holder: null, queue: [] }, gate_processes: [{ pid: 7, kind: 'full-gate', elapsed: '00:05', artifact_dir: '/tmp/gate' }] },
      latest_gate_runs: { status: 'ok', updated_at: updated, items: [{ run_id: 'run-1', sha: 'a'.repeat(40), status: 'passed', started_at: updated, artifact_path: '/tmp/run-1' }] },
      whats_left: { status: 'ok', updated_at: updated, state_counts: { in_progress: 2 }, unchecked_acceptance: ['Ship dashboard'], items: [{ status: 'in_progress', title: 'Dashboard' }] },
      time_estimates: { status: 'ok', updated_at: updated, estimate: { total_estimate_ms: 5000 }, last_actual_ms: 4800, estimate_error_ms: -200, stage_medians: [{ stage: 'jest', samples: 2, median_ms: 1000, last_ms: 900 }] },
      per_test_analytics: { status: 'ok', updated_at: updated, items: [{ test_id: 'suite::slow', samples: 2, median_ms: 500, p90_ms: 700, last_ms: 600, estimate_error_ms: 100 }] },
      recent_closures: { status: 'ok', updated_at: updated, items: [{ completed_at: '2026-07-16', title: 'Previous lane' }] },
    },
  };
}

function loadDashboard(responses = [snapshot()]) {
  const dom = new JSDOM('<!doctype html><main id="root"></main>', { url: 'http://pentacle.test/', runScripts: 'outside-only' });
  global.window = dom.window;
  global.document = dom.window.document;
  dom.window.DASHBOARDS = [];
  dom.window.HOST = { hasDashboardHub: true };
  let index = 0;
  dom.window.cc = { getPentacleMobileTestingStats: async () => responses[Math.min(index++, responses.length - 1)] };
  const path = '../renderer/dashboards/pentacle-mobile-testing';
  delete require.cache[require.resolve(path)];
  const mod = require(path);
  const entry = dom.window.DASHBOARDS.find((item) => item.id === 'pentacle-mobile-testing');
  return { dom, mod, entry, container: dom.window.document.getElementById('root') };
}

function teardown() {
  delete global.window;
  delete global.document;
}

test('self-registers the exact cache-read contract', () => {
  const harness = loadDashboard();
  assert.ok(harness.entry);
  assert.equal(harness.entry.name, 'Pentacle Mobile Testing');
  assert.equal(harness.entry.pollInterval, 1000);
  assert.equal(harness.entry.mount, harness.mod.mount);
  assert.equal(harness.entry.update, harness.mod.update);
  teardown();
});

test('mount and update render all six panels', () => {
  const harness = loadDashboard();
  const refs = harness.mod.mount(harness.container);
  harness.mod.update(refs, snapshot());
  assert.equal(harness.container.querySelectorAll('.pmt-panel').length, 6);
  const text = harness.container.textContent;
  ['Now Running', 'Latest Gate Runs', "What's Left", 'Time Estimates', 'Per-Test Analytics', 'Recent Closures'].forEach((title) => assert.match(text, new RegExp(title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))));
  assert.match(text, /Gate A/);
  assert.match(text, /passed/);
  assert.match(text, /suite::slow/);
  harness.mod.unmount(refs);
  assert.equal(harness.container.textContent, '');
  teardown();
});

test('unreadable source is UNKNOWN and never stalled', () => {
  const harness = loadDashboard();
  const refs = harness.mod.mount(harness.container);
  const data = snapshot();
  data.panels.now_running = { status: 'unavailable', updated_at: data.generated_at, error_codes: ['sim_queue_command_failed'], lanes: [], gate_processes: [], sim_queue: null };
  harness.mod.update(refs, data);
  assert.match(harness.container.textContent, /UNKNOWN — source unreadable/);
  assert.doesNotMatch(harness.container.textContent.toLowerCase(), /stalled/);
  teardown();
});

test('pollFn observes cache replacement within the one-second registry cadence', async () => {
  const harness = loadDashboard([snapshot('First test'), snapshot('Second test')]);
  const refs = harness.mod.mount(harness.container);
  harness.mod.update(refs, await harness.entry.pollFn());
  assert.match(harness.container.textContent, /First test/);
  harness.mod.update(refs, await harness.entry.pollFn());
  assert.match(harness.container.textContent, /Second test/);
  assert.doesNotMatch(harness.container.textContent, /First test/);
  assert.equal(harness.entry.pollInterval, 1000);
  teardown();
});

test('hostile payload strings remain inert text', () => {
  const harness = loadDashboard();
  const refs = harness.mod.mount(harness.container);
  const attack = '<img src=x onerror="window.PWNED=true"><script>window.PWNED=true</script>';
  const data = snapshot(attack);
  data.panels.whats_left.unchecked_acceptance = [attack];
  data.panels.latest_gate_runs.items[0].artifact_path = attack;
  harness.mod.update(refs, data);
  assert.equal(harness.container.querySelector('img'), null);
  assert.equal(harness.container.querySelector('script'), null);
  assert.equal(harness.dom.window.PWNED, undefined);
  assert.match(harness.container.textContent, /<img src=x/);
  teardown();
});

test('renderer independently enforces producer table and list caps', () => {
  const harness = loadDashboard();
  const refs = harness.mod.mount(harness.container);
  const data = snapshot();
  data.panels.now_running.lanes = Array.from({ length: 60 }, (_, index) => ({ title: `lane-${index}` }));
  data.panels.whats_left.unchecked_acceptance = Array.from({ length: 110 }, (_, index) => `criterion-${index}`);
  data.panels.per_test_analytics.items = Array.from({ length: 30 }, (_, index) => ({ test_id: `test-${index}` }));
  harness.mod.update(refs, data);
  assert.equal(harness.container.querySelector('.pmt-now-running tbody').children.length, 50);
  assert.equal(harness.container.querySelector('.pmt-whats-left .pmt-list').children.length, 100);
  assert.equal(harness.container.querySelector('.pmt-per-test-analytics tbody').children.length, 20);
  teardown();
});

test('missing envelope clears prior values and renders reader error', () => {
  const harness = loadDashboard();
  const refs = harness.mod.mount(harness.container);
  harness.mod.update(refs, snapshot());
  harness.mod.update(refs, { error: 'no data yet from hub', _data_stale: true, _transport_stale: true });
  assert.match(harness.container.textContent, /no data yet from hub/);
  assert.doesNotMatch(harness.container.textContent, /Gate A/);
  teardown();
});

test('registry-compatible poll result clears prior values on reader error', async () => {
  const harness = loadDashboard([snapshot(), { error: 'malformed hub envelope' }]);
  const refs = harness.mod.mount(harness.container);
  harness.mod.update(refs, await harness.entry.pollFn());
  assert.match(harness.container.textContent, /Gate A/);
  const errorData = await harness.entry.pollFn();
  assert.equal(errorData.error, undefined);
  harness.mod.update(refs, errorData);
  assert.match(harness.container.textContent, /malformed hub envelope/);
  assert.doesNotMatch(harness.container.textContent, /Gate A/);
  teardown();
});
