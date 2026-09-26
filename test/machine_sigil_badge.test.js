'use strict';
// spec_pentacle__web_machine_sigil_icons_2026_09. The sidebar rows and host
// filter render the per-machine sigil icon (mobile MachineSigil family) instead
// of a T/B/A/M letter, keyed to the host's configured color; when the cosmic
// bundle is unavailable it degrades to the initial letter. Full DOM "no letter
// in sidebar/filter" is asserted by the headless web gate (see spec Validation).

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const hostPresentation = require('../renderer/host_presentation');

function loadFn(windowStub) {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const start = source.indexOf('function machineSigilMarkup(');
  const code = source.slice(start, source.indexOf('\n}', start) + 2);
  // The config-trio target shape: local/thoth yellow (ibis), amaterasu red (sun),
  // merlin royal-blue (mage). Uses generic + fleet ids to prove the mapping.
  const CONFIG = {
    chatStream: { localHost: 'thoth', hosts: ['thoth', 'amaterasu', 'merlin'],
      hostMap: { local: 'thoth', thoth: 'thoth', amaterasu: 'amaterasu', merlin: 'merlin' } },
    hostColors: { local: 'yellow', thoth: 'yellow', amaterasu: 'red', merlin: 'royal-blue' },
  };
  const context = {
    window: windowStub,
    CONFIG,
    HOST_IDS: CONFIG.chatStream.hosts,
    hostPresentation,
    esc: (s) => String(s),
    getSourceInitial: (s) => hostPresentation.initial(s),
  };
  vm.runInNewContext(code, context);
  return context.machineSigilMarkup;
}

// A cosmic stub that records the kind/color it was asked for and returns a real
// SVG element (via JSDOM) like the built bundle does.
function cosmicStub() {
  const doc = new JSDOM('<!doctype html>').window.document;
  const calls = [];
  return {
    calls,
    PentacleCosmic: {
      machineSigil(kind, opts) {
        calls.push({ kind, opts });
        const el = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
        el.setAttribute('data-kind', kind);
        return el;
      },
    },
  };
}

test('sidebar/filter badge renders the mobile sigil kind for the configured color', () => {
  const cosmic = cosmicStub();
  const markup = loadFn(cosmic);
  const out = markup('local', 'Thoth');
  assert.match(out, /<svg/, 'renders an svg, not a letter');
  assert.match(out, /machine-sigil/, 'carries the machine-sigil class');
  assert.match(out, /aria-hidden="true"/);
  // local resolves to yellow -> ibis (the Thoth sigil), in currentColor.
  assert.equal(cosmic.calls[0].kind, 'ibis');
  assert.equal(cosmic.calls[0].opts.size, 15);
  assert.equal(cosmic.calls[0].opts.color, 'currentColor');
});

test('each configured host maps to its mobile sigil kind', () => {
  const cosmic = cosmicStub();
  const markup = loadFn(cosmic);
  markup('amaterasu', 'Amaterasu');
  markup('merlin', 'Merlin');
  assert.equal(cosmic.calls[0].kind, 'sun', 'amaterasu (red) -> sun');
  assert.equal(cosmic.calls[1].kind, 'mage', 'merlin (royal-blue) -> mage');
});

test('degrades to the initial letter when the cosmic bundle is unavailable', () => {
  const markup = loadFn({}); // no window.PentacleCosmic
  assert.equal(markup('thoth', 'Thoth'), 'T');
  assert.equal(markup('amaterasu', 'Amaterasu'), 'A');
});

test('degrades to the letter if machineSigil throws', () => {
  const markup = loadFn({ PentacleCosmic: { machineSigil() { throw new Error('boom'); } } });
  assert.equal(markup('merlin', 'Merlin'), 'M');
});

test('machine stats reuse configured sigils with accessible labels and retain data/state', () => {
  const source = fs.readFileSync(require.resolve('../renderer/app.js'), 'utf8');
  const start = source.indexOf('function renderHostsStats(');
  const code = source.slice(start, source.indexOf('\n}', start) + 2);
  const document = new JSDOM('<div id="machine-stats-section"></div><div id="machine-stats-footer"></div>').window.document;
  const cosmic = cosmicStub();
  const names = { thoth: 'Thoth', amaterasu: 'Amaterasu', merlin: 'Merlin' };
  const colors = { thoth: 'yellow', amaterasu: 'red', merlin: 'royal-blue' };
  const context = {
    document, state: { chatStream: { hostsStats: {} } }, HOST_IDS: Object.keys(names),
    streamHostForHostId: id => id === 'local' ? 'thoth' : id,
    _streamHostToHostId: id => id, getSourceForSession: (_, id) => names[id],
    getSourceColorForSession: (_, id) => colors[id], esc: s => String(s),
    machineSigilMarkup: loadFn(cosmic), statUsagePct: (used, total) => used / total * 100,
    machineStatsIsStale: stats => stats.stale, fmtStatLoad: String, fmtStatPct: n => n + '%',
    fmtStatBytes: String, fmtStatUptime: String, usageBarClass: () => 'low',
  };
  vm.runInNewContext(code, context);
  const stats = stale => ({ cpu_load_1m: 0.42, memory_used_bytes: 4, memory_total_bytes: 8,
    disk_used_bytes: 64, disk_total_bytes: 256, uptime_seconds: 123, stale });
  context.renderHostsStats({ merlin: stats(false), thoth: stats(false), amaterasu: stats(true) });
  const cards = [...document.querySelectorAll('.machine-stat-card')];
  assert.deepEqual(cards.map(e => e.dataset.machineStatsHost), ['thoth', 'amaterasu', 'merlin']);
  assert.deepEqual(cosmic.calls.map(c => c.kind), ['ibis', 'sun', 'mage']);
  for (const card of cards) {
    const host = card.dataset.machineStatsHost;
    const mark = card.querySelector('.machine-stat-mark');
    assert.equal(mark.title, names[host]);
    assert.equal(mark.getAttribute('aria-label'), names[host]);
    assert.ok(mark.classList.contains('color-' + colors[host]));
    assert.equal(mark.querySelector('svg').getAttribute('aria-hidden'), 'true');
    assert.equal(card.querySelector('.machine-stat-name').textContent, names[host]);
    assert.equal(card.querySelector('.machine-stat-state').textContent, host === 'amaterasu' ? 'Stale' : 'Live');
    assert.match(card.textContent, /RAM50%/);
    assert.match(card.textContent, /Storage25%/);
    assert.match(card.textContent, /Load 1m0.42/);
  }
});
