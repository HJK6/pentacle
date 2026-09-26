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
